"""Resolve link-local mechanism requests from the measured scene, never stale poses."""
from copy import deepcopy
import numpy as np
import mujoco
from scipy.spatial.transform import Rotation
from demo.interaction.object_library import scene_object_by_id
from execution.resources import component


def body_name(resources, parameters):
    obj = scene_object_by_id(resources.scene, parameters['target'])
    links = component(obj, 'ArticulatedModel').get('links', [])
    link = parameters.get('link')
    if link is None:
        if len(links) != 1:
            raise ValueError(f'Mechanism target requires a link when {len(links)} links are declared')
        link = links[0]
    if link not in links:
        raise ValueError(f'Unknown articulated link: {link}')
    return obj['object_name_in_scene'] + '__' + link


def resolve_held_contact(resources, backend, parameters):
    """Resolve an omitted contact target from an existing grasp, never a guess."""
    result = deepcopy(parameters)
    if result.get('target') is not None:
        return result
    gripper = getattr(backend, 'gripper_controller', None)
    name = getattr(gripper, 'held_name', None)
    state = gripper.get_state() if gripper else {}
    if not state.get('holding'):
        raise ValueError('A target-free push/pull requires an existing grasp')
    for obj in resources.scene['objects']:
        for link in component(obj, 'ArticulatedModel').get('links', []):
            if obj['object_name_in_scene'] + '__' + link == name:
                result.update(target=obj['object_id_in_scene'], link=link)
                result.setdefault('grasp', {'mode':state['mode']})
                return result
    raise ValueError('Held contact is not a registered manipulation target')


def relative_tool_motion(backend, parameters, operation):
    """Build a displacement from the measured TCP, without object joint data."""
    linear = 'distance' in parameters
    if linear == ('angle_deg' in parameters):
        raise ValueError('Specify either distance or angle_deg for relative motion')
    if any(key in parameters for key in ('target_position_m', 'target_angle_deg')):
        raise ValueError('Relative motion cannot include an absolute mechanism goal')
    frame = parameters.get('frame', 'tool')
    if frame not in {'tool', 'world'}:
        raise ValueError('Relative motion frame must be tool or world')
    pose = backend.get_end_effector_pose()
    rotation = np.asarray(pose['rotation_matrix']) if frame == 'tool' else np.eye(3)
    def vector(value, name):
        array = np.asarray(value, dtype=float)
        if array.shape != (3,) or not np.all(np.isfinite(array)):
            raise ValueError(f'{name} must be a finite xyz vector')
        return rotation @ array
    if linear:
        distance = parameters['distance']
        if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not np.isfinite(distance) or distance <= 0:
            raise ValueError('distance must be positive metres')
        direction = vector(parameters.get('direction', [0,0,-1 if operation == 'pull' else 1]), 'direction')
        norm = np.linalg.norm(direction)
        if norm < 1e-12:
            raise ValueError('direction must not be zero')
        return 'linear', {'direction':(direction/norm).tolist(), 'distance':float(distance)}
    angle = parameters['angle_deg']
    if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not np.isfinite(angle) or angle == 0:
        raise ValueError('angle_deg must be a finite nonzero increment')
    center = np.asarray(pose['position']) + vector(parameters.get('arc_center_offset'), 'arc_center_offset')
    axis = vector(parameters.get('arc_axis'), 'arc_axis')
    if np.linalg.norm(axis) < 1e-12:
        raise ValueError('arc_axis must not be zero')
    return 'circular', {'arc_center':center.tolist(), 'arc_axis':(axis/np.linalg.norm(axis)).tolist(),
                        'arc_angle':float(np.deg2rad(angle))}


def _handle_pose(backend, body):
    """Infer a grasp frame from a bar and its rigid support geoms on one link."""
    model = backend.model
    geoms = [g for g in range(model.ngeom) if int(model.geom_bodyid[g]) == body]
    bars = [g for g in geoms if 'graspable_handle_bar_' in
            (mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g) or '')]
    supports = [g for g in geoms if 'graspable_handle_support_' in
                (mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g) or '')]
    if len(bars) != 1 or not supports:
        raise ValueError('Automatic pull needs one handle bar and its supports on the selected link')
    bar = bars[0]
    center = np.asarray(model.geom_pos[bar], dtype=float)
    rotation = Rotation.from_quat(np.asarray(model.geom_quat[bar])[[1, 2, 3, 0]]).as_matrix()
    long_axis = int(np.argmax(model.geom_size[bar, :3]))
    offset = rotation.T @ (center - np.mean(model.geom_pos[supports], axis=0))
    offset[long_axis] = 0.0
    normal_axis = int(np.argmax(np.abs(offset)))
    if abs(offset[normal_axis]) < 0.002:
        raise ValueError('Handle supports do not determine a unique outward normal')
    normal = rotation[:, normal_axis] * np.sign(offset[normal_axis])
    x_axis = rotation[:, long_axis]
    z_axis = -normal
    y_axis = np.cross(z_axis, x_axis)
    if np.linalg.norm(y_axis) < 0.99:
        raise ValueError('Handle bar axis and outward normal are not perpendicular')
    y_axis /= np.linalg.norm(y_axis)
    x_axis = np.cross(y_axis, z_axis)
    grasp_rotation = np.column_stack((x_axis, y_axis, z_axis))
    return center, normal, Rotation.from_matrix(grasp_rotation).as_quat().tolist()


def prepare_contact_command(resources, backend, parameters, operation='pull'):
    """Prepare handle approach with grasp or ungrasped pad contact."""
    from demo.task_commands.converter import _deep_merge

    result = deepcopy(parameters)
    contact_mode = result.get('contact_mode','grasp')
    if contact_mode not in ('grasp','contact'):
        raise ValueError('contact_mode must be grasp or contact')
    if contact_mode == 'contact':
        if operation != 'push':
            raise ValueError('Only push supports contact without grasping')
        if result.get('grasp') or result.get('grasp_request') or result.get('request', {}).get('grasp'):
            raise ValueError('contact mode cannot include grasp configuration')
    else:
        grasp_request = _deep_merge({'grasp': {'mode': 'physical'}}, result.get('grasp_request'))
        grasp_request['grasp']['mode'] = 'physical'
        result['grasp'] = {'mode': 'physical'}
        result['grasp_request'] = grasp_request
    result['contact_mode'] = contact_mode
    name = body_name(resources, result)
    if 'target_position_m' in result:
        raise ValueError('Use a relative distance instead of target_position_m')
    relative = 'distance' in result or 'angle_deg' in result
    if relative:
        relative_tool_motion(backend, result, operation)  # Validate before approaching/grasping.
    result.setdefault('orchestrate', 'target_angle_deg' in result or relative)
    if not result.get('orchestrate'):
        return result
    result.setdefault('request', {})
    gripper = getattr(backend, 'gripper_controller', None)
    if contact_mode == 'contact' and gripper and getattr(gripper,'held_name',None):
        raise ValueError('Release the held object before using contact push')
    if relative and gripper and gripper.get_state().get('holding'):
        result['request'] = _deep_merge({'constraints':{'maximum_force':60,'timeout':90}}, result['request'])
        return result
    pose_keys = ('contact',) if contact_mode == 'contact' else ('contact', 'contact_normal_local')
    auto_handle = any(key not in result for key in pose_keys)
    if auto_handle:
        center, normal, quaternion = _handle_pose(backend, backend.object_ids[name])
        result.setdefault('contact_normal_local', normal.tolist())
        contact_pose = {'position': center.tolist(), 'quaternion_xyzw': quaternion}
        result.setdefault('contact', {
            'planning_method': 'linear',
            'mechanism_pose': contact_pose,
                'request': {'constraints': {'safety_distance': 0.003, 'timeout': 90}},
        })
        result['request'] = _deep_merge({
            'constraints': {'maximum_force': 60,
                            'timeout': 90, 'normal_alignment_tolerance': 3},
        }, result.get('request'))
        if contact_mode == 'grasp':
            result['grasp_request'] = _deep_merge({
                'strategy_params': {'hold_force': 15, 'contact_threshold': 1},
                'constraints': {'timeout': 8},
            }, result.get('grasp_request'))
    return result


def prepare_pull_command(resources, backend, parameters):
    """Compatibility entry point for callers preparing a pull command."""
    return prepare_contact_command(resources, backend, parameters)


def motion_request(resources, backend, parameters):
    name = body_name(resources, parameters)
    if parameters.get('planning_method', 'joint') not in {'joint', 'linear'}:
        raise ValueError('Mechanism approach/retreat supports joint or linear move; use push/pull for arcs')
    body = backend.object_ids[name]
    pose = parameters['mechanism_pose']
    rotation = backend.data.xmat[body].reshape(3,3)
    world = np.eye(4)
    world[:3,3] = backend.data.xpos[body] + rotation @ np.asarray(pose['position'])
    local_rotation = (Rotation.from_quat(pose['quaternion_xyzw']) if 'quaternion_xyzw' in pose
                      else Rotation.from_euler('xyz', pose['rpy']))
    world[:3,:3] = rotation @ local_rotation.as_matrix()
    if pose.get('frame','grasp') == 'grasp':
        # Compute the fixed control-to-grasp calibration from the loaded model.
        site = mujoco.mj_name2id(backend.model,mujoco.mjtObj.mjOBJ_SITE,'grasp_center')
        if site < 0:
            raise ValueError('The robot has no calibrated grasp_center site')
        tcp=backend.get_end_effector_pose(); wt=np.eye(4);wg=np.eye(4)
        wt[:3,3],wt[:3,:3]=tcp['position'],tcp['rotation_matrix']
        wg[:3,3],wg[:3,:3]=backend.data.site_xpos[site],backend.data.site_xmat[site].reshape(3,3)
        world = world @ np.linalg.inv(np.linalg.inv(wt) @ wg)
    elif pose['frame'] != 'control':
        raise ValueError('mechanism_pose.frame must be grasp or control')
    from demo.task_commands.converter import _deep_merge
    q=Rotation.from_matrix(world[:3,:3]).as_quat()
    request=_deep_merge(resources.scene['move_defaults'],parameters.get('request',{}))
    request['target']={'type':'pose','frame':'world','position':dict(zip('xyz',world[:3,3])),
                       'orientation':dict(representation='quaternion',**dict(zip('xyzw',q)))}
    request['motion']={'path_type':parameters.get('planning_method','joint'),
                       'path_constraint':'hard' if parameters.get('planning_method','joint') == 'linear' else 'soft'}
    if request['motion']['path_type']=='linear':
        request['planning']={'mode':'direct','planning_time':5,'allow_replan':False}
    return request


def apply_global_motion_timing(resources, request):
    """Use the same scene joint velocity limits as move, unless explicitly capped."""
    request = deepcopy(request)
    defaults = resources.scene['move_defaults']
    from planning.time_parameterization import continuous_motion_limits
    count = len(resources.joint_names)
    velocity, acceleration = continuous_motion_limits(defaults, count)
    overrides = request.get('constraints', {})
    if 'max_joint_speed' in overrides:
        velocity = np.minimum(velocity, float(overrides['max_joint_speed']))
    if 'max_joint_acceleration' in overrides:
        cap = float(overrides['max_joint_acceleration'])
        acceleration = np.full(count, cap) if acceleration is None else np.minimum(acceleration, cap)
    for name, value in [('max_velocity', velocity), ('max_acceleration', acceleration)]:
        if value is not None and (value.shape != (count,) or not np.all(np.isfinite(value)) or np.any(value <= 0)):
            raise ValueError(f'Global {name} must contain one finite positive limit per joint')
    path = request.get('motion', {}).get('path_type', request.get('manipulation', {}).get('trajectory'))
    request.setdefault('strategy_params', {})['time_parameterization'] = {
        'source': 'scene.move_defaults', 'max_velocity': velocity.tolist(),
        'limit_source': deepcopy(defaults.get('planning', {}).get('time_parameterization', {}).get('limit_source')),
        'max_acceleration': None if acceleration is None else acceleration.tolist(),
        'task_speed_limit': overrides.get('angular_speed' if path == 'circular' else 'speed'),
    }
    return request


def push_pull_request(resources, backend, parameters, operation):
    name=body_name(resources,parameters);body=backend.object_ids[name]
    if 'target_position_m' in parameters:
        raise ValueError('Use a relative distance instead of target_position_m')
    request=deepcopy(parameters.get('request', {}))
    if 'contact_mode' in parameters:
        mode = parameters['contact_mode']
        if mode not in ('grasp', 'contact') or (mode == 'contact' and operation != 'push'):
            raise ValueError('contact_mode must be grasp, or contact for push')
        if mode == 'contact' and (parameters.get('grasp') or parameters.get('grasp_request') or request.get('grasp')):
            raise ValueError('contact mode cannot include grasp configuration')
        request.setdefault('manipulation', {})['contact_mode'] = 'push_contact' if mode == 'contact' else 'grasp'
        if mode == 'contact':
            request['manipulation']['maintain_contact'] = True
            request.setdefault('constraints', {})['stop_on_contact_loss'] = True
            request['constraints'].setdefault('contact_threshold', .1)
    if 'grasp' in parameters:
        request['grasp'] = deepcopy(parameters['grasp'])
    target=request.setdefault('target',{})
    target.update(type='mechanism',object_id=name)
    if 'distance' in parameters or 'angle_deg' in parameters:
        path, motion_params = relative_tool_motion(backend, parameters, operation)
        if request.get('manipulation', {}).get('trajectory', path) not in {'auto', path}:
            raise ValueError('Relative motion conflicts with the requested trajectory')
        request.setdefault('manipulation', {}).update(operation=operation, trajectory=path)
        request['motion'] = {'path_type':path}
        request.setdefault('strategy_params', {}).update(motion_params)
        if parameters.get('contact_geom'):
            target['contact_geom'] = parameters['contact_geom']
        return apply_global_motion_timing(resources, request)
    rotation=backend.data.xmat[body].reshape(3,3)
    if 'contact_normal_local' in parameters:
        target['contact_normal']=(rotation@np.asarray(parameters['contact_normal_local'])).tolist()
    if 'contact_geom' in parameters:
        target['contact_geom']=parameters['contact_geom']
    manipulation = request.setdefault('manipulation', {})
    manipulation.update(operation=operation, trajectory=manipulation.get('trajectory', 'circular'))
    # Carry the end-effector path explicitly.  A mechanism target can still
    # be driven by a straight tool path.
    request['motion'] = {'path_type': manipulation['trajectory']}
    joint=int(backend.model.body_jntadr[body])
    params=request.setdefault('strategy_params',{})
    if manipulation['trajectory'] == 'circular':
        params.setdefault('arc_center',backend.data.xanchor[joint].tolist())
        params.setdefault('arc_axis',backend.data.xaxis[joint].tolist())
    if 'target_angle_deg' in parameters:
        joint=int(backend.model.body_jntadr[body]);start=float(backend.data.qpos[backend.model.jnt_qposadr[joint]])
        request.setdefault('strategy_params',{})['arc_angle']=float(np.deg2rad(parameters['target_angle_deg'])-start)
    return apply_global_motion_timing(resources, request)


