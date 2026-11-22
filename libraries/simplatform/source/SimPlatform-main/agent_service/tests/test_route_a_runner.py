from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.qwen_runner import PlatformAgentRunner  # noqa: E402
from agent_service.runner import TurnRequest  # noqa: E402


class _Runtime:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *, physics: str = "none", collision: str = "fcl") -> None:
        self.physics = physics
        self.collision = collision
        self.apply_calls: list[dict[str, str]] = []
        self.closed = False

    def backend_status(self) -> dict[str, str]:
        return {"physics": self.physics, "collision": self.collision}

    def apply_backends(self, **values: str) -> dict[str, str | bool]:
        self.apply_calls.append(dict(values))
        changed = self.physics != values["physics"] or self.collision != values["collision"]
        self.physics = values["physics"]
        self.collision = values["collision"]
        return {"physics": self.physics, "collision": self.collision, "changed": changed}

    def close(self) -> None:
        self.closed = True


class _RouteARunner(PlatformAgentRunner):
    def __post_init__(self) -> None:
        self.publish_calls: list[list[dict]] = []
        self.snapshot_calls = 0
        self.runtime = _Runtime()
        self.session = _Session()

    def _connect(self, _endpoint: str):
        return self.session

    def _understand(self, _message: str):
        return SimpleNamespace()

    def _resolve_scene(self, _session, _intent):
        return SimpleNamespace(), SimpleNamespace(objects={})

    def _build_runtime(self, _session):
        return self.runtime

    def _ground(self, _intent, _plan):
        return SimpleNamespace()

    def _skill_plan(self, _task):
        return SimpleNamespace()

    def _compile(self, _skill_plan, _task, _plan, _workdir):
        return SimpleNamespace()

    def _execute(self, _runtime, _commands):
        return SimpleNamespace(
            success=True,
            frames=[
                {"simulationTime": 0.0, "entities": [
                    {"id": "robot", "robot": {"positions": [0.0]}}]},
                {"simulationTime": 0.0, "entities": [
                    {"id": "robot", "robot": {"positions": [0.5]}}]},
            ],
            segments=[],
            affected_objects=["red_ball_01"],
        )

    async def _publish_snapshot(self, _context, _emit):
        self.snapshot_calls += 1
        return 1

    async def _publish(self, _context, _session, frames, _segments, _emit):
        self.publish_calls.append(list(frames))
        return self.snapshot_calls + len(self.publish_calls)

    def _reply(self, _intent, _task, _segments, _frame_count, _applied):
        return "done"


def test_route_a_publishes_composed_scene_before_execution_timeline() -> None:
    runner = _RouteARunner()
    request = TurnRequest(
        conversation_id="conversation-1",
        turn_id="turn-1",
        request_id="request-1",
        message="将红色球放入绿色盒子中。",
        control_context=SimpleNamespace(liveEndpoint="ws://bound-session"),
    )

    async def exercise():
        async def emit(_name: str, _payload: dict) -> None:
            pass

        return await runner.run(request, emit)

    outcome = asyncio.run(exercise())

    assert outcome.status == "succeeded"
    assert outcome.presentation_revision == 2
    assert runner.snapshot_calls == 1
    assert len(runner.publish_calls) == 1
    assert len(runner.publish_calls[0]) == 2
    assert runner.publish_calls[0][1]["simulationTime"] > runner.publish_calls[0][0]["simulationTime"]
    assert runner.runtime.closed is True
    assert runner.session.apply_calls == []


def test_runtime_backend_setup_enables_mujoco_fcl() -> None:
    runner = PlatformAgentRunner.__new__(PlatformAgentRunner)
    session = _Session()

    runner._ensure_runtime_backends(session)

    assert session.apply_calls == [{"physics": "mujoco", "collision": "fcl"}]


def test_runtime_backend_setup_keeps_existing_mujoco_fcl_world() -> None:
    runner = PlatformAgentRunner.__new__(PlatformAgentRunner)
    session = _Session(physics="mujoco", collision="fcl")

    runner._ensure_runtime_backends(session)

    assert session.apply_calls == []


class _FailedRouteARunner(_RouteARunner):
    def _execute(self, _runtime, _commands):
        frame = {"simulationTime": 0.0, "entities": [
            {"id": "robot", "robot": {"positions": [0.0]}}]}
        return SimpleNamespace(
            success=False,
            frames=[dict(frame), dict(frame)],
            segments=[],
            affected_objects=["red_ball_01"],
            commands_total=3,
            commands_completed=1,
            steps=[{"commandId": "command-2", "status": "failed"}],
            failure={"error_code": "PATH_BLOCKED", "message": "target is unreachable"},
        )


def test_route_a_failed_execution_preserves_snapshot_and_reports_failed_phase() -> None:
    runner = _FailedRouteARunner()
    request = TurnRequest(
        conversation_id="conversation-1",
        turn_id="turn-failed",
        request_id="request-failed",
        message="将红色球放入绿色盒子中。",
        control_context=SimpleNamespace(liveEndpoint="ws://bound-session"),
        debug=True,
    )
    events: list[tuple[str, dict]] = []

    async def exercise():
        async def emit(name: str, payload: dict) -> None:
            events.append((name, payload))

        return await runner.run(request, emit)

    outcome = asyncio.run(exercise())

    assert outcome.status == "failed"
    assert outcome.error_code == "PATH_BLOCKED"
    assert outcome.presentation_revision == 1
    assert runner.snapshot_calls == 1
    assert runner.publish_calls == []
    execute_debug = [payload for name, payload in events
                     if name == "turn.debug" and payload.get("phase") == "execute"]
    assert [event["status"] for event in execute_debug] == ["in_progress", "failed"]
    failed = execute_debug[-1]
    assert failed["plan"][4] == {"title": "执行动作", "status": "failed"}
    assert failed["details"]["commandsTotal"] == 3
    assert failed["details"]["commandsCompleted"] == 1
    assert failed["details"]["failure"] == {
        "error_code": "PATH_BLOCKED", "message": "target is unreachable"}
    progress = [payload for name, payload in events if name == "turn.progress"]
    assert any(item.get("phase") == "execute" and item.get("status") == "failed"
               for item in progress)
    assert runner.runtime.closed is True
