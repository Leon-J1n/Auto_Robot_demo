"""Opt-in articulated Robotiq model and inactive payload welds."""
import xml.etree.ElementTree as ET
from copy import deepcopy
from .resources import component


def add_gripper(root, resources):
    config = component(resources.robot_object, 'GripperModel')
    if not config:
        return
    source = ET.parse(resources.asset(config['urdf'])).getroot()
    friction = config.get('_ols_friction')
    friction_attr = ' '.join(format(float(value), '.17g') for value in friction) if friction is not None else None
    bodies = {b.get('name'): b for b in root.iter('body')}
    equality = ET.SubElement(root, 'equality')
    contact = ET.SubElement(root, 'contact')
    master = 'robotiq_2f85_right_driver_joint'
    for joint in source.findall('joint'):
        if joint.get('type') == 'fixed':
            continue
        body = bodies[joint.find('child').get('link')]
        limits = joint.find('limit')
        name = joint.get('name')
        ET.SubElement(body, 'joint', name=name, type='hinge', axis=joint.find('axis').get('xyz'),
                      range=f"{limits.get('lower')} {limits.get('upper')}", damping='0.05', armature='0.001')
        if name != master:
            ratio = -1 if ('coupler' in name or 'follower' in name) else 1
            ET.SubElement(equality, 'joint', joint1=name, joint2=master,
                          polycoef=f'0 {ratio} 0 0 0', solref='0.005 1')
    for side in ('left', 'right'):
        ET.SubElement(contact, 'exclude', body1=f'robotiq_2f85_{side}_coupler', body2=f'robotiq_2f85_{side}_follower')
        for suffix in ('pad', 'silicone_pad'):
            body = bodies[f'robotiq_2f85_{side}_{suffix}']
            for geom in list(body.findall('geom')):
                # Finger pad boxes supply the contact surface; overlapping visual meshes do not.
                if geom.get('type') == 'mesh' or geom.get('mesh'):
                    geom.set('contype', '0'); geom.set('conaffinity', '0')
                else:
                    # Include torsional friction around the pad contact normal.
                    # The pad boxes are the active contact surface; the overlapping
                    # visual meshes remain disabled above.
                    geom.set('solref', '0.005 1'); geom.set('condim', '4')
                    if friction_attr is not None:
                        geom.set('friction', friction_attr)
                    if suffix == 'pad' and geom.get('type') == 'box':
                        # Tile each active pad box into four independent contact
                        # geoms.  Convex box-box collision otherwise often yields
                        # a single representative point for a broad face.
                        size = [float(v) for v in geom.get('size', '').split()]
                        pos = [float(v) for v in geom.get('pos', '0 0 0').split()]
                        if len(size) == 3 and len(pos) == 3:
                            index = list(body).index(geom)
                            body.remove(geom)
                            for ix in (-1, 1):
                                for iz in (-1, 1):
                                    tile = deepcopy(geom)
                                    tile.set('size', f'{size[0] / 2:g} {size[1]:g} {size[2] / 2:g}')
                                    tile.set('pos', f'{pos[0] + ix * size[0] / 2:g} {pos[1]:g} {pos[2] + iz * size[2] / 2:g}')
                                    body.insert(index, tile)
                                    index += 1
    actuators = root.find('actuator')
    ET.SubElement(actuators, 'position', name='grasp_servo', joint=master, kp='10', kv='0.3',
                  ctrlrange='0 0.8', forcerange='-2 2')
