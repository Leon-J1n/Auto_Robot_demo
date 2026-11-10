"""Offline checks for VLM point parsing and RGB-D projection."""

import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_action.perception.vlm_estimate import VLMPointEstimator
from atomic_action.perception.vlm_estimate.estimator import InvalidPointDepth
from atomic_action.perception.vlm_estimate.depth import sample_depth
from atomic_action.perception.location.skill import LocateAction
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


class FakeResponses:
    def __init__(self, text):
        self.text = text
        self.request = None

    def create(self, **request):
        self.request = request
        return SimpleNamespace(output_text=self.text)


class VLMEstimateTests(unittest.TestCase):
    def setUp(self):
        self.responses = FakeResponses('{"x": 0.5, "y": 0.5, "confidence": 0.8}')
        self.estimator = VLMPointEstimator(SimpleNamespace(responses=self.responses))
        self.frame = CameraFrame(
            camera_id="global", timestamp=1.0, sequence=2,
            rgb=np.zeros((3, 3, 3), dtype=np.uint8),
            depth=np.full((3, 3), 2.0, dtype=np.float32),
            intrinsics=CameraIntrinsics(3, 3, 2.0, 2.0, 1.0, 1.0),
            camera_pose=CameraPose(
                position=np.array([1.0, 2.0, 3.0]),
                rotation_matrix=np.eye(3),
                quaternion_wxyz=np.array([1.0, 0.0, 0.0, 0.0]),
            ),
        )

    def test_center_point_projects_to_world(self):
        result = self.estimator.estimate_frame(self.frame, point_type="grasp", target="apple")
        self.assertEqual(result["world_point"], {"x": 1.0, "y": 2.0, "z": 1.0})
        self.assertEqual(result["pixel"], {"x": 1, "y": 1})
        self.assertEqual(result["confidence"], 0.8)
        self.assertIn("input_image", str(self.responses.request["input"]))
        prompt = self.responses.request["input"][0]["content"][0]["text"]
        task = json.loads((Path(__file__).resolve().parents[1] /
                           'atomic_action/perception/vlm_estimate/prompts/vlm_pick.json').read_text(encoding='utf-8'))
        self.assertIn('目标物体：苹果', prompt)
        self.assertIn(task['description'], prompt)
        self.assertIn(f"输出结构：{task['respond']}", prompt)

    def test_place_prompt_and_off_center_projection(self):
        self.responses.text = '{"x": 1, "y": 0}'
        result = self.estimator.estimate_frame(self.frame, point_type="place", target="tray")
        self.assertEqual(result["world_point"], {"x": 2.0, "y": 3.0, "z": 1.0})
        prompt = self.responses.request["input"][0]["content"][0]["text"]
        self.assertIn('目标物体：托盘', prompt)
        self.assertIn('内部可见底面', prompt)
        self.assertIn('只返回 JSON', prompt)

    def test_invalid_depth_uses_local_mean_without_reselection(self):
        for point_type in ('grasp', 'place'):
            for value in (0., -1., np.nan, np.inf, -np.inf):
                with self.subTest(point_type=point_type, depth=value):
                    self.frame.depth[:] = [[1., np.nan, 3.], [0., value, -1.],
                                           [np.inf, -np.inf, 5.]]
                    before = self.frame.depth.copy()
                    create = Mock(return_value=SimpleNamespace(output_text=self.responses.text))
                    self.responses.create = create
                    result = self.estimator.estimate_frame(self.frame, point_type=point_type,
                        target='banana', instruction='保持顶部相机视角')
                    self.assertEqual(create.call_count, 1)
                    self.assertEqual(result['pixel'], {'x': 1, 'y': 1})
                    self.assertEqual(result['world_point'], {'x': 1., 'y': 2., 'z': 0.})
                    self.assertEqual(result['depth_m'], 3.)
                    self.assertEqual(result['depth_sampling']['method'], 'neighbour_mean')
                    self.assertEqual(result['depth_sampling']['valid_samples'], 3)
                    self.assertEqual(result['camera_id'], 'global')
                    self.assertNotIn('depth_retry_count', result)
                    np.testing.assert_array_equal(self.frame.depth, before)

    def test_empty_neighbourhood_fails_without_reselection(self):
        self.frame.depth[:] = np.nan
        create = Mock(return_value=SimpleNamespace(output_text=self.responses.text))
        self.responses.create = create
        with self.assertRaisesRegex(InvalidPointDepth, '周围 5×5 范围内无有效深度') as caught:
            self.estimator.estimate_frame(self.frame, point_type='grasp', target='banana')
        self.assertEqual(create.call_count, 1)
        error = caught.exception.details
        self.assertEqual(error['error_code'], 'VLM_DEPTH_INVALID')
        self.assertEqual(error['failed_stage'], 'perception')
        details = error['details']
        self.assertEqual(details['pixel'], {'x': 1, 'y': 1})
        self.assertEqual(details['depth_state'], 'NaN')
        self.assertEqual(details['image_size'], {'width': 3, 'height': 3})
        self.assertEqual(details['depth_sampling']['valid_samples'], 0)
        self.assertEqual(details['camera_id'], 'global')
        self.assertEqual(details['sequence'], 2)

    def test_valid_selected_depth_is_kept(self):
        self.frame.depth[:] = 100.
        self.frame.depth[1, 1] = 2.
        result = self.estimator.estimate_frame(self.frame, point_type='grasp', target='banana')
        self.assertEqual(result['depth_m'], 2.)
        self.assertEqual(result['depth_sampling']['method'], 'pixel')

    def test_neighbourhood_is_bounded_and_clipped_at_image_edges(self):
        depth = np.full((9, 9), np.nan)
        depth[0, 1], depth[2, 2] = 1., 3.
        depth[3, 3], depth[-1, -1] = 99., 99.
        z, sampling = sample_depth(depth, 0, 0)
        self.assertEqual(z, 2.)
        self.assertEqual(sampling['bounds_xyxy'], [0, 0, 3, 3])
        self.assertEqual(sampling['valid_samples'], 2)
        depth[:] = np.nan
        depth[0, 0] = 1.
        with self.assertRaises(InvalidPointDepth):
            sample_depth(depth, 4, 4)

    def test_local_mean_projects_along_original_off_center_ray(self):
        self.responses.text = '{"x":1,"y":0}'
        self.frame.depth[:] = 4.
        self.frame.depth[0, 2] = np.nan
        result = self.estimator.estimate_frame(self.frame, point_type='place', target='tray')
        self.assertEqual(result['pixel'], {'x': 2, 'y': 0})
        self.assertEqual(result['world_point'], {'x': 3., 'y': 4., 'z': -1.})

    def test_invalid_coordinates_or_response_do_not_retry(self):
        for response, reason in (('{"x":1.2,"y":0.5}', 'normalized'), ('not JSON', 'JSON')):
            with self.subTest(response=response):
                self.responses.create = Mock(return_value=SimpleNamespace(output_text=response))
                with self.assertRaisesRegex(ValueError, reason):
                    self.estimator.estimate_frame(self.frame, point_type='grasp', target='banana')
                self.assertEqual(self.responses.create.call_count, 1)

    def test_locate_strategy_uses_requested_camera_and_point_type(self):
        class Runtime:
            def __init__(self, frame):
                self.frame = frame
                self.requested_camera = None

            def get_context(self, request):
                return {}

            def get_camera_frame(self, camera_id):
                self.requested_camera = camera_id
                return self.frame

        runtime = Runtime(self.frame)
        action = LocateAction(robot_runtime=runtime)
        action.strategies["vlm_estimate"].estimator = self.estimator
        result = action.execute({
            "target": {"type": "place", "object": "tray"},
            "localization": {"strategy": "vlm_estimate"},
            "strategy_params": {"camera_id": "wrist"},
        })
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(runtime.requested_camera, "wrist")
        self.assertEqual(result["localization_result"]["estimate"]["point_type"], "place")
        self.assertEqual(result["localization_result"]["world_point"]["z"], 1.0)

    def test_locate_rejects_non_world_output(self):
        result = LocateAction(robot_runtime=object()).execute({
            "target": {"type": "grasp", "object": "apple"},
            "localization": {"strategy": "vlm_estimate", "output_frame": "base"},
        })
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
