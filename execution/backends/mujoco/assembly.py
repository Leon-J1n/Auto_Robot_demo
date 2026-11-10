"""Assemble source URDF trees in memory; never write a combined URDF file."""
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation


def assemble_robot(arm_path, asset_root, attachments):
    from ...resources import component

    def read(path):
        tree = ET.parse(path).getroot()
        for mesh in tree.findall('.//mesh'):
            filename = (path.parent / mesh.get('filename')).resolve()
            if not filename.is_file():
                raise FileNotFoundError(filename)
            mesh.set('filename', str(filename))
        return tree

    arm = read(arm_path)
    for obj in attachments:
        source = component(obj, 'GripperModel')
        mount = component(obj, 'RobotAttachment')
        tool = read(asset_root / source['urdf'])
        parent_links = {e.get('name') for e in arm.findall('link')}
        child_links = {e.get('name') for e in tool.findall('link')}
        roots = child_links - {j.find('child').get('link') for j in tool.findall('joint')}
        if mount['parent_link'] not in parent_links or roots != {mount['child_link']}:
            raise ValueError('Attachment must connect an existing parent link to the child URDF root')
        if parent_links & child_links:
            raise ValueError('Attachment link names must be unique')
        transform = mount['transform']
        xyz = np.asarray(transform['position'], dtype=float)
        quat = np.asarray(transform['quaternion_xyzw'], dtype=float)
        if transform.get('reference_frame') != 'parent_link' or xyz.shape != (3,) or quat.shape != (4,) or not np.isfinite(np.r_[xyz, quat]).all() or not np.isclose(np.linalg.norm(quat), 1):
            raise ValueError('Attachment requires a finite parent-link pose and unit quaternion')
        names = {e.get('name') for e in arm if e.tag in {'link', 'joint', 'material'}}
        for e in tool:
            if e.tag not in {'link', 'joint', 'material'}:
                continue
            if e.get('name') in names:
                raise ValueError('Duplicate attachment element name: ' + e.get('name'))
            if e.tag == 'joint':
                mimic = e.find('mimic')
                if mimic is not None and float(mimic.get('offset', '0')) != 0:
                    raise ValueError('Nonzero mimic offsets are unsupported')
                # Import the zero-pose structure; the execution gripper adapter
                # restores physical joints and actuators from the original URDF.
                e.set('type', 'fixed')
                for child in list(e):
                    if child.tag in {'axis', 'limit', 'dynamics', 'mimic'}:
                        e.remove(child)
            arm.append(e)
        j = ET.SubElement(arm, 'joint', name=f"attachment_{obj['object_id_in_scene']}", type='fixed')
        ET.SubElement(j, 'parent', link=mount['parent_link'])
        ET.SubElement(j, 'child', link=mount['child_link'])
        ET.SubElement(j, 'origin', xyz=' '.join(map(str, xyz)),
                      rpy=' '.join(map(str, Rotation.from_quat(quat).as_euler('xyz'))))
    return arm
