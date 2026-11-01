"""Collision checking through the FCL backend shipped with SimPlatform.

This adapter deliberately imports the SDK from the SimPlatform checkout supplied
with this project.  It does not use ``python-fcl`` or MuJoCo for planning.
The native SimPlatform library is loaded lazily by the SDK and must be built with
``SIM_ENABLE_FCL=ON``; its path can be supplied with ``SIM_PLATFORM_LIBRARY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import types
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIMPLATFORM_PYTHON = PROJECT_ROOT / "libraries" / "simplatform" / "source" / "SimPlatform-main" / "python"
DEFAULT_SCENE = PROJECT_ROOT / "assets" / "scenes" / "fcl_scene.json"
DEFAULT_ROBOT_URDF = PROJECT_ROOT / "assets" / "models" / "robots" / "1" / "ur5e_arm.urdf"


@dataclass(frozen=True)
class CollisionResult:
    in_collision: bool
    minimum_distance: float
    contacts: tuple[dict[str, object], ...] = ()


def _load_sdk():
    """Load the SDK from the supplied SimPlatform archive, without importing MuJoCo."""
    configured = os.environ.get("SIMPLATFORM_PYTHON_ROOT")
    root = Path(configured).expanduser() if configured else DEFAULT_SIMPLATFORM_PYTHON
    if not root.is_dir():
        raise RuntimeError(
            "SimPlatform Python SDK was not found. Set SIMPLATFORM_PYTHON_ROOT "
            "to the archive's python directory."
        )
    value = str(root.resolve())
    if value not in sys.path:
        sys.path.insert(0, value)
    try:
        from sim_platform.sdk import Simulator, Transform
    except ModuleNotFoundError as exc:
        if exc.name != "bq":
            raise RuntimeError(
                "Unable to load the SimPlatform FCL SDK. Build its C API with "
                "SIM_ENABLE_FCL=ON and set SIM_PLATFORM_LIBRARY to sim_platform_c.dll."
            ) from exc
        # The archive declares bqlog as a packaging dependency, but source-only
        # checkouts do not contain the wheel. The SDK uses it for diagnostics;
        # keep the collision adapter usable with a no-op logger until the native
        # SimPlatform library is loaded.
        class _NoopLogger:
            def is_valid(self): return True
            def verbose(self, *args): pass
            def debug(self, *args): pass
            def info(self, *args): pass
            def warning(self, *args): pass
            def error(self, *args): pass
            def fatal(self, *args): pass
        class _NoopLog:
            def create_log(self, *args): return _NoopLogger()
            def force_flush_all_logs(self): pass
        bq = types.ModuleType("bq")
        bq_log = types.ModuleType("bq.log")
        _noop = _NoopLog()
        bq_log.log = _noop
        bq_defs = types.ModuleType("bq.defs")
        bq_level = types.ModuleType("bq.defs.log_level")
        bq_level.log_level = types.SimpleNamespace(verbose=0, debug=1, info=2, warning=3, error=4, fatal=5)
        sys.modules.update({"bq": bq, "bq.log": bq_log, "bq.defs": bq_defs,
                            "bq.defs.log_level": bq_level})
        try:
            from sim_platform.sdk import Simulator, Transform
        except Exception as retry_exc:
            raise RuntimeError(
                "Unable to load the SimPlatform FCL SDK. Build its C API with "
                "SIM_ENABLE_FCL=ON and set SIM_PLATFORM_LIBRARY to sim_platform_c.dll."
            ) from retry_exc
    except Exception as exc:  # the SDK also validates its native library at import time
        raise RuntimeError(
            "Unable to load the SimPlatform FCL SDK. Build its C API with "
            "SIM_ENABLE_FCL=ON and set SIM_PLATFORM_LIBRARY to sim_platform_c.dll."
        ) from exc
    return Simulator, Transform


def _component(obj: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for item in obj.get("components", []):
        if isinstance(item, Mapping) and item.get("component_type") == name:
            value = item.get("properties", {})
            return value if isinstance(value, Mapping) else {}
    return None


def _asset_path(value: Any, scene_file: Path, asset_root: Path | None = None) -> Path | None:
    if not isinstance(value, str) or not value or value.startswith("$LIBRARY_SERVER"):
        return None
    path = Path(value).expanduser()
    if asset_root is not None:
        candidate = (asset_root / path).resolve()
        return candidate if candidate.is_file() else None
    if path.is_absolute() and path.is_file():
        return path.resolve()
    candidates = (
        scene_file.parent / path,
        PROJECT_ROOT / path,
        PROJECT_ROOT / "assets" / "models" / path,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _resolve_object_asset(obj: Mapping[str, Any], scene_file: Path, asset_root: Path | None = None) -> Path | None:
    """Resolve an explicit asset reference; visual fields alone are not colliders."""
    for component_name in ("Collider", "AssetRef", "RobotModelRef", "MetadataRef", "MeshRenderer"):
        properties = _component(obj, component_name) or {}
        for key in ("collision_mesh", "mesh", "asset", "asset_path", "urdf", "path"):
            path = _asset_path(properties.get(key), scene_file, asset_root)
            if path is not None:
                return path
    return None


def _transform_for(obj: Mapping[str, Any], Transform):
    props = _component(obj, "Transform") or {}
    position = tuple(float(x) for x in props.get("position", (0.0, 0.0, 0.0)))
    xyzw = tuple(float(x) for x in props.get("quaternion_xyzw", (0.0, 0.0, 0.0, 1.0)))
    if len(xyzw) != 4:
        raise ValueError(f"object {obj.get('object_name_in_scene', '<unnamed>')} has invalid quaternion")
    scale = tuple(float(x) for x in props.get("scale", (1.0, 1.0, 1.0)))
    return Transform(position, (xyzw[3], xyzw[0], xyzw[1], xyzw[2]), scale)


class SimPlatformFCLCollisionChecker:
    """FCL checker with the same ``check``/``check_path`` protocol as the old checker."""

    def __init__(
        self,
        runtime: object | None = None,
        *,
        scene_file: str | Path | None = None,
        robot_urdf: str | Path | None = None,
        robot_base_link: str = "base",
        robot_tip_link: str = "attachment_site",
        initial_joints: Sequence[float] | None = None,
        asset_root: str | Path | None = None,
        scene_data: Mapping | None = None,
    ) -> None:
        self.runtime = runtime
        scene_file = scene_file or os.environ.get("FCL_SCENE_FILE", str(DEFAULT_SCENE))
        robot_urdf = robot_urdf or os.environ.get("FCL_ROBOT_URDF", str(DEFAULT_ROBOT_URDF))
        self.scene_file = Path(scene_file).expanduser().resolve()
        self.asset_root = Path(asset_root).expanduser().resolve() if asset_root is not None else None
        self.robot_urdf = Path(robot_urdf).expanduser().resolve()
        if not self.scene_file.is_file():
            raise FileNotFoundError(f"FCL scene file not found: {self.scene_file}")
        if not self.robot_urdf.is_file():
            raise FileNotFoundError(f"FCL robot URDF not found: {self.robot_urdf}")
        Simulator, Transform = _load_sdk()
        self._Transform = Transform
        if scene_data is None:
            with self.scene_file.open("r", encoding="utf-8") as stream:
                self.scene = json.load(stream)
        else:
            from copy import deepcopy
            self.scene = deepcopy(scene_data)
        if not isinstance(self.scene, Mapping) or not isinstance(self.scene.get("objects"), list):
            raise ValueError("FCL scene must contain an objects array")
        robot_object = next((item for item in self.scene["objects"]
                             if isinstance(item, Mapping) and _component(item, "RobotModelRef") is not None), None)
        robot_ref = _component(robot_object, "RobotModelRef") if robot_object is not None else None
        scene_urdf = _asset_path((robot_ref or {}).get("urdf"), self.scene_file, self.asset_root)
        if scene_urdf is not None:
            self.robot_urdf = scene_urdf
        robot_base_link = str((robot_ref or {}).get("base_link", robot_base_link))
        robot_tip_link = str((robot_ref or {}).get("tip_link", robot_tip_link))
        self.robot_base_link = robot_base_link
        self.robot_tip_link = robot_tip_link
        self._allowed_link_pairs = (robot_ref or {}).get('allowed_collision_link_pairs', [])
        self._sim = Simulator(physics="none", collision="fcl")
        self.world = self._sim.create_world()
        self.robot = self.world.load_robot(self.robot_urdf)
        # SimPlatform exposes all URDF joints (including fixed joints) and
        # uses its own joint ordering.  The rest of this project uses the six
        # UR5e actuated joints in ``UR5E_JOINT_NAMES`` order, so keep an
        # explicit adapter map instead of leaking the SDK ordering upward.
        sdk_names = tuple(str(name) for name in self.robot.joint_names)
        project_names = (
            "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
            "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
        )
        missing = [name for name in project_names if name not in sdk_names]
        if missing:
            raise ValueError(f"FCL robot URDF is missing project joints: {missing}")
        self._sdk_joint_names = sdk_names
        self._project_joint_names = project_names
        self._project_to_sdk = np.asarray([sdk_names.index(name) for name in project_names], dtype=int)
        self._fixed_sdk_joints = np.asarray(
            [index for index, name in enumerate(sdk_names) if name not in project_names],
            dtype=int,
        )
        robot_object = next((item for item in self.scene["objects"]
                             if isinstance(item, Mapping) and _component(item, "RobotModelRef") is not None), None)
        self.robot.transform = (_transform_for(robot_object, self._Transform)
                                if robot_object is not None else self._Transform())
        self._entities: dict[str, Any] = {}
        self._geometry_scales: dict[str, tuple[float, ...]] = {}
        self._load_scene_objects()
        self._names = self._entity_names()
        q = np.asarray(initial_joints if initial_joints is not None else self._runtime_joints(), dtype=float)
        if q.shape != (len(self._project_joint_names),):
            raise ValueError(f"FCL robot expects {len(self._project_joint_names)} project joint values")
        # Only structural same/adjacent-link contacts are excluded. Never
        # whitelist an initial robot/environment collision for the whole task.
        self._baseline = self._structural_pairs()
        self.allowed_target_geom_ids: set[Any] | None = None
        self.allowed_target_names: set[str] = set()
        # Temporary, exact contact pairs captured at the beginning of a
        # detach move.  The articulated checker applies the distance rule;
        # this set is only a portable place to keep the policy.
        self._detach_contact_pairs: dict[frozenset[str], float] = {}
        self.robot_geom_ids = set()
        self.baseline_allowed_pairs = set(self._baseline)
        self.cache_enabled = True
        self.native_filter_enabled = True
        self._configuration_cache = OrderedDict()
        self._path_cache = OrderedDict()
        self._cache_stamp = None
        self.cache_stats = {'native_queries': 0, 'configuration_hits': 0, 'path_hits': 0, 'invalidations': 0}

    def _scene_cache_stamp(self):
        # SDK revisions alone do not cover every direct transform setter. Read
        # actual hierarchy/transforms/colliders, including native edits outside
        # set_entity_transform. No rounding of poses or joint angles is allowed.
        stamp = repr(tuple((e.entity, e.parent, e.local, e.world, e.collider)
                           for e in self.world.editor_entities()))
        if stamp != self._cache_stamp:
            self._configuration_cache.clear()
            self._path_cache.clear()
            self._cache_stamp = stamp
            self.cache_stats['invalidations'] += 1
        return stamp

    @staticmethod
    def _remember(cache, key, value, capacity):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > capacity:
            cache.popitem(last=False)

    def _runtime_joints(self) -> np.ndarray:
        if self.runtime is not None and hasattr(self.runtime, "get_joint_positions"):
            return np.asarray(self.runtime.get_joint_positions(), dtype=float)
        return np.zeros(len(self._project_joint_names), dtype=float)

    def _structural_pairs(self):
        links = {int(entity.handle): name for name, entity in self.robot.links}
        entries = {int(item.entity): item for item in self.world.editor_entities()}
        self._collider_links = {}
        for handle, entry in entries.items():
            if entry.collider is None:
                continue
            ancestor = handle
            while ancestor in entries:
                if ancestor in links:
                    self._collider_links[handle] = links[ancestor]
                    break
                ancestor = entries[ancestor].parent
        joints = ET.parse(self.robot_urdf).getroot().findall('joint')
        attachment_links = set()
        for obj in self.scene['objects']:
            if _component(obj, 'GripperModel') is not None:
                source = _component(obj, 'GripperModel') or {}
                path = _asset_path(source.get('urdf'), self.scene_file, self.asset_root)
                attachment_links.update(e.get('name') for e in ET.parse(path).getroot().findall('link'))
        adjacent = {frozenset((j.find('parent').get('link'), j.find('child').get('link'))) for j in joints}
        for pair in self._allowed_link_pairs:
            if len(pair) != 2 or any(name not in set(links.values()) | attachment_links for name in pair):
                raise ValueError(f'Invalid allowed collision link pair: {pair}')
            if any(name in attachment_links for name in pair):
                continue  # Articulated tool pairs are checked by the MuJoCo geometry adapter.
            adjacent.add(frozenset(pair))
        rigid = {name: name for name in links.values()}
        def group(name):
            while rigid[name] != name:
                name = rigid[name]
            return name
        for joint in joints:
            if joint.get('type') == 'fixed':
                parent, child = joint.find('parent').get('link'), joint.find('child').get('link')
                rigid[group(child)] = group(parent)
        return {frozenset((a, b)) for a, la in self._collider_links.items()
                for b, lb in self._collider_links.items()
                if a != b and (group(la) == group(lb) or frozenset((la, lb)) in adjacent)}

    def _to_sdk_joints(self, joints: Sequence[float]) -> np.ndarray:
        values = np.asarray(joints, dtype=float)
        if values.shape != (len(self._project_joint_names),) or not np.all(np.isfinite(values)):
            raise ValueError(f"Collision check requires {len(self._project_joint_names)} project joint values")
        result = np.zeros(self.robot.joint_count, dtype=float)
        result[self._project_to_sdk] = values
        return result

    def _load_scene_objects(self) -> None:
        for obj in self.scene["objects"]:
            if not isinstance(obj, Mapping):
                continue
            name = str(obj.get("object_name_in_scene", obj.get("object_id_in_scene", "object")))
            if _component(obj, "RobotModelRef") is not None:
                continue
            if _component(obj, "RobotAttachment") is not None:
                # Articulated attachment geometry is checked by GraspCollisionChecker.
                continue
            asset = _resolve_object_asset(obj, self.scene_file, self.asset_root)
            collider = _component(obj, "Collider") or {}
            entity = None
            shape = str(collider.get("shape", "")).lower()
            if shape in {"box", "sphere", "capsule", "cylinder"}:
                size = tuple(float(x) for x in collider.get("size", (1.0, 1.0, 1.0)))
                entity = self.world.create_primitive(name, shape, size)
                # SimPlatform stores primitive dimensions in Transform.scale,
                # not in Collider.size. Preserve that scale when assigning pose.
                self._geometry_scales[name] = tuple(entity.transform.scale)
            elif asset is not None:
                if asset.suffix.lower() in {'.obj', '.stl'}:
                    from .mesh_assets import prepare_mesh
                    prepared = prepare_mesh(asset)
                    entity = self.world.create_entity(name)
                    for part in prepared.parts:
                        child = self.world.load_asset(part)
                        child.set_name(name)
                        child.set_parent(entity, preserve_world=False)
                else:
                    entity = self.world.load_asset(asset)
                    entity.set_name(name)
            # Template-only objects (lights/cameras/placeholders) have no collision geometry.
            if entity is not None:
                pose = _transform_for(obj, self._Transform)
                scale = np.asarray(pose.scale) * self._geometry_scales.get(name, (1, 1, 1))
                entity.transform = self._Transform(pose.translation, pose.rotation_wxyz, tuple(scale))
                end_effector = _component(obj, "EndEffectorModelRef") or {}
                if end_effector.get("parent_link"):
                    entity.set_parent(self.robot.get_link(str(end_effector["parent_link"])), preserve_world=False)
                self._entities[name] = entity

    def _entity_names(self) -> dict[int, str]:
        result: dict[int, str] = {}
        entries = {int(item.entity): item for item in self.world.editor_entities()}
        roots = {int(entity.handle): name for name, entity in self._entities.items()}
        for handle, item in entries.items():
            ancestor = handle
            result[handle] = item.name
            while ancestor in entries:
                if ancestor in roots:
                    result[handle] = roots[ancestor]
                    break
                ancestor = entries[ancestor].parent
        return result

    def _pair_keys(self, contacts: Iterable[Any]) -> set[frozenset[int]]:
        return {frozenset((int(pair.robot_collider), int(pair.other))) for pair in contacts}

    def set_allowed_target_names(self, names: Iterable[str]) -> None:
        self.allowed_target_names = {str(name) for name in names}

    def set_entity_transform(self, name: str, *, position: Sequence[float],
                             quaternion_xyzw: Sequence[float] = (0.0, 0.0, 0.0, 1.0),
                             scale: Sequence[float] = (1.0, 1.0, 1.0)) -> None:
        """Update a scene object's pose without consulting MuJoCo."""
        try:
            entity = self._entities[str(name)]
        except KeyError:
            raise KeyError(f"FCL scene object {name!r} was not loaded") from None
        xyzw = tuple(float(value) for value in quaternion_xyzw)
        if len(xyzw) != 4:
            raise ValueError("quaternion_xyzw must contain four values")
        entity.transform = self._Transform(
            tuple(float(value) for value in position),
            (xyzw[3], xyzw[0], xyzw[1], xyzw[2]),
            tuple(np.asarray(scale, dtype=float) * self._geometry_scales.get(str(name), (1, 1, 1))),
        )

    def copy_collision_policy_from(self, other: Any) -> None:
        """Copy portable allowances while retaining this worker's entity handles."""
        self.allowed_target_names = set(getattr(other, "allowed_target_names", set()))
        self.allowed_target_geom_ids = None
        # SimPlatform workers build the same scene independently; raw entity handles
        # from another world are deliberately never copied.
        self.baseline_allowed_pairs = set(self._baseline)

    def _is_allowed(self, pair: Any, allowed: set[Any] | None) -> bool:
        names = {self._names.get(int(pair.robot_collider), ""), self._names.get(int(pair.other), "")}
        targets = self.allowed_target_names | {str(item) for item in (allowed or set())}
        return bool(names & targets)

    def clear_detach_contacts(self) -> None:
        self._detach_contact_pairs.clear()

    def detach_contact_scope(self):
        """Keep captured contacts only for one subsequent motion command."""
        from contextlib import contextmanager
        @contextmanager
        def scope():
            # The command runner clears this after post-execution path
            # verification.  Keeping it through that check is necessary so
            # the accepted initial contact is not reported as a new collision.
            yield
        return scope()

    def check(self, joints: Sequence[float], *, safety_distance: float = 0.0,
              allowed_geom_ids: set[Any] | None = None) -> CollisionResult:
        values = np.asarray(joints, dtype=float)
        if values.shape != (len(self._project_joint_names),):
            raise ValueError(f"Collision check requires a {len(self._project_joint_names)}-value joint vector")
        threshold = max(0.0, float(safety_distance))
        if not np.isfinite(values).all() or not np.isfinite(float(safety_distance)):
            raise ValueError('Collision queries require finite joints and safety distance')
        contacts = []
        minimum = float("inf")
        # Keep zero-margin queries cacheable too.  Native FCL still performs
        # the exact query for a new configuration, but repeated path samples
        # and execution endpoints can reuse the immutable result without
        # crossing the C boundary again.
        stamp = self._scene_cache_stamp() if self.cache_enabled else None
        exclusions = (frozenset(self.baseline_allowed_pairs)
                      if getattr(self, 'native_filter_enabled', True)
                      and getattr(self.robot, 'supports_filtered_contacts', False) else frozenset())
        cache_key = (values.tobytes(), threshold, exclusions)
        pairs = self._configuration_cache.get(cache_key) if stamp is not None else None
        if pairs is None:
            self.cache_stats['native_queries'] += 1
            kwargs = {}
            if exclusions:
                encoded = getattr(self, '_encoded_exclusions', None)
                if encoded is None or encoded[0] != exclusions:
                    encoded = (exclusions, tuple(tuple(pair) for pair in exclusions))
                    self._encoded_exclusions = encoded
                kwargs['excluded_pairs'] = encoded[1]
            pairs = self.robot.configuration_contacts(self._to_sdk_joints(values), distance_threshold=threshold, limits="enforce", **kwargs)
            if stamp is not None:
                if self._scene_cache_stamp() != stamp:
                    raise RuntimeError('Scene changed during collision query')
                self._remember(self._configuration_cache, cache_key, pairs, 4096)
        else:
            self.cache_stats['configuration_hits'] += 1
            self._configuration_cache.move_to_end(cache_key)
        # Native exclusions are part of the cache key. Apply remaining CURRENT
        # policy here; removing an exclusion must force a fresh native query.
        for pair in pairs:
            key = frozenset((int(pair.robot_collider), int(pair.other)))
            if key in self.baseline_allowed_pairs or self._is_allowed(pair, allowed_geom_ids):
                continue
            distance = float(pair.distance)
            minimum = min(minimum, distance)
            if bool(pair.colliding) or distance <= threshold:
                first = self._names.get(int(pair.robot_collider), f"entity_{pair.robot_collider}")
                second = self._names.get(int(pair.other), f"entity_{pair.other}")
                contacts.append({"geom1": first, "geom2": second, "distance": distance,
                                 "self_collision": int(pair.other) in self._robot_handles})
        return CollisionResult(bool(contacts), minimum, tuple(contacts))

    @property
    def _robot_handles(self) -> set[int]:
        return set(self._collider_links)

    def check_path(self, waypoints: Sequence[Sequence[float]], *, resolution: float = 0.05,
                   safety_distance: float = 0.0, allowed_geom_ids: set[Any] | None = None) -> CollisionResult:
        points = np.asarray(waypoints, dtype=float)
        if points.ndim != 2 or points.shape[1] != len(self._project_joint_names) or len(points) < 1:
            raise ValueError("waypoints must have shape (n, project_joint_count)")
        if not np.isfinite(points).all() or not np.isfinite(resolution) or resolution <= 0 or not np.isfinite(safety_distance):
            raise ValueError('Path, resolution and safety distance must be finite; resolution must be positive')
        stamp = self._scene_cache_stamp() if self.cache_enabled else None
        policy = (frozenset(self.baseline_allowed_pairs), frozenset(self.allowed_target_names),
                  frozenset(str(item) for item in (allowed_geom_ids or set())), frozenset(self._names.items()))
        path_key = (points.shape, points.tobytes(), float(resolution), float(safety_distance), policy)
        if stamp is not None and path_key in self._path_cache:
            self.cache_stats['path_hits'] += 1
            self._path_cache.move_to_end(path_key)
            return deepcopy(self._path_cache[path_key])
        minimum = float("inf")
        all_contacts: list[dict[str, object]] = []
        if len(points) == 1:
            return self.check(points[0], safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        for segment, (start, goal) in enumerate(zip(points[:-1], points[1:])):
            count = max(2, int(np.ceil(np.max(np.abs(goal - start)) / max(float(resolution), 1e-9))) + 1)
            alphas = np.linspace(0.0, 1.0, count)
            for index in range(0 if segment == 0 else 1, count):
                # Shared endpoints have already been queried by the previous
                # segment. Use exact endpoints to avoid floating-point drift.
                q = start if index == 0 else goal if index == count - 1 else start + alphas[index] * (goal - start)
                result = self.check(q, safety_distance=safety_distance,
                                    allowed_geom_ids=allowed_geom_ids)
                minimum = min(minimum, result.minimum_distance)
                if result.in_collision:
                    all_contacts.extend(result.contacts)
                    return CollisionResult(True, minimum, tuple(all_contacts))
        result = CollisionResult(False, minimum, ())
        if stamp is not None:
            # A live MuJoCo-backed checker refreshes transforms at every
            # waypoint.  The individual checks already observe the current
            # frame, so a revision change during interpolation is expected
            # and must not be reported as a concurrent mutation.
            live_frame = bool(getattr(self, '_live_backend_bound', False))
            if not live_frame and self._scene_cache_stamp() != stamp:
                raise RuntimeError('Scene changed during path validation')
            self._remember(self._path_cache, path_key, result, 128)
        return result

    def validate_execution_path(self, waypoints, **kwargs):
        """Validate once on the authoritative fine grid.

        The former coarse-then-fine pair traversed the same path twice before
        continuous time preparation traversed it again.  The 1 cm grid is
        already the stricter check, so the coarse pass added work without
        adding a safety guarantee.
        """
        return self.check_path(waypoints, resolution=.01, **kwargs)

    def close(self) -> None:
        if getattr(self, "_sim", None) is not None:
            self._sim.close()
            self._sim = None


def _identity_or_transform(entity: Any, Transform: Any):
    # Keep the robot at the origin unless the scene contains an explicit robot transform.
    return Transform()

