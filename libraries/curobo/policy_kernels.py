# SPDX-FileCopyrightText: Copyright (c) 2023-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
"""Host-policy variants of upstream cuRobo collision kernels (pinned implementation).

This module provides type-generic collision kernels that work with any obstacle
type via Warp function overloading. The kernels use `typing.Any` for the obstacle
set parameter, and Warp dispatches to the correct overloaded functions based on
the struct type at runtime.

Key features:
- Single kernel for all obstacle types (cuboid, mesh, voxel)
- 2D parallelization: threads = num_spheres × max_n_obstacles
- Sparse atomic accumulation: only non-zero costs write to output
- Optimized frame transforms: load once, accumulate in local frame, transform once

Performance optimization:
- Frame transform is loaded once per (sphere, obstacle) pair
- Gradients are accumulated in obstacle local frame
- Single gradient-to-world transform at the end
- Reduces quaternion operations by ~N for N sweep queries

Kernels:
- swept_sphere_obstacle_collision_kernel: Swept collision (trajectory interpolation)
"""

# Standard Library
# =============================================================================
# Automatic SDF Function Overload Registration
# =============================================================================
#
# Registers SDF function overloads from all obstacle data modules.
# Module list is centralized in curobo._src.geom.data.registry.OBSTACLE_SDF_MODULES.
#
# How it works:
# - wp.func uses scope_locals.get(func.func.__name__) to find existing Functions
# - By keeping variables named "is_obs_enabled" and "compute_sdf_with_grad" in scope,
#   subsequent wp.func calls detect the existing Function and add overloads
from importlib import import_module
from typing import Any

# Third Party
import warp as wp

from .policy import GPU_DISTANCE_EPS_M

from curobo._src.geom.collision.wp_collision_common import (
    accumulate_collision,
    apply_collision_activation,
    load_sphere_query,
)
from curobo._src.geom.data.registry import OBSTACLE_SDF_MODULES

# Initialize to None - will be set by first iteration
is_obs_enabled = None
load_obstacle_transform = None
compute_local_sdf = None
compute_local_sdf_with_grad = None


for _module_path in OBSTACLE_SDF_MODULES:
    _data_module = import_module(_module_path)
    _obs_fn = getattr(_data_module, "is_obs_enabled")
    _transform_fn = getattr(_data_module, "load_obstacle_transform")
    _sdf_only_fn = getattr(_data_module, "compute_local_sdf")
    _sdf_fn = getattr(_data_module, "compute_local_sdf_with_grad")

    # Register with wp.func - adds overloads on subsequent iterations
    is_obs_enabled = wp.func(_obs_fn, module=__name__)
    load_obstacle_transform = wp.func(_transform_fn, module=__name__)
    compute_local_sdf = wp.func(_sdf_only_fn, module=__name__)
    compute_local_sdf_with_grad = wp.func(_sdf_fn, module=__name__)

del _module_path, _data_module, _obs_fn, _transform_fn, _sdf_fn


SWEEP_STEPS = wp.constant(3)
DISTANCE_EPS = wp.constant(GPU_DISTANCE_EPS_M)


@wp.func
def outside_bounds(lower: wp.vec3, upper: wp.vec3, bounds: wp.array2d(dtype=wp.float32),
                   index: wp.int32, radius: wp.float32) -> wp.bool:
    """Conservative AABB rejection, including the entire swept centre segment."""
    dx = wp.max(wp.max(bounds[index, 0] - upper[0], lower[0] - bounds[index, 3]), 0.0)
    dy = wp.max(wp.max(bounds[index, 1] - upper[1], lower[1] - bounds[index, 4]), 0.0)
    dz = wp.max(wp.max(bounds[index, 2] - upper[2], lower[2] - bounds[index, 5]), 0.0)
    return dx * dx + dy * dy + dz * dz > wp.max(radius, 0.0) * wp.max(radius, 0.0)


# =============================================================================
# Generic Swept-Sphere Collision Kernel
# =============================================================================


@wp.kernel(enable_backward=False)
def update_policy_world_bounds(
    obs_set: Any,
    local_bounds: wp.array2d(dtype=wp.float32),
    world_bounds: wp.array2d(dtype=wp.float32),
    world_lower: wp.array(dtype=wp.vec3),
    world_upper: wp.array(dtype=wp.vec3),
):
    """Enclose each oriented local AABB once, rather than rotate every pair."""
    index = wp.tid()
    if not is_obs_enabled(obs_set, 0, index):
        world_lower[index] = wp.vec3(0.0, 0.0, 0.0)
        world_upper[index] = wp.vec3(0.0, 0.0, 0.0)
        return
    forward = wp.transform_inverse(load_obstacle_transform(obs_set, 0, index))
    lower = wp.vec3(local_bounds[index, 0], local_bounds[index, 1], local_bounds[index, 2])
    upper = wp.vec3(local_bounds[index, 3], local_bounds[index, 4], local_bounds[index, 5])
    center = wp.transform_point(forward, (lower + upper) * 0.5)
    half = (upper - lower) * 0.5
    extent = (wp.abs(wp.transform_vector(forward, wp.vec3(half[0], 0.0, 0.0)))
              + wp.abs(wp.transform_vector(forward, wp.vec3(0.0, half[1], 0.0)))
              + wp.abs(wp.transform_vector(forward, wp.vec3(0.0, 0.0, half[2]))))
    # The world box is a rejection aid only. Round outward so the extra
    # float32 frame conversion cannot discard a narrow-phase boundary pair.
    padding = wp.float32(1.e-5) * (1.0 + wp.length(center) + wp.length(extent))
    minimum = center - extent - wp.vec3(padding, padding, padding)
    maximum = center + extent + wp.vec3(padding, padding, padding)
    world_lower[index] = minimum
    world_upper[index] = maximum
    for axis in range(3):
        world_bounds[index, axis] = minimum[axis]
        world_bounds[index, axis + 3] = maximum[axis]


@wp.kernel(enable_backward=False)
def compact_policy_candidates(
    obs_set: Any,
    spheres: wp.array(dtype=wp.vec4),
    activation_distance: wp.array(dtype=wp.float32),
    env_query_idx: wp.array(dtype=wp.int32),
    pair_limits: wp.array2d(dtype=wp.float32),
    obstacle_bounds: wp.array2d(dtype=wp.float32),
    candidates: wp.array(dtype=wp.int32),
    candidate_count: wp.array(dtype=wp.int32),
    horizon: wp.int32,
    num_spheres: wp.int32,
    max_n_obs: wp.int32,
    use_multi_env: wp.uint8,
    swept: wp.uint8,
):
    """Conservative broad phase with an exact dense overflow fallback.

    Keep all potential contacts, including the complete previous/next sweep
    segments. No CPU readback, candidate truncation or collision approximation
    is introduced. Separating this kernel avoids paying mesh-kernel register
    pressure for the overwhelmingly common rejected pairs.
    """
    tid = wp.tid()
    sphere = tid / max_n_obs
    obstacle = tid - sphere * max_n_obs
    env = wp.int32(0)
    if use_multi_env == wp.uint8(1):
        env = env_query_idx[sphere / (horizon * num_spheres)]
    if not is_obs_enabled(obs_set, env, obstacle):
        return
    limit = pair_limits[sphere % num_spheres, obstacle]
    if limit < -1.0e10:
        return
    query = load_sphere_query(spheres, sphere, activation_distance[0])
    if query.radius < 0.0:
        return
    lower = query.center
    upper = lower
    if swept == wp.uint8(1):
        step = (sphere / num_spheres) % horizon
        if step > 0:
            previous = spheres[sphere - num_spheres]
            point = wp.vec3(previous[0], previous[1], previous[2])
            lower = wp.min(lower, point)
            upper = wp.max(upper, point)
        if step < horizon - 1:
            following = spheres[sphere + num_spheres]
            point = wp.vec3(following[0], following[1], following[2])
            lower = wp.min(lower, point)
            upper = wp.max(upper, point)
    if outside_bounds(lower, upper, obstacle_bounds, obstacle, query.radius_adjusted + limit):
        return
    slot = wp.atomic_add(candidate_count, 0, 1)
    if slot < candidates.shape[0]:
        candidates[slot] = tid


@wp.kernel(enable_backward=False)
def compact_policy_bvh_candidates(
    obs_set: Any,
    bvh_id: wp.uint64,
    spheres: wp.array(dtype=wp.vec4),
    activation_distance: wp.array(dtype=wp.float32),
    pair_limits: wp.array2d(dtype=wp.float32),
    max_limits: wp.array(dtype=wp.float32),
    obstacle_bounds: wp.array2d(dtype=wp.float32),
    candidates: wp.array(dtype=wp.int32),
    candidate_count: wp.array(dtype=wp.int32),
    horizon: wp.int32,
    num_spheres: wp.int32,
    max_n_obs: wp.int32,
    swept: wp.uint8,
):
    """Visit spatial neighbours per sphere instead of every world obstacle."""
    sphere = wp.tid()
    query = load_sphere_query(spheres, sphere, activation_distance[0])
    if query.radius < 0.0:
        return
    lower = query.center
    upper = lower
    if swept == wp.uint8(1):
        step = (sphere / num_spheres) % horizon
        if step > 0:
            value = spheres[sphere - num_spheres]
            point = wp.vec3(value[0], value[1], value[2])
            lower = wp.min(lower, point)
            upper = wp.max(upper, point)
        if step < horizon - 1:
            value = spheres[sphere + num_spheres]
            point = wp.vec3(value[0], value[1], value[2])
            lower = wp.min(lower, point)
            upper = wp.max(upper, point)
    radius = wp.max(0.0, query.radius_adjusted + max_limits[sphere % num_spheres])
    extent = wp.vec3(radius, radius, radius)
    neighbours = wp.bvh_query_aabb(bvh_id, lower - extent, upper + extent)
    obstacle = wp.int32(0)
    while wp.bvh_query_next(neighbours, obstacle):
        if not is_obs_enabled(obs_set, 0, obstacle):
            continue
        limit = pair_limits[sphere % num_spheres, obstacle]
        if limit < -1.0e10:
            continue
        if outside_bounds(lower, upper, obstacle_bounds, obstacle, query.radius_adjusted + limit):
            continue
        slot = wp.atomic_add(candidate_count, 0, 1)
        if slot < candidates.shape[0]:
            candidates[slot] = sphere * max_n_obs + obstacle


@wp.kernel
def policy_sphere_kernel(
    # Obstacle set (generic - uses Warp function overloading)
    obs_set: Any,
    # Sphere data
    spheres: wp.array(dtype=wp.vec4),  # [batch * horizon * num_spheres]
    # Parameters
    weight: wp.array(dtype=wp.float32),
    activation_distance: wp.array(dtype=wp.float32),
    env_query_idx: wp.array(dtype=wp.int32),
    pair_limits: wp.array2d(dtype=wp.float32),
    obstacle_bounds: wp.array2d(dtype=wp.float32),
    candidates: wp.array(dtype=wp.int32),
    candidate_count: wp.array(dtype=wp.int32),
    use_compact: wp.uint8,
    query_stride: wp.int32,
    # Outputs (pre-zeroed)
    distance: wp.array(dtype=wp.float32),  # [batch * horizon * num_spheres]
    gradient: wp.array(dtype=wp.float32),  # [batch * horizon * num_spheres * 4]
    # Dimensions
    batch_size: wp.int32,
    horizon: wp.int32,
    num_spheres: wp.int32,
    max_n_obs: wp.int32,
    use_multi_env: wp.uint8,
):
    """Compute sphere-obstacle collision with 2D parallelization.

    This is a type-generic kernel that works with any obstacle type via
    Warp function overloading. The correct `compute_sdf_with_grad` and
    `is_obs_enabled` functions are selected based on the struct type of `obs_set`.

    Thread layout: tid = sph_flat_idx * max_n_obs + obs_local_idx

    Args:
        obs_set: Obstacle set (CuboidDataWarp, MeshDataWarp, or VoxelDataWarp).
        spheres: Sphere positions and radii.
        weight: Collision cost weight.
        activation_distance: Distance threshold for collision activation.
        env_query_idx: Environment index per batch element.
        distance: Output distance buffer (pre-zeroed).
        gradient: Output gradient buffer (pre-zeroed).
        batch_size: Number of batch elements.
        horizon: Trajectory horizon.
        num_spheres: Number of spheres per timestep.
        max_n_obs: Maximum obstacles per environment.
        use_multi_env: Whether to use batch-specific environments.
    """
    total = batch_size * horizon * num_spheres * max_n_obs
    compact = use_compact == wp.uint8(1) and candidate_count[0] <= candidates.shape[0]
    if compact:
        total = candidate_count[0]
    for slot in range(wp.tid(), total, query_stride):
        tid = slot
        if compact:
            tid = candidates[slot]

        # 2D decode: (sphere, obstacle)
        sph_flat_idx = tid / max_n_obs
        obs_local_idx = tid - sph_flat_idx * max_n_obs

        total_spheres = batch_size * horizon * num_spheres
        if sph_flat_idx >= total_spheres:
            continue

        # Decode batch index and get environment
        b_idx = sph_flat_idx / (horizon * num_spheres)
        env_idx = wp.int32(0)
        if use_multi_env == wp.uint8(1):
            env_idx = env_query_idx[b_idx]

        # Check if obstacle exists and is enabled
        if not is_obs_enabled(obs_set, env_idx, obs_local_idx):
            continue

        limit = pair_limits[sph_flat_idx % num_spheres, obs_local_idx]
        if limit < -1.0e10:
            continue

        # Load parameters
        eta = activation_distance[0]
        w = weight[0]

        # Load sphere
        query = load_sphere_query(spheres, sph_flat_idx, eta)
        if query.radius < 0.0:
            continue
        radius_adjusted = query.radius_adjusted + limit
        query_distance = wp.max(query.radius_adjusted, query.radius + wp.abs(limit) + eta)

        # Load obstacle transform
        inv_t = load_obstacle_transform(obs_set, env_idx, obs_local_idx)

        # Transform sphere center to local frame
        local_pt = wp.transform_point(inv_t, query.center)
        if outside_bounds(local_pt, local_pt, obstacle_bounds, obs_local_idx, radius_adjusted):
            continue

        # Compute local SDF and gradient
        local_result = compute_local_sdf_with_grad(
            obs_set, env_idx, obs_local_idx, local_pt, query_distance
        )

        # Only compute forward transform and accumulate when in collision
        penetration = -local_result[0] + radius_adjusted
        if penetration > DISTANCE_EPS:
            fwd_t = wp.transform_inverse(inv_t)
            grad_local = wp.vec3(local_result[1], local_result[2], local_result[3])
            grad_world = wp.transform_vector(fwd_t, grad_local)
            activation_result = apply_collision_activation(penetration, eta)
            accumulate_collision(
                sph_flat_idx,
                w * activation_result[0],
                w * activation_result[1] * grad_world,
                distance,
                gradient,
            )


@wp.kernel(enable_backward=False)
def policy_swept_kernel(
    # Obstacle set (generic - uses Warp function overloading)
    obs_set: Any,
    # Sphere data
    spheres: wp.array(dtype=wp.vec4),  # [batch * horizon * num_spheres]
    # Parameters
    weight: wp.array(dtype=wp.float32),
    activation_distance: wp.array(dtype=wp.float32),
    env_query_idx: wp.array(dtype=wp.int32),
    pair_limits: wp.array2d(dtype=wp.float32),
    obstacle_bounds: wp.array2d(dtype=wp.float32),
    candidates: wp.array(dtype=wp.int32),
    candidate_count: wp.array(dtype=wp.int32),
    use_compact: wp.uint8,
    query_stride: wp.int32,
    # Outputs (pre-zeroed)
    distance: wp.array(dtype=wp.float32),  # [batch * horizon * num_spheres]
    gradient: wp.array(dtype=wp.float32),  # [batch * horizon * num_spheres * 4]
    # Dimensions
    batch_size: wp.int32,
    horizon: wp.int32,
    num_spheres: wp.int32,
    max_n_obs: wp.int32,
    use_multi_env: wp.uint8,
):
    """Compute swept-sphere collision with 2D parallelization.

    Sweeps toward previous and next timesteps in the trajectory.
    Uses adaptive stepping based on penetration distance.
    The number of sweep steps is fixed at compile time via SWEEP_STEPS constant.

    Thread layout: tid = sph_flat_idx * max_n_obs + obs_local_idx

    Args:
        obs_set: Obstacle set (CuboidDataWarp, MeshDataWarp, or VoxelDataWarp).
        spheres: Sphere positions and radii for all timesteps.
        weight: Collision cost weight.
        activation_distance: Distance threshold for collision activation.
        env_query_idx: Environment index per batch element.
        distance: Output distance buffer (pre-zeroed).
        gradient: Output gradient buffer (pre-zeroed).
        batch_size: Number of batch elements.
        horizon: Trajectory horizon.
        num_spheres: Number of spheres per timestep.
        max_n_obs: Maximum obstacles per environment.
        use_multi_env: Whether to use batch-specific environments.
    """
    total = batch_size * horizon * num_spheres * max_n_obs
    compact = use_compact == wp.uint8(1) and candidate_count[0] <= candidates.shape[0]
    if compact:
        total = candidate_count[0]
    for slot in range(wp.tid(), total, query_stride):
        tid = slot
        if compact:
            tid = candidates[slot]

        # 2D decode: (sphere, obstacle)
        sph_flat_idx = tid / max_n_obs
        obs_local_idx = tid - sph_flat_idx * max_n_obs

        total_spheres = batch_size * horizon * num_spheres
        if sph_flat_idx >= total_spheres:
            continue



        # Decode batch/horizon indices
        b_idx = sph_flat_idx / (horizon * num_spheres)
        h_idx = (sph_flat_idx - b_idx * horizon * num_spheres) / num_spheres

        # Get environment index
        env_idx = wp.int32(0)
        if use_multi_env == wp.uint8(1):
            env_idx = env_query_idx[b_idx]


        # Check if obstacle exists and is enabled
        if not is_obs_enabled(obs_set, env_idx, obs_local_idx):
            continue

        # Copy struct from param space to local stack to prevent nvcc
        # from generating cvta.to.local on the param pointer
        obs = obs_set

        limit = pair_limits[sph_flat_idx % num_spheres, obs_local_idx]
        if limit < -1.0e10:
            continue

        # Load parameters
        eta = activation_distance[0]
        w = weight[0]

        # Load sphere - extract radius_adjusted as scalar so query struct can die early
        query = load_sphere_query(spheres, sph_flat_idx, eta)
        if query.radius < 0.0:
            continue
        radius_adjusted = query.radius_adjusted + limit
        query_distance = wp.max(query.radius_adjusted, query.radius + wp.abs(limit) + eta)

        # Load obstacle transform ONCE for all sweep queries
        inv_t = load_obstacle_transform(obs, env_idx, obs_local_idx)

        # Accumulators in local frame (apply weight and transform at end)
        cost_sum = wp.float32(0.0)
        grad_sum_local = wp.vec3(0.0, 0.0, 0.0)

        # Transform current sphere center to local frame.
        # After this, query.center is no longer needed - sweep distances
        # are computed in local frame (rigid transforms preserve distances).
        local_current = wp.transform_point(inv_t, query.center)
        lower = local_current
        upper = local_current
        if h_idx > 0:
            previous = spheres[sph_flat_idx - num_spheres]
            point = wp.transform_point(inv_t, wp.vec3(previous[0], previous[1], previous[2]))
            lower = wp.min(lower, point)
            upper = wp.max(upper, point)
        if h_idx < horizon - 1:
            following = spheres[sph_flat_idx + num_spheres]
            point = wp.transform_point(inv_t, wp.vec3(following[0], following[1], following[2]))
            lower = wp.min(lower, point)
            upper = wp.max(upper, point)
        if outside_bounds(lower, upper, obstacle_bounds, obs_local_idx, radius_adjusted):
            continue

        # Process current timestep
        sdf_result = compute_local_sdf_with_grad(
            obs, env_idx, obs_local_idx, local_current, query_distance
        )
        penetration = -sdf_result[0] + radius_adjusted

        if penetration > DISTANCE_EPS:
            activation_result = apply_collision_activation(penetration, eta)
            cost_sum += activation_result[0]
            grad_sum_local += activation_result[1] * wp.vec3(sdf_result[1], sdf_result[2], sdf_result[3])

        # Sweep toward h-1 (previous timestep)
        # Interpolation and distance are computed in local frame to:
        # 1. Avoid per-step quaternion rotations (lerp in local space)
        # 2. Free query.center registers (rigid transforms preserve distances)
        if h_idx > 0 and SWEEP_STEPS > 0:
            prev_sphere = spheres[sph_flat_idx - num_spheres]
            local_prev = wp.transform_point(
                inv_t, wp.vec3(prev_sphere[0], prev_sphere[1], prev_sphere[2])
            )
            half_dist = wp.length(local_prev - local_current) * 0.5
            inv_half_dist = 1.0 / wp.max(half_dist, 0.001)
            jump = wp.float32(0.0)
            for _ in range(SWEEP_STEPS):
                if jump >= half_dist:
                    break
                t = 1.0 - 0.5 * jump * inv_half_dist
                local_pt = t * local_current + (1.0 - t) * local_prev

                # Use one SDF overload in the sweep loops to avoid invalid Warp
                # codegen for VoxelDataWarp when both overloads are mixed here.
                sdf_result = compute_local_sdf_with_grad(
                    obs, env_idx, obs_local_idx, local_pt, query_distance
                )
                penetration = -sdf_result[0] + radius_adjusted

                if penetration > DISTANCE_EPS:
                    activation_result = apply_collision_activation(penetration, eta)
                    cost_sum += activation_result[0]
                    grad_sum_local += activation_result[1] * wp.vec3(sdf_result[1], sdf_result[2], sdf_result[3])
                    jump += penetration
                else:
                    if -penetration >= 1000.0:
                        jump += wp.max(query.radius, 0.001)
                    else:
                        jump += wp.max(-penetration, radius_adjusted)

        # Sweep toward h+1 (next timestep)
        if h_idx < horizon - 1 and SWEEP_STEPS > 0:
            next_sphere = spheres[sph_flat_idx + num_spheres]
            local_next = wp.transform_point(
                inv_t, wp.vec3(next_sphere[0], next_sphere[1], next_sphere[2])
            )
            half_dist = wp.length(local_next - local_current) * 0.5
            inv_half_dist = 1.0 / wp.max(half_dist, 0.001)
            jump = wp.float32(0.0)
            for _ in range(SWEEP_STEPS):
                if jump >= half_dist:
                    break
                t = 1.0 - 0.5 * jump * inv_half_dist
                local_pt = t * local_current + (1.0 - t) * local_next

                # See previous sweep loop for why this uses the with-grad overload.
                sdf_result = compute_local_sdf_with_grad(
                    obs, env_idx, obs_local_idx, local_pt, query_distance
                )
                penetration = -sdf_result[0] + radius_adjusted

                if penetration > DISTANCE_EPS:
                    activation_result = apply_collision_activation(penetration, eta)
                    cost_sum += activation_result[0]
                    grad_sum_local += activation_result[1] * wp.vec3(sdf_result[1], sdf_result[2], sdf_result[3])
                    jump += penetration
                else:
                    if -penetration >= 1000.0:
                        jump += wp.max(query.radius, 0.001)
                    else:
                        jump += wp.max(-penetration, radius_adjusted)

        # Transform accumulated gradient to world frame ONCE and apply weight
        if cost_sum > 0.0:
            fwd_t = wp.transform_inverse(inv_t)
            grad_world = wp.transform_vector(fwd_t, grad_sum_local)
            accumulate_collision(sph_flat_idx, w * cost_sum, w * grad_world, distance, gradient)
