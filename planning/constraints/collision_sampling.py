"""Configurable discrete collision sampling, independent of execution rate."""
from collections.abc import Mapping
import numpy as np


def collision_sampling_settings(request):
    config = request.get('planning', {}).get('collision_validation', {})
    if not isinstance(config, Mapping):
        raise ValueError('planning.collision_validation must be an object')
    from execution.config import planning_settings
    resolution = config.get(
        'resolution_rad', planning_settings()['collision_validation']['resolution_rad'])
    subdivisions = config.get('spline_subdivisions', 2)
    if (isinstance(resolution, bool) or not isinstance(resolution, (int, float))
            or not np.isfinite(resolution) or resolution <= 0):
        raise ValueError('collision_validation.resolution_rad must be finite and positive')
    if (isinstance(subdivisions, bool) or not isinstance(subdivisions, int)
            or subdivisions < 1):
        raise ValueError('collision_validation.spline_subdivisions must be a positive integer')
    return float(resolution), subdivisions


def rrt_resolution(request):
    """Resolve the RRT edge sampling resolution for the current request."""
    config = request.get('planning', {}).get('collision_free', {})
    if not isinstance(config, Mapping):
        raise ValueError('planning.collision_free must be an object')
    from execution.config import planning_settings
    value = config.get('rrt_resolution_rad', planning_settings()['collision_free']['rrt_resolution_rad'])
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not np.isfinite(value) or value <= 0):
        raise ValueError('collision_free.rrt_resolution_rad must be finite and positive')
    return float(value)


def spline_collision_samples(profile, resolution, subdivisions):
    """Keep knots and bound each progress interval by exact derivative bounds.

    This is discrete sampling, not continuous collision detection. Local slope
    bounds avoid applying a single high-curvature interval's density everywhere.
    """
    knots = profile.spline.x
    widths = np.diff(knots)
    counts = np.maximum(subdivisions, np.ceil(
        np.max(profile.slopes, axis=1) * widths / resolution).astype(int))
    progress = np.concatenate([np.linspace(a, b, int(count) + 1)[:-1]
                               for a, b, count in zip(knots[:-1], knots[1:], counts)]
                              + [knots[-1:]])
    return profile.spline(progress)
