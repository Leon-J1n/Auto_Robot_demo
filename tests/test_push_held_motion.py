import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import mujoco

from atomic_skill.push import CommandPushExecutor
from atomic_skill.pull import CommandPullExecutor


class HeldMotionTest(unittest.TestCase):
    def make_executor(self, executor_type=CommandPushExecutor, holding=True, success=True):
        backend = SimpleNamespace(
            gripper_controller=SimpleNamespace(get_state=lambda: {'holding': holding}),
            get_end_effector_pose=lambda: {'position': [1, 0, 0], 'rotation_matrix': np.eye(3)},
            frame_callback=None, set_frame_callback=Mock(), set_status=Mock())
        resources = SimpleNamespace(joint_names=['j'] * 6, scene={'move_defaults': {
            'constraints': {}, 'planning': {'time_parameterization': {
                'max_velocity': [1] * 6, 'max_acceleration': [2] * 6}}}})
        motion = Mock(return_value={'success': success, **({} if success else {
            'error': {'error_code': 'IK_FAILED', 'failed_stage': 'kinematics'}})})
        releaser = SimpleNamespace(execute=Mock(return_value={'success': True}))
        executor = executor_type(resources=resources, backend=backend, kinematics=None,
            synchronizer=SimpleNamespace(sync=Mock()), execute_motion=motion,
            push_pull_skill=None, grasper=None, releaser=releaser,
            checker=SimpleNamespace(support_geoms=set(), carrying_safety_distance=0.0))
        return executor, motion, releaser.execute

    def test_linear_push_and_pull_use_same_action_and_release(self):
        for kind in (CommandPushExecutor, CommandPullExecutor):
            with self.subTest(kind=kind):
                executor, motion, release = self.make_executor(kind)
                result = executor.execute({'frame': 'world', 'distance': 0.1, 'direction': [0, 1, 0]})
                self.assertTrue(result['success'])
                request = motion.call_args.args[0]
                self.assertEqual(request['motion']['path_type'], 'linear')
                self.assertEqual(request['target']['position'], {'x': 1., 'y': .1, 'z': 0.})
                release.assert_called_once()

    def test_reverse_arc_needs_no_object_or_link(self):
        executor, motion, release = self.make_executor()
        result = executor.execute({'frame': 'world', 'arc_center': [0, 0, 0],
                                   'arc_axis': [0, 0, 1], 'angle_deg': -90})
        self.assertTrue(result['success'])
        request = motion.call_args.args[0]
        self.assertEqual(request['motion']['path_type'], 'circular')
        self.assertAlmostEqual(request['strategy_params']['arc_angle'], -np.pi / 2)
        np.testing.assert_allclose(list(request['target']['position'].values()), [0, -1, 0], atol=1e-12)
        self.assertEqual([s['phase'] for s in result['substeps']], ['push_motion', 'release'])
        release.assert_called_once()

    def test_omitted_direction_uses_operation_default_in_selected_frame(self):
        for kind, sign in ((CommandPushExecutor, 1), (CommandPullExecutor, -1)):
            for frame in ('tool', 'world', 'grasp'):
                with self.subTest(kind=kind, frame=frame):
                    executor, motion, release = self.make_executor(kind)
                    rotation = np.array([[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]])
                    executor.backend.get_end_effector_pose = lambda: {
                        'position': [1, 0, 0], 'rotation_matrix': rotation}
                    model = mujoco.MjModel.from_xml_string(
                        '<mujoco><worldbody><site name="grasp_center" euler="90 0 0"/></worldbody></mujoco>')
                    data = mujoco.MjData(model)
                    mujoco.mj_forward(model, data)
                    executor.backend.model, executor.backend.data = model, data
                    parameters = {'distance': .1}
                    if frame != 'tool':
                        parameters['frame'] = frame
                    result = executor.execute(parameters)
                    self.assertTrue(result['success'])
                    request = motion.call_args.args[0]
                    direction = np.array([0., 0., sign])
                    if frame != 'world':
                        direction = rotation @ direction
                    np.testing.assert_allclose(request['strategy_params']['direction'], direction, atol=1e-12)
                    np.testing.assert_allclose(list(request['target']['position'].values()),
                                               np.array([1, 0, 0]) + .1 * direction, atol=1e-12)
                    release.assert_called_once()

    def test_not_held_or_invalid_request_cannot_move_or_release(self):
        cases = [(False, {'distance': .1, 'direction': [1, 0, 0]}, 'OBJECT_NOT_HELD'),
                 (True, {'distance': .1, 'direction': [0, 0, 0]}, 'INVALID_REQUEST'),
                 (True, {'distance': .1, 'angle_deg': 90}, 'INVALID_REQUEST'),
                 (True, {'angle_deg': 90, 'arc_axis': [0, 0, 1]}, 'INVALID_REQUEST'),
                 (True, {'contact_mode': 'contact', 'distance': .1}, 'INVALID_REQUEST')]
        for held, parameters, code in cases:
            executor, motion, release = self.make_executor(holding=held)
            result = executor.execute(parameters)
            self.assertFalse(result['success'])
            self.assertEqual(result['error']['error_code'], code)
            motion.assert_not_called()
            release.assert_not_called()

    def test_motion_failure_does_not_release(self):
        executor, motion, release = self.make_executor(success=False)
        result = executor.execute({'distance': .1, 'direction': [1, 0, 0]})
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'IK_FAILED')
        release.assert_not_called()


if __name__ == '__main__':
    unittest.main()
