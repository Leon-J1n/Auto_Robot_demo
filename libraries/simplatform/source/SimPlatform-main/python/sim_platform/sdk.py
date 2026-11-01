from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import numpy as np

from ._control import effort_limit_values
from ._logging import debug as _log_debug, error as _log_error, log_verbose

if TYPE_CHECKING:
    from .curobo_backend import CuroboCollisionBackend


class _Config(ctypes.Structure):
    _fields_ = [("cache_directory_utf8", ctypes.c_char_p), ("physics_backend", ctypes.c_int),
               ("collision_backend", ctypes.c_int)]


class _Transform(ctypes.Structure):
    _fields_ = [
        ("translation", ctypes.c_double * 3),
        ("rotation_wxyz", ctypes.c_double * 4),
        ("scale", ctypes.c_double * 3),
    ]

class _JointInfo(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("axis", ctypes.c_double * 3),
        ("lower_limit", ctypes.c_double),
        ("upper_limit", ctypes.c_double),
        ("entity", ctypes.c_uint64),
        ("parent_link", ctypes.c_uint64),
        ("child_link", ctypes.c_uint64),
    ]


class _ContactPair(ctypes.Structure):
    _fields_ = [
        ("robot_collider", ctypes.c_uint64),
        ("other", ctypes.c_uint64),
        ("distance", ctypes.c_double),
        ("colliding", ctypes.c_int),
    ]


class _PhysicsContact(ctypes.Structure):
    _fields_ = [("first", ctypes.c_uint64), ("second", ctypes.c_uint64),
                ("distance", ctypes.c_double), ("normal_force", ctypes.c_double)]


class _LightProperties(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("color_rgb", ctypes.c_float * 3), ("intensity", ctypes.c_float),
                ("range", ctypes.c_float), ("decay", ctypes.c_float), ("inner_cone_radians", ctypes.c_float),
                ("outer_cone_radians", ctypes.c_float), ("angular_diameter_radians", ctypes.c_float),
                ("enabled", ctypes.c_int)]


class _Event(ctypes.Structure):
    _fields_ = [
        ("type_utf8", ctypes.c_char * 64),
        ("world", ctypes.c_uint64),
        ("entity", ctypes.c_uint64),
        ("simulation_time", ctypes.c_double),
        ("detail_utf8", ctypes.c_char * 256),
    ]


class _EditorEntityInfo(ctypes.Structure):
    _fields_ = [
        ("entity", ctypes.c_uint64), ("parent", ctypes.c_uint64),
        ("component_flags", ctypes.c_uint32), ("name_utf8", ctypes.c_char * 256),
        ("local_transform", _Transform), ("world_transform", _Transform),
        ("visual_visible", ctypes.c_int), ("min_lod", ctypes.c_uint8),
        ("max_lod", ctypes.c_uint8), ("visual_shape", ctypes.c_int),
        ("visual_size", ctypes.c_double * 3), ("visual_color", ctypes.c_float * 4),
        ("visual_resource_utf8", ctypes.c_char * 1024),
        ("collider_shape", ctypes.c_int), ("collider_size", ctypes.c_double * 3),
        ("collider_resource_utf8", ctypes.c_char * 1024),
        ("collider_trigger", ctypes.c_int), ("friction", ctypes.c_double * 3),
        ("restitution", ctypes.c_double), ("ifps_sphere_count", ctypes.c_uint32),
        ("body_dynamic", ctypes.c_int), ("gravity_enabled", ctypes.c_int),
        ("mass", ctypes.c_double), ("sensor_frame_utf8", ctypes.c_char * 256),
        ("sensor_rate_hz", ctypes.c_double), ("camera_width", ctypes.c_uint32),
        ("camera_height", ctypes.c_uint32),
        ("camera_vertical_fov_radians", ctypes.c_double),
        ("camera_near_plane", ctypes.c_double), ("camera_far_plane", ctypes.c_double),
        ("camera_rgbd_output", ctypes.c_int),
        ("light", _LightProperties),
    ]


def _library_path() -> Path:
    configured = os.environ.get("SIM_PLATFORM_LIBRARY")
    if configured:
        return Path(configured)
    package = Path(__file__).resolve().parent
    names = ["sim_platform_c.dll", "libsim_platform_c.so", "libsim_platform_c.dylib"]
    for parent in [package, package.parent.parent / "build",
                   package.parent.parent / "build-mujoco",
                   package.parent.parent / "build" / "windows-full" / "Release",
                   package.parent.parent / "build" / "windows-gui" / "Release"]:
        for name in names:
            candidate = parent / name
            if candidate.exists():
                return candidate
    raise RuntimeError("sim_platform C API library was not found; set SIM_PLATFORM_LIBRARY")


_DLL_DIRECTORIES: list[object] = []


def _load_library() -> ctypes.CDLL:
    path = _library_path()
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        candidates = [path.parent, *(Path(item) for item in os.environ.get("PATH", "").split(os.pathsep))]
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    _DLL_DIRECTORIES.append(os.add_dll_directory(str(candidate)))
            except OSError:
                # PATH may contain inaccessible application-managed directories.
                pass
    library = ctypes.CDLL(str(path))
    handle = ctypes.c_void_p
    u64 = ctypes.c_uint64
    library.sim_api_version.argtypes = []
    library.sim_api_version.restype = ctypes.c_uint32
    version = int(library.sim_api_version())
    if version < 9:
        raise RuntimeError(
            f"sim_platform C API library is incompatible: {path} reports version {version}, "
            "but this Python SDK requires version 9 or newer; rebuild the library or set "
            "SIM_PLATFORM_LIBRARY to a current build"
        )
    library.sim_last_error.restype = ctypes.c_char_p
    library.sim_create.argtypes = [ctypes.POINTER(_Config), ctypes.POINTER(handle)]
    library.sim_create_with_plugins.argtypes = [ctypes.POINTER(_Config), ctypes.c_char_p,
                                                 ctypes.POINTER(handle)]
    library.sim_destroy.argtypes = [handle]
    library.sim_create_world.argtypes = [handle, ctypes.c_double, u64, ctypes.POINTER(u64)]
    library.sim_editor_entity_count.argtypes = [handle, u64, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_editor_entity_info.argtypes = [
        handle, u64, ctypes.c_size_t, ctypes.POINTER(_EditorEntityInfo)]
    library.sim_scene_snapshot_json_size.argtypes = [
        handle, u64, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_scene_snapshot_json.argtypes = [
        handle, u64, ctypes.POINTER(ctypes.c_char), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t)]
    library.sim_scene_transforms_json_size.argtypes = [
        handle, u64, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_scene_transforms_json.argtypes = [
        handle, u64, ctypes.POINTER(ctypes.c_char), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t)]
    library.sim_scene_metadata.argtypes = [
        handle, u64, ctypes.POINTER(u64), ctypes.POINTER(u64)]
    library.sim_get_object_id.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_char),
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_find_entity_by_object_id.argtypes = [handle, u64, ctypes.c_char_p,
                                                      ctypes.POINTER(u64)]
    library.sim_load_asset.argtypes = [handle, u64, ctypes.c_char_p, ctypes.POINTER(u64)]
    library.sim_create_entity.argtypes = [handle, u64, ctypes.c_char_p, u64, ctypes.POINTER(u64)]
    library.sim_destroy_entity.argtypes = [handle, u64, u64]
    library.sim_set_parent.argtypes = [handle, u64, u64, u64, ctypes.c_int]
    library.sim_set_entity_name.argtypes = [handle, u64, u64, ctypes.c_char_p]
    library.sim_set_transform.argtypes = [handle, u64, u64, ctypes.POINTER(_Transform)]
    library.sim_create_light.argtypes = [handle, u64, ctypes.c_char_p, ctypes.c_int, u64, ctypes.POINTER(_LightProperties), ctypes.POINTER(u64)]
    library.sim_set_light_properties.argtypes = [handle, u64, u64, ctypes.POINTER(_LightProperties)]
    library.sim_get_transform.argtypes = [handle, u64, u64, ctypes.POINTER(_Transform)]
    library.sim_set_transforms.argtypes = [handle, u64, ctypes.POINTER(u64),
                                           ctypes.POINTER(_Transform), ctypes.c_size_t]
    library.sim_robot_joint_count.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_robot_joint_name.argtypes = [handle, u64, u64, ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_char), ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_size_t)]
    library.sim_robot_link_count.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_robot_link_info.argtypes = [handle, u64, u64, ctypes.c_size_t,
                                             ctypes.POINTER(u64), ctypes.POINTER(ctypes.c_char),
                                             ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    double_ptr = ctypes.POINTER(ctypes.c_double)
    library.sim_create_primitive.argtypes = [handle, u64, ctypes.c_char_p, ctypes.c_int,
                                              double_ptr, ctypes.POINTER(u64)]
    library.sim_set_rigid_body.argtypes = [handle, u64, u64, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_double, double_ptr,
                                            ctypes.c_double]
    library.sim_set_visual_properties.argtypes = [handle, u64, u64, ctypes.c_int,
                                                   ctypes.c_uint8, ctypes.c_uint8]
    library.sim_set_ifps_sphere_count.argtypes = [handle, u64, u64, ctypes.c_uint32]
    library.sim_get_ifps_sphere_count.argtypes = [handle, u64, u64,
                                                   ctypes.POINTER(ctypes.c_uint32)]
    library.sim_robot_get_joint_state.argtypes = [handle, u64, u64, double_ptr, double_ptr,
                                                   double_ptr, ctypes.c_size_t]
    library.sim_robot_command_joint_positions.argtypes = [handle, u64, u64, double_ptr,
                                                           ctypes.c_size_t]
    library.sim_robot_set_joint_continuous.argtypes = [handle, u64, u64, ctypes.c_char_p,
                                                        ctypes.c_int]
    library.sim_robot_command_joint_velocities.argtypes = [handle, u64, u64, double_ptr,
                                                            ctypes.c_size_t]
    library.sim_robot_command_joint_positions_batch.argtypes = [
        handle, u64, ctypes.POINTER(u64), ctypes.c_size_t, double_ptr, ctypes.c_size_t]
    library.sim_robot_set_joint_positions.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.c_int]
    library.sim_robot_joint_info.argtypes = [handle, u64, u64, ctypes.c_size_t,
                                             ctypes.POINTER(_JointInfo)]
    library.sim_robot_command_joint_positions_tuned.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.c_double, ctypes.c_double]
    library.sim_robot_command_joint_positions_with_effort_limits.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.c_double, ctypes.c_double,
        double_ptr]
    library.sim_robot_command_joint_efforts.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t]
    library.sim_robot_set_joint_effort_limits.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t]
    library.sim_robot_kinematics_batch.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.POINTER(u64), ctypes.c_size_t, u64, ctypes.c_int,
        ctypes.POINTER(_Transform), double_ptr]
    library.sim_robot_configuration_contacts.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.c_double, ctypes.c_int,
        ctypes.POINTER(_ContactPair), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_collision_sync.argtypes = [handle, u64]
    library.sim_physics_sync.argtypes = [handle, u64]
    library.sim_physics_contacts.argtypes = [handle, u64, ctypes.POINTER(_PhysicsContact),
                                            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_create_camera.argtypes = [handle, u64, u64, ctypes.c_char_p, ctypes.c_double,
                                          ctypes.c_uint32, ctypes.c_uint32, ctypes.c_double,
                                          ctypes.c_double, ctypes.c_double, ctypes.POINTER(u64)]
    library.sim_set_camera_properties.argtypes = [
        handle, u64, u64, ctypes.c_char_p, ctypes.c_double, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_int]
    library.sim_camera_get_dimensions.argtypes = [handle, u64, u64,
                                                   ctypes.POINTER(ctypes.c_uint32),
                                                   ctypes.POINTER(ctypes.c_uint32)]
    library.sim_camera_get_intrinsics.argtypes = [
        handle, u64, u64, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
    library.sim_camera_get_rgb.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_uint8),
                                           ctypes.c_size_t]
    library.sim_camera_get_depth.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_float),
                                             ctypes.c_size_t]
    library.sim_create_lidar.argtypes = [handle, u64, u64, ctypes.c_char_p, ctypes.c_double,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_double,
                                         ctypes.c_double, ctypes.POINTER(u64)]
    library.sim_lidar_point_count.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_size_t)]
    library.sim_lidar_get_points.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_float),
                                              ctypes.POINTER(ctypes.c_float), ctypes.c_size_t]
    library.sim_collision_check.argtypes = [handle, u64, u64, u64, ctypes.POINTER(ctypes.c_int)]
    library.sim_collision_distance.argtypes = [handle, u64, u64, u64,
                                                ctypes.POINTER(ctypes.c_double)]
    library.sim_scene_collision_batch.argtypes = [handle, u64, ctypes.POINTER(u64),
                                                   ctypes.c_size_t,
                                                   ctypes.POINTER(ctypes.c_uint8),
                                                   ctypes.POINTER(ctypes.c_double)]
    library.sim_robot_configuration_collision.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
    library.sim_robot_configurations_collision.argtypes = [
        handle, u64, u64, double_ptr, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint8)]
    library.sim_apply_body_wrench.argtypes = [handle, u64, u64, double_ptr, double_ptr]
    library.sim_start.argtypes = [handle, u64]
    library.sim_pause.argtypes = [handle, u64]
    library.sim_get_status.argtypes = [handle, u64, ctypes.POINTER(ctypes.c_int)]
    library.sim_poll_event.argtypes = [handle, u64, ctypes.POINTER(_Event),
                                       ctypes.POINTER(ctypes.c_int)]
    library.sim_step.argtypes = [handle, u64, u64]
    library.sim_step_unpaced.argtypes = [handle, u64, u64]
    library.sim_step_unpaced.restype = ctypes.c_int
    library.sim_step_profiled.argtypes = [handle, u64, u64, ctypes.POINTER(ctypes.c_double)]
    library.sim_get_time.argtypes = [handle, u64, ctypes.POINTER(ctypes.c_double)]
    library.sim_save_state.argtypes = [handle, u64, ctypes.POINTER(u64)]
    library.sim_restore_state.argtypes = [handle, u64, u64]
    library.sim_save_state_file.argtypes = [handle, u64, ctypes.c_char_p]
    library.sim_restore_state_file.argtypes = [handle, u64, ctypes.c_char_p]
    library.sim_reset.argtypes = [handle, u64]
    return library


_LIB = _load_library()


def api_version() -> int:
    """Return the version of the loaded stable C API."""
    return int(_LIB.sim_api_version())


def _check(result: int) -> None:
    if result != 0:
        message = _LIB.sim_last_error()
        text = message.decode("utf-8", errors="replace") if message else "simulation error"
        _log_error("SDK.CAPI", f"native call failed result={result} error={text}")
        raise RuntimeError(text)


@dataclass(frozen=True)
class Transform:
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_wxyz: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def _native(self) -> _Transform:
        return _Transform(self.translation, self.rotation_wxyz, self.scale)


def _transform(value: _Transform) -> Transform:
    return Transform(tuple(value.translation), tuple(value.rotation_wxyz), tuple(value.scale))


_JOINT_TYPE_NAMES = ("fixed", "revolute", "continuous", "prismatic", "floating", "planar")
_JOINT_LIMIT_POLICIES = {"enforce": 0, "clamp": 1, "ignore": 2}


def _limit_policy(value: str) -> int:
    try:
        return _JOINT_LIMIT_POLICIES[value]
    except KeyError:
        raise ValueError(
            f"limits must be one of {', '.join(sorted(_JOINT_LIMIT_POLICIES))}") from None


@dataclass(frozen=True)
class JointInfo:
    """Static description of one joint, in the order joint_names reports."""

    name: str
    type: str
    axis: tuple[float, float, float]
    lower_limit: float
    upper_limit: float
    entity: int
    parent_link: int
    child_link: int

    @property
    def movable(self) -> bool:
        """Whether the joint is a degree of freedom. A loaded robot carries many fixed joints."""
        return self.type != "fixed"


@dataclass(frozen=True)
class ContactPair:
    """One robot collider measured against one other collider.

    `distance` is surface separation, so it is 0.0 whenever `colliding` is true; penetration
    depth is not reported. Entities are raw handles -- cross-reference
    World.editor_entities() to recover names.
    """

    robot_collider: int
    other: int
    distance: float
    colliding: bool


@dataclass(frozen=True)
class KinematicsBatch:
    """Link poses, and optionally Jacobians, over a batch of configurations."""

    transforms: tuple[tuple[Transform, ...], ...]  # [configuration][link]
    jacobians: np.ndarray | None  # (configuration_count, 6, joint_count)


@dataclass(frozen=True)
class State:
    handle: int


@dataclass(frozen=True)
class SceneSnapshot:
    schema_version: str
    scene_id: str
    world_id: int
    scene_generation: int
    revision: int
    simulation_time: float
    status: str
    entities: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SceneSnapshot":
        if str(value.get("schemaVersion", "")) != "1.0":
            raise ValueError("unsupported scene snapshot schemaVersion")
        entities = value.get("entities")
        if not isinstance(entities, list) or not all(isinstance(item, dict) for item in entities):
            raise ValueError("scene snapshot entities must be an array of objects")
        return cls(
            schema_version="1.0",
            scene_id=str(value.get("sceneId", "main")),
            world_id=int(value["worldId"]),
            scene_generation=int(value.get("sceneGeneration", 0)),
            revision=int(value.get("revision", 0)),
            simulation_time=float(value.get("simulationTime", 0.0)),
            status=str(value.get("status", "stopped")),
            entities=tuple(dict(item) for item in entities),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "sceneId": self.scene_id,
            "worldId": self.world_id,
            "sceneGeneration": self.scene_generation,
            "revision": self.revision,
            "simulationTime": self.simulation_time,
            "status": self.status,
            "entities": [dict(item) for item in self.entities],
        }


@dataclass(frozen=True)
class Event:
    type: str
    world: int
    entity: int
    simulation_time: float
    detail: str


@dataclass(frozen=True)
class EditorEntityInfo:
    entity: int
    parent: int | None
    flags: frozenset[str]
    name: str
    local: Transform
    world: Transform
    visual: dict[str, object] | None
    collider: dict[str, object] | None
    rigid_body: dict[str, object] | None
    sensor: dict[str, object] | None
    camera: dict[str, object] | None
    light: dict[str, object] | None = None


class Entity:
    def __init__(self, world: World, handle: int):
        self._world = world
        self.handle = handle

    @property
    def object_id(self) -> str:
        required = ctypes.c_size_t()
        _check(_LIB.sim_get_object_id(
            self._world._sim._handle, self._world.handle, self.handle,
            None, 0, ctypes.byref(required)))
        buffer = ctypes.create_string_buffer(required.value)
        _check(_LIB.sim_get_object_id(
            self._world._sim._handle, self._world.handle, self.handle,
            buffer, len(buffer), ctypes.byref(required)))
        return buffer.value.decode("ascii", errors="strict")

    @property
    def transform(self) -> Transform:
        value = _Transform()
        _check(_LIB.sim_get_transform(self._world._sim._handle, self._world.handle,
                                     self.handle, ctypes.byref(value)))
        return Transform(tuple(value.translation), tuple(value.rotation_wxyz), tuple(value.scale))

    @transform.setter
    def transform(self, value: Transform) -> None:
        native = value._native()
        _check(_LIB.sim_set_transform(self._world._sim._handle, self._world.handle,
                                     self.handle, ctypes.byref(native)))

    def destroy(self) -> None:
        _check(_LIB.sim_destroy_entity(
            self._world._sim._handle, self._world.handle, self.handle))

    def set_parent(self, parent: Entity | None, *, preserve_world: bool = True) -> None:
        _check(_LIB.sim_set_parent(
            self._world._sim._handle, self._world.handle, self.handle,
            parent.handle if parent else 0, int(preserve_world)))

    def set_name(self, name: str) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("entity name must not be empty")
        _check(_LIB.sim_set_entity_name(
            self._world._sim._handle, self._world.handle, self.handle,
            normalized.encode("utf-8")))

    def set_visual(self, *, visible: bool = True, min_lod: int = 0,
                   max_lod: int = 7) -> None:
        if not 0 <= min_lod <= max_lod <= 7:
            raise ValueError("LOD range must satisfy 0 <= min_lod <= max_lod <= 7")
        _check(_LIB.sim_set_visual_properties(
            self._world._sim._handle, self._world.handle, self.handle,
            int(visible), min_lod, max_lod))

    def set_light(self, *, type: str = "point", color: Iterable[float] = (1.0, 1.0, 1.0),
                  intensity: float = 1.0, range: float = 10.0, decay: float = 2.0,
                  inner_cone_radians: float = 0.35, outer_cone_radians: float = 0.7,
                  angular_diameter_radians: float = 0.0, enabled: bool = True) -> None:
        types = {"point": 0, "spot": 1, "directional": 2, "parallel": 2, "sky": 2,
                 "ambient": 3}
        normalized = type.strip().lower()
        if normalized not in types:
            raise ValueError("light type must be point, spot, directional, or ambient")
        props = _LightProperties(types[normalized], tuple(float(v) for v in color), intensity, range, decay,
                                 inner_cone_radians, outer_cone_radians, angular_diameter_radians, int(enabled))
        _check(_LIB.sim_set_light_properties(self._world._sim._handle, self._world.handle, self.handle, ctypes.byref(props)))

    @property
    def ifps_sphere_count(self) -> int:
        """Configured IFPS leaf count; zero selects the backend default."""
        value = ctypes.c_uint32()
        _check(_LIB.sim_get_ifps_sphere_count(
            self._world._sim._handle, self._world.handle, self.handle, ctypes.byref(value)))
        return value.value

    @ifps_sphere_count.setter
    def ifps_sphere_count(self, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000:
            raise ValueError("IFPS sphere count must be an integer between 0 and 1,000,000")
        _check(_LIB.sim_set_ifps_sphere_count(
            self._world._sim._handle, self._world.handle, self.handle, value))

    def apply_wrench(self, force: Iterable[float], torque: Iterable[float] = (0.0, 0.0, 0.0)) -> None:
        force_value = np.ascontiguousarray(tuple(force), dtype=np.float64)
        torque_value = np.ascontiguousarray(tuple(torque), dtype=np.float64)
        if force_value.shape != (3,) or torque_value.shape != (3,):
            raise ValueError("force and torque must each contain three values")
        _check(_LIB.sim_apply_body_wrench(
            self._world._sim._handle, self._world.handle, self.handle,
            force_value.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            torque_value.ctypes.data_as(ctypes.POINTER(ctypes.c_double))))

    def set_rigid_body(
        self, *, enabled: bool = True, dynamic: bool = False, gravity: bool = True,
        mass: float = 1.0, friction: Iterable[float] = (1.0, 0.005, 0.0001),
        restitution: float = 0.0,
    ) -> None:
        values = np.ascontiguousarray(tuple(friction), dtype=np.float64)
        if values.shape != (3,) or np.any(values < 0.0):
            raise ValueError("friction must contain three non-negative values")
        if mass <= 0.0 or not 0.0 <= restitution <= 1.0:
            raise ValueError("mass must be positive and restitution must be between 0 and 1")
        _check(_LIB.sim_set_rigid_body(
            self._world._sim._handle, self._world.handle, self.handle,
            int(enabled), int(dynamic), int(gravity), float(mass),
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), float(restitution)))


class Robot(Entity):
    def __init__(self, world: World, handle: int):
        super().__init__(world, handle)
        self._collision_backend: CuroboCollisionBackend | None = None

    @property
    def joint_count(self) -> int:
        count = ctypes.c_size_t()
        _check(_LIB.sim_robot_joint_count(self._world._sim._handle, self._world.handle,
                                         self.handle, ctypes.byref(count)))
        return count.value

    @property
    def joint_names(self) -> tuple[str, ...]:
        names = []
        for index in range(self.joint_count):
            required = ctypes.c_size_t()
            _check(_LIB.sim_robot_joint_name(
                self._world._sim._handle, self._world.handle, self.handle, index,
                None, 0, ctypes.byref(required)))
            buffer = ctypes.create_string_buffer(required.value)
            _check(_LIB.sim_robot_joint_name(
                self._world._sim._handle, self._world.handle, self.handle, index,
                buffer, len(buffer), ctypes.byref(required)))
            names.append(buffer.value.decode("utf-8", errors="strict"))
        return tuple(names)

    @property
    def links(self) -> tuple[tuple[str, Entity], ...]:
        count = ctypes.c_size_t()
        _check(_LIB.sim_robot_link_count(
            self._world._sim._handle, self._world.handle, self.handle,
            ctypes.byref(count)))
        result: list[tuple[str, Entity]] = []
        for index in range(count.value):
            link = ctypes.c_uint64()
            required = ctypes.c_size_t()
            _check(_LIB.sim_robot_link_info(
                self._world._sim._handle, self._world.handle, self.handle, index,
                ctypes.byref(link), None, 0, ctypes.byref(required)))
            buffer = ctypes.create_string_buffer(required.value)
            _check(_LIB.sim_robot_link_info(
                self._world._sim._handle, self._world.handle, self.handle, index,
                ctypes.byref(link), buffer, len(buffer), ctypes.byref(required)))
            result.append((buffer.value.decode("utf-8", errors="strict"),
                           Entity(self._world, link.value)))
        return tuple(result)

    def get_link(self, name: str) -> Entity:
        matches = [entity for link_name, entity in self.links if link_name == name]
        if not matches:
            raise KeyError(f"robot link {name!r} was not found")
        if len(matches) > 1:
            raise RuntimeError(f"robot link {name!r} is ambiguous")
        return matches[0]

    @property
    def collision_backend(self) -> str:
        return "curobo" if self._collision_backend is not None else "fcl"

    def use_curobo_collision(
        self, robot_config: object, scene_model: object = None, *, device: str = "cuda:0",
    ) -> CuroboCollisionBackend:
        from .curobo_backend import CuroboCollisionBackend

        backend = CuroboCollisionBackend(robot_config, scene_model, device=device)
        backend.validate_platform_joints(self.joint_names)
        self._collision_backend = backend
        return backend

    def use_fcl_collision(self) -> None:
        self._collision_backend = None

    @property
    def joint_state(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        count = self.joint_count
        positions = np.empty(count, dtype=np.float64)
        velocities = np.empty(count, dtype=np.float64)
        efforts = np.empty(count, dtype=np.float64)
        pointer = ctypes.POINTER(ctypes.c_double)
        _check(_LIB.sim_robot_get_joint_state(
            self._world._sim._handle, self._world.handle, self.handle,
            positions.ctypes.data_as(pointer), velocities.ctypes.data_as(pointer),
            efforts.ctypes.data_as(pointer), count))
        return positions, velocities, efforts

    def command_joint_positions(self, targets: Iterable[float], *,
                                gain: float | None = None,
                                velocity_limit: float | None = None,
                                effort_limits: Iterable[float | None] | None = None) -> None:
        """Drive the joints through the controller.

        `gain` and `velocity_limit` override the controller defaults (20.0 and 10.0). A slow,
        heavily damped approach wants a low velocity limit; leave both unset to keep the
        platform defaults.
        `effort_limits` caps servo effort in Nm (revolute) or N (prismatic), one value
        per joint. A None entry restores unrestricted tracking for that joint. Omitting
        the sequence preserves previously configured ceilings across position commands.
        """
        values = np.ascontiguousarray(list(targets), dtype=np.float64)
        log_verbose(f"Robot.command_joint_positions handle={self.handle} joints={values.size} "
                    f"gain={gain} velocity_limit={velocity_limit}")
        pointer = values.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        if effort_limits is not None:
            limits = effort_limit_values(effort_limits, values.size)
            _log_debug("SDK.Robot.command_joint_positions",
                       f"effort caps joint_count={values.size} capped_joint_count={sum(x is not None for x in limits)}")
            caps = np.ascontiguousarray([np.inf if x is None else x for x in limits], dtype=np.float64)
            _check(_LIB.sim_robot_command_joint_positions_with_effort_limits(
                self._world._sim._handle, self._world.handle, self.handle, pointer, values.size,
                20.0 if gain is None else gain, 10.0 if velocity_limit is None else velocity_limit,
                caps.ctypes.data_as(ctypes.POINTER(ctypes.c_double))))
            return
        if gain is None and velocity_limit is None:
            _check(_LIB.sim_robot_command_joint_positions(self._world._sim._handle,
                                                          self._world.handle, self.handle,
                                                          pointer, values.size))
            return
        _check(_LIB.sim_robot_command_joint_positions_tuned(
            self._world._sim._handle, self._world.handle, self.handle, pointer, values.size,
            20.0 if gain is None else gain,
            10.0 if velocity_limit is None else velocity_limit))

    def command_joint_efforts(self, targets: Iterable[float]) -> None:
        values = np.ascontiguousarray(list(targets), dtype=np.float64)
        log_verbose(f"Robot.command_joint_efforts handle={self.handle} joints={values.size}")
        _check(_LIB.sim_robot_command_joint_efforts(
            self._world._sim._handle, self._world.handle, self.handle,
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), values.size))

    def set_joint_effort_limits(self, limits: Iterable[float]) -> None:
        """Cap the torque each joint's position or velocity servo may apply.

        One limit per joint, in `joint_names` order. `math.inf` -- the default -- leaves a
        joint a kinematic servo: it tracks its target whatever the load, which is a fair
        model of a stiff industrial arm. A finite limit makes the servo a torque source
        that contact can stall short of its target, which is what a gripper needs, since
        the useful output there is force on the payload rather than the commanded angle.
        The commanded angle then says which way to squeeze and this says how hard.

        Persists across the position and velocity commands sent afterwards, because it
        describes the actuator rather than any single command. Honoured by a dynamics
        backend; a purely kinematic controller has nothing to stall against and ignores it.
        """
        values = np.ascontiguousarray(list(limits), dtype=np.float64)
        log_verbose(f"Robot.set_joint_effort_limits handle={self.handle} joints={values.size}")
        _check(_LIB.sim_robot_set_joint_effort_limits(
            self._world._sim._handle, self._world.handle, self.handle,
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), values.size))

    def set_joint_positions(self, positions: Iterable[float], *, limits: str = "enforce") -> None:
        """Pose the joints directly, bypassing the controller.

        `limits` picks what happens to a target outside a joint's range: "enforce" raises,
        "clamp" pulls it into range, "ignore" writes it through. Iterative solvers overshoot on
        intermediate steps, so they want "clamp".
        """
        values = np.ascontiguousarray(list(positions), dtype=np.float64)
        _check(_LIB.sim_robot_set_joint_positions(
            self._world._sim._handle, self._world.handle, self.handle,
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), values.size,
            _limit_policy(limits)))

    @property
    def joint_infos(self) -> tuple[JointInfo, ...]:
        """Type, axis and limits for every joint, in joint_names order."""
        names = self.joint_names
        result: list[JointInfo] = []
        for index, name in enumerate(names):
            info = _JointInfo()
            _check(_LIB.sim_robot_joint_info(self._world._sim._handle, self._world.handle,
                                             self.handle, index, ctypes.byref(info)))
            result.append(JointInfo(
                name=name,
                type=_JOINT_TYPE_NAMES[info.type] if 0 <= info.type < len(_JOINT_TYPE_NAMES)
                else "unknown",
                axis=tuple(info.axis), lower_limit=info.lower_limit,
                upper_limit=info.upper_limit, entity=info.entity,
                parent_link=info.parent_link, child_link=info.child_link))
        return tuple(result)

    @property
    def joint_limits(self) -> np.ndarray:
        """Lower and upper bounds as (joint_count, 2). Continuous joints report -inf/+inf."""
        infos = self.joint_infos
        bounds = np.empty((len(infos), 2), dtype=np.float64)
        for index, info in enumerate(infos):
            if info.type == "continuous":
                bounds[index] = (-np.inf, np.inf)
            else:
                bounds[index] = (info.lower_limit, info.upper_limit)
        return bounds

    def set_joint_continuous(self, joint_name: str, enabled: bool = True) -> None:
        _check(_LIB.sim_robot_set_joint_continuous(
            self._world._sim._handle, self._world.handle, self.handle,
            joint_name.encode("utf-8"), int(enabled)))

    def command_joint_velocities(self, targets: Iterable[float]) -> None:
        values = np.ascontiguousarray(list(targets), dtype=np.float64)
        pointer = values.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        _check(_LIB.sim_robot_command_joint_velocities(
            self._world._sim._handle, self._world.handle, self.handle,
            pointer, values.size))

    def configuration_collides(self, positions: Iterable[float]) -> bool:
        values = np.ascontiguousarray(list(positions), dtype=np.float64)
        if values.shape != (self.joint_count,):
            raise ValueError("positions must contain joint_count values")
        if self._collision_backend is not None:
            return self._collision_backend.configuration_collides(values, self.joint_names)
        colliding = ctypes.c_int()
        _check(_LIB.sim_robot_configuration_collision(
            self._world._sim._handle, self._world.handle, self.handle,
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), values.size,
            ctypes.byref(colliding)))
        return bool(colliding.value)

    def configurations_collide(self, positions: Iterable[Iterable[float]]) -> np.ndarray:
        values = np.ascontiguousarray(list(positions), dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.joint_count:
            raise ValueError("positions must have shape (configuration_count, joint_count)")
        if self._collision_backend is not None:
            return self._collision_backend.configurations_collide(values, self.joint_names)
        result = np.empty(values.shape[0], dtype=np.uint8)
        _check(_LIB.sim_robot_configurations_collision(
            self._world._sim._handle, self._world.handle, self.handle,
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), values.shape[0],
            values.shape[1], result.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))))
        return result.astype(np.bool_)

    def _link_handle(self, link: Entity | str) -> int:
        return self.get_link(link).handle if isinstance(link, str) else link.handle

    def kinematics(self, positions: Iterable[Iterable[float]] | Iterable[float],
                   links: Iterable[Entity | str], *, jacobian_link: Entity | str | None = None,
                   limits: str = "clamp") -> KinematicsBatch:
        """Forward kinematics, and optionally a Jacobian, at joint configurations of your choosing.

        Evaluated on a private copy of the world, so the running simulation is left alone. Pass a
        batch of configurations rather than looping: the copy is made once per call, which is the
        expensive part. `positions` may be a single configuration or a sequence of them; the
        result is always indexed [configuration][link]. Links accept either an Entity or a link
        name. The Jacobian comes back as (configuration_count, 6, joint_count), rows ordered
        linear xyz then angular xyz.
        """
        values = np.ascontiguousarray(list(positions), dtype=np.float64)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.ndim != 2 or values.shape[1] != self.joint_count:
            raise ValueError("positions must have shape (configuration_count, joint_count)")
        count, joints = values.shape
        handles = [self._link_handle(link) for link in links]
        link_buffer = (ctypes.c_uint64 * len(handles))(*handles)
        transforms = (_Transform * (count * len(handles)))()
        wants_jacobian = jacobian_link is not None
        jacobians = np.empty((count, joints, 6) if wants_jacobian else 0, dtype=np.float64)
        double_ptr = ctypes.POINTER(ctypes.c_double)
        _check(_LIB.sim_robot_kinematics_batch(
            self._world._sim._handle, self._world.handle, self.handle,
            values.ctypes.data_as(double_ptr), count, joints,
            link_buffer, len(handles),
            self._link_handle(jacobian_link) if wants_jacobian else 0,
            _limit_policy(limits), transforms,
            jacobians.ctypes.data_as(double_ptr) if wants_jacobian else None))
        poses = tuple(
            tuple(_transform(transforms[index * len(handles) + link])
                  for link in range(len(handles)))
            for index in range(count))
        # Native layout is [configuration][joint][component]; a Jacobian is conventionally
        # indexed [component][joint], so swap the last two axes.
        return KinematicsBatch(poses,
                               np.ascontiguousarray(jacobians.transpose(0, 2, 1))
                               if wants_jacobian else None)

    def configuration_contacts(self, positions: Iterable[float], *,
                               distance_threshold: float = 0.0,
                               limits: str = "clamp") -> tuple[ContactPair, ...]:
        """Every colliding pair at one configuration, reported individually.

        Unlike configuration_collides, which collapses everything into one bool, this reports
        which colliders touch what. Adjacent links of a loaded robot always overlap, so a bare
        bool is always true and the caller has to filter -- take the pairs at the robot's rest
        pose as the baseline and treat anything new as a real collision.

        With `distance_threshold` above 0.0, near-miss pairs within that separation are reported
        too, carrying their `distance`. Requires the FCL collision backend.
        """
        values = np.ascontiguousarray(list(positions), dtype=np.float64)
        if values.shape != (self.joint_count,):
            raise ValueError("positions must contain joint_count values")
        pointer = values.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        policy = _limit_policy(limits)
        required = ctypes.c_size_t()
        capacity = 128
        while True:
            pairs = (_ContactPair * capacity)()
            _check(_LIB.sim_robot_configuration_contacts(
                self._world._sim._handle, self._world.handle, self.handle, pointer, values.size,
                distance_threshold, policy, pairs, capacity, ctypes.byref(required)))
            if required.value <= capacity:
                break
            # Guessed low. The query is not cheap, so grow generously rather than by one.
            capacity = required.value * 2
        return tuple(ContactPair(pair.robot_collider, pair.other, pair.distance,
                                 bool(pair.colliding))
                     for pair in pairs[:required.value])


class LiDAR(Entity):
    def get_points(self) -> tuple[np.ndarray, np.ndarray]:
        count = ctypes.c_size_t()
        _check(_LIB.sim_lidar_point_count(self._world._sim._handle, self._world.handle,
                                         self.handle, ctypes.byref(count)))
        points = np.empty((count.value, 3), dtype=np.float32)
        distances = np.empty(count.value, dtype=np.float32)
        float_pointer = ctypes.POINTER(ctypes.c_float)
        _check(_LIB.sim_lidar_get_points(
            self._world._sim._handle, self._world.handle, self.handle,
            points.ctypes.data_as(float_pointer), distances.ctypes.data_as(float_pointer),
            count.value))
        return points, distances


class Camera(Entity):
    def configure(
        self, *, frame_id: str = "camera", update_rate_hz: float = 30.0,
        width: int = 640, height: int = 480,
        vertical_fov_radians: float = np.pi / 3.0,
        near_plane: float = 0.01, far_plane: float = 1000.0,
        output: str = "rgbd",
    ) -> None:
        normalized = output.strip().lower()
        if normalized not in {"rgb", "rgbd"}:
            raise ValueError("camera output must be 'rgb' or 'rgbd'")
        _check(_LIB.sim_set_camera_properties(
            self._world._sim._handle, self._world.handle, self.handle,
            frame_id.encode("utf-8"), update_rate_hz, width, height,
            vertical_fov_radians, near_plane, far_plane, int(normalized == "rgbd")))

    @property
    def dimensions(self) -> tuple[int, int]:
        width = ctypes.c_uint32()
        height = ctypes.c_uint32()
        _check(_LIB.sim_camera_get_dimensions(
            self._world._sim._handle, self._world.handle, self.handle,
            ctypes.byref(width), ctypes.byref(height)))
        return width.value, height.value

    @property
    def intrinsics(self) -> np.ndarray:
        fx = ctypes.c_double()
        fy = ctypes.c_double()
        cx = ctypes.c_double()
        cy = ctypes.c_double()
        _check(_LIB.sim_camera_get_intrinsics(
            self._world._sim._handle, self._world.handle, self.handle,
            ctypes.byref(fx), ctypes.byref(fy), ctypes.byref(cx), ctypes.byref(cy)))
        return np.array([[fx.value, 0.0, cx.value],
                         [0.0, fy.value, cy.value],
                         [0.0, 0.0, 1.0]], dtype=np.float64)

    @property
    def rgb(self) -> np.ndarray:
        width, height = self.dimensions
        frame = np.empty((height, width, 3), dtype=np.uint8)
        _check(_LIB.sim_camera_get_rgb(
            self._world._sim._handle, self._world.handle, self.handle,
            frame.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), frame.size))
        return frame

    @property
    def depth(self) -> np.ndarray:
        width, height = self.dimensions
        frame = np.empty((height, width), dtype=np.float32)
        _check(_LIB.sim_camera_get_depth(
            self._world._sim._handle, self._world.handle, self.handle,
            frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), frame.size))
        return frame


class World:
    def __init__(self, simulator: Simulator, handle: int):
        self._sim = simulator
        self.handle = handle

    def load_robot(self, path: str | os.PathLike[str]) -> Robot:
        handle = ctypes.c_uint64()
        encoded = os.fsencode(os.fspath(Path(path).resolve()))
        _check(_LIB.sim_load_asset(self._sim._handle, self.handle, encoded, ctypes.byref(handle)))
        robot = Robot(self, handle.value)
        _ = robot.joint_count
        return robot

    def load_asset(self, path: str | os.PathLike[str]) -> Entity:
        handle = ctypes.c_uint64()
        encoded = os.fsencode(os.fspath(Path(path).resolve()))
        _check(_LIB.sim_load_asset(self._sim._handle, self.handle, encoded, ctypes.byref(handle)))
        return Entity(self, handle.value)

    def create_entity(self, name: str, parent: Entity | None = None) -> Entity:
        handle = ctypes.c_uint64()
        _check(_LIB.sim_create_entity(self._sim._handle, self.handle, name.encode("utf-8"),
                                     parent.handle if parent else 0, ctypes.byref(handle)))
        return Entity(self, handle.value)

    def create_primitive(self, name: str, shape: str, size: Iterable[float], *,
                         ifps_sphere_count: int = 0) -> Entity:
        shapes = {"box": 0, "sphere": 1, "capsule": 2, "cylinder": 3}
        normalized = shape.strip().lower()
        if normalized not in shapes:
            raise ValueError(f"primitive shape must be one of {sorted(shapes)}")
        if (not isinstance(ifps_sphere_count, int) or isinstance(ifps_sphere_count, bool)
                or not 0 <= ifps_sphere_count <= 1_000_000):
            raise ValueError("IFPS sphere count must be an integer between 0 and 1,000,000")
        values = np.ascontiguousarray(tuple(size), dtype=np.float64)
        if values.shape != (3,) or np.any(values <= 0.0):
            raise ValueError("size must contain three positive values")
        handle = ctypes.c_uint64()
        _check(_LIB.sim_create_primitive(
            self._sim._handle, self.handle, name.encode("utf-8"), shapes[normalized],
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), ctypes.byref(handle)))
        entity = Entity(self, handle.value)
        if ifps_sphere_count != 0:
            entity.ifps_sphere_count = ifps_sphere_count
        return entity

    def create_light(self, name: str = "Point Light", type: str = "point",
                     parent: Entity | None = None, **kwargs: Any) -> Entity:
        types = {"point": 0, "spot": 1, "directional": 2, "parallel": 2, "sky": 2,
                 "ambient": 3}
        normalized = type.strip().lower()
        if normalized not in types:
            raise ValueError("light type must be point, spot, directional, or ambient")
        props = _LightProperties(types[normalized], tuple(kwargs.get("color", (1.0, 1.0, 1.0))),
                                 kwargs.get("intensity", 1.0), kwargs.get("range", 10.0), kwargs.get("decay", 2.0),
                                 kwargs.get("inner_cone_radians", 0.35), kwargs.get("outer_cone_radians", 0.7),
                                 kwargs.get("angular_diameter_radians", 0.0), int(kwargs.get("enabled", True)))
        handle = ctypes.c_uint64()
        _check(_LIB.sim_create_light(self._sim._handle, self.handle, name.encode("utf-8"), types[normalized],
                                    parent.handle if parent else 0, ctypes.byref(props), ctypes.byref(handle)))
        return Entity(self, handle.value)

    def entity(self, handle: int) -> Entity:
        return Entity(self, handle)

    def find_entity(self, object_id: str) -> Entity:
        handle = ctypes.c_uint64()
        _check(_LIB.sim_find_entity_by_object_id(
            self._sim._handle, self.handle, object_id.encode("ascii"), ctypes.byref(handle)))
        return Entity(self, handle.value)

    def editor_entities(self) -> tuple[EditorEntityInfo, ...]:
        count = ctypes.c_size_t()
        _check(_LIB.sim_editor_entity_count(
            self._sim._handle, self.handle, ctypes.byref(count)))
        names = ("visual", "collider", "rigid_body", "robot", "joint", "sensor", "camera", "light")
        result: list[EditorEntityInfo] = []
        for index in range(count.value):
            value = _EditorEntityInfo()
            _check(_LIB.sim_editor_entity_info(
                self._sim._handle, self.handle, index, ctypes.byref(value)))
            flags = frozenset(name for bit, name in enumerate(names)
                              if value.component_flags & (1 << bit))
            transform = lambda item: Transform(
                tuple(item.translation), tuple(item.rotation_wxyz), tuple(item.scale))
            visual = None
            if "visual" in flags:
                visual = {"visible": bool(value.visual_visible), "min_lod": value.min_lod,
                          "max_lod": value.max_lod, "shape": value.visual_shape,
                          "size": tuple(value.visual_size), "color": tuple(value.visual_color),
                          "resource": value.visual_resource_utf8.decode("utf-8", errors="replace")}
            collider = None
            if "collider" in flags:
                collider = {"shape": value.collider_shape, "size": tuple(value.collider_size),
                            "resource": value.collider_resource_utf8.decode(
                                "utf-8", errors="replace"),
                            "trigger": bool(value.collider_trigger),
                            "friction": tuple(value.friction),
                            "restitution": value.restitution,
                            "ifps_sphere_count": value.ifps_sphere_count}
            rigid_body = None
            if "rigid_body" in flags:
                rigid_body = {"dynamic": bool(value.body_dynamic),
                              "gravity": bool(value.gravity_enabled), "mass": value.mass}
            sensor = None
            if "sensor" in flags:
                sensor = {"frame_id": value.sensor_frame_utf8.decode("utf-8", errors="replace"),
                          "update_rate_hz": value.sensor_rate_hz}
            camera = None
            if "camera" in flags:
                camera = {"width": value.camera_width, "height": value.camera_height,
                          "vertical_fov_radians": value.camera_vertical_fov_radians,
                          "near_plane": value.camera_near_plane,
                          "far_plane": value.camera_far_plane,
                          "output": "rgbd" if value.camera_rgbd_output else "rgb"}
            light = None
            if "light" in flags:
                item = value.light
                light = {"type": ("point", "spot", "directional", "ambient")[item.type], "color": tuple(item.color_rgb),
                         "intensity": item.intensity, "range": item.range, "decay": item.decay,
                         "inner_cone_radians": item.inner_cone_radians, "outer_cone_radians": item.outer_cone_radians,
                         "angular_diameter_radians": item.angular_diameter_radians, "enabled": bool(item.enabled)}
            result.append(EditorEntityInfo(
                entity=value.entity, parent=value.parent or None, flags=flags,
                name=value.name_utf8.decode("utf-8", errors="replace"),
                local=transform(value.local_transform), world=transform(value.world_transform),
                visual=visual, collider=collider, rigid_body=rigid_body,
                sensor=sensor, camera=camera, light=light))
        return tuple(result)

    def check_collision(self, first: Entity, second: Entity) -> bool:
        colliding = ctypes.c_int()
        _check(_LIB.sim_collision_check(self._sim._handle, self.handle, first.handle,
                                        second.handle, ctypes.byref(colliding)))
        return bool(colliding.value)

    def distance(self, first: Entity, second: Entity) -> float:
        value = ctypes.c_double()
        _check(_LIB.sim_collision_distance(self._sim._handle, self.handle, first.handle,
                                           second.handle, ctypes.byref(value)))
        return value.value

    def scene_collisions(self, entities: Iterable[Entity]) -> tuple[np.ndarray, float]:
        items = list(entities)
        handles = (ctypes.c_uint64 * len(items))(*(item.handle for item in items))
        result = np.empty(len(items), dtype=np.uint8)
        elapsed = ctypes.c_double()
        _check(_LIB.sim_scene_collision_batch(
            self._sim._handle, self.handle, handles, len(items),
            result.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), ctypes.byref(elapsed)))
        return result.astype(np.bool_), elapsed.value

    def set_transforms(self, entities: Iterable[Entity], transforms: Iterable[Transform]) -> None:
        entity_list = list(entities)
        transform_list = list(transforms)
        if len(entity_list) != len(transform_list):
            raise ValueError("entity and transform counts must match")
        handles = (ctypes.c_uint64 * len(entity_list))(*(item.handle for item in entity_list))
        values = (_Transform * len(transform_list))(*(item._native() for item in transform_list))
        _check(_LIB.sim_set_transforms(self._sim._handle, self.handle, handles, values,
                                      len(entity_list)))

    def create_lidar(self, parent: Entity, *, frame_id: str = "lidar",
                     update_rate_hz: float = 10.0, horizontal_samples: int = 1024,
                     vertical_channels: int = 16, min_distance: float = 0.05,
                     max_distance: float = 100.0) -> LiDAR:
        handle = ctypes.c_uint64()
        _check(_LIB.sim_create_lidar(
            self._sim._handle, self.handle, parent.handle, frame_id.encode("utf-8"),
            update_rate_hz, horizontal_samples, vertical_channels, min_distance,
            max_distance, ctypes.byref(handle)))
        return LiDAR(self, handle.value)

    def create_camera(self, parent: Entity | None, *, frame_id: str = "camera",
                      update_rate_hz: float = 30.0, width: int = 640,
                      height: int = 480, vertical_fov_radians: float = np.pi / 3.0,
                      near_plane: float = 0.01, far_plane: float = 1000.0) -> Camera:
        handle = ctypes.c_uint64()
        _check(_LIB.sim_create_camera(
            self._sim._handle, self.handle, parent.handle if parent is not None else 0,
            frame_id.encode("utf-8"),
            update_rate_hz, width, height, vertical_fov_radians, near_plane,
            far_plane, ctypes.byref(handle)))
        return Camera(self, handle.value)


class Simulator:
    def __init__(self, *, cache_directory: str | os.PathLike[str] | None = None,
                 physics: str = "none",
                 collision: str = "fcl",
                 plugin_directory: str | os.PathLike[str] | None = None):
        backend = {"none": 0, "mujoco": 1, "bullet": 2, "bullet3": 2}.get(physics)
        if backend is None:
            raise ValueError("physics must be 'none', 'mujoco', 'bullet', or 'bullet3'")
        collision_backend = {"fcl": 0, "ifps-cpu": 1, "ifps_cpu": 1,
                             "ifps-gpu": 2, "ifps_gpu": 2,
                             "bullet3": 3, "bullet": 3,
                             "coal": 4}.get(collision.lower())
        if collision_backend is None:
            raise ValueError(
                "collision must be 'fcl', 'bullet3', 'coal', 'ifps-cpu', or 'ifps-gpu'"
            )
        cache = os.fsencode(os.fspath(cache_directory)) if cache_directory is not None else None
        plugins = (os.fsencode(os.fspath(plugin_directory))
                   if plugin_directory is not None else None)
        config = _Config(cache, backend, collision_backend)
        self._handle = ctypes.c_void_p()
        _check(_LIB.sim_create_with_plugins(
            ctypes.byref(config), plugins, ctypes.byref(self._handle)))

    def close(self) -> None:
        if self._handle:
            _LIB.sim_destroy(self._handle)
            self._handle = ctypes.c_void_p()

    def __enter__(self) -> Simulator:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_world(self, *, fixed_time_step: float = 0.002, seed: int = 0) -> World:
        log_verbose(f"Simulator.create_world fixed_time_step={fixed_time_step} seed={seed}")
        handle = ctypes.c_uint64()
        _check(_LIB.sim_create_world(self._handle, fixed_time_step, seed, ctypes.byref(handle)))
        return World(self, handle.value)

    def step(self, world: World, count: int = 1) -> None:
        log_verbose(f"Simulator.step world={world.handle} ticks={count}")
        _check(_LIB.sim_step(self._handle, world.handle, count))

    def step_unpaced(self, world: World, count: int = 1) -> None:
        """Advance physics without real-time-factor wall-clock pacing."""
        log_verbose(f"Simulator.step_unpaced world={world.handle} ticks={count}")
        _check(_LIB.sim_step_unpaced(self._handle, world.handle, count))

    def physics_contacts(self, world: World) -> tuple[dict[str, Any], ...]:
        """Current MuJoCo contact constraints, with signed distance (m) and force (N)."""
        capacity = 128
        required = ctypes.c_size_t()
        while True:
            contacts = (_PhysicsContact * capacity)()
            _check(_LIB.sim_physics_contacts(self._handle, world.handle, contacts,
                                             capacity, ctypes.byref(required)))
            if required.value <= capacity:
                return tuple({"first": c.first, "second": c.second,
                              "distance": c.distance, "normal_force": c.normal_force}
                             for c in contacts[:required.value])
            capacity = required.value

    def step_profiled(self, world: World, count: int = 1) -> float:
        elapsed = ctypes.c_double()
        _check(_LIB.sim_step_profiled(self._handle, world.handle, count,
                                     ctypes.byref(elapsed)))
        return elapsed.value

    def start(self, world: World) -> None:
        _check(_LIB.sim_start(self._handle, world.handle))

    def pause(self, world: World) -> None:
        _check(_LIB.sim_pause(self._handle, world.handle))

    def status(self, world: World) -> str:
        value = ctypes.c_int()
        _check(_LIB.sim_get_status(self._handle, world.handle, ctypes.byref(value)))
        return {0: "stopped", 1: "running", 2: "paused"}[value.value]

    def poll_event(self, world: World) -> Event | None:
        value = _Event()
        available = ctypes.c_int()
        _check(_LIB.sim_poll_event(self._handle, world.handle, ctypes.byref(value),
                                  ctypes.byref(available)))
        if not available.value:
            return None
        return Event(value.type_utf8.decode("utf-8", errors="replace"), value.world,
                     value.entity, value.simulation_time,
                     value.detail_utf8.decode("utf-8", errors="replace"))

    def time(self, world: World) -> float:
        value = ctypes.c_double()
        _check(_LIB.sim_get_time(self._handle, world.handle, ctypes.byref(value)))
        return value.value

    def scene_snapshot(self, world: World) -> SceneSnapshot:
        required = ctypes.c_size_t()
        _check(_LIB.sim_scene_snapshot_json_size(
            self._handle, world.handle, ctypes.byref(required)))
        for _ in range(3):
            buffer = ctypes.create_string_buffer(required.value)
            actual = ctypes.c_size_t()
            result = _LIB.sim_scene_snapshot_json(
                self._handle, world.handle, buffer, len(buffer), ctypes.byref(actual))
            if result == 0:
                payload = json.loads(buffer.value.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("scene snapshot payload must be an object")
                return SceneSnapshot.from_dict(payload)
            if actual.value > len(buffer):
                required = actual
                continue
            _check(result)
        raise RuntimeError("scene snapshot changed too quickly to read consistently")

    def scene_transforms(self, world: World) -> dict[str, Any]:
        """Return the compact scene state used by Agent timeline frames.

        The native payload retains snapshot header and entity transform identity while
        omitting static visual, collider, rigid-body, sensor, camera, and light fields.
        """
        required = ctypes.c_size_t()
        _check(_LIB.sim_scene_transforms_json_size(
            self._handle, world.handle, ctypes.byref(required)))
        for _ in range(3):
            buffer = ctypes.create_string_buffer(required.value)
            actual = ctypes.c_size_t()
            result = _LIB.sim_scene_transforms_json(
                self._handle, world.handle, buffer, len(buffer), ctypes.byref(actual))
            if result == 0:
                payload = json.loads(buffer.value.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("scene transforms payload must be an object")
                return payload
            if actual.value > len(buffer):
                required = actual
                continue
            _check(result)
        raise RuntimeError("scene transforms changed too quickly to read consistently")

    def scene_metadata(self, world: World) -> tuple[int, int]:
        """Return (scene_generation, revision) without serializing the scene."""
        generation = ctypes.c_uint64()
        revision = ctypes.c_uint64()
        _check(_LIB.sim_scene_metadata(self._handle, world.handle,
                                       ctypes.byref(generation), ctypes.byref(revision)))
        return generation.value, revision.value

    def save_state(self, world: World) -> State:
        handle = ctypes.c_uint64()
        _check(_LIB.sim_save_state(self._handle, world.handle, ctypes.byref(handle)))
        return State(handle.value)

    def restore_state(self, world: World, state: State) -> None:
        _check(_LIB.sim_restore_state(self._handle, world.handle, state.handle))

    def save_state_file(self, world: World, path: str | os.PathLike[str]) -> None:
        _check(_LIB.sim_save_state_file(self._handle, world.handle,
                                       os.fsencode(os.fspath(Path(path).resolve()))))

    def restore_state_file(self, world: World, path: str | os.PathLike[str]) -> None:
        _check(_LIB.sim_restore_state_file(self._handle, world.handle,
                                          os.fsencode(os.fspath(Path(path).resolve()))))

    def reset(self, world: World) -> None:
        _check(_LIB.sim_reset(self._handle, world.handle))

    def sync_collision(self, world: World) -> None:
        """Push the current scene transforms into the collision backend.

        Editing entities marks the backends dirty, and a query rebuilds them on demand, so you
        rarely need this. Call it when you have moved things and want the rebuild to happen now
        rather than inside the next timed query.
        """
        _check(_LIB.sim_collision_sync(self._handle, world.handle))

    def sync_physics(self, world: World) -> None:
        """Adopt the scene's current joint and body poses as the physics state.

        Unlike the collision backend, physics is not rebuilt on demand: it integrates
        from the state it holds. Posing the scene from outside -- `set_joint_positions`,
        an IK solution, moving an object -- therefore leaves the two disagreeing, and a
        robot posed that way collides with nothing until this is called. Velocities are
        cleared, because a teleported body has no meaningful history.
        """
        _check(_LIB.sim_physics_sync(self._handle, world.handle))

    def command_joint_positions_batch(self, world: World, robots: Iterable[Robot],
                                      targets: Iterable[Iterable[float]]) -> None:
        robot_list = list(robots)
        values = np.ascontiguousarray(list(targets), dtype=np.float64)
        if values.ndim != 2 or values.shape[0] != len(robot_list):
            raise ValueError("targets must have shape (robot_count, joints_per_robot)")
        handles = (ctypes.c_uint64 * len(robot_list))(*(item.handle for item in robot_list))
        _check(_LIB.sim_robot_command_joint_positions_batch(
            self._handle, world.handle, handles, len(robot_list),
            values.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), values.shape[1]))
