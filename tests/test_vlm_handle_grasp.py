"""RGB-D handle orientation and execution integration, with no VLM network calls."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from atomic_action.perception.vlm_estimate import VLMPointEstimator
from demo.interaction.grasp_depth_search import validate_depth_candidate
from demo.interaction.object_library import GraspResolutionError
from demo.interaction.requests import relative_tool_motion
from demo.interaction.visual_targets import infer_visual_targets
from demo.interaction.vlm_grasp import VLMGraspResolver
from execution.blackboard import TargetBlackboard
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose
from sensors.camera_mujoco import MujocoCamera


def observation(*, tilt=0., vertical=False, panel=True, noise=0.):
    """A visible bar 4 cm in front of a planar panel, rendered analytically."""
    size, focal = 161, 400.
    vv, uu = np.mgrid[:size, :size]
    rays = np.stack(((uu - 80) / focal, -(vv - 80) / focal, -np.ones_like(uu)), axis=-1)
    normal = Rotation.from_euler('y', tilt, degrees=True).apply([0., 0., 1.])
    origin = np.array([0., 0., -1.04])
    depth = np.dot(normal, origin) / (rays @ normal)
    bar = (np.abs(uu - 80) <= 4) & (np.abs(vv - 80) <= 29) if vertical else (
        (np.abs(vv - 80) <= 4) & (np.abs(uu - 80) <= 29))
    if not panel:
        depth[:] = np.nan
    depth[bar] = 1.
    if noise:
        depth += np.random.default_rng(4).normal(0., noise, depth.shape)
    # Move the camera to an arbitrary world pose to test extrinsic conversion.
    rotation = Rotation.from_euler('xyz', [62., 12., 31.], degrees=True).as_matrix()
    position = np.array([.4, -.6, 1.3])
    frame = CameraFrame('global', 1., 3, np.zeros((size, size, 3), dtype=np.uint8), depth,
                        CameraIntrinsics(size, size, focal, focal, 80., 80.),
                        CameraPose(position, rotation, np.roll(Rotation.from_matrix(rotation).as_quat(), 1)))
    points = [{'x': .5, 'y': .35}, {'x': .5, 'y': .65}] if vertical else [
        {'x': .35, 'y': .5}, {'x': .65, 'y': .5}]
    response = {'grasp_point': {'x': .5, 'y': .5}, 'confidence': .9,
                'is_elongated': True, 'axis_points': points}
    return frame, response, rotation @ normal


def estimator_for(response):
    create = Mock(return_value=SimpleNamespace(output_text=json.dumps(response)))
    estimator = VLMPointEstimator(SimpleNamespace(responses=SimpleNamespace(create=create)))
    return estimator, create


class HandlePerceptionTests(unittest.TestCase):
    def test_rendered_mujoco_rgbd_recovers_front_grasp(self):
        model = mujoco.MjModel.from_xml_string('''<mujoco>
          <visual><global offwidth="161" offheight="161"/></visual>
          <worldbody>
            <camera name="front" pos="0 -1 .7" quat=".70710678 .70710678 0 0" fovy="40"/>
            <geom type="box" pos="0 .01 .7" size=".2 .01 .15"/>
            <geom type="box" pos="0 -.035 .7" size=".09 .005 .01"/>
          </worldbody></mujoco>''')
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        camera = MujocoCamera(model, data, 'front', width=161, height=161)
        try:
            frame = camera.get_current_frame()
        finally:
            camera.close()
        estimator, _ = estimator_for({'grasp_point': {'x': .5, 'y': .5},
            'is_elongated': True, 'axis_points': [{'x': .4, 'y': .5}, {'x': .6, 'y': .5}]})
        result = estimator.estimate_frame(frame, point_type='handle', target='抽屉把手')
        np.testing.assert_allclose(result['panel_normal_world'], [0., -1., 0.], atol=1e-5)
        np.testing.assert_allclose(np.asarray(result['grasp_rotation_world'])[:, 2], [0., 1., 0.], atol=1e-5)
        self.assertAlmostEqual(result['handle_panel_gap_m'], .04, places=4)

    def test_panel_is_fitted_without_extra_vlm_points(self):
        for tilt, vertical, noise in ((0., False, 0.), (28., True, .0006)):
            with self.subTest(tilt=tilt, vertical=vertical):
                frame, response, normal = observation(tilt=tilt, vertical=vertical, noise=noise)
                estimator, create = estimator_for(response)
                result = estimator.estimate_frame(frame, point_type='handle', target='上层抽屉把手')
                self.assertEqual(result['panel_estimation_source'], 'local_rgbd')
                self.assertEqual(result['orientation_source'], 'vlm_rgbd_panel')
                np.testing.assert_allclose(result['panel_normal_world'], normal, atol=.015)
                rotation = np.asarray(result['grasp_rotation_world'])
                np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), atol=1e-10)
                self.assertAlmostEqual(np.linalg.det(rotation), 1.)
                np.testing.assert_allclose(rotation[:, 2], -normal, atol=.015)
                self.assertGreater(result['handle_panel_gap_m'], .03)
                create.assert_called_once()
                prompt = create.call_args.kwargs['input'][0]['content'][0]['text']
                self.assertIn('grasp_point', prompt)
                self.assertNotIn('panel_points', prompt)
                self.assertIn('confidence=0', prompt)
                self.assertIn('可供夹爪抓取', prompt)

    def test_handle_axis_is_fixed_horizontal_even_with_vertical_references(self):
        frame, response, _ = observation(vertical=True)
        estimator, _ = estimator_for(response)
        result = estimator.estimate_frame(frame, point_type='handle', target='把手')
        expected = np.cross([0., 0., 1.], result['panel_normal_world'])
        expected /= np.linalg.norm(expected)
        np.testing.assert_allclose(result['long_axis_world'], expected, atol=1e-10)

    def test_optional_panel_references_and_invalid_geometry(self):
        frame, response, normal = observation()
        response['panel_points'] = [{'x': .3, 'y': .3}, {'x': .7, 'y': .3},
                                    {'x': .3, 'y': .7}, {'x': .7, 'y': .7}]
        estimator, _ = estimator_for(response)
        result = estimator.estimate_frame(frame, point_type='handle', target='把手')
        self.assertEqual(result['panel_estimation_source'], 'vlm_references')
        np.testing.assert_allclose(result['panel_normal_world'], normal, atol=1e-10)
        response['panel_points'] = [{'x': .2, 'y': .3}, {'x': .5, 'y': .3}, {'x': .8, 'y': .3}]
        estimator, _ = estimator_for(response)
        with self.assertRaisesRegex(ValueError, '共线'):
            estimator.estimate_frame(frame, point_type='handle', target='把手')

    def test_missing_panel_or_axis_cannot_fall_back_to_downward_grasp(self):
        for mode in ('panel', 'flat'):
            with self.subTest(mode=mode):
                frame, response, _ = observation(panel=mode != 'panel')
                if mode == 'axis':
                    response.update(is_elongated=False, axis_points=None)
                if mode == 'flat':
                    frame.depth[:] = 1.
                estimator, _ = estimator_for(response)
                with self.assertRaises(ValueError):
                    estimator.estimate_frame(frame, point_type='handle', target='把手')

    def test_wrong_surface_references_are_rejected(self):
        frame, response, _ = observation()
        response['panel_points'] = [{'x': .3, 'y': .3}, {'x': .7, 'y': .3},
                                    {'x': .3, 'y': .7}, {'x': .7, 'y': .7}]
        frame.depth[110:116, 110:116] += .06
        estimator, _ = estimator_for(response)
        with self.assertRaises(ValueError):
            estimator.estimate_frame(frame, point_type='handle', target='把手')

    def test_missing_or_wrong_axis_references_are_ignored(self):
        for axis_points in (None, [{'x': .1, 'y': .1}, {'x': .9, 'y': .9}]):
            frame, response, _ = observation()
            response['axis_points'] = axis_points
            response['is_elongated'] = False
            estimator, _ = estimator_for(response)
            result = estimator.estimate_frame(frame, point_type='handle', target='把手')
            self.assertEqual(result['axis_status'], 'fixed_horizontal')
            self.assertAlmostEqual(result['long_axis_world'][2], 0.)
            self.assertAlmostEqual(np.dot(result['long_axis_world'], result['panel_normal_world']), 0.)


class HandleResolverTests(unittest.TestCase):
    def make_resolver(self):
        frame, response, _ = observation()
        estimator, create = estimator_for(response)
        obj = {'object_id_in_scene': 9, 'object_name_in_scene': 'Drawer', 'components': [
            {'component_type': 'MetadataRef', 'properties': {'type': 'model', 'object_id': 26}},
            {'component_type': 'Semantic', 'properties': {'semantic_name': 'drawer'}}]}
        resources = SimpleNamespace(scene={'objects': [obj]})
        state = {'objects': {'Drawer': {'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
                             'Drawer__upper': {'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]}}}
        backend = SimpleNamespace(get_camera_frame=Mock(return_value=frame),
                                  contact_target=None, contact_target_object_id=None)
        board = TargetBlackboard()
        surface = frame.camera_pose.position - frame.camera_pose.rotation_matrix[:, 2]
        lower = surface + [.01, -.2, -.2]
        upper = surface + [.3, .2, .2]
        bounds = np.column_stack((upper, lower)).reshape(-1).tolist()
        client = SimpleNamespace(base_url='test', uid=0, get_model=Mock(return_value={
            'geometric_properties': {'AABB': bounds}}))
        targets = [{'target_id': 9, 'point_type': 'grasp', 'camera_id': 'global', 'link': 'upper'}]
        resolver = VLMGraspResolver(client, board, backend, visual_targets=targets, estimator=estimator)
        return resolver, resources, state, frame, create

    def test_candidates_use_panel_normal_and_cached_pose_follows_link(self):
        resolver, resources, state, frame, create = self.make_resolver()
        tool = np.eye(4)
        tool[:3, :3] = Rotation.from_euler('x', 24, degrees=True).as_matrix()
        tool[:3, 3] = [.01, -.02, .14]
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=tool):
            resolved = resolver.resolve(9, resources, state)
            evidence = resolved['visual_estimate']
            self.assertEqual(evidence['grasp_type'], 'handle')
            normal = np.asarray(evidence['surface_normal_world'])
            self.assertEqual(evidence['orientation_source'], 'target_local_aabb')
            self.assertEqual(evidence['grasp_shallow_offset_m'], .015)
            self.assertAlmostEqual(resolved['candidates'][0]['grasp_depth_offset_m'], -.015)
            self.assertAlmostEqual(resolved['candidates'][-1]['grasp_depth_offset_m'], -.015)
            self.assertEqual(len(resolved['candidates']), 2)
            self.assertFalse(resolved['depth_search_pending'])
            self.assertTrue(resolved['fixed_grasp_validation_pending'])
            surface = np.array(list(evidence['world_point'].values()))
            for candidate in resolved['candidates']:
                grasp = np.asarray(candidate['world_from_grasp'])
                np.testing.assert_allclose(grasp[:3, 2], -normal, atol=1e-10)
                np.testing.assert_allclose(grasp[:3, 3], surface - candidate['grasp_depth_offset_m'] * normal)
                np.testing.assert_allclose(np.asarray(candidate['world_from_control']) @ tool, grasp, atol=1e-12)
                self.assertLessEqual(candidate['grasp_depth_offset_m'], evidence['max_grasp_depth_m'])
            selected = resolved['candidates'][1]
            resolver.remember_selection({**resolved, 'selected': selected})
            moved = deepcopy(state)
            moved['objects']['Drawer__upper']['position'][0] += .15
            again = resolver.resolve(9, resources, moved)
        create.assert_called_once()
        self.assertEqual(len(again['candidates']), 1)
        self.assertFalse(again['depth_search_pending'])
        self.assertFalse(again['fixed_grasp_validation_pending'])
        expected = np.asarray(selected['world_from_grasp']).copy()
        expected[0, 3] += .15
        np.testing.assert_allclose(again['candidates'][0]['world_from_grasp'], expected)

    def test_failed_candidates_do_not_invoke_table_height_fallback(self):
        resolver, resources, state, _, _ = self.make_resolver()
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            resolved = resolver.resolve(9, resources, state)
        kin = SimpleNamespace(forward=lambda q: {'position': [0., 0., 0.]},
                              solve_all=lambda *a, **k: SimpleNamespace(solutions=[np.zeros(6)], solver='test'))
        checker = SimpleNamespace(check=lambda *a, **k: SimpleNamespace(in_collision=False))
        with (patch('demo.interaction.vlm_grasp.validate_depth_candidate', return_value={'success': False}),
              patch('demo.interaction.vlm_grasp.floor_fallback_candidates') as fallback):
            with self.assertRaises(GraspResolutionError):
                resolver.select(resolved, kin, checker, np.zeros(6))
        fallback.assert_not_called()
        self.assertIsNone(resolver.backend.contact_target)

    def test_logical_index_zero_still_allows_opposite_jaw_orientation(self):
        resolver, resources, state, _, _ = self.make_resolver()
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            resolved = resolver.resolve(9, resources, state)
        kin = SimpleNamespace(forward=lambda q: {'position': [0., 0., 0.]},
                              solve_all=lambda *a, **k: SimpleNamespace(solutions=[np.zeros(6)], solver='test'))
        checker = SimpleNamespace(check=lambda *a, **k: SimpleNamespace(in_collision=False))
        with patch('demo.interaction.vlm_grasp.validate_depth_candidate', side_effect=lambda r, c, *a: {
                'success': c['yaw_offset_rad'] > 0}):
            choice = resolver.select(resolved, kin, checker, np.zeros(6), grasp_index=0, prefer_direct=False)
        self.assertAlmostEqual(choice['selected']['yaw_offset_rad'], np.pi)

    def test_pick_settings_are_shared_with_preceding_approach(self):
        targets = infer_visual_targets([
            {'skill_name': 'approach', 'parameters': {'target': 9}},
            {'skill_name': 'pick', 'parameters': {'target': 9, 'grasp_type': 'handle',
             'target_description': '上层把手', 'camera_id': 'wrist', 'link': 'upper'}}])
        self.assertEqual(targets, [{'target_id': 9, 'point_type': 'grasp', 'grasp_type': 'handle',
                          'target_description': '上层把手', 'camera_id': 'wrist', 'link': 'upper'}])

    def test_handle_preflight_uses_open_geometry_and_normal_without_table_floor(self):
        rotation = Rotation.from_euler('x', 90, degrees=True).as_matrix()
        goal = np.eye(4)
        goal[:3, :3] = rotation
        candidate = {'world_from_control': goal, 'world_from_grasp': goal}
        checker = SimpleNamespace(backend=SimpleNamespace(joint_limits=np.array([[-3., 3.]] * 6)),
                                  check=Mock(return_value=SimpleNamespace(in_collision=False, contacts=[])))
        planner = SimpleNamespace(plan=Mock(return_value=({'distance': .07}, [])))
        with (patch('planning.PickContactPlanner', return_value=planner),
              patch('demo.interaction.table_clearance.table_floor', side_effect=AssertionError('No floor lookup'))):
            result = validate_depth_candidate({'visual_estimate': {'grasp_type': 'handle'}}, candidate,
                                             np.zeros(6), None, checker, np.zeros(6), .003)
        self.assertTrue(result['success'])
        self.assertIn('gripper_width', checker.check.call_args.kwargs)
        np.testing.assert_allclose(planner.plan.call_args.kwargs['direction'], rotation[:, 2])


class GraspMotionFrameTests(unittest.TestCase):
    def test_pull_uses_calibrated_grasp_axes_instead_of_tcp_axes(self):
        model = mujoco.MjModel.from_xml_string('<mujoco><compiler angle="radian"/><worldbody><site name="grasp_center" '
                                              'euler="1.57079632679 0 0"/></worldbody></mujoco>')
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        backend = SimpleNamespace(model=model, data=data, get_end_effector_pose=lambda: {
            'position': [0., 0., 0.], 'rotation_matrix': np.eye(3)})
        with self.assertRaisesRegex(ValueError, 'frame is no longer supported'):
            relative_tool_motion(backend, {'frame': 'grasp', 'distance': .16,
                                           'direction': [0., 0., -1.]}, 'pull')

    def test_arc_center_uses_world_coordinates(self):
        model = mujoco.MjModel.from_xml_string('<mujoco><worldbody><site name="grasp_center" '
                                              'pos=".1 .2 .3"/></worldbody></mujoco>')
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        backend = SimpleNamespace(model=model, data=data, get_end_effector_pose=lambda: {
            'position': [0., 0., 0.], 'rotation_matrix': np.eye(3)})
        _, motion = relative_tool_motion(backend, {'angle_deg': 20,
            'arc_center': [.1, .3, .3], 'arc_axis': [0., 0., 1.]}, 'pull')
        np.testing.assert_allclose(motion['arc_center'], [.1, .3, .3])


if __name__ == '__main__':
    unittest.main()
