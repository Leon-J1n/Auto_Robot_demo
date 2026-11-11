"""Validate VLM references and back-project a horizontal or full 3-D axis."""
import numpy as np

from .depth import InvalidPointDepth, sample_depth


MIN_AXIS_LENGTH_M = .005


def project_pixel(u, v, depth, intrinsics, position, rotation):
    z, sampling = sample_depth(depth, u, v)
    camera = np.array([(u - intrinsics.cx) * z / intrinsics.fx,
                       -(v - intrinsics.cy) * z / intrinsics.fy, -z])
    return position + rotation @ camera, z, sampling


def estimate_axis(point, depth, intrinsics, position, rotation, *, horizontal=True):
    """No model dimensions or simulated object orientation enter this calculation."""
    result = {'is_elongated': point.get('is_elongated'), 'axis_points': None,
              'axis_status': 'missing', 'axis_reason': 'VLM 未提供长轴判断'}
    if 'is_elongated' not in point:
        return result  # Legacy point-only responses remain usable.
    if point['is_elongated'] is False:
        result.update(axis_status='not_elongated', axis_reason='目标没有明显的细长结构')
        return result
    result['axis_status'] = 'invalid'
    try:
        if point['is_elongated'] is not True:
            raise ValueError('is_elongated 必须为布尔值')
        references = point.get('axis_points')
        if not isinstance(references, list) or len(references) != 2:
            raise ValueError('细长物体必须提供两个长轴表面参考点')
        height, width = depth.shape
        projected = []
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError('长轴参考点必须包含 x、y 坐标')
            xy = [reference.get(key) for key in ('x', 'y')]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                   not np.isfinite(v) or not 0 <= v <= 1 for v in xy):
                raise ValueError('长轴参考点坐标必须是 0～1 的有限数值')
            u, v = round(xy[0] * (width - 1)), round(xy[1] * (height - 1))
            world, z, sampling = project_pixel(u, v, depth, intrinsics, position, rotation)
            projected.append({'normalized': dict(zip(('x', 'y'), xy)),
                              'pixel': {'x': u, 'y': v}, 'depth_m': z, 'depth_sampling': sampling,
                              'world_point': dict(zip('xyz', map(float, world)))})
        result['axis_points'] = projected
        worlds = np.array([[item['world_point'][key] for key in 'xyz'] for item in projected])
        direction = worlds[1] - worlds[0]
        if horizontal:
            direction[2] = 0.
        length = float(np.linalg.norm(direction))
        if length < MIN_AXIS_LENGTH_M:
            raise ValueError('长轴水平投影过短，无法确定抓取旋转' if horizontal else '把手三维长轴过短，无法确定抓取旋转')
        # Direction comes only from the two reference depths. A straight
        # image chord through a curved object may cross background; do not
        # sample or validate that chord or compare it with the grasp surface.
        result.update(axis_status='valid', axis_reason=None,
                      long_axis_world=(direction / length).tolist(), axis_length_m=length)
    except ValueError as exc:
        result['axis_reason'] = str(exc)
        if isinstance(exc, InvalidPointDepth):
            result['axis_depth_failure'] = exc.details['details']
    return result
