"""Recover a bar grasp frame from RGB-D references, without model geometry."""
import numpy as np

from .axis import project_pixel


def _reference_cloud(references, depth, intrinsics, position, rotation):
    if not isinstance(references, list) or not 3 <= len(references) <= 8:
        raise ValueError('面板参考点需要至少三个不共线点')
    height, width = depth.shape
    patches, centers = [], []
    for reference in references:
        xy = [reference.get(key) for key in ('x', 'y')] if isinstance(reference, dict) else []
        if len(xy) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                              or not np.isfinite(v) or not 0 <= v <= 1 for v in xy):
            raise ValueError('面板参考点必须是 0～1 的有限坐标')
        u, v = round(xy[0] * (width - 1)), round(xy[1] * (height - 1))
        center, z, _ = project_pixel(u, v, depth, intrinsics, position, rotation)
        centers.append(center)
        vv, uu = np.mgrid[max(0, v - 2):min(height, v + 3), max(0, u - 2):min(width, u + 3)]
        zz = depth[vv, uu]
        valid = np.isfinite(zz) & (zz > 0) & (np.abs(zz - z) <= .015)
        if valid.sum() < 6:
            raise ValueError('面板参考点周围有效深度不足')
        camera = np.column_stack(((uu[valid] - intrinsics.cx) * zz[valid] / intrinsics.fx,
                                  -(vv[valid] - intrinsics.cy) * zz[valid] / intrinsics.fy,
                                  -zz[valid]))
        patches.append(camera @ rotation.T + position)
    centers = np.asarray(centers)
    if np.linalg.svd(centers - centers.mean(axis=0), compute_uv=False)[1] < .01:
        raise ValueError('面板参考点共线或分布范围过小')
    return np.concatenate(patches), centers


def _local_cloud(depth, intrinsics, position, rotation, estimate):
    """Back-project a metric-sized neighborhood, keeping invalid depths out."""
    u, v = estimate['pixel']['x'], estimate['pixel']['y']
    z = estimate['depth_m']
    height, width = depth.shape
    rx, ry = int(np.ceil(intrinsics.fx * .12 / z)), int(np.ceil(intrinsics.fy * .12 / z))
    vv, uu = np.mgrid[max(0, v - ry):min(height, v + ry + 1),
                      max(0, u - rx):min(width, u + rx + 1)]
    zz = depth[vv, uu]
    valid = np.isfinite(zz) & (zz > 0)
    camera = np.column_stack(((uu[valid] - intrinsics.cx) * zz[valid] / intrinsics.fx,
                              -(vv[valid] - intrinsics.cy) * zz[valid] / intrinsics.fy,
                              -zz[valid]))
    cloud = camera @ rotation.T + position
    surface = np.array([estimate['world_point'][key] for key in 'xyz'])
    cloud = cloud[np.linalg.norm(cloud - surface, axis=1) <= .18]
    if len(cloud) > 2500:
        cloud = cloud[np.linspace(0, len(cloud) - 1, 2500, dtype=int)]
    return cloud


def _horizontal_axis(normal):
    axis = np.cross([0., 0., 1.], normal)
    length = np.linalg.norm(axis)
    if length < .2:
        raise ValueError("面板法向接近竖直，无法确定水平把手方向")
    return axis / length


def _panel_frame(cloud, surface, position, references):
    if len(cloud) < 30:
        raise ValueError('把手附近有效面板深度不足')
    # Score only planes behind the handle, parallel to its axis, and visible
    # on both sides of the grasp. This excludes the bar, supports and table.
    rng = np.random.default_rng(0)
    fits = []
    for _ in range(160):
        a, b, c = cloud[rng.choice(len(cloud), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        norm = np.linalg.norm(normal)
        if norm < 1e-8:
            continue
        normal /= norm
        toward_camera = np.asarray(position) - a
        if np.dot(normal, toward_camera) < 0:
            normal = -normal
        gap = float(np.dot(surface - a, normal))
        if not .008 <= gap <= .15:
            continue
        if np.dot(normal, toward_camera) / np.linalg.norm(toward_camera) < .2:
            continue
        inliers = np.abs((cloud - a) @ normal) <= .004
        if inliers.mean() < (.75 if references is not None else .35):
            continue
        try:
            axis = _horizontal_axis(normal)
        except ValueError:
            continue
        y_axis = np.cross(-normal, axis)
        y_axis /= np.linalg.norm(y_axis)
        around = (cloud[inliers] - surface) @ y_axis
        along = (cloud[inliers] - surface) @ axis
        if references is None and (np.quantile(around, .1) > -.012
                or np.quantile(around, .9) < .012
                or np.quantile(along, .1) > -.012 or np.quantile(along, .9) < .012):
            continue
        fits.append((int(inliers.sum()), inliers, normal))
    if not fits:
        raise ValueError('把手附近未找到可靠面板平面；请调整相机视角或提供可选面板参考点')
    fits.sort(key=lambda fit: fit[0], reverse=True)
    count, best, best_normal = fits[0]
    if any(score >= count * .8 and np.dot(normal, best_normal) < .94
           for score, _, normal in fits[1:]):
        raise ValueError('把手附近有多个面板法向候选，无法确定抓取姿态')
    selected = cloud[best]
    origin = selected.mean(axis=0)
    _, singular, vectors = np.linalg.svd(selected - origin, full_matrices=False)
    normal = vectors[-1]
    if singular[1] / np.sqrt(len(selected)) < .005:
        raise ValueError('面板点云不足以确定法向')
    if references is not None and np.max(np.abs((references - origin) @ normal)) > .006:
        raise ValueError('面板参考点不在同一平面上')
    toward_camera = np.asarray(position) - origin
    if np.dot(normal, toward_camera) < 0:
        normal = -normal
    if np.dot(normal, toward_camera) / np.linalg.norm(toward_camera) < .2:
        raise ValueError('面板视角过于倾斜，无法可靠确定朝外法向')
    gap = float(np.dot(surface - origin, normal))
    if not .008 <= gap <= .15:
        raise ValueError('把手表面与面板间距无效，无法确认外凸把手')
    x_axis = _horizontal_axis(normal)
    z_axis = -normal
    y_axis = np.cross(z_axis, x_axis)
    grasp_rotation = np.column_stack((x_axis, y_axis, z_axis))
    return {
        'grasp_type': 'handle', 'orientation_source': 'vlm_rgbd_panel',
        'long_axis_aligned': True, 'long_axis_world': x_axis.tolist(),
        'axis_status': 'fixed_horizontal', 'axis_source': 'world_horizontal_panel_tangent',
        'grasp_rotation_world': grasp_rotation.tolist(),
        'panel_normal_world': normal.tolist(), 'panel_point_world': origin.tolist(),
        'panel_estimation_source': 'vlm_references' if references is not None else 'local_rgbd',
        'panel_reference_points_world': references.tolist() if references is not None else None,
        'panel_fit_inlier_ratio': float(best.mean()),
        'panel_fit_rms_m': float(np.sqrt(np.mean(((selected - origin) @ normal) ** 2))),
        'handle_panel_gap_m': gap, 'max_grasp_depth_m': gap - .005,
    }


def estimate_handle(point, depth, intrinsics, position, rotation, estimate):
    """Recover the local panel automatically; VLM panel references are optional."""
    references = None
    if point.get('panel_points') is not None:
        cloud, references = _reference_cloud(point['panel_points'], depth, intrinsics, position, rotation)
    else:
        cloud = _local_cloud(depth, intrinsics, position, rotation, estimate)
    surface = np.array([estimate['world_point'][key] for key in 'xyz'])
    return _panel_frame(cloud, surface, position, references)
