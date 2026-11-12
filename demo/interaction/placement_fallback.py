"""Bounded random RGB-D placement points near a destination's local center."""
import numpy as np

from demo.interaction.object_library import rigid_pose


CENTER_RADIUS_FRACTION = .20  # Radius / model size on each local XY axis.
MAX_CENTER_POINTS = 3


def random_center_estimates(frame, object_pose, lower, upper, rng):
    """Sample distinct visible points; do not rank by center distance or floor height."""
    depth = np.asarray(frame.depth, dtype=float)
    valid_depth = np.isfinite(depth) & (depth > 0)
    z = np.where(valid_depth, depth, 0.)
    y, x = np.indices(depth.shape)
    intrinsics = frame.intrinsics
    camera = np.stack(((x - intrinsics.cx) * z / intrinsics.fx,
                       -(y - intrinsics.cy) * z / intrinsics.fy, -z), axis=-1)
    world = camera @ frame.camera_pose.rotation_matrix.T + frame.camera_pose.position
    transform = rigid_pose(object_pose['position'], object_pose['quaternion_xyzw'])
    local = (world - transform[:3, 3]) @ transform[:3, :3]
    center, extent = (lower + upper) / 2., upper - lower
    relative_xy = (local[..., :2] - center[:2]) / extent[:2]
    radius = np.linalg.norm(relative_xy, axis=-1)
    valid = (valid_depth & (radius <= CENTER_RADIUS_FRACTION)
             & (radius > .01)
             & (local[..., 2] >= lower[2] - .01) & (local[..., 2] <= upper[2] + .01))
    pixels = np.argwhere(valid)
    if not len(pixels):
        return []
    indices = rng.choice(len(pixels), size=min(MAX_CENTER_POINTS, len(pixels)), replace=False)
    estimates = []
    for index in indices:
        v, u = map(int, pixels[index])
        estimates.append({
            'point_type': 'place', 'pixel': {'x': u, 'y': v},
            'world_point': dict(zip('xyz', map(float, world[v, u]))),
            'depth_m': float(depth[v, u]), 'frame': 'world', 'confidence': None,
            'timestamp': getattr(frame, 'timestamp', None),
            'sequence': getattr(frame, 'sequence', None),
            'placement_center_fallback': {
                'used': True, 'radius_fraction_xy': CENTER_RADIUS_FRACTION,
                'max_points': MAX_CENTER_POINTS, 'sample_index': len(estimates)},
        })
    return estimates
