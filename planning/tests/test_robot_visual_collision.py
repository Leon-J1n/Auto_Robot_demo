"""Visual-derived robot geometry preserves transforms and cache identity."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
import trimesh

from collision.assets.robot_visual import prepare_robot_visual_collisions
from collision.assets.mesh import bound_convex_parts


class RobotVisualCollisionTests(unittest.TestCase):
    def test_link_override_is_applied_only_to_named_link(self):
        from collision.assets.mesh import prepare_mesh
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'robot.urdf'
            source.write_text('<robot name="test">' + ''.join(
                f'<link name="{name}"><visual><geometry><box size=".1 .1 .1"/>'
                '</geometry></visual></link>' for name in ('arm', 'wrist')) + '</robot>')
            options = dict(link_overrides=dict(wrist=dict(max_vertices=128, max_convex_hulls=24)))
            with patch('collision.assets.robot_visual.prepare_mesh', wraps=prepare_mesh) as prepare:
                prepare_robot_visual_collisions(source, options, cache_root=Path(tmp)/'cache')
            self.assertEqual(prepare.call_args_list[0].kwargs['settings']['max_ch_vertex'], 64)
            self.assertEqual(prepare.call_args_list[1].kwargs['settings']['max_ch_vertex'], 128)
            self.assertEqual(prepare.call_args_list[1].kwargs['settings']['max_convex_hull'], 24)
            for overrides in ({'unknown': {}}, {'wrist': {'max_vertices': 0}}, {'wrist': {'exclude': True}}):
                with self.assertRaises(ValueError):
                    prepare_robot_visual_collisions(source, dict(link_overrides=overrides))

    def test_budget_merge_covers_every_original_vertex(self):
        from scipy.spatial import ConvexHull
        hulls = [trimesh.creation.icosphere(subdivisions=1, radius=.02) for _ in range(5)]
        for index, hull in enumerate(hulls):
            hull.apply_translation([index * .03, 0., 0.])
        points = np.vstack([h.vertices for h in hulls])
        result = bound_convex_parts(hulls, max_parts=2, max_vertices=8)
        self.assertEqual(len(result), 2)
        covered = np.zeros(len(points), dtype=bool)
        for hull in result:
            self.assertLessEqual(len(hull.vertices), 8)
            self.assertTrue(hull.is_volume)
            equations = ConvexHull(hull.vertices).equations
            covered |= np.all(points @ equations[:, :3].T + equations[:, 3] <= 1.e-8, axis=1)
        self.assertTrue(covered.all())

    def test_link_merge_scale_origin_cache_and_source_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            mesh = directory / 'mesh.obj'
            trimesh.creation.box(extents=[1, 2, 3]).export(mesh)
            source = directory / 'robot.urdf'
            source.write_text('''<robot name="test"><link name="arm">
                <visual><origin xyz="1 0 0" rpy="0 0 1.5707963267948966"/>
                  <geometry><mesh filename="mesh.obj" scale="2 1 1"/></geometry></visual>
                <collision><geometry><sphere radius="9"/></geometry></collision>
                </link><link name="empty"/></robot>''')
            original = source.read_bytes()
            generated = prepare_robot_visual_collisions(source, cache_root=directory / 'cache')
            root = ET.parse(generated).getroot()
            collider = root.find('link/collision/geometry/mesh')
            self.assertIsNotNone(collider)
            result = trimesh.load(collider.get('filename'), force='mesh')
            np.testing.assert_allclose(result.bounds, [[0, -1, -1.5], [2, 1, 1.5]], atol=1.e-7)
            self.assertEqual(source.read_bytes(), original)
            with patch('collision.assets.robot_visual.prepare_mesh', side_effect=AssertionError('Cache miss')):
                self.assertEqual(prepare_robot_visual_collisions(source, cache_root=directory / 'cache'), generated)
            collider_path = Path(collider.get('filename'))
            original_part = collider_path.read_bytes()
            collider_path.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'Invalid robot collision cache'):
                prepare_robot_visual_collisions(source, cache_root=directory / 'cache')
            collider_path.write_bytes(original_part)
            changed = prepare_robot_visual_collisions(source, {'max_vertices': 32}, cache_root=directory / 'cache')
            self.assertNotEqual(changed, generated)
            trimesh.creation.box(extents=[2, 2, 3]).export(mesh)
            self.assertNotEqual(prepare_robot_visual_collisions(source, cache_root=directory / 'cache'), generated)

    def test_explicit_authored_mode_and_link_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'robot.urdf'
            source.write_text('<robot name="test"><link name="arm"><visual><geometry>'
                              '<box size="1 1 1"/></geometry></visual><collision><geometry>'
                              '<sphere radius="1"/></geometry></collision></link></robot>')
            generated = prepare_robot_visual_collisions(source, {'mode': 'authored'}, cache_root=Path(tmp)/'cache')
            self.assertIsNotNone(ET.parse(generated).getroot().find('link/collision/geometry/mesh'))
            result = prepare_robot_visual_collisions(source, {'exclude_links': ['arm']}, cache_root=Path(tmp)/'cache')
            self.assertIsNone(ET.parse(result).getroot().find('link/collision'))
            with self.assertRaisesRegex(ValueError, 'Unknown'):
                prepare_robot_visual_collisions(source, {'exclude_links': ['typo']})

    def test_mujoco_import_uses_convex_parts(self):
        import mujoco
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'robot.urdf'
            source.write_text('<robot name="test"><link name="arm"><visual><geometry>'
                              '<box size=".1 .2 .3"/></geometry></visual></link></robot>')
            result = prepare_robot_visual_collisions(source, cache_root=Path(tmp)/'cache')
            model = mujoco.MjModel.from_xml_path(str(result))
            self.assertGreater(model.ngeom, 0)
            self.assertEqual(model.nmesh, 1)

    def test_welded_base_neighbour_is_filtered_but_environment_remains(self):
        import mujoco
        from execution.backends.mujoco.scene_compiler import exclude_arm_neighbours
        with tempfile.TemporaryDirectory() as tmp:
            urdf = Path(tmp) / 'robot.urdf'
            urdf.write_text('<robot><joint name="j" type="revolute">'
                            '<parent link="base"/><child link="shoulder"/></joint></robot>')
            root = ET.fromstring('''<mujoco><worldbody><body name="base">
                <geom type="sphere" size=".1"/><body name="shoulder">
                <joint type="hinge"/><geom type="sphere" size=".1"/>
                </body></body><body name="obstacle" pos=".15 0 0">
                <geom type="sphere" size=".1"/></body></worldbody></mujoco>''')
            def contacts():
                model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding='unicode'))
                data = mujoco.MjData(model)
                mujoco.mj_forward(model, data)
                return {frozenset(mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY,
                    int(model.geom_bodyid[g])) for g in (c.geom1, c.geom2)) for c in data.contact}
            self.assertIn(frozenset(('base', 'shoulder')), contacts())
            exclude_arm_neighbours(root, SimpleNamespace(robot_urdf=urdf, robot_object={}))
            self.assertNotIn(frozenset(('base', 'shoulder')), contacts())
            self.assertIn(frozenset(('shoulder', 'obstacle')), contacts())


if __name__ == '__main__':
    unittest.main()
