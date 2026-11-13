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
        backend.model = mujoco.MjModel.from_xml_string(
            '<mujoco><worldbody><site name="grasp_center" pos="1 0 .2"/></worldbody></mujoco>')
        backend.data = mujoco.MjData(backend.model)
        mujoco.mj_forward(backend.model, backend.data)
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
                result = executor.execute({'distance': 0.1, 'direction': [0, 1, 0]})
                self.assertTrue(result['success'])
                request = motion.call_args.args[0]
                self.assertEqual(request['motion']['path_type'], 'linear')
                self.assertEqual(request['target']['position'], {'x': 1., 'y': .1, 'z': 0.})
                release.assert_called_once()

    def test_reverse_arc_needs_no_object_or_link(self):
        executor, motion, release = self.make_executor()
        result = executor.execute({'arc_center': [0, 0, 0],
                                   'arc_axis': [0, 0, 1], 'angle_deg': -90})
        self.assertTrue(result['success'])
        request = motion.call_args.args[0]
        self.assertEqual(request['motion']['path_type'], 'circular')
        self.assertAlmostEqual(request['strategy_params']['arc_angle'], -np.pi / 2)
        np.testing.assert_allclose(list(request['target']['position'].values()), [0, -1, 0], atol=1e-12)
        self.assertEqual([s['phase'] for s in result['substeps']], ['push_motion', 'release'])
        release.assert_called_once()

    def test_linear_direction_uses_rotated_tool_but_arc_axis_remains_world(self):
        rotation = np.array([[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]])
        for kind, sign in ((CommandPushExecutor, 1), (CommandPullExecutor, -1)):
            executor, motion, release = self.make_executor(kind)
            executor.backend.get_end_effector_pose = lambda: {
                'position': [1, 0, 0], 'rotation_matrix': rotation}
            executor.backend.data.site_xpos[0] = np.array([1, 0, 0]) + rotation @ [0, 0, .2]
            executor.backend.data.site_xmat[0] = rotation.ravel()
            self.assertTrue(executor.execute({'distance': .1})['success'])
            np.testing.assert_allclose(motion.call_args.args[0]['strategy_params']['direction'],
                                       rotation @ np.array([0., 0., sign]))
        executor, motion, _ = self.make_executor()
        executor.backend.get_end_effector_pose = lambda: {
            'position': [1, 0, 0], 'rotation_matrix': rotation}
        self.assertTrue(executor.execute({'arc_center': [0, 0, 0],
            'arc_axis': [0, 0, 1], 'angle_deg': 90})['success'])
        np.testing.assert_allclose(list(motion.call_args.args[0]['target']['position'].values()),
                                   [0, 1, 0], atol=1e-12)

    def test_not_held_or_invalid_request_cannot_move_or_release(self):
        cases = [(True, {'frame': 'tool', 'distance': .1}, 'INVALID_REQUEST'),
                 (True, {'distance': .1, 'arc_axis': [0, 0, 1]}, 'INVALID_REQUEST'),
                 (True, {'arc_center_offset': [0, 1, 0], 'arc_axis': [0, 0, 1], 'angle_deg': 90}, 'INVALID_REQUEST'),
                 (False, {'distance': .1, 'direction': [1, 0, 0]}, 'OBJECT_NOT_HELD'),
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

    def test_commands007_preserves_motion_with_asset_calibrated_axes(self):
        import json
        from pathlib import Path
        from scipy.spatial.transform import Rotation
        commands = json.loads((Path(__file__).resolve().parents[1] /
            'demo/task_commands/commands.007/commands.007.json').read_text(encoding='utf-8'))['commands']
        # This gripper's grasp +Z is the control/tool +Y, not tool +Z.
        control_from_grasp_rotation = np.array([[0., 1., 0.], [0., 0., 1.], [1., 0., 0.]])
        tool_rotation = Rotation.from_euler('xyz', [27., -15., 38.], degrees=True).as_matrix()
        for command in commands:
            operation = command['skill_name']
            if operation not in ('push', 'pull'):
                continue
            executor, motion, _ = self.make_executor(
                CommandPushExecutor if operation == 'push' else CommandPullExecutor)
            executor.backend.get_end_effector_pose = lambda: {
                'position': [1, 0, 0], 'rotation_matrix': tool_rotation}
            executor.backend.data.site_xpos[0] = np.array([1, 0, 0]) + tool_rotation @ [0, .2558, 0]
            executor.backend.data.site_xmat[0] = (tool_rotation @ control_from_grasp_rotation).ravel()
            self.assertTrue(executor.execute(command['parameters'])['success'])
            old_grasp_axis = np.array([0., 0., 1. if operation == 'push' else -1.])
            expected = tool_rotation @ control_from_grasp_rotation @ old_grasp_axis
            np.testing.assert_allclose(motion.call_args.args[0]['strategy_params']['direction'], expected)

    def test_offset_gripper_uses_center_line_and_orthogonal_grasp_x(self):
        from demo.interaction.requests import push_pull_rotation, relative_tool_motion
        executor, _, _ = self.make_executor()
        backend = executor.backend
        backend.data.site_xpos[0] = [1.1, .2, .3]
        basis = push_pull_rotation(backend)
        np.testing.assert_allclose(basis[:, 2], np.array([.1, .2, .3]) / np.linalg.norm([.1, .2, .3]))
        np.testing.assert_allclose(basis.T @ basis, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(basis), 1.)
        for axis in np.eye(3):
            _, motion = relative_tool_motion(backend, {'direction': axis, 'distance': .1}, 'push')
            np.testing.assert_allclose(motion['direction'], basis @ axis)
        backend.data.site_xpos[0] = [1.2, 0, 0]  # Grasp X parallel to the center line.
        basis = push_pull_rotation(backend)
        np.testing.assert_allclose(basis.T @ basis, np.eye(3), atol=1e-12)
        np.testing.assert_allclose(basis[:, 2], [1, 0, 0])

    def test_missing_or_coincident_calibration_rejects_linear_but_allows_arc(self):
        for missing in (False, True):
            executor, motion, release = self.make_executor()
            if missing:
                del executor.backend.model
            else:
                executor.backend.data.site_xpos[0] = [1, 0, 0]
            result = executor.execute({'distance': .1})
            self.assertFalse(result['success'])
            self.assertEqual(result['error']['error_code'], 'INVALID_REQUEST')
            motion.assert_not_called()
            release.assert_not_called()
            self.assertTrue(executor.execute({'arc_center': [0, 0, 0],
                'arc_axis': [0, 0, 1], 'angle_deg': 10})['success'])


if __name__ == '__main__':
    unittest.main()
