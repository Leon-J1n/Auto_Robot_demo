"""Collision cost controls preserve endpoint checks and spline validation."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np

from control.continuous_trajectory import ContinuousJointPath
from planning.collision_sampling import collision_sampling_settings, spline_collision_samples
from planning.direct_planner import DirectPlanner
from planning.time_parameterization import prepare_continuous_trajectory
from planning.errors import PlanningError
from atomic_action.motion.common.utils.validation import validate_move_request


class CollisionSamplingTests(unittest.TestCase):
    def test_settings_default_override_and_invalid_values(self):
        self.assertEqual(collision_sampling_settings({}), (.02, 2))
        for config in ({'resolution_rad': 0}, {'resolution_rad': True},
                       {'resolution_rad': float('nan')}, {'spline_subdivisions': 1.5},
                       {'spline_subdivisions': 0}, {'spline_subdivisions': False}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_move_request({'target': {'type': 'joint', 'joint_positions': [0.] * 6},
                                      'planning': {'collision_validation': config}})

    def test_local_sampling_keeps_knots_and_bounds_every_joint_increment(self):
        points = np.zeros((8, 6))
        points[:, 0] = np.linspace(0, .8, 8)
        points[:, 1] = [0, 0, 0, .18, .18, .18, .18, .18]
        profile = ContinuousJointPath(points, np.linspace(0, 1, 8))
        sampled = spline_collision_samples(profile, .02, 2)
        self.assertTrue(all(np.any(np.all(np.isclose(sampled, knot, atol=1e-12), axis=1))
                            for knot in points))
        self.assertLessEqual(np.max(np.abs(np.diff(sampled, axis=0))), .02 + 1e-12)
        self.assertLess(len(sampled), len(spline_collision_samples(profile, .01, 4)))

    def request(self):
        return {'motion': {'path_type': 'joint'},
                'constraints': {'velocity_scale': .4, 'acceleration_scale': .8},
                'planning': {'time_parameterization': {'max_velocity': [3.] * 6,
                                                      'max_acceleration': [3.] * 6}}}

    def test_smoothed_path_still_checked_with_configured_resolution_and_collision_rejected(self):
        points = np.zeros((5, 6)); points[:, 0] = np.linspace(0, .4, 5)
        points[2, 1] = .08
        checker = SimpleNamespace(check_path=Mock(return_value=SimpleNamespace(in_collision=False)))
        request = self.request()
        result = prepare_continuous_trajectory({'waypoints': points.tolist()}, request,
                                              collision_checker=checker)
        self.assertEqual(checker.check_path.call_args.kwargs['resolution'], .02)
        diag = result['diagnostics']['time_parameterization']
        self.assertFalse(diag['path_validation_cache_enabled'])
        self.assertEqual(diag['collision_resolution_rad'], .02)
        self.assertFalse(diag['validation_waypoints_identical_to_planner'])
        default_count = diag['validation_waypoint_count']
        request['planning']['collision_validation'] = {'resolution_rad': .01, 'spline_subdivisions': 4}
        strict = prepare_continuous_trajectory({'waypoints': points.tolist()}, request,
                                              collision_checker=checker)
        self.assertGreater(strict['diagnostics']['time_parameterization']['validation_waypoint_count'], default_count)
        self.assertEqual(checker.check_path.call_args.kwargs['resolution'], .01)
        checker.check_path.return_value = SimpleNamespace(in_collision=True, contacts=[])
        with self.assertRaises(PlanningError):
            prepare_continuous_trajectory({'waypoints': points.tolist()}, request, collision_checker=checker)

    def test_direct_planner_checks_endpoint_and_passes_resolution_without_changing_path_density(self):
        checker = SimpleNamespace(check=Mock(return_value=SimpleNamespace(in_collision=False)),
            validate_execution_path=Mock(return_value=SimpleNamespace(in_collision=False)))
        planner = DirectPlanner(collision_checker=checker, joint_limits=np.tile([-3., 3.], (6, 1)))
        source = np.zeros((2, 6)); source[-1, 0] = .12
        request = {'constraints': {'avoid_collision': True, 'safety_distance': .003, 'velocity_scale': .4},
                   'planning': {'collision_validation': {'resolution_rad': .025}}}
        result = planner.plan({'joint_waypoints': source, 'path_type': 'joint'}, request, {})
        checker.check.assert_called_once()
        self.assertEqual(checker.validate_execution_path.call_args.kwargs['resolution'], .025)
        np.testing.assert_array_equal(result['waypoints'][0], source[0])
        np.testing.assert_array_equal(result['waypoints'][-1], source[-1])
        self.assertLessEqual(np.max(np.abs(np.diff(result['waypoints'], axis=0))), .04 + 1e-12)


if __name__ == '__main__':
    unittest.main()
