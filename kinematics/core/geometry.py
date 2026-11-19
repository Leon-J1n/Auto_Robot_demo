"""Pose conversion and orientation errors, without a simulator dependency."""
from __future__ import annotations
from typing import Any, Mapping
import math
import numpy as np


def transform(position, rotation):
    result = np.eye(4)
    result[:3, 3], result[:3, :3] = position, rotation
    return result


def rotation_error(target: np.ndarray, current: np.ndarray) -> np.ndarray:
    relative = target @ current.T
    angle = math.acos(float(np.clip((np.trace(relative) - 1) * .5, -1, 1)))
    if angle < 1e-9:
        return np.zeros(3)
    if math.pi - angle < 1e-5:
        # The skew part vanishes at pi; extract the axis from the symmetric part.
        _, vectors = np.linalg.eigh((relative + relative.T) * .5)
        axis = vectors[:, -1]
        skew = np.array([relative[2, 1]-relative[1, 2], relative[0, 2]-relative[2, 0], relative[1, 0]-relative[0, 1]])
        if np.dot(axis, skew) < 0:
            axis = -axis
        return axis * angle
    axis = np.array([relative[2, 1]-relative[1, 2], relative[0, 2]-relative[2, 0], relative[1, 0]-relative[0, 1]])
    return axis * (angle / (2 * math.sin(angle)))


def parse_pose(pose: Mapping[str, Any], default_rotation: np.ndarray):
    if pose.get("frame", "world") not in {"world", "base"}:
        raise ValueError("Only world/base poses are supported")
    # 'base' retains the legacy runtime convention (coordinates already world-aligned).
    value = pose.get("position")
    position = np.array([value[k] for k in ("x", "y", "z")], dtype=float) if isinstance(value, Mapping) else np.asarray(value, dtype=float)
    orientation = pose.get("orientation")
    if "rotation_matrix" in pose and orientation is None:
        rotation = np.asarray(pose["rotation_matrix"], dtype=float)
    elif not orientation:
        rotation = np.asarray(default_rotation, dtype=float).copy()
    elif orientation.get("representation", "quaternion") == "rpy":
        roll, pitch, yaw = np.radians([orientation[k] for k in ("roll", "pitch", "yaw")])
        cr, cp, cy = np.cos([roll, pitch, yaw]); sr, sp, sy = np.sin([roll, pitch, yaw])
        rotation = np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr], [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr], [-sp, cp*sr, cp*cr]])
    elif orientation.get("representation", "quaternion") == "quaternion":
        quaternion = np.array([orientation[k] for k in ("w", "x", "y", "z")], dtype=float)
        norm = np.linalg.norm(quaternion)
        if not np.isfinite(norm) or norm < 1e-12:
            raise ValueError("Quaternion must have finite non-zero norm")
        w, x, y, z = quaternion / norm
        rotation = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                             [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                             [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
    else:
        raise ValueError("Unsupported orientation representation")
    if position.shape != (3,) or not np.all(np.isfinite(position)):
        raise ValueError("Pose position must contain 3 finite values")
    if (rotation.shape != (3, 3) or not np.all(np.isfinite(rotation))
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6, rtol=0)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-6)):
        raise ValueError("Pose rotation must be a proper 3x3 rotation matrix")
    return position, rotation
