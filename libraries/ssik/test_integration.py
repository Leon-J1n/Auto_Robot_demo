"""Real SSIK integration checks, run with the optional dependency installed."""
import json
from pathlib import Path
import unittest

import numpy as np

from kinematics import KinematicsService
from libraries.kinematic_chain import ChainRobotModel
from libraries.kinematic_chain.model import axis_rotation
from libraries.ssik.adapter import SSIKBackend


class SSIKIntegrationTests(unittest.TestCase):
    def parameters(self):
        path = Path(__file__).parents[1] / "eaik/model_checks/scene_000.eaik.json"
        return json.loads(path.read_text(encoding="utf-8"))["parameters"]

    def test_fk_ik_with_world_and_tool_rotation(self):
        parameters = self.parameters()
        parameters.pop("joint_transforms_zero", None)
        parameters["R_zero"] = axis_rotation(np.array([1., 0, 0]), .7).tolist()
        base = np.eye(4)
        base[:3, :3] = axis_rotation(np.array([0., 0, 1]), .6)
        base[:3, 3] = [.4, -.2, .8]
        robot = ChainRobotModel(parameters, world_from_base=base)
        kin = KinematicsService(robot)
        rng = np.random.default_rng(41)
        for _ in range(10):
            q = rng.uniform(-1.5, 1.5, len(robot.limits))
            target = robot.forward(q)
            result = kin.solve_all(target, seed=q + .03, backend="ssik", timeout=30)
            self.assertEqual(result.solver, "ssik")
            self.assertTrue(result.solutions)
            for candidate in result.solutions:
                pose = robot.forward(candidate)
                np.testing.assert_allclose(pose["position"], target["position"], atol=1e-8)
                np.testing.assert_allclose(pose["rotation_matrix"], target["rotation_matrix"], atol=1e-8)

    def test_registration_auto_order(self):
        from kinematics import SolverRegistry
        registry = SolverRegistry.discover()
        self.assertIn("ssik", registry.names)
        self.assertEqual(registry.auto_backends, ["eaik", "ssik", "jacobian"])
        robot = ChainRobotModel(self.parameters())
        self.assertTrue(SSIKBackend(robot, robot.chain_parameters).supports(robot))

    def test_real_auto_prefers_eaik(self):
        robot = ChainRobotModel(self.parameters())
        kin = KinematicsService(robot)
        q = np.array([.2, -.6, .8, .3, -.4, .5])
        result = kin.solve_one(robot.forward(q), seed=q + .03)
        self.assertEqual(result.solver, "eaik")
        self.assertEqual(kin.automatic_backends(), ["eaik", "ssik", "jacobian"])

    def test_asset_piper_automatically_uses_ssik(self):
        from libraries.kinematic_chain.urdf import UrdfRobotModel
        path = Path(__file__).parents[2] / "services/object_library_server/data/robots/8/piper.urdf"
        robot = UrdfRobotModel(path, base_link="base_link", tip_link="link6")
        kin = KinematicsService(robot)
        self.assertEqual(kin.automatic_backends(), ["ssik", "jacobian"])
        self.assertIn("eaik", kin.automatic_backend_info()["excluded"])
        q = robot.current_joints()
        result = kin.solve_one(robot.forward(q), seed=q, timeout=5)
        self.assertEqual(result.solver, "ssik")
        np.testing.assert_allclose(robot.forward(result.solution)["position"],
                                   robot.forward(q)["position"], atol=1e-8)


if __name__ == "__main__":
    unittest.main()
