import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_action.motion.common.action import _MoveExecutor
from planning import PlanningError


class PlaceApproachBranchSelectionTests(unittest.TestCase):
    def test_found_detour_does_not_retry_earlier_failed_branch(self):
        executor = _MoveExecutor.__new__(_MoveExecutor)
        executor.kinematics = SimpleNamespace(limits=np.tile([-3., 3.], (6, 1)))
        executor.collision_checker = SimpleNamespace(
            check=Mock(return_value=SimpleNamespace(in_collision=False)),
            check_path=Mock(return_value=SimpleNamespace(in_collision=True, contacts=[])))
        executor.direct_planner = SimpleNamespace(plan=Mock())
        def detour(path, *args):
            if path['goal'][0] < .15:
                raise PlanningError('blocked', code='PATH_NOT_FOUND')
            return {'waypoints': np.vstack((path['start'], np.ones(6), path['goal']))}
        executor.collision_free_planner = SimpleNamespace(plan=Mock(side_effect=detour))
        request = {'planning': {'planning_time': 10., 'mode': 'collision_free', 'allow_replan': True},
                   'motion': {'path_constraint': 'soft'}, 'constraints': {'avoid_collision': True}}
        validator = Mock(side_effect=[False, True, True])
        result = executor._plan_joint_candidates(request, {}, {}, {
            'path_type': 'joint', 'start': np.zeros(6),
            'candidate_goals': [np.full(6, v) for v in [.05, .1, .2]],
            'candidate_validator': validator})
        np.testing.assert_allclose(result['waypoints'][-1], .2)
        self.assertEqual(executor.collision_free_planner.plan.call_count, 2)
        diagnostics = result['diagnostics']['configuration_selection']
        self.assertEqual(diagnostics['contact_candidates_rejected'], 1)
        self.assertTrue(diagnostics['stopped_after_feasible_solution'])
