"""Stable facade: dispatch plugins, validate results and select joint branches."""
from __future__ import annotations
import time
import numpy as np

from .contracts import IKError, IKRequest, IKResult, RobotModel
from .geometry import parse_pose, rotation_error
from .policies import append_distinct, equivalent_configurations, normalize
from .registry import SolverRegistry, get_registry


class KinematicsService:
    def __init__(self, robot: RobotModel, registry: SolverRegistry | None = None):
        self.robot = robot
        self.registry = registry if registry is not None else get_registry()
        self.limits = np.asarray(robot.limits, dtype=float).copy()
        self.joint_periods = np.asarray(robot.joint_periods, dtype=float).copy()
        if (self.limits.ndim != 2 or self.limits.shape[1] != 2
                or self.joint_periods.shape != (len(self.limits),)
                or np.any(np.isnan(self.limits)) or np.any(self.limits[:, 0] > self.limits[:, 1])
                or not np.all(np.isfinite(self.joint_periods)) or np.any(self.joint_periods < 0)):
            raise ValueError("Invalid robot joint limits/periods")
        self._backends = {}  # Instances and mutable workspaces belong to this service.

    def forward(self, joints):
        return self.robot.forward(joints)

    def jacobian(self, joints):
        return self.robot.jacobian(joints)

    def available_backends(self):
        """Registered names/aliases; native availability is checked on first use."""
        return self.registry.names

    def quick_reachability(self, target_pose, *, timeout=0.03):
        """Ordering hint only: verify analytic IK without numerical fallback.

        False never proves unreachability; unsupported models and solver errors
        must still be allowed through the ordinary full planning chain later.
        """
        if 'eaik' not in self.registry.names:
            return None
        try:
            self.solve_one(target_pose, backend='eaik', timeout=timeout)
        except IKError:
            return False
        return True

    def solve_one(self, target_pose, *, seed=None, backend="auto", backends=None,
                  policy="nearest", timeout=1.0, max_joint_step=None) -> IKResult:
        return self._solve(target_pose, seed=seed, backend=backend, backends=backends,
                           policy=policy, timeout=timeout, max_joint_step=max_joint_step,
                           max_solutions=1, enumerate_solutions=False)

    def solve_all(self, target_pose, *, seed=None, backend="auto", backends=None,
                  policy="nearest", timeout=2.0, max_solutions=8,
                  max_joint_step=None) -> IKResult:
        """Candidates from the first successful backend in the fallback chain.

        Local methods return candidates found within the budget, not all IK roots.
        """
        return self._solve(target_pose, seed=seed, backend=backend, backends=backends,
                           policy=policy, timeout=timeout, max_joint_step=max_joint_step,
                           max_solutions=max_solutions, enumerate_solutions=True)

    def _solve(self, target_pose, *, seed, backend, backends, policy, timeout,
               max_joint_step, max_solutions, enumerate_solutions):
        if policy not in {"nearest", "minimum_travel", "continuous"}:
            raise ValueError("Unknown IK selection policy")
        if not np.isfinite(timeout) or timeout <= 0:
            raise ValueError("IK timeout must be positive and finite")
        if isinstance(max_solutions, bool) or not isinstance(max_solutions, int) or max_solutions < 1:
            raise ValueError("max_solutions must be a positive integer")
        if max_joint_step is not None and (not np.isfinite(max_joint_step) or max_joint_step <= 0):
            raise ValueError("max_joint_step must be positive and finite")
        if policy == "continuous" and max_joint_step is None:
            raise ValueError("continuous policy requires max_joint_step")
        reference = np.asarray(self.robot.current_joints() if seed is None else seed, dtype=float)
        if reference.shape != (len(self.limits),) or not np.all(np.isfinite(reference)):
            raise ValueError("IK seed must contain one finite value per joint")
        # FK is only needed for an omitted orientation. An explicit pose must
        # not pay for a second reading/FK of the measured robot state.
        default_rotation = None
        if not target_pose.get("orientation") and "rotation_matrix" not in target_pose:
            default_rotation = self.forward(self.robot.current_joints())["rotation_matrix"]
        position, rotation = parse_pose(target_pose, default_rotation)
        if backends is not None:
            if backend != "auto" or isinstance(backends, str) or not backends:
                raise ValueError("Use either backend or a non-empty backends sequence")
            chain = [self.registry.resolve(name) for name in backends]
        else:
            chain = self.registry.auto_backends if backend == "auto" else [self.registry.resolve(backend)]
        request = IKRequest(position.copy(), rotation.copy(), reference.copy(),
                            time.perf_counter()+timeout, max_solutions,
                            enumerate_solutions, max_joint_step)
        for value in (request.position, request.rotation, request.seed):
            value.setflags(write=False)  # Failed plugins cannot rewrite the fallback target/seed.
        errors = []
        for name in dict.fromkeys(chain):
            if request.remaining_time <= 0:
                errors.append("Shared IK timeout exhausted")
                break
            try:
                if name not in self._backends:
                    self._backends[name] = self.registry.create(name, self.robot)
                raw = self._backends[name].solve(request)
                if not isinstance(raw, IKResult):
                    raise IKError("Backend must return IKResult")
                solutions, rejected = self._validate(raw, request)
                if not solutions:
                    raise IKError("No valid solution" + (f" ({'; '.join(rejected)})" if rejected else ""))
                norm = 1 if policy == "minimum_travel" else 2
                solutions.sort(key=lambda q: float(np.linalg.norm(q-reference, ord=norm)))
                if enumerate_solutions:
                    solutions = solutions[:max_solutions]
                return IKResult(solutions=solutions, solver=name, backend=raw.backend or name,
                                residual=raw.residual, fallback_reason="; ".join(errors) or None,
                                position_tolerance=raw.position_tolerance,
                                orientation_tolerance=raw.orientation_tolerance,
                                diagnostics={**raw.diagnostics, "rejected_candidates": rejected})
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise IKError("; ".join(errors) or "No automatic IK backends registered")

    def _validate(self, result, request):
        # Plugins can request stricter verification, never relax the service ceiling.
        if (not np.isfinite(result.position_tolerance) or not np.isfinite(result.orientation_tolerance)
                or result.position_tolerance <= 0 or result.orientation_tolerance <= 0):
            raise IKError("Invalid backend verification tolerances")
        position_tolerance = min(result.position_tolerance, 2e-4)
        orientation_tolerance = min(result.orientation_tolerance, 2e-3)
        solutions, rejected = [], []
        for raw in result.solutions:
            try:
                q = np.asarray(raw, dtype=float)
                if q.shape != request.seed.shape or not np.all(np.isfinite(q)):
                    raise ValueError("invalid joint vector")
                q = normalize(q, request.seed, self.limits, self.joint_periods)
                if np.any(q < self.limits[:, 0]-1e-9) or np.any(q > self.limits[:, 1]+1e-9):
                    raise ValueError("joint limits")
                if request.max_joint_step is not None and np.max(np.abs(q-request.seed)) > request.max_joint_step:
                    raise ValueError("joint continuity limit")
                pose = self.forward(q)
                if (not np.all(np.isfinite(pose["position"]))
                        or not np.all(np.isfinite(pose["rotation_matrix"]))
                        or np.linalg.norm(pose["position"]-request.position) > position_tolerance
                        or np.linalg.norm(rotation_error(request.rotation, pose["rotation_matrix"])) > orientation_tolerance):
                    raise ValueError("FK target mismatch")
                append_distinct(solutions, q, self.joint_periods,
                                tolerance=1e-6 if result.position_tolerance <= 1e-6 else 1e-3)
            except (ValueError, TypeError) as exc:
                rejected.append(str(exc))
        return solutions, rejected

    def equivalent_configurations(self, joints, reference, max_variants=8):
        return equivalent_configurations(np.asarray(joints, dtype=float),
                                         np.asarray(reference, dtype=float), self.limits,
                                         self.joint_periods, max_variants)
