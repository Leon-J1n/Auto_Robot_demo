"""Resolve horizontal placement regions outside a measured destination boundary."""
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from demo.interaction.object_library import GraspResolutionError, rigid_pose
from demo.task_commands.converter import END_EFFECTOR_DIRECTIONS
from sensors.camera import CameraIntrinsics


REGION_RADIUS_M = .02
_RELATIONS = {'front', 'behind', 'left', 'right'}


def placement_parameters(parameters):
    relation = parameters.get('relation', 'at')
    distance = parameters.get('distance_m', 0.)
    if not isinstance(relation, str) or relation not in _RELATIONS | {'at'}:
        raise ValueError('put.relation 仅支持 at、front、behind、left、right')
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not np.isfinite(distance) or distance < 0:
        raise ValueError('put.distance_m 必须为大于或等于 0 的有限数值，单位 m')
    if relation == 'at' and distance != 0:
        raise ValueError('put 指定 distance_m 时必须同时指定前后左右的 relation')
    return {'relation': relation, 'distance_m': float(distance)}


def relative_region(request, object_pose, lower, upper, resources):
    """Project the oriented model box onto a robot-base horizontal direction."""
    transform = rigid_pose(object_pose['position'], object_pose['quaternion_xyzw'])
    robot = getattr(resources, 'robot_object', {})
    base = next((c.get('properties', {}) for c in robot.get('components', [])
                 if c['component_type'] == 'Transform'), {})
    rotation = Rotation.from_quat(base.get('quaternion_xyzw', [0., 0., 0., 1.])).as_matrix()
    direction = rotation @ np.asarray(END_EFFECTOR_DIRECTIONS[request['relation']])
    direction[2] = 0.
    if np.linalg.norm(direction) < 1e-6:
        raise ValueError('机器人基座方向在桌面平面的投影无效，无法计算相对放置位置')
    direction /= np.linalg.norm(direction)
    center = transform[:3, :3] @ ((lower + upper) / 2.) + transform[:3, 3]
    support = float(np.sum(np.abs(direction @ transform[:3, :3]) * (upper - lower) / 2.))
    boundary = center + direction * support
    point = boundary + direction * request['distance_m']
    return {**request, 'reference': 'target_boundary', 'direction_frame': 'robot_base_horizontal',
            'direction_world': direction.tolist(), 'boundary_point_world': boundary.tolist(),
            'region_center_world': point.tolist(), 'region_radius_m': REGION_RADIUS_M}


def region_error(message):
    error = GraspResolutionError(message)
    error.details = {'error_code': 'PLACEMENT_REGION_UNOBSERVED', 'failed_stage': 'perception',
                     'error_message': message}
    return error


def region_points(frame, region):
    depth = np.asarray(frame.depth, dtype=float)
    valid = np.isfinite(depth) & (depth > 0)
    z = np.where(valid, depth, 0.)
    v, u = np.indices(depth.shape)
    k = frame.intrinsics
    camera = np.stack(((u - k.cx) * z / k.fx, -(v - k.cy) * z / k.fy, -z), axis=-1)
    world = camera @ frame.camera_pose.rotation_matrix.T + frame.camera_pose.position
    center = np.asarray(region['region_center_world'])
    radius = float(region['region_radius_m'])
    # Explicit spatial requests must never turn into a point inside the target.
    boundary = np.asarray(region['boundary_point_world'])
    outside = (world - boundary) @ np.asarray(region['direction_world']) >= -1e-6
    valid &= (np.linalg.norm(world[..., :2] - center[:2], axis=-1) <= radius) & outside
    return world, np.argwhere(valid)


def crop_relative_region(frame, region):
    _, pixels = region_points(frame, region)
    if not len(pixels):
        raise region_error('指定相对放置区域不在当前 RGB-D 的有效可见范围内')
    top, left = np.maximum(pixels.min(axis=0) - 2, 0)
    bottom, right = np.minimum(pixels.max(axis=0) + 3, frame.depth.shape)
    left, top, right, bottom = map(int, (left, top, right, bottom))
    k = frame.intrinsics
    crop = replace(frame, rgb=frame.rgb[top:bottom, left:right], depth=frame.depth[top:bottom, left:right],
                   intrinsics=CameraIntrinsics(right-left, bottom-top, k.fx, k.fy, k.cx-left, k.cy-top))
    return crop, (left, top)


def validate_relative_point(estimate, region):
    point = np.array([estimate['world_point'][a] for a in 'xyz'])
    center = np.asarray(region['region_center_world'])
    boundary = np.asarray(region['boundary_point_world'])
    if (not np.isfinite(point).all()
            or np.linalg.norm(point[:2] - center[:2]) > region['region_radius_m'] + 1e-6
            or np.dot(point - boundary, region['direction_world']) < -1e-6):
        raise region_error('感知放置点未落在指定相对放置区域内')


def relative_instruction(region):
    path = Path(__file__).resolve().parents[2] / 'atomic_action/perception/vlm_estimate/prompts/relative_place.json'
    prompt = json.loads(path.read_text(encoding='utf-8'))
    return prompt['description'].format(relation=prompt['relations'][region['relation']],
                                        distance_cm=region['distance_m'] * 100)


def random_region_estimates(frame, region, rng):
    world, pixels = region_points(frame, region)
    if not len(pixels):
        return []
    indices = rng.choice(len(pixels), size=min(3, len(pixels)), replace=False)
    estimates = []
    for index in indices:
        v, u = map(int, pixels[index])
        estimates.append({'point_type': 'place', 'pixel': {'x': u, 'y': v},
            'world_point': dict(zip('xyz', map(float, world[v, u]))), 'depth_m': float(frame.depth[v, u]),
            'frame': 'world', 'timestamp': frame.timestamp, 'sequence': frame.sequence,
            'placement_center_fallback': {'used': True, 'region': 'requested_relative_region',
                                          'max_points': 3, 'sample_index': len(estimates)}})
    return estimates
