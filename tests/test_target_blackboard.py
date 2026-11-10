import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from scipy.spatial.transform import Rotation

from execution.blackboard import TargetBlackboard
from demo.interaction.object_library import ObjectLibraryGraspResolver


def pose(x=0.0, yaw=0.0):
    return {'position': [x, 0.0, 0.0],
            'quaternion_xyzw': Rotation.from_euler('z', yaw).as_quat().tolist()}


def client():
    return SimpleNamespace(base_url='http://ols.test', uid=0, get_model=Mock(return_value={
        'semantic_properties': {'object_name': 'target'},
        'grasp_properties': {'pose_count': 1, 'poses': [[1, 0, 0, 0, 0, 0, 1, 0, 0, 0]]},
    }))


def resources():
    return SimpleNamespace(scene={'objects': [{
        'object_id_in_scene': 9, 'object_name_in_scene': 'Cabinet',
        'components': [{'component_type': 'MetadataRef',
                        'properties': {'type': 'model', 'path': '$LIBRARY_SERVER/28'}}],
    }]})


class TargetBlackboardTest(unittest.TestCase):
    @patch('demo.interaction.object_library.control_from_grasp_frame', return_value=np.eye(4))
    def test_resolve_fetches_once_and_uses_updated_world_pose(self, _):
        service = client()
        board = TargetBlackboard()
        resolver = ObjectLibraryGraspResolver(service, board)
        first = resolver.resolve(9, resources(), {'simulation_time': 0, 'objects': {'Cabinet': pose()}})
        board.observe({'simulation_time': 1, 'objects': {'Cabinet': pose(2)}})
        # A stale command snapshot must not roll the measured target back.
        second = resolver.resolve(9, resources(), {'simulation_time': 0, 'objects': {'Cabinet': pose()}})
        service.get_model.assert_called_once_with(28)
        self.assertAlmostEqual(first['candidates'][0]['world_from_grasp'][0][3], 1)
        self.assertAlmostEqual(second['candidates'][0]['world_from_grasp'][0][3], 3)
        copy = board.pose('Cabinet')
        copy['position'][0] = 999
        self.assertEqual(board.pose('Cabinet')['position'][0], 2)

    @patch('demo.interaction.object_library.control_from_grasp_frame', return_value=np.eye(4))
    def test_grasp_point_follows_contacted_door_after_release(self, _):
        service = client()
        board = TargetBlackboard()
        resolver = ObjectLibraryGraspResolver(service, board)
        state = {'simulation_time': 0, 'objects': {'Cabinet': pose(), 'Cabinet__door': pose()}}
        resolved = resolver.resolve(9, resources(), state)
        resolver.remember_selection({**resolved, 'selected': resolved['candidates'][0]})
        board.observe({**state, 'simulation_time': 1,
                       'gripper': {'holding': True, 'object_id': 'Cabinet__door'}})
        moved = {'simulation_time': 2, 'objects': {'Cabinet': pose(), 'Cabinet__door': pose(yaw=np.pi / 2)},
                 'mechanisms': {'Cabinet__door': {'joint_position': np.pi / 2}},
                 'gripper': {'holding': False, 'object_id': None}}
        board.observe(moved)
        result = resolver.resolve(9, resources(), moved)
        grasp = np.array(result['candidates'][0]['world_from_grasp'])
        np.testing.assert_allclose(grasp[:3, 3], [0, 1, 0], atol=1e-12)
        self.assertEqual(result['candidates'][0]['pose_source'], 'blackboard_contact_link')
        service.get_model.assert_called_once_with(28)
        self.assertEqual(board.snapshot()['mechanisms']['Cabinet__door']['joint_position'], np.pi / 2)

    def test_failed_fetch_retries_and_cache_is_scoped_by_server(self):
        service = client()
        metadata = service.get_model.return_value
        service.get_model.side_effect = [RuntimeError('offline'), metadata, metadata]
        board = TargetBlackboard()
        with self.assertRaises(RuntimeError):
            board.model(service, 28)
        board.model(service, 28)
        returned = board.model(service, 28)
        returned['grasp_properties']['poses'].clear()
        self.assertEqual(len(board.model(service, 28)['grasp_properties']['poses']), 1)
        service.base_url = 'http://other-ols.test'
        board.model(service, 28)
        self.assertEqual(service.get_model.call_count, 3)
        self.assertEqual(TargetBlackboard().snapshot()['model_cache']['entries'], 0)


if __name__ == '__main__':
    unittest.main()
