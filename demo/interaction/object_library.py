"""Demo object instance -> OLS interaction poses -> world control poses."""
import re
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from execution.resources import component
from kinematics import IKError


def _periodic_joint_delta(q, reference):
    """Return the shortest per-joint angular displacement in radians."""
    delta = np.asarray(q, dtype=float) - np.asarray(reference, dtype=float)
    return np.arctan2(np.sin(delta), np.cos(delta))


class GraspResolutionError(RuntimeError):
    pass


def rigid_pose(position, quaternion_xyzw):
    p, q = np.asarray(position, dtype=float), np.asarray(quaternion_xyzw, dtype=float)
    if p.shape != (3,) or q.shape != (4,) or not np.isfinite(np.r_[p, q]).all() or not np.isclose(np.linalg.norm(q), 1):
        raise ValueError('Pose requires finite xyz and unit xyzw quaternion')
    t = np.eye(4); t[:3, :3] = Rotation.from_quat(q).as_matrix(); t[:3, 3] = p
    return t


def scene_object_by_id(scene, target_id):
    if isinstance(target_id, bool) or not isinstance(target_id, (int, str)):
        raise GraspResolutionError('target must be a scene object ID')
    matches = [obj for obj in scene['objects'] if str(obj['object_id_in_scene']) == str(target_id)]
    if len(matches) != 1:
        raise GraspResolutionError(f'Unknown or ambiguous scene target ID: {target_id}')
    return matches[0]


def model_id_for(obj):
    ref = component(obj, 'MetadataRef')
    if ref.get('type') != 'model':
        raise GraspResolutionError('Target requires MetadataRef type=model')
    value = ref.get('object_id')
    match = re.fullmatch(r'\$LIBRARY_SERVER/(\d+)', ref.get('path', ''))
    if value is None and match:
        value = int(match[1])
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (match and int(match[1]) != value):
        raise GraspResolutionError('Invalid/conflicting model ID in MetadataRef')
    return value


def control_from_grasp_frame(resources):
    """Evaluate fixed URDF transforms from controlled tip to configured pinch frame."""
    ref = component(resources.robot_object, 'RobotModelRef')
    grasp = ref.get('grasp_frame')
    if not grasp:
        raise GraspResolutionError('RobotModelRef.grasp_frame calibration is required')
    root = resources.robot_xml if resources.robot_xml is not None else ET.parse(resources.robot_urdf).getroot()
    parents = {j.find('child').get('link'): j for j in root.findall('joint')}
    link = grasp['link']
    t = rigid_pose(grasp.get('position', [0, 0, 0]), grasp.get('quaternion_xyzw', [0, 0, 0, 1]))
    visited = set()
    while link != ref['tip_link']:
        if link in visited or link not in parents:
            raise GraspResolutionError('Grasp frame must descend from tip_link')
        visited.add(link); j = parents[link]
        if j.get('type') != 'fixed':
            raise GraspResolutionError('Current grasp frame adapter supports fixed tool joints only')
        origin = j.find('origin'); props = origin.attrib if origin is not None else {}
        xyz = [float(x) for x in props.get('xyz', '0 0 0').split()]
        rpy = [float(x) for x in props.get('rpy', '0 0 0').split()]
        t = rigid_pose(xyz, Rotation.from_euler('xyz', rpy).as_quat()) @ t
        link = j.find('parent').get('link')
    tip_from_control = np.asarray(ref.get('tcp_transform', np.eye(4)), dtype=float)
    return np.linalg.inv(tip_from_control) @ t


def world_grasp_candidates(metadata, world_from_object, control_from_grasp):
    """Confirmed OLS convention: metres, fixed-axis XYZ Euler radians, object frame."""
    properties = metadata.get('grasp_properties', {})
    poses = properties.get('poses')
    if not isinstance(poses, list) or not poses or properties.get('pose_count') != len(poses):
        raise GraspResolutionError('OLS grasp poses are empty or pose_count is inconsistent')
    candidates = []
    for index, values in enumerate(poses):
        pose = np.asarray(values, dtype=float)
        if pose.shape != (10,) or not np.isfinite(pose).all() or pose[9] < 0:
            raise GraspResolutionError(f'Invalid OLS grasp pose at index {index}')
        if not np.isclose(np.linalg.norm(pose[6:9]), 1):
            raise GraspResolutionError(f'Invalid tolerance axis at index {index}')
        object_from_grasp = rigid_pose(pose[:3], Rotation.from_euler('xyz', pose[3:6]).as_quat())
        world_from_grasp = world_from_object @ object_from_grasp
        candidates.append({'grasp_index': index, 'model_pose': pose.tolist(),
            'world_from_object': world_from_object.tolist(),
            'world_from_grasp': world_from_grasp.tolist(),
            'world_from_control': (world_from_grasp @ np.linalg.inv(control_from_grasp)).tolist()})
    return candidates


class ObjectLibraryGraspResolver:
    def __init__(self, client, blackboard=None):
        from execution.blackboard import TargetBlackboard
        self.client = client
        self.blackboard = blackboard if blackboard is not None else TargetBlackboard()

    def remember_selection(self, resolved):
        self.blackboard.remember_selection(resolved)

    def resolve(self, target_id, resources, state):
        obj = scene_object_by_id(resources.scene, target_id)
        model_id = model_id_for(obj)
        self.blackboard.observe(state)
        self.blackboard.register(obj['object_id_in_scene'], obj['object_name_in_scene'], model_id)
        pose = self.blackboard.pose(obj['object_name_in_scene'])
        if pose is None:
            raise GraspResolutionError('Target has no measured world pose')
        metadata = self.blackboard.model(self.client, model_id)
        transform = control_from_grasp_frame(resources)
        candidates = world_grasp_candidates(metadata, rigid_pose(pose['position'], pose['quaternion_xyzw']), transform)
        for candidate in candidates:
            candidate['pose_source'] = 'blackboard_object'
            current = self.blackboard.grasp_pose(obj['object_name_in_scene'], candidate['grasp_index'])
            if current is not None:
                candidate['world_from_grasp'] = current.tolist()
                candidate['world_from_control'] = (current @ np.linalg.inv(transform)).tolist()
                candidate['pose_source'] = 'blackboard_contact_link'
        return {'target_id': obj['object_id_in_scene'], 'target_name': obj['object_name_in_scene'],
            'model_id': model_id, 'model_name': metadata['semantic_properties']['object_name'],
            'source_url': self.client.base_url, 'control_from_grasp': transform.tolist(),
            'blackboard_revision': self.blackboard.revision, 'candidates': candidates}

    @staticmethod
    def select(resolved, kin, checker, seed, *, safety_distance=.003, grasp_index=None, prefer_direct=False, candidate_validator=None):
        from planning.arm_posture import configuration_cost, preference_for
        options, rejected = [], []
        candidates = resolved['candidates']
        if grasp_index is not None:
            if isinstance(grasp_index, bool) or not isinstance(grasp_index, int) or not 0 <= grasp_index < len(candidates):
                raise GraspResolutionError('grasp_index is outside the model candidate list')
            candidates = [candidates[grasp_index]]
        else:
            # Cheap conservative pre-pass: remove byte-identical OLS poses and
            # try nearer control poses first. It never discards a distinct
            # candidate, so the complete IK/collision pass retains its original
            # coverage while repeated candidates are not checked twice.
            unique = []
            seen = set()
            seed_pose = kin.forward(seed)
            seed_position = np.asarray(seed_pose['position'], dtype=float)
            for candidate in candidates:
                pose = np.asarray(candidate['world_from_control'], dtype=float)
                marker = pose.tobytes()
                if marker in seen:
                    continue
                seen.add(marker)
                unique.append(candidate)
            candidates = sorted(unique, key=lambda item: float(np.linalg.norm(
                np.asarray(item['world_from_control'], dtype=float)[:3, 3] - seed_position)))
        for candidate in candidates:
            t = np.asarray(candidate['world_from_control'])
            try:
                result = kin.solve_all({'position': t[:3, 3], 'rotation_matrix': t[:3, :3]}, seed=seed, max_solutions=8)
            except IKError as exc:
                rejected.append({'grasp_index': candidate['grasp_index'], 'reason': 'ik_failed', 'message': str(exc)})
                continue
            for q in result.solutions:
                travel = float(np.linalg.norm(_periodic_joint_delta(q, seed)))
                cost = configuration_cost(kin, q, seed, norm=2)
                options.append((cost, travel, candidate, q, result.solver))
        if not options:
            error = GraspResolutionError(f'No reachable collision-free grasp candidate: {rejected}')
            reasons = {item['reason'] for item in rejected}
            error.details = {
                'error_code': ('TARGET_POSE_UNREACHABLE' if reasons == {'ik_failed'} else
                               'TARGET_POSE_COLLISION' if reasons == {'goal_collision'} else
                               'NO_VALID_TARGET_POSE'),
                'failed_stage': 'target_resolution',
                'error_message': str(error),
                'rejected': rejected,
            }
            raise error
        options.sort(key=lambda item: item[0])
        collision_free = []
        had_collision_free = False
        closure_evidence = None
        for option in options:
            _, _, candidate, q, _ = option
            hit = checker.check(q, safety_distance=safety_distance)
            if hit.in_collision:
                rejected.append({'grasp_index': candidate['grasp_index'], 'reason': 'goal_collision', 'contacts': hit.contacts})
                continue
            had_collision_free = True
            if candidate_validator is not None:
                evidence = candidate_validator(q, candidate)
                if not evidence['success']:
                    rejected.append({'grasp_index': candidate['grasp_index'],
                                     'joint_positions': q.tolist(), **evidence})
                    continue
                closure_evidence = evidence
            collision_free.append(option)
            if candidate_validator is not None or not prefer_direct:
                break
        if not collision_free and candidate_validator is not None and had_collision_free:
            error = GraspResolutionError('候选抓取位姿的几何或接触路径检查均未通过')
            error.details = {'error_code': 'GRASP_PRECHECK_FAILED', 'failed_stage': 'grasp_precheck',
                             'recoverable': True, 'rejected': rejected}
            raise error
        if not collision_free:
            error = GraspResolutionError(f'No reachable collision-free grasp candidate: {rejected}')
            reasons = {item['reason'] for item in rejected}
            error.details = {
                'error_code': ('TARGET_POSE_UNREACHABLE' if reasons == {'ik_failed'} else
                               'TARGET_POSE_COLLISION' if reasons == {'goal_collision'} else
                               'NO_VALID_TARGET_POSE'),
                'failed_stage': 'target_resolution',
                'error_message': str(error),
                'rejected': rejected,
            }
            raise error
        options = collision_free
        choice = options[0]
        for option in options if prefer_direct else ():
            if not checker.check_path([seed, option[3]], resolution=.04, safety_distance=safety_distance).in_collision:
                choice = option
                break
        _, travel, selected, joints, solver = choice
        posture = preference_for(kin)
        return {**resolved, 'selected': selected, 'joint_positions': joints.tolist(),
                'ik_solver': solver, 'joint_travel_rad': travel, 'rejected': rejected,
                'closure_validation': closure_evidence,
                **({'arm_posture': posture.metrics(joints)} if posture is not None else {})}


def apply_ols_friction(resources, client, blackboard=None):
    """Copy validated OLS material friction into the runtime scene model."""
    def read(model_id, asset_type='model'):
        metadata = (client.get_robot(model_id) if asset_type == 'robot' else
                    blackboard.model(client, model_id) if blackboard is not None else client.get_model(model_id))
        friction = metadata.get('physical_properties', {}).get('friction', {})
        values = friction.get('values') if isinstance(friction, dict) else None
        if not isinstance(values, list) or len(values) != 3:
            raise GraspResolutionError(f'OLS model {model_id} has invalid physical friction metadata')
        values = [float(value) for value in values]
        if not np.isfinite(values).all() or any(value < 0 for value in values):
            raise GraspResolutionError(f'OLS model {model_id} has invalid physical friction values')
        return values

    for obj in resources.scene['objects']:
        ref = component(obj, 'MetadataRef')
        path = ref.get('path', '')
        if ref.get('type') != 'model' or not isinstance(path, str) or not path.startswith('$LIBRARY_SERVER/'):
            continue
        try:
            model_id = int(path.rsplit('/', 1)[1])
        except ValueError as exc:
            raise GraspResolutionError('Object MetadataRef path must contain an integer OLS model ID') from exc
        obj['_ols_friction'] = read(model_id)

    gripper = component(resources.robot_object, 'GripperModel')
    model_id = gripper.get('_ols_model_id')
    if model_id is not None:
        gripper['_ols_friction'] = read(int(model_id), gripper.get('_ols_asset_type', 'model'))
