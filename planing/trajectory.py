"""Bounded quintic Bezier smoothing followed by scalar Ruckig retiming."""

from math import comb
from time import perf_counter

import numpy as np

from .timing import parameterize


def _positive(value, name):
    value = float(value)
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _limits(value, dof, name):
    result = np.broadcast_to(np.asarray(value, dtype=float), (dof,)).copy()
    if not np.all(np.isfinite(result)) or np.any(result <= 0):
        raise ValueError(f"{name} must contain positive finite joint limits")
    return result


def _simplify(path):
    """Remove duplicates and only exactly collinear, forward intermediate points."""
    points = [path[0]]
    for point in path[1:]:
        if np.max(np.abs(point - points[-1])) <= 1e-12:
            continue
        while len(points) >= 2:
            a, b = points[-2:]
            direction = point - a
            denominator = direction @ direction
            if denominator <= 1e-24:
                break
            fraction = (b - a) @ direction / denominator
            if 0 <= fraction <= 1 and np.max(np.abs(b - a - fraction * direction)) <= 1e-10:
                points.pop()
            else:
                break
        points.append(point)
    return np.asarray(points)


def _controls(path, blend):
    if len(path) == 2:
        # A straight path needs no geometric easing; Ruckig supplies the ramps.
        return (path[0] + np.linspace(0, 1, 6)[:, None]*(path[1]-path[0]))[None, :, :]
    tangent = np.zeros_like(path)
    tangent[0], tangent[-1] = path[1]-path[0], path[-1]-path[-2]
    if len(path) > 2:
        before, after = np.diff(path, axis=0)[:-1], np.diff(path, axis=0)[1:]
        # Shared knot derivative; no tangent for a reversing joint.
        tangent[1:-1] = blend * np.where(before * after > 0,
            np.sign(before) * np.minimum(np.abs(before), np.abs(after)), 0.0)
    # Quintic Bezier: prescribed endpoint position/velocity, zero acceleration.
    return np.stack((path[:-1], path[:-1] + tangent[:-1]/5,
                     path[:-1] + 2*tangent[:-1]/5,
                     path[1:] - 2*tangent[1:]/5,
                     path[1:] - tangent[1:]/5, path[1:]), axis=1)


def _derivatives(controls):
    result = [controls]
    for degree in (5, 4, 3):
        result.append(degree * np.diff(result[-1], axis=1))
    return result


def _evaluate(controls, coordinates):
    index = np.minimum(np.floor(coordinates).astype(int), len(controls)-1)
    u = np.clip(coordinates - index, 0, 1)
    degree = controls.shape[1]-1
    weights = np.array([comb(degree, i)*u**i*(1-u)**(degree-i) for i in range(degree+1)]).T
    return np.einsum("ni,nij->nj", weights, controls[index])


def _retime_polyline(path, velocity, acceleration, jerk, period, method):
    chunks = []
    elapsed = 0.0
    for index, (start, end) in enumerate(zip(path[:-1], path[1:])):
        delta = end-start
        magnitude = np.maximum(np.abs(delta), 1e-12)
        local_t, s, sd, sdd, sddd, backend = parameterize(
            1.0, float(np.min(velocity/magnitude)),
            float(np.min(acceleration/magnitude)), float(np.min(jerk/magnitude)), period, method)
        keep = slice(None) if index == 0 else slice(1, None)
        chunks.append((local_t[keep]+elapsed, start+s[keep, None]*delta,
                       sd[keep, None]*delta, sdd[keep, None]*delta, sddd[keep, None]*delta))
        elapsed += float(local_t[-1])
    return (*[np.concatenate([c[i] for c in chunks]) for i in range(5)], backend)


def _retime_polyline_continuous(path, velocity, acceleration, jerk, period, method):
    """Retime a polyline with one scalar profile shared by all segments.

    Unlike ``_retime_polyline``, this does not restart the profile at every
    waypoint.  It is intended for linear-motion fallbacks where stopping at
    every dense IK sample produces visible stutter.  Corners remain exactly
    on the validated polyline; callers should validate the resulting samples.
    """
    path = np.asarray(path, dtype=float)
    deltas = np.diff(path, axis=0)
    if len(deltas) == 0:
        zeros = np.zeros_like(path)
        return np.array([0.0]), path[:1], zeros[:1], zeros[:1], zeros[:1], "stationary"
    magnitude = np.maximum(np.abs(deltas), 1e-12)
    scalar_velocity = float(np.min(velocity / magnitude))
    scalar_acceleration = float(np.min(acceleration / magnitude))
    scalar_jerk = float(np.min(jerk / magnitude))
    distance = float(len(deltas))
    times, s, sd, sdd, sddd, backend = parameterize(
        distance, scalar_velocity, scalar_acceleration, scalar_jerk, period, method
    )
    segment = np.minimum(np.floor(s).astype(int), len(deltas) - 1)
    fraction = np.clip(s - segment, 0.0, 1.0)
    positions = path[segment] + fraction[:, None] * deltas[segment]
    velocities = deltas[segment] * sd[:, None]
    accelerations = deltas[segment] * sdd[:, None]
    jerks = deltas[segment] * sddd[:, None]
    positions[0], positions[-1] = path[0], path[-1]
    velocities[[0, -1]] = 0.0
    accelerations[[0, -1]] = 0.0
    return times, positions, velocities, accelerations, jerks, backend


class TrajectoryProcessor:
    """Process planner output; no live robot mutation, no implicit path relaxation."""

    def __init__(self, *, collision_checker=None, joint_limits=None, config=None, kinematics=None):
        self.checker = collision_checker
        self.joint_limits = None if joint_limits is None else np.asarray(joint_limits, dtype=float)
        self.config = dict(config or {})
        self.kinematics = kinematics

    def process(self, trajectory, request):
        started = perf_counter()
        config = dict(self.config)
        override = request.get("strategy_params", {}).get("trajectory", {})
        if not isinstance(override, dict):
            raise ValueError("strategy_params.trajectory must be an object")
        config.update(override)
        path = np.asarray(trajectory["waypoints"], dtype=float)
        if path.ndim != 2 or len(path) < 1 or path.shape[1] == 0 or not np.all(np.isfinite(path)):
            raise ValueError("Invalid trajectory waypoints")
        if self.joint_limits is not None and self.joint_limits.shape != (path.shape[1], 2):
            raise ValueError("Joint limit dimensions differ from trajectory")
        constraints = request.get("constraints", {})
        avoid_collision = constraints.get("avoid_collision", True)
        safety_distance = float(constraints.get("safety_distance", 0.0))
        if avoid_collision and self.checker is None:
            raise ValueError("Collision checker required for trajectory validation")
        def valid(points):
            if self.joint_limits is not None and (np.any(points < self.joint_limits[:, 0]) or np.any(points > self.joint_limits[:, 1])):
                return False
            return not avoid_collision or not self.checker.check_path(
                points, resolution=0.02, safety_distance=safety_distance,
                allowed_geom_ids=getattr(self.checker, "allowed_target_geom_ids", None)).in_collision

        original_count = len(path)
        path = _simplify(path)
        if not valid(np.vstack((path, path[-1]))):
            raise ValueError("Input trajectory failed collision/joint-limit validation")
        dof = path.shape[1]
        velocity = _limits(config.get("velocity_limits", 1.0), dof, "velocity_limits") * _positive(constraints.get("velocity_scale", 0.5), "velocity_scale")
        acceleration = _limits(config.get("acceleration_limits", 2.0), dof, "acceleration_limits") * _positive(constraints.get("acceleration_scale", 0.5), "acceleration_scale")
        jerk = _limits(config.get("jerk_limits", 10.0), dof, "jerk_limits") * _positive(config.get("jerk_scale", 1.0), "jerk_scale")
        period = _positive(config.get("sample_period", 0.01), "sample_period")
        if period < 0.001:
            raise ValueError("sample_period must be at least 0.001 seconds")
        free_path = (trajectory.get("path_type") == "joint"
                     and request.get("motion", {}).get("path_constraint", "soft") != "hard"
                     and not constraints.get("keep_end_effector_orientation", False))
        optimize = config.get("optimize", True) and free_path
        if optimize:
            # Deterministic bounded shortcuts, each checked against the live scene.
            rng = np.random.default_rng(17)
            for _ in range(24):
                if len(path) < 3:
                    break
                i, j = sorted(rng.choice(len(path), 2, replace=False))
                if j > i+1 and valid(path[[i, j]]):
                    path = np.concatenate((path[:i+1], path[j:]))

        linear_result = None
        linear_diagnostics = {}
        if (trajectory.get("path_type") == "linear" and self.kinematics is not None
                and config.get("smooth_linear", True) and len(path) > 1):
            from .linear import retime_linear
            # Use the original equally spaced Cartesian IK samples, before the
            # direct planner inserts nonuniform joint-space collision samples.
            source = np.asarray(trajectory.get("cartesian_joint_waypoints", trajectory["waypoints"]), dtype=float)
            try:
                linear_result, linear_diagnostics = retime_linear(
                    source, self.kinematics, valid, request,
                    (velocity, acceleration, jerk), period, config.get("timing_method", "auto"))
            except ValueError as exc:
                linear_diagnostics["linear_smoothing_fallback"] = str(exc)

        if linear_result is not None:
            times, positions, velocities, accelerations, jerks, backend = linear_result
            blend_used = 1.0
        elif len(path) == 1:
            times = np.array([0.0])
            positions = path
            velocities = accelerations = jerks = np.zeros_like(path)
            backend, blend_used = "stationary", 0.0
        elif not optimize or len(path) == 2:
            # Keep constrained Cartesian IK polylines exactly: each corner has
            # zero speed/acceleration. Retiming each edge avoids a single short
            # edge throttling the entire path's scalar speed.
            times, positions, velocities, accelerations, jerks, backend = _retime_polyline(
                path, velocity, acceleration, jerk, period, config.get("timing_method", "auto"))
            blend_used = 0.0
            if not valid(positions):
                raise ValueError("Timed constrained trajectory failed collision validation")
        else:
            # Bound sampling spacing by the Bezier derivative control polygon.
            # All connectors between validation samples are checked by MuJoCo.
            blend_used = 0.0
            for blend in ([1.0, 0.5, 0.0] if optimize else [0.0]):
                controls = _controls(path, blend)
                deriv = _derivatives(controls)
                grid = []
                for index, polygon in enumerate(deriv[1]):
                    count = max(8, int(np.ceil(np.max(np.abs(polygon))/0.01)))
                    grid.extend(index + np.linspace(0, 1, count+1)[:-1])
                grid.append(float(len(controls)))
                if valid(_evaluate(controls, np.asarray(grid))):
                    blend_used = blend
                    break
            else:
                # The geometric path was already validated; a rounded spline
                # may nevertheless cut a corner into an obstacle. Preserve the
                # safe polyline and retime it instead of failing the motion.
                retime = (_retime_polyline_continuous
                          if trajectory.get("path_type") == "linear"
                          else _retime_polyline)
                times, positions, velocities, accelerations, jerks, backend = retime(
                    path, velocity, acceleration, jerk, period, config.get("timing_method", "auto"))
                blend_used = 0.0
                linear_diagnostics["smoothing_fallback"] = "validated_polyline"
                if not valid(positions):
                    if trajectory.get("path_type") == "linear":
                        linear_diagnostics["continuous_retime_fallback"] = "collision_or_limit"
                    # Keep the planner's already validated waypoint sequence as
                    # a final conservative fallback.  Playback interpolates
                    # these points linearly, matching the collision model.
                    if not valid(path):
                        raise ValueError("Timed trajectory failed collision/joint-limit validation")
                    segment_time = np.max(np.abs(np.diff(path, axis=0)), axis=1) / np.maximum(velocity, 0.01).max()
                    times = np.concatenate(([0.0], np.cumsum(np.maximum(segment_time, period))))
                    positions = path.copy()
                    velocities = accelerations = jerks = np.zeros_like(positions)
                    backend = "validated_waypoint_fallback"
                output = dict(trajectory)
                output.update(waypoints=positions.tolist(), time_from_start=times.tolist(),
                              velocities=velocities.tolist(), accelerations=accelerations.tolist(),
                              jerks=jerks.tolist(), duration=float(times[-1]))
                output["postprocessing"] = {
                    "optimizer": "preserve_path",
                    **linear_diagnostics,
                    "timing_method": backend, "blend": blend_used,
                    "input_points": original_count, "path_points": len(path),
                    "sample_count": len(times), "processing_time": perf_counter()-started,
                    "velocity_limits": velocity.tolist(), "acceleration_limits": acceleration.tolist(),
                    "jerk_limits": jerk.tolist(),
                    "maximum_velocity": np.max(np.abs(velocities), axis=0).tolist(),
                    "maximum_acceleration": np.max(np.abs(accelerations), axis=0).tolist(),
                    "maximum_jerk": np.max(np.abs(jerks), axis=0).tolist(),
                }
                return output

            # Convex hulls bound derivatives over the WHOLE curve, not just samples.
            q1, q2, q3 = [np.maximum(np.max(np.abs(d), axis=(0, 1)), 1e-12) for d in deriv[1:]]
            # Chain rule: q'=q_s*s', q''=q_ss*s'^2+q_s*s'',
            # q'''=q_sss*s'^3+3*q_ss*s'*s''+q_s*s'''.
            sv = float(min(np.min(velocity/q1), np.min(np.sqrt(acceleration/(2*q2))), np.min(np.cbrt(jerk/(3*q3)))))
            sa = float(min(np.min(acceleration/(2*q1)), np.min(jerk/(9*q2*sv))))
            sj = float(np.min(jerk/(3*q1)))
            times, s, sd, sdd, sddd, backend = parameterize(len(controls), sv, sa, sj, period, config.get("timing_method", "auto"))
            s = np.clip(s, 0, len(controls))
            positions, d1, d2, d3 = [_evaluate(d, s) for d in deriv]
            velocities = d1 * sd[:, None]
            accelerations = d2 * sd[:, None]**2 + d1*sdd[:, None]
            jerks = d3*sd[:, None]**3 + 3*d2*sd[:, None]*sdd[:, None] + d1*sddd[:, None]
            positions[0], positions[-1] = path[0], path[-1]
            velocities[[0, -1]] = 0
            accelerations[[0, -1]] = 0
            if blend_used == 0.0:
                # No safe corner rounding: avoid double easing with geometric
                # quintics plus a slow global scalar profile.
                times, positions, velocities, accelerations, jerks, backend = _retime_polyline(
                    path, velocity, acceleration, jerk, period, config.get("timing_method", "auto"))
            if not valid(positions):
                if not valid(path):
                    raise ValueError("Timed trajectory failed collision/joint-limit validation")
                segment_time = np.max(np.abs(np.diff(path, axis=0)), axis=1) / np.maximum(velocity, 0.01).max()
                times = np.concatenate(([0.0], np.cumsum(np.maximum(segment_time, period))))
                positions = path.copy()
                velocities = accelerations = jerks = np.zeros_like(positions)
                backend = "validated_waypoint_fallback"
                linear_diagnostics["timed_smoothing_fallback"] = "validated_waypoints"

        output = dict(trajectory)
        output.update(waypoints=positions.tolist(), time_from_start=times.tolist(),
                      velocities=velocities.tolist(), accelerations=accelerations.tolist(),
                      jerks=jerks.tolist(), duration=float(times[-1]))
        output["postprocessing"] = {
            "optimizer": "validated_cartesian_spline" if linear_result is not None else ("validated_quintic_bezier" if optimize else "preserve_path"),
            **linear_diagnostics,
            "timing_method": backend, "blend": blend_used,
            "input_points": original_count, "path_points": len(path),
            "sample_count": len(times), "processing_time": perf_counter()-started,
            "velocity_limits": velocity.tolist(), "acceleration_limits": acceleration.tolist(),
            "jerk_limits": jerk.tolist(),
            "maximum_velocity": np.max(np.abs(velocities), axis=0).tolist(),
            "maximum_acceleration": np.max(np.abs(accelerations), axis=0).tolist(),
            "maximum_jerk": np.max(np.abs(jerks), axis=0).tolist(),
        }
        return output
