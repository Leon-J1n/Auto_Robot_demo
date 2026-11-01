"""Planning helpers shared by interaction task orchestration.

This module owns candidate-distance search, pose offsets, and the pre-contact
linear-path feasibility check used by pick/approach/retreat tasks.  Execution
layers should consume the returned requests and prepared planning diagnostics.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

import numpy as np

from kinematics import IKError
from planning.direct_planner import DirectPlanner
from planning.errors import PlanningError
from skills.motion.move.strategies import LinearMove


def search_distances(params, *, minimum=0.03, maximum=0.10, step=0.005, label="pose"):
    """Return candidate distances in longest-to-shortest order."""
    search = params.get("search", {})
    if not isinstance(search, dict):
        raise ValueError("search must be an object")
    minimum = float(search.get("min_distance", params.get("min_distance", minimum)))
    maximum = float(search.get("max_distance", params.get("max_distance", maximum)))
    step = float(search.get("step", params.get("step", step)))
    if not all(np.isfinite(value) for value in (minimum, maximum, step)):
        raise ValueError(f"{label} search values must be finite")
    if minimum < 0 or maximum < minimum or step <= 0:
        raise ValueError(f"{label} search requires 0 <= min_distance <= max_distance and step > 0")
    return np.arange(maximum, minimum - step * 0.5, -step)


def candidate_distances(params, defaults, *, label="pose"):
    """Use caller-provided ordered fallback distances or shared search config."""
    if "search" not in params and not any(key in params for key in ("min_distance", "max_distance", "step")):
        return tuple(float(value) for value in defaults)
    return tuple(float(value) for value in search_distances(params, label=label))


def offset_pose(transform, axis_world, distance):
    """Translate a homogeneous pose backwards along a world-space axis."""
    result = np.asarray(transform, dtype=float).copy()
    result[:3, 3] -= np.asarray(axis_world, dtype=float) * float(distance)
    return result


def candidate_pose(transform, axis_world, distance):
    """Return a pre-contact pose offset from the interaction pose."""
    return offset_pose(transform, axis_world, distance)


def run_candidate_search(distances, attempt: Callable[[float], Any]):
    """Run ordered candidates until an attempt succeeds or becomes terminal."""
    for distance in distances:
        result = attempt(float(distance))
        if result is True:
            return True
        if result is False:
            return False
    return False


class PickContactPlanner:
    """Validate a pick approach and its constrained contact path.

    The planner returns the selected pre-contact joint state and diagnostics.
    It does not execute motion or mutate runtime state.
    """

    def __init__(self, *, kinematics, collision_checker, joint_limits=None):
        self.kinematics = kinematics
        self.collision_checker = collision_checker
        self.linear = LinearMove(kinematics=kinematics, collision_checker=collision_checker)
        self.direct = DirectPlanner(collision_checker=collision_checker, joint_limits=joint_limits)

    def plan(self, base_request: Mapping[str, Any], state: Mapping[str, Any], *, goal,
              direction, requested_path, distances, pose_request):
        """Return ``(selected, rejected)`` with rejected candidate diagnostics."""
        from scipy.spatial.transform import Rotation
        rejected = []
        for distance in distances:
            approach = candidate_pose(goal, direction, distance)
            try:
                solutions = self.kinematics.solve_all(
                    {"position": approach[:3, 3], "rotation_matrix": approach[:3, :3]},
                    seed=state["joints"], max_solutions=8)
            except IKError as exc:
                rejected.append({"distance_m": distance, "category": "prepoint_ik", "reason": str(exc)})
                continue
            for q in sorted(solutions.solutions, key=lambda value: np.linalg.norm(value - state["joints"])):
                hit = self.collision_checker.check(q, safety_distance=base_request["constraints"]["safety_distance"])
                if hit.in_collision:
                    rejected.append({"distance_m": distance, "category": "prepoint_collision", "contacts": hit.contacts})
                    continue
                contact = dict(base_request)
                contact["motion"] = dict(base_request["motion"], path_type="linear", path_constraint="hard", phase="contact")
                contact["constraints"] = dict(base_request["constraints"], keep_end_effector_orientation=True)
                contact["planning"] = dict(base_request["planning"], mode="direct", allow_replan=False)
                context = {"current_joint_state": np.asarray(q), "current_pose": {
                    "position": approach[:3, 3], "rotation_matrix": approach[:3, :3],
                    "quaternion_wxyz": Rotation.from_matrix(approach[:3, :3]).as_quat()[[3, 0, 1, 2]]}}
                try:
                    path = self.linear.build_path_request(contact, context)
                    self.direct.plan(path, contact, context)
                except (PlanningError, IKError, ValueError) as exc:
                    category = ("contact_ik" if isinstance(exc, IKError) else
                                "contact_collision" if isinstance(exc, PlanningError) and exc.code == "COLLISION_DETECTED" else
                                "contact_planning")
                    entry = {"distance_m": distance, "category": category, "reason": str(exc)}
                    if isinstance(exc, PlanningError) and exc.details:
                        entry["contacts" if category == "contact_collision" else "details"] = exc.details
                    rejected.append(entry)
                    continue
                transit = dict(base_request)
                transit["target"] = ({"type": "joint", "joint_positions": np.asarray(q).tolist()}
                                      if requested_path == "joint" and not base_request["constraints"].get("keep_end_effector_orientation")
                                      else pose_request(approach))
                return ({"approach": approach, "distance": distance, "joint_positions": np.asarray(q),
                         "direction": np.asarray(direction), "rejected": rejected,
                         "contact": contact, "transit": transit}, rejected)
        return None, rejected
