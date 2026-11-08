"""Pull task skill with local contact preparation, execution, and reporting."""
from copy import deepcopy
import math
import mujoco
import numpy as np
from scipy.spatial.transform import Rotation
from demo.task_commands.converter import _deep_merge
from demo.interaction.requests import body_name, motion_request, push_pull_request
from demo.interaction.closure_validation import execute_checked_grasp
from atomic_skill.release import release_request
from atomic_action.motion.common.interaction import execute_grasp_pose_move, planning_failed_without_motion


class PullMotionExecutor:
    operation = None

    def __init__(self, *, resources, backend, kinematics, synchronizer,
                 execute_motion, push_pull_skill, grasper, releaser, checker):
        self.resources, self.backend, self.kinematics = resources, backend, kinematics
        self.sync, self.checker = synchronizer, checker
        self.execute_motion, self.skill = execute_motion, push_pull_skill
        self.grasper, self.releaser = grasper, releaser
        self._demo_ignore_fallback_collisions = False
        self._demo_saved_collision_masks = None


    def _demo_set_robot_collisions(self, disabled, *, restore_pads=False):
        """Disable robot-vs-environment contacts only during visual fallback transit."""
        if not self._demo_ignore_fallback_collisions:
            return
        model = self.backend.model
        if disabled and self._demo_saved_collision_masks is None:
            root = int(self.backend.base_id)
            bodies = {root}
            for body in range(1, model.nbody):
                if int(model.body_parentid[body]) in bodies:
                    bodies.add(body)
            geoms = [g for g in range(model.ngeom) if int(model.geom_bodyid[g]) in bodies]
            self._demo_saved_collision_masks = (
                geoms,
                model.geom_contype[geoms].copy(),
                model.geom_conaffinity[geoms].copy(),
            )
            model.geom_contype[geoms] = 0
            model.geom_conaffinity[geoms] = 0
            mujoco.mj_forward(model, self.backend.data)
        elif not disabled and self._demo_saved_collision_masks is not None:
            geoms, contype, conaffinity = self._demo_saved_collision_masks
            if restore_pads:
                for index, geom in enumerate(geoms):
                    body_name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY,
                                                  int(model.geom_bodyid[geom])) or ''
                    if 'pad' in body_name:
                        model.geom_contype[geom] = contype[index]
                        model.geom_conaffinity[geom] = conaffinity[index]
            else:
                model.geom_contype[geoms] = contype
                model.geom_conaffinity[geoms] = conaffinity
                self._demo_saved_collision_masks = None
            mujoco.mj_forward(model, self.backend.data)


    def _grasp_pose(self):
        b = self.backend
        site = mujoco.mj_name2id(b.model, mujoco.mjtObj.mjOBJ_SITE, "grasp_center")
        if site < 0:
            pose=b.get_end_effector_pose()
            return np.asarray(pose['position']).copy(), np.asarray(pose['rotation_matrix']).copy()
        return b.data.site_xpos[site].copy(), b.data.site_xmat[site].reshape(3,3).copy()


    def _phase(self, name):
        self.phase = name
        self.backend.command_substep = name
        self.backend.set_status(name)


    def _record(self, name, result):
        self.phases.append({'phase':name, **result})
        self.sync.sync(self.backend,self.kinematics,self.checker)
        return bool(result.get('success'))


    def _fail(self, code, message, **details):
        result={'success':False,'error':{'error_code':code,'failed_stage':self.phase,
                                      'error_message':message,'details':details}}
        self._record(self.phase,result)
        return False


    def _control_request(self, position, rotation, linear=False, override=None):
        # Inputs a/b/c refer to grasp_center, not the TCP behind the fingers.
        gp,gr=self._grasp_pose(); tcp=self.backend.get_end_effector_pose()
        local_rotation=np.asarray(tcp['rotation_matrix']).T@gr
        local_offset=np.asarray(tcp['rotation_matrix']).T@(gp-np.asarray(tcp['position']))
        control_rotation=rotation@local_rotation.T
        control_position=position-control_rotation@local_offset
        quat=Rotation.from_matrix(control_rotation).as_quat()
        req=_deep_merge(self.resources.scene['move_defaults'], self.params.get('move_request',{}))
        req=_deep_merge(req,override or {})
        req['target']={'type':'pose','frame':'world','position':dict(zip('xyz',control_position)),
                       'orientation':dict(representation='quaternion',**dict(zip('xyzw',quat)))}
        req['motion']={'path_type':'linear' if linear else 'joint', 'path_constraint':'hard' if linear else 'soft'}
        if linear:
            req['planning']={'mode':'direct','allow_replan':False,'planning_time':5}
        return req


    def _move(self, name, position=None, rotation=None, *, spec=None, linear=False, contact=False, override=None,
              resolved_request=None, grasp_pose_fallback=False):
        self._phase(name)
        self.backend.contact_target=self.name if contact else None
        self.backend.contact_target_object_id = self.params.get('target') if contact else None
        self.sync.sync(self.backend,self.kinematics,self.checker)
        if resolved_request is not None:
            req=deepcopy(resolved_request)
        elif spec:
            spec={'target':self.params['target'],'link':self.params.get('link'),**deepcopy(spec)}
            req=motion_request(self.resources,self.backend,spec)
        else:
            req=self._control_request(np.asarray(position),np.asarray(rotation),linear,override)
        def checked(state):
            if self.callback:self.callback(state)
            self.sync.sync(self.backend,self.kinematics,self.checker)
            if not self._demo_ignore_fallback_collisions:
                hit=self.checker.check(state['joints'])
                if hit.in_collision:raise RuntimeError(f'Execution frame collision: {hit.contacts}')
        self.backend.set_frame_callback(checked)
        try:
            if grasp_pose_fallback:
                req,result=execute_grasp_pose_move(req,self.execute_motion,self.backend)
            else:
                result=self.execute_motion(req)
        finally:
            self.backend.set_frame_callback(self.callback)
        execution_result = result.get('execution_result')
        if isinstance(execution_result, dict):
            execution_result.pop('frames', None)
        return self._record(name,{'request':req,**result})


    def _release(self):
        self._phase('release')
        req=release_request(self.resources,{'target':self.params['target'],'link':self.params.get('link'),**self.params.get('release_request',{})})
        result=self.releaser.execute(req)
        self.checker.support_geoms.clear();self.checker.carrying_safety_distance=0.
        return self._record('release',result)


    def _post_contact_retreat(self, request, *, release_first=False, operation=None):
        # Use the task-level RetreatSkill for every post-contact retreat.  A
        # contact mechanism pose is preferred so the reverse-derived target
        # remains expressed in the same local frame as approach.  Commands
        # without one (for example the world-goal fallback) use the measured
        # current world pose and derive the local axis from the motion request.
        from atomic_skill.retreat import RetreatSkill
        contact = self.params.get('contact') or {}
        target_pose = deepcopy(contact.get('mechanism_pose')) if isinstance(contact, dict) else None
        retreat_params = {
            'search': deepcopy(self.params.get('retreat_search') or {}),
            'pose_axis': self.params.get('pose_axis', '+z'),
        }
        if target_pose is None:
            pos, rot = self._grasp_pose()
            target_pose = {
                'position': pos.tolist(),
                'quaternion_xyzw': Rotation.from_matrix(rot).as_quat().tolist(),
                'frame': 'world',
            }
            strategy = request.get('strategy_params', {})
            direction = np.asarray(strategy.get('direction', [0., 0., 0.]), dtype=float)
            if (operation or self.operation or 'pull') == 'push':
                direction = -direction
            if np.linalg.norm(direction) > 1e-9:
                retreat_params['pose_axis'] = (rot.T @ (-direction / np.linalg.norm(direction))).tolist()
        retreat_params['target_pose'] = target_pose
        return RetreatSkill(self).execute(retreat_params, release_first=release_first)

    def _retreat_clearance_met(self, safety_distance=0.003):
        """Measure retreat clearance while treating the target as an ordinary pair."""
        previous_target = getattr(self.backend, 'contact_target', None)
        try:
            self.backend.contact_target = None
            previous_target_id = getattr(self.backend, 'contact_target_object_id', None)
            self.backend.contact_target_object_id = None
            self.sync.sync(self.backend, self.kinematics, self.checker)
            hit = self.checker.check(self.backend.get_joint_positions(),
                                     safety_distance=float(safety_distance))
            return not hit.in_collision
        finally:
            self.backend.contact_target = previous_target
            self.backend.contact_target_object_id = previous_target_id
            self.sync.sync(self.backend, self.kinematics, self.checker)


    def _low(self, name, request, record_history=False):
        from demo.interaction.requests import apply_global_motion_timing
        request = apply_global_motion_timing(self.resources, request)
        self._phase(name);self.backend.contact_target=self.name
        self.backend.contact_target_object_id = self.params.get('target')
        def record(state):
            if record_history:self.history.append(self._grasp_pose()[0].tolist())
            if self.callback:self.callback(state)
        self.backend.set_frame_callback(record)
        try:result=self.skill.execute(request)
        finally:self.backend.set_frame_callback(self.callback)
        if record_history:self.history.append(self._grasp_pose()[0].tolist())
        self._record(name,{'request':request,**result})
        return result

    def _low_prepared(self, name, request, prepared, record_history=False):
        """Execute the interaction trajectory prepared before approach."""
        self._phase(name)
        self.backend.contact_target = self.name
        self.backend.contact_target_object_id = self.params.get('target')
        def record(state):
            if record_history:
                self.history.append(self._grasp_pose()[0].tolist())
            if self.callback:
                self.callback(state)
        self.backend.set_frame_callback(record)
        try:
            result = self.skill.execute_prepared(request, prepared)
        finally:
            self.backend.set_frame_callback(self.callback)
        if record_history:
            self.history.append(self._grasp_pose()[0].tolist())
        self._record(name, {'request': request, 'prepared': {
            'waypoints': int(prepared.get('waypoints', len(prepared.get('path', [])))),
            'planned_duration': float(prepared.get('planned_duration', 0.0)),
        }, **result})
        return result


    def _record_suspended_motion(self, result):
        """Preserve unfinished execution without accepting it as success."""
        partial = result.get('pull_push_result') or {}
        actual_delta = float(partial.get('actual_delta', 0.0) or 0.0)
        if not partial.get('motion_executed'):
            return False
        error = next((p.get('error') for p in reversed(self.phases)
                      if not p.get('success') and p.get('error')), result.get('error')) or {}
        self.details.update(
            completion='partial',
            best_effort=bool(self.params.get('best_effort')),
            partial_stop_reason=error.get('error_code') or self.details.get('partial_stop_reason'),
            partial_actual_delta=actual_delta,
            partial_remaining_delta=float(partial.get('remaining_delta', 0.0) or 0.0),
            partial_unit=partial.get('unit', 'rad'),
        )
        return True

    @staticmethod
    def _suspended_motion_executed(result):
        partial = result.get('pull_push_result') or {}
        return result.get('status') == 'suspend' and bool(partial.get('motion_executed'))


    def execute(self, parameters):
        operation = self.operation
        self.params=deepcopy(parameters);self.phases=[];self.history=[];self.details={}
        self._interaction_joints = None
        self._demo_ignore_fallback_collisions=bool(self.params.get('demo_ignore_fallback_collisions',False))
        set_demo_ignore=getattr(self.checker,'set_demo_ignore_collisions',None)
        if callable(set_demo_ignore):set_demo_ignore(False)
        self.name=body_name(self.resources,parameters)
        self.callback=self.backend.frame_callback
        self.phase=operation
        try:
            okay=self._run()
        except Exception as exc:
            okay=self._fail('COMMAND_EXECUTION_FAILED',str(exc))
        finally:
            self._demo_set_robot_collisions(False)
            if callable(set_demo_ignore):set_demo_ignore(False)
            self._demo_ignore_fallback_collisions=False
            self.backend.set_frame_callback(self.callback)
        stopped = next((p for p in reversed(self.phases) if not p.get('success')), {})
        partial = self.details.get('completion') == 'partial' or stopped.get('status') == 'suspend'
        error=None if okay and not partial else stopped.get('error')
        return {'success':bool(okay) and not partial,
                'status':'suspend' if partial else ('success' if okay else 'error'),
                'completion':'partial' if partial else ('complete' if okay else 'failed'),
                'error':error,'substeps':self.phases,'orchestration':self.details}


    def _run(self):
        raise NotImplementedError

    def _prepare_contact_approach(self, operation):
        p=self.params
        interaction_joints = getattr(self, '_interaction_joints', None)
        if p.get('contact'):
            from atomic_skill.approach import ApproachSkill
            approach_params = {
                'target_pose': deepcopy(p['contact']['mechanism_pose']),
                'search': deepcopy(p.get('approach_search') or {}),
                'pose_axis': p.get('pose_axis', '+z'),
            }
            ApproachSkill(self).execute(operation, params=approach_params)
        for phase in ('contact',):
            if not p.get(phase):
                continue
            before = self.backend.physics_steps
            phase_spec = deepcopy(p[phase])
            if interaction_joints is not None:
                phase_spec['request'] = _deep_merge(
                    phase_spec.get('request', {}),
                    {'strategy_params': {
                        'ik_seed': np.asarray(interaction_joints, dtype=float).tolist(),
                    }})
            if self._move(f'{operation}_{phase}',spec=phase_spec,contact=phase=='contact'):
                if phase != 'contact' or interaction_joints is None:
                    continue
                actual = np.asarray(self.backend.get_joint_positions(), dtype=float)
                if actual.shape == np.asarray(interaction_joints).shape and np.max(
                        np.abs(self._periodic_joint_error(
                            actual, np.asarray(interaction_joints, dtype=float)))) <= .02:
                    continue
                anchor_request = self._joint_anchor_request(
                    interaction_joints, 'contact', phase_spec.get('request'))
                if self._move(f'{operation}_contact_anchor',
                              resolved_request=anchor_request, contact=True):
                    continue
                return False
            failed = self.phases[-1]
            if not p.get('contact') or not planning_failed_without_motion(failed,self.backend,before):
                return False
            # Keep exactly the rejected contact goal; if the pre-point failed,
            # resolve the selected handle pose without moving to that pre-point.
            request = (failed['request'] if phase == 'contact' else
                motion_request(self.resources,self.backend,{
                    'target':p['target'],'link':p.get('link'),**deepcopy(p['contact'])}))
            if phase == 'contact' and interaction_joints is not None:
                request = self._joint_anchor_request(
                    interaction_joints, 'contact', request)
            self.details['grasp_approach_fallback'] = {
                'mode':'collision_free_move','failed_phase':failed['phase'],
                'reason':deepcopy(failed.get('error')), 'original_target':deepcopy(request['target'])}
            if not self._move(f'{operation}_contact_avoidance',resolved_request=request,
                              contact=True,
                              grasp_pose_fallback=interaction_joints is None):
                return False
            failed['recovered_by'] = f'{operation}_contact_avoidance'
            break
        return True

    def _joint_anchor_request(self, joints, phase, base_request=None):
        request = _deep_merge(self.resources.scene['move_defaults'], base_request or {})
        request['target'] = {'type': 'joint',
                             'joint_positions': np.asarray(joints, dtype=float).tolist()}
        request['motion'] = {'path_type': 'joint', 'path_constraint': 'soft', 'phase': phase}
        request.setdefault('planning', {}).update(mode='collision_free', allow_replan=True)
        return request

    def _periodic_joint_error(self, actual, target):
        """Compare hinge joints by their shortest equivalent angular distance."""
        error = np.asarray(actual, dtype=float) - np.asarray(target, dtype=float)
        joint_ids = np.asarray(getattr(self.backend, 'joint_ids', []), dtype=int)
        if joint_ids.shape == error.shape:
            hinge = self.backend.model.jnt_type[joint_ids] == mujoco.mjtJoint.mjJNT_HINGE
            error = error.copy()
            error[hinge] = (error[hinge] + np.pi) % (2.0 * np.pi) - np.pi
        return error


    def _prepare_grasp(self, operation, *, prepare_approach=True):
        p=self.params
        state = self.backend.gripper_controller.get_state(self.name)
        if state.get('holding') and state.get('mode') != p.get('grasp', {}).get('mode', 'binding'):
            return self._fail('GRASP_MODE_MISMATCH', 'Release the current grasp before changing binding/physical mode')
        # A sequential close reuses the existing grasp at the measured open pose.
        if not state.get('holding') and prepare_approach:
            if not self._prepare_contact_approach(operation):
                return False
        if not self.backend.gripper_controller.get_state(self.name).get('holding'):
            self._phase('grasp')
            req=_deep_merge({'target':{'type':'object','object_id':self.name},'grasp':p.get('grasp', {'mode':'binding'})},p.get('grasp_request',{}))
            if req['target']['object_id']!=self.name:return self._fail('INVALID_REQUEST','Grasp target mismatch')
            result=execute_checked_grasp(self.grasper,req,self.backend,self.checker)
            if not self._record('grasp',result):return False
        return True

    def _interaction_target(self):
        spec = self.params.get('contact')
        if spec:
            resolved = {'target': self.params['target'], 'link': self.params.get('link'), **deepcopy(spec)}
            request = motion_request(self.resources, self.backend, resolved)
            target = request['target']
            position = np.asarray([target['position'][key] for key in 'xyz'], dtype=float)
            orientation = target['orientation']
            rotation = Rotation.from_quat([orientation[key] for key in 'xyzw']).as_matrix()
            return request, {'position': position, 'rotation_matrix': rotation}
        pose = self.backend.get_end_effector_pose()
        return None, {'position': np.asarray(pose['position'], dtype=float).copy(),
                      'rotation_matrix': np.asarray(pose['rotation_matrix'], dtype=float).copy()}

    def _build_interaction_motion_request(self, operation, interaction_pose):
        p = self.params
        request = deepcopy(p.get('request', {}))
        request.setdefault('target', {}).update(type='mechanism', object_id=self.name)
        if p.get('contact_geom'):
            request['target']['contact_geom'] = p['contact_geom']
        body = self.backend.object_ids[self.name]
        if p.get('contact_normal_local') is not None:
            request['target']['contact_normal'] = (
                self.backend.data.xmat[body].reshape(3, 3) @
                np.asarray(p['contact_normal_local'], dtype=float)).tolist()
        request['manipulation'] = {
            'operation': operation,
            'trajectory': 'linear' if 'distance' in p else 'circular',
            'contact_mode': 'grasp',
        }
        request['grasp'] = deepcopy(p.get('grasp', {'mode': 'binding'}))
        if 'distance' in p:
            frame = p.get('frame', 'tool')
            basis = interaction_pose['rotation_matrix'] if frame == 'tool' else np.eye(3)
            local = np.asarray(p.get('direction', [0., 0., -1.]), dtype=float)
            direction = basis @ local
            direction /= np.linalg.norm(direction)
            request['motion'] = {'path_type': 'linear'}
            request['strategy_params'] = {
                'direction': direction.tolist(), 'distance': float(p['distance'])}
        elif 'angle_deg' in p:
            frame = p.get('frame', 'tool')
            basis = interaction_pose['rotation_matrix'] if frame == 'tool' else np.eye(3)
            offset = basis @ np.asarray(p.get('arc_center_offset', [0., 0., 0.]), dtype=float)
            axis = basis @ np.asarray(p['arc_axis'], dtype=float)
            axis /= np.linalg.norm(axis)
            request['motion'] = {'path_type': 'circular'}
            request['strategy_params'] = {
                'arc_center': (interaction_pose['position'] + offset).tolist(),
                'arc_axis': axis.tolist(),
                'arc_angle': float(np.deg2rad(p['angle_deg'])),
            }
        else:
            request = push_pull_request(self.resources, self.backend, p, operation)
        from demo.interaction.requests import apply_global_motion_timing
        return apply_global_motion_timing(self.resources, request)

    def _plan_interaction_motion(self, operation):
        """Plan from the measured interaction state after approach/contact."""
        self._phase(f'{operation}_planning')
        target_request, interaction_pose = self._interaction_target()
        request = self._build_interaction_motion_request(operation, interaction_pose)
        current = np.asarray(self.backend.get_joint_positions(), dtype=float)
        candidate = current.copy()
        outcome = self.skill.plan(
            request, start_joints=candidate, start_pose=interaction_pose,
            allow_unestablished_contact=True)
        if outcome.get('success'):
            self._interaction_joints = candidate.copy()
            self._record('interaction_planning', {
                'success': True, 'request': request,
                'planning': {'waypoints': int(outcome['prepared'].get('waypoints', 0)),
                             'planned_duration': float(outcome['prepared'].get('planned_duration', 0.0)),
                             'start_joints': candidate.tolist()},
            })
            return outcome['request'], outcome['prepared']
        self._record('interaction_planning', {'success': False, 'request': request,
            'error': {'error_code': 'PATH_CONSTRAINT_INFEASIBLE', 'failed_stage': 'planning',
                      'error_message': 'No collision-free interaction trajectory found',
                      'details': {'candidate_count': 1, 'failures': [outcome.get('error')]}}})
        return None

    def _execute_grasped_motion(self, operation):
        self.details['contact_mode'] = 'grasp'
        if not hasattr(self, 'skill') or not hasattr(self, 'resources'):
            if not self._prepare_grasp(operation):
                return False
            request = push_pull_request(self.resources, self.backend, self.params, operation)
            start, rotation = self._grasp_pose()
            arc = request['strategy_params']
            if request.get('motion', {}).get('path_type', 'circular') == 'linear':
                b = start + np.asarray(arc['direction']) * arc['distance']
            else:
                b = np.asarray(arc['arc_center']) + Rotation.from_rotvec(
                    np.asarray(arc['arc_axis']) * arc['arc_angle']).apply(
                        start - np.asarray(arc['arc_center']))
            self.history = [start.tolist()]
            result = self._low(operation, request, True)
            a, _ = self._grasp_pose()
            self.details.update(a=a.tolist(), b=b.tolist(), history=self.history)
            if not result['success'] and not self._suspended_motion_executed(result):
                self._record_suspended_motion(result)
                return False
            if hasattr(self, 'sync'):
                if not self._post_contact_retreat(request, release_first=bool(result['success']), operation=operation):
                    return False
            if not result['success']:
                self._record_suspended_motion(result)
                return False
            verified = self._verify_goal()
            if verified:
                return True
            self._record_suspended_motion(result)
            return False
        if not self._prepare_contact_approach(operation):
            return False
        planned = self._plan_interaction_motion(operation)
        if planned is None:
            return False
        request, prepared = planned
        if not self._prepare_grasp(operation, prepare_approach=False):
            return False
        self.skill.rebase_prepared_object_state(request, prepared)
        start,rotation=self._grasp_pose()
        arc=request['strategy_params']
        if request.get('motion', {}).get('path_type', 'circular') == 'linear':
            b=start+np.asarray(arc['direction'])*arc['distance']
        else:
            axis=np.asarray(arc['arc_axis']);center=np.asarray(arc['arc_center'])
            b=center+Rotation.from_rotvec(axis*arc['arc_angle']).apply(start-center)
        self.history=[start.tolist()]
        result=self._low_prepared(operation,request,prepared,True)
        a,_=self._grasp_pose()
        self.details.update(a=a.tolist(),b=b.tolist(),history=self.history)
        if not result['success'] and not self._suspended_motion_executed(result):
            self._record_suspended_motion(result)
            return False
        # A completed grasp releases before retreat.  After a suspended
        # physical motion the gripper may already have lost its logical hold,
        # so retrying release can produce OBJECT_NOT_HELD and mask the original
        # collision; the partial motion still needs its geometric retreat.
        if not self._post_contact_retreat(
                request, release_first=bool(result['success']), operation=operation):
            return False
        if not result['success']:
            self._record_suspended_motion(result)
            return False
        verified=self._verify_goal()
        if verified:
            return True
        self._record_suspended_motion(result)
        return False


    def _verify_goal(self):
        # Relative requests are verified by measured TCP displacement/rotation.
        # Object joint state is diagnostic evidence, never a motion goal.
        if 'distance' in self.params or 'angle_deg' in self.params:
            return True
        if 'target_angle_deg' not in self.params:return True
        self._phase('goal_verification')
        body=self.backend.object_ids[self.name];model=self.backend.model
        joint=int(model.body_jntadr[body]);actual=math.degrees(float(self.backend.data.qpos[model.jnt_qposadr[joint]]))
        target=float(self.params['target_angle_deg']);gap=abs(target-actual)
        self.details.update(actual_door_angle_deg=actual,remaining_door_angle_deg=gap)
        if gap>float(self.params.get('angle_tolerance_deg',1.2)):
            return self._fail('DOOR_TARGET_NOT_REACHED',f'Door reached {actual:.3f} degrees; target {target:.3f}',remaining_angle_deg=gap)
        return self._record('goal_verification',{'success':True,'actual_angle_deg':actual})




"""Pull command: prepare a grasp, then execute the requested tool motion."""


class CommandPullExecutor(PullMotionExecutor):
    operation = 'pull'

    def _run(self):
        if self.params.get('contact_mode', 'grasp') != 'grasp':
            return self._fail('INVALID_REQUEST', 'Pull requires contact_mode grasp')
        return self._execute_grasped_motion('pull')





