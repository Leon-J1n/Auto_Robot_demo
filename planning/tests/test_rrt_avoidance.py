"""Exercise the default Direct -> RRT route against a blocked joint segment."""

from types import SimpleNamespace
import unittest

import numpy as np

from atomic_action.motion.common.action import _MoveExecutor
from planning import CollisionFreePlanner, DirectPlanner


class _ObstacleChecker:
    def check(self, joints, *, safety_distance, **kwargs):
        hit = np.linalg.norm(np.asarray(joints)[:2]) < .25 + safety_distance
        return SimpleNamespace(in_collision=hit, contacts=["obstacle"] if hit else [])

    def check_path(self, points, *, resolution, safety_distance, **kwargs):
        for start, goal in zip(points[:-1], points[1:]):
            count = max(1, int(np.ceil(np.linalg.norm(np.asarray(goal) - start) / resolution)))
            for alpha in np.linspace(0., 1., count + 1):
                result = self.check(np.asarray(start) + alpha * (np.asarray(goal) - start),
                                    safety_distance=safety_distance)
                if result.in_collision:
                    return result
        return SimpleNamespace(in_collision=False, contacts=[])


class RRTAvoidanceTests(unittest.TestCase):
    def test_auto_routes_around_obstacle_with_rrt(self):
        checker = _ObstacleChecker()
        limits = [[-1., 1.]] * 6
        action = _MoveExecutor.__new__(_MoveExecutor)
        action.direct_planner = DirectPlanner(collision_checker=checker, joint_limits=limits)
        action.collision_free_planner = CollisionFreePlanner(
            {'planning': {'collision_free': {'optimize_path': False, 'shortcut_time': .02}}},
            collision_checker=checker, joint_limits=limits)
        request = {
            'motion': {'path_constraint': 'soft'},
            'planning': {'mode': 'auto', 'planner_type': 'auto', 'allow_replan': True,
                         'planning_time': 2., 'max_attempts': 1},
            'constraints': {'avoid_collision': True, 'safety_distance': .01, 'velocity_scale': .5},
        }
        start = np.array([-.7, 0., 0., 0., 0., 0.])
        goal = -start
        path = {'path_type': 'joint', 'start': start, 'goal': goal,
                'joint_waypoints': [start, goal]}
        selection = {}
        trajectory = action._plan_with_fallback(request, {}, selection, path)
        self.assertEqual(trajectory['planner'], 'rrt')
        self.assertEqual(selection['planner_type'], 'rrt')
        self.assertTrue(selection['fallback_used'])
        points = np.asarray(trajectory['waypoints'])
        np.testing.assert_allclose(points[0], start)
        np.testing.assert_allclose(points[-1], goal)
        self.assertFalse(checker.check_path(points, resolution=.005, safety_distance=.01).in_collision)
        action.collision_free_planner.close()


if __name__ == '__main__':
    unittest.main()
