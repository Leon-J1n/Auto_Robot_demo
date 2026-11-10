"""Execute approved trajectories without runtime geometric collision rejection."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.runner import TaskSkillExecutor
from atomic_skill.pull import PullMotionExecutor
from atomic_skill.retreat import RetreatSkill
from demo.interaction.closure_validation import execute_checked_grasp


def forbidden_checks():
    return SimpleNamespace(carrying_safety_distance=0., support_geoms=set(),
        check=Mock(side_effect=AssertionError('Unexpected execution collision check')),
        check_path=Mock(side_effect=AssertionError('Unexpected executed-path recheck')))


class PlanningOnlyCollisionTests(unittest.TestCase):
    def test_common_motion_records_and_syncs_without_checking_executed_path(self):
        for skill in ('move_to', 'lift_up', 'put_down', 'shift_down'):
            with self.subTest(skill=skill):
                e = TaskSkillExecutor.__new__(TaskSkillExecutor)
                frame = {'joints': [0.] * 6}
                pose = {'position': np.zeros(3), 'rotation_matrix': np.eye(3)}
                e.resources = SimpleNamespace(scene={'move_defaults': {}})
                e.report = {}
                e.resolver = None
                e.checker = forbidden_checks()
                e.sync = SimpleNamespace(sync=Mock())
                e.kinematics = SimpleNamespace(forward=lambda q: pose)
                e.write_frame = Mock()
                e.backend = SimpleNamespace(physics_steps=0, grasp_approach_goal=None,
                    gripper_controller=SimpleNamespace(held_name=None), set_status=Mock(),
                    get_end_effector_pose=lambda: pose)
                e.backend.set_frame_callback = lambda cb: setattr(e.backend, 'frame_callback', cb)
                e.converter = SimpleNamespace(convert_command=lambda *args: {
                    'request': {'target': {'type': 'joint', 'joint_positions': frame['joints']}}})
                def motion(request):
                    e.backend.physics_steps += 1
                    e.backend.frame_callback(frame)
                    return {'success': True, 'trajectory': {'waypoints': [frame['joints']]}}
                e.execute_motion = motion
                result = e._execute_command({'command_id': '1', 'skill_name': skill,
                    'parameters': {}}, 1, frame, {})
                self.assertTrue(result['success'])
                self.assertIsNone(result['actual_path_collision_free'])
                e.checker.check.assert_not_called()
                e.checker.check_path.assert_not_called()
                e.write_frame.assert_called_once()
                self.assertGreaterEqual(e.sync.sync.call_count, 2)

    def test_open_and_close_preserve_force_failure_and_recording(self):
        checker = forbidden_checks()
        recorder = Mock()
        backend = SimpleNamespace(frame_callback=recorder)
        failure = {'success': False, 'error': {'error_code': 'GRASP_NOT_CONFIRMED'}}
        def grasp(request):
            backend.frame_callback({'joints': [0.] * 6})
            return dict(failure)
        result = execute_checked_grasp(SimpleNamespace(execute=grasp), {}, backend, checker)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], failure['error'])
        recorder.assert_called_once()
        self.assertIs(backend.frame_callback, recorder)
        checker.check.assert_not_called()

    def test_push_pull_transit_preserves_request_and_callback_without_collision_checks(self):
        e = PullMotionExecutor.__new__(PullMotionExecutor)
        e.params = {}
        e.backend = SimpleNamespace(set_status=Mock())
        e.backend.set_frame_callback = lambda cb: setattr(e.backend, 'frame_callback', cb)
        e.callback = Mock()
        e.name = 'target'
        e.phases = []
        e.checker = forbidden_checks()
        e.kinematics = None
        e.sync = SimpleNamespace(sync=Mock())
        request = {'planning': {'mode': 'collision_free'}, 'constraints': {'avoid_collision': True}}
        def motion(received):
            self.assertEqual(received, request)
            e.backend.frame_callback({'joints': [0.] * 6})
            return {'success': True}
        e.execute_motion = motion
        self.assertTrue(e._move('move', resolved_request=request))
        e.callback.assert_called_once()
        self.assertIs(e.backend.frame_callback, e.callback)
        e.checker.check.assert_not_called()

    def test_retreat_does_not_reject_successful_motion_with_post_execution_clearance_check(self):
        executor = SimpleNamespace(params={}, phases=[],
            backend=SimpleNamespace(physics_steps=0, get_end_effector_pose=lambda: {
                'position': np.zeros(3), 'rotation_matrix': np.eye(3)}),
            _move=Mock(return_value=True),
            _retreat_clearance_met=Mock(side_effect=AssertionError('No post-motion collision check')))
        self.assertTrue(RetreatSkill(executor).execute())
        executor._move.assert_called_once()
        executor._retreat_clearance_met.assert_not_called()



if __name__ == '__main__':
    unittest.main()
