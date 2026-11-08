"""Convert compact skill commands to the visual-test configuration format."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_RUNTIME = {
    "end_effector_site": "robotiq_2f85_pinch",
    "execution_mode": "kinematic",
    "playback_fps": 60.0,
    "playback_speed": 2.5,
    "minimum_playback_duration": 1.0,
}

DEFAULT_VIEWER = {
    "azimuth": 135.0,
    "elevation": -25.0,
    "distance_scale": 1.4,
    "initial_pause": 1.0,
    "wait_for_key_between_steps": False,
    "continue_keys": ["SPACE", "ENTER"],
    "keep_open_after_last_test": True,
}

END_EFFECTOR_DIRECTIONS = {
    "at": [0.0, 0.0, 0.0],
    "front": [1.0, 0.0, 0.0],
    "behind": [-1.0, 0.0, 0.0],
    "left": [0.0, 1.0, 0.0],
    "right": [0.0, -1.0, 0.0],
    "above": [0.0, 0.0, 1.0],
    "below": [0.0, 0.0, -1.0],
}

PoseProvider = Callable[
    [str, Mapping[str, Any]], Mapping[str, Any] | tuple[Sequence[float], Sequence[float]]
]

class CommandConversionError(ValueError):
    """A command or interaction registry cannot be converted safely."""


class SkillCommandConverter:
    """Convert a ``commands`` document using one scene interaction registry.

    Object poses default to ``spatial.reference_pose``.  A ``pose_provider``
    can be supplied for sequential execution so each target is resolved from
    the current scene state instead.
    """

    def __init__(
        self,
        registry: Mapping[str, Any],
        *,
        collision_checker: Callable[[Mapping[str, Any], Mapping[str, Any], str], bool] | None = None,
        pose_provider: PoseProvider | None = None,
    ) -> None:
        self.registry = deepcopy(dict(registry))
        self.objects = self.registry.get("objects")
        if not isinstance(self.objects, dict):
            raise CommandConversionError("interaction registry must contain an objects mapping")
        if not isinstance(self.registry.get("scene"), str):
            raise CommandConversionError("interaction registry must contain a scene path")
        self._target_index = self._build_target_index()
        self._last_pose: dict[str, Any] | None = None
        self.collision_checker = collision_checker
        self.pose_provider = pose_provider

    @classmethod
    def from_file(
        cls,
        registry_path: str | Path,
        *,
        collision_checker: Callable[[Mapping[str, Any], Mapping[str, Any], str], bool] | None = None,
        pose_provider: PoseProvider | None = None,
    ) -> "SkillCommandConverter":
        path = Path(registry_path)
        with path.open("r", encoding="utf-8") as stream:
            registry = json.load(stream)
        return cls(registry, collision_checker=collision_checker, pose_provider=pose_provider)

    def convert(self, source: Mapping[str, Any]) -> dict[str, Any]:
        commands = source.get("commands")
        if not isinstance(commands, list):
            raise CommandConversionError("command document must contain a commands list")

        self._last_pose = None
        steps = []
        for index, command in enumerate(commands, start=1):
            if not isinstance(command, Mapping):
                raise CommandConversionError(f"command {index} must be an object")
            step = self.convert_command(command, index)
            if step is None:
                continue
            if isinstance(step, list):
                steps.extend(step)
            else:
                steps.append(step)

        config = {
            "scene": self.registry["scene"],
            "runtime": _deep_merge(DEFAULT_RUNTIME, source.get("runtime", {})),
            "viewer": _deep_merge(DEFAULT_VIEWER, source.get("viewer", {})),
            "request_defaults": _deep_merge(
                self.registry.get("move_defaults", {}), source.get("request_defaults", {})
            ),
            "steps": steps,
        }
        return config

    def reset(self) -> None:
        """Reset state used by relative end-effector commands."""
        self._last_pose = None

    def convert_command(
        self, command: Mapping[str, Any], index: int = 1
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Convert one command using the converter's current runtime state."""
        if not isinstance(command, Mapping):
            raise CommandConversionError(f"command {index} must be an object")
        return self._convert_command(command, index)

    def _convert_command(
        self, command: Mapping[str, Any], index: int
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        skill_name = command.get("skill_name")
        parameters = command.get("parameters", {})
        if not isinstance(skill_name, str) or not skill_name:
            raise CommandConversionError(f"command {index} has no valid skill_name")
        if not isinstance(parameters, Mapping):
            raise CommandConversionError(f"command {index} parameters must be an object")

        if skill_name == "put":
            target = parameters.get('target')
            if isinstance(target, bool) or str(target) not in {str(v) for v in self.registry.get('scene_object_ids', [])}:
                raise CommandConversionError('put requires a scene object destination in demo.run_demo')
            step = self._convert_move(parameters, index)
            if step['request']['target'].get('type') != 'object':
                raise CommandConversionError('put requires an interaction object destination')
            return step
        if skill_name in {"move_to", "pick"}:
            return self._convert_move(parameters, index, pick=skill_name == "pick",
                                       move_to=skill_name == "move_to")
        if skill_name in {"approach", "retreat"}:
            target = parameters.get("target")
            if isinstance(target, bool) or not isinstance(target, (str, int)) or target == "":
                raise CommandConversionError(f"{skill_name} command {index} has no valid target")
            if "target_pose" in parameters:
                raise CommandConversionError(f"{skill_name} uses target; target_pose is resolved by the executor")
            return {
                "name": str(parameters.get("name", f"{skill_name} {target}")),
                "type": skill_name,
                "request": deepcopy(dict(parameters)),
            }
        if skill_name in {"grasp", "release", "press", "pull", "push"}:
            return self._convert_action(skill_name, parameters, index)
        raise CommandConversionError(f"command {index} uses unsupported skill {skill_name!r}")

    def _convert_move(
        self, parameters: Mapping[str, Any], index: int, *, pick: bool = False,
        move_to: bool = False
    ) -> dict[str, Any] | list[dict[str, Any]]:
        target_id = parameters.get("target")
        if isinstance(target_id, bool) or not isinstance(target_id, (str, int)) or target_id == "":
            raise CommandConversionError(f"move command {index} has no valid target")

        named_targets = self.registry.get("named_targets", {})
        scene_object_target = str(target_id) in {str(value) for value in self.registry.get('scene_object_ids', [])}
        if pick and (target_id in named_targets or target_id in {"end_effector", "__end_effector__"}):
            raise CommandConversionError('pick requires an interaction object target')
        if target_id in named_targets:
            request = deepcopy(named_targets[target_id].get("request", {}))
            target = request.get("target", {})
            if any(key in parameters for key in ("relation", "distance_m")):
                if target.get("type") != "pose":
                    raise CommandConversionError(
                        f"named target {target_id!r} does not expose a pose for relative motion"
                    )
                request["target"] = self._offset_pose(request["target"], parameters, index)
            default_path_type = "joint" if target.get("type") == "joint" else "joint"
        elif target_id in {"end_effector", "__end_effector__"}:
            request = {"target": self._relative_end_effector_pose(parameters, index)}
            default_path_type = "linear"
        elif scene_object_target:
            if move_to and any(key in parameters for key in ('relation', 'distance_m', 'anchor')):
                request = {"target": self._object_pose(target_id, parameters, index)}
            elif any(key in parameters for key in ('relation', 'distance_m', 'anchor')):
                raise CommandConversionError('Object-ID interaction motion does not support relation/distance_m/anchor offsets')
            else:
                request = {'target': {'type': 'object', 'target_id': target_id}}
                if 'grasp_index' in parameters:
                    request['target']['grasp_index'] = parameters['grasp_index']
            default_path_type = 'joint'
        else:
            object_key, _, _ = self._resolve_target(target_id, index)
            request = {"target": self._object_pose(target_id, parameters, index)}
            default_path_type = "joint"

        path_type = parameters.get("path_type", parameters.get("planning_method", default_path_type))
        if path_type == "auto":
            path_type = default_path_type
        if path_type not in {"joint", "linear", "circular"}:
            raise CommandConversionError(
                f"move command {index} has unsupported path type {path_type!r}"
            )
        request.setdefault("motion", {})["path_type"] = path_type
        if move_to:
            request.setdefault("planning", {}).setdefault("mode", "collision_free")
            request.setdefault("planning", {}).setdefault("allow_replan", True)
        if "phase" in parameters:
            request["motion"]["phase"] = parameters["phase"]
        if "keep_end_effector_orientation" in parameters:
            request.setdefault("constraints", {})["keep_end_effector_orientation"] = bool(
                parameters["keep_end_effector_orientation"]
            )
        if "avoid_collision" in parameters:
            request.setdefault("constraints", {})["avoid_collision"] = bool(
                parameters["avoid_collision"]
            )
        request = _deep_merge(request, parameters.get("request", {}))

        if request.get("target", {}).get("type") == "pose":
            self._last_pose = deepcopy(request["target"])

        # Pick requires a validated pre-point; move always has one target.
        if (
            pick and target_id not in named_targets
            and not scene_object_target
            and target_id not in {"end_effector", "__end_effector__"}
            and self.objects[object_key].get("interactable") is True
            and request.get("target", {}).get("type") == "pose"
        ):
            approach = self._find_approach_pose(request["target"], object_key)
            validated_joints = None
            resolver = getattr(self.collision_checker, "validated_approach_joints", None)
            if approach is not None and callable(resolver):
                search_result = getattr(self, "_last_approach_result", None)
                kwargs = {"search_result": search_result} if search_result is not None else {}
                validated_joints = resolver(approach, request["target"], object_key, **kwargs)
                if validated_joints is None:
                    approach = None
            if approach is not None:
                approach_request = deepcopy(request)
                approach_request["target"] = approach
                approach_request.setdefault("motion", {})["path_type"] = path_type
                if path_type == "joint" and validated_joints is not None:
                    approach_request["target"] = {
                        "type": "joint", "joint_positions": validated_joints,
                    }
                final_request = deepcopy(request)
                final_request.setdefault("motion", {})["path_type"] = "linear"
                final_request.setdefault("motion", {})["phase"] = "contact"
                final_request["motion"]["path_constraint"] = "hard"
                final_request.setdefault("planning", {}).update(mode="direct", allow_replan=False)
                final_request.setdefault("constraints", {})["keep_end_effector_orientation"] = True
                retreat_request = deepcopy(final_request)
                retreat_request["target"] = deepcopy(approach)
                retreat_request["motion"]["phase"] = "retreat"
                self._last_pose = deepcopy(approach)
                return [
                    {
                        "name": str(parameters.get("name", f"move near {target_id}")),
                        "type": "move",
                        "allow_path_change": bool(parameters.get("allow_path_change", True)),
                        "request": approach_request,
                    },
                    {
                        "name": str(parameters.get("contact_name", f"move linearly to {target_id}")),
                        "type": "move",
                        "allow_path_change": False,
                        "request": final_request,
                    },
                    {
                        "name": str(parameters.get("retreat_name", f"return linearly near {target_id}")),
                        "type": "move",
                        "allow_path_change": False,
                        "request": retreat_request,
                    },
                ]

        if pick and not scene_object_target:
            raise CommandConversionError('pick requires a validated interaction pre-point and contact path')
        return {
            "name": str(parameters.get("name", f"move to {target_id}")),
            "type": "move",
            "allow_path_change": bool(parameters.get("allow_path_change", True)),
            "request": request,
        }

    def _find_approach_pose(
        self, target_pose: Mapping[str, Any], object_key: str
    ) -> dict[str, Any] | None:
        """Find a collinear pre-point 10–15 cm behind the tool z-axis."""
        self._last_approach_result = None
        if self.collision_checker is None:
            return None
        target_position = target_pose.get("position", {})
        orientation = target_pose.get("orientation", {})
        if not all(axis in target_position for axis in ("x", "y", "z")):
            return None
        z_axis = _tool_z_axis(orientation)

        def build_candidate(distance_cm: int) -> dict[str, Any]:
            distance = distance_cm / 100.0
            candidate = deepcopy(dict(target_pose))
            candidate["position"] = {
                axis: float(target_position[axis]) - z_axis[i] * distance
                for i, axis in enumerate(("x", "y", "z"))
            }
            return candidate

        # Check the representative far/middle/near points together.  Only if
        # that coarse pass fails do we inspect the remaining centimetre-spaced
        # positions, in small descending batches.  A batch-capable MuJoCo
        # checker evaluates each batch concurrently; simple injected checkers
        # retain the original deterministic sequential behaviour.
        # The farthest point gets a fast-path of its own.  When it succeeds we
        # avoid waiting for speculative nearer checks; after it fails the
        # remaining representative distances are evaluated concurrently.
        coarse_batches = ((15,), (12, 10))
        fine = (14, 13, 11)
        distance_batches = list(coarse_batches)
        distance_batches.extend(
            tuple(fine[start : start + 3]) for start in range(0, len(fine), 3)
        )
        rich_checker = getattr(self.collision_checker, "check_many_candidates", None)
        batch_checker = rich_checker if callable(rich_checker) else getattr(self.collision_checker, "check_many", None)
        for distances in distance_batches:
            candidates = [build_candidate(distance_cm) for distance_cm in distances]
            if callable(batch_checker):
                valid = list(batch_checker(candidates, target_pose, object_key))
                if len(valid) != len(candidates):
                    raise CommandConversionError(
                        "approach collision checker returned an invalid batch result"
                    )
                for candidate, is_valid in zip(candidates, valid):
                    if is_valid:
                        if callable(rich_checker):
                            self._last_approach_result = is_valid
                        return candidate
                continue
            for candidate in candidates:
                if self.collision_checker(candidate, target_pose, object_key):
                    return candidate
        return None

    def _convert_action(
        self, skill_name: str, parameters: Mapping[str, Any], index: int
    ) -> dict[str, Any]:
        target_id = parameters.get("target")
        if not isinstance(target_id, str) or not target_id:
            raise CommandConversionError(f"{skill_name} command {index} has no valid target")
        object_key, _, _ = self._resolve_target(target_id, index)
        obj = self.objects[object_key]
        action_requests = obj.get("action_requests", {})
        if skill_name not in action_requests:
            raise CommandConversionError(
                f"target {target_id!r} does not define action_requests.{skill_name}"
            )
        request = _deep_merge(action_requests[skill_name], parameters.get("request", {}))
        return {
            "name": str(parameters.get("name", f"{skill_name} {target_id}")),
            "type": "pull_push" if skill_name in {"push", "pull"} else skill_name,
            "request": request,
        }

    def _object_pose(
        self, target_id: str, parameters: Mapping[str, Any], index: int
    ) -> dict[str, Any]:
        object_key, indexed_anchor, indexed_relation = self._resolve_target(target_id, index)
        spatial = self.objects[object_key].get("spatial")
        if not isinstance(spatial, Mapping):
            raise CommandConversionError(f"target {target_id!r} has no spatial configuration")

        reference = spatial.get("reference_pose", {})
        position = reference.get("position")
        quaternion = reference.get("quaternion_wxyz", [1.0, 0.0, 0.0, 0.0])
        source_rotation = _quaternion_matrix(quaternion)
        if self.pose_provider is not None:
            live_pose = self.pose_provider(object_key, spatial)
            if isinstance(live_pose, Mapping):
                position = live_pose.get("position")
                quaternion = live_pose.get("quaternion_wxyz", quaternion)
                source_rotation = live_pose.get("rotation_matrix", _quaternion_matrix(quaternion))
            else:
                position, quaternion = live_pose
                source_rotation = _quaternion_matrix(quaternion)
        if not _is_vector(position, 3) or not _is_vector(quaternion, 4):
            raise CommandConversionError(f"target {target_id!r} has an invalid reference_pose")

        # Runtime providers commonly return NumPy arrays or other iterable
        # array-like values. Normalize validated vectors so the pose math can
        # safely use indexing and float conversion for every provider type.
        position = list(position)
        quaternion = list(quaternion)

        anchor_key = str(parameters.get("anchor", indexed_anchor or spatial.get("default_anchor", "target")))
        anchors = spatial.get("anchors", {})
        if anchor_key not in anchors:
            raise CommandConversionError(f"target {target_id!r} has no anchor {anchor_key!r}")
        anchor = anchors[anchor_key]
        local_anchor = anchor.get("local_position", [0.0, 0.0, 0.0])
        relation = str(parameters.get("relation", indexed_relation or "at"))
        direction = spatial.get("directions_local", {}).get(relation)
        if not _is_vector(local_anchor, 3) or not _is_vector(direction, 3):
            raise CommandConversionError(
                f"target {target_id!r} has no valid local direction for relation {relation!r}"
            )
        default_distance = spatial.get("default_distances", {}).get(relation, 0.0)
        distance = float(parameters.get("distance_m", default_distance))
        unit_direction = _normalize(direction)
        local_target = [float(local_anchor[i]) + unit_direction[i] * distance for i in range(3)]
        world_offset = _rotate_by_quaternion(local_target, quaternion)
        world_position = [float(position[i]) + world_offset[i] for i in range(3)]

        orientation = anchor.get("tool_orientation")
        if orientation is None:
            orientation = spatial.get("relation_tool_orientations", {}).get(relation)
        if orientation is None:
            orientation = spatial.get("tool_orientation")
        if not isinstance(orientation, Mapping):
            raise CommandConversionError(f"target {target_id!r} has no tool orientation")
        moved_orientation = spatial.get("moved_tool_orientation")
        if moved_orientation:
            reference_rotation = _quaternion_matrix(
                reference.get("quaternion_wxyz", [1.0, 0.0, 0.0, 0.0])
            )
            threshold = float(spatial.get("moved_orientation_threshold", 0.1))
            if _matrix_distance(source_rotation, reference_rotation) > threshold:
                orientation = moved_orientation
        return {
            "type": "pose",
            "frame": "world",
            "position": dict(zip(("x", "y", "z"), world_position)),
            "orientation": deepcopy(dict(orientation)),
        }

    def _relative_end_effector_pose(
        self, parameters: Mapping[str, Any], index: int
    ) -> dict[str, Any]:
        if self._last_pose is None:
            raise CommandConversionError(
                f"move command {index} targets the end effector before any pose target is known"
            )
        return self._offset_pose(deepcopy(self._last_pose), parameters, index)

    def _offset_pose(
        self, pose: Mapping[str, Any], parameters: Mapping[str, Any], index: int
    ) -> dict[str, Any]:
        """Apply a world-frame relative relation to any resolved pose target."""
        relation = str(parameters.get("relation", "at"))
        direction = END_EFFECTOR_DIRECTIONS.get(relation)
        if direction is None:
            raise CommandConversionError(f"move command {index} has unknown relation {relation!r}")
        try:
            distance = float(parameters.get("distance_m", 0.0))
        except (TypeError, ValueError):
            raise CommandConversionError(
                f"move command {index} has invalid distance_m"
            ) from None
        if not math.isfinite(distance):
            raise CommandConversionError(f"move command {index} has invalid distance_m")
        pose = deepcopy(dict(pose))
        position = pose.get("position")
        if not isinstance(position, Mapping) or not all(axis in position for axis in ("x", "y", "z")):
            raise CommandConversionError(f"move command {index} relative target has no xyz position")
        for axis, delta in zip(("x", "y", "z"), direction):
            pose["position"][axis] = float(pose["position"][axis]) + delta * distance
        return pose

    def _resolve_target(self, target_id: str, index: int) -> tuple[str, str | None, str | None]:
        matches = self._target_index.get(target_id, [])
        if not matches:
            raise CommandConversionError(
                f"command {index} target {target_id!r} is not defined in the interaction registry"
            )
        unique = list(dict.fromkeys(matches))
        if len(unique) != 1:
            raise CommandConversionError(
                f"command {index} target {target_id!r} is ambiguous in the interaction registry"
            )
        return unique[0]

    def _build_target_index(self) -> dict[str, list[tuple[str, str | None, str | None]]]:
        index: dict[str, list[tuple[str, str | None, str | None]]] = {}
        for object_key, obj in self.objects.items():
            base = (object_key, None, None)
            for identifier in {object_key, obj.get("object_id")}:
                if isinstance(identifier, str):
                    index.setdefault(identifier, []).append(base)
            spatial = obj.get("spatial", {})
            for anchor_key, anchor in spatial.get("anchors", {}).items():
                identifier = anchor.get("target_id")
                if isinstance(identifier, str):
                    index.setdefault(identifier, []).append((object_key, anchor_key, "at"))
            for relation, identifier in spatial.get("relation_target_ids", {}).items():
                if isinstance(identifier, str):
                    index.setdefault(identifier, []).append((object_key, None, relation))
        return index




def _deep_merge(base: Mapping[str, Any], override: Any) -> dict[str, Any]:
    result = deepcopy(dict(base))
    if override is None:
        return result
    if not isinstance(override, Mapping):
        raise CommandConversionError("configuration overrides must be objects")
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _is_vector(value: Any, length: int) -> bool:
    if isinstance(value, (str, bytes)):
        return False
    try:
        values = list(value)
    except (TypeError, ValueError):
        return False
    return len(values) == length and all(
        isinstance(item, (int, float)) and math.isfinite(float(item)) for item in values
    )


def _normalize(vector: Sequence[float]) -> list[float]:
    length = math.sqrt(sum(float(value) ** 2 for value in vector))
    if length <= 1e-12:
        return [0.0, 0.0, 0.0]
    return [float(value) / length for value in vector]


def _rotate_by_quaternion(vector: Sequence[float], quaternion_wxyz: Sequence[float]) -> list[float]:
    w, x, y, z = _normalize_quaternion(quaternion_wxyz)
    rotation = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    return [sum(row[i] * float(vector[i]) for i in range(3)) for row in rotation]


def _quaternion_matrix(quaternion: Sequence[float]) -> tuple[tuple[float, float, float], ...]:
    w, x, y, z = _normalize_quaternion(quaternion)
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _matrix_distance(first: Any, second: Any) -> float:
    return math.sqrt(
        sum(
            (float(first[row][column]) - float(second[row][column])) ** 2
            for row in range(3)
            for column in range(3)
        )
    )


def _normalize_quaternion(quaternion: Sequence[float]) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(float(value) ** 2 for value in quaternion))
    if length <= 1e-12:
        raise CommandConversionError("reference_pose quaternion must not be zero")
    return tuple(float(value) / length for value in quaternion)  # type: ignore[return-value]


def _tool_z_axis(orientation: Mapping[str, Any]) -> list[float]:
    """Return the tool's local +Z axis expressed in world coordinates."""
    if orientation.get("representation", "quaternion") == "rpy":
        roll, pitch, yaw = (
            math.radians(float(orientation[key])) for key in ("roll", "pitch", "yaw")
        )
        cr, cp, cy = math.cos(roll), math.cos(pitch), math.cos(yaw)
        sr, sp, sy = math.sin(roll), math.sin(pitch), math.sin(yaw)
        return [cy * sp * cr + sy * sr, sy * sp * cr - cy * sr, cp * cr]
    quaternion = _normalize_quaternion(
        [orientation.get(key, 0.0) for key in ("w", "x", "y", "z")]
    )
    return _rotate_by_quaternion([0.0, 0.0, 1.0], quaternion)





