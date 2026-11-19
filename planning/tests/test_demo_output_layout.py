"""Single-run cleanup, persisted references, metrics and output ownership."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from demo.output_layout import prepare_output, output_lock, summary_view
from demo.run_demo import _write_command_report, _write_command_planning_timing


class DemoOutputTests(unittest.TestCase):
    def test_cleanup_is_scoped_and_protects_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'commands.json'; source.write_text('{}')
            output = root / 'output'; output.mkdir()
            (output / 'old').mkdir(); (output / 'old/history.json').write_text('{}')
            prepare_output(output, protected=[source])
            self.assertFalse((output / 'old').exists())
            self.assertTrue(source.exists())
            self.assertTrue((output / 'trajectories/planned').is_dir())
            with self.assertRaisesRegex(ValueError, 'remove input'):
                prepare_output(root, protected=[source])
            self.assertTrue(source.exists())

    def test_active_run_cannot_be_cleared_by_another_run(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with output_lock(output):
                prepare_output(output)
                with self.assertRaisesRegex(ValueError, 'another run'):
                    with output_lock(output):
                        self.fail('Second writer acquired the output')
            with output_lock(output):
                pass

    def test_detailed_command_and_compact_summary_metrics_share_references(self):
        with tempfile.TemporaryDirectory() as directory:
            output = prepare_output(Path(directory) / 'output')
            trajectory = {'planner': 'rrt', 'waypoints': [[0.] * 6, [1.] * 6],
                          'diagnostics': {'configuration_selection': {'candidate_count': 2,
                              'candidate_timings': [{'error_details': {'contacts': ['obstacle']}}]}}}
            row = {'command_id': '33', 'skill_name': 'approach', 'success': True,
                   'trajectory': deepcopy(trajectory), 'substeps': [
                       {'phase': 'approach', 'timing': {'planning': 1.2}, 'trajectory': deepcopy(trajectory)}],
                   'target_resolution': {'visual_estimate': {'large': ['details'] * 100}}}
            _write_command_planning_timing(output, 29, row)
            row['success'] = False
            _write_command_planning_timing(output, 29, row)
            target = _write_command_report(output, 29, row)
            saved = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(target.name, 'results.jsonl')
            self.assertEqual(saved['step_index'], 29)
            self.assertNotIn('waypoints', saved['trajectory'])
            arrays = json.loads((output / saved['trajectory']['data_file']).read_text())
            self.assertEqual(len(arrays), 1)  # Root and substep copies share one trajectory.
            self.assertEqual(arrays[saved['trajectory']['data_key']]['waypoints'], trajectory['waypoints'])
            self.assertIn('waypoints', row['trajectory'])  # In-memory consumers keep the original API.
            self.assertIn('candidate_timings', saved['trajectory']['diagnostics']['configuration_selection'])
            metrics = [json.loads(line) for line in (output / 'metrics/planning.jsonl').read_text().splitlines()]
            self.assertEqual(len(metrics), 1)
            self.assertFalse(metrics[0]['success'])
            self.assertNotIn('target_resolution_diagnostics', metrics[0])
            self.assertNotIn('candidate_timings', metrics[0]['motions'][0]['planner_diagnostics']['configuration_selection'])
            summary = summary_view({'steps': [row], 'final_state': {'joints': [0.] * 6}, 'success': False})
            self.assertNotIn('steps', summary)
            self.assertEqual(summary['commands_file'], 'commands/results.jsonl')
            self.assertEqual(summary['failure']['step_index'], 29)
            self.assertEqual(summary['failure']['command_id'], '33')
            self.assertEqual(summary['final_state_file'], 'snapshots/state.json')

    def test_commands_append_immediately_and_repeated_failure_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            output = prepare_output(Path(directory) / 'output')
            first = {'command_id': '1', 'skill_name': 'move_to', 'success': True}
            target = _write_command_report(output, 1, first)
            self.assertEqual(len(target.read_text().splitlines()), 1)
            failed = {'command_id': '33', 'skill_name': 'approach', 'success': False,
                      'substeps': [{'phase': 'approach', 'error': {'error_code': 'TRAJECTORY_INVALID'}}],
                      'error': {'error_code': 'TRAJECTORY_INVALID', 'failed_stage': 'planning'}}
            _write_command_report(output, 2, failed)
            failed['status'] = 'error'
            _write_command_report(output, 2, failed)
            rows = [json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual([row['command_id'] for row in rows], ['1', '33'])
            self.assertEqual(rows[1]['status'], 'error')
            self.assertEqual(rows[1]['substeps'], failed['substeps'])
            self.assertEqual(list((output / 'commands').iterdir()), [target])
            summary = summary_view({'success': False, 'steps': [first, failed]})
            self.assertEqual(summary['command_count'], 2)
            self.assertEqual(summary['successful_command_count'], 1)
            self.assertEqual(summary['failed_command_count'], 1)
            self.assertEqual(summary['failure']['step_index'], 2)
            self.assertEqual(summary['failure']['error_code'], 'TRAJECTORY_INVALID')

    def test_summary_reports_initialization_error_without_command_details(self):
        summary = summary_view({'success': False, 'steps': [],
            'error': {'stage': 'execution_initialization', 'message': 'Initial assembled robot collides'}})
        self.assertEqual(summary['command_count'], 0)
        self.assertEqual(summary['failure']['stage'], 'execution_initialization')


if __name__ == '__main__':
    unittest.main()
