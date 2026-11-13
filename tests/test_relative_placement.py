"""Boundary offsets, live external RGB-D, and shared approach/put semantics."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from scipy.spatial.transform import Rotation

from demo.interaction.relative_placement import (
    placement_parameters, relative_region)
from demo.interaction.visual_targets import (infer_command_place_parameters, infer_command_place_cycles,
                                           infer_command_point_types, infer_visual_targets)
from demo.interaction.vlm_grasp import VLMGraspResolver, GraspResolutionError
from demo.task_commands.converter import SkillCommandConverter
from execution.blackboard import TargetBlackboard
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


def command(ident, skill, **parameters):
    return {'command_id': str(ident), 'skill_name': skill, 'parameters': {'target': 12, **parameters}}


class RelativePlacementGeometryTests(unittest.TestCase):
    def test_boundary_uses_rotated_box_and_robot_base_direction(self):
        pose = {'position': [.3, -.2, .7],
                'quaternion_xyzw': Rotation.from_euler('z', 90, degrees=True).as_quat()}
        resources = SimpleNamespace(robot_object={'components': [{'component_type': 'Transform',
            'properties': {'quaternion_xyzw': Rotation.from_euler('z', 90, degrees=True).as_quat().tolist()}}]})
        # Off-center local box center=(.1, .05, 0), halfsize=(.2, .1, .05).
        region = relative_region(placement_parameters({'relation': 'front', 'distance_m': .1}),
            pose, np.array([-.1, -.05, -.05]), np.array([.3, .15, .05]), resources)
        np.testing.assert_allclose(region['direction_world'], [0., 1., 0.], atol=1e-9)
        np.testing.assert_allclose(region['boundary_point_world'], [.25, .1, .7])
        np.testing.assert_allclose(region['region_center_world'], [.25, .2, .7])
        self.assertEqual(region['reference'], 'target_boundary')

    def test_horizontal_directions_and_nonnegative_distance_validation(self):
        pose = {'position': [0., 0., .7], 'quaternion_xyzw': [0., 0., 0., 1.]}
        for relation, expected in (('front', [.2, 0., .7]), ('behind', [-.2, 0., .7]),
                                   ('left', [0., .3, .7]), ('right', [0., -.3, .7])):
            region = relative_region(placement_parameters({'relation': relation, 'distance_m': .1}),
                pose, np.array([-.1, -.2, -.05]), np.array([.1, .2, .05]), SimpleNamespace())
            np.testing.assert_allclose(region['region_center_world'], expected)
        for params in ({'distance_m': .1}, {'relation': 'diagonal'}, {'relation': 'front', 'distance_m': -.1},
                       {'relation': 'front', 'distance_m': True}, {'relation': 'front', 'distance_m': float('nan')},
                       {'relation': 'front', 'distance_m': '0.1'}, {'relation': []}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                placement_parameters(params)
        self.assertEqual(placement_parameters({}), {'relation': 'at', 'distance_m': 0.})

    def test_vertical_directions_use_oriented_top_and_bottom_in_world_z(self):
        pose = {'position': [0., 0., .7],
                'quaternion_xyzw': Rotation.from_euler('y', 90, degrees=True).as_quat()}
        resources = SimpleNamespace(robot_object={'components': [{'component_type': 'Transform',
            'properties': {'quaternion_xyzw': Rotation.from_euler('x', 30, degrees=True).as_quat().tolist()}}]})
        for relation, expected_z, direction in (('above', 1., [0., 0., 1.]),
                                                ('below', .4, [0., 0., -1.])):
            with self.subTest(relation=relation):
                region = relative_region(placement_parameters({'relation': relation, 'distance_m': .1}),
                    pose, np.array([-.2, -.1, -.05]), np.array([.2, .1, .05]), resources)
                np.testing.assert_allclose(region['direction_world'], direction)
                np.testing.assert_allclose(region['region_center_world'], [0., 0., expected_z])
                self.assertEqual(region['direction_frame'], 'world_vertical')

    def test_put_owns_parameters_only_in_its_own_bound_cycle(self):
        commands = [command(1, 'approach'), command(2, 'put', relation='front', distance_m=.1),
                    command(3, 'retreat'), command(4, 'approach'), command(5, 'put'), command(6, 'retreat')]
        original = deepcopy(commands)
        bindings = infer_command_place_parameters(commands)
        self.assertEqual(bindings['1'], {'relation': 'front', 'distance_m': .1})
        self.assertEqual(bindings['2'], bindings['1'])
        self.assertEqual(bindings['3'], bindings['1'])
        self.assertEqual(bindings['4'], {'relation': 'at', 'distance_m': 0.})
        bindings['1']['relation'] = 'left'
        self.assertEqual(bindings['2']['relation'], 'front')
        self.assertEqual(commands, original)

    def test_converter_preserves_object_target_instead_of_applying_legacy_offset(self):
        converter = SkillCommandConverter({'scene': 'unused.json', 'objects': {}, 'scene_object_ids': ['12']})
        step = converter.convert_command(command(1, 'put', relation='front', distance_m=.1))
        self.assertEqual(step['request']['target'], {'type': 'object', 'target_id': 12})
        for relation in ('above', 'below'):
            with self.subTest(relation=relation):
                step = converter.convert_command(command(1, 'put', relation=relation, distance_m=.1))
                self.assertEqual(step['request']['target'], {'type': 'object', 'target_id': 12})
        with self.assertRaises(ValueError):
            converter.convert_command(command(2, 'put', distance_m=.1))

    def test_vertical_put_parameters_bind_to_its_approach(self):
        commands = [command(1, 'approach'), command(2, 'put', relation='above', distance_m=.1)]
        bindings = infer_command_place_parameters(commands)
        self.assertEqual(bindings['1'], {'relation': 'above', 'distance_m': .1})
        self.assertEqual(bindings['2'], bindings['1'])


class RelativePlacementCycleTests(unittest.TestCase):
    def setUp(self):
        self.commands = [command(1, 'approach'), command(2, 'put', relation='front', distance_m=.1),
                         command(3, 'retreat'), command(4, 'approach'), command(5, 'put', relation='behind', distance_m=.1)]
        self.frame = CameraFrame('global', 0., 1, np.zeros((201, 201, 3), dtype=np.uint8),
            np.full((201, 201), .4), CameraIntrinsics(201, 201, 100., 100., 100., 100.),
            CameraPose(np.array([0., 0., 1.]), np.eye(3), np.array([1., 0., 0., 0.])))
        self.backend = SimpleNamespace(get_camera_frame=Mock(return_value=self.frame))
        self.estimator = SimpleNamespace(estimate_frame=Mock(side_effect=self.estimate))
        metadata = {'geometric_properties': {'AABB': [.2, .2, .1]}}
        self.board = TargetBlackboard()
        self.resolver = VLMGraspResolver(SimpleNamespace(base_url='unused', get_model=Mock(return_value=metadata)),
            self.board, self.backend, estimator=self.estimator, visual_targets=infer_visual_targets(self.commands),
            command_point_types=infer_command_point_types(self.commands),
            command_place_cycles=infer_command_place_cycles(self.commands),
            command_place_parameters=infer_command_place_parameters(self.commands))
        self.resolver.placement_rng = np.random.default_rng(42)
        self.resources = SimpleNamespace(scene={'objects': [{'object_id_in_scene': 12, 'object_name_in_scene': 'Tray',
            'components': [{'component_type': 'MetadataRef', 'properties': {
                'type': 'model', 'path': '$LIBRARY_SERVER/12'}}]}]})
        self.state = {'objects': {'Tray': {'position': [0., 0., .7], 'quaternion_xyzw': [0., 0., 0., 1.]}},
                      'gripper': {'holding': True, 'object_id': 'Apple'}}

    def estimate(self, frame, **kwargs):
        # Both external supports are at z=.6, while the tray center is at z=.7.
        x = .2 if self.resolver.place_parameters['relation'] == 'front' else -.2
        return {'world_point': {'x': x, 'y': 0., 'z': .6}, 'depth_m': .4,
                'pixel': {'x': round(frame.intrinsics.cx + x * 100 / .4), 'y': round(frame.intrinsics.cy)}}

    def resolve(self, ident):
        self.resolver.set_command_context(str(ident))
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            return self.resolver.resolve(12, self.resources, self.state)

    def select(self, resolved, reject=lambda q: q[2] < .62 - 1e-9):
        kin = SimpleNamespace(forward=lambda q: {'position': [0., 0., 1.]},
            solve_all=lambda pose, **kwargs: SimpleNamespace(solutions=[np.array(pose['position'])], solver='test'))
        checker = SimpleNamespace(check=lambda q, **kwargs: SimpleNamespace(in_collision=reject(q), contacts=[]),
                                  check_path=Mock(side_effect=AssertionError('No extra path checks')))
        return self.resolver.select(resolved, kin, checker, np.zeros(3))

    def test_approach_inherits_put_request_redepths_external_region_and_caches_selected_height(self):
        first = self.resolve(1)
        np.testing.assert_allclose(np.asarray(first['candidates'][0]['world_from_grasp'])[:3, 3], [.2, 0., .6])
        self.assertEqual(first['visual_estimate']['pixel'], {'x': 150, 'y': 100})
        self.assertEqual(first['visual_estimate']['relative_placement']['reference'], 'target_boundary')
        call = self.estimator.estimate_frame.call_args
        self.assertIn('前侧', call.kwargs['instruction'])
        self.assertIn('10 cm', call.kwargs['instruction'])
        self.assertEqual(call.kwargs['target'], '指定外侧区域的支撑表面')
        self.assertLess(call.args[0].intrinsics.width, self.frame.intrinsics.width)
        chosen = self.select(first)
        self.resolver.remember_selection(chosen)
        put = self.resolve(2)
        np.testing.assert_allclose(np.asarray(put['candidates'][0]['world_from_grasp'])[:3, 3], [.2, 0., .62])
        self.estimator.estimate_frame.assert_called_once()
        self.backend.get_camera_frame.assert_called_once()
        second = self.resolve(4)
        np.testing.assert_allclose(np.asarray(second['candidates'][0]['world_from_grasp'])[:3, 3], [-.2, 0., .6])
        self.assertEqual(second['visual_estimate']['placement_cycle_id'], '5')
        self.assertEqual(self.backend.get_camera_frame.call_count, 2)

    def test_vlm_failure_or_wrong_region_cannot_return_to_tray_interior(self):
        for failure in (ValueError('offline'), {'world_point': {'x': 0., 'y': 0., 'z': .7}}):
            with self.subTest(failure=failure):
                self.setUp()
                self.estimator.estimate_frame.side_effect = failure if isinstance(failure, Exception) else None
                if isinstance(failure, dict):
                    self.estimator.estimate_frame.return_value = failure
                selected = self.select(self.resolve(1))
                evidence = selected['visual_estimate']
                point = np.array([evidence['world_point'][a] for a in 'xyz'])
                self.assertLessEqual(np.linalg.norm(point[:2] - [.2, 0.]), .02)
                self.assertGreater(point[0], .1)
                self.assertAlmostEqual(point[2], .6)
                self.resolver.remember_selection(selected)
                put = self.resolve(2)
                np.testing.assert_allclose(put['candidates'][0]['world_from_grasp'],
                                           selected['selected']['world_from_grasp'])
                self.estimator.estimate_frame.assert_called_once()

    def test_unobserved_region_fails_explicitly_and_does_not_use_tray_depth(self):
        self.frame.depth[:] = np.nan
        with self.assertRaises(GraspResolutionError) as caught:
            self.resolve(1)
        self.assertEqual(caught.exception.details['error_code'], 'PLACEMENT_REGION_UNOBSERVED')
        self.assertIn('指定相对放置区域', str(caught.exception))
        self.estimator.estimate_frame.assert_not_called()
        self.assertIsNone(self.board.visual_place('Tray'))

    def test_primary_height_failure_searches_only_the_requested_region(self):
        first = self.resolve(1)
        chosen = self.select(first, reject=lambda q: abs(q[0] - .2) < 1e-9 and abs(q[1]) < 1e-9)
        self.assertIn('placement_center_fallback', chosen['visual_estimate'])
        point = chosen['visual_estimate']['world_point']
        self.assertLessEqual(np.linalg.norm([point['x'] - .2, point['y']]), .02)
        self.resolver.remember_selection(chosen)
        self.assertEqual(self.resolve(2)['visual_estimate']['placement_request'],
                         {'relation': 'front', 'distance_m': .1})
        self.estimator.estimate_frame.assert_called_once()

    def test_direct_put_perceives_relative_surface_without_preceding_approach(self):
        direct = self.resolve(2)
        np.testing.assert_allclose(np.asarray(direct['candidates'][0]['world_from_grasp'])[:3, 3], [.2, 0., .6])
        self.assertEqual(direct['visual_estimate']['placement_cycle_id'], '2')
        self.assertEqual(direct['visual_estimate']['placement_request'],
                         {'relation': 'front', 'distance_m': .1})
        self.backend.get_camera_frame.assert_called_once_with('global')

    def test_vertical_put_uses_exact_boundary_offset_without_vlm_or_height_search(self):
        for relation, expected_z in (('above', .85), ('below', .55)):
            with self.subTest(relation=relation):
                self.setUp()
                request = placement_parameters({'relation': relation, 'distance_m': .1})
                self.resolver.command_place_parameters['1'] = request
                self.resolver.command_place_parameters['2'] = request
                approach = self.resolve(1)
                self.assertEqual(len(approach['candidates']), 1)
                self.assertEqual(approach['visual_estimate']['placement_height_candidates_m'], [0.])
                self.assertEqual(approach['visual_estimate']['position_source'], 'target_boundary')
                self.assertAlmostEqual(approach['visual_estimate']['world_point']['z'], expected_z)
                self.backend.get_camera_frame.assert_not_called()
                self.estimator.estimate_frame.assert_not_called()
                chosen = self.select(approach, reject=lambda q: False)
                self.resolver.remember_selection(chosen)
                put = self.resolve(2)
                self.assertAlmostEqual(put['candidates'][0]['world_from_grasp'][2][3], expected_z)
                self.backend.get_camera_frame.assert_not_called()

    def test_unreachable_vertical_put_does_not_fall_back_to_another_point(self):
        self.resolver.command_place_parameters['2'] = placement_parameters(
            {'relation': 'above', 'distance_m': .1})
        resolved = self.resolve(2)
        with self.assertRaises(GraspResolutionError) as caught:
            self.select(resolved, reject=lambda q: True)
        self.assertEqual(caught.exception.details['error_code'], 'PLACEMENT_HEIGHT_SEARCH_FAILED')
        self.assertEqual(len(caught.exception.details['details']['height_attempts']), 1)
        from demo.error_messages import describe_error
        self.assertIn('指定上下相对放置位姿不可行', describe_error(caught.exception.details, '放置失败'))
        self.assertNotIn('11 档', describe_error(caught.exception.details, '放置失败'))
        self.backend.get_camera_frame.assert_not_called()

    def test_real_estimator_uses_chinese_relative_prompt_and_cropped_depth(self):
        from atomic_action.perception.vlm_estimate import VLMPointEstimator
        create = Mock(return_value=SimpleNamespace(output_text='{"x": 0.5, "y": 0.5}'))
        self.resolver.estimator = VLMPointEstimator(SimpleNamespace(responses=SimpleNamespace(create=create)))
        estimate = self.resolve(1)['visual_estimate']
        np.testing.assert_allclose([estimate['world_point'][a] for a in 'xyz'], [.2, 0., .6])
        self.assertEqual(estimate['pixel'], {'x': 150, 'y': 100})
        prompt = create.call_args.kwargs['input'][0]['content'][0]['text']
        self.assertIn('参考物体前侧边界向外 10 cm', prompt)
        self.assertIn('输入图像已裁剪到该外侧区域', prompt)
        create.assert_called_once()

    def test_new_cycle_without_relation_restores_normal_internal_placement(self):
        self.resolve(1)
        self.resolver.command_place_parameters['5'] = placement_parameters({})
        self.estimator.estimate_frame.side_effect = None
        self.estimator.estimate_frame.return_value = {
            'world_point': {'x': .01, 'y': 0., 'z': .7}, 'pixel': {'x': 1, 'y': 1}}
        normal = self.resolve(5)
        np.testing.assert_allclose(np.asarray(normal['candidates'][0]['world_from_grasp'])[:3, 3], [.01, 0., .72])
        self.assertNotIn('relative_placement', normal['visual_estimate'])
        self.assertNotIn('instruction', self.estimator.estimate_frame.call_args.kwargs)

    def test_zero_distance_never_samples_inside_destination(self):
        self.resolver.command_place_parameters['1'] = {'relation': 'front', 'distance_m': 0.}
        self.estimator.estimate_frame.side_effect = ValueError('offline')
        point = self.resolve(1)['visual_estimate']['world_point']
        self.assertGreaterEqual(point['x'], .1 - 1e-6)
        self.assertLessEqual(np.linalg.norm([point['x'] - .1, point['y']]), .02)


if __name__ == '__main__':
    unittest.main()
