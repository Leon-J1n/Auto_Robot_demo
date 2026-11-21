import unittest
from unittest.mock import patch

import numpy as np

from atomic_action.manipulation.rotate import RotateAction


class _Runtime:
    joint_names = ['shoulder', 'wrist_3_joint', 'tool_roll']

    def get_joint_positions(self):
        return np.zeros(3)


class RotateActionTests(unittest.TestCase):
    def execute_and_capture(self, request):
        with patch('atomic_action.manipulation.rotate.action.MoveJointAction') as move:
            move.return_value.execute.return_value = {'success': True}
            result = RotateAction(robot_runtime=_Runtime()).execute(request)
        return result, move.return_value.execute.call_args.args[0]['target']['joint_positions']

    def test_defaults_to_terminal_wrist_joint(self):
        result, joints = self.execute_and_capture({'degree': 90})
        self.assertEqual(result, {'success': True})
        np.testing.assert_allclose(joints, [0, np.pi / 2, 0])

    def test_can_select_another_joint_by_name(self):
        _, joints = self.execute_and_capture({'degree': -45, 'joint_name': 'shoulder'})
        np.testing.assert_allclose(joints, [-np.pi / 4, 0, 0])

    def test_unknown_joint_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown joint_name'):
            RotateAction(robot_runtime=_Runtime()).execute({'degree': 10, 'joint_name': 'missing'})


if __name__ == '__main__':
    unittest.main()
