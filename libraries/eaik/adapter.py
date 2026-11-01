"""Optional EAIK plugin: derive the chain from scene geometry at first use."""
from __future__ import annotations

import numpy as np

from kinematics.contracts import IKError, IKResult
from kinematics.geometry import transform
from .model_data import extract_model_data


class EAIKBackend:
    def __init__(self, robot, parameters=None):
        self.robot = robot
        self.parameters = parameters
        self._solver = None

    def supports(self, robot):
        # Explicit parameter injection also supports a simulator-independent model.
        return self.parameters is not None or hasattr(robot, "runtime")

    def _initialize(self):
        from eaik.IK_Homogeneous import HomogeneousRobot

        parameters = self.parameters if self.parameters is not None else extract_model_data(self.robot.runtime)
        solver = HomogeneousRobot(np.asarray(parameters["joint_transforms_zero"], dtype=float))
        if not solver.hasKnownDecomposition():
            raise IKError("EAIK has no supported decomposition for this robot chain")
        count = len(self.robot.limits)
        for q in (np.zeros(count), np.linspace(-.4, .6, count), self.robot.current_joints()):
            actual = self.robot.forward(q)
            predicted = self.robot.base_transform(q) @ np.asarray(solver.fwdKin(q))
            if not np.allclose(predicted, transform(actual["position"], actual["rotation_matrix"]), atol=1e-8, rtol=0):
                raise IKError("EAIK geometry does not match the robot/tool FK")
        self._solver = solver

    def solve(self, request):
        if self._solver is None:
            self._initialize()
        if request.remaining_time <= 0:
            raise IKError("EAIK initialization exhausted the IK budget")
        target = np.linalg.inv(self.robot.base_transform(request.seed)) @ transform(request.position, request.rotation)
        result = self._solver.IK(target)
        raw = list(result.Q)
        least_squares = list(result.is_LS)
        if len(raw) != len(least_squares):
            raise IKError("EAIK returned inconsistent solution flags")
        # At singularities EAIK can flag an exactly correct answer as LS. Final
        # pose accuracy, rather than the flag alone, is checked by the service.
        return IKResult(solutions=[np.asarray(q, dtype=float) for q in raw],
                        backend="eaik_homogeneous", position_tolerance=1e-7,
                        orientation_tolerance=1e-7,
                        diagnostics={"kinematic_family": self._solver.getKinematicFamily(),
                                     "raw_candidates": len(raw), "least_squares_flagged": int(sum(least_squares))})


def create_backend(robot):
    parameters = getattr(robot, "chain_parameters", None)
    if parameters is None:
        parameters = getattr(robot, "eaik_parameters", None)
    return EAIKBackend(robot, parameters)
