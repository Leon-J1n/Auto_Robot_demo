"""Continuous interpolation of a Cartesian line's IK samples, with FK validation."""

import numpy as np

from .timing import parameterize
from .trajectory import _derivatives, _evaluate


def natural_controls(path):
    """Natural cubic through uniformly spaced Cartesian IK samples, as Beziers."""
    count = len(path)
    second = np.zeros_like(path)
    if count > 2:
        matrix = 4*np.eye(count-2) + np.eye(count-2, k=1) + np.eye(count-2, k=-1)
        second[1:-1] = np.linalg.solve(matrix, 6*np.diff(path, n=2, axis=0))
    delta = np.diff(path, axis=0)
    start_velocity = delta - (2*second[:-1]+second[1:])/6
    end_velocity = delta + (second[:-1]+2*second[1:])/6
    controls = np.stack((path[:-1], path[:-1]+start_velocity/3,
                         path[1:]-end_velocity/3, path[1:]), axis=1)
    # Exact degree elevation to reuse the existing derivative/Bezier evaluator.
    for degree in (3, 4):
        elevated = np.empty((count-1, degree+2, path.shape[1]))
        elevated[:, 0], elevated[:, -1] = controls[:, 0], controls[:, -1]
        for i in range(1, degree+1):
            alpha = i/(degree+1)
            elevated[:, i] = alpha*controls[:, i-1] + (1-alpha)*controls[:, i]
        controls = elevated
    return controls


def retime_linear(path, kinematics, valid, request, limits, period, method):
    """Only accept smoothing inside the requested Cartesian tolerances."""
    if len(path) < 2:
        raise ValueError("Linear smoothing needs at least two IK samples")
    poses = [kinematics.forward(path[0]), kinematics.forward(path[-1])]
    start, end = [np.asarray(p["position"]) for p in poses]
    rotation = poses[-1]["rotation_matrix"]
    constraints = request.get("constraints", {})
    position_tolerance = float(constraints.get("position_tolerance", .005))
    orientation_tolerance = min(float(constraints.get("orientation_tolerance", 2)), .25)
    hold_orientation = bool(constraints.get("keep_end_effector_orientation", False))
    controls = natural_controls(path)
    deriv = _derivatives(controls)
    grid = []
    for index, polygon in enumerate(deriv[1]):
        count = max(16, int(np.ceil(np.max(np.abs(polygon))/.005)))
        grid.extend(index+np.linspace(0, 1, count+1)[:-1])
    grid.append(float(len(controls)))
    errors = [0.0, 0.0]

    def validate(coordinates, points):
        if not valid(points):
            raise ValueError("Continuous linear path failed collision/joint-limit validation")
        for s, q in zip(coordinates, points):
            pose = kinematics.forward(q)
            position_error = float(np.linalg.norm(pose["position"] - (start+(end-start)*s/len(controls))))
            relative = rotation @ pose["rotation_matrix"].T
            angle = float(np.degrees(np.arccos(np.clip((np.trace(relative)-1)/2, -1, 1))))
            errors[0], errors[1] = max(errors[0], position_error), max(errors[1], angle)
            orientation_violation = hold_orientation and angle > orientation_tolerance+1e-8
            if position_error > position_tolerance+1e-10 or orientation_violation:
                raise ValueError(f"Continuous linear path exceeds Cartesian tolerance: {position_error:.6g} m, {angle:.6g} deg")

    grid = np.asarray(grid)
    validate(grid, _evaluate(controls, grid))
    velocity, acceleration, jerk = limits
    q1, q2, q3 = [np.maximum(np.max(np.abs(d), axis=(0, 1)), 1e-12) for d in deriv[1:]]
    sv = float(min(np.min(velocity/q1), np.min(np.sqrt(acceleration/(2*q2))), np.min(np.cbrt(jerk/(3*q3)))))
    sa = float(min(np.min(acceleration/(2*q1)), np.min(jerk/(9*q2*sv))))
    sj = float(np.min(jerk/(3*q1)))
    t, s, sd, sdd, sddd, backend = parameterize(len(controls), sv, sa, sj, period, method)
    s = np.clip(s, 0, len(controls))
    q, d1, d2, d3 = [_evaluate(d, s) for d in deriv]
    v = d1*sd[:, None]
    a = d2*sd[:, None]**2 + d1*sdd[:, None]
    j = d3*sd[:, None]**3 + 3*d2*sd[:, None]*sdd[:, None] + d1*sddd[:, None]
    q[0], q[-1] = path[0], path[-1]
    v[[0, -1]], a[[0, -1]] = 0, 0
    validate(s, q)
    return (t, q, v, a, j, backend), {
        "maximum_line_error_m": errors[0], "maximum_orientation_error_deg": errors[1],
        "position_tolerance_m": position_tolerance, "orientation_tolerance_deg": orientation_tolerance,
        "orientation_constraint_enabled": hold_orientation,
    }
