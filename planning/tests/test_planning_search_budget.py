"""Regressions for cold cuRobo starvation and nested approach retries."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from atomic_action.motion.common.action import _MoveExecutor
from atomic_action.motion.common.interaction import planning_failed_without_motion
from atomic_skill.approach import PoseSearchExecutor
from demo.run_demo import _print_command_timing, step_message
from atomic_skill.runner import _planning_prepare
from planning.common.errors import PlanningError
from planning.planners.curobo.collision_free import CollisionFreePlanner


class SearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.request = {
            'motion': {'path_type': 'joint', 'path_constraint': 'soft'},
            'planning': {'planning_time': 10., 'mode': 'collision_free', 'allow_replan': True},
            'constraints': {'timeout': 60., 'safety_distance': .003, 'avoid_collision': True},
        }

    def test_phase_planning_time_takes_precedence_over_inclusive_total(self):
        row = {'command_id': '13', 'substeps': [
            {'error': {'failed_stage': 'planning'},
             'timing': {'planning': 11., 'total': 11.1, 'curobo_attempt': 10.}},
            {'timing': {'planning': 9.7, 'total': 12.8}}]}
        output = StringIO()
        with redirect_stdout(output):
            _print_command_timing(row)
        self.assertIn('planning=20.700s', output.getvalue())

    def test_full_preparation_includes_target_ik_and_trajectory_without_double_counting(self):
        row = {'command_id': '5', 'target_preparation_s': 2.374,
               'pose_search_precheck': [{'elapsed_s': .001}],
               'substeps': [{'timing': {'pre_execution_total': .310, 'planning': .224,
                   'path_generation': .004, 'trajectory_preparation': .082,
                   'execution': 1.78, 'total': 2.09}},
                   {'timing': {'path_generation': .5, 'planning': .2, 'trajectory_preparation': .1}}]}
        output = StringIO()
        with redirect_stdout(output):
            _print_command_timing(row)
        self.assertEqual(output.getvalue().strip(), 'timing|command=5|planning=3.485s')

    def test_target_preparation_excludes_request_wait_even_on_failure(self):
        row = {}
        with patch('atomic_skill.runner.time.perf_counter', side_effect=[1., 9.]), \
             patch('atomic_skill.runner.request_elapsed', side_effect=[2., 7.]):
            with self.assertRaises(ValueError):
                _planning_prepare(row, Mock(side_effect=ValueError('no candidate')))
        self.assertEqual(row['target_preparation_s'], 3.)

    def test_legacy_failed_phase_without_planning_uses_total_once(self):
        output = StringIO()
        with redirect_stdout(output):
            _print_command_timing({'substeps': [
                {'error': {'failed_stage': 'planning'}, 'timing': {'total': 2.}},
                {'timing': {'curobo_attempt': 3., 'total': 4.}}]})
        self.assertIn('planning=5.000s', output.getvalue())

    def test_timing_adds_vlm_duration_and_ignores_cached_evidence(self):
        cases = [
            ({'vlm_request_s': 1.25}, '|vlm_request=1.250s'),
            ({'target_resolution': {'vlm_request_s': 2.5}}, ''),
            ({'target_resolution': {'visual_estimate': {'vlm_request_s': 9.,
                'wrist_refinement': {'status': 'validated'}}}}, ''),
            ({'coarse_target_resolution': {'vlm_request_s': 2.},
              'target_resolution': {'vlm_request_s': 2., 'visual_estimate': {
                  'vlm_request_s': 3., 'wrist_refinement': {'status': 'validated'}}}}, ''),
            ({'vlm_request_s': 0.}, '|vlm_request=0.000s'),
        ]
        for fields, suffix in cases:
            with self.subTest(fields=fields):
                output = StringIO()
                with redirect_stdout(output):
                    _print_command_timing({'command_id': '16', 'timing': {'planning': .562}, **fields})
                self.assertEqual(output.getvalue().strip(), 'timing|command=16|planning=0.562s' + suffix)

    def action(self):
        action = _MoveExecutor.__new__(_MoveExecutor)
        action.collision_checker = SimpleNamespace(
            check=lambda *a, **k: SimpleNamespace(in_collision=False),
            check_path=lambda *a, **k: SimpleNamespace(in_collision=True, contacts=[]))
        return action

    def test_relative_target_is_shown_in_chinese_success_message(self):
        for skill, verb in (('move_to', '已移动到目标'), ('put', '已放置到目标')):
            for relation, label in (('left', '左侧'), ('right', '右侧'), ('front', '前方'),
                                    ('behind', '后方'), ('above', '上方'), ('below', '下方')):
                command = {'skill_name': skill, 'parameters': {'target': 7, 'relation': relation}}
                self.assertEqual(step_message(command, success=True, target_name='Apple'),
                                 f'success: {verb} Apple {label}')
        self.assertEqual(step_message({'skill_name': 'move_to', 'parameters': {'target': 'home'}},
                                     success=True), 'success: 已移动到目标 home')
        for skill, verb in (('approach', '已接近目标'), ('retreat', '已远离目标')):
            self.assertEqual(step_message({'skill_name': skill, 'parameters': {'target': 12}},
                                         success=True, target_name='Basket'), f'success: {verb} Basket')

    def path(self):
        return {'start': np.zeros(6), 'candidate_goals': [np.full(6, .1*i) for i in range(1, 5)]}

    def test_cold_first_branch_gets_remaining_budget_not_one_quarter(self):
        action = self.action()
        calls = []
        def fallback(request, context, selection, path, error):
            calls.append(deepcopy(request))
            if request['planning']['planning_time'] < 2.8:
                raise AssertionError('Cold graph capture was starved')
            self.now += 3.
            return {'waypoints': [path['start'].tolist(), path['goal'].tolist()]}
        action._plan_collision_fallback = fallback
        with patch('atomic_action.motion.common.action.time.perf_counter', side_effect=lambda: self.now):
            result = action._plan_joint_candidates(self.request, {}, {}, self.path())
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0]['planning']['planning_time'], 9.)
        self.assertLessEqual(calls[0]['constraints']['timeout'], 60.)
        self.assertEqual(result['diagnostics']['configuration_selection']['bypass_attempts'], 1)

    def test_worker_timeout_is_terminal_for_branch_and_pose_search(self):
        action = self.action()
        action._plan_collision_fallback = Mock(side_effect=PlanningError(
            'worker stopped', code='CUROBO_TIMEOUT', recoverable=False))
        with self.assertRaises(PlanningError) as caught:
            action._plan_joint_candidates(self.request, {}, {}, self.path())
        self.assertEqual(caught.exception.code, 'CUROBO_TIMEOUT')
        action._plan_collision_fallback.assert_called_once()
        self.assertFalse(planning_failed_without_motion(
            {'error': {'failed_stage': 'planning', 'recoverable': False}},
            SimpleNamespace(physics_steps=0), 0))

    def test_failed_branch_keeps_collision_details_after_another_branch_succeeds(self):
        action = self.action()
        details = {'stage': 'execution_preparation', 'contacts': [
            {'geom1': 'payload', 'geom2': 'Tray', 'distance': -.004}]}
        calls = []
        def fallback(request, context, selection, path, error):
            calls.append(path)
            if len(calls) == 1:
                raise PlanningError('Continuous trajectory failed collision validation',
                                    code='TRAJECTORY_INVALID', recoverable=True, details=details)
            return {'waypoints': [path['start'].tolist(), path['goal'].tolist()]}
        action._plan_collision_fallback = fallback
        with patch('atomic_action.motion.common.action.time.perf_counter', side_effect=lambda: self.now):
            result = action._plan_joint_candidates(self.request, {}, {}, self.path())
        failed = next(t for t in result['diagnostics']['configuration_selection']['candidate_timings']
                      if t.get('error_code') == 'TRAJECTORY_INVALID')
        self.assertEqual(failed['error_message'], 'Continuous trajectory failed collision validation')
        self.assertEqual(failed['error_details'], details)

    def test_initialization_is_reported_separately_from_search_budget(self):
        action = self.action()
        def fallback(request, context, selection, path, error):
            self.now += 13.
            return {'waypoints': [path['start'].tolist(), path['goal'].tolist()],
                    'diagnostics': {'curobo': {'planner_reused': False},
                                    'planning_stages_s': {'preparation': 10.}}}
        action._plan_collision_fallback = fallback
        with patch('atomic_action.motion.common.action.time.perf_counter', side_effect=lambda: self.now):
            result = action._plan_joint_candidates(self.request, {}, {}, self.path())
        stats = result['diagnostics']['configuration_selection']
        self.assertEqual(stats['initialization_s'], 10.)
        self.assertFalse(stats['budget_exhausted'])

    def test_adapter_preserves_worker_restart_failure(self):
        inner = SimpleNamespace(plan=lambda request: {'success': False, 'error': {
            'error_code': 'CUROBO_FAILED', 'error_message': 'worker stopped',
            'details': {'error_code': 'CUROBO_TIMEOUT', 'worker_restart_required': True}}})
        planner = CollisionFreePlanner(collision_checker=self.action().collision_checker,
            joint_limits=[[-2., 2.]]*6, curobo_planner=inner)
        with self.assertRaises(PlanningError) as caught:
            planner.plan({'start': np.zeros(6), 'goal': np.ones(6), 'path_type': 'joint'}, self.request, {})
        self.assertEqual(caught.exception.code, 'CUROBO_TIMEOUT')
        self.assertFalse(caught.exception.recoverable)

    def test_late_trajectory_cannot_reach_execution_after_shared_timeout(self):
        action = self.action()
        request = deepcopy(self.request)
        request['constraints']['timeout'] = 1.
        def late(request, context, selection, path, error):
            self.now = 2.
            return {'waypoints': [path['start'].tolist(), path['goal'].tolist()]}
        action._plan_collision_fallback = late
        with patch('atomic_action.motion.common.action.time.perf_counter', side_effect=lambda: self.now):
            with self.assertRaises(PlanningError) as caught:
                action._plan_joint_candidates(request, {}, {}, self.path())
        self.assertEqual(caught.exception.code, 'PLANNING_TIMEOUT')
        self.assertFalse(caught.exception.recoverable)

    def executor(self, callback):
        return PoseSearchExecutor(resources=SimpleNamespace(scene={'move_defaults': self.request}),
            backend=SimpleNamespace(physics_steps=0), synchronizer=SimpleNamespace(sync=lambda *a: None),
            kinematics=None, checker=None, execute_motion=callback)

    def params(self):
        return {'target_pose': {'position': [0., 0., 1.]},
                'search': {'max_distance': .1, 'min_distance': .09, 'step': .005}}

    def test_all_distances_share_timeout_and_do_not_start_after_expiry(self):
        self.request['constraints']['timeout'] = 1.
        calls = []
        def failed(request):
            calls.append(request)
            self.now += .6
            return {'success': False, 'error': {'failed_stage': 'planning', 'recoverable': True}}
        executor = self.executor(failed)
        with patch('atomic_skill.approach.perf_counter', side_effect=lambda: self.now):
            result = executor.run('approach', self.params())
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(calls[0]['constraints']['timeout'], 1.)
        self.assertAlmostEqual(calls[1]['constraints']['timeout'], .4)
        self.assertAlmostEqual(calls[1]['planning']['planning_time'], .4)
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'PLANNING_TIMEOUT')

    def test_final_move_to_fallback_uses_remaining_timeout(self):
        self.request['constraints']['timeout'] = 2.
        calls = []
        def failed(request):
            calls.append(request)
            self.now += .2
            return {'success': False, 'error': {'failed_stage': 'planning', 'recoverable': True}}
        executor = self.executor(failed)
        with patch('atomic_skill.approach.perf_counter', side_effect=lambda: self.now):
            executor.run('approach', self.params())
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(calls[-1]['constraints']['timeout'], 1.4)

    def test_terminal_failure_does_not_try_next_distance_or_fallback(self):
        failed = Mock(return_value={'success': False, 'error': {
            'error_code': 'CUROBO_TIMEOUT', 'failed_stage': 'planning', 'recoverable': False}})
        result = self.executor(failed).run('approach', self.params())
        self.assertFalse(result['success'])
        failed.assert_called_once()

    def test_invalid_search_timeout_returns_structured_error_without_move(self):
        self.request['constraints']['timeout'] = -1.
        callback = Mock()
        result = self.executor(callback).run('approach', self.params())
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'INVALID_APPROACH_REQUEST')
        callback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
