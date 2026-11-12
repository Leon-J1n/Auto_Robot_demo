"""Translate an approach pose so the wrist camera observes the coarse point."""
import numpy as np


def camera_aligned_pose(world_from_control, control_from_camera, target_world):
    """Center a point along camera -Z, preserving rotation and optical depth.

    Translation is confined to the camera image plane. The original grasp
    transform is never modified; callers must plan and collision-check this
    separate observation pose.
    """
    control = np.asarray(world_from_control, dtype=float)
    extrinsic = np.asarray(control_from_camera, dtype=float)
    target = np.asarray(target_world, dtype=float)
    for value in (control, extrinsic):
        if (value.shape != (4, 4) or not np.isfinite(value).all()
                or not np.allclose(value[3], [0, 0, 0, 1])
                or not np.allclose(value[:3, :3].T @ value[:3, :3], np.eye(3))
                or not np.isclose(np.linalg.det(value[:3, :3]), 1.)):
            raise ValueError('Camera alignment requires rigid finite transforms')
    if target.shape != (3,) or not np.isfinite(target).all():
        raise ValueError('Camera alignment target must be a finite world point')
    camera = control @ extrinsic
    forward = -camera[:3, 2]
    delta = target - camera[:3, 3]
    depth = float(delta @ forward)
    if depth <= 1e-6:
        raise ValueError('Coarse grasp point is behind the approach camera')
    offset = delta - depth * forward
    result = control.copy()
    result[:3, 3] += offset
    return result, {
        'target_world': target.tolist(),
        'translation_world_m': offset.tolist(),
        'translation_distance_m': float(np.linalg.norm(offset)),
        'optical_depth_m': depth,
        'world_from_camera': (result @ extrinsic).tolist(),
    }
