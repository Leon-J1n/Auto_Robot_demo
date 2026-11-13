import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from demo.interaction.object_library import ObjectLibraryGraspResolver


class CandidateCollisionShortCircuitTests(unittest.TestCase):
    def test_checks_solutions_by_cost_and_stops_at_first_collision_free_one(self):
        seed = np.zeros(6)
        farther = np.full(6, .4)
        nearer = np.full(6, .1)
        kin = SimpleNamespace(
            forward=Mock(return_value={'position': np.zeros(3)}),
            solve_all=Mock(return_value=SimpleNamespace(
                solutions=[farther, nearer], solver='test')))
        checker = SimpleNamespace(check=Mock(return_value=SimpleNamespace(
            in_collision=False, contacts=[])))
        control_pose = np.eye(4)
        resolved = {'candidates': [{'grasp_index': 0,
                                    'world_from_control': control_pose.tolist()}]}

        result = ObjectLibraryGraspResolver.select(resolved, kin, checker, seed)

        np.testing.assert_allclose(result['joint_positions'], nearer)
        self.assertEqual(checker.check.call_count, 1)
        np.testing.assert_allclose(checker.check.call_args.args[0], nearer)

    def test_continues_to_next_solution_when_lower_cost_solution_collides(self):
        seed = np.zeros(6)
        nearer = np.full(6, .1)
        farther = np.full(6, .4)
        kin = SimpleNamespace(
            forward=Mock(return_value={'position': np.zeros(3)}),
            solve_all=Mock(return_value=SimpleNamespace(
                solutions=[farther, nearer], solver='test')))
        checker = SimpleNamespace(check=Mock(side_effect=[
            SimpleNamespace(in_collision=True, contacts=['obstacle']),
            SimpleNamespace(in_collision=False, contacts=[])]))
        resolved = {'candidates': [{'grasp_index': 0,
                                    'world_from_control': np.eye(4).tolist()}]}

        result = ObjectLibraryGraspResolver.select(resolved, kin, checker, seed)

        np.testing.assert_allclose(result['joint_positions'], farther)
        self.assertEqual(checker.check.call_count, 2)
        self.assertEqual(result['rejected'][0]['reason'], 'goal_collision')


if __name__ == '__main__':
    unittest.main()
