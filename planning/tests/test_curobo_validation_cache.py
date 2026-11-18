"""Exact path validation reuse must never hide scene or policy mutations."""
from collections import OrderedDict
import unittest
from unittest.mock import Mock, patch

import numpy as np

from collision.backends.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
from demo.collision.grasp_collision import GraspCollisionChecker
from planning.tests.test_curobo_policy import scene


class ValidationCacheTests(unittest.TestCase):
    def configure(self, checker):
        checker.path_validation_cache_enabled = True
        checker.baseline_allowed_pairs = set()
        checker.robot_geom_ids = set()
        checker.dynamic_handles = set()
        checker._query_cache = OrderedDict()
        checker._scene_cache_stamp = Mock(return_value='native snapshot')

    def test_exact_query_reused_but_scene_policy_and_grasp_changes_revalidate(self):
        with scene() as (runtime, checker, _):
            self.configure(checker)
            points = np.arange(6, dtype=float).reshape(-1, 1) * .01
            safe = CollisionResult(False, .01, ())
            with patch.object(SimPlatformFCLCollisionChecker, 'check_path', return_value=safe) as check:
                checker.check_path(points, resolution=.01, safety_distance=.003)
                checker.check_path(points.copy(), resolution=.01, safety_distance=.003)
                self.assertEqual(check.call_count, 1)
                mutations = [
                    lambda: runtime.data.qpos.__setitem__(0, .5),
                    lambda: runtime.model.geom_size.__setitem__((0, 0), .04),
                    lambda: setattr(runtime.gripper_controller, 'held_name', 'payload'),
                    lambda: runtime.gripper_controller.relative_pose.__setitem__((0, 3), .01),
                    lambda: checker.allowed_target_names.add('target'),
                    lambda: checker.support_geoms.add(2),
                    lambda: setattr(checker, '_arm_environment_geom_pairs', frozenset({frozenset((0, 2))})),
                    lambda: setattr(checker._scene_cache_stamp, 'return_value', 'changed native snapshot'),
                ]
                for index, mutate in enumerate(mutations, 2):
                    mutate()
                    checker.check_path(points, resolution=.01, safety_distance=.003)
                    self.assertEqual(check.call_count, index)
                    checker.check_path(points.copy(), resolution=.01, safety_distance=.003)
                    self.assertEqual(check.call_count, index)
                self.assertEqual(checker.path_validation_cache_stats['hits'], len(mutations) + 1)

    def test_resolution_clearance_and_path_are_part_of_query(self):
        with scene() as (_, checker, _):
            self.configure(checker)
            points = np.zeros((4, 1))
            with patch.object(SimPlatformFCLCollisionChecker, 'check_path',
                    return_value=CollisionResult(False, .01, ())) as check:
                checker.check_path(points, resolution=.01, safety_distance=.003)
                checker.check_path(points, resolution=.005, safety_distance=.003)
                checker.check_path(points, resolution=.005, safety_distance=.004)
                checker.check_path(points + .01, resolution=.005, safety_distance=.004)
                self.assertEqual(check.call_count, 4)

    def test_mutation_during_validation_never_publishes_certificate(self):
        with scene() as (_, checker, _):
            self.configure(checker)
            def mutate(*args, **kwargs):
                checker.allowed_target_names.add(str(len(checker.allowed_target_names)))
                return CollisionResult(False, .01, ())
            with patch.object(SimPlatformFCLCollisionChecker, 'check_path', side_effect=mutate) as check:
                for _ in range(2):
                    checker.check_path(np.zeros((4, 1)), resolution=.01, safety_distance=.003)
                self.assertEqual(check.call_count, 2)
                self.assertFalse(checker._validated_paths)


if __name__ == '__main__':
    unittest.main()
