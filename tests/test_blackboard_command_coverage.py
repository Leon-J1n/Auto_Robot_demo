"""Shared command state must not depend on task filenames or skill kinds."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

import numpy as np

from execution.blackboard import TargetBlackboard
from execution.resources import SceneStateSynchronizer
from demo.interaction.object_library import ObjectLibraryGraspResolver
from demo.interaction.requests import motion_request
from atomic_skill.pour import DemoPourExecutor


def pose(x):
    return {'position': [x, 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.]}


def scene_object(target_id, name, model_id):
    return {'object_id_in_scene': target_id, 'object_name_in_scene': name,
            'components': [{'component_type': 'MetadataRef', 'properties': {
                'type': 'model', 'path': f'$LIBRARY_SERVER/{model_id}'}}]}


def runtime():
    state = {'simulation_time': 0., 'joints': [0.], 'base_transform': np.eye(4).tolist(),
             'objects': {'Apple': pose(0)}, 'gripper': {'holding': False}}
    backend = SimpleNamespace(get_state=lambda: deepcopy(state))
    kin = SimpleNamespace(robot=SimpleNamespace(
        set_current_joints=Mock(), base_transform=lambda joints: np.eye(4)))
    checker = Mock()
    return state, backend, kin, checker


class BlackboardCommandCoverageTest(unittest.TestCase):
    def test_default_boards_are_run_scoped(self):
        first, second = SceneStateSynchronizer(), SceneStateSynchronizer()
        first.blackboard.observe({'objects': {'Apple': pose(2)}})
        self.assertIsNone(second.blackboard.pose('Apple'))

    def test_command_boundaries_update_even_without_recorded_frames(self):
        state, backend, kin, checker = runtime()
        sync = SceneStateSynchronizer()
        with sync.command_state(backend, kin, checker):
            self.assertEqual(sync.blackboard.pose('Apple'), pose(0))
            state.update(simulation_time=1., objects={'Apple': pose(3)})
        self.assertEqual(sync.blackboard.pose('Apple'), pose(3))
        self.assertEqual(sync.blackboard.simulation_time, 1.)

    def test_partial_failure_updates_endpoint_and_preserves_exception(self):
        state, backend, kin, checker = runtime()
        sync = SceneStateSynchronizer()
        with self.assertRaisesRegex(RuntimeError, 'motion failed'):
            with sync.command_state(backend, kin, checker):
                state.update(simulation_time=2., objects={'Apple': pose(4)})
                raise RuntimeError('motion failed')
        self.assertEqual(sync.blackboard.require_pose('Apple'), pose(4))

    def test_scene_registration_includes_unqueried_instances(self):
        board = TargetBlackboard()
        board.register_scene({'objects': [scene_object(1, 'Apple', 1),
                                          scene_object(2, 'AppleCopy', 1),
                                          scene_object(9, 'Drawer', 25)]})
        snapshot = board.snapshot()
        self.assertEqual(set(snapshot['targets']), {'1', '2', '9'})
        self.assertEqual(snapshot['targets']['9']['model_id'], 25)
        self.assertEqual(snapshot['model_cache']['entries'], 0)

    @patch('demo.interaction.object_library.control_from_grasp_frame', return_value=np.eye(4))
    def test_rigid_and_articulated_targets_share_cache_and_latest_pose(self, _):
        for name, model_id, body in [('Apple', 1, 'Apple'), ('Drawer', 25, 'Drawer__upper'),
                                     ('Cabinet', 28, 'Cabinet__door')]:
            with self.subTest(target=name):
                board = TargetBlackboard()
                client = SimpleNamespace(base_url='test', uid=0, get_model=Mock(return_value={
                    'semantic_properties': {'object_name': name},
                    'grasp_properties': {'pose_count': 1, 'poses': [[1, 0, 0, 0, 0, 0, 1, 0, 0, 0]]}}))
                resolver = ObjectLibraryGraspResolver(client, board)
                resources = SimpleNamespace(scene={'objects': [scene_object(9, name, model_id)]})
                initial = {'simulation_time': 0, 'objects': {name: pose(0), body: pose(0)}}
                resolved = resolver.resolve(9, resources, initial)
                resolver.remember_selection({**resolved, 'selected': resolved['candidates'][0]})
                board.observe({**initial, 'simulation_time': 1,
                               'gripper': {'holding': True, 'object_id': body}})
                board.observe({'simulation_time': 2, 'objects': {body: pose(3)},
                               'gripper': {'holding': False, 'object_id': None}})
                result = resolver.resolve(9, resources, initial)
                self.assertAlmostEqual(result['candidates'][0]['world_from_grasp'][0][3], 4.)
                client.get_model.assert_called_once_with(model_id)

    def test_link_request_uses_latest_blackboard_pose_without_native_model_access(self):
        obj = scene_object(9, 'Drawer', 25)
        obj['components'].append({'component_type': 'ArticulatedModel',
                                  'properties': {'links': ['upper']}})
        resources = SimpleNamespace(scene={'objects': [obj], 'move_defaults': {}})
        board = TargetBlackboard()
        board.observe({'simulation_time': 2, 'objects': {'Drawer__upper': pose(3)}})
        backend = SimpleNamespace(get_state=lambda: {
            'simulation_time': 1, 'objects': {'Drawer__upper': pose(0)}})
        result = motion_request(resources, backend, {
            'target': 9, 'link': 'upper', 'mechanism_pose': {
                'position': [1, 0, 0], 'quaternion_xyzw': [0, 0, 0, 1], 'frame': 'control'}},
            blackboard=board)
        self.assertEqual(result['target']['position'], {'x': 4., 'y': 0., 'z': 0.})

    def test_pour_resolves_destination_from_shared_blackboard(self):
        state, backend, kin, checker = runtime()
        state['objects'] = {'Bottle': pose(0), 'Cup': pose(0)}
        backend.gripper_controller = SimpleNamespace(held_name='Bottle')
        backend.get_joint_positions = lambda: [0.]
        kin.forward = lambda joints: {'position': np.zeros(3), 'rotation_matrix': np.eye(3)}
        board = TargetBlackboard()
        board.observe({'simulation_time': 2, 'objects': {'Bottle': pose(0), 'Cup': pose(3)}})
        sync = SceneStateSynchronizer(board)
        requests = []
        def execute(request):
            requests.append(request)
            return {'success': True}
        executor = DemoPourExecutor(resources=None, backend=backend, kinematics=kin,
            execute_motion=execute, synchronizer=sync, checker=checker, report={})
        result = executor.execute({'source': 'Bottle', 'target': 'Cup',
                                   'pour_tilt_deg': 140, 'tilt_step_deg': 140}, {})
        self.assertTrue(result['success'])
        self.assertAlmostEqual(requests[1]['target']['position']['x'], 3.)


if __name__ == '__main__':
    unittest.main()
