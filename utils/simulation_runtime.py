"""MuJoCo scene loading, robot-state access, and simulated execution."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import mujoco
import numpy as np

from sensors.camera import CameraFrame, CameraManager, save_camera_frame
from sensors.camera_mujoco import MujocoCamera
from sensors.camera_mujoco.scene import load_scene_with_cameras


UR5E_JOINT_NAMES = (
    "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
    "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
)


class SceneRobotRuntime:
    """Own a MuJoCo model/data pair and expose the UR5e runtime state."""

    def __init__(
        self,
        scene_path: str | Path,
        *,
        end_effector_site: str = "robotiq_2f85_pinch",
        realtime: bool = False,
        execution_mode: str = "kinematic",
        playback_speed: float = 1.0,
        minimum_playback_duration: float = 0.0,
        playback_fps: float = 60.0,
        camera_width: int = 640,
        camera_height: int = 480,
        scene_json_path: str | Path | None = None,
    ) -> None:
        self.scene_path = Path(scene_path).expanduser().resolve()
        if not self.scene_path.is_file():
            raise FileNotFoundError(f"Scene file not found: {self.scene_path}")
        self.model, self._camera_resolutions = load_scene_with_cameras(self.scene_path, scene_json_path)
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
        self.camera_width = int(camera_width)
        self.camera_height = int(camera_height)
        if self.camera_width <= 0 or self.camera_height <= 0:
            raise ValueError("camera_width and camera_height must be positive.")
        self._cameras: CameraManager | None = None
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
        self._kinematics = None

    @property
    def kinematics(self):
        """One independent chain service per runtime; workers own separate runtimes."""
        if self._kinematics is None:
            # FK/Jacobian/IK are built from the canonical URDF.  MuJoCo is
            # only used here to obtain the initial world pose and the live
            # joint/base state that is needed while executing a trajectory.
            from libraries.eaik import load_urdf
            from libraries.kinematic_chain.mujoco import extract_chain_parameters

            parameters = extract_chain_parameters(self)
            base_id = int(
                self.model.body_parentid[self.model.jnt_bodyid[self.joint_ids[0]]]
            )
            urdf_path = Path(__file__).resolve().parents[1] / "assets" / "models" / "robots" / "30" / "ur5e_arm.urdf"
            # The URDF tip is the robot attachment frame.  The runtime TCP is
            # the calibrated pinch site used by the existing MuJoCo scene.
            tcp_transform = np.array(
                [[0.0, 1.0, 0.0, 0.0],
                 [-1.0, 0.0, 0.0, 0.0],
                 [0.0, 0.0, 1.0, 0.1558],
                 [0.0, 0.0, 0.0, 1.0]],
                dtype=float,
            )

            def live_base_transform(_joints):
                result = np.eye(4)
                result[:3, :3] = self.data.xmat[base_id].reshape(3, 3)
                result[:3, 3] = self.data.xpos[base_id]
                return result

            self._kinematics = load_urdf(
                urdf_path,
                base_link="base",
                tip_link="attachment_site",
                tcp_transform=tcp_transform,
                world_from_base=np.asarray(parameters["world_from_base_zero"]),
                current_joints_provider=self.get_joint_positions,
                base_transform_provider=live_base_transform,
            )
        return self._kinematics

    @property
    def cameras(self) -> CameraManager:
        """Return the lazily-created global and wrist RGB-D cameras."""
        if self._cameras is None:
            names = {}
            for camera_index in range(self.model.ncam):
                name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_CAMERA, camera_index)
                if name:
                    names[name] = self._camera_resolutions.get(
                        name, (self.camera_width, self.camera_height)
                    )
            if not names:
                raise RuntimeError("No scene cameras available; provide a scene JSON with camera definitions.")
            self._cameras = CameraManager({
                name: MujocoCamera(self.model, self.data, name, width=width, height=height)
                for name, (width, height) in names.items()
            })
        return self._cameras

    def get_camera_frame(self, camera_id: str) -> CameraFrame:
        """Capture RGB and metric depth from a named simulated camera."""
        camera_id = {
            "global": "global_camera",
            "wrist": "wrist_camera",
        }.get(camera_id, camera_id)
        return self.cameras.get_current_frame(camera_id)

    def save_camera_frame(
        self, camera_id: str, output_dir: str | Path, *, prefix: str | None = None
    ) -> dict[str, Path]:
        """Capture and save RGB plus viewable and lossless depth files."""
        return save_camera_frame(
            self.get_camera_frame(camera_id), output_dir, prefix=prefix
        )

    def close(self) -> None:
        """Release offscreen renderers owned by this runtime."""
        if self._cameras is not None:
            self._cameras.close()
            self._cameras = None

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
        # The gripper controller is created lazily by grasp/release atomic_action.  A
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

    def get_gripper_positions(self) -> np.ndarray:
        """Read gripper driver joint positions without requiring a controller."""
        names = (
            "robotiq_2f85_left_driver_joint",
            "robotiq_2f85_right_driver_joint",
        )
        indices = [self._name_id(mujoco.mjtObj.mjOBJ_JOINT, name) for name in names]
        qpos = self.model.jnt_qposadr[np.asarray(indices, dtype=int)].astype(int)
        return self.data.qpos[qpos].copy()

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
        """Execute the path using kinematic playback or waypoint actuators."""
        waypoints = np.asarray(trajectory["waypoints"], dtype=float)
        if waypoints.ndim != 2 or waypoints.shape[1] != len(self.joint_ids) or len(waypoints) < 2:
            raise ValueError("Trajectory waypoints must have shape (N, 6).")
        start_time = time.perf_counter()
        playback_duration = max(float(trajectory.get("duration", 0.0)) / self.playback_speed, self.minimum_playback_duration)
        execution_waypoints = _resample_for_playback(waypoints, playback_duration, self.playback_fps) if self.execution_mode == "kinematic" and self.realtime else waypoints
        next_frame_time = time.perf_counter()
        if self.execution_mode == "actuator":
            return self._execute_actuator_trajectory(trajectory, waypoints, timeout=timeout)
        for waypoint in execution_waypoints[1:]:
            if time.perf_counter() - start_time > timeout:
                raise TimeoutError("MuJoCo trajectory execution timed out.")
            if self.execution_mode == "kinematic":
                self.data.qpos[self.qpos_indices] = waypoint
                self.data.qvel[self.dof_indices] = 0.0
                mujoco.mj_forward(self.model, self.data)
                if self.viewer is not None:
                    self.viewer.sync()
                if self.realtime:
                    next_frame_time += 1.0 / self.playback_fps
                    delay = next_frame_time - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                continue
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

    def _execute_actuator_trajectory(self, trajectory: Mapping[str, Any], waypoints: np.ndarray,
                                     *, timeout: float) -> dict[str, Any]:
        """Track each waypoint until tolerance or its step budget is reached."""
        started = time.perf_counter()
        simulation_start = float(self.data.time)
        dt = float(self.model.opt.timestep)
        tolerance = float(trajectory.get("execution_tolerance", 0.002))
        per_waypoint_steps = int(trajectory.get("steps_per_waypoint", 500))
        frames: list[dict[str, Any]] = []
        from control.gripper_controller import MujocoGripperController
        gripper_ids = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
                       for name in MujocoGripperController.driver_joint_names]
        gripper_addresses = [int(self.model.jnt_qposadr[j]) for j in gripper_ids if j >= 0]

        def sample() -> None:
            mujoco.mj_forward(self.model, self.data)
            pose = self.get_end_effector_pose()
            frames.append({"joint_positions_rad": self.get_joint_positions().tolist(),
                           "gripper_q": self.data.qpos[gripper_addresses].tolist(),
                           "gripper_width_m": self.get_gripper_width() or 0.0,
                           "end_effector_position_m": pose["position"].tolist(),
                           "end_effector_quaternion_wxyz": pose["quaternion_wxyz"].tolist()})

        sample()
        for waypoint in waypoints[1:]:
            if time.perf_counter() - started > timeout:
                raise TimeoutError("MuJoCo trajectory execution timed out.")
            self.data.ctrl[self.actuator_ids] = waypoint
            for _ in range(per_waypoint_steps):
                mujoco.mj_step(self.model, self.data)
                if float(self.data.time) - simulation_start >= len(frames) / self.playback_fps:
                    sample()
                if self.viewer is not None:
                    self.viewer.sync()
                if np.max(np.abs(self.get_joint_positions() - waypoint)) <= tolerance:
                    break
                if self.realtime:
                    time.sleep(dt)
        sample()
        final = self.get_joint_positions()
        return {"final_joint_state": final.tolist(),
                "final_pose": self._serializable_pose(self.get_end_effector_pose()),
                "joint_error": float(np.max(np.abs(final - waypoints[-1]))),
                "execution_time": time.perf_counter() - started,
                "simulation_duration": float(self.data.time) - simulation_start,
                "samples": frames}

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
