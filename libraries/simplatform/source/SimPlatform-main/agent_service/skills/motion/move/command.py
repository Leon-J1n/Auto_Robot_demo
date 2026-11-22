"""Compiled-command adapter for the Move skill."""

from contextlib import nullcontext
import copy
import json
import logging
import os
import re
from pathlib import Path

import numpy as np

from agent_service.skills.command_registry import (CommandExecutionContext,
                                                   SkillCommandFailure)
from agent_service.runtime.trajectory import segment_markers
from agent_service.scene.model_catalog import (DEFAULT_ROUTE_A_MODEL_CATALOG,
                                                ROUTE_A_MODEL_CATALOG_ENV)
from agent_service.skills.platform_runtime import rotation_mapping, rpy_to_matrix

MoveSkill = None
logger = logging.getLogger(__name__)
STATIC_TARGET_CLEARANCE_M = 0.15
CARRY_CONFIG_PATH = Path(__file__).with_name("carry_config.json")
CARRY_CLEARANCE_CONFIG_PATH = Path(__file__).with_name("carry_clearance_config.json")


def _carry_clearance() -> float:
    """Reload the robot-to-obstacle margin, in metres, for each carried move."""
    try:
        config = json.loads(CARRY_CLEARANCE_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        value = config["safety_distance_m"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("safety_distance_m must be a finite number between 0 and 1")
        return float(value)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SkillCommandFailure({"error_code": "INVALID_CONFIG",
                                   "error_message": f"Invalid move clearance config: {exc}"}) from exc


def _carry_limits() -> tuple[float, float, float]:
    """Reload effective joint-space caps on every held-object command."""
    try:
        config = json.loads(CARRY_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        values = []
        for key in ("velocity_limit_rad_s", "acceleration_limit_rad_s2", "jerk_limit_rad_s3"):
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be a finite positive number")
            values.append(float(value))
        return tuple(values)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SkillCommandFailure({"error_code": "INVALID_CONFIG",
                                   "error_message": f"Invalid move config {CARRY_CONFIG_PATH}: {exc}"}) from exc

_ASSET_ROOTS = (
    Path(__file__).resolve().parents[3] / "objects" / "grasp_objects",
    Path(__file__).resolve().parents[4] / "objects" / "grasp_objects",
    Path("F:/jinliang-agent/demo2/objects/grasp_objects"),
)


def _asset_key(value: str) -> str:
    key = re.sub(r"[^\w]+", "_", str(value).lower()).strip("_")
    # Scene composition appends an instance number (banana_01), while the asset
    # library is keyed by the model name (banana).  Remove only a terminal numeric
    # instance suffix so meaningful digits inside model names remain intact.
    key = re.sub(r"_\d+$", "", key)
    for suffix in ("_body", "_object", "_mesh"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


def _grasp_poses(name: str) -> list[tuple[Path, list[float]]]:
    """Read valid object-relative grasp poses from the installed model library."""
    wanted = _asset_key(name)
    # Scene names may use a catalog alias (rubik_cube_01), not the asset folder
    # name (rubiks_cube). Use the same catalog mapping as scene generation.
    catalog = Path(os.environ.get(ROUTE_A_MODEL_CATALOG_ENV) or DEFAULT_ROUTE_A_MODEL_CATALOG)
    catalog_path = None
    try:
        models = json.loads(catalog.read_text(encoding="utf-8")).get("models", [])
        matches = [model for model in models if model.get("graspPose") and wanted in {
            _asset_key(value) for value in [model.get("name", ""), *model.get("aliases", [])] if value
        }]
        if len(matches) == 1:
            catalog_path = (catalog.parent / matches[0]["graspPose"]).resolve()
    except (OSError, TypeError, ValueError):
        logger.debug("Move grasp catalog unavailable; using installed asset names")
    paths = []
    for root in _ASSET_ROOTS:
        if not root.is_dir():
            continue
        paths.extend(sorted(root.glob("*/grasp_pose.json")))
    if catalog_path is not None:
        paths.insert(0, catalog_path)
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            names = {path.parent.name, (payload.get("object_meta") or {}).get("object_name", "")}
            if path != catalog_path and wanted not in {_asset_key(item) for item in names if item}:
                continue
            poses = []
            for pose in payload.get("poses", []):
                values = [float(item) for item in pose]
                if len(values) == 6 and all(np.isfinite(values)):
                    poses.append((path, values))
            return poses
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return []


def _grasp_pose_requests(runtime, name: str, base_request: dict) -> list[dict]:
    """Expand a movable object target into file-order world grasp-pose requests."""
    poses = _grasp_poses(name)
    object_pose = runtime.object_pose(name) if poses else None
    rotation = np.asarray((object_pose or {}).get("rotation_matrix"), dtype=float)
    if object_pose is None or rotation.shape != (3, 3):
        return [base_request]
    origin = np.asarray(object_pose["position"], dtype=float)
    requests = []
    for index, (source, pose) in enumerate(poses):
        world_position = origin + rotation @ np.asarray(pose[:3], dtype=float)
        world_rotation = rotation @ rpy_to_matrix(*pose[3:])
        request = copy.deepcopy(base_request)
        request["target"] = {"type": "pose", "frame": "world",
                              "position": [float(value) for value in world_position],
                              "orientation": rotation_mapping(world_rotation)}
        request["target_body_name"] = name
        request["constraints"].pop("keep_end_effector_orientation", None)
        request["grasp_pose"] = {"index": index, "source": str(source),
                                  "object_frame": "bbox_center", "local_pose": pose,
                                  "position_unit": "m", "angle_unit": "rad",
                                  "world_target": copy.deepcopy(request["target"])}
        requests.append(request)
    logger.debug("Move grasp-pose expansion", extra={"target": name, "pose_count": len(requests)})
    return requests or [base_request]


def _aggregate_grasp_failures(results: list[dict], target: str) -> dict:
    attempts = []
    for index, result in enumerate(results):
        error = result.get("error") or {}
        attempts.append({"grasp_pose_index": index, "error_code": error.get("error_code"),
                         "error_message": error.get("error_message", result.get("status", "failed")),
                         "grasp_pose": result.get("grasp_pose")})
    last = dict(results[-1]) if results else {"success": False, "status": "failed"}
    last["success"] = False
    last["error"] = {"error_code": "ALL_GRASP_POSES_UNREACHABLE",
                      "error_message": f"All grasp poses for target {target!r} are unreachable.",
                      "failed_stage": "planning", "recoverable": True,
                      "details": {"target": target, "pose_count": len(results), "attempts": attempts}}
    return last


def execute(context: CommandExecutionContext) -> dict:
    if context.skill_name != "move":
        raise ValueError(f"Move adapter cannot execute {context.skill_name!r}")
    runtime = context.services.runtime
    if not hasattr(runtime, "qpos_indices"):
        return context.invoke()
    skill_type = MoveSkill
    if skill_type is None:
        from .skill import MoveSkill as skill_type
    params = context.parameters
    planning_method = params.get("planning_method", params.get("planning_mode", "auto"))
    # Older compiled bundles used `planning_method=linear` for Cartesian macros.
    # `linear` is a path type; keep those bundles executable while new compilation
    # emits the correct `path_type` field.
    if planning_method == "linear":
        planning_method = "auto"
    path_type = params.get("path_type", "auto")
    if params.get("planning_method") == "linear":
        path_type = "linear"
    request = {
        "target": _target(runtime, context.target, params,
                          getattr(context.services, "resolve_target", None)),
        "target_body_name": context.target,
        "motion": {"path_type": path_type,
                   "path_constraint": params.get("path_constraint", "soft"),
                   "phase": params.get("phase", "transit")},
        "planning": {
            "mode": planning_method,
            "allow_replan": params.get("allow_replan", True),
            "planning_time": params.get("planning_time", context.services.planning_time),
            "max_attempts": params.get("max_attempts", context.services.max_attempts)},
        "constraints": {
            "avoid_collision": params.get("avoid_collision", True),
            "velocity_scale": params.get("velocity_scale", context.services.velocity_scale),
            "acceleration_scale": params.get("acceleration_scale", 0.5),
            "safety_distance": params.get("safety_distance", context.services.safety_distance)},
        "strategy_params": dict(params.get("strategy_params", {})),
    }
    # Keep target semantics attached to the normalized target so the selector is the
    # single owner of automatic path choice (the reference implementation does not
    # let the command adapter silently override it).
    request["target"]["semantic_name"] = context.target
    # Relative tool targets are resolved from the current TCP pose and are not scene
    # bodies. They must never enter the static-object retry path merely because they
    # have no entity in the scene index.
    is_relative_target = context.target in {"end_effector", "tcp", "tool"}
    is_dynamic_target = is_relative_target or getattr(
        context.services, "target_is_dynamic", lambda _target: True)(context.target)
    static_target = None
    disable_vertical_move_when_static_item = params.get(
        "disable_vertical_move_when_static_item", True)
    if not isinstance(disable_vertical_move_when_static_item, bool):
        raise SkillCommandFailure({
            "error_code": "INVALID_PARAMETER",
            "error_message": "disable_vertical_move_when_static_item must be boolean.",
        })
    static_clearance = float(params.get("static_target_clearance_m", STATIC_TARGET_CLEARANCE_M))
    if not np.isfinite(static_clearance) or static_clearance < 0.0 or static_clearance > 1.0:
        raise SkillCommandFailure({"error_code": "INVALID_PARAMETER",
                                   "error_message": "static_target_clearance_m must be between 0 and 1 m."})
    if not is_dynamic_target and request.get("target", {}).get("type") == "pose":
        # Static objects are approached in two motions: first reach a collision-free
        # pre-approach pose above the object, then descend vertically with a Cartesian
        # approach. Keeping the original target separate is important; replacing it
        # with the elevated pose alone makes `move {item}` stop short of the object.
        static_target = _static_target_with_current_orientation(runtime, request["target"])
        request["target"] = _raise_static_target(static_target, static_clearance)
    skill = skill_type(robot_runtime=runtime,
                       trajectory_callback=lambda trajectory, *_: _record(
                           context, skill, trajectory))
    # MoveSkill owns its collision checker.  The executor's manipulation exemption is
    # attached to its shared service checker, so copy the same target/held-object
    # exemptions onto the checker that the skill's planners actually query.
    checker = getattr(skill, "collision_checker", None)
    resolve_colliders = getattr(context.services, "colliders_of", None)
    if checker is not None and callable(resolve_colliders):
        allowed = set(getattr(context.services, "held_colliders", lambda: ())())
        # The named target is the object being approached/manipulated. Its colliders
        # are exempt for the intended gripper-to-target contact, while arm/table
        # contacts remain collision failures.
        try:
            allowed.update(resolve_colliders(context.target) or ())
        except (KeyError, TypeError, ValueError):
            logger.debug("could not resolve target colliders", extra={"target": context.target})
        logger.debug("Move collision policy", extra={"target": context.target,
                     "dynamic_target": is_dynamic_target,
                     "allowed_colliders": len(allowed)})
        if allowed:
            checker.allowed_target_geom_ids = allowed
    exemption = getattr(context.services, "manipulation_exemption", None)
    manager = exemption(context.target) if callable(exemption) else nullcontext()
    previous_observer = getattr(runtime, "waypoint_observer", None)
    previous_mode = getattr(runtime, "execution_mode", None)
    physical_carry = bool(context.services.held_colliders())
    previous_speed = getattr(runtime, "max_joint_speed_deg", None)
    if physical_carry:
        velocity_cap, acceleration_cap, jerk_cap = _carry_limits()
        request["constraints"]["safety_distance"] = max(
            float(request["constraints"]["safety_distance"]), _carry_clearance())
        request["constraints"]["avoid_collision"] = True
        logger.debug("Held move obstacle clearance=%s m", request["constraints"]["safety_distance"])
        # The trajectory processor multiplies these limits by velocity_scale.
        # Apply the same configured velocity cap to the retimed path and servo.
        trajectory_config = dict(request["strategy_params"].get("trajectory", {}))
        limits = np.asarray(trajectory_config.get(
            "velocity_limits", getattr(skill, "config", {}).get("trajectory", {}).get("velocity_limits", 3.0)), dtype=float)
        scale = float(request["constraints"]["velocity_scale"])
        trajectory_config["velocity_limits"] = np.minimum(limits, velocity_cap / max(scale, 1e-9)).tolist()
        # Effective limits after the processor applies the request's scaling.
        for key, cap, factor, default in (
                ("acceleration_limits", acceleration_cap, float(request["constraints"]["acceleration_scale"]), 2.0),
                ("jerk_limits", jerk_cap, float(trajectory_config.get("jerk_scale", 1.0)), 10.0)):
            configured = trajectory_config.get(key, getattr(skill, "config", {}).get("trajectory", {}).get(key, default))
            trajectory_config[key] = np.minimum(np.asarray(configured, dtype=float), cap / max(factor, 1e-9)).tolist()
        request["strategy_params"]["trajectory"] = trajectory_config
        speed_deg = float(np.degrees(velocity_cap))
        runtime.max_joint_speed_deg = min(previous_speed, speed_deg) if previous_speed is not None else speed_deg
    if physical_carry and previous_mode == "kinematic":
        runtime.execution_mode = "actuator"
    if hasattr(runtime, "waypoint_observer"):
        runtime.waypoint_observer = context.frames.capture_at_fps
    adjustment = None
    try:
        with manager:
            requests = _grasp_pose_requests(runtime, context.target, request) if is_dynamic_target else [request]
            results = []
            for attempt_request in requests:
                logger.debug("Move grasp pose attempt", extra={
                    "target": context.target, "grasp_pose": attempt_request.get("grasp_pose")})
                # The pre-approach keeps the resolved path type and planning policy.
                # MoveSkill itself performs the reference direct -> RRT fallback.
                result = skill.execute(attempt_request)
                if "grasp_pose" in attempt_request:
                    result["grasp_pose"] = attempt_request["grasp_pose"]
                results.append(result)
                if result.get("success"):
                    if static_target is not None and not disable_vertical_move_when_static_item:
                        approach_request = copy.deepcopy(attempt_request)
                        approach_request["target"] = static_target
                        approach_request["motion"] = dict(approach_request.get("motion", {}))
                        approach_request["motion"]["path_type"] = "linear"
                        approach_request["motion"]["phase"] = "approach"
                        approach_request["motion"]["path_constraint"] = "hard"
                        approach_request["constraints"] = dict(approach_request.get("constraints", {}))
                        approach_request["constraints"]["keep_end_effector_orientation"] = True
                        logger.debug("Static target vertical approach", extra={
                            "target": context.target,
                            "clearance_m": static_clearance,
                        })
                        result = skill.execute(approach_request)
                        results.append(result)
                    break
                logger.debug("Move grasp pose failed", extra={
                    "target": context.target,
                    "grasp_pose_index": (attempt_request.get("grasp_pose") or {}).get("index"),
                    "error_code": (result.get("error") or {}).get("error_code"),
                })
            if results and not results[-1].get("success") and len(requests) > 1:
                result = _aggregate_grasp_failures(results, context.target)
            if static_target is not None:
                adjustment = {"applied": True,
                              "offset_world_m": [0.0, 0.0, static_clearance],
                              "reason": ("static_target_pre_approach_only"
                                         if disable_vertical_move_when_static_item
                                         else "static_target_vertical_approach")}
    finally:
        if physical_carry:
            runtime.max_joint_speed_deg = previous_speed
        if hasattr(runtime, "waypoint_observer"):
            runtime.waypoint_observer = previous_observer
        if physical_carry and previous_mode == "kinematic":
            runtime.execution_mode = previous_mode
    if not result.get("success"):
        error = dict(result.get("error") or {})
        if adjustment is not None:
            details = dict(error.get("details") or {})
            details["target_adjustment"] = adjustment
            error["details"] = details
        raise SkillCommandFailure(error)
    trajectory = dict(result.get("trajectory") or {})
    context.frames.capture()
    execution = dict(result.get("execution_result") or {})
    selection = dict(result.get("selection") or {})
    payload = {**trajectory, **execution, "selection": selection,
            "planner": trajectory.get("planner", selection.get("planning_mode")),
            "waypoint_count": len(trajectory.get("waypoints", ())),
            "direct_probe": "accepted" if selection.get("planning_mode") == "direct"
            else "rejected"}
    if adjustment is not None:
        payload["target_adjustment"] = adjustment
    if result.get("grasp_pose") is not None:
        payload["grasp_pose"] = result["grasp_pose"]
    return payload


def _record(context: CommandExecutionContext, skill, trajectory: dict) -> None:
    segment = dict(trajectory)
    segment["label"] = f"move -> {context.target}"
    context.outcome.segments.extend(segment_markers(
        skill.kinematics, [segment], prefix="segment",
        start_index=len(context.outcome.segments)))
    context.frames.capture()


def _target(runtime, name: str, params, resolve_target=None) -> dict:
    joints = params.get("joint_configuration")
    if joints is not None:
        return {"type": "joint", "joint_positions": list(joints)}
    if name == "home":
        # Keep named-state resolution in MoveSkill so it uses the checked-in
        # robot.named_states.home configuration from move/config.yaml.
        return {"type": "named_state", "state_name": "home"}
    if name in {"end_effector", "tcp", "tool"} and not params.get("target_pose"):
        # Relative compiler macros describe a displacement from the live TCP rather
        # than a named scene state.  Convert that displacement to a pose before the
        # Move Skill validates the request.
        from agent_service.runtime.executor import _relative_pose
        class _Services:
            pass
        services = _Services()
        services.runtime = runtime
        pose = _relative_pose(services, params, name)
        return _pose_target(pose)
    pose = params.get("target_pose")
    if pose is None and callable(resolve_target):
        pose = resolve_target(name, params.get("anchor"))
    pose = pose or runtime.object_pose(name)
    if pose is None:
        return {"type": "named_state", "state_name": name}
    position = pose["position"]
    return _pose_target(pose)


def _raise_static_target(target: dict, clearance_m: float = STATIC_TARGET_CLEARANCE_M) -> dict:
    """Keep a static object's move goal 5 cm above its resolved interaction pose."""
    if not isinstance(target, dict) or target.get("type") != "pose":
        return target
    position = target.get("position")
    if not isinstance(position, dict) or "z" not in position:
        return target
    adjusted = copy.deepcopy(target)
    adjusted["position"]["z"] = float(adjusted["position"]["z"]) + float(clearance_m)
    return adjusted


def _static_target_with_current_orientation(runtime, target: dict) -> dict:
    """Use the scene target position while preserving the current TCP orientation."""
    adjusted = copy.deepcopy(target)
    try:
        current = runtime.get_end_effector_pose()
        quaternion = current.get("quaternion_wxyz")
        if quaternion is not None and len(quaternion) == 4:
            adjusted["orientation"] = {
                "representation": "quaternion",
                "w": float(quaternion[0]), "x": float(quaternion[1]),
                "y": float(quaternion[2]), "z": float(quaternion[3]),
            }
    except (AttributeError, TypeError, ValueError, IndexError):
        # A lightweight runtime used by callers/tests may not expose a TCP pose. In
        # that case the planner's normal target-orientation fallback remains valid.
        pass
    return adjusted


def _pose_target(pose: dict) -> dict:
    position = pose["position"]
    target = {"type": "pose", "frame": "world",
            "position": {key: float(value) for key, value in zip("xyz", position)},
            }
    if "orientation" in pose:
        target["orientation"] = dict(pose["orientation"])
    elif pose.get("quaternion_wxyz") is not None:
        quaternion = pose["quaternion_wxyz"]
        target["orientation"] = {
            "representation": "quaternion", "w": float(quaternion[0]),
            "x": float(quaternion[1]), "y": float(quaternion[2]),
            "z": float(quaternion[3]),
        }
    return target
