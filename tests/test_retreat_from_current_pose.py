"""Retreat starts at the live TCP but keeps the original interaction axis."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.runner import TaskSkillExecutor


class RetreatFromCurrentPoseTests(unittest.TestCase):
    def setUp(self):
        self.executor = e = TaskSkillExecutor.__new__(TaskSkillExecutor)
        e.resources = SimpleNamespace(scene={'move_defaults': {
            'constraints': {'avoid_collision': True, 'safety_distance': .003}}})
        e.resolver = SimpleNamespace(
            resolve=Mock(side_effect=AssertionError('Cached grasp pose is not a retreat goal')),
            select=Mock(side_effect=AssertionError('Do not solve IK for the old grasp pose')),
            remember_selection=Mock(side_effect=AssertionError('Do not overwrite cached grasp')))
        e.backend = SimpleNamespace(physics_steps=0, set_frame_callback=Mock(),
            get_end_effector_pose=Mock(return_value={
                'position': np.array([.44, -.20, .9]),
                'rotation_matrix': np.diag([1., -1., -1.])}))
        e.sync = SimpleNamespace(sync=Mock())
        e.kinematics = SimpleNamespace()
        e.checker = SimpleNamespace(allow_gripper_environment_contact=False)
        e.execute_motion = Mock(return_value={'success': True})
        e.write_frame = Mock()
        self.calibration = np.eye(4)
        self.calibration[2, 3] = .145
        self.command = {'command_id': '12', 'skill_name': 'retreat', 'parameters': {'target': 8}}
        self.reference = np.eye(4)
        self.reference[:3, :3] = np.diag([1., -1., -1.])
        self.reference[:3, 3] = [.44, -.20, .885]
        self.remember('pick')

    def remember(self, skill):
        self.executor._remember_interaction(
            {'command_id': '11', 'skill_name': skill, 'parameters': {'target': 8}},
            {'selected': {'world_from_control': self.reference},
             'control_from_grasp': self.calibration})

    def run_retreat(self):
        return self.executor._execute_pose_search(self.command, {'joints': [0.] * 6}, {})

    def test_plan_uses_lifted_tcp_and_preserves_collision_checking(self):
        e = self.executor
        def motion(request):
            self.assertTrue(e.checker.allow_gripper_environment_contact)
            self.assertTrue(request['constraints']['avoid_collision'])
            self.assertEqual(request['motion']['path_constraint'], 'hard')
            np.testing.assert_allclose(list(request['target']['position'].values()), [.44, -.20, 1.0])
            return {'success': True}
        e.execute_motion.side_effect = motion
        result = self.run_retreat()
        self.assertTrue(result['success'])
        self.assertNotIn('target_resolution', result)
        self.assertEqual(result['retreat_reference']['target_id'], 8)
        self.assertEqual(result['retreat_reference']['skill_name'], 'pick')
        e.execute_motion.assert_called_once()
        e.resolver.resolve.assert_not_called()
        e.resolver.select.assert_not_called()
        e.resolver.remember_selection.assert_not_called()
        self.assertFalse(e.checker.allow_gripper_environment_contact)
        self.assertEqual(self.command['parameters'], {'target': 8})

    def test_retreat_prefers_previous_successful_target_distance_and_validates_again(self):
        self.executor._pose_search_distances = {'8': .09}
        self.assertTrue(self.run_retreat()['success'])
        request = self.executor.execute_motion.call_args.args[0]
        np.testing.assert_allclose(list(request['target']['position'].values()), [.44, -.20, .99])
        self.assertEqual(request['motion']['path_constraint'], 'hard')
        self.assertTrue(request['constraints']['avoid_collision'])
        self.assertEqual(self.executor._pose_search_distances['8'], .09)

    def test_direction_uses_original_frame_even_when_current_tcp_has_rotated(self):
        current_rotation = np.eye(3)
        self.executor.backend.get_end_effector_pose.return_value['rotation_matrix'] = current_rotation
        self.assertTrue(self.run_retreat()['success'])
        goal = self.executor.execute_motion.call_args.args[0]['target']
        np.testing.assert_allclose(list(goal['position'].values()), [.44, -.20, 1.0])
        from scipy.spatial.transform import Rotation
        quat = [goal['orientation'][key] for key in 'xyzw']
        np.testing.assert_allclose(Rotation.from_quat(quat).as_matrix(), current_rotation)

    def test_oblique_original_frame_moves_from_current_position_not_original_point(self):
        from scipy.spatial.transform import Rotation
        self.reference[:3, :3] = Rotation.from_euler('y', 45, degrees=True).as_matrix()
        self.remember('pick')
        self.command['parameters']['search'] = {'min_distance': .06, 'max_distance': .06, 'step': .005}
        self.assertTrue(self.run_retreat()['success'])
        goal = self.executor.execute_motion.call_args.args[0]['target']
        expected = np.array([.44, -.20, .9]) - .06 * self.reference[:3, 2]
        np.testing.assert_allclose(list(goal['position'].values()), expected)

    def test_same_target_uses_latest_interaction_and_frozen_snapshot(self):
        self.reference[:3, :3] = np.eye(3)
        self.remember('put')
        self.reference[:3, :3] = np.diag([1., -1., -1.])
        self.calibration[2, 3] = -.145
        result = self.run_retreat()
        self.assertTrue(result['success'])
        self.assertEqual(result['retreat_reference']['skill_name'], 'put')
        goal = self.executor.execute_motion.call_args.args[0]['target']
        np.testing.assert_allclose(list(goal['position'].values()), [.44, -.20, .8])
        self.calibration[2, 3] = .145
        self.remember('pick')
        result = self.run_retreat()
        self.assertEqual(result['retreat_reference']['skill_name'], 'pick')
        goal = self.executor.execute_motion.call_args.args[0]['target']
        np.testing.assert_allclose(list(goal['position'].values()), [.44, -.20, 1.0])

    def test_explicit_axis_is_expressed_in_original_interaction_frame(self):
        self.command['parameters']['pose_axis'] = '+x'
        self.assertTrue(self.run_retreat()['success'])
        goal = self.executor.execute_motion.call_args.args[0]['target']
        np.testing.assert_allclose(list(goal['position'].values()), [.34, -.20, .9])

    def test_missing_target_or_reference_fails_without_perception_or_motion(self):
        for parameters in ({}, {'target': 99}):
            with self.subTest(parameters=parameters):
                self.command['parameters'] = parameters
                result = self.run_retreat()
                self.assertFalse(result['success'])
                self.assertEqual(result['error']['error_code'], 'RETREAT_REFERENCE_MISSING')
                self.assertIn('无法确定撤退方向', result['error']['error_message'])
                self.executor.execute_motion.assert_not_called()
                self.executor.resolver.resolve.assert_not_called()
                self.assertFalse(self.executor.checker.allow_gripper_environment_contact)

    def test_actual_retreat_path_failures_still_fail_after_distance_search(self):
        e = self.executor
        e.execute_motion.return_value = {'success': False, 'error': {
            'error_code': 'PATH_CONSTRAINT_INFEASIBLE', 'failed_stage': 'planning',
            'error_message': 'Actual retreat path collides'}}
        result = self.run_retreat()
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'PATH_CONSTRAINT_INFEASIBLE')
        self.assertEqual(e.execute_motion.call_count, 15)
        e.resolver.select.assert_not_called()
        self.assertFalse(e.checker.allow_gripper_environment_contact)


if __name__ == '__main__':
    unittest.main()
