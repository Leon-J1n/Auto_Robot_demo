"""Capture final measured poses in the original editable scene schema."""
from copy import deepcopy

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from execution.resources import component


def final_scene(source, backend, state):
    # Start from the authored scene: runtime assembly removes attachments and
    # fuses their components into the robot, which cannot be exported verbatim.
    result = deepcopy(source)
    poses = state.get('objects', {})
    objects = {obj['object_id_in_scene']: obj for obj in result['objects']}
    for obj in result['objects']:
        name = obj['object_name_in_scene']
        transform = component(obj, 'Transform')
        measured = poses.get(name)
        if measured is not None and not component(obj, 'RobotAttachment'):
            position = np.asarray(measured['position'], dtype=float)
            rotation = Rotation.from_quat(measured['quaternion_xyzw'])
            parent = transform.get('parent')
            if parent is not None:
                parent_pose = poses.get(objects[parent]['object_name_in_scene'])
                if parent_pose is None:
                    raise ValueError(f'Cannot export local pose without measured parent: {name}')
                parent_rotation = Rotation.from_quat(parent_pose['quaternion_xyzw'])
                position = parent_rotation.inv().apply(position - parent_pose['position'])
                rotation = parent_rotation.inv() * rotation
            transform.update(position=position.tolist(), quaternion_xyzw=rotation.as_quat().tolist())
        driver = component(obj, 'RobotDriver')
        if not driver:
            continue
        values = {}
        for joint in driver.get('joint_sequence', []):
            candidates = (joint,) if obj['object_id_in_scene'] == backend.resources.robot_object['object_id_in_scene'] or component(obj, 'RobotAttachment') else (f'{name}__{joint}', joint)
            for candidate in candidates:
                ident = mujoco.mj_name2id(backend.model, mujoco.mjtObj.mjOBJ_JOINT, candidate)
                if ident >= 0:
                    values[joint] = float(backend.data.qpos[backend.model.jnt_qposadr[ident]])
                    break
            else:
                raise ValueError(f'Cannot export unmeasured scene joint: {name}/{joint}')
        if values:
            driver['initial_joint_positions_rad'] = values.copy()
            driver['now_joint_positions_rad'] = values.copy()
    return result
