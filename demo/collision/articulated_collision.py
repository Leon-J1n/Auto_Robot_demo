"""Keep native FCL for ordinary geometry, use live articulated links for mechanisms."""
from copy import deepcopy
import numpy as np
from demo.collision.grasp_collision import GraspCollisionChecker
from execution.resources import component


class ArticulatedCollisionChecker(GraspCollisionChecker):
    supports_articulated = True

    def __init__(self, **kwargs):
        scene = deepcopy(kwargs['scene_data'])
        self.mechanism_roots = {o['object_name_in_scene'] for o in scene['objects'] if component(o, 'ArticulatedModel')}
        scene['objects'] = [o for o in scene['objects'] if o['object_name_in_scene'] not in self.mechanism_roots]
        kwargs['scene_data'] = scene
        super().__init__(**kwargs)

    def set_entity_transform(self, name, **pose):
        if any(name == root or name.startswith(root+'__') for root in self.mechanism_roots):
            return
        return super().set_entity_transform(name, **pose)

    def bind_backend(self, backend):
        super().bind_backend(backend)
        self.mechanism_geoms = {g for g in self.geoms if any(self.body_names[backend.model.geom_bodyid[g]].startswith(root+'__') for root in self.mechanism_roots)}
        self.arm_geoms = {g for g in self.geoms if backend.model.geom_bodyid[g] in self.robot_bodies and g not in self.tool_geoms}
        # Topology is fixed for this backend; positions and bounds remain live.
        # Preserve pair order so contact diagnostics match the scalar checker.
        self._arm_mechanism_pairs = np.asarray(
            [(a, z) for a in self.arm_geoms for z in self.mechanism_geoms],
            dtype=np.intp).reshape(-1, 2)


