"""Convert canonical scene resources into MuJoCo MJCF."""
from pathlib import Path
import xml.etree.ElementTree as ET

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from ...resources import component, pose_matrix

def exclude_arm_neighbours(root, resources):
    # MuJoCo does not automatically filter a moving link against its welded
    # world/base parent. Visual-derived base geometry now makes that pair
    # tangible. Match the planner's two-joint-neighbour and explicit exemptions.
    arm = ET.parse(resources.robot_urdf).getroot()
    from collision.mounting import two_joint_neighbour_pairs
    pairs = [tuple(sorted(pair)) for pair in sorted(
        two_joint_neighbour_pairs(arm.findall('joint')), key=lambda pair: tuple(sorted(pair)))]
    pairs.extend(component(resources.robot_object, 'RobotModelRef').get('allowed_collision_link_pairs', []))
    from collision.mounting import fixed_base_links
    base_links = fixed_base_links(resources.robot_urdf)
    # Keep attachments in the robot subtree; only external bodies are exempt.
    robot_name = resources.robot_object.get('object_name_in_scene')
    world = root.find('worldbody')
    for wrapper in world.findall('body') if world is not None else ():
        if wrapper.get('name') == robot_name or any(
                body.get('name') in base_links for body in wrapper.iter('body')):
            continue
        pairs.extend((link, body.get('name')) for link in base_links for body in wrapper.iter('body'))
    if world is not None and world.findall('geom'):
        pairs.extend((link, 'world') for link in base_links)
    contact = root.find('contact')
    if contact is None:
        contact = ET.SubElement(root, 'contact')
    existing = {frozenset((e.get('body1'), e.get('body2'))) for e in contact.findall('exclude')}
    bodies = {b.get('name') for b in root.iter('body')} | {'world'}
    for a, b in pairs:
        key = frozenset((a, b))
        if a in bodies and b in bodies and key not in existing:
            ET.SubElement(contact, 'exclude', body1=a, body2=b)
            existing.add(key)


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
    # The articulated spring links have concave mesh colliders. Their whole
    # convex hull intrudes into the closing workspace; preserve their cavities.
    from copy import deepcopy
    from collision.assets.mesh import prepare_mesh
    for link in root.findall('link'):
        if not link.get('name', '').startswith('robotiq_2f85_') or not link.get('name', '').endswith('_spring_link'):
            continue
        for collision in list(link.findall('collision')):
            if collision.get('name', '').startswith(link.get('name') + '_visual_collision_'):
                # Already decomposed by the shared visual collision pipeline.
                continue
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
    model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding='unicode'))
    intermediate = output_dir / f'{prefix}_import.xml'
    mujoco.mj_saveLastXML(str(intermediate), model)
    imported = ET.parse(intermediate).getroot()
    # mj_saveLastXML rounds numeric attributes (e.g. 1.2217304 -> 1.22173).
    # Keep the compiled URDF limits at full precision so valid IK boundary
    # solutions are also valid for execution and collision queries.
    for joint in imported.iter('joint'):
        name = joint.get('name')
        if name is None:
            continue
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if jid >= 0 and model.jnt_limited[jid]:
            joint.set('range', numbers(model.jnt_range[jid]))
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
    # MuJoCo 3 uses a shared arena for contacts, constraints and the stack.
    # Dense convex meshes can exceed the ~86 MiB legacy nstack allocation.
    ET.SubElement(root, 'size', memory='256M')
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
        object_scale = np.asarray(component(obj, 'Transform').get('scale', [1, 1, 1]), dtype=float)
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
                ET.SubElement(wrapper, 'geom', type='box', size=numbers(np.asarray(collider['size']) * object_scale / 2), **common)
            elif collider.get('shape') == 'cylinder':
                size = collider['size']
                if not np.isclose(object_scale[0], object_scale[1]):
                    raise ValueError(f'Non-uniform cylinder scale is unsupported: {name}')
                ET.SubElement(wrapper, 'geom', type='cylinder', size=numbers([size[0] * object_scale[0], size[2] * object_scale[2] / 2]), **common)
            elif asset_path:
                from collision.assets.mesh import prepare_mesh
                collision_path = resources.asset(collider['collision_mesh']) if collider.get('collision_mesh') else asset_path
                prepared = prepare_mesh(collision_path)
                ET.SubElement(asset, 'mesh', name=name + '_mesh', file=str(asset_path), scale=numbers(object_scale))
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
                    ET.SubElement(asset, 'mesh', name=part_name, file=str(part), scale=numbers(object_scale))
                    attributes = dict(common, name=part_name, group='3')
                    if dynamic:
                        attributes['mass'] = str(float(common['mass']) * volume / total_volume)
                    ET.SubElement(wrapper, 'geom', type='mesh', mesh=part_name, **attributes)
            else:
                raise ValueError(f'Unsupported geometry: {name}')
    actuators = ET.SubElement(root, 'actuator')
    for name in resources.joint_names:
        ET.SubElement(actuators, 'position', name=name + '_servo', joint=name, kp='1200', kv='90')
    from .gripper import add_gripper
    add_gripper(root, resources)
    exclude_arm_neighbours(root, resources)
    path = output_dir / 'scene.mjcf.xml'
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path
