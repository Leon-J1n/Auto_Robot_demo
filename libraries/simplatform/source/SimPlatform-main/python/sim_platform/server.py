from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import socket
import socketserver
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any, Callable

from .live import PROTOCOL_VERSION, default_registry_directory, parse_endpoint
from .sdk import Camera, Entity, LiDAR, Robot, Simulator, State, Transform, World

from ._logging import error as _log_error, info as _log_info, log_verbose, warning as _log_warning


class AgentRpcError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _ObjectRecord:
    object_id: str
    entity: Entity
    kind: str
    name: str
    asset_path: str | None = None
    links: dict[str, str] = field(default_factory=dict)


@dataclass
class _SessionContext:
    workspace: str = "default"
    hello_complete: bool = False


class HeadlessService:
    def __init__(self, *, physics: str = "none", cache_directory: str | None = None,
                 collision: str = "fcl",
                 instance_name: str | None = None, runner_interval: float = 0.002,
                 simulator: Simulator | None = None, world: World | None = None,
                 lock: threading.RLock | None = None,
                 on_scene_changed: Callable[[], None] | None = None,
                 backend_applier: Callable[[str, str], tuple[Simulator, World]] | None = None,
                 run_simulation: bool = True):
        if (simulator is None) != (world is None):
            raise ValueError("simulator and world must be provided together")
        self._owns_runtime = simulator is None
        self.physics = physics
        self.collision = collision
        self.cache_directory = cache_directory
        self._backend_applier = backend_applier
        self.simulator = simulator or Simulator(
            physics=physics, collision=collision, cache_directory=cache_directory)
        self.world = world or self.simulator.create_world()
        self.entities: dict[int, Entity] = {}
        self.robots: dict[int, Robot] = {}
        self.states: dict[int, State] = {}
        self.instance_id = str(uuid.uuid4())
        self.instance_name = instance_name
        self.process_start_time = dt.datetime.now(dt.timezone.utc).isoformat()
        initial_snapshot = self.simulator.scene_snapshot(self.world)
        self.scene_generation = initial_snapshot.scene_generation
        self.scene_revision = initial_snapshot.revision
        self.endpoints: tuple[str, ...] = ()
        self.objects: dict[str, _ObjectRecord] = {}
        self.object_ids_by_handle: dict[int, str] = {}
        self.workspaces: dict[str, dict[str, str]] = {}
        self.workspace_revisions: dict[str, int] = {}
        self.shared_buffers: dict[str, shared_memory.SharedMemory] = {}
        self.lock = lock or threading.RLock()
        self._on_scene_changed = on_scene_changed
        self.shutdown_callback: Any = None
        self._runner_interval = runner_interval
        self._runner_stop = threading.Event()
        self._runner = (threading.Thread(
            target=self._run_simulation, name="simulation-runner", daemon=True)
            if run_simulation else None)
        self._register_existing_objects()
        if self._runner is not None:
            self._runner.start()
        _log_info("RPCService", f"headless runtime ready instance={self.instance_id} "
                  f"physics={self.physics} collision={self.collision} runner={self._runner is not None}")

    def close(self) -> None:
        log_verbose(f"HeadlessService.close instance={self.instance_id} "
                    f"shared_buffers={len(self.shared_buffers)}")
        self._runner_stop.set()
        if self._runner is not None:
            self._runner.join(timeout=5.0)
        for buffer in list(self.shared_buffers.values()):
            buffer.close()
            try:
                buffer.unlink()
            except FileNotFoundError:
                pass
        self.shared_buffers.clear()
        if self._owns_runtime:
            self.simulator.close()

    def rebind_runtime(
        self, simulator: Simulator, world: World, *, physics: str | None = None,
        collision: str | None = None,
    ) -> None:
        """Rebind an embedded service after its owner replaces the native runtime."""
        if self._owns_runtime:
            raise RuntimeError("an owned runtime cannot be rebound")
        with self.lock:
            aliases = {name: dict(values) for name, values in self.workspaces.items()}
            revisions = dict(self.workspace_revisions)
            self.simulator = simulator
            self.world = world
            if physics is not None:
                self.physics = physics
            if collision is not None:
                self.collision = collision
            self.entities.clear()
            self.robots.clear()
            self.states.clear()
            self.objects.clear()
            self.object_ids_by_handle.clear()
            self.workspaces.clear()
            self.workspace_revisions.clear()
            snapshot = self.simulator.scene_snapshot(self.world)
            self.scene_generation = snapshot.scene_generation
            self.scene_revision = snapshot.revision
            self._register_existing_objects()
            self.workspaces = {
                name: {alias: object_id for alias, object_id in values.items()
                       if object_id in self.objects}
                for name, values in aliases.items()
            }
            self.workspace_revisions = revisions

    def dispatch(self, request: dict[str, Any],
                 session: _SessionContext | None = None) -> dict[str, Any]:
        log_verbose("HeadlessService.dispatch validates, invokes, and serializes one RPC request")
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        try:
            if not isinstance(method, str) or not isinstance(params, dict):
                raise AgentRpcError("invalid_request", "method must be a string and params an object")
            with self.lock:
                result = self._invoke(method, params, session or _SessionContext())
            response = {"id": request_id, "ok": True, "result": result}
        except AgentRpcError as error:
            _log_warning("AgentRPC", f"request rejected method={method} request_id={request_id} "
                         f"code={error.code} message={error}")
            response = {"id": request_id, "ok": False,
                        "error": {"code": error.code, "message": str(error)}}
        except Exception as error:  # C API and validation failures become stable Agent errors.
            _log_error("AgentRPC", f"request failed method={method} request_id={request_id} "
                       f"error={type(error).__name__}: {error}")
            response = {"id": request_id, "ok": False,
                        "error": {"code": "simulation_error", "message": str(error)}}
        write_log = _log_info if response["ok"] else _log_error
        write_log("AgentRPC", f"request={json.dumps(request, ensure_ascii=False, separators=(',', ':'))} "
                  f"response={json.dumps(response, ensure_ascii=False, separators=(',', ':'))}")
        return response

    def _invoke(self, method: str, params: dict[str, Any], session: _SessionContext) -> Any:
        if method == "session.hello":
            requested = int(params.get("protocol_version", 0))
            if requested != PROTOCOL_VERSION:
                raise AgentRpcError(
                    "protocol_mismatch",
                    f"server protocol {PROTOCOL_VERSION} is incompatible with client {requested}",
                )
            workspace = str(params.get("workspace", "default")).strip()
            if not workspace or len(workspace) > 128:
                raise AgentRpcError("invalid_argument", "workspace must contain 1 to 128 characters")
            session.workspace = workspace
            session.hello_complete = True
            self.workspaces.setdefault(workspace, {})
            self.workspace_revisions.setdefault(workspace, 0)
            return {
                "protocol_version": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "instance_id": self.instance_id,
                "instance_name": self.instance_name,
                "process_start_time": self.process_start_time,
                "scene_revision": self.scene_revision,
                "scene_generation": self.scene_generation,
                "endpoints": list(self.endpoints),
                "capabilities": ["scene.read", "scene.write", "workspace.aliases",
                                 "robot.links", "robot.control", "simulation.control",
                                 "simulation.state", "backend.configure",
                                 "collision.query", "sensor.camera", "sensor.lidar"],
            }
        if method == "session.ping":
            return {"time": time.time(), "instance_id": self.instance_id}
        if method == "session.shutdown":
            if self.shutdown_callback is None:
                raise AgentRpcError("unsupported_operation", "service cannot be shut down remotely")
            threading.Thread(target=self.shutdown_callback, daemon=True).start()
            return {"closing": True}
        if method == "scene.query":
            return {"entities": [
                {"handle": handle,
                 "translation": list(entity.transform.translation),
                 "kind": "robot" if handle in self.robots else "entity",
                 "object": self._object_ref(self.objects[self.object_ids_by_handle[handle]])
                 if handle in self.object_ids_by_handle else None}
                for handle, entity in sorted(self.entities.items())
            ]}
        if method == "scene.snapshot":
            return {"snapshot": self.simulator.scene_snapshot(self.world).as_dict()}
        if method == "scene.transforms":
            return self.simulator.scene_transforms(self.world)
        if method == "scene.create_entity":
            parent_value = params.get("parent")
            parent = (None if parent_value is None or parent_value == 0 else
                      self._resolve_object(parent_value).entity if isinstance(parent_value, dict)
                      else self.entities.get(int(parent_value)))
            entity = self.world.create_entity(str(params.get("name", "entity")), parent)
            self.entities[entity.handle] = entity
            record = self._register_object(entity, "entity", str(params.get("name", "entity")))
            self._bind_optional_alias(session.workspace, params.get("alias"), record)
            self._scene_changed()
            return {"entity": entity.handle, "object": self._object_ref(record)}
        if method == "scene.create_primitive":
            kind = str(params.get("kind", "")).strip().lower()
            primitives = {
                "cube": ("Cube 1m", "box", (1.0, 1.0, 1.0)),
                "sphere": ("Sphere 1m", "sphere", (0.5, 0.5, 0.5)),
                "plane": ("Plane 1m", "box", (1.0, 1.0, 0.02)),
                "capsule": ("Capsule 1m", "capsule", (0.25, 0.25, 0.5)),
                "cylinder": ("Cylinder 1m", "cylinder", (0.5, 0.5, 1.0)),
            }
            if kind == "box":
                kind = "cube"
            if kind not in primitives:
                raise AgentRpcError(
                    "invalid_argument",
                    "primitive kind must be cube, sphere, plane, capsule, or cylinder",
                )
            default_name, shape, default_size = primitives[kind]
            name = str(params.get("name", default_name))
            size = self._vector(params.get("size", default_size), 3, "size")
            if any(value <= 0.0 for value in size):
                raise AgentRpcError("invalid_argument", "size values must be positive")
            entity = self.world.create_primitive(name, shape, size)
            ifps_count = int(params.get("ifps_sphere_count", 0))
            if ifps_count:
                entity.ifps_sphere_count = ifps_count
            record = self._register_object(entity, "entity", name)
            self._bind_optional_alias(session.workspace, params.get("alias"), record)
            self._scene_changed()
            return {"entity": entity.handle, "name": name, "visual": True,
                    "collider": True, "object": self._object_ref(record)}
        if method == "scene.set_transform":
            entity = self._entity(params)
            entity.transform = Transform(
                translation=tuple(float(value) for value in params.get("translation", [0, 0, 0])),
                rotation_wxyz=tuple(float(value) for value in params.get("rotation_wxyz", [1, 0, 0, 0])),
                scale=tuple(float(value) for value in params.get("scale", [1, 1, 1])),
            )
            self._scene_changed()
            return {"entity": entity.handle}
        if method == "scene.get_transform":
            record = self._resolve_object(params.get("object"))
            value = record.entity.transform
            return {"translation": list(value.translation),
                    "rotation_wxyz": list(value.rotation_wxyz), "scale": list(value.scale)}
        if method == "scene.set_rigid_body":
            record = self._resolve_object(params.get("object"))
            enabled = bool(params.get("enabled", True))
            dynamic = bool(params.get("dynamic", False))
            gravity = bool(params.get("gravity", True))
            try:
                mass = float(params.get("mass", 1.0))
                restitution = float(params.get("restitution", 0.0))
            except (TypeError, ValueError) as error:
                raise AgentRpcError("invalid_argument", "mass and restitution must be numbers") from error
            friction = self._vector(params.get("friction", (1.0, 0.005, 0.0001)), 3, "friction")
            if mass <= 0.0 or not 0.0 <= restitution <= 1.0 or any(value < 0.0 for value in friction):
                raise AgentRpcError("invalid_argument", "invalid rigid body parameters")
            record.entity.set_rigid_body(
                enabled=enabled, dynamic=dynamic, gravity=gravity, mass=mass,
                friction=friction, restitution=restitution,
            )
            self._scene_changed()
            return {"object": self._object_ref(record), "physics": self.physics}
        if method == "scene.set_name":
            record = self._resolve_object(params.get("object"))
            record.entity.set_name(str(params.get("name", "")))
            record.name = str(params["name"]).strip()
            self._scene_changed()
            return {"object": self._object_ref(record), "name": record.name}
        if method == "scene.set_visual":
            record = self._resolve_object(params.get("object"))
            record.entity.set_visual(
                visible=bool(params.get("visible", True)),
                min_lod=int(params.get("min_lod", 0)),
                max_lod=int(params.get("max_lod", 7)),
            )
            self._scene_changed()
            return {"object": self._object_ref(record)}
        if method == "scene.get_ifps_sphere_count":
            record = self._resolve_object(params.get("object"))
            return {"count": record.entity.ifps_sphere_count}
        if method == "scene.set_ifps_sphere_count":
            record = self._resolve_object(params.get("object"))
            record.entity.ifps_sphere_count = int(params.get("count", 0))
            self._scene_changed()
            return {"count": record.entity.ifps_sphere_count}
        if method == "scene.apply_wrench":
            record = self._resolve_object(params.get("object"))
            record.entity.apply_wrench(
                self._vector(params.get("force", (0, 0, 0)), 3, "force"),
                self._vector(params.get("torque", (0, 0, 0)), 3, "torque"),
            )
            return {"object": self._object_ref(record)}
        if method == "scene.set_transforms":
            values = params.get("items")
            if not isinstance(values, list):
                raise AgentRpcError("invalid_argument", "items must be an array")
            entities: list[Entity] = []
            transforms: list[Transform] = []
            for value in values:
                if not isinstance(value, dict):
                    raise AgentRpcError("invalid_argument", "transform item must be an object")
                record = self._resolve_object(value.get("object"))
                entities.append(record.entity)
                transforms.append(Transform(
                    translation=self._vector(value.get("translation", (0, 0, 0)), 3, "translation"),
                    rotation_wxyz=self._vector(value.get("rotation_wxyz", (1, 0, 0, 0)), 4, "rotation_wxyz"),
                    scale=self._vector(value.get("scale", (1, 1, 1)), 3, "scale"),
                ))
            self.world.set_transforms(entities, transforms)
            self._scene_changed()
            return {"count": len(entities)}
        if method == "scene.update_transform":
            record = self._resolve_object(params.get("object"))
            current = record.entity.transform
            record.entity.transform = Transform(
                translation=self._vector(params.get("translation", current.translation), 3,
                                         "translation"),
                rotation_wxyz=self._vector(
                    params.get("rotation_wxyz", current.rotation_wxyz), 4, "rotation_wxyz"),
                scale=self._vector(params.get("scale", current.scale), 3, "scale"),
            )
            self._scene_changed()
            return {"object": self._object_ref(record), "scene_revision": self.scene_revision}
        if method == "scene.set_parent":
            record = self._resolve_object(params.get("object"))
            parent_value = params.get("parent")
            parent = None if parent_value is None else self._resolve_object(parent_value).entity
            record.entity.set_parent(parent, preserve_world=bool(params.get("preserve_world", False)))
            self._scene_changed()
            return {"object": self._object_ref(record), "scene_revision": self.scene_revision}
        if method == "scene.resolve_object":
            return {"object": self._object_ref(self._resolve_object(params.get("object")))}
        if method == "scene.find_object":
            record = self.objects.get(str(params.get("object_id", "")))
            if record is None:
                raise AgentRpcError("object_not_found", "object id was not found")
            return {"object": self._object_ref(record)}
        if method == "scene.destroy_object":
            record = self._resolve_object(params.get("object"))
            record.entity.destroy()
            self._purge_destroyed_objects()
            self._scene_changed()
            return {"object_id": record.object_id, "destroyed": True,
                    "scene_revision": self.scene_revision}
        if method == "scene.load_asset":
            entity = self.world.load_asset(str(params["path"]))
            self.entities[entity.handle] = entity
            record = self._register_object(entity, "entity", Path(str(params["path"])).stem,
                                           self._normalized_path(params["path"]))
            self._bind_optional_alias(session.workspace, params.get("alias"), record)
            self._scene_changed()
            return {"entity": entity.handle, "object": self._object_ref(record)}
        if method == "scene.create_camera":
            parent_value = params.get("parent")
            parent = None if parent_value is None else self._resolve_object(parent_value).entity
            camera = self.world.create_camera(
                parent,
                frame_id=str(params.get("frame_id", "camera")),
                update_rate_hz=float(params.get("update_rate_hz", 30.0)),
                width=int(params.get("width", 640)), height=int(params.get("height", 480)),
                vertical_fov_radians=float(params.get("vertical_fov_radians", 1.0471975512)),
                near_plane=float(params.get("near_plane", 0.01)),
                far_plane=float(params.get("far_plane", 1000.0)),
            )
            record = self._register_object(camera, "camera", str(params.get("frame_id", "camera")))
            self._scene_changed()
            return {"object": self._object_ref(record)}
        if method == "scene.create_lidar":
            parent = self._resolve_object(params.get("parent")).entity
            lidar = self.world.create_lidar(
                parent,
                frame_id=str(params.get("frame_id", "lidar")),
                update_rate_hz=float(params.get("update_rate_hz", 10.0)),
                horizontal_samples=int(params.get("horizontal_samples", 1024)),
                vertical_channels=int(params.get("vertical_channels", 16)),
                min_distance=float(params.get("min_distance", 0.05)),
                max_distance=float(params.get("max_distance", 100.0)),
            )
            record = self._register_object(lidar, "lidar", str(params.get("frame_id", "lidar")))
            self._scene_changed()
            return {"object": self._object_ref(record)}
        if method == "scene.load_robot":
            record = self._load_robot(params["path"])
            self._bind_optional_alias(session.workspace, params.get("alias"), record)
            self._scene_changed()
            robot = self.robots[record.entity.handle]
            return {"robot": robot.handle, "joint_count": robot.joint_count,
                    "object": self._object_ref(record)}
        if method == "scene.ensure_robot":
            alias = self._require_alias(params.get("alias"))
            path = self._normalized_path(params.get("path"))
            existing_id = self.workspaces.setdefault(session.workspace, {}).get(alias)
            if existing_id is not None:
                record = self.objects.get(existing_id)
                if record is None:
                    raise AgentRpcError("stale_object", f"alias {alias!r} refers to a deleted object")
                if record.kind != "robot" or record.asset_path != path:
                    raise AgentRpcError(
                        "alias_conflict", f"alias {alias!r} is bound to a different object"
                    )
                return {"object": self._object_ref(record), "created": False}
            record = self._load_robot(path)
            self._bind_alias(session.workspace, alias, record)
            self._scene_changed()
            return {"object": self._object_ref(record), "created": True}
        if method == "simulation.physics_contacts":
            return {"contacts": self.simulator.physics_contacts(self.world)}
        if method == "simulation.step":
            count = int(params.get("count", 1))
            if count <= 0:
                raise AgentRpcError("invalid_argument", "step count must be positive")
            if params.get("paced", True):
                self.simulator.step(self.world, count)
            else:
                self.simulator.step_unpaced(self.world, count)
            self._scene_changed()
            return {"time": self.simulator.time(self.world)}
        if method == "simulation.step_profiled":
            count = int(params.get("count", 1))
            if count <= 0:
                raise AgentRpcError("invalid_argument", "step count must be positive")
            elapsed_ms = self.simulator.step_profiled(self.world, count)
            self._scene_changed()
            return {"time": self.simulator.time(self.world), "elapsed_ms": elapsed_ms}
        if method == "simulation.poll_event":
            value = self.simulator.poll_event(self.world)
            return {"event": None if value is None else {
                "type": value.type, "world": value.world, "entity": value.entity,
                "simulation_time": value.simulation_time, "detail": value.detail,
            }}
        if method == "simulation.start":
            self.simulator.start(self.world)
            self._scene_changed()
            return {"status": "running"}
        if method == "simulation.pause":
            self.simulator.pause(self.world)
            self._scene_changed()
            return {"status": "paused"}
        if method == "simulation.status":
            return {"status": self.simulator.status(self.world),
                    "time": self.simulator.time(self.world)}
        if method == "simulation.sync_physics":
            # Direct joint writes leave the backends holding stale geometry; callers that probe
            # collisions straight after a write must sync first or get answers for the old pose.
            self.simulator.sync_physics(self.world)
            return {"synced": "physics"}
        if method == "simulation.sync_collision":
            self.simulator.sync_collision(self.world)
            return {"synced": "collision"}
        if method == "simulation.reset":
            self.simulator.reset(self.world)
            self.scene_generation += 1
            self._refresh_registry()
            self._scene_changed()
            return {"time": self.simulator.time(self.world),
                    "scene_generation": self.scene_generation,
                    "objects": [self._object_ref(record) for record in self.objects.values()]}
        if method == "backend.status":
            return {"physics": self.physics, "collision": self.collision}
        if method == "backend.apply":
            physics = str(params.get("physics", self.physics)).lower()
            collision = str(params.get("collision", self.collision)).lower()
            if physics not in {"none", "mujoco", "bullet3"}:
                raise AgentRpcError("invalid_argument", "physics must be none, mujoco, or bullet3")
            if collision not in {"fcl", "bullet3", "coal", "ifps-cpu", "ifps-gpu"}:
                raise AgentRpcError(
                    "invalid_argument",
                    "collision must be fcl, bullet3, coal, ifps-cpu, or ifps-gpu",
                )
            if physics == self.physics and collision == self.collision:
                return {"physics": self.physics, "collision": self.collision, "changed": False,
                        "scene_generation": self.scene_generation}
            if self._backend_applier is not None:
                simulator, world = self._backend_applier(physics, collision)
                self.rebind_runtime(simulator, world)
            elif self._owns_runtime:
                with tempfile.TemporaryDirectory(prefix="sim-live-backend-") as directory:
                    state_path = Path(directory) / "scene.simstate"
                    self.simulator.save_state_file(self.world, state_path)
                    replacement = Simulator(
                        physics=physics, collision=collision,
                        cache_directory=self.cache_directory,
                    )
                    replacement_world = replacement.create_world()
                    try:
                        replacement.restore_state_file(replacement_world, state_path)
                    except Exception:
                        replacement.close()
                        raise
                    old_simulator = self.simulator
                    self.simulator = replacement
                    self.world = replacement_world
                    old_simulator.close()
                    aliases = {name: dict(values) for name, values in self.workspaces.items()}
                    revisions = dict(self.workspace_revisions)
                    self.entities.clear()
                    self.robots.clear()
                    self.states.clear()
                    self.objects.clear()
                    self.object_ids_by_handle.clear()
                    self.workspaces.clear()
                    self.workspace_revisions.clear()
                    snapshot = self.simulator.scene_snapshot(self.world)
                    self.scene_generation = snapshot.scene_generation
                    self.scene_revision = snapshot.revision
                    self._register_existing_objects()
                    self.workspaces = {
                        name: {alias: object_id for alias, object_id in values.items()
                               if object_id in self.objects}
                        for name, values in aliases.items()
                    }
                    self.workspace_revisions = revisions
            else:
                raise AgentRpcError(
                    "unsupported_operation", "embedded service owner cannot apply backends")
            self.physics = physics
            self.collision = collision
            self._scene_changed()
            return {"physics": self.physics, "collision": self.collision, "changed": True,
                    "scene_generation": self.scene_generation,
                    "objects": [self._object_ref(record) for record in self.objects.values()]}
        if method == "robot.execute_joint_segment":
            robot = self._robot_record(params.get("robot_object"))
            steps = params.get("steps")
            start = params.get("start", [])
            target = params.get("target", [])
            if (isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100
                    or len(start) != robot.joint_count or len(target) != robot.joint_count
                    or not all(isinstance(v, (int, float)) and math.isfinite(v)
                               for v in [*start, *target])):
                raise AgentRpcError("invalid_argument", "invalid joint segment or step count (1..100)")
            tuning = {key: params[key] for key in ("gain", "velocity_limit", "effort_limits")
                      if params.get(key) is not None}
            paced = bool(params.get("paced", True))
            for tick in range(1, steps + 1):
                robot.command_joint_positions(
                    [a + (b - a) * tick / steps for a, b in zip(start, target)], **tuning)
                if paced:
                    self.simulator.step(self.world)
                else:
                    self.simulator.step_unpaced(self.world)
            self._scene_changed()
            return {"time": self.simulator.time(self.world)}
        if method == "robot.command_joint_positions":
            robot = (self._robot_record(params.get("robot_object"))
                     if "robot_object" in params else self._robot(params))
            # gain/velocity_limit are optional controller overrides; omitting both keeps the
            # platform defaults, so a capped servo can be requested over Live too.
            tuning: dict[str, Any] = {}
            for name in ("gain", "velocity_limit"):
                if params.get(name) is not None:
                    tuning[name] = float(params[name])
            if params.get("effort_limits") is not None:
                tuning["effort_limits"] = params["effort_limits"]
            robot.command_joint_positions(params.get("targets", []), **tuning)
            self._scene_changed()
            return {"robot": robot.handle, **tuning}
        if method == "robot.set_joint_positions":
            robot = self._robot_record(params.get("robot_object"))
            # Poses the joints directly, bypassing the controller. Iterative solvers overshoot
            # on intermediate steps, so "clamp" is the sane default here.
            robot.set_joint_positions(params.get("positions", []),
                                      limits=str(params.get("limits", "clamp")))
            self._scene_changed()
            return {"robot": robot.handle}
        if method == "robot.list_links":
            record = self._resolve_object(params.get("robot"), kind="robot")
            return {"links": [{"name": name, "object": self._object_ref(self.objects[object_id])}
                              for name, object_id in sorted(record.links.items())
                              if object_id in self.objects]}
        if method == "robot.command_joint_velocities":
            robot = self._robot_record(params.get("robot_object"))
            robot.command_joint_velocities(params.get("targets", []))
            self._scene_changed()
            return {"robot": robot.handle}
        if method == "robot.set_joint_continuous":
            robot = (self._robot_record(params.get("robot_object"))
                     if "robot_object" in params else self._robot(params))
            robot.set_joint_continuous(str(params.get("joint", "")),
                                       bool(params.get("enabled", True)))
            self._scene_changed()
            return {"robot": robot.handle, "joint": str(params.get("joint", "")),
                    "continuous": bool(params.get("enabled", True))}
        if method == "robot.command_joint_positions_batch":
            values = params.get("items")
            if not isinstance(values, list):
                raise AgentRpcError("invalid_argument", "items must be an array")
            robots = [self._robot_record(value.get("robot")) for value in values
                      if isinstance(value, dict)]
            targets = [value.get("targets", []) for value in values if isinstance(value, dict)]
            if len(robots) != len(values):
                raise AgentRpcError("invalid_argument", "batch item must be an object")
            self.simulator.command_joint_positions_batch(self.world, robots, targets)
            self._scene_changed()
            return {"count": len(robots)}
        if method == "robot.configuration_collides":
            robot = self._robot_record(params.get("robot_object"))
            return {"colliding": robot.configuration_collides(params.get("positions", []))}
        if method == "robot.configurations_collide":
            robot = self._robot_record(params.get("robot_object"))
            return {"colliding": robot.configurations_collide(
                params.get("positions", [])).astype(bool).tolist()}
        if method == "robot.kinematics":
            robot = self._robot_record(params.get("robot_object"))
            links = params.get("links")
            if not isinstance(links, list) or not links:
                raise AgentRpcError("invalid_argument", "links must be a non-empty array")
            jacobian_link = params.get("jacobian_link")
            batch = robot.kinematics(
                params.get("positions", []), [str(link) for link in links],
                jacobian_link=None if jacobian_link is None else str(jacobian_link),
                limits=str(params.get("limits", "ignore")))
            return {
                "transforms": [[{"translation": list(pose.translation),
                                 "rotation_wxyz": list(pose.rotation_wxyz),
                                 "scale": list(pose.scale)} for pose in configuration]
                               for configuration in batch.transforms],
                "jacobians": (None if batch.jacobians is None
                              else batch.jacobians.tolist()),
            }
        if method == "robot.configuration_contacts":
            robot = self._robot_record(params.get("robot_object"))
            pairs = robot.configuration_contacts(
                params.get("positions", []),
                distance_threshold=float(params.get("distance_threshold", 0.0)),
                limits=str(params.get("limits", "ignore")))
            return {"contacts": [{"robot_collider": pair.robot_collider, "other": pair.other,
                                  "distance": pair.distance, "colliding": pair.colliding}
                                 for pair in pairs]}
        if method == "robot.joint_limits":
            robot = self._robot_record(params.get("robot_object"))
            # Continuous joints report -inf/+inf, which is not valid strict JSON; send null and
            # let the client restore an unbounded value.
            return {"names": list(robot.joint_names),
                    "limits": [[None if not math.isfinite(value) else float(value)
                                for value in row] for row in robot.joint_limits]}
        if method == "robot.joint_state":
            robot = (self._robot_record(params.get("robot_object"))
                     if "robot_object" in params else self._robot(params))
            positions, velocities, efforts = robot.joint_state
            return {"names": list(robot.joint_names),
                    "positions": positions.tolist(),
                    "velocities": velocities.tolist(),
                    "efforts": efforts.tolist()}
        if method == "robot.get_link":
            record = self._resolve_object(params.get("robot"), kind="robot")
            name = str(params.get("name", ""))
            object_id = record.links.get(name)
            if object_id is None:
                raise AgentRpcError("object_not_found", f"robot link {name!r} was not found")
            link = self.objects.get(object_id)
            if link is None:
                raise AgentRpcError("stale_object", f"robot link {name!r} was deleted")
            return {"object": self._object_ref(link)}
        if method == "collision.check":
            first = self._resolve_object(params.get("first")).entity
            second = self._resolve_object(params.get("second")).entity
            return {"colliding": self.world.check_collision(first, second)}
        if method == "collision.distance":
            first = self._resolve_object(params.get("first")).entity
            second = self._resolve_object(params.get("second")).entity
            return {"distance": self.world.distance(first, second)}
        if method == "collision.scene":
            values = params.get("objects")
            if not isinstance(values, list):
                raise AgentRpcError("invalid_argument", "objects must be an array")
            entities = [self._resolve_object(value).entity for value in values]
            colliding, elapsed_ms = self.world.scene_collisions(entities)
            return {"colliding": colliding.astype(bool).tolist(), "elapsed_ms": elapsed_ms}
        if method == "camera.configure":
            record = self._resolve_object(params.get("object"), kind="camera")
            Camera(self.world, record.entity.handle).configure(
                frame_id=str(params.get("frame_id", "camera")),
                update_rate_hz=float(params.get("update_rate_hz", 30.0)),
                width=int(params.get("width", 640)), height=int(params.get("height", 480)),
                vertical_fov_radians=float(params.get("vertical_fov_radians", 1.0471975512)),
                near_plane=float(params.get("near_plane", 0.01)),
                far_plane=float(params.get("far_plane", 1000.0)),
                output=str(params.get("output", "rgbd")),
            )
            self._scene_changed()
            return {"object": self._object_ref(record)}
        if method == "camera.describe":
            record = self._resolve_object(params.get("object"), kind="camera")
            camera = Camera(self.world, record.entity.handle)
            return {"dimensions": list(camera.dimensions), "intrinsics": camera.intrinsics.tolist()}
        if method == "camera.frame":
            record = self._resolve_object(params.get("object"), kind="camera")
            camera = Camera(self.world, record.entity.handle)
            mode = str(params.get("mode", "rgb"))
            array = camera.rgb if mode == "rgb" else camera.depth
            return {"dtype": str(array.dtype), "shape": list(array.shape),
                    "data": base64.b64encode(array.tobytes()).decode("ascii")}
        if method == "lidar.points":
            record = self._resolve_object(params.get("object"), kind="lidar")
            points, distances = LiDAR(self.world, record.entity.handle).get_points()
            return {"points": base64.b64encode(points.tobytes()).decode("ascii"),
                    "distances": base64.b64encode(distances.tobytes()).decode("ascii"),
                    "count": int(points.shape[0])}
        if method == "workspace.resolve":
            alias = self._require_alias(params.get("alias"))
            object_id = self.workspaces.setdefault(session.workspace, {}).get(alias)
            if object_id is None:
                raise AgentRpcError("object_not_found", f"alias {alias!r} was not found")
            record = self.objects.get(object_id)
            if record is None:
                raise AgentRpcError("stale_object", f"alias {alias!r} refers to a deleted object")
            expected = params.get("kind")
            if expected is not None and record.kind != str(expected):
                raise AgentRpcError(
                    "object_type_mismatch",
                    f"alias {alias!r} is {record.kind}, expected {expected}",
                )
            return {"object": self._object_ref(record)}
        if method == "workspace.get":
            aliases = self.workspaces.setdefault(session.workspace, {})
            return {"revision": self.workspace_revisions.setdefault(session.workspace, 0),
                    "objects": {alias: self._object_ref(self.objects[object_id])
                                for alias, object_id in aliases.items()
                                if object_id in self.objects}}
        if method == "workspace.bind":
            alias = self._require_alias(params.get("alias"))
            record = self._resolve_object(params.get("object"))
            self._bind_alias(session.workspace, alias, record)
            return {"alias": alias, "object": self._object_ref(record),
                    "revision": self.workspace_revisions[session.workspace]}
        if method == "workspace.unbind":
            alias = self._require_alias(params.get("alias"))
            aliases = self.workspaces.setdefault(session.workspace, {})
            removed = aliases.pop(alias, None) is not None
            if removed:
                self.workspace_revisions[session.workspace] = (
                    self.workspace_revisions.get(session.workspace, 0) + 1
                )
            return {"alias": alias, "removed": removed,
                    "revision": self.workspace_revisions.setdefault(session.workspace, 0)}
        if method == "state.save":
            state = self.simulator.save_state(self.world)
            self.states[state.handle] = state
            return {"state": state.handle}
        if method == "state.restore":
            handle = int(params.get("state", 0))
            if handle not in self.states:
                raise AgentRpcError("invalid_handle", "state handle is invalid")
            self.simulator.restore_state(self.world, self.states[handle])
            self.scene_generation += 1
            self._refresh_registry()
            self._scene_changed()
            return {"state": handle, "time": self.simulator.time(self.world),
                    "scene_generation": self.scene_generation,
                    "objects": [self._object_ref(record) for record in self.objects.values()]}
        if method == "state.save_file":
            path = str(params.get("path", "")).strip()
            if not path:
                raise AgentRpcError("invalid_argument", "path is required")
            self.simulator.save_state_file(self.world, path)
            return {"path": path}
        if method == "state.restore_file":
            path = str(params.get("path", "")).strip()
            if not path:
                raise AgentRpcError("invalid_argument", "path is required")
            # Restoring replaces the world's ECS and rebuilds the backends, so every cached
            # object handle is stale afterwards; bump the generation and re-resolve the registry.
            self.simulator.restore_state_file(self.world, path)
            self.scene_generation += 1
            self._refresh_registry()
            self._scene_changed()
            return {"path": path, "time": self.simulator.time(self.world),
                    "scene_generation": self.scene_generation,
                    "objects": [self._object_ref(record) for record in self.objects.values()]}
        if method == "capabilities.query":
            return {"protocol_version": PROTOCOL_VERSION,
                    "instance_id": self.instance_id,
                    "shared_memory": {"available": True,
                    "transport": "named_shared_memory"}, "methods": [
                "scene.query", "scene.snapshot", "scene.transforms", "scene.create_entity", "scene.create_primitive",
                "scene.load_asset", "scene.load_robot", "scene.ensure_robot",
                "scene.set_transform", "scene.set_transforms", "scene.get_transform",
                "scene.update_transform", "scene.set_rigid_body", "scene.set_name",
                "scene.set_visual", "scene.get_ifps_sphere_count",
                "scene.set_ifps_sphere_count", "scene.apply_wrench", "scene.set_parent",
                "scene.destroy_object", "scene.find_object",
                "scene.create_camera", "scene.create_lidar", "camera.configure",
                "camera.describe", "camera.frame", "lidar.points", "collision.check",
                "collision.distance", "collision.scene", "simulation.step",
                "simulation.step_profiled", "simulation.physics_contacts", "simulation.poll_event", "simulation.start",
                "simulation.pause", "simulation.status", "simulation.reset", "robot.get_link",
                "backend.status", "backend.apply",
                "robot.joint_state", "robot.command_joint_positions", "robot.execute_joint_segment",
                "robot.command_joint_velocities", "robot.set_joint_continuous",
                "robot.configuration_collides",
                "robot.configurations_collide", "robot.command_joint_positions_batch",
                "robot.kinematics", "robot.configuration_contacts", "robot.joint_limits",
                "robot.set_joint_positions", "robot.list_links",
                "simulation.sync_physics", "simulation.sync_collision",
                "workspace.get", "workspace.resolve",
                "workspace.bind", "workspace.unbind", "state.save",
                "state.restore", "state.save_file", "state.restore_file",
                "shared_memory.allocate", "shared_memory.describe",
                "shared_memory.release", "session.hello", "session.ping",
                "session.shutdown", "capabilities.query"]}
        if method == "shared_memory.allocate":
            size = int(params.get("size", 0))
            if size <= 0 or size > 1 << 30:
                raise AgentRpcError("invalid_argument", "shared memory size must be between 1 and 1 GiB")
            buffer = shared_memory.SharedMemory(create=True, size=size)
            self.shared_buffers[buffer.name] = buffer
            return {"name": buffer.name, "size": size, "offset": 0}
        if method == "shared_memory.describe":
            name = str(params.get("name", ""))
            if name not in self.shared_buffers:
                raise AgentRpcError("invalid_handle", "shared memory buffer is invalid")
            return {"name": name, "size": self.shared_buffers[name].size, "offset": 0}
        if method == "shared_memory.release":
            name = str(params.get("name", ""))
            buffer = self.shared_buffers.pop(name, None)
            if buffer is None:
                raise AgentRpcError("invalid_handle", "shared memory buffer is invalid")
            buffer.close()
            buffer.unlink()
            return {"name": name, "released": True}
        raise AgentRpcError("method_not_found", f"unknown method: {method}")

    def _run_simulation(self) -> None:
        while not self._runner_stop.is_set():
            stepped = False
            try:
                with self.lock:
                    if self.simulator.status(self.world) == "running":
                        self.simulator.step(self.world)
                        stepped = True
            except Exception:
                self._runner_stop.set()
                return
            self._runner_stop.wait(self._runner_interval if stepped else 0.01)

    def _scene_changed(self) -> None:
        # A mutation notification must not serialize the complete scene.  During Agent
        # execution this is called after every batched physics segment; taking a full
        # snapshot here duplicated the timeline capture RPC and dominated wall time.
        generation, revision = self.simulator.scene_metadata(self.world)
        self.scene_generation = generation
        self.scene_revision = max(self.scene_revision + 1, revision)
        if self._on_scene_changed is not None:
            self._on_scene_changed()

    def _register_existing_objects(self) -> None:
        items = list(self.world.editor_entities())
        for item in items:
            entity = self.world.entity(item.entity)
            if "robot" in item.flags:
                robot = Robot(self.world, item.entity)
                self.robots[robot.handle] = robot
                self._register_object(robot, "robot", item.name)
            else:
                kind = ("link" if "joint" in item.flags else "camera"
                        if "camera" in item.flags else "lidar"
                        if "sensor" in item.flags else "entity")
                self._register_object(entity, kind, item.name)
        for robot in self.robots.values():
            root = self.objects[self.object_ids_by_handle[robot.handle]]
            for name, entity in robot.links:
                link = self._register_object(entity, "link", name)
                root.links.setdefault(name, link.object_id)

    def _refresh_registry(self) -> None:
        aliases = {name: dict(values) for name, values in self.workspaces.items()}
        revisions = dict(self.workspace_revisions)
        self.entities.clear()
        self.robots.clear()
        self.objects.clear()
        self.object_ids_by_handle.clear()
        self._register_existing_objects()
        self.workspaces = {
            name: {alias: object_id for alias, object_id in values.items()
                   if object_id in self.objects}
            for name, values in aliases.items()
        }
        self.workspace_revisions = revisions

    def _purge_destroyed_objects(self) -> None:
        alive = {item.entity for item in self.world.editor_entities()}
        removed_ids = {object_id for object_id, record in self.objects.items()
                       if record.entity.handle not in alive}
        for object_id in removed_ids:
            record = self.objects.pop(object_id)
            self.object_ids_by_handle.pop(record.entity.handle, None)
            self.entities.pop(record.entity.handle, None)
            self.robots.pop(record.entity.handle, None)
        for aliases in self.workspaces.values():
            for alias, object_id in list(aliases.items()):
                if object_id in removed_ids:
                    del aliases[alias]

    def _register_object(self, entity: Entity, kind: str, name: str,
                         asset_path: str | None = None) -> _ObjectRecord:
        existing_id = self.object_ids_by_handle.get(entity.handle)
        if existing_id is not None:
            return self.objects[existing_id]
        record = _ObjectRecord(entity.object_id, entity, kind, name, asset_path)
        self.objects[record.object_id] = record
        self.object_ids_by_handle[entity.handle] = record.object_id
        self.entities[entity.handle] = entity
        return record

    def _object_ref(self, record: _ObjectRecord) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "world_id": self.world.handle,
            "object_id": record.object_id,
            "runtime_handle": record.entity.handle,
            "kind": record.kind,
            "scene_generation": self.scene_generation,
        }

    def _resolve_object(self, value: Any, *, kind: str | None = None) -> _ObjectRecord:
        if not isinstance(value, dict):
            raise AgentRpcError("invalid_argument", "object reference must be an object")
        if str(value.get("instance_id", "")) != self.instance_id:
            raise AgentRpcError("instance_mismatch", "object belongs to another instance")
        if int(value.get("world_id", 0)) != self.world.handle:
            raise AgentRpcError("instance_mismatch", "object belongs to another world")
        if int(value.get("scene_generation", 0)) != self.scene_generation:
            raise AgentRpcError("stale_object", "object reference belongs to an older scene generation")
        object_id = str(value.get("object_id", ""))
        record = self.objects.get(object_id)
        if record is None:
            raise AgentRpcError("stale_object", "object no longer exists")
        if record.entity.handle != int(value.get("runtime_handle", 0)):
            raise AgentRpcError("stale_object", "object runtime handle has changed")
        if kind is not None and record.kind != kind:
            raise AgentRpcError("object_type_mismatch",
                                f"object is {record.kind}, expected {kind}")
        return record

    def _robot_record(self, value: Any) -> Robot:
        record = self._resolve_object(value, kind="robot")
        robot = self.robots.get(record.entity.handle)
        if robot is None:
            raise AgentRpcError("stale_object", "robot no longer exists")
        return robot

    def _load_robot(self, path_value: Any) -> _ObjectRecord:
        path = self._normalized_path(path_value)
        robot = self.world.load_robot(path)
        self.robots[robot.handle] = robot
        root = self._register_object(robot, "robot", Path(path).stem, path)
        for name, entity in robot.links:
            record = self._register_object(entity, "link", name)
            if name in root.links:
                raise AgentRpcError("ambiguous_link",
                                    f"robot contains duplicate link name {name!r}")
            root.links[name] = record.object_id
        return root

    @staticmethod
    def _normalized_path(value: Any) -> str:
        if value is None or not str(value).strip():
            raise AgentRpcError("invalid_argument", "asset path is required")
        return str(Path(str(value)).expanduser().resolve())

    @staticmethod
    def _vector(value: Any, count: int, name: str) -> tuple[float, ...]:
        try:
            packed = tuple(float(item) for item in value)
        except (TypeError, ValueError) as error:
            raise AgentRpcError("invalid_argument", f"{name} must contain numbers") from error
        if len(packed) != count:
            raise AgentRpcError("invalid_argument", f"{name} must contain {count} values")
        return packed

    @staticmethod
    def _require_alias(value: Any) -> str:
        alias = str(value or "").strip()
        if not alias or len(alias) > 128:
            raise AgentRpcError("invalid_argument", "alias must contain 1 to 128 characters")
        if any(character.isspace() for character in alias):
            raise AgentRpcError("invalid_argument", "alias cannot contain whitespace")
        return alias

    def _bind_optional_alias(self, workspace: str, value: Any,
                             record: _ObjectRecord) -> None:
        if value is not None:
            self._bind_alias(workspace, self._require_alias(value), record)

    def _bind_alias(self, workspace: str, alias: str, record: _ObjectRecord) -> None:
        aliases = self.workspaces.setdefault(workspace, {})
        existing = aliases.get(alias)
        if existing is not None and existing != record.object_id:
            raise AgentRpcError("alias_conflict", f"alias {alias!r} is already bound")
        if existing is None:
            aliases[alias] = record.object_id
            self.workspace_revisions[workspace] = self.workspace_revisions.get(workspace, 0) + 1

    def _entity(self, params: dict[str, Any]) -> Entity:
        handle = int(params.get("entity", 0))
        if handle not in self.entities:
            raise AgentRpcError("invalid_handle", "entity handle is invalid")
        return self.entities[handle]

    def _robot(self, params: dict[str, Any]) -> Robot:
        handle = int(params.get("robot", 0))
        if handle not in self.robots:
            raise AgentRpcError("invalid_handle", "robot handle is invalid")
        return self.robots[handle]

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        service: HeadlessService = self.server.service  # type: ignore[attr-defined]
        session = _SessionContext()
        for raw_line in self.rfile:
            try:
                if len(raw_line) > 4 * 1024 * 1024:
                    raise ValueError("request exceeds the 4 MiB message limit")
                request = json.loads(raw_line.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                response = service.dispatch(request, session)
            except Exception as error:
                response = {"id": None, "ok": False,
                            "error": {"code": "invalid_json", "message": str(error)}}
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class _LiveTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _LiveTCPServerIPv6(_LiveTCPServer):
    address_family = socket.AF_INET6


def _tcp_uri(host: str, port: int) -> str:
    return f"tcp://[{host}]:{port}" if ":" in host else f"tcp://{host}:{port}"


def _published_endpoints(host: str, port: int, advertise: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    if host in {"0.0.0.0", ""}:
        result.append(_tcp_uri("127.0.0.1", port))
    elif host == "::":
        result.append(_tcp_uri("::1", port))
    else:
        result.append(_tcp_uri(host, port))
    for value in advertise:
        endpoint = parse_endpoint(value)
        if endpoint.scheme != "tcp":
            raise ValueError("only tcp advertise endpoints are supported")
        advertised_port = port if endpoint.port == 0 else int(endpoint.port)
        result.append(_tcp_uri(str(endpoint.host), advertised_port))
    return tuple(dict.fromkeys(result))


def _write_instance_descriptor(service: HeadlessService, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{os.getpid()}.json"
    temporary = directory / f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    value = {
        "protocol_version": PROTOCOL_VERSION,
        "pid": os.getpid(),
        "process_start_time": service.process_start_time,
        "instance_id": service.instance_id,
        "instance_name": service.instance_name,
        "endpoints": list(service.endpoints),
    }
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    return target


class LiveServer:
    """A lifecycle wrapper for embedding the public Live protocol in another service."""

    def __init__(self, service: HeadlessService, *, host: str = "127.0.0.1",
                 port: int = 0, advertise: list[str] | None = None):
        server_type = _LiveTCPServerIPv6 if ":" in host else _LiveTCPServer
        self.service = service
        self._server = server_type((host, port), _Handler)
        self._server.service = service  # type: ignore[attr-defined]
        actual_port = int(self._server.server_address[1])
        service.endpoints = _published_endpoints(host, actual_port, advertise or [])
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        return self.service.endpoints[0]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="live-control-server", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless robot simulation Agent RPC server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--physics", choices=["none", "mujoco", "bullet3"], default="none")
    parser.add_argument("--collision",
                        choices=["fcl", "bullet3", "coal", "ifps-cpu", "ifps-gpu"],
                        default="fcl")
    parser.add_argument("--cache-directory")
    parser.add_argument("--instance-name")
    parser.add_argument("--registry-dir")
    parser.add_argument("--advertise", action="append", default=[])
    args = parser.parse_args()
    from ._logging import install_standard_logging
    install_standard_logging("sim_platform_server")
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    service = HeadlessService(
        physics=args.physics,
        collision=args.collision,
        cache_directory=args.cache_directory,
        instance_name=args.instance_name,
    )
    descriptor_path: Path | None = None
    server = LiveServer(service, host=args.host, port=args.port, advertise=args.advertise)
    try:
        service.shutdown_callback = server.close
        registry = (Path(args.registry_dir).expanduser().resolve()
                    if args.registry_dir else default_registry_directory())
        descriptor_path = _write_instance_descriptor(service, registry)
        _log_info("AgentRPC", f"server listening endpoints={', '.join(service.endpoints)}")
        server.start()
        assert server._thread is not None
        server._thread.join()
    finally:
        if descriptor_path is not None:
            try:
                current = json.loads(descriptor_path.read_text(encoding="utf-8"))
                if current.get("instance_id") == service.instance_id:
                    descriptor_path.unlink()
            except (OSError, ValueError):
                pass
        server.close()
        service.close()


if __name__ == "__main__":
    main()
