import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from execution.trajectory_export import _command_segments, validate_traj, write_traj


class CommandSegmentsTest(unittest.TestCase):
    def test_commands_share_boundaries_and_preserve_all_samples(self):
        scene = {'objects': [
            {'object_id_in_scene': 2, 'object_name_in_scene': 'Robot', 'components': [
                {'component_type': 'RobotDriver', 'properties': {'joint_sequence': ['j'] * 6}}]},
            {'object_id_in_scene': 9, 'object_name_in_scene': 'Drawer', 'components': [
                {'component_type': 'RobotDriver', 'properties': {'joint_sequence': ['slide'], 'role': 'passive'}}]},
        ]}
        frames = [dict(frame_index=i, command_id=command, substep=phase,
                       joints=[i / 10] * 6, mechanisms={'Drawer__slide': {'joint_position': i / 100}})
                  for i, (command, phase) in enumerate([
                      ('setup', 'initial'), ('pick-A', 'contact'), ('pick-A', 'grasp'),
                      ('pull-B', 'motion'), ('pull-B', 'release'), ('close-C', 'motion')])]
        with tempfile.TemporaryDirectory() as directory:
            path = write_traj(Path(directory) / 'task.traj', scene, frames, initial_joints=[0] * 6)
            result = validate_traj(path)
            self.assertEqual(result['total_frames'], len(frames) + 2 * 60)
            expected = [{'start_frame': start, 'frame_count': count, 'step_count': step}
                        for start, count, step in [(0, 1, 0), (1, 62, 1), (63, 62, 2), (125, 1, 3)]]
            raw = path.read_bytes()
            cursor = 32
            for obj in result['objects']:
                self.assertEqual(obj['segments'], expected)
                oid, _, dim, n = struct.unpack_from('<IIHH', raw, cursor)
                cursor += 12 + dim * 4
                restored = []
                for _ in range(n):
                    start, count, offset, step = struct.unpack_from('<IIII', raw, cursor)
                    cursor += 16
                    samples = np.frombuffer(raw, dtype='<f4', count=count * dim,
                                            offset=offset).reshape(count, dim)
                    if count > 1:
                        np.testing.assert_array_equal(samples[1:61], np.repeat(samples[:1], 60, axis=0))
                        samples = np.concatenate((samples[:1], samples[61:]))
                    restored.append(samples)
                restored = np.concatenate(restored)
                np.testing.assert_allclose(restored[:, 0], np.arange(6) / (10 if oid == 2 else 100), atol=1e-7)

    def test_wait_is_one_second_at_different_frame_rates(self):
        scene = {'objects': [{'object_id_in_scene': 2, 'object_name_in_scene': 'Robot',
                             'components': [{'component_type': 'RobotDriver',
                                             'properties': {'joint_sequence': ['j'] * 6}}]}]}
        frames = [{'frame_index': i, 'command_id': str(i // 2), 'joints': [i] * 6} for i in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            for fps in (24, 30, 60):
                path = write_traj(Path(directory) / 'holds.traj', scene, frames, initial_joints=[0] * 6, fps=fps)
                summary = validate_traj(path)
                segments = summary['objects'][0]['segments']
                for previous, following in zip(segments, segments[1:]):
                    self.assertEqual(following['start_frame'] - previous['start_frame'] - previous['frame_count'], 0)
                self.assertEqual(summary['total_frames'], 6 + 3 * fps)
                raw = path.read_bytes()
                cursor = 32 + 12 + 7 * 4
                for i in range(3):
                    _, count, offset, _ = struct.unpack_from('<IIII', raw, cursor + i * 16)
                    values = np.frombuffer(raw, dtype='<f4', count=count * 7, offset=offset).reshape(count, 7)
                    np.testing.assert_array_equal(values[:fps + 1, :6], np.full((fps + 1, 6), 2 * i))
                    np.testing.assert_array_equal(values[fps + 1, :6], np.full(6, 2 * i + 1))
                # First sample appears immediately; no hold after the last sample.
                self.assertEqual(segments[0]['start_frame'], 0)
                self.assertEqual(segments[-1]['start_frame'] + segments[-1]['frame_count'], summary['total_frames'])

    def test_no_setup_does_not_add_empty_segment(self):
        self.assertEqual(_command_segments([{'frame_index': 0, 'command_id': '20'},
                                            {'frame_index': 1, 'command_id': '30'}], 2),
                         [(0, 1, 1), (1, 1, 2)])

    def test_empty_and_legacy_frames_keep_idle_segment(self):
        self.assertEqual(_command_segments([], 1), [(0, 1, 0)])
        self.assertEqual(_command_segments([{'frame_index': 0}, {'frame_index': 1}], 2), [(0, 2, 0)])

    def test_repeated_command_and_duplicate_frame_fail(self):
        for rows in ([{'frame_index': i, 'command_id': command} for i, command in enumerate(['a', 'b', 'a'])],
                     [{'frame_index': 0}, {'frame_index': 0}]):
            with self.assertRaises(ValueError):
                _command_segments(rows, 3)


if __name__ == '__main__':
    unittest.main()
