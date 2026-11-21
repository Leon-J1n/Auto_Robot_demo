"""A recovered XY must be approached before placement, using the selected pose."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

from atomic_skill.approach import PoseSearchExecutor
from atomic_skill.put import DemoInteractionPut, prepend_place_reapproach


class PutReapproachTests(unittest.TestCase):
    def resolved(self, dx=.2, dz=-.01):
        old = np.eye(4)
        old[:3, :3] = Rotation.from_euler('x', -90, degrees=True).as_matrix()
        goal = old.copy()
        goal[:3, 3] = [dx, 0, dz]
        calibration = np.eye(4)
        calibration[1, 3] = .2558
        return {'selected': {'world_from_control': goal.tolist()},
                'control_from_grasp': calibration.tolist(),
                'placement_revalidation': {'previous_world_from_control': old.tolist()}}

    def test_changed_xy_prepends_approach_and_preserves_contact(self):
        resolved = self.resolved()
        parts = [{'phase': 'contact', 'request': {'target': 'chosen_place'}}]
        original = deepcopy(parts)
        prepend_place_reapproach(parts, resolved)
        self.assertEqual([p['phase'] for p in parts], ['placement_reapproach', 'contact'])
        self.assertEqual(parts[1:], original)
        params = parts[0]['parameters']
        np.testing.assert_allclose(params['target_pose']['position'], [.2, 0, -.01])
        np.testing.assert_allclose(Rotation.from_quat(params['target_pose']['quaternion_xyzw']).as_matrix(),
                                  np.array(resolved['selected']['world_from_control'])[:3, :3], atol=1.e-12)
        self.assertAlmostEqual(resolved['placement_reapproach']['xy_shift_m'], .2)

    def test_height_only_and_no_revalidation_keep_direct_put(self):
        for resolved in (self.resolved(dx=0), {'selected': self.resolved()['selected']}):
            parts = [{'phase': 'contact'}]
            prepend_place_reapproach(parts, resolved)
            self.assertEqual(parts, [{'phase': 'contact'}])

    def test_prepare_uses_revalidated_choice_without_resolving_again(self):
        adapter = DemoInteractionPut.__new__(DemoInteractionPut)
        resolved = self.resolved()
        goal = np.array(resolved['selected']['world_from_control'])
        with patch.object(adapter, '_resolve_request', return_value=({}, resolved, goal, None, 'linear')) as resolve, \
                patch.object(adapter, '_prepare_approach', return_value=([{'phase': 'contact'}], resolved)):
            parts, _ = adapter.prepare({}, {})
        resolve.assert_called_once()
        self.assertEqual(parts[0]['phase'], 'placement_reapproach')

    def test_reapproach_uses_new_point_and_reports_execution_failure(self):
        resolved = self.resolved()
        parts = [{'phase': 'contact'}]
        prepend_place_reapproach(parts, resolved)
        calls = []
        def motion(request):
            calls.append(request)
            return {'success': False, 'error': {'failed_stage': 'execution',
                    'error_code': 'EXECUTION_FAILED'}, 'motion_executed': True}
        executor = PoseSearchExecutor(resources=SimpleNamespace(scene={
            'move_defaults': {'constraints': {'avoid_collision': True}}}),
            backend=SimpleNamespace(physics_steps=0), synchronizer=SimpleNamespace(sync=lambda *a: None),
            kinematics=None, checker=None, execute_motion=motion)
        result = executor.run('approach', parts[0]['parameters'])
        self.assertFalse(result['success'])
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0]['target']['position']['x'], .2)
        self.assertGreater(calls[0]['target']['position']['z'], -.01)
        self.assertTrue(calls[0]['constraints'].get('avoid_collision'))


if __name__ == '__main__':
    unittest.main()
