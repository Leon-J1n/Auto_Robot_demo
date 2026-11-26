"""Linear cuRobo routing and geometry guards without starting a GPU worker."""
import unittest
from unittest.mock import Mock, patch
import time
from copy import deepcopy
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np

from demo.run_demo import _use_curobo
from atomic_skill.approach import PoseSearchExecutor
from atomic_skill.pick import DemoInteractionPick
from atomic_skill.runner import TaskSkillExecutor
from demo.interaction.object_library import GraspResolutionError, ObjectLibraryGraspResolver
from libraries.curobo.linear import (LinearCandidateRejected, line_targets,
                                     search_ik_branches, search_linear_targets,
                                     timed_joint_line, validate_line)
from libraries.curobo.conversion.runtime import _fixed


class DeferredGraspTests(unittest.TestCase):
    def test_gpu_approach_caches_grasp_before_motion(self):
        executor = TaskSkillExecutor.__new__(TaskSkillExecutor)
        executor.resources = SimpleNamespace(scene={})
        executor.backend = Mock()
        executor.kinematics = executor.checker = executor.sync = None
        executor.execute_motion = SimpleNamespace(uses_curobo=lambda _: True)
        pose = np.eye(4).tolist()
        selected = {'selected': {'grasp_index': 0, 'world_from_control': pose},
                    'control_from_grasp': pose, 'contact_validation_deferred': True}
        executor.resolver = SimpleNamespace(resolve=Mock(), select_approach=Mock(return_value=selected),
                                            remember_selection=Mock())

        def move(*args):
            executor.resolver.remember_selection.assert_called_once_with(selected)
            return {'success': True}

        with patch('atomic_skill.runner.PoseSearchExecutor') as pose_search:
            pose_search.return_value.run.side_effect = move
            result = executor._execute_pose_search_inner(
                {'skill_name': 'approach', 'parameters': {'target': 'apple'}}, {'joints': []}, {})

        self.assertTrue(result['success'])
        executor.resolver.remember_selection.assert_called_once_with(selected)

    def test_approach_keeps_target_contact_and_stops_at_first_clear_grasp(self):
        pose = np.eye(4).tolist()
        candidates = [{'grasp_index': index, 'world_from_control': pose} for index in range(3)]
        resolved = {'target_name': 'Apple', 'target_id': 'apple', 'candidates': candidates}
        checks = []

        def precheck(transform, *, contact_target, **kwargs):
            checks.append(contact_target)
            return SimpleNamespace(in_collision=len(checks) == 1, contacts=[])

        checker = SimpleNamespace(check_gripper_pose=precheck)
        selected = ObjectLibraryGraspResolver.select_approach(resolved, checker)
        self.assertEqual(checks, [('Apple', 'apple')] * 2)
        self.assertEqual(selected['selected']['grasp_index'], 1)
        self.assertTrue(selected['contact_validation_deferred'])
        self.assertNotIn('joint_positions', selected)
        checks.clear()
        with self.assertRaises(GraspResolutionError):
            ObjectLibraryGraspResolver.select_approach(resolved, checker, grasp_index=0)

    def test_deferred_pick_submits_only_hard_linear_contact(self):
        pose = np.eye(4)
        adapter = DemoInteractionPick(None, None, None, None, None)
        resolution = {'contact_validation_deferred': True}
        base = {'target': {'type': 'pose'}, 'motion': {}, 'constraints': {}}
        received = []

        def resolve(request, state, **kwargs):
            received.append(kwargs['deferred_grasp_index'])
            return base, resolution, pose, pose, 'joint'

        adapter._resolve_request = resolve
        parts, _ = adapter.prepare({}, {}, deferred_grasp_index=0)
        self.assertEqual(received, [0])
        self.assertEqual(len(parts), 1)
        self.assertNotIn('direct_pick_fallback', parts[0])
        self.assertEqual(parts[0]['request']['motion'],
                         {'path_type': 'linear', 'path_constraint': 'hard', 'phase': 'contact'})


class RuntimeOriginCacheTests(unittest.TestCase):
    def test_tiny_mount_jitter_reuses_geometry_but_real_motion_does_not(self):
        def mount(angle):
            transform = np.eye(4)
            transform[0, 3] = -.0000001
            transform[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0.],
                                           [np.sin(angle), np.cos(angle), 0.],
                                           [0., 0., 1.]])
            robot = ET.Element('robot')
            _fixed(robot, 'mount', 'base', 'tool', transform)
            return ET.tostring(robot)

        self.assertEqual(mount(.002288), mount(.002295))
        self.assertNotEqual(mount(.002288), mount(.003))


class LinearTargetsTests(unittest.TestCase):
    def setUp(self):
        self.converted = {'current_tool_pose': [0., 0., 0., 1., 0., 0., 0.],
                          'base_world': np.eye(4).tolist()}
        self.request = {'target': {'type': 'pose', 'frame': 'base',
                                   'position': {'x': 0.1, 'y': 0., 'z': 0.}},
                        'strategy_params': {'cartesian_step': .02},
                        'constraints': {'position_tolerance': .005,
                                        'orientation_tolerance': 2.,
                                        'keep_end_effector_orientation': True}}

    def test_line_has_even_spacing_and_preserves_orientation(self):
        targets = line_targets(self.converted, self.request, 8)
        np.testing.assert_allclose(targets[0][:, 0], np.linspace(0, .1, 6))
        np.testing.assert_allclose(targets[1], np.tile([1., 0., 0., 0.], (6, 1)))
        report = validate_line(*targets, targets, position_tolerance=.005,
                               orientation_tolerance=np.deg2rad(2.))
        self.assertEqual(report['sample_count'], 6)

    def test_short_and_capacity_length_moves(self):
        request = deepcopy(self.request)
        request['target']['position']['x'] = .001
        self.assertEqual(len(line_targets(self.converted, request, 8)[0]), 2)
        request['target']['position']['x'] = .14
        self.assertEqual(len(line_targets(self.converted, request, 8)[0]), 8)

    def test_invalid_cartesian_step_is_rejected(self):
        request = deepcopy(self.request)
        for step in (0., -0.01, float('nan')):
            request['strategy_params']['cartesian_step'] = step
            with self.assertRaisesRegex(ValueError, 'cartesian_step'):
                line_targets(self.converted, request, 8)

    def test_move_exceeding_knot_spacing_must_be_split(self):
        with self.assertRaisesRegex(ValueError, 'split the move'):
            line_targets(self.converted, self.request, 4)

    def test_world_frame_and_orientation_conflict(self):
        request = deepcopy(self.request)
        request['target']['frame'] = 'world'
        translated = deepcopy(self.converted)
        translated['base_world'][0][3] = 1.
        request['target']['position']['x'] = 1.1
        np.testing.assert_allclose(line_targets(translated, request, 8)[0][-1], [.1, 0., 0.])
        request['target']['orientation'] = {'representation': 'quaternion',
            'x': 0., 'y': 0., 'z': 1., 'w': 0.}
        with self.assertRaisesRegex(ValueError, 'different tool orientation'):
            line_targets(translated, request, 8)

    def test_deviation_reversal_and_endpoint_are_rejected(self):
        targets = line_targets(self.converted, self.request, 8)
        deviated = targets[0].copy()
        deviated[3, 1] = .006
        with self.assertRaisesRegex(ValueError, 'deviates'):
            validate_line(deviated, targets[1], targets, position_tolerance=.005,
                          orientation_tolerance=.04)
        reversed_path = targets[0].copy()
        reversed_path[4, 0] = .01
        with self.assertRaisesRegex(ValueError, 'reverses'):
            validate_line(reversed_path, targets[1], targets, position_tolerance=.005,
                          orientation_tolerance=.04)


class DemoRoutingTests(unittest.TestCase):
    def test_gpu_auto_and_explicit_override(self):
        self.assertFalse(_use_curobo({}, gpu=False))
        self.assertTrue(_use_curobo({}, gpu=True))
        self.assertTrue(_use_curobo({'planning': {'planner_type': 'curobo'}}))
        for planner in ('direct', 'rrt'):
            self.assertFalse(_use_curobo({'planning': {'planner_type': planner}}, gpu=True))


class TimedJointLineTests(unittest.TestCase):
    def test_samples_preserve_knots_and_motion_limits(self):
        waypoints = np.array([[0., 0.], [.2, -.1], [.4, 0.]], dtype=np.float32)
        limits = {name: np.array([[-bound, -bound], [bound, bound]], dtype=np.float32)
                  for name, bound in [('velocity', 1.), ('acceleration', 2.), ('jerk', 10.)]}
        positions, times, velocity, acceleration, jerk = timed_joint_line(
            waypoints, limits, velocity_scale=.5, acceleration_scale=.25, resolution_rad=.02)
        np.testing.assert_allclose(positions[[0, -1]], waypoints[[0, -1]], atol=1e-6)
        self.assertTrue(np.any(np.all(np.isclose(positions, waypoints[1]), axis=1)))
        self.assertTrue(np.all(np.diff(times) > 0))
        self.assertLessEqual(np.abs(velocity).max(), .5 + 1e-5)
        self.assertLessEqual(np.abs(acceleration).max(), .5 + 1e-5)
        self.assertLessEqual(np.abs(jerk).max(), 10. + 1e-5)
        self.assertLessEqual(np.max(np.abs(np.diff(positions, axis=0))), .02 + 1e-5)

    def test_invalid_limits_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'Invalid jerk bounds'):
            timed_joint_line(np.array([[0.], [1.]], dtype=np.float32),
                {'velocity': [[-1.], [1.]], 'acceleration': [[-1.], [1.]],
                 'jerk': [[0.], [0.]]}, velocity_scale=1., acceleration_scale=1.,
                resolution_rad=.02)


class LinearBranchSearchTests(unittest.TestCase):
    def test_only_first_feasible_target_gets_waypoints_if_its_path_passes(self):
        events = []
        diagnostics = {}

        def solve(indices):
            events.append(('endpoints', indices))
            return [np.empty((0, 1)), np.array([[1.]]), np.array([[2.]])]

        def plan_target(index, stats):
            events.append(('waypoints', index))
            stats['ik_calls'] = 2
            return 'verified path'

        result = search_linear_targets([0, 2, 3], solve, plan_target,
            deadline=time.monotonic()+5, diagnostics=diagnostics)
        self.assertEqual(result, (2, 'verified path'))
        self.assertEqual(events, [('endpoints', [0, 2, 3]), ('waypoints', 2)])
        self.assertEqual(diagnostics, dict(endpoint_ik_batches=1, endpoints_tested=3,
                                          waypoint_searches=[dict(index=2, ik_calls=2)]))

    def test_next_target_gets_waypoints_only_after_first_path_is_rejected(self):
        events = []

        def solve(indices):
            events.append(('endpoints', indices))
            return [np.array([[1.]]) for _ in indices]

        def plan_target(index, stats):
            def validate(path):
                events.append(('validate', index))
                if index == 0:
                    raise LinearCandidateRejected('collision on interpolated path')
                return path

            def solve_waypoint(waypoint, seed):
                events.append(('waypoint', index, waypoint))
                return (seed + .1)[None]

            return search_ik_branches([0.], 2, solve_waypoint, validate, diagnostics=stats)

        index, path = search_linear_targets([0, 1, 2], solve, plan_target,
            deadline=time.monotonic()+5, diagnostics={})
        self.assertEqual(index, 1)
        np.testing.assert_allclose(path[:, 0], [0., .1, .2])
        self.assertEqual(events, [('endpoints', [0, 1, 2]),
            ('waypoint', 0, 0), ('waypoint', 0, 1), ('validate', 0),
            ('waypoint', 1, 0), ('waypoint', 1, 1), ('validate', 1)])

    def test_retreat_dispatches_distances_once_and_records_selected_distance(self):
        requests = []

        def move(request):
            requests.append(request)
            return {'success': True, 'trajectory': {'diagnostics': {
                'linear_target_batch': {'selected_index': 2}}}}

        move.uses_curobo = lambda params: True
        executor = PoseSearchExecutor(resources=SimpleNamespace(scene={}),
            backend=SimpleNamespace(get_end_effector_pose=lambda: {
                'position': np.zeros(3), 'rotation_matrix': np.eye(3)}),
            synchronizer=SimpleNamespace(sync=lambda *_: None),
            kinematics=None, checker=None, execute_motion=move)
        result = executor.run('retreat', {'min_distance': .08})
        self.assertTrue(result['success'])
        self.assertAlmostEqual(result['successful_distance_m'], .08)
        self.assertEqual(len(requests), 1)
        np.testing.assert_allclose(requests[0]['strategy_params']['linear_target_offsets_world'],
                                   [[0., 0., -.01], [0., 0., -.02]])

    def test_approach_dispatches_distances_once_and_records_selected_distance(self):
        requests = []

        def move(request):
            requests.append(request)
            return {'success': True, 'trajectory': {'diagnostics': {
                'pose_target_batch': {'selected_index': 2}}}}

        move.uses_curobo = lambda params: True
        executor = PoseSearchExecutor(resources=SimpleNamespace(scene={}),
            backend=SimpleNamespace(physics_steps=0),
            synchronizer=SimpleNamespace(sync=lambda *_: None),
            kinematics=None, checker=None, execute_motion=move)
        result = executor.run('approach', {'min_distance': .08,
            'target_pose': {'position': [0., 0., 1.]}})
        self.assertTrue(result['success'])
        self.assertAlmostEqual(result['successful_distance_m'], .08)
        self.assertEqual(len(requests), 1)
        targets = [requests[0]['target'], *requests[0]['strategy_params']['pose_target_candidates_world']]
        np.testing.assert_allclose([target['position']['z'] for target in targets], [.90, .91, .92])

    def test_backtracking_batches_same_waypoint_and_still_validates_each_path(self):
        batches = []
        validated = []

        def solve(index, previous):
            if index == 0:
                return [[.1], [.2], [.3], [.4]]
            self.assertAlmostEqual(previous[0], .1)
            return np.empty((0, 1))

        def solve_batch(index, parents):
            batches.append((index, parents[:, 0].tolist()))
            return [parent[None] for parent in parents]

        def validate(path):
            validated.append(path[1, 0])
            if path[1, 0] < .3:
                raise LinearCandidateRejected('trajectory collides')
            return path

        diagnostics = {}
        result = search_ik_branches([0.], 2, solve, validate, solve_batch=solve_batch,
                                    max_ik_calls=4, diagnostics=diagnostics)
        self.assertEqual(batches, [(1, [.2, .3])])
        self.assertEqual(validated, [.2, .3])
        np.testing.assert_allclose(result[:, 0], [0., .3, .3])
        self.assertEqual(diagnostics['candidate_paths_tested'], 2)
        self.assertEqual(diagnostics['ik_calls'], 4)
        self.assertEqual(diagnostics['ik_batches'], 3)
        self.assertEqual(diagnostics['farthest_blocked_waypoint']['waypoint'], 2)

    def test_bounded_search_reports_budget_instead_of_retrying_every_branch(self):
        diagnostics = {}

        def solve(index, previous):
            return [[.1], [.2], [.3]] if index == 0 else np.empty((0, 1))

        with self.assertRaisesRegex(RuntimeError, r'IK call budget exhausted \(3\)'):
            search_ik_branches([0.], 2, solve, self.fail, max_ik_calls=3, diagnostics=diagnostics)
        self.assertEqual(diagnostics['ik_calls'], 3)
        self.assertTrue(diagnostics['ik_budget_exhausted'])

    def test_retries_alternate_ik_candidate_after_path_collision(self):
        calls = []

        def solve(index, previous):
            calls.append((index, float(previous[0])))
            return [[.1], [-.1]] if index == 0 else [[previous[0] + .1]]

        def validate(path):
            if path[1, 0] > 0:
                raise LinearCandidateRejected('interpolated path collides')
            return path.copy()

        diagnostics = {}
        result = search_ik_branches([0.], 2, solve, validate, diagnostics=diagnostics)
        np.testing.assert_allclose(result[:, 0], [0., -.1, 0.])
        self.assertEqual(calls, [(0, 0.), (1, .1), (1, -.1)])
        self.assertEqual(diagnostics, {'candidate_paths_tested': 2, 'ik_calls': 3})

    def test_rejects_large_joint_jump_even_for_successful_ik(self):
        result = search_ik_branches([0.], 1, lambda index, previous: [[1.], [.2]],
                                    lambda path: path.copy(), max_joint_step=.4)
        np.testing.assert_allclose(result[:, 0], [0., .2])
        with self.assertRaisesRegex(RuntimeError, 'no continuous'):
            search_ik_branches([0.], 1, lambda index, previous: [[1.]],
                               lambda path: path.copy(), max_joint_step=.4)

    def test_backtracks_when_a_seeded_ik_has_no_successful_candidates(self):
        def solve(index, previous):
            if index == 0:
                return [[.1], [-.1]]
            return np.empty((0, 1)) if previous[0] > 0 else [[0.]]

        result = search_ik_branches([0.], 2, solve, lambda path: path.copy())
        np.testing.assert_allclose(result[:, 0], [0., -.1, 0.])

    def test_validation_failure_cannot_be_bypassed_by_search_limit(self):
        def reject(path):
            raise LinearCandidateRejected('trajectory collides')

        with self.assertRaisesRegex(RuntimeError, 'trajectory collides'):
            search_ik_branches([0.], 1, lambda index, previous: [[.1], [.2]],
                               reject, max_paths=1)

    def test_exhausted_deadline_fails_closed(self):
        with self.assertRaisesRegex(TimeoutError, 'exceeded planning time'):
            search_ik_branches([0.], 1, lambda index, previous: [[.1]],
                               lambda path: path.copy(), deadline=time.monotonic() - 1)


if __name__ == '__main__':
    unittest.main()
