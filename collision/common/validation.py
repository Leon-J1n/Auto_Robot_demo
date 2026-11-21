"""Shared validation configuration for collision backends."""
from __future__ import annotations

from execution.config import planning_settings


DEFAULT_RESOLUTION_RAD = float(planning_settings()['collision_validation']['resolution_rad'])
DEFAULT_SAFETY_DISTANCE = 0.0


def validate_joint_vector(values, *, expected=None):
    if expected is not None and len(values) != expected:
        raise ValueError(f"Collision check requires {expected} joint values")
    if not len(values):
        raise ValueError("Collision check requires at least one joint value")
    return tuple(float(value) for value in values)
