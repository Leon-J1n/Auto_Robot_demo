"""Preserve both failed routes when pick cannot reach its contact pose."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.runner import TaskSkillExecutor
from demo.run_demo import step_message


class PickPlanningFailureTests(unittest.TestCase):
    def test_failed_fallback_keeps_first_collision_and_final_search_failure(self):
        self.check_failed_routes('pick')

    def test_put_retries_joint_planning_and_keeps_both_failure_causes(self):
        self.check_failed_routes('put')

    def check_failed_routes(self, skill):
        e = TaskSkillExecutor.__new__(TaskSkillExecutor)
        pose = {'position': np.zeros(3), 'rotation_matrix': np.eye(3)}
        e.resources = SimpleNamespace(scene={'move_defaults': {}})
        e.report = {}
        e.resolver = SimpleNamespace()
        e.kinematics = SimpleNamespace(forward=lambda q: pose)
        e.sync = SimpleNamespace(sync=Mock())
        e.write_frame = Mock()
        e.converter = SimpleNamespace(convert_command=lambda *args: {
            'request': {'target': {'type': 'object', 'target_id': 7}}})
        e.backend = SimpleNamespace(gripper_controller=SimpleNamespace(held_name=None),
            set_status=Mock(), physics_steps=0, grasp_approach_goal=None, set_frame_callback=Mock())
        e.checker = SimpleNamespace(carrying_safety_distance=0.)
        target = np.eye(4).tolist()
        resolved = {'selected': {'world_from_control': target},
                    'joint_positions': [0.] * 6, 'approach': {}}
        e.adapters = {skill: SimpleNamespace(prepare=lambda *args, **kw: (
            [{'phase': 'contact', 'request': {'constraints': {}},
              'direct_pick_fallback': {'constraints': {}}}], resolved))}
        initial = {'error_code': 'COLLISION_DETECTED', 'failed_stage': 'planning',
            'error_message': 'Goal state is in collision.', 'details': {'contacts': [
                {'geom1': 'right_pad', 'geom2': 'Apple', 'distance': -.0018}]}}
        final = {'error_code': 'PATH_NOT_FOUND', 'failed_stage': 'planning',
            'error_message': 'RRT search failed', 'details': {'planner': 'rrt_connect', 'planning_time': 5.}}
        e.execute_motion = Mock(side_effect=[{'success': False, 'error': initial},
                                             {'success': False, 'error': final}])
        command = {'command_id': '2', 'skill_name': skill, 'parameters': {'target': 7}}
        row = {}
        e._execute_command(command, 2, {'joints': [0.] * 6}, row)
        self.assertFalse(row['success'])
        self.assertEqual(row['error']['error_code'], 'PATH_NOT_FOUND')
        self.assertEqual(row['error']['details']['preceding_contact_error'], initial)
        self.assertNotIn('preceding_contact_error', final['details'])
        self.assertEqual(len(row['substeps']), 2)
        self.assertIsNone(e.backend.grasp_approach_goal)
        message = step_message(command, success=False, error=row['error'])
        self.assertIn('right_pad 与 Apple 穿透 1.800 mm', message)
        self.assertIn('5 s 规划预算内未找到无碰撞路径', message)


if __name__ == '__main__':
    unittest.main()
