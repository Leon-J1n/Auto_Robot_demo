"""Fit labelled runtime geometry with a cache independent of grasp transforms."""
from __future__ import annotations

import json
from pathlib import Path

from .conversion.assets import fingerprint


def install_empty_self_pair_support():
    """Handle a valid empty policy set without upstream's diagnostic 0/0.

    The adapter disables the self cost only for this empty set. Sphere geometry
    stays loaded, so world checks remain available for fully welded assemblies.
    This runtime-only guard leaves every nonempty upstream computation intact.
    """
    import torch
    from curobo._src.robot.types.self_collision_params import SelfCollisionKinematicsCfg
    cls=SelfCollisionKinematicsCfg
    if getattr(cls, '_host_empty_pair_support', False):
        return
    original=cls.create_from_sphere_pair_distances
    def create(sphere_pair_distances,sphere_padding):
        if not bool(torch.isfinite(sphere_pair_distances).any()):
            return cls(num_spheres=sphere_padding.shape[0],sphere_padding=sphere_padding,
                collision_pairs=torch.empty((0,2),device=sphere_padding.device,dtype=torch.int16))
        return original(sphere_pair_distances,sphere_padding)
    cls.create_from_sphere_pair_distances=staticmethod(create)
    # The pinned graph-planner factory does not propagate self_collision_check
    # to its metrics config. An empty set must return differentiable zero there
    # as well, rather than launch an invalid zero-block native kernel.
    from curobo._src.cost.cost_self_collision import SelfCollisionCost
    import torch
    original_forward=SelfCollisionCost.forward
    def forward(self,robot_spheres):
        config=self.config.self_collision_kin_config
        base=(original_forward(self,robot_spheres) if config.collision_pairs.numel()
              else robot_spheres[...,0,:1]*0.)
        convex=getattr(config,'convex_self',None)
        if convex is None:
            return base
        penalty=convex.clearance_cost(robot_spheres)*self._weight
        if self.config.convert_to_binary:
            penalty=torch.where(penalty>0,penalty.clamp(max=1.)+1.,penalty)
        return torch.maximum(base,penalty)
    SelfCollisionCost.forward=forward
    cls._host_empty_pair_support=True


def build_runtime_robot(converted: dict, defaults: dict, cache: Path, compiled: dict):
    from curobo.robot_builder import RobotBuilder
    from curobo._src.geom.sphere_fit.types import SphereFitType
    import yaml

    install_empty_self_pair_support()
    settings = {'runtime_policy_schema': 3, 'official_commit': defaults['official_commit'],
                'sphere_density': defaults['sphere_density'], 'use_collision_mesh': True,
                'fit_type': defaults.get('runtime_sphere_fit_type', 'voxel'),
                'self_ignores': compiled['self_ignores'], 'margin': compiled['margin']}
    key = fingerprint(settings)[:16]
    path = Path(converted['robot_dir']) / f'robot_{key}.yml'
    if not path.exists():
        templates = cache / 'runtime_spheres'
        templates.mkdir(parents=True, exist_ok=True)
        builder = RobotBuilder(converted['urdf'], converted['robot_dir'], tool_frames=[converted['tool_frame']])
        spheres = {}
        for link, shape in converted['shape_keys'].items():
            if link in converted.get('primitive_spheres', {}):
                spheres[link] = converted['primitive_spheres'][link]
                continue
            template_key = fingerprint([shape, settings['sphere_density'], settings['official_commit'], settings['fit_type']])
            template = templates / f'{template_key}.json'
            if template.exists():
                fitted = json.loads(template.read_text('utf-8'))
            else:
                fitted = builder.refit_link_spheres(link, sphere_density=settings['sphere_density'],
                    use_collision_mesh=True, compute_metrics=False, fit_type=SphereFitType(settings['fit_type']))
                if not fitted:
                    raise ValueError(f'Runtime collision geometry produced no spheres: {link}')
                template.write_text(json.dumps(fitted), encoding='utf-8')
            spheres[link] = fitted
        config = builder.build()
        config.collision_spheres = spheres
        config.collision_link_names = list(spheres)
        config.mesh_link_names = list(spheres)
        # Never retain the builder's default-pose or neighbouring virtual-link exclusions.
        config.self_collision_ignore = compiled['self_ignores']
        config.self_collision_buffer = {name: compiled['margin'] / 2 for name in spheres}
        config.collision_sphere_buffer = 0.
        builder.save(config, str(path))
    model = yaml.safe_load(path.read_text('utf-8'))
    model['kinematics']['cspace']['default_joint_position'] = [
        converted['positions'][name] for name in model['kinematics']['cspace']['joint_names']]
    return model, str(path), settings
