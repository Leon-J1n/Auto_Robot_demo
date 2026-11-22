"""Automatic mounting exemptions never cover moving arm links or obstacles."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from collision.mounting import fixed_base_links, mounting_pedestals
from collision.backends.simplatform_fcl import SimPlatformFCLCollisionChecker
from execution.backends.mujoco.scene_compiler import exclude_arm_neighbours


def obj(name, position, semantic='', static=False):
    return dict(object_name_in_scene=name, components=[
        dict(component_type='Transform', properties=dict(position=position)),
        dict(component_type='Semantic', properties=dict(semantic_name=semantic)),
        dict(component_type='PhysicalProperties', properties=dict(is_static=static))])


class MountingPolicyTests(unittest.TestCase):
    def test_recognizes_only_static_aligned_unambiguous_pedestal(self):
        robot = obj('arm', [0, 0, .5])
        pedestal = obj('RobotPedestal', [0, 0, .25], 'robot_pedestal', True)
        scene = dict(objects=[robot, pedestal, obj('Table', [0, 0, .2], 'surface', True)])
        original = deepcopy(scene)
        self.assertEqual(mounting_pedestals(scene, robot), {'RobotPedestal'})
        self.assertEqual(scene, original)
        for position, static in (([.2, 0, .25], True), ([0, 0, .6], True), ([0, 0, .25], False)):
            bad = obj('Other', position, 'robot_pedestal', static)
            self.assertFalse(mounting_pedestals(dict(objects=[robot, bad]), robot))
        self.assertFalse(mounting_pedestals(dict(objects=[robot, pedestal,
            obj('Other', [0, 0, .2], 'robot_pedestal', True)]), robot))

    def test_fixed_links_and_fcl_filter_exclude_only_mounting_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            urdf = Path(tmp)/'arm.urdf'
            urdf.write_text('<robot><link name="root"/><link name="base"/><link name="moving"/>'
                '<joint type="fixed"><parent link="root"/><child link="base"/></joint>'
                '<joint type="revolute"><parent link="base"/><child link="moving"/></joint></robot>')
            self.assertEqual(fixed_base_links(urdf), {'root', 'base'})
            checker = SimpleNamespace(_allowed_arm_environment_names=frozenset(),
                _entities={'RobotPedestal': 1}, _collider_links={10: 'base', 11: 'moving'},
                _arm_link_names={'root', 'base', 'moving'}, _fixed_base_link_names=fixed_base_links(urdf),
                _names={10: 'base', 11: 'moving', 20: 'RobotPedestal', 21: 'Table'},
                _mounting_pedestal_names={'RobotPedestal'})
            self.assertEqual(SimPlatformFCLCollisionChecker._arm_environment_pairs(checker),
                             {frozenset((10, 20))})
            root = ET.fromstring('<mujoco><worldbody><body name="root"><body name="base">'
                '<body name="moving"/></body></body><body name="RobotPedestal"/>'
                '<body name="Table"/></worldbody></mujoco>')
            robot = obj('arm', [0, 0, .5])
            resources = SimpleNamespace(robot_urdf=urdf, robot_object=robot,
                scene=dict(objects=[robot, obj('RobotPedestal', [0, 0, .25], 'robot_pedestal', True)]))
            exclude_arm_neighbours(root, resources)
            pairs = {frozenset((e.get('body1'), e.get('body2'))) for e in root.findall('contact/exclude')}
            self.assertIn(frozenset(('base', 'RobotPedestal')), pairs)
            self.assertNotIn(frozenset(('moving', 'RobotPedestal')), pairs)
            self.assertNotIn(frozenset(('base', 'Table')), pairs)


if __name__ == '__main__':
    unittest.main()
