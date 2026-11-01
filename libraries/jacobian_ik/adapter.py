"""Existing numerical algorithm, decoupled from the MuJoCo implementation."""
from __future__ import annotations
import numpy as np
from kinematics.contracts import IKError, IKResult
from kinematics.geometry import rotation_error
from kinematics.policies import append_distinct, normalize


class JacobianBackend:
    def __init__(self, robot):
        self.robot = robot

    def supports(self, robot):
        return len(robot.limits) > 0 and np.all(np.isfinite(robot.limits))

    def solve(self, request):
        limits, periods = self.robot.limits, self.robot.joint_periods
        rng = np.random.default_rng(7)
        starts = [request.seed] + [rng.uniform(limits[:, 0], limits[:, 1])
                                  for _ in range(24 if request.enumerate_solutions else 12)]
        solutions, best = [], float("inf")
        for start in starts:
            if request.remaining_time <= 0:
                break
            q = np.clip(np.asarray(start, dtype=float), limits[:, 0], limits[:, 1])
            for _ in range(300):
                pose = self.robot.forward(q)
                error = np.concatenate((request.position-pose["position"], rotation_error(request.rotation, pose["rotation_matrix"])))
                weighted = error.copy()
                weighted[3:] *= .35
                residual = float(np.linalg.norm(weighted))
                best = min(best, residual)
                if np.linalg.norm(error[:3]) < 2e-4 and np.linalg.norm(error[3:]) < 2e-3:
                    candidate = normalize(q, request.seed, limits, periods)
                    if request.max_joint_step is None or np.max(np.abs(candidate-request.seed)) <= request.max_joint_step:
                        append_distinct(solutions, candidate, periods)
                        if not request.enumerate_solutions or len(solutions) >= request.max_solutions:
                            return IKResult(solutions=solutions, backend="damped_least_squares", residual=residual)
                    break
                jac = self.robot.jacobian(q).copy()
                jac[3:] *= .35
                damping = 2e-3 + min(residual, .2) * .03
                delta = jac.T @ np.linalg.solve(jac @ jac.T + np.eye(6)*damping*damping, weighted)
                scale = min(1.0, .18/max(float(np.max(np.abs(delta))), 1e-9))
                q = np.clip(q+delta*scale, limits[:, 0], limits[:, 1])
                if request.remaining_time <= 0:
                    break
        if not solutions:
            raise IKError(f"No numerical IK solution within budget/continuity limit; best residual={best:.6f}")
        return IKResult(solutions=solutions, backend="damped_least_squares", residual=best)


def create_backend(robot):
    return JacobianBackend(robot)
