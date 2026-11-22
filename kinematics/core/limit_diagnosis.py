"""Bounded, read-only IK failure diagnostics; candidates never enter execution."""
import time

import numpy as np
from scipy.optimize import least_squares

from .geometry import parse_pose, rotation_error
from .policies import normalize


def diagnose_joint_limits(service, target, seed, *, timeout=1.5):
    started = time.perf_counter()
    deadline = started + timeout
    seed = np.asarray(seed, dtype=float)
    limits = service.limits
    position, rotation = parse_pose(target, service.forward(seed)['rotation_matrix'])
    tolerances = service.ik_validation
    rng = np.random.default_rng(7)
    result = dict(status='inconclusive', diagnostic_only=True, joints=[])

    class BudgetExpired(Exception):
        pass

    def residual(q):
        if time.perf_counter() >= deadline:
            raise BudgetExpired()
        pose = service.forward(q)
        return np.r_[pose['position'] - position,
                     .35 * rotation_error(rotation, pose['rotation_matrix'])]

    def valid(q):
        pose = service.forward(q)
        pe = float(np.linalg.norm(pose['position'] - position))
        ae = float(np.linalg.norm(rotation_error(rotation, pose['rotation_matrix'])))
        return pe <= tolerances['position_tolerance_m'] and ae <= tolerances['orientation_tolerance_rad'], pe, ae

    # Search within the actual limits first. Finding one valid candidate is
    # evidence against attributing the original failure to joint limits.
    try:
        for initial in (seed, *rng.uniform(limits[:, 0], limits[:, 1], (2, len(seed)))):
            fit = least_squares(residual, np.clip(initial, limits[:, 0], limits[:, 1]),
                                bounds=(limits[:, 0], limits[:, 1]), max_nfev=150,
                                ftol=1.e-9, xtol=1.e-9, gtol=1.e-9)
            ok, pe, ae = valid(fit.x)
            if ok:
                result.update(status='within_limits_candidate', position_error_m=pe,
                              orientation_error_rad=ae)
                return result
        # Enlarge only revolute/continuous coordinates; fixed translations or
        # prismatic ranges must not be interpreted as angles.
        periods = service.joint_periods
        angular = periods > 0
        # URDF adapters may use zero period for bounded revolute joints.
        parameters = getattr(service.robot, 'chain_parameters', {})
        if parameters and len(seed) == 6:
            angular = np.ones(len(seed), dtype=bool)  # This adapter is revolute-only.
        if not np.any(angular):
            return result
        low, high = limits[:, 0].copy(), limits[:, 1].copy()
        low[angular] = np.minimum(low[angular], seed[angular] - 2 * np.pi)
        high[angular] = np.maximum(high[angular], seed[angular] + 2 * np.pi)
        candidates = []
        for initial in (seed, *rng.uniform(low, high, (3, len(seed)))):
            fit = least_squares(residual, initial, bounds=(low, high), max_nfev=250,
                                ftol=1.e-10, xtol=1.e-10, gtol=1.e-10)
            ok, pe, ae = valid(fit.x)
            if not ok:
                continue
            q = normalize(fit.x, seed, np.c_[low, high], periods)
            # Prefer an equivalent winding inside the actual limits when one
            # exists; a +/-2pi representation alone is not a limit violation.
            for i in np.flatnonzero(periods > 0):
                shift_low = int(np.ceil((limits[i, 0] - q[i]) / periods[i]))
                shift_high = int(np.floor((limits[i, 1] - q[i]) / periods[i]))
                if shift_low <= shift_high:
                    shift = int(np.clip(round((seed[i] - q[i]) / periods[i]), shift_low, shift_high))
                    q[i] += shift * periods[i]
            ok, pe, ae = valid(q)
            if not ok:
                continue
            excess = np.maximum(limits[:, 0] - q, q - limits[:, 1]).clip(0)
            if not np.any(excess > 1.e-6):
                result.update(status='within_limits_candidate')
                return result
            candidates.append((float(np.linalg.norm(excess)), q, pe, ae))
            break
        if candidates:
            _, q, pe, ae = min(candidates, key=lambda item: item[0])
            names = getattr(service.robot, 'joint_names', [f'joint{i+1}' for i in range(len(seed))])
            result.update(status='limit_violating_candidate', position_error_m=pe,
                orientation_error_rad=ae, joints=[dict(joint=names[i], value=float(q[i]),
                    lower=float(limits[i, 0]), upper=float(limits[i, 1]))
                    for i in range(len(seed)) if q[i] < limits[i, 0]-1.e-6 or q[i] > limits[i, 1]+1.e-6])
    except BudgetExpired:
        result['budget_exhausted'] = True
    finally:
        result['elapsed_s'] = time.perf_counter() - started
    return result
