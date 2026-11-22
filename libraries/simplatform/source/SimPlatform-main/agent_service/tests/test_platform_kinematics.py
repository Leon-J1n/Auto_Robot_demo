from __future__ import annotations

import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from agent_service.skills.platform_runtime.kinematics import PlatformKinematics
from agent_service.runtime.kinematics import IKFAST_MODULE as LIVE_IKFAST_MODULE


class _Robot:
    def __init__(self, *, mappings: bool = False) -> None:
        self.calls = []
        self.mappings = mappings

    def kinematics(self, joints, links, **kwargs):
        self.calls.append((joints, links, kwargs))
        transform = ({"translation": (0.1, 0.2, 0.3),
                      "rotation_wxyz": (1.0, 0.0, 0.0, 0.0)}
                     if self.mappings else SimpleNamespace(
                         translation=(0.1, 0.2, 0.3),
                         rotation_wxyz=(1.0, 0.0, 0.0, 0.0)))
        return SimpleNamespace(
            transforms=((transform,),),
            jacobians=(np.zeros((6, 6)),),
        )


class _Runtime:
    def __init__(self) -> None:
        self.robot = _Robot()
        self.qpos_indices = np.arange(6)
        self.dof_indices = np.arange(6)
        self.joint_limits = np.tile((-2.0 * np.pi, 2.0 * np.pi), (6, 1))
        self.end_effector_site = "robotiq_2f85_pinch"
        self.end_effector_link = object()

    def expand_joints(self, joints, _base=None):
        return np.asarray(joints, dtype=float)

    def full_joint_positions(self):
        return np.zeros(6)

    def link_pose(self, name):
        return {"position": np.zeros(3), "rotation_matrix": np.eye(3)}

    def get_end_effector_pose(self):
        return self.link_pose("tcp")


class PlatformKinematicsTests(unittest.TestCase):
    def test_vendored_ikfast_module_is_importable_from_service_package(self) -> None:
        module = importlib.import_module("agent_service.libraries.ikfast.ur5e_backend")
        self.assertTrue(callable(module.solve))
        self.assertTrue(callable(module.forward))

    def test_platform_kinematics_passes_link_name_instead_of_proxy(self) -> None:
        runtime = _Runtime()
        kinematics = PlatformKinematics(runtime)

        kinematics.jacobian(np.zeros(6))

        _, links, kwargs = runtime.robot.calls[-1]
        self.assertEqual(links, (runtime.end_effector_site,))
        self.assertEqual(kwargs["jacobian_link"], runtime.end_effector_site)

    def test_platform_kinematics_accepts_live_mapping_transforms(self) -> None:
        runtime = _Runtime()
        runtime.robot = _Robot(mappings=True)

        pose = PlatformKinematics(runtime).forward(np.zeros(6))

        np.testing.assert_allclose(pose["position"], (0.1, 0.2, 0.3))
        np.testing.assert_allclose(pose["rotation_matrix"], np.eye(3))

    def test_vendored_ikfast_module_uses_service_package(self) -> None:
        runtime = _Runtime()
        kinematics = PlatformKinematics(runtime)
        imported = []

        def fake_import(name):
            imported.append(name)
            return SimpleNamespace(solve=lambda _position, _rotation: [np.zeros(6)])

        with patch(
            "agent_service.skills.platform_runtime.kinematics.importlib.import_module",
            side_effect=fake_import,
        ):
            solutions = kinematics._solve_ikfast(np.array([.1, .2, .3]), np.eye(3))

        self.assertEqual(imported, ["agent_service.libraries.ikfast.ur5e_backend"])
        self.assertEqual(len(solutions), 1)

    def test_analytic_solution_in_wrong_base_or_tool_frame_is_rejected(self):
        kinematics = PlatformKinematics(_Runtime())
        module = SimpleNamespace(solve=lambda *_: [np.zeros(6)])
        with patch("agent_service.skills.platform_runtime.kinematics.importlib.import_module", return_value=module):
            self.assertEqual(kinematics._solve_ikfast(np.zeros(3), np.eye(3)), [])
            # A 180-degree error must not be mistaken for zero by sin(angle).
            self.assertEqual(kinematics._solve_ikfast(np.array([.1,.2,.3]), np.diag([1.,-1.,-1.])), [])

    def test_auto_falls_back_to_scene_solver_after_analytic_frame_mismatch(self):
        runtime = _Runtime()
        solver = PlatformKinematics(runtime)
        with patch("agent_service.skills.platform_runtime.kinematics.importlib.import_module",
                   return_value=SimpleNamespace(solve=lambda *_: [np.zeros(6)])), patch.object(
                   solver, "_solve_trac_ik_compatible", return_value=(np.ones(6), 0.)) as numerical:
            result = solver.solve({"position": [0.,0.,0.], "orientation": {
                "representation": "quaternion", "w":1.,"x":0.,"y":0.,"z":0.}}, seed=np.zeros(6))
        self.assertEqual(result["solver"], "trac_ik")
        numerical.assert_called_once()

    def test_live_runtime_uses_the_same_vendored_ikfast_module(self) -> None:
        self.assertEqual(
            LIVE_IKFAST_MODULE,
            "agent_service.libraries.ikfast.ur5e_backend",
        )


if __name__ == "__main__":
    unittest.main()
