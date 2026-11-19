"""MuJoCo execution plugin. MJCF is generated from the canonical JSON/URDF."""
from contextlib import nullcontext
import json
from pathlib import Path
import time

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from ...resources import component


from .scene_compiler import compile_scene
class MuJoCoExecutionBackend:
    name = 'mujoco'

    def load(self, resources, output_dir, *, camera_scene_json=None):
        self.resources = resources
        self.scene_path = compile_scene(resources, output_dir)
        self._cameras = None
        self._camera_renderers = {}
        self._camera_resolutions = {}
        if camera_scene_json is not None:
            from sensors.camera_mujoco.scene import load_scene_with_cameras
            self.model, self._camera_resolutions = load_scene_with_cameras(
                self.scene_path, camera_scene_json)
        else:
            self.model = mujoco.MjModel.from_xml_path(str(self.scene_path))
        self.data = mujoco.MjData(self.model)
        self.joint_names = resources.joint_names
        self.joint_ids = np.array([self._id(mujoco.mjtObj.mjOBJ_JOINT, n) for n in self.joint_names])
        self.qpos_indices = self.model.jnt_qposadr[self.joint_ids]
        self.dof_indices = self.model.jnt_dofadr[self.joint_ids]
        self.actuator_ids = np.array([self._id(mujoco.mjtObj.mjOBJ_ACTUATOR, n + '_servo') for n in self.joint_names])
        self.tip_id = self._id(mujoco.mjtObj.mjOBJ_SITE, 'tcp')
        self.base_id = self._id(mujoco.mjtObj.mjOBJ_BODY, resources.robot_object['object_name_in_scene'])
        self.object_ids = {o['object_name_in_scene']: self._id(mujoco.mjtObj.mjOBJ_BODY, o['object_name_in_scene'])
                           for o in resources.scene['objects'] if o is not resources.robot_object}
        self.mechanism_bodies = set()
        for obj in resources.scene['objects']:
            for link in component(obj, 'ArticulatedModel').get('links', []):
                name = obj['object_name_in_scene'] + '__' + link
                self.object_ids[name] = self._id(mujoco.mjtObj.mjOBJ_BODY, name)
                self.mechanism_bodies.add(name)
        self.data.qpos[self.qpos_indices] = resources.initial_joints
        self.data.ctrl[self.actuator_ids] = resources.initial_joints
        source = json.loads(Path(resources.scene_file).read_text(encoding='utf-8'))
        self.scene_driver_joints = {}
        for obj in source.get('objects', []):
            driver = component(obj, 'RobotDriver')
            sequence = driver.get('joint_sequence') or []
            if not sequence:
                continue
            mapped = {}
            is_arm = obj['object_id_in_scene'] == resources.robot_object['object_id_in_scene']
            for joint in sequence:
                candidate = joint if is_arm or component(obj, 'RobotAttachment') else obj['object_name_in_scene'] + '__' + joint
                jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, candidate)
                if jid >= 0 and int(self.model.jnt_type[jid]) in (2, 3):
                    mapped[joint] = int(self.model.jnt_qposadr[jid])
            if len(mapped) == len(sequence):
                self.scene_driver_joints[obj['object_id_in_scene']] = mapped
            current = driver.get('now_joint_positions_rad')
            if current is not None:
                if set(current) != set(sequence) or len(mapped) != len(sequence):
                    raise ValueError('Cannot restore complete current joint state: ' + obj['object_name_in_scene'])
                for joint, address in mapped.items():
                    value = float(current[joint])
                    if not np.isfinite(value):
                        raise ValueError('Non-finite current joint state')
                    self.data.qpos[address] = value
                    actuator_name = ('grasp_servo' if joint == 'robotiq_2f85_right_driver_joint'
                                     else joint + '_servo')
                    actuator = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, actuator_name)
                    if actuator >= 0:
                        self.data.ctrl[actuator] = value
        self.frame_callback = None
        from execution.config import trajectory_settings
        self.trajectory_fps = trajectory_settings(resources.scene)['fps']
        if self.trajectory_fps * float(self.model.opt.timestep) > 1 + 1e-10:
            raise ValueError('trajectory.fps cannot exceed the physics step frequency')
        self.frame_interval = 1.0 / self.trajectory_fps
        self.viewer = None
        self.physics_steps = 0
        self.viewer_sync_count = 0
        self.status = 'Ready'
        self.realtime = False
        self.gripper_controller = None
        self.contact_target = None
        mujoco.mj_forward(self.model, self.data)

    def _id(self, kind, name):
        value = mujoco.mj_name2id(self.model, kind, name)
        if value < 0:
            raise ValueError(f'Missing MuJoCo resource: {name}')
        return value

    @property
    def joint_limits(self):
        return self.model.jnt_range[self.joint_ids].copy()

    def get_joint_positions(self):
        return self.data.qpos[self.qpos_indices].copy()

    def get_end_effector_pose(self):
        rotation = self.data.site_xmat[self.tip_id].reshape(3, 3).copy()
        return {'position': self.data.site_xpos[self.tip_id].copy(),
                'rotation_matrix': rotation,
                'quaternion_wxyz': Rotation.from_matrix(rotation).as_quat()[[3, 0, 1, 2]]}

    def get_context(self, request=None):
        return {'current_joint_state': self.get_joint_positions(),
                'current_pose': self.get_end_effector_pose(),
                'controller_state': {'ready': True, 'mode': 'mujoco_actuator'}}

    def get_control_from_camera(self, camera_id):
        """Read rigid hand-eye calibration from a live camera pose."""
        name = {'global': 'global_camera', 'wrist': 'wrist_camera'}.get(camera_id, camera_id)
        camera = self._id(mujoco.mjtObj.mjOBJ_CAMERA, name)

        def rigid_root(body):
            while body > 0 and self.model.body_jntnum[body] == 0:
                body = int(self.model.body_parentid[body])
            return body

        if rigid_root(int(self.model.cam_bodyid[camera])) != rigid_root(int(self.model.site_bodyid[self.tip_id])):
            raise ValueError('Observation camera must be rigidly attached to the TCP')
        control = self.get_end_effector_pose()
        rotation = np.asarray(control['rotation_matrix'])
        result = np.eye(4)
        result[:3, :3] = rotation.T @ self.data.cam_xmat[camera].reshape(3, 3)
        result[:3, 3] = rotation.T @ (self.data.cam_xpos[camera] - control['position'])
        return result

    def get_camera_frame(self, camera_id):
        from sensors.camera import CameraManager
        from sensors.camera_mujoco import MujocoCamera

        name = {'global': 'global_camera', 'wrist': 'wrist_camera'}.get(camera_id, camera_id)
        if self._cameras is None:
            renderers = {}
            cameras = {}
            for key, size in self._camera_resolutions.items():
                dimensions = tuple(size)
                renderer = renderers.get(dimensions)
                if renderer is None:
                    renderer = mujoco.Renderer(self.model, height=dimensions[1], width=dimensions[0])
                    renderers[dimensions] = renderer
                cameras[key] = MujocoCamera(self.model, self.data, key,
                                            width=dimensions[0], height=dimensions[1],
                                            renderer=renderer)
            self._camera_renderers = renderers
            self._cameras = CameraManager(cameras)
        return self._cameras.get_current_frame(name)

    def set_frame_callback(self, callback):
        self.frame_callback = callback

    def open_viewer(self):
        import mujoco.viewer
        self.viewer = mujoco.viewer.launch_passive(self.model, self.data, show_left_ui=False, show_right_ui=False)
        with self.viewer.lock():
            self.configure_camera(self.viewer.cam)
        self.set_status('Ready')
        self._sync_viewer()
        self.realtime = True

    def set_collision_geometry_visible(self, visible=True):
        """Toggle the MuJoCo collision-geometries display group.

        The scene compiler puts simplified collision meshes in geom group 3
        and keeps textured visual meshes in group 1.  ``geomgroup`` is a
        viewer option, so changing it affects rendering only and never
        changes MuJoCo's contact pairs or the FCL scene.
        """
        if self.viewer is None:
            return False
        with self.viewer.lock():
            groups = self.viewer.opt.geomgroup
            if len(groups) <= 3:
                return False
            groups[3] = 1 if visible else 0
        self._sync_viewer()
        return True

    def create_gripper_controller(self):
        from .gripper_controller import BindingGripperController
        return BindingGripperController(self)

    def payload_gravity_torque(self):
        """Map the separately simulated payload's weight to the arm joints.

        A free body's gravity lives in its own DOFs in qfrc_bias, even when
        contact or a weld transmits its weight to the gripper. Evaluate the
        gripper Jacobian at the measured payload COM, not the free-body Jacobian.
        """
        torque = np.zeros(len(self.dof_indices))
        c = self.gripper_controller
        if c is None or not c._holding or c.held_name is None:
            return torque
        body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, c.held_name)
        if min(c._forces(body).values()) <= .1:
            return torque
        # Only separately simulated free objects need this additional term.
        j = int(self.model.body_jntadr[body])
        if self.model.body_jntnum[body] != 1 or self.model.jnt_type[j] != mujoco.mjtJoint.mjJNT_FREE:
            return torque
        jac = np.zeros((3, self.model.nv))
        mujoco.mj_jac(self.model, self.data, jac, None,
                      self.data.subtree_com[body], c.base_id)
        weight_compensation = -float(self.model.body_subtreemass[body]) * self.model.opt.gravity
        return jac[:, self.dof_indices].T @ weight_compensation

    def apply_arm_compensation(self):
        self.data.qfrc_applied[self.dof_indices] = (
            self.data.qfrc_bias[self.dof_indices] + self.payload_gravity_torque())

    def advance_gripper(self, duration):
        """Hold the arm using its actuators while the gripper and objects evolve."""
        goal = self.get_joint_positions()
        started = time.perf_counter()
        sim_start = float(self.data.time)
        next_frame = sim_start
        while self.data.time - sim_start < duration - 1e-10:
            with self.viewer.lock() if self.viewer is not None else nullcontext():
                self.data.ctrl[self.actuator_ids] = goal
                self.apply_arm_compensation()
                if self.gripper_controller is not None:
                    self.gripper_controller.update_hold_force()
                mujoco.mj_step(self.model, self.data)
                mujoco.mj_forward(self.model, self.data)
            self.physics_steps += 1
            if self.data.time >= next_frame:
                if self.frame_callback:
                    self.frame_callback(self.get_state())
                self._sync_viewer()
                if self.realtime:
                    delay = self.data.time - sim_start - (time.perf_counter() - started)
                    if delay > 0:
                        time.sleep(min(delay, self.frame_interval))
                next_frame += self.frame_interval

    def configure_camera(self, camera):
        options = self.resources.backend_resource.get('options', {}).get('viewer', {})
        camera.lookat[:] = options.get('lookat', self.data.xpos[self.base_id] + [0.25, 0, 0.15])
        camera.distance = options.get('distance', 2.7)
        camera.azimuth = options.get('azimuth', 135)
        camera.elevation = options.get('elevation', -25)

    def set_status(self, status):
        self.status = status
        if self.viewer is not None and self.viewer.is_running():
            self.viewer.set_texts((None, None, status, f'Simulation: {self.data.time:.2f} s | Physics steps: {self.physics_steps}'))

    def _sync_viewer(self):
        if self.viewer is None:
            return
        if not self.viewer.is_running():
            raise RuntimeError('Viewer closed during execution')
        self.set_status(self.status)
        self.viewer.sync()
        self.viewer_sync_count += 1

    def get_state(self):
        base = np.eye(4)
        base[:3, :3] = self.data.xmat[self.base_id].reshape(3, 3)
        base[:3, 3] = self.data.xpos[self.base_id]
        payload = None
        if self.gripper_controller is not None:
            c = self.gripper_controller
            payload = c.get_state()
            payload['object_id'] = c.held_name
        mechanisms = {}
        for name in getattr(self, 'mechanism_bodies', ()):
            body_joint_id = int(self.model.body_jntadr[self.object_ids[name]])
            joint_name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_JOINT, body_joint_id)
            mechanisms[name] = {
                'joint_name': joint_name or name,
                'joint_position': float(self.data.qpos[self.model.jnt_qposadr[body_joint_id]]),
                'joint_type': int(self.model.jnt_type[body_joint_id]),
            }
        return {'robot_object_id': self.resources.robot_object['object_id_in_scene'],
                'scene_driver_joints': {str(identifier): {name: float(self.data.qpos[address])
                    for name, address in joints.items()} for identifier, joints in self.scene_driver_joints.items()},
                'joint_names': list(self.joint_names), 'joints': self.get_joint_positions().tolist(),
                'gripper': payload,
                'mechanisms': mechanisms,
                'arm_control': {
                    'joint_error_rad': (self.data.ctrl[self.actuator_ids] - self.get_joint_positions()).tolist(),
                    'servo_torque_nm': self.data.actuator_force[self.actuator_ids].tolist(),
                    'servo_force_limited': self.model.actuator_forcelimited[self.actuator_ids].astype(bool).tolist(),
                    'payload_gravity_torque_nm': self.payload_gravity_torque().tolist()},
                'base_transform': base.tolist(), 'simulation_time': float(self.data.time),
                'objects': {name: {'position': self.data.xpos[i].tolist(),
                                   'quaternion_xyzw': self.data.xquat[i][[1, 2, 3, 0]].tolist()}
                            for name, i in self.object_ids.items()}}

    def set_object_pose(self, name, position, quaternion_xyzw=(0, 0, 0, 1)):
        i = self.object_ids[name]
        if self.model.body_jntnum[i]:
            raise ValueError('Explicit pose edits only support static objects')
        self.model.body_pos[i] = position
        self.model.body_quat[i] = np.asarray(quaternion_xyzw)[[3, 0, 1, 2]]
        mujoco.mj_forward(self.model, self.data)

    def execute_trajectory(self, trajectory, *, timeout=30.0):
        q = np.asarray(trajectory['time_parameterized_waypoints'], dtype=float)
        stamps = np.asarray(trajectory['timestamps'], dtype=float)
        if (q.ndim != 2 or q.shape[1] != len(self.joint_names) or len(q) < 2
                or stamps.shape != (len(q),) or not np.all(np.isfinite(q))
                or not np.all(np.isfinite(stamps)) or stamps[0] != 0
                or np.any(np.diff(stamps) < 0)):
            raise ValueError('Trajectory requires finite NxDOF joints and nondecreasing timestamps from zero')
        # Ruckig segment endpoints can round to identical timestamps. They
        # represent one sample only when the corresponding positions agree.
        duplicate = np.diff(stamps) == 0
        if np.any(np.abs(np.diff(q, axis=0)[duplicate]) > 1e-10):
            raise ValueError('Different joint positions share a trajectory timestamp')
        keep = np.r_[~duplicate, True]
        q, stamps = q[keep], stamps[keep]
        if len(q) < 2:
            raise ValueError('Trajectory requires a positive duration')
        if not np.isfinite(timeout) or timeout <= 0:
            raise ValueError('Execution timeout must be finite and positive')
        if np.any(q < self.joint_limits[:, 0] - 1e-8) or np.any(q > self.joint_limits[:, 1] + 1e-8):
            raise ValueError('Trajectory exceeds joint limits')
        if not np.allclose(q[0], self.get_joint_positions(), atol=0.002):
            raise ValueError('Trajectory start differs from execution state')
        start = time.perf_counter(); sim_start = self.data.time
        frames, next_frame = [], self.data.time
        dt = float(self.model.opt.timestep)
        max_tracking = 0.0
        from ...runtime.convergence import GoalConvergence, TrajectoryTimeout
        convergence = GoalConvergence(**trajectory.get('goal_tolerances', {}))
        goal_data = mujoco.MjData(self.model)
        goal_data.qpos[:] = self.data.qpos
        goal_data.qpos[self.qpos_indices] = q[-1]
        mujoco.mj_forward(self.model, goal_data)
        goal_position = goal_data.site_xpos[self.tip_id].copy()
        goal_rotation = goal_data.site_xmat[self.tip_id].reshape(3, 3).copy()
        initial_gripper = self.gripper_controller.get_state() if self.gripper_controller else {}
        holding_required = bool(initial_gripper.get('holding') or
            (getattr(self.gripper_controller, 'held_name', None)
             and getattr(self.gripper_controller, '_holding', False)))
        completion = None
        velocity_feedforward = (trajectory.get('diagnostics', {}).get('time_parameterization', {})
                                .get('time_law') == 'local_acceleration_bounds')
        reference_velocities = np.gradient(q, stamps, axis=0)
        reference_velocities[[0, -1]] = 0.0
        gains = self.model.actuator_gainprm[self.actuator_ids, 0]
        damping = -self.model.actuator_biasprm[self.actuator_ids, 2]
        velocity_gain = np.divide(damping, gains, out=np.zeros_like(gains), where=gains > 0)
        # Track a smooth reference with position actuators and gravity compensation.
        # Physics remains active: qpos is never assigned during playback.
        while True:
            elapsed = float(self.data.time - sim_start)
            reference_time = elapsed
            ref = np.array([np.interp(elapsed, stamps, q[:, j]) for j in range(len(self.joint_names))])
            control = ref
            if velocity_feedforward:
                # A position actuator otherwise damps against zero velocity,
                # causing kv/kp * q_dot lag even on a perfectly feasible path.
                # Compensate through the actuator command, retaining its force
                # and control limits; never write the physical joint positions.
                speed_ref = np.array([np.interp(elapsed, stamps, reference_velocities[:, j])
                                      for j in range(len(self.joint_names))])
                control = np.clip(ref + velocity_gain * speed_ref,
                                  self.joint_limits[:, 0], self.joint_limits[:, 1])
            with self.viewer.lock() if self.viewer is not None else nullcontext():
                self.data.ctrl[self.actuator_ids] = control
                self.apply_arm_compensation()
                if self.gripper_controller is not None:
                    self.gripper_controller.update_hold_force()
                mujoco.mj_step(self.model, self.data)
                mujoco.mj_forward(self.model, self.data)
            self.physics_steps += 1
            max_tracking = max(max_tracking, float(np.max(np.abs(self.get_joint_positions() - ref))))
            if self.data.time + 1e-10 >= next_frame:
                state = self.get_state()
                payload = state.get('gripper')
                frames.append(state)
                if self.frame_callback:
                    self.frame_callback(state)
                if self.viewer is not None:
                    self._sync_viewer()
                    # Pace against elapsed wall time rather than adding a fixed
                    # delay on top of collision checks and rendering costs.
                    delay = (self.data.time - sim_start) - (time.perf_counter() - start)
                    if delay > 0:
                        time.sleep(min(delay, self.frame_interval))
                next_frame += self.frame_interval
            error = float(np.max(np.abs(self.get_joint_positions() - q[-1])))
            wall_elapsed = time.perf_counter() - start
            if elapsed >= stamps[-1] or wall_elapsed > timeout:
                position_error = np.linalg.norm(self.data.site_xpos[self.tip_id] - goal_position)
                orientation_error = Rotation.from_matrix(
                    goal_rotation.T @ self.data.site_xmat[self.tip_id].reshape(3, 3)).magnitude()
                completion = convergence.update(elapsed, reference_complete=elapsed >= stamps[-1],
                    joint_error=error, joint_speed=float(np.max(np.abs(self.data.qvel[self.dof_indices]))),
                    position_error=position_error, orientation_error=orientation_error,
                    holding_required=holding_required, contact_valid=True)
                if completion['converged']:
                    break
                if elapsed > stamps[-1] + 5 or wall_elapsed > timeout:
                    reason = 'wall_time_limit' if wall_elapsed > timeout else 'goal_not_converged'
                    raise TrajectoryTimeout(reason, {**completion, 'planned_duration_s': float(stamps[-1]),
                        'simulation_elapsed_s': float(self.data.time - sim_start),
                        'wall_elapsed_s': wall_elapsed, 'wall_timeout_s': timeout,
                        'settling_timeout_s': 5.0})
        self._sync_viewer()
        final_gripper = self.get_state().get('gripper')
        return {'final_joint_state': self.get_joint_positions().tolist(), 'joint_error': error,
                'execution_time': time.perf_counter() - start, 'simulation_duration': float(self.data.time - sim_start),
                'physics_steps': int(round((self.data.time - sim_start) / dt)),
                'max_tracking_error_rad': max_tracking, 'frames': frames,
                'motion_completed': bool(completion and completion['converged']), 'grasp_error': None,
                'contact_diagnostics': {'mode': 'command_end', 'samples': 0},
                'completion': completion,
                'velocity_feedforward': velocity_feedforward,
                'final_gripper_state': final_gripper}

    def wait_for_viewer_close(self):
        """Keep the final scene inspectable without advancing execution physics."""
        viewer = getattr(self, 'viewer', None)
        while viewer is not None and viewer.is_running():
            viewer.sync()
            time.sleep(1 / 30)

    def close(self):
        if getattr(self, '_cameras', None) is not None:
            self._cameras.close()
            self._cameras = None
        for renderer in getattr(self, '_camera_renderers', {}).values():
            renderer.close()
        self._camera_renderers = {}
        if getattr(self, 'viewer', None) is not None:
            self.viewer.close()
        self.viewer = None
