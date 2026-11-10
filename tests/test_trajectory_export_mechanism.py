import struct
import tempfile
import unittest
from pathlib import Path

from execution.trajectory_export import validate_traj, write_traj


def component(kind, properties):
    return {'component_type': kind, 'properties': properties}


class MechanismTrajectoryExportTest(unittest.TestCase):
    def test_passive_joint_uses_measured_angle_instead_of_gripper_width(self):
        scene = {'objects': [
            {'object_id_in_scene': 2, 'object_name_in_scene': 'Robot', 'components': [
                component('RobotDriver', {'joint_sequence': ['j1'] * 6})]},
            {'object_id_in_scene': 9, 'object_name_in_scene': 'Cabinet', 'components': [
                component('Transform', {'position': [-0.3, 0.175, 0.6],
                                        'quaternion_xyzw': [0, 0, 0, 1]}),
                component('RobotDriver', {'role': 'passive',
                                          'joint_sequence': ['door_joint']})]},
            {'object_id_in_scene': 3, 'object_name_in_scene': 'Gripper', 'components': [
                component('RobotAttachment', {'parent_object_id': 2}),
                component('RobotDriver', {'joint_sequence': ['finger_joint']})]},
        ]}
        frames = [
            {'frame_index': 0, 'joints': [0] * 6, 'gripper': {'width': 0.08},
             'mechanisms': {'Cabinet__door_joint': {'joint_position': 0.0}}},
            {'frame_index': 1, 'joints': [0] * 6, 'gripper': {'width': 0.04},
             'mechanisms': {'Cabinet__door_joint': {'joint_position': 1.2}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = write_traj(Path(directory) / 'door.traj', scene, frames,
                              initial_joints=[0] * 6)
            summary = validate_traj(path)
            self.assertEqual(summary['total_frames'], 62)
            raw = path.read_bytes()
            entries = {}
            cursor = 32
            for _ in range(summary['object_count']):
                object_id, _, pose_dim, segments = struct.unpack_from('<IIHH', raw, cursor)
                cursor += 12 + pose_dim * 4
                start, count, offset, _ = struct.unpack_from('<IIII', raw, cursor)
                cursor += segments * 16
                entries[object_id] = struct.unpack_from(f'<{count * pose_dim}f', raw, offset)
            self.assertAlmostEqual(entries[9][61 * 7], 1.2, places=6)
            self.assertEqual(entries[9][61 * 7 + 1:62 * 7], (0.0,) * 6)
            self.assertGreater(entries[3][61 * 7], 0.0)


if __name__ == '__main__':
    unittest.main()
