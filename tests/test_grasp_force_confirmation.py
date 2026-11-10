"""Confirm physical grasps from pad forces, including empty completed closure."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from atomic_action.manipulation.grasp.action import GraspAction
from control.binding_gripper import BindingGripperController
from atomic_skill.runner import TaskSkillExecutor
from demo.run_demo import step_message


class ScriptedGripper(BindingGripperController):
    """Supply deterministic actuator feedback and measured contact samples."""

    def __init__(self, forces, *, stalled=False):
        self.data = SimpleNamespace(time=0., ctrl=np.zeros(1), qpos=np.zeros(1))
        self.model = SimpleNamespace(actuator_forcerange=np.zeros((1, 2)))
        self.runtime = SimpleNamespace(contact_target=None, advance_gripper=self.advance)
        self.actuator_id = self.driver_address = 0
        self.held_name = self.relative_pose = None
        self.confirmed = self._holding = False
        self.evidence = {}
        self.samples = forces
        self.stalled = stalled

    def advance(self, seconds):
        self.data.time += seconds
        if not self.stalled:
            self.data.qpos[:] = self.data.ctrl

    @property
    def width(self):
        return .03 if self.samples(self.data.time) else max(0., .085 * (1. - self.data.qpos[0] / .8))

    def _contact_forces(self):
        return self.samples(self.data.time)

    def _movable_body(self, body):
        return body == 1

    def _relative(self, body):
        return np.eye(4)


class GraspForceConfirmationTests(unittest.TestCase):
    def test_empty_physical_closure_fails_even_when_driver_never_reaches_approximate_goal(self):
        class ClosedAtMechanicalStop(ScriptedGripper):
            def advance(self, seconds):
                super().advance(seconds)
                self.data.qpos[:] = np.minimum(self.data.qpos, .78)

            @property
            def width(self):
                return max(.000016, .085 * (1. - self.data.qpos[0] / .78))

        controller = ClosedAtMechanicalStop(lambda t: {})
        result = controller.close(0., timeout=30.)
        evidence = result['evidence']
        self.assertFalse(result['contact_detected'])
        self.assertEqual(evidence['reason'], 'no_contact_force')
        self.assertTrue(evidence['closure_completed'])
        self.assertTrue(evidence['command_reached'])
        self.assertTrue(evidence['width_reached'])
        self.assertFalse(evidence['driver_reached'])
        self.assertAlmostEqual(evidence['measured_width_m'], .000016)
        self.assertGreaterEqual(evidence['no_contact_time_s'], .1 - 1e-9)
        self.assertLess(controller.data.time, 2.3)
        self.assertIsNone(controller.held_name)
        self.assertFalse(controller._holding)

    def test_empty_closure_fails_promptly_after_reaching_endpoint(self):
        controller = ScriptedGripper(lambda t: {})
        result = controller.close(0., timeout=30.)
        self.assertFalse(result['contact_detected'])
        self.assertEqual(result['evidence']['reason'], 'no_contact_force')
        self.assertTrue(result['evidence']['closure_completed'])
        self.assertGreaterEqual(controller.data.time, 2.1 - 1e-9)
        self.assertLess(controller.data.time, 2.3)
        self.assertIsNone(controller.held_name)
        self.assertFalse(controller._holding)

    def test_no_force_mid_closing_does_not_abort_later_real_contact(self):
        controller = ScriptedGripper(lambda t: {1: {'left': 3., 'right': 3.}} if t >= 1.5 else {})
        with patch('control.binding_gripper.mujoco.mj_id2name', return_value='Apple'):
            result = controller.close(0.)
        self.assertTrue(result['contact_detected'])
        self.assertEqual(controller.held_name, 'Apple')
        self.assertEqual(result['evidence']['reason'], 'stable_bilateral_grasp')

    def test_one_sided_contact_is_not_misreported_as_no_force(self):
        controller = ScriptedGripper(lambda t: {1: {'left': .8, 'right': 0.}})
        result = controller.close(0., timeout=3.)
        self.assertFalse(result['contact_detected'])
        self.assertEqual(result['evidence']['reason'], 'bilateral_contact_not_stable')
        self.assertEqual(result['evidence']['measured_left_force_n'], .8)

    def test_numerical_noise_does_not_confirm_a_grasp(self):
        controller = ScriptedGripper(lambda t: {1: {'left': 1e-6, 'right': 1e-6}})
        result = controller.close(0.)
        self.assertFalse(result['contact_detected'])
        self.assertEqual(result['evidence']['reason'], 'no_contact_force')

    def test_stalled_actuator_is_reported_as_timeout_not_completed_closure(self):
        controller = ScriptedGripper(lambda t: {}, stalled=True)
        result = controller.close(0., timeout=3.)
        self.assertEqual(result['evidence']['reason'], 'closure_timeout_no_contact_force')
        self.assertFalse(result['evidence']['closure_completed'])

    def test_force_limit_failure_is_preserved(self):
        controller = ScriptedGripper(lambda t: {1: {'left': 25., 'right': 25.}})
        result = controller.close(0.)
        self.assertFalse(result['contact_detected'])
        self.assertEqual(result['evidence']['reason'], 'force_limit_exceeded')

    def test_internal_finger_forces_are_excluded_from_object_force(self):
        controller = BindingGripperController.__new__(BindingGripperController)
        controller.base_id = 1
        controller.pad_geoms = {'left': {0}, 'right': {1}}
        controller.model = SimpleNamespace(geom_bodyid=[2, 3, 4], body_parentid=[0, 0, 1, 1, 0])
        controller.data = SimpleNamespace(ncon=2, contact=[
            SimpleNamespace(geom1=0, geom2=1),  # Pads touching each other.
            SimpleNamespace(geom1=0, geom2=2)])  # Left pad touching an object.
        def force(model, data, index, wrench):
            wrench[0] = 10. if index == 0 else 3.
        with patch('control.binding_gripper.mujoco.mj_contactForce', side_effect=force):
            self.assertEqual(controller._contact_forces(), {4: {'left': 3., 'right': 0.}})


class GraspActionFailureTests(unittest.TestCase):
    def test_pick_stops_at_empty_grasp_and_keeps_specific_error(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        identity = np.eye(4).tolist()
        pose = {'position': np.zeros(3), 'rotation_matrix': np.eye(3)}
        controller = SimpleNamespace(held_name=None, mode='physical', open=Mock(), hold=Mock(), close=Mock(return_value={
            'contact_detected': False, 'evidence': {'reason': 'no_contact_force'}}))
        executor.backend = SimpleNamespace(gripper_controller=controller, physics_steps=0,
            grasp_approach_goal=None, set_status=Mock(), set_frame_callback=Mock(),
            get_end_effector_pose=lambda: pose)
        executor.resources = SimpleNamespace(scene={'move_defaults': {}})
        executor.report = {}
        executor.resolver = SimpleNamespace()
        executor.kinematics = SimpleNamespace(forward=lambda q: pose)
        executor.sync = SimpleNamespace(sync=Mock())
        executor.write_frame = Mock()
        executor.converter = SimpleNamespace(convert_command=lambda *args: {
            'request': {'target': {'type': 'object', 'target_id': 7}}})
        executor.checker = SimpleNamespace(carrying_safety_distance=0., capture_support=Mock(),
            check_path=Mock(return_value=SimpleNamespace(in_collision=False)))
        executor.adapters = {'pick': SimpleNamespace(prepare=lambda *args, **kwargs: (
            [{'phase': 'contact', 'request': {'constraints': {}}},
             {'phase': 'lift', 'request': {'constraints': {}}}],
            {'selected': {'world_from_control': identity, 'world_from_grasp': identity},
             'control_from_grasp': identity, 'joint_positions': [0.] * 6}))}
        executor.execute_motion = Mock(return_value={
            'success': True, 'trajectory': {'waypoints': [[0.] * 6]}})
        executor.grasper = GraspAction(robot_runtime=executor.backend)
        command = {'command_id': '2', 'skill_name': 'pick', 'parameters': {'target': 7}}
        row = {}
        with self.assertRaises(RuntimeError):
            executor._execute_command(command, 2, {'joints': [0.] * 6}, row)
        self.assertFalse(row['success'])
        self.assertEqual(row['error']['failed_stage'], 'grasp')
        self.assertIn('未抓到物体', step_message(command, success=False, error=row['error']))
        executor.execute_motion.assert_called_once()  # No lift after failed closure.
        executor.checker.capture_support.assert_not_called()
        controller.hold.assert_not_called()

    def test_empty_grasp_returns_specific_cause_and_never_enters_hold(self):
        evidence = {'reason': 'no_contact_force', 'measured_left_force_n': 0.,
                    'measured_right_force_n': 0., 'closure_completed': True}
        controller = SimpleNamespace(open=Mock(), hold=Mock(), close=Mock(return_value={
            'contact_detected': False, 'final_width': 0., 'evidence': evidence}))
        result = GraspAction(robot_runtime=SimpleNamespace(gripper_controller=controller)).execute({})
        self.assertFalse(result['success'])
        self.assertFalse(result['grasp_result']['holding'])
        self.assertEqual(result['error']['error_code'], 'GRASP_NOT_CONFIRMED')
        self.assertEqual(result['error']['failed_stage'], 'grasp')
        self.assertIn('未检测到有效接触力', result['error']['error_message'])
        self.assertEqual(result['error']['details']['confirmation'], evidence)
        controller.hold.assert_not_called()

    def test_one_sided_contact_failure_reports_unstable_bilateral_contact(self):
        controller = SimpleNamespace(open=Mock(), hold=Mock(), close=Mock(return_value={
            'contact_detected': False, 'evidence': {'reason': 'bilateral_contact_not_stable'}}))
        result = GraspAction(robot_runtime=SimpleNamespace(gripper_controller=controller)).execute({})
        self.assertIn('稳定的双侧接触力', result['error']['error_message'])
        self.assertNotIn('未检测到有效接触力', result['error']['error_message'])


if __name__ == '__main__':
    unittest.main()
