"""Execute demo task commands through the existing base atomic_action."""
from __future__ import annotations

from copy import deepcopy
import numpy as np
from scipy.spatial.transform import Rotation

from demo.interaction.carrying_safety import payload_safety_distance, split_carrying_safety
from demo.task_commands.converter import SkillCommandConverter, _deep_merge
from demo.interaction.object_library import scene_object_by_id
from atomic_skill.grasp import execute_grasp
from atomic_skill.release import execute_release
from atomic_skill.move import DemoInteractionMove, execute_grasp_pose_move, planning_failed_without_motion
from atomic_skill.approach import PoseSearchExecutor
from atomic_skill.approach import ApproachSkill
from atomic_skill.retreat import RetreatSkill
from atomic_skill.pick import DemoInteractionPick, execute_retreat_candidates, resolve_post_grasp_target
from atomic_skill.put import DemoInteractionPut, put_release_request, validate_put_destination
from atomic_skill.pour import DemoPourExecutor


class TaskSkillExecutor:
    """Own command conversion, interaction planning, and phase execution."""

    def __init__(self, *, resources, backend, kinematics, checker, synchronizer,
                 resolver, execute_motion, report, write_frame, grasper=None, releaser=None):
        self.resources, self.backend = resources, backend
        self.kinematics, self.checker, self.sync = kinematics, checker, synchronizer
        self.execute_motion = execute_motion
        self.resolver = resolver
        if grasper is None:
            from atomic_action.manipulation.grasp.action import GraspAction
            grasper = GraspAction(robot_runtime=backend)
        if releaser is None:
            from atomic_action.manipulation.release.action import ReleaseAction
            releaser = ReleaseAction(robot_runtime=backend)
        self.grasper, self.releaser = grasper, releaser
        self.report, self.write_frame = report, write_frame
        self.stage = 'conversion'
        self.adapters = {}
        if resolver is not None:
            limits = backend.joint_limits
            self.adapters = {
                'move': DemoInteractionMove(resolver, resources, kinematics, checker, limits),
                'pick': DemoInteractionPick(resolver, resources, kinematics, checker, limits),
                'put': DemoInteractionPut(resolver, resources, kinematics, checker, limits),
            }
        self.converter = SkillCommandConverter({
            'scene': str(resources.scene_file), 'objects': {},
            'named_targets': resources.scene.get('named_targets', {}),
            'scene_object_ids': {str(o['object_id_in_scene']) for o in resources.scene['objects']},
        })
        self.pour = DemoPourExecutor(resources=resources, backend=backend,
            kinematics=kinematics, execute_motion=execute_motion,
            synchronizer=synchronizer, checker=checker, report=report)

    def execute(self, command, index, state, row):
        if command['skill_name'] == 'pour':
            self.stage = 'pour'
            self.report['active_substep'] = 'pour'
            try:
                return self.pour.execute(command['parameters'], row)
            except Exception as exc:
                row.update(success=False, status='error', error={
                    'error_code': 'POUR_EXECUTION_FAILED',
                    'failed_stage': self.stage, 'error_message': str(exc)})
                raise
        if command['skill_name'] in {'grasp', 'release'}:
            return self._execute_closure(command, row)
        if command['skill_name'] in {'approach', 'retreat'}:
            return self._execute_pose_search(command, state, row)
        if command['skill_name'] not in {'move', 'pick', 'put'}:
            raise ValueError('TaskSkillExecutor supports move, pick, put, grasp, release, approach, retreat, and pour')
        backend, checker, kin, sync = self.backend, self.checker, self.kinematics, self.sync
        parameters = command['parameters']
        skill = command['skill_name']
        self.stage = 'conversion'
        release_request = None
        if skill == 'put':
            release_request = put_release_request(parameters, backend.gripper_controller)

        # Relative targets are resolved against the measured TCP at this command.
        measured = kin.forward(state['joints'])
        quat = Rotation.from_matrix(measured['rotation_matrix']).as_quat()
        self.converter._last_pose = {'type': 'pose', 'frame': 'world',
            'position': dict(zip('xyz', measured['position'])),
            'orientation': dict(representation='quaternion', **dict(zip('xyzw', quat)))}
        if parameters.get('link'):
            from demo.interaction.requests import motion_request
            request = motion_request(self.resources, backend, parameters)
        else:
            step = self.converter.convert_command(command, index)
            request = _deep_merge(self.resources.scene['move_defaults'], step['request'])

        row['substeps'] = []
        if request['target']['type'] == 'object':
            self.stage = 'target_resolution'
            try:
                if skill == 'put':
                    destination = scene_object_by_id(self.resources.scene, parameters['target'])
                    validate_put_destination(release_request, destination['object_name_in_scene'])
                adapter = self.adapters[skill]
                if skill == 'pick':
                    closure_request = {
                        'grasp': {'mode': 'physical'},
                        'strategy_params': {'hold_force': parameters.get('hold_force', 10.0)},
                    }
                    closure_request, _ = split_carrying_safety(closure_request)
                    parts, resolved = adapter.prepare(request, state, grasp_request=closure_request,
                        retreat_lift_height=0.0)
                else:
                    parts, resolved = adapter.prepare(request, state)
            except Exception as exc:
                row.update(success=False, error={'error_code': 'TARGET_RESOLUTION_FAILED',
                    'failed_stage': self.stage, 'error_message': str(exc)})
                if hasattr(exc, 'details'):
                    row['error'].update(exc.details)
                    self.stage = row['error']['failed_stage']
                raise
            row['target_resolution'] = resolved
        else:
            parts = [{'phase': 'move', 'request': request}]

        samples = []
        row['actual_path_collision_free'] = True

        def record(frame_state):
            samples.append(frame_state['joints'])
            self.write_frame(command['command_id'], self.report.get('active_substep', part['phase']), frame_state)
            sync.sync(backend, kin, checker)
            observed = checker.check(frame_state['joints'])
            if observed.in_collision and self.report.get('active_substep') != 'grasp':
                raise RuntimeError(f'Execution frame collision: {observed.contacts}')

        backend.set_frame_callback(record)
        self.stage = 'planning_and_execution'
        for part in parts:
            self.report['active_substep'] = part['phase']
            sync.sync(backend, kin, checker)
            if skill == 'pick' and part['phase'] in {'lift', 'retreat'}:
                resolve_post_grasp_target(part, backend.get_end_effector_pose(), resolved)
            backend.set_status(f"{command['command_id']} | Planning / {part['phase']}")
            samples.clear()
            before = backend.physics_steps
            previous_grasp_goal = getattr(backend, 'grasp_approach_goal', None)
            if skill == 'pick' and part['phase'] == 'contact':
                selected_control = np.asarray(resolved['selected']['world_from_control'], dtype=float)
                selected_quat = Rotation.from_matrix(selected_control[:3, :3]).as_quat()
                backend.grasp_approach_goal = (tuple(selected_control[:3, 3]), tuple(selected_quat),
                                               tuple(resolved['joint_positions']))
                backend.grasp_motion_phase = True
            if skill == 'pick' and part['phase'] == 'retreat':
                result = execute_retreat_candidates(part, backend.get_end_effector_pose(),
                                                    resolved, self.execute_motion)
            elif skill == 'pick' and part.get('grasp_pose_fallback'):
                part['request'],result = execute_grasp_pose_move(part['request'],self.execute_motion,backend)
            else:
                result = self.execute_motion(part['request'])
            if (skill == 'pick' and part['phase'] in {'approach','contact'} and
                    part.get('direct_pick_fallback') and
                    planning_failed_without_motion(result,backend,before)):
                row['substeps'].append({'phase': part['phase'], 'request': deepcopy(part['request']),
                                       **result, 'recovered_by': 'direct_joint_fallback'})
                fallback = deepcopy(part['direct_pick_fallback'])
                resolved['approach']['runtime_fallback'] = {
                    'reason': deepcopy(result.get('error')), 'path_type': 'joint'}
                part['request'] = fallback
                self.report['active_substep'] = 'contact_joint_fallback'
                backend.set_status(f"{command['command_id']} | Planning / contact_joint_fallback")
                result = self.execute_motion(part['request'])
            elif (skill == 'pick' and part['phase'] in {'approach','contact'} and
                    not part.get('grasp_pose_fallback') and not part.get('direct_pick_fallback') and
                    planning_failed_without_motion(result,backend,before)):
                row['substeps'].append({'phase':part['phase'],'request':deepcopy(part['request']),
                                       **result,'recovered_by':'contact_avoidance_attempt'})
                fallback = {'reason':deepcopy(result.get('error')),'failed_phase':part['phase']}
                resolved['approach']['runtime_fallback'] = fallback
                if part['phase'] == 'approach':
                    contact_part=next(p for p in parts if p['phase']=='contact')
                    contact_part['grasp_pose_fallback']=fallback
                    backend.grasp_approach_goal = previous_grasp_goal
                    continue
                part['grasp_pose_fallback']=fallback
                self.report['active_substep']='contact_avoidance'
                backend.set_status(f"{command['command_id']} | Planning / contact_avoidance")
                part['request'],result=execute_grasp_pose_move(part['request'],self.execute_motion,backend)
            backend.grasp_approach_goal = previous_grasp_goal
            result['payload_safety_distance_m'] = payload_safety_distance(
                part['request'].get('constraints', {}).get('safety_distance', 0.),
                getattr(backend.gripper_controller, 'held_name', None), checker.carrying_safety_distance)
            execution_result = result.get('execution_result')
            if isinstance(execution_result, dict):
                execution_result.pop('frames', None)
            row['substeps'].append({'phase': part['phase'], 'request': part['request'], **result,
                                   **({'grasp_pose_fallback':part['grasp_pose_fallback']} if part.get('grasp_pose_fallback') else {})})
            row.update(result)
            if not result.get('success'):
                break

            controller = getattr(backend, 'gripper_controller', None)
            if controller and controller.mode == 'physical' and controller.held_name:
                row['actual_path_verification'] = 'live_physics_frames'
            else:
                actual_check = checker.check_path(samples, resolution=0.015)
                row['actual_path_collision_free'] &= not actual_check.in_collision
                if actual_check.in_collision:
                    raise AssertionError(f'Executed trajectory collides: {actual_check.contacts}')

            if row.get('target_resolution') and part['phase'] in {'move', 'contact', 'lift', 'retreat'}:
                self.stage = 'interaction_position_verification'
                grasp = row['target_resolution']
                actual_pose = backend.get_end_effector_pose()
                actual = np.eye(4)
                actual[:3, :3], actual[:3, 3] = actual_pose['rotation_matrix'], actual_pose['position']
                if part['phase'] in {'lift', 'retreat'}:
                    desired = np.asarray(grasp.get(part['phase'], grasp['approach'])['world_from_control'])
                    prefix = part['phase']
                else:
                    actual = actual @ np.asarray(grasp['control_from_grasp'])
                    desired = np.asarray(grasp['selected']['world_from_grasp'])
                    prefix = 'interaction'
                position_error = float(np.linalg.norm(actual[:3, 3] - desired[:3, 3]))
                orientation_error = float(Rotation.from_matrix(desired[:3, :3].T @ actual[:3, :3]).magnitude())
                row[f'{prefix}_position_error_m'] = position_error
                row[f'{prefix}_orientation_error_rad'] = orientation_error
                row['substeps'][-1].update(position_error_m=position_error,
                                           orientation_error_rad=orientation_error)
                if position_error > .002 or orientation_error > .02:
                    row.update(success=False, error={'error_code': 'POSE_VERIFICATION_FAILED',
                        'failed_stage': self.stage, 'error_message': f'{prefix} pose outside tolerance'})
                    raise RuntimeError(f'{prefix} pose outside tolerance')
                self.stage = 'planning_and_execution'

            if skill == 'pick' and part['phase'] == 'contact':
                # Keep target collisions enabled through execution and path
                # verification; only the subsequent closure phase restores
                # the intentional gripper-target contact exception.
                backend.grasp_motion_phase = False
                self.stage = 'grasp'
                self.report['active_substep'] = 'grasp'
                backend.set_status(f"{command['command_id']} | Closing and confirming grasp")
                grasp_request = {
                    'target': {'type': 'object', 'object_id': resolved['target_name']},
                    'grasp': {'mode': 'physical'},
                    'strategy_params': {'hold_force': parameters.get('hold_force', 10.0)},
                }
                if grasp_request['target'].get('object_id') != resolved['target_name']:
                    raise ValueError('pick grasp target must match its motion target')
                grasp_request, carrying_distance = split_carrying_safety(grasp_request)
                from demo.interaction.closure_validation import execute_checked_grasp
                previous_grasp_goal = getattr(backend, 'grasp_approach_goal', None)
                selected_control = np.asarray(resolved['selected']['world_from_control'], dtype=float)
                selected_quat = Rotation.from_matrix(selected_control[:3, :3]).as_quat()
                backend.grasp_approach_goal = (tuple(selected_control[:3, 3]), tuple(selected_quat),
                                               tuple(resolved['joint_positions']))
                backend.grasp_motion_phase = False
                try:
                    grasp_result = execute_checked_grasp(self.grasper, grasp_request, backend, checker)
                finally:
                    backend.grasp_approach_goal = previous_grasp_goal
                row['grasp_result'] = grasp_result
                if not grasp_result['success']:
                    row.update(success=False, error=grasp_result['error'])
                    self.stage = grasp_result['error'].get('failed_stage', 'grasp')
                    raise RuntimeError(f"Grasp failed: {grasp_result['error']}")
                checker.carrying_safety_distance = carrying_distance
                checker.capture_support()
                sync.sync(backend, kin, checker)
                self.stage = 'planning_and_execution'
            if skill == 'pick' and part['phase'] in {'lift', 'retreat'}:
                checker.support_geoms.clear()
            if skill == 'put' and part['phase'] == 'contact':
                self.stage = 'release'
                self.report['active_substep'] = 'release'
                backend.set_status(f"{command['command_id']} | Releasing held object")
                if backend.gripper_controller.held_name != release_request['target']['object_id']:
                    raise ValueError('put payload was lost before release')
                release_result = self.releaser.execute(release_request)
                row['release_result'] = release_result
                checker.support_geoms.clear()
                if backend.gripper_controller.held_name is None:
                    checker.carrying_safety_distance = 0.
                sync.sync(backend, kin, checker)
                if not release_result['success']:
                    row.update(success=False, error=release_result['error'])
                    raise RuntimeError(f"Release failed: {release_result['error']}")
                self.stage = 'planning_and_execution'

        if result.get('success'):
            sync.sync(backend, kin, checker)
            goal_q = np.asarray(result['trajectory']['waypoints'][-1])
            expected_pose = kin.forward(goal_q)
            actual_pose = backend.get_end_effector_pose()
            row['tcp_error_m'] = float(np.linalg.norm(expected_pose['position'] - actual_pose['position']))
            execution_result = row.get('execution_result')
            if isinstance(execution_result, dict):
                execution_result.pop('frames', None)
            grasp_errors = [s['execution_result']['grasp_error'] for s in row['substeps']
                            if (s.get('execution_result') or {}).get('grasp_error')]
            if grasp_errors:
                row.update(success=False, status='failed', motion_completed=True,
                           error=grasp_errors[0])
        return row

    def _execute_pose_search(self, command, state, row):
        """Resolve approach targets; retreat uses the live end-effector pose."""
        parameters = deepcopy(command['parameters'])
        resolved = None
        if command['skill_name'] in {'approach', 'retreat'}:
            if self.resolver is None:
                raise ValueError(f"{command['skill_name']} requires an object-library resolver")
            target = parameters.pop('target', None)
            self.stage = 'target_resolution'
            resolved = self.resolver.resolve(target, self.resources, state)
            selected = self.resolver.select(
                resolved, self.kinematics, self.checker, state['joints'],
                safety_distance=float(self.resources.scene.get('move_defaults', {})
                                      .get('constraints', {}).get('safety_distance', 0.003)),
                prefer_direct=False)
            pose = np.asarray(selected['selected']['world_from_control'], dtype=float)
            control_from_grasp = np.asarray(resolved['control_from_grasp'], dtype=float)
            parameters['grasp_offset_local'] = control_from_grasp[:3, 3].tolist()
            if command['skill_name'] == 'approach':
                parameters['target_pose'] = {
                    'position': pose[:3, 3].tolist(),
                    'quaternion_xyzw': Rotation.from_matrix(pose[:3, :3]).as_quat().tolist(),
                    'frame': 'world',
                }
        executor = PoseSearchExecutor(
            resources=self.resources, backend=self.backend,
            synchronizer=self.sync, kinematics=self.kinematics,
            checker=self.checker, execute_motion=self.execute_motion)
        self.stage = command['skill_name']
        result = executor.run(command['skill_name'], parameters)
        row.update(result, **({'target_resolution': resolved} if resolved is not None else {}))
        return row

    def _execute_closure(self, command, row):
        """Execute direct grasp/release commands after resolving scene names."""
        skill = command['skill_name']
        parameters = command['parameters']
        self.stage = skill
        self.report['active_substep'] = skill
        self.backend.set_frame_callback(None)
        if skill == 'grasp':
            result, carrying_distance = execute_grasp(
                self.resources, parameters, self.grasper, self.backend, self.checker)
        else:
            result = execute_release(self.resources, parameters, self.releaser)
        row.update(result)
        if result.get('success') and skill == 'release':
            # Let released dynamic objects settle in the live physics state
            # before the next command plans against them.  The release skill
            # opens the fingers but does not advance MuJoCo while realtime is
            # disabled, so an immediately following motion could see the
            # falling payload at the gripper and report a false path collision.
            settle = parameters.get('strategy_params', {}).get(
                'settle_time', parameters.get('settle_time', 0.0))
            try:
                settle = float(settle)
            except (TypeError, ValueError):
                settle = 0.0
            if settle > 0.0:
                self.backend.advance_gripper(settle)
                row['release_settle_time_s'] = settle
        if result.get('success') and skill == 'grasp':
            self.checker.carrying_safety_distance = carrying_distance
            self.checker.capture_support()
        if skill == 'release' and hasattr(self.checker, 'support_geoms'):
            self.checker.support_geoms.clear()
            if self.backend.gripper_controller.held_name is None:
                self.checker.carrying_safety_distance = 0.
        self.sync.sync(self.backend, self.kinematics, self.checker)
        if not result.get('success'):
            self.stage = result.get('error', {}).get('failed_stage', skill)
        return row



