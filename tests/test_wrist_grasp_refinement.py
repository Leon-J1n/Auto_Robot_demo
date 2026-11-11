"""Wrist refinement geometry, cache transaction, and approach integration."""
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
import numpy as np
import test_vlm_handle_grasp as fixtures
import test_approach_move_to_fallback as approach_fixtures
from demo.interaction.wrist_refinement import refine_surface, refinement_error
from demo.interaction.object_library import GraspResolutionError


class WristRefinementTests(unittest.TestCase):
    def test_default_vlm_client_bounds_network_wait_without_retries(self):
        with patch('openai.OpenAI') as client:
            estimator = fixtures.VLMPointEstimator()
            estimator._get_client()
        self.assertEqual(client.call_args.kwargs['timeout'], 45.)
        self.assertEqual(client.call_args.kwargs['max_retries'], 0)

    def test_full_frame_preserves_rgbd_and_camera_calibration(self):
        frame, response, _ = fixtures.observation()
        estimator, _ = fixtures.estimator_for(response)
        surface = frame.camera_pose.position - frame.camera_pose.rotation_matrix[:, 2]
        with patch.object(estimator, 'estimate_frame', wraps=estimator.estimate_frame) as call:
            estimate, diagnostics = refine_surface(estimator, frame, surface, 'handle')
        self.assertIs(call.call_args.args[0], frame)
        self.assertNotIn('投影', call.call_args.kwargs['instruction'])
        pixel = estimate['pixel']
        ray = np.array([(pixel['x'] - 80) / 400, -(pixel['y'] - 80) / 400, -1.])
        expected = frame.camera_pose.position + frame.camera_pose.rotation_matrix @ ray
        np.testing.assert_allclose(list(estimate['world_point'].values()), expected)
        self.assertLessEqual(abs(pixel['x'] - 80), 1)
        self.assertLessEqual(abs(pixel['y'] - 80), 1)
        self.assertEqual(diagnostics['image_region'], 'full_frame')
        self.assertNotIn('crop_origin_pixel', estimate)

    def test_uncertain_or_distant_detection_is_rejected(self):
        frame, _, _ = fixtures.observation()
        surface = frame.camera_pose.position - frame.camera_pose.rotation_matrix[:, 2]
        for confidence, delta in ((.2, [0, 0, 0]), (.9, [.11, 0, 0]), (.9, [0, 0, .06])):
            point = surface + delta
            estimator = Mock()
            estimator.estimate_frame.return_value = {'confidence': confidence,
                'world_point': dict(zip('xyz', point)), 'pixel': {'x': 1, 'y': 1}}
            with self.assertRaises(GraspResolutionError):
                refine_surface(estimator, frame, surface, 'handle')

    def test_relaxed_correction_accepts_eight_cm_and_four_cm_height(self):
        frame, _, _ = fixtures.observation()
        surface = frame.camera_pose.position - frame.camera_pose.rotation_matrix[:, 2]
        point = surface + [.08, 0, .04]
        estimator = Mock()
        estimator.estimate_frame.return_value = {'confidence': .9,
            'world_point': dict(zip('xyz', point)), 'pixel': {'x': 1, 'y': 1}}
        estimate, diagnostics = refine_surface(estimator, frame, surface, 'handle')
        np.testing.assert_allclose(list(estimate['world_point'].values()), point)
        self.assertAlmostEqual(diagnostics['correction_distance_m'], np.linalg.norm([.08, 0, .04]))

    def test_planar_prompt_uses_grasp_without_handle_instruction(self):
        frame, response, _ = fixtures.observation()
        response.update(is_elongated=False, axis_points=None)
        estimator, _ = fixtures.estimator_for(response)
        surface = frame.camera_pose.position - frame.camera_pose.rotation_matrix[:, 2]
        with patch.object(estimator, 'estimate_frame', wraps=estimator.estimate_frame) as call:
            estimate, _ = refine_surface(estimator, frame, surface, 'apple', vertical=False)
        self.assertEqual(call.call_args.kwargs['point_type'], 'grasp')
        self.assertNotIn('横杆', call.call_args.kwargs['instruction'])
        self.assertIn('同一目标物体', call.call_args.kwargs['instruction'])
        self.assertEqual(estimate['point_type'], 'grasp')

    def test_planar_refinement_rebuilds_depths_and_keeps_selected_yaw(self):
        resolver, resources, state, _, _ = fixtures.HandleResolverTests().make_resolver()
        resolver.visual_targets[('9', 'grasp')]['grasp_type'] = 'top_down'
        state['joints'] = [.1] * 6
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            resolved = resolver.resolve(9, resources, state)
        resolved['selected'] = resolved['candidates'][1]
        resolver.remember_selection(resolved)
        old = np.asarray(resolved['selected']['world_from_grasp'])
        old_surface = old[:3, 3] - resolved['selected']['grasp_depth_offset_m'] * old[:3, 2]
        surface = old_surface + [.005, 0, .002]
        estimate = {'world_point': dict(zip('xyz', surface)), 'confidence': .9,
                    'axis_status': 'not_elongated', 'long_axis_world': [1, 0, 0]}
        def select(candidate, *args, **kwargs):
            self.assertFalse(candidate['fixed_grasp_validation_pending'])
            self.assertTrue(candidate['depth_search_pending'])
            self.assertEqual(len(candidate['candidates']), 16)
            self.assertEqual(candidate['visual_estimate']['axis_status'], resolved['visual_estimate']['axis_status'])
            for item in candidate['candidates']:
                pose = np.asarray(item['world_from_grasp'])
                np.testing.assert_allclose(pose[:3, :3], old[:3, :3])
                np.testing.assert_allclose(pose[:3, 3], surface + item['grasp_depth_offset_m'] * pose[:3, 2])
            return dict(candidate, selected=candidate['candidates'][3])
        with (patch('demo.interaction.wrist_refinement.refine_surface', return_value=(estimate, {})) as detect,
              patch.object(resolver, 'select', side_effect=select)):
            refined = resolver.refine_after_approach(resolved, resources, state, None, None)
        np.testing.assert_allclose(detect.call_args.args[2], old_surface)
        self.assertFalse(detect.call_args.kwargs['vertical'])
        resolver.backend.get_camera_frame.reset_mock()
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            cached = resolver.resolve(9, resources, state)
        np.testing.assert_allclose(cached['candidates'][0]['world_from_grasp'], refined['selected']['world_from_grasp'])
        resolver.backend.get_camera_frame.assert_not_called()

    def prepare(self):
        resolver, resources, state, _, _ = fixtures.HandleResolverTests().make_resolver()
        state['joints'] = [.1] * 6
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            resolved = resolver.resolve(9, resources, state)
        resolved['selected'] = resolved['candidates'][1]
        resolver.remember_selection(resolved)
        return resolver, resources, state, resolved

    def test_validated_position_preserves_rotation_offset_and_pick_cache(self):
        resolver, resources, state, resolved = self.prepare()
        old = np.asarray(resolved['selected']['world_from_grasp'])
        surface = old[:3, 3] + .015 * old[:3, 2] + [0, .005, 0]
        estimate = {'world_point': dict(zip('xyz', surface)), 'confidence': .9}
        def select(candidate, *args, **kwargs):
            self.assertTrue(candidate['fixed_grasp_validation_pending'])
            self.assertFalse(candidate['depth_search_pending'])
            self.assertEqual(args[2], state['joints'])
            return dict(candidate, selected=candidate['candidates'][0])
        with (patch('demo.interaction.wrist_refinement.refine_surface', return_value=(estimate, {})),
              patch.object(resolver, 'select', side_effect=select)):
            refined = resolver.refine_after_approach(resolved, resources, state, None, None)
        pose = np.asarray(refined['selected']['world_from_grasp'])
        np.testing.assert_allclose(pose[:3, :3], old[:3, :3])
        np.testing.assert_allclose(pose[:3, 3], surface - .015 * pose[:3, 2])
        resolver.backend.get_camera_frame.assert_called_with('wrist')
        resolver.backend.get_camera_frame.reset_mock()
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            cached = resolver.resolve(9, resources, state)
        np.testing.assert_allclose(cached['candidates'][0]['world_from_grasp'], pose)
        resolver.backend.get_camera_frame.assert_not_called()

    def test_failed_validation_keeps_cache_for_pick(self):
        resolver, resources, state, resolved = self.prepare()
        before = resolver.blackboard.visual_grasp('Drawer').copy()
        estimate = {'world_point': dict(zip('xyz', before[:3, 3])), 'confidence': .9}
        with (patch('demo.interaction.wrist_refinement.refine_surface', return_value=(estimate, {})),
              patch.object(resolver, 'select', side_effect=ValueError('collision'))):
            fallback = resolver.refine_after_approach(resolved, resources, state, None, None)
        np.testing.assert_allclose(resolver.blackboard.visual_grasp('Drawer'), before)
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            cached = resolver.resolve(9, resources, state)
        np.testing.assert_allclose(cached['candidates'][0]['world_from_grasp'], before)
        self.assertEqual(fallback['visual_estimate']['wrist_refinement']['status'], 'fallback_to_coarse')
        failure = fallback['visual_estimate']['wrist_refinement']['error']['details']
        self.assertEqual(failure['substage'], 'validation')
        self.assertIn('vlm', failure['timing_s'])

    def test_perception_failure_returns_original_pose_without_validation(self):
        for error in (refinement_error('偏移过大'), TimeoutError('Request timed out.')):
            resolver, resources, state, resolved = self.prepare()
            before = resolver.blackboard.visual_grasp('Drawer').copy()
            with (patch('demo.interaction.wrist_refinement.refine_surface', side_effect=error),
                  patch.object(resolver, 'select') as select):
                fallback = resolver.refine_after_approach(resolved, resources, state, None, None)
            select.assert_not_called()
            np.testing.assert_allclose(fallback['selected']['world_from_grasp'], before)
            np.testing.assert_allclose(resolver.blackboard.visual_grasp('Drawer'), before)
            self.assertEqual(fallback['visual_estimate']['wrist_refinement']['status'], 'fallback_to_coarse')

    def test_runner_refines_only_after_success_and_records_failure(self):
        for success, refinement_fails, method in ((True, False, 'vertical_aabb'),
                (False, False, 'vertical_aabb'), (True, True, 'vertical_aabb'),
                (True, False, 'planar'), (False, False, 'planar'), (True, True, 'planar')):
            fixture = approach_fixtures.ApproachMoveToFallbackTests()
            fixture.setUp()
            executor = fixture.executor
            executor.report = {}
            executor.backend.set_status = Mock()
            fixture.resolved['visual_estimate'] = {'grasp_estimation_method': method}
            fixture.resolved['selected']['pose_source'] = 'vlm_rgbd_grasp'
            refined = deepcopy(fixture.resolved)
            refined['visual_estimate']['wrist_refinement'] = {'status': 'validated'}
            executor.resolver.refine_after_approach = Mock(return_value=refined)
            if refinement_fails:
                executor.resolver.refine_after_approach.side_effect = refinement_error('occluded')
            with patch('atomic_skill.runner.PoseSearchExecutor') as search:
                search.return_value.run.return_value = {'success': success}
                if refinement_fails:
                    with self.assertRaises(GraspResolutionError):
                        fixture.run_approach()
                else:
                    result = fixture.run_approach()
                    self.assertEqual(result['success'], success)
            self.assertEqual(executor.resolver.refine_after_approach.call_count, int(success))
            if success:
                self.assertIs(executor.resolver.refine_after_approach.call_args.args[2], executor.sync.sync.return_value)


if __name__ == '__main__':
    unittest.main()
