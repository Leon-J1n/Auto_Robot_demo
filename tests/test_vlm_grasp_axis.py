"""Offline RGB-D axis projection, retry, yaw selection and cache regression tests."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from atomic_action.perception.vlm_estimate import VLMPointEstimator
from demo.interaction.vlm_grasp import VLMGraspResolver, _downward_grasp_rotation
from execution.blackboard import TargetBlackboard
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


def response(**changes):
    result = {'grasp_point': {'x': .5, 'y': .5}, 'confidence': .9,
              'is_elongated': True,
              'axis_points': [{'x': .2, 'y': .5}, {'x': .8, 'y': .5}]}
    result.update(changes)
    return SimpleNamespace(output_text=json.dumps(result))


class VLMAxisTests(unittest.TestCase):
    def setUp(self):
        self.frame = CameraFrame(
            camera_id='global', timestamp=1., sequence=5,
            rgb=np.zeros((101, 101, 3), dtype=np.uint8), depth=np.ones((101, 101)),
            intrinsics=CameraIntrinsics(101, 101, 500., 500., 50., 50.),
            camera_pose=CameraPose(np.array([0., 0., 1.6]), np.eye(3), np.array([1., 0., 0., 0.])))
        self.create = Mock(return_value=response())
        self.estimator = VLMPointEstimator(SimpleNamespace(responses=SimpleNamespace(create=self.create)))

    def estimate(self):
        return self.estimator.estimate_frame(self.frame, point_type='grasp', target='banana')

    def test_reference_points_project_with_camera_extrinsics(self):
        self.frame.camera_pose.rotation_matrix[:] = [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]]
        result = self.estimate()
        self.assertEqual(result['axis_status'], 'valid')
        np.testing.assert_allclose(result['long_axis_world'], [0., 1., 0.])
        self.assertAlmostEqual(result['axis_length_m'], .12)
        self.assertEqual(result['axis_points'][0]['pixel'], {'x': 20, 'y': 50})
        self.assertAlmostEqual(result['axis_points'][0]['world_point']['y'], -.06)
        self.assertEqual(result['sequence'], 5)
        rotation, _ = _downward_grasp_rotation(result)
        np.testing.assert_allclose(rotation[:, 0], [0., 1., 0.])
        np.testing.assert_allclose(rotation[:, 2], [0., 0., -1.])
        self.assertAlmostEqual(np.linalg.det(rotation), 1.)
        self.assertEqual(self.create.call_count, 1)

    def test_invalid_reference_retries_once_using_same_frame(self):
        self.create.side_effect = [response(axis_points=None), response()]
        result = self.estimate()
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(result['axis_status'], 'valid')
        self.assertEqual(result['axis_retry_count'], 1)
        first, second = self.create.call_args_list
        self.assertEqual(first.kwargs['input'][0]['content'][1], second.kwargs['input'][0]['content'][1])
        self.assertIn('上一次长轴参考点不可用', second.kwargs['input'][0]['content'][0]['text'])

    def test_bad_reference_variants_fall_back_after_one_retry(self):
        cases = [None, [], [{'x': .2, 'y': .5}],
                 [{'x': .2, 'y': .5}, {'x': 1.2, 'y': .5}],
                 [{'x': .49, 'y': .5}, {'x': .51, 'y': .5}]]
        for points in cases:
            with self.subTest(points=points):
                self.create.reset_mock(side_effect=True)
                self.create.return_value = response(axis_points=points)
                result = self.estimate()
                self.assertEqual(self.create.call_count, 2)
                self.assertEqual(result['axis_status'], 'invalid')
                self.assertTrue(result['axis_reason'])
                rotation, details = _downward_grasp_rotation(result)
                self.assertFalse(details['long_axis_aligned'])
                np.testing.assert_allclose(rotation, [[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])

    def test_invalid_endpoint_depths_use_local_mean_without_retry(self):
        for value, column in ((0., 20), (float('nan'), 20), (-1., 80), (float('inf'), 80)):
            with self.subTest(value=value, column=column):
                self.frame.depth[:] = 1.
                self.frame.depth[50, column] = value
                self.create.reset_mock()
                result = self.estimate()
                self.assertEqual(result['axis_status'], 'valid')
                self.assertEqual(self.create.call_count, 1)
                np.testing.assert_allclose(result['long_axis_world'], [1., 0., 0.])
                reference = result['axis_points'][0 if column == 20 else 1]
                self.assertEqual(reference['depth_sampling']['method'], 'neighbour_mean')
                self.assertEqual(reference['depth_sampling']['valid_samples'], 24)
                self.assertEqual(reference['depth_m'], 1.)

    def test_empty_endpoint_neighbourhood_does_not_trigger_reselection(self):
        self.frame.depth[48:53, 18:23] = np.nan
        result = self.estimate()
        self.assertEqual(result['axis_status'], 'invalid')
        self.assertIn('周围 5×5 范围内无有效深度', result['axis_reason'])
        self.assertEqual(result['axis_depth_failure']['pixel'], {'x': 20, 'y': 50})
        self.assertEqual(self.create.call_count, 1)
        self.assertNotIn('axis_retry_count', result)
        self.assertAlmostEqual(result['world_point']['z'], .6)

    def test_depth_along_reference_line_is_not_checked(self):
        for value in (0., float('nan'), 1.2):
            with self.subTest(intermediate_depth=value):
                self.create.reset_mock()
                self.frame.depth[:] = 1.
                # Endpoints and grasp pixel stay valid; this formerly sampled
                # point may now be empty background or at a very different height.
                self.frame.depth[50, 35] = value
                result = self.estimate()
                self.assertEqual(result['axis_status'], 'valid')
                np.testing.assert_allclose(result['long_axis_world'], [1., 0., 0.])
                rotation, orientation = _downward_grasp_rotation(result)
                self.assertTrue(orientation['long_axis_aligned'])
                np.testing.assert_allclose(rotation[:, 0], [1., 0., 0.])
                self.assertEqual(self.create.call_count, 1)
                self.assertNotIn('axis_retry_count', result)

    def test_endpoint_heights_need_not_match_grasp_surface(self):
        self.frame.depth[50, 20] = 1.2
        result = self.estimate()
        self.assertEqual(result['axis_status'], 'valid')
        np.testing.assert_allclose(result['long_axis_world'], [1., 0., 0.])
        self.assertEqual(self.create.call_count, 1)

    def test_reference_line_need_not_intersect_grasp_pixel(self):
        self.create.return_value = response(axis_points=[{'x': .2, 'y': .1}, {'x': .8, 'y': .1}])
        result = self.estimate()
        self.assertEqual(result['axis_status'], 'valid')
        self.assertEqual(self.create.call_count, 1)

    def test_metric_separation_is_used_without_an_extra_pixel_threshold(self):
        frame = CameraFrame(
            camera_id='global', timestamp=1., sequence=5,
            rgb=self.frame.rgb, depth=self.frame.depth,
            intrinsics=CameraIntrinsics(101, 101, 100., 100., 50., 50.),
            camera_pose=self.frame.camera_pose)
        self.create.return_value = response(axis_points=[{'x': .49, 'y': .5}, {'x': .51, 'y': .5}])
        result = self.estimator.estimate_frame(frame, point_type='grasp', target='banana')
        self.assertEqual(result['axis_status'], 'valid')
        self.assertAlmostEqual(result['axis_length_m'], .02)

    def test_too_short_metric_or_vertical_projection_is_rejected(self):
        self.frame.depth[:] = .01
        self.assertEqual(self.estimate()['axis_status'], 'invalid')
        self.frame.depth[:] = 1.
        self.frame.camera_pose.rotation_matrix[:] = [[0., 0., 1.], [0., 1., 0.], [-1., 0., 0.]]
        self.assertIn('水平投影过短', self.estimate()['axis_reason'])

    def test_symmetric_and_legacy_responses_do_not_retry(self):
        for item in (response(is_elongated=False, axis_points=None),
                     SimpleNamespace(output_text='{"x":0.5,"y":0.5}')):
            self.create.reset_mock()
            self.create.return_value = item
            result = self.estimate()
            self.assertIn(result['axis_status'], {'missing', 'not_elongated'})
            self.assertEqual(self.create.call_count, 1)

    def test_bad_retry_preserves_first_valid_grasp(self):
        self.create.side_effect = [response(axis_points=None), SimpleNamespace(output_text='bad JSON')]
        result = self.estimate()
        self.assertEqual(result['axis_status'], 'invalid')
        self.assertIn('重试失败', result['axis_reason'])
        self.assertAlmostEqual(result['world_point']['z'], .6)

    def test_place_remains_point_only(self):
        self.create.return_value = SimpleNamespace(output_text='{"x":0.5,"y":0.5}')
        result = self.estimator.estimate_frame(self.frame, point_type='place', target='basket')
        self.assertNotIn('axis_status', result)
        self.assertEqual(self.create.call_count, 1)

    def test_reversing_reference_order_does_not_change_canonical_yaw(self):
        first = self.estimate()
        self.create.return_value = response(axis_points=[{'x': .8, 'y': .5}, {'x': .2, 'y': .5}])
        second = self.estimate()
        np.testing.assert_allclose(_downward_grasp_rotation(first)[0],
                                   _downward_grasp_rotation(second)[0])


class VLMAxisSelectionTests(unittest.TestCase):
    def test_opposite_yaw_is_selected_then_cached_and_follows_object(self):
        obj = {'object_id_in_scene': 7, 'object_name_in_scene': 'Apple',
               'components': [{'component_type': 'MetadataRef', 'properties': {
                   'type': 'model', 'path': '$LIBRARY_SERVER/1'}}]}
        resources = SimpleNamespace(scene={'objects': [obj]})
        client = SimpleNamespace(base_url='http://unused', get_model=Mock(return_value={
            'geometric_properties': {'AABB': [.08, .08, .08]}}))
        frame = CameraFrame('global', 1., 1, np.zeros((101, 101, 3), dtype=np.uint8),
            np.ones((101, 101)), CameraIntrinsics(101, 101, 500., 500., 50., 50.),
            CameraPose(np.array([0., 0., 1.6]), np.eye(3), np.array([1., 0., 0., 0.])))
        backend = SimpleNamespace(get_camera_frame=Mock(return_value=frame),
            contact_target=None, contact_target_object_id=None)
        estimator = SimpleNamespace(estimate_frame=Mock(return_value={
            'world_point': {'x': 0., 'y': 0., 'z': .7},
            'axis_status': 'valid', 'long_axis_world': [0., 1., 0.]}))
        board = TargetBlackboard()
        resolver = VLMGraspResolver(client, board, backend, target_id=7, estimator=estimator)
        state = {'objects': {'Apple': {'position': [0., 0., .65], 'quaternion_xyzw': [0., 0., 0., 1.]}}}
        with (patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)),
              patch('demo.interaction.vlm_grasp.validate_depth_candidate', return_value={'success': True})):
            resolved = resolver.resolve(7, resources, state)
            self.assertEqual(len(resolved['candidates']), 14)
            first, opposite = [np.asarray(c['world_from_grasp']) for c in resolved['candidates'][:2]]
            np.testing.assert_allclose(first[:3, :2], -opposite[:3, :2])
            np.testing.assert_allclose(first[:3, 2:], opposite[:3, 2:])
            # Only the opposite yaw has a feasible IK solution.
            from kinematics import IKError
            kin = SimpleNamespace(forward=Mock(return_value={'position': [0., 0., 0.]}),
                solve_all=Mock(side_effect=[IKError('no solution'),
                    SimpleNamespace(solutions=[np.zeros(6)], solver='test')]))
            checker = SimpleNamespace(check=Mock(return_value=SimpleNamespace(in_collision=False)))
            selected = resolver.select(resolved, kin, checker, np.zeros(6))
            np.testing.assert_allclose(selected['selected']['world_from_grasp'], opposite)
            resolver.remember_selection(selected)
            cached = resolver.resolve(7, resources, state)
            self.assertEqual(len(cached['candidates']), 1)
            np.testing.assert_allclose(cached['candidates'][0]['world_from_grasp'], opposite)
            resolver.remember_selection({**cached, 'selected': cached['candidates'][0]})
            self.assertAlmostEqual(board.visual_grasp_evidence('Apple')['selected_yaw_offset_rad'], np.pi)
            state['objects']['Apple']['position'][0] += .1
            moved = resolver.resolve(7, resources, state)
            expected = opposite.copy()
            expected[0, 3] += .1
            np.testing.assert_allclose(moved['candidates'][0]['world_from_grasp'], expected)
            state['objects']['Apple']['quaternion_xyzw'] = [0., 0., np.sqrt(.5), np.sqrt(.5)]
            rotated = resolver.resolve(7, resources, state)
            world_rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
            np.testing.assert_allclose(np.asarray(rotated['candidates'][0]['world_from_grasp'])[:3, :3],
                                       world_rotation @ opposite[:3, :3], atol=1e-12)
        estimator.estimate_frame.assert_called_once()
        backend.get_camera_frame.assert_called_once_with('global')
        self.assertTrue(board.visual_grasp_evidence('Apple')['yaw_selected'])
        self.assertEqual(backend.contact_target, None)


if __name__ == '__main__':
    unittest.main()
