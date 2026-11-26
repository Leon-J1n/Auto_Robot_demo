"""cuRobo avoidance adapter for the host Move planner contract."""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any, Mapping

import numpy as np

from ...common.errors import PlanningError
from ...constraints.collision_sampling import collision_sampling_settings


class CollisionFreePlanner:
    """Plan a free-space detour and validate it against the host scene."""

    planner_id = "curobo"

    def __init__(
        self, config: Mapping[str, Any] | None = None, *, collision_checker: Any = None,
        joint_limits: Any = None, kinematics: Any = None, robot_runtime: Any = None,
        curobo_planner: Any = None,
    ) -> None:
        self.config = config or {}
        self.collision_checker = collision_checker
        self.joint_limits = np.asarray(joint_limits, dtype=float)
        self.robot_runtime = robot_runtime
        self.curobo_planner = curobo_planner
        self._own_curobo_planner = False

    def plan(
        self, path_request: Mapping[str, Any], move_request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        if move_request["motion"]["path_constraint"] == "hard" and path_request["path_type"] != "joint":
            raise PlanningError("A hard Cartesian path cannot be relaxed for collision avoidance.",
                                code="PATH_CONSTRAINT_INFEASIBLE", recoverable=True)
        if (path_request.get("orientation_constraint", {}).get("enabled")
                or move_request["constraints"].get("keep_end_effector_orientation")):
            raise PlanningError("cuRobo avoidance cannot preserve a fixed orientation along the entire path.",
                                code="PATH_CONSTRAINT_INFEASIBLE", recoverable=True)
        if move_request.get("target_body_name") and not hasattr(self.collision_checker, 'tool_geoms'):
            raise PlanningError("cuRobo avoidance does not support allowed target contacts.",
                                code="PATH_CONSTRAINT_INFEASIBLE", recoverable=True)
        if self.collision_checker is None:
            raise PlanningError("cuRobo avoidance requires host collision validation.")

        started = time.perf_counter()
        start = np.asarray(path_request["start"], dtype=float)
        goal = np.asarray(path_request["goal"], dtype=float)
        if (start.shape != (6,) or goal.shape != (6,)
                or not np.all(np.isfinite(start)) or not np.all(np.isfinite(goal))):
            raise PlanningError("Path strategy returned invalid joint endpoints.", code="TRAJECTORY_INVALID")
        if (self.joint_limits.shape != (6, 2)
                or np.any(start < self.joint_limits[:, 0]) or np.any(start > self.joint_limits[:, 1])
                or np.any(goal < self.joint_limits[:, 0]) or np.any(goal > self.joint_limits[:, 1])):
            raise PlanningError("Joint endpoints violate limits.", code="JOINT_LIMIT_VIOLATION")

        clearance = float(move_request["constraints"].get("safety_distance", 0.0))
        avoid_collision = move_request['constraints'].get('avoid_collision', True)
        endpoint_started = time.perf_counter()
        for location, joints in ((("start", start), ("goal", goal)) if avoid_collision else ()):
            hit = self.collision_checker.check(joints, safety_distance=clearance)
            if hit.in_collision:
                raise PlanningError(f"{location.capitalize()} state is in collision.",
                                    code="COLLISION_DETECTED", recoverable=True,
                                    details={"location": location, "contacts": list(hit.contacts)})

        if self.curobo_planner is None:
            if self.robot_runtime is None:
                raise PlanningError("cuRobo avoidance requires a current robot runtime and scene.")
            from .planner import CuroboPlanner
            self.curobo_planner = CuroboPlanner(config=self.config, robot_runtime=self.robot_runtime,
                                               collision_checker=self.collision_checker)
            self._own_curobo_planner = True

        planned_request = deepcopy(move_request)
        planned_request["target"] = {"type": "joint", "joint_positions": goal.tolist()}
        planned_request["motion"]["path_type"] = "joint"
        planned_request["planning"]["planner_type"] = "curobo"
        endpoint_check_s = time.perf_counter() - endpoint_started
        try:
            result = self.curobo_planner.plan(planned_request)
        except Exception as exc:
            runtime_error = getattr(exc, 'curobo_error', None)
            if isinstance(runtime_error, dict) and runtime_error.get('worker_restart_required'):
                raise PlanningError(str(exc), code=runtime_error.get('error_code', 'CUROBO_FAILED'),
                                    recoverable=False, details=runtime_error) from exc
            raise PlanningError(str(exc), code="CUROBO_FAILED", recoverable=True,
                                details=runtime_error) from exc
        if not result.get("success"):
            error = result.get("error") or {}
            runtime_error = error.get('details')
            if isinstance(runtime_error, dict) and runtime_error.get('worker_restart_required'):
                # A timeout/fatal CUDA error has discarded the process. Do not
                # respawn it for each branch or approach distance of this search.
                raise PlanningError(error.get('error_message') or 'cuRobo worker stopped.',
                                    code=runtime_error.get('error_code', 'CUROBO_FAILED'),
                                    recoverable=False, details=runtime_error)
            raise PlanningError(error.get("error_message") or "cuRobo could not find a collision-free path.",
                                code=error.get("error_code") or "CUROBO_FAILED", recoverable=True,
                                details=error.get("details"))

        trajectory = deepcopy(result["trajectory"])
        points = np.asarray(trajectory.get("waypoints"), dtype=float)
        if (points.ndim != 2 or points.shape[1] != 6 or len(points) < 2
                or not np.all(np.isfinite(points))
                or np.any(points < self.joint_limits[:, 0] - 1e-5)
                or np.any(points > self.joint_limits[:, 1] + 1e-5)
                or not np.allclose(points[0], start, atol=1e-3, rtol=0)
                or not np.allclose(points[-1], goal, atol=0.02, rtol=0)):
            raise PlanningError("cuRobo returned a trajectory with invalid endpoints or joint limits.",
                                code="TRAJECTORY_INVALID", recoverable=True)
        if (self.robot_runtime is not None
                and list(self.robot_runtime.joint_names) != trajectory.get("joint_names")):
            raise PlanningError("cuRobo and execution backend joint names/order differ.",
                                code="TRAJECTORY_INVALID", recoverable=True)

        collision_resolution, _ = collision_sampling_settings(move_request)
        validation_started = time.perf_counter()
        check = self.collision_checker.check_path(
            points, resolution=min(collision_resolution, 0.01), safety_distance=clearance,
        ) if avoid_collision and not trajectory.get('diagnostics', {}).get('gpu_collision_validation') else None
        validation_s = time.perf_counter() - validation_started
        if check is not None and check.in_collision:
            raise PlanningError("Host collision validation rejected the cuRobo trajectory.",
                                code="TRAJECTORY_INVALID", recoverable=True,
                                details={"stage": "final_collision_check", "contacts": list(check.contacts)})
        timeout = float(move_request['constraints'].get('timeout', 30.))
        if time.perf_counter() - started > timeout:
            raise PlanningError('cuRobo validation exceeded the enclosing request timeout.',
                                code='PLANNING_TIMEOUT', recoverable=False,
                                details={'timeout_s': timeout, 'elapsed_s': time.perf_counter() - started})
        trajectory["planner"] = self.planner_id
        trajectory["original_path_type"] = path_request["path_type"]
        trajectory["planning_time"] = time.perf_counter() - started
        diagnostics = trajectory.setdefault("diagnostics", {})
        diagnostics["path_relaxed"] = path_request["path_type"] != "joint"
        diagnostics["planning_stages_s"] = {
            "endpoint_collision_checks": endpoint_check_s,
            "curobo": result.get("timing", {}).get("planning", 0.0),
            "host_collision_validation": validation_s,
            "host_collision_validation_initial": result.get("timing", {}).get("host_collision_validation", 0.0),
            "host_collision_validation_total": result.get("timing", {}).get("host_collision_validation", 0.0) + validation_s,
            "preparation": result.get("timing", {}).get("preparation", 0.0),
            "conversion": result.get("timing", {}).get("conversion", 0.0),
            "collision_resolution_rad": min(collision_resolution, 0.01),
        }
        return trajectory

    def close(self) -> None:
        if self.curobo_planner is not None and self._own_curobo_planner:
            self.curobo_planner.close()
            self.curobo_planner = None
            self._own_curobo_planner = False
