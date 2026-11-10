"""Put recovery searches different IK solutions while preserving its pose."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.put import DemoInteractionPut
from atomic_action.motion.common.interaction import pose_request
from atomic_action.motion.common.strategies import JointMove
from atomic_action.motion.common.action import _MoveExecutor
from planning.direct_planner import DirectPlanner


class PutIKFallbackTests(unittest.TestCase):
    def make_parts(self):
        goal = np.eye(4)
        goal[:3, 3] = [.1, .2, .9]
        base = {'target': pose_request(goal), 'motion': {'path_type': 'linear'},
                'constraints': {}, 'planning': {'planning_time': 5.}, 'strategy_params': {}}
        resolved = {'joint_positions': [1.] * 6}
        adapter = DemoInteractionPut.__new__(DemoInteractionPut)
        adapter._resolve_request = Mock(return_value=(base, resolved, goal, goal, 'linear'))
        parts, resolved = adapter.prepare({}, {'joints': [0.] * 6})
        return parts, resolved, goal

    def test_fallback_retains_world_pose_and_enables_collision_free_search(self):
        parts, _, goal = self.make_parts()
        fallback = parts[0]['direct_pick_fallback']
        self.assertEqual(fallback['target'], pose_request(goal))
        self.assertEqual(fallback['motion']['path_type'], 'joint')
        self.assertEqual(fallback['planning']['mode'], 'collision_free')
        self.assertTrue(fallback['planning']['allow_replan'])
        self.assertEqual(parts[0]['request']['motion']['path_type'], 'linear')

    def test_joint_fallback_skips_colliding_solution_and_uses_another(self):
        parts, _, _ = self.make_parts()
        fallback = deepcopy(parts[0]['direct_pick_fallback'])
        first, second = np.full(6, .1), np.full(6, .2)
        kin = SimpleNamespace(limits=np.array([[-3., 3.]] * 6),
            solve_all=Mock(return_value=SimpleNamespace(solutions=[first, second])),
            equivalent_configurations=lambda q, *args, **kwargs: [q])
        checker = SimpleNamespace(check=Mock(side_effect=lambda q, **kw:
            SimpleNamespace(in_collision=np.allclose(q, first), contacts=(
                {'geom1': 'Screwdriver', 'geom2': 'forearm_link', 'distance': -.046738},))),
            check_path=Mock(return_value=SimpleNamespace(in_collision=False)))
        context = {'current_joint_state': np.zeros(6)}
        path = JointMove(kinematics=kin, collision_checker=checker).build_path_request(fallback, context)
        self.assertEqual(len(path['candidate_goals']), 2)
        executor = SimpleNamespace(collision_checker=checker, direct_planner=DirectPlanner(
            collision_checker=checker, joint_limits=kin.limits))
        fallback['constraints'].update(avoid_collision=True, safety_distance=.003, velocity_scale=.6)
        selection = {}
        trajectory = _MoveExecutor._plan_joint_candidates(executor, fallback, context, selection, path)
        np.testing.assert_allclose(trajectory['waypoints'][-1], second)
        self.assertEqual(checker.check.call_count, 3)
        self.assertEqual(kin.solve_all.call_args.args[0]['type'], 'pose')


if __name__ == '__main__':
    unittest.main()
