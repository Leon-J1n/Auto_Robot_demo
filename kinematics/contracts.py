"""Stable, simulation-independent contracts for solver plugins (API v1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence
import time

import numpy as np


class IKError(RuntimeError):
    """No requested backend produced a valid IK solution."""


class RobotModel(Protocol):
    """Positions are world metres, rotations are matrices, joint angles radians.

    joint_periods is zero for non-periodic coordinates (e.g. prismatic joints).
    Each instance owns its FK workspace; it is not shared between workers.
    """
    limits: np.ndarray
    joint_periods: np.ndarray

    def current_joints(self) -> np.ndarray: ...
    def forward(self, joints: Sequence[float]) -> dict[str, np.ndarray]: ...
    def jacobian(self, joints: Sequence[float]) -> np.ndarray: ...
    def base_transform(self, joints: Sequence[float]) -> np.ndarray: ...


@dataclass(frozen=True)
class IKRequest:
    position: np.ndarray
    rotation: np.ndarray
    seed: np.ndarray
    deadline: float  # absolute time.perf_counter(), shared across fallback backends
    max_solutions: int = 1
    enumerate_solutions: bool = False
    max_joint_step: float | None = None

    @property
    def remaining_time(self) -> float:
        return max(0.0, self.deadline - time.perf_counter())


@dataclass
class IKResult:
    solutions: list[np.ndarray] = field(default_factory=list)
    solver: str = ""
    backend: str = ""
    residual: float | None = None
    fallback_reason: str | None = None
    position_tolerance: float = 2e-4
    orientation_tolerance: float = 2e-3  # radians
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def solution(self) -> np.ndarray:
        if not self.solutions:
            raise IKError("Result contains no IK solutions")
        return self.solutions[0]

    def metadata(self) -> dict[str, Any]:
        """Solver information for trajectory diagnostics, without joint arrays."""
        result = {"solver": self.solver, "backend": self.backend}
        if self.residual is not None:
            result["residual"] = self.residual
        if self.fallback_reason is not None:
            result["fallback_reason"] = self.fallback_reason
        if self.diagnostics:
            result["diagnostics"] = self.diagnostics.copy()
        return result


class IKBackend(Protocol):
    def supports(self, robot: RobotModel) -> bool: ...
    def solve(self, request: IKRequest) -> IKResult: ...
