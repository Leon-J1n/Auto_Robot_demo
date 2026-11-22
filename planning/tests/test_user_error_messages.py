"""Console errors stay actionable without losing structured diagnostics."""
from copy import deepcopy
import unittest

from demo.run_demo import step_message


class UserErrorMessagesTests(unittest.TestCase):
    def message(self, code, **fields):
        command = dict(skill_name='pick', parameters=dict(target=7))
        error = dict(error_code=code, error_message='Internal English diagnostics', **fields)
        before = deepcopy(error)
        result = step_message(command, success=False, error=error, target_name='Apple')
        self.assertEqual(error, before)
        self.assertNotIn('Internal English', result)
        self.assertNotIn('\n', result)
        self.assertNotIn('detail|', result)
        return result

    def test_perception_region_and_grasp_failure(self):
        self.assertIn('未能识别目标物体 Apple', self.message('TARGET_NOT_FOUND'))
        self.assertIn('无法观察到 Apple 的指定放置区域', self.message('PLACEMENT_REGION_UNOBSERVED'))
        self.assertIn('夹爪未能稳定抓住 Apple', self.message('GRASP_FAILED'))
        self.assertIn('夹爪与 Apple 失去接触', self.message('GRASP_FAILED', evidence={'contact_lost': True}))

    def test_opening_timeout_is_not_reported_as_grasp_or_generic_failure(self):
        result = self.message('GRIPPER_OPENING_TIMEOUT')
        self.assertEqual(result, 'error|夹爪操作|夹爪张开超时，请检查夹爪是否受阻或设备状态。')
        result = step_message(dict(skill_name='pick', parameters=dict(target=11)), success=False,
            error=dict(error_message='Gripper opening timed out', failed_stage='grasp'), target_name='ToyPanda')
        self.assertIn('夹爪张开超时', result)

    def test_ik_failure_does_not_invent_joint_limit_cause(self):
        result = self.message('PATH_CONSTRAINT_INFEASIBLE', details={
            'samples': [{'reason': 'ik_failed: no certified solution'}]})
        self.assertIn('无法规划出合适的机械臂姿态', result)
        self.assertNotIn('超出活动范围', result)

    def test_explicit_joint_limits_and_motion_failures(self):
        result = self.message('JOINT_LIMIT_VIOLATION', details={
            'joint': 'joint5', 'lower': -1.22, 'upper': 1.22, 'value': 1.4})
        self.assertIn('joint5 超出活动范围 [-1.22, 1.22] rad', result)
        self.assertIn('目标位置与周围环境冲突', self.message('TARGET_POSE_COLLISION'))
        self.assertIn('未能找到可用的目标位置', self.message('PLACEMENT_HEIGHT_SEARCH_FAILED'))
        self.assertIn('目标路径受阻', self.message('COLLISION_DETECTED'))
        self.assertIn('机械臂未能到达目标位置', self.message('EXECUTION_TIMEOUT'))


if __name__ == '__main__':
    unittest.main()
