"""Configured pose tolerances must govern solving and host FK validation."""
from copy import deepcopy
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

from execution.config import load_global_config, validate_config
from kinematics.core.contracts import IKRequest, IKResult
from kinematics.core.service import KinematicsService
from libraries.eaik.adapter import EAIKBackend
from libraries.jacobian_ik.adapter import JacobianBackend


class _Robot:
    limits = np.tile([-1., 1.], (6, 1))
    joint_periods = np.zeros(6)

    def current_joints(self):
        return np.zeros(6)

    def base_transform(self, joints):
        return np.eye(4)

    def forward(self, joints):
        q = np.asarray(joints)
        return {'position': q[:3], 'rotation_matrix': Rotation.from_rotvec(q[3:]).as_matrix()}

    def jacobian(self, joints):
        return np.eye(6)


class IKToleranceTests(unittest.TestCase):
    def service(self):
        config = deepcopy(load_global_config())
        config['planning']['ik_validation'] = {
            'position_tolerance_m': .001, 'orientation_tolerance_rad': .01}
        registry = SimpleNamespace(auto_backends=['eaik', 'jacobian'], resolve=lambda name: name)
        with patch('execution.config.load_global_config', return_value=config):
            return KinematicsService(_Robot(), registry)

    def test_eaik_near_pose_uses_config_without_numerical_fallback(self):
        service = self.service()
        analytic = EAIKBackend(service.robot)
        analytic._solver = SimpleNamespace(
            IK=lambda target: SimpleNamespace(Q=[np.zeros(6)], is_LS=[True]),
            getKinematicFamily=lambda: 'test')
        service._backends['eaik'] = analytic
        target = {'position': [.0008, 0., 0.],
                  'rotation_matrix': Rotation.from_rotvec([0., 0., .005]).as_matrix()}
        result = service.solve_one(target)
        self.assertEqual(result.solver, 'eaik')
        self.assertEqual(result.position_tolerance, .001)
        self.assertEqual(result.orientation_tolerance, .01)
        self.assertNotIn('jacobian', service._backends)

    def request(self, seed=None, **kwargs):
        return IKRequest(np.zeros(3), np.eye(3), np.zeros(6) if seed is None else seed,
                         time.perf_counter() + 1., position_tolerance=.001,
                         orientation_tolerance=.01, **kwargs)

    def test_jacobian_accepts_configured_near_pose_without_further_iteration(self):
        robot = _Robot()
        seed = np.array([.0008, 0., 0., 0., 0., .005])
        with patch.object(robot, 'jacobian', side_effect=AssertionError('Already within tolerance')):
            result = JacobianBackend(robot).solve(self.request(seed))
        np.testing.assert_array_equal(result.solution, seed)
        self.assertEqual(result.position_tolerance, .001)
        self.assertEqual(result.orientation_tolerance, .01)

    def test_service_rejects_outside_tolerance_even_if_plugin_claims_loose_limits(self):
        service = self.service()
        for q in ([.0011, 0., 0., 0., 0., 0.], [0., 0., 0., 0., 0., .011]):
            with self.subTest(q=q):
                accepted, rejected = service._validate(IKResult(
                    solutions=[q], position_tolerance=1., orientation_tolerance=1.), self.request())
                self.assertFalse(accepted)
                self.assertIn('position_error_m=', rejected[0])
                self.assertIn('orientation_error_rad=', rejected[0])

    def test_continuity_and_joint_limits_remain_required(self):
        service = self.service()
        request = self.request(max_joint_step=.1)
        for q, reason in (([.2, 0., 0., 0., 0., 0.], 'joint continuity limit'),
                          ([2., 0., 0., 0., 0., 0.], 'joint limits')):
            with self.subTest(q=q):
                accepted, rejected = service._validate(IKResult(solutions=[q]), request)
                self.assertFalse(accepted)
                self.assertTrue(any(reason in item for item in rejected))

    def test_invalid_global_ik_tolerances_are_rejected(self):
        for key in ('position_tolerance_m', 'orientation_tolerance_rad'):
            for value in (0., -1., float('nan'), float('inf'), True):
                with self.subTest(key=key, value=value):
                    config = deepcopy(load_global_config())
                    config['planning']['ik_validation'][key] = value
                    with self.assertRaises(ValueError):
                        validate_config(config)


if __name__ == '__main__':
    unittest.main()
