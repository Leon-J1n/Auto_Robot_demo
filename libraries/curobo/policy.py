"""Compile host geometry-pair permissions without importing GPU dependencies."""
from __future__ import annotations

from itertools import combinations

import numpy as np


IGNORE_DISTANCE = -1.e20
# Float32 FK/SDF arithmetic can report nanometre-scale positive penetration at
# an exact clearance boundary. This comparison tolerance does not change the
# requested margin.
GPU_DISTANCE_EPS_M = 1.e-7


def compile_policy(converted: dict) -> dict:
    """Resolve geometry identity once; never interpret backend hand-off filters."""
    source = converted['collision_policy']
    allowed = {tuple(sorted(pair)) for pair in source['allowed_pairs']}
    overrides = {tuple(sorted((int(a), int(b)))): float(limit)
                 for a, b, limit in source['distance_overrides']}
    links = converted['geometry_links']
    ignores = {name: [] for name in links}
    for first, second in combinations(links, 2):
        pair = tuple(sorted((links[first], links[second])))
        if pair in allowed:
            ignores[first].append(second)
            ignores[second].append(first)
        elif pair in overrides:
            # Current support and lift-clearance exceptions are robot/world pairs.
            # Fail rather than silently approximate a future self-pair threshold.
            raise ValueError(f'Per-pair self distance requires a self-collision kernel extension: {pair}')
    return {'allowed': allowed, 'overrides': overrides, 'self_ignores': ignores,
            'margin': float(source['safety_distance']),
            'fingerprint': source['fingerprint']}


def world_limits(compiled: dict, sphere_geometries: list[int], obstacle_geometries: list[int | None]) -> np.ndarray:
    """Build [sphere, obstacle-slot] thresholds; padded slots stay disabled."""
    result = np.full((len(sphere_geometries), len(obstacle_geometries)), IGNORE_DISTANCE, dtype=np.float32)
    for row, first in enumerate(sphere_geometries):
        for column, second in enumerate(obstacle_geometries):
            if second is None:
                continue
            pair = tuple(sorted((first, second)))
            if pair not in compiled['allowed']:
                result[row, column] = compiled['overrides'].get(pair, compiled['margin'])
    return result
