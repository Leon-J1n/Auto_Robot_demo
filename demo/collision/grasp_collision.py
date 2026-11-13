"""FCL arm checks plus current articulated fingers and carried payload geometry."""
import mujoco
import numpy as np
from scipy.spatial.transform import Rotation
from collections import OrderedDict
from copy import deepcopy
from contextlib import contextmanager
import pickle
from time import perf_counter

from collision.simplatform_fcl import SimPlatformFCLCollisionChecker, CollisionResult
from execution.resources import component


class GraspCollisionChecker(SimPlatformFCLCollisionChecker):
    def _path_validation_state(self):
        """Exact live scene/model/policy guard, never a rounded joint key."""
        from planning.prepared_trajectory import planning_state_signature
        b = self.backend
        c = b.gripper_controller
        policy = (
            self._scene_cache_stamp(),
            getattr(self, 'scene_revision', 0),
            frozenset(self.allowed_target_names), frozenset(self._names.items()),
            frozenset(self.dynamic_names), frozenset(self.dynamic_handles),
            frozenset(self.tool_geoms), frozenset(self.robot_bodies),
            frozenset(self.geoms), frozenset(self.excluded), frozenset(self.adjacent),
            tuple(self.body_names), frozenset(self._collider_links.items()),
            getattr(b, 'contact_target', None), getattr(b, 'contact_target_object_id', None),
            getattr(self, '_physical_closure_target', None),
            getattr(c, 'held_name', None), getattr(c, 'mode', None), getattr(c, 'base_id', None),
            np.asarray(getattr(c, 'relative_pose', [])).tobytes(),
            pickle.dumps(getattr(b, 'grasp_approach_goal', None)),
            pickle.dumps(getattr(getattr(b, 'resources', None), 'scene', None)),
            tuple(np.asarray(getattr(b.data, name, [])).tobytes() for name in ('xpos', 'xmat')),
            tuple(np.asarray(getattr(b.model, name, [])).tobytes() for name in
                  ('geom_rbound', 'body_weldid', 'mesh_vert', 'mesh_face', 'mesh_vertadr',
                   'mesh_vertnum', 'mesh_faceadr', 'mesh_facenum', 'geom_dataid', 'jnt_qposadr')),
            np.asarray(b.qpos_indices).tobytes(),
            tuple(sorted(getattr(b, 'object_ids', {}).items())),
            self.allow_gripper_environment_contact, frozenset(self.support_geoms),
            frozenset(getattr(self, '_lift_clearance_pairs', {}).items()),
            getattr(self, '_lift_ignored_payload_geoms', frozenset()),
            getattr(self, '_lift_ignored_payload_names', frozenset()),
        )
        return planning_state_signature(b, self), policy

    def check_path(self, waypoints, *, resolution=.05, safety_distance=0., allowed_geom_ids=None):
        # The base FCL path cache is disabled for live articulated geometry.
        # Reuse only an identical whole query guarded by BOTH live and FCL state.
        if not hasattr(self, 'backend') or not getattr(self, 'path_validation_cache_enabled', False):
            return super().check_path(waypoints, resolution=resolution,
                safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        points = np.asarray(waypoints, dtype=float)
        # RRT edges and TrajOpt local probes are small; hashing a whole scene
        # for those would cost more than the reuse opportunity is worth.
        if (points.ndim != 2 or len(points) < 4 or not np.all(np.isfinite(points))
                or not np.isfinite(resolution) or resolution <= 0 or not np.isfinite(safety_distance)):
            return super().check_path(points, resolution=resolution,
                safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        if not hasattr(self, '_validated_paths'):
            self._validated_paths = OrderedDict()
            self.path_validation_cache_stats = {'hits': 0, 'misses': 0, 'invalidations': 0,
                                                'reused_waypoints': 0, 'state_guard_s': 0.0}
        def state_snapshot():
            started = perf_counter()
            state = self._path_validation_state()
            self.path_validation_cache_stats['state_guard_s'] += perf_counter() - started
            return state
        state = state_snapshot()
        if state != getattr(self, '_validated_path_state', None):
            self._validated_paths.clear()
            self._query_cache.clear()
            self._validated_path_state = state
            self.path_validation_cache_stats['invalidations'] += 1
        key = (points.shape, points.tobytes(), float(resolution), float(safety_distance),
               frozenset(allowed_geom_ids or ()))
        if key in self._validated_paths:
            self.path_validation_cache_stats['hits'] += 1
            self.path_validation_cache_stats['reused_waypoints'] += len(points)
            self._validated_paths.move_to_end(key)
            return deepcopy(self._validated_paths[key])
        self.path_validation_cache_stats['misses'] += 1
        result = super().check_path(points, resolution=resolution,
            safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        # Never publish a certificate if a probe or callback changed the guard.
        if state_snapshot() == state:
            self._validated_paths[key] = deepcopy(result)
            while len(self._validated_paths) > 128:
                self._validated_paths.popitem(last=False)
        return result

    @contextmanager
    def lift_payload_collision_scope(self):
        """Ignore only the captured held object's collisions during one lift."""
        old_geoms = getattr(self, '_lift_ignored_payload_geoms', frozenset())
        old_names = getattr(self, '_lift_ignored_payload_names', frozenset())
        b = self.backend
        held = getattr(b.gripper_controller, 'held_name', None)
        root = getattr(b, 'object_ids', {}).get(held) if held else None
        bodies = set()
        if root is not None:
            for body in range(1, b.model.nbody):
                ancestor = body
                while ancestor > 0:
                    if ancestor == int(root):
                        bodies.add(body)
                        break
                    ancestor = int(b.model.body_parentid[ancestor])
        self._lift_ignored_payload_geoms = frozenset(
            g for g in self.geoms if int(b.model.geom_bodyid[g]) in bodies)
        self._lift_ignored_payload_names = frozenset(self.body_names[body] for body in bodies)
        try:
            yield
        finally:
            self._lift_ignored_payload_geoms = old_geoms
            self._lift_ignored_payload_names = old_names

    def on_scene_sync(self, state, *, changed_objects=(), revision=0):
        # The MuJoCo probe reads the live backend qpos on every query. Keep the
        # revision for diagnostics so planners can confirm which scene snapshot
        # was used without rebuilding unchanged FCL entities.
        self.scene_revision = int(revision)
        self.scene_changed_objects = tuple(changed_objects)
        # Any synchronized scene update can change dynamic contacts. Keep the
        # cache scoped to one synchronized snapshot.
        if (getattr(self, '_query_cache_revision', None) != self.scene_revision or
                getattr(self, '_query_cache_objects', None) != self.scene_changed_objects):
            getattr(self, '_query_cache', {}).clear()
            self._query_cache_revision = self.scene_revision
            self._query_cache_objects = self.scene_changed_objects

    def bind_backend(self, backend):
        self.backend = backend
        self.dynamic_names = {o['object_name_in_scene'] for o in backend.resources.scene['objects']
                              if component(o, 'RigidBody').get('dynamic')
                              or bool(component(o, 'ArticulatedModel'))}
        # The articulated query has its own state-aware cache. The native FCL
        # cache remains disabled here because finger/payload poses are live.
        self.cache_enabled = False
        self._query_cache = OrderedDict()
        self._query_cache_revision = getattr(self, 'scene_revision', 0)
        self._query_cache_objects = getattr(self, 'scene_changed_objects', ())
        self.carrying_safety_distance = 0.0
        self.carrying_contact_log = {}
        self.allowed_proxy_target_names = set()
        self.allow_gripper_environment_contact = False
        entries = {int(e.entity): e for e in self.world.editor_entities()}
        handles = [h for h, e in entries.items() if e.collider is not None]
        tool_handles = [h for h, link in self._collider_links.items() if link.startswith('robotiq_2f85')]
        self.baseline_allowed_pairs.update(frozenset((a, z)) for a in tool_handles for z in handles if a != z)
        roots = {int(self._entities[name].handle) for name in self.dynamic_names}
        self.dynamic_handles = set()
        for handle in handles:
            ancestor = handle
            while ancestor in entries:
                if ancestor in roots:
                    self.dynamic_handles.add(handle)
                    break
                ancestor = entries[ancestor].parent
        self.baseline_allowed_pairs.update(frozenset((a, z)) for a in self.dynamic_handles for z in self._robot_handles)
        self._arm_policy = set(self.baseline_allowed_pairs)
        self._policy_held = None
        model = backend.model
        self.probe = mujoco.MjData(model)
        self.geom_names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i) or f'geom_{i}' for i in range(model.ngeom)]
        self.body_names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i) or 'world' for i in range(model.nbody)]
        self.tcp_site_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, 'tcp')
        self.geoms = [i for i in range(model.ngeom) if model.geom_contype[i] or model.geom_conaffinity[i]]
        self.tool_geoms = {i for i in self.geoms if self.body_names[model.geom_bodyid[i]].startswith('robotiq_2f85')}
        self.robot_bodies = set()
        for i in range(model.nbody):
            parent = i
            while parent > 0:
                if parent == backend.base_id:
                    self.robot_bodies.add(i); break
                parent = int(model.body_parentid[parent])
        self.adjacent = {frozenset((int(model.body_weldid[i]), int(model.body_weldid[model.body_parentid[i]]))) for i in range(1, model.nbody)}
        self.excluded = {frozenset((int(v) >> 16, int(v) & 65535)) for v in model.exclude_signature}
        for a, b in component(backend.resources.robot_object, 'RobotModelRef').get('allowed_collision_link_pairs', []):
            self.excluded.add(frozenset((self.body_names.index(a), self.body_names.index(b))))
        self.support_geoms = set()
        self._tool_environment_pairs = np.asarray([
            (tool, other) for tool in self.tool_geoms for other in self.geoms
            if other not in self.tool_geoms
            and int(model.geom_bodyid[other]) not in self.robot_bodies], dtype=int).reshape(-1, 2)

    def set_entity_transform(self, name, **pose):
        dynamic = getattr(self, 'dynamic_names', set())
        if name in dynamic or any(str(name).startswith(root + '__') for root in dynamic):
            # Dynamic object/robot and payload/environment geometry is checked from
            # current MuJoCo poses below; its stale FCL copy is fully excluded.
            return
        return super().set_entity_transform(name, **pose)

    def _is_allowed(self, pair, allowed):
        # Fixed-open FCL tool geometry is replaced by measured articulated geometry below.
        handles = (int(pair.robot_collider), int(pair.other))
        if any(self._names.get(h) in getattr(self, '_lift_ignored_payload_names', ()) for h in handles):
            return True
        if any(h in getattr(self, 'dynamic_handles', set()) for h in handles):
            return True
        if any(self._collider_links.get(h, '').startswith('robotiq_2f85') for h in handles):
            return True
        controller = getattr(getattr(self, 'backend', None), 'gripper_controller', None)
        held = getattr(controller, 'held_name', None)
        if held and any(self._names.get(h) == held for h in handles):
            return True  # Payload/arm checks are evaluated at the candidate payload pose below.
        return super()._is_allowed(pair, allowed)

    def _at_grasp_goal(self, data):
        """Return whether the probe TCP is in the terminal grasp region."""
        goal = getattr(getattr(self, 'backend', None), 'grasp_approach_goal', None)
        if goal is None or not hasattr(self, 'backend'):
            return False
        position, _, *goal_joints = goal
        try:
            if goal_joints:
                expected = np.asarray(goal_joints[0], dtype=float)
                actual = np.asarray(data.qpos[self.backend.qpos_indices], dtype=float)
                # The final linear sample can be slightly different from the
                # selected IK branch after time-parameterization. Keep the
                # exception limited to the terminal joint neighborhood.
                if expected.shape == actual.shape and np.max(np.abs(actual - expected)) <= 0.5:
                    return True
            if self.tcp_site_id >= 0:
                tcp = np.asarray(data.site_xpos[self.tcp_site_id], dtype=float)
            else:
                tcp = np.asarray(data.xpos[self.backend.gripper_controller.base_id], dtype=float)
            return float(np.linalg.norm(tcp - np.asarray(position, dtype=float))) <= 0.012
        except (AttributeError, IndexError, TypeError, ValueError):
            return False

    def check_gripper_pose(self, world_from_control, *, safety_distance=0.0,
                           gripper_width=None, floor_z=None):
        """Precheck only the articulated gripper at a known TCP pose, without IK."""
        from types import SimpleNamespace
        from demo.interaction.table_clearance import gripper_clearance, set_static_gripper_width

        backend, model = self.backend, self.backend.model
        pose = np.asarray(world_from_control, dtype=float)
        if pose.shape != (4, 4) or not np.isfinite(pose).all():
            raise ValueError('Gripper pose precheck requires a finite 4x4 transform')
        if getattr(self, 'allow_gripper_environment_contact', False):
            return CollisionResult(False, float('inf'), ())
        data = getattr(self, '_gripper_pose_probe', None)
        if data is None:
            data = self._gripper_pose_probe = mujoco.MjData(model)
        data.qpos[:] = backend.data.qpos
        data.qvel[:] = 0
        if gripper_width is not None:
            set_static_gripper_width(model, data, gripper_width)
        mujoco.mj_kinematics(model, data)
        if self.tcp_site_id < 0:
            raise ValueError('Gripper pose precheck requires the configured TCP site')
        world_from_current = np.eye(4)
        world_from_current[:3, :3] = data.site_xmat[self.tcp_site_id].reshape(3, 3)
        world_from_current[:3, 3] = data.site_xpos[self.tcp_site_id]
        delta = pose @ np.linalg.inv(world_from_current)
        tool_ids = np.asarray(sorted(self.tool_geoms), dtype=int)
        if not len(tool_ids):
            return CollisionResult(False, float('inf'), ())
        tool_positions = data.geom_xpos[tool_ids].copy()
        tool_rotations = data.geom_xmat[tool_ids].reshape(-1, 3, 3).copy()
        data.geom_xpos[tool_ids] = tool_positions @ delta[:3, :3].T + delta[:3, 3]
        data.geom_xmat[tool_ids] = np.einsum('ij,njk->nik', delta[:3, :3], tool_rotations).reshape(-1, 9)

        if floor_z is not None:
            from demo.interaction.table_clearance import TABLE_FLOOR_OFFSET_M
            floor_limit = float(floor_z) + TABLE_FLOOR_OFFSET_M
            clearance = gripper_clearance(SimpleNamespace(model=model, data=data), floor_limit)
            if not clearance['success']:
                return CollisionResult(True, float(clearance['clearance_m']), ({
                    'geom1': clearance['lowest_link'], 'geom2': 'table_floor',
                    'distance': float(clearance['clearance_m']), 'self_collision': False},))

        pairs = getattr(self, '_tool_environment_pairs', None)
        if pairs is None:
            pairs = np.asarray([(tool, other) for tool in self.tool_geoms for other in self.geoms
                                if other not in self.tool_geoms
                                and int(model.geom_bodyid[other]) not in self.robot_bodies], dtype=int).reshape(-1, 2)
        if not len(pairs):
            return CollisionResult(False, float('inf'), ())
        tool_ids, other_ids = pairs[:, 0], pairs[:, 1]
        target = (getattr(backend.gripper_controller, 'held_name', None)
                  or getattr(backend, 'contact_target', None)
                  or getattr(self, '_physical_closure_target', None))
        if target:
            target_id = (getattr(backend, 'contact_target_object_id', None)
                         or getattr(self, '_physical_closure_target', None))
            target_bodies = set()
            if target_id is not None:
                for obj in backend.resources.scene['objects']:
                    if str(obj.get('object_id_in_scene')) == str(target_id):
                        root = backend.object_ids.get(obj.get('object_name_in_scene'))
                        if root is not None:
                            target_bodies.add(int(root))
                            changed = True
                            while changed:
                                changed = False
                                for body in range(1, model.nbody):
                                    if int(model.body_parentid[body]) in target_bodies and body not in target_bodies:
                                        target_bodies.add(body)
                                        changed = True
                        break
            other_bodies = model.geom_bodyid[other_ids]
            if target_bodies:
                pairs = pairs[~np.isin(other_bodies, tuple(target_bodies))]
            else:
                target_text = str(target)
                target_prefix = target_text.rsplit('_', 1)[0]
                names = np.asarray([self.body_names[int(body)] for body in other_bodies])
                matches = np.asarray([name == target_text or name.startswith(target_text + '_')
                                      or target_text.startswith(name + '_')
                                      or name.startswith(target_prefix + '_')
                                      for name in names])
                pairs = pairs[~matches]
            tool_ids, other_ids = pairs[:, 0], pairs[:, 1]
        if not len(pairs):
            return CollisionResult(False, float('inf'), ())
        gap = (np.linalg.norm(data.geom_xpos[tool_ids] - data.geom_xpos[other_ids], axis=1)
               - model.geom_rbound[tool_ids] - model.geom_rbound[other_ids])
        threshold = float(safety_distance)
        candidate_indices = np.flatnonzero(gap <= max(threshold, .005))
        contacts, minimum = [], float('inf')
        for index in candidate_indices:
            tool, other = int(tool_ids[index]), int(other_ids[index])
            distance = float(mujoco.mj_geomDistance(
                model, data, tool, other, max(.005, threshold + .001), None))
            minimum = min(minimum, distance)
            if distance <= threshold + 1e-9:
                contacts.append({'geom1': self.body_names[int(model.geom_bodyid[tool])],
                                 'geom2': self.body_names[int(model.geom_bodyid[other])],
                                 'distance': distance, 'self_collision': False})
        return CollisionResult(bool(contacts), minimum, tuple(contacts))

    def capture_support(self):
        """Allow existing resting contact while the initial retreat lifts away."""
        b = self.backend
        c = b.gripper_controller
        held_body = b.object_ids[c.held_name]
        self.support_geoms = set()
        for contact in b.data.contact:
            for payload, other in ((contact.geom1, contact.geom2), (contact.geom2, contact.geom1)):
                if b.model.geom_bodyid[payload] == held_body and b.model.geom_bodyid[other] not in self.robot_bodies:
                    # Only support surfaces below the payload; side obstacles remain forbidden.
                    if b.data.geom_xpos[other][2] < b.data.xpos[held_body][2]:
                        self.support_geoms.add(int(other))

    def capture_lift_clearance(self, safety_distance):
        """Capture existing tool/payload gaps, including penetration, for lift only."""
        self._lift_clearance_pairs = {}
        b = self.backend
        held = getattr(b.gripper_controller, 'held_name', None)
        if not held:
            return
        m, d = b.model, b.data
        held_root = getattr(b, 'object_ids', {}).get(held)
        payload_bodies = set()
        if held_root is not None:
            for body in range(1, m.nbody):
                ancestor = body
                while ancestor > 0:
                    if ancestor == int(held_root):
                        payload_bodies.add(body)
                        break
                    ancestor = int(m.body_parentid[ancestor])
        moving = self.tool_geoms | {g for g in self.geoms
                                    if int(m.geom_bodyid[g]) in payload_bodies}
        for tool in moving:
            for other in self.geoms:
                body = int(m.geom_bodyid[other])
                if other in moving or body in self.robot_bodies:
                    continue
                distance = float(mujoco.mj_geomDistance(
                    m, d, tool, other, max(.005, float(safety_distance) + .001), None))
                if distance <= max(0., float(safety_distance)):
                    self._lift_clearance_pairs[frozenset((tool, other))] = distance

    def clear_lift_clearance(self):
        self._lift_clearance_pairs = {}

    def _leaves_lift_clearance(self, a, z, distance):
        initial_gap = getattr(self, '_lift_clearance_pairs', {}).get(frozenset((int(a), int(z))))
        if initial_gap is None:
            return False
        # Keep a 1 micrometre numerical tolerance. A positive initial gap must
        # never become penetration; existing penetration may only stay or lessen.
        lower_bound = initial_gap - 1e-6 if initial_gap < 0 else max(0., initial_gap - 1e-6)
        return distance >= lower_bound

    def check(self, joints, *, safety_distance=0.0, allowed_geom_ids=None, gripper_width=None):
        if hasattr(self, 'backend'):
            held = getattr(self.backend.gripper_controller, 'held_name', None)
            if held != self._policy_held:
                self.baseline_allowed_pairs = set(self._arm_policy)
                if held:
                    payload_handles = [h for h, name in self._names.items() if name == held]
                    self.baseline_allowed_pairs.update(frozenset((a, z)) for a in payload_handles for z in self._robot_handles)
                self._policy_held = held
        # Include every mutable input that affects the articulated probe. This
        # makes reuse semantics-preserving across grasp/release and target
        # changes while eliminating identical repeated checks.
        b = getattr(self, 'backend', None)
        if b is None:
            return super().check(joints, safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        qpos = np.asarray(b.data.qpos, dtype=float)
        held = getattr(b.gripper_controller, 'held_name', None)
        query_key = (getattr(self, 'scene_revision', 0), qpos.tobytes(),
               np.asarray(joints, dtype=float).tobytes(), float(safety_distance),
               tuple(sorted(allowed_geom_ids)) if allowed_geom_ids is not None else None,
               held, getattr(b, 'contact_target', None),
               getattr(b, 'contact_target_object_id', None),
               getattr(self, '_physical_closure_target', None),
               self.allow_gripper_environment_contact,
               frozenset(getattr(self, '_lift_clearance_pairs', {}).items()),
               getattr(self, '_lift_ignored_payload_geoms', frozenset()),
               frozenset(self.support_geoms), gripper_width)
        cached = self._query_cache.get(query_key)
        if cached is not None:
            self._query_cache.move_to_end(query_key)
            return deepcopy(cached)
        base = super().check(joints, safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        if not hasattr(self, 'backend'):
            return base
        b, d, m = self.backend, self.probe, self.backend.model
        d.qpos[:] = b.data.qpos
        d.qvel[:] = 0
        d.qpos[b.qpos_indices] = joints
        if gripper_width is not None:
            from demo.interaction.table_clearance import set_static_gripper_width
            set_static_gripper_width(m, d, gripper_width)
        # Geometry queries need transforms only. Full forward dynamics builds
        # contact constraints for even deliberately infeasible candidate poses.
        mujoco.mj_kinematics(m, d)
        closure_target = getattr(self, '_physical_closure_target', None)
        terminal_grasp_region = self._at_grasp_goal(d) or closure_target is not None
        controller = b.gripper_controller
        held = getattr(controller, 'held_name', None)
        payload_body = b.object_ids[held] if held else None
        if held and m.jnt_type[m.body_jntadr[payload_body]] == mujoco.mjtJoint.mjJNT_FREE:
            t = np.eye(4); t[:3, :3] = d.xmat[controller.base_id].reshape(3, 3); t[:3, 3] = d.xpos[controller.base_id]
            # Forecast physical carrying from the measured relative pose; only the
            # separate collision probe is moved, never the live dynamic object.
            relative = controller._relative(payload_body) if controller.mode == 'physical' else controller.relative_pose
            t = t @ relative
            address = m.jnt_qposadr[m.body_jntadr[payload_body]]
            d.qpos[address:address + 3] = t[:3, 3]
            d.qpos[address + 3:address + 7] = Rotation.from_matrix(t[:3, :3]).as_quat()[[3, 0, 1, 2]]
            mujoco.mj_kinematics(m, d)
        dynamic_geoms = {g for g in self.geoms if self.body_names[m.geom_bodyid[g]] in self.dynamic_names}
        moving = self.tool_geoms | dynamic_geoms
        target = held or b.contact_target or closure_target
        target_text = str(target) if target else ''
        target_prefix = target_text.rsplit('_', 1)[0] if target_text else ''
        # The mechanism's fixed shell and moving link can intentionally share
        # contact/overlap in their collision proxies. Ignore only contacts
        # whose two reported bodies belong to this selected target model.
        def target_internal_contact(contact):
            if not target_prefix:
                return False
            names = (str(contact.get('geom1', '')), str(contact.get('geom2', '')))
            return all(n.startswith(target_prefix) for n in names)
        ignored_names = getattr(self, '_lift_ignored_payload_names', frozenset())
        base_contacts = tuple(c for c in base.contacts if not target_internal_contact(c)
                              and not ignored_names.intersection((c.get('geom1'), c.get('geom2'))))
        # Explicit contact targets (including pick's selected object) permit
        # all gripper/target contacts throughout planning and live execution.
        # Arm/target and every other environment pair remain checked below.
        allow_target_contact = bool(target)
        if target and allow_target_contact:
            # FCL reports the intended gripper/object contact separately
            # from the MuJoCo articulated probe; suppress only that exact
            # contact for the selected target.
            base_contacts = tuple(c for c in base_contacts
                                  if not (str(target) in {str(c.get('geom1', '')), str(c.get('geom2', ''))}
                                          and any(str(name).startswith('robotiq_2f85')
                                                  for name in (c.get('geom1', ''), c.get('geom2', '')))))
        target_object_id = ((getattr(b, 'contact_target_object_id', None) or closure_target)
                            if not held else None)
        target_bodies = set()
        if target_object_id is not None:
            for obj in b.resources.scene['objects']:
                if str(obj.get('object_id_in_scene')) == str(target_object_id):
                    root = b.object_ids.get(obj.get('object_name_in_scene'))
                    if root is not None:
                        target_bodies.add(int(root))
                        changed = True
                        while changed:
                            changed = False
                            for body in range(1, m.nbody):
                                if int(m.body_parentid[body]) in target_bodies and body not in target_bodies:
                                    target_bodies.add(body); changed = True
                    break
        pair_key = (held, target, target_object_id, frozenset(target_bodies),
                    frozenset(self.support_geoms), self.allow_gripper_environment_contact,
                    allow_target_contact, gripper_width is not None,
                    getattr(self, '_lift_ignored_payload_geoms', frozenset()))
        if getattr(self, '_pair_key', None) != pair_key:
            pairs, visited = [], set()
            for a in moving:
                for z in self.geoms:
                    pair = frozenset((a, z))
                    if a == z or pair in visited:
                        continue
                    visited.add(pair)
                    if a in getattr(self, '_lift_ignored_payload_geoms', ()) or z in getattr(self, '_lift_ignored_payload_geoms', ()):
                        continue
                    # A commanded fully closed shape can contain internal
                    # finger contact. This endpoint prefilter checks the tool
                    # against the environment, not its internal closing limit.
                    if gripper_width is not None and a in self.tool_geoms and z in self.tool_geoms:
                        continue
                    ba, bz = int(m.geom_bodyid[a]), int(m.geom_bodyid[z])
                    if ba not in self.robot_bodies and bz not in self.robot_bodies and payload_body not in (ba, bz):
                        continue
                    if self.allow_gripper_environment_contact and (
                            (a in self.tool_geoms and bz not in self.robot_bodies) or
                            (z in self.tool_geoms and ba not in self.robot_bodies)):
                        continue
                    rigid_pair = frozenset((int(m.body_weldid[ba]), int(m.body_weldid[bz])))
                    if ba == bz or (ba in self.robot_bodies and bz in self.robot_bodies and (
                            m.body_weldid[ba] == m.body_weldid[bz] or rigid_pair in self.adjacent or frozenset((ba, bz)) in self.excluded)):
                        continue
                    names = (self.body_names[ba], self.body_names[bz])
                    # Once an object is confirmed as held, the whole articulated
                    # gripper is allowed to remain in contact with that payload.
                    # Pick also allows the whole gripper to contact its selected
                    # object while approaching. Other scene contacts stay checked.
                    # A mechanism request names a specific articulated link,
                    # while MuJoCo contacts may report either that link or its
                    # mechanism root body. Treat both forms as the same target
                    # for the narrow retreat-contact allowance.
                    target_text = str(target) if target else ''
                    target_prefix = target_text.rsplit('_', 1)[0] if target_text else ''
                    target_body_match = bool(target_bodies) and (ba in target_bodies or bz in target_bodies)
                    target_internal_match = bool(target_bodies) and ba in target_bodies and bz in target_bodies
                    target_name_internal = bool(target_prefix) and all(
                        str(n).startswith(target_prefix) for n in names)
                    target_match = bool(target) and (
                        target_text in names or
                        # Link target -> mechanism root body, e.g.
                        # Drawer__Nightstand0061_drawer01 -> Drawer__Nightstand0061.
                        any(target_text.startswith(str(n) + '_') for n in names) or
                        # Root target -> one of its articulated links.
                        any(str(n).startswith(target_text + '_') for n in names)
                    )
                    if target_internal_match or target_name_internal or (allow_target_contact and
                            (target_body_match or target_match) and
                            any(n.startswith('robotiq_2f85') for n in names)):
                        continue
                    support = payload_body in (ba, bz) and (a in self.support_geoms or z in self.support_geoms)
                    pairs.append((a, z, support))
            self._pairs = np.asarray(pairs, dtype=int).reshape(-1, 3)
            self._pair_key = pair_key
        contacts = list(base_contacts)
        minimum = base.minimum_distance
        if len(base_contacts) != len(base.contacts) and minimum < 0:
            minimum = min((float(c.get('distance', float('inf'))) for c in base_contacts),
                          default=float('inf'))
        if len(self._pairs):
            aa, zz = self._pairs[:, 0], self._pairs[:, 1]
            # Conservative bounding spheres avoid expensive exact mesh-distance queries.
            gap = np.linalg.norm(d.geom_xpos[aa] - d.geom_xpos[zz], axis=1) - m.geom_rbound[aa] - m.geom_rbound[zz]
            for a, z, support in self._pairs[gap <= max(float(safety_distance), .005)]:
                ba, bz = int(m.geom_bodyid[a]), int(m.geom_bodyid[z])
                limit = -.001 if support else float(safety_distance)
                distance = float(mujoco.mj_geomDistance(m, d, int(a), int(z), max(.005, limit + .001), None))
                minimum = min(minimum, distance)
                if distance <= limit + 1e-9:
                    # Lift may retain an existing tool/payload penetration up
                    # to its captured depth. New pairs and deeper overlap fail.
                    if self._leaves_lift_clearance(a, z, distance):
                        continue
                    contacts.append({'geom1': self.body_names[ba], 'geom2': self.body_names[bz], 'distance': distance,
                                     'self_collision': ba in self.robot_bodies and bz in self.robot_bodies})
        result = CollisionResult(bool(contacts), minimum, tuple(contacts))
        self._query_cache[query_key] = result
        self._query_cache.move_to_end(query_key)
        while len(self._query_cache) > 2048:
            self._query_cache.popitem(last=False)
        return deepcopy(result)
