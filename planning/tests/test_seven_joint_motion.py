"""Motion validation and Cartesian path construction support seven-joint arms."""
from types import SimpleNamespace
import unittest
import numpy as np

from atomic_action.motion.common.utils.validation import validate_move_request
from atomic_action.motion.common.strategies import LinearMove
from kinematics.core.contracts import IKResult


class SevenJointMotionTests(unittest.TestCase):
    def test_seven_arm_joints_export_without_overwriting_joint7(self):
        from execution.output.trajectory_export import _pose_default, _frame_pose
        q = [i / 10 for i in range(7)]
        self.assertEqual(_pose_default(dict(object_id_in_scene=2), 2, 3, q), q)
        pose = _frame_pose(dict(joints=q, gripper=dict(grasp_value=.8)), 'nero', 2, 3,
                           {'nero': 2}, {})
        self.assertEqual(pose, q)

    def test_joint_request_and_ik_seed_accept_seven_coordinates(self):
        request = validate_move_request(dict(target=dict(type='joint', joint_positions=[0.] * 7),
                                            strategy_params=dict(ik_seed=[0.] * 7)))
        self.assertEqual(len(request['target']['joint_positions']), 7)
        self.assertEqual(len(request['strategy_params']['ik_seed']), 7)

    def test_linear_path_preserves_seven_joint_coordinates(self):
        def solve(pose, *, seed, **kwargs):
            q = np.asarray(seed).copy()
            q[:3] = pose['position']
            return IKResult(solutions=[q])
        kin = SimpleNamespace(solve_one=solve)
        context = dict(current_joint_state=np.zeros(7), current_pose=dict(
            position=np.zeros(3), quaternion_wxyz=[1., 0., 0., 0.]))
        request = validate_move_request(dict(target=dict(type='pose', frame='world',
            position=dict(x=.05, y=0., z=0.)), constraints=dict(avoid_collision=False)))
        path = LinearMove(kinematics=kin).build_path_request(request, context)
        self.assertEqual(np.asarray(path['joint_waypoints']).shape[1], 7)
        np.testing.assert_allclose(path['goal'][:3], [.05, 0., 0.])


if __name__ == '__main__':
    unittest.main()
