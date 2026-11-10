import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mujoco
import numpy as np

from demo.interaction.table_clearance import (table_floor, gripper_clearance,
                                             validate_grasp_geometry)


class TableClearanceTests(unittest.TestCase):
    def test_pick_allows_only_gripper_selected_object_contacts(self):
        from collections import OrderedDict
        from collision.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
        from demo.collision.grasp_collision import GraspCollisionChecker
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <body name="arm"><joint name="slide" type="slide" axis="0 0 1"/>
            <geom size=".02" pos="0 0 .6"/>
            <body name="robotiq_2f85_right_pad" pos="0 0 1"><geom size=".02"/></body>
            <body name="robotiq_2f85_left_follower" pos=".01 0 1"><geom size=".02"/></body>
          </body>
          <body name="Apple" pos="0 0 1"><geom size=".02"/></body>
          <body name="Banana" pos="0 0 3"><geom size=".02"/></body>
          <body name="WorkTable" pos="0 0 4"><geom size=".02"/></body>
          </worldbody></mujoco>''')
        data = mujoco.MjData(model)
        mujoco.mj_kinematics(model, data)
        checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        checker.backend = SimpleNamespace(model=model, data=data, qpos_indices=np.array([0]),
            gripper_controller=SimpleNamespace(held_name=None), contact_target='Apple',
            grasp_motion_phase=True)
        checker._policy_held = None
        checker._query_cache = OrderedDict()
        checker.probe = mujoco.MjData(model)
        checker._names = {}
        checker.tool_geoms = {1, 2}
        checker.geoms = [0, 1, 2, 3, 4, 5]
        checker.robot_bodies = {1, 2, 3}
        checker.dynamic_names = {'Apple', 'Banana'}
        checker.body_names = ['world', 'arm', 'robotiq_2f85_right_pad',
                              'robotiq_2f85_left_follower', 'Apple', 'Banana', 'WorkTable']
        checker.adjacent = checker.excluded = checker.support_geoms = set()
        checker.allow_gripper_environment_contact = False
        with patch.object(SimPlatformFCLCollisionChecker, 'check',
                          return_value=CollisionResult(False, float('inf'), ())):
            # Ignore internal contacts between the two deliberately overlapping tool parts.
            checker.excluded = {frozenset((2, 3))}
            self.assertFalse(checker.check(np.array([0.])).in_collision)
            checker.backend.contact_target = None
            collision = checker.check(np.array([0.]))
            self.assertTrue(collision.in_collision)
            self.assertEqual(collision.contacts[0]['geom2'], 'Apple')
            checker.backend.contact_target = 'Apple'
            self.assertFalse(checker.check(np.array([0.])).in_collision)
            # Both other-object and table collisions remain forbidden.
            for name in ('Banana', 'WorkTable'):
                body = checker.body_names.index(name)
                original = model.body_pos[body].copy()
                model.body_pos[body] = [0., 0., 1.]
                checker._query_cache.clear()
                collision = checker.check(np.array([0.]))
                self.assertTrue(collision.in_collision)
                self.assertTrue(any(name in (c['geom1'], c['geom2']) for c in collision.contacts))
                model.body_pos[body] = original
            # Gripper exemption never exempts the arm from hitting the target.
            model.body_pos[4] = [0., 0., .6]
            checker._query_cache.clear()
            collision = checker.check(np.array([0.]))
            self.assertTrue(collision.in_collision)
            self.assertTrue(any({'arm', 'Apple'} == {c['geom1'], c['geom2']}
                                for c in collision.contacts))
        self.assertEqual(data.time, 0.)

    def test_floor_fallback_preserves_xy_orientation_and_tool_calibration_without_simulating(self):
        from demo.interaction.grasp_depth_search import floor_fallback_candidates
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <body name="WorkTable" pos="0 0 .5"><geom type="box" size="1 1 .1"/></body>
          <body name="arm" pos="0 0 1"><joint name="slide" type="slide" axis="0 0 1"/>
            <geom size=".01" contype="0" conaffinity="0"/><site name="tcp"/>
            <body name="robotiq_2f85_right_driver">
              <joint name="robotiq_2f85_right_driver_joint" axis="0 1 0"/>
              <geom name="finger" type="box" pos=".1 0 0" size=".01 .01 .01"/>
            </body>
          </body></worldbody></mujoco>''')
        data = mujoco.MjData(model)
        mujoco.mj_kinematics(model, data)
        runtime = SimpleNamespace(model=model, data=data, qpos_indices=np.array([0]))
        finger = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, 'finger')
        checker = SimpleNamespace(backend=runtime, tool_geoms={finger})
        calibration = np.eye(4)
        calibration[2, 3] = .1
        sources = []
        for rotation in (np.diag([1., -1., -1.]), np.diag([-1., 1., -1.])):
            grasp = np.eye(4)
            grasp[:3, :3] = rotation
            grasp[:3, 3] = [.15, -.12, .6]
            sources.append({'grasp_depth_offset_m': 0., 'world_from_grasp': grasp.tolist(),
                            'world_from_control': (grasp @ np.linalg.inv(calibration)).tolist()})
        before = data.qpos.copy()
        with (patch('mujoco.mj_forward', side_effect=AssertionError('No dynamics')),
              patch('mujoco.mj_step', side_effect=AssertionError('No simulation'))):
            results = floor_fallback_candidates({'candidates': sources}, checker, np.array([.2]))
        self.assertEqual(len(results), 2)
        for source, result in zip(sources, results):
            control, grasp = (np.asarray(result[key]) for key in ('world_from_control', 'world_from_grasp'))
            np.testing.assert_allclose(grasp[:2, 3], [.15, -.12])
            np.testing.assert_allclose(grasp[:3, :3], np.asarray(source['world_from_grasp'])[:3, :3])
            np.testing.assert_allclose(control @ calibration, grasp)
            self.assertAlmostEqual(result['floor_fallback']['table_height_m'], .6)
            self.assertAlmostEqual(result['floor_fallback']['minimum_open_clearance_m'], .003)
            self.assertAlmostEqual(result['floor_fallback']['table_floor_z_m'], .617)
            self.assertAlmostEqual(result['floor_fallback']['table_floor_offset_m'], .017)
            self.assertAlmostEqual(result['floor_fallback']['minimum_table_clearance_m'], .020)
            # Independently transform all eight corners of the pre-closure open box.
            from itertools import product
            angle = .8 * (1. - .08 / .085)
            rotation = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                                 [-np.sin(angle), 0, np.cos(angle)]])
            corners = np.array(list(product((-.01, .01), repeat=3))) + [.1, 0, 0]
            world = (corners @ rotation.T) @ control[:3, :3].T + control[:3, 3]
            self.assertAlmostEqual(world[:, 2].min(), .620)
            self.assertAlmostEqual(result['grasp_depth_offset_m'], .6 - grasp[2, 3])
        np.testing.assert_array_equal(data.qpos, before)
        self.assertEqual(data.time, 0.)

    def test_collision_probe_checks_closed_fingers_without_changing_live_or_cached_open_pose(self):
        from collections import OrderedDict
        from collision.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
        from demo.collision.grasp_collision import GraspCollisionChecker
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <geom name="table" type="box" pos="0 0 .85" size="1 1 .1"/>
          <body name="arm" pos="0 0 1"><joint name="lift" type="slide" axis="0 0 1"/>
            <geom size=".01" contype="0" conaffinity="0"/>
            <body name="robotiq_2f85_right_driver">
              <joint name="robotiq_2f85_right_driver_joint" axis="0 1 0"/>
              <geom name="pad" pos=".1 0 0" size=".01"/>
            </body>
            <body name="robotiq_2f85_left_pad" pos=".1 0 0">
              <geom name="opposite_pad" size=".01"/>
            </body>
          </body></worldbody></mujoco>''')
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        checker.backend = SimpleNamespace(model=model, data=data, qpos_indices=np.array([0]),
            gripper_controller=SimpleNamespace(held_name=None), contact_target=None)
        checker._policy_held = None
        checker._query_cache = OrderedDict()
        checker.probe = mujoco.MjData(model)
        checker._names = {}
        checker.tool_geoms = {2, 3}
        checker.geoms = [0, 2, 3]
        checker.robot_bodies = {1, 2, 3}
        checker.dynamic_names = set()
        checker.body_names = ['world', 'arm', 'robotiq_2f85_right_driver', 'robotiq_2f85_left_pad']
        checker.adjacent = checker.excluded = checker.support_geoms = set()
        checker.allow_gripper_environment_contact = False
        before = data.qpos.copy()
        with (patch.object(SimPlatformFCLCollisionChecker, 'check',
                           return_value=CollisionResult(False, float('inf'), ())),
              patch('mujoco.mj_forward', side_effect=AssertionError('No contact constraint assembly')),
              patch('mujoco.mj_step', side_effect=AssertionError('No physics steps'))):
            # Actual/current-shape checks still detect internal collisions.
            self.assertTrue(checker.check(np.array([0.])).in_collision)
            self.assertFalse(checker.check(np.array([0.]), gripper_width=.085).in_collision)
            # The static prefilter must still reject environment collisions.
            closed = checker.check(np.array([0.]), gripper_width=0.)
            self.assertTrue(closed.in_collision)
            self.assertTrue(all('world' in (c['geom1'], c['geom2']) for c in closed.contacts))
            checker._query_cache.clear()
            # Rebuild the actual-shape pair list after a static check.
            self.assertTrue(checker.check(np.array([0.])).in_collision)
        np.testing.assert_array_equal(data.qpos, before)
        self.assertEqual(data.time, 0.)

    def test_static_open_geometry_uses_mimics_and_never_checks_closed_shape(self):
        from unittest.mock import Mock
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <body name="arm" pos="0 0 1"><joint name="arm_slide" type="slide" axis="0 0 1"/>
            <geom size=".01"/>
            <body name="robotiq_2f85_right_driver" pos="0 0 0">
              <joint name="robotiq_2f85_right_driver_joint" axis="0 1 0"/>
              <geom type="box" pos=".1 0 0" size=".01 .01 .01"/>
            </body>
            <body name="robotiq_2f85_left_follower">
              <joint name="follower" axis="0 1 0"/>
              <geom type="box" pos="-.1 0 0" size=".01 .01 .01"/>
            </body>
          </body></worldbody><equality><joint joint1="follower"
            joint2="robotiq_2f85_right_driver_joint" polycoef="0 -1 0 0 0"/>
          </equality></mujoco>''')
        runtime = SimpleNamespace(model=model, data=mujoco.MjData(model), qpos_indices=np.array([0]))
        checker = SimpleNamespace(backend=runtime, check=Mock(return_value=SimpleNamespace(
            in_collision=False, contacts=())))
        before = runtime.data.qpos.copy()
        with patch('mujoco.mj_step', side_effect=AssertionError('No physics steps')):
            rejected = validate_grasp_geometry(checker, np.array([0.]), .99)
            self.assertFalse(rejected['success'])
            checker.check.assert_not_called()
            lowest = rejected['table_clearance']['gripper_lowest_z_m']
            raised_rejected = validate_grasp_geometry(checker, np.array([0.]), lowest - .004)
            self.assertFalse(raised_rejected['success'])
            self.assertEqual(raised_rejected['reason'], 'grasp_below_table_floor')
            self.assertAlmostEqual(raised_rejected['table_clearance']['clearance_m'], -.013)
            self.assertAlmostEqual(raised_rejected['table_clearance']['table_floor_offset_m'], .017)
            checker.check.assert_not_called()
            # At this same arm pose a closed finger would dip below .95 m,
            # but the pre-closure open geometry is above it and must pass.
            open_only = validate_grasp_geometry(checker, np.array([0.]), .95)
            self.assertTrue(open_only['success'])
            self.assertEqual(open_only['table_clearance']['gripper_configuration'], 'preclosure_open')
            checker.check.assert_called_once_with(np.array([0.]), safety_distance=.003, gripper_width=.08)
            checker.check.reset_mock()
            accepted = validate_grasp_geometry(checker, np.array([.2]), .95)
            self.assertTrue(accepted['success'])
            self.assertEqual(accepted['validation_mode'], 'static_open_geometry')
            checker.check.assert_called_once_with(np.array([.2]), safety_distance=.003, gripper_width=.08)
            self.assertAlmostEqual(checker._static_grasp_probe.qpos[1], .8 * (1. - .08 / .085))
            self.assertAlmostEqual(checker._static_grasp_probe.qpos[2], -.8 * (1. - .08 / .085))
            checker.check.return_value = SimpleNamespace(in_collision=True, contacts=({'geom1': 'pad'},))
            self.assertEqual(validate_grasp_geometry(checker, np.array([.2]), .95)['reason'],
                             'grasp_geometry_collision')
        np.testing.assert_array_equal(runtime.data.qpos, before)
        self.assertEqual(runtime.data.time, 0.)

    def runtime(self):
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <body name="WorkTable" pos="0 0 .5">
            <geom name="collision_top" type="box" size="1 1 .098"/>
            <geom name="visible_top" type="box" size="1 1 .1" contype="0" conaffinity="0"/>
          </body>
          <body name="other_table" pos="4 0 1"><geom type="box" size="1 1 .1"/></body>
          <body name="robotiq_2f85_base" pos="0 0 .8"><joint name="lift" type="slide"/>
            <geom size=".01" mass="1"/>
            <body name="robotiq_2f85_left_pad" pos="0 0 -.1">
              <geom type="box" size=".01 .01 .02"/>
            </body>
          </body>
          </worldbody><actuator><position joint="lift"/></actuator></mujoco>''')
        class Runtime:
            def set_frame_callback(self, callback):
                self.frame_callback = callback
            def get_joint_positions(self):
                return self.data.qpos.copy()
        runtime = Runtime()
        runtime.model, runtime.data = model, mujoco.MjData(model)
        runtime.qpos_indices = runtime.dof_indices = runtime.actuator_ids = np.array([0])
        mujoco.mj_forward(model, runtime.data)
        return runtime

    def test_table_is_local_to_target_and_all_gripper_links_are_checked(self):
        runtime = self.runtime()
        floor = table_floor(runtime, [0., 0., .6])
        self.assertAlmostEqual(floor, .6)
        evidence = gripper_clearance(runtime, floor)
        self.assertTrue(evidence['success'])
        self.assertAlmostEqual(evidence['gripper_lowest_z_m'], .68)
        self.assertEqual(evidence['lowest_link'], 'robotiq_2f85_left_pad')
        self.assertAlmostEqual(table_floor(runtime, [4., 0., 1.2]), 1.1)
        with self.assertRaisesRegex(ValueError, '无法确定抓取高度底限'):
            table_floor(runtime, [8., 0., .6])

    def test_supplied_geometry_penetration_and_exact_touch_fail(self):
        runtime = self.runtime()
        before = runtime.data.qpos.copy()
        for floor, ok in ((.679, True), (.68, False), (.681, False)):
            with self.subTest(floor=floor):
                result = gripper_clearance(runtime, floor)
                self.assertEqual(result['success'], ok)
                self.assertAlmostEqual(result['clearance_m'], .68-floor)
        np.testing.assert_array_equal(before, runtime.data.qpos)


if __name__ == '__main__':
    unittest.main()
