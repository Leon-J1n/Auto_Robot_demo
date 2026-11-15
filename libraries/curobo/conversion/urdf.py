"""URDF FK and algebraic mimic expansion, independent of yourdfpy."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

from .assets import ConversionError, safe_relative


def vector(value, count, field):
    array = np.asarray(value, dtype=float)
    if array.shape != (count,) or not np.all(np.isfinite(array)):
        raise ConversionError(f'{field} requires {count} finite numbers')
    return array


def pose(properties=None):
    p = properties or {}
    matrix = np.eye(4)
    matrix[:3, 3] = vector(p.get('position', [0, 0, 0]), 3, 'position')
    quaternion = vector(p.get('quaternion_xyzw', [0, 0, 0, 1]), 4, 'quaternion_xyzw')
    if np.linalg.norm(quaternion) < 1e-10:
        raise ConversionError('Quaternion cannot be zero')
    matrix[:3, :3] = Rotation.from_quat(quaternion).as_matrix()
    scale = vector(p.get('scale', [1, 1, 1]), 3, 'scale')
    if np.any(scale <= 0):
        raise ConversionError('Scale must be positive')
    matrix[:3, :3] = matrix[:3, :3] @ np.diag(scale)
    return matrix


def rigid(matrix):
    scales = np.linalg.norm(matrix[:3, :3], axis=0)
    rotation = matrix[:3, :3] / scales
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-7) or np.linalg.det(rotation) < 0:
        raise ConversionError('This transform has shear/reflection; an explicit baked mesh is required')
    result = matrix.copy()
    result[:3, :3] = rotation
    return result, scales


def origin(node):
    if node is None:
        return np.eye(4)
    matrix = np.eye(4)
    matrix[:3, 3] = vector(node.get('xyz', '0 0 0').split(), 3, 'URDF xyz')
    matrix[:3, :3] = Rotation.from_euler('xyz', vector(node.get('rpy', '0 0 0').split(), 3, 'URDF rpy')).as_matrix()
    return matrix


def set_origin(joint, matrix):
    p, scales = rigid(matrix)
    if not np.allclose(scales, 1):
        raise ConversionError('An assembled robot mount cannot contain scaling')
    old = joint.find('origin')
    if old is not None:
        joint.remove(old)
    ET.SubElement(joint, 'origin', xyz=' '.join(map(str, p[:3, 3])),
                  rpy=' '.join(map(str, Rotation.from_matrix(p[:3, :3]).as_euler('xyz'))))


class URDFModel:
    def __init__(self, path):
        self.path = Path(path)
        self.xml = ET.parse(path).getroot()
        self.links = {n.attrib['name']: n for n in self.xml.findall('link')}
        self.joints = {n.attrib['name']: n for n in self.xml.findall('joint')}
        children = {j.find('child').attrib['link'] for j in self.joints.values()}
        roots = set(self.links) - children
        if len(roots) != 1:
            raise ConversionError(f'URDF must have one root: {path}')
        self.root = roots.pop()
        self.independent = [name for name,j in self.joints.items()
                            if j.get('type') != 'fixed' and j.find('mimic') is None]
        for name,j in self.joints.items():
            if j.get('type') not in {'fixed', 'revolute', 'continuous', 'prismatic'}:
                raise ConversionError(f'Unsupported URDF joint type: {name}/{j.get("type")}')
            self.mimic_relation(name)

    def mimic_relation(self, name, trail=()):
        if name in trail or name not in self.joints:
            raise ConversionError(f'Invalid/cyclic mimic chain: {trail + (name,)}')
        joint = self.joints[name]
        mimic = joint.find('mimic')
        if mimic is None:
            return name, 1., 0.
        source = mimic.get('joint')
        if source not in self.joints or self.joints[source].get('type') == 'fixed':
            raise ConversionError(f'Mimic source is missing/fixed: {name}/{source}')
        root, scale, offset = self.mimic_relation(source, trail + (name,))
        a,b = float(mimic.get('multiplier', 1)), float(mimic.get('offset', 0))
        if not np.isfinite([a,b]).all():
            raise ConversionError('Mimic coefficients must be finite')
        return root, a*scale, a*offset+b

    def positions(self, driver):
        initial = driver.get('initial_joint_positions_rad') or {}
        now = driver.get('now_joint_positions_rad') or {}
        if not isinstance(initial, dict) or not isinstance(now, dict):
            raise ConversionError('Joint positions must be dictionaries keyed by joint name')
        declared = {**initial, **now}
        unknown = set(declared) - set(self.joints)
        if unknown:
            raise ConversionError(f'Joint positions name unknown URDF joints: {sorted(unknown)}')
        effective = {}
        for name in self.joints:
            source,a,b = self.mimic_relation(name)
            q = a*float(declared.get(source, 0.))+b
            if not np.isfinite(q):
                raise ConversionError(f'Non-finite joint: {name}')
            if source != name and name in now and not np.isclose(float(now[name]), q, atol=1e-5):
                raise ConversionError(f'Current mimic value disagrees with driver: {name}')
            limit = self.joints[name].find('limit')
            if limit is not None and self.joints[name].get('type') != 'continuous':
                lo,hi = float(limit.get('lower', '-inf')),float(limit.get('upper', 'inf'))
                if q < lo-1e-5 or q > hi+1e-5:
                    raise ConversionError(f'Current joint outside URDF limits: {name}={q}, [{lo},{hi}]')
            scene_limit = driver.get('joint_limits', {}).get(name)
            if scene_limit is not None and not float(scene_limit[0])-1e-5 <= q <= float(scene_limit[1])+1e-5:
                raise ConversionError(f'Current joint outside scene limits: {name}={q}')
            effective[name] = q
        return effective

    def fk(self, positions):
        result = {self.root: np.eye(4)}
        pending = list(self.joints.values())
        while pending:
            previous = len(pending)
            for joint in pending[:]:
                parent = joint.find('parent').get('link')
                child = joint.find('child').get('link')
                if parent not in result:
                    continue
                motion = np.eye(4)
                kind = joint.get('type')
                axis_node = joint.find('axis')
                axis = vector(axis_node.get('xyz', '1 0 0').split() if axis_node is not None else [1,0,0],3,'joint axis')
                if kind != 'fixed':
                    if np.linalg.norm(axis) < 1e-12:
                        raise ConversionError('Joint axis cannot be zero')
                    axis /= np.linalg.norm(axis)
                    q = positions[joint.get('name')]
                    if kind == 'prismatic':
                        motion[:3,3] = axis*q
                    else:
                        motion[:3,:3] = Rotation.from_rotvec(axis*q).as_matrix()
                result[child] = result[parent] @ origin(joint.find('origin')) @ motion
                pending.remove(joint)
            if previous == len(pending):
                raise ConversionError('URDF joint tree is disconnected/cyclic')
        if set(result) != set(self.links):
            raise ConversionError('URDF has disconnected links')
        return result

    def namespaced(self, prefix):
        result = deepcopy(self.xml)
        for mesh in result.findall('.//mesh'):
            name = mesh.get('filename', '')
            if name.startswith('package://'):
                raise ConversionError(f'package:// mesh needs an explicit OLS mapping: {name}')
            relative = safe_relative(name)
            filename = self.path.parent.joinpath(*relative.parts).resolve()
            if not filename.is_file():
                raise ConversionError(f'URDF dependency missing from OLS reference list: {name}')
            mesh.set('filename', str(filename))
        for node in result:
            if node.tag in {'link', 'joint', 'material'} and node.get('name'):
                node.set('name', prefix+node.get('name'))
            if node.tag == 'joint':
                original = node.get('name')[len(prefix):]
                for tag in ('parent','child'):
                    n=node.find(tag)
                    n.set('link',prefix+n.get('link'))
                mimic=node.find('mimic')
                if mimic is not None:
                    source,a,b=self.mimic_relation(original)
                    mimic.attrib.update(joint=prefix+source,multiplier=str(a),offset=str(b))
        for material in result.findall('.//visual/material'):
            if material.get('name'):
                material.set('name',prefix+material.get('name'))
        return result
