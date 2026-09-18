"""Feasibility-preserving TrajOpt-style optimization for RRT paths.

The optimizer uses sequential convex smoothness steps inside a trust region.
Collision constraints are enforced on every accepted segment with the existing
continuous MuJoCo path checker.  Since the input is already a feasible RRT path,
failure to improve it can safely fall back to that input.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import numpy as np


class TrajOptOptimizer:
    """Shorten and smooth a collision-free joint path without moving its ends."""

    optimizer_id = "trajopt"

    def __init__(
        self,
        *,
        collision_checker: Any,
        joint_limits: Any,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.collision_checker = collision_checker
        self.joint_limits = np.asarray(joint_limits, dtype=float)
        self.config = dict(config or {})

    def optimize(
        self,
        path: Any,
        *,
        safety_distance: float = 0.0,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Return an improved path and diagnostics; preserve the seed on failure."""
        seed = np.asarray(path, dtype=float)
        diagnostics: dict[str, Any] = {
            "algorithm": self.optimizer_id,
            "backend": "numpy_sequential_convex",
            "enabled": bool(self.config.get("enabled", True)),
            "applied": False,
            "fallback_to_rrt": False,
            "iterations": 0,
        }
        if not diagnostics["enabled"] or len(seed) < 3:
            diagnostics["reason"] = "disabled" if not diagnostics["enabled"] else "path_has_no_interior_waypoints"
            return seed.copy(), diagnostics
        if seed.ndim != 2 or self.joint_limits.shape != (seed.shape[1], 2):
            diagnostics.update(fallback_to_rrt=True, reason="invalid_path_or_joint_limits")
            return seed.copy(), diagnostics

        max_iterations = max(1, int(self.config.get("max_iterations", 40)))
        max_time = max(0.01, float(self.config.get("max_time", 0.5)))
        trust_region = max(1e-5, float(self.config.get("trust_region", 0.12)))
        minimum_trust_region = max(1e-7, float(self.config.get("minimum_trust_region", 1e-4)))
        convergence_tolerance = max(0.0, float(self.config.get("convergence_tolerance", 1e-5)))
        smoothness_weight = max(0.0, float(self.config.get("smoothness_weight", 0.25)))
        collision_resolution = max(1e-4, float(self.config.get("collision_resolution", 0.02)))

        current = seed.copy()
        deadline = time.perf_counter() + max_time
        initial_cost = self._objective(current, smoothness_weight)
        current_cost = initial_cost
        accepted_steps = 0

        for iteration in range(max_iterations):
            if time.perf_counter() >= deadline:
                diagnostics["stopped_by_time_limit"] = True
                break
            sweep_improvement = 0.0
            # Alternating sweep direction avoids a systematic start/goal bias.
            indices = range(1, len(current) - 1)
            if iteration % 2:
                indices = reversed(range(1, len(current) - 1))
            for index in indices:
                if time.perf_counter() >= deadline:
                    diagnostics["stopped_by_time_limit"] = True
                    break
                target = 0.5 * (current[index - 1] + current[index + 1])
                delta = np.clip(target - current[index], -trust_region, trust_region)
                if np.max(np.abs(delta)) <= convergence_tolerance:
                    continue
                for scale in (1.0, 0.5, 0.25, 0.125):
                    candidate_point = np.clip(
                        current[index] + scale * delta,
                        self.joint_limits[:, 0],
                        self.joint_limits[:, 1],
                    )
                    local_path = np.vstack((current[index - 1], candidate_point, current[index + 1]))
                    collision = self.collision_checker.check_path(
                        local_path,
                        resolution=collision_resolution,
                        safety_distance=safety_distance,
                        allowed_geom_ids=getattr(self.collision_checker, "allowed_target_geom_ids", None),
                    )
                    if collision.in_collision:
                        continue
                    candidate = current.copy()
                    candidate[index] = candidate_point
                    candidate_cost = self._objective(candidate, smoothness_weight)
                    improvement = current_cost - candidate_cost
                    if improvement > convergence_tolerance:
                        current = candidate
                        current_cost = candidate_cost
                        sweep_improvement += improvement
                        accepted_steps += 1
                        break
            diagnostics["iterations"] = iteration + 1
            if sweep_improvement <= convergence_tolerance:
                trust_region *= 0.5
                if trust_region < minimum_trust_region:
                    break

        final_collision = self.collision_checker.check_path(
            current,
            resolution=collision_resolution,
            safety_distance=safety_distance,
            allowed_geom_ids=getattr(self.collision_checker, "allowed_target_geom_ids", None),
        )
        improved = accepted_steps > 0 and current_cost < initial_cost and not final_collision.in_collision
        diagnostics.update(
            applied=improved,
            accepted_steps=accepted_steps,
            initial_cost=initial_cost,
            final_cost=current_cost if improved else initial_cost,
            cost_reduction=(initial_cost - current_cost) if improved else 0.0,
        )
        if not improved:
            diagnostics["fallback_to_rrt"] = True
            diagnostics["reason"] = "no_safe_improvement" if not final_collision.in_collision else "final_collision_check_failed"
            return seed.copy(), diagnostics
        return current, diagnostics

    @staticmethod
    def _objective(path: np.ndarray, smoothness_weight: float) -> float:
        velocity = np.diff(path, axis=0)
        acceleration = np.diff(path, n=2, axis=0)
        return float(np.sum(velocity * velocity) + smoothness_weight * np.sum(acceleration * acceleration))
