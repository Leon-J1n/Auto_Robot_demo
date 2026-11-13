"""Scene 3 uses RGB-D for the apple grasp and basket placement."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from demo.interaction.vlm_grasp import VLMGraspResolver
from demo.interaction.visual_targets import (
    infer_command_point_types, infer_command_place_cycles, infer_visual_targets)
from atomic_skill.runner import TaskSkillExecutor
from execution.blackboard import TargetBlackboard
from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


ROOT = Path(__file__).resolve().parents[1]


class Commands003VLMPlaceTests(unittest.TestCase):
    def test_grasp_is_cached_and_place_is_shared_within_its_cycle(self):
        # Keep this resolver regression independent of the user's editable demo targets.
        commands = {'commands': [
            {'command_id': str(index), 'skill_name': skill, 'parameters': {'target': target}}
            for index, skill, target in ((1, 'approach', 7), (2, 'pick', 7),
                (6, 'approach', 12), (7, 'put', 12), (8, 'retreat', 12),
                (10, 'pick', 8), (15, 'put', 13))]}
        self.assertNotIn('visual_estimates', commands)
        visual_targets = infer_visual_targets(commands['commands'])
        visual_command_point_types = infer_command_point_types(commands['commands'])
        self.assertEqual([(item['target_id'], item['point_type']) for item in visual_targets],
                         [(7, 'grasp'), (12, 'place'), (8, 'grasp'), (13, 'place')])
        objects = [
            {'object_id_in_scene': ident, 'object_name_in_scene': name,
             'components': [{'component_type': 'MetadataRef', 'properties': {
                 'type': 'model', 'path': f'$LIBRARY_SERVER/{model_id}'}}]}
            for ident, name, model_id in ((7, 'Apple', 1), (12, 'Basket', 12))
        ]
        resources = SimpleNamespace(
            scene={'objects': objects},
            robot_object={'components': [{'component_type': 'GripperModel',
                'properties': {'urdf': 'robots/2/robotiq_2f85.urdf'}}]},
            asset=lambda path: ROOT / 'assets' / path,
        )
        metadata = {model_id: json.loads((ROOT / f'assets/models/{model_id}/{name}.json').read_text(encoding='utf-8'))
                    for model_id, name in ((1, 'apple'), (12, 'basket'))}
        client = SimpleNamespace(base_url='http://unused', uid=0,
                                 get_model=Mock(side_effect=lambda ident: metadata[ident]))
        frame = CameraFrame(
            camera_id='global', timestamp=0., sequence=1,
            rgb=np.zeros((480, 640, 3), dtype=np.uint8),
            depth=np.full((480, 640), .98, dtype=np.float32),
            intrinsics=CameraIntrinsics(640, 480, 500., 500., 320., 240.),
            camera_pose=CameraPose(np.array([0., 0., 1.6]),
                                   np.eye(3), np.array([1., 0., 0., 0.])),
        )
        backend = SimpleNamespace(
            get_camera_frame=Mock(return_value=frame),
            get_end_effector_pose=Mock(return_value={
                'position': np.array([0., 0., .9]), 'rotation_matrix': np.eye(3)}),
            contact_target=None, contact_target_object_id=None,
        )
        estimator = SimpleNamespace(estimate_frame=Mock(side_effect=[
            {'world_point': {'x': 0., 'y': 0., 'z': .78}, 'confidence': .9},
            {'world_point': {'x': -.2, 'y': .15, 'z': .62},
             'pixel': {'x': 40, 'y': 40}, 'confidence': .8},
        ]))
        board = TargetBlackboard()
        resolver = VLMGraspResolver(client, board, backend,
                                    visual_targets=visual_targets,
                                    command_point_types=visual_command_point_types,
                                    command_place_cycles=infer_command_place_cycles(commands['commands']),
                                    estimator=estimator)
        objects_state = {
            'Apple': {'position': [0., 0., .75], 'quaternion_xyzw': [0., 0., 0., 1.]},
            'Basket': {'position': [-.2, .15, .6], 'quaternion_xyzw': [0., 0., 0., 1.]},
        }
        grasp_state = {'simulation_time': 0., 'objects': objects_state, 'gripper': {'holding': False}}
        place_state = {'simulation_time': 1., 'objects': objects_state,
                       'gripper': {'holding': True, 'object_id': 'Apple'}}
        with patch('demo.interaction.vlm_grasp.control_from_grasp_frame', return_value=np.eye(4)):
            resolver.set_command_context('2')
            apple = resolver.resolve(7, resources, grasp_state)
            resolver.set_command_context('7')
            basket = resolver.resolve(12, resources, place_state)
            basket_again = resolver.resolve(12, resources, place_state)

        self.assertEqual(estimator.estimate_frame.call_args_list[0].kwargs['point_type'], 'grasp')
        self.assertEqual(estimator.estimate_frame.call_args_list[1].kwargs['point_type'], 'place')
        self.assertEqual(estimator.estimate_frame.call_count, 2)
        self.assertEqual(backend.get_camera_frame.call_count, 2)
        self.assertLess(estimator.estimate_frame.call_args_list[1].args[0].intrinsics.width, 640)
        self.assertEqual(apple['candidates'][0]['pose_source'], 'vlm_rgbd_grasp')
        self.assertEqual(basket['candidates'][0]['pose_source'], 'vlm_rgbd_place')
        np.testing.assert_allclose(np.asarray(basket['candidates'][0]['world_from_grasp'])[:3, :3],
                                   [[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])
        np.testing.assert_allclose(basket_again['candidates'][0]['world_from_grasp'],
                                   basket['candidates'][0]['world_from_grasp'])
        estimate = board.snapshot()['visual_places']['Basket']['estimate']
        self.assertAlmostEqual(estimate['world_point']['z'], .817471, delta=.005)
        self.assertAlmostEqual(estimate['rgbd_world_point']['z'], .62, delta=.005)
        self.assertAlmostEqual(estimate['target_point_world']['z'],
                               estimate['world_point']['z'])
        self.assertNotIn('held_bottom_offset_m', estimate)
        self.assertNotIn('placement_clearance_m', estimate)
        self.assertNotIn('placement_world_z_offset_m', estimate)
        self.assertEqual(estimate['placement_height_candidates_m'], [cm / 100. for cm in range(-3, 6)])
        self.assertEqual(estimate['held_object'], 'Apple')
        self.assertNotIn('floor_pixel', estimate)
        self.assertEqual(estimate['world_point'], {'x': -.2, 'y': .15, 'z': .817471})

    def test_retreat_after_visual_place_uses_current_pose(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.resolver = SimpleNamespace(is_visual_place=lambda target: target == 12,
                                            resolve=Mock(side_effect=AssertionError('must not reselect basket')))
        executor.resources = SimpleNamespace()
        executor.backend = SimpleNamespace(set_frame_callback=Mock())
        executor.sync = executor.kinematics = executor.execute_motion = object()
        executor.checker = SimpleNamespace(allow_gripper_environment_contact=False)
        executor.write_frame = Mock()
        executor.stage = None
        row = {}
        pose_search = Mock()
        pose_search.phase = 'retreat'
        pose_search.run.side_effect = lambda *_: (
            self.assertTrue(executor.checker.allow_gripper_environment_contact) or
            {'success': True, 'substeps': []})
        transform = np.eye(4)
        transform[:3, 3] = [0., 0., .145]
        reference_pose = np.diag([1., -1., -1., 1.])
        executor._remember_interaction(
            {'command_id': '6', 'skill_name': 'put', 'parameters': {'target': 12}},
            {'selected': {'world_from_control': reference_pose}, 'control_from_grasp': transform})
        with patch('atomic_skill.runner.PoseSearchExecutor', return_value=pose_search):
            result = executor._execute_pose_search(
                {'command_id': '7', 'skill_name': 'retreat', 'parameters': {'target': 12}}, {}, row)
        self.assertTrue(result['success'])
        params = pose_search.run.call_args.args[1]
        self.assertNotIn('target', params)
        self.assertEqual(params['grasp_offset_local'], [0., 0., .145])
        np.testing.assert_allclose(params['reference_rotation_world'], reference_pose[:3, :3])
        self.assertEqual(result['retreat_reference']['skill_name'], 'put')
        executor.resolver.resolve.assert_not_called()
        self.assertFalse(executor.checker.allow_gripper_environment_contact)

    def test_retreat_collision_allowance_is_restored_on_error(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.resolver = SimpleNamespace(is_visual_place=lambda target: True)
        executor.resources = SimpleNamespace()
        executor.backend = SimpleNamespace(set_frame_callback=Mock())
        executor.sync = executor.kinematics = executor.execute_motion = object()
        executor.checker = SimpleNamespace(allow_gripper_environment_contact=False)
        executor.write_frame = Mock()
        pose_search = Mock()
        pose_search.phase = 'retreat'
        pose_search.run.side_effect = RuntimeError('planning failed')
        executor._remember_interaction(
            {'command_id': '6', 'skill_name': 'put', 'parameters': {'target': 12}},
            {'selected': {'world_from_control': np.eye(4)}, 'control_from_grasp': np.eye(4)})
        with patch('atomic_skill.runner.PoseSearchExecutor', return_value=pose_search):
            with self.assertRaisesRegex(RuntimeError, 'planning failed'):
                executor._execute_pose_search(
                    {'command_id': '7', 'skill_name': 'retreat', 'parameters': {'target': 12}}, {}, {})
        self.assertFalse(executor.checker.allow_gripper_environment_contact)

    def test_collision_allowance_covers_retreat_resolution_only(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.checker = SimpleNamespace(allow_gripper_environment_contact=False)
        observed = []
        def inspect(command, *_):
            observed.append((command['skill_name'], executor.checker.allow_gripper_environment_contact))
            return {}
        executor._execute_pose_search_inner = inspect
        for skill in ('approach', 'retreat'):
            executor._execute_pose_search({'skill_name': skill}, {}, {})
            self.assertFalse(executor.checker.allow_gripper_environment_contact)
        self.assertEqual(observed, [('approach', False), ('retreat', True)])


if __name__ == '__main__':
    unittest.main()
