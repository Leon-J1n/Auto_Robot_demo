from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from execution.config import load_global_config, trajectory_settings, validate_config
from execution.resources import apply_default_scene_settings
from execution.trajectory_export import write_traj, validate_traj
from planning.time_parameterization import continuous_motion_limits
from atomic_action.motion.common.utils.validation import validate_move_request
from demo.interaction.requests import apply_global_motion_timing


class GlobalConfigTest(unittest.TestCase):
    def test_defaults_and_explicit_motion_scales(self):
        scene = apply_default_scene_settings({})
        self.assertEqual(scene['move_defaults']['constraints']['velocity_scale'], .6)
        self.assertEqual(scene['move_defaults']['constraints']['acceleration_scale'], 1.)
        self.assertEqual(scene['trajectory'], {'fps': 60, 'pause_seconds': 1.})
        explicit = {'move_defaults': {'constraints': {'velocity_scale': .2}},
                    'trajectory': {'pause_seconds': 0}}
        original = deepcopy(explicit)
        merged = apply_default_scene_settings(explicit)
        self.assertEqual(merged['move_defaults']['constraints']['velocity_scale'], .2)
        self.assertEqual(merged['move_defaults']['constraints']['acceleration_scale'], 1.)
        self.assertEqual(merged['trajectory'], {'fps': 60, 'pause_seconds': 0})
        self.assertEqual(explicit, original)

    def test_file_changes_are_seen_without_restarting_python(self):
        config = load_global_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'global.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            with patch('execution.config.GLOBAL_CONFIG_PATH', path):
                self.assertEqual(load_global_config()['motion']['velocity_scale'], .6)
                config['motion']['velocity_scale'] = .3
                path.write_text(json.dumps(config), encoding='utf-8')
                self.assertEqual(apply_default_scene_settings({})['move_defaults']['constraints']['velocity_scale'], .3)

    def test_invalid_values_are_not_silently_defaulted(self):
        for section, key, values in [
                ('motion', 'velocity_scale', [True, 0, -1, 1.1, float('nan')]),
                ('motion', 'acceleration_scale', [False, 0, float('inf')]),
                ('trajectory', 'fps', [True, 0, -1, 29.5]),
                ('trajectory', 'pause_seconds', [False, -1, float('inf')])]:
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_config({section: {key: value}}, partial=True)
        with self.assertRaises(ValueError):
            validate_config({'trajectory': {'fpps': 60}}, partial=True)

    def test_action_validation_and_timing_use_same_defaults(self):
        request = {'target': {'type': 'joint', 'joint_positions': [0.] * 6}}
        normalized = validate_move_request(request)
        self.assertEqual(normalized['constraints']['velocity_scale'], .6)
        self.assertEqual(normalized['constraints']['acceleration_scale'], 1.)
        base = {'planning': {'time_parameterization': {
            'max_velocity': [2.] * 6, 'max_acceleration': [4.] * 6}}}
        v, a = continuous_motion_limits(base, 6)
        np.testing.assert_allclose(v, [1.2] * 6)
        np.testing.assert_allclose(a, [4.] * 6)
        override = {'constraints': {'velocity_scale': .2, 'acceleration_scale': .5}}
        resources = SimpleNamespace(scene={'move_defaults': base}, joint_names=['j'] * 6)
        timing = apply_global_motion_timing(resources, override)['strategy_params']['time_parameterization']
        np.testing.assert_allclose(timing['max_velocity'], [.4] * 6)
        np.testing.assert_allclose(timing['max_acceleration'], [2.] * 6)

    def test_export_override_pause_rounding_and_zero_preserve_samples(self):
        scene = {'trajectory': {'fps': 30, 'pause_seconds': .25}, 'objects': [{
            'object_id_in_scene': 2, 'object_name_in_scene': 'Robot', 'components': [
                {'component_type': 'RobotDriver', 'properties': {'joint_sequence': ['j'] * 6}}]}]}
        frames = [{'frame_index': i, 'command_id': 'move', 'joints': [i] * 6} for i in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            for pause, count in [(None, 8), (0., 0), (.1, 3)]:
                path = write_traj(Path(directory) / 'out.traj', scene, frames,
                                  initial_joints=[0] * 6, pause_seconds=pause)
                summary = validate_traj(path)
                self.assertEqual(summary['fps'], 30)
                self.assertEqual(summary['total_frames'], 3 + count)
                raw = path.read_bytes()
                _, n, offset, _ = struct.unpack_from('<IIII', raw, 32 + 12 + 7 * 4)
                data = np.frombuffer(raw, dtype='<f4', offset=offset, count=n * 7).reshape(n, 7)
                np.testing.assert_allclose(data[:count + 1, 0], 0)
                np.testing.assert_allclose(data[count + 1:, 0], [1, 2])
        self.assertEqual(trajectory_settings(scene, fps=24), {'fps': 24, 'pause_seconds': .25})


if __name__ == '__main__':
    unittest.main()
