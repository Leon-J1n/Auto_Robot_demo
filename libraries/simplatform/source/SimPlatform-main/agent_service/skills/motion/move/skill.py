"""Executable Move Skill for a RobotSimPlatform UR5e simulation."""

from __future__ import annotations

import copy
import re
from contextlib import nullcontext
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills.motion.move.planners import CollisionFreePlanner, CollisionPlanningError, DirectPlanner, PlanningError
from planing import TrajectoryProcessor
from skills.motion.move.selector import StrategySelectionError, StrategySelector
from skills.motion.move.strategies import CircularMove, JointMove, LinearMove
from skills.motion.move.utils.error_codes import ErrorCode, failure_result
from skills.motion.move.utils.validation import MoveValidationError, validate_move_request
from skills.platform_runtime import (
    IKError,
    PlatformCollisionChecker,
    PlatformKinematics,
    SceneRobotRuntime,
    rotation_mapping,
)
from sim_platform._logging import log_verbose, info as log_info


DEFAULT_SCENE = PROJECT_ROOT / "tmp_state" / "world_with_objects.simstate"

# What a candidate is called when it is offered to a person. These are display names, not
# strategy ids: a reader choosing between paths wants to know the shape of the motion, and
# "circle" says that more plainly than the internal "circular".
CANDIDATE_LABELS = {"joint_move": "joint_move", "circular_move": "circle_move",
                    "linear_move": "linear_move"}

# The request vocabulary for the same three strategies, which is what the selector reads.
_PATH_TYPES = {"joint_move": "joint", "circular_move": "circular", "linear_move": "linear"}


@dataclass
class MoveCandidate:
    """One planned, collision-checked trajectory that a caller may choose to execute.

    Holds the trajectory itself rather than the recipe for producing it, so executing a
    candidate runs the path that was shown and measured. Replanning at execution time
    would be free to return something else -- IK is seeded from the live pose and RRT is
    randomised -- which would make the choice a suggestion rather than a decision.
    """

    number: int
    label: str
    strategy: str
    planning_mode: str
    trajectory: Mapping[str, Any]
    selection: Mapping[str, Any]
    request: Mapping[str, Any]

    @property
    def waypoint_count(self) -> int:
        return len(self.trajectory.get("waypoints", ()))

    @property
    def used_rrt(self) -> bool:
        return self.planning_mode == "collision_free"


class MoveSkill:
    """Validate, select, plan, collision-check, and execute one motion request."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, robot_runtime: SceneRobotRuntime | None = None, scene_path: str | Path = DEFAULT_SCENE, trajectory_callback: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], None] | None = None) -> None:
        log_verbose("MoveSkill.__init__ starts planner and runtime setup")
        self.config: Mapping[str, Any] = config or _load_default_config()
        self.robot_runtime = robot_runtime or SceneRobotRuntime(scene_path)
        self.kinematics = PlatformKinematics(self.robot_runtime)
        self.collision_checker = PlatformCollisionChecker(self.robot_runtime)
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
        log_verbose("MoveSkill.__init__ completed")

    def execute(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        """Run the complete Move pipeline and always return a structured result."""
        log_verbose("MoveSkill.execute starts complete motion pipeline")
        state: Dict[str, Any] = {}
        timing: Dict[str, float] = {}
        started = time.perf_counter()

        def action() -> Dict[str, Any]:
            stage = time.perf_counter()
            normalized = validate_move_request(request, self.config)
            timing["validation"] = time.perf_counter() - stage
            stage = time.perf_counter()
            # Keep the target in the collision scene during transit and approach.
            # The compiled-command adapter may pre-populate this with colliders of an
            # object already held by the gripper.  Do not erase that exemption before
            # planning a post-grasp lift or retreat.
            if not hasattr(self.collision_checker, "allowed_target_geom_ids"):
                self.collision_checker.allowed_target_geom_ids = set()
            context = self._get_runtime_context(normalized)
            state["runtime"] = self.robot_runtime
            timing["context"] = time.perf_counter() - stage
            stage = time.perf_counter()
            selection = state["selection"] = self.selector.select(normalized, context)
            timing["selection"] = time.perf_counter() - stage
            strategy = self.strategies[selection["path_strategy"]]
            stage = time.perf_counter()
            path_request = strategy.build_path_request(normalized, context)
            timing["path_generation"] = time.perf_counter() - stage
            stage = time.perf_counter()
            trajectory = self._plan_with_fallback(normalized, context, selection, path_request)
            timing["planning"] = time.perf_counter() - stage
            stage = time.perf_counter()
            trajectory = self._process_trajectory(trajectory, normalized)
            timing["trajectory_processing"] = time.perf_counter() - stage
            if self.trajectory_callback is not None:
                self.trajectory_callback(trajectory, normalized, selection)
            stage = time.perf_counter()
            execution_result = self._execute_trajectory(trajectory, normalized, context)
            timing["execution"] = time.perf_counter() - stage
            execution_result["planning_time"] = trajectory.get("planning_time", 0.0)
            execution_result["planned_duration"] = trajectory.get("duration", 0.0)
            timing["total"] = time.perf_counter() - started
            return {"success": True, "status": "completed", "selection": selection,
                    "execution_result": execution_result, "trajectory": trajectory,
                    "timing": timing, "motion_time": execution_result.get("execution_time", 0.0),
                    "error": None}

        result = self._guarded(action, state)
        timing["total"] = time.perf_counter() - started
        result["timing"] = timing
        log_info("Perf", f"[Perf] Name=Trajectory.MoveExecute DurationMs={timing['total'] * 1000.0:.3f}")
        log_verbose("MoveSkill.execute returns structured result")
        return result

    def _process_trajectory(self, trajectory: Mapping[str, Any], request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Apply the new trajectory smoothing/retiming stage using platform adapters."""
        log_verbose("MoveSkill._process_trajectory starts smoothing and retiming")
        started = time.perf_counter()
        try:
            result = TrajectoryProcessor(
                kinematics=self.kinematics,
                collision_checker=self.collision_checker,
                joint_limits=self.robot_runtime.joint_limits,
                config=self.config.get("trajectory", {}),
            ).process(trajectory, request)
            log_info("Perf", f"[Perf] Name=Trajectory.Process DurationMs={(time.perf_counter() - started) * 1000.0:.3f}")
            log_verbose("MoveSkill._process_trajectory returns processed path")
            return result
        except ValueError as exc:
            raise PlanningError(str(exc), code=ErrorCode.TRAJECTORY_INVALID.value, recoverable=True) from exc

    def _prepare(self, request: Mapping[str, Any]) -> tuple[Dict[str, Any], Mapping[str, Any]]:
        """Validate a request and read the world state it will be planned against."""
        log_verbose("MoveSkill._prepare validates request and reads context")
        normalized = validate_move_request(request, self.config)
        # The target remains a real obstacle while the gripper approaches it. The
        # fine mesh and open gripper geometry make a valid grasp fit naturally; hiding
        # the target from collision checks can instead permit the arm to knock it over.
        if not hasattr(self.collision_checker, "allowed_target_geom_ids"):
            self.collision_checker.allowed_target_geom_ids = set()
        return normalized, self._get_runtime_context(normalized)

    def _guarded(self, action: Callable[[], Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
        """Run one pipeline stage, turning every known failure into a structured result.

        Shared by execute() and plan_candidates() so both report a failure the same way,
        with the same codes and recommended actions. `state` carries whatever the action
        established before failing -- the strategy selection, most usefully -- so a failure
        result can still say what was being attempted.
        """
        log_verbose("MoveSkill._guarded enters protected action")
        try:
            scope = getattr(self.collision_checker, "planning_scope", nullcontext)
            with scope():
                return action()
        except MoveValidationError as exc:
            result = failure_result(ErrorCode.INVALID_REQUEST, str(exc), failed_stage="validation", recoverable=True, recommended_action="Correct the request fields and retry.")
        except StrategySelectionError as exc:
            result = failure_result(ErrorCode.UNKNOWN_STRATEGY, str(exc), failed_stage="selection", recoverable=True)
        except IKError as exc:
            result = failure_result(ErrorCode.IK_FAILED, str(exc), failed_stage="kinematics", recoverable=True, recommended_action="Try TRAC-IK, another seed, or a reachable target pose.")
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
            result = failure_result(ErrorCode.EXECUTION_TIMEOUT, str(exc), failed_stage="execution", recoverable=True, status="timeout")
        except ValueError as exc:
            result = failure_result(ErrorCode.PLANNING_FAILED, str(exc), failed_stage="path_generation", recoverable=True)
        except RuntimeError as exc:
            limit_error = _joint_limit_execution_error(exc, state.get("runtime"))
            if limit_error is not None:
                result = failure_result(
                    ErrorCode.JOINT_LIMIT_VIOLATION, limit_error["message"],
                    failed_stage="execution", recoverable=True,
                    recommended_action="Keep every commanded robot and gripper joint within its reported limits, then retry.",
                    details=limit_error["details"],
                )
            else:
                result = failure_result(ErrorCode.EXECUTION_FAILED, str(exc), failed_stage="execution", recoverable=True)
        except Exception as exc:
            result = failure_result(ErrorCode.INTERNAL_ERROR, str(exc), failed_stage="unknown", recoverable=False)
        if state.get("selection") is not None:
            result["selection"] = state["selection"]
        log_verbose(f"MoveSkill._guarded returns success={result.get('success')} status={result.get('status')}")
        return result

    def _get_runtime_context(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Delegate scene and robot-state acquisition to the root utils module."""
        return self.robot_runtime.get_context(request)

    def _plan_with_fallback(self, request: Mapping[str, Any], context: Mapping[str, Any], selection: Dict[str, Any], path_request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Always test the direct path before applying a permitted fallback."""
        try:
            trajectory = self.direct_planner.plan(path_request, request, context)
            selection["planning_mode"] = "direct"
            return trajectory
        except CollisionPlanningError as collision_error:
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
                raise
            trajectory = self.collision_free_planner.plan(path_request, request, context)
            selection["planning_mode"] = "collision_free"
            selection["fallback_used"] = True
            selection["selection_reason"] += "; direct path collision triggered RRT-Connect"
            return trajectory

    def plan_candidates(self, request: Mapping[str, Any], *, max_linear: int = 1,
                        max_candidates: int = 8) -> Dict[str, Any]:
        """Enumerate the trajectories that reach the target, without executing any.

        One target admits several genuinely different motions: a joint interpolation, an arc
        that lifts over the straight line, and one Cartesian path per IK branch. Which is
        wanted depends on things the request does not say -- what the gripper is carrying,
        what the motion has to stay clear of -- so all of them are offered and the choice is
        left to the caller.

        Nothing is stepped, so the world is where it was when this returns and every
        candidate stays valid to execute.

        `max_linear` defaults to one because LinearMove's branches differ only in posture:
        each interpolates the same straight line and solves IK along it, so every branch
        puts the gripper through identical points and they would draw as one line in
        several colours. Raise it to offer a choice of postures along that line -- the
        elbow, not the hand, is what would differ.
        """
        state: Dict[str, Any] = {}

        def action() -> Dict[str, Any]:
            normalized, context = self._prepare(request)
            # The Cartesian strategies need a destination in space. A joint or named-state
            # target names one only implicitly, so FK converts it: `move home` can then be
            # offered as an arc or a straight line too, not only as the joint interpolation
            # that happens to be how it was written.
            cartesian = self._as_pose_request(normalized, context)
            candidates: List[MoveCandidate] = []
            rejected: List[Dict[str, Any]] = []
            state["rejected"] = rejected
            collided: List[tuple[str, Any, Any, Dict[str, Any]]] = []
            for strategy_id, source, limit in (("joint_move", normalized, 1),
                                               ("circular_move", cartesian, 1),
                                               ("linear_move", cartesian, max_linear)):
                self._collect(strategy_id, source, context, limit,
                              candidates, rejected, collided)
            if not candidates:
                # Every direct path failed or collides, which leaves RRT. It is tried only
                # here because it is much the most expensive planner: running it alongside
                # paths that were already clear would spend that cost to produce another
                # way of drawing a curve the caller can already see.
                for entry in collided:
                    self._collect_rrt(*entry, context, candidates, rejected)
            if not candidates:
                raise PlanningError(
                    "No strategy could reach the target.",
                    code=ErrorCode.PATH_NOT_FOUND.value, recoverable=True,
                    details={"rejected": rejected})
            del candidates[max_candidates:]
            for number, candidate in enumerate(candidates, start=1):
                candidate.number = number
            return {"success": True, "status": "planned", "candidates": candidates,
                    "rejected": rejected, "error": None}

        result = self._guarded(action, state)
        result.setdefault("candidates", [])
        result.setdefault("rejected", state.get("rejected", []))
        return result

    def execute_candidate(self, candidate: MoveCandidate) -> Dict[str, Any]:
        """Execute a trajectory that plan_candidates() returned.

        Runs the stored waypoints instead of replanning, so what executes is the path that
        was shown and chosen. Replanning here would be free to return something else -- IK
        is seeded from the live pose and RRT is randomised -- which would quietly turn the
        caller's decision back into a suggestion. Everything after that is execute()'s own
        tail, tracking-error check included.
        """
        state: Dict[str, Any] = {"selection": candidate.selection}

        def action() -> Dict[str, Any]:
            context = self._get_runtime_context(candidate.request)
            # plan_candidates() already time-parameterizes selectable candidates so the
            # duration shown to the user is the duration that will execute. Keep a small
            # compatibility path for callers constructing legacy candidates by hand.
            trajectory = (candidate.trajectory if "time_from_start" in candidate.trajectory
                          else self._process_trajectory(candidate.trajectory, candidate.request))
            if self.trajectory_callback is not None:
                self.trajectory_callback(trajectory, candidate.request, candidate.selection)
            execution_result = self._execute_trajectory(trajectory, candidate.request, context)
            execution_result["planning_time"] = trajectory.get("planning_time", 0.0)
            execution_result["planned_duration"] = trajectory.get("duration", 0.0)
            return {"success": True, "status": "completed",
                    "selection": dict(candidate.selection), "execution_result": execution_result,
                    "trajectory": trajectory, "error": None}

        return self._guarded(action, state)

    def _collect(self, strategy_id: str, source: Mapping[str, Any],
                 context: Mapping[str, Any], limit: int,
                 candidates: List[MoveCandidate], rejected: List[Dict[str, Any]],
                 collided: List[tuple]) -> None:
        """Add one strategy's direct-path candidates, recording why any were dropped.

        A strategy that cannot serve this target is not a failure of the enumeration:
        CircularMove has no arc through coincident points, and IK may have no branch for a
        pose. Its complaint is recorded and the next strategy is tried. Only a target that
        no strategy can reach is an error, which plan_candidates raises itself.
        """
        request = self._with_path_type(source, strategy_id)
        strategy = self.strategies[strategy_id]
        # Timed from here so a rejection can report what the attempt cost. A strategy that
        # is turned down is not free -- building a Cartesian chain and finding it blocked is
        # often the most expensive thing enumeration does -- and a timing log that shows
        # only the paths that succeeded leaves that time unaccounted for.
        started = time.perf_counter()
        try:
            selection = self.selector.select(request, context)
            if hasattr(strategy, "build_path_requests"):
                path_requests = strategy.build_path_requests(request, context,
                                                             max_candidates=limit)
            else:
                path_requests = [strategy.build_path_request(request, context)]
        except (PlanningError, StrategySelectionError, IKError, ValueError) as exc:
            rejected.append({"strategy": strategy_id, "reason": str(exc),
                             "planning_time": time.perf_counter() - started})
            return
        for path_request in path_requests:
            attempt = dict(selection)
            try:
                trajectory = self.direct_planner.plan(path_request, request, context)
            except CollisionPlanningError as exc:
                rejected.append({"strategy": strategy_id, "reason": str(exc),
                                 "collision": True,
                                 "planning_time": time.perf_counter() - started})
                # One collided path per strategy is enough to retry through RRT. Further
                # branches of the same strategy go around the same obstacle.
                if not any(entry[0] == strategy_id for entry in collided):
                    collided.append((strategy_id, request, path_request, attempt))
                continue
            except (PlanningError, IKError, ValueError) as exc:
                rejected.append({"strategy": strategy_id, "reason": str(exc),
                                 "planning_time": time.perf_counter() - started})
                continue
            try:
                trajectory = self._process_trajectory(trajectory, request)
            except (PlanningError, IKError, ValueError) as exc:
                rejected.append({"strategy": strategy_id, "reason": str(exc),
                                 "planning_time": time.perf_counter() - started})
                continue
            attempt["planning_mode"] = "direct"
            candidates.append(self._candidate(strategy_id, attempt, trajectory, request))
            # The next branch's build cost is its own, not this one's.
            started = time.perf_counter()

    def _collect_rrt(self, strategy_id: str, request: Mapping[str, Any],
                     path_request: Mapping[str, Any], selection: Dict[str, Any],
                     context: Mapping[str, Any], candidates: List[MoveCandidate],
                     rejected: List[Dict[str, Any]]) -> None:
        """Retry one collided path through RRT-Connect, if the request permits it.

        The permission checks mirror _plan_with_fallback exactly: a request that pinned
        planning to `direct` gets no RRT candidate offered, and neither does a hard
        Cartesian constraint, whose whole point is that the path may not be rerouted.
        Offering one anyway would present a motion the caller has already ruled out.
        """
        mode = request["planning"]["mode"]
        if mode == "direct" or (mode == "auto" and not request["planning"]["allow_replan"]):
            return
        if request["motion"]["path_constraint"] == "hard" and path_request["path_type"] != "joint":
            return
        started = time.perf_counter()
        try:
            trajectory = self.collision_free_planner.plan(path_request, request, context)
        except (PlanningError, IKError, ValueError) as exc:
            rejected.append({"strategy": strategy_id, "reason": str(exc), "rrt": True,
                             "planning_time": time.perf_counter() - started})
            return
        try:
            trajectory = self._process_trajectory(trajectory, request)
        except (PlanningError, IKError, ValueError) as exc:
            rejected.append({"strategy": strategy_id, "reason": str(exc),
                             "rrt": True,
                             "planning_time": time.perf_counter() - started})
            return
        selection["planning_mode"] = "collision_free"
        selection["fallback_used"] = True
        selection["selection_reason"] += "; direct path collision triggered RRT-Connect"
        candidates.append(self._candidate(strategy_id, selection, trajectory, request))

    def _candidate(self, strategy_id: str, selection: Dict[str, Any],
                   trajectory: Mapping[str, Any],
                   request: Mapping[str, Any]) -> MoveCandidate:
        """Wrap a planned trajectory, labelling it by shape and by how it was planned."""
        label = CANDIDATE_LABELS.get(strategy_id, strategy_id)
        planning_mode = str(selection.get("planning_mode", "direct"))
        if planning_mode == "collision_free":
            # A rerouted path no longer follows the shape its strategy names, so the label
            # says so: the arm will not travel the straight line that `linear_move` alone
            # would promise.
            label += ",RRT"
        return MoveCandidate(number=0, label=label, strategy=strategy_id,
                             planning_mode=planning_mode, trajectory=trajectory,
                             selection=selection, request=request)

    @staticmethod
    def _with_path_type(request: Mapping[str, Any], strategy_id: str) -> Dict[str, Any]:
        """A copy of the request that names one path type explicitly.

        The selector is asked rather than told which strategy to use, even though the caller
        already knows: it fills in the planning mode, the optimisation goal's effect, and
        the reason text, and a hand-built selection dict would drift from whatever it
        decides next.
        """
        adapted = copy.deepcopy(dict(request))
        adapted["motion"]["path_type"] = _PATH_TYPES[strategy_id]
        return adapted

    def _as_pose_request(self, request: Mapping[str, Any],
                         context: Mapping[str, Any]) -> Dict[str, Any]:
        """The same destination expressed as a pose, for the Cartesian strategies.

        FK on the goal configuration gives both position and orientation, so the arm ends
        up where the joint target put it and facing the same way -- an arc to `home` reaches
        the same physical pose as the joint interpolation, by a different route.
        """
        if request["target"]["type"] == "pose":
            return copy.deepcopy(dict(request))
        goal = self.strategies["joint_move"].build_path_request(request, context)["goal"]
        pose = self.kinematics.forward(goal)
        converted = copy.deepcopy(dict(request))
        converted["target"] = {
            "type": "pose", "frame": "world",
            "position": dict(zip(("x", "y", "z"), (float(v) for v in pose["position"]))),
            "orientation": rotation_mapping(pose["rotation_matrix"]),
        }
        # Arc geometry that came with a joint target was never checked against this pose and
        # would not reach it. A via point is the caller's own, so it stays.
        for key in ("arc_center", "arc_axis", "arc_angle"):
            converted["strategy_params"].pop(key, None)
        return converted

    def _execute_trajectory(self, trajectory: Mapping[str, Any], request: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
        end_planning = getattr(self.collision_checker, "end_planning", None)
        if end_planning is not None:
            end_planning()
        if not context["controller_state"].get("ready"):
            raise MoveExecutionError(
                "Simulation controller is not ready.", code=ErrorCode.CONTROLLER_NOT_READY.value,
                recoverable=True, recommended_action="Initialize or enable the controller, clear faults, and retry.",
            )
        requested_timeout = float(request["constraints"]["timeout"])
        # Execution is measured in wall-clock time while the trajectory duration is
        # simulated time.  Include the planned duration and a small RPC/snapshot margin
        # so a correctly retimed path is not rejected merely for being longer than the
        # legacy fixed timeout default.
        planned_duration = max(float(trajectory.get("duration", 0.0)), 0.0)
        timeout = max(requested_timeout, planned_duration + 5.0)
        result = self.robot_runtime.execute_trajectory(trajectory, timeout=timeout)
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


_JOINT_LIMIT_MESSAGE = re.compile(r"joint target violates limits:\s*(?P<name>[^\s,;]+)", re.IGNORECASE)


def _joint_limit_execution_error(error: BaseException, runtime: Any) -> dict[str, Any] | None:
    """Turn a platform limit exception into a stable Move error with joint details."""
    match = _JOINT_LIMIT_MESSAGE.search(str(error))
    if match is None:
        return None
    name = match.group("name")
    details: dict[str, Any] = {"joint_name": name, "source_error": str(error)}
    names = tuple(getattr(runtime, "platform_joint_names", ())) if runtime is not None else ()
    limits = getattr(runtime, "all_joint_limits", None) if runtime is not None else None
    positions = None
    if runtime is not None:
        try:
            positions = runtime.full_joint_positions().tolist()
        except Exception:
            positions = None
    if name in names and limits is not None:
        index = names.index(name)
        details.update({"joint_index": index,
                        "limits": [float(v) for v in limits[index]],
                        "actual_position": None if positions is None else float(positions[index]),
                        "joint_group": "gripper" if name.startswith("robotiq_2f85_") else "robot"})
    return {"message": f"Joint '{name}' violates its configured position limits.", "details": details}


def run(request: Mapping[str, Any], *, scene_path: str | Path = DEFAULT_SCENE) -> Dict[str, Any]:
    """Convenience entrypoint used by agents and simple scripts."""
    return MoveSkill(scene_path=scene_path).execute(request)


def _load_default_config() -> Mapping[str, Any]:
    """Load the checked-in move defaults when callers do not supply an override."""
    path = Path(__file__).with_name("config.yaml")
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else {}
    except (ImportError, OSError, ValueError):
        return {}
