"""Planning-side interface for trajectory time parameterization."""
from __future__ import annotations
import time
from typing import Any, Mapping
import numpy as np
from .errors import PlanningError
from execution.config import motion_settings
from .collision_sampling import collision_sampling_settings, spline_collision_samples


def _scaled_joint_limit(config, key, scale_key, dof, *, multiplier_key=None, clip_to_base=False):
    """Resolve a global scale plus optional per-joint scale, with URDF caps."""
    if key not in config:
        raise ValueError(f'{key} must be resolved from the robot URDF before continuous timing')
    base = np.asarray(config[key], dtype=float)
    scale = float(config.get('_constraints', {}).get(scale_key, 1.0))
    if base.shape != (dof,) or not np.all(np.isfinite(base)) or np.any(base <= 0) or not np.isfinite(scale) or scale <= 0:
        raise ValueError(f'{key} and {scale_key} must be finite positive joint limits and scale')
    values = base * scale
    if multiplier_key:
        multipliers = np.asarray(config.get(multiplier_key, np.ones(dof)), dtype=float)
        if multipliers.shape != (dof,) or not np.all(np.isfinite(multipliers)) or np.any(multipliers <= 0):
            raise ValueError(f'{multiplier_key} must contain one finite positive multiplier per joint')
        values = values * multipliers
    if clip_to_base:
        values = np.minimum(values, base)
    return values


def continuous_motion_limits(defaults, dof):
    """Resolve shared joint limits; both global scale factors participate."""
    constraints = motion_settings(defaults.get('constraints', {}))
    config = defaults.get('planning', {}).get('time_parameterization', {})
    limits = []
    constrained = dict(config)
    constrained['_constraints'] = constraints
    limits.append(_scaled_joint_limit(constrained, 'max_velocity', 'velocity_scale', dof,
                                      clip_to_base=True))
    limits.append(_scaled_joint_limit(constrained, 'max_acceleration', 'acceleration_scale', dof))
    return tuple(limits)


def prepare_continuous_trajectory(trajectory, move_request, *, collision_checker=None, timestep=0.002, kinematics=None):
    from control.continuous_trajectory import ContinuousJointPath
    result = dict(trajectory)
    circular = move_request.get('motion', {}).get('path_type') == 'circular'
    points = np.asarray(result.get('cartesian_joint_waypoints', result['waypoints'])
                        if circular else result['waypoints'], dtype=float)
    if points.ndim != 2 or len(points) < 2 or points.shape[1] == 0 or not np.all(np.isfinite(points)):
        raise ValueError('waypoints must be finite NxDOF, N >= 2')
    velocity, acceleration = continuous_motion_limits(move_request, points.shape[1])
    collision_resolution, spline_subdivisions = collision_sampling_settings(move_request)
    timing_config = move_request.get('planning', {}).get('time_parameterization', {})
    max_jerk = timing_config.get('max_jerk')
    local_timing = circular and max_jerk is None
    corner_slowdown = timing_config.get('corner_slowdown', 0.25)
    def sample(profile):
        if local_timing:
            return profile.local_samples(velocity, acceleration, timestep, slowdown=corner_slowdown)
        return profile.samples(1., 0., None, velocity, acceleration, timestep,
                               max_jerk=max_jerk, corner_slowdown=corner_slowdown)
    points = points[np.r_[True, np.any(np.diff(points, axis=0) != 0, axis=1)]]
    sampling_started = time.perf_counter()
    if len(points) == 1:
        references = np.repeat(points, 2, axis=0)
        duration = timestep
        validation_points = references
        profile = None
    else:
        distance = np.max(np.abs(np.diff(points, axis=0)), axis=1)
        # Circular IK samples are equally spaced in arc angle. Do not turn
        # collision-check densification points into extra spline constraints.
        fractions = np.linspace(0., 1., len(points)) if circular else np.r_[0., np.cumsum(distance)]
        interpolator = timing_config.get('path_interpolator', 'cubic_c2')
        profile = ContinuousJointPath(points, fractions, interpolator=interpolator)
        references, duration = sample(profile)
        validation_points = spline_collision_samples(profile, collision_resolution, spline_subdivisions)
    trajectory_sampling_s = time.perf_counter() - sampling_started
    linear_validation_s = 0.0
    if kinematics is not None and move_request.get('motion', {}).get('path_type') == 'linear':
        linear_validation_started = time.perf_counter()
        start_pose, end_pose = kinematics.forward(points[0]), kinematics.forward(points[-1])
        origin = np.asarray(start_pose['position'])
        direction = np.asarray(end_pose['position']) - origin
        length_squared = float(direction @ direction)
        # Keep Cartesian straightness validation at its previous density;
        # only collision sampling is relaxed by collision_validation settings.
        geometry_points = (profile.spline(np.linspace(0, 1,
            max(len(points)*4, int(np.ceil(profile.max_slope/.01))+1)))
            if profile is not None else validation_points)
        for q in geometry_points:
            actual = np.asarray(kinematics.forward(q)['position'])
            fraction = np.clip(float((actual-origin) @ direction)/length_squared, 0, 1) if length_squared > 1e-16 else 0
            if np.linalg.norm(actual-origin-fraction*direction) > 0.005:
                raise PlanningError('Smoothed trajectory exceeds linear path tolerance', code='TRAJECTORY_INVALID',
                                    recoverable=True, details={'stage':'execution_preparation'})
        linear_validation_s = time.perf_counter() - linear_validation_started
    collision_fallback = None
    collision_validation_s = 0.0
    cache_before = dict(getattr(collision_checker, 'path_validation_cache_stats', {}) or {})
    if collision_checker is not None:
        collision_validation_started = time.perf_counter()
        checked = collision_checker.check_path(validation_points, resolution=collision_resolution,
            safety_distance=float(move_request.get('constraints', {}).get('safety_distance', 0.0)),
            allowed_geom_ids=getattr(collision_checker, 'allowed_target_geom_ids', None))
        if checked.in_collision:
            # A cubic spline is preferred for C2 continuity, but it can bow
            # outside a tightly constrained free-space corridor.  Retry the
            # same validated waypoints with monotone PCHIP before rejecting a
            # feasible RRT path; both candidates are checked by FCL.
            if len(points) > 1 and profile.interpolator == 'cubic_c2':
                fallback = ContinuousJointPath(points, fractions, interpolator='pchip')
                fallback_refs, fallback_duration = sample(fallback)
                fallback_validation = spline_collision_samples(
                    fallback, collision_resolution, spline_subdivisions)
                fallback_check = collision_checker.check_path(
                    fallback_validation, resolution=collision_resolution,
                    safety_distance=float(move_request.get('constraints', {}).get('safety_distance', 0.0)),
                    allowed_geom_ids=getattr(collision_checker, 'allowed_target_geom_ids', None))
                if not fallback_check.in_collision:
                    profile, references, duration, validation_points = (
                        fallback, fallback_refs, fallback_duration, fallback_validation)
                    collision_fallback = 'pchip'
                else:
                    raise PlanningError('Continuous trajectory failed collision validation', code='TRAJECTORY_INVALID',
                        recoverable=True, details={'stage': 'execution_preparation', 'contacts': list(fallback_check.contacts)})
            else:
                raise PlanningError('Continuous trajectory failed collision validation', code='TRAJECTORY_INVALID',
                    recoverable=True, details={'stage': 'execution_preparation', 'contacts': list(checked.contacts)})
        collision_validation_s = time.perf_counter() - collision_validation_started
    circular_validation_s = 0.0
    if circular and kinematics is not None and profile is not None:
        circular_validation_started = time.perf_counter()
        arc = result.get('diagnostics', {})
        center, axis, angle = (np.asarray(arc['center']), np.asarray(arc['axis']), float(arc['angle']))
        from scipy.spatial.transform import Rotation
        start = kinematics.forward(points[0])
        max_position_error = max_orientation_error = 0.0
        # Validate the final interpolator, including a possible collision fallback.
        for s in np.unique(np.r_[np.linspace(0, 1, max(501, len(points)*16)), profile.spline.x]):
            pose = kinematics.forward(profile.spline(s))
            rotation = Rotation.from_rotvec(axis * angle * s).as_matrix()
            desired = center + rotation @ (np.asarray(start['position']) - center)
            desired_rotation = (start['rotation_matrix'] if move_request.get('constraints', {}).get('keep_end_effector_orientation')
                                else rotation @ start['rotation_matrix'])
            max_position_error = max(max_position_error, float(np.linalg.norm(pose['position'] - desired)))
            max_orientation_error = max(max_orientation_error, float(Rotation.from_matrix(desired_rotation.T @ pose['rotation_matrix']).magnitude()))
        if max_position_error > 0.005 or max_orientation_error > 0.02:
            raise PlanningError('Smoothed trajectory exceeds circular path tolerance', code='TRAJECTORY_INVALID',
                                recoverable=True, details={'position_error_m': max_position_error,
                                                          'orientation_error_rad': max_orientation_error})
        result.setdefault('diagnostics', {})['circular_path_validation'] = {
            'max_position_error_m': max_position_error, 'max_orientation_error_rad': max_orientation_error}
        circular_validation_s = time.perf_counter() - circular_validation_started
    result.update(geometric_waypoints=points.tolist(), time_parameterized_waypoints=references.tolist(),
                  timestamps=np.linspace(0, duration, len(references)).tolist(), duration=float(duration))
    result.pop('velocities', None)
    result.pop('accelerations', None)
    result['diagnostics'] = dict(result.get('diagnostics', {}))
    result['diagnostics']['time_parameterization'] = {
        'backend': 'continuous_precomputed', 'path_interpolator': profile.interpolator if profile is not None else None,
        'collision_fallback': collision_fallback, 'max_velocity': velocity.tolist(),
        'corner_slowdown': corner_slowdown,
        'max_acceleration': acceleration.tolist(), 'max_jerk': max_jerk,
        'acceleration_limited': True, 'jerk_limited': max_jerk is not None,
        'time_law': 'local_acceleration_bounds' if local_timing else 'global_quintic',
        'limit_source': move_request.get('planning', {}).get('time_parameterization', {}).get('limit_source'),
        'trajectory_sampling_s': trajectory_sampling_s,
        'linear_path_validation_s': linear_validation_s,
        'collision_validation_s': collision_validation_s,
        'circular_path_validation_s': circular_validation_s,
        'validation_waypoint_count': len(validation_points),
        'collision_resolution_rad': collision_resolution,
        'spline_subdivisions': spline_subdivisions,
        'path_validation_cache_enabled': bool(getattr(collision_checker, 'path_validation_cache_enabled', False)),
        'planner_waypoint_count': len(points),
        'validation_waypoints_identical_to_planner': bool(np.array_equal(validation_points, points)),
        'path_validation_cache': {
            key: value - cache_before.get(key, 0)
            for key, value in (getattr(collision_checker, 'path_validation_cache_stats', {}) or {}).items()
        },
    }
    if local_timing:
        sample_dt = duration / (len(references) - 1)
        peak_velocity = np.max(np.abs(np.diff(references, axis=0) / sample_dt), axis=0)
        peak_acceleration = (np.max(np.abs(np.diff(references, n=2, axis=0) / sample_dt**2), axis=0)
                             if len(references) > 2 else np.zeros(points.shape[1]))
        if np.any(peak_velocity > velocity * (1 + 1e-6)) or np.any(peak_acceleration > acceleration * (1 + 1e-6)):
            raise PlanningError('Locally timed references exceed joint limits', code='TRAJECTORY_INVALID',
                                recoverable=True, details={'stage': 'execution_preparation'})
        result['diagnostics']['time_parameterization'].update(
            geometric_waypoint_count=len(points), input_waypoint_count=len(trajectory['waypoints']),
            sampled_peak_velocity_rad_s=peak_velocity.tolist(),
            sampled_peak_acceleration_rad_s2=peak_acceleration.tolist())
    return result


def prepare_interpolated_trajectory(trajectory: Mapping[str, Any], move_request: Mapping[str, Any], *, collision_checker=None):
    """Keep the geometric polyline and assign timestamps, without using Ruckig.

    Timing respects joint velocity limits but does not constrain acceleration
    or jerk at corners. Intermediate samples are not commanded to stop.
    """
    result = dict(trajectory)
    points = np.asarray(result['waypoints'], dtype=float)
    if points.ndim != 2 or len(points) < 2 or points.shape[1] == 0 or not np.all(np.isfinite(points)):
        raise ValueError('waypoints must be finite with shape (N, DOF), N >= 2')
    constraints = move_request.get('constraints', {})
    config = move_request.get('planning', {}).get('time_parameterization', {})
    scale = float(motion_settings(constraints)['velocity_scale'])
    limits = np.asarray(config.get('max_velocity', np.ones(points.shape[1])), dtype=float)
    if (not np.isfinite(scale) or scale <= 0 or limits.shape != (points.shape[1],)
            or not np.all(np.isfinite(limits)) or np.any(limits <= 0)):
        raise ValueError('velocity_scale and per-joint max_velocity must be finite and positive')
    limits = np.minimum(limits * scale, limits)
    # Remove exact consecutive duplicates only; preserve every geometric corner.
    points = points[np.r_[True, np.any(np.diff(points, axis=0) != 0, axis=1)]]
    if len(points) == 1:
        points = np.repeat(points, 2, axis=0)
        stamps = np.array([0.0, 0.01])
    else:
        durations = np.max(np.abs(np.diff(points, axis=0)) / limits, axis=1)
        stamps = np.r_[0.0, np.cumsum(durations)]
    if collision_checker is not None:
        checked = collision_checker.check_path(points, resolution=0.01,
            safety_distance=float(constraints.get('safety_distance', 0.0)),
            allowed_geom_ids=getattr(collision_checker, 'allowed_target_geom_ids', None))
        if checked.in_collision:
            raise PlanningError('Interpolated trajectory failed collision validation',
                code='TRAJECTORY_INVALID', recoverable=True,
                details={'stage': 'execution_preparation', 'contacts': list(checked.contacts)})
    result['geometric_waypoints'] = result['waypoints']
    result['time_parameterized_waypoints'] = points.tolist()
    result['timestamps'] = stamps.tolist()
    result['duration'] = float(stamps[-1])
    # Never retain stale derivatives from a previously timed trajectory.
    result.pop('velocities', None)
    result.pop('accelerations', None)
    result['diagnostics'] = dict(result.get('diagnostics', {}))
    result['diagnostics']['time_parameterization'] = {
        'backend': 'linear_interpolation', 'max_velocity': limits.tolist(),
        'acceleration_limited': False, 'jerk_limited': False,
    }
    return result


def parameterize_trajectory(trajectory: Mapping[str, Any], move_request: Mapping[str, Any], parameterizer=None, *, collision_checker=None):
    """Attach Ruckig timing data while retaining the geometric path."""
    from libraries.ruckig import RuckigTimeParameterizer
    result = dict(trajectory)
    points = np.asarray(result["waypoints"], dtype=float)
    constraints = move_request.get("constraints", {})
    config = move_request.get("planning", {}).get("time_parameterization", {})
    base_velocity = np.asarray(config.get("max_velocity", np.ones(points.shape[1])), dtype=float)
    base_acceleration = np.asarray(config.get("max_acceleration", np.ones(points.shape[1])), dtype=float)
    scales = motion_settings(constraints)
    velocity_scale = float(scales['velocity_scale'])
    acceleration_scale = float(scales['acceleration_scale'])
    if (base_velocity.shape != (points.shape[1],) or base_acceleration.shape != (points.shape[1],)
            or not np.all(np.isfinite(base_velocity)) or not np.all(np.isfinite(base_acceleration))
            or np.any(base_velocity <= 0)
            or np.any(base_acceleration <= 0)
            or not np.isfinite(velocity_scale) or velocity_scale <= 0
            or not np.isfinite(acceleration_scale) or acceleration_scale <= 0):
        raise ValueError('Ruckig joint limits and scales must be finite and positive')
    velocity = np.minimum(base_velocity * velocity_scale, base_velocity)
    acceleration = base_acceleration * acceleration_scale
    parameterizer = parameterizer or RuckigTimeParameterizer(
        control_cycle=config.get("control_cycle", 0.01), max_velocity=velocity.tolist(),
        max_acceleration=acceleration.tolist(), max_jerk=config.get("max_jerk"))
    timed = parameterizer.parameterize(points, velocity_scale=1.0,
        acceleration_scale=1.0, max_velocity=velocity.tolist(),
        max_acceleration=acceleration.tolist(), max_jerk=config.get("max_jerk"))
    if collision_checker is not None:
        check = collision_checker.check_path(
            timed["positions"], resolution=0.01,
            safety_distance=float(constraints.get("safety_distance", 0.0)),
            allowed_geom_ids=getattr(collision_checker, "allowed_target_geom_ids", None),
        )
        if check.in_collision:
            raise PlanningError(
                "Time-parameterized trajectory failed collision validation",
                code="TRAJECTORY_INVALID", recoverable=True,
                details={"stage": "time_parameterization", "contacts": list(check.contacts)},
            )
    result["geometric_waypoints"] = result["waypoints"]
    result["time_parameterized_waypoints"] = timed["positions"].tolist()
    result["timestamps"] = timed["timestamps"]
    result["velocities"] = timed["velocities"].tolist()
    result["accelerations"] = timed["accelerations"].tolist()
    result["duration"] = timed["duration"]
    result.setdefault("diagnostics", {})["time_parameterization"] = {"backend": "ruckig", "control_cycle": timed["control_cycle"], "max_velocity": timed["max_velocity"].tolist(), "max_acceleration": timed["max_acceleration"].tolist(), "max_jerk": timed["max_jerk"].tolist()}
    return result
