import unittest
from types import SimpleNamespace

import numpy as np

from demo.error_messages import display_error
from demo.run_demo import step_message
from atomic_action.motion.common.strategies.joint_move.joint_move import JointMove
from planning.common.errors import PlanningError


class DemoDisplayErrorTests(unittest.TestCase):
    def test_target_collision_uses_short_user_message(self):
        self.assertEqual(display_error({'error_code': 'TARGET_POSE_COLLISION'}), (
            '目标位置解析', '目标位置与周围环境冲突，无法到达。'))

    def test_unrecognized_target_uses_perception_guidance(self):
        self.assertEqual(display_error({'error_code': 'TARGET_NOT_FOUND'}), (
            '目标位置解析', '未能识别目标物体，请确认物体在视野内且清晰可见。'))

    def test_joint_limit_includes_name_when_available(self):
        error = {'error_code': 'JOINT_LIMIT_VIOLATION',
                 'details': {'joints': [{'joint': 'elbow_joint', 'current': 2.1}]}}
        self.assertEqual(display_error(error), (
            '机械臂运动', '关节 elbow_joint 超出活动范围，请调整目标位置。'))

    def test_joint_move_attaches_joint_name_to_limit_error(self):
        kinematics = SimpleNamespace(
            limits=np.array([[-1., 1.]]),
            robot=SimpleNamespace(joint_names=('elbow_joint',)),
        )
        strategy = JointMove(kinematics=kinematics)
        with self.assertRaises(PlanningError) as caught:
            strategy.build_path_request(
                {'target': {'type': 'joint', 'joint_positions': [2.]}, 'strategy_params': {}},
                {'current_joint_state': [0.]},
            )
        self.assertEqual(caught.exception.details['joint'], 'elbow_joint')

    def test_inverse_kinematics_failure_hides_internal_reason(self):
        error = {'error_code': 'IK_FAILED', 'error_message': 'No IK configuration reaches the target.'}
        self.assertEqual(display_error(error), (
            '机械臂运动', '无法规划出合适的机械臂姿态，请调整目标位置或方向。'))

    def test_path_and_execution_errors_have_distinct_user_messages(self):
        self.assertEqual(display_error({'error_code': 'PATH_NOT_FOUND'}), (
            '机械臂运动', '目标路径受阻，机械臂无法安全移动。'))
        self.assertEqual(display_error({'error_code': 'EXECUTION_TIMEOUT'}), (
            '机械臂运动', '机械臂未能到达目标位置，请重试或检查设备状态。'))

    def test_lost_gripper_contact_uses_item_name(self):
        self.assertEqual(display_error({'error_code': 'CONTACT_LOST',
                                        'details': {'item_name': '工件 A'}}), (
            '夹爪操作', '夹爪与抓取物「工件 A」失去接触，请检查抓取物状态。'))

    def test_command_target_is_not_assumed_to_be_the_grasped_item(self):
        command = {'skill_name': 'approach', 'parameters': {'target': 'Tray'}}
        self.assertEqual(step_message(command, success=False,
                                      error={'error_code': 'CONTACT_LOST'},
                                      target_name='Tray'),
                         'error|夹爪操作|夹爪与抓取物失去接触，请检查抓取物状态。')

    def test_step_message_preserves_output_format(self):
        command = {'skill_name': 'move_to', 'parameters': {'target': 3}}
        self.assertEqual(step_message(command, success=False,
                                      error={'error_code': 'PATH_NOT_FOUND'}),
                         'error|机械臂运动|目标路径受阻，机械臂无法安全移动。')


if __name__ == '__main__':
    unittest.main()
