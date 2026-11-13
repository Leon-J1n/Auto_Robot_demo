"""Execute demo task commands through the existing base atomic_action."""
from __future__ import annotations

from copy import deepcopy
import time
import numpy as np
from scipy.spatial.transform import Rotation

from demo.interaction.carrying_safety import payload_safety_distance, split_carrying_safety
from demo.task_commands.converter import SkillCommandConverter, _deep_merge
from demo.interaction.object_library import scene_object_by_id
from atomic_skill.grasp import execute_grasp
from atomic_skill.release import execute_release
from atomic_action.motion.common.interaction import DemoInteractionMove, execute_grasp_pose_move, planning_failed_without_motion, pose_request
from atomic_skill.move_to import MoveToSkill
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
        self._interaction_history = {}
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
                'move_to': MoveToSkill(resolver, resources, kinematics, checker, limits),
                'pick': DemoInteractionPick(resolver, resources, kinematics, checker, limits),
                'put': DemoInteractionPut(resolver, resources, kinematics, checker, limits),
            }
        robot_transform = next(c['properties'] for c in resources.robot_object['components']
                               if c['component_type'] == 'Transform')
        base_rotation = Rotation.from_quat(robot_transform.get('quaternion_xyzw', [0, 0, 0, 1])).as_matrix()
        self.converter = SkillCommandConverter({
            'scene': str(resources.scene_file), 'objects': {},
            'named_targets': resources.scene.get('named_targets', {}),
            'scene_object_ids': {str(o['object_id_in_scene']) for o in resources.scene['objects']},
        }, end_effector_rotation=base_rotation)
        self.pour = DemoPourExecutor(resources=resources, backend=backend,
            kinematics=kinematics, execute_motion=execute_motion,
            synchronizer=synchronizer, checker=checker, report=report)

    def execute(self, command, index, state, row):
        from contextlib import ExitStack
        capture = getattr(self.checker, 'capture_lift_clearance', None)
        clear = getattr(self.checker, 'clear_lift_clearance', None)
        lift = command['skill_name'] == 'lift_up'
        with ExitStack() as cleanup:
            scope = getattr(self.checker, 'lift_payload_collision_scope', None)
            if lift and callable(scope):
                cleanup.enter_context(scope())
            if lift and callable(capture) and callable(clear):
                cleanup.callback(clear)
                margin = float(self.resources.scene.get('move_defaults', {})
                               .get('constraints', {}).get('safety_distance', .003))
                capture(margin)
            result = self._execute_command(command, index, state, row)
        from execution.grasp_verification import verify_command_grasp
        return verify_command_grasp(self.backend, result)

    def _execute_command(self, command, index, state, row):
        set_command_context = getattr(self.resolver, 'set_command_context', None)
        if callable(set_command_context):
            set_command_context(command.get('command_id'))
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
        if command['skill_name'] not in {'move_to', 'lift_up', 'put_down', 'shift_down', 'pick', 'put'}:
            raise ValueError('TaskSkillExecutor supports move_to, pick, put, lift_up, put_down, shift_down, grasp, release, approach, retreat, and pour')
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
        relative_move_to = (
            skill == 'move_to'
            and any(key in parameters for key in ('relation', 'distance_m'))
            and self.resolver is not None
            and parameters.get('target') not in self.resources.scene.get('named_targets', {})
            and parameters.get('target') not in {'end_effector', '__end_effector__'}
        )
        if relative_move_to:
            target_started = time.perf_counter()
            resolved_move = self.resolver.resolve(parameters['target'], self.resources, state)
            target_resolve_s = time.perf_counter() - target_started
            select_started = time.perf_counter()
            selected_move = self.resolver.select(
                resolved_move, self.kinematics, self.checker, state['joints'],
                safety_distance=float(self.resources.scene.get('move_defaults', {})
                                      .get('constraints', {}).get('safety_distance', 0.003)),
                prefer_direct=False)
            row['target_resolution_timing_s'] = {
                'resolve_total': target_resolve_s,
                'candidate_selection': time.perf_counter() - select_started,
            }
            self.resolver.remember_selection(selected_move)
            if selected_move.get('visual_estimate') is not None:
                row['visual_grasp_resolution'] = selected_move
            goal = np.asarray(selected_move['selected']['world_from_control'], dtype=float)
            target_pose = self.converter._offset_pose(
                pose_request(goal), parameters, index,
                direction_rotation=self.converter.end_effector_rotation)
            # The relative offset changes the final pose, so re-solve IK for
            # that exact pose and reject goal configurations already colliding
            # with the table or other scene geometry before planning.
            solutions = self.kinematics.solve_all(
                target_pose, seed=state['joints'], max_solutions=8).solutions
            valid_goals = []
            current_q = np.asarray(state['joints'], dtype=float)
            for candidate_q in solutions:
                candidate_q = np.asarray(candidate_q, dtype=float)
                hit = self.checker.check(
                    candidate_q,
                    safety_distance=float(self.resources.scene.get('move_defaults', {})
                                          .get('constraints', {}).get('safety_distance', 0.003)))
                if hit.in_collision:
                    continue
                delta = np.arctan2(np.sin(candidate_q - current_q),
                                   np.cos(candidate_q - current_q))
                valid_goals.append((float(np.linalg.norm(delta)), candidate_q))
            if not valid_goals:
                raise ValueError('No collision-free IK solution exists for the relative move_to target')
            from planning.arm_posture import configuration_cost
            valid_goals.sort(key=lambda item: configuration_cost(kin, item[1], current_q, norm=2))
            selected_joint_goal = valid_goals[0][1]
            path_type = parameters.get('planning_method', 'joint')
            request = _deep_merge(
                self.resources.scene['move_defaults'],
                {'target': target_pose,
                 'motion': {'path_type': path_type},
                 'planning': {'mode': 'collision_free', 'allow_replan': True}},
            )
            if path_type == 'joint':
                request['target'] = {'type': 'joint', 'joint_positions': selected_joint_goal.tolist()}
        elif parameters.get('link') and not (
                callable(getattr(self.resolver, 'is_visual_handle', None))
                and self.resolver.is_visual_handle(parameters.get('target'), self.resources)):
            from demo.interaction.requests import motion_request
            request = motion_request(self.resources, backend, parameters, blackboard=sync.blackboard)
        else:
            step = self.converter.convert_command(command, index)
            request = _deep_merge(self.resources.scene['move_defaults'], step['request'])

        request = _deep_merge(request, parameters.get('request', {}))
        row['substeps'] = []
        resolved = None
        if request['target']['type'] == 'object':
            self.stage = 'target_resolution'
            try:
                adapter = self.adapters[skill]
                resolution_started = time.perf_counter()
                if skill == 'pick':
                    # Pick only resolves and reaches the grasp pose here.
                    # Closure is a target-free gripper action performed after
                    # contact; do not run the legacy target-body closure probe.
                    parts, resolved = adapter.prepare(request, state, grasp_request=None,
                        retreat_lift_height=0.0)
                    if resolved.get('visual_estimate', {}).get('grasp_type') == 'handle':
                        backend.contact_target = resolved['target_name']
                        backend.contact_target_object_id = parameters['target']
                else:
                    parts, resolved = adapter.prepare(request, state)
                row['target_resolution_timing_s'] = dict(
                    getattr(adapter, 'last_resolution_timing_s', {}) or {})
                row['target_resolution_timing_s']['adapter_prepare_total'] = (
                    time.perf_counter() - resolution_started)
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

        row['actual_path_collision_free'] = None
        row['actual_path_verification'] = 'disabled'
        row['collision_policy'] = 'planning_only'

        def record(frame_state):
            self.write_frame(command['command_id'], self.report.get('active_substep', part['phase']), frame_state)
            sync.sync(backend, kin, checker)

        backend.set_frame_callback(record)
        self.stage = 'planning_and_execution'
        for part in parts:
            self.report['active_substep'] = part['phase']
            sync.sync(backend, kin, checker)
            if skill == 'pick' and part['phase'] in {'lift', 'retreat'}:
                resolve_post_grasp_target(part, backend.get_end_effector_pose(), resolved)
            backend.set_status(f"{command['command_id']} | Planning / {part['phase']}")
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
            if (skill in {'pick', 'put'} and part['phase'] in {'approach','contact'} and
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
                if not result.get('success') and isinstance(result.get('error'), dict):
                    error = deepcopy(result['error'])
                    error.setdefault('details', {})['preceding_contact_error'] = deepcopy(
                        resolved['approach']['runtime_fallback']['reason'])
                    result = {**result, 'error': error}
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
                # Gripper/selected-object contacts are permitted throughout
                # pick, including motion, verification, and physical closure.
                backend.grasp_motion_phase = False
                self.stage = 'grasp'
                self.report['active_substep'] = 'grasp'
                backend.set_status(f"{command['command_id']} | Closing and confirming grasp")
                grasp_request = {
                    'grasp': {'mode': 'physical'},
                    'strategy_params': {'hold_force': parameters.get('hold_force', 10.0)},
                }
                carrying_distance = 0.0
                previous_grasp_goal = getattr(backend, 'grasp_approach_goal', None)
                selected_control = np.asarray(resolved['selected']['world_from_control'], dtype=float)
                selected_quat = Rotation.from_matrix(selected_control[:3, :3]).as_quat()
                backend.grasp_approach_goal = (tuple(selected_control[:3, 3]), tuple(selected_quat),
                                               tuple(resolved['joint_positions']))
                backend.grasp_motion_phase = False
                try:
                    grasp_result = self.grasper.execute(grasp_request)
                finally:
                    backend.grasp_approach_goal = previous_grasp_goal
                row['grasp_result'] = grasp_result
                if not grasp_result['success']:
                    row.update(success=False, error=grasp_result['error'])
                    self.stage = grasp_result['error'].get('failed_stage', 'grasp')
                    raise RuntimeError(f"Grasp failed: {grasp_result['error']}")
                if backend.gripper_controller.held_name is not None:
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
                release_result = self.releaser.execute(release_request)
                row['release_result'] = release_result
                checker.support_geoms.clear()
                if backend.gripper_controller.held_name is None:
                    checker.carrying_safety_distance = 0.
                sync.sync(backend, kin, checker)
                if not release_result['success']:
                    row.update(success=False, error=release_result['error'])
                    raise RuntimeError(f"Release failed: {release_result['error']}")
                if (callable(getattr(self.resolver, 'is_visual_place', None)) and
                        self.resolver.is_visual_place(parameters.get('target'))):
                    backend.advance_gripper(.25)
                    row['release_settle_time_s'] = .25
                    sync.sync(backend, kin, checker)
                self.stage = 'planning_and_execution'

        if result.get('success'):
            if skill == 'lift_up':
                checker.support_geoms.clear()
            sync.sync(backend, kin, checker)
            goal_q = np.asarray(result['trajectory']['waypoints'][-1])
            expected_pose = kin.forward(goal_q)
            actual_pose = backend.get_end_effector_pose()
            row['tcp_error_m'] = float(np.linalg.norm(expected_pose['position'] - actual_pose['position']))
            execution_result = row.get('execution_result')
            if isinstance(execution_result, dict):
                execution_result.pop('frames', None)
        if row.get('success') and skill in {'pick', 'put'} and resolved is not None:
            self._remember_interaction(command, resolved)
        return row

    def _remember_interaction(self, command, resolved):
        """Freeze the chosen interaction frame before the target moves again."""
        selected = resolved['selected']
        calibration = np.asarray(resolved['control_from_grasp'], dtype=float)
        control_pose = selected.get('world_from_control')
        if control_pose is None:
            control_pose = np.asarray(selected['world_from_grasp']) @ np.linalg.inv(calibration)
        if not hasattr(self, '_interaction_history'):
            self._interaction_history = {}
        self._interaction_history[str(command['parameters']['target'])] = {
            'command_id': command.get('command_id'),
            'skill_name': command['skill_name'],
            'world_from_control': np.asarray(control_pose, dtype=float).copy().tolist(),
            'control_from_grasp': calibration.copy().tolist(),
        }

    def _execute_pose_search(self, command, state, row):
        """Retreat combines the stored interaction direction with the live TCP."""
        checker = self.checker
        previous_gripper_allowance = getattr(checker, 'allow_gripper_environment_contact', False)
        if command['skill_name'] == 'retreat' and hasattr(checker, 'allow_gripper_environment_contact'):
            checker.allow_gripper_environment_contact = True
        try:
            return self._execute_pose_search_inner(command, state, row)
        finally:
            if hasattr(checker, 'allow_gripper_environment_contact'):
                checker.allow_gripper_environment_contact = previous_gripper_allowance

    def _execute_pose_search_inner(self, command, state, row):
        parameters = deepcopy(command['parameters'])
        resolved = None
        if command['skill_name'] == 'retreat':
            target = parameters.pop('target', None)
            reference = getattr(self, '_interaction_history', {}).get(str(target))
            if target is None or reference is None:
                self.stage = 'target_resolution'
                row.update(success=False, status='error', error={
                    'error_code': 'RETREAT_REFERENCE_MISSING',
                    'failed_stage': self.stage,
                    'error_message': f'目标 {target} 没有成功的 approach、pick 或 put 交互位姿，无法确定撤退方向'})
                return row
            # Only use the original frame's orientation. Its position is not a
            # retreat goal and must never trigger another grasp IK/depth search.
            parameters['reference_rotation_world'] = np.asarray(
                reference['world_from_control'])[:3, :3].tolist()
            parameters['grasp_offset_local'] = np.asarray(
                reference['control_from_grasp'])[:3, 3].tolist()
            parameters['_preferred_distance_m'] = getattr(
                self, '_pose_search_distances', {}).get(str(target))
            row['retreat_reference'] = {'target_id': target, **deepcopy(reference)}
        elif command['skill_name'] == 'approach':
            if self.resolver is None:
                raise ValueError(f"{command['skill_name']} requires an object-library resolver")
            target = parameters.pop('target', None)
            self.stage = 'target_resolution'
            resolve_started = time.perf_counter()
            resolved = self.resolver.resolve(target, self.resources, state)
            resolve_elapsed = time.perf_counter() - resolve_started
            select_started = time.perf_counter()
            selected = self.resolver.select(
                resolved, self.kinematics, self.checker, state['joints'],
                safety_distance=float(self.resources.scene.get('move_defaults', {})
                                      .get('constraints', {}).get('safety_distance', 0.003)),
                prefer_direct=False)
            row['target_resolution_timing_s'] = {
                'resolve_total': resolve_elapsed,
                'candidate_selection': time.perf_counter() - select_started,
            }
            self.resolver.remember_selection(selected)
            resolved = selected
            pose = np.asarray(selected['selected']['world_from_control'], dtype=float)
            control_from_grasp = np.asarray(resolved['control_from_grasp'], dtype=float)
            parameters['grasp_offset_local'] = control_from_grasp[:3, 3].tolist()
            parameters['target_pose'] = {
                'position': pose[:3, 3].tolist(),
                'quaternion_xyzw': Rotation.from_matrix(pose[:3, :3]).as_quat().tolist(),
                'frame': 'world',
            }
            # Observation changes only the approach pose, never the cached grasp.
            if (parameters.get('wrist_camera_alignment', True)
                    and resolved.get('visual_estimate')
                    and resolved['selected'].get('pose_source') in
                    ('vlm_rgbd_grasp', 'blackboard_contact_link')
                    and callable(getattr(self.resolver, 'refine_after_approach', None))):
                calibration = getattr(self.backend, 'get_control_from_camera', None)
                try:
                    if not callable(calibration):
                        raise ValueError('Backend does not expose rigid camera calibration')
                    world_grasp = np.asarray(resolved['selected'].get(
                        'world_from_grasp', pose @ control_from_grasp), dtype=float)
                    depth = float(resolved['selected'].get('grasp_depth_offset_m',
                        resolved['visual_estimate'].get('grasp_depth_offset_m', 0.)))
                    surface = world_grasp[:3, 3] - depth * world_grasp[:3, 2]
                    parameters['_wrist_observation'] = {
                        'control_from_camera': np.asarray(calibration('wrist')).tolist(),
                        'target_world': surface.tolist(),
                    }
                except (ValueError, KeyError, TypeError) as exc:
                    row['wrist_observation'] = {'status': 'fallback_to_gripper_approach',
                                              'reason': str(exc)}
        executor = PoseSearchExecutor(
            resources=self.resources, backend=self.backend,
            synchronizer=self.sync, kinematics=self.kinematics,
            checker=self.checker, execute_motion=self.execute_motion)
        self.stage = command['skill_name']
        self.backend.set_frame_callback(
            lambda frame_state: self.write_frame(command['command_id'], executor.phase, frame_state))
        try:
            result = executor.run(command['skill_name'], parameters)
        finally:
            self.backend.set_frame_callback(None)
        for substep in result.get('substeps', ()):
            execution_result = substep.get('execution_result')
            if isinstance(execution_result, dict):
                execution_result.pop('frames', None)
        row.update(result, **({'target_resolution': resolved} if resolved is not None else {}))
        if result.get('success') and result.get('successful_distance_m') is not None:
            if not hasattr(self, '_pose_search_distances'):
                self._pose_search_distances = {}
            self._pose_search_distances[str(command['parameters']['target'])] = result['successful_distance_m']
        if result.get('success') and command['skill_name'] == 'approach':
            refine = getattr(self.resolver, 'refine_after_approach', None)
            if (callable(refine) and (resolved or {}).get('visual_estimate')
                    and resolved.get('selected', {}).get('pose_source') in
                    ('vlm_rgbd_grasp', 'blackboard_contact_link')):
                self.stage = 'wrist_refinement'
                self.report['active_substep'] = self.stage
                self.backend.set_status('Wrist camera / refining grasp position')
                current = self.sync.sync(self.backend, self.kinematics, self.checker)
                row['coarse_target_resolution'] = deepcopy(resolved)
                try:
                    resolved = refine(resolved, self.resources, current, self.kinematics, self.checker)
                except Exception as exc:
                    row.update(success=False, status='error', error=getattr(exc, 'details', {
                        'error_code': 'WRIST_GRASP_REFINEMENT_FAILED', 'failed_stage': self.stage,
                        'error_message': str(exc)}))
                    raise
                row['target_resolution'] = resolved
                row['wrist_refinement'] = resolved['visual_estimate']['wrist_refinement']
            self._remember_interaction(command, resolved)
        return row

    def _execute_closure(self, command, row):
        """Execute direct grasp/release commands after resolving scene names."""
        skill = command['skill_name']
        parameters = command['parameters']
        self.stage = skill
        self.report['active_substep'] = skill
        self.backend.set_frame_callback(
            lambda frame_state: self.write_frame(command['command_id'], skill, frame_state))
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






