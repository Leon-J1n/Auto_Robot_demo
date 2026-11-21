"""Tests for the FCL-only URDF visual-collider fallback."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

from collision.assets.robot_urdf import prepare_fcl_robot_urdf


class FclRobotUrdfTests(unittest.TestCase):
    def test_preserves_authored_collision_and_copies_missing_primitive(self):
        with tempfile.TemporaryDirectory(prefix='fcl-urdf-') as temp:
            source = Path(temp) / 'arm.urdf'
            source.write_text('''<robot name="test">
              <link name="authored">
                <visual><geometry><sphere radius="0.2"/></geometry></visual>
                <collision><origin xyz="0 0 0.1"/><geometry><box size="1 2 3"/></geometry></collision>
              </link>
              <link name="fallback">
                <visual><origin xyz="1 2 3" rpy="0.1 0.2 0.3"/>
                  <geometry><cylinder radius="0.04" length="0.5"/></geometry></visual>
              </link>
              <link name="frame"/>
              <joint name="frame_mount" type="fixed"><parent link="authored"/>
                <child link="frame"/></joint>
            </robot>''', encoding='utf-8')

            prepared = prepare_fcl_robot_urdf(source, cache_root=Path(temp) / 'cache')
            root = ET.parse(prepared).getroot()
            authored = root.find("link[@name='authored']")
            fallback = root.find("link[@name='fallback']")
            self.assertNotEqual(prepared, source)
            self.assertEqual(authored.find('collision/geometry/box').attrib,
                             {'size': '1 2 3'})
            self.assertIsNone(authored.find('collision/geometry/sphere'))
            self.assertEqual(len(fallback.findall('collision')), 1)
            self.assertEqual(fallback.find('collision/origin').attrib,
                             {'xyz': '1 2 3', 'rpy': '0.1 0.2 0.3'})
            self.assertEqual(fallback.find('collision/geometry/cylinder').attrib,
                             {'radius': '0.04', 'length': '0.5'})
            self.assertEqual(len(root.find("link[@name='frame']").findall('collision')), 0)

    def test_mesh_fallback_is_convex_preprocessed_and_content_cached(self):
        with tempfile.TemporaryDirectory(prefix='fcl-urdf-mesh-') as temp:
            root_path = Path(temp)
            mesh_path = root_path / 'visual.obj'
            trimesh.creation.box(extents=[0.2, 0.3, 0.4]).export(mesh_path)
            source = root_path / 'arm.urdf'
            source.write_text('''<robot name="mesh_test"><link name="arm">
              <visual><origin xyz="0.01 0 0"/><geometry>
                <mesh filename="visual.obj" scale="2 1 1"/>
              </geometry></visual></link></robot>''', encoding='utf-8')
            cache = root_path / 'cache'

            first = prepare_fcl_robot_urdf(source, cache_root=cache)
            second = prepare_fcl_robot_urdf(source, cache_root=cache)

            self.assertEqual(first, second)
            mesh = ET.parse(first).getroot().find('link/collision/geometry/mesh')
            self.assertTrue(Path(mesh.get('filename')).is_file())
            self.assertIsNone(mesh.get('scale'))
            generated = trimesh.load(mesh.get('filename'), force='mesh', process=False)
            np.testing.assert_allclose(generated.extents, [0.4, 0.3, 0.4], atol=1e-6)

    def test_inertial_link_without_visual_or_collision_fails(self):
        with tempfile.TemporaryDirectory(prefix='fcl-urdf-invalid-') as temp:
            source = Path(temp) / 'arm.urdf'
            source.write_text('''<robot name="invalid"><link name="link">
              <inertial><mass value="1"/></inertial></link></robot>''', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'no collision or visual geometry'):
                prepare_fcl_robot_urdf(source, cache_root=Path(temp) / 'cache')

    def test_moving_link_without_any_geometry_fails(self):
        with tempfile.TemporaryDirectory(prefix='fcl-urdf-empty-moving-') as temp:
            source = Path(temp) / 'arm.urdf'
            source.write_text('''<robot name="invalid">
              <link name="base"><collision><geometry><sphere radius="0.1"/></geometry></collision></link>
              <link name="moving"/>
              <joint name="joint" type="revolute"><parent link="base"/>
                <child link="moving"/></joint></robot>''', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'moving has no collision or visual geometry'):
                prepare_fcl_robot_urdf(source, cache_root=Path(temp) / 'cache')

    def test_disconnected_empty_link_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory(prefix='fcl-urdf-empty-') as temp:
            source = Path(temp) / 'arm.urdf'
            source.write_text('''<robot name="invalid"><link name="isolated"/></robot>''',
                              encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'isolated has no collision or visual geometry'):
                prepare_fcl_robot_urdf(source, cache_root=Path(temp) / 'cache')


if __name__ == '__main__':
    unittest.main()
