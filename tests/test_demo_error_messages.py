import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from demo.error_messages import describe_error, error_from_exception
from demo.run_demo import step_message
from planning.errors import PlanningError


class DemoErrorMessagesTests(unittest.TestCase):
    command = {'skill_name': 'pick', 'parameters': {'target': 7}}

    def test_depth_search_summarizes_all_failures_without_blame_on_one_branch(self):
        message = self.render({'error_code': 'GRASP_DEPTH_SEARCH_FAILED', 'rejected': [
            {'reason': 'goal_collision', 'contacts': [
                {'geom1': 'Marker', 'geom2': 'upper_arm_link', 'distance': -.042388}]},
            {'reason': 'grasp_geometry_collision', 'contacts': [
                {'geom1': 'pad', 'geom2': 'table', 'distance': -.000163}]}]})
        self.assertNotIn('Marker', message)
        self.assertNotIn('穿透', message)
        self.assertIn('目标位姿间距不足或碰撞 1 次', message)
        self.assertIn('静态张开构型与环境碰撞或安全间距不足 1 次', message)

    def render(self, error, **kwargs):
        return step_message(self.command, success=False, error=error, **kwargs)

    def test_vlm_exception_keeps_specific_cause(self):
        error = error_from_exception(ValueError(
            'VLM grasp depth is on the hidden side; selected depth may belong to the table/background'),
            stage='target_resolution')
        message = self.render(error)
        self.assertIn('疑似选到了桌面或背景深度', message)
        self.assertNotIn('详情见报告', message)

    def test_invalid_vlm_depth_reports_pixel_state_and_code(self):
        from atomic_action.perception.vlm_estimate.estimator import InvalidPointDepth
        exc = InvalidPointDepth(204, 407, float('nan'), {'x': .2, 'y': .4}, 1024, 1024)
        error = error_from_exception(exc, stage='target_resolution')
        message = self.render(error)
        self.assertIn('(204, 407)', message)
        self.assertIn('深度无效（NaN）', message)
        self.assertIn('无法换算三维坐标', message)
        self.assertTrue(message.startswith('error|观测与感知|'))
        self.assertEqual(error['details']['image_size'], {'width': 1024, 'height': 1024})

    def test_rrt_timeout_keeps_prior_contact_failure_without_claiming_impossible(self):
        error = {'error_code': 'PATH_NOT_FOUND', 'error_message': 'RRT search failed',
                 'details': {'planner': 'rrt_connect', 'planning_time': 5.,
                    'preceding_contact_error': {'error_code': 'COLLISION_DETECTED',
                        'details': {'location': 'goal', 'contacts': [
                            {'geom1': 'right_pad', 'geom2': 'Apple', 'distance': -.001821}]}}}}
        message = self.render(error)
        self.assertIn('直线下探失败：right_pad 与 Apple 穿透 1.821 mm', message)
        self.assertIn('关节避障失败：RRT-Connect 在5 s 规划预算内未找到无碰撞路径', message)
        self.assertTrue(message.startswith('error|路径规划|'))
        self.assertNotIn('不存在', message)
        del error['details']['preceding_contact_error']
        self.assertNotIn('直线下探', self.render(error))

    def test_unknown_cause_is_not_discarded(self):
        for code in (None, 'CUSTOM_FAILURE'):
            with self.subTest(code=code):
                message = self.render({'error_code': code, 'error_message': '目标 Banana 缺少模型尺寸'})
                self.assertIn('目标 Banana 缺少模型尺寸', message)
                self.assertTrue(message.startswith('error|请求与状态检查|'))

    def test_clearance_and_penetration_differ(self):
        for distance, text in ((.0001577127, '安全间距不足，当前 0.158 mm'),
                               (-.0057, '穿透 5.700 mm')):
            with self.subTest(distance=distance):
                message = self.render({'error_code': 'PATH_CONSTRAINT_INFEASIBLE',
                    'details': {'safety_distance': .003, 'contacts': [
                        {'geom1': 'right_pad', 'geom2': 'WorkTable', 'distance': distance}]}})
                self.assertIn('right_pad 与 WorkTable', message)
                self.assertIn(text, message)
                self.assertTrue(message.startswith('error|路径规划|'))
                self.assertEqual('要求 3.000 mm' in message, distance > 0)

    def test_nested_rejected_contacts_are_history_not_an_overall_collision(self):
        message = self.render({'error_code': 'NO_VALID_APPROACH', 'rejected': [
            {'details': {'contacts': [{'body1': 'pad', 'body2': 'table', 'distance': .001}]}},
            {'contacts': [{'geom1': 'arm', 'geom2': 'basket', 'distance': -.002}]}]})
        self.assertIn('预接近候选全部不可行', message)
        self.assertIn('碰撞或安全间距检查失败 2 次', message)
        self.assertNotIn('穿透', message)

    def test_depth_attempts_take_priority_over_flattened_duplicate_records(self):
        rejected = [{'reason': 'goal_collision'}, {'reason': 'grasp_below_table_floor'}]
        message = self.render({'error_code': 'GRASP_DEPTH_SEARCH_FAILED',
            'rejected': rejected * 2,
            'details': {'depth_attempts': [
                {'error': {'rejected': rejected}}, {'error': {'rejected': rejected}}]}})
        self.assertIn('2 档抓深均不可行', message)
        self.assertIn('目标位姿间距不足或碰撞 2 次', message)
        self.assertIn('桌面底限 2 次', message)
        self.assertNotIn('4 次', message)

    def test_current_collision_is_not_replaced_by_rejected_or_fallback_history(self):
        error = {'error_code': 'COLLISION_DETECTED', 'details': {'contacts': [
            {'geom1': 'pad', 'geom2': 'table', 'distance': -.001}],
            'rejected': [{'contacts': [
                {'geom1': 'Marker', 'geom2': 'arm', 'distance': -.1}]}],
            'fallback': {'contacts': [{'geom1': 'old', 'geom2': 'old', 'distance': -.2}]}}}
        message = self.render(error)
        self.assertIn('pad 与 table 穿透 1.000 mm', message)
        self.assertNotIn('Marker', message)
        self.assertNotIn('old', message)

    def test_timeout_and_grasp_loss_are_not_replaced_by_incidental_contacts(self):
        contacts = [{'geom1': 'Marker', 'geom2': 'arm', 'distance': -.1}]
        message = self.render({'error_code': 'PLANNING_TIMEOUT',
            'error_message': '规划预算耗尽', 'details': {'contacts': contacts},
            'rejected': [{'reason': 'goal_collision', 'contacts': contacts}]})
        self.assertIn('规划预算耗尽', message)
        self.assertNotIn('Marker', message)
        message = self.render({'error_code': 'GRASP_FAILED', 'contact_loss_duration_s': .1,
                              'details': {'contacts': contacts}})
        self.assertIn('持续丢失 0.100 s', message)
        self.assertNotIn('Marker', message)

    def test_search_timeout_is_not_reported_as_proven_infeasibility(self):
        message = self.render({'error_code': 'JOINT_CONFIGURATION_SEARCH_FAILED', 'details': {
            'budget_exhausted': True, 'rejected': [{'category': 'PATH_NOT_FOUND'}]}})
        self.assertIn('规划预算内未找到路径 1 次', message)
        self.assertIn('未证明不存在其他可行路径', message)

    def test_contact_wrappers_and_invalid_records(self):
        message = self.render({'error_code': 'COLLISION_DETECTED', 'details': {'contacts': {
            'location': 'goal', 'contacts': [
                {}, {'geom1': 'incomplete', 'distance': -.5},
                {'geom1': 'pad', 'geom2': 'table', 'distance': -.001}]}}})
        self.assertIn('pad 与 table 穿透 1.000 mm', message)
        self.assertNotIn('incomplete', message)

    def test_joint_configuration_search_retains_all_failures_instead_of_last_one(self):
        from atomic_action.motion.common.action import _MoveExecutor
        executor = SimpleNamespace(direct_planner=Mock())
        executor.direct_planner.plan.side_effect = [
            PlanningError('分支一目标碰撞', code='COLLISION_DETECTED', details={'contacts': [
                {'geom1': 'Marker', 'geom2': 'arm', 'distance': -.04}]}),
            PlanningError('分支二超过关节限位', code='JOINT_LIMIT_VIOLATION')]
        with self.assertRaises(PlanningError) as caught:
            _MoveExecutor._plan_joint_candidates(executor,
                {'planning': {'planning_time': 60.}}, {}, {},
                {'start': np.zeros(6), 'candidate_goals': [np.ones(6), np.ones(6)*.5]})
        error = error_from_exception(caught.exception)
        self.assertEqual(error['error_code'], 'JOINT_CONFIGURATION_SEARCH_FAILED')
        self.assertEqual(len(error['details']['rejected']), 2)
        message = self.render(error)
        self.assertIn('碰撞或安全间距检查失败 1 次', message)
        self.assertIn('超出关节限位 1 次', message)
        self.assertNotIn('Marker', message)

    def test_motion_executor_preserves_structured_execution_failure(self):
        from atomic_action.motion.common.action import _MoveExecutor
        executor = _MoveExecutor.__new__(_MoveExecutor)
        executor.config = {}
        executor.collision_checker = SimpleNamespace()
        executor.selector = SimpleNamespace(select=lambda *args: {'path_strategy': 'joint'})
        executor.strategies = {'joint': Mock()}
        executor._get_runtime_context = Mock(return_value={})
        executor._reuse_prepared_trajectory = Mock(return_value={'waypoints': [[0.]*6]*2})
        executor.trajectory_callback = None
        failure = RuntimeError('执行期间碰撞')
        failure.details = {'error_code': 'COLLISION_DETECTED', 'failed_stage': 'execution',
            'error_message': '执行期间碰撞', 'details': {'contacts': [
                {'geom1': 'pad', 'geom2': 'table', 'distance': -.001}]}}
        executor._execute_trajectory = Mock(side_effect=failure)
        with patch('atomic_action.motion.common.action.validate_move_request', return_value={}):
            result = executor.execute({})
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'], 'COLLISION_DETECTED')
        self.assertEqual(result['error']['failed_stage'], 'execution')
        self.assertIn('pad 与 table 穿透 1.000 mm', self.render(result['error']))

    def test_joint_and_travel_evidence(self):
        cases = [
            ('PATH_CONSTRAINT_INFEASIBLE', {'joint': 'j2', 'step': .5, 'maximum_step': .1},
             'j2 单步变化 0.5000 rad，超过允许 0.1000 rad'),
            ('JOINT_LIMIT', {'joint': 'j1', 'current': 2., 'lower': -1., 'upper': 1.},
             'j1 当前 2.0000 rad，允许范围 [-1.0000, 1.0000]'),
            ('TRAVEL_LIMIT', {'requested': .3, 'limit': .2, 'unit': 'm'},
             '请求行程 0.3000 m，超过允许 0.2000 m'),
        ]
        for code, details, expected in cases:
            with self.subTest(code=code):
                self.assertIn(expected, self.render({'error_code': code, 'details': details}))

    def test_grasp_loss_evidence(self):
        self.assertIn('持续丢失 0.100 s', self.render(
            {'error_code': 'GRASP_FAILED', 'contact_loss_duration_s': .1}))
        self.assertIn('滑移 8.00 mm', self.render(
            {'error_code': 'GRASP_FAILED', 'evidence': {'relative_position_error_m': .008}}))

    def test_candidate_failure_counts(self):
        self.assertIn('预接近位姿无逆解 2 次', self.render(
            {'error_code': 'NO_VALID_APPROACH', 'rejected': [
                {'category': 'prepoint_ik'}, {'category': 'prepoint_ik'}]}))

    def test_placement_errors_hide_search_implementation_and_keep_causes(self):
        error = {'error_code': 'PLACEMENT_HEIGHT_SEARCH_FAILED',
                 'error_message': '中央随机兜底均不可行',
                 'details': {'center_fallback': {'unavailable_reason': '兜底点不可用'}},
                 'rejected': [{'reason': 'goal_collision'}] * 172 + [{'reason': 'ik_failed'}]}
        message = self.render(error)
        self.assertIn('未找到可用放置位姿', message)
        self.assertIn('碰撞 172 次', message)
        self.assertIn('目标位姿无逆解 1 次', message)
        for hidden in ('兜底', '随机', '中央'):
            self.assertNotIn(hidden, message)
        error['rejected'] = []
        self.assertEqual(describe_error(error, '失败'), '未找到可用放置位姿')
        error['error_code'] = 'PLACEMENT_PERCEPTION_FAILED'
        self.assertEqual(describe_error(error, '失败'), '放置感知失败，无法获得有效放置点')

    def test_placement_messages_hide_recovery_even_without_center_diagnostics(self):
        from copy import deepcopy
        command = {'skill_name': 'put', 'parameters': {'target': 12}}
        for details in ({}, {'floor_fallback': {'success': False}},
                        {'relative_region': {'relation': 'above'}},
                        {'placement_revalidation': {'trigger': 'cached_place_pose_infeasible'}}):
            error = {'error_code': 'PLACEMENT_HEIGHT_SEARCH_FAILED',
                     'error_message': '随机兜底重试均失败', 'details': details}
            original = deepcopy(error)
            message = step_message(command, success=False, error=error)
            for hidden in ('兜底', '随机', '重试', '档', '10 cm'):
                self.assertNotIn(hidden, message)
            self.assertIn('放置位姿', message)
            self.assertEqual(error, original)  # Internal diagnostic history remains intact.

    def test_exception_diagnostics_support_all_payload_shapes(self):
        for payload in (None, [], [{'geom1': 'a'}], {'contacts': []}):
            with self.subTest(payload=payload):
                error = error_from_exception(PlanningError('具体规划原因', details=payload))
                self.assertEqual(error['error_code'], 'PLANNING_FAILED')
                self.assertEqual(error['error_message'], '具体规划原因')
                if payload is not None:
                    self.assertEqual(error['details'], payload)
        exc = RuntimeError('原始异常')
        exc.details = {'error_code': 'TARGET_POSE_COLLISION', 'rejected': []}
        result = error_from_exception(exc, existing={'failed_stage': 'planning'})
        self.assertEqual(result['error_code'], 'TARGET_POSE_COLLISION')
        self.assertEqual(result['error_message'], '原始异常')
        self.assertEqual(result['failed_stage'], 'planning')

    def test_success_suspend_and_missing_evidence(self):
        self.assertEqual(step_message(self.command, success=True, target_name='Apple'),
                         'success: 已抓取目标 Apple')
        message = self.render({'error_code': 'CONTACT_LOST', 'error_message': '物体已脱离夹爪'},
                              status='suspend', target_name='Apple')
        self.assertEqual(message, 'error|运动执行|任务中止，Apple 未完成目标；物体已脱离夹爪')
        self.assertIn('上游未提供具体失败原因', self.render({'error_code': 'IK_FAILED'}))
        self.assertEqual(describe_error(None, '失败'), '失败；上游未提供具体失败原因')

    def test_vlm_failures_use_dedicated_categories_and_details(self):
        cases = [
            ({'error_code': 'VLM_REQUEST_TIMEOUT', 'failed_stage': 'vlm_request'},
             'error|VLM 交互|VLM 请求超时'),
            ({'error_code': 'VLM_REQUEST_FAILED', 'failed_stage': 'vlm_request',
              'error_message': '服务返回 503'},
             'error|VLM 交互|VLM 请求失败：服务返回 503'),
            ({'error_code': 'VLM_RESPONSE_FAILED', 'failed_stage': 'vlm_response',
              'error_message': 'VLM response must be a JSON object.'},
             'error|VLM 交互|VLM 返回失败：VLM 返回内容不是 JSON 对象'),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(self.render(error), expected)

    def test_error_text_cannot_break_pipe_delimited_format(self):
        self.assertEqual(self.render({'error_message': '上游错误 | 连接拒绝'}),
                         'error|请求与状态检查|上游错误 ｜ 连接拒绝')

    def test_error_format_assigns_pose_and_motion_failures_to_distinct_categories(self):
        self.assertEqual(self.render({'error_code': 'TARGET_POSE_UNREACHABLE'}),
                         'error|目标可行性|目标位姿候选均无可用逆解')
        self.assertEqual(self.render({'error_code': 'IK_FAILED'}),
                         'error|目标可行性|逆运动学求解失败；上游未提供具体失败原因')
        self.assertTrue(self.render({'error_code': 'EXECUTION_TIMEOUT'}).startswith('error|运动执行|'))


if __name__ == '__main__':
    unittest.main()
