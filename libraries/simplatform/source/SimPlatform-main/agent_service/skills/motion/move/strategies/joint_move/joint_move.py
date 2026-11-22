"""Joint-space path strategy."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ...planners.errors import PlanningError


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
        if target["type"] == "joint":
            goal = np.asarray(target["joint_positions"], dtype=float)
        elif target["type"] == "named_state":
            # move/config.yaml stores named states under robot.named_states.
            # Accept the legacy top-level form too, but prefer the checked-in
            # robot section so `move home` uses the configured pose exactly.
            presets = dict(self.config.get("named_states", {}))
            robot_config = self.config.get("robot", {})
            if isinstance(robot_config, Mapping):
                presets.update(robot_config.get("named_states", {}))
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
            result = self.kinematics.solve(
                target,
                seed=ik_seed,
                method=request["strategy_params"].get("ik_method", "auto"),
                timeout=min(float(request["planning"]["planning_time"]), 2.0),
            )
            goal = np.asarray(result["solution"], dtype=float)
            if request["constraints"]["avoid_collision"] and self.collision_checker is not None:
                candidates = sorted(
                    result.get("solutions", [goal]),
                    key=lambda q: float(np.linalg.norm(np.asarray(q) - ik_seed)),
                )
                # Numerical IK may return only its nearest branch. If that branch
                # collides, ask the kinematics backend for additional deterministic
                # branches before declaring the goal blocked.
                if not any(not self.collision_checker.check(
                        candidate,
                        safety_distance=float(request["constraints"].get("safety_distance", 0.0)),
                    ).in_collision for candidate in candidates):
                    discover = getattr(self.kinematics, "solve_candidates", None)
                    if callable(discover):
                        try:
                            extra = discover(
                                target,
                                seed=ik_seed,
                                method=request["strategy_params"].get("ik_method", "auto"),
                                timeout=min(float(request["planning"]["planning_time"]), 2.0),
                                max_solutions=8,
                            )
                            candidates.extend(extra)
                        except Exception:
                            # Preserve the original solver result and diagnostics.
                            pass
                selected = None
                seen = set()
                limit_rejections = []
                collision_rejections = []
                limits = np.asarray(self.kinematics.limits, dtype=float)
                for candidate in candidates:
                    candidate = np.asarray(candidate, dtype=float)
                    key = tuple(np.round(candidate, 8))
                    if key in seen:
                        continue
                    seen.add(key)
                    if candidate.shape != (len(limits),):
                        limit_rejections.append({"reason": "invalid_shape", "target": candidate.tolist()})
                        continue
                    invalid = np.flatnonzero(
                        (candidate < limits[:, 0] - 1e-9) |
                        (candidate > limits[:, 1] + 1e-9))
                    if len(invalid):
                        limit_rejections.append({
                            "joint_indices": invalid.tolist(),
                            "target": candidate.tolist(),
                            "limits": limits[invalid].tolist(),
                        })
                        continue
                    collision = self.collision_checker.check(
                        candidate,
                        safety_distance=float(request["constraints"].get("safety_distance", 0.0)),
                    )
                    if not collision.in_collision:
                        selected = candidate
                        break
                    collision_rejections.append({
                        "target": candidate.tolist(),
                        "contacts": list(collision.contacts),
                        "minimum_distance": collision.minimum_distance,
                    })
                if selected is None:
                    if limit_rejections and len(limit_rejections) == len(seen):
                        raise PlanningError(
                            "Every IK posture that reaches the target violates joint limits.",
                            code="JOINT_LIMIT_VIOLATION", recoverable=True,
                            details={"postures_tested": len(seen), "rejections": limit_rejections[:8]},
                        )
                    raise PlanningError(
                        "Every IK posture that reaches the target is in collision.",
                        code="COLLISION_DETECTED", recoverable=True,
                        details={"postures_tested": len(seen),
                                 "rejections": collision_rejections[:8]},
                    )
                goal = selected
            diagnostics["ik"] = {key: value for key, value in result.items() if key not in {"solution", "solutions"}}
        limits = self.kinematics.limits
        if np.any(goal < limits[:, 0]) or np.any(goal > limits[:, 1]):
            raise PlanningError(
                "Target joint state violates UR5e joint limits.",
                code="JOINT_LIMIT_VIOLATION", recoverable=True,
                details={"target": goal.tolist(), "limits": limits.tolist()},
            )
        return {"path_type": "joint", "joint_waypoints": np.vstack((start, goal)), "start": start, "goal": goal, "diagnostics": diagnostics}
