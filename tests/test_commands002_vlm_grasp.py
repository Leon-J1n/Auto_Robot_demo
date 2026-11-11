"""The first commands.002 apple pose comes from RGB-D, then the blackboard."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from demo.interaction.vlm_grasp import (
    VLMGraspResolver, _downward_grasp_rotation,
)
from demo.interaction.visual_targets import infer_visual_targets
from execution.blackboard import TargetBlackboard
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


ROOT = Path(__file__).resolve().parents[1]


class Commands002VLMGraspTests(unittest.TestCase):
    def test_first_pose_is_vertical_vlm_grasp_and_reused(self):
        commands = json.loads((ROOT / 'demo/task_commands/commands.002/commands.002.json').read_text(encoding='utf-8'))
        self.assertEqual(commands['commands'][0]['parameters']['target'], 7)
        self.assertNotIn('visual_grasp', commands)
        visual_targets = infer_visual_targets(commands['commands'])
        self.assertEqual(visual_targets, [{'target_id': 7, 'point_type': 'grasp', 'camera_id': 'global'}])
        apple = {
            'object_id_in_scene': 7, 'object_name_in_scene': 'Apple',
            'components': [{'component_type': 'MetadataRef', 'properties': {
                'type': 'model', 'path': '$LIBRARY_SERVER/1'}}],
        }
        other_apples = [
            {**apple, 'object_id_in_scene': other_id, 'object_name_in_scene': name}
            for other_id, name in ((8, 'Apple2'), (9, 'Apple3'))]
        resources = SimpleNamespace(
            scene={'objects': [apple, *other_apples]},
            robot_object={'components': [{'component_type': 'GripperModel',
                'properties': {'urdf': 'robots/2/robotiq_2f85.urdf'}}]},
            asset=lambda path: ROOT / 'assets' / path,
        )
        rgb = np.zeros((480, 640, 3), dtype=np.uint8)
        rgb[:, 350:400, 0] = 255  # Only the requested apple occupies this region.
        rgb[:, 180:230, 1] = 255
        rgb[:, 480:530, 2] = 255
        frame = CameraFrame('global', 0., 1, rgb, np.ones((480, 640), dtype=np.float32),
            CameraIntrinsics(640, 480, 500., 500., 320., 240.),
            CameraPose(np.array([0., 0., 1.6]), np.eye(3), np.array([1., 0., 0., 0.])))
        backend = SimpleNamespace(
            get_camera_frame=Mock(return_value=frame),
            contact_target=None, contact_target_object_id=None,
        )
        estimator = SimpleNamespace(estimate_frame=Mock(return_value={
            'world_point': {'x': 0.1, 'y': 0., 'z': 0.5998},
            'pixel': {'x': 20, 'y': 20}, 'depth_m': 1.03,
            'axis_points': [{'pixel': {'x': 10, 'y': 20}}],
            'confidence': 0.9,
        }))
        client = SimpleNamespace(base_url='http://unused', uid=0,
            get_model=Mock(return_value={'geometric_properties': {'AABB': [.08, .08, .08]}}))
        board = TargetBlackboard()
        resolver = VLMGraspResolver(client, board, backend, visual_targets=visual_targets, estimator=estimator)
        state = {'simulation_time': 0, 'objects': {
            'Apple': {'position': [0.1, 0., 0.63], 'quaternion_xyzw': [0, 0, 0, 1]},
            'Apple2': {'position': [-.2, 0., .63], 'quaternion_xyzw': [0, 0, 0, 1]},
            'Apple3': {'position': [.35, 0., .63], 'quaternion_xyzw': [0, 0, 0, 1]}}}
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            first = resolver.resolve(7, resources, state)
            second = resolver.resolve(7, resources, state)

        backend.get_camera_frame.assert_called_once_with('global')
        estimator.estimate_frame.assert_called_once()
        self.assertNotIn('instruction', estimator.estimate_frame.call_args.kwargs)
        client.get_model.assert_called_once_with(1)
        cropped = estimator.estimate_frame.call_args.args[0]
        self.assertLess(cropped.intrinsics.width, frame.intrinsics.width)
        self.assertTrue(np.any(cropped.rgb[:, :, 0] == 255))
        self.assertFalse(np.any(cropped.rgb[:, :, 1:]))
        self.assertEqual(first['visual_estimate']['pixel']['x'],
                         20 + first['visual_estimate']['crop_origin_pixel']['x'])
        self.assertEqual(first['visual_estimate']['axis_points'][0]['pixel']['x'],
                         10 + first['visual_estimate']['crop_origin_pixel']['x'])
        grasp = np.asarray(first['candidates'][0]['world_from_grasp'])
        np.testing.assert_allclose(grasp[:3, 3], [0.1, 0., 0.5698])
        np.testing.assert_allclose(grasp[:3, 2], [0, 0, -1])
        np.testing.assert_allclose(grasp[:3, 0], [0, 1, 0])
        np.testing.assert_allclose(second['candidates'][0]['world_from_grasp'], grasp)
        evidence = board.snapshot()['visual_grasps']['Apple']['estimate']
        self.assertFalse(evidence['long_axis_aligned'])
        self.assertEqual(evidence['confidence'], 0.9)
        self.assertEqual(evidence['world_point'], {'x': 0.1, 'y': 0., 'z': 0.5998})
        self.assertFalse(evidence['depth_selected'])
        self.assertEqual(len(first['candidates']), 16)
        self.assertEqual([c['grasp_depth_offset_m'] for c in first['candidates']],
                         [.03, .028, .026, .024, .022, .020, .018, .016, .014, .012,
                          .010, .008, .006, .004, .002, 0.])
        self.assertAlmostEqual(first['candidates'][-1]['world_from_grasp'][2][3], .5998)

    def test_rgbd_axis_controls_yaw_without_model_bounds(self):
        rotation, details = _downward_grasp_rotation({
            'axis_status': 'valid', 'long_axis_world': [0., 1., 0.]})
        np.testing.assert_allclose(rotation[:, 0], [0., 1., 0.], atol=1e-12)
        np.testing.assert_allclose(rotation[:, 2], [0., 0., -1.])
        self.assertTrue(details['long_axis_aligned'])
        self.assertEqual(details['orientation_source'], 'vlm_rgbd_axis')
        self.assertAlmostEqual(np.linalg.det(rotation), 1.)
        self.assertAlmostEqual(np.dot(rotation[:, 1], [0., 1., 0.]), 0.)

    def test_nonelongated_object_uses_fixed_yaw(self):
        rotation, details = _downward_grasp_rotation({
            'axis_status': 'not_elongated', 'axis_reason': '目标没有明显的细长结构'})
        np.testing.assert_allclose(rotation, [[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])
        self.assertAlmostEqual(np.linalg.det(rotation), 1.)
        self.assertFalse(details['long_axis_aligned'])

if __name__ == '__main__':
    unittest.main()
