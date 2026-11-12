"""Hand-eye centering geometry and safe approach fallback regression tests."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

import test_approach_move_to_fallback as fixtures
from atomic_skill.approach import PoseSearchExecutor
from atomic_skill.wrist_observation import camera_aligned_pose
from execution.mujoco import MuJoCoExecutionBackend
from demo.interaction.wrist_refinement import coarse_point_projection


class WristObservationTests(unittest.TestCase):
    def test_centers_point_for_rotated_hand_eye_and_preserves_optical_depth(self):
        control = np.eye(4)
        control[:3, :3] = Rotation.from_euler('xyz', [.2, -.7, 1.2]).as_matrix()
        control[:3, 3] = [.3, -.2, .8]
        extrinsic = np.eye(4)
        extrinsic[:3, :3] = Rotation.from_euler('xyz', [.1, -.2, .3]).as_matrix()
        extrinsic[:3, 3] = [.045, -.02, -.12]
        camera = control @ extrinsic
        target = camera[:3, 3] + camera[:3, :3] @ [.03, -.04, -.25]
        old_control, old_extrinsic = control.copy(), extrinsic.copy()
        aligned, diagnostics = camera_aligned_pose(control, extrinsic, target)
        new_camera = aligned @ extrinsic
        camera_point = new_camera[:3, :3].T @ (target - new_camera[:3, 3])
        np.testing.assert_allclose(camera_point, [0, 0, -.25], atol=1e-12)
        np.testing.assert_array_equal(aligned[:3, :3], control[:3, :3])
        np.testing.assert_array_equal(control, old_control)
        np.testing.assert_array_equal(extrinsic, old_extrinsic)
        self.assertAlmostEqual(diagnostics['translation_distance_m'], .05)
        self.assertAlmostEqual(diagnostics['optical_depth_m'], .25)

    def test_centered_point_needs_no_translation_and_invalid_inputs_are_rejected(self):
        aligned, diag = camera_aligned_pose(np.eye(4), np.eye(4), [0, 0, -.2])
        np.testing.assert_array_equal(aligned, np.eye(4))
        self.assertEqual(diag['translation_distance_m'], 0.)
        bad = np.eye(4)
        bad[0, 0] = 2.
        for extrinsic, point in ((np.eye(4), [0, 0, .2]),
                                 (np.eye(4), [np.nan, 0, -.2]),
                                 (bad, [0, 0, -.2])):
            with self.assertRaises(ValueError):
                camera_aligned_pose(np.eye(4), extrinsic, point)

    def test_actual_projection_reports_center_and_out_of_frame(self):
        frame = SimpleNamespace(camera_pose=SimpleNamespace(position=np.zeros(3), rotation_matrix=np.eye(3)),
            intrinsics=SimpleNamespace(fx=400, fy=400, cx=319.5, cy=239.5, width=640, height=480))
        projection = coarse_point_projection(frame, [0, 0, -.2])
        self.assertEqual(projection['pixel'], {'x': 319.5, 'y': 239.5})
        self.assertTrue(projection['in_frame'])
        self.assertEqual(projection['distance_from_principal_point_px'], 0.)
        self.assertFalse(coarse_point_projection(frame, [1, 0, -.2])['in_frame'])
        self.assertFalse(coarse_point_projection(frame, [0, 0, .2])['in_front'])

    def test_backend_reads_rigid_calibration_without_rendering_and_rejects_fixed_camera(self):
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <camera name="global_camera" pos="0 0 2"/>
          <body pos=".2 .1 .3"><joint type="hinge" axis="0 0 1"/>
            <geom size=".01"/><body pos="0 0 .1"><site name="tcp" pos="0 0 .15"/>
              <camera name="wrist_camera" pos="0 -.045 .03"/>
            </body>
          </body></worldbody></mujoco>''')
        backend = MuJoCoExecutionBackend()
        backend.model, backend.data = model, mujoco.MjData(model)
        backend.tip_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, 'tcp')
        mujoco.mj_forward(model, backend.data)
        calibration = backend.get_control_from_camera('wrist')
        np.testing.assert_allclose(calibration[:3, 3], [0, -.045, -.12])
        backend.data.qpos[0] = .8
        mujoco.mj_forward(model, backend.data)
        np.testing.assert_allclose(backend.get_control_from_camera('wrist'), calibration, atol=1e-12)
        self.assertEqual(backend.data.time, 0.)
        with self.assertRaises(ValueError):
            backend.get_control_from_camera('global')

    def make_executor(self, results):
        backend = SimpleNamespace(physics_steps=0)
        executor = PoseSearchExecutor(resources=SimpleNamespace(scene={}), backend=backend,
            synchronizer=SimpleNamespace(sync=Mock()), kinematics=None, checker=None,
            execute_motion=Mock(side_effect=results))
        extrinsic = np.eye(4)
        extrinsic[:3, :3] = np.diag([1., -1., -1.])
        extrinsic[:3, 3] = [.045, 0, -.12]
        params = {'target_pose': {'position': [0, 0, 0]},
                  'search': {'min_distance': .1, 'max_distance': .1},
                  '_wrist_observation': {'control_from_camera': extrinsic.tolist(),
                                         'target_world': [0, 0, 0]}}
        return executor, params

    def test_aligned_approach_is_collision_planned_and_leaves_grasp_target_unchanged(self):
        executor, params = self.make_executor([{'success': True}])
        original = deepcopy(params)
        result = executor.run('approach', params)
        self.assertTrue(result['success'])
        self.assertEqual(result['wrist_observation']['status'], 'camera_aligned')
        request = executor.execute_motion.call_args.args[0]
        np.testing.assert_allclose(list(request['target']['position'].values()), [-.045, 0, -.1])
        self.assertEqual(request['planning']['mode'], 'collision_free')
        self.assertEqual(params, original)

    def test_planning_failure_falls_back_but_execution_failure_never_retries(self):
        planning_failure = {'success': False, 'error': {'failed_stage': 'planning'}}
        executor, params = self.make_executor([planning_failure, {'success': True}])
        result = executor.run('approach', params)
        self.assertTrue(result['success'])
        self.assertEqual(executor.execute_motion.call_count, 2)
        self.assertEqual(result['wrist_observation']['status'], 'fallback_to_gripper_approach')
        np.testing.assert_allclose(list(executor.execute_motion.call_args.args[0]['target']['position'].values()),
                                   [0, 0, -.1])
        for stage, steps, executed in (('execution', 0, False), ('planning', 1, False),
                                       ('planning', 0, True)):
            executor, params = self.make_executor([])
            def fail(request):
                executor.backend.physics_steps += steps
                return {'success': False, 'motion_executed': executed,
                        'error': {'failed_stage': stage}}
            executor.execute_motion.side_effect = fail
            self.assertFalse(executor.run('approach', params)['success'])
            executor.execute_motion.assert_called_once()

    def test_behind_camera_falls_back_without_attempting_invalid_observation(self):
        executor, params = self.make_executor([{'success': True}])
        params['_wrist_observation']['control_from_camera'] = np.eye(4).tolist()
        result = executor.run('approach', params)
        self.assertTrue(result['success'])
        executor.execute_motion.assert_called_once()
        self.assertEqual(result['wrist_observation']['status'], 'fallback_to_gripper_approach')
        self.assertEqual(len(result['wrist_observation']['rejected']), 1)

    def test_runner_passes_surface_and_calibration_only_for_visual_grasp(self):
        fixture = fixtures.ApproachMoveToFallbackTests()
        fixture.setUp()
        e, resolved = fixture.executor, fixture.resolved
        resolved['selected'].update(pose_source='vlm_rgbd_grasp', grasp_depth_offset_m=.015)
        resolved['visual_estimate'] = {'grasp_depth_offset_m': .01}
        grasp = fixture.goal @ np.asarray(resolved['control_from_grasp'])
        resolved['selected']['world_from_grasp'] = grasp.tolist()
        e.resolver.refine_after_approach = Mock()
        e.backend.get_control_from_camera = Mock(return_value=np.eye(4))
        # Stop after planning; this test checks handoff, not the VLM pipeline.
        from unittest.mock import patch
        with patch('atomic_skill.runner.PoseSearchExecutor') as executor:
            executor.return_value.run.return_value = {'success': False}
            fixture.run_approach()
            params = executor.return_value.run.call_args.args[1]
            np.testing.assert_allclose(params['_wrist_observation']['target_world'],
                                       grasp[:3, 3] - .015 * grasp[:3, 2])
            e.backend.get_control_from_camera.assert_called_once_with('wrist')
            e.backend.get_control_from_camera.reset_mock()
            fixture.command['parameters']['wrist_camera_alignment'] = False
            fixture.run_approach()
            self.assertNotIn('_wrist_observation', executor.return_value.run.call_args.args[1])
            e.backend.get_control_from_camera.assert_not_called()
            fixture.command['parameters'].pop('wrist_camera_alignment')
            resolved['selected']['pose_source'] = 'vlm_rgbd_place'
            fixture.run_approach()
            self.assertNotIn('_wrist_observation', executor.return_value.run.call_args.args[1])
            e.backend.get_control_from_camera.assert_not_called()


if __name__ == '__main__':
    unittest.main()
