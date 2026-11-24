"""Infer fixed arm mounting contact from existing assets, without scene extensions."""
import xml.etree.ElementTree as ET

import numpy as np


def component(obj, kind):
    return next((c.get('properties', {}) for c in obj.get('components', ())
                 if c.get('component_type') == kind), {})


def fixed_base_links(urdf):
    """Links rigidly connected to the URDF root, stopping at every moving joint."""
    root = ET.parse(urdf).getroot()
    joints = root.findall('joint')
    links = {link.get('name') for link in root.findall('link')}
    fixed = links - {j.find('child').get('link') for j in joints}
    while True:
        added = {j.find('child').get('link') for j in joints
                 if j.get('type') == 'fixed' and j.find('parent').get('link') in fixed}
        if added <= fixed:
            return frozenset(fixed)
        fixed.update(added)


def two_joint_neighbour_pairs(joints):
    """Unordered link pairs separated by one or two URDF joints.

    Fixed and moving joints each count as one edge. Traversal is undirected,
    so siblings connected through their common parent are also two apart.
    """
    graph = {}
    for joint in joints:
        a, b = joint.find('parent').get('link'), joint.find('child').get('link')
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    pairs = set()
    for link, neighbours in graph.items():
        nearby = set(neighbours)
        for neighbour in neighbours:
            nearby.update(graph[neighbour])
        pairs.update(frozenset((link, other)) for other in nearby if other != link)
    return pairs


def mounting_pedestals(scene, robot):
    """Recognize a static pedestal aligned below this arm, never ordinary obstacles."""
    base = np.asarray(component(robot, 'Transform').get('position', ()), dtype=float)
    if base.shape != (3,) or not np.isfinite(base).all():
        return frozenset()
    candidates = []
    for obj in scene.get('objects', ()):
        if obj is robot:
            continue
        semantic = component(obj, 'Semantic').get('semantic_name', '').lower()
        metadata = component(obj, 'MetadataRef')
        is_pedestal = semantic == 'robot_pedestal' or (
            metadata.get('type') == 'robot' and metadata.get('path') == '$LIBRARY_SERVER/4')
        if not is_pedestal or not component(obj, 'PhysicalProperties').get('is_static'):
            continue
        position = np.asarray(component(obj, 'Transform').get('position', ()), dtype=float)
        if (position.shape == (3,) and np.isfinite(position).all()
                and np.linalg.norm(position[:2] - base[:2]) <= .02
                and position[2] < base[2]):
            candidates.append(obj['object_name_in_scene'])
    # Ambiguous mounting assemblies need no automatic collision exemption.
    return frozenset(candidates) if len(candidates) == 1 else frozenset()
