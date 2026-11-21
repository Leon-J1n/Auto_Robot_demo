"""Contact distances must exhaust analytic paths before numerical fallback."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from kinematics import IKError
from kinematics.core.contracts import IKResult
from planning.planners.interaction_planners import PickContactPlanner


class ContactIKSearchTests(unittest.TestCase):
    def run_search(self, analytic_success=(), linear_failure=(), names=None):
        calls = []

        class Robot:
            limits = np.tile([-4., 4.], (6, 1))
            joint_periods = np.full(6, 2*np.pi)

            def automatic_backends(self):
                return ['ssik', 'jacobian'] if names is None else names

            def solve_all(self, pose, **kwargs):
                route = kwargs.get('backends', ['configured'])
                route = [route[-1]]
                distance = round(float(pose['position'][2]), 2)
                calls.append(('point', route[0], distance))
                if route[0] == 'ssik' and distance not in analytic_success:
                    raise IKError('No analytic branch')
                return IKResult(solutions=[np.array([distance, 0., 0., 0., 0., 0.])])

            def solve_one(self, pose, **kwargs):
                route = kwargs.get('backends', ['configured'])
                route = [route[-1]]
                distance = round(float(pose['position'][2]), 2)
                calls.append(('segment', route[0], distance))
                if route[0] == 'ssik' and distance in linear_failure:
                    raise IKError('No continuous analytic branch')
                return IKResult(solutions=[np.zeros(6)])

            def forward(self, q):
                return {'position': np.zeros(3), 'rotation_matrix': np.eye(3)}

        class Linear:
            def __init__(self, *, kinematics, collision_checker):
                self.kin = kinematics

            def build_path_request(self, request, context):
                self.kin.solve_one({'position': context['current_pose']['position']}, backend='auto')
                return {}

        kin = Robot()
        checker = SimpleNamespace(check=lambda *a, **kw: SimpleNamespace(in_collision=False))
        planner = PickContactPlanner(kinematics=kin, collision_checker=checker, joint_limits=kin.limits)
        request = {'motion': {}, 'constraints': {'safety_distance': .003}, 'planning': {}}
        with patch('planning.planners.interaction_planners.LinearMove', Linear), \
             patch('planning.planners.interaction_planners.configuration_cost', return_value=0.), \
             patch.object(planner.direct, 'plan', return_value={}):
            selected, rejected = planner.plan(request, {'joints': np.zeros(6)}, goal=np.eye(4),
                direction=[0, 0, -1], requested_path='joint',
                distances=iter([.1, .07, .04]), pose_request=lambda pose: {})
        return selected, rejected, calls

    def test_shorter_analytic_candidate_precedes_numerical(self):
        selected, rejected, calls = self.run_search(analytic_success=[.07])
        self.assertEqual(selected['distance'], .07)
        self.assertEqual(selected['ik_search_pass'], 'analytic')
        self.assertEqual(calls, [('point', 'ssik', .1), ('point', 'ssik', .07), ('segment', 'ssik', .07)])
        self.assertEqual(rejected[0]['ik_search_pass'], 'analytic')

    def test_whole_analytic_pass_precedes_numerical_and_reuses_distances(self):
        selected, _, calls = self.run_search()
        self.assertEqual(selected['ik_search_pass'], 'numerical_fallback')
        self.assertEqual(calls, [('point', 'ssik', .1), ('point', 'ssik', .07), ('point', 'ssik', .04),
                                 ('point', 'jacobian', .1), ('segment', 'jacobian', .1)])

    def test_segment_failure_does_not_trigger_early_numerical_fallback(self):
        selected, _, calls = self.run_search(analytic_success=[.1, .07], linear_failure=[.1])
        self.assertEqual(selected['distance'], .07)
        self.assertTrue(all(call[1] == 'ssik' for call in calls))

    def test_numerical_only_robot_keeps_working(self):
        selected, _, calls = self.run_search(names=['jacobian'])
        self.assertEqual(selected['ik_search_pass'], 'numerical_fallback')
        self.assertEqual(calls[0], ('point', 'jacobian', .1))


if __name__ == '__main__':
    unittest.main()
