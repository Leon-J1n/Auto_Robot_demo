from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.planning.contracts import (  # noqa: E402
    GroundedEntity,
    GroundedTask,
)
from agent_service.planning.task_understanding import (  # noqa: E402
    ParseEntity,
    ParseOperation,
    TaskParseLLMOutput,
)
from agent_service.qwen_runner import PlatformAgentRunner  # noqa: E402
from agent_service.runner import TurnBlocked, TurnOutcome, TurnRequest  # noqa: E402
from agent_service.service import ConversationService  # noqa: E402
from agent_service.store import ConversationStore  # noqa: E402


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Provider:
    def __init__(self) -> None:
        self.understand_calls = 0
        self.plan_calls = 0

    def understand(self, _request):
        self.understand_calls += 1
        return TaskParseLLMOutput(
            status="accepted",
            entities=[ParseEntity(id="banana", name="香蕉", category="object")],
            operations=[ParseOperation(type="move", target="banana")],
        )

    def plan(self, _request):
        self.plan_calls += 1
        raise AssertionError("the deterministic move recipe must not call Qwen planning")


class _ReuseRunner(PlatformAgentRunner):
    def __post_init__(self) -> None:
        pass

    def _connect(self, _endpoint: str):
        return _Closable()

    def _resolve_scene(self, _session, _intent):
        return SimpleNamespace(), None

    def _build_runtime(self, _session):
        return _Closable()

    def _ground(self, intent, _plan):
        return GroundedTask(
            instruction=intent.instruction,
            task_types=intent.task_types,
            entities=[GroundedEntity(
                entity_id="banana", semantic_name="香蕉", object_id="banana_01",
                grounding_method="asset_scene_binding")],
            operations=intent.operations,
            spatial_relations=[],
            scene_id="main",
        )

    def _compile(self, _skill_plan, _task, _plan, _workdir):
        return SimpleNamespace(commands=[])

    def _execute(self, _runtime, _commands):
        return SimpleNamespace(success=True, frames=[], segments=[], affected_objects=[])


def _request(conversation_id: str, message: str,
             model_context: dict | None = None) -> TurnRequest:
    return TurnRequest(
        conversation_id=conversation_id,
        turn_id=f"turn-{conversation_id}-{message[:5]}",
        request_id=f"request-{conversation_id}-{message[:5]}",
        message=message,
        control_context=SimpleNamespace(liveEndpoint="ws://bound-session"),
        model_context=model_context or {},
    )


async def _run(runner: PlatformAgentRunner, request: TurnRequest) -> TurnOutcome:
    async def emit(_name: str, _payload: dict) -> None:
        pass

    return await runner.run(request, emit)


def test_first_debug_turn_fails_without_calling_qwen() -> None:
    provider = _Provider()
    runner = _ReuseRunner(provider=provider)
    request = _request("conversation-1", "DEBUG repeat")

    try:
        asyncio.run(_run(runner, request))
    except TurnBlocked as error:
        assert error.code == "MODEL_CONTEXT_MISSING"
    else:
        raise AssertionError("DEBUG without a cached result must fail")

    assert provider.understand_calls == 0
    assert provider.plan_calls == 0


def test_normal_turn_updates_cache_and_debug_reuses_it_without_qwen_calls() -> None:
    provider = _Provider()
    runner = _ReuseRunner(provider=provider)
    normal = _request("conversation-1", "移动香蕉")

    outcome = asyncio.run(_run(runner, normal))

    assert outcome.status == "succeeded"
    assert normal.model_context_updated is True
    assert set(normal.model_context) >= {"version", "instruction", "understanding"}
    assert "planning" not in normal.model_context
    assert (provider.understand_calls, provider.plan_calls) == (1, 0)

    debug = _request("conversation-2", "DEBUG rerun", normal.model_context)
    repeated = asyncio.run(_run(runner, debug))

    assert repeated.status == "succeeded"
    assert (provider.understand_calls, provider.plan_calls) == (1, 0)
    assert debug.model_context_updated is False


class _ContextRunner:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, dict]] = []

    async def run(self, request: TurnRequest, _emit) -> TurnOutcome:
        self.seen.append((request.conversation_id, request.message,
                          dict(request.model_context)))
        if not request.message.startswith("DEBUG"):
            request.model_context["owner"] = request.conversation_id
            request.model_context_updated = True
        return TurnOutcome(status="succeeded", message="ok")


async def _wait_for_turn(service: ConversationService, conversation_id: str,
                         turn_id: str) -> None:
    for _ in range(100):
        if service.turn_view(conversation_id, turn_id).status not in {"queued", "running"}:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


def test_model_cache_is_global_and_survives_store_reopen() -> None:
    async def exercise() -> list[tuple[str, str, dict]]:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "conversations.db"
            first_runner = _ContextRunner()
            first_store = ConversationStore(database)
            first_service = ConversationService(first_store, first_runner)  # type: ignore[arg-type]
            first = first_service.create_conversation()
            second = first_service.create_conversation()
            try:
                normal = first_service.submit_turn(
                    first, request_id="normal", message="移动香蕉")
                await _wait_for_turn(first_service, first, normal.turnId)
                cross_conversation = first_service.submit_turn(
                    second, request_id="cross", message="DEBUG rerun")
                await _wait_for_turn(first_service, second, cross_conversation.turnId)
            finally:
                await first_service.close()
                first_store.close()

            second_runner = _ContextRunner()
            second_store = ConversationStore(database)
            second_service = ConversationService(second_store, second_runner)  # type: ignore[arg-type]
            restarted = second_service.create_conversation()
            try:
                reused = second_service.submit_turn(
                    restarted, request_id="restart", message="DEBUG rerun")
                await _wait_for_turn(second_service, restarted, reused.turnId)
                return first_runner.seen + second_runner.seen
            finally:
                await second_service.close()
                second_store.close()

    seen = asyncio.run(exercise())

    assert seen[1][2] == {"owner": seen[0][0]}
    assert seen[2][2] == {"owner": seen[0][0]}
