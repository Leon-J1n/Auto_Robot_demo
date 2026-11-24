import xml.etree.ElementTree as ET
from types import SimpleNamespace

from collision.mounting import two_joint_neighbour_pairs
from collision.backends.simplatform_fcl import SimPlatformFCLCollisionChecker
from execution.backends.mujoco.scene_compiler import exclude_arm_neighbours


def test_two_joint_policy_in_fcl_and_mujoco(tmp_path):
    links = ['base', 'one', 'two', 'three', 'branch']
    tree = ET.Element('robot')
    for name in links:
        ET.SubElement(tree, 'link', name=name)
    for a, b in [('base', 'one'), ('one', 'two'), ('two', 'three'), ('one', 'branch')]:
        joint = ET.SubElement(tree, 'joint', type='revolute')
        ET.SubElement(joint, 'parent', link=a)
        ET.SubElement(joint, 'child', link=b)
    path = tmp_path / 'robot.urdf'
    ET.ElementTree(tree).write(path)
    expected = two_joint_neighbour_pairs(tree.findall('joint'))
    assert frozenset(('base', 'two')) in expected
    assert frozenset(('two', 'branch')) in expected
    assert frozenset(('base', 'three')) not in expected
    assert all(len(pair) == 2 for pair in expected)
    entities = [SimpleNamespace(entity=i, collider=True, parent=-1) for i in range(len(links))]
    checker = SimpleNamespace(robot=SimpleNamespace(links=[(name, SimpleNamespace(handle=i)) for i, name in enumerate(links)]),
        world=SimpleNamespace(editor_entities=lambda: entities), robot_urdf=path,
        scene={'objects': []}, _allowed_link_pairs=[])
    actual = SimPlatformFCLCollisionChecker._structural_pairs(checker)
    assert {frozenset(links[i] for i in pair) for pair in actual} == expected
    root = ET.fromstring('<mujoco><worldbody/></mujoco>')
    wrapper = ET.SubElement(root.find('worldbody'), 'body', name='robot')
    for name in links:
        ET.SubElement(wrapper, 'body', name=name)
    ET.SubElement(root.find('worldbody'), 'body', name='obstacle')
    exclude_arm_neighbours(root, SimpleNamespace(robot_urdf=path, robot_object={'object_name_in_scene': 'robot'}))
    pairs = {frozenset((e.get('body1'), e.get('body2'))) for e in root.findall('./contact/exclude')}
    assert expected <= pairs
    assert frozenset(('base', 'three')) not in pairs
    assert frozenset(('two', 'obstacle')) not in pairs
