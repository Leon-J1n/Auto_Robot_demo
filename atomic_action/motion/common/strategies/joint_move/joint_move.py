"""Joint-space path strategy."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from planning.common.errors import PlanningError
from planning.constraints.arm_posture import configuration_cost


def _periodic_joint_delta(q, reference):
    """Return the shortest per-joint angular displacement in radians."""
    delta = np.asarray(q, dtype=float) - np.asarray(reference, dtype=float)
    return np.arctan2(np.sin(delta), np.cos(delta))


def _nearest_equivalent(q, reference, limits):
    """Choose the 2π-equivalent joint values nearest the current state."""
    q = np.asarray(q, dtype=float)
    reference = np.asarray(reference, dtype=float)
    limits = np.asarray(limits, dtype=float)
    result = q.copy()
    for index, value in enumerate(q):
        candidates = [float(value + turns * 2.0 * np.pi)
                      for turns in range(-3, 4)
                      if limits[index, 0] - 1e-9 <= value + turns * 2.0 * np.pi <= limits[index, 1] + 1e-9]
        if candidates:
            result[index] = min(candidates, key=lambda candidate: abs(candidate - reference[index]))
    return result


class JointMove:
    """Translate a normalized Move request into a joint-path specification."""

    strategy_id = "joint_move"

    def __init__(self, config: Mapping[str, Any] | None = None, *, kinematics: Any = None, collision_checker: Any = None) -> None:
        self.config = config or {}
        self.kinematics = kinematics
        self.collision_checker = collision_checker

    def build_path_request(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        target = request["target"]
        start = np.asarray(context["current_joint_state"], dtype=float)
        diagnostics = {}
        candidate_goals = None
        if target["type"] == "joint":
            goal = np.asarray(target["joint_positions"], dtype=float)
        elif target["type"] == "named_state":
            presets = self.config.get("named_states", {})
            name = target["state_name"]
            if name not in presets:
                raise PlanningError(f"Unknown named robot state: {name}", code="TARGET_UNREACHABLE", recoverable=True, details={"state_name": name})
            goal = np.asarray(presets[name], dtype=float)
        else:
            seed_value = request["strategy_params"].get("ik_seed")
            ik_seed = np.asarray(seed_value, dtype=float) if seed_value is not None else start
            solutions = self.kinematics.solve_all(
                target,
                seed=ik_seed,
                backend=request["strategy_params"].get("ik_method", "auto"),
                timeout=min(float(request["planning"]["planning_time"]), 2.0),
                max_solutions=8,
            ).solutions
            if not solutions:
                raise PlanningError("No IK configuration reaches the target.", code="IK_FAILED", recoverable=True)
            candidate_goals = []
            limits = self.kinematics.limits
            for solution in solutions:
                for candidate in self.kinematics.equivalent_configurations(solution, start, max_variants=8):
                    candidate = _nearest_equivalent(candidate, start, limits)
                    if not any(np.allclose(candidate, q, atol=1e-8) for q in candidate_goals):
                        candidate_goals.append(np.asarray(candidate, dtype=float))
            candidate_goals.sort(key=lambda q: configuration_cost(self.kinematics, q, start))
            goal = candidate_goals[0]
            diagnostics["ik"] = {"candidate_count": len(candidate_goals)}
        limits = self.kinematics.limits
        if goal.shape != start.shape:
            raise PlanningError(
                f"Target joint vector has {goal.size} values; this robot requires {start.size}.",
                code="JOINT_LIMIT_VIOLATION", recoverable=True,
            )
        if np.any(goal < limits[:, 0]) or np.any(goal > limits[:, 1]):
            outside = np.flatnonzero((goal < limits[:, 0]) | (goal > limits[:, 1]))
            joint_names = getattr(getattr(self.kinematics, "robot", None), "joint_names", ())
            details = {"target": goal.tolist(), "limits": limits.tolist()}
            if outside.size and int(outside[0]) < len(joint_names):
                details["joint"] = joint_names[int(outside[0])]
            raise PlanningError(
                "Target joint state violates robot joint limits.",
                code="JOINT_LIMIT_VIOLATION", recoverable=True,
                details=details,
            )
        return {"path_type": "joint", "joint_waypoints": np.vstack((start, goal)), "start": start, "goal": goal, "diagnostics": diagnostics,
                **({"candidate_goals": candidate_goals} if candidate_goals is not None else {})}


