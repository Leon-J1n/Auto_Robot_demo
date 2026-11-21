"""Map the project's base-frame H/P chain and world TCP poses to SSIK."""
from __future__ import annotations

import numpy as np

from kinematics.core.contracts import IKError, IKResult
from kinematics.core.geometry import transform


class SSIKBackend:
    def __init__(self, robot, parameters=None):
        self.robot = robot
        self.parameters = parameters
        self._solver = None
        self._prepared = False

    def supports(self, robot):
        return len(robot.limits) in (6, 7) and (
            self.parameters is not None or hasattr(robot, "runtime"))

    def _initialize(self):
        from ssik import Manipulator

        parameters = self.parameters
        if parameters is None:
            from libraries.kinematic_chain.mujoco import extract_chain_parameters
            parameters = extract_chain_parameters(self.robot.runtime)
        self._tip_rotation = np.eye(4)
        self._tip_rotation[:3, :3] = np.asarray(parameters["R_zero"], dtype=float)
        solver = Manipulator.from_axes(parameters["H_rows"], parameters["P_rows"],
                                       limits=self.robot.limits)
        count = len(self.robot.limits)
        for q in (np.zeros(count), np.linspace(-.4, .6, count), self.robot.current_joints()):
            actual = self.robot.forward(q)
            predicted = self.robot.base_transform(q) @ solver.fk(q) @ self._tip_rotation
            if not np.allclose(predicted, transform(actual["position"], actual["rotation_matrix"]),
                               atol=1e-8, rtol=0):
                raise IKError("SSIK geometry does not match the robot/tool FK")
        self._solver = solver

    def prepare(self):
        """Verify FK and move one-time symbolic work outside the solve budget."""
        if self._solver is None:
            self._initialize()
        if not self._prepared:
            self._solver.prepare()
            self._prepared = True

    def solve(self, request):
        if self._solver is None:
            self._initialize()
        if request.remaining_time <= 0:
            raise IKError("SSIK initialization exhausted the IK budget")
        target = (np.linalg.inv(self.robot.base_transform(request.seed))
                  @ transform(request.position, request.rotation)
                  @ self._tip_rotation.T)
        # Return geometric branches; the shared service applies winding,
        # limits, continuity, FK validation and the caller's ranking policy.
        solutions = self._solver.solve(target, q_seed=request.seed,
                                       respect_limits="wrap", enumerate_windings=False,
                                       allow_refinement=True)
        return IKResult(solutions=[np.asarray(item.q, dtype=float) for item in solutions],
                        backend="ssik", position_tolerance=request.position_tolerance,
                        orientation_tolerance=request.orientation_tolerance,
                        diagnostics={"solver_name": self._solver.solver_name,
                                     "raw_candidates": len(solutions),
                                     "refinement_used": sum(bool(item.refinement_used) for item in solutions)})


def create_backend(robot):
    parameters = getattr(robot, "chain_parameters", None)
    if parameters is None:
        parameters = getattr(robot, "eaik_parameters", None)
    return SSIKBackend(robot, parameters)
