"""Contact loss is checked after each command; slip is diagnostic only."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from atomic_skill.runner import TaskSkillExecutor
from execution.grasp_verification import verify_command_grasp
from tests import test_arc_timing_and_convergence as servo_tests


class CommandEndGraspTests(unittest.TestCase):
    def backend(self, **payload):
        return SimpleNamespace(gripper_controller=SimpleNamespace(
            held_name='Apple', _holding=True, get_state=Mock(return_value={
                'holding': True, 'contact_lost': False,
                'relative_position_error_m': 0., 'relative_orientation_error_rad': 0., **payload})))

    def test_final_loss_is_checked_even_when_live_holding_is_false(self):
        row = verify_command_grasp(self.backend(holding=False, contact_lost=True,
                                  relative_position_error_m=.00696), {'success': True})
        self.assertFalse(row['success'])
        self.assertTrue(row['motion_completed'])
        self.assertEqual(row['error']['failed_stage'], 'post_command_verification')
        self.assertIn('双侧指垫接触', row['error']['error_message'])
        from demo.error_messages import describe_error
        self.assertIn('双侧指垫接触', describe_error(row['error'], '检查失败'))
        self.assertNotIn('滑移', describe_error(row['error'], '检查失败'))

    def test_final_slip_and_rotation_are_ignored_when_contact_remains(self):
        for evidence in ({'relative_position_error_m': .00696},
                         {'relative_position_error_m': .01229},
                         {'relative_orientation_error_rad': .06},
                         {'relative_position_error_m': .1, 'relative_orientation_error_rad': 1.}):
            with self.subTest(evidence=evidence):
                row = verify_command_grasp(self.backend(**evidence), {'success': True})
                self.assertTrue(row['success'])
                self.assertTrue(row['grasp_verification']['success'])
                self.assertNotIn('error', row)
                for key, value in evidence.items():
                    self.assertEqual(row['grasp_verification']['evidence'][key], value)

    def test_released_and_empty_gripper_are_skipped(self):
        for backend in (SimpleNamespace(gripper_controller=None),
                        SimpleNamespace(gripper_controller=SimpleNamespace(
                            held_name=None, _holding=False,
                            get_state=lambda: {'holding': False, 'contact_lost': True}))):
            row = verify_command_grasp(backend, {'success': True})
            self.assertEqual(row, {'success': True})

    def test_original_command_failure_is_preserved(self):
        error = {'error_code': 'PATH_NOT_FOUND', 'error_message': '原始规划原因'}
        row = verify_command_grasp(self.backend(contact_lost=True), {'success': False, 'error': error})
        self.assertIs(row['error'], error)
        self.assertFalse(row['grasp_verification']['success'])

    def test_task_checks_once_after_all_substeps_for_each_skill(self):
        for skill in ('approach', 'retreat', 'pick', 'put', 'lift_up', 'move_to', 'pour', 'grasp', 'release'):
            with self.subTest(skill=skill):
                executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
                executor.backend = backend = self.backend()
                executor.checker = SimpleNamespace()
                def command(*args):
                    # Multiple motion/closure phases complete before the check.
                    backend.gripper_controller.get_state.assert_not_called()
                    return {'success': True, 'substeps': [{'success': True}] * 3}
                executor._execute_command = command
                result = executor.execute({'skill_name': skill}, 1, {}, {})
                self.assertTrue(result['grasp_verification']['success'])
                backend.gripper_controller.get_state.assert_called_once()

    def test_transient_loss_or_slip_does_not_latch_failure(self):
        backend = self.backend()
        backend.gripper_controller.get_state.return_value.update(
            holding=False, contact_lost=True, relative_position_error_m=.02)
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.backend, executor.checker = backend, SimpleNamespace()
        def command(*args):
            backend.gripper_controller.get_state.assert_not_called()
            backend.gripper_controller.get_state.return_value.update(
                holding=True, contact_lost=False, relative_position_error_m=.001)
            return {'success': True}
        executor._execute_command = command
        self.assertTrue(executor.execute({'skill_name': 'approach'}, 1, {}, {})['success'])

    def test_both_mujoco_backends_finish_trajectory_despite_contact_loss(self):
        import importlib.util
        from pathlib import Path
        from execution.mujoco import MuJoCoExecutionBackend
        # The archived backend package has an unrelated broken __init__ import;
        # load its implementation directly to exercise the retained code too.
        source = Path(__file__).resolve().parents[1] / 'execution/backends/mujoco/backend.py'
        spec = importlib.util.spec_from_file_location('execution.backends.mujoco.backend', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        AlternateBackend = module.MuJoCoExecutionBackend
        for backend_type in (MuJoCoExecutionBackend, AlternateBackend):
            with self.subTest(backend=backend_type.__module__):
                backend = servo_tests.ServoExecutionTest.backend()
                backend.gripper_controller = SimpleNamespace(
                    held_name='Apple', _holding=True, update_hold_force=lambda: None,
                    get_state=lambda: {'holding': False, 'contact_lost': True,
                                       'relative_position_error_m': .02})
                backend.payload_gravity_torque = lambda: np.zeros(1)
                result = backend_type.execute_trajectory(backend, servo_tests.ServoExecutionTest.trajectory(True))
                self.assertTrue(result['motion_completed'])
                self.assertTrue(result['completion']['converged'])
                self.assertGreater(result['physics_steps'], 500)
                self.assertIsNone(result['grasp_error'])
                self.assertEqual(result['contact_diagnostics']['samples'], 0)
                self.assertTrue(result['final_gripper_state']['contact_lost'])
                row = verify_command_grasp(backend, {'success': True})
                self.assertFalse(row['success'])


if __name__ == '__main__':
    unittest.main()
