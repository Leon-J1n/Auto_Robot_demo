"""Rotation conversions, replacing the mju_* helpers the MuJoCo layer used.

Quaternions are (w, x, y, z) throughout, matching both the platform SDK and the
convention the skills already exchange in their request/result payloads.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np


def quaternion_to_matrix(value: Sequence[float]) -> np.ndarray:
    """Rotation matrix from a (w, x, y, z) quaternion."""
    q = np.asarray(value, dtype=float)
    if q.shape != (4,):
        raise ValueError("Quaternion must contain w, x, y, z.")
    norm = float(np.linalg.norm(q))
    if norm < 1e-12:
        raise ValueError("Quaternion has zero length.")
    w, x, y, z = q / norm
    return np.array([
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
    ])


def matrix_to_quaternion(rotation: np.ndarray) -> np.ndarray:
    """(w, x, y, z) quaternion from a rotation matrix.

    Uses Shepperd's method: pick the largest diagonal term first so the divisor
    never approaches zero, which a naive trace-only formula does at 180 degrees.
    """
    m = np.asarray(rotation, dtype=float).reshape(3, 3)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        return np.array([0.25 * scale, (m[2, 1] - m[1, 2]) / scale,
                         (m[0, 2] - m[2, 0]) / scale, (m[1, 0] - m[0, 1]) / scale])
    if m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        scale = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        return np.array([(m[2, 1] - m[1, 2]) / scale, 0.25 * scale,
                         (m[0, 1] + m[1, 0]) / scale, (m[0, 2] + m[2, 0]) / scale])
    if m[1, 1] > m[2, 2]:
        scale = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        return np.array([(m[0, 2] - m[2, 0]) / scale, (m[0, 1] + m[1, 0]) / scale,
                         0.25 * scale, (m[1, 2] + m[2, 1]) / scale])
    scale = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
    return np.array([(m[1, 0] - m[0, 1]) / scale, (m[0, 2] + m[2, 0]) / scale,
                     (m[1, 2] + m[2, 1]) / scale, 0.25 * scale])


def rpy_to_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Rotation matrix from ZYX intrinsic Euler angles, in radians."""
    cr, cp, cy = np.cos([roll, pitch, yaw])
    sr, sp, sy = np.sin([roll, pitch, yaw])
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def quaternion_mapping(value: Sequence[float]) -> dict[str, float]:
    """The {representation, w, x, y, z} form the skills pass around."""
    q = np.asarray(value, dtype=float)
    return {"representation": "quaternion", "w": float(q[0]), "x": float(q[1]),
            "y": float(q[2]), "z": float(q[3])}


def rotation_mapping(rotation: np.ndarray) -> dict[str, float]:
    return quaternion_mapping(matrix_to_quaternion(rotation))


def axis_angle_matrix(axis: Sequence[float], angle: float) -> np.ndarray:
    """Rodrigues rotation about an arbitrary axis."""
    direction = np.asarray(axis, dtype=float)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-12:
        raise ValueError("Rotation axis has zero length.")
    direction = direction / norm
    cross = np.array([[0.0, -direction[2], direction[1]],
                      [direction[2], 0.0, -direction[0]],
                      [-direction[1], direction[0], 0.0]])
    return (np.eye(3) + math.sin(angle) * cross
            + (1.0 - math.cos(angle)) * (cross @ cross))


def rotation_error(target: np.ndarray, current: np.ndarray) -> np.ndarray:
    """Rotation vector taking `current` onto `target`, as axis * angle."""
    relative = np.asarray(target, dtype=float) @ np.asarray(current, dtype=float).T
    angle = math.acos(float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0)))
    if angle < 1e-9:
        return np.zeros(3)
    axis = np.array([relative[2, 1] - relative[1, 2],
                     relative[0, 2] - relative[2, 0],
                     relative[1, 0] - relative[0, 1]])
    axis /= max(2.0 * math.sin(angle), 1e-9)
    return axis * angle


def serializable_pose(pose: Mapping[str, object]) -> dict[str, object]:
    return {"position": np.asarray(pose["position"], dtype=float).tolist(),
            "quaternion_wxyz": np.asarray(pose["quaternion_wxyz"], dtype=float).tolist()}
