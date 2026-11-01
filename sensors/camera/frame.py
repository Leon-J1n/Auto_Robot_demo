"""Camera observation value objects."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CameraIntrinsics:
    """Pinhole intrinsics in pixel units."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @property
    def matrix(self) -> np.ndarray:
        return np.asarray(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]],
            dtype=float,
        )


@dataclass(frozen=True)
class CameraPose:
    """Camera pose in the MuJoCo world frame."""

    position: np.ndarray
    rotation_matrix: np.ndarray
    quaternion_wxyz: np.ndarray


@dataclass(frozen=True)
class CameraFrame:
    """One synchronized RGB-D observation from a named camera."""

    camera_id: str
    timestamp: float
    sequence: int
    rgb: np.ndarray
    depth: np.ndarray
    intrinsics: CameraIntrinsics
    camera_pose: CameraPose

