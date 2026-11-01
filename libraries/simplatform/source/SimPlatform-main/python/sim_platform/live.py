from __future__ import annotations

import ctypes
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping
from urllib.parse import urlsplit

import numpy as np

from ._control import effort_limit_values

if TYPE_CHECKING:
    from .sdk import SceneSnapshot


PROTOCOL_VERSION = 1
LAUNCHED_SESSION_TIMEOUT = 60.0
_LAUNCHED_PROCESSES: dict[int, subprocess.Popen[Any]] = {}


class LiveControlError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LiveKinematicsBatch:
    """Poses indexed [configuration][link]; `jacobians` is (count, 6, joint_count) or None."""

    transforms: tuple[tuple[dict[str, tuple[float, ...]], ...], ...]
    jacobians: np.ndarray | None = None


@dataclass(frozen=True)
class LiveContactPair:
    robot_collider: int
    other: int
    distance: float
    colliding: bool


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str | None = None
    port: int | None = None
    pid: int | None = None

    def __str__(self) -> str:
        if self.scheme == "pid":
            return f"pid:{self.pid}"
        host = self.host or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}"


@dataclass(frozen=True)
class ObjectRef:
    instance_id: str
    world_id: int
    object_id: str
    runtime_handle: int
    kind: str
    scene_generation: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ObjectRef":
        return cls(
            instance_id=str(value["instance_id"]),
            world_id=int(value["world_id"]),
            object_id=str(value["object_id"]),
            runtime_handle=int(value["runtime_handle"]),
            kind=str(value["kind"]),
            scene_generation=int(value["scene_generation"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "world_id": self.world_id,
            "object_id": self.object_id,
            "runtime_handle": self.runtime_handle,
            "kind": self.kind,
            "scene_generation": self.scene_generation,
        }


@dataclass(frozen=True)
class LaunchSpec:
    kind: str = "headless"
    instance_name: str | None = None
    listen: tuple[str, ...] = ()
    advertise: tuple[str, ...] = ()
    detached: bool = True
    executable: str | os.PathLike[str] | None = None
    physics: str = "none"
    collision: str = "fcl"
    cache_directory: str | os.PathLike[str] | None = None
    registry_directory: str | os.PathLike[str] | None = None
    startup_timeout: float = 15.0
    environment: Mapping[str, str] = field(default_factory=dict)


def parse_endpoint(value: str | int | Endpoint) -> Endpoint:
    if isinstance(value, Endpoint):
        return value
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("PID must be positive")
        return Endpoint("pid", pid=value)
    text = value.strip()
    if text.startswith("pid:"):
        try:
            pid = int(text[4:])
        except ValueError as error:
            raise ValueError(f"invalid PID endpoint: {value}") from error
        if pid <= 0:
            raise ValueError("PID must be positive")
        return Endpoint("pid", pid=pid)
    if "://" not in text:
        text = f"tcp://{text}"
    parsed = urlsplit(text)
    if parsed.scheme not in {"tcp", "tls"}:
        raise ValueError(f"unsupported endpoint scheme: {parsed.scheme}")
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"endpoint must include host and port: {value}")
    if not 0 <= parsed.port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return Endpoint(parsed.scheme, parsed.hostname, parsed.port)


def default_registry_directory() -> Path:
    configured = os.environ.get("SIM_CONTROL_REGISTRY_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RobotSimPlatform" / "instances"
    if sys.platform == "darwin":
        base = Path(os.environ.get("TMPDIR", "/tmp"))
        return base / f"robot-sim-platform-{os.getuid()}" / "instances"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "robot-sim-platform" / "instances"
    return Path(os.environ.get("TMPDIR", "/tmp")) / f"robot-sim-platform-{os.getuid()}" / "instances"


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information, False, pid
        )
        if not handle:
            return ctypes.windll.kernel32.GetLastError() == 5  # type: ignore[attr-defined]
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_descriptor(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        pid = int(value["pid"])
        uuid.UUID(str(value["instance_id"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not _pid_exists(pid):
        return None
    return value


def list_instances(registry_directory: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any], ...]:
    directory = (Path(registry_directory).expanduser().resolve()
                 if registry_directory is not None else default_registry_directory())
    if not directory.is_dir():
        return ()
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        value = _read_descriptor(path)
        if value is not None:
            result.append(value)
    return tuple(result)


def _descriptor_for_pid(pid: int, registry_directory: str | os.PathLike[str] | None) -> dict[str, Any]:
    for descriptor in list_instances(registry_directory):
        if int(descriptor["pid"]) == pid:
            return descriptor
    raise LiveControlError("instance_not_found", f"no live instance was found for PID {pid}")


def _descriptor_for_name(name: str, registry_directory: str | os.PathLike[str] | None) -> dict[str, Any]:
    matches = [item for item in list_instances(registry_directory)
               if item.get("instance_name") == name]
    if not matches:
        raise LiveControlError("instance_not_found", f"no live instance named {name!r} was found")
    if len(matches) > 1:
        raise LiveControlError("ambiguous_instance", f"multiple live instances are named {name!r}")
    return matches[0]


def _tcp_endpoints(descriptor: Mapping[str, Any]) -> tuple[Endpoint, ...]:
    result: list[Endpoint] = []
    for raw in descriptor.get("endpoints", []):
        try:
            endpoint = parse_endpoint(str(raw))
        except ValueError:
            continue
        if endpoint.scheme == "tcp":
            result.append(endpoint)
    return tuple(result)


class LiveSession:
    def __init__(self, connection: socket.socket, endpoint: Endpoint, *, workspace: str = "default"):
        self._connection = connection
        self._stream = connection.makefile("rwb")
        self._request_id = 1
        self._request_lock = threading.Lock()
        self._workspace = workspace
        self.endpoint = str(endpoint)
        self.endpoints: tuple[str, ...] = (self.endpoint,)
        self.instance_id = ""
        self.pid = 0
        self.scene_revision = 0
        self.scene_generation = 0
        self.capabilities: frozenset[str] = frozenset()
        self._proxies: weakref.WeakSet[EntityProxy] = weakref.WeakSet()
        hello = self._request("session.hello", {
            "protocol_version": PROTOCOL_VERSION,
            "client": "sim-platform-python/0.1.0",
            "workspace": workspace,
        })
        self.instance_id = str(hello["instance_id"])
        self.pid = int(hello.get("pid", 0))
        self.scene_revision = int(hello.get("scene_revision", 0))
        self.scene_generation = int(hello.get("scene_generation", 1))
        self.capabilities = frozenset(str(item) for item in hello.get("capabilities", []))
        self.endpoints = tuple(str(item) for item in hello.get("endpoints", [self.endpoint]))
        self.objects = ObjectRegistry(self)

    def _request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._request_lock:
            request = {"id": self._request_id, "method": method, "params": dict(params or {})}
            self._request_id += 1
            try:
                payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._stream.write(payload + b"\n")
                self._stream.flush()
                line = self._stream.readline()
            except (OSError, ValueError) as error:
                raise LiveControlError("connection_lost", str(error)) from error
        if not line:
            raise LiveControlError("connection_lost", "live control connection was closed")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise LiveControlError("invalid_response", "server returned invalid JSON") from error
        if not response.get("ok"):
            detail = response.get("error", {})
            raise LiveControlError(str(detail.get("code", "simulation_error")),
                                   str(detail.get("message", "live control request failed")))
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise LiveControlError("invalid_response", "server result must be an object")
        return result

    def _set_request_timeout(self, timeout: float) -> None:
        self._connection.settimeout(timeout)

    def _refresh_proxies(self, result: Mapping[str, Any]) -> None:
        self.scene_generation = int(result.get("scene_generation", self.scene_generation))
        references = {
            str(value.get("object_id")): ObjectRef.from_dict(value)
            for value in result.get("objects", [])
            if isinstance(value, dict) and value.get("object_id")
        }
        for proxy in tuple(self._proxies):
            reference = references.get(proxy.object_id)
            if reference is not None:
                proxy.reference = reference

    def _proxy(self, value: Mapping[str, Any]) -> "EntityProxy":
        reference = ObjectRef.from_dict(value)
        types = {"robot": RobotProxy, "link": LinkProxy,
                 "camera": CameraProxy, "lidar": LidarProxy}
        proxy = types.get(reference.kind, EntityProxy)(self, reference)
        self._proxies.add(proxy)
        return proxy

    def load_robot(self, path: str | os.PathLike[str], *, alias: str | None = None) -> "RobotProxy":
        params: dict[str, Any] = {"path": os.fspath(path)}
        if alias is not None:
            params["alias"] = alias
        result = self._request("scene.load_robot", params)
        proxy = self._proxy(result["object"])
        if not isinstance(proxy, RobotProxy):
            raise LiveControlError("invalid_response", "loaded object is not a robot")
        return proxy

    def load_asset(
        self, path: str | os.PathLike[str], *, alias: str | None = None
    ) -> "EntityProxy":
        params: dict[str, Any] = {"path": os.fspath(path)}
        if alias is not None:
            params["alias"] = alias
        return self._proxy(self._request("scene.load_asset", params)["object"])

    def create_entity(
        self, name: str, parent: "EntityProxy | None" = None, *, alias: str | None = None
    ) -> "EntityProxy":
        params: dict[str, Any] = {
            "name": name, "parent": None if parent is None else parent.reference.as_dict(),
        }
        if alias is not None:
            params["alias"] = alias
        return self._proxy(self._request("scene.create_entity", params)["object"])

    def create_primitive(
        self, kind: str, *, name: str | None = None,
        size: Iterable[float] | None = None, ifps_sphere_count: int = 0,
        alias: str | None = None,
    ) -> "EntityProxy":
        normalized = kind.strip().lower()
        valid = {"box", "cube", "sphere", "plane", "capsule", "cylinder"}
        if normalized not in valid:
            raise ValueError(f"primitive kind must be one of {sorted(valid)}")
        params: dict[str, Any] = {"kind": normalized, "ifps_sphere_count": ifps_sphere_count}
        if name is not None:
            params["name"] = name
        if size is not None:
            params["size"] = [float(value) for value in size]
        if alias is not None:
            params["alias"] = alias
        result = self._request("scene.create_primitive", params)
        return self._proxy(result["object"])

    def create_camera(
        self, parent: "EntityProxy | None" = None, *, frame_id: str = "camera",
        update_rate_hz: float = 30.0, width: int = 640, height: int = 480,
        vertical_fov_radians: float = np.pi / 3.0, near_plane: float = 0.01,
        far_plane: float = 1000.0,
    ) -> "CameraProxy":
        proxy = self._proxy(self._request("scene.create_camera", {
            "parent": None if parent is None else parent.reference.as_dict(),
            "frame_id": frame_id, "update_rate_hz": update_rate_hz,
            "width": width, "height": height,
            "vertical_fov_radians": vertical_fov_radians,
            "near_plane": near_plane, "far_plane": far_plane,
        })["object"])
        if not isinstance(proxy, CameraProxy):
            raise LiveControlError("invalid_response", "server did not return a camera")
        return proxy

    def create_light(self, type: str = "point", *, name: str | None = None,
                     color: Iterable[float] = (1.0, 1.0, 1.0), intensity: float = 1.0,
                     range: float = 10.0, decay: float = 2.0,
                     inner_cone_radians: float = 0.35, outer_cone_radians: float = 0.7,
                     angular_diameter_radians: float = 0.0, enabled: bool = True) -> "EntityProxy":
        params: dict[str, Any] = {"type": type, "color": list(color), "intensity": intensity,
                                  "range": range, "decay": decay,
                                  "innerConeRadians": inner_cone_radians,
                                  "outerConeRadians": outer_cone_radians,
                                  "angularDiameterRadians": angular_diameter_radians,
                                  "enabled": enabled}
        if name is not None: params["name"] = name
        return self._proxy(self._request("scene.create_light", params)["object"])

    def create_lidar(
        self, parent: "EntityProxy", *, frame_id: str = "lidar",
        update_rate_hz: float = 10.0, horizontal_samples: int = 1024,
        vertical_channels: int = 16, min_distance: float = 0.05,
        max_distance: float = 100.0,
    ) -> "LidarProxy":
        proxy = self._proxy(self._request("scene.create_lidar", {
            "parent": parent.reference.as_dict(), "frame_id": frame_id,
            "update_rate_hz": update_rate_hz, "horizontal_samples": horizontal_samples,
            "vertical_channels": vertical_channels, "min_distance": min_distance,
            "max_distance": max_distance,
        })["object"])
        if not isinstance(proxy, LidarProxy):
            raise LiveControlError("invalid_response", "server did not return a lidar")
        return proxy

    def ensure_robot(self, path: str | os.PathLike[str], *, alias: str) -> "RobotProxy":
        result = self._request("scene.ensure_robot", {"path": os.fspath(path), "alias": alias})
        proxy = self._proxy(result["object"])
        if not isinstance(proxy, RobotProxy):
            raise LiveControlError("invalid_response", "ensured object is not a robot")
        return proxy

    def require_robot(self, alias: str) -> "RobotProxy":
        proxy = self.objects.require(alias, kind="robot")
        if not isinstance(proxy, RobotProxy):
            raise LiveControlError("invalid_response", f"alias {alias!r} is not a robot")
        return proxy

    def find_entity(self, object_id: str) -> "EntityProxy":
        return self._proxy(self._request("scene.find_object", {
            "object_id": object_id,
        })["object"])

    def command_joint_positions_batch(
        self, values: Iterable[tuple["RobotProxy", Iterable[float]]]
    ) -> None:
        self._request("robot.command_joint_positions_batch", {"items": [
            {"robot": robot.reference.as_dict(),
             "targets": [float(value) for value in targets]}
            for robot, targets in values
        ]})

    def start(self) -> None:
        self._request("simulation.start")

    def pause(self) -> None:
        self._request("simulation.pause")

    def step(self, count: int = 1) -> float:
        return float(self._request("simulation.step", {"count": count})["time"])

    def step_unpaced(self, count: int = 1) -> float:
        return float(self._request("simulation.step", {"count": count, "paced": False})["time"])

    def physics_contacts(self) -> tuple[dict[str, Any], ...]:
        """Read actual physics contact constraints without advancing simulation."""
        return tuple(self._request("simulation.physics_contacts")["contacts"])

    def step_profiled(self, count: int = 1) -> tuple[float, float]:
        result = self._request("simulation.step_profiled", {"count": count})
        return float(result["time"]), float(result["elapsed_ms"])

    def poll_event(self) -> dict[str, Any] | None:
        value = self._request("simulation.poll_event").get("event")
        return dict(value) if isinstance(value, dict) else None

    def reset(self) -> float:
        result = self._request("simulation.reset")
        self._refresh_proxies(result)
        return float(result["time"])

    def sync_physics(self) -> None:
        """Push direct joint writes into the physics backend before querying it again."""
        self._request("simulation.sync_physics")

    def sync_collision(self) -> None:
        """Push direct joint writes into the collision backend before querying it again."""
        self._request("simulation.sync_collision")

    def save_state_file(self, path: str) -> str:
        return str(self._request("state.save_file", {"path": str(path)})["path"])

    def restore_state_file(self, path: str) -> float:
        """Replace the world from a `.simstate` file. Every cached proxy is re-resolved."""
        result = self._request("state.restore_file", {"path": str(path)})
        self._refresh_proxies(result)
        return float(result["time"])

    def save_state(self) -> int:
        return int(self._request("state.save")["state"])

    def restore_state(self, state: int) -> float:
        result = self._request("state.restore", {"state": int(state)})
        self._refresh_proxies(result)
        return float(result["time"])

    def status(self) -> tuple[str, float]:
        result = self._request("simulation.status")
        return str(result["status"]), float(result["time"])

    def backend_status(self) -> dict[str, str]:
        result = self._request("backend.status")
        return {"physics": str(result["physics"]), "collision": str(result["collision"])}

    def apply_backends(
        self, *, physics: str | None = None, collision: str | None = None
    ) -> dict[str, str | bool]:
        params: dict[str, str] = {}
        if physics is not None:
            params["physics"] = physics
        if collision is not None:
            params["collision"] = collision
        result = self._request("backend.apply", params)
        self._refresh_proxies(result)
        return {
            "physics": str(result["physics"]),
            "collision": str(result["collision"]),
            "changed": bool(result.get("changed", False)),
        }

    def scene_snapshot(self) -> SceneSnapshot:
        from .sdk import SceneSnapshot

        result = self._request("scene.snapshot")
        snapshot = result.get("snapshot")
        if not isinstance(snapshot, dict):
            raise LiveControlError("invalid_response", "scene snapshot is missing")
        value = SceneSnapshot.from_dict(snapshot)
        self.scene_revision = value.revision
        self.scene_generation = value.scene_generation
        return value

    def scene_transforms(self) -> dict[str, Any]:
        """Read only dynamic transforms and timing metadata, omitting static assets."""
        result = self._request("scene.transforms")
        if not isinstance(result.get("entities"), list):
            raise LiveControlError("invalid_response", "scene transforms are missing")
        self.scene_revision = int(result.get("revision", self.scene_revision))
        self.scene_generation = int(result.get("sceneGeneration", self.scene_generation))
        return dict(result)

    def set_transforms(self, values: Iterable[tuple["EntityProxy", Mapping[str, Iterable[float]]]]) -> None:
        items = []
        for proxy, transform in values:
            item: dict[str, Any] = {"object": proxy.reference.as_dict()}
            for name, count in (("translation", 3), ("rotation_wxyz", 4), ("scale", 3)):
                if name in transform:
                    packed = [float(value) for value in transform[name]]
                    if len(packed) != count:
                        raise ValueError(f"{name} must contain {count} values")
                    item[name] = packed
            items.append(item)
        self._request("scene.set_transforms", {"items": items})

    def check_collision(self, first: "EntityProxy", second: "EntityProxy") -> bool:
        return bool(self._request("collision.check", {
            "first": first.reference.as_dict(), "second": second.reference.as_dict(),
        })["colliding"])

    def collision_distance(self, first: "EntityProxy", second: "EntityProxy") -> float:
        return float(self._request("collision.distance", {
            "first": first.reference.as_dict(), "second": second.reference.as_dict(),
        })["distance"])

    def scene_collisions(self, objects: Iterable["EntityProxy"]) -> tuple[np.ndarray, float]:
        result = self._request("collision.scene", {
            "objects": [value.reference.as_dict() for value in objects],
        })
        return np.asarray(result["colliding"], dtype=np.bool_), float(result["elapsed_ms"])

    def shutdown(self) -> None:
        self._request("session.shutdown")
        process = _LAUNCHED_PROCESSES.pop(self.pid, None)
        if process is not None:
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5.0)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._connection.close()

    def __enter__(self) -> "LiveSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class EntityProxy:
    def __init__(self, session: LiveSession, reference: ObjectRef):
        self._session = session
        self.reference = reference

    @property
    def object_id(self) -> str:
        return self.reference.object_id

    @property
    def handle(self) -> int:
        return self.reference.runtime_handle

    @property
    def transform(self) -> dict[str, tuple[float, ...]]:
        result = self._session._request("scene.get_transform", {"object": self.reference.as_dict()})
        return {name: tuple(float(item) for item in result[name])
                for name in ("translation", "rotation_wxyz", "scale")}

    def set_position(self, value: Iterable[float]) -> None:
        self._update_transform("translation", value, 3)

    def set_rotation(self, value: Iterable[float]) -> None:
        self._update_transform("rotation_wxyz", value, 4)

    def set_scale(self, value: Iterable[float]) -> None:
        self._update_transform("scale", value, 3)

    def set_name(self, name: str) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("entity name must not be empty")
        self._session._request("scene.set_name", {
            "object": self.reference.as_dict(), "name": normalized,
        })

    def set_visual(self, *, visible: bool = True, min_lod: int = 0, max_lod: int = 7) -> None:
        if not 0 <= min_lod <= max_lod <= 7:
            raise ValueError("LOD range must satisfy 0 <= min_lod <= max_lod <= 7")
        self._session._request("scene.set_visual", {
            "object": self.reference.as_dict(), "visible": bool(visible),
            "min_lod": min_lod, "max_lod": max_lod,
        })

    @property
    def ifps_sphere_count(self) -> int:
        return int(self._session._request("scene.get_ifps_sphere_count", {
            "object": self.reference.as_dict(),
        })["count"])

    @ifps_sphere_count.setter
    def ifps_sphere_count(self, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000:
            raise ValueError("IFPS sphere count must be an integer between 0 and 1,000,000")
        self._session._request("scene.set_ifps_sphere_count", {
            "object": self.reference.as_dict(), "count": value,
        })

    def apply_wrench(
        self, force: Iterable[float], torque: Iterable[float] = (0.0, 0.0, 0.0)
    ) -> None:
        force_values = [float(value) for value in force]
        torque_values = [float(value) for value in torque]
        if len(force_values) != 3 or len(torque_values) != 3:
            raise ValueError("force and torque must each contain three values")
        self._session._request("scene.apply_wrench", {
            "object": self.reference.as_dict(), "force": force_values,
            "torque": torque_values,
        })

    def set_rigid_body(
        self, *, enabled: bool = True, dynamic: bool = False, gravity: bool = True,
        mass: float = 1.0, friction: Iterable[float] = (1.0, 0.005, 0.0001),
        restitution: float = 0.0,
    ) -> str:
        """Configure this entity's rigid body through the Live public RPC."""
        if mass <= 0.0 or not 0.0 <= restitution <= 1.0:
            raise ValueError("mass must be positive and restitution must be between 0 and 1")
        packed = [float(value) for value in friction]
        if len(packed) != 3 or any(value < 0.0 for value in packed):
            raise ValueError("friction must contain three non-negative values")
        result = self._session._request("scene.set_rigid_body", {
            "object": self.reference.as_dict(), "enabled": bool(enabled),
            "dynamic": bool(dynamic), "gravity": bool(gravity), "mass": float(mass),
            "friction": packed, "restitution": float(restitution),
        })
        return str(result.get("physics", "configured"))

    def _update_transform(self, field_name: str, values: Iterable[float], count: int) -> None:
        packed = [float(item) for item in values]
        if len(packed) != count:
            raise ValueError(f"{field_name} must contain {count} values")
        self._session._request("scene.update_transform", {
            "object": self.reference.as_dict(), field_name: packed,
        })

    def set_parent(self, parent: "EntityProxy | None", *, preserve_world: bool = False) -> None:
        self._session._request("scene.set_parent", {
            "object": self.reference.as_dict(),
            "parent": None if parent is None else parent.reference.as_dict(),
            "preserve_world": preserve_world,
        })

    def destroy(self) -> None:
        self._session._request("scene.destroy_object", {"object": self.reference.as_dict()})


class LinkProxy(EntityProxy):
    pass


class CameraProxy(EntityProxy):
    def configure(
        self, *, frame_id: str = "camera", update_rate_hz: float = 30.0,
        width: int = 640, height: int = 480,
        vertical_fov_radians: float = np.pi / 3.0, near_plane: float = 0.01,
        far_plane: float = 1000.0, output: str = "rgbd",
    ) -> None:
        self._session._request("camera.configure", {
            "object": self.reference.as_dict(), "frame_id": frame_id,
            "update_rate_hz": update_rate_hz, "width": width, "height": height,
            "vertical_fov_radians": vertical_fov_radians, "near_plane": near_plane,
            "far_plane": far_plane, "output": output,
        })

    @property
    def dimensions(self) -> tuple[int, int]:
        value = self._session._request("camera.describe", {
            "object": self.reference.as_dict(),
        })["dimensions"]
        return int(value[0]), int(value[1])

    @property
    def intrinsics(self) -> np.ndarray:
        value = self._session._request("camera.describe", {
            "object": self.reference.as_dict(),
        })["intrinsics"]
        return np.asarray(value, dtype=np.float64)

    def _frame(self, mode: str) -> np.ndarray:
        result = self._session._request("camera.frame", {
            "object": self.reference.as_dict(), "mode": mode,
        })
        return np.frombuffer(base64.b64decode(result["data"]), dtype=np.dtype(
            str(result["dtype"]))).reshape(tuple(int(value) for value in result["shape"])).copy()

    @property
    def rgb(self) -> np.ndarray:
        return self._frame("rgb")

    @property
    def depth(self) -> np.ndarray:
        return self._frame("depth")


class LidarProxy(EntityProxy):
    def get_points(self) -> tuple[np.ndarray, np.ndarray]:
        result = self._session._request("lidar.points", {
            "object": self.reference.as_dict(),
        })
        count = int(result["count"])
        points = np.frombuffer(base64.b64decode(result["points"]), dtype=np.float32).reshape(
            (count, 3)).copy()
        distances = np.frombuffer(
            base64.b64decode(result["distances"]), dtype=np.float32).copy()
        return points, distances


class RobotProxy(EntityProxy):
    def _joint_data(self) -> dict[str, Any]:
        return self._session._request("robot.joint_state", {
            "robot_object": self.reference.as_dict(),
        })

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self._joint_data()["names"])

    @property
    def joint_count(self) -> int:
        return len(self.joint_names)

    @property
    def joint_state(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        result = self._joint_data()
        return tuple(
            np.asarray(result[name], dtype=np.float64)
            for name in ("positions", "velocities", "efforts")
        )  # type: ignore[return-value]

    def get_link(self, name: str) -> LinkProxy:
        result = self._session._request("robot.get_link", {
            "robot": self.reference.as_dict(), "name": name,
        })
        proxy = self._session._proxy(result["object"])
        if not isinstance(proxy, LinkProxy):
            raise LiveControlError("invalid_response", "server did not return a link")
        return proxy

    def command_joint_positions(self, targets: Iterable[float], *,
                                gain: float | None = None,
                                velocity_limit: float | None = None,
                                effort_limits: Iterable[float | None] | None = None) -> None:
        """Drive the joints through the controller.

        `gain` and `velocity_limit` override the controller defaults; leave both unset to keep
        the platform's. A slow, heavily damped approach wants a low velocity limit.
        `effort_limits` supplies persistent per-joint caps in Nm/N; None entries restore
        unrestricted tracking. Omitting the sequence preserves existing caps.
        """
        params: dict[str, Any] = {
            "robot_object": self.reference.as_dict(),
            "targets": [float(item) for item in targets],
        }
        if gain is not None:
            params["gain"] = float(gain)
        if velocity_limit is not None:
            params["velocity_limit"] = float(velocity_limit)
        if effort_limits is not None:
            params["effort_limits"] = effort_limit_values(effort_limits, len(params["targets"]))
        self._session._request("robot.command_joint_positions", params)

    def set_joint_positions(self, positions: Iterable[float], *,
                            limits: str = "clamp") -> None:
        """Pose the joints directly, bypassing the controller.

        `limits` picks what happens to a target outside a joint's range: "enforce" raises,
        "clamp" pulls it into range, "ignore" writes it through. Unlike
        `command_joint_positions` this costs no simulation steps, so it is the cheap way to
        place the arm at a known configuration.
        """
        self._session._request("robot.set_joint_positions", {
            "robot_object": self.reference.as_dict(),
            "positions": [float(item) for item in positions],
            "limits": str(limits),
        })

    def execute_joint_segment(self, start: Iterable[float], target: Iterable[float],
                              steps: int, *, paced: bool = True, **tuning: Any) -> float:
        """Interpolate commands at every physics tick within one Live request."""
        result = self._session._request("robot.execute_joint_segment", {
            "robot_object": self.reference.as_dict(), "start": list(start),
            "target": list(target), "steps": steps, "paced": paced, **tuning})
        return float(result.get("time", 0.0))

    def list_links(self) -> tuple[tuple[str, "LinkProxy"], ...]:
        """Every named link of this robot, so callers need not probe names one at a time."""
        result = self._session._request("robot.list_links", {
            "robot": self.reference.as_dict(),
        })
        links: list[tuple[str, LinkProxy]] = []
        for item in result["links"]:
            proxy = self._session._proxy(item["object"])
            if isinstance(proxy, LinkProxy):
                links.append((str(item["name"]), proxy))
        return tuple(links)

    def set_joint_continuous(self, joint_name: str, enabled: bool = True) -> None:
        self._session._request("robot.set_joint_continuous", {
            "robot_object": self.reference.as_dict(),
            "joint": str(joint_name),
            "enabled": bool(enabled),
        })

    def command_joint_velocities(self, targets: Iterable[float]) -> None:
        self._session._request("robot.command_joint_velocities", {
            "robot_object": self.reference.as_dict(),
            "targets": [float(item) for item in targets],
        })

    def configuration_collides(self, positions: Iterable[float]) -> bool:
        return bool(self._session._request("robot.configuration_collides", {
            "robot_object": self.reference.as_dict(),
            "positions": [float(item) for item in positions],
        })["colliding"])

    def configurations_collide(self, positions: Iterable[Iterable[float]]) -> np.ndarray:
        result = self._session._request("robot.configurations_collide", {
            "robot_object": self.reference.as_dict(),
            "positions": [[float(item) for item in row] for row in positions],
        })
        return np.asarray(result["colliding"], dtype=np.bool_)

    @property
    def joint_limits(self) -> np.ndarray:
        """Lower and upper bounds as (joint_count, 2). Continuous joints report -inf/+inf."""
        result = self._session._request("robot.joint_limits", {
            "robot_object": self.reference.as_dict(),
        })
        rows = [[-np.inf if lower is None else float(lower),
                 np.inf if upper is None else float(upper)]
                for lower, upper in result["limits"]]
        return np.asarray(rows, dtype=np.float64)

    def kinematics(self, positions: Iterable[Iterable[float]] | Iterable[float],
                   links: Iterable[str], *, jacobian_link: str | None = None,
                   limits: str = "ignore") -> "LiveKinematicsBatch":
        """Forward kinematics, and optionally a Jacobian, without disturbing the running world.

        Evaluated on a private copy of the world server-side. Each call is one round trip and one
        copy, so pass a batch of configurations rather than looping. The result is always indexed
        [configuration][link]; the Jacobian is (configuration_count, 6, joint_count), rows ordered
        linear xyz then angular xyz.
        """
        values = np.asarray(list(positions), dtype=np.float64)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.ndim != 2:
            raise ValueError("positions must have shape (configuration_count, joint_count)")
        params: dict[str, Any] = {
            "robot_object": self.reference.as_dict(),
            "positions": values.tolist(),
            "links": [str(link) for link in links],
            "limits": str(limits),
        }
        if jacobian_link is not None:
            params["jacobian_link"] = str(jacobian_link)
        result = self._session._request("robot.kinematics", params)
        transforms = tuple(
            tuple({"translation": tuple(float(v) for v in pose["translation"]),
                   "rotation_wxyz": tuple(float(v) for v in pose["rotation_wxyz"])}
                  for pose in configuration)
            for configuration in result["transforms"])
        jacobians = result.get("jacobians")
        return LiveKinematicsBatch(
            transforms,
            None if jacobians is None else np.asarray(jacobians, dtype=np.float64))

    def configuration_contacts(self, positions: Iterable[float], *,
                               distance_threshold: float = 0.0,
                               limits: str = "ignore") -> tuple["LiveContactPair", ...]:
        """Every colliding pair at one hypothetical configuration, reported individually.

        Adjacent links of a loaded robot always overlap, so take the pairs at the robot's rest
        pose as a baseline and treat only new pairs as real collisions. `distance` is surface
        separation and is 0.0 whenever `colliding` is set; penetration depth is not reported.
        """
        result = self._session._request("robot.configuration_contacts", {
            "robot_object": self.reference.as_dict(),
            "positions": [float(item) for item in positions],
            "distance_threshold": float(distance_threshold),
            "limits": str(limits),
        })
        return tuple(LiveContactPair(int(pair["robot_collider"]), int(pair["other"]),
                                     float(pair["distance"]), bool(pair["colliding"]))
                     for pair in result["contacts"])


class ObjectRegistry:
    def __init__(self, session: LiveSession):
        self._session = session

    def get(self, alias: str) -> EntityProxy | None:
        try:
            return self.require(alias)
        except LiveControlError as error:
            if error.code == "object_not_found":
                return None
            raise

    def require(self, alias: str, *, kind: str | None = None) -> EntityProxy:
        params: dict[str, Any] = {"alias": alias}
        if kind is not None:
            params["kind"] = kind
        result = self._session._request("workspace.resolve", params)
        return self._session._proxy(result["object"])

    def bind(self, alias: str, proxy: EntityProxy) -> None:
        self._session._request("workspace.bind", {
            "alias": alias, "object": proxy.reference.as_dict(),
        })

    def unbind(self, alias: str) -> bool:
        return bool(self._session._request("workspace.unbind", {"alias": alias})["removed"])


def _connect_tcp(endpoint: Endpoint, *, workspace: str, timeout: float) -> LiveSession:
    if endpoint.scheme == "tls":
        raise LiveControlError("unsupported_transport", "native TLS transport is not implemented")
    if endpoint.host is None or endpoint.port is None:
        raise LiveControlError("invalid_endpoint", "TCP endpoint is incomplete")
    try:
        connection = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
        connection.settimeout(timeout)
    except OSError as error:
        raise LiveControlError("instance_not_found", f"cannot connect to {endpoint}: {error}") from error
    try:
        return LiveSession(connection, endpoint, workspace=workspace)
    except Exception:
        connection.close()
        raise


def connect(endpoint: str | int | Endpoint, *, workspace: str = "default", timeout: float = 10.0,
            registry_directory: str | os.PathLike[str] | None = None) -> LiveSession:
    parsed = parse_endpoint(endpoint)
    candidates: tuple[Endpoint, ...]
    expected_instance: str | None = None
    if parsed.scheme == "pid":
        descriptor = _descriptor_for_pid(int(parsed.pid), registry_directory)
        candidates = _tcp_endpoints(descriptor)
        expected_instance = str(descriptor["instance_id"])
    else:
        candidates = (parsed,)
    if not candidates:
        raise LiveControlError("unsupported_transport", "instance has no supported TCP endpoint")
    errors: list[str] = []
    for candidate in candidates:
        try:
            session = _connect_tcp(candidate, workspace=workspace, timeout=timeout)
        except LiveControlError as error:
            errors.append(str(error))
            continue
        if expected_instance is not None and session.instance_id != expected_instance:
            session.close()
            raise LiveControlError("instance_mismatch", "discovery record and server instance differ")
        return session
    raise LiveControlError("instance_not_found", "; ".join(errors))


def connect_named(name: str, *, workspace: str = "default", timeout: float = 10.0,
                  registry_directory: str | os.PathLike[str] | None = None) -> LiveSession:
    descriptor = _descriptor_for_name(name, registry_directory)
    return connect(int(descriptor["pid"]), workspace=workspace, timeout=timeout,
                   registry_directory=registry_directory)


def _launch_command(spec: LaunchSpec, registry: Path) -> list[str]:
    listen = spec.listen or ("tcp://127.0.0.1:0",)
    endpoints = [parse_endpoint(item) for item in listen]
    if len(endpoints) != 1 or endpoints[0].scheme != "tcp":
        raise LiveControlError("unsupported_transport", "launch currently accepts one TCP listener")
    endpoint = endpoints[0]
    if spec.kind == "headless":
        command = ([sys.executable, "-m", "sim_platform.server"]
                   if spec.executable is None else [os.fspath(spec.executable)])
        command.extend(["--host", str(endpoint.host), "--port", str(endpoint.port),
                        "--physics", spec.physics, "--collision", spec.collision,
                        "--registry-dir", str(registry)])
    elif spec.kind == "editor":
        executable = Path(spec.executable).expanduser().resolve() if spec.executable else None
        if executable is None:
            configured = os.environ.get("SIM_EDITOR_EXECUTABLE")
            candidates = [Path(configured)] if configured else []
            root = Path(__file__).resolve().parents[2]
            names = ["sim_editor.exe", "sim_editor"]
            candidates.extend(directory / name for directory in (
                root / "build" / "windows-gui" / "Release",
                root / "build" / "windows-gui",
                root / "build",
            ) for name in names)
            executable = next((item.expanduser().resolve() for item in candidates
                               if item.is_file()), None)
        if executable is None or not executable.is_file():
            raise LiveControlError(
                "launch_failed", "sim_editor executable was not found; set executable or SIM_EDITOR_EXECUTABLE"
            )
        command = [str(executable), "--control-host", str(endpoint.host),
                   "--control-port", str(endpoint.port), "--registry-dir", str(registry)]
    else:
        raise LiveControlError("unsupported_launch_kind", f"unsupported launch kind: {spec.kind}")
    if spec.instance_name:
        command.extend(["--instance-name", spec.instance_name])
    if spec.cache_directory is not None and spec.kind == "headless":
        command.extend(["--cache-directory", os.fspath(spec.cache_directory)])
    for item in spec.advertise:
        command.extend(["--advertise", item])
    return command


def launch(spec: LaunchSpec, *, workspace: str = "default") -> LiveSession:
    registry = (Path(spec.registry_directory).expanduser().resolve()
                if spec.registry_directory is not None else default_registry_directory())
    registry.mkdir(parents=True, exist_ok=True)
    requested = tuple(parse_endpoint(item) for item in spec.listen)
    for endpoint in requested:
        if endpoint.scheme != "tcp" or endpoint.port in {None, 0}:
            continue
        family = socket.AF_INET6 if endpoint.host and ":" in endpoint.host else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.bind((str(endpoint.host), int(endpoint.port)))
        except OSError as error:
            raise LiveControlError("bind_failed", f"cannot bind {endpoint}: {error}") from error
        finally:
            probe.close()
    command = _launch_command(spec, registry)
    environment = os.environ.copy()
    environment.update(spec.environment)
    creationflags = 0
    start_new_session = False
    if spec.detached:
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            start_new_session = True
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if spec.detached else None,
        stderr=subprocess.DEVNULL if spec.detached else None,
        env=environment,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    deadline = time.monotonic() + spec.startup_timeout
    last_error: LiveControlError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LiveControlError("launch_failed", f"instance exited with code {process.returncode}")
        try:
            session = connect(process.pid, workspace=workspace, timeout=1.0,
                              registry_directory=registry)
            session._set_request_timeout(max(LAUNCHED_SESSION_TIMEOUT, spec.startup_timeout))
            _LAUNCHED_PROCESSES[process.pid] = process
            return session
        except LiveControlError as error:
            last_error = error
            time.sleep(0.05)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
    detail = f": {last_error}" if last_error is not None else ""
    raise LiveControlError("startup_timeout", f"instance did not become ready{detail}")


class _LaunchLock:
    def __init__(self, path: Path, timeout: float):
        self.path = path
        self.timeout = timeout
        self.token = str(uuid.uuid4())

    def __enter__(self) -> "_LaunchLock":
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "token": self.token, "time": time.time()})
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                    output.write(payload)
                return self
            except FileExistsError:
                age = 0.0
                try:
                    age = time.time() - self.path.stat().st_mtime
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                    stale = (not _pid_exists(int(existing.get("pid", 0))) or
                             age > self.timeout * 2)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError):
                    # Another Windows process may still be writing the newly created lock.
                    stale = age > self.timeout * 2
                if stale:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LiveControlError("startup_timeout", "timed out waiting for instance launch lock")
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if existing.get("token") == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass


def connect_or_launch(name: str, spec: LaunchSpec, *, workspace: str = "default") -> LiveSession:
    registry = (Path(spec.registry_directory).expanduser().resolve()
                if spec.registry_directory is not None else default_registry_directory())
    try:
        session = connect_named(
            name, workspace=workspace, timeout=1.0, registry_directory=registry
        )
        session._set_request_timeout(max(LAUNCHED_SESSION_TIMEOUT, spec.startup_timeout))
        return session
    except LiveControlError as error:
        if error.code != "instance_not_found":
            raise
    safe_name = "".join(character if character.isalnum() or character in "-_" else "_"
                        for character in name)
    with _LaunchLock(registry / f"launch-{safe_name}.lock", spec.startup_timeout):
        try:
            session = connect_named(
                name, workspace=workspace, timeout=1.0, registry_directory=registry
            )
            session._set_request_timeout(max(LAUNCHED_SESSION_TIMEOUT, spec.startup_timeout))
            return session
        except LiveControlError as error:
            if error.code != "instance_not_found":
                raise
        values = dict(spec.__dict__)
        values["instance_name"] = name
        return launch(LaunchSpec(**values), workspace=workspace)


__all__ = [
    "Endpoint", "EntityProxy", "LaunchSpec", "LinkProxy", "LiveControlError",
    "LiveSession", "ObjectRef", "RobotProxy", "connect", "connect_named",
    "connect_or_launch", "default_registry_directory", "launch", "list_instances",
    "parse_endpoint",
]
