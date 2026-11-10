import unittest

import numpy as np

from demo.interaction.visual_targets import infer_command_point_types, infer_visual_targets
from execution.blackboard import TargetBlackboard


def command(skill, target):
    return {'skill_name': skill, 'parameters': {'target': target}}


class VisualTargetInferenceTests(unittest.TestCase):
    def test_scan_uses_future_pick_and_put_and_deduplicates(self):
        targets = infer_visual_targets([
            command('approach', 7), command('pick', 7), command('retreat', 7),
            command('move_to', 'home'), command('approach', 12), command('put', 12),
            command('pick', 7),
        ])
        self.assertEqual(targets, [
            {'target_id': 7, 'point_type': 'grasp', 'camera_id': 'global'},
            {'target_id': 12, 'point_type': 'place', 'camera_id': 'global'},
        ])

    def test_same_target_uses_phase_specific_approach_and_retreat(self):
        commands = [
            {'command_id': '1', **command('approach', 12)},
            {'command_id': '2', **command('put', 12)},
            {'command_id': '3', **command('retreat', 12)},
            {'command_id': '4', **command('approach', 12)},
            {'command_id': '5', **command('pick', 12)},
            {'command_id': '6', **command('retreat', 12)},
        ]
        types = infer_command_point_types(commands)
        self.assertEqual(types, {
            '1': 'place', '2': 'place', '3': 'place',
            '4': 'grasp', '5': 'grasp', '6': 'grasp',
        })
        self.assertEqual(infer_visual_targets(commands), [
            {'target_id': 12, 'point_type': 'place', 'camera_id': 'global'},
            {'target_id': 12, 'point_type': 'grasp', 'camera_id': 'global'},
        ])

    def test_other_commands_do_not_request_visual_points(self):
        self.assertEqual(infer_visual_targets([
            command('move_to', 7), command('approach', 7), command('grasp', 7),
            command('release', 7), command('put_down', None),
        ]), [])

    def test_conflicting_point_types_are_reported(self):
        self.assertEqual(infer_visual_targets([command('pick', 7), command('put', '7')]), [
            {'target_id': 7, 'point_type': 'grasp', 'camera_id': 'global'},
            {'target_id': '7', 'point_type': 'place', 'camera_id': 'global'},
        ])

    def test_cached_grasp_and_place_follow_latest_target_transform(self):
        board = TargetBlackboard()
        board.observe({'simulation_time': 1., 'objects': {'Tray': {
            'position': [1., 2., 3.], 'quaternion_xyzw': [0., 0., 0., 1.]}}})
        grasp = np.eye(4)
        grasp[:3, 3] = [1.1, 2., 3.]
        place = np.eye(4)
        place[:3, 3] = [1., 2.2, 3.]
        board.remember_visual_grasp('Tray', grasp, {'estimate': 'grasp'})
        board.remember_visual_place('Tray', place, {'estimate': 'place'})

        rotation = [0., 0., np.sqrt(.5), np.sqrt(.5)]
        board.observe({'simulation_time': 2., 'objects': {'Tray': {
            'position': [4., 5., 6.], 'quaternion_xyzw': rotation}}})
        np.testing.assert_allclose(board.visual_grasp('Tray')[:3, 3], [4., 5.1, 6.])
        np.testing.assert_allclose(board.visual_place('Tray')[:3, 3], [3.8, 5., 6.])
        self.assertEqual(board.visual_grasp_evidence('Tray'), {'estimate': 'grasp'})
        self.assertEqual(board.visual_place_evidence('Tray'), {'estimate': 'place'})

    def test_cached_point_can_follow_an_articulated_link(self):
        board = TargetBlackboard()
        board.observe({'simulation_time': 1., 'objects': {
            'Cabinet': {'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
            'Cabinet__door': {'position': [1., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
        }})
        grasp = np.eye(4)
        grasp[:3, 3] = [1., 1., 0.]
        board.remember_visual_grasp('Cabinet', grasp, {}, anchor_frame='Cabinet__door')
        board.observe({'simulation_time': 2., 'objects': {
            'Cabinet': {'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
            'Cabinet__door': {'position': [2., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]},
        }})
        np.testing.assert_allclose(board.visual_grasp('Cabinet')[:3, 3], [2., 1., 0.])


if __name__ == '__main__':
    unittest.main()
