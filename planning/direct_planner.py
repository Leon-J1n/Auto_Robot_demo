"""Direct trajectory generation and validation."""

from __future__ import annotations

import time
from typing import Any, Mapping

import numpy as np

from .errors import CollisionPlanningError, PlanningError


class DirectPlanner:
    """Generate and validate the direct trajectory implied by a Path Strategy."""

    planner_id = "direct"

    def __init__(self, config: Mapping[str, Any] | None = None, *, collision_checker: Any = None, joint_limits: Any = None) -> None:
        self.config = config or {}
        self.collision_checker = collision_checker
        self.joint_limits = np.asarray(joint_limits, dtype=float)

    def plan(
        self,
        path_request: Mapping[str, Any],
        move_request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Any:
        started = time.perf_counter()
        source = np.asarray(path_request["joint_waypoints"], dtype=float)
        if source.ndim != 2 or source.shape[1] != 6 or len(source) < 2:
            raise PlanningError("Path strategy returned an invalid joint path.")
        if not np.all(np.isfinite(source)):
            raise PlanningError("Path contains non-finite joint values.")
        if np.any(source < self.joint_limits[:, 0]) or np.any(source > self.joint_limits[:, 1]):
            raise PlanningError("Path violates joint limits.", code="JOINT_LIMIT_VIOLATION")
        if move_request["constraints"]["avoid_collision"]:
            goal_check = self.collision_checker.check(
                source[-1],
                safety_distance=float(move_request["constraints"].get("safety_distance", 0.0)),
                allowed_geom_ids=getattr(self.collision_checker, "allowed_target_geom_ids", None),
            )
            if goal_check.in_collision:
                # A colliding endpoint cannot be repaired by routing around an
                # obstacle. Let candidate selection try another configuration;
                # only an interior path collision should trigger RRT fallback.
                raise PlanningError(
                    "Goal state is in collision.", code="COLLISION_DETECTED", recoverable=True,
                    details={"location": "goal", "contacts": list(goal_check.contacts)},
                )
        # Keep the exported joint trajectory dense enough for actuator
        # tracking and route recording.  This is a per-joint max increment.
        waypoints = _densify(source, max_step=0.04)
        if move_request["constraints"]["avoid_collision"]:
            safety_distance = float(move_request["constraints"].get("safety_distance", 0.0))
            validator = getattr(self.collision_checker, 'validate_execution_path', None)
            kwargs = dict(safety_distance=safety_distance,
                          allowed_geom_ids=getattr(self.collision_checker, "allowed_target_geom_ids", None))
            result = (validator(waypoints, **kwargs) if callable(validator) else
                      self.collision_checker.check_path(waypoints, resolution=.04, **kwargs))
            if result.in_collision:
                raise CollisionPlanningError("Direct path collides with the robot or environment.", details=result.contacts)
        velocity_scale = float(move_request["constraints"]["velocity_scale"])
        duration = float(np.sum(np.max(np.abs(np.diff(waypoints, axis=0)), axis=1)) / max(velocity_scale, 0.01))
        trajectory = {
            "planner": self.planner_id,
            "path_type": path_request["path_type"],
            "waypoints": waypoints.tolist(),
            **({"cartesian_joint_waypoints": source.tolist()} if path_request["path_type"] == "linear" else {}),
            "duration": duration,
            "planning_time": time.perf_counter() - started,
            "diagnostics": path_request.get("diagnostics", {}),
        }
        return trajectory


def _densify(points: np.ndarray, max_step: float) -> np.ndarray:
    output = [points[0]]
    for start, goal in zip(points[:-1], points[1:]):
        count = max(1, int(np.ceil(np.max(np.abs(goal - start)) / max_step)))
        output.extend(start + alpha * (goal - start) for alpha in np.linspace(0.0, 1.0, count + 1)[1:])
    return np.asarray(output)
