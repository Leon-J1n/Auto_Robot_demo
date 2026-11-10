import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

from control.continuous_trajectory import ContinuousJointPath
from execution.convergence import GoalConvergence, TrajectoryTimeout, ContactMonitor
from planning.time_parameterization import prepare_continuous_trajectory
from planning.errors import PlanningError


class LocalTimingTest(unittest.TestCase):
    def test_local_bend_does_not_slow_entire_path_and_respects_limits(self):
        s = np.linspace(0, 1, 101)
        points = np.column_stack([s, .05 * np.exp(-((s - .4) / .025)**2)])
        path = ContinuousJointPath(points, s)
        vmax, amax = np.array([1.8, 1.8]), np.array([3., 3.])
        _, old_duration = path.samples(1, 0, None, vmax, amax, .001, corner_slowdown=.25)
        q, duration = path.local_samples(vmax, amax, .001, slowdown=.25)
        self.assertLess(duration, old_duration * .7)
        np.testing.assert_allclose(q[[0, -1]], points[[0, -1]], atol=1e-12)
        self.assertGreaterEqual(np.min(np.diff(q[:, 0])), -1e-12)
        self.assertTrue(np.all(np.max(abs(np.diff(q, axis=0)/.001), axis=0) <= vmax))
        self.assertTrue(np.all(np.max(abs(np.diff(q, n=2, axis=0)/.001**2), axis=0) <= amax))
        self.assertAlmostEqual(duration/.001, round(duration/.001))

    @staticmethod
    def fixture():
        points = np.zeros((9, 6)); points[:, 0] = np.linspace(0, np.pi/2, 9)
        trajectory = {'waypoints': points.tolist(), 'cartesian_joint_waypoints': points.tolist(),
                      'diagnostics': {'center': [0, 0, 0], 'axis': [0, 0, 1], 'angle': np.pi/2}}
        request = {'motion': {'path_type': 'circular'}, 'constraints': {'velocity_scale': .6, 'acceleration_scale': 1.},
                   'planning': {'time_parameterization': {'max_velocity': [3.] * 6, 'max_acceleration': [3.] * 6}}}
        def forward(q):
            return {'position': np.array([np.cos(q[0]), np.sin(q[0]), 0]),
                    'rotation_matrix': Rotation.from_euler('z', q[0]).as_matrix()}
        return trajectory, request, SimpleNamespace(forward=forward)

    def test_arc_validation_and_source_points_survive_densification(self):
        tr, req, kin = self.fixture()
        tr['waypoints'] = np.repeat(tr['waypoints'], 3, axis=0).tolist()
        result = prepare_continuous_trajectory(tr, req, kinematics=kin)
        self.assertEqual(len(result['geometric_waypoints']), 9)
        self.assertEqual(result['diagnostics']['time_parameterization']['time_law'], 'local_acceleration_bounds')
        self.assertLess(result['diagnostics']['circular_path_validation']['max_position_error_m'], 1e-10)

    def test_arc_deviation_fails_and_explicit_jerk_is_not_ignored(self):
        tr, req, kin = self.fixture()
        bad_kin = SimpleNamespace(forward=lambda q: {'position': np.array([1., 0., 0.]),
                                                    'rotation_matrix': np.eye(3)})
        with self.assertRaises(PlanningError):
            prepare_continuous_trajectory(tr, req, kinematics=bad_kin)
        req['planning']['time_parameterization']['max_jerk'] = [10.] * 6
        with patch.object(ContinuousJointPath, 'local_samples', side_effect=AssertionError('must retain jerk limits')):
            result = prepare_continuous_trajectory(tr, req, kinematics=kin)
        self.assertEqual(result['diagnostics']['time_parameterization']['time_law'], 'global_quintic')


class GoalConvergenceTest(unittest.TestCase):
    def values(self, **overrides):
        return dict(reference_complete=True, joint_error=.00105, joint_speed=.001,
                    position_error=.0007, orientation_error=.001,
                    holding_required=True, contact_valid=True, **overrides)

    def test_loaded_static_offset_needs_pose_accuracy_and_stable_window(self):
        goal = GoalConvergence()
        self.assertFalse(goal.update(1., **self.values())['converged'])
        self.assertFalse(goal.update(1.05, **self.values())['converged'])
        self.assertTrue(goal.update(1.1, **self.values())['converged'])

    def test_obstruction_contact_loss_and_moving_endpoints_never_pass(self):
        for key, value in [('reference_complete', False), ('joint_error', .003),
                           ('joint_speed', .02), ('position_error', .003),
                           ('orientation_error', .03), ('contact_valid', False),
                           ('holding_required', False)]:
            with self.subTest(key=key):
                values = self.values(); values[key] = value
                goal = GoalConvergence()
                goal.update(1., **values)
                self.assertFalse(goal.update(2., **values)['converged'])

    def test_unstable_sample_resets_window_and_stricter_request_is_honored(self):
        goal = GoalConvergence()
        goal.update(1., **self.values())
        values = self.values(); values['contact_valid'] = False
        goal.update(1.05, **values)
        self.assertFalse(goal.update(1.1, **self.values())['converged'])
        self.assertTrue(goal.update(1.2, **self.values())['converged'])
        strict = GoalConvergence(position_tolerance=.0001)
        self.assertFalse(strict.update(2., **self.values())['converged'])

    def test_timeout_retains_machine_readable_reason(self):
        for reason in ['wall_time_limit', 'goal_not_converged']:
            exc = TrajectoryTimeout(reason, {'joint_error_rad': .00105, 'planned_duration_s': 5.})
            self.assertIsInstance(exc, TimeoutError)
            self.assertEqual(exc.details['reason'], reason)

    def test_contact_chatter_is_recorded_but_sustained_loss_latches_failure(self):
        monitor = ContactMonitor()
        lost = {'contact_lost': True, 'relative_position_error_m': .0001,
                'relative_orientation_error_rad': .0001}
        for t in (0., .002, .004):
            self.assertIsNone(monitor.update(lost, t))
        self.assertIsNone(monitor.update({'contact_lost': False}, .006))
        self.assertAlmostEqual(monitor.snapshot()['longest_contact_loss_s'], .006)
        self.assertIsNone(monitor.update(lost, 1.))
        failure = monitor.update(lost, 1.1)
        self.assertEqual(failure['error_code'], 'GRASP_FAILED')
        self.assertIs(monitor.update({'contact_lost': False}, 1.2), failure)

    def test_contact_loss_with_measured_slip_fails_immediately(self):
        for slip in [{'relative_position_error_m': .006}, {'relative_orientation_error_rad': .06}]:
            monitor = ContactMonitor()
            self.assertIsNotNone(monitor.update({'contact_lost': True, **slip}, 0.))


class ServoExecutionTest(unittest.TestCase):
    @staticmethod
    def backend():
        import mujoco
        from execution.mujoco import MuJoCoExecutionBackend
        runtime = MuJoCoExecutionBackend()
        runtime.model = mujoco.MjModel.from_xml_string('''<mujoco>
          <option timestep="0.002" gravity="0 0 0" integrator="implicitfast"/>
          <worldbody><body><joint name="j" type="hinge" range="-120 120"/>
            <geom type="sphere" size="0.1" mass="1"/><site name="tcp" pos="0.1 0 0"/>
          </body></worldbody><actuator><position joint="j" kp="1200" kv="90"/>
          </actuator></mujoco>''')
        runtime.data = mujoco.MjData(runtime.model)
        runtime.joint_names = ['j']
        runtime.joint_ids = runtime.qpos_indices = runtime.dof_indices = runtime.actuator_ids = np.array([0])
        runtime.tip_id = runtime.base_id = 0
        runtime.object_ids = {}
        runtime.gripper_controller = runtime.viewer = runtime.frame_callback = None
        runtime.physics_steps = 0
        runtime.frame_interval = 1/60
        mujoco.mj_forward(runtime.model, runtime.data)
        return runtime

    @staticmethod
    def trajectory(feedforward):
        t = np.linspace(0, 1, 501)
        q = .5*t**3*(10 - 15*t + 6*t*t)
        return {'timestamps': t.tolist(), 'time_parameterized_waypoints': q[:, None].tolist(),
                'diagnostics': {'time_parameterization': {
                    'time_law': 'local_acceleration_bounds' if feedforward else 'global_quintic'}}}

    def test_feedforward_reduces_pd_lag_without_teleporting(self):
        raw = self.backend().execute_trajectory(self.trajectory(False))
        fed = self.backend().execute_trajectory(self.trajectory(True))
        self.assertLess(fed['max_tracking_error_rad'], raw['max_tracking_error_rad'] * .2)
        self.assertTrue(fed['completion']['converged'])
        self.assertGreater(fed['physics_steps'], 500)

    def test_stalled_servo_has_structured_settling_timeout(self):
        backend = self.backend()
        backend.model.actuator_gainprm[0, 0] = 0
        backend.model.actuator_biasprm[0, :] = 0
        with self.assertRaises(TrajectoryTimeout) as caught:
            backend.execute_trajectory(self.trajectory(False))
        self.assertEqual(caught.exception.details['reason'], 'goal_not_converged')
        self.assertGreater(caught.exception.details['joint_error_rad'], .4)

    def test_wall_timeout_is_distinct_from_settling_timeout(self):
        with self.assertRaises(TrajectoryTimeout) as caught:
            self.backend().execute_trajectory(self.trajectory(False), timeout=1e-9)
        self.assertEqual(caught.exception.details['reason'], 'wall_time_limit')
        self.assertLess(caught.exception.details['simulation_elapsed_s'], 1.)


if __name__ == '__main__':
    unittest.main()
