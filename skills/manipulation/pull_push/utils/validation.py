"""Validation and normalization for Push/Pull requests."""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Dict, Mapping


class PullPushValidationError(ValueError):
    pass


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise PullPushValidationError(f"{field} must be a finite number.")
    value = float(value)
    if value < 0.0 or positive and value <= 0.0:
        raise PullPushValidationError(f"{field} must be {'positive' if positive else 'non-negative'}.")
    return value


def _unit_vector(value: Any, field: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PullPushValidationError(f"{field} must contain three numbers.")
    vector = [float(v) for v in value]
    if not all(math.isfinite(v) for v in vector):
        raise PullPushValidationError(f"{field} must be finite.")
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 1e-12:
        raise PullPushValidationError(f"{field} must not be zero.")
    return [v / norm for v in vector]


def validate_pull_push_request(request: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    if not isinstance(request, Mapping):
        raise PullPushValidationError("Push/Pull request must be an object.")
    result = deepcopy(dict(request))
    grasp = result.get('grasp', {})
    if not isinstance(grasp, Mapping) or ('mode' in grasp and
            (not isinstance(grasp['mode'], str) or grasp['mode'] not in {'binding', 'physical'})):
        raise PullPushValidationError('grasp.mode must be binding or physical.')
    target = result.get("target")
    if not isinstance(target, Mapping) or target.get("type") not in {"object", "mechanism"} or not isinstance(target.get("object_id"), str) or not target["object_id"]:
        raise PullPushValidationError("target.type and target.object_id are required.")
    result["target"] = dict(target)
    action = result.get("manipulation", {})
    if not isinstance(action, Mapping):
        raise PullPushValidationError("manipulation must be an object.")
    result["manipulation"] = {
        "operation": "push",
        "trajectory": "auto",
        "interaction_mode": "auto",
        "maintain_contact": False,
        "verification_mode": "auto",
        "contact_mode": "auto",
        **action,
    }
    if result["manipulation"]["operation"] not in {"push", "pull"}:
        raise PullPushValidationError("manipulation.operation must be push or pull.")
    if result["manipulation"]["trajectory"] not in {"auto", "linear", "circular"}:
        raise PullPushValidationError("manipulation.trajectory must be auto, linear, or circular.")
    if result["manipulation"]["interaction_mode"] not in {"auto", "mechanism", "free_object"}:
        raise PullPushValidationError("manipulation.interaction_mode must be auto, mechanism, or free_object.")
    if not isinstance(result["manipulation"]["maintain_contact"], bool):
        raise PullPushValidationError("manipulation.maintain_contact must be boolean.")
    defaults = {"maximum_force": None, "maximum_travel": None, "maximum_angle": None, "contact_threshold": 1.0, "zero_force_threshold": 0.0, "speed": 0.03, "angular_speed": 0.2, "timeout": 30.0, "normal_alignment_tolerance": 1.0, "avoid_collision": True, "verify_motion": True, "stop_on_contact_loss": True, "emergency_retract": False, "ignore_path_constraints": True, "cartesian_step": 0.005, "angular_step": 0.02, "position_tolerance": 0.01, "angle_tolerance": 0.04, "contact_loss_timeout": 0.1, "settle_time": 1.0, "max_joint_speed": 0.5, "max_joint_acceleration": 1.0}
    constraints = result.get("constraints", {})
    if not isinstance(constraints, Mapping):
        raise PullPushValidationError("constraints must be an object.")
    result["constraints"] = {**defaults, **dict((config or {}).get("constraints", {})), **constraints}
    if result["constraints"]["emergency_retract"]:
        raise PullPushValidationError("Automatic emergency retreat is unsupported; plan a separate recovery move.")
    for key in ("maximum_travel", "maximum_angle", "contact_threshold", "zero_force_threshold"):
        if result["constraints"][key] is not None:
            result["constraints"][key] = _number(result["constraints"][key], f"constraints.{key}", positive=key not in {"contact_threshold", "zero_force_threshold"})
    for key in ("speed", "angular_speed", "timeout", "cartesian_step", "angular_step", "position_tolerance", "angle_tolerance", "contact_loss_timeout", "settle_time", "max_joint_speed", "max_joint_acceleration"):
        result["constraints"][key] = _number(result["constraints"][key], f"constraints.{key}", positive=True)
    result["constraints"]["normal_alignment_tolerance"] = _number(result["constraints"]["normal_alignment_tolerance"], "constraints.normal_alignment_tolerance")
    if result["constraints"]["normal_alignment_tolerance"] > 10.0:
        raise PullPushValidationError("normal_alignment_tolerance must not exceed 10 degrees.")
    if result["constraints"].get("avoid_collision") is not True:
        raise PullPushValidationError("Push/Pull requires constraints.avoid_collision=true.")
    if result["constraints"]["maximum_force"] is not None:
        result["constraints"]["maximum_force"] = _number(result["constraints"]["maximum_force"], "constraints.maximum_force", positive=True)
    for key in ("verify_motion", "stop_on_contact_loss"):
        if not isinstance(result["constraints"][key], bool):
            raise PullPushValidationError(f"constraints.{key} must be boolean.")
    if not result["constraints"]["verify_motion"]:
        raise PullPushValidationError("Completion must be verified against measured motion.")
    params = result.get("strategy_params", {})
    if not isinstance(params, Mapping):
        raise PullPushValidationError("strategy_params must be an object.")
    result["strategy_params"] = dict(params)
    carried_objects = result["strategy_params"].get("carried_objects", [])
    if not isinstance(carried_objects, list) or not all(isinstance(name, str) and name for name in carried_objects):
        raise PullPushValidationError("strategy_params.carried_objects must be a list of object names.")
    result["strategy_params"]["carried_objects"] = carried_objects
    interaction_mode = result["manipulation"]["interaction_mode"]
    if interaction_mode == "auto":
        interaction_mode = "free_object" if target["type"] == "object" else "mechanism"
    if interaction_mode == "free_object" and target["type"] != "object":
        raise PullPushValidationError("free_object interaction requires target.type=object.")
    if interaction_mode == "mechanism" and target["type"] != "mechanism":
        raise PullPushValidationError("mechanism interaction requires target.type=mechanism.")
    if interaction_mode == "free_object" and carried_objects:
        raise PullPushValidationError("carried_objects is only valid for mechanism interaction.")
    contact_mode = result["manipulation"]["contact_mode"]
    if contact_mode not in {"auto", "grasp", "push_contact"}:
        raise PullPushValidationError("contact_mode must be auto, grasp or push_contact.")
    if contact_mode == "push_contact" and result["manipulation"]["operation"] == "pull":
        raise PullPushValidationError("Non-grasped contact cannot pull.")
    if target.get("normal_frame", "world") != "world" or params.get("frame", "world") != "world":
        raise PullPushValidationError("Only world-frame vectors are supported; transform explicitly first.")
    if carried_objects:
        raise PullPushValidationError("carried_objects is obsolete: contents must move through physics contacts.")
    if result["manipulation"]["verification_mode"] != "auto":
        raise PullPushValidationError("Only automatic measured-motion verification is supported.")
    point = target.get("contact_point")
    if point is not None:
        if not isinstance(point, (list, tuple)) or len(point) != 3 or not all(math.isfinite(float(v)) for v in point):
            raise PullPushValidationError("contact_point must be three finite target-body-local coordinates.")
    result["manipulation"]["contact_mode"] = contact_mode
    result["manipulation"]["interaction_mode"] = interaction_mode
    result["resolved_interaction_mode"] = interaction_mode
    # `motion.path_type` is the explicit end-effector geometry.  Keep
    # manipulation.trajectory as the backwards-compatible alias, but do not
    # let a mechanism's inferred/legacy trajectory silently override an
    # explicit tool path.
    motion = result.get("motion", {})
    if not isinstance(motion, Mapping):
        raise PullPushValidationError("motion must be an object.")
    explicit_path = motion.get("path_type")
    if explicit_path is not None:
        if explicit_path not in {"linear", "circular"}:
            raise PullPushValidationError("motion.path_type must be linear or circular.")
        legacy_path = result["manipulation"]["trajectory"]
        if legacy_path not in {"auto", explicit_path}:
            raise PullPushValidationError(
                "motion.path_type conflicts with manipulation.trajectory."
            )
        trajectory = explicit_path
        result["manipulation"]["trajectory"] = trajectory
    else:
        trajectory = result["manipulation"]["trajectory"]
    if trajectory == "auto":
        trajectory = "circular" if any(k in params for k in ("arc_center", "arc_axis", "arc_angle")) else "linear"
        result["manipulation"]["trajectory"] = trajectory
    # Keep end-effector path geometry explicit.  Target type describes contact
    # semantics and must not select the arm path.
    result["motion"] = {"path_type": trajectory}
    if result["manipulation"]["operation"] != "verify_only":
        result["resolved_contact_normal"] = (_unit_vector(target["contact_normal"], "target.contact_normal") if "contact_normal" in target else None)
        normal_frame = str(target.get("normal_frame", "world"))
        if normal_frame not in {"world", "base"}:
            raise PullPushValidationError("target.normal_frame must be world or base.")
        result["target"]["normal_frame"] = normal_frame
        if trajectory == "linear":
            result["strategy_params"]["direction"] = _unit_vector(params.get("direction"), "strategy_params.direction")
            if interaction_mode == "free_object" and contact_mode == "push_contact" and result["resolved_contact_normal"] is not None:
                direction = result["strategy_params"]["direction"]
                normal = result["resolved_contact_normal"]
                cosine = sum(a * b for a, b in zip(direction, normal))
                tolerance = math.cos(math.radians(result["constraints"]["normal_alignment_tolerance"]))
                if cosine > -tolerance:
                    raise PullPushValidationError(
                        "Free-object push direction must be opposite and collinear with the outward contact normal."
                    )
            distance = _number(params.get("distance"), "strategy_params.distance", positive=True)
            result["strategy_params"]["distance"] = distance
        else:
            if "arc_center" not in params or "arc_axis" not in params:
                raise PullPushValidationError("Circular motion requires arc_center (point on axis) and arc_axis in world coordinates.")
            if "arc_center" in params:
                value = params["arc_center"]
                if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(math.isfinite(float(v)) for v in value):
                    raise PullPushValidationError("arc_center must contain three finite numbers.")
                result["strategy_params"]["arc_center"] = [float(v) for v in value]
            if "arc_axis" in params:
                result["strategy_params"]["arc_axis"] = _unit_vector(params["arc_axis"], "strategy_params.arc_axis")
            signed_angle = params.get("arc_angle", 0.0)
            if isinstance(signed_angle, bool) or not isinstance(signed_angle, (int, float)):
                raise PullPushValidationError("arc_angle must be a finite signed number.")
            angle = _number(abs(signed_angle), "strategy_params.arc_angle", positive=True)
            result["strategy_params"]["arc_angle"] = math.copysign(angle, float(params["arc_angle"]))
    return result
