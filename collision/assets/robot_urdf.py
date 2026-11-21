"""Prepare an FCL-only URDF, filling missing physical colliders from visuals."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

from .mesh import CACHE_ROOT, prepare_mesh


def _mesh_path(filename, urdf_path):
    if not filename:
        raise ValueError(f'URDF mesh in {urdf_path} has no filename')
    path = Path(filename).expanduser()
    if not path.is_absolute():
        if '://' in filename:
            raise ValueError(f'Unsupported URDF mesh URI: {filename}')
        path = urdf_path.parent / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f'URDF mesh not found: {path}')
    return path


def _mesh_scale(mesh):
    values = np.asarray([float(v) for v in mesh.get('scale', '1 1 1').split()], dtype=float)
    if values.shape != (3,) or not np.isfinite(values).all() or np.any(values <= 0):
        raise ValueError(f'Invalid URDF mesh scale: {mesh.get("scale")}')
    return values


def _geometry_shape(geometry):
    shapes = list(geometry) if geometry is not None else []
    if len(shapes) != 1 or shapes[0].tag not in {'box', 'cylinder', 'sphere', 'mesh'}:
        raise ValueError('URDF collision fallback supports one box, cylinder, sphere, or mesh per visual')
    return shapes[0]


def _append_visual_collision(link, visual, *, source, mesh_cache_root, fallback_index):
    geometry = visual.find('geometry')
    shape = _geometry_shape(geometry)
    origin = visual.find('origin')
    if shape.tag != 'mesh':
        collision = ET.Element('collision', name=f'fcl_visual_fallback_{fallback_index}')
        if origin is not None:
            collision.append(deepcopy(origin))
        collision.append(deepcopy(geometry))
        link.append(collision)
        return 1

    mesh_path = _mesh_path(shape.get('filename'), source)
    scale = _mesh_scale(shape)
    decomposition = prepare_mesh(mesh_path, scale=scale, cache_root=mesh_cache_root)
    for index, part in enumerate(decomposition.parts):
        collision = ET.Element('collision',
                               name=f'fcl_visual_fallback_{fallback_index}_{index:04d}')
        if origin is not None:
            collision.append(deepcopy(origin))
        geometry = ET.SubElement(collision, 'geometry')
        ET.SubElement(geometry, 'mesh', filename=str(part))
        link.append(collision)
    return len(decomposition.parts)


def prepare_fcl_robot_urdf(robot_urdf, *, cache_root=None):
    """Return a cached FCL URDF with visual-derived collision geometry as needed.

    Authored ``collision`` elements always win. Empty, massless fixed-frame
    links are valid URDF structure and remain geometry-free; inertial or moving
    links without collision or visual geometry are rejected.
    """
    source = Path(robot_urdf).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f'Robot URDF not found: {source}')
    root = ET.parse(source).getroot()
    if root.tag != 'robot':
        raise ValueError(f'Invalid robot URDF root: {source}')

    mesh_cache_root = Path(cache_root).resolve() if cache_root is not None else CACHE_ROOT
    joint_types = {}
    for joint in root.findall('joint'):
        for tag in ('parent', 'child'):
            endpoint = joint.find(tag)
            if endpoint is not None:
                joint_types.setdefault(endpoint.get('link'), set()).add(joint.get('type'))
    fallbacks = 0
    fallback_shapes = 0
    mesh_hashes = {}
    for link in root.findall('link'):
        collisions = link.findall('collision')
        visuals = link.findall('visual')
        for collision in collisions:
            geometry = collision.find('geometry')
            _geometry_shape(geometry)
        if collisions:
            continue
        if not visuals:
            connected_joints = joint_types.get(link.get('name'), set())
            frame_only = (link.find('inertial') is None and connected_joints
                          and all(kind == 'fixed' for kind in connected_joints))
            if not frame_only:
                raise ValueError(
                    f'Robot link {link.get("name")} has no collision or visual geometry'
                )
            continue
        for index, visual in enumerate(visuals):
            shape = _geometry_shape(visual.find('geometry'))
            if shape.tag == 'mesh':
                path = _mesh_path(shape.get('filename'), source)
                mesh_hashes[str(path)] = sha256(path.read_bytes()).hexdigest()
            fallback_shapes += _append_visual_collision(
                link, visual, source=source, mesh_cache_root=mesh_cache_root,
                fallback_index=index,
            )
        fallbacks += 1

    if not fallbacks:
        return source

    # The cached URDF lives outside the asset directory, so resolve all URDF
    # mesh and texture references against the original source before publishing.
    for mesh in root.findall('.//mesh'):
        path = _mesh_path(mesh.get('filename'), source)
        mesh_hashes[str(path)] = sha256(path.read_bytes()).hexdigest()
        mesh.set('filename', str(path))
    for texture in root.findall('.//texture'):
        filename = texture.get('filename')
        if filename:
            path = _mesh_path(filename, source)
            texture.set('filename', str(path))

    ET.indent(root)
    xml_bytes = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    identity = sha256(json.dumps({
        'schema': 1,
        'source_urdf_sha256': sha256(source.read_bytes()).hexdigest(),
        'mesh_sha256': mesh_hashes,
        'generated_urdf_sha256': sha256(xml_bytes).hexdigest(),
    }, sort_keys=True).encode()).hexdigest()
    directory = mesh_cache_root / 'robot_urdf' / identity
    output = directory / source.name
    if output.exists():
        if output.read_bytes() != xml_bytes:
            raise ValueError(f'Invalid cached FCL robot URDF: {output}')
        return output

    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=source.name + '.')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(xml_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    print(f'FCL visual collision fallback: {source.name}, {fallbacks} links, '
          f'{fallback_shapes} collision shapes', flush=True)
    return output
