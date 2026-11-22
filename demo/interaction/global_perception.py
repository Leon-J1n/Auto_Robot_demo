"""Batch observe visible operation targets before motion, then validate on use."""
from copy import deepcopy

import numpy as np

from demo.interaction.object_library import GraspResolutionError, model_id_for, scene_object_by_id
from demo.interaction.relative_placement import placement_parameters, validate_relative_point
from demo.interaction.visual_targets import infer_operation_ids


def _hint(frame, world):
    x, y, z = (world - frame.camera_pose.position) @ frame.camera_pose.rotation_matrix
    k = frame.intrinsics
    if z >= 0:
        return None
    uv = [(k.cx - k.fx*x/z)/(k.width-1), (k.cy + k.fy*y/z)/(k.height-1)]
    return uv if all(0 <= value <= 1 for value in uv) else None


def prepare(resolver, commands, resources):
    board, backend = resolver.blackboard, resolver.backend
    resolver.operation_ids = infer_operation_ids(commands)
    state = backend.get_state()
    board.observe(state)
    frame = backend.get_camera_frame('global')
    labels = backend.camera_body_labels(frame)
    operations, contexts, deferred = [], {}, {}
    for command in commands:
        skill = command['skill_name']
        if skill not in ('pick', 'put'):
            continue
        params = command['parameters']
        key = str(command['command_id'])
        kind = 'grasp' if skill == 'pick' else 'place'
        obj = scene_object_by_id(resources.scene, params['target'])
        name = obj['object_name_in_scene']
        resolver.set_command_context(key)
        target = resolver._visual_target(params['target'], kind)
        if target is None:
            continue
        if target.get('camera_id', 'global') != 'global':
            continue
        anchor = name + '__' + target['link'] if target.get('link') else name
        relation = placement_parameters(params) if kind == 'place' else {'relation': 'at'}
        if relation['relation'] in ('above', 'below'):
            continue  # Already determined geometrically at execution.
        pose = board.require_pose(name)
        region = None
        body = backend.object_ids[anchor]
        center = board.require_pose(anchor)['position']
        if relation['relation'] != 'at':
            metadata = board.model(resolver.client, model_id_for(obj))
            try:
                region = resolver.support_region(name, relation, pose, metadata, resources)
            except GraspResolutionError as exc:
                deferred[key] = str(exc)
                continue
            body = region['support_body_id']
            center = region['region_center_world']
        hint = _hint(frame, np.asarray(center))
        if hint is None:
            continue
        operations.append(dict(command_id=key, point_type=kind, target=target.get('target_description', anchor),
                               hint_xy=hint, **relation))
        contexts[key] = (name, anchor, body, region)
    estimates = resolver.estimator.estimate_operations(frame, operations) if operations else {}
    for key, estimate in estimates.items():
        name, anchor, body, region = contexts[key]
        u, v = (estimate['pixel'][axis] for axis in 'xy')
        if labels[v, u] != body or not np.isfinite(frame.depth[v, u]):
            deferred[key] = 'pixel does not show the intended surface'
            continue
        if region:
            try:
                validate_relative_point(estimate, region)
            except GraspResolutionError as exc:
                deferred[key] = str(exc)
                continue
        board.operation_candidates[key] = dict(estimate=estimate, target=name, anchor=anchor,
                                               support_body_id=int(body), status='available')
    resolver.observed_objects = deepcopy(board.objects)
    for key in contexts.keys() - estimates.keys():
        deferred[key] = 'no valid visual point'
    return dict(requested=len(operations), accepted=len(board.operation_candidates), deferred=deferred)


def current_candidate(resolver, state):
    entry = resolver.blackboard.operation_candidates.get(resolver.operation_id)
    if not entry or entry['status'] != 'available':
        return None
    backend = resolver.backend
    point = np.array([entry['estimate']['world_point'][a] for a in 'xyz'])
    held = (state.get('gripper') or {}).get('object_id')
    for name, old in resolver.observed_objects.items():
        new = state.get('objects', {}).get(name)
        if new is None:
            entry['status'] = 'scene_changed'
            return None
        delta = np.linalg.norm(np.subtract(new['position'], old['position']))
        alignment = abs(np.dot(new['quaternion_xyzw'], old['quaternion_xyzw']))
        if delta <= .003 and alignment >= np.cos(np.deg2rad(1)):
            continue
        body = backend.object_ids[name]
        dependency = name in (entry['anchor'], entry['target']) or body == entry['support_body_id']
        if not dependency:
            if name == held:
                continue
            geoms = np.flatnonzero(backend.model.geom_bodyid == body)
            near = any(np.linalg.norm(backend.data.geom_xpos[g] - point)
                       <= backend.model.geom_rbound[g] + .05 for g in geoms)
            if not near:
                continue
        entry['status'] = 'scene_changed'
        return None
    entry['status'] = 'used'
    return deepcopy(entry['estimate'])
