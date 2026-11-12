"""MuJoCo execution plugin. MJCF is generated from the canonical JSON/URDF."""
from pathlib import Path
from contextlib import nullcontext
import time
import xml.etree.ElementTree as ET

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from .resources import component, pose_matrix


def numbers(value):
    return ' '.join(format(float(x), '.17g') for x in value)


def obj_diffuse_texture(path):
    """Read the diffuse map for a single-material OBJ, preserving its UVs."""
    path = Path(path)
    if path.suffix.lower() != '.obj':
        return None
    lines = path.read_text(encoding='utf-8').splitlines()
    used = {line.split(maxsplit=1)[1].strip() for line in lines if line.startswith('usemtl ')}
    if len(used) != 1:
        return None
    for line in lines:
        if not line.startswith('mtllib '):
            continue
        library = path.parent / line.split(maxsplit=1)[1].strip()
        if not library.is_file():
            continue
        current = None
        for entry in library.read_text(encoding='utf-8').splitlines():
            entry = entry.strip()
            if entry.startswith('newmtl '):
                current = entry.split(maxsplit=1)[1]
            elif current in used and entry.startswith('map_Kd '):
                texture = library.parent / entry.split(maxsplit=1)[1].strip()
                if texture.is_file():
                    return texture.resolve()
    return None


def use_asset_texture(renderer):
    """White color overrides are neutral; only explicit overrides hide maps."""
    override = renderer.get('forceOverrideColor')
    return not override or (isinstance(override, str) and
                            override.upper() in {'#FFFFFF', '#FFFFFFFF'})


def urdf_mjcf(path, output_dir, prefix, tree=None):
    from copy import deepcopy
    root = deepcopy(tree) if tree is not None else ET.parse(path).getroot()
    extra = root.find('mujoco')
    if extra is None:
        extra = ET.SubElement(root, 'mujoco')
    compiler = extra.find('compiler')
    if compiler is None:
        compiler = ET.SubElement(extra, 'compiler')
    compiler.attrib.update(strippath='false', discardvisual='false', fusestatic='false')
    for mesh in root.findall('.//mesh'):
        mesh.set('filename', str((path.parent / mesh.get('filename')).resolve()))
    # Resolve MTL paths before basename aliases relocate OBJ files into the cache.
    textured_visuals = []
    for mesh in root.findall('./link/visual/geometry/mesh'):
        texture = obj_diffuse_texture(mesh.get('filename'))
        if texture:
            textured_visuals.append((mesh, texture))
    # MuJoCo's URDF importer keys meshes by basename, even across directories.
    # Cabinet body/panel/handle commonly all use mesh.obj; give distinct sources
    # unique cache filenames without changing the original URDF or assets.
    from collections import defaultdict
    import hashlib
    import shutil
    groups = defaultdict(list)
    for mesh in root.findall('.//mesh'):
        groups[Path(mesh.get('filename')).name].append(mesh)
    for meshes in groups.values():
        identities = {(m.get('filename'), m.get('scale', '1 1 1')) for m in meshes}
        if len(identities) <= 1:
            continue
        cache = output_dir / 'urdf_meshes'
        cache.mkdir(exist_ok=True)
        for mesh in meshes:
            source = Path(mesh.get('filename'))
            identity = str(source) + '|' + mesh.get('scale', '1 1 1')
            alias = cache / (hashlib.sha256(identity.encode()).hexdigest()[:16] + '_' + source.name)
            if not alias.exists() or alias.stat().st_size != source.stat().st_size or alias.stat().st_mtime_ns != source.stat().st_mtime_ns:
                shutil.copy2(source, alias)
            mesh.set('filename', str(alias))
    # The articulated spring links have concave mesh colliders. Their whole
    # convex hull intrudes into the closing workspace; preserve their cavities.
    from copy import deepcopy
    from collision.mesh_assets import prepare_mesh
    for link in root.findall('link'):
        if not link.get('name', '').startswith('robotiq_2f85_') or not link.get('name', '').endswith('_spring_link'):
            continue
        for collision in list(link.findall('collision')):
            mesh = collision.find('geometry/mesh')
            if mesh is None:
                continue
            parts = prepare_mesh(mesh.get('filename'), scale=[float(v) for v in mesh.get('scale', '1 1 1').split()])
            link.remove(collision)
            for index, part in enumerate(parts.parts):
                item = deepcopy(collision)
                item.set('name', f'{link.get("name")}_collision_{index}')
                item.find('geometry/mesh').attrib.update(filename=str(part), scale='1 1 1')
                link.append(item)
    model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding='unicode'))
    intermediate = output_dir / f'{prefix}_import.xml'
    mujoco.mj_saveLastXML(str(intermediate), model)
    imported = ET.parse(intermediate).getroot()
    # MuJoCo imports OBJ UVs through URDF, but does not load the associated MTL.
    # Match the final mesh filename so identically named source OBJs stay distinct.
    textures = {Path(mesh.get('filename')).resolve(): texture
                for mesh, texture in textured_visuals}
    asset = imported.find('asset')
    if asset is not None:
        for mesh in asset.findall('mesh'):
            texture = textures.get(Path(mesh.get('file')).resolve())
            if texture is None:
                continue
            name = mesh.get('name') + '_diffuse'
            ET.SubElement(asset, 'texture', name=name, type='2d', file=str(texture))
            ET.SubElement(asset, 'material', name=name, texture=name,
                          rgba='1 1 1 1', texuniform='false')
            for geom in imported.iter('geom'):
                if geom.get('mesh') == mesh.get('name') and geom.get('group') == '1':
                    geom.set('material', name)
                    geom.set('rgba', '1 1 1 1')
    return imported


def compile_scene(resources, output_dir):
    """Backend-specific conversion, keeping URDF inertias and collision geometry."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if resources.backend_resource.get('format') != 'generate_from_scene':
        raise ValueError('MuJoCo plugin expects format=generate_from_scene')
    root = ET.Element('mujoco', model=str(resources.scene['scene_id']))
    ET.SubElement(root, 'compiler', angle='radian')
    # Collision-rich target resolution can require more temporary workspace
    # than MuJoCo's default computation stack provides.
    ET.SubElement(root, 'size', nconmax='10000', nstack='10000000')
    ET.SubElement(root, 'option', timestep='0.002', integrator='implicitfast', gravity='0 0 -9.81',
                  noslip_iterations='1')
    visual = ET.SubElement(root, 'visual')
    ET.SubElement(visual, 'headlight', ambient='0.3 0.3 0.3', diffuse='0.5 0.5 0.5', specular='0.2 0.2 0.2')
    ET.SubElement(visual, 'global', offwidth='960', offheight='720')
    asset = ET.SubElement(root, 'asset')
    ET.SubElement(asset, 'texture', name='sky', type='skybox', builtin='gradient',
                  rgb1='0.32 0.40 0.50', rgb2='0.75 0.80 0.85', width='512', height='3072')
    world = ET.SubElement(root, 'worldbody')
    ET.SubElement(world, 'light', pos='0 -2 4', dir='0 0 -1', directional='true', diffuse='0.4 0.4 0.4')
    for obj in resources.scene['objects']:
        name = obj['object_name_in_scene']
        t = pose_matrix(obj)
        quat = Rotation.from_matrix(t[:3, :3]).as_quat()[[3, 0, 1, 2]]
        wrapper = ET.SubElement(world, 'body', name=name, pos=numbers(t[:3, 3]), quat=numbers(quat))
        robot = bool(component(obj, 'RobotModelRef'))
        asset_path = resources.robot_urdf if robot else resources.asset(component(obj, 'AssetRef')['asset_path']) if component(obj, 'AssetRef') else None
        dynamic = component(obj, 'RigidBody').get('dynamic', False)
        if robot and dynamic:
            raise ValueError('Floating robot bases are not supported')
        if dynamic:
            ET.SubElement(wrapper, 'freejoint', name=name + '_free')
        if asset_path and asset_path.suffix.lower() == '.urdf':
            if dynamic and not robot:
                raise ValueError('Dynamic environment URDF requires an articulated-object adapter')
            imported = urdf_mjcf(asset_path, output_dir, name, resources.robot_xml if robot else None)
            # URDF imports keep both visual meshes (group 1) and simplified
            # collision geoms (group 0). Hide collision overlays by default,
            # while retaining their physical contact properties.
            for geom in imported.iter('geom'):
                if geom.get('group', '0') == '0' and (
                        int(geom.get('contype', '1')) or int(geom.get('conaffinity', '1'))):
                    geom.set('group', '3')
            if not robot:
                if not component(obj, 'ArticulatedModel') and any(j.get('type') != 'fixed' for j in ET.parse(asset_path).getroot().findall('joint')):
                    raise ValueError('Only static environment URDFs are supported')
                # Namespace environment URDF link/material/mesh names.
                for el in imported.iter():
                    for key in ('name', 'mesh', 'material', 'texture', 'joint', 'joint1', 'joint2', 'body1', 'body2'):
                        if key in el.attrib:
                            el.set(key, name + '__' + el.get(key))
            if not robot:
                for body in imported.iter('body'):
                    for index, geom in enumerate(body.findall('geom')):
                        if not geom.get('name'):
                            geom.set('name', body.get('name') + '_geom_' + str(index))
            if imported.find('asset') is not None:
                asset.extend(list(imported.find('asset')))
            wrapper.extend(list(imported.find('worldbody')))
            if robot:
                ref = component(obj, 'RobotModelRef')
                tip = next((b for b in wrapper.iter('body') if b.get('name') == ref['tip_link']), None)
                if tip is None:
                    raise ValueError('URDF TCP link missing from imported MJCF')
                tcp = np.asarray(ref.get('tcp_transform', np.eye(4)), dtype=float)
                ET.SubElement(tip, 'site', name='tcp', pos=numbers(tcp[:3, 3]),
                              quat=numbers(Rotation.from_matrix(tcp[:3, :3]).as_quat()[[3, 0, 1, 2]]), size='0.008')
                grasp = ref.get('grasp_frame')
                if grasp:
                    grasp_body = next((b for b in wrapper.iter('body') if b.get('name') == grasp['link']), None)
                    if grasp_body is None:
                        raise ValueError('Configured grasp frame link is absent from MJCF')
                    ET.SubElement(grasp_body, 'site', name='grasp_center',
                        pos=numbers(grasp.get('position', [0, 0, 0])),
                        quat=numbers(np.asarray(grasp.get('quaternion_xyzw', [0, 0, 0, 1]))[[3, 0, 1, 2]]),
                        size='0.004', rgba='0 0.8 0.8 1')
                for joint in wrapper.iter('joint'):
                    joint.set('damping', '4')
                    joint.set('armature', '0.05')
        else:
            collider = component(obj, 'Collider')
            rgba = component(obj, 'MeshRenderer').get('color', [0.55, 0.58, 0.62, 1])
            common = {'name': name + '_collision', 'rgba': numbers(rgba)}
            friction = obj.get('_ols_friction')
            if friction is not None:
                common['friction'] = numbers(friction)
            if dynamic:
                common['mass'] = str(component(obj, 'RigidBody').get('mass', 0.1))
            if collider.get('shape') == 'box':
                ET.SubElement(wrapper, 'geom', type='box', size=numbers(np.asarray(collider['size']) / 2), **common)
            elif collider.get('shape') == 'cylinder':
                size = collider['size']
                ET.SubElement(wrapper, 'geom', type='cylinder', size=numbers([size[0], size[2] / 2]), **common)
            elif asset_path:
                from collision.mesh_assets import prepare_mesh
                collision_path = resources.asset(collider['collision_mesh']) if collider.get('collision_mesh') else asset_path
                prepared = prepare_mesh(collision_path)
                ET.SubElement(asset, 'mesh', name=name + '_mesh', file=str(asset_path))
                visual = {'rgba': numbers(rgba)}
                texture = obj_diffuse_texture(asset_path)
                if texture and use_asset_texture(component(obj, 'MeshRenderer')):
                    ET.SubElement(asset, 'texture', name=name + '_texture', type='2d', file=str(texture))
                    ET.SubElement(asset, 'material', name=name + '_material', texture=name + '_texture',
                                  rgba='1 1 1 1', texuniform='false')
                    visual = {'material': name + '_material', 'rgba': '1 1 1 1'}
                ET.SubElement(wrapper, 'geom', type='mesh', mesh=name + '_mesh',
                              name=name + '_visual', **visual,
                              contype='0', conaffinity='0', group='1', mass='0')
                total_volume = sum(prepared.volumes)
                for i, (part, volume) in enumerate(zip(prepared.parts, prepared.volumes)):
                    part_name = f'{name}_part_{i:04d}'
                    ET.SubElement(asset, 'mesh', name=part_name, file=str(part))
                    attributes = dict(common, name=part_name, group='3')
                    if dynamic:
                        attributes['mass'] = str(float(common['mass']) * volume / total_volume)
                    ET.SubElement(wrapper, 'geom', type='mesh', mesh=part_name, **attributes)
            else:
                raise ValueError(f'Unsupported geometry: {name}')
    actuators = ET.SubElement(root, 'actuator')
    for name in resources.joint_names:
        ET.SubElement(actuators, 'position', name=name + '_servo', joint=name, kp='1200', kv='90')
    from .gripper_model import add_gripper
    add_gripper(root, resources)
    path = output_dir / 'scene.mjcf.xml'
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path


class MuJoCoExecutionBackend:
    name = 'mujoco'

    def load(self, resources, output_dir, *, camera_scene_json=None):
        self.resources = resources
        self.scene_path = compile_scene(resources, output_dir)
        self._cameras = None
        self._camera_resolutions = {}
        from sensors.camera_mujoco.scene import load_scene_with_cameras
        self.model, self._camera_resolutions = load_scene_with_cameras(
            self.scene_path, camera_scene_json or resources.scene_file)
        self.data = mujoco.MjData(self.model)
        self.joint_names = resources.joint_names
        self.joint_ids = np.array([self._id(mujoco.mjtObj.mjOBJ_JOINT, n) for n in self.joint_names])
        self.qpos_indices = self.model.jnt_qposadr[self.joint_ids]
        self.dof_indices = self.model.jnt_dofadr[self.joint_ids]
        self.actuator_ids = np.array([self._id(mujoco.mjtObj.mjOBJ_ACTUATOR, n + '_servo') for n in self.joint_names])
        self.tip_id = self._id(mujoco.mjtObj.mjOBJ_SITE, 'tcp')
        self.base_id = self._id(mujoco.mjtObj.mjOBJ_BODY, resources.robot_object['object_name_in_scene'])
        self.object_ids = {o['object_name_in_scene']: self._id(mujoco.mjtObj.mjOBJ_BODY, o['object_name_in_scene'])
                           for o in resources.scene['objects'] if o is not resources.robot_object}
        self.mechanism_bodies = set()
        for obj in resources.scene['objects']:
            for link in component(obj, 'ArticulatedModel').get('links', []):
                name = obj['object_name_in_scene'] + '__' + link
                self.object_ids[name] = self._id(mujoco.mjtObj.mjOBJ_BODY, name)
                self.mechanism_bodies.add(name)
        self.data.qpos[self.qpos_indices] = resources.initial_joints
        self.data.ctrl[self.actuator_ids] = resources.initial_joints
        self.frame_callback = None
        from execution.config import trajectory_settings
        self.trajectory_fps = trajectory_settings(resources.scene)['fps']
        if self.trajectory_fps * float(self.model.opt.timestep) > 1 + 1e-10:
            raise ValueError('trajectory.fps cannot exceed the physics step frequency')
        self.frame_interval = 1.0 / self.trajectory_fps
        self.viewer = None
        self.physics_steps = 0
        self.viewer_sync_count = 0
        self.status = 'Ready'
        self.realtime = False
        self.gripper_controller = None
        self.contact_target = None
        mujoco.mj_forward(self.model, self.data)

    def _id(self, kind, name):
        value = mujoco.mj_name2id(self.model, kind, name)
        if value < 0:
            raise ValueError(f'Missing MuJoCo resource: {name}')
        return value

    @property
    def joint_limits(self):
        return self.model.jnt_range[self.joint_ids].copy()

    def get_joint_positions(self):
        return self.data.qpos[self.qpos_indices].copy()

    def get_end_effector_pose(self):
        rotation = self.data.site_xmat[self.tip_id].reshape(3, 3).copy()
        return {'position': self.data.site_xpos[self.tip_id].copy(),
                'rotation_matrix': rotation,
                'quaternion_wxyz': Rotation.from_matrix(rotation).as_quat()[[3, 0, 1, 2]]}

    def get_context(self, request=None):
        return {'current_joint_state': self.get_joint_positions(),
                'current_pose': self.get_end_effector_pose(),
                'controller_state': {'ready': True, 'mode': 'mujoco_actuator'}}

    def get_control_from_camera(self, camera_id):
        """Read rigid hand-eye calibration from live poses without rendering."""
        name = {'global': 'global_camera', 'wrist': 'wrist_camera'}.get(camera_id, camera_id)
        camera = self._id(mujoco.mjtObj.mjOBJ_CAMERA, name)

        def rigid_root(body):
            while body > 0 and self.model.body_jntnum[body] == 0:
                body = int(self.model.body_parentid[body])
            return body

        if rigid_root(int(self.model.cam_bodyid[camera])) != rigid_root(int(self.model.site_bodyid[self.tip_id])):
            raise ValueError('Observation camera must be rigidly attached to the TCP')
        control = self.get_end_effector_pose()
        rotation = np.asarray(control['rotation_matrix'])
        result = np.eye(4)
        result[:3, :3] = rotation.T @ self.data.cam_xmat[camera].reshape(3, 3)
        result[:3, 3] = rotation.T @ (self.data.cam_xpos[camera] - control['position'])
        return result

    def get_camera_frame(self, camera_id):
        from sensors.camera import CameraManager
        from sensors.camera_mujoco import MujocoCamera

        name = {'global': 'global_camera', 'wrist': 'wrist_camera'}.get(camera_id, camera_id)
        if self._cameras is None:
            self._cameras = CameraManager({
                key: MujocoCamera(self.model, self.data, key, width=size[0], height=size[1])
                for key, size in self._camera_resolutions.items()
            })
        return self._cameras.get_current_frame(name)

    def set_frame_callback(self, callback):
        self.frame_callback = callback

    def open_viewer(self):
        import mujoco.viewer
        self.viewer = mujoco.viewer.launch_passive(self.model, self.data, show_left_ui=False, show_right_ui=False)
        with self.viewer.lock():
            self.configure_camera(self.viewer.cam)
        self.set_status('Ready')
        self._sync_viewer()
        self.realtime = True

    def set_collision_geometry_visible(self, visible=True):
        """Toggle the MuJoCo collision-geometries display group.

        The scene compiler puts simplified collision meshes in geom group 3
        and keeps textured visual meshes in group 1.  ``geomgroup`` is a
        viewer option, so changing it affects rendering only and never
        changes MuJoCo's contact pairs or the FCL scene.
        """
        if self.viewer is None:
            return False
        with self.viewer.lock():
            groups = self.viewer.opt.geomgroup
            if len(groups) <= 3:
                return False
            groups[3] = 1 if visible else 0
        self._sync_viewer()
        return True

    def create_gripper_controller(self):
        from control.binding_gripper import BindingGripperController
        return BindingGripperController(self)

    def payload_gravity_torque(self):
        """Map the separately simulated payload's weight to the arm joints.

        A free body's gravity lives in its own DOFs in qfrc_bias, even when
        contact or a weld transmits its weight to the gripper. Evaluate the
        gripper Jacobian at the measured payload COM, not the free-body Jacobian.
        """
        torque = np.zeros(len(self.dof_indices))
        c = self.gripper_controller
        if c is None or not c._holding or c.held_name is None:
            return torque
        body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, c.held_name)
        if min(c._forces(body).values()) <= .1:
            return torque
        # Only separately simulated free objects need this additional term.
        j = int(self.model.body_jntadr[body])
        if self.model.body_jntnum[body] != 1 or self.model.jnt_type[j] != mujoco.mjtJoint.mjJNT_FREE:
            return torque
        jac = np.zeros((3, self.model.nv))
        mujoco.mj_jac(self.model, self.data, jac, None,
                      self.data.subtree_com[body], c.base_id)
        weight_compensation = -float(self.model.body_subtreemass[body]) * self.model.opt.gravity
        return jac[:, self.dof_indices].T @ weight_compensation

    def apply_arm_compensation(self):
        self.data.qfrc_applied[self.dof_indices] = (
            self.data.qfrc_bias[self.dof_indices] + self.payload_gravity_torque())

    def advance_gripper(self, duration):
        """Hold the arm using its actuators while the gripper and objects evolve."""
        goal = self.get_joint_positions()
        started = time.perf_counter()
        sim_start = float(self.data.time)
        next_frame = sim_start
        while self.data.time - sim_start < duration - 1e-10:
            with self.viewer.lock() if self.viewer is not None else nullcontext():
                self.data.ctrl[self.actuator_ids] = goal
                self.apply_arm_compensation()
                if self.gripper_controller is not None:
                    self.gripper_controller.update_hold_force()
                mujoco.mj_step(self.model, self.data)
                mujoco.mj_forward(self.model, self.data)
            self.physics_steps += 1
            if self.data.time >= next_frame:
                if self.frame_callback:
                    self.frame_callback(self.get_state())
                self._sync_viewer()
                if self.realtime:
                    delay = self.data.time - sim_start - (time.perf_counter() - started)
                    if delay > 0:
                        time.sleep(min(delay, self.frame_interval))
                next_frame += self.frame_interval

    def configure_camera(self, camera):
        options = self.resources.backend_resource.get('options', {}).get('viewer', {})
        camera.lookat[:] = options.get('lookat', self.data.xpos[self.base_id] + [0.25, 0, 0.15])
        camera.distance = options.get('distance', 2.7)
        camera.azimuth = options.get('azimuth', 135)
        camera.elevation = options.get('elevation', -25)

    def set_status(self, status):
        self.status = status
        if self.viewer is not None and self.viewer.is_running():
            self.viewer.set_texts((None, None, status, f'Simulation: {self.data.time:.2f} s | Physics steps: {self.physics_steps}'))

    def _sync_viewer(self):
        if self.viewer is None:
            return
        if not self.viewer.is_running():
            raise RuntimeError('Viewer closed during execution')
        self.set_status(self.status)
        self.viewer.sync()
        self.viewer_sync_count += 1

    def get_state(self):
        base = np.eye(4)
        base[:3, :3] = self.data.xmat[self.base_id].reshape(3, 3)
        base[:3, 3] = self.data.xpos[self.base_id]
        payload = None
        if self.gripper_controller is not None:
            c = self.gripper_controller
            payload = c.get_state()
            payload['object_id'] = c.held_name
        return {'joint_names': list(self.joint_names), 'joints': self.get_joint_positions().tolist(),
                'gripper': payload,
                'mechanisms': {name: {'joint_position': float(self.data.qpos[self.model.jnt_qposadr[self.model.body_jntadr[self.object_ids[name]]]]),
                    'joint_type': int(self.model.jnt_type[self.model.body_jntadr[self.object_ids[name]]])}
                    for name in getattr(self, 'mechanism_bodies', ())},
                'arm_control': {
                    'joint_error_rad': (self.data.ctrl[self.actuator_ids] - self.get_joint_positions()).tolist(),
                    'servo_torque_nm': self.data.actuator_force[self.actuator_ids].tolist(),
                    'servo_force_limited': self.model.actuator_forcelimited[self.actuator_ids].astype(bool).tolist(),
                    'payload_gravity_torque_nm': self.payload_gravity_torque().tolist()},
                'base_transform': base.tolist(), 'simulation_time': float(self.data.time),
                'objects': {name: {'position': self.data.xpos[i].tolist(),
                                   'quaternion_xyzw': self.data.xquat[i][[1, 2, 3, 0]].tolist()}
                            for name, i in self.object_ids.items()}}

    def set_object_pose(self, name, position, quaternion_xyzw=(0, 0, 0, 1)):
        i = self.object_ids[name]
        if self.model.body_jntnum[i]:
            raise ValueError('Explicit pose edits only support static objects')
        self.model.body_pos[i] = position
        self.model.body_quat[i] = np.asarray(quaternion_xyzw)[[3, 0, 1, 2]]
        mujoco.mj_forward(self.model, self.data)

    def execute_trajectory(self, trajectory, *, timeout=30.0):
        q = np.asarray(trajectory['time_parameterized_waypoints'], dtype=float)
        stamps = np.asarray(trajectory['timestamps'], dtype=float)
        if (q.ndim != 2 or q.shape[1] != len(self.joint_names) or len(q) < 2
                or stamps.shape != (len(q),) or not np.all(np.isfinite(q))
                or not np.all(np.isfinite(stamps)) or stamps[0] != 0
                or np.any(np.diff(stamps) < 0)):
            raise ValueError('Trajectory requires finite NxDOF joints and nondecreasing timestamps from zero')
        # Ruckig segment endpoints can round to identical timestamps. They
        # represent one sample only when the corresponding positions agree.
        duplicate = np.diff(stamps) == 0
        if np.any(np.abs(np.diff(q, axis=0)[duplicate]) > 1e-10):
            raise ValueError('Different joint positions share a trajectory timestamp')
        keep = np.r_[~duplicate, True]
        q, stamps = q[keep], stamps[keep]
        if len(q) < 2:
            raise ValueError('Trajectory requires a positive duration')
        if not np.isfinite(timeout) or timeout <= 0:
            raise ValueError('Execution timeout must be finite and positive')
        if np.any(q < self.joint_limits[:, 0] - 1e-8) or np.any(q > self.joint_limits[:, 1] + 1e-8):
            raise ValueError('Trajectory exceeds joint limits')
        if not np.allclose(q[0], self.get_joint_positions(), atol=0.002):
            raise ValueError('Trajectory start differs from execution state')
        start = time.perf_counter(); sim_start = self.data.time
        frames, next_frame = [], self.data.time
        dt = float(self.model.opt.timestep)
        max_tracking = 0.0
        from execution.convergence import GoalConvergence, TrajectoryTimeout
        convergence = GoalConvergence(**trajectory.get('goal_tolerances', {}))
        goal_data = mujoco.MjData(self.model)
        goal_data.qpos[:] = self.data.qpos
        goal_data.qpos[self.qpos_indices] = q[-1]
        mujoco.mj_forward(self.model, goal_data)
        goal_position = goal_data.site_xpos[self.tip_id].copy()
        goal_rotation = goal_data.site_xmat[self.tip_id].reshape(3, 3).copy()
        initial_gripper = self.gripper_controller.get_state() if self.gripper_controller else {}
        holding_required = bool(initial_gripper.get('holding') or
            (getattr(self.gripper_controller, 'held_name', None)
             and getattr(self.gripper_controller, '_holding', False)))
        completion = None
        velocity_feedforward = (trajectory.get('diagnostics', {}).get('time_parameterization', {})
                                .get('time_law') == 'local_acceleration_bounds')
        reference_velocities = np.gradient(q, stamps, axis=0)
        reference_velocities[[0, -1]] = 0.0
        gains = self.model.actuator_gainprm[self.actuator_ids, 0]
        damping = -self.model.actuator_biasprm[self.actuator_ids, 2]
        velocity_gain = np.divide(damping, gains, out=np.zeros_like(gains), where=gains > 0)
        # Track a smooth reference with position actuators and gravity compensation.
        # Physics remains active: qpos is never assigned during playback.
        while True:
            elapsed = float(self.data.time - sim_start)
            reference_time = elapsed
            ref = np.array([np.interp(elapsed, stamps, q[:, j]) for j in range(len(self.joint_names))])
            control = ref
            if velocity_feedforward:
                # A position actuator otherwise damps against zero velocity,
                # causing kv/kp * q_dot lag even on a perfectly feasible path.
                # Compensate through the actuator command, retaining its force
                # and control limits; never write the physical joint positions.
                speed_ref = np.array([np.interp(elapsed, stamps, reference_velocities[:, j])
                                      for j in range(len(self.joint_names))])
                control = np.clip(ref + velocity_gain * speed_ref,
                                  self.joint_limits[:, 0], self.joint_limits[:, 1])
            with self.viewer.lock() if self.viewer is not None else nullcontext():
                self.data.ctrl[self.actuator_ids] = control
                self.apply_arm_compensation()
                if self.gripper_controller is not None:
                    self.gripper_controller.update_hold_force()
                mujoco.mj_step(self.model, self.data)
                mujoco.mj_forward(self.model, self.data)
            self.physics_steps += 1
            max_tracking = max(max_tracking, float(np.max(np.abs(self.get_joint_positions() - ref))))
            if self.data.time + 1e-10 >= next_frame:
                state = self.get_state()
                payload = state.get('gripper')
                frames.append(state)
                if self.frame_callback:
                    self.frame_callback(state)
                if self.viewer is not None:
                    self._sync_viewer()
                    # Pace against elapsed wall time rather than adding a fixed
                    # delay on top of collision checks and rendering costs.
                    delay = (self.data.time - sim_start) - (time.perf_counter() - start)
                    if delay > 0:
                        time.sleep(min(delay, self.frame_interval))
                next_frame += self.frame_interval
            error = float(np.max(np.abs(self.get_joint_positions() - q[-1])))
            wall_elapsed = time.perf_counter() - start
            if elapsed >= stamps[-1] or wall_elapsed > timeout:
                position_error = np.linalg.norm(self.data.site_xpos[self.tip_id] - goal_position)
                orientation_error = Rotation.from_matrix(
                    goal_rotation.T @ self.data.site_xmat[self.tip_id].reshape(3, 3)).magnitude()
                completion = convergence.update(elapsed, reference_complete=elapsed >= stamps[-1],
                    joint_error=error, joint_speed=float(np.max(np.abs(self.data.qvel[self.dof_indices]))),
                    position_error=position_error, orientation_error=orientation_error,
                    holding_required=holding_required, contact_valid=True)
                if completion['converged']:
                    break
                if elapsed > stamps[-1] + 5 or wall_elapsed > timeout:
                    reason = 'wall_time_limit' if wall_elapsed > timeout else 'goal_not_converged'
                    raise TrajectoryTimeout(reason, {**completion, 'planned_duration_s': float(stamps[-1]),
                        'simulation_elapsed_s': float(self.data.time - sim_start),
                        'wall_elapsed_s': wall_elapsed, 'wall_timeout_s': timeout,
                        'settling_timeout_s': 5.0})
        self._sync_viewer()
        final_gripper = self.get_state().get('gripper')
        return {'final_joint_state': self.get_joint_positions().tolist(), 'joint_error': error,
                'execution_time': time.perf_counter() - start, 'simulation_duration': float(self.data.time - sim_start),
                'physics_steps': int(round((self.data.time - sim_start) / dt)),
                'max_tracking_error_rad': max_tracking, 'frames': frames,
                'motion_completed': bool(completion and completion['converged']), 'grasp_error': None,
                'contact_diagnostics': {'mode': 'command_end', 'samples': 0},
                'completion': completion,
                'velocity_feedforward': velocity_feedforward,
                'final_gripper_state': final_gripper}

    def wait_for_viewer_close(self):
        """Keep the final scene inspectable without advancing execution physics."""
        viewer = getattr(self, 'viewer', None)
        while viewer is not None and viewer.is_running():
            viewer.sync()
            time.sleep(1 / 30)

    def close(self):
        if getattr(self, '_cameras', None) is not None:
            self._cameras.close()
            self._cameras = None
        if getattr(self, 'viewer', None) is not None:
            self.viewer.close()
        self.viewer = None
