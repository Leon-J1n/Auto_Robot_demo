"""Shared sphere/box planning geometry, independent of simulation contacts.

Every merge stays inside one articulated body and one exact source-policy row.
New boxes and grid spheres enclose their source shapes. The sensitive Robotiq
mechanism retains its previously validated cuRobo sphere fit, including that
fit's approximation error, rather than filling its internal gaps with boxes.
"""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
from itertools import product
import json
from pathlib import Path

import numpy as np

from collision.backends.simplatform_fcl import CollisionResult


DEFAULTS = dict(sphere_cell_size=.035, container_names=['Tray', 'Basket'], batch_size=32)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _pair_key(a, b):
    return tuple(sorted((tuple(sorted(a['source_geoms'])), tuple(sorted(b['source_geoms'])))))


def _corners(low, high):
    return np.array(list(product(*zip(low, high))), dtype=float)


def _limits(source, geometries):
    ids = [g['id'] for g in geometries]
    index = {v: i for i, v in enumerate(ids)}
    result = np.full((len(ids), len(ids)), float(source['safety_distance']))
    np.fill_diagonal(result, -np.inf)
    for a, b in source['allowed_pairs']:
        if a in index and b in index:
            result[index[a], index[b]] = result[index[b], index[a]] = -np.inf
    for a, b, limit in source['distance_overrides']:
        if a in index and b in index and np.isfinite(result[index[a], index[b]]):
            result[index[a], index[b]] = result[index[b], index[a]] = limit
    # Environment/environment relations never enter the planner. Normalize
    # them before comparing policy rows, so irrelevant IDs cannot prevent merges.
    env = [i for i, g in enumerate(geometries) if not g['robot']]
    result[np.ix_(env, env)] = -np.inf
    return result


def _local_points(g, cache):
    kind, size = g['kind'], np.asarray(g['size'], dtype=float)
    if kind == 'mesh':
        import trimesh
        payload = Path(g['file_path']).read_bytes()
        key = sha256(payload).digest()
        if key in cache:
            return cache[key]
        mesh = trimesh.load(g['file_path'], force='mesh', process=False)
        vertices = np.asarray(mesh.vertices, dtype=float)
        if not len(vertices) or not np.isfinite(vertices).all():
            raise ValueError('Invalid proxy source mesh')
        cache[key] = vertices
        if len(cache) > 512:
            cache.pop(next(iter(cache)))
        return vertices
    if kind == 'box':
        half = size[:3]
    elif kind == 'sphere':
        half = np.full(3, size[0])
    elif kind == 'cylinder':
        half = np.array([size[0], size[0], size[1]])
    elif kind == 'capsule':
        half = np.array([size[0], size[0], size[1] + size[0]])
    elif kind == 'ellipsoid':
        half = size[:3]
    else:
        raise ValueError(f'No conservative planning proxy for geometry {kind!r}')
    return _corners(-half, half)


def _cover_box(low, high, cell_size):
    # Each rectangular cell is wholly contained in its circumsphere. Therefore
    # the union covers the entire box, including face interiors, not just vertices.
    lengths = np.maximum(high - low, 1.e-8)
    ratios = lengths / cell_size
    if not np.isfinite(ratios).all() or np.any(ratios > 20000):
        raise ValueError('Proxy sphere budget exceeded; author a simpler collider')
    counts = np.maximum(1, np.ceil(ratios).astype(int))
    if np.prod(counts) > 20000:
        raise ValueError('Proxy sphere budget exceeded; author a simpler collider')
    widths = lengths / counts
    radius = float(np.linalg.norm(widths) / 2)
    axes = [low[i] + (np.arange(counts[i]) + .5) * widths[i] for i in range(3)]
    return [{'center': list(map(float, c)), 'radius': radius} for c in product(*axes)]


def _gripper_spheres(g, templates):
    shape = {key:g[key] for key in ('kind', 'size')}
    if 'file_path' in g:
        shape['mesh_sha256'] = sha256(Path(g['file_path']).read_bytes()).hexdigest()
    template = templates.get(_digest(shape))
    if template is None or template['shape'] != shape:
        raise ValueError(f'No validated gripper proxy template for {g["body_name"]}; author a matching template')
    transform = np.asarray(g['matrix'])
    return [dict(center=(transform[:3,:3] @ s['center'] + transform[:3,3]).tolist(), radius=float(s['radius']))
            for s in template['spheres']]


def simplify_snapshot(raw: dict, options=None, *, point_cache=None, lift_bounds=None) -> dict:
    if options and set(options) - set(DEFAULTS):
        raise ValueError(f'Unknown planning proxy options: {sorted(set(options)-set(DEFAULTS))}')
    options = {**deepcopy(DEFAULTS), **deepcopy(options or {})}
    cell = float(options['sphere_cell_size'])
    if isinstance(options['sphere_cell_size'], bool) or not np.isfinite(cell) or cell < 1.e-5:
        raise ValueError('sphere_cell_size must be positive and finite')
    if not isinstance(options['container_names'], list) or any(not isinstance(name,str) or not name for name in options['container_names']):
        raise ValueError('container_names must contain object names')
    if isinstance(options['batch_size'], bool) or not isinstance(options['batch_size'],int) or not 1 <= options['batch_size'] <= 256:
        raise ValueError('Proxy batch_size must be an integer in [1, 256]')
    geoms = raw['geometries']
    if not any(g['robot'] for g in geoms):
        raise ValueError('Planning proxy requires robot geometry')
    for g in geoms:
        for transform in (g['matrix'], raw['body_world'][str(g['body'])]):
            transform = np.asarray(transform, dtype=float)
            if (transform.shape != (4,4) or not np.isfinite(transform).all()
                    or not np.allclose(transform[3],[0,0,0,1],atol=1.e-8)
                    or not np.allclose(transform[:3,:3].T @ transform[:3,:3],np.eye(3),atol=1.e-7)):
                raise ValueError('Planning proxy requires finite rigid transforms')
        if len(g['size']) != 3 or not np.isfinite(g['size']).all() or np.any(np.asarray(g['size']) < 0):
            raise ValueError('Invalid source proxy dimensions')
    point_cache = {} if point_cache is None else point_cache
    templates = json.loads((Path(__file__).with_name('proxies') / 'robotiq_2f85.json').read_text())['templates']
    limits = _limits(raw['policy'], geoms)
    groups = {}
    for i, g in enumerate(geoms):
        # Preserve authored cylinder/sphere geometry instead of enclosing two
        # differently oriented arm cylinders in a large common rectangular box.
        primitive = g['id'] if g['robot'] and g['kind'] in ('cylinder', 'capsule', 'sphere') else None
        key = (g['body'], g['robot'], limits[i].tobytes(), primitive)
        groups.setdefault(key, []).append(i)
    output = []
    for (_, robot, _, _), members in groups.items():
        first = geoms[members[0]]
        body_world = np.asarray(raw['body_world'][str(first['body'])], dtype=float)
        boxes = []
        for i in members:
            g = geoms[i]
            transform = np.asarray(g['matrix'], dtype=float)
            if not robot:
                transform = np.linalg.inv(body_world) @ transform
            points = _local_points(g, point_cache) @ transform[:3, :3].T + transform[:3, 3]
            boxes.append((points.min(axis=0), points.max(axis=0)))
        bins = [list(range(len(boxes)))]
        if not robot and first['body_name'] in options['container_names']:
            # Five open-top slabs. Assign each source convex part to one side,
            # then enclose that entire part; every source volume remains covered.
            low = np.min([b[0] for b in boxes], axis=0)
            high = np.max([b[1] for b in boxes], axis=0)
            extent = np.maximum(high - low, 1.e-8)
            sides = {}
            for j, (a, z) in enumerate(boxes):
                distances = np.r_[(a - low) / extent, (high - z) / extent]
                thickness = (z - a) / extent
                score = distances + np.r_[thickness, thickness]
                score[5] = np.inf  # No top/lid slab.
                side = int(np.argmin(score))
                sides.setdefault(side, []).append(j)
            bins = list(sides.values())
        for selection in bins:
            selected = [members[j] for j in selection]
            low = np.min([boxes[j][0] for j in selection], axis=0)
            high = np.max([boxes[j][1] for j in selection], axis=0)
            item = dict(id=len(output), body=first['body'], robot=robot,
                        name=f'proxy_{len(output)}', body_name=first['body_name'],
                        source_geoms=[int(geoms[j]['id']) for j in selected],
                        source_index=selected[0])
            if robot:
                if first['body_name'].startswith('robotiq_2f85'):
                    spheres = [s for j in selected for s in _gripper_spheres(geoms[j], templates)]
                elif len(selected) == 1 and first['kind'] == 'sphere':
                    transform = np.asarray(first['matrix'])
                    spheres = [dict(center=transform[:3, 3].tolist(), radius=float(first['size'][0]))]
                elif len(selected) == 1 and first['kind'] in ('cylinder', 'capsule'):
                    radius, half = first['size'][:2]
                    count = max(1, int(np.ceil(2 * half / cell)))
                    transform = np.asarray(first['matrix'])
                    axial = -half + (np.arange(count) + .5) * 2 * half / count
                    if first['kind'] == 'capsule':
                        axial = np.r_[-half, axial, half]
                    cover_radius = float(np.hypot(radius, half / count))
                    centers = np.c_[np.zeros(len(axial)), np.zeros(len(axial)), axial]
                    centers = centers @ transform[:3, :3].T + transform[:3, 3]
                    spheres = [dict(center=c.tolist(), radius=cover_radius) for c in centers]
                else:
                    spheres = _cover_box(low, high, cell)
                item.update(kind='sphere_set', size=[0., 0., 0.], matrix=np.eye(4).tolist(), spheres=spheres)
            else:
                local = np.eye(4); local[:3, 3] = (low + high) / 2
                item.update(kind='box', size=((high - low) / 2 + 1.e-8).tolist(),
                            local_matrix=local.tolist(), matrix=(body_world @ local).tolist())
            output.append(item)
    policy = deepcopy(raw['policy'])
    policy.update(allowed_pairs=[], distance_overrides=[], arm_environment_pairs=[],
                  source_fingerprint=policy['fingerprint'])
    old_arm = {tuple(sorted(p)) for p in raw['policy'].get('arm_environment_pairs', [])}
    base_overrides = {tuple(sorted((a,b))): value for a,b,value in raw['policy'].get('base_distance_overrides', [])}
    for i, a in enumerate(output):
        for b in output[i + 1:]:
            if not (a['robot'] or b['robot']):
                continue
            limit = limits[a['source_index'], b['source_index']]
            if np.isfinite(limit) and lift_bounds and _pair_key(a,b) in lift_bounds:
                base = base_overrides.get(tuple(sorted((a['source_geoms'][0], b['source_geoms'][0]))), policy['safety_distance'])
                limit = min(base, lift_bounds[_pair_key(a,b)])
            pair = [a['id'], b['id']]
            if not np.isfinite(limit):
                policy['allowed_pairs'].append(pair)
            elif limit != policy['safety_distance']:
                policy['distance_overrides'].append([*pair, float(limit)])
            if all(tuple(sorted((x, y))) in old_arm for x in a['source_geoms'] for y in b['source_geoms']):
                policy['arm_environment_pairs'].append(pair)
    for item in output:
        item.pop('source_index')
    report = dict(schema_version=1, geometry_source='shared_simplified_proxy',
                  source_geometry_count=len(geoms), proxy_geometry_count=len(output),
                  robot_spheres=sum(len(g.get('spheres', [])) for g in output),
                  world_boxes=sum(not g['robot'] for g in output), options=options,
                  coverage='outer_envelope_except_retained_gripper_sphere_fit',
                  gripper_fit='retained_validated_cuRobo_voxel_templates',
                  maximum_box_cover_extra_m=float(np.sqrt(3) * cell / 2))
    policy['fingerprint'] = _digest([policy, output, report])
    return {**raw, 'geometries': output, 'policy': policy, 'planning_proxy': report}


class PlanningProxy:
    """CPU batch queries and cuRobo export over the exact same proxy parameters."""

    _COMPILED_FIELDS = ('snapshot', 'spheres', 'sphere_bodies', 'sphere_geoms',
        'sphere_groups', 'group_counts', 'group_starts', 'group_radii',
        'self_pairs', 'self_pair_groups', 'self_group_limits', 'world_pairs',
        'world_pair_groups', 'world_group_limits', 'world_half', 'source_geometries', 'source_limits')

    def __init__(self, checker, options=None):
        self.checker = checker
        self.options = {**deepcopy(DEFAULTS), **deepcopy(options or {})}
        self._key = None
        self._paths = OrderedDict()
        self._data = None
        self._point_cache = {}
        self.lift_bounds = {}
        self._scope_state = None
        self._structure_key = None
        self._compiled = OrderedDict()
        self.stats = dict(builds=0, structure_hits=0, state_refreshes=0, configurations=0, path_queries=0, path_hits=0)

    def prepare(self, constraints, *, allowed_geom_ids=None, gripper_width=None):
        from collision.planning_policy import planning_signature
        from libraries.curobo.runtime_policy import export_runtime
        checker = self.checker
        state = self._scope_state or planning_signature(checker.backend, checker)
        key = (state, _digest(self.options), repr(self.lift_bounds),
               float(constraints.get('safety_distance', 0.)), constraints.get('avoid_collision', True),
               tuple(sorted(map(str, allowed_geom_ids or ()))), gripper_width)
        if key != self._key:
            self._paths.clear()
            self._key = None
            structure_key = (planning_signature(checker.backend, checker, include_state=False), *key[1:])
            if structure_key != self._structure_key and structure_key in self._compiled:
                for name, value in self._compiled[structure_key].items():
                    setattr(self, name, value)
                self._structure_key = structure_key
                self._compiled.move_to_end(structure_key)
                self.stats['structure_hits'] += 1
            if structure_key == self._structure_key:
                snapshot = export_runtime(checker.backend, checker, constraints, _raw=True,
                    gripper_width=gripper_width, allowed_geom_ids=allowed_geom_ids,
                    _template=self.snapshot)
                # Geometry and effective pair limits are unchanged, but neither
                # world poses nor robot/gripper/payload kinematics may be reused.
                snapshot['policy'] = deepcopy(snapshot['policy'])
                snapshot['policy']['scene_revision'] = int(getattr(checker, 'scene_revision', 0))
                snapshot['policy']['fingerprint'] = _digest([
                    snapshot['policy']['source_fingerprint'], snapshot['policy']['scene_revision'],
                    snapshot['geometries'], snapshot['bodies']])
                self.snapshot = snapshot
                self.geometries = snapshot['geometries']
                self.robots = [g for g in self.geometries if g['robot']]
                self.world = [g for g in self.geometries if not g['robot']]
                self.world_centers = np.array([np.asarray(g['matrix'])[:3, 3] for g in self.world]).reshape(-1, 3)
                self.world_rotations = np.array([np.asarray(g['matrix'])[:3, :3] for g in self.world]).reshape(-1, 3, 3)
                self._key = key
                self.stats['state_refreshes'] += 1
                return self.snapshot
            raw = export_runtime(checker.backend, checker, constraints, _raw=True,
                                 gripper_width=gripper_width, allowed_geom_ids=allowed_geom_ids)
            snapshot = simplify_snapshot(raw, self.options, point_cache=self._point_cache, lift_bounds=self.lift_bounds)
            self.source_geometries = {g['id']: g for g in raw['geometries']}
            source_limits = _limits(raw['policy'], raw['geometries'])
            self.source_limits = {(a['id'], b['id']): source_limits[i, j]
                                  for i, a in enumerate(raw['geometries'])
                                  for j, b in enumerate(raw['geometries'])
                                  if a['robot'] and not b['robot']}
            self.snapshot = snapshot
            self.geometries = snapshot['geometries']
            self.robots = [g for g in self.geometries if g['robot']]
            self.world = [g for g in self.geometries if not g['robot']]
            self.spheres = np.array([[*s['center'], s['radius']] for g in self.robots for s in g['spheres']])
            self.sphere_bodies = np.array([g['body'] for g in self.robots for s in g['spheres']], dtype=int)
            self.sphere_geoms = np.array([g['id'] for g in self.robots for s in g['spheres']], dtype=int)
            self.sphere_groups = np.repeat(np.arange(len(self.robots)), [len(g['spheres']) for g in self.robots])
            self.group_counts = np.array([len(g['spheres']) for g in self.robots])
            self.group_starts = np.r_[0, np.cumsum(self.group_counts)[:-1]]
            group_radii = []
            for group in self.robots:
                local = np.array([s['center'] for s in group['spheres']])
                radii = np.array([s['radius'] for s in group['spheres']])
                group_radii.append(np.max(np.linalg.norm(local-local.mean(axis=0),axis=1)+radii))
            self.group_radii = np.array(group_radii)
            limits = _limits(snapshot['policy'], self.geometries)
            aa, bb = np.triu_indices(len(self.spheres), 1)
            values = limits[self.sphere_geoms[aa], self.sphere_geoms[bb]]
            keep = np.isfinite(values)
            self.self_pairs = aa[keep], bb[keep], values[keep]
            self.self_pair_groups = self.sphere_groups[aa[keep]] * len(self.robots) + self.sphere_groups[bb[keep]]
            group_ids = np.array([g['id'] for g in self.robots], dtype=int)
            self.self_group_limits = limits[np.ix_(group_ids, group_ids)]
            aa, bb = np.indices((len(self.spheres), len(self.world))).reshape(2, -1)
            world_ids = np.array([g['id'] for g in self.world], dtype=int)
            values = limits[self.sphere_geoms[aa], world_ids[bb]]
            keep = np.isfinite(values)
            self.world_pairs = aa[keep], bb[keep], values[keep]
            self.world_pair_groups = self.sphere_groups[aa[keep]] * len(self.world) + bb[keep]
            self.world_group_limits = limits[np.ix_(group_ids, world_ids)]
            self.world_centers = np.array([np.asarray(g['matrix'])[:3, 3] for g in self.world]).reshape(-1, 3)
            self.world_rotations = np.array([np.asarray(g['matrix'])[:3, :3] for g in self.world]).reshape(-1, 3, 3)
            self.world_half = np.array([g['size'] for g in self.world]).reshape(-1, 3)
            self._key = key
            self._structure_key = structure_key
            self._compiled[structure_key] = {name: getattr(self, name) for name in self._COMPILED_FIELDS}
            self._compiled.move_to_end(structure_key)
            while len(self._compiled) > 4:
                self._compiled.popitem(last=False)
            self.stats['builds'] += 1
        return self.snapshot

    def export_runtime(self, constraints):
        return deepcopy(self.prepare(constraints))

    @contextmanager
    def query_scope(self):
        """One exact before/after state guard for a synchronous plan."""
        from collision.planning_policy import planning_signature
        if self._scope_state is not None:
            yield
            return
        before = planning_signature(self.checker.backend, self.checker)
        self._scope_state = before
        try:
            yield
        finally:
            self._scope_state = None
            if before != planning_signature(self.checker.backend, self.checker):
                self._paths.clear()
                raise RuntimeError('Scene or policy changed during proxy collision validation')

    def capture_lift_clearance(self, safety_distance):
        """Measure proxy gaps at scope entry; never copy a mesh's gap to spheres."""
        self.lift_bounds.clear()
        self._key = None
        self.prepare(dict(safety_distance=safety_distance))
        centers = self._centers([self.checker.backend.get_joint_positions()], None)[0]
        source = set(self.checker._lift_clearance_pairs)
        for robot in self.robots:
            indices = np.flatnonzero(self.sphere_geoms == robot['id'])
            for j, world in enumerate(self.world):
                if not any(frozenset((a,b)) in source for a in robot['source_geoms'] for b in world['source_geoms']):
                    continue
                delta = (centers[indices] - self.world_centers[j]) @ self.world_rotations[j]
                q = np.abs(delta) - self.world_half[j]
                gap = float(np.min(np.linalg.norm(np.maximum(q,0),axis=-1) + np.minimum(np.max(q,axis=-1),0) - self.spheres[indices,3]))
                lower = gap - 1.e-6 if gap < 0 else max(0., gap - 1.e-6)
                self.lift_bounds[_pair_key(robot,world)] = lower
        self._key = None

    def _centers(self, configurations, gripper_width):
        import mujoco
        from scipy.spatial.transform import Rotation
        checker, runtime = self.checker, self.checker.backend
        model = runtime.model
        if self._data is None:
            self._data = mujoco.MjData(model)
        data = self._data
        data.qpos[:] = runtime.data.qpos
        data.mocap_pos[:] = runtime.data.mocap_pos
        data.mocap_quat[:] = runtime.data.mocap_quat
        if gripper_width is not None:
            from demo.interaction.table_clearance import set_static_gripper_width
            set_static_gripper_width(model, data, gripper_width)
        controller = runtime.gripper_controller
        held = getattr(controller, 'held_name', None)
        payload = runtime.object_ids.get(held) if held else None
        free = payload is not None and model.body_jntnum[payload] and model.jnt_type[model.body_jntadr[payload]] == mujoco.mjtJoint.mjJNT_FREE
        centers = []
        for q in configurations:
            data.qpos[runtime.qpos_indices] = q
            mujoco.mj_kinematics(model, data)
            if free:
                pose = np.eye(4)
                pose[:3, :3] = data.xmat[controller.base_id].reshape(3, 3)
                pose[:3, 3] = data.xpos[controller.base_id]
                relative = controller._relative(payload) if controller.mode == 'physical' else controller.relative_pose
                pose = pose @ relative
                address = model.jnt_qposadr[model.body_jntadr[payload]]
                data.qpos[address:address + 3] = pose[:3, 3]
                data.qpos[address + 3:address + 7] = Rotation.from_matrix(pose[:3, :3]).as_quat()[[3, 0, 1, 2]]
                mujoco.mj_kinematics(model, data)
            rotations = data.xmat[self.sphere_bodies].reshape(-1, 3, 3)
            centers.append(np.einsum('nij,nj->ni', rotations, self.spheres[:, :3]) + data.xpos[self.sphere_bodies])
        self.stats['configurations'] += len(configurations)
        return np.asarray(centers)

    def _evaluate(self, points, gripper_width=None):
        centers = self._centers(points, gripper_width)
        group_centers = np.add.reduceat(centers, self.group_starts, axis=1) / self.group_counts[None,:,None]
        group_gap = np.linalg.norm(group_centers[:,:,None,:] - group_centers[:,None,:,:], axis=-1)
        group_gap -= self.group_radii[:,None] + self.group_radii[None,:]
        possible = np.any(group_gap <= self.self_group_limits[None,:,:] + 1.e-8, axis=0).reshape(-1)
        self_selection = (np.flatnonzero(possible[self.self_pair_groups]) if getattr(self, 'broadphase', True)
                          else np.arange(len(self.self_pairs[0])))
        aa, bb, limits = self.self_pairs
        aa, bb, limits = aa[self_selection], bb[self_selection], limits[self_selection]
        distances = np.linalg.norm(centers[:, aa] - centers[:, bb], axis=-1) - self.spheres[aa, 3] - self.spheres[bb, 3]
        delta = group_centers[:,:,None,:] - self.world_centers[None,None,:,:]
        local = np.einsum('bgwi,wij->bgwj', delta, self.world_rotations)
        q = np.abs(local) - self.world_half[None,None,:,:]
        gap = np.linalg.norm(np.maximum(q,0),axis=-1) + np.minimum(np.max(q,axis=-1),0) - self.group_radii[None,:,None]
        possible = np.any(gap <= self.world_group_limits[None,:,:] + 1.e-8, axis=0).reshape(-1)
        world_selection = (np.flatnonzero(possible[self.world_pair_groups]) if getattr(self, 'broadphase', True)
                           else np.arange(len(self.world_pairs[0])))
        wa, wb, wlimits = self.world_pairs
        wa, wb, wlimits = wa[world_selection], wb[world_selection], wlimits[world_selection]
        delta = centers[:, wa] - self.world_centers[wb]
        local = np.einsum('bpi,pij->bpj', delta, self.world_rotations[wb])
        q = np.abs(local) - self.world_half[wb]
        world = np.linalg.norm(np.maximum(q, 0), axis=-1) + np.minimum(np.max(q, axis=-1), 0) - self.spheres[wa, 3]
        distances = np.concatenate((distances, world), axis=1)
        thresholds = np.r_[limits, wlimits]
        violating = distances <= thresholds[None, :] + 1.e-9
        # Axial covering balls extend past a finite cylinder's flat end caps.
        # Confirm these world candidates against the original convex geometry;
        # the enclosing proxy remains the inexpensive rejection broadphase.
        for row in np.flatnonzero(np.any(violating[:, len(aa):], axis=1)):
            candidates = {}
            for j in np.flatnonzero(violating[row, len(aa):]):
                robot = self.geometries[self.sphere_geoms[wa[j]]]
                sources = robot['source_geoms']
                if len(sources) == 1 and self.source_geometries[sources[0]]['kind'] == 'cylinder':
                    candidates.setdefault((robot['id'], int(wb[j])), []).append(j)
            if candidates:
                import mujoco
                self._centers([points[row]], gripper_width)
                for (ident, world_index), indices in candidates.items():
                    source = self.geometries[ident]['source_geoms'][0]
                    gaps = []
                    collision = False
                    for other in self.world[world_index]['source_geoms']:
                        limit = self.source_limits[source, other]
                        if not np.isfinite(limit):
                            continue
                        distance = mujoco.mj_geomDistance(self.checker.backend.model, self._data,
                                                         source, other, max(1., limit + 1.), None)
                        gaps.append(distance)
                        collision |= distance < limit - 1.e-9
                    indices = len(aa) + np.asarray(indices)
                    violating[row, indices] = collision
                    distances[row, indices] = min(gaps, default=np.inf)
        hits = np.flatnonzero(np.any(violating, axis=1))
        if not len(hits):
            return CollisionResult(False, float(np.min(distances, initial=np.inf)), ())
        row = int(hits[0])
        contacts = []
        for index in np.flatnonzero(violating[row])[:64]:
            if index < len(aa):
                a, b = self.sphere_geoms[aa[index]], self.sphere_geoms[bb[index]]
                own = True
            else:
                j = index - len(aa)
                a, b = self.sphere_geoms[wa[j]], self.world[wb[j]]['id']
                own = False
            contacts.append(dict(geom1=self.geometries[a]['body_name'], geom2=self.geometries[b]['body_name'],
                                 distance=float(distances[row, index]), self_collision=own, geometry_source='shared_simplified_proxy'))
        return CollisionResult(True, float(np.min(distances[:row + 1], initial=np.inf)), tuple(contacts))

    def check(self, joints, *, safety_distance=0., allowed_geom_ids=None, gripper_width=None):
        points = self._validate(np.asarray(joints, dtype=float).reshape(1, -1), 1., safety_distance)
        self.prepare(dict(safety_distance=safety_distance), allowed_geom_ids=allowed_geom_ids, gripper_width=gripper_width)
        result = self._evaluate(points, gripper_width)
        self._assert_current()
        return result

    def _assert_current(self):
        from collision.planning_policy import planning_signature
        if self._scope_state is not None:
            return
        if planning_signature(self.checker.backend, self.checker) != self._key[0]:
            self._paths.clear()
            raise RuntimeError('Scene or policy changed during proxy collision validation')

    def _validate(self, points, resolution, safety_distance):
        count = len(self.checker.backend.qpos_indices)
        if points.ndim != 2 or points.shape[1] != count or not len(points) or not np.isfinite(points).all():
            raise ValueError('Proxy path requires finite joint vectors')
        if not np.isfinite(resolution) or resolution <= 0 or not np.isfinite(safety_distance):
            raise ValueError('Proxy clearance and positive resolution must be finite')
        bounds = np.asarray(self.checker.backend.joint_limits)
        if np.any(points < bounds[:, 0] - 1.e-9) or np.any(points > bounds[:, 1] + 1.e-9):
            raise ValueError('Proxy collision joints violate joint limits')
        return points

    def check_path(self, waypoints, *, resolution=.05, safety_distance=0., allowed_geom_ids=None):
        points = self._validate(np.asarray(waypoints, dtype=float), resolution, safety_distance)
        self.prepare(dict(safety_distance=safety_distance), allowed_geom_ids=allowed_geom_ids)
        self.stats['path_queries'] += 1
        key = (points.shape, points.tobytes(), float(resolution), float(safety_distance))
        if key in self._paths:
            self.stats['path_hits'] += 1
            self._paths.move_to_end(key)
            return deepcopy(self._paths[key])
        batch, minimum = [], float('inf')
        def samples():
            yield points[0]
            for a, z in zip(points[:-1], points[1:]):
                count = max(1, int(np.ceil(np.max(np.abs(z - a)) / max(resolution, 1.e-9))))
                for j in range(1, count + 1):
                    yield z if j == count else a + (j / count) * (z - a)
        size = int(self.options['batch_size'])
        if size < 1 or size > 256:
            raise ValueError('Proxy batch_size must be in [1, 256]')
        for q in samples():
            batch.append(q)
            if len(batch) == size:
                result = self._evaluate(batch)
                minimum = min(minimum, result.minimum_distance)
                if result.in_collision:
                    self._assert_current()
                    return CollisionResult(True, minimum, result.contacts)
                batch = []
        if batch:
            result = self._evaluate(batch)
            minimum = min(minimum, result.minimum_distance)
            if result.in_collision:
                self._assert_current()
                return CollisionResult(True, minimum, result.contacts)
        result = CollisionResult(False, minimum, ())
        self._assert_current()
        self._paths[key] = result
        while len(self._paths) > 32:
            self._paths.popitem(last=False)
        return result
