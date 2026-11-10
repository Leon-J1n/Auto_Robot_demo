"""Regression for pad/payload contact after release and during settling."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.runner import TaskSkillExecutor


class PutReleaseSettlingTests(unittest.TestCase):
    def setUp(self):
        self.command = {'command_id': '7', 'skill_name': 'put', 'parameters': {'target': 12}}
        self.frame = {'joints': [0.] * 6}
        self.pose = {'position': np.zeros(3), 'rotation_matrix': np.eye(3)}
        self.executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        e = self.executor
        e.resources = SimpleNamespace(scene={'move_defaults': {}})
        e.report = {}
        e.resolver = SimpleNamespace(set_command_context=Mock(), is_visual_place=lambda target: True)
        e.kinematics = SimpleNamespace(forward=lambda q: self.pose)
        e.sync = SimpleNamespace(sync=Mock())
        e.write_frame = Mock()
        e.converter = SimpleNamespace(convert_command=lambda *args: {
            'request': {'target': {'type': 'object', 'target_id': 12}}})
        e.backend = SimpleNamespace(gripper_controller=SimpleNamespace(held_name='Apple', mode='physical'),
            set_status=Mock(), physics_steps=0, get_end_effector_pose=lambda: self.pose,
            grasp_approach_goal=None, frame_callback=None)
        e.backend.set_frame_callback = lambda callback: setattr(e.backend, 'frame_callback', callback)
        self.contacts = [{'geom1': 'robotiq_2f85_left_pad', 'geom2': 'Apple',
                          'distance': -1.206994744353386e-6, 'self_collision': False}]
        e.checker = SimpleNamespace(support_geoms=set(), carrying_safety_distance=0.,
            check=Mock(side_effect=AssertionError('Execution must not check collisions')),
            check_path=Mock(side_effect=AssertionError('No collision recheck after execution')))
        target = np.eye(4).tolist()
        e.adapters = {'put': SimpleNamespace(prepare=lambda *args: (
            [{'phase': 'contact', 'request': {'constraints': {}}}],
            {'selected': {'world_from_grasp': target}, 'control_from_grasp': target}))}
        def motion(request):
            e.backend.frame_callback(self.frame)
            return {'success': True, 'trajectory': {'waypoints': [[0.] * 6]}}
        e.execute_motion = motion
        def release(request):
            e.backend.gripper_controller.held_name = None
            # Real physics and recording continue throughout release.
            e.backend.frame_callback(self.frame)
            return {'success': True, 'release_result': {'holding': False}}
        e.releaser = SimpleNamespace(execute=release)
        e.backend.advance_gripper = Mock(side_effect=lambda seconds: e.backend.frame_callback(self.frame))

    def run_put(self):
        return self.executor._execute_command(self.command, 7, self.frame, {})

    def test_intended_payload_contact_is_allowed_during_opening_and_settling(self):
        result = self.run_put()
        self.assertTrue(result['success'])
        self.assertTrue(result['release_result']['success'])
        self.assertEqual(result['release_settle_time_s'], .25)
        self.executor.backend.advance_gripper.assert_called_once_with(.25)
        self.executor.backend.frame_callback(self.frame)
        self.executor.checker.check.assert_not_called()
        self.executor.checker.check_path.assert_not_called()
        self.assertIsNone(result['actual_path_collision_free'])
        self.assertEqual(result['collision_policy'], 'planning_only')
        snapshot = self.executor._interaction_history['12']
        self.assertEqual(snapshot['skill_name'], 'put')
        self.assertEqual(snapshot['command_id'], '7')
        np.testing.assert_allclose(snapshot['world_from_control'], np.eye(4))

    def test_environment_contacts_do_not_interrupt_release_or_settling(self):
        for other in ('Basket', 'WorkTable__work_table', 'Banana'):
            with self.subTest(other=other):
                self.setUp()
                def settle(seconds):
                    self.contacts.append({'geom1': 'robotiq_2f85_right_pad', 'geom2': other,
                                          'distance': -.001, 'self_collision': False})
                    self.executor.backend.frame_callback(self.frame)
                self.executor.backend.advance_gripper.side_effect = settle
                result = self.run_put()
                self.assertTrue(result['success'])
                self.executor.backend.frame_callback(self.frame)
                self.executor.checker.check.assert_not_called()
                self.executor.checker.check_path.assert_not_called()

    def add_joint_fallback(self):
        e = self.executor
        prepare = e.adapters['put'].prepare
        def parts_with_fallback(*args):
            parts, resolved = prepare(*args)
            parts[0]['direct_pick_fallback'] = {'constraints': {}}
            resolved['approach'] = {}
            return parts, resolved
        e.adapters['put'].prepare = parts_with_fallback
        e.releaser.execute = Mock(wraps=e.releaser.execute)

    def test_put_releases_only_after_successful_joint_fallback(self):
        self.add_joint_fallback()
        self.executor.execute_motion = Mock(side_effect=[
            {'success': False, 'error': {'error_code': 'COLLISION_DETECTED', 'failed_stage': 'planning'}},
            {'success': True, 'trajectory': {'waypoints': [[0.] * 6]}}])
        result = self.run_put()
        self.assertTrue(result['success'])
        self.assertEqual(self.executor.execute_motion.call_count, 2)
        self.executor.releaser.execute.assert_called_once()
        self.assertEqual(len(result['substeps']), 2)

    def test_put_does_not_retry_or_release_after_motion_has_started(self):
        for stage, moved in (('execution', False), ('planning', True)):
            with self.subTest(stage=stage, moved=moved):
                self.setUp()
                self.add_joint_fallback()
                self.executor.execute_motion = Mock(return_value={
                    'success': False, 'motion_executed': moved,
                    'error': {'error_code': 'COLLISION_DETECTED', 'failed_stage': stage}})
                result = self.run_put()
                self.assertFalse(result['success'])
                self.executor.execute_motion.assert_called_once()
                self.executor.releaser.execute.assert_not_called()
                self.assertNotIn('12', getattr(self.executor, '_interaction_history', {}))


if __name__ == '__main__':
    unittest.main()
