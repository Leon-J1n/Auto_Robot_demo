"""Host-side contract tests for the cuRobo avoidance fallback."""

from copy import deepcopy
from types import SimpleNamespace
import unittest

import numpy as np

from atomic_action.motion.common.action import _MoveExecutor
from planning import CollisionFreePlanner as DefaultPlanner
from planning.planners.curobo.collision_free import CollisionFreePlanner
from planning.common.errors import CollisionPlanningError, PlanningError
from planning.planners.collision_free_planner import CollisionFreePlanner as LegacyImport
from planning.trajectory.prepared import PreparedTrajectory


class _Checker:
    def __init__(self):
        self.path_collision = False
        self.path_checks = []

    def check(self, joints, *, safety_distance):
        return SimpleNamespace(in_collision=False, contacts=[])

    def check_path(self, points, *, resolution, safety_distance):
        self.path_checks.append((np.asarray(points), resolution, safety_distance))
        return SimpleNamespace(in_collision=self.path_collision, contacts=["blocked"] if self.path_collision else [])


class _Curobo:
    def __init__(self, start, goal):
        self.start = start
        self.goal = goal
        self.requests = []

    def plan(self, request):
        self.requests.append(deepcopy(request))
        points = np.vstack((self.start, (self.start + self.goal) / 2, self.goal))
        return {"success": True, "timing": {"planning": 0.1}, "trajectory": {
            "joint_names": [f"joint_{i}" for i in range(6)],
            "waypoints": points.tolist(), "duration": 1.0,
            "planning_time": 0.1, "diagnostics": {"backend": "curobo"},
        }}


class CuroboAvoidanceTests(unittest.TestCase):
    def test_default_import_resolves_to_rrt(self):
        self.assertIs(LegacyImport, DefaultPlanner)
        self.assertIsNot(DefaultPlanner, CollisionFreePlanner)
        self.assertEqual(DefaultPlanner.planner_id, "rrt")

    def setUp(self):
        self.start = np.zeros(6)
        self.goal = np.full(6, 0.3)
        self.checker = _Checker()
        self.curobo = _Curobo(self.start, self.goal)
        self.runtime = SimpleNamespace(joint_names=[f"joint_{i}" for i in range(6)])
        self.planner = CollisionFreePlanner(
            collision_checker=self.checker, joint_limits=[[-1, 1]] * 6,
            robot_runtime=self.runtime, curobo_planner=self.curobo,
        )
        self.path = {"path_type": "joint", "start": self.start, "goal": self.goal}
        self.request = {
            "target": {"type": "pose", "position": {"x": 0, "y": 0, "z": 0}},
            "motion": {"path_type": "joint", "path_constraint": "soft"},
            "planning": {"mode": "auto", "allow_replan": True, "planner_type": "auto"},
            "constraints": {"safety_distance": 0.02, "keep_end_effector_orientation": False},
        }

    def test_direct_collision_uses_curobo_and_validates_path(self):
        action = _MoveExecutor.__new__(_MoveExecutor)
        action.collision_free_planner = self.planner
        action.direct_planner = SimpleNamespace(plan=lambda *args: (_ for _ in ()).throw(
            CollisionPlanningError("blocked")))
        selection = {"selection_reason": "joint move", "fallback_used": False}
        trajectory = action._plan_with_fallback(self.request, {}, selection, self.path)

        self.assertEqual(trajectory["planner"], "curobo")
        self.assertEqual(selection["planner_type"], "curobo")
        self.assertTrue(selection["fallback_used"])
        self.assertEqual(self.curobo.requests[0]["target"]["joint_positions"], self.goal.tolist())
        self.assertEqual(self.curobo.requests[0]["planning"]["planner_type"], "curobo")
        self.assertEqual(len(self.checker.path_checks), 1)
        self.assertEqual(self.checker.path_checks[0][1:], (0.01, 0.02))
        self.assertEqual(self.request["target"]["type"], "pose")

    def test_host_collision_rejects_curobo_trajectory(self):
        self.checker.path_collision = True
        with self.assertRaises(PlanningError) as caught:
            self.planner.plan(self.path, self.request, {})
        self.assertEqual(caught.exception.code, "TRAJECTORY_INVALID")
        self.assertEqual(len(self.curobo.requests), 1)

    def test_diagnostics_include_initial_and_outer_host_validation(self):
        original = self.curobo.plan
        def with_timings(request):
            result = original(request)
            result['timing'].update(host_collision_validation=5.4, preparation=.17, conversion=.08)
            return result
        self.curobo.plan = with_timings
        trajectory = self.planner.plan(self.path, self.request, {})
        stages = trajectory['diagnostics']['planning_stages_s']
        self.assertEqual(stages['host_collision_validation_initial'], 5.4)
        self.assertAlmostEqual(stages['host_collision_validation_total'],
                               5.4 + stages['host_collision_validation'])
        self.assertEqual(stages['preparation'], .17)
        self.assertEqual(stages['conversion'], .08)

    def test_fixed_orientation_never_calls_curobo(self):
        constrained = {**self.path, "orientation_constraint": {"enabled": True}}
        with self.assertRaises(PlanningError) as caught:
            self.planner.plan(constrained, self.request, {})
        self.assertEqual(caught.exception.code, "PATH_CONSTRAINT_INFEASIBLE")
        self.assertEqual(self.curobo.requests, [])

    def test_curobo_failure_does_not_invoke_another_planner(self):
        self.curobo.plan = lambda request: {"success": False, "error": {
            "error_code": "CUROBO_FAILED", "error_message": "No feasible path",
        }}
        with self.assertRaises(PlanningError) as caught:
            self.planner.plan(self.path, self.request, {})
        self.assertEqual(caught.exception.code, "CUROBO_FAILED")
        self.assertEqual(self.checker.path_checks, [])

    def test_explicit_direct_mode_disallows_avoidance(self):
        action = _MoveExecutor.__new__(_MoveExecutor)
        action.collision_free_planner = self.planner
        direct_only = deepcopy(self.request)
        direct_only["planning"]["planner_type"] = "direct"
        with self.assertRaises(CollisionPlanningError):
            action._plan_collision_fallback(
                direct_only, {}, {}, self.path, CollisionPlanningError("blocked"),
            )
        self.assertEqual(self.curobo.requests, [])

    def test_prepared_rrt_path_is_not_reused(self):
        action = _MoveExecutor.__new__(_MoveExecutor)
        prepared = PreparedTrajectory(
            request=self.request, trajectory={"planner": "collision_free"},
            selection={}, state_signature="unused",
        )
        self.assertIsNone(action._reuse_prepared_trajectory(
            prepared, self.request, {}, {"path_strategy": "joint_move"},
        ))


if __name__ == "__main__":
    unittest.main()
