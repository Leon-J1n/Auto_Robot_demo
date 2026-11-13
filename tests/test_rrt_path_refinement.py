"""Geometry and collision-policy regressions for bounded RRT refinement."""
import unittest
from types import SimpleNamespace

import numpy as np

from planning.collision_free_planner import CollisionFreePlanner, _densify, _path_metrics
from planning.trajopt_optimizer import TrajOptOptimizer


class CircleChecker:
    def __init__(self):
        self.resolutions = []

    def check_path(self, points, *, resolution, **kwargs):
        self.resolutions.append(resolution)
        points = np.asarray(points)
        for a, b in zip(points[:-1], points[1:]):
            direction = b - a
            t = np.clip(-np.dot(a, direction) / max(np.dot(direction, direction), 1e-20), 0., 1.)
            if np.linalg.norm(a + t * direction) < .8:
                return SimpleNamespace(in_collision=True)
        return SimpleNamespace(in_collision=False)


class RRTPathRefinementTests(unittest.TestCase):
    def test_interior_shortcuts_remove_detour_when_vertex_connections_collide(self):
        path = np.array([[-1., 0.], [-1., 1.], [1., 1.], [1., 0.]])
        checker = CircleChecker()
        planner = CollisionFreePlanner(collision_checker=checker,
                                       joint_limits=np.tile([-3., 3.], (2, 1)))
        diagnostics = {}
        result = planner._shortcut(list(path), np.random.default_rng(17), .003,
                                   iterations=120, diagnostics=diagnostics)
        np.testing.assert_array_equal(result[0], path[0])
        np.testing.assert_array_equal(result[-1], path[-1])
        self.assertGreater(diagnostics['interior_shortcuts'], 0)
        self.assertLess(_path_metrics(result)['joint_length_l2_rad'], 3.8)
        self.assertFalse(checker.check_path(result, resolution=.03).in_collision)
        self.assertTrue(all(r == .03 for r in checker.resolutions))

    def test_local_cost_matches_full_objective_at_boundaries_and_interior(self):
        rng = np.random.default_rng(3)
        for count in (3, 4, 20):
            path = rng.normal(size=(count, 6))
            for index in range(1, count - 1):
                candidate = rng.normal(size=6)
                changed = path.copy()
                changed[index] = candidate
                expected = (TrajOptOptimizer._objective(path, .25)
                            - TrajOptOptimizer._objective(changed, .25))
                self.assertAlmostEqual(TrajOptOptimizer._local_improvement(
                    path, index, candidate, .25), expected, places=10)

    def test_sparse_optimization_keeps_endpoints_and_avoids_obstacle(self):
        path = _densify(np.array([[-1., 0.], [-1., 1.], [1., 1.], [1., 0.]]), .16)
        checker = CircleChecker()
        optimizer = TrajOptOptimizer(collision_checker=checker,
                                    joint_limits=np.tile([-3., 3.], (2, 1)),
                                    config={'max_time': 2., 'max_iterations': 6, 'collision_resolution': .01})
        result, diagnostics = optimizer.optimize(path)
        np.testing.assert_array_equal(result[[0, -1]], path[[0, -1]])
        self.assertTrue(diagnostics['applied'])
        self.assertLess(diagnostics['final_cost'], diagnostics['initial_cost'])
        self.assertFalse(checker.check_path(result, resolution=.01).in_collision)
        self.assertTrue(all(r == .01 for r in checker.resolutions))

    def test_straight_path_exits_without_local_collision_queries(self):
        checker = CircleChecker()
        path = np.array([[-1., 1.], [0., 1.], [1., 1.]])
        optimizer = TrajOptOptimizer(collision_checker=checker,
                                    joint_limits=np.tile([-3., 3.], (2, 1)))
        result, diagnostics = optimizer.optimize(path)
        np.testing.assert_array_equal(result, path)
        self.assertEqual(diagnostics['iterations'], 2)
        self.assertEqual(diagnostics['collision_checks'], 1)


if __name__ == '__main__':
    unittest.main()
