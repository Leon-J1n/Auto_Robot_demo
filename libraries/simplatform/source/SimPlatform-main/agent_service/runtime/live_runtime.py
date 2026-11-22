"""Robot-state access and simulated execution against an already-running world.

The equivalent of the in-process `SceneRobotRuntime`, with one structural
difference: this class does not own a simulator. The agent service is a separate
process from the web gateway that holds the authoritative world, so everything here
goes through `sim_platform.live` -- `connect(endpoint)`, a `LiveSession`, and the
`RobotProxy` / `EntityProxy` surface. Connect-only by construction: there is no code
path that launches an instance, and a failed connection raises.

Joint positions are 6-vectors in UR5e order. The platform indexes joints
alphabetically across the whole robot -- 26 of them for a loaded UR5e with a Robotiq
gripper, only 12 of which move -- so this class owns the mapping between that full
vector and the 6 arm joints the callers speak in.

Every read is a TCP round trip, which the in-process version did not have to think
about. Two consequences shape the design. Joint state is cached and invalidated on
write, because the collision checker and the gripper build hypothetical
configurations from it hundreds of times per search and re-reading it each time would
dominate the cost. And link and object proxies are resolved once and kept, because
`get_link` and `find_entity` are themselves requests.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from sim_platform.live import (EntityProxy, LiveControlError, LiveSession, RobotProxy,
                               connect)

from .scene_index import SceneIndex
from .transforms import quaternion_to_matrix, serializable_pose

UR5E_JOINT_NAMES: tuple[str, ...] = (
    "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
    "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
)
END_EFFECTOR_LINK = "robotiq_2f85_pinch"
ARM_JOINT_COUNT = len(UR5E_JOINT_NAMES)
ACTUATOR_STEP_BATCH = 10


class RuntimeConnectionError(RuntimeError):
    """Raised when the runtime cannot reach the live world it was pointed at."""


class LiveSceneRuntime:
    """Drive the gateway's authoritative world over Live Python.

    Owns the session only in the sense of using it; it never launches one, and
    `close()` drops the connection without shutting the far side down.
    """

    def __init__(
        self,
        session: LiveSession,
        *,
        robot_alias: str | None = None,
        joint_names: Sequence[str] = UR5E_JOINT_NAMES,
        end_effector_link: str = END_EFFECTOR_LINK,
        execution_mode: str = "kinematic",
        realtime: bool = False,
        playback_speed: float = 1.0,
        minimum_playback_duration: float = 0.0,
        playback_fps: float = 60.0,
        fixed_time_step: float = 0.002,
        steps_per_waypoint: int = 500,
        max_joint_speed_deg: float | None = None,
    ) -> None:
        if execution_mode not in {"kinematic", "actuator"}:
            raise ValueError("execution_mode must be kinematic or actuator.")
        if max_joint_speed_deg is not None and max_joint_speed_deg <= 0.0:
            raise ValueError("max_joint_speed_deg must be positive.")
        self.session = session
        self.execution_mode = execution_mode
        # Cap on how fast the joint servo may slew, in degrees per second. Only meaningful
        # in actuator mode, where the servo -- not the planned duration -- decides how fast
        # the arm actually moves. None leaves the platform's own default of 10 rad/s
        # (573 deg/s), which is fast enough to fling a carried object out of the gripper.
        self.max_joint_speed_deg = (None if max_joint_speed_deg is None
                                   else float(max_joint_speed_deg))
        self.fixed_time_step = float(fixed_time_step)
        self.realtime = realtime
        self.playback_speed = max(float(playback_speed), 0.01)
        self.minimum_playback_duration = max(float(minimum_playback_duration), 0.0)
        self.playback_fps = max(float(playback_fps), 15.0)
        self.steps_per_waypoint = max(int(steps_per_waypoint), 1)
        self.viewer = None
        # Optional observer installed by the Agent Service while publishing a move.
        # It is called after each waypoint has actually been applied.
        self.waypoint_observer = None
        self._simulation_time: float | None = None

        self.robot = self._find_robot(robot_alias)
        self.joint_names = tuple(joint_names)
        platform_names = tuple(self.robot.joint_names)
        missing = [name for name in self.joint_names if name not in platform_names]
        if missing:
            raise ValueError(f"Scene robot is missing UR5e joints: {', '.join(missing)}")
        # Named "qpos_indices" because planners and controllers already address the
        # runtime through that attribute; here it indexes the platform joint vector.
        self.qpos_indices = np.asarray(
            [platform_names.index(name) for name in self.joint_names], dtype=int)
        self.dof_indices = self.qpos_indices
        self.actuator_ids = self.qpos_indices
        self.platform_joint_names = platform_names
        self.platform_joint_count = len(platform_names)

        self.end_effector_site = end_effector_link
        self._links: dict[str, EntityProxy] = {}
        self._objects: dict[str, EntityProxy | None] = {}
        self.end_effector_link = self.link(end_effector_link)
        self.end_effector_site_id = self.end_effector_link.handle
        self.scene = SceneIndex(self.session, self.robot)
        self._all_limits = self._read_joint_limits()
        self._joint_cache: np.ndarray | None = None
        # The pose the scene was authored in, captured before anything moves. This is the
        # "home" a move can safely return to: it is by construction collision-free in this
        # scene, unlike an all-zeros vector, which for a UR5e is the arm stretched out
        # horizontally and straight through the table it stands on.
        self.home_configuration = np.asarray(self.get_joint_positions(), dtype=float).copy()

    # ------------------------------------------------------------------ connection

    @classmethod
    def connect(cls, endpoint: str | int, *, workspace: str = "default",
                timeout: float = 10.0, **kwargs: Any) -> "LiveSceneRuntime":
        """Attach to a running instance at `endpoint`. Never launches one.

        A failed connection is an error rather than a cue to start a simulator: the
        world this runtime drives is the gateway's, and a private fallback instance
        would silently plan against a scene nobody is looking at.
        """
        try:
            session = connect(endpoint, workspace=workspace, timeout=timeout)
        except (LiveControlError, ValueError, OSError) as error:
            raise RuntimeConnectionError(
                f"Cannot reach a live simulation at {endpoint!r}: {error}") from error
        try:
            return cls(session, **kwargs)
        except Exception:
            session.close()
            raise

    def _find_robot(self, alias: str | None) -> RobotProxy:
        """Find the scene's robot, by alias when given and by snapshot otherwise."""
        if alias:
            return self.session.require_robot(alias)
        snapshot = self.session.scene_snapshot()
        candidates = [record for record in snapshot.entities if "robot" in record]
        if not candidates:
            raise RuntimeConnectionError(
                "The live scene holds no robot; load one before starting the runtime.")
        if len(candidates) > 1:
            # Prefer the one that actually articulates; a scene could hold a fixture.
            movable = [record for record in candidates
                       if len(record.get("robot", {}).get("jointNames", ())) > 0]
            if len(movable) != 1:
                named = ", ".join(sorted(str(record.get("name", "")) for record in candidates))
                raise RuntimeConnectionError(
                    f"The live scene holds several robots ({named}); pass robot_alias.")
            candidates = movable
        object_id = str(candidates[0].get("objectId", ""))
        if not object_id:
            raise RuntimeConnectionError("Robot record carries no object id.")
        proxy = self.session.find_entity(object_id)
        if not isinstance(proxy, RobotProxy):
            raise RuntimeConnectionError("Resolved scene object is not a robot.")
        return proxy

    def _read_joint_limits(self) -> np.ndarray:
        limits = np.asarray(self.robot.joint_limits, dtype=float)
        if limits.ndim != 2 or limits.shape != (self.platform_joint_count, 2):
            raise ValueError("joint_limits must have shape (joint_count, 2).")
        return limits

    # ------------------------------------------------------------------ joint state

    @property
    def joint_limits(self) -> np.ndarray:
        """(6, 2) lower/upper bounds for the arm joints."""
        return self._all_limits[self.qpos_indices].copy()

    @property
    def all_joint_limits(self) -> np.ndarray:
        """Lower/upper bounds for every platform joint, gripper linkage included."""
        return self._all_limits.copy()

    def get_joint_positions(self) -> np.ndarray:
        return self.full_joint_positions()[self.qpos_indices].copy()

    def full_joint_positions(self, *, refresh: bool = False) -> np.ndarray:
        """The complete platform joint vector, including gripper and fixed joints.

        Cached: the collision checker and the gripper's stop-angle search each build
        many hypothetical configurations from this vector, and over Live every read
        is a round trip. `invalidate()` runs on every write this class performs, so
        the cache only goes stale if something else writes to the world.
        """
        if refresh or self._joint_cache is None:
            self._joint_cache = np.asarray(self.robot.joint_state[0], dtype=float)
        return self._joint_cache.copy()

    def invalidate(self) -> None:
        """Drop the cached joint state, after the world was written to."""
        self._joint_cache = None

    def expand_joints(self, joints: Sequence[float],
                      base: Sequence[float] | None = None) -> np.ndarray:
        """Place a 6-vector into a full platform joint vector.

        Everything not an arm joint keeps its current value, so a query about the arm
        never disturbs the gripper's pose.
        """
        values = np.asarray(joints, dtype=float)
        if values.shape != (ARM_JOINT_COUNT,):
            raise ValueError(f"UR5e joint vector must contain {ARM_JOINT_COUNT} values.")
        full = (self.full_joint_positions() if base is None
                else np.asarray(base, dtype=float).copy())
        full[self.qpos_indices] = values
        return self._clamp_non_arm_joint_targets(full)

    def _clamp_non_arm_joint_targets(self, positions: Sequence[float]) -> np.ndarray:
        """Keep preserved non-arm joints inside their scene-declared bounds."""
        full = np.asarray(positions, dtype=float).copy()
        if full.shape != (self.platform_joint_count,):
            raise ValueError(f"Platform joint vector must contain {self.platform_joint_count} values.")
        # Move plans address only the six UR5e arm joints.  Preserve the gripper
        # configuration, but keep its passive/mimic joints just inside the
        # platform limits before sending a full-vector command.  Contact and
        # finite-effort servo settling can leave a follower a few ulps outside
        # its declared range; rejecting an otherwise valid arm move because of
        # that stale non-arm value produces a misleading joint-limit failure.
        non_arm = np.ones(self.platform_joint_count, dtype=bool)
        non_arm[self.qpos_indices] = False
        lower = self._all_limits[non_arm, 0]
        upper = self._all_limits[non_arm, 1]
        finite = np.isfinite(lower) & np.isfinite(upper)
        if np.any(finite):
            indices = np.flatnonzero(non_arm)[finite]
            full[indices] = np.clip(full[indices], lower[finite], upper[finite])
        return full

    def expand_joint_batch(self, configurations: Sequence[Sequence[float]],
                           base: Sequence[float] | None = None) -> np.ndarray:
        """Place many 6-vectors into full platform joint vectors, in one array.

        The batch form exists because a single `kinematics()` or
        `configuration_contacts()` call carrying many configurations costs one round
        trip, where a loop costs one each.
        """
        values = np.atleast_2d(np.asarray(configurations, dtype=float))
        if values.shape[1] != ARM_JOINT_COUNT:
            raise ValueError(f"UR5e joint vectors must contain {ARM_JOINT_COUNT} values.")
        reference = (self.full_joint_positions() if base is None
                     else np.asarray(base, dtype=float))
        full = np.repeat(reference[None, :], len(values), axis=0)
        full[:, self.qpos_indices] = values
        return np.asarray([self._clamp_non_arm_joint_targets(row) for row in full])

    def command_arm(self, joints: Sequence[float]) -> None:
        """Drive the arm toward a configuration through the joint controller.

        Commanding is what makes contact and carried objects behave, and what the
        timeline animates, but the arm only arrives as the world steps.
        `execute_trajectory` does the stepping; this method only issues the command.
        To place the arm at a known configuration without motion, use
        `set_arm_positions`.
        """
        positions = self.expand_joints(joints)
        gripper = getattr(self, "gripper_controller", None)
        if gripper is not None and hasattr(gripper, "preserve_commanded_target"):
            positions = gripper.preserve_commanded_target(positions)
        self.command_full(positions)

    def command_full(self, positions: Sequence[float]) -> None:
        """Command a full platform joint vector, honouring the servo cap."""
        targets = self._clamp_non_arm_joint_targets(positions)
        self.robot.command_joint_positions([float(value) for value in targets],
                                           **self._command_tuning())
        self.invalidate()

    def _command_tuning(self) -> dict:
        tuning = dict(self._servo_limits)
        gripper = getattr(self, "gripper_controller", None)
        if gripper is not None and hasattr(gripper, "joint_effort_limits"):
            limits = gripper.joint_effort_limits()
            if limits is not None:
                tuning["effort_limits"] = limits
        return tuning

    def set_arm_positions(self, joints: Sequence[float], *,
                          limits: str = "clamp") -> None:
        """Pose the arm directly, leaving the gripper as it is. Costs no steps."""
        self.set_full_positions(self.expand_joints(joints), limits=limits)

    def set_full_positions(self, positions: Sequence[float], *,
                           limits: str = "clamp") -> None:
        """Write a full platform joint vector straight into the scene.

        Bypasses the controller, so the arm arrives without the world stepping. That
        makes it the cheap way to stage a known configuration, and the wrong way to
        produce motion: nothing is simulated on the way there, so contact is never
        resolved and an object held only by friction is left behind. Callers that need
        either must command and step instead.

        A direct write also leaves the physics and collision backends holding the
        previous geometry, so `sync_backends()` must follow before anything queries
        contact.
        """
        self.robot.set_joint_positions([float(value) for value in positions],
                                       limits=limits)
        self.invalidate()

    @property
    def _servo_limits(self) -> dict[str, float]:
        """Keyword arguments capping the joint servo, empty when no cap was asked for.

        Passed per command rather than configured once because the platform's servo
        takes the limit with the command; there is no persistent setting to write.
        """
        if self.max_joint_speed_deg is None:
            return {}
        return {"velocity_limit": float(np.radians(self.max_joint_speed_deg))}

    # ------------------------------------------------------------------ sync

    def sync_backends(self) -> None:
        """Push scene writes into the physics and collision backends.

        Required after anything poses the world directly, or the collision backend
        answers from stale geometry -- a configuration query would then be judged
        against where the robot used to be.
        """
        self.session.sync_physics()
        self.session.sync_collision()

    def step(self, count: int = 1) -> float:
        advance = getattr(self.session, "step_unpaced", self.session.step)
        result = float(advance(count))
        self._simulation_time = result
        self.invalidate()
        self._sync_viewer()
        return result

    # ------------------------------------------------------------------ poses

    def link(self, name: str) -> EntityProxy:
        """A cached proxy for one of the robot's links."""
        proxy = self._links.get(name)
        if proxy is None:
            proxy = self.robot.get_link(name)
            self._links[name] = proxy
        return proxy

    def link_pose(self, link: Any | str) -> dict[str, Any]:
        entity = self.link(link) if isinstance(link, str) else link
        transform = entity.transform
        return {"position": np.asarray(transform["translation"], dtype=float),
                "quaternion_wxyz": np.asarray(transform["rotation_wxyz"], dtype=float),
                "rotation_matrix": quaternion_to_matrix(transform["rotation_wxyz"])}

    def get_end_effector_pose(self) -> dict[str, Any]:
        return self.link_pose(self.end_effector_link)

    def object_proxy(self, name: str) -> EntityProxy | None:
        """A cached proxy for a scene object addressed by name."""
        if name in self._objects:
            return self._objects[name]
        entity = self.scene.object_entity(name)
        object_id = None if entity is None else self.scene.object_id_of(entity)
        proxy: EntityProxy | None = None
        if object_id:
            try:
                proxy = self.session.find_entity(object_id)
            except LiveControlError:
                proxy = None
        self._objects[name] = proxy
        return proxy

    def object_pose(self, name: str) -> dict[str, Any] | None:
        """World pose of a scene object by name, or None when it is not present."""
        proxy = self.object_proxy(name)
        if proxy is None:
            return None
        transform = proxy.transform
        return {"position": np.asarray(transform["translation"], dtype=float),
                "quaternion_wxyz": np.asarray(transform["rotation_wxyz"], dtype=float),
                "rotation_matrix": quaternion_to_matrix(transform["rotation_wxyz"])}

    def ensure_dynamic_payload(self, entity: int, *, mass: float = 0.2) -> bool:
        """Ensure a released object is a gravity-driven MuJoCo rigid body.

        Route-A scene application normally configures this at creation time. The guard is
        still needed for older scenes and for objects authored through the editor: a grasp
        can carry such an object kinematically, but release would otherwise leave it as a
        static body. Return whether a scene/backend rebuild was required.
        """
        if (self.scene.is_dynamic_object(entity) and self.scene.has_gravity(entity)):
            return False
        object_id = self.scene.object_id_of(entity)
        if not object_id:
            return False
        proxy = self.session.find_entity(object_id)
        proxy.set_rigid_body(enabled=True, dynamic=True, gravity=True,
                             mass=float(mass), friction=(1.0, 1.0, 1.0))
        self.refresh_scene()
        return True

    def refresh_scene(self) -> None:
        """Rebuild the scene index and drop resolved proxies, after the scene changed."""
        self._objects.clear()
        self.scene.refresh()
        self.invalidate()

    # ------------------------------------------------------------------ context

    def get_context(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "current_joint_state": self.get_joint_positions(),
            "current_pose": self.get_end_effector_pose(),
            "robot_model": self.robot,
            "planning_scene": {"endpoint": self.session.endpoint, "model": self.robot,
                               "scene": self.scene},
            "obstacle_state": {},
            "controller_state": {"ready": True, "mode": f"live_{self.execution_mode}"},
            "runtime": self,
        }

    # ------------------------------------------------------------------ execution

    def execute_trajectory(self, trajectory: Mapping[str, Any], *,
                           timeout: float = 30.0) -> dict[str, Any]:
        """Track joint waypoints, kinematically or through the joint controller.

        `kinematic` writes each waypoint straight in and never advances the clock, so
        the result is an animation of the plan: one request per waypoint, and the arm
        passes through exactly the configurations that were planned. `actuator`
        commands the controller and steps until the arm is within tolerance, which
        costs far more but is the only mode where contact resolves and a carried object
        comes along -- the arm may also fall short of a waypoint, which is the
        simulation disagreeing with the plan rather than an error.
        """
        waypoints = np.asarray(trajectory["waypoints"], dtype=float)
        if waypoints.ndim != 2 or waypoints.shape[1] != len(self.joint_names):
            raise ValueError("Trajectory waypoints must have shape (N, 6).")
        start_time = time.perf_counter()
        tolerance = float(trajectory.get("execution_tolerance", 2e-3))
        per_waypoint_steps = int(trajectory.get("steps_per_waypoint",
                                               self.steps_per_waypoint))
        if self.execution_mode == "actuator" and self.max_joint_speed_deg is not None:
            # A capped servo needs proportionally longer to cover the same joint travel, so
            # the step budget grows with the cap rather than staying at the default. Without
            # this, a slow arm simply stops short of each waypoint when the budget runs out
            # and the motion silently under-travels. Sized from the largest single-waypoint
            # step in this trajectory, doubled for the servo's exponential approach to
            # tolerance, and floored at the default so a cap never buys fewer steps.
            biggest = (float(np.max(np.abs(np.diff(waypoints, axis=0))))
                       if len(waypoints) > 1 else 0.0)
            seconds = biggest / max(float(np.radians(self.max_joint_speed_deg)), 1e-9)
            needed = int(np.ceil(2.0 * seconds / self.fixed_time_step))
            per_waypoint_steps = max(per_waypoint_steps, needed)
        playback_duration = max(
            float(trajectory.get("duration", 0.0)) / self.playback_speed,
            self.minimum_playback_duration)
        execution_waypoints = (
            resample_for_playback(waypoints, playback_duration, self.playback_fps)
            if self.execution_mode == "kinematic" and self.realtime else waypoints)
        next_frame_time = time.perf_counter()
        timestamps = trajectory.get("time_from_start")
        timed_steps = None
        if self.execution_mode == "actuator" and timestamps is not None:
            times = np.asarray(timestamps, dtype=float)
            if (times.shape != (len(waypoints),) or not np.all(np.isfinite(times))
                    or times[0] != 0 or np.any(np.diff(times) <= 0)):
                raise ValueError("Trajectory timestamps must start at zero and strictly increase.")
            # Consume the retimer's schedule, rather than converging independently
            # at every dense sample (which stretches seconds into tens of seconds).
            ticks = np.ceil(times / self.fixed_time_step - 1e-9).astype(int)
            timed_steps = np.diff(ticks)
        for index, waypoint in enumerate(execution_waypoints[1:]):
            # Kinematic replay is a sequence of authoritative pose writes.  Its wall-clock
            # cost is dominated by one RPC and optional snapshot capture per waypoint, so a
            # long path can legitimately exceed its simulated duration.  Timeout is still
            # meaningful for actuator stepping, where it detects a stalled controller; it
            # must not reject a kinematic path merely because it has many waypoints.
            if (self.execution_mode != "kinematic" and timed_steps is None
                    and time.perf_counter() - start_time > timeout):
                raise TimeoutError("Trajectory execution timed out.")
            if self.execution_mode == "kinematic":
                # Kinematic replay is a display of the plan, not a simulation of it, so
                # the pose is written straight in and the clock never advances. One
                # request per waypoint instead of a command plus a step burst.
                self.set_arm_positions(waypoint)
                self._sync_viewer()
                if self.realtime:
                    next_frame_time += 1.0 / self.playback_fps
                    delay = next_frame_time - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                if callable(self.waypoint_observer):
                    self.waypoint_observer()
                continue
            remaining_steps = int(timed_steps[index]) if timed_steps is not None else per_waypoint_steps
            if timed_steps is not None:
                # A 50 FPS timeline is not a 50 Hz actuator. Interpolate the
                # command at the physics rate so a held payload does not see
                # a new position step followed by ten identical servo targets.
                previous = execution_waypoints[index]
                segment = getattr(self.robot, "execute_joint_segment", None)
                if callable(segment) and remaining_steps:
                    full = self.expand_joint_batch([previous, waypoint])
                    gripper = getattr(self, "gripper_controller", None)
                    if gripper is not None and hasattr(gripper, "preserve_commanded_target"):
                        full = np.asarray([gripper.preserve_commanded_target(row) for row in full])
                    for first_tick in range(0, remaining_steps, 100):
                        last_tick = min(first_tick + 100, remaining_steps)
                        start = full[0] + (full[1] - full[0]) * first_tick / remaining_steps
                        end = full[0] + (full[1] - full[0]) * last_tick / remaining_steps
                        sim_time = segment(start.tolist(), end.tolist(), last_tick-first_tick,
                                           paced=False, **self._command_tuning())
                        self._simulation_time = sim_time
                    self.invalidate()
                    self._sync_viewer()
                else:
                    for tick in range(1, remaining_steps + 1):
                        self.command_arm(previous + (waypoint - previous) * tick / remaining_steps)
                        self.step()
                remaining_steps = 0
            else:
                self.command_arm(waypoint)
            while remaining_steps > 0:
                # `step(count)` is one RPC to the live simulator.  Advance a small
                # batch while retaining the same total number of MuJoCo steps; this
                # removes hundreds of round trips from a carried-object trajectory.
                batch = min(ACTUATOR_STEP_BATCH, remaining_steps)
                self.step(batch)
                remaining_steps -= batch
                if timed_steps is None and np.max(np.abs(self.get_joint_positions() - waypoint)) <= tolerance:
                    break
            if callable(self.waypoint_observer):
                self.waypoint_observer()
        if timed_steps is not None:
            # Only final convergence is a servo wait. Bound it in simulation time;
            # RPC and snapshot latency must not truncate a progressing trajectory.
            remaining = max(1, int(np.ceil(timeout / self.fixed_time_step)))
            while np.max(np.abs(self.get_joint_positions() - waypoints[-1])) > tolerance:
                if remaining <= 0:
                    raise TimeoutError("Trajectory execution timed out during final convergence.")
                batch = min(ACTUATOR_STEP_BATCH, remaining)
                self.step(batch)
                remaining -= batch
                if callable(self.waypoint_observer):
                    self.waypoint_observer()
        if self.execution_mode == "kinematic" and len(execution_waypoints) > 1:
            # Direct writes leave the backends on the old geometry. Synced once at the
            # end rather than per waypoint: nothing queries contact mid-replay, and a
            # sync per waypoint would cost more than the writes it follows.
            self.sync_backends()
        final_joint_state = self.get_joint_positions()
        return {
            "final_joint_state": final_joint_state.tolist(),
            "final_pose": serializable_pose(self.get_end_effector_pose()),
            "joint_error": float(np.max(np.abs(final_joint_state - waypoints[-1]))),
            "execution_time": time.perf_counter() - start_time,
            "mode": self.execution_mode,
            # True when a cap was requested, which in kinematic mode is not a claim
            # about speed: a direct write has no servo to limit.
            "servo_limit_applied": bool(self._servo_limits)
            and self.execution_mode == "actuator",
        }

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.session.start()

    def pause(self) -> None:
        self.session.pause()

    def attach_viewer(self, viewer: Any | None) -> None:
        """Attach anything with a `sync()` to be notified as motion advances."""
        self.viewer = viewer

    def _sync_viewer(self) -> None:
        if self.viewer is not None:
            self.viewer.sync()

    def save_state(self, path: str | Path) -> None:
        self.session.save_state_file(str(path))

    def restore_state(self, path: str | Path) -> None:
        self.session.restore_state_file(str(path))
        self.refresh_scene()

    def close(self) -> None:
        """Drop the connection. The far-side instance keeps running."""
        self.session.close()

    def __enter__(self) -> "LiveSceneRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _serializable_pose(pose: Mapping[str, Any]) -> dict[str, Any]:
        return serializable_pose(pose)


def resample_for_playback(waypoints: np.ndarray, duration: float,
                          fps: float) -> np.ndarray:
    """Resample a joint path by arc length for smooth fixed-rate display."""
    if len(waypoints) < 2:
        return waypoints
    segment_lengths = np.linalg.norm(np.diff(waypoints, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(segment_lengths)))
    if cumulative[-1] < 1e-12:
        return np.repeat(waypoints[:1], max(2, int(duration * fps) + 1), axis=0)
    frame_count = max(2, int(np.ceil(duration * fps)) + 1)
    samples = np.linspace(0.0, cumulative[-1], frame_count)
    result = np.empty((frame_count, waypoints.shape[1]), dtype=float)
    for joint_index in range(waypoints.shape[1]):
        result[:, joint_index] = np.interp(samples, cumulative, waypoints[:, joint_index])
    return result


def iter_pairs(values: Iterable[Any]) -> Iterable[tuple[Any, Any]]:
    """Consecutive pairs of an iterable, for path segment walks."""
    items = list(values)
    return zip(items[:-1], items[1:])
