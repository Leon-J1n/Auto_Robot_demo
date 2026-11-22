"""Drive a compiled command sequence against the live world, returning plain data.

This is the seam between the compiled plan and the deterministic runtime. It takes a
command document -- the real `CommandDocument` or any duck-typed stand-in -- dispatches
each command to the runtime services in this package, and returns an
`ExecutionOutcome` carrying snapshot frames, per-segment trajectory markers, and a
step report per command.

Three things it deliberately does not do. It does not draw: trajectory markers come
back as data and the publisher injects them into frames. It does not import from
`agent_service.compile` or `agent_service.scene`, which would be a cycle; command
documents and target resolution arrive duck-typed instead. And it does not fake
missing hardware: a force-mode press and an articulated push/pull fail with a
structured code rather than returning a plausible number.

Failure is fail-fast. The first failed command stops the run, but the frames and
segments captured up to that point are still returned, so a caller can see how far the
robot actually got.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import numpy as np

from agent_service.skills import (COMMAND_SKILLS, CommandExecutionContext,
                                  SkillCommandFailure)

from .collision import LiveCollisionChecker
from .gripper import LiveGripperController
from .kinematics import IKError, LiveKinematics
from .live_runtime import resample_for_playback
from .planners import CollisionFreePlanner, DirectPlanner, PlanningError
from .trajectory import TrajectoryMarkers, segment_markers
from .transforms import quaternion_mapping, serializable_pose

try:  # The protocol package is optional so this module imports standalone.
    from agent_service.protocol.errors import ErrorCode as _ErrorCode

    def _code(name: str) -> str:
        return str(getattr(_ErrorCode, name))
except ImportError:  # pragma: no cover - exercised only without the protocol package
    def _code(name: str) -> str:
        return name


COMPILE_TIME_SKILLS = frozenset({"locate"})
EXECUTABLE_SKILLS = COMMAND_SKILLS.names - COMPILE_TIME_SKILLS
# `locate` is resolved deterministically at compile time. It still gets a step report
# so the trace chain skill_step_id -> command_id -> runtime_step_id stays unbroken.
DEFAULT_MAX_FRAMES = None
CAPTURE_FPS = 50.0
LOGGER = logging.getLogger(__name__)


@dataclass
class ExecutionOutcome:
    """Everything one run produced, as plain data."""

    success: bool = True
    frames: list[dict[str, Any]] = field(default_factory=list)
    segments: list[TrajectoryMarkers] = field(default_factory=list)
    affected_objects: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    commands_total: int = 0
    commands_started: int = 0
    commands_completed: int = 0
    frames_captured: int = 0
    frame_budget_exhausted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "frames": list(self.frames),
            "segments": [item.as_dict() for item in self.segments],
            "affected_objects": list(self.affected_objects),
            "steps": list(self.steps),
            "failure": None if self.failure is None else dict(self.failure),
            "commands_total": self.commands_total,
            "commands_started": self.commands_started,
            "commands_completed": self.commands_completed,
            "frames_captured": self.frames_captured,
            "frame_budget_exhausted": self.frame_budget_exhausted,
        }


class CommandFailure(RuntimeError):
    """A command failed with a structured error code."""

    def __init__(self, error_code: str, message: str, *, recoverable: bool = False,
                 details: Any = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.recoverable = recoverable
        self.details = details


class _FrameCapture:
    """Snapshot capture sampled for smooth playback across the complete run.

    Production capture is intentionally unbounded by frame count: truncating at 600
    frames made a 50 FPS timeline stop near 12 seconds. Tests and callers may still
    provide an explicit budget when they need one.
    """

    def __init__(self, runtime: Any, *, max_frames: int | None, enabled: bool) -> None:
        self.runtime = runtime
        self.max_frames = None if max_frames is None else max(int(max_frames), 0)
        self.enabled = bool(enabled) and (self.max_frames is None or self.max_frames > 0)
        self.frames: list[dict[str, Any]] = []
        self.exhausted = False
        self._last_sample_time: float | None = None
        self._base_snapshot: dict[str, Any] | None = None

    def _dynamic_frame(self) -> dict[str, Any]:
        """Merge compact transform data with the one static scene snapshot."""
        read_transforms = getattr(self.runtime.session, "scene_transforms", None)
        if not callable(read_transforms):
            return self.runtime.session.scene_snapshot().as_dict()
        if self._base_snapshot is None:
            self._base_snapshot = self.runtime.session.scene_snapshot().as_dict()
            return self._base_snapshot
        compact = read_transforms()
        if (compact.get("sceneGeneration") != self._base_snapshot.get("sceneGeneration")
                or {item["id"] for item in compact["entities"]}
                != {item["id"] for item in self._base_snapshot["entities"]}):
            raise RuntimeError("Scene structure changed during timeline capture")
        by_id = {str(item.get("id")): item for item in compact.get("entities", [])}
        entities = []
        for original in self._base_snapshot.get("entities", []):
            value = dict(original)
            update = by_id.get(str(original.get("id")))
            if update:
                for key in ("parent", "local", "world"):
                    if key in update:
                        value[key] = update[key]
            entities.append(value)
        return {**self._base_snapshot, **{
            key: compact[key] for key in ("revision", "simulationTime", "status")
            if key in compact}, "entities": entities}

    @property
    def remaining(self) -> int:
        if self.max_frames is None:
            return 2**63 - 1
        return max(self.max_frames - len(self.frames), 0)

    def capture(self) -> bool:
        """Take one snapshot, unless capture is off or the budget is spent."""
        if not self.enabled:
            return False
        if self.remaining <= 0:
            self.exhausted = True
            return False
        snapshot = self._dynamic_frame()
        if self.frames and snapshot == self.frames[-1]:
            LOGGER.debug("timeline capture skipped an unchanged duplicate frame")
            return False
        self.frames.append(snapshot)
        self._last_sample_time = float(snapshot.get("simulationTime", 0.0))
        return True

    def capture_at_fps(self, fps: float = CAPTURE_FPS) -> bool:
        """Capture a snapshot only after one playback interval has elapsed."""
        if not self.enabled or self.remaining <= 0:
            return False
        current_time = getattr(self.runtime, "_simulation_time", None)
        if current_time is None:
            current_time = getattr(self.runtime.session, "simulation_time", None)
        if self.frames and current_time is not None and self._last_sample_time is not None:
            if (getattr(self.runtime, "execution_mode", None) != "kinematic"
                    and float(current_time) - self._last_sample_time < 1.0 / max(float(fps), 1e-9) - 1e-9):
                return False
        snapshot = self._dynamic_frame()
        if self.remaining <= 0:
            self.exhausted = True
            return False
        if self.frames and snapshot == self.frames[-1]:
            return False
        self.frames.append(snapshot)
        self._last_sample_time = float(snapshot.get("simulationTime", current_time or 0.0))
        return True

    def stride(self, waypoint_count: int, *, duration: float = 0.0,
               motions_remaining: int = 0) -> int:
        """Choose a stride that samples motion at no less than 50 FPS.

        A waypoint is one simulator step for the live trajectory path. Using the
        planned duration also keeps this correct for paths whose waypoint count is
        generated at a different resolution.
        """
        if not self.enabled or waypoint_count <= 0:
            return max(waypoint_count, 1)
        expected_frames = max(1, int(np.ceil(max(float(duration), 0.0) * CAPTURE_FPS)))
        stride = max(1, int(np.ceil(max(waypoint_count - 1, 1) / expected_frames)))
        LOGGER.debug(
            "timeline capture sampling: waypoints=%d duration=%.6f fps=%.1f stride=%d",
            waypoint_count, duration, CAPTURE_FPS, stride)
        return stride


def execute_commands(runtime: Any, commands: Any, *,
                     max_frames: int | None = DEFAULT_MAX_FRAMES, capture: bool = True,
                     kinematics: Any = None, collision_checker: Any = None,
                     gripper: Any = None,
                     resolve_target: Callable[[str, str | None], Any] | None = None,
                     planning_time: float = 5.0, max_attempts: int = 3,
                     velocity_scale: float = 1.0, safety_distance: float = 0.0,
                     observer: Callable[[dict[str, Any]], None] | None = None,
                     ) -> ExecutionOutcome:
    """Run a compiled command sequence and report what happened.

    `commands` is a `CommandDocument`-like object with a `.commands` sequence, or a
    plain mapping with a `"commands"` key. Each command carries `command_id`,
    `source_skill_step_id`, `skill_name` and `parameters`.

    The runtime services are built here unless a caller injects them, which is what
    makes this testable without a simulator: pass fakes for `kinematics`,
    `collision_checker` and `gripper` and no service is constructed.
    """
    items = _command_list(commands)
    execution_started = time.perf_counter()
    LOGGER.debug("execute_commands started command_count=%d max_frames=%s",
                 len(items), max_frames)
    outcome = ExecutionOutcome(commands_total=len(items))
    services = _Services(runtime, kinematics, collision_checker, gripper,
                         resolve_target=resolve_target, planning_time=planning_time,
                         max_attempts=max_attempts, velocity_scale=velocity_scale,
                         safety_distance=safety_distance)
    frames = _FrameCapture(runtime, max_frames=max_frames, enabled=capture)
    frames.capture()

    motions_remaining = sum(1 for item in items if _skill_of(item) == "move")
    touched: list[str] = []
    for command in items:
        skill = _skill_of(command)
        target = _target_of(command)
        started = time.perf_counter()
        LOGGER.debug("command started index=%d/%d skill=%s target=%s command_id=%s",
                     outcome.commands_started + 1, outcome.commands_total, skill, target,
                     _attribute(command, "command_id"))
        started_at = datetime.now(timezone.utc)
        outcome.commands_started += 1
        if observer is not None:
            observer({
                "type": "command", "status": "in_progress",
                "commandId": str(_attribute(command, "command_id") or ""),
                "stepId": str(_attribute(command, "source_skill_step_id") or ""),
                "skill": skill, "target": target,
                "current": outcome.commands_started, "total": outcome.commands_total,
            })
        if skill == "move":
            motions_remaining = max(motions_remaining - 1, 0)
        try:
            if skill not in COMMAND_SKILLS.names:
                raise CommandFailure(_code("UNSUPPORTED_SKILL"),
                                     f"Runtime cannot execute skill {skill!r}.")
            else:
                context = CommandExecutionContext(
                    skill_name=skill,
                    services=services,
                    command=command,
                    frames=frames,
                    outcome=outcome,
                    motions_remaining=motions_remaining,
                    invoke_runtime=_dispatch_registered_skill,
                )
                previous_gripper_observer = None
                if skill in {"grasp", "release"}:
                    previous_gripper_observer = getattr(services.gripper, "step_observer", None)
                    services.gripper.step_observer = (
                        lambda: frames.capture_at_fps(CAPTURE_FPS))
                try:
                    result = COMMAND_SKILLS.execute(context)
                finally:
                    if skill in {"grasp", "release"}:
                        services.gripper.step_observer = previous_gripper_observer
        except Exception as error:
            LOGGER.exception("command execution raised skill=%s target=%s", skill, target)
            if isinstance(error, (CommandFailure, SkillCommandFailure)):
                failure = error
            else:
                failure = CommandFailure(
                    _code("EXECUTION_FAILED"),
                    f"Skill adapter {skill!r} failed: {error}",
                    recoverable=False,
                    details={"exception_type": type(error).__name__},
                )
            outcome.steps.append(_step_report(
                command, skill, target, False, started, started_at,
                {"error_code": failure.error_code, "error_message": str(failure),
                 "details": failure.details,
                 "parameters": dict(_parameters_of(command))}))
            outcome.success = False
            outcome.failure = {
                "command_id": _attribute(command, "command_id"),
                "source_skill_step_id": _attribute(command, "source_skill_step_id"),
                "runtime_step_id": outcome.steps[-1]["runtime_step_id"],
                "skill_name": skill,
                "target": target,
                "error_code": failure.error_code,
                "error_message": str(failure),
                "recoverable": failure.recoverable,
            }
            if observer is not None:
                observer({
                    "type": "command", "status": "failed",
                    "commandId": str(_attribute(command, "command_id") or ""),
                    "stepId": str(_attribute(command, "source_skill_step_id") or ""),
                    "skill": skill, "target": target,
                    "parameters": dict(_parameters_of(command)),
                    "failure": dict(outcome.failure),
                    "current": outcome.commands_started, "total": outcome.commands_total,
                })
            break
        outcome.steps.append(_step_report(command, skill, target, True, started,
                                          started_at, result))
        outcome.commands_completed += 1
        LOGGER.info("[Perf] Name=Agent.Command Skill=%s Target=%s DurationMs=%.3f",
                    skill, target, (time.perf_counter() - started) * 1000.0)
        if observer is not None:
            observer({
                "type": "command", "status": "completed",
                "commandId": str(_attribute(command, "command_id") or ""),
                "stepId": str(_attribute(command, "source_skill_step_id") or ""),
                "skill": skill, "target": target,
                "parameters": dict(_parameters_of(command)),
                "current": outcome.commands_completed, "total": outcome.commands_total,
                "result": _observation_result(result),
            })
        if skill in EXECUTABLE_SKILLS and target and target != "home":
            if target not in touched:
                touched.append(target)

    frames.capture()
    outcome.frames = frames.frames
    outcome.frames_captured = len(frames.frames)
    outcome.frame_budget_exhausted = frames.exhausted
    outcome.affected_objects = touched
    LOGGER.info("[Perf] Name=Agent.ExecuteCommands DurationMs=%.3f Commands=%d "
                "Completed=%d Frames=%d Capture=%s",
                (time.perf_counter() - execution_started) * 1000.0,
                outcome.commands_total, outcome.commands_completed,
                outcome.frames_captured, capture)
    return outcome


class _Services:
    """The runtime services a run needs, built lazily so fakes can replace them."""

    def __init__(self, runtime: Any, kinematics: Any, collision_checker: Any,
                 gripper: Any, *, resolve_target: Any, planning_time: float,
                 max_attempts: int, velocity_scale: float,
                 safety_distance: float) -> None:
        self.runtime = runtime
        self.kinematics = kinematics if kinematics is not None else LiveKinematics(runtime)
        self.collision = (collision_checker if collision_checker is not None
                          else LiveCollisionChecker(runtime))
        if gripper is None:
            gripper = getattr(runtime, "gripper_controller", None)
        if gripper is None:
            gripper = LiveGripperController(runtime)
        self.gripper = gripper
        # The collision checker reads held colliders off the runtime, so the gripper
        # has to be reachable from there for a carried object to be excused.
        runtime.gripper_controller = gripper
        self._colliders: dict[str, frozenset[int]] = {}
        self.resolve_target = resolve_target
        self.planning_time = float(planning_time)
        self.max_attempts = int(max_attempts)
        self.velocity_scale = float(velocity_scale)
        self.safety_distance = float(safety_distance)

    @property
    def joint_limits(self) -> np.ndarray:
        return np.asarray(self.runtime.joint_limits, dtype=float)

    def colliders_of(self, target: str) -> frozenset[int]:
        """Collider entities of a named scene object, resolved once per run.

        A miss triggers one index rebuild before giving up. The scene index is a
        snapshot, so anything created or reparented after it was built is invisible to
        it -- and a composed scene creates its objects as parented children, so an
        index built a moment too early reports a container's walls as belonging to
        nothing. Not finding a name the caller is manipulating is exactly that
        symptom, and it is cheap to rule out: one snapshot per run at worst.
        """
        if target in self._colliders:
            return self._colliders[target]
        value = self._resolve_colliders(target)
        if not value and _refresh_scene(self.runtime):
            self._colliders.clear()
            value = self._resolve_colliders(target)
        self._colliders[target] = value
        return value

    def _resolve_colliders(self, target: str) -> frozenset[int]:
        scene = getattr(self.runtime, "scene", None)
        entity = None if scene is None else scene.object_entity(target)
        return frozenset(scene.colliders_of(entity)) if entity is not None else frozenset()

    def forget_colliders(self) -> None:
        """Drop resolved collider sets, after the scene's composition changed."""
        self._colliders.clear()

    def held_colliders(self) -> frozenset[int]:
        return frozenset(getattr(self.gripper, "held_colliders", ()) or ())

    def target_is_dynamic(self, target: str) -> bool:
        """Return the target body's dynamic flag, conservatively false if unknown."""
        scene = getattr(self.runtime, "scene", None)
        entity = None if scene is None else scene.object_entity(target)
        checker = getattr(scene, "is_dynamic_object", None) if scene is not None else None
        # Older test doubles and non-indexed runtimes predate dynamic metadata;
        # preserve their historical target-contact behavior.
        if not callable(checker):
            return True
        if entity is None and _refresh_scene(self.runtime):
            self._colliders.clear()
            entity = scene.object_entity(target)
        if entity is None:
            return False
        return bool(checker(entity))

    @contextmanager
    def manipulation_exemption(self, target: str) -> Iterator[frozenset[int]]:
        """Permit contact with the objects a motion is manipulating, while it plans.

        Reaching a grasp pose means putting the pads around a dynamic target. The
        target is excused only when its rigid body is dynamic; static targets such as
        tables and containers remain obstacles. Whatever the gripper currently holds
        is also excused, covering held-object contact that the gripper's pad exemption
        does not.

        Exempting is not the same as ignoring: the arm and everything else in the scene
        are still checked, and the exemption is removed as soon as the motion is
        planned, so it cannot leak into an unrelated check.
        """
        allowed = set(self.held_colliders())
        if self.target_is_dynamic(target):
            allowed.update(self.colliders_of(target))
        allowed = frozenset(allowed)
        previous = getattr(self.collision, "allowed_target_geom_ids", None)
        if allowed:
            self.collision.allowed_target_geom_ids = set(allowed)
        try:
            yield allowed
        finally:
            if previous is None:
                # Only clear what this block set; a caller's own exemption survives.
                if hasattr(self.collision, "allowed_target_geom_ids") and allowed:
                    self.collision.allowed_target_geom_ids = None
            else:
                self.collision.allowed_target_geom_ids = previous

    def move_request(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        """The request shape both planners read."""
        return {
            "motion": {"path_constraint": str(parameters.get("path_constraint", "soft"))},
            "planning": {
                "planning_time": float(parameters.get("planning_time", self.planning_time)),
                "max_attempts": int(parameters.get("max_attempts", self.max_attempts)),
            },
            "constraints": {
                "avoid_collision": bool(parameters.get("avoid_collision", True)),
                "safety_distance": float(
                    parameters.get("safety_distance", self.safety_distance)),
                "velocity_scale": float(
                    parameters.get("velocity_scale", self.velocity_scale)),
            },
            "strategy_params": dict(parameters.get("strategy_params", {})),
            "target": parameters.get("target_pose", {}),
        }


def _dispatch_registered_skill(context: CommandExecutionContext) -> dict[str, Any]:
    """Run the real-time implementation selected by a skill command adapter."""
    skill = context.skill_name
    if skill in COMPILE_TIME_SKILLS:
        return {
            "skipped": True,
            "reason": "locate is resolved deterministically at compile time",
        }
    services = context.services
    command = context.command
    frames = context.frames
    outcome = context.outcome
    parameters = _parameters_of(command)
    target = _target_of(command)
    if skill == "move":
        return _execute_move(services, command, parameters, target, frames, outcome,
                             motions_remaining=context.motions_remaining)
    if skill == "grasp":
        return _execute_grasp(services, parameters, target, frames)
    if skill == "release":
        return _execute_release(services, parameters, target)
    if skill == "press":
        return _execute_press(services, parameters, target, frames)
    return _execute_push_pull(services, skill, parameters, target, frames)


# ---------------------------------------------------------------------- move

def _execute_move(services: _Services, command: Any, parameters: Mapping[str, Any],
                  target: str, frames: _FrameCapture, outcome: ExecutionOutcome, *,
                  motions_remaining: int) -> dict[str, Any]:
    """Plan a motion to the command's target and track it.

    The direct planner is probed first even when a caller asked for collision-free
    planning: a straight interpolation that passes validation is shorter, faster and
    reproducible, and the RRT is only worth its cost when the direct path is actually
    blocked.
    """
    runtime = services.runtime
    start = np.asarray(runtime.get_joint_positions(), dtype=float)
    goal = _resolve_goal_configuration(services, parameters, target, start)
    request = services.move_request(parameters)
    joint_path = np.vstack((start, goal))

    plan: dict[str, Any] | None = None
    direct_error: PlanningError | None = None
    # Both planners run under the exemption, and so does the RRT's own final
    # validation gate, which is inside `plan()`. Excusing the search but not the gate
    # would reject every path the search found.
    with services.manipulation_exemption(target) as exempt:
        try:
            plan = DirectPlanner(collision_checker=services.collision,
                                 joint_limits=services.joint_limits).plan(
                {"path_type": "joint", "joint_waypoints": joint_path,
                 "diagnostics": {"probe": "direct"}}, request, {})
        except PlanningError as error:
            direct_error = error

        if plan is None:
            try:
                plan = CollisionFreePlanner(collision_checker=services.collision,
                                            joint_limits=services.joint_limits,
                                            kinematics=services.kinematics).plan(
                    {"path_type": "joint", "start": start, "goal": goal,
                     "orientation_constraint": dict(
                         parameters.get("orientation_constraint", {}))},
                    request, runtime.get_context())
            except PlanningError as error:
                raise CommandFailure(_planning_code(error), str(error),
                                     recoverable=error.recoverable,
                                     details={"direct": None if direct_error is None
                                              else str(direct_error),
                                              "exempt_colliders": sorted(exempt),
                                              "details": error.details}) from error

    waypoints = np.asarray(plan["waypoints"], dtype=float)
    label = f"{_attribute(command, 'command_id') or 'move'} -> {target}"
    segment = dict(plan)
    segment["label"] = label
    # Offset by what the timeline already holds, so ids stay unique and each segment
    # keeps its own colour across commands.
    outcome.segments.extend(segment_markers(
        services.kinematics, [segment], prefix="segment",
        start_index=len(outcome.segments)))

    duration = float(plan.get("duration", 0.0))
    execution_waypoints = resample_for_playback(waypoints, duration, CAPTURE_FPS)
    stride = frames.stride(len(execution_waypoints), duration=duration,
                           motions_remaining=motions_remaining + 1)
    # A held object is a live dynamic body.  Kinematic pose writes teleport the arm
    # without advancing MuJoCo, so contact friction cannot carry the object along.
    # Use the actuator path only for this move and restore the caller's mode afterward
    # so unladen motion keeps the cheap kinematic replay behavior.
    previous_mode = getattr(runtime, "execution_mode", None)
    physical_carry = bool(services.held_colliders())
    if physical_carry and previous_mode == "kinematic":
        runtime.execution_mode = "actuator"
    try:
        executed = _track_path(services, execution_waypoints, plan, frames, stride)
    finally:
        if physical_carry and previous_mode == "kinematic":
            runtime.execution_mode = previous_mode
    return {
        "planner": plan["planner"],
        "path_type": plan["path_type"],
        "direct_probe": "accepted" if direct_error is None else "rejected",
        "direct_rejection": None if direct_error is None else str(direct_error),
        "waypoint_count": int(len(waypoints)),
        "execution_waypoint_count": int(len(execution_waypoints)),
        "planning_time": float(plan.get("planning_time", 0.0)),
        "duration": float(plan.get("duration", 0.0)),
        "capture_stride": int(stride),
        "exempt_colliders": sorted(exempt),
        "diagnostics": plan.get("diagnostics", {}),
        **executed,
    }


def _track_path(services: _Services, waypoints: np.ndarray, plan: Mapping[str, Any],
                frames: _FrameCapture, stride: int) -> dict[str, Any]:
    """Execute a path in stride-sized chunks, capturing a frame after each.

    Chunks overlap by one waypoint because the runtime treats a trajectory's first
    waypoint as where the arm already is and drives to the rest.
    """
    runtime = services.runtime
    total = len(waypoints)
    duration = float(plan.get("duration", 0.0))
    result: dict[str, Any] = {}
    index = 1
    while index < total:
        end = min(index + stride, total)
        chunk = waypoints[index - 1:end]
        share = duration * (len(chunk) - 1) / max(total - 1, 1)
        # A segment can legitimately take longer than the runtime's historical 30 s
        # default (especially after retiming).  The timeout is a wall-clock guard, so
        # budget the planned segment plus a small allowance for RPC/viewer overhead;
        # otherwise a valid long trajectory is reported as EXECUTION_TIMEOUT.
        segment_timeout = max(30.0, share + 5.0)
        try:
            result = runtime.execute_trajectory({"waypoints": chunk.tolist(),
                                                 "duration": share},
                                                 timeout=segment_timeout)
        except TimeoutError as error:
            raise CommandFailure(_code("RUNTIME_TIMEOUT"), str(error),
                                 recoverable=True) from error
        frames.capture()
        index = end
    final = np.asarray(runtime.get_joint_positions(), dtype=float)
    return {
        "final_joint_state": final.tolist(),
        "final_pose": serializable_pose(runtime.get_end_effector_pose()),
        "joint_error": float(np.max(np.abs(final - waypoints[-1]))),
        "servo_limit_applied": bool(result.get("servo_limit_applied", False))
        if total > 1 else False,
    }


def _resolve_goal_configuration(services: _Services, parameters: Mapping[str, Any],
                                target: str, seed: np.ndarray) -> np.ndarray:
    """The joint configuration a move should end at.

    Four sources, in order of how explicit they are: joint values in the command, a
    Cartesian pose in the command, a caller-supplied resolver for the target's anchor
    -- which is where the interaction registry enters, without this module importing
    it -- and finally the target object's own pose in the live scene.
    """
    limits = services.joint_limits
    if parameters.get("joint_configuration") is not None:
        goal = np.asarray(parameters["joint_configuration"], dtype=float)
        if goal.shape != (len(seed),):
            raise CommandFailure(_code("INVALID_REQUEST"),
                                 "joint_configuration must match the arm joint count.")
        return np.clip(goal, limits[:, 0], limits[:, 1])
    if target == "home":
        home = parameters.get("home_configuration",
                              getattr(services.runtime, "home_configuration", None))
        if home is None:
            # Hold position rather than fabricating a pose. An all-zeros vector is not a
            # neutral choice: for a UR5e it stretches the arm out horizontally, through
            # whatever the robot is standing on.
            return np.asarray(seed, dtype=float).copy()
        return np.clip(np.asarray(home, dtype=float), limits[:, 0], limits[:, 1])

    pose = _target_pose(services, parameters, target)
    try:
        solved = services.kinematics.solve(
            pose, seed=seed, method=str(parameters.get("ik_method", "auto")),
            timeout=float(parameters.get("ik_timeout", 1.0)))
    except IKError as error:
        raise CommandFailure(_code("IK_FAILED"),
                             f"No IK solution for {target!r}: {error}",
                             recoverable=True) from error
    return np.clip(np.asarray(solved["solution"], dtype=float),
                   limits[:, 0], limits[:, 1])


# Symbolic move targets that mean "relative to the tool", not a scene object.
_RELATIVE_TARGETS = frozenset({"end_effector", "tcp", "tool"})

# World-frame unit directions, mirroring the generated interaction registry's convention
# (+x away from the robot base). Kept local: this layer must not import from `scene`.
_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "above": (0.0, 0.0, 1.0), "below": (0.0, 0.0, -1.0),
    "front": (1.0, 0.0, 0.0), "behind": (-1.0, 0.0, 0.0),
    "left": (0.0, 1.0, 0.0), "right": (0.0, -1.0, 0.0),
    "at": (0.0, 0.0, 0.0),
}


def _relative_pose(services: _Services, parameters: Mapping[str, Any],
                   target: str) -> dict[str, Any]:
    """Offset the current tool pose along a named direction."""
    current = services.runtime.get_end_effector_pose()
    position = np.asarray(current["position"], dtype=float)
    relation = str(parameters.get("relation", "above")).strip().lower()
    direction = _DIRECTIONS.get(relation)
    if direction is None:
        raise CommandFailure(
            _code("INVALID_REQUEST"),
            f"Unknown move relation {relation!r} for target {target!r}.")
    offset = np.asarray(direction, dtype=float) * float(parameters.get("distance_m", 0.0))
    frame = str(parameters.get("frame", "world")).strip().lower()
    if frame in {"tool", "tcp", "end_effector"}:
        # Rotate the direction into world axes; otherwise a tool-frame lift would travel
        # along world +z regardless of how the wrist is turned.
        offset = np.asarray(current["rotation_matrix"], dtype=float) @ offset
    elif frame not in {"world", "base", ""}:
        raise CommandFailure(_code("INVALID_REQUEST"),
                             f"Unknown move frame {frame!r} for target {target!r}.")
    # Orientation is deliberately left out when the command did not ask for one: the IK
    # layer then holds the tool's current rotation, which is what a lift or retreat wants.
    pose: dict[str, Any] = {"position": position + offset}
    if parameters.get("orientation") is not None:
        pose["orientation"] = parameters["orientation"]
    return pose


def _target_pose(services: _Services, parameters: Mapping[str, Any],
                 target: str) -> dict[str, Any]:
    """A pose mapping for the IK solver, from the most explicit source available."""
    for key in ("target_pose", "pose"):
        value = parameters.get(key)
        if isinstance(value, Mapping) and value.get("position") is not None:
            return dict(value)
    if parameters.get("position") is not None:
        pose: dict[str, Any] = {"position": parameters["position"]}
        if parameters.get("orientation") is not None:
            pose["orientation"] = parameters["orientation"]
        return pose
    if target in _RELATIVE_TARGETS:
        # A relative move from wherever the tool is now. The compiler emits these for its
        # deterministic post-grasp lift and post-release retreat, naming a direction and a
        # distance rather than a point, because only the runtime knows the live TCP pose.
        return _relative_pose(services, parameters, target)
    if services.resolve_target is not None:
        resolved = services.resolve_target(target, parameters.get("anchor"))
        if isinstance(resolved, Mapping) and resolved.get("position") is not None:
            return dict(resolved)
    live = services.runtime.object_pose(target)
    if live is None:
        raise CommandFailure(
            _code("TARGET_NOT_FOUND"),
            f"Move target {target!r} is not in the live scene and no pose was supplied.")
    position = np.asarray(live["position"], dtype=float)
    approach = parameters.get("approach_offset")
    if approach is not None:
        position = position + np.asarray(approach, dtype=float)
    pose = {"position": position}
    if parameters.get("orientation") is not None:
        pose["orientation"] = parameters["orientation"]
    return pose


# ---------------------------------------------------------------------- gripper

def _execute_grasp(services: _Services, parameters: Mapping[str, Any],
                   target: str, frames: _FrameCapture) -> dict[str, Any]:
    gripper = services.gripper
    width = float(parameters.get("width", 0.0))
    previous_observer = getattr(gripper, "step_observer", None)
    try:
        gripper.step_observer = lambda: frames.capture_at_fps(CAPTURE_FPS)
        result = dict(gripper.close(
            width, speed=float(parameters.get("speed", 0.5)),
            force=float(parameters.get("force", 20.0)),
            timeout=float(parameters.get("timeout", 5.0)),
            target_object_id=target))
    finally:
        gripper.step_observer = previous_observer
    if not result.get("contact_detected"):
        raise CommandFailure(
            _code("EXECUTION_FAILED"),
            f"Gripper closed on nothing: no pad contact with {target!r}.",
            recoverable=True, details=result)
    services.collision.refresh_baseline()
    return result


def _execute_release(services: _Services, parameters: Mapping[str, Any],
                     target: str) -> dict[str, Any]:
    gripper = services.gripper
    width = parameters.get("width")
    result = dict(gripper.open(
        float(width) if width is not None else gripper.maximum_width,
        speed=float(parameters.get("speed", 0.5)),
        timeout=float(parameters.get("timeout", 5.0))))
    services.collision.refresh_baseline()
    result["released"] = target
    return result


# ---------------------------------------------------------------------- press

def _execute_press(services: _Services, parameters: Mapping[str, Any], target: str,
                   frames: _FrameCapture) -> dict[str, Any]:
    """Press along a fixed world axis by local IK, gating on geometric contact.

    Displacement only. The platform exposes no contact wrench and no penetration depth
    to derive one from -- reported distances are clamped at zero -- so a force-mode
    request fails rather than returning a fabricated newton value. The consequence for
    displacement mode is that `contact_threshold` and `maximum_force` cannot bite,
    because the value they compare against never rises above zero.
    """
    mode = str(parameters.get("control_mode", parameters.get("mode", "displacement")))
    if mode != "displacement":
        raise CommandFailure(
            _code("SENSOR_DATA_UNAVAILABLE"),
            "Force-mode press needs a calibrated contact-force readout, which the "
            "simulation platform does not expose. Use displacement press, which gates "
            "on geometric contact.",
            recoverable=True)

    runtime = services.runtime
    entity = runtime.scene.object_entity(target)
    if entity is None:
        raise CommandFailure(_code("TARGET_NOT_FOUND"),
                             f"Press target {target!r} is not in the live scene.")
    target_colliders = set(runtime.scene.colliders_of(entity))
    direction = np.asarray(parameters.get("direction", (0.0, 0.0, -1.0)), dtype=float)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        raise CommandFailure(_code("INVALID_REQUEST"),
                             "Press direction has zero length.")
    direction = direction / norm

    maximum_travel = float(parameters.get("maximum_travel", 0.10))
    post_contact = float(parameters.get("press_depth",
                                        parameters.get("post_contact_depth", 0.005)))
    step = float(parameters.get("step", 0.002))
    timeout = float(parameters.get("timeout", 10.0))
    stop_on_contact = bool(parameters.get("stop_on_contact", False))

    start_q = np.asarray(runtime.get_joint_positions(), dtype=float)
    start_pose = runtime.get_end_effector_pose()
    start_position = np.asarray(start_pose["position"], dtype=float)
    orientation = quaternion_mapping(start_pose["quaternion_wxyz"])
    started = time.perf_counter()
    current = start_q.copy()
    travel = 0.0
    contact_travel: float | None = None
    samples: list[dict[str, Any]] = []

    while travel < maximum_travel - 1e-9:
        if time.perf_counter() - started > timeout:
            raise CommandFailure(_code("RUNTIME_TIMEOUT"),
                                 "Press execution timed out.", recoverable=True)
        travel = min(travel + step, maximum_travel)
        try:
            solved = services.kinematics.solve(
                {"position": start_position + direction * travel,
                 "orientation": orientation},
                seed=current, method="trac_ik", timeout=0.25)
        except IKError as error:
            _retract(services, start_q)
            raise CommandFailure(
                _code("IK_FAILED"),
                f"Press IK failed at travel {travel:.4f} m: {error}",
                recoverable=True) from error
        current = np.asarray(solved["solution"], dtype=float)
        # Written directly: a press gates on geometric contact, which the sync below
        # makes exact, and stepping the world at every sample would multiply the cost
        # of a scan that already runs local IK per step.
        runtime.set_arm_positions(current)
        runtime.sync_backends()

        contacts = _contacts_with(services, target_colliders)
        foreign = _foreign_contacts(services, target_colliders)
        if foreign:
            _retract(services, start_q)
            raise CommandFailure(_code("LOCAL_COLLISION_DETECTED"),
                                 "Unexpected collision during press.",
                                 recoverable=True, details=foreign)
        if contacts and contact_travel is None:
            contact_travel = travel
        samples.append({"travel": travel, "contact": bool(contacts),
                        # No wrench and no penetration depth exist to derive this from.
                        "normal_force": 0.0})
        if contact_travel is not None and (
                stop_on_contact or travel - contact_travel >= post_contact - 1e-9):
            break

    frames.capture()
    if contact_travel is None:
        if bool(parameters.get("require_contact", True)):
            _retract(services, start_q)
            raise CommandFailure(
                _code("TARGET_DEPTH_NOT_REACHED"),
                f"Press travelled {travel:.4f} m without touching {target!r}.",
                recoverable=True, details={"maximum_travel": maximum_travel})
    retracted = False
    if bool(parameters.get("retract", True)):
        retracted = _retract(services, start_q)
        frames.capture()
    return {
        "control_mode": "displacement",
        "contact_detected": contact_travel is not None,
        "contact_travel": contact_travel,
        "travel_distance": travel,
        "target_actuated": contact_travel is not None,
        "retracted": retracted,
        "peak_force": 0.0,
        "final_force": 0.0,
        "force_feedback_available": False,
        "sample_count": len(samples),
        "final_pose": serializable_pose(runtime.get_end_effector_pose()),
    }


def _retract(services: _Services, start_q: np.ndarray) -> bool:
    """Return the arm to where a local motion began."""
    runtime = services.runtime
    current = np.asarray(runtime.get_joint_positions(), dtype=float)
    count = max(2, int(np.ceil(float(np.max(np.abs(current - start_q))) / 0.05)) + 1)
    path = np.linspace(current, start_q, count)
    try:
        runtime.execute_trajectory({"waypoints": path.tolist(), "duration": 0.0})
    except TimeoutError:
        return False
    runtime.sync_backends()
    return True


# ---------------------------------------------------------------------- push/pull

def _execute_push_pull(services: _Services, skill: str,
                       parameters: Mapping[str, Any], target: str,
                       frames: _FrameCapture) -> dict[str, Any]:
    """Translate a free scene object along a line while the arm follows it.

    Pull and push reach the same implementation; only the requested direction differs.
    A mechanism request -- driving a cabinet hinge or a drawer slide -- fails: the
    platform's entity model has no scene-object joints, so there is no articulation to
    drive and animating one would mean inventing state the scene does not contain.
    """
    interaction = str(parameters.get("interaction_mode", "free_object"))
    if interaction != "free_object":
        raise CommandFailure(
            _code("PUSH_PULL_UNSUPPORTED"),
            f"{skill.capitalize()} in {interaction!r} mode needs a joint on {target!r}. "
            "The simulation platform's scene model has no scene-object joints, so "
            "there is no mechanism to drive.",
            recoverable=False)

    runtime = services.runtime
    entity = runtime.scene.object_entity(target)
    if entity is None:
        raise CommandFailure(_code("TARGET_NOT_FOUND"),
                             f"Push/pull target {target!r} is not in the live scene.")
    if runtime.scene.parent_of(entity) is not None:
        # A transform write is local, so translating a parented entity by a world
        # delta would be wrong. Pushable scene objects are roots.
        raise CommandFailure(
            _code("PUSH_PULL_UNSUPPORTED"),
            f"Push/pull target {target!r} is parented to another entity; only free "
            "scene objects can be pushed.")
    proxy = runtime.object_proxy(target)
    if proxy is None:
        raise CommandFailure(_code("TARGET_NOT_FOUND"),
                             f"Push/pull target {target!r} has no addressable entity.")

    direction = np.asarray(parameters.get("direction", (1.0, 0.0, 0.0)), dtype=float)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        raise CommandFailure(_code("INVALID_REQUEST"),
                             "Push/pull direction has zero length.")
    direction = direction / norm
    if skill == "pull":
        direction = -direction
    distance = float(parameters.get("distance", 0.05))
    step = float(parameters.get("step", 0.005))
    timeout = float(parameters.get("timeout", 15.0))
    maintain_contact = bool(parameters.get("maintain_contact", True))
    target_colliders = set(runtime.scene.colliders_of(entity))

    start_q = np.asarray(runtime.get_joint_positions(), dtype=float)
    start_pose = runtime.get_end_effector_pose()
    start_position = np.asarray(start_pose["position"], dtype=float)
    orientation = quaternion_mapping(start_pose["quaternion_wxyz"])
    object_start = np.asarray(proxy.transform["translation"], dtype=float)
    if maintain_contact and not _contacts_with(services, target_colliders):
        raise CommandFailure(
            _code("EXECUTION_FAILED"),
            f"Push/pull needs an existing tool contact with {target!r} before moving.",
            recoverable=True)

    started = time.perf_counter()
    current = start_q.copy()
    contact_maintained = True
    count = max(2, int(np.ceil(distance / max(step, 1e-6))) + 1)
    stride = frames.stride(count, motions_remaining=1)
    for index, travel in enumerate(np.linspace(0.0, distance, count)[1:], start=1):
        if time.perf_counter() - started > timeout:
            raise CommandFailure(_code("RUNTIME_TIMEOUT"),
                                 "Push/pull execution timed out.", recoverable=True)
        try:
            solved = services.kinematics.solve(
                {"position": start_position + direction * float(travel),
                 "orientation": orientation},
                seed=current, method="trac_ik", timeout=0.25)
        except IKError as error:
            _restore_object(runtime, proxy, object_start)
            _retract(services, start_q)
            raise CommandFailure(
                _code("IK_FAILED"),
                f"Arm cannot follow the push/pull line: {error}",
                recoverable=True) from error
        current = np.asarray(solved["solution"], dtype=float)
        # Both poses are written directly. The object is carried by being translated
        # explicitly, not by physics pushing it, so stepping would buy nothing here and
        # would let a dynamic body drift away from the pose just written for it.
        runtime.set_arm_positions(current)
        proxy.set_position(object_start + direction * float(travel))
        # Non-robot colliders keep the transform from the last sync, so without this
        # the object would answer contact queries at its old pose.
        runtime.sync_backends()
        has_contact = bool(_contacts_with(services, target_colliders))
        contact_maintained = contact_maintained and has_contact
        if maintain_contact and not has_contact:
            _restore_object(runtime, proxy, object_start)
            _retract(services, start_q)
            raise CommandFailure(
                _code("EXECUTION_FAILED"),
                f"Tool contact with {target!r} was lost during {skill}.",
                recoverable=True)
        if index % stride == 0:
            frames.capture()

    frames.capture()
    final = np.asarray(proxy.transform["translation"], dtype=float)
    moved = float(np.linalg.norm(final - object_start))
    return {
        "interaction_mode": "free_object",
        "operation": skill,
        "contact_established": True,
        "contact_maintained": contact_maintained,
        "target_moved": moved > 1e-5,
        "travel_distance": moved,
        "rotation_angle": 0.0,
        "peak_force": 0.0,
        "object_final_state": {"object_id": target, "position": final.tolist()},
        "final_pose": serializable_pose(runtime.get_end_effector_pose()),
    }


def _restore_object(runtime: Any, proxy: Any, position: np.ndarray) -> None:
    proxy.set_position([float(value) for value in position])
    runtime.sync_backends()


def _contacts_with(services: _Services, colliders: set[int]) -> list[dict[str, Any]]:
    """Colliding pairs between the robot and one object, at the live pose."""
    runtime = services.runtime
    result: list[dict[str, Any]] = []
    for pair in runtime.robot.configuration_contacts(
            [float(value) for value in runtime.full_joint_positions()],
            limits="ignore"):
        colliding, first, second = _contact_fields(pair)
        if not colliding:
            continue
        if first in colliders or second in colliders:
            result.append({"geom1": runtime.scene.name_of(first),
                           "geom2": runtime.scene.name_of(second)})
    return result


def _foreign_contacts(services: _Services,
                      colliders: set[int]) -> list[dict[str, Any]]:
    """Colliding pairs that are neither the intended target nor pre-existing."""
    runtime = services.runtime
    baseline = services.collision.baseline_allowed_pairs
    result: list[dict[str, Any]] = []
    for pair in runtime.robot.configuration_contacts(
            [float(value) for value in runtime.full_joint_positions()],
            limits="ignore"):
        colliding, first, second = _contact_fields(pair)
        if not colliding or first in colliders or second in colliders:
            continue
        if frozenset((first, second)) in baseline:
            continue
        result.append({"geom1": runtime.scene.name_of(first),
                       "geom2": runtime.scene.name_of(second)})
    return result


def _contact_fields(value: Any) -> tuple[bool, int, int]:
    if isinstance(value, Mapping):
        return (bool(value["colliding"]), int(value["robot_collider"]),
                int(value["other"]))
    return bool(value.colliding), int(value.robot_collider), int(value.other)


# ---------------------------------------------------------------------- plumbing

def _refresh_scene(runtime: Any) -> bool:
    """Rebuild the runtime's scene index. False when it has no way to.

    Failure is swallowed deliberately: this runs on a lookup miss, where a rebuild is
    an attempt to explain the miss rather than something the caller asked for. If it
    cannot happen, the miss is reported as the miss it already was.
    """
    refresh = getattr(runtime, "refresh_scene", None)
    if not callable(refresh):
        return False
    try:
        refresh()
    except Exception:
        return False
    return True


def _observation_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = ("planner", "path_type", "waypoint_count", "duration", "planning_time",
               "capture_stride", "contacts", "holding", "object_id")
    return {key: value[key] for key in allowed if key in value}


def _planning_code(error: PlanningError) -> str:
    """Map a planner's own code onto the service's closed error vocabulary."""
    mapping = {
        "COLLISION_DETECTED": "LOCAL_COLLISION_DETECTED",
        "JOINT_LIMIT_VIOLATION": "INVALID_REQUEST",
        "PATH_NOT_FOUND": "EXECUTION_FAILED",
        "TRAJECTORY_INVALID": "EXECUTION_FAILED",
        "PATH_CONSTRAINT_INFEASIBLE": "EXECUTION_FAILED",
        "PLANNING_FAILED": "EXECUTION_FAILED",
    }
    return _code(mapping.get(error.code, "EXECUTION_FAILED"))


def _command_list(commands: Any) -> list[Any]:
    if isinstance(commands, Mapping):
        items = commands.get("commands", ())
    else:
        items = getattr(commands, "commands", commands)
    if items is None:
        return []
    if isinstance(items, (str, bytes)):
        raise TypeError("commands must be a document or a sequence of commands")
    return list(items)


def _attribute(command: Any, name: str) -> Any:
    if isinstance(command, Mapping):
        return command.get(name)
    return getattr(command, name, None)


def _skill_of(command: Any) -> str:
    return str(_attribute(command, "skill_name") or _attribute(command, "skill") or "")


def _target_of(command: Any) -> str:
    parameters = _parameters_of(command)
    return str(parameters.get("target", "") or "")


def _parameters_of(command: Any) -> Mapping[str, Any]:
    value = _attribute(command, "parameters")
    return value if isinstance(value, Mapping) else {}


def _step_report(command: Any, skill: str, target: str, success: bool,
                 started: float, started_at: datetime,
                 result: Mapping[str, Any]) -> dict[str, Any]:
    finished_at = datetime.now(timezone.utc)
    return {
        "runtime_step_id": f"runtime-{uuid.uuid4().hex[:12]}",
        "command_id": _attribute(command, "command_id"),
        "source_skill_step_id": _attribute(command, "source_skill_step_id"),
        "skill": skill,
        "target": target,
        "success": success,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": max(time.perf_counter() - started, 0.0),
        "result": dict(result),
    }
