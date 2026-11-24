"""Generate a cached collision URDF from link-local visual geometry.

Only the arm URDF is processed, before attachments are assembled. Kinematics,
inertias and visual assets stay intact; both physics and FCL use the result.
"""
from copy import deepcopy
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation
import trimesh

from .mesh import CACHE_ROOT, prepare_mesh


DEFAULTS = dict(mode='visual', max_convex_hulls=8, max_vertices=64,
                max_input_faces=3000, threshold=.05, exclude_links=[], package_paths={}, link_overrides={})
DECOMPOSITION_SETTINGS = dict(preprocess_mode='on', preprocess_resolution=30,
                              resolution=300, mcts_iterations=5)


def _asset(filename, urdf, packages):
    if filename.startswith('package://'):
        package, relative = filename[10:].split('/', 1)
        if package in packages:
            path = Path(packages[package]) / relative
        else:
            path = next((parent / package / relative for parent in urdf.parents
                         if (parent / package / relative).is_file()), None)
            if path is None:
                raise ValueError(f'Unresolved package {package!r}; set collision_geometry.package_paths')
    else:
        path = Path(filename.removeprefix('file://'))
        if not path.is_absolute():
            path = urdf.parent / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def prepare_robot_visual_collisions(source, options=None, *, cache_root=None):
    """Return a derived URDF path, with bounded convex geometry per link."""
    source = Path(source).resolve()
    options = {**deepcopy(DEFAULTS), **deepcopy(options or {})}
    if set(options) - set(DEFAULTS) or options['mode'] not in ('auto', 'visual', 'authored'):
        raise ValueError('Invalid robot collision_geometry options')
    # Legacy scene modes remain readable, but never bypass visual generation.
    options['mode'] = 'visual'
    for key, minimum in (('max_convex_hulls', 1), ('max_vertices', 8), ('max_input_faces', 12)):
        if type(options[key]) is not int or options[key] < minimum:
            raise ValueError(f'collision_geometry.{key} must be an integer >= {minimum}')
    if (isinstance(options['threshold'], bool) or not np.isfinite(options['threshold'])
            or options['threshold'] <= 0):
        raise ValueError('collision_geometry.threshold must be positive and finite')
    if (not isinstance(options['exclude_links'], list)
            or any(not isinstance(v, str) for v in options['exclude_links'])
            or not isinstance(options['package_paths'], dict)):
        raise ValueError('Invalid collision_geometry link exclusions or package paths')
    root = ET.parse(source).getroot()
    overrides = options['link_overrides']
    allowed_overrides = {'max_convex_hulls', 'max_vertices', 'max_input_faces', 'threshold'}
    links = {link.get('name') for link in root.findall('link')}
    if (not isinstance(overrides, dict) or set(overrides) - links
            or any(not isinstance(value, dict) or set(value) - allowed_overrides for value in overrides.values())):
        raise ValueError('Invalid collision_geometry.link_overrides')
    for values in overrides.values():
        for key, value in values.items():
            if key == 'threshold':
                valid = not isinstance(value, bool) and isinstance(value, (int, float)) and np.isfinite(value) and value > 0
            else:
                valid = type(value) is int and value >= {'max_convex_hulls': 1, 'max_vertices': 8, 'max_input_faces': 12}[key]
            if not valid:
                raise ValueError(f'Invalid collision_geometry.link_overrides.{key}')
    missing = set(options['exclude_links']) - {link.get('name') for link in root.findall('link')}
    if missing:
        raise ValueError(f'Unknown collision_geometry.exclude_links: {sorted(missing)}')
    for link in root.findall('link'):
        if (link.findall('collision') and not link.findall('visual')
                and link.get('name') not in options['exclude_links']):
            raise ValueError(f'Robot link {link.get("name")} has collision but no visual geometry')
    # Precomputed bundles must explicitly certify this same visual-only policy
    # and its effective options; older authored/mixed bundles cannot bypass it.
    from .bundle import configured_bundle
    bundle = configured_bundle()
    if bundle is not None:
        if bundle.manifest.get('robot_collision_geometry') != options:
            raise ValueError('collision_bundle_visual_policy_mismatch')
        bundled = bundle.robot_urdf(source)
        if bundled is None:
            raise ValueError('collision_bundle_asset_missing')
        return bundled
    # Obsolete authored mesh paths need not exist when visuals replace them.
    for link in root.findall('link'):
        if link.findall('visual') or link.get('name') in options['exclude_links']:
            for collider in list(link.findall('collision')):
                link.remove(collider)
    # Resolve visual asset paths after removing authored colliders.
    inputs = []
    for mesh in root.findall('.//mesh'):
        path = _asset(mesh.get('filename'), source, options['package_paths'])
        mesh.set('filename', str(path))
        inputs.append(dict(path=str(path), sha256=sha256(path.read_bytes()).hexdigest()))
    identity = dict(schema=3, coacd=version('coacd'), trimesh=version('trimesh'),
                    simplifier=version('fast-simplification'),
                    urdf=ET.tostring(root, encoding='unicode'),
                    inputs=inputs, options=options, decomposition=DECOMPOSITION_SETTINGS)
    key = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    directory = Path(cache_root or CACHE_ROOT) / 'robots' / key
    target, manifest = directory / 'robot.urdf', directory / 'manifest.json'
    if directory.exists() and not manifest.exists():
        raise ValueError(f'Incomplete robot collision cache: {directory}')
    if manifest.exists():
        record = json.loads(manifest.read_text(encoding='utf-8'))
        if (record['identity'] != identity or any(
                sha256(Path(p).read_bytes()).hexdigest() != digest
                for p, digest in record['files'].items())):
            raise ValueError(f'Invalid robot collision cache: {directory}')
        return target
    directory.parent.mkdir(parents=True, exist_ok=True)
    settings = dict(max_convex_hull=options['max_convex_hulls'], decimate=True,
                    max_ch_vertex=options['max_vertices'], threshold=options['threshold'],
                    max_input_faces=options['max_input_faces'], **DECOMPOSITION_SETTINGS)
    records, dependencies = [], {}
    with tempfile.TemporaryDirectory(dir=directory.parent, prefix=key + '.') as tmp:
        staging = Path(tmp)
        for index, link in enumerate(root.findall('link')):
            name = link.get('name')
            visuals = link.findall('visual')
            if name in options['exclude_links']:
                for collider in list(link.findall('collision')):
                    link.remove(collider)
                records.append(dict(link=name, source='excluded', parts=0))
                continue
            if not visuals:
                records.append(dict(link=name, source='empty', parts=0))
                continue
            pieces = []
            for visual in visuals:
                geometry = visual.find('geometry')
                node = geometry.find('mesh') if geometry is not None else None
                if node is not None:
                    mesh = trimesh.load(node.get('filename'), force='mesh', process=True)
                    scale = np.fromstring(node.get('scale', '1 1 1'), sep=' ')
                    if scale.shape != (3,) or not np.isfinite(scale).all() or np.any(scale <= 0):
                        raise ValueError(f'Invalid visual scale on {name}')
                    mesh.apply_scale(scale)
                elif geometry is not None and geometry.find('box') is not None:
                    mesh = trimesh.creation.box(extents=np.fromstring(geometry.find('box').get('size'), sep=' '))
                elif geometry is not None and geometry.find('cylinder') is not None:
                    node = geometry.find('cylinder')
                    mesh = trimesh.creation.cylinder(radius=float(node.get('radius')), height=float(node.get('length')))
                elif geometry is not None and geometry.find('sphere') is not None:
                    mesh = trimesh.creation.icosphere(subdivisions=2, radius=float(geometry.find('sphere').get('radius')))
                else:
                    raise ValueError(f'Unsupported visual geometry on {name}')
                origin = visual.find('origin')
                transform = np.eye(4)
                if origin is not None:
                    xyz = np.fromstring(origin.get('xyz', '0 0 0'), sep=' ')
                    rpy = np.fromstring(origin.get('rpy', '0 0 0'), sep=' ')
                    if xyz.shape != (3,) or rpy.shape != (3,) or not np.isfinite(np.r_[xyz, rpy]).all():
                        raise ValueError(f'Invalid visual origin on {name}')
                    transform[:3, :3] = Rotation.from_euler('xyz', rpy).as_matrix()
                    transform[:3, 3] = xyz
                mesh.apply_transform(transform)
                pieces.append(mesh)
            merged = trimesh.util.concatenate(pieces)
            merged.merge_vertices(merge_tex=True, merge_norm=True)
            combined = staging / f'link_{index}.obj'
            combined.write_text(trimesh.exchange.obj.export_obj(merged, include_texture=False), encoding='utf-8')
            link_settings = dict(settings)
            mapping = dict(max_convex_hulls='max_convex_hull', max_vertices='max_ch_vertex',
                           max_input_faces='max_input_faces', threshold='threshold')
            link_settings.update({mapping[key]: value for key, value in overrides.get(name, {}).items()})
            prepared = prepare_mesh(combined, cache_root=cache_root, settings=link_settings)
            for collider in list(link.findall('collision')):
                link.remove(collider)
            for part_index, part in enumerate(prepared.parts):
                collider = ET.SubElement(link, 'collision', name=f'{name}_visual_collision_{part_index}')
                geometry = ET.SubElement(collider, 'geometry')
                ET.SubElement(geometry, 'mesh', filename=str(part), scale='1 1 1')
                dependencies[str(part)] = sha256(part.read_bytes()).hexdigest()
            records.append(dict(link=name, source='visual', parts=len(prepared.parts), cache_key=prepared.key))
        data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        (staging / 'robot.urdf').write_bytes(data)
        dependencies[str(target)] = sha256(data).hexdigest()
        (staging / 'manifest.json').write_text(json.dumps(dict(identity=identity, links=records,
            files=dependencies), indent=2), encoding='utf-8')
        try:
            os.rename(staging, directory)
        except FileExistsError:
            return prepare_robot_visual_collisions(source, options, cache_root=cache_root)
    return target
