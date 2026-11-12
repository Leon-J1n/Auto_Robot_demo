"""Avoidance must receive time after slow direct candidate checks."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from atomic_action.motion.common.action import _MoveExecutor
from planning.errors import CollisionPlanningError, PlanningError


class JointSearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.
        self.executor = _MoveExecutor.__new__(_MoveExecutor)
        self.executor.collision_checker = SimpleNamespace(check=Mock(
            return_value=SimpleNamespace(in_collision=False)),
            check_path=Mock(return_value=SimpleNamespace(in_collision=False)))
        self.executor.direct_planner = SimpleNamespace(plan=Mock(side_effect=self.blocked_direct))
        self.executor.collision_free_planner = SimpleNamespace(plan=Mock(side_effect=self.detour))
        self.request = {'planning': {'planning_time': 5., 'mode': 'collision_free', 'allow_replan': True},
                        'motion': {'path_constraint': 'soft'},
                        'constraints': {'avoid_collision': True, 'safety_distance': .003}}
        self.path = {'path_type': 'joint', 'start': np.zeros(6),
                     'candidate_goals': [np.full(6, value) for value in (.1, .2, .3, .4)]}

    def blocked_direct(self, path, request, context):
        self.clock += 3.
        raise CollisionPlanningError('blocked direct route', details=[
            {'geom1': 'gripper', 'geom2': 'Basket', 'distance': -.001}])

    def detour(self, path, request, context):
        self.clock += .1
        return {'waypoints': [path['start'].tolist(), (path['start'] + .05).tolist(),
                               path['goal'].tolist()]}

    def search(self, selection=None):
        with patch('atomic_action.motion.common.action.time.perf_counter', side_effect=lambda: self.clock):
            return self.executor._plan_joint_candidates(self.request, {}, selection if selection is not None else {}, self.path)

    def test_expensive_direct_check_reserves_time_for_rrt_without_repeating_probe(self):
        # Two other IK solutions have invalid endpoints and must be discarded cheaply.
        self.executor.collision_checker.check.side_effect = lambda q, **kw: SimpleNamespace(
            in_collision=np.allclose(q, .2) or np.allclose(q, .3), contacts=())
        selection = {}
        trajectory = self.search(selection)
        self.assertEqual(self.executor.collision_checker.check.call_count, 4)
        self.executor.direct_planner.plan.assert_called_once()
        self.executor.collision_free_planner.plan.assert_called_once()
        bounded = self.executor.collision_free_planner.plan.call_args.args[1]
        self.assertEqual(bounded['planning']['planning_time'], 1.)
        self.assertTrue(selection['fallback_used'])
        diagnostics = trajectory['diagnostics']['configuration_selection']
        self.assertEqual(diagnostics['bypass_attempts'], 1)
        self.assertEqual(diagnostics['endpoint_rejected_count'], 2)
        self.assertEqual(diagnostics['untested_direct_count'], 0)
        self.assertEqual(diagnostics['candidates_pruned_by_lower_bound'], 1)
        self.assertTrue(diagnostics['candidate_timings'])
        self.assertTrue(any('direct_elapsed_s' in item for item in diagnostics['candidate_timings']))
        self.assertTrue(any('coarse_path_check_s' in item and 'fine_direct_plan_s' in item
                            for item in diagnostics['candidate_timings']))
        self.assertTrue(any('elapsed_s' in item and 'budget_s' in item
                            for item in diagnostics['candidate_timings']))
        self.assertLess(self.clock, 5.)

    def test_failed_early_bypass_is_retried_with_remaining_budget(self):
        def blocked(path, request, context):
            self.clock += .1
            raise CollisionPlanningError('blocked')
        self.executor.direct_planner.plan.side_effect = blocked
        self.path['candidate_goals'] = self.path['candidate_goals'][:2]
        def failed_detour(path, request, context):
            self.clock += request['planning']['planning_time']
            raise PlanningError('budget ended', code='PATH_NOT_FOUND')
        self.executor.collision_free_planner.plan.side_effect = failed_detour
        with self.assertRaises(PlanningError) as caught:
            self.search()
        self.assertEqual(self.executor.direct_planner.plan.call_count, 1)
        self.assertEqual(self.executor.collision_free_planner.plan.call_count, 2)
        details = caught.exception.details
        self.assertEqual(details['bypass_attempts'], 2)
        self.assertTrue(details['budget_exhausted'])
        self.assertEqual(details['planning_elapsed_s'], 5.)
        self.assertEqual(details['planning_time_budget_s'], 5.)
        self.assertTrue(details['candidate_timings'])

    def test_near_detour_prunes_expensive_distant_direct_candidates(self):
        trajectory = self.search()
        self.executor.direct_planner.plan.assert_called_once()
        self.executor.collision_free_planner.plan.assert_called_once()
        diagnostics = trajectory['diagnostics']['configuration_selection']
        self.assertEqual(diagnostics['candidates_pruned_by_lower_bound'], 3)
        direct = next(t for t in diagnostics['candidate_timings'] if 'direct_elapsed_s' in t)
        self.assertEqual(direct['direct_elapsed_s'], 3.)

    def test_invalid_endpoints_never_receive_path_search_budget(self):
        self.executor.collision_checker.check.return_value = SimpleNamespace(in_collision=True, contacts=())
        with self.assertRaises(PlanningError) as caught:
            self.search()
        self.executor.direct_planner.plan.assert_not_called()
        self.executor.collision_free_planner.plan.assert_not_called()
        self.assertEqual(caught.exception.details['endpoint_rejected_count'], 4)
        self.assertFalse(caught.exception.details['budget_exhausted'])

    def test_coarse_collision_skips_fine_direct_validation(self):
        self.path['candidate_goals'] = self.path['candidate_goals'][:1]
        self.executor.collision_checker.check_path.return_value = SimpleNamespace(
            in_collision=True, contacts=({'geom1': 'gripper', 'geom2': 'Basket', 'distance': -.001},))
        trajectory = self.search()
        self.executor.direct_planner.plan.assert_not_called()
        self.executor.collision_free_planner.plan.assert_called_once()
        self.assertEqual(trajectory['diagnostics']['configuration_selection']['bypass_attempts'], 1)

    def test_coarse_success_still_requires_fine_execution_validation(self):
        self.path['candidate_goals'] = self.path['candidate_goals'][:1]
        self.executor.direct_planner.plan.side_effect = self.detour
        self.search()
        self.executor.collision_checker.check_path.assert_called_once()
        self.executor.direct_planner.plan.assert_called_once()
        self.executor.collision_free_planner.plan.assert_not_called()

    def test_fallback_respects_direct_only_and_hard_cartesian_requests(self):
        blocked = CollisionPlanningError('blocked')
        for mode, path_type, constraint, expected in (
                ('direct', 'joint', 'soft', 'COLLISION_DETECTED'),
                ('collision_free', 'linear', 'hard', 'PATH_CONSTRAINT_INFEASIBLE')):
            with self.subTest(mode=mode):
                request = deepcopy(self.request)
                request['planning']['mode'] = mode
                request['motion']['path_constraint'] = constraint
                with self.assertRaises(PlanningError) as caught:
                    self.executor._plan_collision_fallback(request, {}, {}, {'path_type': path_type}, blocked)
                self.assertEqual(caught.exception.code, expected)
        self.executor.collision_free_planner.plan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
