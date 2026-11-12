"""Use one live RGB-D estimate for a task's grasp target."""

from dataclasses import replace
from copy import deepcopy
from itertools import product
from time import perf_counter

import numpy as np

from atomic_action.perception.vlm_estimate import VLMPointEstimator
from demo.interaction.object_library import (
    GraspResolutionError, ObjectLibraryGraspResolver,
    control_from_grasp_frame, model_id_for, rigid_pose, scene_object_by_id,
)
from execution.resources import component
from demo.interaction.grasp_depth_search import (GRASP_DEPTHS_M, validate_depth_candidate,
                                                 floor_fallback_candidates)
from sensors.camera import CameraIntrinsics
from demo.interaction.placement_fallback import random_center_estimates
from demo.interaction.relative_placement import (
    placement_parameters, relative_region, crop_relative_region, relative_instruction,
    validate_relative_point, random_region_estimates)


PLACEMENT_HEIGHTS_M = tuple(cm / 100. for cm in range(9))
# Rz(+90 degrees) @ Rx(180 degrees), expressed in the world frame.
_DOWNWARD_GRASP_ROTATION = np.array([[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])
_DOWNWARD_PLACE_ROTATION = _DOWNWARD_GRASP_ROTATION.copy()


def _model_bounds(metadata):
    bounds = np.asarray(metadata.get('geometric_properties', {}).get('AABB'), dtype=float)
    if bounds.shape == (3,) and np.isfinite(bounds).all() and np.all(bounds > 0):
        half = bounds / 2
        return -half, half
    if bounds.shape != (6,) or not np.isfinite(bounds).all():
        raise GraspResolutionError('Model geometric_properties.AABB must contain six finite bounds')
    lower, upper = bounds[1::2], bounds[::2]
    if np.any(upper <= lower):
        raise GraspResolutionError('Model AABB bounds must have positive extent')
    return lower, upper


def _downward_grasp_rotation(estimate):
    """Use only the RGB-D surface reference direction, never model AABB axes."""
    details = {'long_axis_aligned': False, 'orientation_source': 'fixed_downward'}
    if estimate.get('axis_status') != 'valid':
        details['long_axis_fallback'] = estimate.get('axis_reason') or 'VLM 未提供有效长轴'
        return _DOWNWARD_GRASP_ROTATION.copy(), details
    axis = np.asarray(estimate.get('long_axis_world'), dtype=float)
    if axis.shape != (3,) or not np.isfinite(axis).all():
        details['long_axis_fallback'] = '长轴世界坐标方向无效'
        return _DOWNWARD_GRASP_ROTATION.copy(), details
    axis = axis.copy()
    axis[2] = 0.
    norm = float(np.linalg.norm(axis))
    if norm < 1e-6:
        details['long_axis_fallback'] = '长轴水平投影过短'
        return _DOWNWARD_GRASP_ROTATION.copy(), details
    x_axis = axis / norm
    if x_axis[0] < -1e-9 or (abs(x_axis[0]) <= 1e-9 and x_axis[1] < 0):
        x_axis = -x_axis
    # Existing grasp-frame calibration uses Y for jaw opening and Z for approach.
    z_axis = np.array([0., 0., -1.])
    y_axis = np.cross(z_axis, x_axis)
    details.update(long_axis_aligned=True, orientation_source='vlm_rgbd_axis',
                   long_axis_world=x_axis.tolist())
    return np.column_stack((x_axis, y_axis, z_axis)), details


def _target_crop(frame, object_pose, metadata):
    lower, upper = _model_bounds(metadata)
    world_from_object = rigid_pose(object_pose['position'], object_pose['quaternion_xyzw'])
    corners = np.asarray(list(product(*zip(lower, upper))), dtype=float)
    world = corners @ world_from_object[:3, :3].T + world_from_object[:3, 3]
    camera = (world - frame.camera_pose.position) @ frame.camera_pose.rotation_matrix
    depth = -camera[:, 2]
    if np.any(depth <= 0):
        raise GraspResolutionError('Target is behind the requested camera')
    intrinsics = frame.intrinsics
    pixels_x = intrinsics.fx * camera[:, 0] / depth + intrinsics.cx
    pixels_y = intrinsics.cy - intrinsics.fy * camera[:, 1] / depth
    center_x, center_y = (np.min(pixels_x) + np.max(pixels_x)) / 2, (np.min(pixels_y) + np.max(pixels_y)) / 2
    # Show the whole destination so VLM can choose free space away from its center.
    half_width = (np.max(pixels_x) - np.min(pixels_x)) * .5
    half_height = (np.max(pixels_y) - np.min(pixels_y)) * .5
    left = max(0, int(np.floor(center_x - half_width)) - 4)
    right = min(intrinsics.width, int(np.ceil(center_x + half_width)) + 5)
    top = max(0, int(np.floor(center_y - half_height)) - 4)
    bottom = min(intrinsics.height, int(np.ceil(center_y + half_height)) + 5)
    if right - left < 16 or bottom - top < 16:
        raise GraspResolutionError('Target projects outside the requested camera image')
    cropped_intrinsics = CameraIntrinsics(right - left, bottom - top, intrinsics.fx,
                                          intrinsics.fy, intrinsics.cx - left, intrinsics.cy - top)
    return replace(frame, rgb=frame.rgb[top:bottom, left:right],
                   depth=frame.depth[top:bottom, left:right],
                   intrinsics=cropped_intrinsics), (left, top)


class VLMGraspResolver(ObjectLibraryGraspResolver):
    def __init__(self, client, blackboard, backend, *, target_id=None, camera_id="global",
                 visual_targets=None, command_point_types=None, command_place_cycles=None,
                 command_place_parameters=None, estimator=None):
        super().__init__(client, blackboard)
        self.backend = backend
        targets = visual_targets or [{'target_id': target_id, 'point_type': 'grasp', 'camera_id': camera_id}]
        self.visual_targets = {(str(item['target_id']), item['point_type']): item for item in targets}
        self.command_point_types = command_point_types or {}
        self.command_place_cycles = command_place_cycles or {}
        self.command_place_parameters = command_place_parameters or {}
        self.place_parameters = placement_parameters({})
        self.place_cycle_id = None
        self.point_type = 'grasp'
        self.estimator = estimator or VLMPointEstimator()
        self.placement_rng = np.random.default_rng()
        self._placement_contexts = {}

    def set_command_context(self, command_id):
        self.point_type = self.command_point_types.get(str(command_id), 'grasp')
        self.place_cycle_id = (self.command_place_cycles.get(str(command_id), str(command_id))
                               if self.point_type == 'place' else None)
        self.place_parameters = placement_parameters(self.command_place_parameters.get(str(command_id), {}))

    def is_visual_place(self, target_id):
        return (str(target_id), self.point_type) in self.visual_targets and self.point_type == 'place'

    def is_visual_handle(self, target_id, resources):
        target = self.visual_targets.get((str(target_id), self.point_type))
        if target is None or self.point_type != 'grasp':
            return False
        if 'grasp_type' in target:
            return target['grasp_type'] == 'handle'
        obj = scene_object_by_id(resources.scene, target_id)
        return component(obj, 'Semantic').get('semantic_name', '').lower() == 'drawer'

    def select(self, resolved, kin, checker, seed, **kwargs):
        target = self.visual_targets.get((str(resolved['target_id']), self.point_type))
        if target is not None and self.point_type == 'place' and resolved.get('placement_height_search_pending'):
            return self._select_place_height(resolved, kin, checker, seed, **kwargs)
        if (target is not None and self.point_type == 'place'
                and resolved.get('visual_estimate', {}).get('placement_height_selected')):
            try:
                return super().select(resolved, kin, checker, seed, **kwargs)
            except GraspResolutionError as exc:
                # A physical payload can move within the fingers during transit.
                # Revalidate against the current state, then recover by searching
                # absolute height offsets from the same observed surface point.
                retry = deepcopy(resolved)
                evidence = retry['visual_estimate']
                selected_height = float(evidence['placement_height_offset_m'])
                heights = evidence.get('placement_height_candidates_m', PLACEMENT_HEIGHTS_M)
                candidates = []
                for candidate in resolved['candidates']:
                    surface = np.asarray(candidate['world_from_grasp'], dtype=float).copy()
                    surface[2, 3] -= float(candidate.get('placement_height_offset_m', selected_height))
                    for height in heights:
                        pose = surface.copy()
                        pose[2, 3] += height
                        candidates.append({**candidate, 'placement_height_offset_m': height,
                            'world_from_grasp': pose.tolist(),
                            'world_from_control': (pose @ np.linalg.inv(
                                np.asarray(resolved['control_from_grasp']))).tolist()})
                retry.update(candidates=candidates, placement_height_search_pending=True)
                evidence['placement_height_selected'] = False
                recovery = {'trigger': 'cached_place_pose_infeasible',
                            'previous_height_offset_m': selected_height,
                            'previous_error_code': getattr(exc, 'details', {}).get('error_code')}
                evidence['placement_revalidation'] = recovery
                try:
                    choice = self._select_place_height(retry, kin, checker, seed, **kwargs)
                except GraspResolutionError as recovery_error:
                    recovery_error.details.setdefault('details', {})['placement_revalidation'] = recovery
                    raise
                choice['placement_revalidation'] = recovery
                return choice
        if target is None or self.point_type != 'grasp':
            return super().select(resolved, kin, checker, seed, **kwargs)
        if resolved.get('visual_estimate', {}).get('grasp_type') == 'handle':
            if kwargs.get('grasp_index') not in (None, 0):
                raise GraspResolutionError('Visual handle grasp has only logical grasp_index 0')
            kwargs = {**kwargs, 'grasp_index': None}
        previous_target = getattr(self.backend, 'contact_target', None)
        previous_id = getattr(self.backend, 'contact_target_object_id', None)
        self.backend.contact_target = resolved['target_name']
        self.backend.contact_target_object_id = resolved['target_id']
        try:
            if resolved.get('fixed_grasp_validation_pending'):
                caller_validator = kwargs.get('candidate_validator')
                def validate_fixed(q, candidate):
                    evidence = validate_depth_candidate(
                        resolved, candidate, q, kin, checker, seed, kwargs.get('safety_distance', .003))
                    if evidence['success'] and caller_validator is not None:
                        return caller_validator(q, candidate)
                    return evidence
                return super().select(resolved, kin, checker, seed,
                    **{**kwargs, 'candidate_validator': validate_fixed})
            if not resolved.get('depth_search_pending'):
                return super().select(resolved, kin, checker, seed, **kwargs)
            attempts = []
            caller_validator = kwargs.get('candidate_validator')
            def validate(q, candidate):
                evidence = validate_depth_candidate(
                    resolved, candidate, q, kin, checker, seed, kwargs.get('safety_distance', .003))
                if evidence['success'] and caller_validator is not None:
                    return caller_validator(q, candidate)
                return evidence
            for depth in resolved.get('visual_estimate', {}).get('grasp_depth_candidates_m', GRASP_DEPTHS_M):
                group = [c for c in resolved['candidates'] if c['grasp_depth_offset_m'] == depth]
                if not group:
                    continue
                try:
                    choice = super().select(
                        {**resolved, 'candidates': group}, kin, checker, seed,
                        **{**kwargs, 'candidate_validator': validate})
                except GraspResolutionError as exc:
                    details = getattr(exc, 'details', {})
                    attempts.append({'grasp_depth_offset_m': depth, 'success': False,
                                     'error': details or {'error_message': str(exc)}})
                    continue
                attempts.append({'grasp_depth_offset_m': depth, 'success': True,
                                 'rejected': choice.get('rejected', [])})
                choice['grasp_depth_attempts'] = attempts
                return choice
            if resolved.get('visual_estimate', {}).get('grasp_type') == 'handle':
                error = GraspResolutionError('把手正面抓取候选均不可行，未执行接近')
                error.details = {'error_code': 'GRASP_DEPTH_SEARCH_FAILED',
                    'error_message': str(error), 'failed_stage': 'target_resolution',
                    'details': {'depth_attempts': attempts},
                    'rejected': [item for attempt in attempts
                                 for item in attempt['error'].get('rejected', [])]}
                raise error
            fallback_candidates = floor_fallback_candidates(resolved, checker, seed)
            fallback = {'used': True, 'success': False, 'candidates': fallback_candidates}
            try:
                choice = super().select({**resolved, 'candidates': fallback_candidates},
                    kin, checker, seed, **{**kwargs, 'grasp_index': None,
                                          'candidate_validator': validate})
            except GraspResolutionError as exc:
                fallback['error'] = getattr(exc, 'details', {}) or {'error_message': str(exc)}
            else:
                fallback.update(success=True, selected=choice['selected']['floor_fallback'],
                                rejected=choice.get('rejected', []))
                choice.update(grasp_depth_attempts=attempts, grasp_floor_fallback=fallback)
                return choice
            error = GraspResolutionError('0～30 mm 抓深候选及桌面底限兜底均不可行，未执行下探')
            error.details = {'error_code': 'GRASP_DEPTH_SEARCH_FAILED',
                             'error_message': str(error), 'failed_stage': 'target_resolution',
                             'details': {'depth_attempts': attempts, 'floor_fallback': fallback},
                             'rejected': [item for attempt in attempts
                                          for item in attempt['error'].get('rejected', [])]
                                         + fallback['error'].get('rejected', [])}
            raise error
        finally:
            self.backend.contact_target = previous_target
            self.backend.contact_target_object_id = previous_id

    def _search_place_heights(self, resolved, kin, checker, seed, **kwargs):
        """Find the lowest feasible offset using the monotone-height assumption."""
        attempts = []
        groups = {
            height: [c for c in resolved['candidates'] if c['placement_height_offset_m'] == height]
            for height in PLACEMENT_HEIGHTS_M
        }
        heights = [height for height in PLACEMENT_HEIGHTS_M if groups[height]]
        tested = {}

        def test(index):
            height = heights[index]
            if height in tested:
                return tested[height]
            attempt_started = perf_counter()
            try:
                choice = ObjectLibraryGraspResolver.select(
                    {**resolved, 'candidates': groups[height]}, kin, checker, seed, **kwargs)
            except GraspResolutionError as exc:
                result = {'choice': None, 'attempt': {
                    'placement_height_offset_m': height, 'success': False, 'evaluated': True,
                    'elapsed_s': perf_counter() - attempt_started,
                    'error': getattr(exc, 'details', {}) or {'error_message': str(exc)}}}
            else:
                result = {'choice': choice, 'attempt': {
                    'placement_height_offset_m': height, 'success': True, 'evaluated': True,
                    'elapsed_s': perf_counter() - attempt_started,
                    'rejected': choice.get('rejected', [])}}
            tested[height] = result
            return result

        if not heights:
            return None, attempts

        low = test(0)
        selected = low if low['choice'] is not None else None
        if selected is None and len(heights) > 1:
            high = test(len(heights) - 1)
            if high['choice'] is not None:
                lower, upper = 0, len(heights) - 1
                selected = high
                while upper - lower > 1:
                    middle = (lower + upper) // 2
                    result = test(middle)
                    if result['choice'] is None:
                        lower = middle
                    else:
                        upper = middle
                        selected = result

        # Keep diagnostics in physical height order and make inferred results
        # explicit; only measured candidates contribute elapsed time.
        for index, height in enumerate(heights):
            result = tested.get(height)
            if result is None:
                if selected is None:
                    success = False
                    reason = 'all higher tested heights were infeasible'
                elif index < heights.index(selected['attempt']['placement_height_offset_m']):
                    success = False
                    reason = 'binary search inferred infeasible below the selected height'
                else:
                    success = True
                    reason = 'binary search inferred feasible above the selected height'
                attempts.append({'placement_height_offset_m': height, 'success': success,
                                 'evaluated': False, 'inferred_by_monotonicity': True,
                                 'inference': reason})
            else:
                attempts.append(result['attempt'])
        if selected is None:
            return None, attempts
        selected['choice']['placement_height_attempts'] = attempts
        return selected['choice'], attempts

    def _center_estimates(self, target_id):
        context = self._placement_contexts.get(str(target_id))
        if context is None or context['cycle_id'] != self.place_cycle_id:
            return []
        if 'center_estimates' not in context:
            if context.get('relative_region') is not None:
                context['center_estimates'] = random_region_estimates(
                    context['frame'], context['relative_region'], self.placement_rng)
            else:
                lower, upper = _model_bounds(context['metadata'])
                context['center_estimates'] = random_center_estimates(
                    context['frame'], context['object_pose'], lower, upper, self.placement_rng)
        return context['center_estimates']

    @staticmethod
    def _restore_crop_pixel(estimate, crop_origin):
        estimate = deepcopy(estimate)
        if crop_origin is not None:
            estimate['crop_origin_pixel'] = {'x': crop_origin[0], 'y': crop_origin[1]}
            if 'pixel' in estimate:
                estimate['pixel'] = {'x': estimate['pixel']['x'] + crop_origin[0],
                                     'y': estimate['pixel']['y'] + crop_origin[1]}
            for reference in estimate.get('axis_points') or []:
                if 'pixel' in reference:
                    reference['pixel'] = {'x': reference['pixel']['x'] + crop_origin[0],
                                          'y': reference['pixel']['y'] + crop_origin[1]}
        return estimate

    def _select_place_height(self, resolved, kin, checker, seed, **kwargs):
        choice, attempts = self._search_place_heights(resolved, kin, checker, seed, **kwargs)
        original_fallback = resolved['visual_estimate'].get('placement_center_fallback', {})
        if choice is not None:
            if original_fallback.get('used'):
                original_fallback['attempts'] = [{
                    'world_point': resolved['visual_estimate']['world_point'],
                    'height_attempts': attempts, 'success': True}]
            return choice
        relative = resolved['visual_estimate'].get('relative_placement', {})
        if relative.get('relation') in {'above', 'below'}:
            error = GraspResolutionError('指定上下相对放置位姿不可行，未执行放置')
            error.details = {'error_code': 'PLACEMENT_HEIGHT_SEARCH_FAILED',
                             'failed_stage': 'target_resolution', 'error_message': str(error),
                             'details': {'height_attempts': attempts, 'relative_region': relative},
                             'rejected': [item for attempt in attempts
                                          for item in attempt.get('error', {}).get('rejected', [])]}
            raise error
        fallback = {'used': True, 'trigger': original_fallback.get('trigger', 'vlm_point_infeasible'),
                    'attempts': []}
        if original_fallback.get('vlm_error_type'):
            fallback['vlm_error_type'] = original_fallback['vlm_error_type']
        if original_fallback.get('used'):
            fallback['attempts'].append({'world_point': resolved['visual_estimate']['world_point'],
                                        'height_attempts': attempts, 'success': False})
        try:
            points = self._center_estimates(resolved['target_id'])
        except (ValueError, AttributeError, GraspResolutionError) as exc:
            points = []
            fallback['unavailable_reason'] = str(exc)
        if not points and not fallback['attempts']:
            fallback.setdefault('unavailable_reason', '容器中央附近没有可用的 RGB-D 随机点')
        while points:
            point_started = perf_counter()
            estimate = points.pop(0)
            context = self._placement_contexts[str(resolved['target_id'])]
            estimate = self._restore_crop_pixel(estimate, context['crop_origin'])
            estimate.update(held_object=resolved['visual_estimate']['held_object'],
                            placement_cycle_id=self.place_cycle_id,
                            placement_height_selected=False,
                            placement_height_candidates_m=list(PLACEMENT_HEIGHTS_M))
            if 'relative_placement' in resolved['visual_estimate']:
                estimate['relative_placement'] = deepcopy(resolved['visual_estimate']['relative_placement'])
            estimate['placement_request'] = deepcopy(resolved['visual_estimate'].get(
                'placement_request', placement_parameters({})))
            if 'placement_revalidation' in resolved['visual_estimate']:
                estimate['placement_revalidation'] = deepcopy(
                    resolved['visual_estimate']['placement_revalidation'])
            estimate['placement_center_fallback'].update(trigger=fallback['trigger'])
            if fallback.get('vlm_error_type'):
                estimate['placement_center_fallback']['vlm_error_type'] = fallback['vlm_error_type']
            base = deepcopy(resolved['candidates'][0])
            pose = np.asarray(base['world_from_grasp'], dtype=float).copy()
            pose[:3, 3] = [estimate['world_point'][axis] for axis in 'xyz']
            transform = np.asarray(resolved['control_from_grasp'], dtype=float)
            candidates = []
            for height in PLACEMENT_HEIGHTS_M:
                shifted = pose.copy()
                shifted[2, 3] += height
                candidates.append({**base, 'placement_height_offset_m': height,
                    'world_from_grasp': shifted.tolist(),
                    'world_from_control': (shifted @ np.linalg.inv(transform)).tolist()})
            alternative = {**resolved, 'candidates': candidates, 'visual_estimate': estimate}
            choice, alternative_attempts = self._search_place_heights(
                alternative, kin, checker, seed, **kwargs)
            fallback['attempts'].append({'world_point': estimate['world_point'],
                'height_attempts': alternative_attempts, 'success': choice is not None,
                'elapsed_s': perf_counter() - point_started})
            if choice is not None:
                estimate['placement_center_fallback'].update(
                    attempts=deepcopy(fallback['attempts']))
                if not original_fallback.get('used'):
                    estimate['placement_center_fallback']['primary_height_attempts'] = attempts
                return choice
        error = GraspResolutionError('未找到可用放置位姿，未执行放置')
        all_attempts = attempts + [a for point in fallback['attempts']
                                  for a in point['height_attempts']]
        if original_fallback.get('used'):
            all_attempts = [a for point in fallback['attempts'] for a in point['height_attempts']]
        error.details = {'error_code': 'PLACEMENT_HEIGHT_SEARCH_FAILED',
                         'failed_stage': 'target_resolution', 'error_message': str(error),
                         'details': {'height_attempts': attempts, 'center_fallback': fallback},
                         'rejected': [item for attempt in all_attempts
                                      for item in attempt.get('error', {}).get('rejected', [])]}
        raise error

    def remember_selection(self, resolved):
        selected = resolved['selected']
        evidence = resolved.get('visual_estimate')
        if (isinstance(evidence, dict)
                and selected.get('pose_source') == 'vlm_rgbd_grasp'):
            # All depth/yaw variants represent the same logical grasp (index 0).
            # Cache the selection so approach and pick use the same pose.
            evidence = dict(evidence, yaw_selected=True, depth_selected=True,
                            grasp_depth_offset_m=selected.get(
                                'grasp_depth_offset_m', evidence.get('grasp_depth_offset_m', 0.)),
                            grasp_depth_attempts=resolved.get(
                                'grasp_depth_attempts', evidence.get('grasp_depth_attempts', [])),
                            selected_yaw_offset_rad=selected.get(
                                'yaw_offset_rad', evidence.get('selected_yaw_offset_rad', 0.)))
            if resolved.get('closure_validation') is not None:
                evidence['grasp_depth_preflight'] = resolved['closure_validation']
            if resolved.get('grasp_floor_fallback') is not None:
                evidence['grasp_floor_fallback'] = resolved['grasp_floor_fallback']
            point = np.asarray(selected['world_from_grasp'])[:3, 3]
            evidence['target_point_world'] = dict(zip('xyz', map(float, point)))
            evidence['grasp_point_world'] = evidence['target_point_world'].copy()
            self.blackboard.remember_visual_grasp(
                resolved['target_name'], np.asarray(selected['world_from_grasp']), evidence,
                anchor_frame=evidence.get('anchor_frame'))
            resolved['visual_estimate'] = evidence
        elif isinstance(evidence, dict) and selected.get('pose_source') == 'vlm_rgbd_place':
            evidence = dict(evidence, placement_height_selected=True,
                            placement_height_offset_m=selected['placement_height_offset_m'],
                            placement_height_attempts=resolved.get(
                                'placement_height_attempts', evidence.get('placement_height_attempts', [])))
            point = np.asarray(selected['world_from_grasp'])[:3, 3]
            evidence['target_point_world'] = dict(zip('xyz', map(float, point)))
            self.blackboard.remember_visual_place(
                resolved['target_name'], np.asarray(selected['world_from_grasp']), evidence)
            resolved['visual_estimate'] = evidence
        super().remember_selection(resolved)

    def refine_after_approach(self, resolved, resources, state, kin, checker):
        """Validate wrist position refinement before atomically replacing the cache."""
        from demo.interaction.wrist_refinement import refine_surface, refinement_error, coarse_point_projection
        name = resolved['target_name']
        evidence = resolved.get('visual_estimate', {})
        if (self.point_type != 'grasp' or not evidence
                or (str(resolved['target_id']), 'grasp') not in self.visual_targets):
            return resolved
        vertical = evidence.get('grasp_estimation_method') == 'vertical_aabb'
        self.blackboard.observe(state)
        from time import perf_counter
        started = perf_counter()
        stage_started = started
        timings = {}
        stage = None

        def progress(next_stage, label):
            nonlocal stage, stage_started
            now = perf_counter()
            if stage is not None:
                timings[stage] = now - stage_started
            stage, stage_started = next_stage, now
            set_status = getattr(self.backend, 'set_status', None)
            if callable(set_status):
                set_status(label)
        try:
            if (state.get('gripper') or {}).get('holding'):
                raise refinement_error('夹爪仍持有物体，不能重新定位抓取位置')
            # Successful approach already passed the runtime settling gate.
            # Re-read the cached pose at the latest observed target transform.
            grasp = self.blackboard.grasp_pose(name, 0)
            if grasp is None:
                grasp = self.blackboard.visual_grasp(name)
            if grasp is None:
                raise refinement_error('缺少已选定的抓取姿态')
            grasp = np.asarray(grasp).copy()
            selected_depth = float(evidence.get('grasp_depth_offset_m', 0.))
            surface = grasp[:3, 3] - selected_depth * grasp[:3, 2]
            progress('capture', 'Wrist camera / capturing RGB-D')
            frame = self.backend.get_camera_frame('wrist')
            framing = coarse_point_projection(frame, surface)
            target = self.visual_targets[(str(resolved['target_id']), 'grasp')]
            crop_origin = None
            if not vertical:
                object_pose = self.blackboard.require_pose(name)
                metadata = self.blackboard.model(self.client, resolved['model_id'])
                frame, crop_origin = _target_crop(frame, object_pose, metadata)
            progress('vlm', 'Wrist camera / VLM localization')
            estimate, diagnostics = refine_surface(self.estimator, frame, surface,
                target.get('target_description', name.lower()), vertical=vertical)
            diagnostics['coarse_point_in_full_image'] = framing
            estimate = self._restore_crop_pixel(estimate, crop_origin)
            if crop_origin is not None:
                diagnostics.update(image_region='target_aabb_crop',
                                   crop_origin_pixel={'x': crop_origin[0], 'y': crop_origin[1]})
            progress('validation', 'Wrist camera / validating IK, collision and contact path')
            refined = deepcopy(resolved)
            new_surface = np.array([estimate['world_point'][key] for key in 'xyz'])
            transform = np.asarray(resolved['control_from_grasp'])
            depths = ([-float(evidence.get('grasp_shallow_offset_m', .015))] if vertical
                      else list(evidence.get('grasp_depth_candidates_m', GRASP_DEPTHS_M)))
            candidates = []
            for depth in depths:
                pose = grasp.copy()
                pose[:3, 3] = new_surface + depth * pose[:3, 2]
                candidate = deepcopy(resolved['selected'])
                candidate.pop('floor_fallback', None)
                candidate.update(world_from_grasp=pose.tolist(),
                    world_from_control=(pose @ np.linalg.inv(transform)).tolist(),
                    pose_source='vlm_rgbd_grasp', grasp_depth_offset_m=depth)
                candidates.append(candidate)
            updated = deepcopy(evidence)
            # New observation supplies position only; retain the selected axis evidence.
            updated.update({key: value for key, value in estimate.items()
                            if not key.startswith(('axis_', 'long_axis')) and key != 'is_elongated'})
            updated.update(grasp_rotation_world=grasp[:3, :3].tolist(),
                surface_normal_world=(-grasp[:3, 2]).tolist(),
                wrist_refinement={**diagnostics, 'status': 'validated'},
                depth_selected=False)
            # Do not retain evidence from the old position's preflight.
            for key in ('grasp_depth_preflight', 'grasp_floor_fallback', 'grasp_depth_attempts'):
                updated.pop(key, None)
                refined.pop(key, None)
            refined.pop('closure_validation', None)
            refined.update(candidates=candidates, visual_estimate=updated,
                fixed_grasp_validation_pending=vertical, depth_search_pending=not vertical)
            choice = self.select(refined, kin, checker, state['joints'], prefer_direct=False,
                safety_distance=float(resources.scene.get('move_defaults', {}).get('constraints', {}).get('safety_distance', .003)))
            progress('cache', 'Wrist camera / refinement validated')
            choice['visual_estimate']['wrist_refinement']['timing_s'] = dict(timings)
            choice['visual_estimate']['wrist_refinement']['elapsed_s'] = perf_counter() - started
            self.blackboard.clear_grasp_anchor(name)
            self.remember_selection(choice)
            return choice
        except Exception as exc:
            if stage is not None:
                timings[stage] = perf_counter() - stage_started
            error = refinement_error('腕部抓取精定位失败：' + str(exc),
                cause=getattr(exc, 'details', {'type': type(exc).__name__}),
                target_id=resolved['target_id'], substage=stage, timing_s=timings,
                elapsed_s=perf_counter() - started,
                **({'refined_estimate': estimate, 'refinement_diagnostics': diagnostics}
                   if stage in {'validation', 'cache'} else {}))
            if stage in {'validation', 'cache'}:
                raise error from exc
            fallback = deepcopy(resolved)
            fallback['visual_estimate']['wrist_refinement'] = {
                'status': 'fallback_to_coarse', 'error': error.details,
                'timing_s': dict(timings), 'elapsed_s': perf_counter() - started}
            # Failed refinement must not replace the previously validated pose
            # or its contact anchor. Pick resolves that existing cache normally.
            set_status = getattr(self.backend, 'set_status', None)
            if callable(set_status):
                set_status('Wrist refinement unavailable / using validated coarse grasp')
            return fallback

    def resolve(self, target_id, resources, state):
        target = self.visual_targets.get((str(target_id), self.point_type))
        if target is None:
            return super().resolve(target_id, resources, state)
        obj = scene_object_by_id(resources.scene, target_id)
        name = obj['object_name_in_scene']
        self.blackboard.observe(state)
        model_id = model_id_for(obj)
        self.blackboard.register(obj['object_id_in_scene'], name, model_id)
        object_pose = self.blackboard.require_pose(name)
        point_type = target['point_type']
        handle = self.is_visual_handle(target_id, resources)
        anchor_frame = name + '__' + target['link'] if target.get('link') else name
        if target.get('link'):
            self.blackboard.require_pose(anchor_frame)
        grasp = (self.blackboard.grasp_pose(name, 0) if point_type == 'grasp' else None)
        if grasp is None:
            if point_type == 'grasp':
                grasp = self.blackboard.visual_grasp(name)
            else:
                previous = self.blackboard.visual_place_evidence(name)
                # A placement cache is valid only within this put's interaction
                # cycle. A new put must see the current contents of the container.
                if (self.place_cycle_id is not None and previous is not None
                        and previous.get('placement_cycle_id') == self.place_cycle_id
                        and previous.get('placement_request', placement_parameters({})) == self.place_parameters):
                    grasp = self.blackboard.visual_place(name)
        if grasp is None:
            vertical_place = point_type == 'place' and self.place_parameters['relation'] in {'above', 'below'}
            frame = None if vertical_place else self.backend.get_camera_frame(target.get('camera_id', 'global'))
            place_metadata = self.blackboard.model(self.client, model_id) if point_type == 'place' else None
            crop_origin = None
            region = None
            if point_type == 'place':
                if self.place_parameters['relation'] != 'at':
                    lower, upper = _model_bounds(place_metadata)
                    region = relative_region(self.place_parameters, object_pose, lower, upper, resources)
                    if not vertical_place:
                        frame, crop_origin = crop_relative_region(frame, region)
                else:
                    frame, crop_origin = _target_crop(frame, object_pose, place_metadata)
                if not vertical_place:
                    self._placement_contexts[str(target_id)] = {
                        'cycle_id': self.place_cycle_id, 'frame': frame,
                        'metadata': place_metadata, 'object_pose': deepcopy(object_pose),
                        'crop_origin': crop_origin, 'relative_region': region}
            elif not handle:
                metadata = self.blackboard.model(self.client, model_id)
                frame, crop_origin = _target_crop(frame, object_pose, metadata)
            visual_started = perf_counter()
            try:
                if vertical_place:
                    estimate = {'point_type': 'place',
                        'world_point': dict(zip('xyz', region['region_center_world'])),
                        'frame': 'world', 'confidence': None, 'position_source': 'target_boundary'}
                elif region is not None:
                    estimate = self.estimator.estimate_frame(frame, point_type=point_type,
                        target='指定外侧区域的支撑表面', instruction=relative_instruction(region))
                    validate_relative_point(estimate, region)
                else:
                    estimate = self.estimator.estimate_frame(frame,
                        point_type='vertical' if handle else point_type,
                        target=target.get('target_description', name.lower()))
            except Exception as exc:
                if point_type != 'place' or vertical_place:
                    raise
                try:
                    points = self._center_estimates(target_id)
                    unavailable_reason = '容器中央附近没有有效深度'
                except (ValueError, AttributeError, GraspResolutionError) as fallback_exc:
                    points = []
                    unavailable_reason = str(fallback_exc)
                if not points:
                    error = GraspResolutionError('VLM 放置感知失败，无法获得有效放置点')
                    error.details = {'error_code': 'PLACEMENT_PERCEPTION_FAILED',
                        'failed_stage': 'perception', 'error_message': str(error),
                        'details': {'vlm_error_type': type(exc).__name__, 'center_fallback': {
                            'used': True, 'trigger': 'vlm_error', 'unavailable_reason': unavailable_reason}}}
                    raise error from exc
                estimate = points.pop(0)
                estimate['placement_center_fallback'].update(trigger='vlm_error', vlm_error_type=type(exc).__name__)
            else:
                estimate['visual_estimation_s'] = perf_counter() - visual_started
            estimate = self._restore_crop_pixel(estimate, crop_origin)
            if point_type == 'place':
                estimate['placement_request'] = dict(self.place_parameters)
                if region is not None:
                    estimate['relative_placement'] = deepcopy(region)
            grasp = np.eye(4)
            if point_type == 'grasp':
                if handle:
                    from demo.interaction.vertical_grasp import estimate_vertical_grasp
                    metadata = self.blackboard.model(self.client, model_id)
                    lower, upper = _model_bounds(metadata)
                    estimate.update(estimate_vertical_grasp(
                        estimate['world_point'], object_pose, lower, upper))
                    grasp[:3, :3] = np.asarray(estimate['grasp_rotation_world'], dtype=float)
                    estimate['anchor_frame'] = anchor_frame
                else:
                    grasp[:3, :3], orientation = _downward_grasp_rotation(estimate)
                    estimate.update(orientation)
            else:
                # Placement remains vertical; only grasp yaw follows the RGB-D reference axis.
                grasp[:3, :3] = _DOWNWARD_PLACE_ROTATION
            surface_point = np.array([estimate['world_point'][axis] for axis in 'xyz'], dtype=float)
            if point_type == 'grasp':
                # Cache the surface anchor; depth is selected later by ordered feasibility checks.
                grasp[:3, 3] = surface_point
                shallow = estimate.get('grasp_shallow_offset_m', 0.)
                depths = [-shallow] if handle else [round(depth - shallow, 6) for depth in GRASP_DEPTHS_M]
                estimate.update(depth_selected=False, grasp_depth_candidates_m=depths,
                                depth_search_enabled=not handle)
            else:
                gripper = state.get('gripper') or {}
                held_name = gripper.get('object_id')
                if not gripper.get('holding') or not held_name:
                    raise GraspResolutionError('VLM placement requires a confirmed held object')
                # Preserve the exact VLM RGB-D point. Height selection only
                # adds a world-Z offset; it never reselects a floor pixel.
                grasp[:3, 3] = surface_point
                estimate.update(held_object=held_name, placement_height_selected=False,
                                placement_height_candidates_m=[0.] if vertical_place else list(PLACEMENT_HEIGHTS_M))
            estimate['target_point_world'] = {
                axis: float(grasp[index, 3]) for index, axis in enumerate('xyz')}
            if point_type == 'grasp':
                estimate['grasp_point_world'] = estimate['target_point_world'].copy()
                self.blackboard.remember_visual_grasp(name, grasp, estimate, anchor_frame=anchor_frame)
            else:
                estimate['placement_cycle_id'] = self.place_cycle_id
                self.blackboard.remember_visual_place(name, grasp, estimate)
        transform = control_from_grasp_frame(resources)
        world_from_object = rigid_pose(object_pose['position'], object_pose['quaternion_xyzw'])
        candidate = {
            'grasp_index': 0,
            'world_from_object': world_from_object.tolist(),
            'world_from_grasp': grasp.tolist(),
            'world_from_control': (grasp @ np.linalg.inv(transform)).tolist(),
            'pose_source': 'vlm_rgbd_' + point_type,
        }
        if point_type == 'grasp' and self.blackboard.grasp_pose(name, 0) is not None:
            candidate['pose_source'] = 'blackboard_contact_link'
        evidence = (self.blackboard.visual_grasp_evidence(name) if point_type == 'grasp'
                    else self.blackboard.visual_place_evidence(name))
        if evidence is None:
            raise GraspResolutionError('Visual grasp estimate was not recorded')
        candidates = [candidate]
        pending = (point_type == 'grasp' and not evidence.get('depth_selected')
                   and candidate['pose_source'] != 'blackboard_contact_link')
        if pending:
            candidates = []
            for depth in evidence.get('grasp_depth_candidates_m', GRASP_DEPTHS_M):
                if depth > evidence.get('max_grasp_depth_m', float('inf')):
                    continue
                shifted = grasp.copy()
                shifted[:3, 3] += depth * shifted[:3, 2]
                for yaw in ((0., float(np.pi)) if evidence.get('long_axis_aligned') else (0.,)):
                    pose = shifted.copy()
                    if yaw:
                        pose[:3, :2] *= -1
                    candidates.append({**candidate, 'grasp_depth_offset_m': depth,
                        'yaw_offset_rad': yaw, 'world_from_grasp': pose.tolist(),
                        'world_from_control': (pose @ np.linalg.inv(transform)).tolist()})
        elif point_type == 'grasp':
            candidate['grasp_depth_offset_m'] = evidence.get('grasp_depth_offset_m', 0.)
            candidate['yaw_offset_rad'] = evidence.get('selected_yaw_offset_rad', 0.)
        placement_pending = point_type == 'place' and not evidence.get('placement_height_selected')
        if placement_pending:
            candidates = []
            for height in evidence.get('placement_height_candidates_m', PLACEMENT_HEIGHTS_M):
                pose = grasp.copy()
                pose[2, 3] += height  # World Z, independent of the tool's orientation.
                candidates.append({**candidate, 'placement_height_offset_m': height,
                    'world_from_grasp': pose.tolist(),
                    'world_from_control': (pose @ np.linalg.inv(transform)).tolist()})
        elif point_type == 'place':
            candidate['placement_height_offset_m'] = evidence['placement_height_offset_m']
        return {
            'target_id': obj['object_id_in_scene'],
            'target_name': name,
            'model_id': model_id,
            'model_name': name,
            'source_url': None,
            'control_from_grasp': transform.tolist(),
            'blackboard_revision': self.blackboard.revision,
            'candidates': candidates,
            'depth_search_pending': pending and not handle,
            'fixed_grasp_validation_pending': pending and handle,
            'placement_height_search_pending': placement_pending,
            'depth_search_move_defaults': resources.scene.get('move_defaults', {}),
            'visual_estimate': evidence,
        }
