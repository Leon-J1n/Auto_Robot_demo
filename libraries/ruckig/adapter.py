"""Project-facing adapter around the optional Ruckig Python package."""
from __future__ import annotations

from typing import Any, Sequence
import numpy as np


class RuckigParameterizationError(RuntimeError):
    """Ruckig could not produce a valid time-parameterized segment."""


class RuckigTimeParameterizer:
    """Time-parameterize a joint path one segment at a time."""

    def __init__(self, *, control_cycle=0.01, max_velocity=None,
                 max_acceleration=None, max_jerk=None):
        self.control_cycle = float(control_cycle)
        if not np.isfinite(self.control_cycle) or self.control_cycle <= 0:
            raise ValueError("control_cycle must be positive and finite")
        self.max_velocity = self._validate_optional(max_velocity, "max_velocity")
        self.max_acceleration = self._validate_optional(max_acceleration, "max_acceleration")
        self.max_jerk = self._validate_optional(max_jerk, "max_jerk")

    @staticmethod
    def _validate_optional(value, name):
        if value is None:
            return None
        result = np.asarray(value, dtype=float)
        if result.ndim != 1 or not np.all(np.isfinite(result)) or np.any(result <= 0):
            raise ValueError(f"{name} must contain positive finite values")
        return result

    @staticmethod
    def _limits(value, fallback, dof, name):
        if value is None:
            value = fallback
        if value is None:
            return np.full(dof, {"max_velocity": 1.0, "max_acceleration": 2.0, "max_jerk": 10.0}[name])
        result = np.asarray(value, dtype=float)
        if result.shape != (dof,) or not np.all(np.isfinite(result)) or np.any(result <= 0):
            raise ValueError(f"{name} must contain one positive finite value per joint")
        return result.copy()

    def parameterize(self, waypoints: Any, *, velocity_scale=1.0,
                     acceleration_scale=1.0, max_velocity=None,
                     max_acceleration=None, max_jerk=None):
        points = np.asarray(waypoints, dtype=float)
        if points.ndim != 2 or len(points) < 2 or not np.all(np.isfinite(points)):
            raise ValueError("waypoints must be a finite array with shape (N, dof), N >= 2")
        velocity_scale, acceleration_scale = float(velocity_scale), float(acceleration_scale)
        if not np.isfinite(velocity_scale) or velocity_scale <= 0 or not np.isfinite(acceleration_scale) or acceleration_scale <= 0:
            raise ValueError("velocity_scale and acceleration_scale must be positive and finite")
        dof = points.shape[1]
        velocity = self._limits(max_velocity, self.max_velocity, dof, "max_velocity") * velocity_scale
        acceleration = self._limits(max_acceleration, self.max_acceleration, dof, "max_acceleration") * acceleration_scale
        jerk = self._limits(max_jerk, self.max_jerk, dof, "max_jerk")
        try:
            import ruckig
        except ImportError as exc:  # pragma: no cover
            raise RuckigParameterizationError("Ruckig is not installed") from exc
        timestamps, positions, velocities, accelerations = [0.0], [points[0].copy()], [np.zeros(dof)], [np.zeros(dof)]
        elapsed = 0.0
        for start, target in zip(points[:-1], points[1:]):
            if np.max(np.abs(target - start)) <= 1e-12:
                continue
            inp = ruckig.InputParameter(dof)
            inp.current_position, inp.current_velocity, inp.current_acceleration = start.tolist(), np.zeros(dof).tolist(), np.zeros(dof).tolist()
            inp.target_position, inp.target_velocity, inp.target_acceleration = target.tolist(), np.zeros(dof).tolist(), np.zeros(dof).tolist()
            inp.max_velocity, inp.max_acceleration, inp.max_jerk = velocity.tolist(), acceleration.tolist(), jerk.tolist()
            trajectory = ruckig.Trajectory(dof)
            result = ruckig.Ruckig(dof, self.control_cycle).calculate(inp, trajectory)
            if getattr(result, "value", -1) < 0:
                raise RuckigParameterizationError(f"Ruckig failed for segment: {result}")
            duration = float(trajectory.duration)
            for sample in range(1, max(1, int(np.ceil(duration / self.control_cycle))) + 1):
                local = min(sample * self.control_cycle, duration)
                q, dq, ddq = trajectory.at_time(local)
                timestamps.append(elapsed + local)
                positions.append(np.asarray(q, dtype=float)); velocities.append(np.asarray(dq, dtype=float)); accelerations.append(np.asarray(ddq, dtype=float))
            elapsed += duration
        if len(timestamps) == 1:
            timestamps.append(self.control_cycle); positions.append(points[-1].copy()); velocities.append(np.zeros(dof)); accelerations.append(np.zeros(dof))
        return {"timestamps": timestamps, "positions": np.asarray(positions), "velocities": np.asarray(velocities), "accelerations": np.asarray(accelerations), "duration": float(timestamps[-1]), "backend": "ruckig", "control_cycle": self.control_cycle, "max_velocity": velocity, "max_acceleration": acceleration, "max_jerk": jerk}
