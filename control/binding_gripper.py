"""Measured bilateral grasp confirmation with binding or friction-only holding."""
from collections import deque

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation


class BindingGripperController:
    maximum_width = .085
    maximum_force = 40.
    supported_grasp_modes = {'binding', 'physical'}

    def __init__(self, runtime):
        self.runtime, self.model, self.data = runtime, runtime.model, runtime.data
        self.actuator_id = self._id(mujoco.mjtObj.mjOBJ_ACTUATOR, 'grasp_servo')
        self.base_id = self._id(mujoco.mjtObj.mjOBJ_BODY, 'robotiq_2f85_base')
        self.driver_id = self._id(mujoco.mjtObj.mjOBJ_JOINT, 'robotiq_2f85_right_driver_joint')
        self.driver_address = int(self.model.jnt_qposadr[self.driver_id])
        self.pad_geoms = {}
        for side in ('left', 'right'):
            body = self._id(mujoco.mjtObj.mjOBJ_BODY, f'robotiq_2f85_{side}_pad')
            self.pad_geoms[side] = {g for g in range(self.model.ngeom) if self.model.geom_bodyid[g] == body
                                    and (self.model.geom_contype[g] or self.model.geom_conaffinity[g])}
        self.target_name = self.held_name = None
        self.weld_id = None
        self.relative_pose = None
        self.confirmed = False
        self.evidence = {}
        self._holding = False
        self.mode = 'binding'
        self.hold_force_target = None
        self.filtered_hold_force = None

    def _id(self, kind, name):
        value = mujoco.mj_name2id(self.model, kind, name)
        if value < 0:
            raise NotImplementedError(f'Binding grasp requires the articulated gripper backend: missing {name}')
        return value

    @property
    def width(self):
        # Robotiq parallelogram: compute actual inner pad surface separation.
        centers = []
        for side in ('left', 'right'):
            geoms = sorted(self.pad_geoms[side])
            positions = [self.data.geom_xpos[g] + self.data.geom_xmat[g].reshape(3, 3) @ np.array([0, -.004, 0]) for g in geoms]
            centers.append(np.mean(positions, axis=0))
        axis = self.data.xmat[self.base_id].reshape(3, 3)[:, 1]
        return float(abs(np.dot(centers[0] - centers[1], axis)))

    def _relative(self, body):
        base_rotation = self.data.xmat[self.base_id].reshape(3, 3)
        t = np.eye(4)
        t[:3, :3] = base_rotation.T @ self.data.xmat[body].reshape(3, 3)
        t[:3, 3] = base_rotation.T @ (self.data.xpos[body] - self.data.xpos[self.base_id])
        return t

    def _forces(self, body):
        forces = {'left': 0., 'right': 0.}
        for i in range(self.data.ncon):
            c = self.data.contact[i]
            for pad, other in ((int(c.geom1), int(c.geom2)), (int(c.geom2), int(c.geom1))):
                if int(self.model.geom_bodyid[other]) != body:
                    continue
                for side, geoms in self.pad_geoms.items():
                    if pad in geoms:
                        wrench = np.zeros(6)
                        mujoco.mj_contactForce(self.model, self.data, i, wrench)
                        forces[side] += max(0., float(wrench[0]))
        return forces

    def open(self, width, *, speed=.5, timeout=5.):
        self.detach()
        self.confirmed = False
        self.runtime.contact_target = self.target_name
        start = float(self.data.time)
        goal = float(np.clip(.8 * (1 - width / self.maximum_width), 0, .8))
        try:
            while self.data.time - start < timeout:
                current = self.data.ctrl[self.actuator_id]
                self.data.ctrl[self.actuator_id] = current + np.clip(goal - current, -.8 * speed * .01, .8 * speed * .01)
                self.runtime.advance_gripper(.01)
                if abs(self.data.qpos[self.driver_address] - goal) < .005 and abs(self.data.ctrl[self.actuator_id] - goal) < 1e-8:
                    return {'final_width': self.width, 'contact_detected': False}
            raise TimeoutError('Gripper opening timed out')
        finally:
            self.runtime.contact_target = None

    def close(self, width, *, speed=.5, force=20., timeout=5., target_object_id=None,
              mode='binding', confirmation_time=.1, contact_threshold=2.,
              width_stability_tolerance=.0005, position_stability_tolerance=.001,
              orientation_stability_tolerance=.035, maximum_force=40.):
        if mode not in self.supported_grasp_modes:
            raise NotImplementedError(f'Unsupported grasp mode: {mode}')
        if self.held_name is not None:
            raise ValueError('Release the held object before acquiring another grasp')
        body = self.validate_target(target_object_id, mode=mode)
        self.target_name, self.confirmed = target_object_id, False
        self.mode = mode
        self.runtime.contact_target = target_object_id
        self.model.actuator_forcerange[self.actuator_id] = [-force * .04, force * .04]
        start = float(self.data.time)
        window = deque()
        goal = float(np.clip(.8 * (1 - width / self.maximum_width), 0, .8))
        try:
            while self.data.time - start < timeout:
                self.data.ctrl[self.actuator_id] = min(goal, self.data.ctrl[self.actuator_id] + .8 * speed * .01)
                self.runtime.advance_gripper(.01)
                forces = self._forces(body)
                current_width, pose = self.width, self._relative(body)
                valid = min(forces.values()) >= contact_threshold and current_width > .002
                total_force = sum(forces.values())
                if total_force > maximum_force:
                    valid = False
                    reason = 'force_limit_exceeded'
                else:
                    reason = 'bilateral_contact_not_stable'
                if not valid:
                    window.clear()
                else:
                    window.append((float(self.data.time), current_width, pose))
                    while len(window) > 1 and self.data.time - window[1][0] >= confirmation_time:
                        window.popleft()
                stable_s = float(self.data.time - window[0][0]) if window else 0.
                width_range = float(np.ptp([v[1] for v in window])) if window else 0.
                position_range = max((float(np.linalg.norm(v[2][:3, 3] - pose[:3, 3])) for v in window), default=0.)
                rotation_range = max((float(Rotation.from_matrix(v[2][:3, :3].T @ pose[:3, :3]).magnitude()) for v in window), default=0.)
                stable = width_range <= width_stability_tolerance and position_range <= position_stability_tolerance and rotation_range <= orientation_stability_tolerance
                self.evidence = {'left_force_n': forces['left'], 'right_force_n': forces['right'],
                    'stable_time_s': stable_s, 'width_range_m': width_range, 'position_range_m': position_range,
                    'orientation_range_rad': rotation_range, 'bilateral_contact': valid,
                    'confirmed': valid and stable and stable_s + 1e-9 >= confirmation_time,
                    'reason': reason, 'mode': mode}
                if self.evidence['confirmed']:
                    self.confirmed = True
                    self.evidence['reason'] = 'stable_bilateral_grasp'
                    break
                if total_force > maximum_force:
                    break
            # Stop further squeezing while retaining the force just established.
            self.data.ctrl[self.actuator_id] = min(self.data.ctrl[self.actuator_id], goal)
            return {'contact_detected': self.confirmed, 'final_width': self.width, 'evidence': dict(self.evidence)}
        finally:
            self.runtime.contact_target = None
            if not self.confirmed:
                self.data.ctrl[self.actuator_id] = float(self.data.qpos[self.driver_address])

    def bind(self, target):
        if not self.confirmed or target != self.target_name:
            raise ValueError('Binding requires a confirmed grasp of this target')
        body = self._id(mujoco.mjtObj.mjOBJ_BODY, target)
        self.weld_id = self._id(mujoco.mjtObj.mjOBJ_EQUALITY, 'grasp_weld_' + target)
        relative = self._relative(body)
        # MuJoCo weld data: anchor(3), relative position(3), relative quaternion(4), torque scale.
        self.model.eq_data[self.weld_id, :3] = 0
        self.model.eq_data[self.weld_id, 3:6] = relative[:3, 3]
        self.model.eq_data[self.weld_id, 6:10] = Rotation.from_matrix(relative[:3, :3]).as_quat()[[3, 0, 1, 2]]
        self.data.eq_active[self.weld_id] = True
        self.relative_pose, self.held_name = relative.copy(), target
        self._holding = True
        self.confirmed = False
        mujoco.mj_forward(self.model, self.data)

    def validate_target(self, target, *, mode=None):
        body = self._id(mujoco.mjtObj.mjOBJ_BODY, target)
        if mode == 'binding':
            self._id(mujoco.mjtObj.mjOBJ_EQUALITY, 'grasp_weld_' + target)
        j = int(self.model.body_jntadr[body])
        if self.model.body_jntnum[body] != 1 or self.model.jnt_type[j] not in (mujoco.mjtJoint.mjJNT_FREE, mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE):
            raise ValueError('Grasp requires one free, hinge or slide joint on the target body')
        return body

    def retain_physical(self, target):
        """Track a confirmed payload without changing constraints or object state."""
        if self.mode != 'physical' or not self.confirmed or target != self.target_name:
            raise ValueError('Physical holding requires a confirmed grasp of this target')
        body = self.validate_target(target)
        self.relative_pose, self.held_name = self._relative(body).copy(), target
        self._holding, self.confirmed = True, False

    def detach(self):
        self.hold_force_target = None
        self.filtered_hold_force = None
        if self.weld_id is not None:
            self.data.eq_active[self.weld_id] = False
        self.held_name = self.weld_id = self.relative_pose = None
        self._holding = False
        self.confirmed = False

    def hold(self, force):
        if self.mode == 'physical':
            if not np.isfinite(force) or not 0 < force <= self.maximum_force:
                raise ValueError('Physical hold force must be in (0, 40] N')
            self.hold_force_target = float(force)
            self.filtered_hold_force = None
            # Independent actuator protection, not a conversion from contact force.
            self.model.actuator_forcerange[self.actuator_id] = [-2., 2.]
        self._holding = True

    def update_hold_force(self):
        """Regulate total pad normal force before each physics step."""
        if self.mode != 'physical' or not self._holding or not self.held_name or self.hold_force_target is None:
            return
        body = self._id(mujoco.mjtObj.mjOBJ_BODY, self.held_name)
        forces = self._forces(body)
        measured = sum(forces.values())
        dt = float(self.model.opt.timestep)
        if self.filtered_hold_force is None:
            self.filtered_hold_force = measured
        self.filtered_hold_force += (1 - np.exp(-dt / .02)) * (measured - self.filtered_hold_force)
        # No blind tightening after losing a pad; leave loss reporting to execution.
        if min(forces.values()) <= .1:
            return
        error = self.hold_force_target - self.filtered_hold_force
        if abs(error) < .2:
            return
        delta = np.clip(.01 * error, -.2, .2) * dt
        current = float(self.data.ctrl[self.actuator_id])
        # Limit stored position error too, so saturation cannot wind up the loop.
        joint = float(self.data.qpos[self.driver_address])
        lo, hi = self.model.actuator_ctrlrange[self.actuator_id]
        self.data.ctrl[self.actuator_id] = np.clip(current + delta, max(lo, joint - .2), min(hi, joint + .2))

    def get_state(self, target_object_id=None):
        body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, target_object_id or '')
        forces = self._forces(body) if body >= 0 else {'left': 0., 'right': 0.}
        tracked = self.held_name is not None and target_object_id == self.held_name
        bound = tracked and self.weld_id is not None and bool(self.data.eq_active[self.weld_id])
        physical = tracked and self.mode == 'physical'
        bilateral = min(forces.values()) > 0.1
        position_error = orientation_error = 0.
        if tracked:
            actual = self._relative(body)
            position_error = float(np.linalg.norm(actual[:3, 3] - self.relative_pose[:3, 3]))
            orientation_error = float(Rotation.from_matrix(actual[:3, :3].T @ self.relative_pose[:3, :3]).magnitude())
        confirmed = self.confirmed and target_object_id == self.target_name
        contact_lost = physical and max(forces.values()) <= .1
        return {'known': True, 'initialized': True, 'width': self.width, 'force': sum(forces.values()),
            'contact_detected': confirmed or bound or (physical and bilateral), 'object_present': confirmed or bound or (physical and bilateral),
            'holding': (bound or (physical and bilateral)) and self._holding, 'bound': bound, 'mode': self.mode,
            'slip_detected': contact_lost if physical else bound and (position_error > .002 or orientation_error > .035),
            'bilateral_contact': bilateral, 'contact_lost': contact_lost,
            'left_force_n': forces['left'], 'right_force_n': forces['right'],
            'hold_force_target_n': self.hold_force_target,
            'filtered_hold_force_n': self.filtered_hold_force,
            'hold_force_torque_saturated': bool(self.hold_force_target is not None and
                abs(self.data.actuator_force[self.actuator_id]) >= 1.99),
            'relative_position_error_m': position_error, 'relative_orientation_error_rad': orientation_error,
            'binding_position_error_m': position_error, 'binding_orientation_error_rad': orientation_error,
            'confirmation': dict(self.evidence)}
