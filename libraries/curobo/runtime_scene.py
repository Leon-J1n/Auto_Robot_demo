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


class FixedSimulationScene:
    """RoboTwin-style fixed robot and table for a single MuJoCo demo run."""

    def __init__(self, runtime, checker):
        from execution.backends.mujoco.backend import MuJoCoExecutionBackend
        if not isinstance(runtime, MuJoCoExecutionBackend):
            raise ConversionError('Fixed cuRobo scene is only supported in MuJoCo simulation')
        self.runtime, self.checker = runtime, checker
        self.template = None

    def snapshot(self, constraints, cache_dir):
        from collision.planning_policy import planning_signature
        from .conversion.assets import fingerprint
        from .runtime_policy import matrix

        runtime, checker = self.runtime, self.checker
        if not constraints.get('avoid_collision', True):
            raise ConversionError('Fixed cuRobo simulation scene requires collision checking')
        before = planning_signature(runtime, checker)
        if self.template is None:
            scene, arm_id = runtime_snapshot(runtime, collision_checker=checker,
                constraints={**constraints, 'avoid_collision': True}, cache_dir=cache_dir)
            frozen = scene['_runtime_collision']
            if frozen['payload_bodies']:
                raise ConversionError('Initialize the fixed cuRobo scene before grasping a payload')
            table_bodies = {int(body) for name, body in runtime.object_ids.items()
                            if name.lower().replace('_', '') in {'table', 'worktable'}}
            for body in range(1, runtime.model.nbody):
                if int(runtime.model.body_parentid[body]) in table_bodies:
                    table_bodies.add(body)
            geometries = [geom for geom in frozen['geometries']
                          if geom['robot'] or geom['body'] in table_bodies]
            if not any(not geom['robot'] for geom in geometries):
                raise ConversionError('Fixed cuRobo scene requires collision geometry for Table or WorkTable')
            frozen['geometries'] = geometries
            kept = {geom['id'] for geom in geometries}
            policy = frozen['policy']
            for key in ('allowed_pairs', 'distance_overrides', 'arm_environment_pairs'):
                policy[key] = [entry for entry in policy.get(key, ()) if set(entry[:2]) <= kept]
            frozen.pop('body_world', None)
            frozen['fixed_simulation_scene'] = True
            self.template = scene, arm_id

        scene, arm_id = self.template
        frozen = scene['_runtime_collision']
        site = checker.tcp_site_id
        base = matrix(runtime.data.xpos[runtime.base_id], runtime.data.xmat[runtime.base_id]).tolist()
        if not np.allclose(base, frozen['base_world'], rtol=0., atol=1e-8):
            raise ConversionError('Robot base moved after capturing the fixed cuRobo scene')
        policy = {**frozen['policy'], 'safety_distance': float(constraints['safety_distance'])}
        policy.pop('fingerprint', None)
        policy.pop('collision_override', None)
        policy['fingerprint'] = fingerprint(policy)
        current = {**frozen, 'policy': policy, 'state_signature': before,
            'joints': runtime.get_joint_positions().tolist(),
            'tcp_world': matrix(runtime.data.site_xpos[site], runtime.data.site_xmat[site]).tolist()}
        if before != planning_signature(runtime, checker):
            raise ConversionError('Runtime changed while reading fixed-scene start state')
        return {**scene, '_runtime_collision': current}, arm_id


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
