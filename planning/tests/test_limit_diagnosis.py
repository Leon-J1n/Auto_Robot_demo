"""Failure diagnostics must never change physical limits or execute candidates."""
from types import SimpleNamespace
import unittest

import numpy as np

from kinematics.core.limit_diagnosis import diagnose_joint_limits


class LimitDiagnosisTests(unittest.TestCase):
    def service(self):
        def forward(q):
            return dict(position=np.asarray(q[:3]), rotation_matrix=np.eye(3))
        return SimpleNamespace(limits=np.tile([-.5, .5], (6, 1)),
            joint_periods=np.full(6, 2*np.pi), forward=forward,
            robot=SimpleNamespace(joint_names=[f'joint{i+1}' for i in range(6)]),
            ik_validation=dict(position_tolerance_m=.01, orientation_tolerance_rad=.05))

    def test_limit_violation_has_valid_pose_evidence_and_preserves_state(self):
        service = self.service()
        limits = service.limits.copy()
        seed = np.zeros(6)
        result = diagnose_joint_limits(service, dict(position=[.8, 0, 0], rotation_matrix=np.eye(3)), seed)
        self.assertEqual(result['status'], 'limit_violating_candidate')
        self.assertEqual(result['joints'][0]['joint'], 'joint1')
        self.assertGreater(result['joints'][0]['value'], .5)
        self.assertLessEqual(result['position_error_m'], .01)
        self.assertTrue(result['diagnostic_only'])
        np.testing.assert_array_equal(service.limits, limits)
        np.testing.assert_array_equal(seed, np.zeros(6))

    def test_within_limits_candidate_does_not_report_limits(self):
        result = diagnose_joint_limits(self.service(), dict(position=[.2, 0, 0]), np.zeros(6))
        self.assertEqual(result['status'], 'within_limits_candidate')
        self.assertEqual(result['joints'], [])

    def test_timeout_is_inconclusive(self):
        result = diagnose_joint_limits(self.service(), dict(position=[.8, 0, 0]), np.zeros(6), timeout=0)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertTrue(result['budget_exhausted'])
        self.assertEqual(result['joints'], [])


if __name__ == '__main__':
    unittest.main()
