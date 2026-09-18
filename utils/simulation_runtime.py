"""MuJoCo scene loading, robot-state access, and simulated execution."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import mujoco
import numpy as np


UR5E_JOINT_NAMES = (
    "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
    "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
)


class SceneRobotRuntime:
    """Own a MuJoCo model/data pair and expose the UR5e runtime state."""

    def __init__(self, scene_path: str | Path, *, end_effector_site: str = "robotiq_2f85_pinch", realtime: bool = False, execution_mode: str = "kinematic", playback_speed: float = 1.0, minimum_playback_duration: float = 0.0, playback_fps: float = 60.0) -> None:
        self.scene_path = Path(scene_path).expanduser().resolve()
        if not self.scene_path.is_file():
            raise FileNotFoundError(f"Scene file not found: {self.scene_path}")
        self.model = mujoco.MjModel.from_xml_path(str(self.scene_path))
        self.data = mujoco.MjData(self.model)
        home_key = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "home")
        if home_key >= 0:
            mujoco.mj_resetDataKeyframe(self.model, self.data, home_key)
        self.realtime = realtime
        if execution_mode not in {"kinematic", "actuator"}:
            raise ValueError("execution_mode must be kinematic or actuator.")
        self.execution_mode = execution_mode
        self.playback_speed = max(float(playback_speed), 0.01)
        self.minimum_playback_duration = max(float(minimum_playback_duration), 0.0)
        self.playback_fps = max(float(playback_fps), 15.0)
        self.viewer = None
        self.joint_names = UR5E_JOINT_NAMES
        self.joint_ids = np.asarray([self._name_id(mujoco.mjtObj.mjOBJ_JOINT, name) for name in self.joint_names], dtype=int)
        self.qpos_indices = self.model.jnt_qposadr[self.joint_ids].astype(int)
        self.dof_indices = self.model.jnt_dofadr[self.joint_ids].astype(int)
        self.actuator_ids = self._find_joint_actuators()
        if home_key >= 0:
            self.data.ctrl[self.actuator_ids] = self.data.qpos[self.qpos_indices]
        self.end_effector_site = end_effector_site
        self.end_effector_site_id = self._name_id(mujoco.mjtObj.mjOBJ_SITE, end_effector_site)
        mujoco.mj_forward(self.model, self.data)

    def _name_id(self, object_type: mujoco.mjtObj, name: str) -> int:
        object_id = mujoco.mj_name2id(self.model, object_type, name)
        if object_id < 0:
            raise ValueError(f"MuJoCo object not found: {name}")
        return object_id

    def _find_joint_actuators(self) -> np.ndarray:
        actuator_ids = []
        for joint_id, joint_name in zip(self.joint_ids, self.joint_names):
            matches = np.flatnonzero(self.model.actuator_trnid[:, 0] == joint_id)
            if not len(matches):
                raise ValueError(f"No actuator controls joint: {joint_name}")
            actuator_ids.append(int(matches[0]))
        return np.asarray(actuator_ids, dtype=int)

    @property
    def joint_limits(self) -> np.ndarray:
        return self.model.jnt_range[self.joint_ids].copy()

    def get_joint_positions(self) -> np.ndarray:
        return self.data.qpos[self.qpos_indices].copy()

    def get_gripper_width(self) -> float | None:
        """Return the Robotiq opening width represented by the scene state."""
        controller = getattr(self, "gripper_controller", None)
        if controller is not None:
            return float(controller.width)
        # The gripper controller is created lazily by grasp/release skills.  A
        # move before the first gripper action must still get a useful width.
        names = (
            "robotiq_2f85_left_driver_joint",
            "robotiq_2f85_right_driver_joint",
        )
        try:
            joint_ids = [self._name_id(mujoco.mjtObj.mjOBJ_JOINT, name) for name in names]
        except ValueError:
            return None
        qpos = self.model.jnt_qposadr[np.asarray(joint_ids, dtype=int)].astype(int)
        angle = float(np.mean(self.data.qpos[qpos]))
        return float(np.clip(0.085 * (1.0 - angle / 0.8), 0.0, 0.085))

    def get_end_effector_pose(self) -> dict[str, Any]:
        rotation = self.data.site_xmat[self.end_effector_site_id].reshape(3, 3).copy()
        quaternion = np.empty(4, dtype=float)
        mujoco.mju_mat2Quat(quaternion, rotation.reshape(-1))
        return {"position": self.data.site_xpos[self.end_effector_site_id].copy(), "quaternion_wxyz": quaternion, "rotation_matrix": rotation}

    def get_context(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "current_joint_state": self.get_joint_positions(),
            "current_pose": self.get_end_effector_pose(),
            "robot_model": self.model,
            "planning_scene": {"scene_path": str(self.scene_path), "model": self.model},
            "obstacle_state": {},
            "controller_state": {"ready": True, "mode": f"mujoco_{self.execution_mode}"},
            "runtime": self,
        }

    def execute_trajectory(self, trajectory: Mapping[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        """Track joint waypoints with the scene's position actuators."""
        waypoints = np.asarray(trajectory["waypoints"], dtype=float)
        if waypoints.ndim != 2 or waypoints.shape[1] != len(self.joint_ids):
            raise ValueError("Trajectory waypoints must have shape (N, 6).")
        if "time_from_start" in trajectory:
            return self._execute_timed_trajectory(trajectory, timeout=timeout)
        start_time = time.perf_counter()
        tolerance = float(trajectory.get("execution_tolerance", 2e-3))
        per_waypoint_steps = int(trajectory.get("steps_per_waypoint", 500))
        playback_duration = max(float(trajectory.get("duration", 0.0)) / self.playback_speed, self.minimum_playback_duration)
        execution_waypoints = _resample_for_playback(waypoints, playback_duration, self.playback_fps) if self.execution_mode == "kinematic" and self.realtime else waypoints
        next_frame_time = time.perf_counter()
        for waypoint in execution_waypoints[1:]:
            if time.perf_counter() - start_time > timeout:
                raise TimeoutError("MuJoCo trajectory execution timed out.")
            if self.execution_mode == "kinematic":
                self.data.qpos[self.qpos_indices] = waypoint
                self.data.qvel[self.dof_indices] = 0.0
                mujoco.mj_forward(self.model, self.data)
                gripper_controller = getattr(self, "gripper_controller", None)
                if gripper_controller is not None:
                    gripper_controller.sync_held_object()
                if self.viewer is not None:
                    self.viewer.sync()
                if self.realtime:
                    next_frame_time += 1.0 / self.playback_fps
                    delay = next_frame_time - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                continue
            self.data.ctrl[self.actuator_ids] = waypoint
            for _ in range(per_waypoint_steps):
                mujoco.mj_step(self.model, self.data)
                if self.viewer is not None:
                    self.viewer.sync()
                if np.max(np.abs(self.get_joint_positions() - waypoint)) <= tolerance:
                    break
                if self.realtime:
                    time.sleep(self.model.opt.timestep)
        final_joint_state = self.get_joint_positions()
        return {
            "final_joint_state": final_joint_state.tolist(),
            "final_pose": self._serializable_pose(self.get_end_effector_pose()),
            "joint_error": float(np.max(np.abs(final_joint_state - waypoints[-1]))),
            "execution_time": time.perf_counter() - start_time,
        }

    def attach_viewer(self, viewer: Any | None) -> None:
        """Attach a passive viewer so execution updates are displayed."""
        self.viewer = viewer

    def _execute_timed_trajectory(self, trajectory: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        """Track a timestamped reference without arc-length reparameterization.

        Playback speed affects only kinematic visualization. Actuator execution
        follows simulation time and checks settling only at the final target.
        """
        points = np.asarray(trajectory["waypoints"], dtype=float)
        timestamps = np.asarray(trajectory["time_from_start"], dtype=float)
        if (timestamps.shape != (len(points),) or len(points) == 0
                or not np.all(np.isfinite(points)) or not np.all(np.isfinite(timestamps))
                or timestamps[0] != 0 or np.any(np.diff(timestamps) <= 0)):
            raise ValueError("Trajectory timestamps must start at zero and strictly increase.")
        duration = float(timestamps[-1])
        if duration > timeout and (self.execution_mode == "actuator" or not self.realtime):
            raise TimeoutError("Timed trajectory duration exceeds execution timeout.")
        started = time.perf_counter()
        if self.execution_mode == "kinematic":
            display_duration = max(duration / self.playback_speed, self.minimum_playback_duration) if self.realtime else duration
            if self.realtime and display_duration > timeout:
                raise TimeoutError("Trajectory playback duration exceeds execution timeout.")
            frames = max(1, int(np.ceil(display_duration * self.playback_fps))) if self.realtime else max(1, len(points)-1)
            for index in range(frames+1):
                if time.perf_counter()-started > timeout:
                    raise TimeoutError("MuJoCo trajectory execution timed out.")
                t = duration * index / frames
                self.data.qpos[self.qpos_indices] = _sample_timed_path(points, timestamps, t)
                self.data.qvel[self.dof_indices] = 0.0
                mujoco.mj_forward(self.model, self.data)
                controller = getattr(self, "gripper_controller", None)
                if controller is not None:
                    controller.sync_held_object()
                if self.realtime:
                    delay = started + display_duration*index/frames - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                if self.viewer is not None:
                    self.viewer.sync()
        else:
            dt = float(self.model.opt.timestep)
            steps = int(np.ceil(duration/dt))
            settle_steps = int(trajectory.get("steps_per_waypoint", 500))
            tolerance = float(trajectory.get("execution_tolerance", 2e-3))
            next_display = 0.0
            for index in range(steps + settle_steps):
                simulation_time = index*dt
                if simulation_time > timeout or time.perf_counter()-started > timeout:
                    raise TimeoutError("MuJoCo trajectory execution timed out.")
                self.data.ctrl[self.actuator_ids] = _sample_timed_path(points, timestamps, min(simulation_time, duration))
                mujoco.mj_step(self.model, self.data)
                if self.viewer is not None and simulation_time >= next_display:
                    self.viewer.sync()
                    next_display += 1.0/self.playback_fps
                if self.realtime:
                    delay = started + (index+1)*dt - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                if index >= steps and np.max(np.abs(self.get_joint_positions()-points[-1])) <= tolerance:
                    break
        final = self.get_joint_positions()
        return {"final_joint_state": final.tolist(),
                "final_pose": self._serializable_pose(self.get_end_effector_pose()),
                "joint_error": float(np.max(np.abs(final-points[-1]))),
                "execution_time": time.perf_counter()-started,
                "timed_execution": True}

    @staticmethod
    def _serializable_pose(pose: Mapping[str, Any]) -> dict[str, Any]:
        return {"position": np.asarray(pose["position"]).tolist(), "quaternion_wxyz": np.asarray(pose["quaternion_wxyz"]).tolist()}


def _resample_for_playback(waypoints: np.ndarray, duration: float, fps: float) -> np.ndarray:
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


def _sample_timed_path(points: np.ndarray, timestamps: np.ndarray, time_value: float) -> np.ndarray:
    """Interpolate sampled positions by time rather than by geometric arc length."""
    return np.asarray([np.interp(time_value, timestamps, points[:, j]) for j in range(points.shape[1])])
