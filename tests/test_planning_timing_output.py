import json
import tempfile
import unittest
from pathlib import Path

from demo.run_demo import _write_command_planning_timing


class PlanningTimingOutputTest(unittest.TestCase):
    def test_writes_each_motion_phase_without_execution_time(self):
        row = {
            'command_id': 'pick/contact',
            'skill_name': 'pick',
            'success': True,
            'target_resolution_timing_s': {'resolve_total': .4, 'candidate_selection': .2},
            'target_resolution': {'placement_height_attempts': [
                {'placement_height_offset_m': 0.0, 'elapsed_s': .1}]},
            'substeps': [
                {
                    'phase': 'approach',
                    'selection': {'planning_mode': 'collision_free'},
                    'timing': {'path_generation': 0.02, 'planning': 0.4,
                               'trajectory_preparation': 0.1, 'execution': 3.0, 'total': 3.5},
                    'trajectory': {'planner': 'collision_free', 'diagnostics': {
                        'algorithm': 'rrt_connect+trajopt',
                        'planning_stages_s': {'rrt_search': 0.2, 'trajopt': 0.1},
                        'path_quality': {'final': {'joint_length_l2_rad': 1.2}},
                        'configuration_selection': {'candidate_timings': [
                            {'direct_elapsed_s': .15, 'direct_success': True}]},
                    }},
                },
                {
                    'phase': 'contact',
                    'timing': {'path_generation': 0.01, 'planning': 0.03},
                    'trajectory': {'planner': 'direct', 'diagnostics': {}},
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            relative = _write_command_planning_timing(Path(directory), 2, row)
            self.assertEqual(relative.as_posix(), 'commands/002_pick_contact/planning_timing.json')
            payload = json.loads((Path(directory) / relative).read_text(encoding='utf-8'))

        self.assertEqual([motion['phase'] for motion in payload['motions']], ['approach', 'contact'])
        self.assertEqual(payload['motions'][0]['planner_diagnostics']['planning_stages_s']['rrt_search'], 0.2)
        self.assertEqual(payload['motions'][0]['planner_diagnostics']['path_quality']
                         ['final']['joint_length_l2_rad'], 1.2)
        self.assertEqual(payload['target_resolution_timing_s']['resolve_total'], .4)
        self.assertEqual(payload['target_resolution_diagnostics']['placement_height_attempts'][0]['elapsed_s'], .1)
        self.assertEqual(payload['motions'][0]['planner_diagnostics']['configuration_selection']
                         ['candidate_timings'][0]['direct_elapsed_s'], .15)
        self.assertNotIn('execution', payload['motions'][0]['timing_s'])
        self.assertNotIn('total', payload['motions'][0]['timing_s'])
        self.assertEqual(row['planning_timing_file'], relative.as_posix())


if __name__ == '__main__':
    unittest.main()
