import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from demo.interaction.grasp_depth_search import GRASP_DEPTHS_M, validate_depth_candidate
from demo.interaction.vlm_grasp import VLMGraspResolver
from demo.interaction.object_library import GraspResolutionError
from execution.blackboard import TargetBlackboard


class GraspDepthSearchTests(unittest.TestCase):
    def setUp(self):
        self.backend = SimpleNamespace(contact_target='previous', contact_target_object_id=99)
        self.board = TargetBlackboard()
        self.board.observe({'objects': {'Apple': {
            'position': [0., 0., .6], 'quaternion_xyzw': [0., 0., 0., 1.]}}})
        self.resolver = VLMGraspResolver(object(), self.board, self.backend, target_id=7)
        candidates = []
        for depth in GRASP_DEPTHS_M:
            pose = np.diag([1., -1., -1., 1.])
            pose[2, 3] = .7 - depth
            candidates.append({'grasp_index': 0, 'grasp_depth_offset_m': depth,
                'world_from_grasp': pose.tolist(), 'world_from_control': pose.tolist(),
                'pose_source': 'vlm_rgbd_grasp'})
        self.resolved = {'target_id': 7, 'target_name': 'Apple', 'candidates': candidates,
                         'depth_search_pending': True,
                         'visual_estimate': {'depth_selected': False, 'long_axis_aligned': False}}
        self.kin = SimpleNamespace(forward=lambda seed: {'position': [0., 0., 0.]},
            solve_all=Mock(return_value=SimpleNamespace(solutions=[np.zeros(6)], solver='test')))
        self.checker = SimpleNamespace(check=Mock(return_value=SimpleNamespace(in_collision=False)))

    def select(self):
        return self.resolver.select(self.resolved, self.kin, self.checker, np.zeros(6))

    def test_deepest_feasible_wins_and_cache_records_depth(self):
        seen = []
        def validate(resolved, candidate, *args):
            depth = candidate['grasp_depth_offset_m']
            seen.append(depth)
            return {'success': depth <= .01, 'reason': 'grasp_confirmation_failed'}
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate', side_effect=validate):
            result = self.select()
        self.assertEqual(seen, [.03, .028, .026, .024, .022, .020, .018, .016, .014, .012, .010])
        self.assertEqual(result['selected']['grasp_depth_offset_m'], .010)
        self.resolver.remember_selection(result)
        evidence = self.board.visual_grasp_evidence('Apple')
        self.assertTrue(evidence['depth_selected'])
        self.assertEqual(evidence['grasp_depth_offset_m'], .010)
        self.assertEqual(len(evidence['grasp_depth_attempts']), 11)
        self.assertAlmostEqual(self.board.visual_grasp('Apple')[2, 3], .690)
        self.assertEqual(self.backend.contact_target, 'previous')

    def test_endpoint_collision_skips_deep_candidate_before_preflight(self):
        self.checker.check.side_effect = [SimpleNamespace(in_collision=True, contacts=[
            {'geom1': 'pad', 'geom2': 'table', 'distance': -.001}]),
            SimpleNamespace(in_collision=False)]
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate', return_value={'success': True}) as validate:
            result = self.select()
        self.assertEqual(result['selected']['grasp_depth_offset_m'], .028)
        validate.assert_called_once()
        self.assertEqual(result['grasp_depth_attempts'][0]['error']['error_code'], 'TARGET_POSE_COLLISION')

    def test_surface_zero_is_included(self):
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate', side_effect=lambda r, c, *a: {
                'success': c['grasp_depth_offset_m'] == 0., 'reason': 'grasp_confirmation_failed'}):
            result = self.select()
        self.assertEqual(result['selected']['grasp_depth_offset_m'], 0.)
        self.assertEqual(len(result['grasp_depth_attempts']), 16)

    def test_all_fail_reports_attempts_and_does_not_lock_depth(self):
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate', return_value={
                'success': False, 'reason': 'contact_planning', 'rejected': []}), \
             patch('demo.interaction.vlm_grasp.floor_fallback_candidates',
                   return_value=[{**self.resolved['candidates'][-1], 'floor_fallback': {'used': True}}]):
            with self.assertRaises(GraspResolutionError) as caught:
                self.select()
        error = caught.exception.details
        self.assertEqual(error['error_code'], 'GRASP_DEPTH_SEARCH_FAILED')
        self.assertEqual(len(error['details']['depth_attempts']), 16)
        self.assertFalse(error['details']['floor_fallback']['success'])
        from demo.run_demo import step_message
        message = step_message({'skill_name': 'pick', 'parameters': {'target': 7}},
                               success=False, error=error)
        self.assertIn('接触路径规划失败', message)
        self.assertIn('桌面底限兜底失败', message)
        self.assertIn('GRASP_DEPTH_SEARCH_FAILED', message)
        self.assertIsNone(self.board.visual_grasp('Apple'))
        self.assertEqual(self.backend.contact_target, 'previous')
        self.assertEqual(self.backend.contact_target_object_id, 99)

    def test_yaw_is_compared_within_depth_before_trying_shallower(self):
        opposite = deepcopy(self.resolved['candidates'][0])
        opposite['yaw_offset_rad'] = np.pi
        opposite['world_from_control'][0][0] = -1.
        self.resolved['candidates'].insert(1, opposite)
        def validate(resolved, candidate, *args):
            return {'success': bool(candidate.get('yaw_offset_rad')), 'reason': 'grasp_confirmation_failed'}
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate', side_effect=validate):
            result = self.select()
        self.assertEqual(result['selected']['grasp_depth_offset_m'], .03)
        self.assertAlmostEqual(result['selected']['yaw_offset_rad'], np.pi)
        self.assertEqual(len(result['grasp_depth_attempts']), 1)

    def test_floor_fallback_runs_only_after_all_regular_depths_and_is_cached(self):
        fallback = deepcopy(self.resolved['candidates'][-1])
        fallback['world_from_control'][2][3] = .735
        fallback['world_from_grasp'][2][3] = .735
        fallback['grasp_depth_offset_m'] = -.035
        fallback['floor_fallback'] = {'used': True, 'vertical_shift_m': .035,
                                      'outside_search_range': True}
        seen = []
        def validate(r, c, *args):
            seen.append(c['grasp_depth_offset_m'])
            return {'success': 'floor_fallback' in c, 'reason': 'grasp_below_table_floor'}
        with (patch('demo.interaction.vlm_grasp.validate_depth_candidate', side_effect=validate),
              patch('demo.interaction.vlm_grasp.floor_fallback_candidates',
                    return_value=[fallback]) as build):
            result = self.select()
        build.assert_called_once()
        self.assertEqual(seen, list(GRASP_DEPTHS_M) + [-.035])
        self.assertEqual(result['selected']['grasp_depth_offset_m'], -.035)
        self.assertTrue(result['grasp_floor_fallback']['success'])
        self.resolver.remember_selection(result)
        evidence = self.board.visual_grasp_evidence('Apple')
        self.assertTrue(evidence['depth_selected'])
        self.assertTrue(evidence['grasp_floor_fallback']['success'])
        self.assertAlmostEqual(self.board.visual_grasp('Apple')[2, 3], .735)

    def test_regular_depth_success_does_not_build_floor_fallback(self):
        with (patch('demo.interaction.vlm_grasp.validate_depth_candidate', return_value={'success': True}),
              patch('demo.interaction.vlm_grasp.floor_fallback_candidates') as build):
            self.select()
        build.assert_not_called()

    def test_cached_selection_does_not_research(self):
        self.resolved['depth_search_pending'] = False
        self.resolved['candidates'] = [c for c in self.resolved['candidates']
                                       if c['grasp_depth_offset_m'] == 0.]
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate') as validate:
            result = self.select()
        validate.assert_not_called()
        self.assertEqual(result['selected']['grasp_depth_offset_m'], 0.)

    def test_pick_resolution_uses_static_geometry_instead_of_physical_closure(self):
        from atomic_action.motion.common.interaction import DemoInteractionMove
        self.resolved['depth_search_pending'] = False
        candidate = self.resolved['candidates'][0]
        def select(resolved, kin, checker, seed, **kwargs):
            result = kwargs['candidate_validator'](seed, candidate)
            self.assertTrue(result['success'])
            return {**resolved, 'selected': candidate, 'joint_positions': seed.tolist()}
        resolver = SimpleNamespace(resolve=lambda *args: self.resolved, select=select)
        self.checker.backend = self.backend
        adapter = DemoInteractionMove(resolver, object(), self.kin, self.checker, None)
        with (patch('demo.interaction.table_clearance.table_floor', return_value=.6),
              patch('demo.interaction.table_clearance.validate_grasp_geometry',
                    return_value={'success': True}) as geometry,
              patch('control.binding_gripper.BindingGripperController',
                    side_effect=AssertionError('No simulated closure'))):
            adapter._resolve_request({'target': {'type': 'object', 'target_id': 7}},
                {'joints': np.zeros(6)}, prefer_direct=False, closure_request={})
            geometry.assert_called_once()

    def test_static_geometry_rejection_skips_path_and_physical_closure(self):
        self.checker.backend = SimpleNamespace(joint_limits=np.array([[-3., 3.]] * 6))
        planner = Mock()
        planner.plan.return_value = ({'distance': .07}, [])
        with (patch('planning.PickContactPlanner', return_value=planner),
              patch('demo.interaction.table_clearance.table_floor', return_value=.6),
              patch('demo.interaction.table_clearance.validate_grasp_geometry') as geometry,
              patch('control.binding_gripper.BindingGripperController',
                    side_effect=AssertionError('Physical closure must not run'))):
            for reason in ('grasp_below_table_floor', 'grasp_geometry_collision'):
                geometry.return_value = {'success': False, 'reason': reason}
                result = validate_depth_candidate(self.resolved, self.resolved['candidates'][0],
                    np.zeros(6), self.kin, self.checker, np.zeros(6), .003)
                self.assertFalse(result['success'])
                self.assertEqual(result['reason'], reason)
                planner.plan.assert_not_called()
            geometry.return_value = {'success': True, 'validation_mode': 'static_geometry'}
            result = validate_depth_candidate(self.resolved, self.resolved['candidates'][0],
                np.zeros(6), self.kin, self.checker, np.zeros(6), .003)
            self.assertTrue(result['success'])
            self.assertEqual(result['contact_preflight']['distance_m'], .07)
            planner.plan.assert_called_once()


if __name__ == '__main__':
    unittest.main()
