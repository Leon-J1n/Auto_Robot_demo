"""Planner boundary checks for frozen collision policies, without a GPU."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np

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

    def test_joint_direct_selection_still_runs_host_collision_validation(self):
        self.client.plan.return_value['planning_mode'] = 'direct'
        result = self.planner.plan(self.request)
        self.assertTrue(result['success'])
        self.assertEqual(result['selection']['planner_type'], 'curobo')
        self.assertEqual(result['selection']['planning_mode'], 'direct')
        self.checker.check_path.assert_called_once()

    def test_worker_gpu_verification_replaces_host_validation(self):
        self.planner.scene['_runtime_collision']['planning_proxy'] = {'geometry_source': 'source_convex_geometry'}
        self.client.plan.return_value['diagnostics'] = {
            'gpu_collision_validation': {'samples': 11, 'resolution_rad': .01}}
        self.assertTrue(self.planner.plan(self.request)['success'])
        self.client.plan.return_value['diagnostics'] = {}
        result = self.planner.plan(self.request)
        self.assertFalse(result['success'])
        self.assertIn('worker did not verify', result['error']['error_message'])
        self.checker.check_path.assert_not_called()

    def test_target_scope_restores_after_worker_failure(self):
        self.client.plan.side_effect = RuntimeError('worker failure')
        result = self.planner.plan({**self.request, 'target_body_name': 'selected'})
        self.assertFalse(result['success'])
        self.assertEqual(self.checker.set_allowed_target_names.call_args_list[0].args, ({'selected'},))
        self.assertEqual(self.checker.set_allowed_target_names.call_args_list[-1].args, ({'previous'},))

    def test_explicit_planner_preserves_timeout_and_stops_automatic_pose_retries(self):
        failure = TimeoutError('worker stopped')
        failure.curobo_error = {'error_code': 'CUROBO_TIMEOUT', 'worker_restart_required': True}
        self.client.plan.side_effect = failure
        result = self.planner.plan(self.request)
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'CUROBO_TIMEOUT')
        self.assertFalse(result['error']['recoverable'])
        self.assertEqual(result['status'], 'timeout')

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

    def test_linear_pose_is_sent_to_gpu_and_marked_linear(self):
        request = {'target': {'type': 'pose', 'position': {'x': .1, 'y': 0., 'z': 0.}},
                   'motion': {'path_type': 'linear'}, 'constraints': {'safety_distance': .003,
                   'keep_end_effector_orientation': True}}
        result = self.planner.plan(request)
        self.assertTrue(result['success'])
        self.assertEqual(result['selection']['path_strategy'], 'linear_move')
        self.assertEqual(self.client.plan.call_args.args[0]['motion']['path_type'], 'linear')
        self.checker.check_path.assert_called_once()

    def test_proxy_linear_never_falls_back_to_host_validation(self):
        request = {'target': {'type': 'pose', 'position': {'x': .1, 'y': 0., 'z': 0.}},
                   'motion': {'path_type': 'linear'}, 'constraints': {'safety_distance': .003}}
        self.planner.scene['_runtime_collision']['planning_proxy'] = {'geometry_source': 'source_convex_geometry'}
        result = self.planner.plan(request)
        self.assertFalse(result['success'])
        self.assertIn('worker did not verify', result['error']['error_message'])
        self.checker.check_path.assert_not_called()

    def test_circular_fails_without_silent_cpu_fallback(self):
        result = self.planner.plan({**self.request, 'motion': {'path_type': 'circular',
            'path_constraint': 'hard'}, 'strategy_params': {'via_point': [0., 0., 0.]}})
        self.assertFalse(result['success'])
        self.assertIn('circular', result['error']['error_message'])
        self.client.plan.assert_not_called()


class FixedSimulationSceneTests(unittest.TestCase):
    def test_freezes_geometry_but_refreshes_start_and_requires_gpu_verification(self):
        from execution.backends.mujoco.backend import MuJoCoExecutionBackend
        runtime = Mock(spec=MuJoCoExecutionBackend)
        runtime.resources = SimpleNamespace(scene={})
        runtime.base_id = 0
        runtime.model = SimpleNamespace(nbody=4, body_parentid=[0, 0, 1, 0])
        runtime.object_ids = {'WorkTable': 1, 'Apple': 3}
        runtime.data = SimpleNamespace(xpos=np.zeros((4, 3)), xmat=np.tile(np.eye(3), (4, 1, 1)),
                                       site_xpos=np.zeros((1, 3)), site_xmat=np.eye(3)[None])
        runtime.get_joint_positions.side_effect = [np.zeros(6), np.ones(6)]
        checker = Mock(tcp_site_id=0)
        exported = {'_runtime_collision': {
            'payload_bodies': [], 'base_world': np.eye(4).tolist(),
            'geometries': [{'id': body, 'body': body, 'robot': body == 0} for body in (0, 2, 3)],
            'policy': {'allowed_pairs': [[0, 2], [0, 3]], 'avoid_collision': True}}}
        planner = CuroboPlanner(robot_runtime=runtime, collision_checker=checker, client=Mock(),
                                fixed_simulation_scene=True)
        request = {'target': {'type': 'joint', 'joint_positions': [.1] * 6},
                   'constraints': {'safety_distance': .003}}
        with patch('libraries.curobo.runtime_scene.runtime_snapshot', return_value=(exported, 1)) as export, \
                patch('collision.planning_policy.planning_signature', return_value='unchanged'):
            first, _ = planner._snapshot(request)
            runtime.data.site_xpos[0, 0] = .1
            second, _ = planner._snapshot(request)
        export.assert_called_once()
        first, second = first['_runtime_collision'], second['_runtime_collision']
        self.assertEqual([item['id'] for item in second['geometries']], [0, 2])
        self.assertEqual(second['policy']['allowed_pairs'], [[0, 2]])
        self.assertEqual(first['policy']['fingerprint'], second['policy']['fingerprint'])
        self.assertEqual(second['joints'], [1.] * 6)
        self.assertEqual(second['tcp_world'][0][3], .1)
        planner.client.plan.return_value = {'diagnostics': {}}
        with patch.object(planner, '_snapshot', return_value=({'_runtime_collision': second}, 1)):
            result = planner.plan(request)
        self.assertFalse(result['success'])
        self.assertIn('worker did not verify', result['error']['error_message'])
        checker.check_path.assert_not_called()


if __name__ == '__main__':
    unittest.main()
