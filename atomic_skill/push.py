"""Push task skill with local contact preparation, execution, and reporting."""
from copy import deepcopy
import math
import mujoco
import numpy as np
from scipy.spatial.transform import Rotation
from demo.task_commands.converter import _deep_merge
from demo.interaction.requests import body_name, motion_request, push_pull_request
from demo.interaction.closure_validation import execute_checked_grasp
from atomic_skill.release import release_request
from atomic_skill._move_common import execute_grasp_pose_move, planning_failed_without_motion


class PushMotionExecutor:
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


    def _retreat(self, name, direction=None):
        # Keep the legacy fallback entry point, but route it through the same
        # RetreatSkill used by the normal push chain.  ``direction`` is the
        # desired world displacement of the endpoint; RetreatSkill receives
        # the equivalent local axis and reverse-derives that endpoint.
        from atomic_skill.retreat import RetreatSkill
        pos, rot = self._grasp_pose()
        direction = rot[:, 2] if direction is None else np.asarray(direction, dtype=float)
        norm = np.linalg.norm(direction)
        if norm <= 1e-9:
            return self._fail('RETREAT_FAILED', 'Retreat direction is undefined')
        target_pose = {
            'position': pos.tolist(),
            'quaternion_xyzw': Rotation.from_matrix(rot).as_quat().tolist(),
            'frame': 'world',
        }
        retreat_params = {
            'target_pose': target_pose,
            'search': deepcopy(self.params.get('retreat_search') or {}),
            'pose_axis': (rot.T @ (-direction / norm)).tolist(),
        }
        return RetreatSkill(self).execute(retreat_params)

    def _post_contact_retreat(self, request, *, release_first=False, operation=None):
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
        mode = 'push_contact' if p.get('contact_mode') == 'contact' else 'grasp'
        request['manipulation'] = {
            'operation': operation,
            'trajectory': 'linear' if ('distance' in p or 'goal_position_world' in p) else 'circular',
            'contact_mode': mode,
        }
        if mode == 'grasp':
            request['grasp'] = deepcopy(p.get('grasp', {'mode': 'binding'}))
        if 'distance' in p or 'goal_position_world' in p:
            frame = p.get('frame', 'tool')
            if 'goal_position_world' in p:
                delta = np.asarray(p['goal_position_world'], dtype=float) - interaction_pose['position']
                distance = float(np.linalg.norm(delta))
                if distance < 1e-9:
                    raise ValueError('Push goal is already reached without contact')
                direction = delta / distance
            else:
                basis = interaction_pose['rotation_matrix'] if frame == 'tool' else np.eye(3)
                local = np.asarray(p.get('direction', [0., 0., 1.]), dtype=float)
                direction = basis @ local
                distance = float(p['distance'])
            direction /= np.linalg.norm(direction)
            request['motion'] = {'path_type': 'linear'}
            request['strategy_params'] = {
                'direction': direction.tolist(), 'distance': distance}
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
        if mode == 'push_contact':
            request['manipulation']['maintain_contact'] = True
            request.setdefault('constraints', {})['stop_on_contact_loss'] = True
            request['constraints'].setdefault('contact_threshold', .1)
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




"""Push command: grasped relative motion or an existing contact-push request."""
from copy import deepcopy
import mujoco
import numpy as np
from scipy.spatial.transform import Rotation
from demo.task_commands.converter import _deep_merge


def aim_rotation(rotation, direction):
    """Align grasp Z with travel; choose the smallest rotation from current roll."""
    direction = np.asarray(direction, dtype=float)
    if np.linalg.norm(direction) < 1e-8:
        raise ValueError("Push direction cannot be zero")
    direction /= np.linalg.norm(direction)
    current = Rotation.from_matrix(rotation)
    aligned, _ = Rotation.align_vectors([direction], [current.apply([0, 0, 1])])
    return (aligned * current).as_matrix()


def historical_candidates(history, a, b, rotation, *, spacing=.01, max_candidates=30,
                          min_push_distance=.005):
    """Build c candidates, nearest to the measured current point a first.

    A candidate contains only the contact-point position, the tool rotation
    that aims its local Z axis from c to b, and that unit push direction.  The
    object or mechanism is deliberately absent from this geometric step.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != (3,) or b.shape != (3,) or not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        raise ValueError('a and b must be finite world xyz points')
    candidates = []
    valid = []
    for point in history:
        position = np.asarray(point, dtype=float)
        if position.shape == (3,) and np.all(np.isfinite(position)):
            valid.append(position)
    for position in sorted(valid, key=lambda p: np.linalg.norm(p-a)):
        if np.linalg.norm(position-b) < min_push_distance or any(np.linalg.norm(position-c[0]) < spacing for c in candidates):
            continue
        direction = (b-position)/np.linalg.norm(b-position)
        candidates.append((position, aim_rotation(rotation, direction), direction))
        if len(candidates) >= max_candidates:
            break
    return candidates


def reverse_ray_candidates(a, b, rotation, *, spacing=.01, max_candidates=30,
                           min_push_distance=.005):
    """Build legacy fallback points behind ``a`` on the line opposite ``a -> b``.

    Candidate positions are sampled from the closest point behind ``a``
    outwards.  The supplied rotation is kept unchanged for every candidate;
    it is an arbitrary c-pose orientation, subject to the caller's IK and
    collision checks.  The caller uses the returned direction for the later
    straight c-to-b motion.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != (3,) or b.shape != (3,) or not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        raise ValueError('a and b must be finite world xyz points')
    delta = b - a
    length = float(np.linalg.norm(delta))
    if length < 1e-8:
        raise ValueError('a and b must be distinct points')
    if spacing <= 0 or max_candidates <= 0:
        raise ValueError('spacing and max_candidates must be positive')
    direction = delta / length
    tool_rotation = np.asarray(rotation, dtype=float)
    if tool_rotation.shape != (3, 3) or not np.all(np.isfinite(tool_rotation)):
        raise ValueError('rotation must be a finite 3x3 matrix')
    candidates = []
    for index in range(1, int(max_candidates) + 1):
        c = a - direction * float(spacing) * index
        if np.linalg.norm(c - b) < min_push_distance:
            continue
        candidates.append((c, tool_rotation.copy(), direction.copy()))
    return candidates


def local_y_candidates(a, b, rotation, *, min_offset=.03, max_offset=.07,
                       spacing=.008, max_candidates=30, min_push_distance=.005):
    """Build c candidates along the current end-effector local negative y axis.

    The frame is used only for this fallback construction.  Every candidate
    uses the same orientation ``R_current @ Ry(45°) @ Rz(90°)``.  The first
    rotation is the whole end-effector rotation around the grasped shaft
    (the interaction-frame y axis).  The second is the J6 rotation; in the
    loaded UR5e/grasp-center calibration, J6 is the interaction-frame z axis.
    The returned direction is the world direction from that candidate
    position to ``b``.
    Candidates are ordered from 3 cm outward toward 7 cm by default.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    base_rotation = np.asarray(rotation, dtype=float)
    if (a.shape != (3,) or b.shape != (3,) or
            not np.all(np.isfinite(a)) or not np.all(np.isfinite(b))):
        raise ValueError('a and b must be finite world xyz points')
    if (base_rotation.shape != (3, 3) or
            not np.all(np.isfinite(base_rotation))):
        raise ValueError('rotation must be a finite 3x3 matrix')
    if min_offset <= 0 or max_offset < min_offset or spacing <= 0 or max_candidates <= 0:
        raise ValueError('offsets, spacing, and max_candidates must be positive')
    y_axis = base_rotation[:, 1]
    y_norm = float(np.linalg.norm(y_axis))
    if y_norm < 1e-8:
        raise ValueError('rotation y axis must be nonzero')
    y_axis = y_axis / y_norm
    # Apply the rotations in the physical order requested by the task:
    # first rotate the whole end-effector around the grasped shaft, then
    # rotate the gripper through its J6 joint. Both are local rotations, so
    # they are post-multiplied by the current world-from-tool orientation.
    shaft_rotation = Rotation.from_euler('y', 45.0, degrees=True).as_matrix()
    j6_rotation = Rotation.from_euler('z', 90.0, degrees=True).as_matrix()
    local_rotation = shaft_rotation @ j6_rotation
    candidate_rotation = base_rotation @ local_rotation
    candidates = []
    offsets = np.arange(float(min_offset), float(max_offset) + spacing * .5,
                        float(spacing))
    for offset in offsets[:int(max_candidates)]:
        c = a - y_axis * float(offset)
        delta = b - c
        distance = float(np.linalg.norm(delta))
        if distance < max(float(min_push_distance), 1e-8):
            continue
        candidates.append((c, candidate_rotation.copy(), delta / distance))
    return candidates


class CommandPushExecutor(PushMotionExecutor):
    operation = 'push'

    def _run(self):
        mode = self.params.get('contact_mode', 'grasp')
        if mode not in ('grasp', 'contact'):
            return self._fail('INVALID_REQUEST', 'contact_mode must be grasp or contact')
        if mode == 'contact':
            return self._execute_contact_push()
        if any(key in self.params for key in ('distance', 'angle_deg')):
            return self._execute_grasped_motion('push')
        return self._push_to_world_goal()

    def _execute_contact_push(self):
        """Advance the requested stroke, establishing contact during execution."""
        from demo.interaction.requests import relative_tool_motion
        p = self.params
        if p.get('grasp') or p.get('grasp_request') or p.get('request', {}).get('grasp'):
            return self._fail('INVALID_REQUEST', 'contact mode cannot include grasp configuration')
        gripper = self.backend.gripper_controller
        if getattr(gripper, 'held_name', None) or gripper.get_state(self.name).get('holding'):
            return self._fail('INVALID_REQUEST', 'Release the held object before contact push')
        if 'distance' not in p:
            return self._fail('INVALID_REQUEST', 'contact push requires a relative distance')
        relative_tool_motion(self.backend, p, 'push')
        self.details['contact_mode'] = 'contact'
        if not self._prepare_contact_approach('push'):
            return False
        planned = self._plan_interaction_motion('push')
        if planned is None:
            return False
        request, prepared = planned
        start, _ = self._grasp_pose()
        grasp_offset = start-np.asarray(self.backend.get_end_effector_pose()['position'])
        motion = request['strategy_params']
        self.details.update(precontact_start=start.tolist(), a=None, b=None,
            distance_reference='first_contact', contact_acquisition='during_continuous_push')
        result = self._low_prepared('push', request, prepared, True)
        partial_motion = self._suspended_motion_executed(result)
        if result.get('success') or partial_motion:
            retreat_ok = self._post_contact_retreat(request, release_first=False)
            if not retreat_ok:
                if result.get('success'):
                    return self._fail('RETREAT_FAILED', 'Unable to clear the pushed mechanism safely')
                self._record_suspended_motion(result)
                return False
            capture = getattr(getattr(self, 'checker', None), 'capture_detach_contacts', None)
            if callable(capture):
                self.details['detach_contact_pairs'] = int(capture(self.name))
        measured = result.get('pull_push_result') or {}
        self.details.update(precontact_distance_m=measured.get('precontact_distance'),
                            total_travel_distance_m=measured.get('total_travel_distance'))
        if measured.get('contact_tcp_position') is not None:
            contact = np.asarray(measured['contact_tcp_position'])+grasp_offset
            self.details.update(a=contact.tolist(),
                b=(contact+np.asarray(motion['direction'])*motion['distance']).tolist())
        if not result['success']:
            self._record_suspended_motion(result)
        return bool(result['success'])

    def _push_to_world_goal(self):
        p=self.params
        if p.get('grasp'):
            mode = p['grasp'].get('mode', 'binding')
            if mode not in {'binding', 'physical'}:
                return self._fail('INVALID_REQUEST', 'grasp.mode must be binding or physical')
            if not hasattr(self, 'skill') or not hasattr(self, 'resources'):
                req = _deep_merge({'target': {'type':'object','object_id':self.name}, 'grasp':p['grasp']}, p.get('grasp_request', {}))
                if req['target']['object_id'] != self.name or req['grasp']['mode'] != mode:
                    return self._fail('INVALID_REQUEST', 'Push grasp target or mode mismatch')
                if not self._prepare_grasp('push'):
                    return False
                pos, rot = self._grasp_pose()
                target = np.asarray(p['goal_position_world'], dtype=float)
                direction = target - pos
                if np.linalg.norm(direction) < .001:
                    return self._fail('INVALID_REQUEST', 'Push goal is already reached without contact')
                req.update(target={'type':'mechanism','object_id':self.name}, grasp=p['grasp'],
                           manipulation={'operation':'push','trajectory':'linear','contact_mode':'grasp'},
                           motion={'path_type':'linear'},
                           strategy_params={'direction':(direction / np.linalg.norm(direction)).tolist(),
                                            'distance':float(np.linalg.norm(direction))})
                result = self._low('straight_push', req)
                return bool(result['success'])
            if not self._prepare_contact_approach('push'):
                return False
            planned = self._plan_interaction_motion('push')
            if planned is None:
                return False
            request, prepared = planned
            if not self._prepare_grasp('push', prepare_approach=False):
                return False
            self.skill.rebase_prepared_object_state(request, prepared)
            start, rotation = self._grasp_pose()
            self.history = [start.tolist()]
            result = self._low_prepared('straight_push', request, prepared, True)
            if not result['success'] and not self._suspended_motion_executed(result):
                self._record_suspended_motion(result)
                return False
            if not self._post_contact_retreat(request, release_first=bool(result['success']), operation='push'):
                return False
            if not result['success']:
                self._record_suspended_motion(result)
                return False
            return self._verify_goal()
        pos,rot=self._grasp_pose()
        target=np.asarray(p['goal_position_world'],dtype=float)
        direction=target-pos
        if np.linalg.norm(direction)<.001:return self._fail('INVALID_REQUEST','Push goal is already reached without contact')
        if p.get('grasp'):
            req = deepcopy(p.get('request', {}))
            req.update(target={'type':'mechanism','object_id':self.name}, grasp=p['grasp'],
                       manipulation={'operation':'push','trajectory':'linear','contact_mode':'grasp'},
                       motion={'path_type':'linear'},
                       strategy_params={'direction':(direction/np.linalg.norm(direction)).tolist(),
                                        'distance':float(np.linalg.norm(direction))})
            result = self._low('straight_push', req)
            if not result['success']:
                self._record_suspended_motion(result)
            return bool(result['success'])
        return self._push_line(target,direction/np.linalg.norm(direction),p)


    def _push_line(self,b,direction,cfg,selected_candidate=None,ignore_path_collision=False,
                   retreat_after_push=True):
        """Approach, confirm contact, push along a line, and verify.

        Ordinary push commands may request a post-push retreat.  The c-point
        fallback ends at b, so it disables that extra retreat.

        ``ignore_path_collision`` is reserved for the local-y c fallback:
        the c pose is checked before the push, while later line collisions do
        not stop the requested c-to-b motion.

        ``b`` is the requested contact point.  The actual point after contact
        search is recorded as c_actual and is used to compute the remaining
        push distance, so the physical push never relies on the nominal c.
        """
        # Small, collision-checked straight approach steps until measured pad contact.
        from control.pull_push_controller import MujocoPullPushController
        controller=MujocoPullPushController(self.backend,kinematics=self.kinematics)
        body=self.backend.object_ids[self.name];bodies=controller._descendants(body)
        geom=None
        if cfg.get('contact_geom'):
            geom=mujoco.mj_name2id(self.backend.model,mujoco.mjtObj.mjOBJ_GEOM,cfg['contact_geom'])
            if geom<0 or int(self.backend.model.geom_bodyid[geom]) not in bodies:return self._fail('INVALID_REQUEST','Invalid contact geom')
        step=float(cfg.get('contact_step',.003));threshold=float(cfg.get('contact_threshold',.1))
        if step<=0 or threshold<0:return self._fail('INVALID_REQUEST','Invalid contact step or threshold')
        _,rotation=self._grasp_pose()
        contact_steps=0
        for _ in range(int(cfg.get('max_contact_steps',150))):
            contact_steps += 1
            force,pairs=controller._contacts(self.backend.data,bodies,geom)
            if pairs and force>=threshold:break
            pos,_=self._grasp_pose();remaining=float((b-pos)@direction)
            if remaining<=.001:return self._fail('CONTACT_NOT_ESTABLISHED','Reached b without measured pad contact')
            if not self._move('push_contact',pos+direction*min(step,remaining),rotation,linear=True,contact=True,override=cfg.get('contact_request')):return False
        else:return self._fail('CONTACT_NOT_ESTABLISHED','Contact search budget exhausted')
        pos,_=self._grasp_pose()
        contact_details={
            'success':True,
            'contact_position':pos.tolist(),
            'contact_force_n':float(force),
            'contact_pairs': [sorted(int(v) for v in pair) for pair in pairs],
            'search_steps':contact_steps,
        }
        self._phase('contact_confirmation');self._record('contact_confirmation',contact_details)
        distance=float((b-pos)@direction)
        self.details.update(
            c_actual=pos.tolist(),
            push_distance=float(max(0.0,distance)),
            contact_force_n=float(force),
            contact_search_steps=contact_steps,
        )
        if selected_candidate is not None:
            self.details['c_nominal'] = np.asarray(selected_candidate, dtype=float).tolist()
        if distance<=.001:return self._fail('NO_PUSH_TRAVEL','Contact established at or beyond b')
        req=deepcopy(cfg.get('request',{}))
        req['target']={'type':'mechanism','object_id':self.name}
        if cfg.get('contact_geom'):req['target']['contact_geom']=cfg['contact_geom']
        req['manipulation']={'operation':'push','trajectory':'linear','contact_mode':'push_contact'}
        req['motion']={'path_type':'linear'}
        req['strategy_params']={'direction':direction.tolist(),'distance':distance}
        req.setdefault('constraints',{}).setdefault('contact_threshold',threshold)
        result=self._low('straight_push',req)
        if result.get('success'):
            # The next command may start while the open gripper still touches
            # the drawer. Let that one move detach the measured contact.
            capture = getattr(getattr(self, 'checker', None), 'capture_detach_contacts', None)
            if callable(capture):
                self.details['detach_contact_pairs'] = int(capture(self.name))
        if not result['success']:
            self._record_suspended_motion(result)
        if not retreat_after_push:
            verified = bool(result['success'] and self._verify_goal())
            if not verified:
                self._record_suspended_motion(result)
            return verified
        # A safe reverse retreat is attempted after an ordinary incomplete move.
        error=(result.get('error') or {}).get('error_code')
        if error in {'COLLISION_DETECTED','FORCE_LIMIT','PHYSICS_UNSTABLE'}:return False
        retreat=self._retreat('push_retreat',-direction)
        verified=self._verify_goal()
        if not retreat or not verified:
            self._record_suspended_motion(result)
        return bool(result['success'] and retreat and verified)


    def _fallback(self,a,b):
        self._phase('fallback_selection')
        cfg=self.params['fallback_push'];_,approach_rotation=self._grasp_pose()
        # c is sampled from the current grasp frame's negative local-y axis.
        # This frame convention is used only to construct fallback poses;
        # the low-level motion skill remains independent of the mechanism.
        # Confirmation below still requires a measured MuJoCo contact and
        # force threshold.
        set_demo_ignore=getattr(self.checker,'set_demo_ignore_collisions',None)
        if self._demo_ignore_fallback_collisions and callable(set_demo_ignore):
            set_demo_ignore(True)
        self.backend.contact_target=self.name
        self.backend.contact_target_object_id = self.params.get('target')
        self.sync.sync(self.backend,self.kinematics,self.checker)
        attempts=[]
        self.details.update(
            a=np.asarray(a,dtype=float).tolist(),
            b=np.asarray(b,dtype=float).tolist(),
            fallback_mode='local_negative_y_offset',
            c_orientation_rule='R_current @ Ry(45deg) @ R_J6(90deg); J6=local z',
            candidate_attempts=attempts,
        )
        candidates=local_y_candidates(
            a, b, approach_rotation,
            min_offset=float(cfg.get('candidate_min_offset',.03)),
            max_offset=float(cfg.get('candidate_max_offset',.07)),
            spacing=float(cfg.get('candidate_spacing',.008)),
            max_candidates=int(cfg.get('max_candidates',30)),
            min_push_distance=float(cfg.get('min_push_distance',.005)),
        )
        self.details['candidate_count'] = len(candidates)
        approach_override = deepcopy(cfg.get('approach_request') or {})
        # Moving to c is a free transit move after release.  Let the move
        # layer try a direct joint path and switch to collision-free planning
        # when that path is blocked.
        approach_override.setdefault('planning', {})['mode'] = 'collision_free'
        approach_override['planning']['allow_replan'] = True
        approach_override.setdefault('constraints', {}).setdefault('safety_distance', 0.0)
        for rank,(c,rotation,direction) in enumerate(candidates,1):
            c_offset=float(np.linalg.norm(c-np.asarray(a)))
            candidate = {
                'rank': rank,
                'c': c.tolist(),
                'c_offset_m': float(c_offset),
                'distance_to_a': float(np.linalg.norm(c-np.asarray(a))),
                'distance_to_b': float(np.linalg.norm(c-np.asarray(b))),
                'offset_from_a': float(c_offset),
                'push_direction': direction.tolist(),
                'c_rotation_matrix': np.asarray(rotation, dtype=float).tolist(),
                'shaft_rotation_deg': 45.0,
                'j6_rotation_deg': 90.0,
            }
            req=self._control_request(c,rotation,override=cfg.get('approach_request'))
            try:
                q=np.asarray(self.kinematics.solve_one(req['target'],seed=self.backend.get_joint_positions(),backend='auto',timeout=.3).solution)
                if q.shape!=self.backend.get_joint_positions().shape or not np.all(np.isfinite(q)):raise ValueError('No IK')
                if np.any(q<self.backend.joint_limits[:,0]) or np.any(q>self.backend.joint_limits[:,1]):raise ValueError('joint_limit')
                # The fallback rule requires the reached c pose to be free of
                # actual forbidden contact.  It does not add the ordinary
                # 3 mm planning margin used by the main move flow.
                if not self._demo_ignore_fallback_collisions:
                    hit=self.checker.check(q,safety_distance=float(cfg.get('safety_distance',0.0)))
                    if hit.in_collision:
                        candidate.update(rejected='collision', contacts=hit.contacts)
                        attempts.append(candidate);continue
            except Exception as exc:
                candidate.update(rejected=str(exc))
                attempts.append(candidate);continue
            before=self.backend.physics_steps
            self._demo_set_robot_collisions(True)
            approach_ok = False
            try:
                approach_ok = self._move('move_c',c,rotation,contact=False,override=approach_override)
            finally:
                self._demo_set_robot_collisions(False, restore_pads=approach_ok)
            if not approach_ok:
                candidate.update(rejected=self.phases[-1].get('error'))
                attempts.append(candidate)
                if self.backend.physics_steps!=before:return False
                continue
            candidate.update(selected=True)
            attempts.append(candidate)
            self.details.update(c=c.tolist(),push_direction=direction.tolist(),selected_candidate=rank)
            return self._push_line(
                b, direction, cfg, selected_candidate=c,
                ignore_path_collision=True, retreat_after_push=False,
            )
        return self._fail(
            'NO_SAFE_FALLBACK_POINT',
            'No local-negative-y c point has a reachable collision-free pose.',
            candidate_count=len(attempts),
        )






