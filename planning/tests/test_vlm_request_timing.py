"""Measure actual client calls, including errors, without postprocessing time."""
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from atomic_action.perception.vlm_estimate.estimator import VLMPointEstimator
from atomic_action.perception.vlm_estimate.timing import command_timing_scope, start_command, stamp_command
from demo.run_demo import _print_command_timing


class RequestTimingTests(unittest.TestCase):
    def test_client_success_failure_parse_failure_and_command_reset(self):
        clock = [0.]
        calls = [0]
        def create(**kwargs):
            calls[0] += 1
            clock[0] += 2.
            if calls[0] == 2:
                raise TimeoutError('request timed out')
            return SimpleNamespace(output_text='invalid' if calls[0] == 3 else '{"x":0.5,"y":0.5}')
        estimator = VLMPointEstimator.__new__(VLMPointEstimator)
        estimator._client = SimpleNamespace(responses=SimpleNamespace(create=create))
        estimator.model = 'test'
        intrinsics = SimpleNamespace(width=3, height=3, fx=1., fy=1., cx=1., cy=1.)
        pose = SimpleNamespace(position=np.zeros(3), rotation_matrix=np.eye(3))
        def depth(*args, **kwargs):
            clock[0] += 10.  # Local processing must not enter request timing.
            return 1., {}
        with command_timing_scope(), patch('atomic_action.perception.vlm_estimate.timing.perf_counter',
                side_effect=lambda: clock[0]), patch('atomic_action.perception.vlm_estimate.estimator.sample_depth', side_effect=depth):
            start_command('16')
            def estimate():
                return estimator.estimate(np.zeros((3, 3, 3), dtype=np.uint8), np.ones((3, 3)),
                    intrinsics, pose, point_type='place', target='Tray')
            result = estimate()
            self.assertEqual(result['vlm_request_s'], 2.)
            with self.assertRaises(TimeoutError):
                estimate()
            with self.assertRaises(ValueError):
                estimate()
            row = {'command_id': '16', 'timing': {'planning': .562}}
            stamp_command(row)
            self.assertEqual(row['vlm_request_s'], 6.)
            self.assertEqual(row['vlm_request_count'], 3)
            self.assertEqual(row['vlm_failed_request_count'], 1)
            output = StringIO()
            with redirect_stdout(output):
                _print_command_timing(row)
            self.assertEqual(output.getvalue().strip(), 'timing|command=16|planning=0.562s|vlm_request=6.000s')
            start_command('17')
            cached = {'command_id': '17', 'target_resolution': {'vlm_request_s': 99.}}
            output = StringIO()
            with redirect_stdout(output):
                _print_command_timing(cached)
            self.assertNotIn('vlm_request=', output.getvalue())
            self.assertEqual(cached['vlm_request_count'], 0)


if __name__ == '__main__':
    unittest.main()
