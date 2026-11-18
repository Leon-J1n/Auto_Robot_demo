"""Planner boundary checks for frozen collision policies, without a GPU."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from planning.planners.curobo.planner import CuroboPlanner


class PolicyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.request = {'target': {'type': 'joint', 'joint_positions': [.1] * 6},
                        'constraints': {'safety_distance': .003}}
        self.scene = {'_runtime_collision': {'policy': {
            'safety_distance': .003, 'avoid_collision': True}, 'state_signature': 'original'}}
        self.checker = Mock()
        self.checker.allowed_target_names = {'previous'}
        self.checker.check_path.return_value = SimpleNamespace(in_collision=False, contacts=[])
        self.client = Mock()
        self.client.plan.return_value = {'waypoints': [[0.] * 6, [.1] * 6],
                                         'planning_time': .01, 'duration': 1.}
        self.planner = CuroboPlanner(scene=self.scene, client=self.client,
                                      collision_checker=self.checker)

    def test_plan_only_still_runs_host_collision_validation(self):
        result = self.planner.plan(self.request)
        self.assertTrue(result['success'])
        self.assertTrue(result['planned_only'])
        self.checker.check_path.assert_called_once()
        self.checker.check_path.return_value = SimpleNamespace(in_collision=True, contacts=['blocked'])
        result = self.planner.plan(self.request)
        self.assertFalse(result['success'])
        self.assertIn('Host collision validation', result['error']['error_message'])

    def test_target_scope_restores_after_worker_failure(self):
        self.client.plan.side_effect = RuntimeError('worker failure')
        result = self.planner.plan({**self.request, 'target_body_name': 'selected'})
        self.assertFalse(result['success'])
        self.assertEqual(self.checker.set_allowed_target_names.call_args_list[0].args, ({'selected'},))
        self.assertEqual(self.checker.set_allowed_target_names.call_args_list[-1].args, ({'previous'},))

    def test_host_validation_honors_finer_requested_sampling(self):
        request = deepcopy(self.request)
        request['planning'] = {'collision_validation': {'resolution_rad': .004}}
        result = self.planner.plan(request)
        self.assertTrue(result['success'])
        self.assertEqual(self.checker.check_path.call_args.kwargs['resolution'], .004)

    def test_stale_runtime_policy_rejects_plan_before_host_check(self):
        self.planner.robot_runtime = SimpleNamespace()
        with patch('collision.planning_policy.planning_signature', return_value='changed'):
            result = self.planner.plan(self.request)
        self.assertFalse(result['success'])
        self.assertIn('changed during cuRobo planning', result['error']['error_message'])
        self.checker.check_path.assert_not_called()

    def test_snapshot_uses_merged_and_normalized_phase_constraints(self):
        runtime = SimpleNamespace(resources=SimpleNamespace(scene={
            'move_defaults': {'constraints': {'velocity_scale': .4}}}))
        planner = CuroboPlanner(robot_runtime=runtime, client=self.client,
            collision_checker=self.checker, config={'move_defaults': {'constraints': {'acceleration_scale': .3}}})
        request = {'target': deepcopy(self.request['target']), 'motion': {'phase': 'approach'}}
        with patch('libraries.curobo.runtime_scene.runtime_snapshot', return_value=({}, 1)) as capture:
            planner._snapshot(request)
        constraints = capture.call_args.kwargs['constraints']
        self.assertEqual(constraints['safety_distance'], .02)
        self.assertEqual(constraints['velocity_scale'], .4)
        self.assertEqual(constraints['acceleration_scale'], .3)
        self.assertNotIn('constraints', request)

    def test_policy_margin_mismatch_never_calls_worker(self):
        result = self.planner.plan({**self.request, 'constraints': {'safety_distance': .01}})
        self.assertFalse(result['success'])
        self.assertIn('disagrees', result['error']['error_message'])
        self.client.plan.assert_not_called()

    def test_explicit_collision_disable_skips_host_validation(self):
        self.scene['_runtime_collision']['policy']['avoid_collision'] = False
        planner = CuroboPlanner(scene=self.scene, client=self.client, collision_checker=self.checker)
        result = planner.plan({**self.request, 'constraints': {'safety_distance': .003, 'avoid_collision': False}})
        self.assertTrue(result['success'])
        self.checker.check_path.assert_not_called()


if __name__ == '__main__':
    unittest.main()
