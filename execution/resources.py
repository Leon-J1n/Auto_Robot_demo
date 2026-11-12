"""Canonical scene resolution and execution-to-planning state synchronization."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from copy import deepcopy
from contextlib import contextmanager
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation


def component(obj, name):
    return next((c['properties'] for c in obj.get('components', [])
                 if c['component_type'] == name), {})


DEFAULT_SCENE_SETTINGS = {
    'named_targets': {},
    'move_defaults': {
        'constraints': {
            'avoid_collision': True,
            'safety_distance': 0.003,
            'timeout': 60.0,
        },
        'planning': {
            'mode': 'auto',
            'planning_time': 10.0,
            'allow_replan': True,
            'collision_validation': {'resolution_rad': .02, 'spline_subdivisions': 2},
        },
    },
    'object_library': {
        'base_url': 'http://127.0.0.1:8000',
        'uid': 0,
        'pose_convention': 'object-local grasp center; metres; fixed-axis XYZ Euler radians',
    },
}


def apply_default_scene_settings(scene, global_config=None):
    """Add portable defaults so complete scenes do not need a runtime file."""
    result = deepcopy(scene)
    from .config import load_global_config
    config = global_config if global_config is not None else load_global_config()
    defaults = deepcopy(DEFAULT_SCENE_SETTINGS)
    defaults['move_defaults']['constraints'].update(config['motion'])
    defaults['trajectory'] = deepcopy(config['trajectory'])
    for key, value in defaults.items():
        current = result.get(key)
        if not isinstance(current, dict):
            result[key] = deepcopy(value)
            continue
        for nested_key, nested_value in value.items():
            if nested_key not in current:
                current[nested_key] = deepcopy(nested_value)
            elif isinstance(nested_value, dict) and isinstance(current[nested_key], dict):
                for leaf_key, leaf_value in nested_value.items():
                    current[nested_key].setdefault(leaf_key, deepcopy(leaf_value))
    return result


def apply_runtime_config(scene, config):
    """Build an execution view without writing extensions into the asset scene."""
    if any(config.get(k) != scene.get(k) for k in ('scene_id', 'scene_version')):
        raise ValueError('Runtime configuration scene identity mismatch')
    result = deepcopy(scene)
    settings = config.get('scene_settings', {})
    if set(settings) - {'named_targets', 'move_defaults', 'object_library', 'trajectory'}:
        raise ValueError('Unsupported runtime scene settings')
    result.update(deepcopy(settings))
    objects = {str(o['object_id_in_scene']): o for o in result['objects']}
    allowed = {'RobotModelRef', 'GripperModel', 'AssetRef', 'Collider', 'RigidBody', 'MeshRenderer', 'RobotAttachment', 'ArticulatedModel'}
    for ident, components in config.get('object_components', {}).items():
        if ident not in objects:
            raise ValueError(f'Runtime configuration references unknown object: {ident}')
        seen = set()
        for entry in components:
            kind = entry['component_type']
            if kind not in allowed or kind in seen:
                raise ValueError(f'Unsupported or duplicate runtime component: {kind}')
            seen.add(kind)
            target = objects[ident]['components']
            existing = next((c for c in target if c['component_type'] == kind), None)
            if existing is None:
                target.append(deepcopy(entry))
            else:
                existing['properties'].update(deepcopy(entry['properties']))
    excluded = set(map(str, config.get('nonphysical_objects', [])))
    for ident in excluded:
        if ident not in objects or not (component(objects[ident], 'Camera') or component(objects[ident], 'Light')):
            raise ValueError('Only camera/light objects may be excluded from the control scene')
    result['objects'] = [o for o in result['objects'] if str(o['object_id_in_scene']) not in excluded]
    for obj in result['objects']:
        transform = component(obj, 'Transform')
        q = np.asarray(transform.get('quaternion_xyzw', [0, 0, 0, 1]), dtype=float)
        norm = np.linalg.norm(q)
        if q.shape == (4,) and np.isfinite(norm) and abs(norm-1) < 1e-4:
            transform['quaternion_xyzw'] = (q/norm).tolist()
    return result


def pose_matrix(obj):
    p = component(obj, 'Transform')
    q = np.asarray(p.get('quaternion_xyzw', [0, 0, 0, 1]), dtype=float)
    xyz = np.asarray(p.get('position', [0, 0, 0]), dtype=float)
    if q.shape != (4,) or xyz.shape != (3,) or not np.all(np.isfinite(np.r_[q, xyz])):
        raise ValueError('Transform requires finite position and unit xyzw quaternion')
    norm = np.linalg.norm(q)
    if not np.isclose(norm, 1.0, atol=1e-3):
        raise ValueError('Transform requires finite position and unit xyzw quaternion')
    if norm != 1.0:
        q = q / norm
    if p.get('parent') is not None or not np.allclose(p.get('scale', [1, 1, 1]), [1, 1, 1]):
        raise ValueError('This scene compiler currently requires world-parented objects with unit scale')
    t = np.eye(4)
    t[:3, :3], t[:3, 3] = Rotation.from_quat(q).as_matrix(), xyz
    return t


@dataclass(frozen=True)
class ResolvedResources:
    scene_file: Path
    scene: dict
    robot_object: dict
    robot_urdf: Path
    asset_root: Path
    joint_names: tuple
    initial_joints: np.ndarray
    backend_name: str
    backend_resource: dict
    fingerprint: str
    robot_xml: object = None

    def asset(self, value):
        path = (self.asset_root / value).resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Missing scene asset: {path}')
        return path


class SceneResourceCoordinator:
    def __init__(self, catalog):
        self.catalog_path = Path(catalog).resolve()
        self.catalog = json.loads(self.catalog_path.read_text(encoding='utf-8'))

    def resolve(self, task, backend='mujoco'):
        if task.get('schema_version') != '2.0' or not task.get('request_id'):
            raise ValueError('Task requires schema_version=2.0 and request_id')
        # Register authoring scenes discovered under assets/scenes before
        # resolving the requested identity. Existing runtime/backend settings
        # in the catalog are preserved by the registrar.
        from .scene_catalog import register_scene
        self.catalog = register_scene(
            self.catalog_path,
            self.catalog_path.parent / 'scenes',
            task.get('scene_id'),
            task.get('scene_version'),
        )
        record = self.catalog['scenes'].get(str(task.get('scene_id'))) or self.catalog['scenes'].get(task.get('scene_id'))
        if record is None or record['version'] != task.get('scene_version'):
            raise ValueError('Unknown scene_id or scene_version mismatch')
        path = (self.catalog_path.parent / record['path']).resolve()
        scene = json.loads(path.read_text(encoding='utf-8'))
        if scene.get('scene_schema_version') not in (1, 2):
            raise ValueError('Supported scene_schema_version values are 1 and 2')
        if scene['scene_id'] != task['scene_id'] or scene['scene_version'] != task['scene_version']:
            raise ValueError('Scene identity disagrees with catalog/task')
        from .config import load_global_config, validate_config, trajectory_settings
        global_config = load_global_config()
        scene = apply_default_scene_settings(scene, global_config)
        runtime_bytes = b''
        if record.get('runtime_config'):
            runtime_path = (self.catalog_path.parent / record['runtime_config']).resolve()
            runtime_bytes = runtime_path.read_bytes()
            scene = apply_runtime_config(scene, json.loads(runtime_bytes))
        scene = apply_default_scene_settings(scene, global_config)
        task_defaults = validate_config(task.get('defaults', {}), partial=True)
        scene['move_defaults']['constraints'].update(task_defaults.get('motion', {}))
        scene['trajectory'].update(task_defaults.get('trajectory', {}))
        validate_config({'motion': {key: scene['move_defaults']['constraints'][key]
                                   for key in global_config['motion']}}, partial=True)
        scene['trajectory'] = trajectory_settings(scene)
        # Assemble new OLS MetadataRef authoring scenes into executable components.
        if not any(component(o, 'RobotModelRef') for o in scene.get('objects', [])):
            if any(component(o, 'MetadataRef').get('type') == 'robot' for o in scene.get('objects', [])):
                from demo.scene.assembler import SceneAssembler
                from services.object_library import ObjectLibraryClient, LocalObjectLibraryService
                config = scene.get('object_library', {})
                client = ObjectLibraryClient(config.get('base_url', 'http://127.0.0.1:8000'), uid=config.get('uid', 0))
                service = None
                robot_id = next(int(component(o, 'MetadataRef').get('path', '').rsplit('/', 1)[-1]) for o in scene['objects'] if component(o, 'MetadataRef').get('type') == 'robot')
                try:
                    client.get_robot(robot_id)
                except Exception:
                    service = LocalObjectLibraryService()
                    client = ObjectLibraryClient(service.url, uid=config.get('uid', 0))
                try:
                    root = (self.catalog_path.parent / self.catalog['asset_root']).resolve()
                    scene = SceneAssembler(root, client).assemble(scene, task=task)
                finally:
                    if service is not None:
                        service.close()
        robots = [o for o in scene['objects'] if component(o, 'RobotModelRef')]
        if len(robots) != 1:
            raise ValueError('Exactly one robot is supported by this runner')
        ref = component(robots[0], 'RobotModelRef')
        if not ref.get('urdf'):
            raise ValueError('Scene RobotModelRef must define urdf')
        root = (self.catalog_path.parent / self.catalog['asset_root']).resolve()
        urdf = (root / ref['urdf']).resolve()
        if not urdf.is_file():
            raise FileNotFoundError(f'Missing robot URDF: {urdf}')
        tree = ET.parse(urdf).getroot()
        attachments = [o for o in scene['objects'] if component(o, 'RobotAttachment')]
        if len({o['object_id_in_scene'] for o in scene['objects']}) != len(scene['objects']) or len({o['object_name_in_scene'] for o in scene['objects']}) != len(scene['objects']):
            raise ValueError('Duplicate scene object ID/name')
        for obj in attachments:
            mount = component(obj, 'RobotAttachment')
            source = component(obj, 'GripperModel')
            metadata = component(obj, 'MetadataRef')
            model_path = metadata.get('path', '')
            if metadata.get('type') == 'robot' and isinstance(model_path, str) and model_path.startswith('$LIBRARY_SERVER/'):
                try:
                    source = deepcopy(source)
                    source['_ols_model_id'] = int(model_path.rsplit('/', 1)[1])
                    source['_ols_asset_type'] = 'robot'
                except ValueError:
                    raise ValueError('Gripper MetadataRef path must contain an integer OLS model ID')
            if mount.get('parent_object_id') != robots[0]['object_id_in_scene'] or mount.get('connection_type') != 'fixed':
                raise ValueError('Only fixed attachments to the scene robot are supported')
            if component(obj, 'Transform').get('parent') != mount['parent_object_id']:
                raise ValueError('Attachment Transform.parent disagrees with RobotAttachment')
            if component(robots[0], 'GripperModel'):
                raise ValueError('Only one gripper attachment is supported')
            robots[0]['components'].append({'component_type': 'GripperModel', 'properties': deepcopy(source)})
        from .assembly import assemble_robot
        assembled = assemble_robot(urdf, root, attachments)
        scene['objects'] = [o for o in scene['objects'] if o not in attachments]
        roots = {link.get('name') for link in tree.findall('link')} - {j.find('child').get('link') for j in tree.findall('joint')}
        if ref['base_link'] not in roots:
            raise ValueError('Scene robot base_link must be the URDF root')
        if backend not in record['backend_resources']:
            raise ValueError(f'No resources for execution backend {backend}')
        names = tuple(component(robots[0], 'RobotDriver')['joint_sequence'])
        from .motion_limits import read_urdf_motion_limits
        timing = scene.setdefault('move_defaults', {}).setdefault('planning', {}).setdefault('time_parameterization', {})
        timing.update(read_urdf_motion_limits(urdf, names))
        active = {j.get('name') for j in ET.parse(urdf).getroot().findall('joint') if j.get('type') == 'revolute'}
        if len(names) != 6 or len(set(names)) != 6 or set(names) != active:
            raise ValueError('RobotDriver joint_sequence must match URDF revolute joints')
        initial = component(robots[0], 'RobotDriver').get('initial_joint_positions_rad')
        if initial is None:
            raise ValueError('Scene RobotDriver must define initial_joint_positions_rad')
        q = np.asarray([initial[name] for name in names], dtype=float)
        if q.shape != (6,) or not np.all(np.isfinite(q)):
            raise ValueError('Invalid initial_joints')
        ids, obj_names = set(), set()
        digest = hashlib.sha256(path.read_bytes() + runtime_bytes + urdf.read_bytes())
        digest.update(json.dumps({'move_defaults': scene['move_defaults'],
                                  'trajectory': scene['trajectory']}, sort_keys=True).encode('utf-8'))
        for obj in attachments:
            digest.update((root / component(obj, 'GripperModel')['urdf']).read_bytes())
        for obj in scene['objects']:
            name = obj['object_name_in_scene']
            if not isinstance(name, str) or not name or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for ch in name):
                raise ValueError('Object names require ASCII letters, numbers, underscore or hyphen')
            if obj['object_id_in_scene'] in ids or obj['object_name_in_scene'] in obj_names:
                raise ValueError('Duplicate scene object ID/name')
            ids.add(obj['object_id_in_scene']); obj_names.add(obj['object_name_in_scene'])
            pose_matrix(obj)
            if component(obj, 'EndEffectorModelRef'):
                raise ValueError('Articulated end effectors require a separate execution adapter')
            collider = component(obj, 'Collider')
            asset = component(obj, 'AssetRef').get('asset_path')
            if collider.get('enabled') is False:
                raise ValueError('Disabled colliders are not supported by this test pipeline')
            if collider.get('shape') in {'box', 'cylinder'}:
                if asset:
                    raise ValueError('Specify either primitive geometry or AssetRef, not both')
                size = np.asarray(collider['size'], dtype=float)
                if size.shape != (3,) or not np.all(np.isfinite(size)) or np.any(size <= 0):
                    raise ValueError('Primitive size must contain three positive dimensions')
                if collider['shape'] == 'cylinder' and not np.isclose(size[0], size[1]):
                    raise ValueError('Cylinder size is [radius, radius, full_height]')
            elif not component(obj, 'RobotModelRef') and not asset:
                raise ValueError('Environment requires a box collider or AssetRef')
            if asset:
                asset_path = (root / asset).resolve()
                if not asset_path.is_file():
                    raise FileNotFoundError(asset_path)
                if asset_path.suffix.lower() not in {'.urdf', '.obj', '.stl'}:
                    raise ValueError('Supported environment assets: URDF, OBJ, STL')
                digest.update(asset_path.read_bytes())
        digest.update(json.dumps(record['backend_resources'][backend], sort_keys=True).encode())
        return ResolvedResources(path, scene, robots[0], urdf, root, names, q,
                                 backend, record['backend_resources'][backend], digest.hexdigest(), assembled)

    @staticmethod
    def validate_consistency(resources, kin, checker, backend):
        if checker.scene_file != resources.scene_file or checker.robot_urdf != resources.robot_urdf:
            raise ValueError('Collision resource mismatch')
        state = backend.get_state()
        if not np.allclose(state['joints'], resources.initial_joints, atol=1e-9):
            raise ValueError('Initial joint state mismatch')
        if tuple(state['joint_names']) != tuple(kin.robot.joint_names) or tuple(kin.robot.joint_names) != resources.joint_names:
            raise ValueError('Joint order mismatch')
        if not np.allclose(state['base_transform'], kin.robot.base_transform(state['joints']), atol=1e-9):
            raise ValueError('Robot base pose mismatch')
        q = state['joints']; fk = kin.forward(q); actual = backend.get_end_effector_pose()
        pos_error = float(np.linalg.norm(fk['position'] - actual['position']))
        rot_error = float(np.max(np.abs(fk['rotation_matrix'] - actual['rotation_matrix'])))
        if pos_error > 2e-6 or rot_error > 2e-6:
            raise ValueError(f'URDF/TCP mismatch: {pos_error}, {rot_error}')
        for obj in resources.scene['objects']:
            if obj is resources.robot_object:
                continue
            name = obj['object_name_in_scene']
            expected = pose_matrix(obj)
            actual = state['objects'][name]
            if component(obj, 'ArticulatedModel') and getattr(checker, 'supports_articulated', False):
                if not np.allclose(actual['position'], expected[:3, 3], atol=1e-8):
                    raise ValueError(f'Articulated base pose mismatch: {name}')
                continue
            fcl_pose = checker._entities[name].transform if name in checker._entities else None
            if (fcl_pose is None or not np.allclose(actual['position'], expected[:3, 3], atol=1e-8)
                    or not np.allclose(Rotation.from_quat(actual['quaternion_xyzw']).as_matrix(), expected[:3, :3], atol=1e-8)
                    or not np.allclose(fcl_pose.translation, expected[:3, 3], atol=1e-8)
                    or not np.allclose(Rotation.from_quat(np.asarray(fcl_pose.rotation_wxyz)[[1, 2, 3, 0]]).as_matrix(), expected[:3, :3], atol=1e-8)):
                raise ValueError(f'Object mismatch: {name}')
            collider = component(obj, 'Collider')
            if collider.get('shape') in {'box', 'cylinder'}:
                expected_size = np.asarray(collider['size'])
                if collider['shape'] == 'cylinder':
                    expected_size = expected_size * [2, 2, 1]
                if not np.allclose(fcl_pose.scale, expected_size, atol=1e-8):
                    raise ValueError(f'FCL primitive dimensions mismatch: {name}')
        return {'fk_position_error_m': pos_error, 'fk_rotation_matrix_error': rot_error,
                'scene_id': resources.scene['scene_id'], 'fingerprint': resources.fingerprint}


class SceneStateSynchronizer:
    """Execution is authoritative after initialization; never teleport it from planning."""
    def __init__(self, blackboard=None):
        from execution.blackboard import TargetBlackboard
        self.blackboard = blackboard if blackboard is not None else TargetBlackboard()
        self._poses = {}
        self._joint_states = {}
        self.revision = 0
        self.last_changed_objects = ()

    @contextmanager
    def command_state(self, backend, kin, checker):
        """Refresh every command, including failed or newly added skills."""
        state = self.sync(backend, kin, checker)
        try:
            yield state
        finally:
            # Observe the actual endpoint even when execution raises midway.
            # Collision synchronization is deferred to the next planning step.
            self.blackboard.observe(backend.get_state())

    def sync(self, backend, kin, checker):
        state = backend.get_state()
        if self.blackboard is not None:
            self.blackboard.observe(state)
        checker_key = id(checker)
        joints = np.asarray(state['joints'], dtype=float)
        previous_joints = self._joint_states.get(checker_key)
        joints_changed = (previous_joints is None or
                          not np.allclose(previous_joints, joints, atol=1e-9, rtol=0))
        if joints_changed:
            kin.robot.set_current_joints(joints)
            self._joint_states[checker_key] = joints.copy()
        if not np.allclose(state['base_transform'], kin.robot.base_transform(state['joints']), atol=1e-9):
            raise ValueError('Moving robot bases are not supported in this release')
        changed = []
        for name, pose in state['objects'].items():
            key = (checker, name)
            previous = self._poses.get(key)
            # Settled physics bodies retain sub-micrometre solver jitter. Avoid
            # rebuilding native collision transforms for invisible changes;
            # compare to the last transmitted pose, so movement accumulates.
            unchanged = previous is not None and (
                np.allclose(previous['position'], pose['position'], atol=1e-7, rtol=0)
                and abs(float(np.dot(previous['quaternion_xyzw'], pose['quaternion_xyzw']))) >= 1 - 1e-14)
            if not unchanged:
                checker.set_entity_transform(name, **pose)
                self._poses[key] = {
                    'position': list(pose['position']),
                    'quaternion_xyzw': list(pose['quaternion_xyzw']),
                }
                changed.append(name)
        if changed or joints_changed:
            self.revision += 1
        self.last_changed_objects = tuple(changed)
        hook = getattr(checker, 'on_scene_sync', None)
        if callable(hook):
            hook(state, changed_objects=tuple(changed), revision=self.revision)
        return state
