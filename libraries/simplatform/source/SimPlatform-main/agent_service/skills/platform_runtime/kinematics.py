"""UR5e kinematics backed by the scene's robot on RobotSimPlatform.

Replaces the MuJoCo-backed kinematics service. Behaviour is preserved: the same DLS
solver with the same starts, tolerances, damping and step clamp, and the same result
dictionaries. What changed underneath is where FK and the Jacobian come from -- the
platform's batch kinematics query, which walks the scene graph of the robot that is
actually loaded. No URDF is consulted, and evaluating a configuration never disturbs
the live world.
"""

from __future__ import annotations

import importlib
import itertools
import logging
import math
import time
from typing import Any, Mapping, Sequence

import numpy as np

from .transforms import quaternion_to_matrix, rotation_error, rpy_to_matrix

IKFAST_MODULE = "agent_service.libraries.ikfast.ur5e_backend"
LOGGER = logging.getLogger(__name__)


class IKError(RuntimeError):
    """Raised when no requested IK backend can solve a target."""


class PlatformKinematics:
    """FK/Jacobian service plus IKFast and TRAC-IK-compatible solving."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.qpos_indices = runtime.qpos_indices
        self.dof_indices = runtime.dof_indices
        self.limits = runtime.joint_limits
        # Robot.kinematics accepts link names.  SceneRobotRuntime also keeps a
        # LinkProxy for pose reads, but passing that proxy makes the live backend
        # stringify the Python object and fail to resolve the robot link.
        self.end_effector_link = runtime.end_effector_site
        self._links = (self.end_effector_link,)
        LOGGER.debug("Configured platform kinematics end-effector link: %s",
                     self.end_effector_link)

    def _evaluate(self, joints: Sequence[float], *, want_jacobian: bool,
                  base: np.ndarray | None = None) -> tuple[dict[str, np.ndarray],
                                                           np.ndarray | None]:
        """FK, and optionally the Jacobian, in one platform call.

        Non-arm joints hold the values in `base` -- the live gripper pose unless a
        caller pins it -- so the pinch frame reflects how the fingers actually sit.
        Limits are ignored rather than clamped, matching the original: FK is a pure
        geometric query, and callers already clip before asking.
        """
        full = self.runtime.expand_joints(joints, base)
        result = self.robot.kinematics(
            full, self._links,
            jacobian_link=self.end_effector_link if want_jacobian else None,
            limits="ignore")
        pose = _pose(_transforms(result)[0][0])
        if not want_jacobian:
            return pose, None
        jacobians = _jacobians(result)
        if jacobians is None:
            raise IKError("The platform returned no Jacobian for the requested link.")
        # Keep only the arm columns; the platform reports all 26 joints.
        return pose, np.ascontiguousarray(
            np.asarray(jacobians[0], dtype=float)[:, self.dof_indices])

    def forward(self, joints: Sequence[float]) -> dict[str, np.ndarray]:
        return self._evaluate(joints, want_jacobian=False)[0]

    def jacobian(self, joints: Sequence[float]) -> np.ndarray:
        return self._evaluate(joints, want_jacobian=True)[1]

    def forward_batch(self, configurations: Sequence[Sequence[float]],
                      ) -> list[dict[str, np.ndarray]]:
        """FK for many configurations in one call, for path checking and scoring."""
        values = np.atleast_2d(np.asarray(configurations, dtype=float))
        if values.shape[1] != 6:
            raise ValueError("UR5e joint vectors must contain 6 values.")
        base = self.runtime.full_joint_positions()
        full = np.repeat(base[None, :], len(values), axis=0)
        full[:, self.qpos_indices] = values
        result = self.robot.kinematics(full, self._links, limits="ignore")
        return [_pose(poses[0]) for poses in _transforms(result)]

    def solve(self, target_pose: Mapping[str, Any], *,
              seed: Sequence[float] | None = None, method: str = "auto",
              timeout: float = 1.0) -> dict[str, Any]:
        target_position, target_rotation = self._parse_pose(target_pose)
        seed_array = np.asarray(
            self.runtime.get_joint_positions() if seed is None else seed, dtype=float)
        errors = []
        if method in {"auto", "ikfast"}:
            try:
                solutions = self._solve_ikfast(target_position, target_rotation)
                if solutions:
                    solutions = [self._normalize_to_reference(q, seed_array)
                                 for q in solutions]
                    selected = min(solutions, key=lambda q: np.linalg.norm(q - seed_array))
                    return {"solution": selected, "solutions": solutions,
                            "solver": "ikfast"}
            except Exception as exc:
                errors.append(f"IKFast: {exc}")
                if method == "ikfast":
                    raise IKError(errors[-1]) from exc
        if method in {"auto", "trac_ik"}:
            try:
                solution, residual = self._solve_trac_ik_compatible(
                    target_position, target_rotation, seed_array, timeout)
                return {"solution": solution, "solutions": [solution],
                        "solver": "trac_ik", "backend": "robotsim_jacobian_compat",
                        "residual": residual}
            except Exception as exc:
                errors.append(f"TRAC-IK: {exc}")
        raise IKError("; ".join(errors) or f"Unsupported IK method: {method}")

    def solve_candidates(self, target_pose: Mapping[str, Any], *,
                         seed: Sequence[float] | None = None, method: str = "auto",
                         timeout: float = 2.0,
                         max_solutions: int = 8) -> list[np.ndarray]:
        """Return distinct IK branches instead of stopping at the first solution.

        Numerical IK is local, so a deterministic set of starts is explored.  The
        caller remains responsible for collision and path-continuity filtering.
        """
        target_position, target_rotation = self._parse_pose(target_pose)
        reference = np.asarray(
            self.runtime.get_joint_positions() if seed is None else seed, dtype=float)
        base = self.runtime.full_joint_positions()
        solutions: list[np.ndarray] = []

        if method in {"auto", "ikfast"}:
            try:
                for candidate in self._solve_ikfast(target_position, target_rotation):
                    self._append_distinct(solutions, candidate)
            except Exception:
                if method == "ikfast":
                    raise

        if method in {"auto", "trac_ik"} and len(solutions) < max_solutions:
            deadline = time.perf_counter() + max(timeout, 0.1)
            rng = np.random.default_rng(7)
            starts = [reference]
            starts.extend(rng.uniform(self.limits[:, 0], self.limits[:, 1])
                          for _ in range(24))
            for start in starts:
                candidate = self._solve_numerical_from_start(
                    target_position, target_rotation, start, deadline, base)
                if candidate is not None:
                    self._append_distinct(solutions, candidate)
                    if len(solutions) >= max_solutions:
                        break
                if time.perf_counter() >= deadline:
                    break

        solutions.sort(key=lambda q: float(
            np.linalg.norm(self._normalize_to_reference(q, reference) - reference)))
        return solutions[:max_solutions]

    def _solve_ikfast(self, position: np.ndarray,
                      rotation: np.ndarray) -> list[np.ndarray]:
        LOGGER.debug("Loading IKFast backend module: %s", IKFAST_MODULE)
        module = importlib.import_module(IKFAST_MODULE)
        solutions = []
        from agent_service.libraries.ikfast.scene_target import solver_target
        solver_position, solver_rotation = solver_target(self.runtime, position, rotation)
        base = self.runtime.full_joint_positions()
        for raw in module.solve(solver_position, solver_rotation):
            candidate = np.asarray(raw, dtype=float)
            if candidate.shape == (6,) and self._within_limits(candidate):
                # The compiled model may use a different base/tool frame than
                # this scene. Never execute an analytic solution unless scene FK
                # proves it reaches the requested world-space TCP pose.
                # The vendored UR5e dimensions differ slightly from the loaded
                # model. Refine this analytic branch, without a random restart.
                for _ in range(8):
                    error, delta, _ = self._step(position, rotation, candidate, base)
                    if np.linalg.norm(error[:3]) < 2e-5 and np.linalg.norm(error[3:]) < 2e-4:
                        break
                    candidate = np.clip(candidate + np.clip(delta, -0.05, 0.05),
                                        self.limits[:, 0], self.limits[:, 1])
                actual = self.forward(candidate)
                angle = math.acos(float(np.clip(
                    (np.trace(rotation @ actual["rotation_matrix"].T) - 1.0) / 2.0,
                    -1.0, 1.0)))
                if (np.linalg.norm(actual["position"] - position) < 2e-4
                        and angle < 2e-3):
                    solutions.append(candidate)
        return solutions

    def _step(self, target_position: np.ndarray, target_rotation: np.ndarray,
              q: np.ndarray, base: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
        """One DLS iteration's error, step and residual at configuration q."""
        pose, jac = self._evaluate(q, want_jacobian=True, base=base)
        error = np.concatenate((
            target_position - pose["position"],
            rotation_error(target_rotation, pose["rotation_matrix"])))
        weighted = error.copy()
        weighted[3:] *= 0.35
        residual = float(np.linalg.norm(weighted))
        jac = jac.copy()
        jac[3:] *= 0.35
        damping = 2e-3 + min(residual, 0.2) * 0.03
        delta = jac.T @ np.linalg.solve(
            jac @ jac.T + np.eye(6) * damping * damping, weighted)
        return error, delta, residual

    def _solve_trac_ik_compatible(self, target_position: np.ndarray,
                                  target_rotation: np.ndarray, seed: np.ndarray,
                                  timeout: float) -> tuple[np.ndarray, float]:
        deadline = time.perf_counter() + max(timeout, 0.05)
        base = self.runtime.full_joint_positions()
        rng = np.random.default_rng(7)
        starts = [seed] + [rng.uniform(self.limits[:, 0], self.limits[:, 1])
                           for _ in range(12)]
        best: tuple[float, np.ndarray] | None = None
        for start in starts:
            q = np.clip(np.asarray(start, dtype=float),
                        self.limits[:, 0], self.limits[:, 1])
            for _ in range(300):
                error, delta, residual = self._step(
                    target_position, target_rotation, q, base)
                if best is None or residual < best[0]:
                    best = (residual, q.copy())
                if (np.linalg.norm(error[:3]) < 2e-4
                        and np.linalg.norm(error[3:]) < 2e-3):
                    return self._normalize_to_reference(q, seed), residual
                scale = min(1.0, 0.18 / max(float(np.max(np.abs(delta))), 1e-9))
                q = np.clip(q + delta * scale, self.limits[:, 0], self.limits[:, 1])
                if time.perf_counter() >= deadline:
                    break
            if time.perf_counter() >= deadline:
                break
        residual = best[0] if best else float("inf")
        raise IKError(f"No numerical IK solution; best residual={residual:.6f}")

    def _solve_numerical_from_start(self, target_position: np.ndarray,
                                    target_rotation: np.ndarray,
                                    start: Sequence[float], deadline: float,
                                    base: np.ndarray) -> np.ndarray | None:
        q = np.clip(np.asarray(start, dtype=float),
                    self.limits[:, 0], self.limits[:, 1])
        for _ in range(300):
            error, delta, _ = self._step(target_position, target_rotation, q, base)
            if np.linalg.norm(error[:3]) < 2e-4 and np.linalg.norm(error[3:]) < 2e-3:
                return self._normalize_to_reference(q, start)
            scale = min(1.0, 0.18 / max(float(np.max(np.abs(delta))), 1e-9))
            q = np.clip(q + delta * scale, self.limits[:, 0], self.limits[:, 1])
            if time.perf_counter() >= deadline:
                return None
        return None

    @staticmethod
    def _append_distinct(solutions: list[np.ndarray], candidate: Sequence[float],
                         tolerance: float = 1e-3) -> None:
        value = np.asarray(candidate, dtype=float)
        if not any(np.linalg.norm((value - existing + math.pi) % (2.0 * math.pi)
                                  - math.pi) < tolerance for existing in solutions):
            solutions.append(value.copy())

    def equivalent_configurations(self, joints: Sequence[float],
                                 reference: Sequence[float],
                                 max_variants: int = 8) -> list[np.ndarray]:
        """Return limit-valid 2*pi representations ordered by joint travel."""
        value = np.asarray(joints, dtype=float)
        target = np.asarray(reference, dtype=float)
        choices: list[np.ndarray] = []
        for index in range(len(value)):
            equivalents = value[index] + 2.0 * math.pi * np.arange(-2, 3)
            valid = equivalents[
                (equivalents >= self.limits[index, 0] - 1e-9)
                & (equivalents <= self.limits[index, 1] + 1e-9)
            ]
            choices.append(valid)
        variants = [np.asarray(items, dtype=float) for items in itertools.product(*choices)]
        variants.sort(key=lambda q: float(np.linalg.norm(q - target)))
        return variants[:max_variants]

    def _normalize_to_reference(self, joints: Sequence[float],
                                reference: Sequence[float]) -> np.ndarray:
        """Choose the limit-valid 2*pi representation nearest to reference.

        Revolute joints use bounded numeric coordinates even when several coordinates
        describe the same physical angle, so numerical IK may land on an equivalent
        value a full turn away from where the arm is now.
        """
        value = np.asarray(joints, dtype=float).copy()
        target = np.asarray(reference, dtype=float)
        for index in range(len(value)):
            equivalents = value[index] + 2.0 * math.pi * np.arange(-2, 3)
            valid = equivalents[
                (equivalents >= self.limits[index, 0] - 1e-9)
                & (equivalents <= self.limits[index, 1] + 1e-9)
            ]
            if len(valid):
                value[index] = valid[int(np.argmin(np.abs(valid - target[index])))]
        return value

    def _within_limits(self, joints: np.ndarray) -> bool:
        return bool(np.all(joints >= self.limits[:, 0])
                    and np.all(joints <= self.limits[:, 1]))

    def _parse_pose(self, pose: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
        value = pose.get("position")
        position = (np.array([value[k] for k in ("x", "y", "z")], dtype=float)
                    if isinstance(value, Mapping) else np.asarray(value, dtype=float))
        if position.shape != (3,):
            raise ValueError("Pose position must contain x, y, z.")
        orientation = pose.get("orientation")
        if not orientation:
            rotation = self.runtime.get_end_effector_pose()["rotation_matrix"]
        elif orientation.get("representation", "quaternion") == "rpy":
            # Degrees: the Move schema declares `unit: degree` for these three fields,
            # unlike every other angle in a request. rpy_to_matrix takes radians.
            rotation = rpy_to_matrix(
                *np.radians([orientation[k] for k in ("roll", "pitch", "yaw")]))
        else:
            quaternion = np.array([orientation[k] for k in ("w", "x", "y", "z")],
                                  dtype=float)
            rotation = quaternion_to_matrix(quaternion / np.linalg.norm(quaternion))
        return position, rotation


def _transforms(result: Any) -> Sequence[Sequence[Any]]:
    value = (result.get("transforms") if isinstance(result, Mapping)
             else getattr(result, "transforms", None))
    if value is None:
        raise IKError("The platform returned no transforms for the requested links.")
    return value


def _jacobians(result: Any) -> Any:
    return (result.get("jacobians") if isinstance(result, Mapping)
            else getattr(result, "jacobians", None))


def _pose(transform: Any) -> dict[str, np.ndarray]:
    if isinstance(transform, Mapping):
        translation = transform["translation"]
        rotation = transform["rotation_wxyz"]
    else:
        translation = transform.translation
        rotation = transform.rotation_wxyz
    return {"position": np.asarray(translation, dtype=float),
            "rotation_matrix": quaternion_to_matrix(rotation)}
