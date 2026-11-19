"""Read the host execution state into a copy of its original scene snapshot."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from .conversion.assets import component, ConversionError


def ensure_component(obj,kind):
    for entry in obj.setdefault('components',[]):
        if entry.get('component_type')==kind:
            return entry.setdefault('properties',{})
    result={}
    obj['components'].append({'component_type':kind,'properties':result})
    return result


def runtime_snapshot(runtime, *, collision_checker=None, constraints=None, cache_dir=None):
    resources=getattr(runtime,'resources',None)
    if resources is None:
        raise ConversionError('A runtime must provide resources.scene and robot_object')
    scene=deepcopy(resources.scene)
    # The existing compiler removes attachments/cameras. Restore them only in this bridge;
    # externally supplied current JSON is never replaced by the OLS initial scene.
    source_path=getattr(resources,'scene_file',None)
    if source_path and Path(source_path).is_file():
        original=json.loads(Path(source_path).read_text('utf-8'))
        current={o['object_id_in_scene']:o for o in scene['objects']}
        for obj in original.get('objects',[]):
            if obj['object_id_in_scene'] not in current and (
                component(obj,'RobotAttachment') or component(obj,'Camera') or component(obj,'Light')):
                scene['objects'].append(deepcopy(obj))
    state=runtime.get_state()
    arm_id=resources.robot_object['object_id_in_scene']
    for obj in scene['objects']:
        ident=obj['object_id_in_scene']
        name=obj.get('object_name_in_scene')
        transform=ensure_component(obj,'Transform')
        if ident==arm_id:
            matrix=np.asarray(state['base_transform'],dtype=float)
            transform.update(position=matrix[:3,3].tolist(),
                quaternion_xyzw=Rotation.from_matrix(matrix[:3,:3]).as_quat().tolist(),parent=None)
            ensure_component(obj,'RobotDriver')['now_joint_positions_rad']=dict(zip(state['joint_names'],state['joints']))
        elif not component(obj,'RobotAttachment') and name in state.get('objects',{}):
            measured=state['objects'][name]
            transform.update(position=list(measured['position']),
                quaternion_xyzw=list(measured['quaternion_xyzw']),parent=None)
        sequence=component(obj,'RobotDriver').get('joint_sequence',[])
        if ident!=arm_id and sequence:
            current={}
            for joint in sequence:
                import mujoco
                candidates=[joint] if component(obj,'RobotAttachment') else [f'{name}__{joint}',joint]
                jid=next((j for candidate in candidates
                          if (j:=mujoco.mj_name2id(runtime.model,mujoco.mjtObj.mjOBJ_JOINT,candidate))>=0),None)
                if jid is None:
                    raise ConversionError(f'Cannot read current runtime joint: {name}/{joint}')
                current[joint]=float(runtime.data.qpos[runtime.model.jnt_qposadr[jid]])
            ensure_component(obj,'RobotDriver')['now_joint_positions_rad']=current
    payload=state.get('gripper') or {}
    if collision_checker is not None and hasattr(collision_checker, 'tool_geoms'):
        from .runtime_policy import export_runtime
        scene['_runtime_collision'] = export_runtime(runtime, collision_checker, constraints or {}, cache_dir)
    elif payload.get('holding') or payload.get('object_id'):
        raise ConversionError('cuRobo free-space motion requires an empty gripper; held payload geometry must be explicitly assembled first')
    return scene,arm_id
