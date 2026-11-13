"""Bounded, lazy ordering hints; never discard an IK candidate."""
from time import perf_counter

import numpy as np


def ordered_distances(executor, distances, transform_for, *, preferred=None):
    distances = list(distances)
    if preferred is not None:
        match = next((d for d in distances if abs(d - preferred) < 1e-8), None)
        if match is not None:
            distances.remove(match)
            distances.insert(0, match)
    precheck = getattr(getattr(executor, 'kinematics', None), 'quick_reachability', None)
    if not callable(precheck):
        yield from distances
        return
    if not hasattr(executor, 'pose_search_precheck'):
        executor.pose_search_precheck = []
    executor.sync.sync(executor.backend, executor.kinematics, executor.checker)
    # Lazy evaluation means a successful first candidate pays for one check.
    # Bound total precheck work as well as each individual solver request.
    deferred = []
    remaining = .15
    for distance in distances:
        if remaining <= 0:
            yield distance
            continue
        try:
            transform = np.asarray(transform_for(distance), dtype=float)
        except ValueError:
            # The regular attempt owns invalid camera-alignment diagnostics.
            yield distance
            continue
        started = perf_counter()
        reachable = precheck({'position': transform[:3, 3].tolist(),
                             'rotation_matrix': transform[:3, :3].tolist()},
                            timeout=min(.03, remaining))
        elapsed = perf_counter() - started
        remaining -= elapsed
        executor.pose_search_precheck.append({
            'distance_m': float(distance), 'elapsed_s': elapsed,
            'status': 'analytic_solution' if reachable is True else
                      'deferred' if reachable is False else 'unavailable',
        })
        if reachable is False:
            deferred.append(distance)
        else:
            yield distance
    # Full numerical IK remains available if analytically promising candidates
    # failed collision/continuity checks. No candidates are pruned.
    yield from deferred
