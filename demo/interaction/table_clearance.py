"""World-height floor for the articulated gripper, from runtime collision geometry."""
import mujoco
import numpy as np
from atomic_action.manipulation.grasp.action import DEFAULT_OPEN_WIDTH_M

TABLE_FLOOR_OFFSET_M = .017


def set_static_gripper_width(model, data, width=0.):
    """Set Robotiq joints and their mimic constraints without physics stepping."""
    master = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT,
                             'robotiq_2f85_right_driver_joint')
    if master < 0:
        raise ValueError('找不到夹爪驱动关节，无法设置静态夹爪开口')
    data.qpos[model.jnt_qposadr[master]] = np.clip(.8 * (1. - width / .085), 0., .8)
    known = {master}
    # Resolve chains using the runtime's actual mimic coefficients.
    for _ in range(model.neq + 1):
        changed = False
        for eq in range(model.neq):
            if int(model.eq_type[eq]) != int(mujoco.mjtEq.mjEQ_JOINT):
                continue
            child, parent = int(model.eq_obj1id[eq]), int(model.eq_obj2id[eq])
            if parent not in known or child in known:
                continue
            value = data.qpos[model.jnt_qposadr[parent]]
            data.qpos[model.jnt_qposadr[child]] = np.polynomial.polynomial.polyval(
                value, model.eq_data[eq, :5])
            known.add(child)
            changed = True
        if not changed:
            break


def validate_grasp_geometry(checker, joints, floor_z, *, safety_distance=.003):
    """Check only the open, pre-closure geometry before path planning."""
    from types import SimpleNamespace
    runtime = checker.backend
    model = runtime.model
    data = getattr(checker, '_static_grasp_probe', None)
    if data is None:
        data = checker._static_grasp_probe = mujoco.MjData(model)
    data.qpos[:] = runtime.data.qpos
    data.qpos[runtime.qpos_indices] = joints
    set_static_gripper_width(model, data, DEFAULT_OPEN_WIDTH_M)
    mujoco.mj_kinematics(model, data)
    floor_limit = float(floor_z) + TABLE_FLOOR_OFFSET_M
    clearance = gripper_clearance(SimpleNamespace(model=model, data=data), floor_limit)
    clearance.update(table_height_m=float(floor_z), table_floor_z_m=floor_limit,
                     table_floor_offset_m=TABLE_FLOOR_OFFSET_M,
                     gripper_configuration='preclosure_open', open_width_m=DEFAULT_OPEN_WIDTH_M)
    if not clearance['success']:
        message = (f"闭爪前张开构型 {clearance['lowest_link']} 最低点 "
                   f"{clearance['gripper_lowest_z_m']:.6f} m 未高于桌面底限 "
                   f"{floor_limit:.6f} m（实际桌面上方 {TABLE_FLOOR_OFFSET_M * 1000:g} mm），"
                   f"净空 {clearance['clearance_m'] * 1000:.3f} mm")
        return {'success': False, 'reason': 'grasp_below_table_floor',
                'table_clearance': clearance, 'error': {
                    'error_code': 'GRASP_BELOW_TABLE_FLOOR', 'error_message': message,
                    'details': clearance}}
    hit = checker.check(joints, safety_distance=safety_distance, gripper_width=DEFAULT_OPEN_WIDTH_M)
    return {'success': not hit.in_collision,
            'reason': 'grasp_geometry_collision' if hit.in_collision else 'static_geometry_valid',
            'contacts': list(hit.contacts), 'table_clearance': clearance,
            'validation_mode': 'static_open_geometry'}


def geom_world_bounds(model, data, geom):
    rotation = data.geom_xmat[geom].reshape(3, 3)
    position = data.geom_xpos[geom]
    kind = int(model.geom_type[geom])
    size = model.geom_size[geom]
    if kind == int(mujoco.mjtGeom.mjGEOM_MESH):
        mesh = int(model.geom_dataid[geom])
        start, count = int(model.mesh_vertadr[mesh]), int(model.mesh_vertnum[mesh])
        vertices = model.mesh_vert[start:start + count] @ rotation.T + position
        return vertices.min(axis=0), vertices.max(axis=0)
    if kind == int(mujoco.mjtGeom.mjGEOM_SPHERE):
        extent = np.full(3, size[0])
    elif kind == int(mujoco.mjtGeom.mjGEOM_BOX):
        extent = np.abs(rotation) @ size
    elif kind == int(mujoco.mjtGeom.mjGEOM_ELLIPSOID):
        extent = np.sqrt(np.sum((rotation * size) ** 2, axis=1))
    elif kind in (int(mujoco.mjtGeom.mjGEOM_CAPSULE), int(mujoco.mjtGeom.mjGEOM_CYLINDER)):
        axis = np.abs(rotation[:, 2])
        radial = np.ones(3) if kind == int(mujoco.mjtGeom.mjGEOM_CAPSULE) else np.sqrt(np.maximum(0., 1. - axis ** 2))
        extent = size[0] * radial + size[1] * axis
    else:
        raise ValueError('桌面/夹爪包含不支持的碰撞几何，无法计算高度底限')
    return position - extent, position + extent


def table_floor(runtime, point):
    """Find the top of named static table geometry below target XY."""
    model, data = runtime.model, runtime.data
    heights = []
    for geom in range(model.ngeom):
        body = int(model.geom_bodyid[geom])
        names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, geom) or '']
        ancestor = body
        while ancestor > 0:
            names.append(mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, ancestor) or '')
            ancestor = int(model.body_parentid[ancestor])
        if int(model.body_weldid[body]) != 0 or not any('table' in name.lower() for name in names):
            continue
        lower, upper = geom_world_bounds(model, data, geom)
        if np.all(np.asarray(point)[:2] >= lower[:2]) and np.all(np.asarray(point)[:2] <= upper[:2]):
            heights.append(float(upper[2]))
    if not heights:
        raise ValueError('目标下方没有可识别的静态桌面几何，无法确定抓取高度底限')
    return max(heights)


def gripper_clearance(runtime, floor_z):
    """Measure the whole gripper at the supplied static configuration."""
    model, data = runtime.model, runtime.data
    lowest = None
    for geom in range(model.ngeom):
        if not (model.geom_contype[geom] or model.geom_conaffinity[geom]):
            continue
        body = int(model.geom_bodyid[geom])
        ancestor, is_tool = body, False
        while ancestor > 0:
            name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, ancestor) or ''
            if name.startswith('robotiq_2f85'):
                is_tool = True
                break
            ancestor = int(model.body_parentid[ancestor])
        if not is_tool:
            continue
        lower, _ = geom_world_bounds(model, data, geom)
        if lowest is None or lower[2] < lowest[0]:
            lowest = (float(lower[2]), mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body))
    if lowest is None:
        raise ValueError('找不到夹爪碰撞几何，无法验证桌面底限')
    clearance = lowest[0] - float(floor_z)
    return {'success': clearance > 0., 'table_height_m': float(floor_z),
            'gripper_lowest_z_m': lowest[0], 'clearance_m': clearance,
            'lowest_link': lowest[1]}
