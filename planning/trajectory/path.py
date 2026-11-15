"""Continuous joint path with one rest-to-rest time law for the whole motion."""
import numpy as np
from scipy.interpolate import CubicSpline, PchipInterpolator


class ContinuousJointPath:
    def __init__(self, path, fractions, *, interpolator="cubic_c2"):
        """Build a continuous joint path over normalized progress.

        The default natural cubic spline is C2 continuous at every interior
        knot, so the time law does not introduce an acceleration jump whenever
        an RRT waypoint is crossed.  PCHIP remains available as a conservative
        fallback for paths where monotonicity is more important than curvature
        continuity.
        """
        fractions = np.asarray(fractions, dtype=float)
        path = np.asarray(path, dtype=float)
        if fractions.ndim != 1 or len(fractions) != len(path) or len(path) < 2:
            raise ValueError("path and fractions must contain at least two matching samples")
        if not np.all(np.isfinite(fractions)) or not np.all(np.isfinite(path)):
            raise ValueError("path and fractions must be finite")
        if np.any(np.diff(fractions) <= 0) or fractions[-1] <= 0:
            raise ValueError("fractions must be strictly increasing and end above zero")
        x = fractions / fractions[-1]
        if interpolator in ("pchip", "monotone"):
            self.spline = PchipInterpolator(x, path, axis=0)
            self.interpolator = "pchip"
        elif interpolator in ("cubic", "cubic_c2", "bspline"):
            # Natural cubic splines retain C2 continuity without forcing a
            # zero geometric slope at the first/last RRT sample.  The outer
            # quintic time law already brings the commanded motion to rest;
            # clamping the geometric slope here creates a large, artificial
            # jerk spike for an otherwise straight path.
            self.spline = CubicSpline(x, path, axis=0, bc_type="natural")
            self.interpolator = "cubic_c2"
        else:
            raise ValueError(f"Unsupported path interpolator: {interpolator}")
        # Exact component-wise derivative bounds for each cubic interval.
        a, b, c, _ = self.spline.c
        widths = np.diff(self.spline.x)[:, None]
        vertex = np.zeros_like(a)
        np.divide(-b, 3 * a, out=vertex, where=a != 0)
        vertex = np.clip(vertex, 0, widths)
        self.slopes = np.max(np.abs(np.stack([
            c, 3*a*widths**2 + 2*b*widths + c,
            3*a*vertex**2 + 2*b*vertex + c,
        ])), axis=0)
        self.curvatures = np.max(np.abs(np.stack([2*b, 6*a*widths + 2*b])), axis=0)
        self.max_slope = float(np.max(self.slopes))

    def local_samples(self, max_speed, max_acceleration, timestep, *, slowdown=0.0):
        """Locally bound q'(s)s_dot and q''(s)s_dot² + q'(s)s_ddot.

        Half the acceleration budget goes to curvature and half to tangential
        acceleration. Exact cubic derivative bounds on each interval make this
        conservative without stretching the entire path for a local bend.
        Jerk limits are intentionally handled by the existing quintic sampler.
        """
        velocity = np.asarray(max_speed, dtype=float)
        acceleration = np.asarray(max_acceleration, dtype=float)
        if (velocity.shape != self.spline.c.shape[2:] or acceleration.shape != velocity.shape
                or not np.all(np.isfinite(velocity)) or np.any(velocity <= 0)
                or not np.all(np.isfinite(acceleration)) or np.any(acceleration <= 0)
                or not np.isfinite(timestep) or timestep <= 0
                or not np.isfinite(slowdown) or slowdown < 0):
            raise ValueError('Local timing requires finite positive joint limits and timestep')
        grid = np.unique(np.concatenate([np.linspace(a, b, 9) for a, b in
                                        zip(self.spline.x[:-1], self.spline.x[1:])]))
        left, right = grid[:-1], grid[1:]
        indices = np.clip(np.searchsorted(self.spline.x, left, side='right') - 1,
                          0, len(self.spline.x) - 2)
        a, b, c, _ = self.spline.c[:, indices, :]
        lo = (left - self.spline.x[indices])[:, None]
        hi = (right - self.spline.x[indices])[:, None]
        vertex = np.zeros_like(a)
        np.divide(-b, 3 * a, out=vertex, where=a != 0)
        vertex = np.clip(vertex, lo, hi)
        slope = np.maximum.reduce([np.abs(3*a*z*z + 2*b*z + c) for z in (lo, hi, vertex)])
        curvature = np.maximum(np.abs(6*a*lo + 2*b), np.abs(6*a*hi + 2*b))
        slope = np.maximum(slope, 1e-12)
        curvature = np.maximum(curvature, 1e-12)
        caps = np.minimum(np.min((velocity / slope)**2, axis=1),
                          np.min(0.5 * acceleration / curvature, axis=1))
        tangential = np.min(0.5 * acceleration / slope, axis=1)
        squared_speed = np.r_[caps[0], np.minimum(caps[:-1], caps[1:]), caps[-1]]
        squared_speed[0] = squared_speed[-1] = 0.0
        ds = np.diff(grid)
        for i in range(len(ds)):
            squared_speed[i + 1] = min(squared_speed[i + 1],
                                      squared_speed[i] + 2 * tangential[i] * ds[i])
        for i in range(len(ds) - 1, -1, -1):
            squared_speed[i] = min(squared_speed[i],
                                   squared_speed[i + 1] + 2 * tangential[i] * ds[i])
        path_speed = np.sqrt(squared_speed)
        times = np.r_[0., np.cumsum(2 * ds / (path_speed[:-1] + path_speed[1:]))]
        duration = float(times[-1]) * (1 + slowdown)
        steps = max(1, int(np.ceil(duration / timestep)))
        duration = steps * timestep
        sample_times = np.linspace(0, times[-1], steps + 1)
        segment = np.clip(np.searchsorted(times, sample_times, side='right') - 1, 0, len(ds) - 1)
        tau = sample_times - times[segment]
        path_acceleration = np.diff(squared_speed) / (2 * ds)
        progress = (grid[segment] + path_speed[segment] * tau
                    + 0.5 * path_acceleration[segment] * tau**2)
        progress = np.clip(progress, grid[0], grid[-1])
        progress[0], progress[-1] = grid[0], grid[-1]
        return self.spline(progress), duration

    def samples(self, limit, travel, speed, max_speed, max_acceleration, timestep, max_jerk=None,
                corner_slowdown=0.0):
        velocity_limits = np.asarray(max_speed, dtype=float)
        if not np.all(np.isfinite(velocity_limits)) or np.any(velocity_limits <= 0):
            raise ValueError('Joint velocity limits must be finite and positive')
        if max_acceleration is not None:
            acceleration_limits = np.asarray(max_acceleration, dtype=float)
            if not np.all(np.isfinite(acceleration_limits)) or np.any(acceleration_limits <= 0):
                raise ValueError('Joint acceleration limits must be finite and positive')
        # Evaluate the composed time law directly.  The previous estimate
        # used only 60*path_slope, which can miss the p'' and p''' terms of
        # q'''(t) and therefore under-estimate the duration at curved knots.
        u_dense = np.linspace(0.0, 1.0, 1001)
        progress_dense = limit * u_dense**3 * (10 + u_dense*(-15 + 6*u_dense))
        h1 = 30.0 * u_dense**2 * (1.0 - u_dense)**2
        h2 = 60.0 * u_dense * (1.0 - u_dense) * (1.0 - 2.0 * u_dense)
        h3 = 60.0 * (1.0 - 6.0 * u_dense + 6.0 * u_dense**2)
        p1 = np.asarray(self.spline.derivative(1)(progress_dense))
        p2 = np.asarray(self.spline.derivative(2)(progress_dense))
        p3 = np.asarray(self.spline.derivative(3)(progress_dense))
        velocity_coefficient = np.max(np.abs(p1 * (limit * h1)[:, None]), axis=0)
        acceleration_coefficient = np.max(
            np.abs(p2 * (limit * h1)[:, None] ** 2 + p1 * (limit * h2)[:, None]), axis=0
        )
        jerk_coefficient = np.max(
            np.abs(
                p3 * (limit * h1)[:, None] ** 3
                + 3.0 * p2 * (limit * h1)[:, None] * (limit * h2)[:, None]
                + p1 * (limit * h3)[:, None]
            ), axis=0
        )
        duration = max(float(np.max(velocity_coefficient / velocity_limits)), timestep)
        if max_acceleration is not None:
            duration = max(duration, float(np.max(
                np.sqrt(acceleration_coefficient / acceleration_limits)
            )))
        if max_jerk is not None:
            jerk_limits = np.asarray(max_jerk, dtype=float)
            if not np.all(np.isfinite(jerk_limits)) or np.any(jerk_limits <= 0):
                raise ValueError('Joint jerk limits must be finite and positive')
            duration = max(duration, float(np.max(
                (jerk_coefficient / jerk_limits) ** (1.0 / 3.0)
            )))
        if speed is not None:
            if not np.isfinite(speed) or speed <= 0:
                raise ValueError('Task speed limit must be finite and positive')
            duration = max(duration, 1.875*travel/speed)
        # Conservative corner handling: a large second derivative means that
        # the reference changes direction sharply in joint space.  Add time
        # around such paths so the servo does not have to track the nominal
        # limit at the sharpest bend.  Straight paths are unchanged.
        corner_slowdown = float(corner_slowdown)
        if not np.isfinite(corner_slowdown) or corner_slowdown < 0:
            raise ValueError('corner_slowdown must be finite and nonnegative')
        if corner_slowdown:
            curvature_ratio = float(np.max(np.abs(p2)) / max(np.max(np.abs(p1)), 1e-9))
            duration *= 1.0 + corner_slowdown * min(1.0, curvature_ratio)
        steps = int(np.ceil(duration/timestep))
        u = np.linspace(0, 1, steps+1)
        progress = limit * u**3 * (10 + u*(-15 + 6*u))
        return self.spline(progress), steps*timestep
