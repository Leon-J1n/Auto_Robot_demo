"""Ordered grasp-depth candidates with static geometry before path checks."""
from copy import deepcopy

import numpy as np


GRASP_DEPTHS_M = tuple(mm / 1000. for mm in range(30, -1, -5))


def floor_fallback_candidates(resolved, checker, seed):
    """Keep XY/yaw and place the open tool 3 mm above the raised table floor."""
    import mujoco
    from types import SimpleNamespace
    from demo.interaction.table_clearance import (table_floor, set_static_gripper_width,
                                                 gripper_clearance, DEFAULT_OPEN_WIDTH_M,
                                                 TABLE_FLOOR_OFFSET_M)
    runtime = checker.backend
    model = runtime.model
    data = getattr(checker, '_floor_fallback_probe', None)
    if data is None:
        data = checker._floor_fallback_probe = mujoco.MjData(model)
    data.qpos[:] = runtime.data.qpos
    data.qpos[runtime.qpos_indices] = seed
    set_static_gripper_width(model, data, DEFAULT_OPEN_WIDTH_M)
    mujoco.mj_kinematics(model, data)
    site = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, 'tcp')
    if site < 0:
        raise ValueError('找不到 TCP，无法计算桌面底限兜底位姿')
    current = np.eye(4)
    current[:3, :3] = data.site_xmat[site].reshape(3, 3)
    current[:3, 3] = data.site_xpos[site]
    # All tool links are rigid relative to TCP at this prescribed opening.
    positions = data.geom_xpos.copy()
    rotations = data.geom_xmat.copy().reshape(-1, 3, 3)
    tool_geoms = sorted(checker.tool_geoms)
    result = []
    for source in resolved['candidates']:
        if source['grasp_depth_offset_m'] != 0.:
            continue
        candidate = deepcopy(source)
        control = np.asarray(source['world_from_control'], dtype=float).copy()
        grasp = np.asarray(source['world_from_grasp'], dtype=float).copy()
        transform = control @ np.linalg.inv(current)
        for geom in tool_geoms:
            data.geom_xpos[geom] = transform[:3, :3] @ positions[geom] + transform[:3, 3]
            data.geom_xmat[geom] = (transform[:3, :3] @ rotations[geom]).reshape(9)
        floor = table_floor(runtime, grasp[:3, 3])
        clearance = gripper_clearance(SimpleNamespace(model=model, data=data), floor)
        floor_limit = floor + TABLE_FLOOR_OFFSET_M
        shift = floor_limit + .003 - clearance['gripper_lowest_z_m']
        control[2, 3] += shift
        grasp[2, 3] += shift
        # Downward grasps use negative depth for a point above the RGB-D surface.
        candidate.update(world_from_control=control.tolist(), world_from_grasp=grasp.tolist(),
            grasp_depth_offset_m=-float(shift), floor_fallback={
                'used': True, 'table_height_m': floor, 'table_floor_z_m': floor_limit,
                'table_floor_offset_m': TABLE_FLOOR_OFFSET_M, 'minimum_open_clearance_m': .003,
                'minimum_table_clearance_m': TABLE_FLOOR_OFFSET_M + .003,
                'gripper_configuration': 'preclosure_open', 'open_width_m': DEFAULT_OPEN_WIDTH_M,
                'vertical_shift_m': float(shift),
                'original_grasp_z_m': float(np.asarray(source['world_from_grasp'])[2, 3]),
                'adjusted_grasp_z_m': float(grasp[2, 3]),
                'outside_search_range': not 0. <= -shift <= .03})
        result.append(candidate)
    return result


def validate_depth_candidate(resolved, candidate, joints, kin, checker, seed, safety_distance):
    """Check static open geometry first, then the grasp-axis contact path."""
    from atomic_action.motion.common.interaction import pose_request
    from atomic_action.motion.common.utils.validation import validate_move_request
    from planning import PickContactPlanner
    from demo.interaction.table_clearance import table_floor, validate_grasp_geometry

    goal = np.asarray(candidate['world_from_control'], dtype=float)
    grasp = np.asarray(candidate['world_from_grasp'], dtype=float)
    if resolved.get('visual_estimate', {}).get('grasp_type') == 'handle':
        from demo.interaction.table_clearance import DEFAULT_OPEN_WIDTH_M
        hit = checker.check(joints, safety_distance=safety_distance, gripper_width=DEFAULT_OPEN_WIDTH_M)
        geometry = {'success': not hit.in_collision,
                    'reason': 'grasp_geometry_collision' if hit.in_collision else 'static_geometry_valid',
                    'contacts': list(hit.contacts), 'validation_mode': 'static_open_geometry'}
    else:
        floor_z = table_floor(checker.backend, grasp[:3, 3])
        geometry = validate_grasp_geometry(checker, joints, floor_z, safety_distance=safety_distance)
    if not geometry['success']:
        return geometry
    base = deepcopy(resolved.get('depth_search_move_defaults') or {})
    base['target'] = pose_request(goal)
    base.setdefault('constraints', {}).update(avoid_collision=True, safety_distance=safety_distance)
    base = validate_move_request(base)
    planner = PickContactPlanner(kinematics=kin, collision_checker=checker,
                                 joint_limits=checker.backend.joint_limits)
    planned, rejected = planner.plan(
        base, {'joints': np.asarray(seed)}, goal=goal, direction=grasp[:3, 2],
        requested_path='joint', distances=(.10, .07, .04, .03, .02), pose_request=pose_request)
    if planned is None:
        return {'success': False, 'reason': 'contact_planning', 'rejected': rejected}
    return {**geometry, 'contact_preflight': {'distance_m': planned['distance'], 'rejected': rejected}}
