from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from struct import pack
from typing import Any

from aiohttp import WSMsgType, web


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "python") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "python"))
if "SIM_PLATFORM_LIBRARY" not in os.environ:
    candidate = PROJECT_ROOT / "build" / "windows-full" / "Release" / "sim_platform_c.dll"
    if candidate.is_file():
        os.environ["SIM_PLATFORM_LIBRARY"] = str(candidate)
os.environ.setdefault("SIM_PLUGIN_DIR", str(
    PROJECT_ROOT / "build" / "windows-full" / "Release" / "plugins"))

from sim_platform.sdk import Camera  # noqa: E402
from sim_platform.server import HeadlessService, LiveServer  # noqa: E402
from sim_platform._logging import install_standard_logging  # noqa: E402

from .agent import AgentClient, AgentServiceClient, AgentTurnError  # noqa: E402
from .agent_state import GatewayAgentState  # noqa: E402
from .assets import AssetService  # noqa: E402
from .features import DEFAULT_FEATURE_CONFIG, load_feature_config  # noqa: E402
from .presentation import PresentationError, PresentationStore  # noqa: E402
from .scene import SceneError, SceneService  # noqa: E402

LIVE_ENDPOINT_KEY = web.AppKey("live_endpoint", str)
LIVE_INSTANCE_ID_KEY = web.AppKey("live_instance_id", str)
DEFAULT_TIMELINE_MAX_BYTES = 512 * 1024 * 1024


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return pack(">I", len(payload)) + kind + payload + pack(">I", zlib.crc32(kind + payload))


def encode_png(width: int, height: int, channels: int, pixels: bytes) -> bytes:
    color_type = 2 if channels == 3 else 0
    stride = width * channels
    scanlines = b"".join(b"\x00" + pixels[row * stride:(row + 1) * stride]
                         for row in range(height))
    header = pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) +
            _png_chunk(b"IDAT", zlib.compress(scanlines, 6)) + _png_chunk(b"IEND", b""))


class EventHub:
    def __init__(self) -> None:
        self.clients: set[web.WebSocketResponse] = set()

    async def publish(self, event: dict[str, Any]) -> None:
        dead: list[web.WebSocketResponse] = []
        for client in self.clients:
            try:
                await client.send_json(event)
            except (ConnectionError, RuntimeError):
                dead.append(client)
        for client in dead:
            self.clients.discard(client)


def create_app(*, agent_host: str | None = None, agent_port: int = 8765,
               static_directory: Path | None = None,
               asset_service: AssetService | None = None,
               feature_config: Path | None = None,
               agent_client: AgentClient | None = None,
               agent_control_base_url: str | None = None,
               presentation_storage: Path | None = None,
               agent_state_storage: Path | None = None) -> web.Application:
    install_standard_logging("web_gateway")
    assets = asset_service or AssetService()
    features = load_feature_config(feature_config or DEFAULT_FEATURE_CONFIG)
    scene = SceneService(
        assets,
        cache_directory=PROJECT_ROOT / ".sim-cache" / "web",
        plugin_directory=Path(os.environ["SIM_PLUGIN_DIR"]),
    )

    def live_scene_changed() -> None:
        scene.status = scene.simulator.status(scene.world)
        scene.revision += 1

    def live_apply_backends(physics: str, collision: str) -> tuple[Any, Any]:
        scene.dispatch("backend.apply", {
            "physics": physics,
            "collision": collision,
            "renderer": scene.config.renderer,
            "browserRenderer": scene.config.browser_renderer,
            "fixedTimeStep": scene.config.fixed_time_step,
            "realTimeFactor": scene.config.real_time_factor,
        })
        generation = scene.simulator.scene_snapshot(scene.world).scene_generation
        presentation.binding = (live_service.instance_id, generation)
        if presentation.mode == "real-sync":
            presentation.sync(authoritative_snapshot())
        return scene.simulator, scene.world

    live_service = HeadlessService(
        physics=scene.config.physics,
        collision=scene.config.collision,
        simulator=scene.simulator,
        world=scene.world,
        lock=scene.lock,
        instance_name="web-gateway-main",
        on_scene_changed=live_scene_changed,
        backend_applier=live_apply_backends,
        run_simulation=False,
    )
    live_server = LiveServer(live_service)
    agent = agent_client or AgentServiceClient(agent_host, agent_port)
    hub = EventHub()
    agent_state = GatewayAgentState(
        agent_state_storage,
        max_jobs=int(os.environ.get("SIM_WEB_AGENT_JOB_LIMIT", "1000")))
    jobs = agent_state.jobs
    job_tasks: dict[str, asyncio.Task[None]] = {}
    agent_scene_lock = asyncio.Lock()
    active_agent_job: str | None = None
    # A compact timeline is now one request rather than thousands of capture requests.
    app = web.Application(client_max_size=max(102 * 1024 * 1024, int(os.environ.get(
        "SIM_WEB_TIMELINE_MAX_BYTES", str(DEFAULT_TIMELINE_MAX_BYTES)))))
    ticker_task: asyncio.Task[None] | None = None
    checkpoint_directory = (
        agent_state_storage.parent / "conversation-states"
        if agent_state_storage is not None
        else PROJECT_ROOT / ".sim-cache" / "web" / "conversation-states"
    )

    def conversation_checkpoint(conversation_id: str) -> Path:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return checkpoint_directory / f"{digest}.simstate"

    def conversation_presentation(conversation_id: str) -> Path:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return checkpoint_directory / f"{digest}.presentation.json"

    def save_conversation_presentation(conversation_id: str) -> None:
        path = conversation_presentation(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(presentation.current(), ensure_ascii=False), encoding="utf-8")

    def load_conversation_presentation(conversation_id: str) -> dict[str, Any] | None:
        path = conversation_presentation(conversation_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def authoritative_snapshot() -> dict[str, Any]:
        current = scene.snapshot(agent_connected=agent.connected, agent_endpoint=agent.endpoint)
        current["features"] = features
        return current

    def presentation_frame(value: Any, authoritative: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("schemaVersion") != "1.0":
            raise PresentationError("INVALID_TIMELINE", "frame is not a SceneSnapshot v1")
        if (str(value.get("sceneId")) != scene.scene_id or
                int(value.get("worldId", -1)) != scene.world.handle or
                int(value.get("sceneGeneration", -1)) != live_service.scene_generation):
            raise PresentationError(
                "SCENE_BINDING_MISMATCH", "timeline frame belongs to another scene generation")
        raw_entities = value.get("entities")
        if not isinstance(raw_entities, list):
            raise PresentationError("INVALID_TIMELINE", "frame entities must be an array")
        # A capture contains many frames from one scene generation. Reusing the
        # authoritative base avoids rebuilding the full scene for every uploaded frame.
        current = dict(authoritative) if authoritative is not None else authoritative_snapshot()
        entities = [copy.deepcopy(current["entities"][0])]
        for raw in raw_entities:
            if not isinstance(raw, dict):
                raise PresentationError("INVALID_TIMELINE", "frame entity must be an object")
            local = raw.get("local")
            world_transform = raw.get("world")
            if not isinstance(local, dict) or not isinstance(world_transform, dict):
                raise PresentationError("INVALID_TIMELINE", "frame transform is missing")
            flags = [name for name in (
                "visual", "collider", "rigidBody", "sensor", "camera", "light", "robot")
                if name in raw]
            visual = copy.deepcopy(raw.get("visual"))
            collider = copy.deepcopy(raw.get("collider"))
            if isinstance(visual, dict):
                visual["resource"] = assets.register_resource(str(visual.get("resource", "")))
            if isinstance(collider, dict):
                collider["resource"] = assets.register_resource(
                    str(collider.get("resource", "")))
            camera = copy.deepcopy(raw.get("camera"))
            light = copy.deepcopy(raw.get("light"))
            sensor = raw.get("sensor")
            if isinstance(camera, dict):
                camera["verticalFovDegrees"] = math.degrees(
                    float(camera.pop("verticalFovRadians", 0.0)))
                if isinstance(sensor, dict):
                    camera["frameId"] = str(sensor.get("frameId", ""))
                    camera["updateRateHz"] = float(sensor.get("updateRateHz", 0.0))
            if isinstance(light, dict):
                light = {
                    "type": str(light.get("type", "directional")),
                    "color": copy.deepcopy(light.get("color", [1.0, 1.0, 1.0])),
                    "intensity": float(light.get("intensity", 1.0)),
                    "range": float(light.get("range", 0.0)),
                    "decay": float(light.get("decay", 2.0)),
                    "innerConeRadians": float(light.get(
                        "innerConeRadians", light.get("inner_cone_radians", 0.35))),
                    "outerConeRadians": float(light.get(
                        "outerConeRadians", light.get("outer_cone_radians", 0.7))),
                    "angularDiameterRadians": float(light.get(
                        "angularDiameterRadians", light.get(
                            "angular_diameter_radians", 0.0))),
                    "enabled": bool(light.get("enabled", True)),
                }
            kind = "robot" if "robot" in raw else "camera" if camera else "light" if light else (
                str(visual.get("shape", "entity")) if isinstance(visual, dict) else "entity")
            parent = raw.get("parent")
            entities.append({
                "id": int(raw["id"]),
                "name": str(raw.get("name", "Entity")),
                "parent": 0 if parent is None else int(parent),
                "kind": kind,
                "flags": flags,
                "robotManaged": False,
                "local": copy.deepcopy(local),
                "world": copy.deepcopy(world_transform),
                **({"visual": visual} if isinstance(visual, dict) else {}),
                **({"collider": collider} if isinstance(collider, dict) else {}),
                **({"rigidBody": copy.deepcopy(raw["rigidBody"])}
                   if isinstance(raw.get("rigidBody"), dict) else {}),
                **({"camera": camera} if isinstance(camera, dict) else {}),
                **({"light": light} if isinstance(light, dict) else {}),
                **({"robot": copy.deepcopy(raw["robot"])}
                   if isinstance(raw.get("robot"), dict) else {}),
            })
        current.update({
            "revision": int(value.get("revision", 0)),
            "simulationTime": float(value.get("simulationTime", 0.0)),
            "status": str(value.get("status", "stopped")),
            "entities": entities,
        })
        return current

    presentation = PresentationStore(
        authoritative_snapshot(), storage_path=presentation_storage,
        binding=(live_service.instance_id, live_service.scene_generation),
        scene_id=scene.scene_id)
    timeline_max_bytes = int(os.environ.get(
        "SIM_WEB_TIMELINE_MAX_BYTES", str(DEFAULT_TIMELINE_MAX_BYTES)))
    conversations = agent_state.conversations
    job_control_tokens: dict[str, str] = {}
    capture: dict[str, Any] | None = None
    metrics: dict[str, int] = {
        "agentTurnsStarted": 0,
        "agentTurnsSucceeded": 0,
        "agentTurnsFailed": 0,
        "agentTurnsCancelled": 0,
        "agentTurnsTimeout": 0,
        "agentTurnLatencyMsTotal": 0,
        "agentTurnLatencySamples": 0,
        "captureFrames": 0,
        "captureBytes": 0,
        "websocketConnections": 0,
    }

    def control_base_url(request: web.Request) -> str:
        """Return the Gateway origin the Agent Service must call for this job.

        A caller that embeds the app (or runs it on a non-default port) does not have
        enough information for the old hard-coded 8080 fallback.  The production CLI
        still passes an explicit loopback URL, while embedded deployments use the
        actual listener selected by aiohttp.
        """
        if agent_control_base_url:
            return agent_control_base_url.rstrip("/")
        return f"{request.scheme}://{request.host}"

    def require_agent_control(request: web.Request) -> str:
        job_id = request.headers.get("X-Agent-Job-Id")
        supplied = request.headers.get("X-Agent-Control-Token")
        expected = job_control_tokens.get(job_id) if job_id else None
        if (active_agent_job is None or job_id != active_agent_job or
                expected is None or supplied is None or
                not hmac.compare_digest(supplied, expected)):
            raise web.HTTPForbidden(text="agent control token is invalid")
        return job_id

    def restore_missing_lights(value: dict[str, Any]) -> dict[str, Any]:
        """Hydrate timelines saved before published frames preserved light components."""
        authoritative_lights = {
            int(item["id"]): item for item in authoritative_snapshot()["entities"]
            if isinstance(item.get("light"), dict)
        }
        for item in value.get("entities", []):
            source = authoritative_lights.get(int(item.get("id", -1)))
            if source is None or isinstance(item.get("light"), dict):
                continue
            item["light"] = copy.deepcopy(source["light"])
            item["flags"] = sorted(set(item.get("flags", [])) | {"light"})
            if item.get("kind") == "entity":
                item["kind"] = "light"
        return value

    def snapshot() -> dict[str, Any]:
        current = restore_missing_lights(presentation.current()["snapshot"])
        # PresentationStore persists world state, but Agent connectivity is live metadata.
        current["agent"] = {
            "connected": agent.connected,
            "endpoint": agent.endpoint,
        }
        current["features"] = features
        current["presentation"] = {
            "mode": presentation.mode,
            "revision": presentation.revision,
            "timeline": presentation.current()["timeline"],
            "detached": presentation.detached,
        }
        return current

    def sync_presentation() -> dict[str, Any]:
        presentation.sync(authoritative_snapshot())
        return snapshot()

    async def event(kind: str, payload: Any, actor: str = "system") -> None:
        await hub.publish({
            "eventId": uuid.uuid4().hex, "sceneId": scene.scene_id,
            "revision": scene.revision, "type": kind,
            "presentationRevision": presentation.revision,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": {"type": actor}, "payload": payload,
        })

    async def ensure_agent_animation_mode() -> None:
        if presentation.mode == "sync-and-animate":
            return
        current = presentation.set_mode("sync-and-animate", authoritative_snapshot())
        await event("presentation.mode_changed", current, "agent")

    async def save_conversation_checkpoint(conversation_id: str) -> None:
        await asyncio.to_thread(
            scene.save_state_file, conversation_checkpoint(conversation_id)
        )

    async def save_active_conversation_checkpoint() -> None:
        active = conversations.get(scene.scene_id)
        if active is not None:
            await save_conversation_checkpoint(active[0])
            save_conversation_presentation(active[0])

    async def prune_unrestorable_conversations() -> list[dict[str, Any]]:
        history = await agent.list_conversations()
        retained: list[dict[str, Any]] = []
        retained_checkpoints: set[Path] = set()
        active = conversations.get(scene.scene_id)
        for item in history:
            conversation_id = str(item.get("conversationId", ""))
            checkpoint = conversation_checkpoint(conversation_id)
            if conversation_id and checkpoint.is_file():
                retained.append(item)
                retained_checkpoints.add(checkpoint)
                continue
            if (active is not None and active[0] == conversation_id and
                    (active_agent_job is not None or agent_scene_lock.locked())):
                retained.append(item)
                continue
            if active is not None and active[0] == conversation_id:
                agent_state.unbind(scene.scene_id)
                active = None
            try:
                await agent.delete_conversation(conversation_id)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to remove unrestorable conversation %s", conversation_id
                )
        # A binding can outlive the conversation it names, and the loop above only notices
        # when that conversation is still in the Agent Service's history. Replacing the Agent
        # Service leaves it with an empty database, so the loop never runs and a stale binding
        # survives -- the Gateway then advertises an activeConversationId whose fetch 404s,
        # which fails the browser's entire startup sequence rather than just the history panel.
        if active is not None and not any(
                str(item.get("conversationId", "")) == active[0] for item in retained):
            if active_agent_job is None and not agent_scene_lock.locked():
                agent_state.unbind(scene.scene_id)
                active = None
        if checkpoint_directory.is_dir():
            for checkpoint in checkpoint_directory.glob("*.simstate"):
                if checkpoint not in retained_checkpoints:
                    checkpoint.unlink(missing_ok=True)
            retained_presentations = {
                conversation_presentation(str(item.get("conversationId", "")))
                for item in retained
            }
            for stored in checkpoint_directory.glob("*.presentation.json"):
                if stored not in retained_presentations:
                    stored.unlink(missing_ok=True)
        return retained

    def show_authoritative_scene() -> dict[str, Any]:
        authoritative = authoritative_snapshot()
        if presentation.mode == "real-sync":
            presentation.sync(authoritative)
        else:
            presentation.set_mode("real-sync", authoritative)
        return snapshot()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "Web Gateway", "version": 1,
                                  "agentConnected": agent.connected})

    async def bootstrap(_: web.Request) -> web.Response:
        return web.json_response(snapshot())

    async def command(request: web.Request) -> web.Response:
        body = await request.json()
        request_id = str(body.get("requestId", uuid.uuid4().hex))
        try:
            if agent_scene_lock.locked():
                raise SceneError("SCENE_BUSY", "an Agent turn owns the scene write lease",
                                 retryable=True)
            if presentation.mode == "sync-and-animate":
                raise SceneError("MODE_READ_ONLY",
                                 "scene commands are disabled in sync-and-animate mode")
            if body.get("sceneId") != scene.scene_id:
                raise SceneError("SCENE_NOT_FOUND", "Scene was not found")
            if int(body.get("baseRevision", -1)) != scene.revision:
                raise SceneError("REVISION_CONFLICT", "Scene changed after this edit started",
                                 retryable=True)
            async with agent_scene_lock:
                result = await asyncio.to_thread(
                    scene.dispatch, str(body.get("method", "")), dict(body.get("params", {})))
                await save_active_conversation_checkpoint()
            if body.get("method") == "backend.apply":
                live_service.rebind_runtime(
                    scene.simulator, scene.world, physics=scene.config.physics,
                    collision=scene.config.collision)
                agent_state.unbind(scene.scene_id)
            current = sync_presentation()
            await event("scene.changed", {"command": body.get("method"), "snapshot": current}, "user")
            return web.json_response({"requestId": request_id, "data": result,
                                      "revision": scene.revision})
        except SceneError as error:
            return web.json_response({"requestId": request_id, "error": {
                "code": error.code, "message": str(error), "retryable": error.retryable,
                "details": {"actualRevision": scene.revision}}}, status=409 if error.retryable else 400)
        except Exception as error:
            return web.json_response({"requestId": request_id, "error": {
                "code": "SIMULATION_ERROR", "message": str(error), "retryable": False}}, status=500)

    async def asset_list(_: web.Request) -> web.Response:
        assets.refresh()
        return web.json_response({"assets": assets.summaries()})

    async def asset_file(request: web.Request) -> web.StreamResponse:
        try:
            path, content_type = assets.file(request.match_info["asset_id"], request.match_info.get("path"))
            return web.FileResponse(path, headers={"Content-Type": content_type,
                                                   "Cache-Control": "private, max-age=3600"})
        except ValueError as error:
            raise web.HTTPNotFound(text=str(error)) from error

    async def import_remote_asset(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            url = str(body.get("url", "")).strip()
            if not url:
                raise web.HTTPBadRequest(text="url is required")
            record = await assets.fetch_remote(url)
            return web.json_response({"asset": record.summary()}, status=201)
        except (ValueError, TypeError) as error:
            raise web.HTTPBadRequest(text=str(error)) from error

    async def remote_proxy(request: web.Request) -> web.StreamResponse:
        import aiohttp as _aiohttp
        raw_url = request.rel_url.query.get("url", "").strip()
        if not raw_url:
            raise web.HTTPBadRequest(text="url parameter is required")
        parsed = urllib.parse.urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise web.HTTPBadRequest(text="url must be an http or https address")
        # Fetch the origin URL as-is (no extra query parameters added by the renderer).
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(raw_url, allow_redirects=True) as upstream:
                    if upstream.status != 200:
                        raise web.HTTPBadGateway(
                            text=f"remote server returned {upstream.status}")
                    body_bytes = await upstream.read()
                    content_type = upstream.headers.get(
                        "Content-Type", "application/octet-stream")
        except _aiohttp.ClientError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return web.Response(
            body=body_bytes,
            content_type=content_type.split(";")[0].strip(),
            headers={"Cache-Control": "private, max-age=3600",
                     "Access-Control-Allow-Origin": "*"},
        )

    async def load_simstate(request: web.Request) -> web.Response:
        reader = await request.multipart()
        data: bytes | None = None
        while (part := await reader.next()) is not None:
            if part.name == "file" and part.filename:
                data = await part.read(decode=False)
        if not data:
            raise web.HTTPBadRequest(text="simstate file is required")
        with tempfile.NamedTemporaryFile(suffix=".simstate", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            await asyncio.to_thread(scene.restore_state_file, tmp_path)
            live_service.rebind_runtime(
                scene.simulator, scene.world,
                physics=scene.config.physics, collision=scene.config.collision)
            presentation.binding = (live_service.instance_id, live_service.scene_generation)
            current = sync_presentation()
            await event("scene.changed", {"command": "state.load_simstate", "snapshot": current}, "user")
            return web.json_response({"revision": scene.revision})
        except Exception as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        finally:
            tmp_path.unlink(missing_ok=True)

    async def upload(request: web.Request) -> web.Response:
        reader = await request.multipart()
        entry = ""
        paths: list[str] = []
        uploads: list[tuple[str, bytes]] = []
        while (part := await reader.next()) is not None:
            if part.name == "entry":
                entry = await part.text()
            elif part.name == "path":
                paths.append(await part.text())
            elif part.name in {"file", "files"} and part.filename:
                uploads.append((part.filename, await part.read(decode=False)))
        if not uploads:
            raise web.HTTPBadRequest(text="asset file is required")
        try:
            if entry:
                if len(paths) != len(uploads):
                    raise ValueError("asset bundle paths do not match uploaded files")
                record = assets.save_bundle(entry, {
                    path: content for path, (_, content) in zip(paths, uploads, strict=True)
                })
            else:
                filename, content = uploads[0]
                record = assets.save_upload(filename, content)
            return web.json_response({"asset": record.summary()}, status=201)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error

    async def delete_asset(request: web.Request) -> web.Response:
        try:
            if scene.asset_in_use(request.match_info["asset_id"]):
                raise ValueError("asset is referenced by the active scene")
            assets.delete(request.match_info["asset_id"])
            return web.json_response({"deleted": request.match_info["asset_id"]})
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error

    async def sensor_frame(request: web.Request) -> web.Response:
        entity_id = int(request.match_info["entity_id"])
        mode = request.query.get("mode", "rgb")
        camera = Camera(scene.world, entity_id)
        try:
            await asyncio.to_thread(scene.simulator.step, scene.world, 1)
            width, height = camera.dimensions
            if mode == "depth":
                depth = camera.depth
                finite = depth.copy()
                near = float(finite.min())
                far = max(near + 1e-6, float(finite.max()))
                gray = ((1.0 - (finite - near) / (far - near)) * 255.0).clip(0, 255).astype("uint8")
                payload = encode_png(width, height, 1, gray.tobytes())
            else:
                payload = encode_png(width, height, 3, camera.rgb.tobytes())
            return web.Response(body=payload, content_type="image/png",
                                headers={"Cache-Control": "no-store"})
        except Exception as error:
            return web.json_response({"error": {"code": "SENSOR_UNAVAILABLE",
                                                 "message": str(error)}}, status=503)

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        metrics["websocketConnections"] += 1
        hub.clients.add(socket)
        await socket.send_json({"type": "connected", "revision": scene.revision})
        async for message in socket:
            if message.type == WSMsgType.TEXT and message.data == "ping":
                await socket.send_str("pong")
            elif message.type == WSMsgType.ERROR:
                break
        hub.clients.discard(socket)
        return socket

    def public_conversation(value: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        public = {
            key: value[key]
            for key in (
                "conversationId", "target", "sceneBinding", "presentationMode",
                "createdAt", "updatedAt", "title",
            )
            if key in value
        }
        if detail:
            public["messages"] = [
                {
                    key: message[key]
                    for key in ("role", "content", "turnId", "createdAt")
                    if key in message
                }
                for message in value.get("messages", [])
                if isinstance(message, dict)
            ]
            public["debugEvents"] = value.get("debugEvents", [])
            public["presentation"] = value.get("presentation")
        return public

    async def conversation_detail(conversation_id: str) -> dict[str, Any]:
        conversation = await agent.get_conversation(conversation_id)
        try:
            events = await agent.list_events(conversation_id)
        except (AttributeError, RuntimeError):
            events = []
        conversation["debugEvents"] = [
            {"eventId": item.get("eventId"), "turnId": item.get("turnId"),
             "payload": item.get("payload"), "createdAt": item.get("createdAt")}
            for item in events
            if item.get("type") == "turn.debug" and isinstance(item.get("payload"), dict)
        ]
        conversation["presentation"] = load_conversation_presentation(conversation_id)
        return conversation

    async def list_agent_conversations(_: web.Request) -> web.Response:
        if not features.get("agent.history", False):
            raise web.HTTPNotFound(text="Agent conversation history is disabled")
        try:
            history = await prune_unrestorable_conversations()
        except Exception as error:
            raise web.HTTPBadGateway(text="Agent conversation history is unavailable") from error
        active = conversations.get(scene.scene_id)
        # The browser fetches the advertised conversation immediately, and its whole startup
        # sequence fails if that fetch 404s. So only advertise one this Gateway can actually
        # serve, even if a binding somehow outlived its history entry.
        active_id = active[0] if active is not None else None
        if active_id is not None and not any(
                str(item.get("conversationId", "")) == active_id for item in history):
            active_id = None
        return web.json_response({
            "activeConversationId": active_id,
            "conversations": [public_conversation(item) for item in history],
        })

    async def delete_agent_conversation(request: web.Request) -> web.Response:
        if not features.get("agent.history", False):
            raise web.HTTPNotFound(text="Agent conversation history is disabled")
        if active_agent_job is not None or agent_scene_lock.locked():
            raise web.HTTPConflict(text="conversation cannot change during an Agent turn")
        conversation_id = request.match_info["conversation_id"]
        active = conversations.get(scene.scene_id)
        if active is not None and active[0] == conversation_id:
            raise web.HTTPConflict(text="active conversation cannot be deleted")
        async with agent_scene_lock:
            try:
                await agent.delete_conversation(conversation_id)
            except Exception as error:
                raise web.HTTPBadGateway(text="Agent conversation could not be deleted") from error
            conversation_checkpoint(conversation_id).unlink(missing_ok=True)
            conversation_presentation(conversation_id).unlink(missing_ok=True)
        return web.json_response({"deleted": conversation_id})

    async def get_agent_conversation(request: web.Request) -> web.Response:
        if not features.get("agent.history", False):
            raise web.HTTPNotFound(text="Agent conversation history is disabled")
        conversation_id = request.match_info["conversation_id"]
        if not conversation_checkpoint(conversation_id).is_file():
            raise web.HTTPNotFound(text="conversation scene state was not found")
        try:
            conversation = await conversation_detail(conversation_id)
        except Exception as error:
            raise web.HTTPBadGateway(text="Agent conversation is unavailable") from error
        if conversation.get("target") != "platform":
            raise web.HTTPNotFound(text="conversation was not found")
        return web.json_response(public_conversation(conversation, detail=True))

    async def create_agent_conversation(_: web.Request) -> web.Response:
        if not features.get("agent.history", False):
            raise web.HTTPNotFound(text="Agent conversation history is disabled")
        if active_agent_job is not None or agent_scene_lock.locked():
            raise web.HTTPConflict(text="conversation cannot change during an Agent turn")
        async with agent_scene_lock:
            previous = conversations.get(scene.scene_id)
            try:
                await save_active_conversation_checkpoint()
                await asyncio.to_thread(scene.reset_to_default)
                live_service.rebind_runtime(
                    scene.simulator, scene.world, physics=scene.config.physics,
                    collision=scene.config.collision)
                presentation.binding = (
                    live_service.instance_id, live_service.scene_generation
                )
                show_authoritative_scene()
                conversation = await agent.create_conversation(
                    scene_id=scene.scene_id,
                    sim_session_id=live_service.instance_id,
                    scene_generation=live_service.scene_generation,
                    connect=live_server.endpoint,
                    presentation_mode=presentation.mode,
                )
                conversation_id = str(conversation["conversationId"])
                await save_conversation_checkpoint(conversation_id)
                save_conversation_presentation(conversation_id)
            except Exception as error:
                if previous is not None:
                    checkpoint = conversation_checkpoint(previous[0])
                    if checkpoint.is_file():
                        try:
                            await asyncio.to_thread(scene.restore_state_file, checkpoint)
                            live_service.rebind_runtime(
                                scene.simulator, scene.world, physics=scene.config.physics,
                                collision=scene.config.collision)
                            presentation.binding = (
                                live_service.instance_id, live_service.scene_generation)
                            stored = load_conversation_presentation(previous[0])
                            if stored is not None:
                                presentation.restore_current(stored)
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "Previous conversation could not be restored")
                raise web.HTTPBadGateway(text="Agent conversation could not be created") from error
            next_mode = (
                "sync-and-animate"
                if features.get("presentation.syncAndAnimate", False)
                else presentation.mode
            )
            agent_state.bind(scene.scene_id, (
                conversation_id,
                live_service.instance_id,
                live_service.scene_generation,
                next_mode,
            ))
        current = show_authoritative_scene()
        result = public_conversation(conversation, detail=True)
        result.update({"active": True, "messages": [], "snapshot": current,
                      "presentation": presentation.current()})
        await event("scene.changed", {
            "command": "agent.conversation.create", "snapshot": current
        }, "user")
        return web.json_response(result, status=201)

    async def activate_agent_conversation(request: web.Request) -> web.Response:
        if not features.get("agent.history", False):
            raise web.HTTPNotFound(text="Agent conversation history is disabled")
        if active_agent_job is not None or agent_scene_lock.locked():
            raise web.HTTPConflict(text="conversation cannot change during an Agent turn")
        conversation_id = request.match_info["conversation_id"]
        checkpoint = conversation_checkpoint(conversation_id)
        if not checkpoint.is_file():
            raise web.HTTPNotFound(text="conversation scene state was not found")
        async with agent_scene_lock:
            try:
                conversation = await conversation_detail(conversation_id)
                if conversation.get("target") != "platform":
                    raise web.HTTPNotFound(text="conversation was not found")
                active = conversations.get(scene.scene_id)
                if active is not None and active[0] != conversation_id:
                    await save_conversation_checkpoint(active[0])
                    save_conversation_presentation(active[0])
                await asyncio.to_thread(scene.restore_state_file, checkpoint)
                live_service.rebind_runtime(
                    scene.simulator, scene.world, physics=scene.config.physics,
                    collision=scene.config.collision)
                presentation.binding = (
                    live_service.instance_id, live_service.scene_generation
                )
                stored_presentation = load_conversation_presentation(conversation_id)
                if stored_presentation is not None:
                    presentation.restore_current(stored_presentation)
            except web.HTTPException:
                raise
            except Exception as error:
                raise web.HTTPBadGateway(text="Agent conversation is unavailable") from error
            # Keep the restored conversation presentation (including its timeline).
            # `show_authoritative_scene` intentionally switches back to real-sync and
            # would discard the timeline that was just restored.
            current = authoritative_snapshot()
            if presentation.mode == "real-sync":
                presentation.sync(current)
            next_mode = (
                "sync-and-animate"
                if features.get("presentation.syncAndAnimate", False)
                else presentation.mode
            )
            agent_state.bind(scene.scene_id, (
                conversation_id,
                live_service.instance_id,
                live_service.scene_generation,
                next_mode,
            ))
        result = public_conversation(conversation, detail=True)
        result.update({"active": True, "snapshot": current,
                      "presentation": presentation.current()})
        await event("scene.changed", {
            "command": "agent.conversation.activate", "snapshot": current
        }, "user")
        return web.json_response(result)

    async def submit_agent(request: web.Request) -> web.Response:
        if not features.get("agent.conversation", False):
            raise web.HTTPNotFound(text="conversation Agent is disabled")
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise web.HTTPBadRequest(text="command text is required")
        request_id = str(body.get("requestId", uuid.uuid4().hex))
        job_id = uuid.uuid4().hex
        job_control_tokens[job_id] = uuid.uuid4().hex
        jobs[job_id] = {"id": job_id, "sceneId": scene.scene_id, "text": text,
                        "status": "queued", "createdAt": time.time(), "message": "Queued"}
        agent_state.put_job(jobs[job_id])

        async def run() -> None:
            nonlocal active_agent_job, capture
            job = jobs[job_id]
            gateway_url = control_base_url(request)
            started = time.perf_counter()
            try:
                async with agent_scene_lock:
                    try:
                        active_agent_job = job_id
                        await ensure_agent_animation_mode()
                        metrics["agentTurnsStarted"] += 1
                        job.update(status="running", message="Agent is processing the command")
                        await event("agent.turn_updated", dict(job), "agent")
                        await event("agent.job_updated", dict(job), "agent")
                        control_context = {
                            "gatewayBaseUrl": gateway_url,
                            "controlToken": job_control_tokens[job_id],
                            "jobId": job_id,
                            "sceneId": scene.scene_id,
                            "simSessionId": live_service.instance_id,
                            "sceneGeneration": live_service.scene_generation,
                            "liveEndpoint": live_server.endpoint,
                            "presentationMode": presentation.mode,
                            "robotAssets": [
                                {"assetId": item["id"], "name": item["name"]}
                                for item in assets.summaries()
                                if item.get("kind") == "robot"
                            ],
                        }

                        async def forward_debug(payload: dict[str, Any]) -> None:
                            entries = job.setdefault("debugEvents", [])
                            entries.append(payload)
                            item = payload.get("item")
                            kind = str(payload.get("type") or (
                                item.get("type") if isinstance(item, dict) else "working"))
                            job["message"] = f"Agent is working: {kind}"
                            await event("agent.job_updated", dict(job), "agent")
                            await event("agent.debug", {
                                "jobId": job_id,
                                "index": len(entries) - 1,
                                "event": payload,
                            }, "agent")

                        async def forward_progress(payload: dict[str, Any]) -> None:
                            phase = str(payload.get("phase", ""))
                            job["phase"] = phase
                            job["message"] = str(payload.get("label") or payload.get("title") or phase)
                            agent_state.put_job(job)
                            await event("agent.job_updated", dict(job), "agent")
                        cached = conversations.get(scene.scene_id)
                        conversation_id = (
                            cached[0]
                            if cached is not None and cached[1:] == (
                                live_service.instance_id,
                                live_service.scene_generation,
                                presentation.mode,
                            )
                            else None
                        )
                        conversation_id = await agent.ensure_conversation(
                            conversation_id=conversation_id,
                            scene_id=scene.scene_id,
                            sim_session_id=live_service.instance_id,
                            scene_generation=live_service.scene_generation,
                            connect=live_server.endpoint,
                            presentation_mode=presentation.mode,
                        )
                        agent_state.bind(scene.scene_id, (
                            conversation_id,
                            live_service.instance_id,
                            live_service.scene_generation,
                            presentation.mode,
                        ))
                        agent_task = asyncio.create_task(agent.execute(
                            conversation_id=conversation_id,
                            scene_id=scene.scene_id,
                            sim_session_id=live_service.instance_id,
                            scene_generation=live_service.scene_generation,
                            connect=live_server.endpoint,
                            text=text,
                            presentation_mode=presentation.mode,
                            control_context=control_context,
                            request_id=request_id,
                            debug=features.get("agent.debug", False),
                            on_debug=(forward_debug if features.get("agent.debug", False)
                                      else None),
                            on_progress=forward_progress,
                        ))
                        while not agent_task.done():
                            try:
                                result = await asyncio.wait_for(asyncio.shield(agent_task), 2.0)
                            except asyncio.TimeoutError:
                                job["message"] = "Agent is still working..."
                                agent_state.put_job(job)
                                await event("agent.job_updated", dict(job), "agent")
                            else:
                                break
                        if not agent_task.done():
                            result = await agent_task
                        else:
                            result = agent_task.result()
                        agent_state.bind(scene.scene_id, (
                            result.conversation_id,
                            live_service.instance_id,
                            live_service.scene_generation,
                            presentation.mode,
                        ))
                        await save_conversation_checkpoint(result.conversation_id)
                        if presentation.mode == "real-sync":
                            sync_presentation()
                            await event("scene.changed", {
                                "command": "agent.execute", "snapshot": snapshot()}, "agent")
                        job.update(status="success", message=result.message,
                                   conversationId=result.conversation_id,
                                   turnId=result.turn_id, revision=scene.revision)
                    finally:
                        if capture is not None and capture["sourceTurnId"] == job_id:
                            capture = None
                        job_control_tokens.pop(job_id, None)
                        active_agent_job = None
            except asyncio.TimeoutError:
                job.update(status="timeout", message="Agent request timed out")
            except asyncio.CancelledError:
                job.update(status="cancelled", message="Agent job was cancelled")
            except Exception as error:
                logging.getLogger(__name__).exception(
                    "Agent job %s failed with %s", job_id, type(error).__name__)
                if isinstance(error, AgentTurnError):
                    job.update(status="failed", message=str(error), errorCode=error.code)
                else:
                    job.update(status="failed", message="Agent execution failed")
            terminal_metric = {
                "success": "agentTurnsSucceeded",
                "failed": "agentTurnsFailed",
                "cancelled": "agentTurnsCancelled",
                "timeout": "agentTurnsTimeout",
            }.get(job["status"])
            if terminal_metric is not None:
                metrics[terminal_metric] += 1
            metrics["agentTurnLatencyMsTotal"] += int((time.perf_counter() - started) * 1000)
            metrics["agentTurnLatencySamples"] += 1
            agent_state.put_job(job)
            await event("agent.turn_updated", dict(job), "agent")
            await event("agent.job_updated", dict(job), "agent")
            job_tasks.pop(job_id, None)

        job_tasks[job_id] = asyncio.create_task(run())
        return web.json_response(jobs[job_id], status=202)

    async def get_job(request: web.Request) -> web.Response:
        job = jobs.get(request.match_info["job_id"])
        if job is None:
            raise web.HTTPNotFound(text="job was not found")
        return web.json_response(job)

    async def list_jobs(_: web.Request) -> web.Response:
        return web.json_response({"jobs": list(jobs.values())})

    async def cancel_job(request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        job = jobs.get(job_id)
        if job is None:
            raise web.HTTPNotFound(text="job was not found")
        if job["status"] in {"success", "failed", "cancelled", "timeout"}:
            return web.json_response(job)
        job.update(status="cancelled", message="Agent job was cancelled")
        agent_state.put_job(job)
        task = job_tasks.get(job_id)
        if task is not None:
            task.cancel()
        await event("agent.turn_updated", dict(job), "agent")
        await event("agent.job_updated", dict(job), "agent")
        return web.json_response(job, status=202)

    async def diagnostics(_: web.Request) -> web.Response:
        return web.json_response({"logs": scene.logs})

    async def get_metrics(_: web.Request) -> web.Response:
        return web.json_response(dict(metrics))

    async def get_presentation(_: web.Request) -> web.Response:
        return web.json_response(presentation.current())

    async def set_presentation_mode(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            if active_agent_job is not None:
                raise PresentationError(
                    "SCENE_BUSY", "presentation mode cannot change during an Agent turn")
            if int(body.get("basePresentationRevision", -1)) != presentation.revision:
                raise PresentationError(
                    "PRESENTATION_REVISION_CONFLICT", "presentation changed after this request")
            requested_mode = str(body.get("mode", ""))
            if (requested_mode == "sync-and-animate" and
                    not features.get("presentation.syncAndAnimate", False)):
                raise PresentationError("FEATURE_DISABLED", "sync-and-animate is disabled")
            current = presentation.set_mode(
                requested_mode, authoritative_snapshot())
            await event("presentation.mode_changed", current)
            return web.json_response(current)
        except PresentationError as error:
            return web.json_response({"error": {"code": error.code, "message": str(error),
                                                 "retryable": (
                                                     error.code ==
                                                     "PRESENTATION_REVISION_CONFLICT"),
                                                 "details": {
                                                     "actualPresentationRevision":
                                                     presentation.revision,
                                                 }}}, status=(
                                                     409 if error.code ==
                                                     "PRESENTATION_REVISION_CONFLICT" else 400))

    async def publish_snapshot(request: web.Request) -> web.Response:
        require_agent_control(request)
        await ensure_agent_animation_mode()
        current = presentation.publish_snapshot(authoritative_snapshot())
        active = conversations.get(scene.scene_id)
        if active is not None:
            save_conversation_presentation(active[0])
        await event("presentation.snapshot_published", current, "agent")
        return web.json_response(current)

    async def publish_timeline(request: web.Request) -> web.Response:
        require_agent_control(request)
        started = time.perf_counter()
        body = await request.json()
        try:
            if not isinstance(body, dict):
                raise PresentationError("INVALID_TIMELINE", "timeline must be an object")
            frames = body.get("frames", [])
            await ensure_agent_animation_mode()
            if not isinstance(frames, list):
                raise PresentationError("INVALID_TIMELINE", "frames must be an array")
            if not features.get("presentation.timeline", False):
                raise PresentationError("FEATURE_DISABLED", "timeline publishing is disabled")
            base_snapshot = None
            if "baseSnapshot" in body:
                # Register meshes/materials once; subsequent frames contain transforms only.
                base_snapshot = presentation_frame(body["baseSnapshot"])
                base_ids = {entity["id"] for entity in base_snapshot["entities"]}
                if len(base_ids) != len(base_snapshot["entities"]):
                    raise PresentationError("INVALID_TIMELINE", "base entity ids must be unique")
                converted = []
                for frame in frames:
                    if not isinstance(frame, dict) or frame.get("schemaVersion") != "1.0":
                        raise PresentationError("INVALID_TIMELINE", "invalid compact frame")
                    for key in ("sceneId", "worldId", "sceneGeneration"):
                        if frame.get(key) != body["baseSnapshot"].get(key):
                            raise PresentationError("SCENE_BINDING_MISMATCH", "compact frame binding differs from base")
                    entities = frame.get("entities")
                    if not isinstance(entities, list):
                        raise PresentationError("INVALID_TIMELINE", "frame entities must be an array")
                    seen = set()
                    transforms = []
                    for entity in entities:
                        if not isinstance(entity, dict):
                            raise PresentationError("INVALID_TIMELINE", "invalid transform entity")
                        entity_id = entity.get("id")
                        if (not isinstance(entity_id, int) or isinstance(entity_id, bool)
                                or entity_id not in base_ids or entity_id in seen):
                            raise PresentationError("INVALID_TIMELINE", "unknown or duplicate entity id")
                        seen.add(entity_id)
                        transformed = {"id": entity_id}
                        if "parent" in entity:
                            parent = entity["parent"]
                            if parent is not None and (not isinstance(parent, int) or
                                    isinstance(parent, bool) or parent not in base_ids):
                                raise PresentationError("INVALID_TIMELINE", "unknown parent entity")
                            transformed["parent"] = 0 if parent is None else parent
                        for key in ("local", "world"):
                            if key not in entity:
                                continue
                            transform = entity.get(key)
                            if not isinstance(transform, dict):
                                raise PresentationError("INVALID_TIMELINE", "frame transform is missing")
                            for component, size in (("translation", 3), ("rotationWxyz", 4), ("scale", 3)):
                                values = transform.get(component)
                                if (not isinstance(values, list) or len(values) != size or
                                        any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values)):
                                    raise PresentationError("INVALID_TIMELINE", "transform must contain finite vectors")
                            transformed[key] = transform
                        transforms.append(transformed)
                    converted.append({key: frame[key] for key in (
                        "schemaVersion", "sceneId", "worldId", "sceneGeneration", "revision",
                        "simulationTime", "status") if key in frame} | {"entities": transforms})
            else:
                authoritative = authoritative_snapshot()
                converted = [presentation_frame(frame, authoritative) for frame in frames]
            encoded_size = len(json.dumps(
                {"baseSnapshot": base_snapshot, "frames": converted},
                ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if encoded_size > timeline_max_bytes:
                raise PresentationError(
                    "TIMELINE_TOO_LARGE",
                    f"timeline exceeds the {timeline_max_bytes} byte limit")
            times = [float(frame["simulationTime"]) for frame in converted]
            if (any(not math.isfinite(value) for value in times) or
                    any(right <= left for left, right in zip(times, times[1:], strict=False))):
                raise PresentationError(
                    "INVALID_TIMELINE",
                    "frame simulationTime values must be finite and strictly increasing")
            current = presentation.publish_timeline(
                converted,
                active_agent_job or "unknown",
                base_snapshot=base_snapshot,
            )
            active = conversations.get(scene.scene_id)
            if active is not None:
                save_conversation_presentation(active[0])
            await event("presentation.timeline_published", current, "agent")
            logging.getLogger(__name__).debug(
                "Timeline upload frames=%d bytes=%d compact=%s elapsed_seconds=%.3f",
                len(converted), encoded_size, base_snapshot is not None, time.perf_counter() - started)
            return web.json_response(current)
        except (ValueError, TypeError, KeyError):
            return web.json_response({"error": {"code": "INVALID_TIMELINE",
                                                 "message": "invalid timeline value",
                                                 "retryable": False}}, status=400)
        except PresentationError as error:
            return web.json_response({"error": {"code": error.code, "message": str(error),
                                                 "retryable": False}}, status=400)

    async def resolve_agent_asset(request: web.Request) -> web.Response:
        require_agent_control(request)
        body = await request.json()
        identifier = str(body.get("asset", "")).strip()
        assets.refresh()
        try:
            record = assets.resolve_file(identifier)
        except ValueError:
            return web.json_response({"error": {"code": "ASSET_NOT_FOUND",
                                                 "message": "asset identifier is not unique or has no file"}},
                                     status=404)
        return web.json_response({"assetId": record.id, "path": str(record.path)})

    async def capture_start(request: web.Request) -> web.Response:
        nonlocal capture
        require_agent_control(request)
        await ensure_agent_animation_mode()
        if capture is not None:
            return web.json_response({"error": {"code": "CAPTURE_BUSY",
                                                 "message": "a capture is already active"}},
                                     status=409)
        await request.json()
        capture = {
            "captureId": uuid.uuid4().hex,
            "sourceTurnId": active_agent_job or "unknown",
            "frames": [],
            "bytes": 0,
            "authoritative": authoritative_snapshot(),
        }
        return web.json_response({key: value for key, value in capture.items()
                                  if key not in {"frames", "bytes", "authoritative"}}, status=201)

    async def capture_frame(request: web.Request) -> web.Response:
        nonlocal capture
        require_agent_control(request)
        if capture is None or request.match_info["capture_id"] != capture["captureId"]:
            raise web.HTTPNotFound(text="capture was not found")
        body = await request.json()
        frame = presentation_frame(body.get("snapshot"), capture.get("authoritative"))
        frames = capture["frames"]
        frame_time = float(frame["simulationTime"])
        if not math.isfinite(frame_time) or (frames and frame_time <= float(
                frames[-1]["simulationTime"])):
            return web.json_response({"error": {"code": "INVALID_TIMELINE",
                                                 "message": (
                                                     "capture times must be finite and strictly "
                                                     "increasing")}}, status=400)
        encoded = len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
        if capture["bytes"] + encoded > timeline_max_bytes:
            return web.json_response({"error": {"code": "CAPTURE_LIMIT",
                                                 "message": "capture byte limit reached"}},
                                     status=409)
        frames.append(frame)
        capture["bytes"] += encoded
        metrics["captureFrames"] += 1
        metrics["captureBytes"] += encoded
        return web.json_response({"captureId": capture["captureId"],
                                  "frameCount": len(frames)})

    async def capture_finish(request: web.Request) -> web.Response:
        nonlocal capture
        require_agent_control(request)
        if capture is None or request.match_info["capture_id"] != capture["captureId"]:
            raise web.HTTPNotFound(text="capture was not found")
        body = await request.json()
        value = capture
        capture = None
        if not bool(body.get("publish", True)):
            return web.json_response({"captureId": value["captureId"], "published": False})
        try:
            current = presentation.publish_timeline(
                value["frames"], value["sourceTurnId"])
            active = conversations.get(scene.scene_id)
            if active is not None:
                save_conversation_presentation(active[0])
        except PresentationError as error:
            return web.json_response({"error": {"code": error.code,
                                                 "message": str(error)}}, status=400)
        await event("presentation.timeline_published", current, "agent")
        return web.json_response(current)

    async def capture_abort(request: web.Request) -> web.Response:
        nonlocal capture
        require_agent_control(request)
        if capture is None or request.match_info["capture_id"] != capture["captureId"]:
            raise web.HTTPNotFound(text="capture was not found")
        capture_id = capture["captureId"]
        capture = None
        return web.json_response({"captureId": capture_id, "aborted": True})

    async def timeline_frame(request: web.Request) -> web.Response:
        try:
            frame = presentation.frame(
                request.match_info["timeline_id"], int(request.match_info["index"]))
            response = web.json_response(
                frame, headers={"Cache-Control": "private, max-age=3600"})
            response.enable_compression()
            return response
        except (KeyError, IndexError, ValueError) as error:
            raise web.HTTPNotFound(text="timeline frame was not found") from error

    async def timeline_data(request: web.Request) -> web.Response:
        try:
            response = web.Response(
                text=json.dumps(presentation.timeline_data(request.match_info["timeline_id"]),
                                separators=(",", ":")),
                content_type="application/json", zlib_executor_size=1024 * 1024,
                headers={"Cache-Control": "private, max-age=3600"})
            response.enable_compression()
            return response
        except KeyError as error:
            raise web.HTTPNotFound(text="timeline data was not found") from error

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/commands", command)
    app.router.add_get("/api/assets", asset_list)
    app.router.add_post("/api/assets/upload", upload)
    app.router.add_post("/api/assets/import-remote", import_remote_asset)
    app.router.add_post("/api/state/load-simstate", load_simstate)
    app.router.add_delete("/api/assets/{asset_id}", delete_asset)
    app.router.add_get("/api/assets/{asset_id}/file", asset_file)
    app.router.add_get("/api/assets/{asset_id}/file/{path:.*}", asset_file)
    app.router.add_get("/api/sensors/{entity_id}/frame", sensor_frame)
    app.router.add_post("/api/agent/jobs", submit_agent)
    app.router.add_get("/api/agent/jobs", list_jobs)
    app.router.add_get("/api/agent/jobs/{job_id}", get_job)
    app.router.add_post("/api/agent/jobs/{job_id}/cancel", cancel_job)
    app.router.add_get("/api/agent/conversations", list_agent_conversations)
    app.router.add_post("/api/agent/conversations", create_agent_conversation)
    app.router.add_delete(
        "/api/agent/conversations/{conversation_id}", delete_agent_conversation)
    app.router.add_get(
        "/api/agent/conversations/{conversation_id}", get_agent_conversation)
    app.router.add_post(
        "/api/agent/conversations/{conversation_id}/activate",
        activate_agent_conversation)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_get("/api/metrics", get_metrics)
    app.router.add_get("/api/presentation", get_presentation)
    app.router.add_post("/api/presentation/mode", set_presentation_mode)
    app.router.add_post("/api/agent-control/presentation/snapshot", publish_snapshot)
    app.router.add_post("/api/agent-control/presentation/timeline", publish_timeline)
    app.router.add_post("/api/agent-control/assets/resolve", resolve_agent_asset)
    app.router.add_post("/api/agent-control/captures", capture_start)
    app.router.add_post("/api/agent-control/captures/{capture_id}/frames", capture_frame)
    app.router.add_post("/api/agent-control/captures/{capture_id}/finish", capture_finish)
    app.router.add_post("/api/agent-control/captures/{capture_id}/abort", capture_abort)
    app.router.add_get("/api/timelines/{timeline_id}/frames/{index}", timeline_frame)
    app.router.add_get("/api/timelines/{timeline_id}/data", timeline_data)
    app.router.add_get("/events", websocket)
    if static_directory is not None and static_directory.is_dir():
        index_path = static_directory / "index.html"

        async def editor_index(_: web.Request) -> web.FileResponse:
            return web.FileResponse(index_path)

        assets_directory = static_directory / "assets"
        if assets_directory.is_dir():
            app.router.add_static("/assets/", assets_directory, show_index=False)
        app.router.add_get("/", editor_index)
        app.router.add_get("/{route:.*}", editor_index)

    async def ticker(application: web.Application) -> None:
        last_publish = 0.0
        last_revision = scene.revision
        while True:
            started = time.perf_counter()
            changed = await asyncio.to_thread(scene.tick)
            revision_changed = scene.revision != last_revision
            if (changed or revision_changed) and started - last_publish >= 0.05:
                if presentation.mode == "real-sync":
                    await event("simulation.tick", {"snapshot": sync_presentation()})
                last_publish = started
                last_revision = scene.revision
            elapsed = time.perf_counter() - started
            interval = scene.config.fixed_time_step / max(scene.config.real_time_factor, 0.01)
            await asyncio.sleep(max(0.001, interval - elapsed))

    async def start_background(application: web.Application) -> None:
        nonlocal ticker_task
        try:
            await asyncio.wait_for(agent.probe(), timeout=3.0)
        except Exception:
            pass
        try:
            await asyncio.wait_for(prune_unrestorable_conversations(), timeout=5.0)
        except Exception:
            logging.getLogger(__name__).warning(
                "Agent conversation cleanup could not be completed")
        active = conversations.get(scene.scene_id)
        if active is not None:
            checkpoint = conversation_checkpoint(active[0])
            if checkpoint.is_file():
                try:
                    await asyncio.to_thread(scene.restore_state_file, checkpoint)
                    live_service.rebind_runtime(
                        scene.simulator, scene.world, physics=scene.config.physics,
                        collision=scene.config.collision)
                    presentation.binding = (
                        live_service.instance_id, live_service.scene_generation
                    )
                    show_authoritative_scene()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Active conversation scene could not be restored")
        live_server.start()
        for old_scene, binding in list(conversations.items()):
            conversation_id, instance_id, generation, mode = binding
            try:
                turns = await asyncio.wait_for(agent.list_turns(conversation_id), timeout=3.0)
            except Exception:
                continue
            for turn in turns:
                if str(turn.get("status")) not in {"queued", "running", "interrupted"}:
                    continue
                recovered = {
                    "id": f"recovered-{turn.get('turnId')}",
                    "sceneId": old_scene,
                    "status": "interrupted" if turn.get("status") in {
                        "queued", "running"} else str(turn.get("status")),
                    "createdAt": time.time(),
                    "message": str(turn.get("message", "Agent turn was interrupted")),
                    "conversationId": conversation_id,
                    "turnId": str(turn.get("turnId", "")),
                    "recovered": True,
                }
                jobs[recovered["id"]] = recovered
                agent_state.put_job(recovered)
        ticker_task = asyncio.create_task(ticker(application))

    async def stop_background(application: web.Application) -> None:
        del application
        if ticker_task is None:
            live_server.close()
            live_service.close()
            scene.close()
            return
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass
        for task in list(job_tasks.values()):
            task.cancel()
        if job_tasks:
            await asyncio.gather(*list(job_tasks.values()), return_exceptions=True)
        live_server.close()
        live_service.close()
        scene.close()

    app[LIVE_ENDPOINT_KEY] = live_server.endpoint
    app[LIVE_INSTANCE_ID_KEY] = live_service.instance_id

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="RobotSimPlatform Web Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--agent-host")
    parser.add_argument("--agent-port", type=int, default=8765)
    parser.add_argument("--static-dir", type=Path,
                        default=PROJECT_ROOT / "web" / "dist")
    parser.add_argument("--feature-config", type=Path,
                        default=Path(os.environ.get(
                            "SIM_WEB_FEATURE_CONFIG", DEFAULT_FEATURE_CONFIG)))
    args = parser.parse_args()
    install_standard_logging("web_gateway")
    static_directory = args.static_dir.resolve() if args.static_dir.is_dir() else None
    web.run_app(create_app(agent_host=args.agent_host, agent_port=args.agent_port,
                           static_directory=static_directory,
                           feature_config=args.feature_config,
                           agent_control_base_url=f"http://127.0.0.1:{args.port}",
                           presentation_storage=(PROJECT_ROOT / ".sim-cache" / "web" /
                                                 "presentation.json"),
                           agent_state_storage=(PROJECT_ROOT / ".sim-cache" / "web" /
                                                "agent-state.json")),
                host=args.host, port=args.port, print=lambda message: print(message, flush=True))


if __name__ == "__main__":
    main()
