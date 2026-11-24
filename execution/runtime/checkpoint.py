"""Portable physical grasp checkpoint, keyed by scene object IDs."""
import hashlib
import json
from pathlib import Path

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from execution.resources import component


def asset_identity(backend):
    resources = backend.resources
    paths = [resources.robot_urdf]
    tool = component(resources.robot_object, 'GripperModel').get('urdf')
    if tool:
        paths.append(resources.asset(tool))
    version = resources.asset_root / 'VERSION.txt'
    if version.is_file():
        paths.append(version)
    return [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]


def capture(backend, request_id):
    c = backend.gripper_controller
    if c is None:
        return None
    state = c.get_state()
    if not state['holding']:
        return None
    source = json.loads(Path(backend.resources.scene_file).read_text())
    objects = {o['object_name_in_scene']: o for o in source['objects']}
    if c.held_name not in objects or c.hold_force_target is None:
        raise ValueError('Cannot checkpoint unverified held object or hold force')
    held = objects[c.held_name]
    body = backend.object_ids[c.held_name]
    tool = next(o for o in source['objects'] if component(o, 'RobotAttachment'))
    held_joint = int(backend.model.body_jntadr[body])
    if backend.model.jnt_type[held_joint] != mujoco.mjtJoint.mjJNT_FREE:
        raise ValueError('Checkpoint requires a free held object')
    velocity_address = int(backend.model.jnt_dofadr[held_joint])
    joints = {}
    for j in range(backend.model.njnt):
        if int(backend.model.jnt_type[j]) not in (2, 3):
            continue
        name = mujoco.mj_id2name(backend.model, mujoco.mjtObj.mjOBJ_JOINT, j)
        joints[name] = [float(backend.data.qpos[backend.model.jnt_qposadr[j]]),
                        float(backend.data.qvel[backend.model.jnt_dofadr[j]])]
    snapshot = dict(schema_version=1, source_request_id=request_id,
        source_scene_id=source['scene_id'], source_scene_version=source['scene_version'],
        gripper_object_id=tool['object_id_in_scene'], gripper_metadata=component(tool, 'MetadataRef'),
        robot_object_id=backend.resources.robot_object['object_id_in_scene'],
        held_object_id=held['object_id_in_scene'], asset_identity=asset_identity(backend),
        held_metadata=component(held, 'MetadataRef'), joints=joints,
        relative_pose=c._relative(body).tolist(), hold_force_target=c.hold_force_target,
        gripper_control=float(backend.data.ctrl[c.actuator_id]),
        held_velocity=backend.data.qvel[velocity_address:velocity_address + 6].tolist(),
        filtered_hold_force=c.filtered_hold_force, measured_width=state['width'],
        confirmation=dict(c.evidence), mode='physical')
    return json.loads(json.dumps(snapshot, default=lambda value: value.item(), allow_nan=False))


def restore(backend, snapshot):
    if snapshot is None:
        return
    c = backend.gripper_controller
    try:
        if (snapshot['schema_version'] != 1 or snapshot['mode'] != 'physical'
                or snapshot['robot_object_id'] != backend.resources.robot_object['object_id_in_scene']
                or snapshot['asset_identity'] != asset_identity(backend) or not snapshot['source_request_id']):
            raise ValueError('robot or asset identity changed')
        expected_joints = {mujoco.mj_id2name(backend.model, mujoco.mjtObj.mjOBJ_JOINT, j)
                           for j in range(backend.model.njnt)
                           if int(backend.model.jnt_type[j]) in (2, 3)}
        if set(snapshot['joints']) != expected_joints or not snapshot['confirmation'].get('confirmed'):
            raise ValueError('incomplete or unconfirmed grasp checkpoint')
        obj = next(o for o in backend.resources.scene['objects']
                   if o['object_id_in_scene'] == snapshot['held_object_id'])
        if component(obj, 'MetadataRef') != snapshot['held_metadata']:
            raise ValueError('held asset changed')
        source = json.loads(Path(backend.resources.scene_file).read_text())
        tool = next(o for o in source['objects'] if o['object_id_in_scene'] == snapshot['gripper_object_id'])
        if (source['scene_id'] != snapshot['source_scene_id']
                or component(tool, 'MetadataRef') != snapshot['gripper_metadata']
                or component(tool, 'RobotAttachment').get('parent_object_id') != snapshot['robot_object_id']):
            raise ValueError('scene or gripper identity changed')
        name = obj['object_name_in_scene']
        body = backend.object_ids[name]
        # Validate before restoring the extra linkage DOFs, which are not
        # represented by the editor's single gripper driver coordinate.
        for joint, (position, velocity) in snapshot['joints'].items():
            j = backend._id(mujoco.mjtObj.mjOBJ_JOINT, joint)
            if not np.isfinite([position, velocity]).all():
                raise ValueError('nonfinite joint state')
            address = int(backend.model.jnt_qposadr[j])
            if address in backend.qpos_indices or address == c.driver_address:
                if abs(backend.data.qpos[address] - position) > 1e-6:
                    raise ValueError('scene joint state changed')
            backend.data.qpos[address] = position
            backend.data.qvel[backend.model.jnt_dofadr[j]] = velocity
        mujoco.mj_forward(backend.model, backend.data)
        relative = np.asarray(snapshot['relative_pose'], dtype=float)
        actual = c._relative(body)
        if (relative.shape != (4, 4) or not np.isfinite(relative).all()
                or np.linalg.norm(relative[:3, 3] - actual[:3, 3]) > .002
                or Rotation.from_matrix(relative[:3, :3].T @ actual[:3, :3]).magnitude() > .02):
            raise ValueError('held object pose changed')
        control = float(snapshot['gripper_control'])
        if not np.isfinite(control) or not 0 <= control <= .8:
            raise ValueError('invalid gripper control')
        c.held_name = name
        c.relative_pose = relative.copy()
        c.confirmed = True
        c.evidence = dict(snapshot['confirmation'])
        c.hold(snapshot['hold_force_target'])
        filtered = snapshot.get('filtered_hold_force')
        if filtered is not None and (not np.isfinite(filtered) or filtered < 0):
            raise ValueError('invalid measured hold force')
        c.filtered_hold_force = filtered
        velocity = np.asarray(snapshot['held_velocity'], dtype=float)
        if velocity.shape != (6,) or not np.isfinite(velocity).all():
            raise ValueError('invalid held object velocity')
        joint = int(backend.model.body_jntadr[body])
        if backend.model.body_jntnum[body] != 1 or backend.model.jnt_type[joint] != mujoco.mjtJoint.mjJNT_FREE:
            raise ValueError('held object is no longer free')
        address = int(backend.model.jnt_dofadr[joint])
        backend.data.qvel[address:address + 6] = velocity
        backend.data.ctrl[c.actuator_id] = control
        backend.advance_gripper(.15)
        measured = c.get_state()
        if (not measured['holding'] or measured['slip_detected']
                or measured['relative_position_error_m'] > .003
                or measured['relative_orientation_error_rad'] > .05):
            raise ValueError('bilateral grasp did not recover')
    except Exception as exc:
        if c is not None:
            c.detach()
        raise ValueError(f'GRASP_RESTORE_FAILED: {exc}') from exc
