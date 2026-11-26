"""Build a geometry-labelled planning model from a frozen execution runtime."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from .assets import ConversionError, fingerprint
from .scene import official_pose
from .urdf import set_origin


def _link(body: int) -> str:
    return f'runtime_body_{body}'


def _set_runtime_origin(joint, transform):
    set_origin(joint, transform)
    origin = joint.find('origin')
    for name, digits in (('xyz', 6), ('rpy', 4)):
        values = (round(float(value), digits) for value in origin.get(name).split())
        origin.set(name, ' '.join(str(value if value else 0.0) for value in values))


def _fixed(xml, name, parent, child, transform):
    joint = ET.SubElement(xml, 'joint', name=name, type='fixed')
    ET.SubElement(joint, 'parent', link=parent)
    ET.SubElement(joint, 'child', link=child)
    _set_runtime_origin(joint, np.asarray(transform))


def _geometry(entry: dict, folder: Path) -> tuple[ET.Element, str]:
    """Keep the collision frame separate so fitted spheres are reusable."""
    node = ET.Element('geometry')
    kind, size = entry['kind'], entry['size']
    shape = {k: entry[k] for k in ('kind', 'size', 'file_path') if k in entry}
    key = fingerprint(shape)
    if kind == 'box':
        ET.SubElement(node, 'box', size=' '.join(str(2 * x) for x in size))
    elif kind == 'sphere':
        ET.SubElement(node, 'sphere', radius=str(size[0]))
    elif kind == 'cylinder':
        ET.SubElement(node, 'cylinder', radius=str(size[0]), length=str(2 * size[1]))
    elif kind == 'mesh':
        ET.SubElement(node, 'mesh', filename=entry['file_path'])
    elif kind in {'capsule', 'ellipsoid'}:
        import trimesh
        path = folder / f'{key}.obj'
        if not path.exists():
            if kind == 'capsule':
                mesh = trimesh.creation.capsule(height=2 * size[1], radius=size[0])
                # trimesh versions differ on whether the capsule is centred.
                mesh.apply_translation(-mesh.bounds.mean(axis=0))
            else:
                mesh = trimesh.creation.icosphere(subdivisions=3)
                mesh.apply_scale(size)
            mesh.export(path)
        ET.SubElement(node, 'mesh', filename=str(path))
    else:
        raise ConversionError(f'Unsupported robot runtime geometry: {kind}')
    return node, key


def convert_runtime(snapshot: dict, cache_dir) -> dict:
    """Produce the same worker contract as the asset-based SceneConverter."""
    runtime = snapshot['_runtime_collision']
    if runtime.get('schema_version') != 1 or runtime['policy'].get('schema_version') != 1:
        raise ConversionError('Unsupported runtime collision-policy schema')
    cache = Path(cache_dir)
    folder = cache / 'runtime_geometry'
    folder.mkdir(parents=True, exist_ok=True)
    xml = ET.Element('robot', name='runtime_collision_assembly')
    for body in runtime['bodies']:
        name = _link(body['id'])
        ET.SubElement(xml, 'link', name=name)
        if body['parent'] is None:
            continue
        parent = _link(body['parent'])
        transform = np.round(np.asarray(body['matrix']), 12)
        if 'joint_name' not in body:
            _fixed(xml, f'{name}_mount', parent, name, transform)
        else:
            pivot = name + '_pivot'
            ET.SubElement(xml, 'link', name=pivot)
            joint = ET.SubElement(xml, 'joint', name=body['joint_name'], type=body['kind'])
            ET.SubElement(joint, 'parent', link=parent)
            ET.SubElement(joint, 'child', link=pivot)
            _set_runtime_origin(joint, transform)
            ET.SubElement(joint, 'axis', xyz=' '.join(map(str, body['axis'])))
            ET.SubElement(joint, 'limit', lower=str(body['limit'][0]), upper=str(body['limit'][1]),
                          effort='1000', velocity='3.14')
            _fixed(xml, name + '_pivot_mount', pivot, name, np.linalg.inv(body['pivot']))
    ET.SubElement(xml, 'link', name='curobo_tcp')
    _fixed(xml, 'curobo_tcp_fixed', _link(runtime['tcp_body']), 'curobo_tcp', runtime['tcp_local'])
    world, geometry_links, shape_keys, world_geoms, primitive_spheres = {}, {}, {}, {}, {}
    base_inverse = np.linalg.inv(runtime['base_world'])
    for geom in runtime['geometries']:
        ident = int(geom['id'])
        if geom['robot']:
            name = f'runtime_geom_{ident}'
            link = ET.SubElement(xml, 'link', name=name)
            if geom['kind'] == 'sphere_set':
                key = fingerprint(geom['spheres'])
                primitive_spheres[name] = deepcopy(geom['spheres'])
                for sphere in geom['spheres']:
                    for tag in ('collision', 'visual'):
                        element = ET.SubElement(link, tag)
                        local = np.eye(4); local[:3, 3] = sphere['center']
                        set_origin(element, local)
                        shape = ET.SubElement(element, 'geometry')
                        ET.SubElement(shape, 'sphere', radius=str(sphere['radius']))
            else:
                node, key = _geometry(geom, folder)
                for tag in ('collision', 'visual'):
                    ET.SubElement(link, tag).append(deepcopy(node))
            _fixed(xml, name + '_mount', _link(geom['body']), name, geom['matrix'])
            geometry_links[name], shape_keys[name] = ident, key
            if geom['kind'] == 'sphere':
                primitive_spheres[name] = [{'center': [0., 0., 0.], 'radius': float(geom['size'][0])}]
        else:
            name = f'runtime_obstacle_{ident}'
            transform = base_inverse @ np.asarray(geom['matrix'])
            kind, size = geom['kind'], geom['size']
            pose = official_pose(transform)
            if kind == 'box':
                world.setdefault('cuboid', {})[name] = {'pose': pose, 'dims': [2*x for x in size]}
            elif kind == 'plane':
                # An infinite plane cannot be replaced by a finite box without changing policy.
                raise ConversionError('Infinite runtime collision planes require an explicit bounded scene collider')
            elif kind in {'sphere', 'cylinder'}:
                item = {'pose': pose, 'radius': size[0]}
                if kind != 'sphere':
                    item['height'] = 2 * size[1]
                world.setdefault(kind, {})[name] = item
            else:
                node, _ = _geometry(geom, folder)
                path = node.find('mesh').get('filename')
                world.setdefault('mesh', {})[name] = {'pose': pose, 'file_path': path}
            world_geoms[name] = ident
    if not geometry_links:
        raise ConversionError('Runtime robot has no collision geometry')
    text = ET.tostring(xml, encoding='unicode')
    key = fingerprint(text)
    directory = cache / 'robots' / key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'assembly.urdf'
    if not path.exists():
        path.write_text(text, encoding='utf-8')
    limits = {b['joint_name']: b['limit'] for b in runtime['bodies'] if 'joint_name' in b}
    result = {'robot_key': key, 'robot_dir': str(directory), 'urdf': str(path),
        'base_link': _link(runtime['root']), 'tool_frame': 'curobo_tcp',
        'tool_spec': {'runtime_site': 'tcp', 'body': runtime['tcp_body']},
        'joint_names': runtime['joint_names'], 'active_names': runtime['joint_names'],
        'positions': dict(zip(runtime['joint_names'], runtime['joints'])), 'locked_joints': {},
        'world': world, 'base_world': runtime['base_world'],
        'current_tool_pose': official_pose(base_inverse @ np.asarray(runtime['tcp_world'])),
        'scene_limits': limits, 'collision_ignores': {}, 'asset_revisions': {},
        'geometry_links': geometry_links, 'shape_keys': shape_keys, 'world_geoms': world_geoms,
        'primitive_spheres': primitive_spheres,
        'collision_policy': runtime['policy'], 'payload_bodies': runtime['payload_bodies'],
        'planning_proxy': runtime.get('planning_proxy'),
        'fixed_simulation_scene': runtime.get('fixed_simulation_scene', False),
        'convex_self': runtime.get('convex_self'),
        'notes': ['Measured runtime geometry and frozen host pair policy; sphere fitting remains approximate.']}
    (directory / 'conversion_manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
