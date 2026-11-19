"""Joint representations, branch deduplication and selection policies."""
from __future__ import annotations
import itertools
import math
import numpy as np


def equivalents(value, limit, period):
    if period <= 0:
        return np.array([value]) if limit[0]-1e-9 <= value <= limit[1]+1e-9 else np.array([])
    if not np.all(np.isfinite(limit)):
        raise ValueError("Periodic coordinates require finite limits")
    first = math.ceil((limit[0]-value-1e-9) / period)
    last = math.floor((limit[1]-value+1e-9) / period)
    return value + period * np.arange(first, last+1)


def normalize(joints, reference, limits, periods):
    value = np.asarray(joints, dtype=float).copy()
    for index in range(len(value)):
        valid = equivalents(value[index], limits[index], periods[index])
        if len(valid):
            value[index] = valid[np.argmin(np.abs(valid-reference[index]))]
    return value


def append_distinct(solutions, candidate, periods, tolerance=1e-3):
    for existing in solutions:
        delta = np.asarray(candidate) - existing
        periodic = periods > 0
        delta[periodic] = (delta[periodic]+periods[periodic]/2) % periods[periodic]-periods[periodic]/2
        if np.linalg.norm(delta) < tolerance:
            return
    solutions.append(np.asarray(candidate, dtype=float).copy())


def equivalent_configurations(joints, reference, limits, periods, max_variants=8):
    choices = [equivalents(q, limit, period) for q, limit, period in zip(joints, limits, periods)]
    variants = [np.asarray(items, dtype=float) for items in itertools.product(*choices)]
    variants.sort(key=lambda q: float(np.linalg.norm(q-reference)))
    return variants[:max_variants]
