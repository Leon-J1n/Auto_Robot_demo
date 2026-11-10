"""A fresh placement estimate per put, stable within its approach/put cycle."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from demo.interaction.visual_targets import (
    infer_command_point_types, infer_command_place_cycles, infer_visual_targets)
from demo.interaction.vlm_grasp import VLMGraspResolver, GraspResolutionError, PLACEMENT_HEIGHTS_M
from execution.blackboard import TargetBlackboard
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


def command(index, skill, target=12):
    return {'command_id': str(index), 'skill_name': skill, 'parameters': {'target': target}}


class PlacementCycleTests(unittest.TestCase):
    def setUp(self):
        self.commands = [command(1, 'approach'), command(2, 'put'), command(3, 'retreat'),
                         command(4, 'pick', 7), command(5, 'approach'), command(6, 'put'),
                         command(7, 'retreat'), command(8, 'put')]
        self.board = TargetBlackboard()
        self.backend = SimpleNamespace(get_camera_frame=Mock(side_effect=[
            SimpleNamespace(sequence=1), SimpleNamespace(sequence=2), SimpleNamespace(sequence=3)]))
        self.estimator = SimpleNamespace(estimate_frame=Mock(side_effect=[
            {'world_point': {'x': x, 'y': 0., 'z': .6}, 'pixel': {'x': 1, 'y': 1}}
            for x in (.01, .02, .03)]))
        self.resources = SimpleNamespace(scene={'objects': [{
            'object_id_in_scene': 12, 'object_name_in_scene': 'Basket',
            'components': [{'component_type': 'MetadataRef', 'properties': {
                'type': 'model', 'path': '$LIBRARY_SERVER/12'}}]}]})
        self.resolver = VLMGraspResolver(
            SimpleNamespace(base_url='http://unused', get_model=Mock(return_value={})),
            self.board, self.backend, estimator=self.estimator,
            visual_targets=infer_visual_targets(self.commands),
            command_point_types=infer_command_point_types(self.commands),
            command_place_cycles=infer_command_place_cycles(self.commands))
        self.state = {'objects': {'Basket': {'position': [0., 0., .6],
                                           'quaternion_xyzw': [0., 0., 0., 1.]}},
                      'gripper': {'holding': True, 'object_id': 'Apple'}}

    def resolve(self, command_id):
        self.resolver.set_command_context(command_id)
        return self.resolver.resolve(12, self.resources, self.state)

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch('demo.interaction.vlm_grasp._target_crop',
                                 side_effect=lambda frame, *args: (frame, (0, 0))))
        stack.enter_context(patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)))
        return stack

    def test_bindings_separate_each_put_including_direct_put(self):
        self.assertEqual(infer_command_place_cycles(self.commands), {
            '1': '2', '2': '2', '3': '2', '5': '6', '6': '6', '7': '6', '8': '8'})
        self.assertEqual(infer_command_point_types(self.commands)['4'], 'grasp')

    def test_off_center_vlm_point_is_preserved_in_all_height_candidates(self):
        point = {'x': .3, 'y': -.12, 'z': .67}
        self.estimator.estimate_frame.side_effect = None
        self.estimator.estimate_frame.return_value = {
            'world_point': point.copy(), 'pixel': {'x': 8, 'y': 3}}
        with self.patches():
            resolved = self.resolve('1')
        self.assertEqual(resolved['visual_estimate']['world_point'], point)
        self.assertNotIn('floor_pixel', resolved['visual_estimate'])
        for offset, candidate in zip(PLACEMENT_HEIGHTS_M, resolved['candidates']):
            np.testing.assert_allclose(np.asarray(candidate['world_from_grasp'])[:3, 3],
                                       [.3, -.12, .67 + offset])

    def test_each_put_refreshes_but_approach_and_put_share_one_estimate(self):
        with self.patches():
            first = self.resolve('1')
            repeated = self.resolve('1')
            put = self.resolve('2')
            self.assertEqual(self.estimator.estimate_frame.call_count, 1)
            for result in (repeated, put):
                np.testing.assert_allclose(result['candidates'][0]['world_from_grasp'],
                                           first['candidates'][0]['world_from_grasp'])
            self.state['gripper']['object_id'] = 'Banana'
            second = self.resolve('5')
            self.resolve('6')
            self.assertEqual(self.estimator.estimate_frame.call_count, 2)
            self.assertEqual(second['visual_estimate']['placement_cycle_id'], '6')
            self.assertEqual(second['visual_estimate']['held_object'], 'Banana')
            pose = np.asarray(second['candidates'][0]['world_from_grasp'])
            np.testing.assert_allclose(pose[:3, 3], [.02, 0., .6])
            direct_put = self.resolve('8')
            self.assertEqual(direct_put['visual_estimate']['placement_cycle_id'], '8')
            self.assertEqual(self.backend.get_camera_frame.call_count, 3)
            self.assertEqual(self.estimator.estimate_frame.call_count, 3)

    def test_failed_new_perception_does_not_fall_back_to_previous_cycle(self):
        with self.patches():
            self.resolve('1')
            self.estimator.estimate_frame.side_effect = ValueError('VLM unavailable')
            with self.assertRaisesRegex(GraspResolutionError, 'VLM 放置感知失败'):
                self.resolve('5')
            with self.assertRaisesRegex(GraspResolutionError, 'VLM 放置感知失败'):
                self.resolve('6')
        self.assertEqual(self.backend.get_camera_frame.call_count, 3)
        self.assertEqual(self.board.visual_place_evidence('Basket')['placement_cycle_id'], '2')

    def test_same_cycle_still_tracks_destination_motion(self):
        with self.patches():
            first = self.resolve('1')
            self.state['objects']['Basket']['position'][0] += .2
            moved = self.resolve('2')
        expected = np.asarray(first['candidates'][0]['world_from_grasp'])
        expected[0, 3] += .2
        np.testing.assert_allclose(moved['candidates'][0]['world_from_grasp'], expected)
        self.estimator.estimate_frame.assert_called_once()

    def selection_tools(self, minimum_z):
        kin = SimpleNamespace(
            forward=lambda seed: {'position': [0., 0., 0.]},
            solve_all=Mock(side_effect=lambda pose, **kwargs: SimpleNamespace(
                solutions=[np.array([pose['position'][2]])], solver='test')))
        checker = SimpleNamespace(check=Mock(side_effect=lambda q, **kwargs: SimpleNamespace(
            in_collision=q[0] < minimum_z - 1e-9,
            contacts=[{'geom1': 'Apple', 'geom2': 'Basket', 'distance': -.001}]
                     if q[0] < minimum_z - 1e-9 else [])),
            check_path=Mock(side_effect=AssertionError('No duplicate path preflight')))
        return kin, checker

    def test_height_search_is_ascending_and_selected_height_is_shared_without_accumulation(self):
        kin, checker = self.selection_tools(.63)
        with self.patches(), patch('demo.interaction.vlm_grasp.validate_depth_candidate',
                                  side_effect=AssertionError('No grasp/closure preflight for placement')):
            resolved = self.resolve('1')
            self.assertEqual([c['placement_height_offset_m'] for c in resolved['candidates']],
                             list(PLACEMENT_HEIGHTS_M))
            selected = self.resolver.select(resolved, kin, checker, np.zeros(1))
            self.assertEqual(selected['selected']['placement_height_offset_m'], .03)
            self.assertEqual([a['placement_height_offset_m'] for a in selected['placement_height_attempts']],
                             [0., .01, .02, .03])
            np.testing.assert_allclose([call.args[0]['position'][2] for call in kin.solve_all.call_args_list],
                                       [.60, .61, .62, .63])
            self.resolver.remember_selection(selected)
            evidence = self.board.visual_place_evidence('Basket')
            self.assertTrue(evidence['placement_height_selected'])
            self.assertEqual(evidence['placement_height_offset_m'], .03)
            self.assertNotIn('held_bottom_offset_m', evidence)
            self.assertNotIn('placement_clearance_m', evidence)
            for _ in range(2):
                put = self.resolve('2')
                self.assertFalse(put['placement_height_search_pending'])
                self.assertEqual(len(put['candidates']), 1)
                np.testing.assert_allclose(np.asarray(put['candidates'][0]['world_from_grasp'])[:3, 3],
                                           [.01, 0., .63])
                choice = self.resolver.select(put, kin, checker, np.zeros(1))
                self.resolver.remember_selection(choice)
            self.estimator.estimate_frame.assert_called_once()
            self.state['objects']['Basket']['position'][0] += .2
            moved = self.resolve('2')
            np.testing.assert_allclose(np.asarray(moved['candidates'][0]['world_from_grasp'])[:3, 3],
                                       [.21, 0., .63])
            next_cycle = self.resolve('5')
            self.assertTrue(next_cycle['placement_height_search_pending'])
            np.testing.assert_allclose(np.asarray(next_cycle['candidates'][0]['world_from_grasp'])[:3, 3],
                                       [.02, 0., .60])
            self.assertEqual(self.estimator.estimate_frame.call_count, 2)
        checker.check_path.assert_not_called()

    def test_height_search_includes_zero_and_ten_cm_and_stops_at_first_feasible(self):
        for minimum_z, offset, calls in ((.60, 0., 1), (.70, .10, 11)):
            with self.subTest(offset=offset), self.patches():
                kin, checker = self.selection_tools(minimum_z)
                selected = self.resolver.select(self.resolve('1'), kin, checker, np.zeros(1))
                self.assertEqual(selected['selected']['placement_height_offset_m'], offset)
                self.assertEqual(kin.solve_all.call_count, calls)
                checker.check_path.assert_not_called()

    def test_all_height_failures_report_all_attempts_without_locking_height(self):
        from demo.error_messages import describe_error
        kin, checker = self.selection_tools(.71)
        with self.patches():
            with self.assertRaises(GraspResolutionError) as caught:
                self.resolver.select(self.resolve('1'), kin, checker, np.zeros(1))
        error = caught.exception.details
        self.assertEqual(error['error_code'], 'PLACEMENT_HEIGHT_SEARCH_FAILED')
        self.assertEqual(len(error['details']['height_attempts']), 11)
        self.assertIn('未找到可用放置位姿', describe_error(error, '放置失败'))
        for hidden in ('兜底', '随机', '中央'):
            self.assertNotIn(hidden, describe_error(error, '放置失败'))
        self.assertIn('碰撞', describe_error(error, '放置失败'))
        self.assertFalse(self.board.visual_place_evidence('Basket')['placement_height_selected'])
        self.assertEqual(kin.solve_all.call_count, 11)
        checker.check_path.assert_not_called()


class PlacementFallbackTests(unittest.TestCase):
    patches = PlacementCycleTests.patches
    resolve = PlacementCycleTests.resolve

    def setUp(self):
        PlacementCycleTests.setUp(self)
        self.frame = CameraFrame(
            camera_id='global', timestamp=0., sequence=1,
            rgb=np.zeros((81, 81, 3), dtype=np.uint8), depth=np.full((81, 81), .4),
            intrinsics=CameraIntrinsics(81, 81, 100., 100., 40., 40.),
            camera_pose=CameraPose(np.array([0., 0., 1.]), np.eye(3), np.array([1., 0., 0., 0.])))
        self.backend.get_camera_frame.side_effect = None
        self.backend.get_camera_frame.return_value = self.frame
        self.resolver.client.get_model.return_value = {'geometric_properties': {'AABB': [.4, .4, .1]}}
        self.resolver.placement_rng = np.random.default_rng(42)

    def fallback_tools(self, reject):
        kin = SimpleNamespace(
            forward=lambda seed: {'position': [0., 0., 0.]},
            solve_all=Mock(side_effect=lambda pose, **kwargs: SimpleNamespace(
                solutions=[np.array(pose['position'])], solver='test')))
        checker = SimpleNamespace(check=Mock(side_effect=lambda q, **kwargs: SimpleNamespace(
            in_collision=reject(q), contacts=[{'geom1': 'Apple', 'geom2': 'Basket', 'distance': -.001}]
            if reject(q) else [])), check_path=Mock(side_effect=AssertionError('No path preflight')))
        return kin, checker

    def test_successful_vlm_never_generates_random_points(self):
        kin, checker = self.fallback_tools(lambda q: False)
        with self.patches(), patch('demo.interaction.vlm_grasp.random_center_estimates',
                                  side_effect=AssertionError('VLM point should win')):
            selected = self.resolver.select(self.resolve('1'), kin, checker, np.zeros(3))
        self.assertNotIn('placement_center_fallback', selected['visual_estimate'])
        self.assertEqual(kin.solve_all.call_count, 1)

    def test_no_central_depth_reports_perception_failure_without_reusing_old_cycle(self):
        with self.patches():
            self.resolve('1')
            self.frame.depth[:] = np.nan
            self.estimator.estimate_frame.side_effect = ValueError('VLM unavailable')
            with self.assertRaises(GraspResolutionError) as caught:
                self.resolve('5')
        self.assertEqual(caught.exception.details['error_code'], 'PLACEMENT_PERCEPTION_FAILED')
        self.assertIn('无法获得有效放置点', str(caught.exception))
        self.assertNotIn('兜底', str(caught.exception))
        self.assertEqual(self.board.visual_place_evidence('Basket')['placement_cycle_id'], '2')

    def test_vlm_error_uses_random_point_and_refreshes_next_cycle(self):
        self.estimator.estimate_frame.side_effect = ValueError('VLM unavailable')
        kin, checker = self.fallback_tools(lambda q: False)
        with self.patches():
            selected = self.resolver.select(self.resolve('1'), kin, checker, np.zeros(3))
            self.resolver.remember_selection(selected)
            evidence = selected['visual_estimate']['placement_center_fallback']
            self.assertEqual(evidence['trigger'], 'vlm_error')
            self.assertEqual(evidence['vlm_error_type'], 'ValueError')
            self.assertTrue(evidence['attempts'][0]['success'])
            repeated = self.resolve('2')
            np.testing.assert_allclose(repeated['candidates'][0]['world_from_grasp'],
                                       selected['selected']['world_from_grasp'])
            self.estimator.estimate_frame.assert_called_once()
            fresh = self.resolve('5')
            self.assertEqual(fresh['visual_estimate']['placement_cycle_id'], '6')
            self.assertEqual(self.estimator.estimate_frame.call_count, 2)
            self.assertNotEqual(fresh['visual_estimate']['world_point'], selected['visual_estimate']['world_point'])
        checker.check_path.assert_not_called()

    def test_failed_primary_heights_use_random_point_then_keep_selection_for_put(self):
        # VLM X=.01 lies between pixel-grid points; reject it at every height.
        kin, checker = self.fallback_tools(lambda q: abs(q[0] - .01) < 1e-9 or q[2] < .62 - 1e-9)
        with self.patches():
            selected = self.resolver.select(self.resolve('1'), kin, checker, np.zeros(3))
            evidence = selected['visual_estimate']['placement_center_fallback']
            self.assertEqual(evidence['trigger'], 'vlm_point_infeasible')
            self.assertEqual(len(evidence['primary_height_attempts']), 11)
            self.assertEqual(selected['selected']['placement_height_offset_m'], .02)
            self.assertEqual(kin.solve_all.call_count, 14)
            self.resolver.remember_selection(selected)
            put = self.resolve('2')
            np.testing.assert_allclose(put['candidates'][0]['world_from_grasp'],
                                       selected['selected']['world_from_grasp'])
            np.testing.assert_allclose(np.asarray(selected['selected']['world_from_grasp'])[:3, :3],
                                       [[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])
        checker.check_path.assert_not_called()

    def test_all_failures_are_bounded_and_report_every_point_without_double_counting(self):
        from demo.error_messages import describe_error
        for vlm_error, expected_calls in ((False, 44), (True, 33)):
            with self.subTest(vlm_error=vlm_error):
                self.setUp()
                if vlm_error:
                    self.estimator.estimate_frame.side_effect = ValueError('VLM unavailable')
                kin, checker = self.fallback_tools(lambda q: True)
                with self.patches(), self.assertRaises(GraspResolutionError) as caught:
                    self.resolver.select(self.resolve('1'), kin, checker, np.zeros(3))
                error = caught.exception.details
                fallback = error['details']['center_fallback']
                self.assertEqual(len(fallback['attempts']), 3)
                self.assertEqual(kin.solve_all.call_count, expected_calls)
                self.assertEqual(len(error['rejected']), expected_calls)
                self.assertEqual(len({tuple(a['world_point'].values()) for a in fallback['attempts']}), 3)
                self.assertIn(f'{expected_calls} 次', describe_error(error, '失败'))
                self.assertFalse(self.board.visual_place_evidence('Basket')['placement_height_selected'])
                checker.check_path.assert_not_called()


if __name__ == '__main__':
    unittest.main()
