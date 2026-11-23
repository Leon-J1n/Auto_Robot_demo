"""Freeze the articulated checker's effective pair rules for another planner.

Backend hand-off exclusions are deliberately not exported as permissions.
The articulated pair builder remains the authority for contact allowances.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import copy
import hashlib
import json

import numpy as np
from demo.interaction.carrying_safety import payload_pair_limit


def planning_signature(runtime, checker, *, include_state=True) -> str:
    """Hash exact validation state, or reusable model/policy inputs only.

    Omitting state never authorizes reuse of a collision result: FK, world poses,
    scene revision and full before/after guards still have to be refreshed.
    """
    digest = hashlib.sha256()
    digest.update(repr(getattr(getattr(checker, 'planning_proxy', None), 'options', None)).encode())
    digest.update(repr(getattr(getattr(checker, 'planning_proxy', None), 'lift_bounds', None)).encode())
    for owner, names in ((runtime.data, ('qpos', 'mocap_pos', 'mocap_quat') if include_state else ()),
            (runtime.model, ('qpos0', 'body_mocapid', 'body_pos', 'body_quat', 'body_parentid', 'jnt_pos', 'jnt_axis',
                'jnt_type', 'jnt_range', 'site_pos', 'site_quat', 'geom_pos', 'geom_quat',
                'geom_size', 'geom_type', 'geom_bodyid', 'geom_contype', 'geom_conaffinity',
                'mesh_vert', 'mesh_face', 'mesh_vertadr', 'mesh_vertnum',
                'mesh_faceadr', 'mesh_facenum', 'geom_dataid', 'body_weldid',
                'body_jntadr', 'body_jntnum', 'jnt_qposadr', 'site_bodyid', 'exclude_signature'))):
        for name in names:
            value = np.asarray(getattr(owner, name, ()))
            digest.update(name.encode())
            digest.update(str((value.shape, value.dtype)).encode())
            digest.update(value.tobytes())
    controller = runtime.gripper_controller
    for owner, names in ((runtime, ('contact_target', 'contact_target_object_id')),
            (controller, ('held_name', 'base_id', 'mode', 'relative_pose', '_holding')),
            (checker, ('scene_revision', 'allow_gripper_environment_contact', 'support_geoms',
                '_lift_clearance_pairs', '_lift_ignored_payload_geoms', 'allowed_target_names',
                '_physical_closure_target', '_ignore_collisions', 'excluded', 'adjacent',
                '_arm_environment_geom_pairs',
                'carrying_safety_distance',
                'geoms', 'tool_geoms', 'robot_bodies', 'dynamic_names', 'body_names',
                '_lift_ignored_payload_names', 'tcp_site_id')),
            (runtime, ('base_id', 'joint_ids', 'qpos_indices', 'joint_names', 'joint_limits', 'object_ids'))):
        for name in names:
            if not include_state and name == 'scene_revision':
                continue
            value = getattr(owner, name, None)
            digest.update(name.encode())
            digest.update(value.tobytes() if isinstance(value, np.ndarray) else repr(value).encode())
    scene = getattr(getattr(runtime, 'resources', None), 'scene', {})
    digest.update(repr([(obj.get('object_id_in_scene'), obj.get('object_name_in_scene'))
                        for obj in scene.get('objects', ())]).encode())
    return digest.hexdigest()

def freeze_policy(checker, joints, safety_distance: float, *, avoid_collision: bool = True,
                  gripper_width=None, allowed_geom_ids=None) -> tuple[dict, object]:
    """Return a JSON policy and an independent, current kinematic probe."""
    import mujoco

    requested_avoid_collision = bool(avoid_collision)
    while '_checker' in vars(checker):
        avoid_collision = avoid_collision and not bool(getattr(checker, '_ignore_collisions', False))
        checker = checker._checker
    if not hasattr(checker, 'tool_geoms') or not hasattr(checker, 'backend'):
        raise ValueError('Runtime pair-policy export requires the articulated collision checker')
    probe = copy(checker)
    probe.probe = mujoco.MjData(checker.backend.model)
    probe._query_cache = OrderedDict()
    # The live checker may already have a PlanningProxy.  A policy-only probe
    # must build the articulated pair list itself; delegating to the copied
    # proxy would skip that list and leave ``_pairs`` unset.
    probe.planning_proxy = None
    probe._pair_key = None
    probe._policy_only = True
    extra = {}
    if gripper_width is not None:
        extra['gripper_width'] = gripper_width
    if allowed_geom_ids is not None:
        extra['allowed_geom_ids'] = allowed_geom_ids
    probe.check(joints, safety_distance=safety_distance, **extra)
    model = checker.backend.model
    # Some lightweight backends do not expose an articulated pair array during
    # initialization.  The frozen policy still remains safe (all unlisted
    # pairs retain the requested distance limit); do not fail the whole control
    # request merely while collecting optional pair metadata.
    articulated = {frozenset((int(a), int(b))): bool(support)
                   for a, b, support in getattr(probe, '_pairs', ())}
    allowed, thresholds, base_thresholds = [], [], []
    allowed_names = set(getattr(checker, 'allowed_target_names', ())) | set(map(str, allowed_geom_ids or ()))
    # The runtime target is sometimes carried only as an object id (the web
    # bridge deliberately does not expose an internal body name).  Resolve it
    # against the scene before freezing the policy so the planning proxy gets
    # the same narrow gripper->target allowance as the articulated checker.
    runtime = checker.backend
    target_object_id = (getattr(runtime, 'contact_target_object_id', None)
                        or getattr(checker, '_physical_closure_target', None))
    if target_object_id is not None:
        for obj in getattr(getattr(runtime, 'resources', None), 'scene', {}).get('objects', ()):
            if str(obj.get('object_id_in_scene')) != str(target_object_id):
                continue
            for key in ('object_name_in_scene',):
                if obj.get(key):
                    allowed_names.add(str(obj[key]))
            for component in obj.get('components', ()):
                if component.get('component_type') == 'Semantic':
                    semantic = component.get('properties', {}) or {}
                    if semantic.get('semantic_name'):
                        allowed_names.add(str(semantic['semantic_name']))
            break
    if getattr(runtime, 'contact_target', None):
        allowed_names.add(str(runtime.contact_target))
    held = getattr(checker.backend.gripper_controller, 'held_name', None)
    held_body = getattr(checker.backend, 'object_ids', {}).get(held)
    attached_bodies = set()
    if held_body is not None:
        joint = int(model.body_jntadr[held_body])
        if joint >= 0 and model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_FREE:
            for body in range(1, model.nbody):
                ancestor = body
                while ancestor:
                    if ancestor == held_body:
                        attached_bodies.add(body)
                        break
                    ancestor = int(model.body_parentid[ancestor])
    planned_bodies = checker.robot_bodies | attached_bodies
    moving = checker.tool_geoms | {
        g for g in checker.geoms if checker.body_names[int(model.geom_bodyid[g])] in checker.dynamic_names
        or int(model.geom_bodyid[g]) in attached_bodies
    }
    lift_ignored = getattr(checker, '_lift_ignored_payload_geoms', ())
    def named_allowance(body):
        while body:
            if checker.body_names[body] in allowed_names:
                return True
            body = int(model.body_parentid[body])
        return False
    def internal_gripper_body_pair(first, second):
        """Return true only for contacts inside the Robotiq mechanism.

        The reviewed Robotiq templates contain several links whose MuJoCo
        parent is not their immediate mechanical neighbour (notably the
        coupler/spring links).  Relying only on parent-child ancestry therefore
        misses valid structural contacts and makes the planning proxy reject
        every endpoint.  Both bodies must belong to the named gripper; arm or
        environment contacts never qualify.
        """
        names = (checker.body_names[int(first)], checker.body_names[int(second)])
        return (int(first) in checker.robot_bodies and int(second) in checker.robot_bodies
                and all(name.startswith('robotiq_2f85') for name in names))
    for index, a in enumerate(checker.geoms):
        ba = int(model.geom_bodyid[a])
        for b in checker.geoms[index + 1:]:
            bb = int(model.geom_bodyid[b])
            if not ({ba, bb} & planned_bodies):
                continue  # Environment/environment pairs never enter the planning model.
            pair = frozenset((a, b))
            if (internal_gripper_body_pair(ba, bb)
                    or not avoid_collision or a in lift_ignored or b in lift_ignored
                    or pair in getattr(checker, '_arm_environment_geom_pairs', ())):
                ignore = True
            elif a in moving or b in moving:
                ignore = pair not in articulated
            else:
                robot_pair = ba in checker.robot_bodies and bb in checker.robot_bodies
                rigid = frozenset((int(model.body_weldid[ba]), int(model.body_weldid[bb])))
                ignore = (ba == bb or (robot_pair and (
                    model.body_weldid[ba] == model.body_weldid[bb]
                    or rigid in checker.adjacent or frozenset((ba, bb)) in checker.excluded))
                    or named_allowance(ba) or named_allowance(bb)
                    or (not ({ba, bb} & checker.robot_bodies) and held_body not in (ba, bb)))
            if ignore:
                allowed.append([int(a), int(b)])
                continue
            limit = payload_pair_limit(safety_distance, getattr(checker, 'carrying_safety_distance', 0.),
                                       ba, bb, attached_bodies, checker.robot_bodies,
                                       support=articulated.get(pair, False))
            if limit != float(safety_distance):
                base_thresholds.append([int(a), int(b), limit])
            initial = getattr(checker, '_lift_clearance_pairs', {}).get(pair)
            if initial is not None:
                bound = initial - 1e-6 if initial < 0 else max(0., initial - 1e-6)
                limit = min(limit, float(bound))
            if limit != float(safety_distance):
                thresholds.append([int(a), int(b), limit])
    result = {
        'schema_version': 1, 'safety_distance': float(safety_distance),
        'avoid_collision': requested_avoid_collision, 'collision_override': not avoid_collision,
        'allowed_pairs': allowed,
        'arm_environment_pairs': [sorted(pair) for pair in sorted(
            getattr(checker, '_arm_environment_geom_pairs', ()), key=lambda pair: tuple(sorted(pair)))],
        'distance_overrides': thresholds, 'scene_revision': int(getattr(checker, 'scene_revision', 0)),
        'base_distance_overrides': base_thresholds,
        'held_name': getattr(checker.backend.gripper_controller, 'held_name', None),
        'contact_target': getattr(checker.backend, 'contact_target', None),
        'allow_gripper_environment_contact': bool(checker.allow_gripper_environment_contact),
        'lift_ignored_geoms': sorted(getattr(checker, '_lift_ignored_payload_geoms', ())),
        'support_geoms': sorted(checker.support_geoms),
        'initial_clearance_pairs': [[*sorted(pair), float(gap)] for pair, gap in
                                    getattr(checker, '_lift_clearance_pairs', {}).items()],
    }
    result['fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result, probe.probe


def pair_limit(policy: dict, first: int, second: int) -> float | None:
    """Resolve a frozen pair for tests and CPU-side conversion (None = exempt)."""
    pair = sorted((int(first), int(second)))
    if first == second or pair in policy['allowed_pairs']:
        return None
    for a, b, limit in policy['distance_overrides']:
        if pair == [a, b]:
            return float(limit)
    return float(policy['safety_distance'])
