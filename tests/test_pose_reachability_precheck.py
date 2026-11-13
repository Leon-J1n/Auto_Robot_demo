import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.pose_precheck import ordered_distances
from kinematics.service import KinematicsService
from kinematics.contracts import IKError


class PoseReachabilityPrecheckTests(unittest.TestCase):
    @staticmethod
    def pose(distance):
        result = np.eye(4)
        result[2, 3] = distance
        return result

    def executor(self, check):
        return SimpleNamespace(kinematics=SimpleNamespace(quick_reachability=check),
                               sync=SimpleNamespace(sync=Mock()), backend=object(), checker=object(),
                               pose_search_precheck=[])

    def test_unreachable_distances_are_deferred_and_not_discarded(self):
        check = Mock(side_effect=lambda pose, **kw: pose['position'][2] <= .09)
        executor = self.executor(check)
        actual = list(ordered_distances(executor, [.10, .095, .09, .085], self.pose))
        self.assertEqual(actual, [.09, .085, .10, .095])
        self.assertEqual([d['status'] for d in executor.pose_search_precheck],
                         ['deferred', 'deferred', 'analytic_solution', 'analytic_solution'])

    def test_successful_first_candidate_does_not_precheck_remaining_distances(self):
        check = Mock(return_value=True)
        executor = self.executor(check)
        candidates = ordered_distances(executor, [.10, .095, .09], self.pose, preferred=.09)
        self.assertEqual(next(candidates), .09)
        self.assertEqual(check.call_count, 1)
        self.assertLessEqual(check.call_args.kwargs['timeout'], .03)

    def test_missing_backend_keeps_candidates_and_ignores_out_of_range_preference(self):
        executor = self.executor(Mock(return_value=None))
        self.assertEqual(list(ordered_distances(executor, [.10, .095], self.pose,
                                              preferred=.09)), [.10, .095])

    def test_analytic_precheck_never_uses_numerical_fallback(self):
        service = KinematicsService.__new__(KinematicsService)
        service.registry = SimpleNamespace(names=('eaik', 'jacobian'))
        service.solve_one = Mock(side_effect=IKError('no valid analytic solution'))
        self.assertFalse(service.quick_reachability({'position': [0., 0., 0.]}))
        self.assertEqual(service.solve_one.call_args.kwargs['backend'], 'eaik')
        service.registry = SimpleNamespace(names=('jacobian',))
        service.solve_one.reset_mock()
        self.assertIsNone(service.quick_reachability({}))
        service.solve_one.assert_not_called()
