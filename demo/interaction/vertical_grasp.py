"""垂面抓取估计：RGB-D position and target-local AABB surface orientation."""
import numpy as np

from .object_library import GraspResolutionError, rigid_pose


def estimate_vertical_grasp(world_point, object_pose, lower, upper):
    """Match finite side faces in the target frame, keeping the RGB-D point."""
    transform = rigid_pose(object_pose['position'], object_pose['quaternion_xyzw'])
    rotation = transform[:3, :3]
    point = np.array([world_point[key] for key in 'xyz'], dtype=float)
    lower, upper = np.asarray(lower, dtype=float), np.asarray(upper, dtype=float)
    if (point.shape != (3,) or not np.isfinite(point).all() or lower.shape != (3,)
            or upper.shape != (3,) or not np.isfinite(np.r_[lower, upper]).all()
            or np.any(upper <= lower)):
        raise GraspResolutionError('垂面抓取需要有效三维抓取点和 target 局部 AABB')
    local = rotation.T @ (point - transform[:3, 3])
    candidates = []
    for axis in range(3):
        for sign, boundary, label in ((-1., lower[axis], 'min'), (1., upper[axis], 'max')):
            normal_local = np.eye(3)[axis] * sign
            normal_world = rotation @ normal_local
            # Vertical grasping uses side faces; top/bottom faces are handled
            # by the separate downward (平面) estimator.
            if abs(normal_world[2]) > .5:
                continue
            closest = np.clip(local, lower, upper)
            closest[axis] = boundary
            candidates.append({'face': 'xyz'[axis] + '_' + label,
                'distance_m': float(np.linalg.norm(local - closest)),
                'normal_local': normal_local.tolist(), 'normal_world': normal_world.tolist(),
                'closest_point_local': closest.tolist()})
    candidates.sort(key=lambda item: item['distance_m'])
    if not candidates:
        raise GraspResolutionError('target AABB 没有适用于垂面抓取的侧面')
    if len(candidates) > 1 and candidates[1]['distance_m'] - candidates[0]['distance_m'] <= .002:
        error = GraspResolutionError('抓取点接近多个 AABB 侧面，无法唯一确定朝外法向')
        error.details = {'error_code': 'AABB_FACE_AMBIGUOUS', 'failed_stage': 'perception',
                         'error_message': str(error), 'details': {'face_candidates': candidates}}
        raise error
    selected = candidates[0]
    normal = np.asarray(selected['normal_world'])
    x_axis = np.cross([0., 0., 1.], normal)
    x_axis /= np.linalg.norm(x_axis)
    z_axis = -normal
    y_axis = np.cross(z_axis, x_axis)
    return {'grasp_type': 'handle', 'grasp_estimation_method': 'vertical_aabb',
        'orientation_source': 'target_local_aabb', 'axis_status': 'fixed_horizontal',
        'axis_source': 'world_horizontal_aabb_tangent', 'long_axis_aligned': True,
        'long_axis_world': x_axis.tolist(),
        'grasp_rotation_world': np.column_stack((x_axis, y_axis, z_axis)).tolist(),
        'surface_normal_world': normal.tolist(),
        'aabb_estimate': {'frame': 'target_local', 'lower': lower.tolist(), 'upper': upper.tolist(),
            'grasp_point_local': local.tolist(), 'selected_face': selected['face'],
            'face_candidates': candidates},
        # A whole-target AABB can include the protruding bar. Its face is not
        # a measured panel, so it cannot supply a handle-to-panel depth limit.
        'max_grasp_depth_m': .02, 'grasp_shallow_offset_m': .015}
