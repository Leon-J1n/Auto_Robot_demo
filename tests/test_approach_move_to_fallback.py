"""Approach may move directly to its selected interaction pose after planning failure."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.runner import TaskSkillExecutor


class ApproachMoveToFallbackTests(unittest.TestCase):
    def setUp(self):
        self.executor = e = TaskSkillExecutor.__new__(TaskSkillExecutor)
        e.resources = SimpleNamespace(scene={'move_defaults': {
            'constraints': {'avoid_collision': True, 'safety_distance': .003}}})
        e.backend = SimpleNamespace(physics_steps=0, set_frame_callback=Mock())
        e.kinematics = SimpleNamespace()
        e.checker = SimpleNamespace()
        e.sync = SimpleNamespace(sync=Mock())
        e.write_frame = Mock()
        self.goal = np.eye(4)
        self.goal[:3, :3] = np.array([[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])
        self.goal[:3, 3] = [.2, -.1, .7]
        calibration = np.eye(4)
        calibration[2, 3] = .145
        self.resolved = {'selected': {'world_from_control': self.goal.tolist(),
                                     'pose_source': 'vlm_rgbd_place'},
                         'control_from_grasp': calibration.tolist()}
        e.resolver = SimpleNamespace(resolve=Mock(return_value={'target_id': 12}),
            select=Mock(return_value=self.resolved), remember_selection=Mock())
        self.failure = {'success': False, 'error': {
            'error_code': 'PATH_NOT_FOUND', 'failed_stage': 'planning',
            'error_message': 'No collision-free path'}}
        e.execute_motion = Mock(side_effect=[deepcopy(self.failure) for _ in range(17)] + [{'success': True}])
        self.command = {'command_id': '6', 'skill_name': 'approach', 'parameters': {'target': 12}}

    def run_approach(self):
        return self.executor._execute_pose_search(self.command, {'joints': [0.] * 6}, {})

    def test_exhausted_distances_move_to_same_place_pose_and_preserve_history(self):
        original = deepcopy(self.command)
        result = self.run_approach()
        self.assertTrue(result['success'])
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['substeps']), 18)
        self.assertEqual(result['substeps'][-2]['phase'], 'approach_approach_20mm')
        final = result['substeps'][-1]
        self.assertEqual(final['phase'], 'approach_move_to')
        self.assertEqual(final['approach_search_error'], self.failure['error'])
        request = final['request']
        np.testing.assert_allclose(list(request['target']['position'].values()), self.goal[:3, 3])
        self.assertEqual(request['motion']['path_type'], 'joint')
        self.assertEqual(request['planning']['mode'], 'collision_free')
        self.assertTrue(request['constraints']['avoid_collision'])
        self.assertEqual(result['target_resolution']['selected']['pose_source'], 'vlm_rgbd_place')
        self.executor.resolver.resolve.assert_called_once()
        self.executor.resolver.select.assert_called_once()
        self.executor.resolver.remember_selection.assert_called_once()
        np.testing.assert_allclose(self.executor._interaction_history['12']['world_from_control'], self.goal)
        self.assertEqual(self.command, original)

    def test_successful_approach_never_attempts_direct_target(self):
        self.executor.execute_motion.side_effect = [{'success': True}]
        result = self.run_approach()
        self.assertTrue(result['success'])
        self.executor.execute_motion.assert_called_once()
        self.assertNotIn('fallback_from', result['substeps'][0])

    def test_direct_target_failure_is_reported_and_original_attempts_are_retained(self):
        failure = {'success': False, 'error': {'error_code': 'TARGET_POSE_COLLISION',
                   'failed_stage': 'planning', 'error_message': 'Target collides'}}
        self.executor.execute_motion.side_effect = [deepcopy(self.failure) for _ in range(17)] + [failure]
        result = self.run_approach()
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], failure['error'])
        self.assertEqual(len(result['substeps']), 18)
        self.assertEqual(result['substeps'][-1]['approach_search_error'], self.failure['error'])
        self.assertFalse(getattr(self.executor, '_interaction_history', {}))

    def test_execution_failure_or_started_motion_never_retries(self):
        for stage, steps, motion_executed in (('execution', 0, False), ('planning', 1, False),
                                               ('planning', 0, True)):
            with self.subTest(stage=stage, steps=steps, motion_executed=motion_executed):
                self.setUp()
                def fail(request):
                    self.executor.backend.physics_steps += steps
                    return {'success': False, 'motion_executed': motion_executed,
                            'error': {'error_code': 'MOVE_FAILED', 'failed_stage': stage}}
                self.executor.execute_motion.side_effect = fail
                self.assertFalse(self.run_approach()['success'])
                self.executor.execute_motion.assert_called_once()

    def test_invalid_search_does_not_move(self):
        self.command['parameters']['search'] = {'step': 0.}
        result = self.run_approach()
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'INVALID_APPROACH_REQUEST')
        self.executor.execute_motion.assert_not_called()


if __name__ == '__main__':
    unittest.main()
