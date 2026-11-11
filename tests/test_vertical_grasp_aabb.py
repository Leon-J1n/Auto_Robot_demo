import unittest
from unittest.mock import patch
import numpy as np
from scipy.spatial.transform import Rotation

from demo.interaction.vertical_grasp import estimate_vertical_grasp
from demo.interaction.object_library import GraspResolutionError
from test_vlm_handle_grasp import observation, estimator_for


class VerticalAABBTests(unittest.TestCase):
    def test_rotated_translated_target_all_four_side_normals(self):
        rotation = Rotation.from_euler('z', 57, degrees=True)
        pose = {'position': [1., -2., .6], 'quaternion_xyzw': rotation.as_quat()}
        for axis in (0, 1):
            for sign in (-1, 1):
                local = np.array([.0, .0, .2])
                local[axis] = sign * .21
                world = rotation.apply(local) + pose['position']
                result = estimate_vertical_grasp(dict(zip('xyz', world)), pose,
                                                  [-.2, -.2, 0.], [.2, .2, .4])
                normal = rotation.apply(np.eye(3)[axis] * sign)
                np.testing.assert_allclose(result['surface_normal_world'], normal, atol=1e-12)
                grasp = np.asarray(result['grasp_rotation_world'])
                np.testing.assert_allclose(grasp[:, 2], -normal)
                self.assertAlmostEqual(grasp[2, 0], 0.)
                np.testing.assert_allclose(grasp.T @ grasp, np.eye(3), atol=1e-12)
                self.assertAlmostEqual(np.linalg.det(grasp), 1.)
                np.testing.assert_allclose(result['aabb_estimate']['grasp_point_local'], local, atol=1e-12)

    def test_near_top_still_selects_vertical_side(self):
        result = estimate_vertical_grasp({'x': .19, 'y': 0., 'z': .399},
            {'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
            [-.2, -.2, 0.], [.2, .2, .4])
        self.assertEqual(result['aabb_estimate']['selected_face'], 'x_max')

    def test_corner_ambiguity_is_explicit(self):
        with self.assertRaises(GraspResolutionError) as caught:
            estimate_vertical_grasp({'x': .2, 'y': .2, 'z': .2},
                {'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
                [-.2, -.2, 0.], [.2, .2, .4])
        self.assertEqual(caught.exception.details['error_code'], 'AABB_FACE_AMBIGUOUS')

    def test_vertical_perception_needs_only_rgbd_point(self):
        frame, _, _ = observation(panel=False)
        estimator, create = estimator_for({'grasp_point': {'x': .5, 'y': .5}, 'confidence': .9})
        with patch('atomic_action.perception.vlm_estimate.handle.estimate_handle',
                   side_effect=AssertionError('No point-cloud plane fitting')):
            result = estimator.estimate_frame(frame, point_type='vertical', target='drawer')
        self.assertIn('world_point', result)
        prompt = create.call_args.kwargs['input'][0]['content'][0]['text']
        self.assertNotIn('panel_points', prompt)
        self.assertIn('axis_points', prompt)  # Shared pick schema; vertical geometry ignores optional axes.
        create.assert_called_once()


if __name__ == '__main__':
    unittest.main()
