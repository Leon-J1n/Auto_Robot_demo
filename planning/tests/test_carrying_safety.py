"""Signed payload margins stay scoped to planning payload/world pairs."""
import unittest
import tempfile
from unittest.mock import patch
import mujoco
import numpy as np

from demo.interaction.carrying_safety import split_carrying_safety, payload_safety_distance
from collision.planning_policy import freeze_policy, pair_limit, planning_signature
from planning.tests.test_curobo_policy import scene
from libraries.curobo.runtime_policy import export_runtime
from libraries.curobo.conversion.runtime import convert_runtime
from libraries.curobo.policy import compile_policy, world_limits


class CarryingSafetyTests(unittest.TestCase):
    def test_default_allows_three_mm_only_for_payload_planning(self):
        for request in ({}, {'strategy_params': {}}, {'strategy_params': {'hold_force': 1.}}):
            _, value = split_carrying_safety(request)
            self.assertEqual(value, -.003)
        _, explicit_zero = split_carrying_safety({'strategy_params': {'safety_distance': 0.}})
        self.assertEqual(explicit_zero, 0.)
        self.assertEqual(payload_safety_distance(.003, 'payload'), -.003)
        self.assertEqual(payload_safety_distance(0., 'payload'), 0.)
        self.assertEqual(payload_safety_distance(.003, None), .003)
        with scene() as (runtime, checker, g):
            runtime.gripper_controller.held_name = 'payload'
            _, checker.carrying_safety_distance = split_carrying_safety({})
            policy, _ = freeze_policy(checker, [.4], .003)
            self.assertEqual(pair_limit(policy, g('payload'), g('target')), -.003)
            self.assertEqual(pair_limit(policy, g('tool'), g('target')), .003)
            self.assertEqual(pair_limit(policy, g('arm'), g('target')), .003)

    def test_signed_input_and_legacy_positive_semantics(self):
        request = {'strategy_params': {'safety_distance': -.002, 'force': 1}}
        result, value = split_carrying_safety(request)
        self.assertEqual(value, -.002)
        self.assertNotIn('safety_distance', result['strategy_params'])
        self.assertIn('safety_distance', request['strategy_params'])
        for invalid in (True, '0', float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                split_carrying_safety({'strategy_params': {'safety_distance': invalid}})
        for extra, expected in ((-.002, -.002), (0., .003), (.001, .003), (.01, .01)):
            self.assertEqual(payload_safety_distance(.003, 'payload', extra), expected)
            self.assertEqual(payload_safety_distance(0., 'payload', extra), 0.)
            self.assertEqual(payload_safety_distance(.003, None, extra), .003)

    def test_frozen_pair_scope_execution_release_and_signature(self):
        with scene() as (runtime, checker, g):
            runtime.gripper_controller.held_name = 'payload'
            initial = planning_signature(runtime, checker)
            checker.carrying_safety_distance = -.002
            self.assertNotEqual(initial, planning_signature(runtime, checker))
            policy, _ = freeze_policy(checker, [.4], .003)
            self.assertEqual(pair_limit(policy, g('payload'), g('target')), -.002)
            self.assertEqual(pair_limit(policy, g('tool'), g('target')), .003)
            self.assertEqual(pair_limit(policy, g('arm'), g('target')), .003)
            execution, _ = freeze_policy(checker, [.4], 0.)
            self.assertEqual(pair_limit(execution, g('payload'), g('target')), 0.)
            runtime.gripper_controller.held_name = None
            released, _ = freeze_policy(checker, [.4], .003)
            self.assertEqual(pair_limit(released, g('tool'), g('payload')), .003)

    def test_signed_margin_survives_curobo_export(self):
        with scene() as (runtime, checker, g), tempfile.TemporaryDirectory() as directory:
            runtime.gripper_controller.held_name = 'payload'
            checker.carrying_safety_distance = -.002
            raw = export_runtime(runtime, checker, {'safety_distance': .003}, directory)
            converted = convert_runtime({'_runtime_collision': raw}, directory)
            limits = world_limits(compile_policy(converted), [g('payload'), g('tool'), g('arm')], [g('target')])
            np.testing.assert_allclose(limits[:, 0], [-.002, .003, .003])

    def test_actual_shallow_overlap_native_and_proxy(self):
        for simplified in (False, True):
            with self.subTest(simplified=simplified), scene() as (runtime, checker, g):
                runtime.gripper_controller.held_name = 'payload'
                relative = np.eye(4); relative[0, 3] = .1
                runtime.gripper_controller.relative_pose = relative
                model = runtime.model
                model.geom_type[g('payload')] = mujoco.mjtGeom.mjGEOM_SPHERE
                model.geom_size[g('payload')] = [.02, 0, 0]
                tool = runtime.gripper_controller.base_id
                center = runtime.data.xpos[tool] + runtime.data.xmat[tool].reshape(3, 3) @ relative[:3, 3]
                target = runtime.object_ids['target']
                model.body_pos[target] = center + [0, 0, -.119]
                model.geom_size[g('target')] = [.01, .01, .1]
                checker.carrying_safety_distance = -.002
                # This fixture's gripper is an exact sphere, so no production mesh template is needed.
                with patch('collision.proxy._gripper_spheres', side_effect=lambda geom, _: [
                        {'center': np.asarray(geom['matrix'])[:3, 3].tolist(), 'radius': geom['size'][0]}]):
                    if simplified:
                        checker.enable_planning_proxy()
                    def payload_contacts(margin):
                        result = checker.check([.4], safety_distance=margin)
                        return [c for c in result.contacts if {c['geom1'], c['geom2']} == {'payload', 'target'}]
                    self.assertFalse(payload_contacts(.003))  # 1 mm overlap allowed in planning.
                    self.assertTrue(payload_contacts(0.))  # Execution still rejects penetration.
                    checker.carrying_safety_distance = 0.
                    self.assertTrue(payload_contacts(.003))  # Setting changes invalidate cached results.
                    checker.carrying_safety_distance = -.002
                    model.body_pos[target, 2] += .002
                    checker.scene_revision = getattr(checker, 'scene_revision', 0) + 1
                    self.assertTrue(payload_contacts(.003))  # 3 mm overlap exceeds the allowance.


if __name__ == '__main__':
    unittest.main()
