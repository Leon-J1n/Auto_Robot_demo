"""Contract tests for the Agent Service HTTP surface.

These assert the shapes the Gateway's `AgentServiceClient` already depends on
(`web/gateway/agent.py`), so a change that would break the Web panel fails here first. The
runner is stubbed: nothing in this file needs a simulator or a model.

Async tests follow the repo convention in `web/tests/test_gateway.py` -- a sync test wrapping
`asyncio.run`, since pytest-asyncio is not a dependency.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.api import create_app  # noqa: E402
from agent_service.runner import (EmitEvent, TURN_DEBUG, TURN_PROGRESS, TurnBlocked,  # noqa: E402
                                  TurnOutcome, TurnRequest)
from agent_service.service import ConversationService  # noqa: E402
from agent_service.store import ConversationStore  # noqa: E402


class StubRunner:
    def __init__(self, *, outcome: TurnOutcome | None = None,
                 blocked: TurnBlocked | None = None, delay: float = 0.0,
                 model_state: str = "ok"):
        self.outcome = outcome or TurnOutcome(status="succeeded", message="完成。场景同步完成。")
        self.blocked = blocked
        self.delay = delay
        self.model_state = model_state
        self.seen: list[TurnRequest] = []

    def model_status(self) -> dict[str, object]:
        return {"status": self.model_state}

    async def run(self, request: TurnRequest, emit: EmitEvent) -> TurnOutcome:
        self.seen.append(request)
        await emit(TURN_PROGRESS, {"phase": "scene"})
        await emit(TURN_DEBUG, {"plan": [{"title": "检查场景", "status": "completed"}]})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.blocked is not None:
            raise self.blocked
        return self.outcome


@asynccontextmanager
async def _client(runner: StubRunner | None = None) -> AsyncIterator[TestClient]:
    service = ConversationService(ConversationStore(None), runner or StubRunner())  # type: ignore[arg-type]
    client = TestClient(TestServer(create_app(service)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def _conversation(client: TestClient, **body: object) -> str:
    response = await client.post("/v1/conversations", json=body or {})
    assert response.status == 201
    return str((await response.json())["conversationId"])


async def _drain(client: TestClient, conversation_id: str, turn_id: str) -> dict:
    """Poll a turn to a terminal status the way the Gateway client does."""
    for _ in range(300):
        response = await client.get(f"/v1/conversations/{conversation_id}/turns/{turn_id}")
        assert response.status == 200
        turn = await response.json()
        if turn["status"] not in {"queued", "running"}:
            return turn
        await asyncio.sleep(0.02)
    raise AssertionError("turn never reached a terminal status")


async def _submit(client: TestClient, conversation_id: str, *, request_id: str,
                  message: str = "go", debug: bool = False,
                  control: dict | None = None) -> dict:
    body: dict = {"requestId": request_id, "message": message, "debug": debug}
    if control is not None:
        body["controlContext"] = control
    response = await client.post(f"/v1/conversations/{conversation_id}/turns", json=body)
    assert response.status == 202
    return await response.json()


LIVE = {"liveEndpoint": "tcp://127.0.0.1:9100"}


# -- health ---------------------------------------------------------------------------

async def _exercise_health() -> None:
    async with _client() as client:
        body = await (await client.get("/health")).json()
        assert body["status"] == "ok"
    # Health stays ok when the model is unconfigured, so the Gateway can tell "online but
    # unconfigured" from "down".
    async with _client(StubRunner(model_state="MODEL_UNAVAILABLE")) as client:
        body = await (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["model"]["status"] == "MODEL_UNAVAILABLE"


def test_health_reports_ok_even_without_a_model() -> None:
    asyncio.run(_exercise_health())


# -- conversations --------------------------------------------------------------------

async def _exercise_conversation_lifecycle() -> None:
    async with _client() as client:
        conversation_id = await _conversation(
            client, target="platform",
            sceneBinding={"sceneId": "main", "simSessionId": "sess-1",
                          "sceneGeneration": 3, "connect": "tcp://127.0.0.1:9100"},
            presentationMode="sync-and-animate")

        listed = await (await client.get("/v1/conversations?target=platform")).json()
        assert [item["conversationId"] for item in listed] == [conversation_id]

        detail = await (await client.get(f"/v1/conversations/{conversation_id}")).json()
        assert detail["sceneBinding"]["sceneGeneration"] == 3
        assert detail["presentationMode"] == "sync-and-animate"

        assert (await client.delete(f"/v1/conversations/{conversation_id}")).status == 200
        assert (await client.get(f"/v1/conversations/{conversation_id}")).status == 404


def test_conversation_lifecycle() -> None:
    asyncio.run(_exercise_conversation_lifecycle())


async def _exercise_missing_conversation() -> None:
    async with _client() as client:
        response = await client.get("/v1/conversations/missing")
        assert response.status == 404
        # The Gateway client raises RuntimeError(body["detail"]), so that key must exist.
        assert "detail" in await response.json()


def test_unknown_conversation_returns_detail() -> None:
    asyncio.run(_exercise_missing_conversation())


# -- turns ----------------------------------------------------------------------------

async def _exercise_successful_turn() -> None:
    async with _client() as client:
        conversation_id = await _conversation(client)
        turn = await _submit(
            client, conversation_id, request_id="req-1", message="把红色方块放到托盘里",
            debug=True,
            control={"gatewayBaseUrl": "http://127.0.0.1:8080", "controlToken": "secret",
                     "jobId": "job-1", **LIVE})
        result = await _drain(client, conversation_id, turn["turnId"])
        assert result["status"] == "succeeded"
        assert result["message"].endswith("场景同步完成。")

        detail = await (await client.get(f"/v1/conversations/{conversation_id}")).json()
        assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
        # The raw user text is the user message; control fields are never echoed as one, and
        # the control token must not leak into any user-visible payload.
        assert detail["messages"][0]["content"] == "把红色方块放到托盘里"
        assert "secret" not in str(detail)


def test_turn_succeeds_and_records_messages() -> None:
    asyncio.run(_exercise_successful_turn())


async def _exercise_idempotent_turn() -> None:
    async with _client() as client:
        conversation_id = await _conversation(client)
        first = await _submit(client, conversation_id, request_id="same", control=LIVE)
        await _drain(client, conversation_id, first["turnId"])
        second = await _submit(client, conversation_id, request_id="same", control=LIVE)
        assert second["turnId"] == first["turnId"]


def test_turn_is_idempotent_on_request_id() -> None:
    asyncio.run(_exercise_idempotent_turn())


async def _exercise_blocked_turn() -> None:
    runner = StubRunner(blocked=TurnBlocked("SESSION_NOT_BOUND", "没有绑定的仿真会话。"))
    async with _client(runner) as client:
        conversation_id = await _conversation(client)
        turn = await _submit(client, conversation_id, request_id="req-blocked")
        result = await _drain(client, conversation_id, turn["turnId"])
        # A blocker is explicit, never a silent degrade.
        assert result["status"] == "failed"
        assert result["errorCode"] == "SESSION_NOT_BOUND"


def test_blocked_turn_fails_with_explicit_code() -> None:
    asyncio.run(_exercise_blocked_turn())


async def _exercise_cancel() -> None:
    async with _client(StubRunner(delay=5.0)) as client:
        conversation_id = await _conversation(client)
        turn = await _submit(client, conversation_id, request_id="req-cancel", control=LIVE)
        await asyncio.sleep(0.05)
        response = await client.post(
            f"/v1/conversations/{conversation_id}/turns/{turn['turnId']}/cancel")
        assert response.status == 200
        assert (await response.json())["status"] == "cancelled"


def test_cancel_marks_turn_cancelled() -> None:
    asyncio.run(_exercise_cancel())


# -- events ---------------------------------------------------------------------------

async def _exercise_events() -> None:
    async with _client() as client:
        conversation_id = await _conversation(client)
        turn = await _submit(client, conversation_id, request_id="req-debug", debug=True,
                             control=LIVE)
        await _drain(client, conversation_id, turn["turnId"])

        events = await (await client.get(
            f"/v1/conversations/{conversation_id}/events?after=0&limit=200")).json()
        assert events
        ids = [event["eventId"] for event in events]
        # The Gateway pages with `after=<eventId>`, so ids must be monotonic.
        assert ids == sorted(ids) and all(value > 0 for value in ids)
        debug = [event for event in events if event["type"] == TURN_DEBUG]
        # The panel mines a plan/steps array out of the debug payload; without one the todo
        # rows stay empty.
        assert debug and isinstance(debug[0]["payload"]["plan"], list)
        progress = [event for event in events if event["type"] == TURN_PROGRESS]
        assert progress and progress[0]["payload"]["label"] == "正在检查场景"

        after = await (await client.get(
            f"/v1/conversations/{conversation_id}/events?after={ids[-1]}")).json()
        assert after == []


def test_events_are_monotonic_and_carry_plan_rows() -> None:
    asyncio.run(_exercise_events())


async def _exercise_debug_suppression() -> None:
    async with _client() as client:
        conversation_id = await _conversation(client)
        turn = await _submit(client, conversation_id, request_id="req-quiet", debug=False,
                             control=LIVE)
        await _drain(client, conversation_id, turn["turnId"])
        events = await (await client.get(
            f"/v1/conversations/{conversation_id}/events?limit=200")).json()
        assert not [event for event in events if event["type"] == TURN_DEBUG]


def test_debug_events_suppressed_without_debug_flag() -> None:
    asyncio.run(_exercise_debug_suppression())


# -- recovery -------------------------------------------------------------------------

def test_restart_marks_running_turns_interrupted() -> None:
    store = ConversationStore(None)
    conversation_id = store.create_conversation()
    turn_id = store.create_turn(conversation_id, "req-1")
    store.update_turn(turn_id, status="running")
    service = ConversationService(store, StubRunner())  # type: ignore[arg-type]
    assert service.recover() == 1
    # A turn that was mid-flight may already have changed the simulation, so it is marked
    # interrupted and never blindly replayed.
    row = store.turn(turn_id)
    assert row is not None and row["status"] == "interrupted"
