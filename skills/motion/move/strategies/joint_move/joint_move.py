"""Joint-space path strategy."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from planning.errors import PlanningError


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
            defaults = {
                "home": [0.0, -1.5708, 1.5708, -1.5708, -1.5708, 0.0],
                "ready": [0.0, -1.2, 1.4, -1.75, -1.5708, 0.0],
                "park": [0.0, -1.5708, 0.0, -1.5708, 0.0, 0.0],
            }
            name = target["state_name"]
            if name not in presets and name not in defaults:
                raise PlanningError(f"Unknown named robot state: {name}", code="TARGET_UNREACHABLE", recoverable=True, details={"state_name": name})
            goal = np.asarray(presets.get(name, defaults[name]), dtype=float)
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
            for solution in solutions:
                for candidate in self.kinematics.equivalent_configurations(solution, start, max_variants=8):
                    if not any(np.allclose(candidate, q, atol=1e-8) for q in candidate_goals):
                        candidate_goals.append(np.asarray(candidate, dtype=float))
            candidate_goals.sort(key=lambda q: float(np.sum(np.abs(q - start))))
            goal = candidate_goals[0]
            diagnostics["ik"] = {"candidate_count": len(candidate_goals)}
        limits = self.kinematics.limits
        if np.any(goal < limits[:, 0]) or np.any(goal > limits[:, 1]):
            raise PlanningError(
                "Target joint state violates UR5e joint limits.",
                code="JOINT_LIMIT_VIOLATION", recoverable=True,
                details={"target": goal.tolist(), "limits": limits.tolist()},
            )
        return {"path_type": "joint", "joint_waypoints": np.vstack((start, goal)), "start": start, "goal": goal, "diagnostics": diagnostics,
                **({"candidate_goals": candidate_goals} if candidate_goals is not None else {})}

