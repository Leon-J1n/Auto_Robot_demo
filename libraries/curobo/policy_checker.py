"""GPU policy queries shared by IK, graph search and trajectory optimization."""
from __future__ import annotations

import torch
import warp as wp

from curobo._src.geom.collision.checker_collision import CollisionChecker
from curobo._src.geom.collision.wp_speed_metric import apply_speed_metric
from curobo._src.util.warp import get_warp_device_stream

from .policy import world_limits
from .policy_kernels import (
    compact_policy_candidates, compact_policy_bvh_candidates, update_policy_world_bounds,
    policy_sphere_kernel, policy_swept_kernel,
)

# Bound flattened Warp indices and individual GPU launches. Keep whole horizons
# together so swept queries never lose a neighbouring trajectory sample.
MAX_QUERY_PAIRS = 1 << 25
MAX_COMPACT_CANDIDATES = 1 << 20


class _PolicyQuery(torch.autograd.Function):
    @staticmethod
    def forward(ctx, spheres, buffer, scene, weight, eta, env_idx, multi_env,
                tables, swept, dt, speed_metric, return_loss):
        batch, horizon, count, _ = spheres.shape
        buffer.zero_()
        device, stream = get_warp_device_stream(spheres)
        tables, bounds, world_bounds, spatial, max_limits, workspaces, compact = tables
        query = wp.from_torch(spheres.detach().view(-1, 4), dtype=wp.vec4)
        distance = wp.from_torch(buffer.distance.detach().view(-1))
        gradient = wp.from_torch(buffer.gradient.detach().view(-1), dtype=wp.float32)
        for data in scene.get_valid_data():
            limits = tables[type(data).__name__]
            pairs_per_batch = horizon * count * data.max_n
            if pairs_per_batch >= (1 << 31):
                raise ValueError('One trajectory exceeds the policy collision kernel index limit')
            chunk_size = max(1, MAX_QUERY_PAIRS // max(1, pairs_per_batch))
            if compact and data.max_n >= 32:
                # BVH visits scale with spheres, not the Cartesian pair set.
                # Keep chunks only for int32 index safety. A full queue falls
                # back to exact dense evaluation entirely on the GPU.
                chunk_size = max(1, ((1 << 31) - 1) // max(1, pairs_per_batch))
            capacity = min(min(batch, chunk_size) * pairs_per_batch, MAX_COMPACT_CANDIDATES)
            # Power-of-two buckets reuse stable GPU storage across query shapes.
            # Retain smaller buckets as well: existing captured graphs may use them.
            bucket = 1 << max(0, (capacity - 1).bit_length())
            workspace_key = (stream.cuda_stream, bucket)
            if workspace_key not in workspaces:
                workspaces[workspace_key] = (
                    torch.empty(bucket, device=spheres.device, dtype=torch.int32),
                    torch.zeros(1, device=spheres.device, dtype=torch.int32))
            candidates, counter = workspaces[workspace_key]
            candidate_array, count_array = wp.from_torch(candidates), wp.from_torch(counter)
            obstacle_data = data.to_warp()
            weight_array, eta_array = wp.from_torch(weight), wp.from_torch(eta)
            limit_array = wp.from_torch(limits)
            bound_array = wp.from_torch(bounds[type(data).__name__])
            world_bound_array = wp.from_torch(world_bounds[type(data).__name__])
            if compact:
                lower, upper, bvh = spatial[type(data).__name__]
            for start in range(0, batch, chunk_size):
                stop = min(batch, start + chunk_size)
                chunk_query = wp.from_torch(spheres.detach()[start:stop].reshape(-1, 4), dtype=wp.vec4)
                chunk_distance = wp.from_torch(buffer.distance.detach()[start:stop].reshape(-1))
                chunk_gradient = wp.from_torch(buffer.gradient.detach()[start:stop].reshape(-1), dtype=wp.float32)
                env_array = wp.from_torch(env_idx[start:stop].reshape(-1), dtype=wp.int32)
                if compact:
                    counter.zero_()
                    if data.max_n >= 32:
                        wp.launch(kernel=compact_policy_bvh_candidates,
                            dim=(stop - start) * horizon * count,
                            inputs=[obstacle_data, wp.uint64(bvh.id), chunk_query, eta_array, limit_array,
                                wp.from_torch(max_limits[type(data).__name__]), world_bound_array,
                                candidate_array, count_array, horizon, count, data.max_n, wp.uint8(swept)],
                            stream=stream, device=device)
                    else:
                        wp.launch(kernel=compact_policy_candidates,
                            dim=(stop - start) * pairs_per_batch,
                            inputs=[obstacle_data, chunk_query, eta_array, env_array, limit_array,
                                world_bound_array, candidate_array, count_array, horizon, count, data.max_n,
                                wp.uint8(multi_env), wp.uint8(swept)], stream=stream, device=device)
                # A bounded persistent grid consumes the device-side queue.
                # Do not schedule millions of expensive mesh-kernel threads
                # merely to discover that only a few candidates survived.
                narrow_threads = min((stop - start) * pairs_per_batch, 32768)
                wp.launch(kernel=policy_swept_kernel if swept else policy_sphere_kernel,
                    dim=narrow_threads,
                    inputs=[obstacle_data, chunk_query, weight_array, eta_array,
                        env_array, limit_array, bound_array, candidate_array, count_array, wp.uint8(compact), narrow_threads,
                        chunk_distance, chunk_gradient, stop - start, horizon, count, data.max_n, wp.uint8(multi_env)],
                    stream=stream, device=device)
        if swept and speed_metric:
            wp.launch(kernel=apply_speed_metric, dim=batch * horizon * count,
                inputs=[query, distance, gradient, wp.from_torch(dt), batch, horizon, count],
                stream=stream, device=device)
        ctx.return_loss = return_loss
        ctx.save_for_backward(buffer.gradient)
        return buffer.distance

    @staticmethod
    def backward(ctx, grad_output):
        gradient = None
        if ctx.needs_input_grad[0]:
            gradient, = ctx.saved_tensors
            if ctx.return_loss:
                gradient = gradient * grad_output.unsqueeze(-1)
        return (gradient,) + (None,) * 11


class PolicyCollisionChecker(CollisionChecker):
    """Keep table storage stable across scene changes and CUDA graph replay.

    Call configure after each world/policy update, before any queries. The
    runtime worker does this for every request, after updating its world.
    """

    def configure(self, scene, kinematics, converted: dict, compiled: dict) -> None:
        if scene.num_envs != 1:
            raise ValueError('Host policy snapshots currently describe exactly one planning environment')
        params = kinematics.config.kinematics_config
        inverse = {index: name for name, index in params.link_name_to_idx_map.items()}
        sphere_geometries = [converted['geometry_links'][inverse[index]]
                             for index in params.link_sphere_idx_map.cpu().tolist()]
        tables = getattr(self, 'tables', {})
        bounds = getattr(self, 'bounds', {})
        world_bounds = getattr(self, 'world_bounds', {})
        spatial = getattr(self, 'spatial', {})
        max_limits = getattr(self, 'max_limits', {})
        for data in scene.data.get_valid_data():
            names = list(data.names[0]) + [None] * (data.max_n - len(data.names[0]))
            geoms = []
            for name in names:
                if name is not None and name not in converted['world_geoms']:
                    raise ValueError(f'Unmapped obstacle in policy collision scene: {name}')
                geoms.append(None if name is None else converted['world_geoms'][name])
            tensor = torch.as_tensor(world_limits(compiled, sphere_geometries, geoms),
                                     device=self.device_cfg.device, dtype=torch.float32)
            key = type(data).__name__
            if hasattr(data, 'wp_cache'):
                mesh_bounds = {item.mesh_id: torch.stack(item.get_bounds()).reshape(6)
                               for item in data.wp_cache.values()}
                rows = [mesh_bounds[int(ident)] if name is not None else
                        torch.zeros(6, device=self.device_cfg.device)
                        for ident, name in zip(data.mesh_ids[0].cpu().tolist(), names)]
                bound_tensor = torch.stack(rows).to(dtype=torch.float32)
            elif key == 'CuboidData':
                half = data.dims[0, :, :3] / 2
                bound_tensor = torch.stack((-half, half), dim=1).reshape(data.max_n, 6).contiguous()
            else:
                bound_tensor = torch.tensor([[-1.e20] * 3 + [1.e20] * 3],
                    device=self.device_cfg.device).repeat(data.max_n, 1)
            if key in tables:
                if tables[key].shape != tensor.shape:
                    raise ValueError('Policy GPU buffer shape changed; rebuild the planner before CUDA graph capture')
                tables[key].copy_(tensor)
                bounds[key].copy_(bound_tensor)
                max_limits[key].copy_(tensor.max(dim=1).values.clamp(min=0))
            else:
                tables[key] = tensor
                bounds[key] = bound_tensor
                world_bounds[key] = torch.empty_like(bound_tensor)
                max_limits[key] = tensor.max(dim=1).values.clamp(min=0)
                lower = torch.zeros((data.max_n, 3), device=tensor.device, dtype=torch.float32)
                upper = torch.zeros_like(lower)
                spatial[key] = (lower, upper, None)
            lower, upper, bvh = spatial[key]
            device, stream = get_warp_device_stream(tensor)
            wp.launch(kernel=update_policy_world_bounds, dim=data.max_n,
                inputs=[data.to_warp(), wp.from_torch(bounds[key]), wp.from_torch(world_bounds[key]),
                    wp.from_torch(lower, dtype=wp.vec3), wp.from_torch(upper, dtype=wp.vec3)],
                stream=stream, device=device)
            if data.max_n >= 32:
                with wp.ScopedStream(stream):
                    if bvh is None:
                        bvh = wp.Bvh(wp.from_torch(lower, dtype=wp.vec3), wp.from_torch(upper, dtype=wp.vec3))
                        spatial[key] = (lower, upper, bvh)
                    else:
                        bvh.refit()
        self.tables = tables
        self.bounds = bounds
        self.world_bounds = world_bounds
        self.spatial = spatial
        self.max_limits = max_limits
        if not hasattr(self, 'query_workspaces'):
            self.query_workspaces = {}
            self.env_indices = {}

    def _query(self, scene, query_sphere, collision_buffer, weight, activation_distance,
               env_query_idx=None, return_loss=False, *, swept=False, dt=None, speed_metric=False):
        multi = env_query_idx is not None
        if not multi:
            batch = query_sphere.shape[0]
            if batch not in self.env_indices:
                self.env_indices[batch] = torch.zeros(batch, device=query_sphere.device, dtype=torch.int32)
            env_query_idx = self.env_indices[batch]
        return _PolicyQuery.apply(query_sphere, collision_buffer, scene, weight, activation_distance,
            env_query_idx, multi, (self.tables, self.bounds, self.world_bounds,
                self.spatial, self.max_limits, self.query_workspaces,
                getattr(self, 'compact_queries', True)), swept, dt, speed_metric, return_loss)

    def get_sphere_distance(self, scene, query_sphere, collision_buffer, weight, activation_distance,
                            env_query_idx=None, return_loss=False):
        return self._query(scene, query_sphere, collision_buffer, weight, activation_distance,
                           env_query_idx, return_loss)

    def get_swept_sphere_distance(self, scene, query_sphere, collision_buffer, weight,
            activation_distance, trajectory_dt, enable_speed_metric=False,
            env_query_idx=None, return_loss=False):
        return self._query(scene, query_sphere, collision_buffer, weight, activation_distance,
            env_query_idx, return_loss, swept=True, dt=trajectory_dt, speed_metric=enable_speed_metric)
