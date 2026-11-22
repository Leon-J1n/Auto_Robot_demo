"""Scene loading, robot-state access, and simulated execution on RobotSimPlatform.

Replaces the MuJoCo-backed SceneRobotRuntime. The public surface is unchanged, so the
skills, planners, and strategies that consume it need no edits: joint positions are
6-vectors in UR5e order, get_context returns the same keys, and execute_trajectory
returns the same result dictionary.

The platform indexes joints alphabetically across the whole robot -- 26 of them for a
loaded UR5e with a Robotiq gripper, only 12 of which move -- so this class owns the
mapping between that full vector and the 6 arm joints the skills speak in.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from . import bootstrap  # noqa: F401  (puts the SDK on sys.path)
from .scene_index import SceneIndex
from .transforms import matrix_to_quaternion, quaternion_to_matrix, serializable_pose

from sim_platform import Simulator
from sim_platform.sdk import Robot

UR5E_JOINT_NAMES = (
    "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
    "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
)
END_EFFECTOR_LINK = "robotiq_2f85_pinch"


class SceneRobotRuntime:
    """Own a platform simulator/world pair and expose the UR5e runtime state."""

    def __init__(
        self,
        scene_path: str | Path,
        *,
        end_effector_site: str = END_EFFECTOR_LINK,
        realtime: bool = False,
        execution_mode: str = "kinematic",
        playback_speed: float = 1.0,
        minimum_playback_duration: float = 0.0,
        playback_fps: float = 60.0,
        cache_directory: str | Path | None = None,
        fixed_time_step: float = 0.002,
        max_joint_speed_deg: float | None = None,
    ) -> None:
        self.scene_path = Path(scene_path).expanduser().resolve()
        if not self.scene_path.is_file():
            raise FileNotFoundError(f"Scene file not found: {self.scene_path}")
        if execution_mode not in {"kinematic", "actuator"}:
            raise ValueError("execution_mode must be kinematic or actuator.")
        if max_joint_speed_deg is not None and max_joint_speed_deg <= 0.0:
            raise ValueError("max_joint_speed_deg must be positive.")
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
        self.viewer = None

        # FCL is required: every physics query in this layer goes through the
        # contact-pair API, which the MuJoCo physics backend does not provide.
        self.simulator = Simulator(
            physics="mujoco", collision="fcl",
            cache_directory=str(cache_directory) if cache_directory else None,
        )
        self.world = self.simulator.create_world(fixed_time_step=fixed_time_step)
        self.simulator.restore_state_file(self.world, str(self.scene_path))
        self.simulator.start(self.world)

        self.robot = self._find_robot()
        self.joint_names = UR5E_JOINT_NAMES
        platform_names = self.robot.joint_names
        missing = [name for name in self.joint_names if name not in platform_names]
        if missing:
            raise ValueError(f"Scene robot is missing UR5e joints: {', '.join(missing)}")
        # Named "qpos_indices" because planners and controllers already address the
        # runtime through that attribute; here it indexes the platform joint vector.
        self.qpos_indices = np.asarray(
            [platform_names.index(name) for name in self.joint_names], dtype=int)
        self.dof_indices = self.qpos_indices
        self.actuator_ids = self.qpos_indices
        self.platform_joint_count = self.robot.joint_count

        self.end_effector_site = end_effector_site
        self.end_effector_link = self.robot.get_link(end_effector_site)
        self.end_effector_site_id = self.end_effector_link.handle
        self.scene = SceneIndex(self.world, self.robot)
        self._all_limits = self.robot.joint_limits

    def _find_robot(self) -> Robot:
        """Find the scene's robot, identified by carrying a robot component."""
        candidates = [info for info in self.world.editor_entities()
                      if "robot" in info.flags]
        if not candidates:
            raise ValueError(f"No robot found in scene: {self.scene_path}")
        if len(candidates) > 1:
            # Prefer the one that actually articulates; a scene could hold a fixture.
            named = ", ".join(sorted(info.name for info in candidates))
            movable = [info for info in candidates
                       if Robot(self.world, info.entity).joint_count > 0]
            if len(movable) != 1:
                raise ValueError(f"Scene holds several robots ({named}); "
                                 "load a scene with exactly one.")
            candidates = movable
        return Robot(self.world, candidates[0].entity)

    # ---------------------------------------------------------------- joint state

    @property
    def joint_limits(self) -> np.ndarray:
        """(6, 2) lower/upper bounds for the arm joints."""
        return self._all_limits[self.qpos_indices].copy()

    def get_joint_positions(self) -> np.ndarray:
        return self.robot.joint_state[0][self.qpos_indices].copy()

    def full_joint_positions(self) -> np.ndarray:
        """The complete platform joint vector, including the gripper and fixed joints."""
        return self.robot.joint_state[0].copy()

    def expand_joints(self, joints: Sequence[float],
                      base: Sequence[float] | None = None) -> np.ndarray:
        """Place a 6-vector into a full platform joint vector.

        Everything not an arm joint keeps its current value, so a query about the arm
        never disturbs the gripper's pose.
        """
        values = np.asarray(joints, dtype=float)
        if values.shape != (6,):
            raise ValueError("UR5e joint vector must contain 6 values.")
        full = self.full_joint_positions() if base is None else np.asarray(base, dtype=float).copy()
        full[self.qpos_indices] = values
        return full

    def set_joint_positions(self, joints: Sequence[float]) -> None:
        """Pose the arm directly, leaving the gripper as it is."""
        self.robot.set_joint_positions(self.expand_joints(joints), limits="clamp")
        self._sync_viewer()

    # ---------------------------------------------------------------- poses

    def link_pose(self, link: Any | str) -> dict[str, Any]:
        entity = self.robot.get_link(link) if isinstance(link, str) else link
        transform = entity.transform
        rotation = quaternion_to_matrix(transform.rotation_wxyz)
        return {"position": np.asarray(transform.translation, dtype=float),
                "quaternion_wxyz": np.asarray(transform.rotation_wxyz, dtype=float),
                "rotation_matrix": rotation}

    def get_end_effector_pose(self) -> dict[str, Any]:
        return self.link_pose(self.end_effector_link)

    def object_pose(self, name: str) -> dict[str, Any] | None:
        """World pose of a scene object by name, or None when it is not present."""
        entity = self.scene.object_entity(name)
        if entity is None:
            return None
        transform = self.world.entity(entity).transform
        return {"position": np.asarray(transform.translation, dtype=float),
                "quaternion_wxyz": np.asarray(transform.rotation_wxyz, dtype=float),
                "rotation_matrix": quaternion_to_matrix(transform.rotation_wxyz)}

    # ---------------------------------------------------------------- context

    def get_context(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "current_joint_state": self.get_joint_positions(),
            "current_pose": self.get_end_effector_pose(),
            "robot_model": self.robot,
            "planning_scene": {"scene_path": str(self.scene_path), "model": self.robot,
                               "scene": self.scene},
            "obstacle_state": {},
            "controller_state": {"ready": True, "mode": f"robotsim_{self.execution_mode}"},
            "runtime": self,
        }

    # ---------------------------------------------------------------- execution

    def execute_trajectory(self, trajectory: Mapping[str, Any], *,
                           timeout: float = 30.0) -> dict[str, Any]:
        """Track joint waypoints, kinematically or through the joint controller."""
        waypoints = np.asarray(trajectory["waypoints"], dtype=float)
        if waypoints.ndim != 2 or waypoints.shape[1] != len(self.joint_names):
            raise ValueError("Trajectory waypoints must have shape (N, 6).")
        start_time = time.perf_counter()
        tolerance = float(trajectory.get("execution_tolerance", 2e-3))
        default_steps = int(trajectory.get("steps_per_waypoint", 500))
        playback_duration = max(
            float(trajectory.get("duration", 0.0)) / self.playback_speed,
            self.minimum_playback_duration)
        parameterized_times = np.asarray(trajectory.get("time_from_start", ()), dtype=float)
        has_timing = (
            self.execution_mode == "actuator"
            and parameterized_times.shape == (len(waypoints),)
            and np.all(np.isfinite(parameterized_times))
            and np.all(np.diff(parameterized_times) >= 0.0)
            and parameterized_times[-1] > 0.0
        )
        execution_waypoints = (
            _resample_for_playback(waypoints, playback_duration, self.playback_fps)
            if self.execution_mode == "kinematic" and self.realtime else waypoints)
        next_frame_time = time.perf_counter()
        if has_timing:
            # Time-parameterized Move trajectories are sampled at explicit timestamps.
            # Consume those timestamps directly instead of waiting for each servo target
            # to converge; otherwise the controller turns every sample into an unrelated
            # stop-and-go segment and the measured duration no longer describes the path.
            segment_steps = np.maximum(1, np.rint(
                np.diff(parameterized_times) / self.fixed_time_step).astype(int))
        elif self.execution_mode == "actuator" and len(execution_waypoints) > 1:
            # For legacy trajectories without timestamps, allocate the planned duration
            # across segments. The old fixed 500-step budget was a hidden one-second pause
            # per waypoint (500 * 0.002 s).
            segment_lengths = np.max(np.abs(np.diff(execution_waypoints, axis=0)), axis=1)
            total_length = float(np.sum(segment_lengths))
            if "steps_per_waypoint" in trajectory:
                segment_steps = np.full(len(segment_lengths), max(1, default_steps), dtype=int)
            elif total_length <= 1e-12:
                segment_steps = np.ones(len(segment_lengths), dtype=int)
            else:
                total_steps = max(len(segment_lengths), int(np.ceil(
                    playback_duration / self.fixed_time_step)))
                raw = total_steps * segment_lengths / total_length
                segment_steps = np.maximum(1, np.floor(raw).astype(int))
                remainder = total_steps - int(np.sum(segment_steps))
                if remainder > 0:
                    order = np.argsort(-(raw - np.floor(raw)))
                    segment_steps[order[:remainder]] += 1
        else:
            segment_steps = np.full(max(0, len(execution_waypoints) - 1),
                                    max(1, default_steps), dtype=int)
        for index, waypoint in enumerate(execution_waypoints[1:]):
            # Kinematic replay performs direct scene writes and does not advance physics.
            # Do not use the total RPC wall time as a motion timeout; long paths are valid
            # and their capture cost is unrelated to controller progress.
            if self.execution_mode != "kinematic" and time.perf_counter() - start_time > timeout:
                raise TimeoutError("Trajectory execution timed out.")
            if self.execution_mode == "kinematic":
                self.set_joint_positions(waypoint)
                if self.realtime:
                    next_frame_time += 1.0 / self.playback_fps
                    delay = next_frame_time - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                continue
            self.robot.command_joint_positions(self.expand_joints(waypoint),
                                               **self._servo_limits)
            for _ in range(int(segment_steps[index])):
                # Agent execution is an offline batch; browser real-time pacing must
                # never throttle physics ticks here.
                self.simulator.step_unpaced(self.world)
                self._sync_viewer()
                if not has_timing and np.max(np.abs(self.get_joint_positions() - waypoint)) <= tolerance:
                    break
            if has_timing and self.realtime:
                # Simulator.step advances simulation time without wall-clock pacing when
                # the runtime is used for offline control. A parameterized trajectory is a
                # playback contract as well, so keep wall time aligned with its timestamp
                # unless physics/rendering itself has already overrun the segment.
                target_wall_time = start_time + float(parameterized_times[index + 1]) / self.playback_speed
                delay = target_wall_time - time.perf_counter()
                if delay > 0.0:
                    time.sleep(delay)
        final_joint_state = self.get_joint_positions()
        return {
            "final_joint_state": final_joint_state.tolist(),
            "final_pose": serializable_pose(self.get_end_effector_pose()),
            "joint_error": float(np.max(np.abs(final_joint_state - waypoints[-1]))),
            "execution_time": time.perf_counter() - start_time,
        }

    @property
    def _servo_limits(self) -> dict[str, float]:
        """Keyword arguments capping the joint servo, empty when no cap was asked for.

        Passed per command rather than configured once because the platform's servo takes
        the limit with the command; there is no persistent setting to write.
        """
        if self.max_joint_speed_deg is None:
            return {}
        return {"velocity_limit": float(np.radians(self.max_joint_speed_deg))}

    def step(self, count: int = 1) -> None:
        self.simulator.step_unpaced(self.world, count)
        self._sync_viewer()

    def attach_viewer(self, viewer: Any | None) -> None:
        """Attach a viewer so execution updates are displayed."""
        self.viewer = viewer

    def _sync_viewer(self) -> None:
        if self.viewer is not None:
            self.viewer.sync()

    def save_state(self, path: str | Path) -> None:
        self.simulator.save_state_file(self.world, str(path))

    def close(self) -> None:
        self.simulator.close()

    def __enter__(self) -> "SceneRobotRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _serializable_pose(pose: Mapping[str, Any]) -> dict[str, Any]:
        return serializable_pose(pose)


def _resample_for_playback(waypoints: np.ndarray, duration: float,
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
