"""Contact-policy regression tests using the existing articulated pair builder."""
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mujoco
import numpy as np

from collision.backends.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
from collision.planning_policy import freeze_policy, pair_limit, planning_signature
from demo.collision.grasp_collision import GraspCollisionChecker
from libraries.curobo.conversion.runtime import convert_runtime
from libraries.curobo.conversion.urdf import URDFModel
from libraries.curobo.policy import compile_policy, world_limits, IGNORE_DISTANCE
from libraries.curobo.runtime_policy import export_runtime


@contextmanager
def scene():
    model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
      <body name="base" pos="0 0 1"><geom name="base_geom" type="sphere" size=".03"/>
        <body name="arm" pos=".1 0 .1"><joint name="joint" pos=".02 0 0" axis="0 0 1" ref="15" range="-180 180"/>
          <geom name="arm_geom" type="sphere" size=".03"/>
          <body name="robotiq_2f85_base" pos=".2 0 0"><geom name="tool_geom" type="sphere" size=".02"/>
            <site name="tcp" pos="0 0 .04"/>
          </body>
        </body>
      </body>
      <body name="payload" pos=".3 0 1.1"><freejoint/><geom name="payload_geom" type="box" size=".02 .02 .02" mass=".1"/></body>
      <body name="table" pos="0 0 .5"><geom name="table_geom" type="box" size="1 1 .1"/></body>
      <body name="target" pos="1 0 .5"><geom name="target_geom" type="box" size=".1 .1 .1"/></body>
      <body name="RobotPedestal" pos="0 0 .8"><geom name="pedestal_geom" type="box" size=".05 .05 .1"/>
        <body name="pedestal_child"><geom name="pedestal_child_geom" type="sphere" size=".01"/></body>
      </body>
    </worldbody></mujoco>''')
    data = mujoco.MjData(model)
    data.qpos[0] = .4
    mujoco.mj_forward(model, data)
    body = lambda name: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
    geom = lambda name: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, name + '_geom')
    base, arm, tool, payload = [body(n) for n in ('base', 'arm', 'robotiq_2f85_base', 'payload')]
    controller = SimpleNamespace(held_name=None, base_id=tool, mode='attached', relative_pose=np.eye(4))
    runtime = SimpleNamespace(model=model, data=data, base_id=base, joint_ids=[0], qpos_indices=[0],
        joint_names=['joint'], joint_limits=np.array([[-np.pi, np.pi]]), gripper_controller=controller,
        contact_target=None, contact_target_object_id=None, resources=SimpleNamespace(scene={'objects': []}),
        object_ids={'payload': payload, 'target': body('target'), 'RobotPedestal': body('RobotPedestal')},
        get_joint_positions=lambda: data.qpos[:1].copy())
    checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
    checker.backend = runtime
    checker.probe = mujoco.MjData(model)
    checker.geoms = list(range(model.ngeom))
    checker.geom_names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i) for i in checker.geoms]
    checker.body_names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i) or 'world' for i in range(model.nbody)]
    checker.dynamic_names = {'payload'}
    checker.tool_geoms = {geom('tool')}
    checker.robot_bodies = {base, arm, tool}
    checker.adjacent = {frozenset((base, arm)), frozenset((arm, tool))}
    checker.excluded = set()
    checker.support_geoms = set()
    checker.allow_gripper_environment_contact = False
    checker.tcp_site_id = 0
    checker._query_cache = OrderedDict()
    checker._policy_held = None
    checker._arm_policy = set()
    checker._names = {}
    checker._collider_links = {}
    checker.allowed_target_names = set()
    with patch.object(SimPlatformFCLCollisionChecker, 'check', return_value=CollisionResult(False, float('inf'), ())):
        yield runtime, checker, geom


class PolicyTests(unittest.TestCase):
    def test_runtime_export_accepts_numpy_hinge_joint_with_mujoco_enums(self):
        # MuJoCo 3.12 / NumPy 2.2 tuple membership can compare enum==scalar
        # differently from scalar==enum. A supported joint must still export.
        with scene() as (runtime, checker, _), tempfile.TemporaryDirectory() as directory:
            exported = export_runtime(runtime, checker, {'safety_distance': .003}, directory)
            articulated = [body for body in exported['bodies'] if 'joint_name' in body]
            self.assertEqual(len(articulated), len(runtime.joint_names))
            self.assertTrue(all(body['kind'] in ('revolute', 'prismatic') for body in articulated))

    def test_arm_pedestal_policy_preserves_tool_payload_and_other_obstacles(self):
        with scene() as (runtime, checker, g), tempfile.TemporaryDirectory() as directory:
            checker._arm_link_names = frozenset({'base', 'arm'})
            checker._allowed_arm_environment_names = frozenset({'RobotPedestal'})
            checker._bind_arm_environment_policy()
            runtime.gripper_controller.held_name = 'payload'
            policy, _ = freeze_policy(checker, [.4], .003)
            checker.check([.4], safety_distance=.003)
            host_pairs = {frozenset((a, b)) for a, b, _ in checker._pairs}
            for fixture in ('pedestal', 'pedestal_child'):
                self.assertIsNone(pair_limit(policy, g('arm'), g(fixture)))
                self.assertIsNone(pair_limit(policy, g('base'), g(fixture)))
                for actor in ('tool', 'payload'):
                    self.assertEqual(pair_limit(policy, g(actor), g(fixture)), .003)
                    self.assertIn(frozenset((g(actor), g(fixture))), host_pairs)
            self.assertEqual(pair_limit(policy, g('arm'), g('table')), .003)
            exported = export_runtime(runtime, checker, {'safety_distance': .003}, directory)
            compiled = compile_policy(convert_runtime({'_runtime_collision': exported}, directory))
            limits = world_limits(compiled, [g('arm'), g('tool'), g('payload')], [g('pedestal'), g('table')])
            self.assertLess(limits[0, 0], IGNORE_DISTANCE / 2)
            np.testing.assert_allclose(limits[1:, 0], .003)
            np.testing.assert_allclose(limits[:, 1], .003)

    def test_arm_pedestal_change_invalidates_signature_and_host_query_cache(self):
        with scene() as (runtime, checker, _):
            checker._arm_environment_geom_pairs = frozenset()
            initial = planning_signature(runtime, checker)
            checker.check([.4], safety_distance=.003)
            checker._arm_link_names = frozenset({'base', 'arm'})
            checker._allowed_arm_environment_names = frozenset({'RobotPedestal'})
            checker._bind_arm_environment_policy()
            self.assertNotEqual(initial, planning_signature(runtime, checker))
            checker.check([.4], safety_distance=.003)
            self.assertEqual(len(checker._query_cache), 2)

    def test_native_arm_fixture_pairs_do_not_include_attachment_or_payload(self):
        checker = SimPlatformFCLCollisionChecker.__new__(SimPlatformFCLCollisionChecker)
        checker._allowed_arm_environment_names = frozenset({'RobotPedestal'})
        checker._arm_link_names = frozenset({'base', 'arm'})
        checker._entities = {'RobotPedestal': object()}
        checker._collider_links = {1: 'arm', 2: 'robotiq_2f85_base', 3: 'base'}
        checker._names = {1: 'ur5', 2: 'gripper', 3: 'ur5', 4: 'RobotPedestal',
                          5: 'payload', 6: 'table', 7: 'RobotPedestal'}
        self.assertEqual(checker._arm_environment_pairs(),
                         {frozenset((a, b)) for a in (1, 3) for b in (4, 7)})
        checker._allowed_arm_environment_names = frozenset({'missing_fixture'})
        with self.assertRaisesRegex(ValueError, 'unloaded objects'):
            checker._arm_environment_pairs()

    def test_host_still_rejects_tool_and_held_payload_penetrating_pedestal(self):
        with scene() as (runtime, checker, g):
            checker._arm_link_names = frozenset({'base', 'arm'})
            checker._allowed_arm_environment_names = frozenset({'RobotPedestal'})
            checker._bind_arm_environment_policy()
            runtime.gripper_controller.held_name = 'payload'
            pedestal_body = int(runtime.model.geom_bodyid[g('pedestal')])
            runtime.model.body_pos[pedestal_body] = runtime.data.xpos[runtime.gripper_controller.base_id]
            result = checker.check([.4], safety_distance=.003)
            self.assertTrue(result.in_collision)
            contacts = {frozenset((c['geom1'], c['geom2'])) for c in result.contacts}
            self.assertIn(frozenset(('robotiq_2f85_base', 'RobotPedestal')), contacts)
            self.assertIn(frozenset(('payload', 'RobotPedestal')), contacts)

    def test_authoring_arm_fixture_rule_survives_assembly(self):
        from demo.scene.assembler import SceneAssembler
        from execution.resources import component
        import json
        source = json.loads((Path(__file__).resolve().parents[2] /
                             'requests/commands.005/ur5_table_sorting.json').read_text(encoding='utf-8'))
        robot = next(o for o in source['objects'] if o['object_name_in_scene'] == 'ur5')
        self.assertEqual(component(robot, 'RobotDriver')['allowed_arm_environment_names'], ['RobotPedestal'])
        # Authoring metadata must still trigger the complete OLS assembly.
        self.assertFalse(component(robot, 'RobotModelRef'))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'arm.urdf').write_text('<robot name="arm"><link name="base"/></robot>', encoding='utf-8')
            assembler = SceneAssembler(directory, None)
            result = assembler._robot_model_ref(robot, 'arm.urdf', [], None)
            self.assertEqual(result['allowed_arm_environment_names'], ['RobotPedestal'])

    def test_pick_only_allows_tool_target(self):
        with scene() as (runtime, checker, g):
            runtime.contact_target = 'target'
            policy, _ = freeze_policy(checker, [.4], .003)
            self.assertIsNone(pair_limit(policy, g('tool'), g('target')))
            self.assertEqual(pair_limit(policy, g('arm'), g('target')), .003)
            self.assertEqual(len(checker._query_cache), 0)

    def test_held_object_does_not_exempt_environment_or_arm(self):
        with scene() as (runtime, checker, g):
            runtime.gripper_controller.held_name = 'payload'
            policy, _ = freeze_policy(checker, [.4], .003)
            self.assertIsNone(pair_limit(policy, g('tool'), g('payload')))
            self.assertEqual(pair_limit(policy, g('payload'), g('target')), .003)
            self.assertEqual(pair_limit(policy, g('payload'), g('arm')), .003)

    def test_retreat_does_not_exempt_arm_or_payload(self):
        with scene() as (runtime, checker, g):
            runtime.gripper_controller.held_name = 'payload'
            checker.allow_gripper_environment_contact = True
            policy, _ = freeze_policy(checker, [.4], .003)
            self.assertIsNone(pair_limit(policy, g('tool'), g('target')))
            self.assertEqual(pair_limit(policy, g('arm'), g('target')), .003)
            self.assertEqual(pair_limit(policy, g('payload'), g('target')), .003)

    def test_lift_scope_restores_and_does_not_mutate_captured_history(self):
        with scene() as (runtime, checker, g):
            runtime.gripper_controller.held_name = 'payload'
            with checker.lift_payload_collision_scope():
                policy, _ = freeze_policy(checker, [.4], .003)
                self.assertIsNone(pair_limit(policy, g('payload'), g('target')))
                self.assertIsNone(pair_limit(policy, g('payload'), g('arm')))
            after, _ = freeze_policy(checker, [.4], .003)
            self.assertEqual(pair_limit(after, g('payload'), g('target')), .003)

    def test_support_threshold_and_initial_penetration(self):
        with scene() as (runtime, checker, g):
            runtime.gripper_controller.held_name = 'payload'
            checker.support_geoms = {g('table')}
            checker._lift_clearance_pairs = {frozenset((g('tool'), g('target'))): -.002}
            policy, _ = freeze_policy(checker, [.4], .003)
            self.assertEqual(pair_limit(policy, g('payload'), g('table')), -.001)
            self.assertAlmostEqual(pair_limit(policy, g('tool'), g('target')), -.002001)
            self.assertEqual(pair_limit(policy, g('arm'), g('table')), .003)

    def test_disable_collision_is_explicit_and_scoped(self):
        with scene() as (_, checker, g):
            policy, _ = freeze_policy(checker, [.4], .003, avoid_collision=False)
            self.assertIsNone(pair_limit(policy, g('arm'), g('target')))
            after, _ = freeze_policy(checker, [.4], .003)
            self.assertEqual(pair_limit(after, g('arm'), g('target')), .003)

    def test_signature_detects_grasp_policy_and_geometry_changes(self):
        with scene() as (runtime, checker, _):
            initial = planning_signature(runtime, checker)
            runtime.gripper_controller.held_name = 'payload'
            self.assertNotEqual(initial, planning_signature(runtime, checker))
            runtime.gripper_controller.held_name = None
            self.assertEqual(initial, planning_signature(runtime, checker))
            checker.allow_gripper_environment_contact = True
            self.assertNotEqual(initial, planning_signature(runtime, checker))
            checker.allow_gripper_environment_contact = False
            runtime.model.geom_size[0, 0] += .001
            self.assertNotEqual(initial, planning_signature(runtime, checker))

    def test_demo_wrapper_does_not_share_mutable_export_probe(self):
        from demo.run_demo import DemoCollisionOverrideChecker
        with scene() as (_, checker, g):
            wrapper = DemoCollisionOverrideChecker(checker)
            original_probe = checker.probe
            wrapper.set_demo_ignore_collisions(True)
            policy, _ = freeze_policy(wrapper, [.4], .003)
            self.assertIsNone(pair_limit(policy, g('arm'), g('target')))
            self.assertIs(checker.probe, original_probe)
            self.assertEqual(len(checker._query_cache), 0)
            wrapper.set_demo_ignore_collisions(False)
            policy, _ = freeze_policy(wrapper, [.4], .003)
            self.assertEqual(pair_limit(policy, g('arm'), g('target')), .003)

    def test_runtime_export_fk_joint_pivot_reference_and_payload(self):
        with scene() as (runtime, checker, g), tempfile.TemporaryDirectory() as directory:
            runtime.gripper_controller.held_name = 'payload'
            relative = np.eye(4); relative[:3, 3] = [.03, 0, .03]
            runtime.gripper_controller.relative_pose = relative
            qpos = runtime.data.qpos.copy()
            exported = export_runtime(runtime, checker, {'safety_distance': .003}, directory)
            converted = convert_runtime({'_runtime_collision': exported}, directory)
            urdf = URDFModel(converted['urdf'])
            for q in (.4, -.7):
                runtime.data.qpos[0] = q
                mujoco.mj_kinematics(runtime.model, runtime.data)
                fk = urdf.fk({'joint': q})
                expected = runtime.data.site_xpos[0]
                actual = np.asarray(converted['base_world']) @ fk['curobo_tcp']
                np.testing.assert_allclose(actual[:3, 3], expected, atol=1e-9)
            runtime.data.qpos[:] = qpos
            self.assertTrue(converted['payload_bodies'])
            self.assertNotIn(f'runtime_obstacle_{g("payload")}', converted['world_geoms'])
            self.assertIn(f'runtime_geom_{g("payload")}', converted['geometry_links'])
            compiled = compile_policy(converted)
            table = world_limits(compiled, [g('tool'), g('arm')], [g('target'), None])
            self.assertEqual(table.dtype, np.float32)
            self.assertTrue(np.all(table[:, 1] < IGNORE_DISTANCE / 2))


if __name__ == '__main__':
    unittest.main()
