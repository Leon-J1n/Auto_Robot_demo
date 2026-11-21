"""Retreat lift sequencing and world-axis regression tests (no robot needed)."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

import numpy as np
from scipy.spatial.transform import Rotation

from atomic_skill.retreat import RetreatSkill


class Executor:
    def __init__(self, *, fail_lift=False, fail_retreat=False):
        self.position = np.array([.2, .3, .4])
        self.rotation = np.eye(3)
        self.backend = SimpleNamespace(physics_steps=0, get_end_effector_pose=self.pose)
        self.resources = SimpleNamespace(scene={})
        self.active_lift_scope = False
        self.clearance = False
        self.checker = SimpleNamespace(
            lift_payload_collision_scope=self.lift_scope,
            capture_lift_clearance=lambda margin: setattr(self, 'clearance', True),
            clear_lift_clearance=lambda: setattr(self, 'clearance', False),
            support_geoms={1})
        self.calls = []
        self.phases = []
        self.fail_lift, self.fail_retreat = fail_lift, fail_retreat

    def pose(self):
        return {'position': self.position.copy(), 'rotation_matrix': self.rotation.copy()}

    @contextmanager
    def lift_scope(self):
        self.active_lift_scope = True
        try:
            yield
        finally:
            self.active_lift_scope = False

    def _move(self, name, *, position, rotation, **kwargs):
        self.calls.append((name, position.copy(), self.active_lift_scope, self.clearance))
        if name == 'lift_up' and self.fail_lift:
            return False
        if name.startswith('retreat') and self.fail_retreat:
            self.fail_retreat = False
            self.phases.append({'success': False, 'error': {
                'error_code': 'PLANNING_FAILED', 'failed_stage': 'planning'}})
            return False
        self.position = position.copy()
        if name == 'lift_up':
            self.position[2] -= .001  # The following retreat must use measured TCP.
        return True

    def _fail(self, code, message):
        self.error = (code, message)
        return False


class RetreatLiftTests(unittest.TestCase):
    def test_default_and_explicit_lift_from_measured_endpoint(self):
        for params, height in (({}, .01), ({'lift_distance': .025}, .025)):
            with self.subTest(params=params):
                e = Executor()
                self.assertTrue(RetreatSkill(e).execute({'pose_axis': '+x', **params}))
                self.assertEqual(len(e.calls), 2)
                np.testing.assert_allclose(e.calls[0][1], [.2, .3, .4 + height])
                np.testing.assert_allclose(e.calls[1][1], [.1, .3, .4 + height - .001])
                self.assertEqual(e.calls[0][2:], (True, True))
                self.assertEqual(e.calls[1][2:], (False, False))
                self.assertFalse(e.checker.support_geoms)

    def test_zero_skips_lift(self):
        e = Executor()
        self.assertTrue(RetreatSkill(e).execute({'pose_axis': '+x', 'lift_distance': 0}))
        self.assertEqual(len(e.calls), 1)
        np.testing.assert_allclose(e.position, [.1, .3, .4])

    def test_retreat_retry_does_not_repeat_lift(self):
        e = Executor(fail_retreat=True)
        self.assertTrue(RetreatSkill(e).execute({'pose_axis': '+x'}))
        self.assertEqual([call[0] for call in e.calls],
                         ['lift_up', 'retreat_100mm', 'retreat_95mm'])
        np.testing.assert_allclose(e.calls[1][1], [.1, .3, .409])
        np.testing.assert_allclose(e.calls[2][1], [.105, .3, .409])

    def test_short_default_retreat_and_explicit_minimum(self):
        for search, expected in (({}, True), ({'min_distance': .02}, False)):
            with self.subTest(search=search):
                e = Executor()
                attempted = []
                def move(name, *, position, **kwargs):
                    distance = np.linalg.norm(position - e.position)
                    attempted.append(distance)
                    if distance > .015 + 1.e-9:
                        e.phases.append({'success': False, 'error': {
                            'error_code': 'PATH_CONSTRAINT_INFEASIBLE', 'failed_stage': 'planning'}})
                        return False
                    return True
                e._move = move
                self.assertEqual(bool(RetreatSkill(e).execute({
                    'pose_axis': '+z', 'search': search})), expected)
                if expected:
                    self.assertAlmostEqual(e.successful_distance_m, .015)
                else:
                    self.assertGreaterEqual(min(attempted), .02 - 1.e-9)

    def test_world_z_angle_boundary_in_both_directions(self):
        for degrees in (0, 14.9, 15, 15.1, 90, 164.9, 165, 180):
            with self.subTest(degrees=degrees):
                e = Executor()
                ref = Rotation.from_euler('y', degrees, degrees=True).as_matrix()
                self.assertTrue(RetreatSkill(e).execute({
                    'pose_axis': '+z', 'reference_rotation_world': ref.tolist()}))
                expected_lift = 15 < degrees < 165
                self.assertEqual(e.calls[0][0] == 'lift_up', expected_lift)

    def test_upward_retreat_uses_initial_bounds_without_ignoring_payload(self):
        for params in ({}, {'lift_distance': 0}):
            with self.subTest(params=params):
                e = Executor()
                self.assertTrue(RetreatSkill(e).execute({'pose_axis': '-z', **params}))
                self.assertEqual(len(e.calls), 1)
                self.assertEqual(e.calls[0][2:], (False, True))
                self.assertFalse(e.clearance)
                self.assertFalse(e.checker.support_geoms)

    def test_downward_and_horizontal_retreat_keep_normal_collision_policy(self):
        for axis in ('+z', '+x'):
            with self.subTest(axis=axis):
                e = Executor()
                self.assertTrue(RetreatSkill(e).execute({'pose_axis': axis, 'lift_distance': 0}))
                self.assertEqual(e.calls[0][2:], (False, False))

    def test_upward_retry_keeps_same_clearance_until_search_finishes(self):
        e = Executor(fail_retreat=True)
        self.assertTrue(RetreatSkill(e).execute({'pose_axis': '-z'}))
        self.assertEqual(len(e.calls), 2)
        self.assertTrue(all(call[3] for call in e.calls))
        self.assertFalse(e.clearance)

    def test_invalid_lift_distance_has_no_motion(self):
        for value in (-.01, float('nan'), float('inf'), None, 'bad'):
            with self.subTest(value=value):
                e = Executor()
                self.assertFalse(RetreatSkill(e).execute({'pose_axis': '+x', 'lift_distance': value}))
                self.assertEqual(e.error[0], 'INVALID_RETREAT_REQUEST')
                self.assertEqual(e.calls, [])

    def test_failed_lift_stops_and_restores_collision_scope(self):
        e = Executor(fail_lift=True)
        self.assertFalse(RetreatSkill(e).execute({'pose_axis': '+x'}))
        self.assertEqual(len(e.calls), 1)
        self.assertFalse(e.active_lift_scope)
        self.assertFalse(e.clearance)
        self.assertEqual(e.checker.support_geoms, {1})


if __name__ == '__main__':
    unittest.main()
