from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ._control import effort_limit_values


class EditorRpcError(RuntimeError):
    pass


@dataclass(frozen=True)
class EditorRobot:
    _session: "EditorSession"
    handle: int
    joint_names: tuple[str, ...]

    @property
    def joint_count(self) -> int:
        return len(self.joint_names)

    def get_link(self, name: str) -> int:
        """Return a robot link entity handle by its exact link name."""
        if not name or not name.strip():
            raise ValueError("link name must not be empty")
        result = self._session._request(
            "robot.get_link", {"robot": self.handle, "name": str(name)}
        )
        try:
            return int(result["object"]["runtime_handle"])
        except (KeyError, TypeError, ValueError) as error:
            raise EditorRpcError("simulator returned an invalid robot link") from error

    @property
    def joint_state(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        result = self._session._request("robot.joint_state", {"robot": self.handle})
        return tuple(
            np.asarray(result[name], dtype=np.float64)
            for name in ("positions", "velocities", "efforts")
        )  # type: ignore[return-value]

    def command_joint_velocities(self, targets: Iterable[float]) -> None:
        values = [float(value) for value in targets]
        if len(values) != self.joint_count:
            raise ValueError("joint velocity count does not match the robot")
        self._session._request(
            "robot.command_joint_velocities", {"robot": self.handle, "targets": values}
        )

    def command_joint_positions(
        self,
        targets: Iterable[float],
        *,
        gain: float = 10.0,
        velocity_limit: float = 1.0,
        effort_limits: Iterable[float | None] | None = None,
    ) -> None:
        values = [float(value) for value in targets]
        if len(values) != self.joint_count:
            raise ValueError("joint position count does not match the robot")
        if gain <= 0.0 or velocity_limit <= 0.0:
            raise ValueError("gain and velocity_limit must be positive")
        params = {
            "robot": self.handle,
            "targets": values,
            "gain": float(gain),
            "velocity_limit": float(velocity_limit),
        }
        if effort_limits is not None:
            params["effort_limits"] = effort_limit_values(effort_limits, len(values))
        self._session._request(
            "robot.command_joint_positions", params,
        )

    def set_joint_continuous(self, joint_name: str, enabled: bool = True) -> None:
        if joint_name not in self.joint_names:
            raise ValueError(f"joint {joint_name!r} was not found")
        self._session._request(
            "robot.set_joint_continuous",
            {"robot": self.handle, "joint": joint_name, "enabled": enabled},
        )

    def set_joint_positions(self, positions: Iterable[float]) -> None:
        """Pose the robot directly, bypassing its servos.

        The editor renders a scene rather than simulating one, so it does not step
        physics: a target given to `command_joint_positions` is never converged toward
        and the arm stays where it is. Use this to show a pose. Values outside a joint's
        limits are clamped rather than refused, since a solver's intermediate waypoints
        can sit marginally out of range.
        """
        values = [float(value) for value in positions]
        if len(values) != self.joint_count:
            raise ValueError("joint position count does not match the robot")
        self._session._request(
            "robot.set_joint_positions", {"robot": self.handle, "positions": values}
        )


class EditorSession:
    def __init__(self, process: subprocess.Popen[bytes], connection: socket.socket):
        self._process = process
        self._connection = connection
        self._stream = connection.makefile("rwb")
        self._request_id = 1

    @classmethod
    def launch(
        cls, editor: str | Path, *, startup_timeout: float = 15.0
    ) -> "EditorSession":
        executable = Path(editor).expanduser().resolve()
        if not executable.is_file():
            raise FileNotFoundError(f"simulator executable was not found: {executable}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([str(executable), "--control-port", str(port)])
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"simulator exited during startup with code {process.returncode}")
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=0.25)
                # Exact robot collision meshes can take several seconds to compile in MuJoCo.
                connection.settimeout(60.0)
                session = cls(process, connection)
                session._request("capabilities.query")
                return session
            except OSError:
                time.sleep(0.05)
        process.terminate()
        raise TimeoutError("timed out waiting for the simulator control server")

    @property
    def is_open(self) -> bool:
        return self._process.poll() is None

    def load_robot(self, path: str | Path) -> EditorRobot:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"robot asset was not found: {source}")
        result = self._request("scene.load_robot", {"path": str(source)})
        return EditorRobot(
            self, int(result["robot"]), tuple(str(name) for name in result["joint_names"])
        )

    def load_asset(self, path: str | Path) -> int:
        """Load a URDF, OBJ, or FBX asset and return its scene root entity."""
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"asset was not found: {source}")
        result = self._request("scene.load_asset", {"path": str(source)})
        return int(result["entity"])

    def import_remote_asset(self, url: str) -> int:
        """Download a remote asset URL into the editor cache and load it into the scene.

        The Qt editor downloads the asset (plus its dependencies) into its
        .sim-cache/remote-downloads/ directory, then loads it locally via the
        standard load_asset path. Returns the root entity handle.
        """
        if not url or not url.strip():
            raise ValueError("url must not be empty")
        result = self._request("scene.load_asset_remote", {"url": str(url).strip()})
        return int(result["entity"])

    def scene_hierarchy(self) -> tuple[dict[str, Any], ...]:
        """Return the editor scene hierarchy as flat entity records."""
        result = self._request("scene.hierarchy")
        return tuple(dict(item) for item in result["entities"])

    def set_sensor_enabled(self, entity: int, enabled: bool = True) -> None:
        """Enable or suspend a sensor without changing its saved configuration."""
        self._request(
            "sensor.set_enabled", {"entity": int(entity), "enabled": bool(enabled)}
        )

    def set_parent(self, entity: int, parent: int | None) -> None:
        """Reparent an entity while preserving its current world transform."""
        self._request(
            "scene.set_parent",
            {"entity": int(entity), "parent": None if parent is None else int(parent)},
        )

    def create_primitive(self, kind: str) -> int:
        """Create a one-metre untextured primitive with matching collision geometry."""
        normalized = kind.strip().lower()
        valid = {"cube", "sphere", "plane", "capsule", "cylinder"}
        if normalized not in valid:
            raise ValueError(f"primitive kind must be one of {sorted(valid)}")
        result = self._request("scene.create_primitive", {"kind": normalized})
        return int(result["entity"])

    def draw_trajectories(
        self,
        trajectories: Iterable[tuple[str, Iterable[Iterable[float]], Iterable[float]]],
        *,
        radius: float = 0.008,
    ) -> int:
        """Draw named trajectories as rows of coloured marker spheres.

        Each entry is `(id, points, color)`, where points are world positions and color is
        RGB or RGBA in 0..1. Redrawing an id replaces that trajectory. Markers are visual
        only -- no collider, no rigid body -- so a drawn path never obstructs the arm whose
        motion it depicts and never falls under gravity.

        The whole set travels in one request on purpose: the editor rebuilds its outliner
        once per call, so sending candidates individually would rebuild it once each.
        Returns the total number of markers created.
        """
        if radius <= 0.0:
            raise ValueError("radius must be positive")
        packed = []
        for name, points, color in trajectories:
            identifier = str(name).strip()
            if not identifier:
                raise ValueError("trajectory id must not be empty")
            coordinates = [[float(value) for value in point] for point in points]
            if any(len(point) != 3 for point in coordinates):
                raise ValueError("each waypoint must contain three coordinates")
            channels = [float(value) for value in color]
            if len(channels) == 3:
                channels.append(1.0)
            if len(channels) != 4 or any(not 0.0 <= value <= 1.0 for value in channels):
                raise ValueError("color must be three or four values between 0 and 1")
            packed.append({
                "id": identifier, "points": coordinates, "color": channels,
                "radius": float(radius),
            })
        if not packed:
            return 0
        result = self._request("scene.draw_trajectories", {"trajectories": packed})
        return int(result["markers"])

    def clear_trajectories(self, name: str | None = None) -> int:
        """Erase one drawn trajectory, or all of them. Returns how many were removed."""
        params = {} if name is None else {"id": str(name).strip()}
        return int(self._request("scene.clear_trajectories", params)["cleared"])

    def mirror_pose(
        self,
        *,
        joints: Iterable[float] | None = None,
        objects: Iterable[tuple[int, Iterable[float], Iterable[float] | None]] = (),
    ) -> None:
        """Push a robot pose and object poses to the editor on the fast display path.

        Built for streaming: unlike `set_transform`, this skips the physics/collision
        rebuild and the outliner rebuild, and it does not render inline. It marks the
        viewport dirty and returns, leaving the editor's own ~30 fps frame timer to draw.
        Poses may therefore be sent as fast as motion produces them without stalling the
        editor's UI thread.

        Objects are `(entity, translation, rotation_wxyz_or_None)`.
        """
        params: dict[str, Any] = {}
        if joints is not None:
            params["joints"] = [float(value) for value in joints]
        packed = []
        for entity, translation, rotation in objects:
            entry: dict[str, Any] = {"entity": int(entity)}
            position = [float(value) for value in translation]
            if len(position) != 3:
                raise ValueError("translation must contain three values")
            entry["translation"] = position
            if rotation is not None:
                orientation = [float(value) for value in rotation]
                if len(orientation) != 4:
                    raise ValueError("rotation_wxyz must contain four values")
                entry["rotation_wxyz"] = orientation
            packed.append(entry)
        if packed:
            params["objects"] = packed
        if params:
            self._request("scene.mirror_pose", params)

    def create_camera(
        self, parent: int | None = None, *, output: str = "rgbd",
        frame_id: str = "camera_optical_frame", update_rate_hz: float = 30.0,
        width: int = 640, height: int = 480,
        vertical_fov_radians: float = 1.0471975511965976,
        near_plane: float = 0.1, far_plane: float = 10.0,
    ) -> int:
        """Create an RGB or RGBD camera sensor in the editor scene."""
        normalized = output.strip().lower()
        if normalized not in {"rgb", "rgbd"}:
            raise ValueError("output must be 'rgb' or 'rgbd'")
        if (update_rate_hz <= 0.0 or width <= 0 or height <= 0 or
                not 0.0 < vertical_fov_radians < np.pi or
                near_plane <= 0.0 or far_plane <= near_plane):
            raise ValueError("camera parameters are invalid")
        result = self._request("scene.create_camera", {
            "parent": None if parent is None else int(parent),
            "output": normalized, "frame_id": str(frame_id),
            "update_rate_hz": float(update_rate_hz), "width": int(width),
            "height": int(height),
            "vertical_fov_radians": float(vertical_fov_radians),
            "near_plane": float(near_plane), "far_plane": float(far_plane),
        })
        return int(result["entity"])

    def save_state(self, path: str | Path) -> Path:
        """Save the complete editor scene to a .simstate file."""
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._request("scene.save_state", {"path": str(target)})
        return target

    def load_state(self, path: str | Path) -> int:
        """Replace the editor scene from a .simstate file and return its entity count."""
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"state file was not found: {source}")
        result = self._request("scene.load_state", {"path": str(source)})
        return int(result["entities"])

    def set_backends(self, *, physics: str | None = None,
                     collision: str | None = None) -> tuple[str, str]:
        """Select the editor's physics and collision backends; returns what became active.

        The editor's backend combo boxes default to their first entry, which is "None" for
        physics -- so an editor driven purely over this socket simulates nothing until told
        otherwise. Pass the same backends the client's own world uses, or the two will
        disagree about anything gravity touches.

        Omitting either argument leaves that backend as it is.
        """
        params: dict[str, object] = {}
        if physics is not None:
            params["physics"] = str(physics)
        if collision is not None:
            params["collision"] = str(collision)
        result = self._request("scene.set_backends", params)
        return str(result["physics"]), str(result["collision"])

    def check_collision(self, first: int, second: int) -> bool:
        result = self._request(
            "collision.check", {"first": int(first), "second": int(second)}
        )
        return bool(result["colliding"])

    def create_collision_grid_benchmark(
        self, *, physics: str = "mujoco", collision: str = "fcl", grid_size: int = 10
    ) -> tuple[int, tuple[int, ...]]:
        """Create a square falling-ball benchmark scene in one native batch."""
        normalized = physics.strip().lower()
        if normalized not in {"mujoco", "bullet3"}:
            raise ValueError("physics must be 'mujoco' or 'bullet3'")
        collision = collision.strip().lower()
        if collision not in {"fcl", "bullet3", "coal", "ifps-cpu", "ifps-gpu"}:
            raise ValueError(
                "collision must be 'fcl', 'bullet3', 'coal', 'ifps-cpu', or 'ifps-gpu'"
            )
        if grid_size < 2:
            raise ValueError("grid_size must be at least 2")
        result = self._request(
            "benchmark.create_collision_grid",
            {"physics": normalized, "collision": collision, "grid_size": int(grid_size)},
        )
        return int(result["plane"]), tuple(int(value) for value in result["balls"])

    def profile_step(self, entities: Iterable[int], *, render: bool = True) -> dict[str, Any]:
        """Advance one fixed tick and return native physics, collision, and render timings."""
        result = self._request(
            "diagnostics.profile_step",
            {"entities": [int(value) for value in entities], "render": bool(render)},
        )
        return {
            "simulation_time": float(result["simulation_time"]),
            "physics_tick_ms": float(result["physics_tick_ms"]),
            "collision_sync_ms": float(result["collision_sync_ms"]),
            "collision_check_ms": float(result["collision_check_ms"]),
            "render_ms": float(result["render_ms"]),
            "collisions": [bool(value) for value in result["collisions"]],
        }

    def set_transform(
        self,
        entity: int,
        *,
        translation: Iterable[float] | None = None,
        rotation_wxyz: Iterable[float] | None = None,
        scale: Iterable[float] | None = None,
    ) -> None:
        params: dict[str, Any] = {"entity": int(entity)}
        for name, values, count in (
            ("translation", translation, 3),
            ("rotation_wxyz", rotation_wxyz, 4),
            ("scale", scale, 3),
        ):
            if values is None:
                continue
            packed = [float(value) for value in values]
            if len(packed) != count:
                raise ValueError(f"{name} must contain {count} values")
            params[name] = packed
        self._request("scene.set_transform", params)

    def set_name(self, entity: int, name: str) -> None:
        """Set an entity's stable display/object name for scene references."""
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("entity name must not be empty")
        self._request("scene.set_name", {"entity": int(entity), "name": normalized})

    def get_transform(
        self, entity: int
    ) -> tuple[tuple[float, float, float], tuple[float, float, float, float],
               tuple[float, float, float]]:
        result = self._request("scene.get_transform", {"entity": int(entity)})
        return tuple(
            tuple(float(value) for value in result[name])
            for name in ("translation", "rotation_wxyz", "scale")
        )  # type: ignore[return-value]

    def set_rigid_body(
        self,
        entity: int,
        *,
        enabled: bool = True,
        dynamic: bool = False,
        gravity: bool = True,
        mass: float = 1.0,
        restitution: float | None = None,
        friction: Iterable[float] | None = None,
    ) -> str:
        """Configure an editor entity's rigid body and return the active physics backend."""
        if mass <= 0.0:
            raise ValueError("mass must be positive")
        params: dict[str, Any] = {
            "entity": int(entity),
            "enabled": bool(enabled),
            "dynamic": bool(dynamic),
            "gravity": bool(gravity),
            "mass": float(mass),
        }
        if restitution is not None:
            if not 0.0 <= restitution <= 1.0:
                raise ValueError("restitution must be between 0 and 1")
            params["restitution"] = float(restitution)
        if friction is not None:
            packed = [float(value) for value in friction]
            if len(packed) != 3 or any(value < 0.0 for value in packed):
                raise ValueError("friction must contain three non-negative values")
            params["friction"] = packed
        result = self._request("scene.set_rigid_body", params)
        return str(result["physics"])

    def start(self) -> None:
        self._request("simulation.start")

    def pause(self) -> None:
        self._request("simulation.pause")

    @property
    def simulation_time(self) -> float:
        return float(self._request("simulation.time")["time"])

    @property
    def performance(self) -> tuple[float, float]:
        result = self._request("diagnostics.performance")
        return float(result["render_fps"]), float(result["render_ms"])

    def close(self) -> None:
        if self._process.poll() is None:
            try:
                self._request("application.quit")
            except (EditorRpcError, OSError):
                pass
        try:
            self._stream.close()
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            self._process.wait(timeout=5)

    def __enter__(self) -> "EditorSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = {"id": self._request_id, "method": method, "params": params or {}}
        self._request_id += 1
        try:
            self._stream.write(
                json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            self._stream.flush()
            line = self._stream.readline()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as error:
            raise EditorRpcError("simulator window was closed") from None
        if not line:
            raise EditorRpcError("simulator control connection was closed")
        response = json.loads(line.decode("utf-8"))
        if not response.get("ok"):
            error = response.get("error", {})
            raise EditorRpcError(str(error.get("message", "simulator control request failed")))
        return dict(response.get("result", {}))
