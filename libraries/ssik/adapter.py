"""ssik adapter for the repository's common IK backend contract.

The vendored source lives under ``src/ssik``.  Runtime users should install
that package from this directory (or use the already installed ``ssik``
wheel); this adapter deliberately imports it lazily so discovery stays cheap.
"""
from __future__ import annotations

import numpy as np

from kinematics.core.contracts import IKError, IKResult
from kinematics.core.geometry import transform


class SsikBackend:
    def __init__(self, robot):
        self.robot = robot
        self._arm = None
        self._tip_rotation = np.eye(3)

    def supports(self, robot):
        parameters = getattr(robot, "chain_parameters", None)
        if parameters is None:
            parameters = getattr(robot, "eaik_parameters", None)
        if parameters is None:
            return False
        axes = np.asarray(parameters.get("H_rows", []), dtype=float)
        limits = np.asarray(parameters.get("joint_limits", []), dtype=float)
        return axes.ndim == 2 and axes.shape[1:] == (3,) and len(axes) in (6, 7) and limits.shape == (len(axes), 2)

    def _initialize(self):
        import ssik

        parameters = getattr(self.robot, "chain_parameters", None)
        if parameters is None:
            parameters = self.robot.eaik_parameters
        self._tip_rotation = np.asarray(parameters.get("R_zero", np.eye(3)), dtype=float)
        self._arm = ssik.Manipulator.from_axes(
            parameters["H_rows"], parameters["P_rows"], limits=parameters["joint_limits"])

    def solve(self, request):
        if self._arm is None:
            self._initialize()
        if request.remaining_time <= 0:
            raise IKError("ssik initialization exhausted the IK budget")
        target = np.linalg.inv(self.robot.base_transform(request.seed)) @ transform(request.position, request.rotation)
        target[:3, :3] = target[:3, :3] @ self._tip_rotation.T
        try:
            results = self._arm.solve(target, max_solutions=request.max_solutions if request.enumerate_solutions else 1,
                                      q_seed=request.seed, respect_limits=True)
        except Exception as exc:
            raise IKError(f"ssik solve failed: {exc}") from exc
        solutions = [np.asarray(item.q, dtype=float) for item in results]
        if not solutions:
            raise IKError("ssik returned no certified solution")
        return IKResult(solutions=solutions, backend="ssik", position_tolerance=request.position_tolerance,
                        orientation_tolerance=request.orientation_tolerance,
                        diagnostics={"candidates": len(solutions), "solver": self._arm.solver_name})


def create_backend(robot):
    return SsikBackend(robot)
