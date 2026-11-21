"""Geometry preparation, fallback, and explicit backend routing regressions."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from kinematics import KinematicsService
from kinematics.core.contracts import IKError, IKResult


class RoutingTests(unittest.TestCase):
    def service(self, eaik_error=None, ssik_error=None):
        robot = SimpleNamespace(
            limits=np.tile([-2., 2.], (6, 1)), joint_periods=np.zeros(6),
            current_joints=lambda: np.zeros(6),
            forward=lambda q: {"position": np.zeros(3), "rotation_matrix": np.eye(3)})
        backends = {}
        for name, error in (("eaik", eaik_error), ("ssik", ssik_error), ("jacobian", None)):
            backends[name] = SimpleNamespace(
                prepare=Mock(side_effect=error),
                solve=Mock(side_effect=lambda request: IKResult(solutions=[np.zeros(6)])))
        registry = SimpleNamespace(auto_backends=["eaik", "ssik", "jacobian"],
                                   resolve=lambda name: name,
                                   create=Mock(side_effect=lambda name, robot: backends[name]))
        return KinematicsService(robot, registry), backends

    def solve(self, service, **kwargs):
        return service.solve_one(service.robot.forward(np.zeros(6)), **kwargs)

    def test_eaik_preferred_and_preparation_cached(self):
        service, backends = self.service()
        for _ in range(2):
            self.assertEqual(self.solve(service).solver, "eaik")
        backends["eaik"].prepare.assert_called_once()
        backends["ssik"].prepare.assert_called_once()
        backends["ssik"].solve.assert_not_called()
        backends["jacobian"].solve.assert_not_called()

    def test_unsupported_geometry_routes_to_ssik_and_quick_check(self):
        service, backends = self.service(eaik_error=IKError("unsupported geometry"))
        self.assertEqual(self.solve(service).solver, "ssik")
        self.assertTrue(service.quick_reachability(service.robot.forward(np.zeros(6))))
        self.assertEqual(service.automatic_backends(), ["ssik", "jacobian"])
        self.assertIn("eaik", service.automatic_backend_info()["excluded"])
        backends["eaik"].prepare.assert_called_once()
        backends["eaik"].solve.assert_not_called()

    def test_missing_dependencies_use_jacobian(self):
        service, _ = self.service(ImportError("eaik missing"), ImportError("ssik missing"))
        self.assertEqual(self.solve(service).solver, "jacobian")
        self.assertIsNone(service.quick_reachability(service.robot.forward(np.zeros(6))))

    def test_pose_failure_falls_back_without_disabling_geometry(self):
        service, backends = self.service()
        backends["eaik"].solve.side_effect = IKError("no solution")
        self.assertEqual(self.solve(service).solver, "ssik")
        backends["ssik"].solve.side_effect = IKError("no solution")
        self.assertEqual(self.solve(service).solver, "jacobian")
        self.assertEqual(service.automatic_backends(), ["eaik", "ssik", "jacobian"])

    def test_explicit_choice_bypasses_auto_preparation(self):
        service, backends = self.service()
        self.assertEqual(self.solve(service, backend="ssik").solver, "ssik")
        for backend in backends.values():
            backend.prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
