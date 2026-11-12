"""Move Skill with injectable kinematics, collision checking and execution."""

from __future__ import annotations

import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collision import SimPlatformFCLCollisionChecker
from kinematics import IKError, KinematicsService
from planning.prepared_trajectory import PreparedTrajectory, planning_state_signature
from planning.arm_posture import ArmPosturePreference, configuration_cost, preference_for
from planning import CollisionFreePlanner, CollisionPlanningError, DirectPlanner, PlanningError
from atomic_action.motion.common.selector import StrategySelectionError, StrategySelector
from atomic_action.motion.common.strategies import CircularMove, JointMove, LinearMove
from atomic_action.motion.common.utils.error_codes import ErrorCode, failure_result
from atomic_action.motion.common.utils.validation import MoveValidationError, validate_move_request
if TYPE_CHECKING:
    from utils import SceneRobotRuntime


DEFAULT_SCENE = PROJECT_ROOT / "world_model" / "robotsim" / "scene_000.xml"


class _MoveExecutor:
    """Validate, select, plan, collision-check, and execute one motion request."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, robot_runtime: SceneRobotRuntime | None = None, scene_path: str | Path = DEFAULT_SCENE, trajectory_callback: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], None] | None = None, kinematics: KinematicsService | None = None, collision_checker: Any = None) -> None:
        self.config: Mapping[str, Any] = config or {}
        if robot_runtime is None:
            from utils import SceneRobotRuntime
            robot_runtime = SceneRobotRuntime(scene_path)
        self.robot_runtime = robot_runtime
        self.kinematics = kinematics if kinematics is not None else self.robot_runtime.kinematics
        if not hasattr(self.kinematics, 'arm_posture'):
            import mujoco
            if isinstance(getattr(robot_runtime, 'model', None), mujoco.MjModel):
                self.kinematics.arm_posture = ArmPosturePreference(robot_runtime)
        # Planning collision queries are independent of the live MuJoCo model.
        # MuJoCo remains the execution/physics backend below this layer.
        self.collision_checker = collision_checker if collision_checker is not None else SimPlatformFCLCollisionChecker(self.robot_runtime)
        self.selector = StrategySelector(self.config)
        self.trajectory_callback = trajectory_callback
        self.strategies = {
            "joint_move": JointMove(self.config, kinematics=self.kinematics, collision_checker=self.collision_checker),
            "linear_move": LinearMove(self.config, kinematics=self.kinematics, collision_checker=self.collision_checker),
            "circular_move": CircularMove(self.config, kinematics=self.kinematics),
        }
        planner_kwargs = {
            "collision_checker": self.collision_checker,
            "joint_limits": self.robot_runtime.joint_limits,
        }
        self.direct_planner = DirectPlanner(self.config, **planner_kwargs)
        self.collision_free_planner = CollisionFreePlanner(self.config, kinematics=self.kinematics, **planner_kwargs)

    def execute(self, request: Mapping[str, Any], *, prepared_trajectory: PreparedTrajectory | None = None) -> Dict[str, Any]:
        """Run the complete Move pipeline and always return a structured result."""
        selection: Dict[str, Any] | None = None
        trajectory = None
        timing: Dict[str, float] = {}
        started = time.perf_counter()
        try:
            stage_started = time.perf_counter()
            try:
                normalized = validate_move_request(request, self.config)
            finally:
                timing["validation"] = time.perf_counter() - stage_started
            target_body_name = normalized.get("target_body_name")
            self.collision_checker.allowed_target_geom_ids = None
            if hasattr(self.collision_checker, "set_allowed_target_names"):
                self.collision_checker.set_allowed_target_names(set())
            if target_body_name and hasattr(self.collision_checker, "set_allowed_target_names"):
                self.collision_checker.set_allowed_target_names({str(target_body_name)})
            stage_started = time.perf_counter()
            try:
                context = self._get_runtime_context(normalized)
            finally:
                timing["context"] = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            try:
                selection = self.selector.select(normalized, context)
            finally:
                timing["selection"] = time.perf_counter() - stage_started
            strategy = self.strategies[selection["path_strategy"]]
            stage_started = time.perf_counter()
            try:
                trajectory = self._reuse_prepared_trajectory(prepared_trajectory, normalized, context, selection)
                path_request = strategy.build_path_request(normalized, context) if trajectory is None else None
            finally:
                timing["path_generation"] = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            try:
                if trajectory is None:
                    trajectory = self._plan_with_fallback(normalized, context, selection, path_request)
            finally:
                timing["planning"] = time.perf_counter() - stage_started
            if self.trajectory_callback is not None:
                stage_started = time.perf_counter()
                try:
                    self.trajectory_callback(trajectory, normalized, selection)
                finally:
                    timing["trajectory_preparation"] = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            posture = preference_for(getattr(self, 'kinematics', None))
            if posture is not None:
                trajectory.setdefault('diagnostics', {})['arm_posture'] = {
                    'objective': 'elevated_arm_soft_preference',
                    'start': posture.metrics(trajectory['waypoints'][0]),
                    'goal': posture.metrics(trajectory['waypoints'][-1]),
                    'path_penalty_rad': posture.path_penalty(trajectory['waypoints'])}
            timing["posture_scoring"] = time.perf_counter() - stage_started
            timing["pre_execution_total"] = time.perf_counter() - started
            planning_stages = trajectory.get("diagnostics", {}).get("planning_stages_s")
            if planning_stages:
                timing["planner_stages"] = dict(planning_stages)
            stage_started = time.perf_counter()
            try:
                execution_result = self._execute_trajectory(trajectory, normalized, context)
            finally:
                timing["execution"] = time.perf_counter() - stage_started
            execution_result["planning_time"] = trajectory.get("planning_time", 0.0)
            execution_result["planned_duration"] = trajectory.get("duration", 0.0)
            timing["total"] = time.perf_counter() - started
            if execution_result.get('grasp_error'):
                error = execution_result['grasp_error']
                result = failure_result('GRASP_FAILED', error['error_message'], failed_stage='execution',
                                        recoverable=True, details=error)
                result.update(execution_result=execution_result, trajectory=trajectory, timing=timing,
                              selection=selection)
                return result
            return {"success": True, "status": "completed", "selection": selection, "execution_result": execution_result, "trajectory": trajectory, "timing": timing, "motion_time": execution_result.get("execution_time", 0.0), "error": None}
        except MoveValidationError as exc:
            result = failure_result(ErrorCode.INVALID_REQUEST, str(exc), failed_stage="validation", recoverable=True, recommended_action="Correct the request fields and retry.")
        except StrategySelectionError as exc:
            result = failure_result(ErrorCode.UNKNOWN_STRATEGY, str(exc), failed_stage="selection", recoverable=True)
        except IKError as exc:
            result = failure_result(ErrorCode.IK_FAILED, str(exc), failed_stage="kinematics", recoverable=True, recommended_action="Try another registered IK backend, another seed, or a reachable target pose.")
        except CollisionPlanningError as exc:
            result = failure_result(ErrorCode.COLLISION_DETECTED, str(exc), failed_stage="planning", recoverable=True, recommended_action="Allow collision-free planning or change the target/path constraint.", details={"contacts": exc.details or []})
        except PlanningError as exc:
            result = failure_result(
                exc.code, str(exc), failed_stage="planning", recoverable=exc.recoverable,
                recommended_action=self._planning_recommendation(exc.code), details=exc.details,
            )
        except MoveExecutionError as exc:
            result = failure_result(
                exc.code, str(exc), failed_stage="execution", recoverable=exc.recoverable,
                recommended_action=exc.recommended_action,
            )
        except TimeoutError as exc:
            result = failure_result(ErrorCode.EXECUTION_TIMEOUT, str(exc), failed_stage="execution", recoverable=True,
                                    status="timeout", details=getattr(exc, 'details', None))
            if trajectory is not None:
                result['trajectory'] = trajectory
        except ValueError as exc:
            result = failure_result(ErrorCode.PLANNING_FAILED, str(exc), failed_stage="path_generation", recoverable=True)
        except RuntimeError as exc:
            from demo.error_messages import error_from_exception
            error = error_from_exception(exc, stage="execution")
            result = failure_result(error.get('error_code', ErrorCode.EXECUTION_FAILED),
                                    error['error_message'],
                                    failed_stage=error.get('failed_stage') or 'execution',
                                    recoverable=error.get('recoverable', True),
                                    details=error.get('details'))
            result['error'].update(error)
        except Exception as exc:
            result = failure_result(ErrorCode.INTERNAL_ERROR, str(exc), failed_stage="unknown", recoverable=False)
        if selection is not None:
            result["selection"] = selection
        if trajectory is not None:
            planning_stages = trajectory.get("diagnostics", {}).get("planning_stages_s")
            if planning_stages:
                timing["planner_stages"] = dict(planning_stages)
        timing["total"] = time.perf_counter() - started
        result["timing"] = timing
        return result

    def _get_runtime_context(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Delegate scene and robot-state acquisition to the root utils module."""
        return self.robot_runtime.get_context(request)

    def _reuse_prepared_trajectory(self, prepared, request, context, selection):
        if (not isinstance(prepared, PreparedTrajectory) or request != prepared.request
                or selection["path_strategy"] != "joint_move"
                or planning_state_signature(self.robot_runtime, self.collision_checker) != prepared.state_signature):
            return None
        trajectory = deepcopy(dict(prepared.trajectory))
        points = np.asarray(trajectory.get("waypoints", []), dtype=float)
        if (points.ndim != 2 or points.shape[1] != 6 or len(points) < 2
                or not np.all(np.isfinite(points))
                or np.any(points < self.robot_runtime.joint_limits[:, 0])
                or np.any(points > self.robot_runtime.joint_limits[:, 1])
                or not np.allclose(points[0], context["current_joint_state"], atol=1e-10, rtol=0)
                or not np.allclose(points[-1], request["target"]["joint_positions"], atol=1e-10, rtol=0)):
            return None
        # Final validation uses the live skill and its held-object/contact rules.
        if self.collision_checker.check_path(
            points, resolution=0.01,
            safety_distance=float(request["constraints"]["safety_distance"]),
        ).in_collision:
            return None
        selection.update(prepared.selection)
        selection["prepared_trajectory_reused"] = True
        trajectory["planning_time"] = 0.0
        trajectory.setdefault("diagnostics", {})["prepared_trajectory_reused"] = True
        return trajectory

    def _plan_with_fallback(self, request: Mapping[str, Any], context: Mapping[str, Any], selection: Dict[str, Any], path_request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Always test the direct path before applying a permitted fallback."""
        if "candidate_goals" in path_request:
            return self._plan_joint_candidates(request, context, selection, path_request)
        try:
            trajectory = self.direct_planner.plan(path_request, request, context)
            selection["planning_mode"] = "direct"
            return trajectory

        except CollisionPlanningError as collision_error:
            return self._plan_collision_fallback(request, context, selection, path_request, collision_error)

    def _plan_collision_fallback(self, request, context, selection, path_request, collision_error):
        """Plan around an already checked blocked segment without probing it twice."""
        mode = request["planning"]["mode"]
        hard_constraint = request["motion"]["path_constraint"] == "hard"
        fallback_allowed = mode == "collision_free" or (mode == "auto" and request["planning"]["allow_replan"])
        if hard_constraint and path_request["path_type"] != "joint":
            raise PlanningError(
                "Direct path collides and its hard Cartesian constraint cannot be relaxed.",
                code=ErrorCode.PATH_CONSTRAINT_INFEASIBLE.value, recoverable=True,
                details={"contacts": collision_error.details or []},
            )
        if not fallback_allowed:
            raise collision_error
        trajectory = self.collision_free_planner.plan(path_request, request, context)
        selection["planning_mode"] = "collision_free"
        selection["fallback_used"] = True
        selection["selection_reason"] = selection.get("selection_reason", "") + "; direct path collision triggered RRT-Connect"
        return trajectory

    def _plan_joint_candidates(self, request, context, selection, path_request):
        """Compare feasible paths by joint travel and elevated-arm preference."""
        started = time.perf_counter()
        deadline = started + float(request["planning"]["planning_time"])
        start = np.asarray(path_request["start"], dtype=float)
        def periodic_delta(q):
            delta = np.asarray(q, dtype=float) - start
            return np.arctan2(np.sin(delta), np.cos(delta))
        kinematics = getattr(self, 'kinematics', None)
        posture = preference_for(kinematics)
        goals = sorted(path_request["candidate_goals"], key=lambda q: configuration_cost(kinematics, q, start))
        validator = path_request.get("candidate_validator")
        best = None
        pending = []
        failures = []
        evaluated = 0
        validated = pruned = rejected = 0
        bypass_attempts = 0
        direct_check_time = 0.0
        untested = []
        candidate_timings = []

        def consider(trajectory, candidate_selection):
            nonlocal best
            travel = np.sum(np.abs(np.diff(np.asarray(trajectory["waypoints"]), axis=0)), axis=0)
            penalty = posture.path_penalty(trajectory['waypoints']) if posture is not None else 0.
            score = (float(np.sum(travel)) + penalty, float(np.sum(travel)), float(np.max(travel)), penalty)
            if best is None or score < best[0]:
                best = (score, trajectory, candidate_selection)

        def bypass(entry, budget):
            nonlocal bypass_attempts
            _, path, collision_error = entry
            bounded = deepcopy(request)
            bounded["planning"].update(planning_time=budget, max_attempts=1)
            candidate_selection = dict(selection)
            bypass_attempts += 1
            timing = {'goal': np.asarray(path['goal'], dtype=float).tolist(), 'elapsed_s': 0.0}
            bypass_started = time.perf_counter()
            try:
                consider(self._plan_collision_fallback(
                    bounded, context, candidate_selection, path, collision_error), candidate_selection)
                timing['success'] = True
            except PlanningError as exc:
                failures.append(exc)
                timing.update(success=False, error_code=exc.code)
            finally:
                timing['elapsed_s'] = time.perf_counter() - bypass_started
                timing['budget_s'] = float(budget)
                candidate_timings.append(timing)
            return timing['success']

        # Reject colliding endpoints before spending the shared budget on long
        # segment checks. Such goals cannot be repaired by an RRT detour.
        viable_goals = []
        for goal in goals:
            endpoint_started = time.perf_counter()
            if request.get('constraints', {}).get('avoid_collision'):
                hit = self.collision_checker.check(goal, safety_distance=float(
                    request['constraints'].get('safety_distance', 0.0)),
                    allowed_geom_ids=getattr(self.collision_checker, 'allowed_target_geom_ids', None))
                if hit.in_collision:
                    failures.append(PlanningError('Goal state is in collision.',
                        code='COLLISION_DETECTED', recoverable=True,
                        details={'location': 'goal', 'contacts': list(hit.contacts)}))
                    candidate_timings.append({'goal': np.asarray(goal).tolist(),
                        'endpoint_check_s': time.perf_counter() - endpoint_started,
                        'endpoint_collision': True, 'status': 'endpoint_collision'})
                    continue
            viable_goals.append(goal)
            candidate_timings.append({'goal': np.asarray(goal).tolist(),
                'endpoint_check_s': time.perf_counter() - endpoint_started,
                'endpoint_collision': False, 'status': 'pending'})

        for goal_index, goal in enumerate(viable_goals):
            remaining = deadline - time.perf_counter()
            # One slow direct probe must not starve all known blocked paths of
            # avoidance time. Reserve the remainder when another probe is likely
            # to consume it, rather than finishing every direct branch first.
            if (pending and best is None and
                    remaining <= max(2.0 * direct_check_time,
                                     .5 * float(request['planning']['planning_time']))):
                untested = viable_goals[goal_index:]
                break
            if remaining <= 0:
                untested = viable_goals[goal_index:]
                break
            lower_bound = float(np.sum(np.abs(periodic_delta(goal))))
            if best is not None and lower_bound > best[0][0] + 1e-9:
                pruned += 1
                candidate_timings.append({'goal': np.asarray(goal).tolist(),
                    'status': 'pruned_by_lower_bound', 'lower_bound_cost': lower_bound})
                continue
            # Approach contact validation is expensive. Apply the proven
            # travel lower bound before constructing another contact line.
            if validator is not None:
                validated += 1
                validation_started = time.perf_counter()
                if not validator(goal):
                    rejected += 1
                    candidate_timings.append({'goal': np.asarray(goal).tolist(),
                        'status': 'contact_validation_rejected',
                        'contact_validation_s': time.perf_counter() - validation_started})
                    continue
                candidate_timings.append({'goal': np.asarray(goal).tolist(),
                    'status': 'contact_validated',
                    'contact_validation_s': time.perf_counter() - validation_started})
            path = {key: value for key, value in path_request.items() if key not in {"candidate_goals", "candidate_validator"}}
            path.update(goal=np.asarray(goal), joint_waypoints=np.vstack((start, goal)))
            candidate_selection = dict(selection)
            direct_started = time.perf_counter()
            candidate_timing = {'goal': np.asarray(goal).tolist(), 'status': 'direct_pending'}
            candidate_timings.append(candidate_timing)
            try:
                if request.get('constraints', {}).get('avoid_collision'):
                    # Reject blocked joint segments on the 0.04 rad grid before
                    # the expensive 0.01 rad execution validation. A survivor
                    # still receives the full fine check in DirectPlanner.
                    coarse_started = time.perf_counter()
                    coarse = self.collision_checker.check_path(
                        path['joint_waypoints'], resolution=.04,
                        safety_distance=float(request['constraints'].get('safety_distance', 0.0)),
                        allowed_geom_ids=getattr(self.collision_checker, 'allowed_target_geom_ids', None))
                    candidate_timing['coarse_path_check_s'] = time.perf_counter() - coarse_started
                    if coarse.in_collision:
                        raise CollisionPlanningError('Direct path collides with the robot or environment.',
                                                     details=coarse.contacts)
                    # The endpoint was checked above with the same policy. The
                    # planner still validates the complete path at fine resolution.
                    path['endpoint_collision_prechecked'] = True
                direct_plan_started = time.perf_counter()
                try:
                    trajectory = self.direct_planner.plan(path, request, context)
                finally:
                    candidate_timing['fine_direct_plan_s'] = time.perf_counter() - direct_plan_started
                candidate_timing['direct_success'] = True
                candidate_selection.update(planning_mode="direct", fallback_used=False)
                consider(trajectory, candidate_selection)
            except CollisionPlanningError as exc:
                candidate_timing.update(direct_success=False, direct_collision=True)
                failures.append(exc)
                # Try the nearer blocked goal before finely validating distant
                # branches. A feasible detour supplies an upper bound for pruning.
                candidate_timing['direct_elapsed_s'] = time.perf_counter() - direct_started
                remaining = deadline - time.perf_counter()
                entry = (lower_bound, path, exc)
                if remaining > 0:
                    budget = min(remaining, max(.25, remaining / (len(viable_goals) - goal_index)))
                    if not bypass(entry, budget):
                        pending.append(entry)
                else:
                    pending.append(entry)
            except PlanningError as exc:
                candidate_timing.update(direct_success=False, direct_collision=False,
                                        error_code=exc.code)
                failures.append(exc)
            finally:
                candidate_timing.setdefault('direct_elapsed_s', time.perf_counter() - direct_started)
                direct_check_time = max(direct_check_time, candidate_timing['direct_elapsed_s'])
            evaluated += 1
        for pending_index, entry in enumerate(pending):
            lower_bound = entry[0]
            if best is not None and lower_bound > best[0][0] + 1e-9:
                continue
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            bypass(entry, remaining / (len(pending) - pending_index))
        if best is None:
            # Every failure belongs to an attempted branch. The last branch
            # alone cannot explain failure of the whole configuration search.
            attempts = [{'category': exc.code, 'error': {
                'error_code': exc.code, 'error_message': str(exc), 'details': exc.details}}
                for exc in failures]
            attempts.extend({'category': 'contact_validation_failed'} for _ in range(rejected))
            raise PlanningError('关节构型搜索未找到可用路径',
                code='JOINT_CONFIGURATION_SEARCH_FAILED', recoverable=True,
                details={'candidate_count': len(goals), 'evaluated_count': evaluated,
                         'endpoint_rejected_count': len(goals) - len(viable_goals),
                         'untested_direct_count': len(untested), 'bypass_attempts': bypass_attempts,
                         'planning_time_budget_s': float(request['planning']['planning_time']),
                         'planning_elapsed_s': time.perf_counter() - started,
                         'budget_exhausted': time.perf_counter() >= deadline,
                         'candidate_timings': candidate_timings,
                         'rejected': attempts})
        score, trajectory, chosen = best
        selection.update(chosen)
        trajectory["planning_time"] = time.perf_counter() - started
        trajectory.setdefault("diagnostics", {})["configuration_selection"] = {
            "objective": "joint_travel_plus_arm_posture" if posture is not None else "total_joint_travel_rad",
            "selection_cost_rad": score[0], "total_joint_travel_rad": score[1],
            "maximum_joint_travel_rad": score[2], "arm_posture_penalty_rad": score[3],
            "candidate_count": len(goals),
            "direct_candidates_evaluated": evaluated,
            "endpoint_rejected_count": len(goals) - len(viable_goals),
            "untested_direct_count": len(untested), "bypass_attempts": bypass_attempts,
            "contact_candidates_validated": validated, "contact_candidates_rejected": rejected,
            "candidates_pruned_by_lower_bound": pruned,
            "budget_exhausted": time.perf_counter() >= deadline,
            "candidate_timings": candidate_timings,
        }
        return trajectory
    def _execute_trajectory(self, trajectory: Mapping[str, Any], request: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
        if not context["controller_state"].get("ready"):
            raise MoveExecutionError(
                "MuJoCo controller is not ready.", code=ErrorCode.CONTROLLER_NOT_READY.value,
                recoverable=True, recommended_action="Initialize or enable the controller, clear faults, and retry.",
            )
        execution_trajectory = dict(trajectory)
        execution_trajectory['goal_tolerances'] = {
            'position_tolerance': float(request['constraints']['position_tolerance']),
            'orientation_tolerance': np.deg2rad(float(request['constraints']['orientation_tolerance'])),
        }
        result = self.robot_runtime.execute_trajectory(execution_trajectory, timeout=float(request["constraints"]["timeout"]))
        if result.get('grasp_error'):
            return result
        if result["joint_error"] > 0.02:
            raise MoveExecutionError(
                f"Final joint tracking error {result['joint_error']:.6f} exceeds tolerance.",
                code=ErrorCode.GOAL_TOLERANCE_EXCEEDED.value, recoverable=True,
                recommended_action="Retry at a lower speed or inspect controller tracking and goal tolerance.",
            )
        return result

    @staticmethod
    def _planning_recommendation(code: str) -> str:
        return {
            ErrorCode.PATH_CONSTRAINT_INFEASIBLE.value: "Allow path changes, change the start/target pose, or relax the path/orientation constraint.",
            ErrorCode.JOINT_LIMIT_VIOLATION.value: "Choose joint values within the reported limits or use another reachable pose.",
            ErrorCode.COLLISION_DETECTED.value: "Change the start/goal state or allow collision-free replanning.",
            ErrorCode.PATH_NOT_FOUND.value: "Increase planning time, relax constraints, or change the start/target pose.",
            ErrorCode.TRAJECTORY_INVALID.value: "Replan after refreshing the scene and collision state.",
            ErrorCode.INVALID_ARC_DEFINITION.value: "Correct the arc center, axis, angle, or via point and retry.",
            ErrorCode.TARGET_UNREACHABLE.value: "Choose a defined named state or another reachable target.",
        }.get(str(code), "Inspect the planning diagnostics and adjust the target or constraints.")


class MoveExecutionError(RuntimeError):
    def __init__(self, message: str, *, code: str, recoverable: bool, recommended_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.recommended_action = recommended_action


def run(request: Mapping[str, Any], *, scene_path: str | Path = DEFAULT_SCENE) -> Dict[str, Any]:
    """Convenience entrypoint used by agents and simple scripts."""
    return _MoveExecutor(scene_path=scene_path).execute(request)
# Backward-compatible aliases for callers still using the former Skill names.
