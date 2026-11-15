"""Run-scoped target knowledge; measured state always wins over model metadata."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation


def _matrix(pose):
    result = np.eye(4)
    result[:3, :3] = Rotation.from_quat(pose['quaternion_xyzw']).as_matrix()
    result[:3, 3] = pose['position']
    return result


class TargetBlackboard:
    def __init__(self):
        self.objects = {}
        self.mechanisms = {}
        self.gripper = {}
        self.simulation_time = None
        self.revision = 0
        self._models = {}
        self._targets = {}
        self._pending = {}
        self._anchors = {}
        self._visual_grasps = {}
        self._visual_places = {}
        self.model_hits = 0
        self.model_misses = 0

    def model(self, client, model_id):
        key = (client.base_url, getattr(client, 'uid', 0), int(model_id))
        if key not in self._models:
            # Failed requests are not cached.
            self._models[key] = deepcopy(client.get_model(model_id))
            self.model_misses += 1
        else:
            self.model_hits += 1
        return deepcopy(self._models[key])

    def register(self, target_id, name, model_id):
        self._targets[str(target_id)] = {'name': name, 'model_id': model_id}

    def register_scene(self, scene):
        """Register every scene instance without fetching OLS metadata."""
        for obj in scene.get('objects', ()):
            ref = next((c.get('properties', {}) for c in obj.get('components', ())
                        if c['component_type'] == 'MetadataRef'), {})
            model_id = None
            if ref.get('type') == 'model':
                model_id = ref.get('object_id')
                path = ref.get('path', '')
                if model_id is None and path.startswith('$LIBRARY_SERVER/'):
                    suffix = path.rsplit('/', 1)[-1]
                    if suffix.isdigit():
                        model_id = int(suffix)
            self.register(obj['object_id_in_scene'], obj['object_name_in_scene'], model_id)

    def observe(self, state):
        timestamp = state.get('simulation_time')
        if timestamp is not None and self.simulation_time is not None and timestamp < self.simulation_time:
            return  # Never replace a newer measurement with an old planning snapshot.
        self.objects.update(deepcopy(state.get('objects') or {}))
        self.mechanisms.update(deepcopy(state.get('mechanisms') or {}))
        if 'gripper' in state:
            self.gripper = deepcopy(state.get('gripper') or {})
        if timestamp is not None:
            self.simulation_time = timestamp
        self.revision += 1
        held = self.gripper.get('object_id')
        if self.gripper.get('holding') and held in self.objects:
            for name, selected in self._pending.items():
                if held != name and not held.startswith(name + '__'):
                    continue
                reference = selected['reference_poses'].get(held)
                if reference is None:
                    continue
                key = (name, selected['grasp_index'])
                # Preserve the grasp frame relative to the physically contacted
                # link, including after release. This never constrains physics.
                self._anchors[key] = {
                    'body': held,
                    'body_from_grasp': (np.linalg.inv(_matrix(reference)) @
                                        np.asarray(selected['world_from_grasp'])).tolist(),
                }

    def pose(self, name):
        return deepcopy(self.objects.get(name))

    def require_pose(self, name):
        pose = self.pose(name)
        if pose is None:
            raise ValueError(f'Target has no measured world pose: {name}')
        return pose

    def remember_selection(self, resolved):
        selected = resolved['selected']
        self._pending[resolved['target_name']] = {
            'grasp_index': selected['grasp_index'],
            'world_from_grasp': deepcopy(selected['world_from_grasp']),
            'reference_poses': deepcopy(self.objects),
        }

    def grasp_pose(self, name, index):
        anchor = self._anchors.get((name, index))
        if anchor is None or anchor['body'] not in self.objects:
            return None
        return _matrix(self.objects[anchor['body']]) @ np.asarray(anchor['body_from_grasp'])

    def clear_grasp_anchor(self, name, index=0):
        """Replace an old contact anchor after a newly validated visual refinement."""
        self._anchors.pop((name, index), None)

    def remember_visual_grasp(self, name, world_from_grasp, estimate, *, anchor_frame=None):
        anchor_frame = anchor_frame or name
        anchor_pose = self.require_pose(anchor_frame)
        self._visual_grasps[name] = {
            'anchor_frame': anchor_frame,
            'anchor_from_pose': (np.linalg.inv(_matrix(anchor_pose)) @ np.asarray(world_from_grasp)).tolist(),
            'estimate': deepcopy(estimate),
        }

    def visual_grasp(self, name):
        cached = self._visual_grasps.get(name)
        if cached is None:
            return None
        anchor_frame = cached.get('anchor_frame', name)
        return _matrix(self.require_pose(anchor_frame)) @ np.asarray(cached.get(
            'anchor_from_pose', cached.get('object_from_grasp')))

    def visual_grasp_evidence(self, name):
        cached = self._visual_grasps.get(name)
        return deepcopy(cached['estimate']) if cached else None

    def remember_visual_place(self, name, world_from_grasp, estimate, *, anchor_frame=None):
        anchor_frame = anchor_frame or name
        anchor_pose = self.require_pose(anchor_frame)
        self._visual_places[name] = {
            'anchor_frame': anchor_frame,
            'anchor_from_pose': (np.linalg.inv(_matrix(anchor_pose)) @ np.asarray(world_from_grasp)).tolist(),
            'estimate': deepcopy(estimate),
        }

    def visual_place(self, name):
        cached = self._visual_places.get(name)
        if cached is None:
            return None
        anchor_frame = cached.get('anchor_frame', name)
        return _matrix(self.require_pose(anchor_frame)) @ np.asarray(cached.get(
            'anchor_from_pose', cached.get('object_from_grasp')))

    def visual_place_evidence(self, name):
        cached = self._visual_places.get(name)
        return deepcopy(cached['estimate']) if cached else None

    def snapshot(self):
        return deepcopy({
            'revision': self.revision, 'simulation_time': self.simulation_time,
            'targets': self._targets, 'objects': self.objects,
            'mechanisms': self.mechanisms, 'gripper': self.gripper,
            'model_cache': {'entries': len(self._models), 'hits': self.model_hits, 'misses': self.model_misses},
            'interaction_anchors': [dict(target=name, grasp_index=index, **anchor)
                                    for (name, index), anchor in self._anchors.items()],
            'visual_grasps': self._visual_grasps, 'visual_places': self._visual_places,
        })
