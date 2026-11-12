"""Exact whole-path reuse must not certify a changed path or collision scene."""
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from collision.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
from demo.collision.grasp_collision import GraspCollisionChecker
from planning.errors import PlanningError
from planning.time_parameterization import prepare_continuous_trajectory


class PathValidationReuseTests(unittest.TestCase):
    def fixture(self):
        checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        checker.path_validation_cache_enabled = True  # Explicit opt-in for cache correctness tests.
        checker.backend = SimpleNamespace(
            data=SimpleNamespace(qpos=np.zeros(6), xpos=np.zeros((2, 3)), xmat=np.zeros((2, 9))),
            model=SimpleNamespace(geom_size=np.ones((1, 3))),
            qpos_indices=np.arange(6), resources=SimpleNamespace(scene={'objects': []}),
            gripper_controller=SimpleNamespace(held_name=None, mode='physical', base_id=1),
            contact_target=None, object_ids={})
        for name in ('robot_geom_ids', 'baseline_allowed_pairs', 'allowed_target_names',
                     'dynamic_names', 'dynamic_handles', 'tool_geoms', 'robot_bodies',
                     'geoms', 'excluded', 'adjacent', 'support_geoms'):
            setattr(checker, name, set())
        checker._names = {}
        checker._collider_links = {}
        checker.body_names = []
        checker.allow_gripper_environment_contact = False
        checker._query_cache = OrderedDict()
        checker._scene_cache_stamp = Mock(return_value='static-world')
        path = np.linspace(np.zeros(6), np.ones(6), 5)
        return checker, path

    def test_identical_query_reuses_result_and_returns_an_independent_copy(self):
        checker, path = self.fixture()
        result = CollisionResult(True, -.1, ({'geom1': 'arm', 'geom2': 'box'},))
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path', return_value=result) as query:
            checker.check_path(path, resolution=.01)
            cached = checker.check_path(path.copy(), resolution=.01)
            cached.contacts[0]['geom1'] = 'changed by caller'
            again = checker.check_path(path, resolution=.01)
        query.assert_called_once()
        self.assertEqual(again.contacts[0]['geom1'], 'arm')
        self.assertEqual(checker.path_validation_cache_stats['hits'], 2)

    def test_every_relevant_change_requires_a_fresh_validation(self):
        def geometry(c):
            c.backend.model.geom_size[0, 0] += .01
        def state(c):
            c.backend.data.qpos[0] += 1e-12
        def fcl(c):
            c._scene_cache_stamp.return_value = 'moved-static-world'
        changes = [geometry, state, fcl,
            lambda c: setattr(c.backend, 'contact_target', 'Apple'),
            lambda c: c.support_geoms.add(3),
            lambda c: setattr(c, 'allow_gripper_environment_contact', True),
            lambda c: setattr(c.backend.gripper_controller, 'held_name', 'Apple'),
            lambda c: setattr(c.backend.gripper_controller, 'relative_pose', np.eye(4)),
            lambda c: setattr(c, '_lift_clearance_pairs', {frozenset((1, 2)): -.01}),
            lambda c: setattr(c, 'scene_revision', 1),
            lambda c: c.allowed_target_names.add('Tray')]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                checker, path = self.fixture()
                with patch.object(SimPlatformFCLCollisionChecker, 'check_path',
                                  return_value=CollisionResult(False, 1., ())) as query:
                    checker.check_path(path, resolution=.01)
                    change(checker)
                    checker.check_path(path, resolution=.01)
                self.assertEqual(query.call_count, 2)

    def test_path_resolution_margin_and_allowances_are_exact_keys(self):
        checker, path = self.fixture()
        changed = path.copy()
        changed[2, 0] += 1e-14
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path',
                          return_value=CollisionResult(False, 1., ())) as query:
            checker.check_path(path, resolution=.01)
            checker.check_path(changed, resolution=.01)
            checker.check_path(path, resolution=.02)
            checker.check_path(path, resolution=.01, safety_distance=.003)
            checker.check_path(path, resolution=.01, allowed_geom_ids={5})
        self.assertEqual(query.call_count, 5)

    def test_state_change_during_query_never_publishes_a_cached_result(self):
        checker, path = self.fixture()
        def query(*args, **kwargs):
            checker.backend.data.qpos[0] += .01
            return CollisionResult(False, 1., ())
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path', side_effect=query) as checked:
            checker.check_path(path)
            checker.check_path(path)
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(checker.path_validation_cache_stats['hits'], 0)

    def test_small_paths_and_disabled_cache_use_original_checker(self):
        checker, path = self.fixture()
        checker._path_validation_state = Mock(side_effect=AssertionError('no snapshot overhead'))
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path',
                          return_value=CollisionResult(False, 1., ())) as query:
            checker.check_path(path[:2])
            checker.path_validation_cache_enabled = False
            checker.check_path(path)
        self.assertEqual(query.call_count, 2)

    def test_default_cache_is_disabled_and_has_no_state_snapshot_overhead(self):
        checker, path = self.fixture()
        del checker.path_validation_cache_enabled
        checker._path_validation_state = Mock(side_effect=AssertionError('default must not hash scene'))
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path',
                          return_value=CollisionResult(False, 1., ())) as query:
            checker.check_path(path)
            checker.check_path(path)
        self.assertEqual(query.call_count, 2)
        checker._path_validation_state.assert_not_called()

    def test_smoothed_path_is_checked_even_when_planner_path_is_cached(self):
        checker, points = self.fixture()
        points[2, 1] += .2
        request = {'motion': {'path_type': 'joint'},
                   'constraints': {'velocity_scale': .4, 'acceleration_scale': .8},
                   'planning': {'time_parameterization': {'max_velocity': [3.] * 6,
                                                         'max_acceleration': [3.] * 6}}}
        seen = []
        def query(path, **kwargs):
            seen.append(np.asarray(path).copy())
            return CollisionResult(False, 1., ())
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path', side_effect=query):
            checker.check_path(points, resolution=.01)
            result = prepare_continuous_trajectory({'waypoints': points.tolist()}, request,
                                                  collision_checker=checker)
            again = prepare_continuous_trajectory({'waypoints': points.tolist()}, request,
                                                 collision_checker=checker)
        self.assertEqual(len(seen), 2)
        self.assertFalse(np.array_equal(seen[0], seen[1]))
        diag = result['diagnostics']['time_parameterization']
        self.assertFalse(diag['validation_waypoints_identical_to_planner'])
        self.assertEqual(again['diagnostics']['time_parameterization']['path_validation_cache']['hits'], 1)

    def test_collision_on_new_spline_cannot_be_hidden_by_cached_polyline(self):
        checker, path = self.fixture()
        path[2, 1] += .2
        request = {'motion': {'path_type': 'joint'},
                   'constraints': {'velocity_scale': .4, 'acceleration_scale': .8},
                   'planning': {'time_parameterization': {'max_velocity': [3.] * 6,
                                                         'max_acceleration': [3.] * 6}}}
        def query(points, **kwargs):
            return CollisionResult(not np.array_equal(points, path), -.1, ())
        with patch.object(SimPlatformFCLCollisionChecker, 'check_path', side_effect=query):
            checker.check_path(path, resolution=.01)
            with self.assertRaises(PlanningError):
                prepare_continuous_trajectory({'waypoints': path.tolist()}, request,
                                              collision_checker=checker)


if __name__ == '__main__':
    unittest.main()
