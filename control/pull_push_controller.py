"""Plan on scratch state, then drive only arm actuators through MuJoCo physics."""
from contextlib import nullcontext
from copy import deepcopy
import time

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation
from control.continuous_trajectory import ContinuousJointPath


DEFAULT_FCL_SAFETY_DISTANCE = 0.003
JOINT_PERIOD = 2.0 * np.pi
PREPARED_OBJECT_STATE_TOLERANCE = 1e-4


class ContactMotionError(RuntimeError):
    recoverable = True

    def __init__(self, message, *, stage='execution', code='PUSH_PULL_EXECUTION_ERROR', details=None):
        super().__init__(message)
        self.failed_stage, self.error_code, self.details = stage, code, details or {}


class MujocoPullPushController:
    def __init__(self, runtime, *, kinematics=None, collision_checker=None):
        self.runtime = runtime
        self.model, self.data = runtime.model, runtime.data
        self.kinematics = kinematics if kinematics is not None else runtime.kinematics
        self.collision_checker = collision_checker
        root = int(self.model.body_parentid[self.model.jnt_bodyid[runtime.joint_ids[0]]])
        self.robot_bodies = self._descendants(root)

    def _descendants(self, root):
        bodies = {root}
        for body in range(1, self.model.nbody):
            if int(self.model.body_parentid[body]) in bodies:
                bodies.add(body)
        return bodies

    def _body_joint(self, name):
        body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, name)
        if body < 0 or self.model.body_jntnum[body] != 1:
            raise ContactMotionError('Target must identify one movable body, not the cabinet root.', stage='validation')
        return body, int(self.model.body_jntadr[body])

    def get_state(self, object_id, *, interaction_mode='mechanism', contact_mode='grasp'):
        _, joint = self._body_joint(object_id)
        gripper = getattr(self.runtime, 'gripper_controller', None)
        holding = bool(gripper and gripper.get_state().get('holding'))
        holding_any = bool(gripper and getattr(gripper, '_holding', holding))
        valid = (self.model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_FREE if interaction_mode == 'free_object'
                 else self.model.jnt_type[joint] in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE))
        return {'ready': bool(valid and (holding if contact_mode == 'grasp' else not holding_any)),
                'fault': False, 'safe': True, 'gripper_holding': holding}

    def rebase_prepared_object_state(self, request, prepared):
        """Refresh the prepared-state guard after an intentional grasp.

        Closing a physical gripper can move the target joint by a small amount.
        That change is part of the requested grasp, so it must become the new
        validation baseline without regenerating the already planned path.
        """
        target = request['target']
        _, joint = self._body_joint(target['object_id'])
        address = int(self.model.jnt_qposadr[joint])
        free = request.get('resolved_interaction_mode') == 'free_object'
        width = 7 if free else 1
        prepared['initial_object_qpos'] = np.asarray(
            self.data.qpos[address:address + width], dtype=float).copy()
        return prepared

    def _pad(self, geom):
        name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY, int(self.model.geom_bodyid[geom])) or ''
        return name.startswith('robotiq_2f85') and 'pad' in name

    def _is_gripper_body(self, body_id):
        name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY, int(body_id)) or ''
        return name.startswith('robotiq_2f85')

    def _is_target_gripper_pair(self, body_a, body_b, target_bodies):
        """Allow intentional gripper contact with the active push/pull target."""
        return ((int(body_a) in target_bodies and self._is_gripper_body(body_b)) or
                (int(body_b) in target_bodies and self._is_gripper_body(body_a)))

    def _contacts(self, data, target_bodies, contact_geom):
        force = 0.0
        pairs = set()
        for i in range(data.ncon):
            contact = data.contact[i]
            a, b = int(contact.geom1), int(contact.geom2)
            for tool, obj in ((a, b), (b, a)):
                if (self._pad(tool) and int(self.model.geom_bodyid[obj]) in target_bodies
                        and (contact_geom is None or obj == contact_geom)):
                    wrench = np.zeros(6)
                    mujoco.mj_contactForce(self.model, data, i, wrench)
                    force += max(0.0, float(wrench[0]))
                    if contact.dist <= 0:
                        pairs.add(frozenset((a, b)))
        return force, pairs

    def _collision(self, data, target_bodies, allowed_pairs, support_pairs, self_pairs,
                   interaction_bodies=None):
        interaction_bodies = target_bodies if interaction_bodies is None else interaction_bodies
        for i in range(data.ncon):
            c = data.contact[i]
            if c.dist >= 0:
                continue
            a, b = int(c.geom1), int(c.geom2)
            bodies = {int(self.model.geom_bodyid[a]), int(self.model.geom_bodyid[b])}
            if not bodies & (target_bodies | self.robot_bodies):
                continue
            if len(bodies) == 2 and self._is_target_gripper_pair(
                    *tuple(bodies), interaction_bodies):
                continue
            # Contacts between bodies that belong to the same articulated
            # target model are structural mechanism contacts. They are allowed
            # globally during push/pull execution; collisions with the robot,
            # environment, or another model remain subject to normal checks.
            if bodies.issubset(target_bodies):
                continue
            pair = frozenset((a, b))
            if pair in self_pairs:
                continue
            if pair in allowed_pairs and (data is not self.data or c.dist >= -0.003):
                continue
            if pair in support_pairs and c.dist >= min(-0.001, support_pairs[pair] - 0.0005):
                continue
            body_a = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY,
                                       int(self.model.geom_bodyid[a])) or f'body_{self.model.geom_bodyid[a]}'
            body_b = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY,
                                       int(self.model.geom_bodyid[b])) or f'body_{self.model.geom_bodyid[b]}'
            geom_a = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, a) or f'geom_{a}'
            geom_b = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, b) or f'geom_{b}'
            distance = float(c.dist)
            raise ContactMotionError(
                f'Forbidden contact: {body_a} ({geom_a}) ↔ {body_b} ({geom_b}); distance={distance:.6g}',
                code='COLLISION_DETECTED',
                details={'contacts': [{'geom1': body_a, 'geom2': body_b,
                                       'geometry1': geom_a, 'geometry2': geom_b,
                                       'distance': distance, 'geom_ids': [a, b],
                                       'body_ids': [int(self.model.geom_bodyid[a]), int(self.model.geom_bodyid[b])]}]})

    def _fcl_collision(self, joints):
        checker = self.collision_checker
        if checker is None:
            return
        joints = np.asarray(joints, dtype=float).reshape(-1)
        expected = len(getattr(self.kinematics.robot, 'joint_names', joints))
        if joints.shape != (expected,):
            return
        gripper = getattr(self.runtime, 'gripper_controller', None)
        target = getattr(self, '_fcl_target_name', None)
        # Push/pull owns the live MuJoCo scratch state. Calling the hybrid
        # checker here would try to copy that controller's mechanism qpos into
        # the separate grasp probe. Use only the native FCL arm/static query;
        # MuJoCo below remains responsible for dynamic contact geometry.
        from collision.simplatform_fcl import SimPlatformFCLCollisionChecker
        result = SimPlatformFCLCollisionChecker.check(
            checker, joints, safety_distance=DEFAULT_FCL_SAFETY_DISTANCE)
        if result.in_collision:
            held_target = bool(getattr(self, '_allow_planned_target_contact', False)) or (
                gripper is not None and target is not None and
                getattr(gripper, 'held_name', None) == target)
            contacts = [contact for contact in result.contacts
                        if not self._is_fcl_target_gripper_contact(contact)]
            if held_target:
                contacts = [contact for contact in contacts
                            if not (target in {str(contact.get('geom1', '')), str(contact.get('geom2', ''))}
                                    and any(str(name).startswith('robotiq_2f85')
                                            for name in (contact.get('geom1', ''), contact.get('geom2', ''))))]
            if not contacts:
                return
            raise ContactMotionError(
                'FCL detected a forbidden robot/environment collision.',
                stage='planning', code='COLLISION_DETECTED',
                details={'contacts': contacts, 'source': 'fcl'})

    def _is_fcl_target_gripper_contact(self, contact):
        target = str(getattr(self, '_fcl_target_name', '') or '')
        if not target:
            return False
        names = (str(contact.get('geom1', '')), str(contact.get('geom2', '')))
        has_gripper = any(name.startswith('robotiq_2f85') for name in names)
        if not has_gripper:
            return False
        return any(name == target or name.startswith(target + '_') or target.startswith(name + '_')
                   for name in names if not name.startswith('robotiq_2f85'))

    def _measured_delta(self, circular, params):
        pose = self.runtime.get_end_effector_pose()
        if not circular:
            return float((np.asarray(pose['position']) - self._linear_start_position) @ self._linear_direction)
        current = Rotation.from_matrix(pose['rotation_matrix'])
        self._rotation_delta += float((current * self._previous_rotation.inv()).as_rotvec() @ np.asarray(params['arc_axis']))
        self._previous_rotation = current
        return self._rotation_delta

    def _progress_delta(self, circular, params):
        measured = self._measured_delta(circular, params)
        if not circular and self._count_from_contact:
            return 0.0 if self._contact_start_distance is None else measured-self._contact_start_distance
        return measured

    def _travel_breakdown(self, circular, params):
        if circular or not self._count_from_contact:
            return {}
        total = self._measured_delta(False, params)
        return {'precontact_distance': (total if self._contact_start_distance is None else self._contact_start_distance),
                'total_travel_distance': total,
                'contact_tcp_position': (None if self._contact_start_position is None else self._contact_start_position.tolist())}

    def execute(self, request):
        # Preserve measured progress for every stop, including pre-motion failures.
        body, joint = self._body_joint(request['target']['object_id'])
        address = int(self.model.jnt_qposadr[joint])
        free = self.model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_FREE
        initial = self.data.qpos[address:address + (7 if free else 1)].copy()
        motion = request.get('motion', {})
        path_type = motion.get('path_type', request['manipulation']['trajectory'])
        if path_type not in {'linear', 'circular'}:
            raise ContactMotionError('Unsupported end-effector path type.', stage='validation', code='INVALID_REQUEST')
        circular = path_type == 'circular'
        self._rotation_delta = 0.0
        self._previous_rotation = Rotation.from_matrix(self.runtime.get_end_effector_pose()['rotation_matrix'])
        p = request['strategy_params']
        self._linear_start_position = np.asarray(self.runtime.get_end_effector_pose()['position'], dtype=float).copy()
        self._linear_direction = np.asarray(p.get('direction', [0, 0, 0]), dtype=float)
        self._count_from_contact = not circular and request['manipulation']['contact_mode'] == 'push_contact'
        self._contact_start_distance = None
        self._contact_start_position = None
        requested = float(p['arc_angle'] if circular else p['distance'])
        direction = np.asarray(p.get('direction', [0, 0, 0]))
        sign = 1.0
        steps = self.runtime.physics_steps
        try:
            result = self._execute(request)
        except ContactMotionError as exc:
            actual = self._progress_delta(circular, p) * sign
            result = exc.details.get('result', {})
            result.update(requested_delta=requested, actual_delta=actual,
                          remaining_delta=abs(requested-actual), unit='rad' if circular else 'm',
                          stop_reason=exc.error_code, target_reached=False, verified=False,
                          motion_executed=self.runtime.physics_steps > steps,
                          travel_distance=abs(actual) if not circular else 0.0,
                          rotation_angle=actual if circular else 0.0,
                          object_final_state={'qpos': self.data.qpos[address:address+(7 if free else 1)].tolist()})
            result.setdefault('contact_established', False)
            result.setdefault('contact_maintained', False)
            result.setdefault('peak_force', 0.0)
            result.update(self._travel_breakdown(circular, p))
            exc.details.update(result=result, requested_delta=requested, actual_delta=actual,
                               remaining_delta=abs(requested-actual), unit=result['unit'],
                               motion_executed=result['motion_executed'])
            raise
        actual = float(result['rotation_angle'] * sign if circular else result.pop('signed_travel_distance'))
        if circular:
            result['rotation_angle'] = actual
        result.update(requested_delta=requested, actual_delta=actual, remaining_delta=abs(requested-actual),
                      unit='rad' if circular else 'm', stop_reason='TARGET_REACHED',
                      motion_executed=self.runtime.physics_steps > steps)
        result.update(self._travel_breakdown(circular, p))
        return result

    def plan(self, request, *, start_joints=None, start_pose=None,
              allow_unestablished_contact=False):
        """Plan a complete contact trajectory without advancing the runtime.

        ``start_joints`` and ``start_pose`` describe the interaction pose.  A
        task skill can therefore prepare the push/pull path before moving to
        that pose with its approach motion.  The returned dictionary is an
        immutable-by-convention prepared trajectory for ``execute_prepared``;
        this method never changes live qpos, actuator commands, or physics
        time.
        """
        planned = dict(request)
        planned['_planning_only'] = True
        planned['_planning_context'] = {
            'start_joints': None if start_joints is None else np.asarray(start_joints, dtype=float).copy(),
            'start_pose': start_pose,
            'allow_unestablished_contact': bool(allow_unestablished_contact),
            'allow_target_contact': bool(allow_unestablished_contact),
        }
        self._allow_planned_target_contact = bool(allow_unestablished_contact)
        try:
            raw = self._execute(planned)
        finally:
            self._allow_planned_target_contact = False
        return raw['prepared']

    def execute_prepared(self, request, prepared):
        """Execute one trajectory returned by :meth:`plan` without replanning."""
        return self._execute_prepared(request, prepared)

    def _execute_prepared(self, request, prepared):
        """Run a previously validated path; this method never calls IK."""
        r, data, model = self.runtime, self.data, self.model
        c, p, action, target = (request[k] for k in ('constraints', 'strategy_params', 'manipulation', 'target'))
        body, joint = self._body_joint(target['object_id'])
        self._fcl_target_name = target['object_id']
        address = int(model.jnt_qposadr[joint])
        free = request['resolved_interaction_mode'] == 'free_object'
        motion = request.get('motion', {})
        path_type = motion.get('path_type', action['trajectory'])
        circular = path_type == 'circular'
        if path_type not in {'linear', 'circular'}:
            raise ContactMotionError('Unsupported end-effector path type.', stage='validation', code='INVALID_REQUEST')
        actual_q = np.asarray(r.get_joint_positions(), dtype=float)
        prepared, planned_q, _ = self._align_prepared_joint_branch(
            prepared, actual_q)
        if planned_q.shape != actual_q.shape or np.max(np.abs(actual_q - planned_q)) > 0.02:
            raise ContactMotionError(
                'Robot is not at the prepared interaction configuration.',
                stage='planning', code='PREPARED_PATH_START_MISMATCH',
                details={'maximum_joint_error': float(np.max(np.abs(actual_q - planned_q))),
                         'tolerance': 0.02})
        object_initial = data.qpos[address:address + (7 if free else 1)].copy()
        planned_object = np.asarray(prepared['initial_object_qpos'], dtype=float)
        if planned_object.shape == object_initial.shape and np.max(np.abs(object_initial - planned_object)) > PREPARED_OBJECT_STATE_TOLERANCE:
            raise ContactMotionError(
                'Target state changed after the path was prepared.',
                stage='planning', code='PREPARED_PATH_STATE_MISMATCH',
                details={'maximum_object_state_error': float(np.max(np.abs(object_initial - planned_object)))})
        initial = object_initial
        bodies = self._descendants(body)
        mechanism_root = body
        while int(model.body_parentid[mechanism_root]) != 0:
            mechanism_root = int(model.body_parentid[mechanism_root])
        mechanism_bodies = self._descendants(mechanism_root)
        geom = prepared.get('geom')
        if geom is not None:
            geom = int(geom)
        start_pos = np.asarray(prepared['start_position'], dtype=float)
        start_rot = np.asarray(prepared['start_rotation'], dtype=float)
        center = np.asarray(prepared['center'], dtype=float)
        axis = np.asarray(prepared['axis'], dtype=float)
        delta = float(prepared['delta'])
        direction = np.asarray(prepared['direction'], dtype=float)
        self._rotation_delta = 0.0
        self._previous_rotation = Rotation.from_matrix(start_rot)
        self._linear_start_position = start_pos.copy()
        self._linear_direction = direction.copy()
        self._count_from_contact = not circular and action['contact_mode'] == 'push_contact'
        self._contact_start_distance = None
        self._contact_start_position = None
        force, pairs = self._contacts(data, bodies, geom)
        initial_contact = bool(pairs and force >= c['contact_threshold'])
        established = initial_contact
        if action['contact_mode'] == 'grasp':
            gripper = getattr(r, 'gripper_controller', None)
            grasp_state = gripper.get_state() if gripper else {}
            if not grasp_state.get('holding'):
                pairs = set()
                established = False
        acquire_during_motion = action['contact_mode'] == 'push_contact' and not circular
        if not established and not acquire_during_motion:
            raise ContactMotionError('Establish measured pad contact before Push/Pull.', stage='contact', code='CONTACT_NOT_ESTABLISHED')
        if c['maximum_force'] is not None and force > c['maximum_force']:
            raise ContactMotionError('Initial contact exceeds maximum_force.', stage='contact', code='FORCE_LIMIT')
        local = target.get('contact_point')

        def verify_contact_point(contact_pairs):
            actual_point = data.xpos[body] + data.xmat[body].reshape(3, 3) @ np.asarray(local)
            if not any(np.linalg.norm(data.contact[i].pos - actual_point) < 0.02
                       for i in range(data.ncon)
                       if frozenset((int(data.contact[i].geom1), int(data.contact[i].geom2))) in contact_pairs):
                raise ContactMotionError('contact_point is not at the measured pad contact.', stage='contact')

        if local is not None and established:
            verify_contact_point(pairs)
        references = np.asarray(prepared['references'], dtype=float)
        planned_duration = float(prepared['planned_duration'])
        timing = prepared['timing']
        final_reference = references[-1]
        sim_start = float(data.time)
        wall_start = time.perf_counter()
        lost_since = None
        peak = force
        maintained = True
        next_view = sim_start
        first_contact_delta = 0.0 if established else None
        try:
            while True:
                elapsed = float(data.time) - sim_start
                ref = references[min(int(round(elapsed / model.opt.timestep)), len(references) - 1)]
                viewer = getattr(r, 'viewer', None)
                with viewer.lock() if viewer else nullcontext():
                    data.ctrl[r.actuator_ids] = ref
                    r.apply_arm_compensation()
                    gripper = getattr(r, 'gripper_controller', None)
                    if gripper:
                        gripper.update_hold_force()
                    previous_time = float(data.time)
                    mujoco.mj_step(model, data)
                    if data.time <= previous_time or not np.all(np.isfinite(data.qpos)):
                        raise ContactMotionError('Physics became unstable.', code='PHYSICS_UNSTABLE')
                    mujoco.mj_forward(model, data)
                r.physics_steps += 1
                if circular:
                    self._measured_delta(circular, p)
                force, pairs = self._contacts(data, bodies, geom)
                if action['contact_mode'] == 'grasp':
                    grasp_state = gripper.get_state() if gripper else {}
                    if not grasp_state.get('holding'):
                        pairs = set()
                peak = max(peak, force)
                if c['maximum_force'] is not None and force > c['maximum_force']:
                    raise ContactMotionError('Measured contact force exceeded maximum_force.', code='FORCE_LIMIT')
                zero_force = force <= c.get('zero_force_threshold', 0.0)
                contact_now = bool(pairs and (
                    (not established and force >= c['contact_threshold']) or
                    (established and not zero_force)))
                if contact_now and not established:
                    if local is not None:
                        verify_contact_point(pairs)
                    established = True
                    first_contact_delta = self._measured_delta(circular, p)
                    if self._count_from_contact:
                        self._contact_start_distance = first_contact_delta
                        self._contact_start_position = np.asarray(r.get_end_effector_pose()['position']).copy()
                if established and not contact_now:
                    lost_since = float(data.time) if lost_since is None else lost_since
                    if data.time - lost_since > c['contact_loss_timeout']:
                        maintained = False
                        raise ContactMotionError('Measured target contact lost.', code='CONTACT_LOST')
                else:
                    lost_since = None
                if data.time >= next_view:
                    if getattr(r, 'frame_callback', None):
                        r.frame_callback(r.get_state())
                    if viewer:
                        r._sync_viewer()
                    next_view = data.time + 1 / 60
                    if getattr(r, 'realtime', False):
                        time.sleep(max(0, min(1 / 60, data.time - sim_start - (time.perf_counter() - wall_start))))
                if elapsed >= planned_duration and np.max(np.abs(r.get_joint_positions() - final_reference)) < 0.002 and np.max(np.abs(data.qvel[r.dof_indices])) < 0.01:
                    break
                if elapsed >= planned_duration + c['settle_time']:
                    break
                if elapsed >= c['timeout'] or time.perf_counter() - wall_start > c['timeout']:
                    raise ContactMotionError('Execution timed out.', code='PUSH_PULL_TIMEOUT')
            if np.max(np.abs(r.get_joint_positions() - final_reference)) >= 0.002:
                raise ContactMotionError('Arm did not reach the planned endpoint.', code='ACTUATOR_TRACKING_FAILED')
            actual = self._progress_delta(circular, p)
            tolerance = c['angle_tolerance'] if circular else c['position_tolerance']
            reached = abs(actual - delta) <= tolerance
            expected_position = (center + Rotation.from_rotvec(axis * delta).apply(start_pos - center)
                                 if circular else
                                 (self._contact_start_position if self._count_from_contact and established else start_pos) + direction * delta)
            measured_pose = r.get_end_effector_pose()
            reached = reached and np.linalg.norm(np.asarray(measured_pose['position']) - expected_position) <= c['position_tolerance']
            expected_rotation = Rotation.from_rotvec(axis * delta).as_matrix() @ start_rot if circular else start_rot
            orientation_error = Rotation.from_matrix(expected_rotation.T @ measured_pose['rotation_matrix']).magnitude()
            reached = reached and orientation_error <= c['angle_tolerance']
            if not established:
                raise ContactMotionError('Requested forward stroke ended without measured pad contact.', stage='contact', code='CONTACT_NOT_ESTABLISHED')
            if not reached:
                raise ContactMotionError('Actual target motion did not reach the requested endpoint.', code='TARGET_NOT_REACHED')
            return {
                'contact_established': True,
                'contact_maintained': maintained,
                'target_moved': abs(actual) > 1e-5,
                'target_reached': bool(reached),
                'travel_distance': abs(actual) if not circular else 0.0,
                'rotation_angle': actual if circular else 0.0,
                'signed_travel_distance': actual if not circular else 0.0,
                'peak_force': peak,
                'object_final_state': {'qpos': data.qpos[address:address + (7 if free else 1)].tolist()},
                'execution_time': time.perf_counter() - wall_start,
                'evidence': {
                    'progress_reference': 'tcp', 'execution_mode': 'actuator_dynamics',
                    'contact_mode': action['contact_mode'], 'motion_path_type': path_type,
                    'initial_contact_established': initial_contact,
                    'first_contact_delta': first_contact_delta,
                    'distance_reference': 'first_contact' if self._count_from_contact else 'motion_start',
                    'trajectory_profile': 'continuous_precomputed',
                    'planned_duration': planned_duration, 'time_parameterization': timing,
                    'collision_policy': 'static_planning_only',
                    'planning_time': float(prepared.get('planning_time', 0.0)),
                    'waypoints': int(prepared.get('waypoints', len(prepared.get('path', [])))),
                    'requested_delta': delta,
                },
            }
        except Exception as exc:
            error = exc if isinstance(exc, ContactMotionError) else ContactMotionError(str(exc))
            measured_delta = self._progress_delta(circular, p)
            error.details.update({
                'requested_delta': delta, 'actual_delta': measured_delta,
                'unit': 'rad' if circular else 'm',
                'initial_qpos': initial.tolist(),
                'actual_qpos': data.qpos[address:address + (7 if free else 1)].tolist(),
                'peak_force': peak,
                'simulation_duration': float(data.time) - sim_start,
                'result': {
                    'peak_force': peak,
                    'contact_established': established,
                    'contact_maintained': established and maintained,
                    'execution_time': time.perf_counter() - wall_start,
                    'evidence': {
                        'progress_reference': 'tcp', 'execution_mode': 'actuator_dynamics',
                        'contact_mode': action['contact_mode'], 'motion_path_type': path_type,
                        'planning_time': float(prepared.get('planning_time', 0.0)),
                        'initial_contact_established': initial_contact,
                        'first_contact_delta': first_contact_delta,
                        'distance_reference': 'first_contact' if self._count_from_contact else 'motion_start',
                        'trajectory_profile': 'continuous_precomputed',
                        'planned_duration': planned_duration,
                        'time_parameterization': timing,
                        'waypoints': int(prepared.get('waypoints', len(prepared.get('path', [])))),
                    },
                },
            })
            raise error
        finally:
            data.ctrl[r.actuator_ids] = r.get_joint_positions()

    def _execute(self, request):
        r, data, model = self.runtime, self.data, self.model
        c, p, action, target = (request[k] for k in ('constraints', 'strategy_params', 'manipulation', 'target'))
        ignore_path_constraints = bool(c.get('ignore_path_constraints', False))
        planning_context = request.get('_planning_context') or {}
        planning_only = bool(request.get('_planning_only'))
        body, joint = self._body_joint(target['object_id'])
        self._fcl_target_name = target['object_id']
        address = int(model.jnt_qposadr[joint])
        free = request['resolved_interaction_mode'] == 'free_object'
        motion = request.get('motion', {})
        path_type = motion.get('path_type', action['trajectory'])
        if path_type not in {'linear', 'circular'}:
            raise ContactMotionError('Unsupported end-effector path type.', stage='validation', code='INVALID_REQUEST')
        circular = path_type == 'circular'
        pose = planning_context.get('start_pose') or r.get_end_effector_pose()
        start_pos, start_rot = np.asarray(pose['position'], dtype=float), np.asarray(pose['rotation_matrix'], dtype=float)
        self._count_from_contact = not circular and action['contact_mode'] == 'push_contact'
        self._linear_start_position = start_pos.copy()
        self._contact_start_distance = None
        self._contact_start_position = None
        initial = data.qpos[address:address + (7 if free else 1)].copy()
        bodies = self._descendants(body)
        mechanism_root = body
        while int(model.body_parentid[mechanism_root]) != 0:
            mechanism_root = int(model.body_parentid[mechanism_root])
        mechanism_bodies = self._descendants(mechanism_root)
        geom = None
        if target.get('contact_geom'):
            geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, target['contact_geom'])
            if geom < 0 or int(model.geom_bodyid[geom]) not in bodies:
                raise ContactMotionError('contact_geom must belong to the target link.', stage='validation')
        planning_data = mujoco.MjData(model)
        mujoco.mj_copyData(planning_data, model, data)
        planned_start_joints = planning_context.get('start_joints')
        if planned_start_joints is not None:
            planned_start_joints = np.asarray(planned_start_joints, dtype=float).reshape(-1)
            if planned_start_joints.shape != r.get_joint_positions().shape or not np.all(np.isfinite(planned_start_joints)):
                raise ContactMotionError('Invalid prepared interaction joint state.', stage='planning', code='INVALID_REQUEST')
            planning_data.qpos[r.qpos_indices] = planned_start_joints
            mujoco.mj_forward(model, planning_data)
        force, allowed = self._contacts(planning_data, bodies, geom)
        established = bool(allowed and force >= c['contact_threshold'])
        initial_contact = established
        if established and self._count_from_contact:
            self._contact_start_distance = 0.0
            self._contact_start_position = self._linear_start_position.copy()
        acquire_during_motion = action['contact_mode'] == 'push_contact' and not circular
        allow_unestablished = bool(planning_context.get('allow_unestablished_contact'))
        if not established and not acquire_during_motion and not allow_unestablished:
            raise ContactMotionError('Establish measured pad contact before Push/Pull.', stage='contact', code='CONTACT_NOT_ESTABLISHED')
        if acquire_during_motion or (allow_unestablished and not established):
            # Permit only pad contact with the requested target surface. The
            # first contact need not exist before planning a forward stroke.
            pads = [g for g in range(model.ngeom) if self._pad(g)]
            surfaces = [g for g in range(model.ngeom) if int(model.geom_bodyid[g]) in bodies
                        and (geom is None or g == geom)]
            allowed = {frozenset((pad, surface)) for pad in pads for surface in surfaces}
        if c['maximum_force'] is not None and force > c['maximum_force']:
            raise ContactMotionError('Initial contact exceeds maximum_force.', stage='contact', code='FORCE_LIMIT')
        self_pairs, support = set(), {}
        for i in range(data.ncon):
            ct = data.contact[i]; a, b = int(ct.geom1), int(ct.geom2)
            pair = frozenset((a, b))
            ba, bb = int(model.geom_bodyid[a]), int(model.geom_bodyid[b])
            if ba in self.robot_bodies and bb in self.robot_bodies:
                self_pairs.add(pair)
            if free and ((ba in bodies) != (bb in bodies)) and ba not in self.robot_bodies and bb not in self.robot_bodies:
                # Only a surface normal perpendicular to travel is a support.
                if abs(float(np.dot(ct.frame[:3], (np.cross(p['arc_axis'], initial[:3]-np.asarray(p['arc_center'])) if circular else p['direction'])))) < 0.1:
                    support[pair] = float(ct.dist)
        normal = (np.asarray(request['resolved_contact_normal']) if request['resolved_contact_normal'] is not None else None)
        if normal is not None and np.degrees(np.arccos(np.clip(abs(start_rot[:, 2] @ normal), -1, 1))) > c['normal_alignment_tolerance']:
            raise ContactMotionError('Tool Z axis is not aligned with contact normal.', stage='planning')
        center = np.asarray(p['arc_center'] if circular else [0., 0., 0.], dtype=float)
        axis = np.asarray(p['arc_axis'] if circular else [0., 0., 0.], dtype=float)
        delta = float(p['arc_angle'] if circular else p['distance'])
        direction = np.asarray(p.get('direction', [0., 0., 0.]))
        self._linear_direction = direction.copy()
        # Only predict object motion when the requested geometry matches its joint.
        # Arbitrary Cartesian pushes keep the target at its measured pose in the
        # geometric probe. Actual movement and contact are checked during physics.
        joint_alignment = float(axis @ data.xaxis[joint])
        hinge_follow = (circular and not free and model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_HINGE
                        and abs(joint_alignment) > 1-1e-5
                        and np.linalg.norm(np.cross(center-data.xanchor[joint], axis)) < 1e-4)
        slide_follow = (not circular and not free and model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_SLIDE
                        and abs(float(direction @ data.xaxis[joint])) > 1-1e-5)
        planning_stop = None
        end_fraction = 1.0
        cap = c['maximum_angle'] if circular else c['maximum_travel']
        if cap is not None and abs(delta) > cap:
            raise ContactMotionError(
                'Configured travel limit reached.', stage='planning', code='TRAVEL_LIMIT',
                details={'requested': abs(delta), 'limit': float(cap),
                         'unit': 'rad' if circular else 'm'})
        if hinge_follow and model.jnt_limited[joint]:
            low, high = model.jnt_range[joint]
            reachable = max(0.0, (high-initial[0]) if delta*joint_alignment > 0 else (initial[0]-low))
            if reachable / abs(delta) < end_fraction:
                joint_name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_JOINT, joint) or f'joint_{joint}'
                raise ContactMotionError(
                    'Mechanism joint limit reached.', stage='planning', code='JOINT_LIMIT',
                    details={'joint': joint_name, 'current': float(initial[0]),
                             'lower': float(low), 'upper': float(high),
                             'requested_delta': float(delta), 'reachable_delta': float(reachable)})
        local = target.get('contact_point')
        def verify_contact_point(pairs):
            actual_point = data.xpos[body] + data.xmat[body].reshape(3, 3) @ np.asarray(local)
            # Check that the nominated surface point is actually contacted.
            if not any(np.linalg.norm(data.contact[i].pos - actual_point) < 0.02
                       for i in range(data.ncon) if frozenset((int(data.contact[i].geom1), int(data.contact[i].geom2))) in pairs):
                raise ContactMotionError('contact_point is not at the measured pad contact.', stage='contact')
        if local is not None and established:
            verify_contact_point(self._contacts(data, bodies, geom)[1])
        tangent = np.cross(axis, start_pos - center) * np.sign(delta) if circular else direction
        if normal is not None and action['contact_mode'] == 'push_contact' and float(tangent @ normal) >= -1e-6:
            raise ContactMotionError('Requested motion pulls away from the surface; non-grasped contact can only push.', stage='planning')
        started = time.perf_counter()
        scratch = planning_data
        # During a non-grasped approach the object stays at its measured pose
        # until the scratch path first reaches a pad contact. This predicts no
        # live motion: execution moves both arm and object only through physics.
        predicted_contact_fraction = 0.0 if established else None
        previous_precontact_fraction = 0.0
        previous_precontact_q = r.get_joint_positions().copy()

        def validate_reference(q, fraction, *, check_geometry=False):
            nonlocal predicted_contact_fraction, previous_precontact_fraction, previous_precontact_q
            scratch.qpos[r.qpos_indices] = q
            object_fraction = fraction
            if acquire_during_motion:
                object_fraction = (0.0 if predicted_contact_fraction is None else
                                   max(0.0, fraction-predicted_contact_fraction))
            if free:
                if circular:
                    predicted_rotation = Rotation.from_rotvec(axis*delta*fraction)
                    scratch.qpos[address:address+3] = center + predicted_rotation.apply(initial[:3]-center)
                    quat = (predicted_rotation * Rotation.from_quat(initial[[4,5,6,3]])).as_quat()
                    scratch.qpos[address+3:address+7] = quat[[3,0,1,2]]
                else:
                    scratch.qpos[address:address+3] = initial[:3] + direction*p['distance']*object_fraction
            elif hinge_follow:
                scratch.qpos[address] = initial[0] + delta*joint_alignment*fraction
            elif slide_follow:
                scratch.qpos[address] = initial[0] + delta*float(direction @ data.xaxis[joint])*object_fraction
            mujoco.mj_forward(model, scratch)
            if acquire_during_motion and predicted_contact_fraction is None:
                if self._contacts(scratch, bodies, geom)[1]:
                    # Locate contact within the last checked edge, rather than
                    # adding an entire coarse Cartesian sample to the stroke.
                    low, high = previous_precontact_fraction, float(fraction)
                    for _ in range(24):
                        if abs(delta)*(high-low) <= 1e-5:
                            break
                        middle = (low+high)/2
                        alpha = (middle-previous_precontact_fraction)/(fraction-previous_precontact_fraction)
                        scratch.qpos[r.qpos_indices] = previous_precontact_q+alpha*(q-previous_precontact_q)
                        mujoco.mj_forward(model, scratch)
                        if self._contacts(scratch, bodies, geom)[1]:
                            high = middle
                        else:
                            low = middle
                    predicted_contact_fraction = high
                    return validate_reference(q, fraction, check_geometry=check_geometry)
                previous_precontact_fraction = float(fraction)
                previous_precontact_q = q.copy()
            if check_geometry and hasattr(r, 'tip_id'):
                rotation = Rotation.from_rotvec(axis*delta*fraction).as_matrix() if circular else np.eye(3)
                expected_position = (center + rotation @ (start_pos-center) if circular
                                     else start_pos + direction*p['distance']*fraction)
                expected_rotation = rotation @ start_rot
                position_error = np.linalg.norm(scratch.site_xpos[r.tip_id] - expected_position)
                angle_error = Rotation.from_matrix(expected_rotation.T @ scratch.site_xmat[r.tip_id].reshape(3,3)).magnitude()
                if position_error > c['position_tolerance'] or angle_error > c['angle_tolerance']:
                    raise ContactMotionError('Smoothed path exceeds Cartesian path tolerance.',
                                             stage='planning', code='PATH_CONSTRAINT_INFEASIBLE')
            self._fcl_collision(q)
            self._collision(scratch, mechanism_bodies, allowed, support, self_pairs, bodies)

        planning_start_joints = (planned_start_joints.copy() if planned_start_joints is not None
                                 else r.get_joint_positions().copy())
        seed = planning_start_joints.copy()
        path = [seed.copy()]
        fractions = [0.0]
        travel = abs(delta) * np.linalg.norm(np.cross(axis, start_pos-center)) if circular else abs(delta)
        count = max(2, int(np.ceil(travel * end_fraction / c['cartesian_step'])) + 1,
                    int(np.ceil(abs(delta) * end_fraction / c['angular_step'])) + 1 if circular else 2)
        # The contact stroke excludes the gap. Extend the same precomputed path
        # when geometry first predicts contact; measured contact remains the
        # authoritative origin for execution feedback and final verification.
        stroke_fraction = end_fraction
        fraction_step = end_fraction / (count-1)
        extended_for_contact = not acquire_during_motion or established
        self._fcl_collision(r.get_joint_positions())
        self._collision(scratch, mechanism_bodies, allowed, support, self_pairs, bodies)
        try:
            while fractions[-1] < end_fraction-1e-12:
                fraction = min(end_fraction, fractions[-1]+fraction_step)
                if time.perf_counter() - started > c['timeout']:
                    raise ContactMotionError('Planning timeout.', stage='planning', code='PUSH_PULL_TIMEOUT')
                rotation = Rotation.from_rotvec(axis * delta * fraction).as_matrix() if circular else np.eye(3)
                position = center + rotation @ (start_pos-center) if circular else start_pos + direction * p['distance'] * fraction
                orientation = Rotation.from_matrix(rotation @ start_rot).as_quat()
                target_pose = {'position': position,
                               'orientation': dict(zip(('x','y','z','w'), orientation))}
                q = None
                candidate_error = None
                continuity_rejected = False
                if hasattr(self.kinematics, 'solve_all'):
                    solved = self.kinematics.solve_all(
                        target_pose, seed=seed, backend='auto', timeout=min(0.25, c['timeout']),
                        max_solutions=8,
                        max_joint_step=None if ignore_path_constraints else 0.3)
                    solutions = solved.solutions
                else:
                    # Small test/runtime adapters may expose only solve_one;
                    # keep the same continuity checks with a single branch.
                    solutions = [self.kinematics.solve_one(
                        target_pose, seed=seed, backend='auto',
                        timeout=min(0.25, c['timeout']),
                        max_joint_step=None if ignore_path_constraints else 0.3).solution]
                candidates = sorted((np.asarray(candidate, dtype=float) for candidate in solutions),
                                    key=lambda candidate: float(np.linalg.norm(candidate-seed)))
                for candidate in candidates[:8]:
                    if candidate.shape != seed.shape or not np.all(np.isfinite(candidate)):
                        continue
                    if (not ignore_path_constraints and
                            np.max(np.abs(candidate-seed)) > 0.3 + 1e-8):
                        continuity_rejected = True
                        continue
                    try:
                        previous_fraction = fractions[-1]
                        for sub in np.linspace(0, 1, max(2, int(np.ceil(np.max(np.abs(candidate-seed))/0.02))+1))[1:]:
                            f = previous_fraction + sub * (fraction-previous_fraction)
                            validate_reference(seed + sub*(candidate-seed), f,
                                               check_geometry=not ignore_path_constraints)
                        q = candidate
                        break
                    except ContactMotionError as exc:
                        candidate_error = exc
                if q is None:
                    if candidate_error is not None:
                        raise candidate_error
                    if continuity_rejected:
                        raise ContactMotionError(
                            'No continuous IK branch at path sample.',
                            stage='planning', code='PATH_CONSTRAINT_INFEASIBLE',
                            details={'maximum_step': 0.3})
                    raise ContactMotionError('No collision-free IK branch at path sample.',
                                             stage='planning', code='COLLISION_DETECTED')
                if q.shape != seed.shape or not np.all(np.isfinite(q)):
                    raise ValueError('IK returned no finite joint solution')
                if np.any(q < r.joint_limits[:, 0]) or np.any(q > r.joint_limits[:, 1]):
                    violating = np.flatnonzero((q < r.joint_limits[:, 0]) | (q > r.joint_limits[:, 1]))
                    joint_names = getattr(r, 'joint_names', tuple(f'joint_{i}' for i in range(len(q))))
                    details = {'joints': [
                        {'joint': str(joint_names[int(index)]), 'value': float(q[index]),
                         'lower': float(r.joint_limits[index, 0]), 'upper': float(r.joint_limits[index, 1])}
                        for index in violating]}
                    raise ContactMotionError('IK reaches arm joint limits', stage='planning',
                                             code='ROBOT_JOINT_LIMIT', details=details)
                if not ignore_path_constraints and np.max(np.abs(q-seed)) > 0.3 + 1e-8:
                    delta_q = np.abs(q-seed)
                    index = int(np.argmax(delta_q))
                    joint_names = getattr(r, 'joint_names', tuple(f'joint_{i}' for i in range(len(q))))
                    raise ContactMotionError(
                        'No continuous IK continuation', stage='planning', code='PATH_CONSTRAINT_INFEASIBLE',
                        details={'joint': str(joint_names[index]), 'step': float(delta_q[index]),
                                 'maximum_step': 0.3})
                path.append(q.copy()); fractions.append(float(fraction)); seed = q
                if not extended_for_contact and predicted_contact_fraction is not None:
                    end_fraction = predicted_contact_fraction+stroke_fraction
                    extended_for_contact = True
        except Exception as exc:
            planning_stop = exc if isinstance(exc, ContactMotionError) else ContactMotionError(
                str(exc), stage='planning', code='PATH_CONSTRAINT_INFEASIBLE')
            planning_stop.failed_stage = 'planning'
        if planning_stop:
            planning_stop.details.update(motion_executed=False)
            raise planning_stop
        if len(path) < 2:
            raise ContactMotionError('No complete path.', stage='planning', code='PATH_CONSTRAINT_INFEASIBLE')
        path = np.asarray(path)
        # Smooth across IK samples and check the resulting curve before moving.
        # Only the endpoints stop; all actuator setpoints are prepared up front.
        profile = ContinuousJointPath(path, fractions)
        samples = np.linspace(0, 1, max(2, len(path)*4,
                                       int(np.ceil(profile.max_slope/0.01))+1))
        for progress in samples:
            if time.perf_counter() - started > c['timeout']:
                raise ContactMotionError('Trajectory validation timeout.', stage='planning', code='PUSH_PULL_TIMEOUT')
            try:
                validate_reference(profile.spline(progress), progress,
                                   check_geometry=not ignore_path_constraints)
            except ContactMotionError:
                raise
        timing = p.get('time_parameterization')
        if timing is None:
            timing = {'source': 'push_pull_constraints', 'max_velocity': c['max_joint_speed'],
                      'max_acceleration': c['max_joint_acceleration'],
                      'task_speed_limit': c['angular_speed'] if circular else c['speed']}
        references, planned_duration = profile.samples(
            1.0, abs(delta),
            timing['task_speed_limit'], timing['max_velocity'], timing['max_acceleration'], model.opt.timestep)
        prepared = {
            'path': np.asarray(path, dtype=float).copy(),
            'fractions': np.asarray(fractions, dtype=float).copy(),
            'references': np.asarray(references, dtype=float).copy(),
            'planned_duration': float(planned_duration),
            'timing': timing,
            'initial_object_qpos': np.asarray(initial, dtype=float).copy(),
            'start_joints': planning_start_joints.copy(),
            'start_position': np.asarray(start_pos, dtype=float).copy(),
            'start_rotation': np.asarray(start_rot, dtype=float).copy(),
            'delta': float(delta),
            'direction': np.asarray(direction, dtype=float).copy(),
            'center': np.asarray(center, dtype=float).copy(),
            'axis': np.asarray(axis, dtype=float).copy(),
            'circular': bool(circular),
            'body': int(body),
            'joint': int(joint),
            'address': int(address),
            'free': bool(free),
            'geom': None if geom is None else int(geom),
            'initial_contact': bool(initial_contact),
            'planning_force': float(force),
            'waypoints': int(len(path)),
            'planning_time': float(time.perf_counter() - started),
        }
        if planning_only:
            return {'prepared': prepared}
        final_reference = references[-1]
        sim_start = float(data.time); wall_start = time.perf_counter(); lost_since = None
        peak = force; maintained = True; next_view = sim_start
        first_contact_delta = 0.0 if established else None
        try:
            while True:
                elapsed = float(data.time)-sim_start
                ref = references[min(int(round(elapsed/model.opt.timestep)), len(references)-1)]
                viewer = getattr(r, 'viewer', None)
                with viewer.lock() if viewer else nullcontext():
                    data.ctrl[r.actuator_ids] = ref
                    r.apply_arm_compensation()
                    gripper = getattr(r, 'gripper_controller', None)
                    if gripper:
                        gripper.update_hold_force()
                    previous_time = float(data.time)
                    mujoco.mj_step(model, data)
                    if data.time <= previous_time or not np.all(np.isfinite(data.qpos)):
                        raise ContactMotionError("Physics became unstable.", code="PHYSICS_UNSTABLE")
                    mujoco.mj_forward(model, data)
                r.physics_steps += 1
                if circular:
                    self._measured_delta(circular, p)
                force, pairs = self._contacts(data, bodies, geom)
                if action['contact_mode'] == 'grasp':
                    grasp_state = gripper.get_state() if gripper else {}
                    if not grasp_state.get('holding'):
                        pairs = set()
                peak = max(peak, force)
                if c['maximum_force'] is not None and force > c['maximum_force']:
                    raise ContactMotionError('Measured contact force exceeded maximum_force.', code='FORCE_LIMIT')
                # Establish contact with the force threshold. Once pushing,
                # a real pad contact remains valid at low load during braking;
                # losing the actual contact pairs still starts the loss timer.
                zero_force = force <= c.get('zero_force_threshold', 0.0)
                contact_now = bool(pairs and (
                    (not established and force >= c['contact_threshold']) or
                    (established and not zero_force)))
                if contact_now and not established:
                    if local is not None:
                        verify_contact_point(pairs)
                    established = True
                    first_contact_delta = self._measured_delta(circular, p)
                    if self._count_from_contact:
                        self._contact_start_distance = first_contact_delta
                        self._contact_start_position = np.asarray(r.get_end_effector_pose()['position']).copy()
                if established and not contact_now:
                    lost_since = float(data.time) if lost_since is None else lost_since
                    if data.time-lost_since > c['contact_loss_timeout']:
                        maintained = False
                        raise ContactMotionError('Measured target contact lost.', code='CONTACT_LOST')
                else:
                    lost_since = None
                if data.time >= next_view:
                    if getattr(r, 'frame_callback', None):
                        r.frame_callback(r.get_state())
                    if viewer:
                        r._sync_viewer()
                    next_view = data.time+1/60
                    if getattr(r, 'realtime', False):
                        time.sleep(max(0, min(1/60, data.time-sim_start-(time.perf_counter()-wall_start))))
                if elapsed >= planned_duration and np.max(np.abs(r.get_joint_positions()-final_reference)) < 0.002 and np.max(np.abs(data.qvel[r.dof_indices])) < 0.01:
                    break
                if elapsed >= planned_duration+c['settle_time']:
                    break
                if elapsed >= c['timeout'] or time.perf_counter()-wall_start > c['timeout']:
                    raise ContactMotionError('Execution timed out.', code='PUSH_PULL_TIMEOUT')
            if np.max(np.abs(r.get_joint_positions()-final_reference)) >= 0.002:
                raise ContactMotionError('Arm did not reach the planned endpoint.', code='ACTUATOR_TRACKING_FAILED')
            actual = self._progress_delta(circular, p)
            tolerance = c['angle_tolerance'] if circular else c['position_tolerance']
            reached = abs(actual-delta) <= tolerance
            expected_position = (center + Rotation.from_rotvec(axis*delta).apply(start_pos-center)
                                 if circular else
                                 (self._contact_start_position if self._count_from_contact and established else start_pos)+direction*delta)
            measured_pose = r.get_end_effector_pose()
            reached = reached and np.linalg.norm(np.asarray(measured_pose['position'])-expected_position) <= c['position_tolerance']
            expected_rotation = Rotation.from_rotvec(axis*delta).as_matrix() @ start_rot if circular else start_rot
            orientation_error = Rotation.from_matrix(expected_rotation.T @ measured_pose['rotation_matrix']).magnitude()
            reached = reached and orientation_error <= c['angle_tolerance']
            if not established:
                raise ContactMotionError('Requested forward stroke ended without measured pad contact.',
                                         stage='contact', code='CONTACT_NOT_ESTABLISHED')
            if not reached:
                raise ContactMotionError('Actual target motion did not reach the requested endpoint.', code='TARGET_NOT_REACHED')
            return {'contact_established': True, 'contact_maintained': maintained, 'target_moved': abs(actual)>1e-5,
                    'target_reached': bool(reached), 'travel_distance': abs(actual) if not circular else 0.0,
                    'rotation_angle': actual if circular else 0.0, 'signed_travel_distance': actual if not circular else 0.0, 'peak_force': peak,
                    'object_final_state': {'qpos': data.qpos[address:address+(7 if free else 1)].tolist()},
                    'execution_time': time.perf_counter()-wall_start,
                    'evidence': {'progress_reference': 'tcp', 'execution_mode': 'actuator_dynamics', 'contact_mode': action['contact_mode'],
                                 'motion_path_type': path_type,
                                 'initial_contact_established': initial_contact,
                                 'first_contact_delta': first_contact_delta,
                                 'distance_reference': 'first_contact' if self._count_from_contact else 'motion_start',
                                 'trajectory_profile': 'continuous_precomputed', 'planned_duration': planned_duration,
                                 'time_parameterization': timing,
                                 'collision_policy': 'static_planning_only',
                                 'planning_time': wall_start-started, 'waypoints': len(path),
                                 'requested_delta': delta}}
        except Exception as exc:
            error = exc if isinstance(exc, ContactMotionError) else ContactMotionError(str(exc))
            measured_delta = self._progress_delta(circular, p)
            error.details.update({'requested_delta': delta, 'actual_delta': measured_delta, 'unit': 'rad' if circular else 'm', 'initial_qpos': initial.tolist(),
                                  'actual_qpos': data.qpos[address:address+(7 if free else 1)].tolist(),
                                  'peak_force': peak, 'simulation_duration': float(data.time)-sim_start,
                                  'result': {'peak_force': peak, 'contact_established': established,
                                             'contact_maintained': established and maintained,
                                             'execution_time': time.perf_counter()-wall_start,
                                             'evidence': {'progress_reference': 'tcp', 'execution_mode': 'actuator_dynamics', 'motion_path_type': path_type, 'planning_time': wall_start-started,
                                                          'initial_contact_established': initial_contact,
                                                          'first_contact_delta': first_contact_delta,
                                                          'distance_reference': 'first_contact' if self._count_from_contact else 'motion_start',
                                                          'trajectory_profile': 'continuous_precomputed', 'planned_duration': planned_duration,
                                                          'time_parameterization': timing,
                                                          'waypoints': len(path)}}})
            raise error
        finally:
            # Stop advancing the reference; never teleport the arm or the door back.
            data.ctrl[r.actuator_ids] = r.get_joint_positions()

    def _align_prepared_joint_branch(self, prepared, actual_q):
        """Align periodic prepared joints with the measured winding."""
        planned_q = np.asarray(prepared.get('start_joints', []), dtype=float)
        actual_q = np.asarray(actual_q, dtype=float)
        if planned_q.shape != actual_q.shape or planned_q.ndim != 1:
            return prepared, planned_q, np.zeros_like(actual_q)
        joint_ids = np.asarray(getattr(self.runtime, 'joint_ids', []), dtype=int)
        if joint_ids.shape != planned_q.shape:
            return prepared, planned_q, np.zeros_like(actual_q)
        hinge = self.model.jnt_type[joint_ids] == mujoco.mjtJoint.mjJNT_HINGE
        offsets = np.zeros_like(planned_q)
        limits = np.asarray(self.runtime.joint_limits, dtype=float)
        path = np.asarray(prepared.get('path', []), dtype=float)
        references = np.asarray(prepared.get('references', []), dtype=float)
        for index in np.flatnonzero(hinge):
            turns = int(np.rint((actual_q[index] - planned_q[index]) / JOINT_PERIOD))
            if turns == 0:
                continue
            candidate = planned_q[index] + turns * JOINT_PERIOD
            if abs(actual_q[index] - candidate) > 0.02:
                continue
            values = [candidate]
            if path.ndim == 2 and path.shape[1] == len(planned_q):
                values.extend(path[:, index] + turns * JOINT_PERIOD)
            if references.ndim == 2 and references.shape[1] == len(planned_q):
                values.extend(references[:, index] + turns * JOINT_PERIOD)
            if limits.shape == (len(planned_q), 2) and any(
                    value < limits[index, 0] - 1e-8 or value > limits[index, 1] + 1e-8
                    for value in values):
                continue
            offsets[index] = turns * JOINT_PERIOD
        if not np.any(np.abs(offsets) > 0.0):
            return prepared, planned_q, offsets
        aligned = deepcopy(prepared)
        aligned['start_joints'] = planned_q + offsets
        for key in ('path', 'references'):
            values = np.asarray(aligned.get(key, []), dtype=float)
            if values.ndim == 2 and values.shape[1] == len(offsets):
                aligned[key] = values + offsets
        aligned['joint_branch_alignment'] = {
            'offsets': offsets.tolist(),
            'reason': 'periodic_equivalent_interaction_configuration',
        }
        return aligned, np.asarray(aligned['start_joints'], dtype=float), offsets

