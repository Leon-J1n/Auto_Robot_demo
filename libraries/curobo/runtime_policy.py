"""Export measured MuJoCo geometry, kinematics, and the host's frozen policy."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from collision.planning_policy import freeze_policy, planning_signature
from .conversion.assets import ConversionError


def matrix(position, rotation) -> np.ndarray:
    result = np.eye(4)
    result[:3, :3] = np.asarray(rotation).reshape(3, 3)
    result[:3, 3] = position
    return result


def export_runtime(runtime, checker, constraints: dict, cache_dir=None) -> dict:
    """Snapshot one motion without modifying the simulator or live policy."""
    import mujoco

    model = runtime.model
    initial_signature = planning_signature(runtime, checker)
    policy, data = freeze_policy(checker, runtime.get_joint_positions(),
        float(constraints.get('safety_distance', 0.)),
        avoid_collision=constraints.get('avoid_collision', True))
    cache = Path(cache_dir or Path(__file__).resolve().parents[2] / '.cache/curobo') / 'runtime_geometry'
    cache.mkdir(parents=True, exist_ok=True)
    root = int(runtime.base_id)
    robot_bodies = set(checker.robot_bodies)
    controller = runtime.gripper_controller
    held = getattr(controller, 'held_name', None)
    held_root = getattr(runtime, 'object_ids', {}).get(held)
    attached = set()
    if held_root is not None:
        joint = int(model.body_jntadr[held_root])
        if joint >= 0 and model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_FREE:
            for body in range(1, model.nbody):
                ancestor = body
                while ancestor:
                    if ancestor == held_root:
                        attached.add(body)
                        break
                    ancestor = int(model.body_parentid[ancestor])
    assembly = robot_bodies | attached
    world = {i: matrix(data.xpos[i], data.xmat[i]) for i in range(model.nbody)}
    active = {int(j): i for i, j in enumerate(runtime.joint_ids)}
    bodies = []
    for body in sorted(assembly):
        if body == root:
            bodies.append({'id': body, 'parent': None})
            continue
        parent = int(controller.base_id) if body == held_root and body in attached else int(model.body_parentid[body])
        if parent not in assembly:
            raise ConversionError(f'Runtime robot body has an unmodelled parent: {body}/{parent}')
        local = np.linalg.inv(world[parent]) @ world[body]
        joints = [j for j in range(int(model.body_jntadr[body]),
                    int(model.body_jntadr[body]) + int(model.body_jntnum[body])) if j in active]
        entry = {'id': body, 'parent': parent, 'matrix': local.tolist()}
        if joints:
            if len(joints) != 1 or model.body_jntnum[body] != 1:
                raise ConversionError('A planned runtime body must have one hinge or slide joint')
            joint = joints[0]
            if model.jnt_type[joint] not in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE):
                raise ConversionError('Only hinge and slide arm joints are supported')
            q = float(data.qpos[model.jnt_qposadr[joint]])
            axis = model.jnt_axis[joint].copy()
            pivot = np.eye(4); pivot[:3, 3] = model.jnt_pos[joint]
            motion = np.eye(4)
            slide = model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_SLIDE
            if slide:
                motion[:3, 3] = -axis * q
            else:
                motion[:3, :3] = Rotation.from_rotvec(-axis * q).as_matrix()
            # Measured parent->body = origin * motion(q) * inverse(pivot).
            # Remove q, not q-qref: this also preserves MuJoCo reference angles.
            entry.update(matrix=(local @ pivot @ motion).tolist(), pivot=pivot.tolist(),
                joint_name=runtime.joint_names[active[joint]], axis=axis.tolist(),
                kind='prismatic' if slide else 'revolute',
                limit=np.asarray(runtime.joint_limits[active[joint]]).tolist())
        bodies.append(entry)
    geometries = []
    for geom in checker.geoms:
        body = int(model.geom_bodyid[geom])
        kind = int(model.geom_type[geom])
        entry = {'id': int(geom), 'body': body, 'robot': body in assembly,
                 'name': checker.geom_names[geom], 'body_name': checker.body_names[body],
                 'matrix': (np.linalg.inv(world[body]) @ matrix(data.geom_xpos[geom], data.geom_xmat[geom])).tolist()
                           if body in assembly else matrix(data.geom_xpos[geom], data.geom_xmat[geom]).tolist(),
                 'size': model.geom_size[geom].tolist()}
        kinds = {mujoco.mjtGeom.mjGEOM_BOX: 'box', mujoco.mjtGeom.mjGEOM_SPHERE: 'sphere',
                 mujoco.mjtGeom.mjGEOM_CYLINDER: 'cylinder', mujoco.mjtGeom.mjGEOM_CAPSULE: 'capsule',
                 mujoco.mjtGeom.mjGEOM_ELLIPSOID: 'ellipsoid', mujoco.mjtGeom.mjGEOM_PLANE: 'plane'}
        if kind == mujoco.mjtGeom.mjGEOM_MESH:
            mesh = int(model.geom_dataid[geom])
            start, count = int(model.mesh_vertadr[mesh]), int(model.mesh_vertnum[mesh])
            vertices = np.asarray(model.mesh_vert[start:start + count])
            key = hashlib.sha256(vertices.tobytes()).hexdigest()
            path = cache / f'{key}.obj'
            if not path.exists():
                import trimesh
                # mj_geomDistance uses the convex collision hull for mesh geoms.
                trimesh.convex.convex_hull(vertices).export(path)
            entry.update(kind='mesh', file_path=str(path))
        elif kind in kinds:
            entry['kind'] = kinds[kind]
        else:
            raise ConversionError(f'Unsupported runtime collision geometry: {entry["name"]}/{kind}')
        geometries.append(entry)
    site = int(checker.tcp_site_id)
    tcp_body = int(model.site_bodyid[site])
    if planning_signature(runtime, checker) != initial_signature:
        raise ConversionError('Runtime or collision policy changed while capturing the planning scene')
    return {'schema_version': 1, 'policy': policy, 'state_signature': initial_signature,
        'bodies': bodies, 'geometries': geometries,
        'root': root, 'base_world': world[root].tolist(), 'payload_bodies': sorted(attached),
        'joint_names': list(runtime.joint_names), 'joints': runtime.get_joint_positions().tolist(),
        'tcp_body': tcp_body, 'tcp_local': matrix(model.site_pos[site],
            Rotation.from_quat(model.site_quat[site][[1, 2, 3, 0]]).as_matrix()).tolist(),
        'tcp_world': matrix(data.site_xpos[site], data.site_xmat[site]).tolist()}
