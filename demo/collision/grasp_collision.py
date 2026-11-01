"""FCL arm checks plus current articulated fingers and carried payload geometry."""
import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from collision.simplatform_fcl import SimPlatformFCLCollisionChecker, CollisionResult
from execution.resources import component


class GraspCollisionChecker(SimPlatformFCLCollisionChecker):
    def on_scene_sync(self, state, *, changed_objects=(), revision=0):
        # The MuJoCo probe reads the live backend qpos on every query. Keep the
        # revision for diagnostics so planners can confirm which scene snapshot
        # was used without rebuilding unchanged FCL entities.
        self.scene_revision = int(revision)
        self.scene_changed_objects = tuple(changed_objects)

    def bind_backend(self, backend):
        self.backend = backend
        self.dynamic_names = {o['object_name_in_scene'] for o in backend.resources.scene['objects']
                              if component(o, 'RigidBody').get('dynamic')
                              or bool(component(o, 'ArticulatedModel'))}
        self.cache_enabled = False  # Finger aperture and payload transform are part of every query.
        self.carrying_safety_distance = 0.0
        self.carrying_contact_log = {}
        self.allowed_proxy_target_names = set()
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
        if any(h in getattr(self, 'dynamic_handles', set()) for h in handles):
            return True
        if any(self._collider_links.get(h, '').startswith('robotiq_2f85') for h in handles):
            return True
        controller = getattr(getattr(self, 'backend', None), 'gripper_controller', None)
        held = getattr(controller, 'held_name', None)
        if held and any(self._names.get(h) == held for h in handles):
            return True  # Payload/arm checks are evaluated at the candidate payload pose below.
        return super()._is_allowed(pair, allowed)

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

    def check(self, joints, *, safety_distance=0.0, allowed_geom_ids=None):
        if hasattr(self, 'backend'):
            held = getattr(self.backend.gripper_controller, 'held_name', None)
            if held != self._policy_held:
                self.baseline_allowed_pairs = set(self._arm_policy)
                if held:
                    payload_handles = [h for h, name in self._names.items() if name == held]
                    self.baseline_allowed_pairs.update(frozenset((a, z)) for a in payload_handles for z in self._robot_handles)
                self._policy_held = held
        base = super().check(joints, safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids)
        if not hasattr(self, 'backend'):
            return base
        b, d, m = self.backend, self.probe, self.backend.model
        d.qpos[:] = b.data.qpos
        d.qvel[:] = 0
        d.qpos[b.qpos_indices] = joints
        mujoco.mj_forward(m, d)
        controller = b.gripper_controller
        held = getattr(controller, 'held_name', None)
        payload_body = b.object_ids[held] if held else None
        if held:
            t = np.eye(4); t[:3, :3] = d.xmat[controller.base_id].reshape(3, 3); t[:3, 3] = d.xpos[controller.base_id]
            # Forecast physical carrying from the measured relative pose; only the
            # separate collision probe is moved, never the live dynamic object.
            relative = controller._relative(payload_body) if controller.mode == 'physical' else controller.relative_pose
            t = t @ relative
            address = m.jnt_qposadr[m.body_jntadr[payload_body]]
            d.qpos[address:address + 3] = t[:3, 3]
            d.qpos[address + 3:address + 7] = Rotation.from_matrix(t[:3, :3]).as_quat()[[3, 0, 1, 2]]
            mujoco.mj_forward(m, d)
        dynamic_geoms = {g for g in self.geoms if self.body_names[m.geom_bodyid[g]] in self.dynamic_names}
        moving = self.tool_geoms | dynamic_geoms
        target = held or b.contact_target
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
        base_contacts = tuple(c for c in base.contacts if not target_internal_contact(c))
        target_object_id = getattr(b, 'contact_target_object_id', None) if not held else None
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
        key = (held, target, target_object_id, frozenset(target_bodies), frozenset(self.support_geoms))
        if getattr(self, '_pair_key', None) != key:
            pairs, visited = [], set()
            for a in moving:
                for z in self.geoms:
                    pair = frozenset((a, z))
                    if a == z or pair in visited:
                        continue
                    visited.add(pair)
                    ba, bz = int(m.geom_bodyid[a]), int(m.geom_bodyid[z])
                    if ba not in self.robot_bodies and bz not in self.robot_bodies and payload_body not in (ba, bz):
                        continue
                    rigid_pair = frozenset((int(m.body_weldid[ba]), int(m.body_weldid[bz])))
                    if ba == bz or (ba in self.robot_bodies and bz in self.robot_bodies and (
                            m.body_weldid[ba] == m.body_weldid[bz] or rigid_pair in self.adjacent or frozenset((ba, bz)) in self.excluded)):
                        continue
                    names = (self.body_names[ba], self.body_names[bz])
                    # Once an object is confirmed as held, the whole articulated
                    # gripper is allowed to remain in contact with that payload.
                    # Before grasping, target contacts are still checked normally;
                    # contacts with every other scene object remain forbidden.
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
                    if target_internal_match or target_name_internal or ((target_body_match or target_match) and
                                                 any(n.startswith('robotiq_2f85') for n in names)):
                        continue
                    support = payload_body in (ba, bz) and (a in self.support_geoms or z in self.support_geoms)
                    pairs.append((a, z, support))
            self._pairs = np.asarray(pairs, dtype=int).reshape(-1, 3)
            self._pair_key = key
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
                    contacts.append({'geom1': self.body_names[ba], 'geom2': self.body_names[bz], 'distance': distance,
                                     'self_collision': ba in self.robot_bodies and bz in self.robot_bodies})
        return CollisionResult(bool(contacts), minimum, tuple(contacts))
