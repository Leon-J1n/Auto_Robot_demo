"""Random fallback respects target-local bounds and RGB-D camera geometry."""
import unittest
from dataclasses import replace

import numpy as np

from demo.interaction.object_library import rigid_pose
from demo.interaction.placement_fallback import random_center_estimates
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


class CenterPointTests(unittest.TestCase):
    def setUp(self):
        self.frame = CameraFrame(
            camera_id='global', timestamp=4., sequence=7,
            rgb=np.zeros((81, 81, 3), dtype=np.uint8), depth=np.full((81, 81), .4),
            intrinsics=CameraIntrinsics(81, 81, 100., 100., 40., 40.),
            camera_pose=CameraPose(np.array([0., 0., 1.]), np.eye(3), np.array([1., 0., 0., 0.])))
        self.pose = {'position': [.02, -.01, .6],
                     'quaternion_xyzw': [0., 0., np.sin(.35), np.cos(.35)]}
        self.lower = np.array([-.2, -.1, -.05])
        self.upper = -self.lower

    def sample(self):
        return random_center_estimates(self.frame, self.pose, self.lower, self.upper,
                                       np.random.default_rng(19))

    def test_random_points_are_distinct_inside_rotated_central_region_and_back_project(self):
        estimates = self.sample()
        self.assertEqual(len(estimates), 3)
        self.assertEqual(len({tuple(e['pixel'].values()) for e in estimates}), 3)
        transform = rigid_pose(self.pose['position'], self.pose['quaternion_xyzw'])
        for estimate in estimates:
            world = np.array([estimate['world_point'][a] for a in 'xyz'])
            local = (world - transform[:3, 3]) @ transform[:3, :3]
            relative = local[:2] / (self.upper - self.lower)[:2]
            self.assertLessEqual(np.linalg.norm(relative), .20)
            self.assertGreater(np.linalg.norm(relative), .01)
            u, v = estimate['pixel']['x'], estimate['pixel']['y']
            np.testing.assert_allclose(world, [(u - 40) * .004, -(v - 40) * .004, .6])
            self.assertEqual(estimate['sequence'], 7)
        # Injected RNG makes tests reproducible; production uses a fresh RNG.
        self.assertEqual(estimates, self.sample())

    def test_only_valid_visible_depth_is_used_and_exact_center_is_excluded(self):
        self.pose = {'position': [0., 0., .6], 'quaternion_xyzw': [0., 0., 0., 1.]}
        self.frame.depth[:] = np.nan
        self.frame.depth[40, 40] = .4  # Exact center is not an allowed point.
        self.frame.depth[40, 41] = 0.
        self.frame.depth[40, 42] = np.inf
        self.assertEqual(self.sample(), [])
        self.frame.depth[40, 42] = .4
        samples = self.sample()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['pixel'], {'x': 42, 'y': 40})
        self.frame.depth[40, 42] = 1.  # Background outside the destination volume.
        self.assertEqual(self.sample(), [])

    def test_radius_is_twenty_percent_of_model_size(self):
        self.pose = {'position': [0., 0., .6], 'quaternion_xyzw': [0., 0., 0., 1.]}
        self.frame.depth[:] = np.nan
        self.frame.depth[40, 59] = .4  # X=.076, inside radius .20*.4=.08 m.
        self.frame.depth[40, 61] = .4  # X=.084, outside.
        samples = self.sample()
        self.assertEqual([s['pixel'] for s in samples], [{'x': 59, 'y': 40}])
        self.assertEqual(samples[0]['placement_center_fallback']['radius_fraction_xy'], .20)

    def test_camera_rotation_and_cropped_intrinsics_preserve_world_points(self):
        from demo.interaction.vlm_grasp import _target_crop
        # Rotate the camera and target together about world Z and translate them.
        theta = .6
        rotation = np.array([[np.cos(theta), -np.sin(theta), 0.],
                             [np.sin(theta), np.cos(theta), 0.], [0., 0., 1.]])
        offset = np.array([.2, -.1, .05])
        self.frame = replace(self.frame, camera_pose=CameraPose(
            rotation @ self.frame.camera_pose.position + offset,
            rotation, np.array([np.cos(theta / 2), 0., 0., np.sin(theta / 2)])))
        self.pose = {'position': offset + [0., 0., .6],
                     'quaternion_xyzw': [0., 0., np.sin(theta / 2), np.cos(theta / 2)]}
        metadata = {'geometric_properties': {'AABB': [.12, .12, .1]}}
        cropped, origin = _target_crop(self.frame, self.pose, metadata)
        self.assertGreater(origin[0], 0)
        samples = random_center_estimates(cropped, self.pose, np.array([-.06, -.06, -.05]),
                                         np.array([.06, .06, .05]), np.random.default_rng(42))
        self.assertEqual(len(samples), 3)
        for sample in samples:
            u = sample['pixel']['x'] + origin[0]
            v = sample['pixel']['y'] + origin[1]
            expected = rotation @ np.array([(u - 40) * .004, -(v - 40) * .004, .6]) + offset
            np.testing.assert_allclose([sample['world_point'][a] for a in 'xyz'], expected)


if __name__ == '__main__':
    unittest.main()
