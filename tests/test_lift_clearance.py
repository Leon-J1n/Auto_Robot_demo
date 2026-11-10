import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import mujoco

from atomic_skill.runner import TaskSkillExecutor
from demo.collision.grasp_collision import GraspCollisionChecker


class LiftClearanceTests(unittest.TestCase):
    def test_real_geometry_allows_departure_but_rejects_approach_and_penetration(self):
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <geom name="table" type="box" size="1 1 .1"/>
          <body name="finger" pos="0 0 .2002">
            <geom name="pad" type="box" size=".01 .01 .1"/>
          </body></worldbody></mujoco>''')
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        checker.backend = SimpleNamespace(model=model, data=data,
            gripper_controller=SimpleNamespace(held_name='Banana'))
        checker.tool_geoms = {1}
        checker.geoms = [0, 1]
        checker.robot_bodies = {1}
        checker.body_names = ['world', 'finger']
        checker.dynamic_names = set()
        checker.capture_lift_clearance(.003)
        self.assertEqual(len(checker._lift_clearance_pairs), 1)
        def gap(z):
            model.body_pos[1, 2] = z
            mujoco.mj_forward(model, data)
            return mujoco.mj_geomDistance(model, data, 1, 0, .01, None)
        self.assertTrue(checker._leaves_lift_clearance(1, 0, gap(.2002)))
        self.assertTrue(checker._leaves_lift_clearance(1, 0, gap(.202)))
        self.assertFalse(checker._leaves_lift_clearance(1, 0, gap(.2001)))
        self.assertFalse(checker._leaves_lift_clearance(1, 0, gap(.199)))
        self.assertFalse(checker._leaves_lift_clearance(1, 2, .001))
        checker.clear_lift_clearance()
        self.assertFalse(checker._leaves_lift_clearance(1, 0, gap(.202)))

    def test_allowance_is_scoped_to_lift_even_when_execution_fails(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.resources = SimpleNamespace(scene={'move_defaults': {
            'constraints': {'safety_distance': .003}}})
        executor.checker = SimpleNamespace(capture_lift_clearance=Mock(), clear_lift_clearance=Mock())
        executor._execute_command = Mock(side_effect=RuntimeError('planning failed'))
        with self.assertRaisesRegex(RuntimeError, 'planning failed'):
            executor.execute({'skill_name': 'lift_up'}, 1, {}, {})
        executor.checker.capture_lift_clearance.assert_called_once_with(.003)
        executor.checker.clear_lift_clearance.assert_called_once()

    def test_payload_penetration_can_leave_but_not_deepen(self):
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <geom name="table" type="box" size="1 1 .1"/>
          <body name="Banana" pos="0 0 .107977"><freejoint/>
            <geom name="banana" type="sphere" size=".01" mass=".1"/>
            <body name="Banana_link"><geom name="banana_link" type="sphere"
              pos=".03 0 0" size=".01" mass=".1"/></body>
          </body>
          <body name="finger" pos="0 0 .3"><joint name="lift" type="slide" axis="0 0 1"/>
            <geom name="pad" type="box" size=".01 .01 .01" mass=".1"/></body>
          <body name="neighbor" pos=".5 0 .2"><geom name="neighbor_geom" size=".01"/></body>
          </worldbody></mujoco>''')
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        banana_body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'Banana')
        tool_body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'finger')
        table, banana, child, pad, neighbor = [mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, name)
            for name in ('table', 'banana', 'banana_link', 'pad', 'neighbor_geom')]
        checker.backend = SimpleNamespace(model=model, data=data,
            object_ids={'Banana': banana_body}, gripper_controller=SimpleNamespace(held_name='Banana'))
        checker.tool_geoms = {pad}
        checker.geoms = list(range(model.ngeom))
        checker.robot_bodies = {tool_body}
        checker.capture_lift_clearance(.003)
        initial = checker._lift_clearance_pairs[frozenset((banana, table))]
        self.assertAlmostEqual(initial, -.002023, places=8)
        self.assertIn(frozenset((child, table)), checker._lift_clearance_pairs)
        for depth in (initial, -.001, 0., .001, .004):
            self.assertTrue(checker._leaves_lift_clearance(banana, table, depth))
        self.assertFalse(checker._leaves_lift_clearance(banana, table, -.0021))
        self.assertFalse(checker._leaves_lift_clearance(banana, neighbor, -.001))
        # Geometry-query order cannot loosen or mutate the captured baseline.
        self.assertEqual(checker._lift_clearance_pairs[frozenset((banana, table))], initial)
        # Exercise the complete collision probe, including forecasting the held
        # payload at a candidate arm state, without loading the FCL service.
        from collections import OrderedDict
        from unittest.mock import patch
        import numpy as np
        from collision.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
        checker.backend.qpos_indices = np.array([int(model.jnt_qposadr[
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, 'lift')])])
        controller = checker.backend.gripper_controller
        controller.base_id, controller.mode = tool_body, 'physical'
        relative = np.eye(4)
        relative[:3, 3] = data.xpos[banana_body] - data.xpos[tool_body]
        controller._relative = lambda body: relative
        checker.body_names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i) or 'world'
                              for i in range(model.nbody)]
        checker.dynamic_names = {'Banana', 'Banana_link'}
        checker.probe = mujoco.MjData(model)
        checker._query_cache = OrderedDict()
        checker._policy_held = 'Banana'
        checker.allow_gripper_environment_contact = False
        checker.support_geoms = set()
        checker.adjacent = checker.excluded = set()
        with (patch.object(SimPlatformFCLCollisionChecker, 'check',
                           return_value=CollisionResult(False, float('inf'), ())),
              patch('mujoco.mj_forward', side_effect=AssertionError('Geometry probes need no dynamics'))):
            self.assertFalse(checker.check(np.array([0.]), safety_distance=.003).in_collision)
            self.assertFalse(checker.check(np.array([.001]), safety_distance=.003).in_collision)
            deeper = checker.check(np.array([-.0002]), safety_distance=.003)
            self.assertTrue(deeper.in_collision)
            self.assertTrue(any(c['geom1'].startswith('Banana') or c['geom2'].startswith('Banana')
                                for c in deeper.contacts))
        checker.clear_lift_clearance()
        self.assertFalse(checker._leaves_lift_clearance(banana, table, initial))
        with patch.object(SimPlatformFCLCollisionChecker, 'check',
                          return_value=CollisionResult(False, float('inf'), ())):
            self.assertTrue(checker.check(np.array([0.]), safety_distance=.003).in_collision)
            with checker.lift_payload_collision_scope():
                self.assertEqual(checker._lift_ignored_payload_geoms, frozenset((banana, child)))
                # Includes deeper penetration, newly encountered obstacles,
                # and the payload's child links. Cached outside results differ.
                self.assertFalse(checker.check(np.array([0.]), safety_distance=.003).in_collision)
                self.assertFalse(checker.check(np.array([-.03]), safety_distance=.003).in_collision)
                neighbor_body = int(model.geom_bodyid[neighbor])
                model.body_pos[neighbor_body] = [0., 0., .08]
                self.assertFalse(checker.check(np.array([-.0301]), safety_distance=.003).in_collision)
                # Finger/environment collisions remain forbidden during lift.
                blocked = checker.check(np.array([-.2]), safety_distance=.003)
                self.assertTrue(blocked.in_collision)
                self.assertTrue(any('finger' in (c['geom1'], c['geom2']) for c in blocked.contacts))
            self.assertEqual(checker._lift_ignored_payload_geoms, frozenset())
            self.assertTrue(checker.check(np.array([0.]), safety_distance=.003).in_collision)

        checker._names = {1: 'Banana_link', 2: 'finger', 3: 'neighbor'}
        with checker.lift_payload_collision_scope():
            self.assertTrue(checker._is_allowed(SimpleNamespace(robot_collider=1, other=2), None))
            with patch.object(SimPlatformFCLCollisionChecker, 'check', return_value=CollisionResult(
                    True, -.01, ({'geom1': 'Banana_link', 'geom2': 'neighbor', 'distance': -.01},))):
                self.assertFalse(checker.check(np.array([.0001])).in_collision)

    def test_payload_exemption_is_scoped_even_on_failure_and_only_applies_to_lift(self):
        from contextlib import contextmanager
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.backend = SimpleNamespace(gripper_controller=None)
        active = []
        @contextmanager
        def scope():
            active.append('payload')
            try:
                yield
            finally:
                active.pop()
        executor.checker = SimpleNamespace(lift_payload_collision_scope=scope)
        def fail(*args):
            self.assertEqual(active, ['payload'])
            raise RuntimeError('execution failed')
        executor._execute_command = fail
        with self.assertRaisesRegex(RuntimeError, 'execution failed'):
            executor.execute({'skill_name': 'lift_up'}, 1, {}, {})
        self.assertEqual(active, [])
        def other(*args):
            self.assertEqual(active, [])
            return 'ok'
        executor._execute_command = other
        for skill in ('move_to', 'retreat', 'pick', 'put'):
            self.assertEqual(executor.execute({'skill_name': skill}, 1, {}, {}), 'ok')

    def test_no_held_object_does_not_exempt_anything(self):
        model = mujoco.MjModel.from_xml_string('<mujoco><worldbody><geom size=".1"/></worldbody></mujoco>')
        checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        checker.backend = SimpleNamespace(model=model, gripper_controller=SimpleNamespace(held_name=None))
        checker.geoms, checker.body_names = [0], ['world']
        with checker.lift_payload_collision_scope():
            self.assertEqual(checker._lift_ignored_payload_geoms, frozenset())
            self.assertEqual(checker._lift_ignored_payload_names, frozenset())

    def test_other_commands_do_not_capture_lift_allowance(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.backend = SimpleNamespace(gripper_controller=None)
        executor.checker = SimpleNamespace(capture_lift_clearance=Mock(), clear_lift_clearance=Mock())
        executor._execute_command = Mock(return_value='ok')
        self.assertEqual(executor.execute({'skill_name': 'pick'}, 1, {}, {}), 'ok')
        executor.checker.capture_lift_clearance.assert_not_called()
        executor.checker.clear_lift_clearance.assert_not_called()


if __name__ == '__main__':
    unittest.main()
