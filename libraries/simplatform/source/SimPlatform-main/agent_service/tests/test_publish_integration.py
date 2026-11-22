"""End-to-end check that published frames survive the Gateway's validation.

The trajectory requirement rests on one claim: marker entities injected into a snapshot frame
are accepted by the Gateway and reach the browser as renderable spheres. `test_trajectory.py`
tests the marker construction in isolation; this exercises the real seam — a genuine
`SceneSnapshot` read from a live session, markers injected, published through
`/api/agent-control/*` with a real per-job token, then read back through the browser-facing
timeline route.

The coupling direction matters: this lives in `agent_service` because the service depends on the
Gateway's contract. The Gateway must stay implementation-agnostic and never import this package.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from agent_service.gateway_client import (GatewayClient, MARKER_ID_BASE, TrajectorySegment,  # noqa: E402
                                          normalize_frame_times, with_trajectories)

try:
    from sim_platform.live import connect

    from web.gateway.assets import AssetService
    from web.gateway.server import LIVE_ENDPOINT_KEY, create_app
except Exception as error:  # noqa: BLE001 - the native library may be absent
    pytest.skip(f"gateway or platform runtime unavailable: {error}",
                allow_module_level=True)


class _StubAgent:
    """Stands in for the Agent Service so the Gateway mints a real control token."""

    connected = True
    endpoint = "stub://agent"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.release = asyncio.Event()

    async def probe(self) -> bool:
        return True

    async def ensure_conversation(self, **_: Any) -> str:
        return "conversation-1"

    async def execute(self, **params: Any):
        from web.gateway.agent import AgentResult

        self.calls.append(params)
        await self.release.wait()
        return AgentResult(conversation_id="conversation-1", turn_id="turn-1",
                           message="completed", status="succeeded")


def _segments() -> list[TrajectorySegment]:
    return [
        TrajectorySegment(id="segment_1",
                          points=[(0.30, 0.00, 0.70), (0.40, 0.05, 0.72),
                                  (0.50, 0.10, 0.74)]),
        TrajectorySegment(id="segment_2",
                          points=[(0.50, 0.10, 0.74), (0.55, 0.20, 0.70)]),
    ]


async def _exercise_marker_publish(tmp_path: Path) -> None:
    agent = _StubAgent()
    app = create_app(agent_client=agent,  # type: ignore[arg-type]
                     agent_state_storage=tmp_path / "agent-state.json",
                     presentation_storage=tmp_path / "presentation.json",
                     # Isolated so this never touches the repo's real upload root or the
                     # shared presentation store that other suites read.
                     asset_service=AssetService(tmp_path / "uploads"))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # A real snapshot of the Gateway's authoritative world, over Live Python.
        session = await asyncio.to_thread(connect, app[LIVE_ENDPOINT_KEY])
        try:
            native = (await asyncio.to_thread(session.scene_snapshot)).as_dict()
        finally:
            await asyncio.to_thread(session.close)

        segments = _segments()
        scene_entity_count = len(native["entities"])
        frames = normalize_frame_times(
            [with_trajectories(dict(native), segments) for _ in range(3)])
        expected_markers = sum(len(segment.points) for segment in segments)
        assert len(frames[0]["entities"]) == scene_entity_count + expected_markers
        # The Gateway prepends the World root when rebuilding browser entities, so a published
        # frame carries one more entity than was submitted.
        expected_published = 1 + scene_entity_count + expected_markers

        # A turn must own the write lease before publishing, which is what mints the token.
        await client.post("/api/agent/jobs", json={
            "requestId": "publish-1", "text": "trace a path", "sceneId": "main"})
        for _ in range(200):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        assert agent.calls, "the gateway never dispatched the turn"
        context = agent.calls[0]["control_context"]
        headers = {"X-Agent-Job-Id": str(context["jobId"]),
                   "X-Agent-Control-Token": str(context["controlToken"])}

        published = await client.post("/api/agent-control/presentation/timeline",
                                      headers=headers,
                                      json={"sourceTurnId": "turn-1", "frames": frames})
        body = await published.json()
        assert published.status == 200, body
        timeline = body["timeline"]
        assert timeline["frameCount"] == 3

        # Read a frame back the way the browser does.
        frame = await (await client.get(timeline["frames"][1]["snapshotUrl"])).json()
        markers = [entity for entity in frame["entities"]
                   if str(entity["name"]).startswith("__trajectory_")]
        assert len(markers) == expected_markers

        for marker in markers:
            # Rendered generically from visual.shape, so a marker needs no frontend change.
            assert marker["kind"] == "sphere"
            assert marker["visual"]["shape"] == "sphere"
            assert marker["flags"] == ["visual"]
            # Visual only: a drawn path must never obstruct the arm nor fall under gravity.
            assert "collider" not in marker
            assert "rigidBody" not in marker
            assert marker["id"] >= MARKER_ID_BASE

        # Each segment keeps its own colour, so the segments stay distinguishable.
        colours = {tuple(marker["visual"]["color"][:3]) for marker in markers}
        assert len(colours) == len(segments)

        # The real scene survived intact alongside the markers.
        assert len(frame["entities"]) == expected_published
        names = {entity["name"] for entity in frame["entities"]}
        assert {entity["name"] for entity in native["entities"]} <= names

        # The optimized upload must keep every sample but send markers only once.
        compact_frames = normalize_frame_times([dict(native) for _ in range(3)])
        async with GatewayClient(str(client.make_url("/")), str(context["controlToken"]),
                                 str(context["jobId"])) as uploader:
            compact_result = await uploader.publish_capture(compact_frames, segments=segments)
        data = await (await client.get(compact_result["timeline"]["dataUrl"])).json()
        assert len(data["snapshot"]["entities"]) == expected_published
        assert len(data["frames"]) == 3
        assert all(item["entities"] == [] for item in data["frames"])
        animated = [copy.deepcopy(item) for item in compact_frames]
        animated[1]["entities"][0]["world"]["translation"][0] += 0.25
        async with GatewayClient(str(client.make_url("/")), str(context["controlToken"]),
                                 str(context["jobId"])) as uploader:
            moved_result = await uploader.publish_capture(animated, segments=segments)
        moved = await (await client.get(moved_result["timeline"]["frames"][1]["snapshotUrl"])).json()
        returned = await (await client.get(moved_result["timeline"]["frames"][2]["snapshotUrl"])).json()
        object_id = native["entities"][0]["id"]
        assert next(e for e in moved["entities"] if e["id"] == object_id)["world"] == animated[1]["entities"][0]["world"]
        assert next(e for e in returned["entities"] if e["id"] == object_id)["world"] == native["entities"][0]["world"]
        # Bad binding, time, id and transform payloads are rejected atomically.
        base = with_trajectories(compact_frames[0], segments)
        valid = [{**item, "entities": []} for item in compact_frames]
        for mutate, expected_code in (
            (lambda values: values[1].update(sceneGeneration=-1), "SCENE_BINDING_MISMATCH"),
            (lambda values: values[1].update(simulationTime=values[0]["simulationTime"]), "INVALID_TIMELINE"),
            (lambda values: values[1].update(entities=[{"id": -999}]), "INVALID_TIMELINE"),
            (lambda values: values[1].update(entities=[{"id": object_id, "parent": -999}]), "INVALID_TIMELINE"),
            (lambda values: values[1].update(entities=[{"id": object_id, "world": {}}]), "INVALID_TIMELINE"),
            (lambda values: values[1].update(entities=[{"id": native["entities"][0]["id"], "local": {}, "world": {}}]), "INVALID_TIMELINE"),
        ):
            bad = copy.deepcopy(valid)
            mutate(bad)
            rejected = await client.post("/api/agent-control/presentation/timeline", headers=headers,
                                         json={"baseSnapshot": base, "frames": bad})
            assert rejected.status == 400
            assert (await rejected.json())["error"]["code"] == expected_code
        unchanged = await (await client.get("/api/presentation")).json()
        assert unchanged["timeline"]["timelineId"] == moved_result["timeline"]["timelineId"]
        agent.release.set()
    finally:
        agent.release.set()
        await client.close()


def test_trajectory_markers_survive_gateway_publish(tmp_path: Path) -> None:
    asyncio.run(_exercise_marker_publish(tmp_path))


async def _exercise_binding_mismatch(tmp_path: Path) -> None:
    """A frame from another scene generation must be rejected, markers or not.

    Checked on the timeline route because that is the only publish path that accepts supplied
    frames; a static publish is read from the authoritative world and has nothing to validate.
    """
    agent = _StubAgent()
    app = create_app(agent_client=agent,  # type: ignore[arg-type]
                     agent_state_storage=tmp_path / "agent-state.json",
                     presentation_storage=tmp_path / "presentation.json",
                     asset_service=AssetService(tmp_path / "uploads"))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        session = await asyncio.to_thread(connect, app[LIVE_ENDPOINT_KEY])
        try:
            native = (await asyncio.to_thread(session.scene_snapshot)).as_dict()
        finally:
            await asyncio.to_thread(session.close)

        stale = with_trajectories(
            dict(native, sceneGeneration=int(native["sceneGeneration"]) + 5), _segments())
        await client.post("/api/agent/jobs", json={
            "requestId": "publish-2", "text": "trace a path", "sceneId": "main"})
        for _ in range(200):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        context = agent.calls[0]["control_context"]
        headers = {"X-Agent-Job-Id": str(context["jobId"]),
                   "X-Agent-Control-Token": str(context["controlToken"])}
        response = await client.post(
            "/api/agent-control/presentation/timeline", headers=headers,
            json={"sourceTurnId": "turn-1", "frames": [stale, dict(stale)]})
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "SCENE_BINDING_MISMATCH"
        agent.release.set()
    finally:
        agent.release.set()
        await client.close()


def test_frame_from_another_generation_is_rejected(tmp_path: Path) -> None:
    asyncio.run(_exercise_binding_mismatch(tmp_path))
