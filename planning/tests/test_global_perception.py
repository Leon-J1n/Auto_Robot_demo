"""Global observations must preserve operation identity and scene validity."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

from atomic_action.perception.vlm_estimate.estimator import VLMPointEstimator
from demo.interaction.global_perception import current_candidate
from demo.interaction.object_library import GraspResolutionError
from demo.interaction.relative_placement import validate_relative_point


class GlobalPerceptionTests(unittest.TestCase):
    def test_batch_projects_visible_operations_and_skips_occluded(self):
        estimator = VLMPointEstimator.__new__(VLMPointEstimator)
        estimator._request_image = Mock(return_value=(
            '{"2":{"x":0.5,"y":0.5},"6":null,"13":{"x":1,"y":0.5}}', 2.))
        frame = SimpleNamespace(rgb=np.zeros((3, 3, 3), dtype=np.uint8),
            depth=np.ones((3, 3)), intrinsics=SimpleNamespace(fx=1., fy=1., cx=1., cy=1.),
            camera_pose=SimpleNamespace(position=np.zeros(3), rotation_matrix=np.eye(3)),
            camera_id='global', sequence=1, timestamp=0.)
        operations = [dict(command_id=key, point_type='place') for key in ('2', '6', '13')]
        estimates = estimator.estimate_operations(frame, operations)
        self.assertEqual(set(estimates), {'2', '13'})
        self.assertEqual(estimates['13']['world_point'], dict(x=1., y=0., z=-1.))
        self.assertEqual(estimates['2']['observation_source'], 'global_preobservation')
        estimator._request_image.assert_called_once()

    def test_candidate_invalidates_when_reference_moves(self):
        pose = dict(position=[0., 0., 0.], quaternion_xyzw=[0., 0., 0., 1.])
        entry = dict(status='available', target='Tray', anchor='Tray', support_body_id=2,
                     estimate=dict(world_point=dict(x=0., y=0., z=0.)))
        resolver = SimpleNamespace(operation_id='13',
            blackboard=SimpleNamespace(operation_candidates={'13': entry}),
            backend=SimpleNamespace(object_ids={'Tray': 1}), observed_objects={'Tray': pose})
        state = dict(objects={'Tray': deepcopy(pose)}, gripper={})
        state['objects']['Tray']['position'][0] = .01
        self.assertIsNone(current_candidate(resolver, state))
        self.assertEqual(entry['status'], 'scene_changed')

    def test_horizontal_region_rejects_robot_depth_above_support(self):
        region = dict(relation='behind', region_center_world=[0., 0., .7],
            boundary_point_world=[.05, 0., .72], direction_world=[-1., 0., 0.],
            region_radius_m=.02)
        validate_relative_point(dict(world_point=dict(x=0., y=0., z=.7)), region)
        with self.assertRaises(GraspResolutionError):
            validate_relative_point(dict(world_point=dict(x=0., y=0., z=1.)), region)


if __name__ == '__main__':
    unittest.main()
