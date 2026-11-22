from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
import pytest

from web.gateway.agent import AgentResult, AgentServiceClient, AgentTurnError
from web.gateway.agent_state import GatewayAgentState
from web.gateway.assets import AssetService
from web.gateway.features import load_feature_config
from web.gateway.presentation import PresentationStore
from web.gateway.server import (DEFAULT_TIMELINE_MAX_BYTES, LIVE_ENDPOINT_KEY,
                                LIVE_INSTANCE_ID_KEY, create_app)
from sim_platform.live import connect


def test_gateway_openapi_documents_agent_presentation_contract() -> None:
    contract = (Path(__file__).parents[1] / "contracts" / "openapi.yaml").read_text(
        encoding="utf-8")
    for route in (
        "/api/agent/jobs:",
        "/api/agent/conversations:",
        "/api/agent/conversations/{conversationId}:",
        "/api/agent/conversations/{conversationId}/activate:",
        "/api/presentation:",
        "/api/presentation/mode:",
        "/api/agent-control/presentation/snapshot:",
        "/api/agent-control/assets/resolve:",
        "/api/agent-control/presentation/timeline:",
        "/api/agent-control/captures:",
        "/api/agent-control/captures/{captureId}/frames:",
        "/api/agent-control/captures/{captureId}/finish:",
        "/api/agent-control/captures/{captureId}/abort:",
        "/api/timelines/{timelineId}/frames/{index}:",
        "/events:",
        "/api/metrics:",
    ):
        assert f"  {route}" in contract
    assert "AgentControl:" in contract
    assert "TimelineManifest:" in contract


def test_default_timeline_byte_limit_is_512_mib() -> None:
    assert DEFAULT_TIMELINE_MAX_BYTES == 512 * 1024 * 1024


def test_asset_resolver_accepts_unique_digest_suffix(tmp_path: Path) -> None:
    asset = AssetService(tmp_path / "uploads")
    record = asset.save_upload("jaka_zu7.urdf", b"<robot name='jaka'></robot>")
    assert asset.resolve_file(record.id).id == record.id
    assert asset.resolve_file(record.id.split(":", 1)[1]).id == record.id


async def _exercise_metrics_contract() -> None:
    client = TestClient(TestServer(create_app(agent_client=FakeAgentClient())))
    await client.start_server()
    try:
        response = await client.get("/api/metrics")
        assert response.status == 200
        payload = await response.json()
        assert payload["agentTurnsStarted"] == 0
        assert "providerSessionId" not in json.dumps(payload)

        await client.post("/api/agent/jobs", json={"text": "inspect the scene"})
        for _ in range(100):
            payload = await (await client.get("/api/metrics")).json()
            if payload["agentTurnsSucceeded"] == 1:
                break
            await asyncio.sleep(0.01)
        assert payload["agentTurnsStarted"] == 1
        assert payload["agentTurnsSucceeded"] == 1
        assert payload["agentTurnLatencySamples"] == 1
    finally:
        await client.close()


def test_gateway_metrics_contract() -> None:
    asyncio.run(_exercise_metrics_contract())


class FakeAgentClient:
    connected = True
    endpoint = "fake://agent"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.deleted_conversations: list[str] = []
        self.conversation_counter = 0
        self.conversations = {
            "conversation-old": self._conversation("conversation-old", "create a red cube")
        }

    @staticmethod
    def _conversation(conversation_id: str, title: str = "New conversation") -> dict:
        return {
            "conversationId": conversation_id,
            "target": "platform",
            "sceneBinding": {"sceneId": "main", "simSessionId": "old-session"},
            "presentationMode": "sync-and-animate",
            "createdAt": "2026-08-05T10:00:00Z",
            "updatedAt": "2026-08-05T10:01:00Z",
            "title": title,
            "providerSessionId": "must-not-leak",
            "messages": [],
        }

    async def execute(self, **params):
        self.calls.append(params)
        return AgentResult(
            conversation_id=params["conversation_id"] or "conversation-1",
            turn_id=f"turn-{len(self.calls)}",
            message="completed",
            status="succeeded",
        )

    async def probe(self):
        return True

    async def ensure_conversation(self, **params):
        return params["conversation_id"] or "conversation-1"

    async def list_turns(self, conversation_id):
        del conversation_id
        return []

    async def list_conversations(self):
        return list(self.conversations.values())

    async def get_conversation(self, conversation_id):
        if conversation_id not in self.conversations:
            raise RuntimeError("conversation was not found")
        return self.conversations[conversation_id]

    async def create_conversation(self, **params):
        del params
        self.conversation_counter += 1
        conversation_id = f"conversation-new-{self.conversation_counter}"
        value = self._conversation(conversation_id)
        self.conversations[conversation_id] = value
        return value

    async def delete_conversation(self, conversation_id):
        self.deleted_conversations.append(conversation_id)
        self.conversations.pop(conversation_id, None)


class DebuggingAgentClient(FakeAgentClient):
    async def execute(self, **params):
        await params["on_debug"]({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "echo debug"},
        })
        return await super().execute(**params)


class InitiallyDisconnectedAgentClient(FakeAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.connected = False

    async def probe(self):
        self.connected = True
        return True


async def _exercise_agent_connection_snapshot() -> None:
    agent = InitiallyDisconnectedAgentClient()
    client = TestClient(TestServer(create_app(agent_client=agent)))
    await client.start_server()
    try:
        snapshot = await (await client.get("/api/bootstrap")).json()
        assert snapshot["agent"]["connected"] is True
        assert snapshot["agent"]["endpoint"] == "fake://agent"
    finally:
        await client.close()


def test_gateway_refreshes_agent_connection_in_snapshot() -> None:
    asyncio.run(_exercise_agent_connection_snapshot())


async def _exercise_agent_conversation_history(tmp_path: Path) -> None:
    agent = FakeAgentClient()
    client = TestClient(TestServer(create_app(
        agent_client=agent,
        agent_state_storage=tmp_path / "agent-state.json",
    )))
    await client.start_server()
    try:
        assert agent.deleted_conversations == ["conversation-old"]
        first = await (await client.post("/api/agent/conversations")).json()
        assert first["conversationId"] == "conversation-new-1"
        bootstrap = await (await client.get("/api/bootstrap")).json()
        created = await client.post("/api/commands", json={
            "requestId": "create-cube",
            "sceneId": "main",
            "baseRevision": bootstrap["revision"],
            "method": "scene.create_primitive",
            "params": {"kind": "cube", "name": "Conversation Cube"},
        })
        assert created.status == 200
        second = await (await client.post("/api/agent/conversations")).json()
        assert second["conversationId"] == "conversation-new-2"
        assert [item["name"] for item in second["snapshot"]["entities"]] == [
            "World", "Sky Light", "环境光"]

        history_response = await client.get("/api/agent/conversations")
        assert history_response.status == 200
        history = await history_response.json()
        assert len(history["conversations"]) == 2
        assert "providerSessionId" not in json.dumps(history)

        delete_active = await client.delete(
            "/api/agent/conversations/conversation-new-2")
        assert delete_active.status == 409

        activate_response = await client.post(
            "/api/agent/conversations/conversation-new-1/activate")
        assert activate_response.status == 200
        activated = await activate_response.json()
        assert activated["active"] is True
        assert activated["messages"] == []
        assert [item["name"] for item in activated["snapshot"]["entities"]] == [
            "World", "Sky Light", "环境光", "Conversation Cube"]
        sky_light = activated["snapshot"]["entities"][1]
        assert sky_light["kind"] == "light"
        assert sky_light["local"]["rotationWxyz"] == pytest.approx(
            [2 ** -0.5, 0.0, 2 ** -0.5, 0.0])
        assert "providerSessionId" not in json.dumps(activated)

        restored = await (await client.post(
            "/api/agent/conversations/conversation-new-2/activate")).json()
        assert not any(item["name"] == "Conversation Cube"
                       for item in restored["snapshot"]["entities"])

        deleted = await client.delete(
            "/api/agent/conversations/conversation-new-1")
        assert deleted.status == 200
        assert "conversation-new-1" in agent.deleted_conversations

        submitted = await client.post("/api/agent/jobs", json={"text": "move it"})
        job_id = (await submitted.json())["id"]
        for _ in range(100):
            job = await (await client.get(f"/api/agent/jobs/{job_id}")).json()
            if job["status"] == "success":
                break
            await asyncio.sleep(0.01)
        assert agent.calls[0]["conversation_id"] == "conversation-new-2"
    finally:
        await client.close()


def test_agent_conversation_history_can_be_activated(tmp_path: Path) -> None:
    asyncio.run(_exercise_agent_conversation_history(tmp_path))


async def _exercise_active_conversation_restored_on_restart(tmp_path: Path) -> None:
    agent = FakeAgentClient()
    state_path = tmp_path / "agent-state.json"
    first = TestClient(TestServer(create_app(
        agent_client=agent, agent_state_storage=state_path)))
    await first.start_server()
    try:
        conversation = await (await first.post("/api/agent/conversations")).json()
        bootstrap = await (await first.get("/api/bootstrap")).json()
        response = await first.post("/api/commands", json={
            "requestId": "restart-cube", "sceneId": "main",
            "baseRevision": bootstrap["revision"], "method": "scene.create_primitive",
            "params": {"kind": "cube", "name": "Restart Cube"},
        })
        assert response.status == 200
        conversation_id = conversation["conversationId"]
    finally:
        await first.close()

    second = TestClient(TestServer(create_app(
        agent_client=agent, agent_state_storage=state_path)))
    await second.start_server()
    try:
        restored = await (await second.get("/api/bootstrap")).json()
        assert any(item["name"] == "Restart Cube" for item in restored["entities"])
        history = await (await second.get("/api/agent/conversations")).json()
        assert history["activeConversationId"] == conversation_id
    finally:
        await second.close()


def test_active_conversation_scene_is_restored_on_restart(tmp_path: Path) -> None:
    asyncio.run(_exercise_active_conversation_restored_on_restart(tmp_path))


async def _exercise_agent_debug_forwarding(tmp_path: Path) -> None:
    config = tmp_path / "features.json"
    switches = load_feature_config()
    switches["agent.debug"] = True
    config.write_text(json.dumps({"version": 1, "features": switches}), encoding="utf-8")
    agent = DebuggingAgentClient()
    client = TestClient(TestServer(create_app(
        agent_client=agent, feature_config=config,
        agent_state_storage=tmp_path / "agent-state.json")))
    await client.start_server()
    try:
        response = await client.post("/api/agent/jobs", json={"text": "inspect"})
        job_id = (await response.json())["id"]
        for _ in range(100):
            job = await (await client.get(f"/api/agent/jobs/{job_id}")).json()
            if job["status"] == "success":
                break
            await asyncio.sleep(0.01)
        assert agent.calls[0]["debug"] is True
        assert job["debugEvents"][0]["item"]["command"] == "echo debug"
    finally:
        await client.close()


def test_gateway_forwards_agent_debug_when_enabled(tmp_path: Path) -> None:
    asyncio.run(_exercise_agent_debug_forwarding(tmp_path))


class SlowAgentClient(FakeAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def execute(self, **params):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            return await super().execute(**params)
        finally:
            self.active -= 1


class PublishingAgentClient(FakeAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def execute(self, **params):
        self.calls.append(params)
        await self.release.wait()
        return AgentResult(
            conversation_id=params["conversation_id"] or "conversation-1",
            turn_id=f"turn-{len(self.calls)}",
            message="completed",
            status="succeeded",
        )


async def _exercise_conversation_switch_blocked_during_turn(tmp_path: Path) -> None:
    agent = PublishingAgentClient()
    client = TestClient(TestServer(create_app(
        agent_client=agent,
        agent_state_storage=tmp_path / "agent-state.json",
    )))
    await client.start_server()
    try:
        await client.post("/api/agent/jobs", json={"text": "simulate"})
        for _ in range(100):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        response = await client.post(
            "/api/agent/conversations/conversation-old/activate")
        assert response.status == 409
    finally:
        agent.release.set()
        await client.close()


def test_conversation_switch_is_blocked_during_agent_turn(tmp_path: Path) -> None:
    asyncio.run(_exercise_conversation_switch_blocked_during_turn(tmp_path))


class LeakingAgentClient(FakeAgentClient):
    async def execute(self, **params):
        del params
        raise RuntimeError("shell: rm secret; token=abc; C:\\private\\trace.json")


class RecordingAgentServiceClient(AgentServiceClient):
    def __init__(self) -> None:
        super().__init__("fake")
        self.requests: list[tuple[str, str]] = []
        self.turn_created = asyncio.Event()

    async def _request(self, method: str, path: str, payload=None):
        del payload
        self.requests.append((method, path))
        if path == "/v1/conversations":
            return {"conversationId": "conversation-1"}
        if path.endswith("/turns"):
            self.turn_created.set()
            return {"turnId": "turn-1", "status": "queued"}
        if path.endswith("/cancel"):
            return {"turnId": "turn-1", "status": "cancelled"}
        return {"turnId": "turn-1", "status": "running"}


class FailingProgressAgentServiceClient(AgentServiceClient):
    def __init__(self) -> None:
        super().__init__("fake")
        self.polls = 0

    async def _request(self, method: str, path: str, payload=None):
        del method, payload
        if path == "/v1/conversations":
            return {"conversationId": "conversation-1"}
        if path.endswith("/turns"):
            return {"turnId": "turn-1", "status": "queued"}
        if "/events?" in path:
            if "after=1" in path:
                return []
            return [{
                "eventId": 1,
                "turnId": "turn-1",
                "type": "turn.progress",
                "payload": {
                    "phase": "execute",
                    "status": "failed",
                    "title": "执行动作",
                    "failure": {"code": "PATH_BLOCKED", "message": "target is unreachable"},
                },
            }]
        if path.endswith("/turn-1"):
            self.polls += 1
            return {
                "turnId": "turn-1",
                "status": "failed",
                "errorCode": "PATH_BLOCKED",
                "message": "target is unreachable",
            }
        raise AssertionError(f"unexpected request path: {path}")


async def _exercise_agent_client_progress_before_failure() -> None:
    agent = FailingProgressAgentServiceClient()
    progress: list[dict[str, Any]] = []
    caught: AgentTurnError | None = None
    try:
        await agent.execute(
            conversation_id=None,
            scene_id="main",
            sim_session_id="sim-1",
            scene_generation=0,
            connect="tcp://127.0.0.1:1234",
            text="move",
            presentation_mode="real-sync",
            control_context={"controlToken": "trusted", "jobId": "job-1"},
            request_id="request-1",
            on_progress=lambda value: _append_async(progress, value),
        )
    except AgentTurnError as error:
        caught = error

    assert progress == [{
        "phase": "execute",
        "status": "failed",
        "title": "执行动作",
        "failure": {"code": "PATH_BLOCKED", "message": "target is unreachable"},
    }]
    assert caught is not None
    assert caught.code == "PATH_BLOCKED"
    assert str(caught) == "target is unreachable"


async def _append_async(values: list[dict[str, Any]], value: dict[str, Any]) -> None:
    values.append(value)


def test_agent_client_forwards_progress_before_structured_terminal_error() -> None:
    asyncio.run(_exercise_agent_client_progress_before_failure())


def _agent_control(control_context: dict[str, Any]) -> dict[str, str]:
    token = control_context.get("controlToken")
    job = control_context.get("jobId")
    assert isinstance(token, str) and token
    assert isinstance(job, str) and job
    return {
        "X-Agent-Control-Token": token,
        "X-Agent-Job-Id": job,
    }


async def _exercise_gateway() -> None:
    server = TestServer(create_app())
    client = TestClient(server)
    await client.start_server()
    try:
        health = await (await client.get("/api/health")).json()
        assert health["ok"] is True
        assert health["service"] == "Web Gateway"

        snapshot = await (await client.get("/api/bootstrap")).json()
        assert snapshot["sceneId"] == "main"
        assert snapshot["entities"][0]["name"] == "World"
        assert snapshot["features"]["panels.details.physics.ifpsSphereCount"] is True
        assert snapshot["features"]["agent.input.voiceInput"] is True
        revision = snapshot["revision"]

        async def command(method: str, params: dict, base_revision: int):
            return await client.post("/api/commands", json={
                "requestId": f"test-{method}", "sceneId": "main",
                "baseRevision": base_revision, "method": method, "params": params,
            })

        created_response = await command("scene.create_primitive", {
            "kind": "cube", "name": "Gateway Cube", "translation": [1, 2, 3],
        }, revision)
        assert created_response.status == 200
        created = await created_response.json()
        entity = created["data"]["entity"]
        revision = created["revision"]

        for light_type, expected_name in (
                ("spot", "Spotlight"), ("directional", "Directional Light"),
                ("ambient", "环境光")):
            light_response = await command("scene.create_light", {
                "type": light_type,
            }, revision)
            assert light_response.status == 200, await light_response.text()
            revision = (await light_response.json())["revision"]
            light_snapshot = await (await client.get("/api/bootstrap")).json()
            assert any(item["name"] == expected_name and item.get("light", {}).get("type") == light_type
                       for item in light_snapshot["entities"])

        invalid_cone = await command("scene.create_light", {
            "type": "spot", "innerConeRadians": 0.8, "outerConeRadians": 0.4,
        }, revision)
        assert invalid_cone.status == 400
        assert (await invalid_cone.json())["error"]["code"] == "INVALID_LIGHT_CONE"

        conflict = await command("scene.update_entity", {"entity": entity, "name": "Stale"}, revision - 1)
        assert conflict.status == 409
        assert (await conflict.json())["error"]["code"] == "REVISION_CONFLICT"

        updated = await command("scene.update_entity", {
            "entity": entity, "name": "Updated Cube",
            "transform": {"translation": [2, 3, 4], "scale": [1.5, 1.5, 1.5]},
            "visual": {"visible": True, "minLod": 1, "maxLod": 4},
            "rigidBody": {"enabled": True, "dynamic": True, "gravity": True,
                          "mass": 2.5, "friction": [0.8, 0.01, 0.001],
                          "restitution": 0.2},
            "ifpsSphereCount": 32,
        }, revision)
        assert updated.status == 200
        revision = (await updated.json())["revision"]
        updated_snapshot = await (await client.get("/api/bootstrap")).json()
        updated_entity = next(item for item in updated_snapshot["entities"]
                              if item["id"] == entity)
        assert updated_entity["rigidBody"] == {
            "enabled": True, "dynamic": True, "gravity": True, "mass": 2.5,
        }
        assert updated_entity["collider"]["friction"] == [0.8, 0.01, 0.001]
        assert updated_entity["collider"]["restitution"] == 0.2
        assert updated_entity["collider"]["ifpsSphereCount"] == 32

        assets = (await (await client.get("/api/assets")).json())["assets"]
        mesh_robot = next(item for item in assets if item["name"] == "mesh_two_link")
        loaded_response = await command("scene.load_asset", {
            "parent": 0, "assetId": mesh_robot["id"],
        }, revision)
        assert loaded_response.status == 200, await loaded_response.text()
        revision = (await loaded_response.json())["revision"]
        mesh_snapshot = await (await client.get("/api/bootstrap")).json()
        mesh_colliders = [item["collider"] for item in mesh_snapshot["entities"]
                          if item.get("collider", {}).get("shape") == "mesh"]
        assert mesh_colliders
        assert all(collider["resource"].startswith("/api/assets/")
                   for collider in mesh_colliders)
        assert all("format=obj" in collider["resource"] for collider in mesh_colliders)

        camera_response = await command("scene.create_camera", {
            "parent": 0, "output": "rgbd", "name": "World Camera",
            "width": 64, "height": 48,
        }, revision)
        assert camera_response.status == 200, await camera_response.text()
        camera = await camera_response.json()
        revision = camera["revision"]

        frame_response = await client.get(
            f"/api/sensors/{camera['data']['entity']}/frame?mode=rgb")
        assert frame_response.status == 200, await frame_response.text()
        assert frame_response.content_type == "image/png"
        assert (await frame_response.read()).startswith(b"\x89PNG\r\n\x1a\n")

        reparented = await command("scene.set_parent", {"entity": entity, "parent": camera["data"]["entity"]}, revision)
        assert reparented.status == 200
        revision = (await reparented.json())["revision"]

        deleted = await command("scene.delete_entity", {"entity": entity}, revision)
        assert deleted.status == 200
        revision = (await deleted.json())["revision"]

        undone = await command("editor.undo", {}, revision)
        assert undone.status == 200
        revision = (await undone.json())["revision"]
        restored_snapshot = await (await client.get("/api/bootstrap")).json()
        assert any(item["name"] == "Updated Cube" for item in restored_snapshot["entities"])

        redone = await command("editor.redo", {}, revision)
        assert redone.status == 200

        final_snapshot = await (await client.get("/api/bootstrap")).json()
        payload = str(final_snapshot)
        assert "Updated Cube" not in payload
        assert str(Path.cwd().resolve()) not in payload
        assert all(not str(asset.get("id", "")).startswith(("C:\\", "F:\\"))
                   for asset in (await (await client.get("/api/assets")).json())["assets"])
    finally:
        await client.close()


def test_gateway_native_command_flow() -> None:
    asyncio.run(_exercise_gateway())


async def _exercise_presentation_and_agent_contract() -> None:
    agent = PublishingAgentClient()
    app = create_app(agent_client=agent)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        snapshot = await (await client.get("/api/bootstrap")).json()
        switched = await client.post("/api/presentation/mode", json={
            "mode": "sync-and-animate",
            "basePresentationRevision": snapshot["presentation"]["revision"],
        })
        assert switched.status == 200
        assert (await switched.json())["mode"] == "sync-and-animate"

        readonly = await client.post("/api/commands", json={
            "requestId": "readonly", "sceneId": "main",
            "baseRevision": snapshot["revision"], "method": "scene.create_entity",
            "params": {"name": "blocked"},
        })
        assert readonly.status == 400
        assert (await readonly.json())["error"]["code"] == "MODE_READ_ONLY"

        session = await asyncio.to_thread(connect, app[LIVE_ENDPOINT_KEY])
        try:
            native = (await asyncio.to_thread(session.scene_snapshot)).as_dict()
        finally:
            await asyncio.to_thread(session.close)
        frames = [dict(native, simulationTime=value) for value in (0.0, 0.1, 0.2)]
        denied = await client.post("/api/agent-control/presentation/timeline", json={
            "sourceTurnId": "turn-0", "frames": frames,
        })
        assert denied.status == 403

        response = await client.post("/api/agent/jobs", json={
            "requestId": "agent-0", "text": "prepare animation", "sceneId": "main",
        })
        job = await response.json()
        for _ in range(100):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        busy = await client.post("/api/presentation/mode", json={
            "mode": "real-sync", "basePresentationRevision": 1})
        assert busy.status == 400
        assert (await busy.json())["error"]["code"] == "SCENE_BUSY"
        headers = _agent_control(agent.calls[0]["control_context"])
        published = await client.post(
            "/api/agent-control/presentation/timeline", headers=headers, json={
                "sourceTurnId": "turn-0", "frames": frames,
            })
        assert published.status == 200
        timeline = (await published.json())["timeline"]
        assert timeline["frameCount"] == 3
        assert timeline["defaultFps"] == 50
        frame = await client.get(timeline["frames"][1]["snapshotUrl"])
        frame_snapshot = await frame.json()
        assert frame_snapshot["simulationTime"] == 0.1
        sky_light = next(item for item in frame_snapshot["entities"]
                         if item["name"] == "Sky Light")
        assert sky_light["kind"] == "light"
        assert sky_light["flags"] == ["light"]
        assert sky_light["light"]["type"] == "directional"
        assert sky_light["light"]["enabled"] is True
        assert sky_light["light"]["intensity"] == pytest.approx(1.0)
        agent.release.set()

        for request_id in ("agent-1", "agent-2"):
            response = await client.post("/api/agent/jobs", json={
                "requestId": request_id, "text": "move the robot", "sceneId": "main",
            })
            job = await response.json()
            for _ in range(100):
                status = await (await client.get(f"/api/agent/jobs/{job['id']}")).json()
                if status["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.01)
            assert status["status"] == "success"
        assert agent.calls[0]["conversation_id"] == "conversation-1"
        assert agent.calls[1]["conversation_id"] == "conversation-1"
        control_context = agent.calls[0]["control_context"]
        assert control_context["sceneId"] == "main"
        assert control_context["gatewayBaseUrl"]
        assert control_context["liveEndpoint"]
        assert any(
            str(asset["name"]).startswith("jaka_zu7")
            for asset in control_context["robotAssets"]
        )
        assert agent.calls[0]["connect"].startswith("tcp://127.0.0.1:")
        assert agent.calls[0]["sim_session_id"] != "gateway-local-main"
        assert agent.calls[1]["text"] == "move the robot"
    finally:
        await client.close()


def test_presentation_modes_timeline_and_agent_prompt_contract() -> None:
    asyncio.run(_exercise_presentation_and_agent_contract())


async def _exercise_agent_scene_serialization() -> None:
    agent = SlowAgentClient()
    client = TestClient(TestServer(create_app(agent_client=agent)))
    await client.start_server()
    try:
        submitted = [await (await client.post("/api/agent/jobs", json={
            "requestId": f"serial-{index}", "text": f"turn {index}", "sceneId": "main",
        })).json() for index in range(2)]
        for _ in range(300):
            statuses = [await (await client.get(
                f"/api/agent/jobs/{item['id']}")).json() for item in submitted]
            if all(item["status"] not in {"queued", "running"} for item in statuses):
                break
            await asyncio.sleep(0.01)
        assert [item["status"] for item in statuses] == ["success", "success"]
        assert agent.max_active == 1
    finally:
        await client.close()


def test_agent_jobs_are_serialized_per_scene() -> None:
    asyncio.run(_exercise_agent_scene_serialization())


async def _exercise_agent_error_filtering() -> None:
    client = TestClient(TestServer(create_app(agent_client=LeakingAgentClient())))
    await client.start_server()
    try:
        socket = await client.ws_connect("/events")
        await socket.receive_json()
        submitted = await client.post("/api/agent/jobs", json={
            "requestId": "leak", "text": "fail", "sceneId": "main",
        })
        job = await submitted.json()
        event_values = []
        for _ in range(100):
            current = await (await client.get(f"/api/agent/jobs/{job['id']}")).json()
            if current["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        for _ in range(6):
            try:
                message = await socket.receive(timeout=0.5)
            except TimeoutError:
                break
            if message.type.name == "TEXT":
                event_values.append(message.data)
                if '"agent.turn_updated"' in message.data:
                    parsed = json.loads(message.data)
                    assert "presentationRevision" in parsed
                    break
        visible = json.dumps({"job": current, "events": event_values})
        assert current["message"] == "Agent execution failed"
        assert "rm secret" not in visible
        assert "token=abc" not in visible
        assert "private" not in visible
        await socket.close()
    finally:
        await client.close()


def test_agent_internal_error_details_are_not_web_visible() -> None:
    asyncio.run(_exercise_agent_error_filtering())


async def _exercise_gateway_concurrency_and_limits(tmp_path: Path) -> None:
    agent = PublishingAgentClient()
    client = TestClient(TestServer(create_app(agent_client=agent)))
    await client.start_server()
    try:
        initial = await (await client.get("/api/bootstrap")).json()
        stale = await client.post("/api/presentation/mode", json={
            "mode": "sync-and-animate", "basePresentationRevision": 99,
        })
        assert stale.status == 409
        assert (await stale.json())["error"]["code"] == (
            "PRESENTATION_REVISION_CONFLICT")

        submitted = await client.post("/api/agent/jobs", json={
            "requestId": "lease", "text": "hold the lease", "sceneId": "main",
        })
        assert submitted.status == 202
        for _ in range(100):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        assert agent.calls
        blocked = await client.post("/api/commands", json={
            "requestId": "browser-write", "sceneId": "main",
            "baseRevision": initial["revision"], "method": "scene.create_primitive",
            "params": {"kind": "cube"},
        })
        assert blocked.status == 409
        assert (await blocked.json())["error"]["code"] == "SCENE_BUSY"

        session = await asyncio.to_thread(connect, client.server.app[LIVE_ENDPOINT_KEY])
        try:
            native = (await asyncio.to_thread(session.scene_snapshot)).as_dict()
        finally:
            await asyncio.to_thread(session.close)
        headers = _agent_control(agent.calls[0]["control_context"])
        long_timeline = await client.post(
            "/api/agent-control/presentation/timeline", headers=headers,
            json={"frames": [dict(native, simulationTime=0.0),
                             dict(native, simulationTime=60.1)]})
        assert long_timeline.status == 200
        assert (await long_timeline.json())["timeline"]["durationSeconds"] == 60.1
        repeated = await client.post(
            "/api/agent-control/presentation/timeline", headers=headers,
            json={"frames": [dict(native, simulationTime=1.0),
                             dict(native, simulationTime=1.0)]})
        assert repeated.status == 400
        agent.release.set()
    finally:
        await client.close()

    switches = load_feature_config()
    switches["agent.conversation"] = False
    config = tmp_path / "disabled-agent.json"
    config.write_text(
        json.dumps({"version": 1, "features": switches}), encoding="utf-8")
    disabled = TestClient(TestServer(create_app(feature_config=config)))
    await disabled.start_server()
    try:
        response = await disabled.post("/api/agent/jobs", json={"text": "ignored"})
        assert response.status == 404
    finally:
        await disabled.close()


def test_gateway_write_lease_revision_limits_and_feature_flag(tmp_path: Path) -> None:
    asyncio.run(_exercise_gateway_concurrency_and_limits(tmp_path))


async def _exercise_capture_contract() -> None:
    agent = PublishingAgentClient()
    client = TestClient(TestServer(create_app(agent_client=agent)))
    await client.start_server()
    try:
        initial = await (await client.get("/api/presentation")).json()
        assert initial["mode"] == "real-sync"
        await client.post("/api/agent/jobs", json={
            "requestId": "capture", "text": "capture motion", "sceneId": "main",
        })
        for _ in range(100):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        initial = await (await client.get("/api/presentation")).json()
        assert initial["mode"] == "sync-and-animate"
        assert agent.calls[0]["control_context"]["presentationMode"] == "sync-and-animate"
        headers = _agent_control(agent.calls[0]["control_context"])
        session = await asyncio.to_thread(connect, client.server.app[LIVE_ENDPOINT_KEY])
        try:
            native = (await asyncio.to_thread(session.scene_snapshot)).as_dict()
        finally:
            await asyncio.to_thread(session.close)
        frames = [dict(native, simulationTime=value) for value in (1.0, 2.1)]

        legacy_limit = await client.post(
            "/api/agent-control/captures", headers=headers,
            json={"maxFrames": 2})
        assert legacy_limit.status == 201
        legacy_capture_id = (await legacy_limit.json())["captureId"]
        for value in (2.0, 2.1, 62.2):
            appended = await client.post(
                f"/api/agent-control/captures/{legacy_capture_id}/frames",
                headers=headers, json={"snapshot": dict(native, simulationTime=value)})
            assert appended.status == 200
        await client.post(
            f"/api/agent-control/captures/{legacy_capture_id}/abort", headers=headers)

        started = await client.post(
            "/api/agent-control/captures", headers=headers,
            json={"maxFrames": 2, "maxDurationSeconds": 1.0})
        assert started.status == 201
        capture_id = (await started.json())["captureId"]
        for frame in frames:
            appended = await client.post(
                f"/api/agent-control/captures/{capture_id}/frames",
                headers=headers, json={"snapshot": frame})
            assert appended.status == 200
        before_abort = await (await client.get("/api/presentation")).json()
        assert before_abort == initial
        aborted = await client.post(
            f"/api/agent-control/captures/{capture_id}/abort", headers=headers)
        assert aborted.status == 200
        assert await (await client.get("/api/presentation")).json() == initial

        started = await client.post(
            "/api/agent-control/captures", headers=headers, json={"maxFrames": 2})
        capture_id = (await started.json())["captureId"]
        for frame in frames:
            await client.post(
                f"/api/agent-control/captures/{capture_id}/frames",
                headers=headers, json={"snapshot": frame})
        finished = await client.post(
            f"/api/agent-control/captures/{capture_id}/finish",
            headers=headers, json={"publish": True})
        assert finished.status == 200
        timeline = (await finished.json())["timeline"]
        assert timeline["sceneId"] == "main"
        assert timeline["simSessionId"] == client.server.app[LIVE_INSTANCE_ID_KEY]
        assert timeline["sceneGeneration"] == native["sceneGeneration"]
        assert timeline["presentationRevision"] == 2
        assert timeline["frameCount"] == 2

        dangling = await client.post(
            "/api/agent-control/captures", headers=headers, json={"maxFrames": 2})
        assert dangling.status == 201
        agent.release.set()
        for _ in range(100):
            jobs = (await (await client.get("/api/agent/jobs")).json())["jobs"]
            if jobs[0]["status"] == "success":
                break
            await asyncio.sleep(0.01)

        agent.release.clear()
        await client.post("/api/agent/jobs", json={
            "requestId": "capture-2", "text": "capture again", "sceneId": "main",
        })
        for _ in range(100):
            if len(agent.calls) == 2:
                break
            await asyncio.sleep(0.01)
        next_headers = _agent_control(agent.calls[1]["control_context"])
        restarted = await client.post(
            "/api/agent-control/captures", headers=next_headers, json={"maxFrames": 2})
        assert restarted.status == 201
        agent.release.set()
    finally:
        await client.close()


def test_capture_is_invisible_until_finish_and_abort_preserves_projection() -> None:
    asyncio.run(_exercise_capture_contract())


async def _exercise_agent_cancel_propagation() -> None:
    agent = RecordingAgentServiceClient()
    task = asyncio.create_task(agent.execute(
        conversation_id=None,
        scene_id="main",
        sim_session_id="sim-1",
        scene_generation=0,
        connect="tcp://127.0.0.1:1234",
        text="wait",
        presentation_mode="real-sync",
        control_context={"controlToken": "trusted", "jobId": "job-cancel-1"},
        request_id="cancel-1",
    ))
    await agent.turn_created.wait()
    task.cancel()
    try:
        await task
        raise AssertionError("cancelled Agent request unexpectedly completed")
    except asyncio.CancelledError:
        pass
    assert ("POST", "/v1/conversations/conversation-1/turns/turn-1/cancel") in agent.requests


def test_agent_client_propagates_gateway_cancellation() -> None:
    asyncio.run(_exercise_agent_cancel_propagation())


async def _exercise_shared_live_world() -> None:
    agent = PublishingAgentClient()
    app = create_app(agent_client=agent)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        session = await asyncio.to_thread(connect, app[LIVE_ENDPOINT_KEY])
        try:
            live_instance_id = session.instance_id
            primitive = await asyncio.to_thread(session.create_primitive, "cube")
            await asyncio.to_thread(primitive.set_position, (1.0, 2.0, 3.0))
            for _ in range(100):
                snapshot = await (await client.get("/api/bootstrap")).json()
                matching = [item for item in snapshot["entities"]
                            if item["id"] == primitive.handle]
                if (matching and
                        matching[0]["local"]["translation"] == [1.0, 2.0, 3.0]):
                    break
                await asyncio.sleep(0.01)
            assert matching[0]["local"]["translation"] == [1.0, 2.0, 3.0]

            before = (await (await client.get("/api/bootstrap")).json())["simulationTime"]
            await asyncio.to_thread(session.start)
            for _ in range(100):
                running = await (await client.get("/api/bootstrap")).json()
                if running["simulationTime"] > before:
                    break
                await asyncio.sleep(0.01)
            assert running["status"] == "running"
            assert running["simulationTime"] > before
            await asyncio.to_thread(session.pause)

            assert await asyncio.to_thread(session.backend_status) == {
                "physics": "none", "collision": "fcl",
            }
            applied = await asyncio.to_thread(session.apply_backends, physics="mujoco")
            assert applied == {"physics": "mujoco", "collision": "fcl", "changed": True}
            assert session.instance_id == live_instance_id
            assert app[LIVE_INSTANCE_ID_KEY] == live_instance_id
            assert primitive.transform["translation"] == (1.0, 2.0, 3.0)

            floor = await asyncio.to_thread(
                session.create_primitive, "box", name="MuJoCo Floor", size=(5.0, 5.0, 0.1))
            await asyncio.to_thread(floor.set_position, (0.0, 0.0, -0.05))
            assert await asyncio.to_thread(
                floor.set_rigid_body, dynamic=False, gravity=False) == "mujoco"
            ball = await asyncio.to_thread(
                session.create_primitive, "sphere", name="MuJoCo Ball", size=(0.2, 0.2, 0.2))
            await asyncio.to_thread(ball.set_position, (0.0, 0.0, 1.5))
            assert await asyncio.to_thread(
                ball.set_rigid_body, dynamic=True, gravity=True, mass=1.0,
                restitution=0.7) == "mujoco"
            await asyncio.to_thread(session.step, 1515)
            assert session.status()[1] >= 3.03
            assert ball.transform["translation"][2] < 1.0

            for _ in range(100):
                live_snapshot = await (await client.get("/api/bootstrap")).json()
                visible_ball = next((item for item in live_snapshot["entities"]
                                     if item.get("id") == ball.handle), None)
                if visible_ball is not None:
                    break
                await asyncio.sleep(0.01)
            assert live_snapshot["sceneId"] == "main"
            assert visible_ball is not None
            assert visible_ball["local"]["translation"][2] < 1.0

            switched = await client.post(
                "/api/presentation/mode", json={
                    "mode": "sync-and-animate",
                    "basePresentationRevision": live_snapshot["presentation"]["revision"],
                })
            assert switched.status == 200
            frozen = (await switched.json())["snapshot"]
            second = await asyncio.to_thread(session.create_primitive, "sphere")
            await asyncio.sleep(0.1)
            review = await (await client.get("/api/bootstrap")).json()
            assert review["entities"] == frozen["entities"]
            assert all(item["id"] != second.handle for item in review["entities"])

            response = await client.post("/api/agent/jobs", json={
                "requestId": "publish-token", "text": "publish", "sceneId": "main",
            })
            await response.json()
            for _ in range(100):
                if agent.calls:
                    break
                await asyncio.sleep(0.01)
            headers = _agent_control(agent.calls[-1]["control_context"])
            published = await client.post(
                "/api/agent-control/presentation/snapshot", headers=headers, json={})
            assert published.status == 200
            visible = (await published.json())["snapshot"]
            assert any(item["id"] == second.handle for item in visible["entities"])
            agent.release.set()
        finally:
            await asyncio.to_thread(session.close)
    finally:
        await client.close()


def test_live_session_and_gateway_share_one_world() -> None:
    asyncio.run(_exercise_shared_live_world())


def test_feature_config_validation_and_editor_coverage(tmp_path: Path) -> None:
    features = load_feature_config()
    assert len(features) >= 100
    assert all(type(value) is bool for value in features.values())

    invalid = tmp_path / "invalid-features.json"
    invalid.write_text(json.dumps({"version": 1, "features": {"agent.input.voiceInput": "yes"}}),
                       encoding="utf-8")
    try:
        load_feature_config(invalid)
        raise AssertionError("non-boolean feature switch was accepted")
    except ValueError as error:
        assert "must be boolean" in str(error)

    editor = (Path(__file__).resolve().parents[1] / "editor" / "src" / "main.ts").read_text(
        encoding="utf-8")
    referenced = set(re.findall(
        r'(?:data-feature="|data-requires-feature="|enabled\("|when\(")'
        r'([A-Za-z][A-Za-z0-9.]*)', editor))
    referenced.update({
        "panels.scene", "panels.details", "panels.settings", "panels.assets",
        "panels.assets.groups.builtin", "panels.assets.groups.project",
        "panels.assets.groups.uploaded",
    })
    assert referenced <= features.keys()
    for panel in ("scene", "details", "settings", "assets"):
        assert re.search(
            rf'data-panel="{panel}"[^>]*data-requires-feature="panels\.{panel}"', editor)


async def _exercise_custom_feature_config(tmp_path: Path) -> None:
    config = tmp_path / "features.json"
    switches = {
        "panels.scene": False,
        "viewport.tools.move": False,
        "panels.details.physics.ifpsSphereCount": False,
        "agent.input.voiceInput": False,
    }
    config.write_text(json.dumps({"version": 1, "features": switches}), encoding="utf-8")
    client = TestClient(TestServer(create_app(
        asset_service=AssetService(tmp_path / "uploads"), feature_config=config)))
    await client.start_server()
    try:
        snapshot = await (await client.get("/api/bootstrap")).json()
        assert snapshot["features"] == switches
    finally:
        await client.close()


def test_custom_feature_config_is_exposed_by_bootstrap(tmp_path: Path) -> None:
    asyncio.run(_exercise_custom_feature_config(tmp_path))


async def _exercise_feature_config_overrides_persisted_snapshot(tmp_path: Path) -> None:
    config = tmp_path / "features.json"
    switches = {"agent.conversation": True, "agent.debug": True}
    config.write_text(json.dumps({"version": 1, "features": switches}), encoding="utf-8")
    presentation = tmp_path / "presentation.json"
    presentation.write_text(json.dumps({
        "mode": "real-sync",
        "presentationRevision": 1,
        "snapshot": {"features": {"stale": True}},
        "timeline": None,
        "binding": None,
    }), encoding="utf-8")
    client = TestClient(TestServer(create_app(
        agent_client=FakeAgentClient(), feature_config=config,
        presentation_storage=presentation)))
    await client.start_server()
    try:
        snapshot = await (await client.get("/api/bootstrap")).json()
        assert snapshot["features"] == switches
    finally:
        await client.close()


def test_feature_config_overrides_persisted_snapshot(tmp_path: Path) -> None:
    asyncio.run(_exercise_feature_config_overrides_persisted_snapshot(tmp_path))


def test_upload_validation_and_bundle_storage(tmp_path: Path) -> None:
    assets = AssetService(tmp_path)
    try:
        assets.save_upload("fake.obj", b"not geometry")
        raise AssertionError("invalid OBJ content was accepted")
    except ValueError as error:
        assert "geometry" in str(error)

    record = assets.save_upload("robot mesh.obj", b"o cube\nv 0 0 0\nf 1 1 1\n")
    assert record.source == "uploaded"
    assert record.path is not None
    assert record.path.parent.parent == tmp_path.resolve()
    assert record.path.name == "robot mesh.obj"
    assert not record.summary().get("path")


def test_urdf_bundle_preserves_dependencies_and_only_lists_entry(tmp_path: Path) -> None:
    assets = AssetService(tmp_path)
    urdf = b'''<robot name="arm"><link name="base"><visual><geometry>
<mesh filename="meshes/jaka_zu7_meshes/Link_0.STL"/>
</geometry></visual></link></robot>'''
    record = assets.save_bundle("jaka.urdf", {
        "jaka.urdf": urdf,
        "meshes/jaka_zu7_meshes/Link_0.STL": b"solid link\nendsolid link\n",
    })
    assert record.path is not None
    assert record.path.parent.parent == tmp_path.resolve()
    dependency = record.path.parent / "meshes" / "jaka_zu7_meshes" / "Link_0.STL"
    assert dependency.is_file()
    uploaded = [item for item in assets.summaries() if item["source"] == "uploaded"]
    assert [item["name"] for item in uploaded] == ["jaka"]
    resource = assets.register_resource(str(dependency))
    assert resource is not None
    assets.refresh()
    resource_id = resource.split("/api/assets/", 1)[1].split("/file", 1)[0]
    assert assets.file(resource_id)[0] == dependency.resolve()
    assert [
        item["name"] for item in assets.summaries() if item["source"] == "uploaded"
    ] == ["jaka"]

    bundle = record.path.parent
    assets.delete(record.id)
    assert not bundle.exists()


def test_obj_bundle_preserves_mtl_and_texture_and_normalizes_absolute_reference(tmp_path: Path) -> None:
    assets = AssetService(tmp_path)
    obj = b"mtllib C:/export/model.mtl\nv 0 0 0\nf 1 1 1\n"
    mtl = b"newmtl material\nmap_Kd C:\\export\\texture.png\n"
    record = assets.save_bundle("models/model.obj", {
        "models/model.obj": obj,
        "models/model.mtl": mtl,
        "models/texture.png": b"PNG",
    })
    assert record.path is not None
    assert record.path.read_bytes().startswith(b"mtllib model.mtl")
    assert (record.path.parent / "model.mtl").read_bytes() == b"newmtl material\nmap_Kd texture.png\n"
    assert assets.file(record.id, "models/texture.png")[0].is_file()


def test_presentation_store_recovers_published_timeline(tmp_path: Path) -> None:
    storage = tmp_path / "presentation.json"
    first = {"revision": 7, "simulationTime": 1.0, "entities": []}
    second = {"revision": 8, "simulationTime": 2.0, "entities": []}
    store = PresentationStore(first, storage_path=storage, binding=("instance-1", 2))
    store.set_mode("sync-and-animate", first)
    published = store.publish_timeline([first, second], "turn-1")

    recovered = PresentationStore(
        {"revision": 99, "simulationTime": 99.0, "entities": []},
        storage_path=storage,
        binding=("instance-1", 2),
    )
    current = recovered.current()

    assert current == published
    assert recovered.frame(current["timeline"]["timelineId"], 1) == second

    detached = PresentationStore(
        {"revision": 100, "simulationTime": 100.0, "entities": []},
        storage_path=storage,
        binding=("instance-2", 0),
    ).current()
    assert detached["detached"] is True
    assert detached["timeline"] == published["timeline"]


def test_presentation_store_accepts_more_than_600_frames() -> None:
    first = {"revision": 0, "simulationTime": 0.0, "entities": []}
    frames = [dict(first, revision=index, simulationTime=index / 120.0)
              for index in range(721)]
    store = PresentationStore(first, binding=("instance-1", 2))

    published = store.publish_timeline(frames, "turn-long")

    assert published["timeline"]["frameCount"] == 721
    assert store.frame(published["timeline"]["timelineId"], 720) == frames[-1]


def test_gateway_agent_state_recovers_jobs_and_binding(tmp_path: Path) -> None:
    path = tmp_path / "agent-state.json"
    state = GatewayAgentState(path)
    state.bind("main", ("conversation-1", "instance-1", 2, "real-sync"))
    state.put_job({
        "id": "job-1", "sceneId": "main", "status": "running",
        "message": "working",
    })

    recovered = GatewayAgentState(path)

    assert recovered.conversations["main"] == (
        "conversation-1", "instance-1", 2, "real-sync")
    assert recovered.jobs["job-1"]["status"] == "interrupted"
    assert recovered.jobs["job-1"]["recovered"] is True


def test_urdf_bundle_rejects_missing_and_escaping_dependencies(tmp_path: Path) -> None:
    assets = AssetService(tmp_path)
    missing = b'''<robot name="arm"><link name="base"><visual><geometry>
<mesh filename="meshes/missing.stl"/>
</geometry></visual></link></robot>'''
    try:
        assets.save_bundle("robot.urdf", {"robot.urdf": missing})
        raise AssertionError("missing URDF dependency was accepted")
    except ValueError as error:
        assert "meshes/missing.stl" in str(error)

    escaping = b'''<robot name="arm"><link name="base"><visual><geometry>
<mesh filename="../outside.stl"/>
</geometry></visual></link></robot>'''
    try:
        assets.save_bundle("robot.urdf", {"robot.urdf": escaping})
        raise AssertionError("escaping URDF dependency was accepted")
    except ValueError as error:
        assert "escapes the asset bundle" in str(error)


def test_urdf_bundle_resolves_ros_package_dependencies(tmp_path: Path) -> None:
    assets = AssetService(tmp_path)
    urdf = b'''<robot name="arm"><link name="base"><visual><geometry>
<mesh filename="package://jaka_description/meshes/jaka_zu7_meshes/Link_0.STL"/>
</geometry></visual></link></robot>'''
    record = assets.save_bundle("urdf/jaka_zu7.urdf", {
        "urdf/jaka_zu7.urdf": urdf,
        "meshes/jaka_zu7_meshes/Link_0.STL": b"solid link\nendsolid link\n",
    })
    assert record.path is not None
    assert (record.path.parent.parent / "meshes" /
            "jaka_zu7_meshes" / "Link_0.STL").is_file()


async def _exercise_bundle_upload_endpoint(tmp_path: Path) -> None:
    assets = AssetService(tmp_path)
    client = TestClient(TestServer(create_app(asset_service=assets)))
    await client.start_server()
    try:
        form = FormData()
        form.add_field("entry", "robot.urdf")
        form.add_field("path", "robot.urdf")
        form.add_field("files", b'''<robot name="arm"><link name="base"><visual><geometry>
<mesh filename="meshes/link.stl"/>
</geometry></visual></link></robot>''', filename="robot.urdf")
        form.add_field("path", "meshes/link.stl")
        form.add_field("files", b"solid link\nendsolid link\n", filename="link.stl")
        response = await client.post("/api/assets/upload", data=form)
        assert response.status == 201, await response.text()
        payload = await response.json()
        assert payload["asset"]["name"] == "robot"
        assert (assets.require(payload["asset"]["id"]).path.parent / "meshes" / "link.stl").is_file()
    finally:
        await client.close()


def test_bundle_upload_endpoint(tmp_path: Path) -> None:
    asyncio.run(_exercise_bundle_upload_endpoint(tmp_path))


def test_resource_url_preserves_format_and_mesh_selection(tmp_path: Path) -> None:
    mesh = tmp_path / "assembly.stl"
    mesh.write_text("solid assembly\nendsolid assembly\n", encoding="utf-8")
    resource = AssetService(tmp_path).register_resource(f"{mesh}#mesh=2")
    assert resource is not None
    assert resource.endswith("/file?format=stl#mesh=2")


def test_project_obj_resource_serves_its_declared_material(tmp_path: Path) -> None:
    source = tmp_path / "external"
    source.mkdir()
    obj = source / "green_box.obj"
    material = source / "green_box.mtl"
    obj.write_text("mtllib green_box.mtl\nv 0 0 0\nf 1 1 1\n", encoding="utf-8")
    material.write_text("newmtl green\nKd 0.08 0.55 0.16\n", encoding="utf-8")
    assets = AssetService(tmp_path / "uploads")

    resource = assets.register_resource(f"{obj}#mesh=0")
    assert resource is not None
    resource_id = resource.split("/api/assets/", 1)[1].split("/file", 1)[0]

    path, content_type = assets.file(resource_id, "green_box.mtl")
    assert path == material.resolve()
    assert content_type in {"model/mtl", "text/plain", "application/octet-stream"}


def test_project_obj_resource_serves_texture_declared_by_material(tmp_path: Path) -> None:
    source = tmp_path / "external"
    source.mkdir()
    obj = source / "banana.obj"
    material = source / "banana.mtl"
    texture = source / "texture_map.png"
    obj.write_text("mtllib banana.mtl\nv 0 0 0\nf 1 1 1\n", encoding="utf-8")
    material.write_text(
        "newmtl banana\nKd 1 1 1\nmap_Kd texture_map.png\n", encoding="utf-8")
    texture.write_bytes(b"PNG")
    assets = AssetService(tmp_path / "uploads")

    resource = assets.register_resource(str(obj))
    assert resource is not None
    resource_id = resource.split("/api/assets/", 1)[1].split("/file", 1)[0]

    path, content_type = assets.file(resource_id, "texture_map.png")
    assert path == texture.resolve()
    assert content_type == "image/png"


def test_project_obj_resource_rejects_undeclared_sibling(tmp_path: Path) -> None:
    source = tmp_path / "external"
    source.mkdir()
    obj = source / "green_box.obj"
    obj.write_text("mtllib green_box.mtl\nv 0 0 0\nf 1 1 1\n", encoding="utf-8")
    (source / "green_box.mtl").write_text("newmtl green\n", encoding="utf-8")
    (source / "private.txt").write_text("do not serve", encoding="utf-8")
    assets = AssetService(tmp_path / "uploads")
    resource = assets.register_resource(str(obj))
    assert resource is not None
    resource_id = resource.split("/api/assets/", 1)[1].split("/file", 1)[0]

    with pytest.raises(ValueError, match="dependency"):
        assets.file(resource_id, "private.txt")


class EmptyHistoryAgentClient(FakeAgentClient):
    """An Agent Service with a fresh database: it knows no conversations at all.

    This is what replacing the Agent Service looks like from the Gateway's side.
    """

    def __init__(self) -> None:
        super().__init__()
        self.conversations = {}


async def _exercise_stale_binding_is_not_advertised(tmp_path: Path) -> None:
    # A binding left by a previous Agent Service, naming a conversation the new one has
    # never heard of and for which no scene checkpoint exists.
    storage = tmp_path / "agent-state.json"
    storage.write_text(json.dumps({
        "version": 1,
        "conversations": {"main": ["gone-conversation", "old-session", 0, "sync-and-animate"]},
        "jobs": {},
    }), encoding="utf-8")

    agent = EmptyHistoryAgentClient()
    client = TestClient(TestServer(create_app(
        agent_client=agent, agent_state_storage=storage,
        asset_service=AssetService(tmp_path / "uploads"),
        presentation_storage=tmp_path / "presentation.json")))
    await client.start_server()
    try:
        listed = await (await client.get("/api/agent/conversations")).json()
        # The browser fetches the advertised conversation immediately and its whole startup
        # sequence fails on a 404, so an unserveable id must never be advertised.
        assert listed["activeConversationId"] is None
        assert listed["conversations"] == []

        # And the stale binding is reconciled away rather than surviving every restart.
        persisted = json.loads(storage.read_text(encoding="utf-8"))
        assert persisted["conversations"] == {}

        # The rest of the startup sequence still works.
        for route in ("/api/bootstrap", "/api/assets", "/api/health"):
            assert (await client.get(route)).status == 200
    finally:
        await client.close()


def test_stale_conversation_binding_is_not_advertised(tmp_path: Path) -> None:
    asyncio.run(_exercise_stale_binding_is_not_advertised(tmp_path))


async def _exercise_advertised_conversation_is_serveable(tmp_path: Path) -> None:
    """Whatever the Gateway advertises must be fetchable, or startup breaks."""
    agent = FakeAgentClient()
    client = TestClient(TestServer(create_app(
        agent_client=agent,
        agent_state_storage=tmp_path / "agent-state.json",
        asset_service=AssetService(tmp_path / "uploads"),
        presentation_storage=tmp_path / "presentation.json")))
    await client.start_server()
    try:
        for _ in range(3):
            listed = await (await client.get("/api/agent/conversations")).json()
            active = listed["activeConversationId"]
            if active is None:
                break
            # Exactly the request main.ts makes next.
            assert (await client.get(
                f"/api/agent/conversations/{active}")).status == 200
    finally:
        await client.close()


def test_advertised_conversation_can_be_fetched(tmp_path: Path) -> None:
    asyncio.run(_exercise_advertised_conversation_is_serveable(tmp_path))
