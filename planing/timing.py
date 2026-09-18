"""Adapted from ROBOTSIM/algorithms/timing/ruckig/parameterizer.py.

Reuse its offline calculate/at_time sampling, applied to a scalar path coordinate.
This keeps joint synchronization on the validated geometric path. No cloud API.
"""

import numpy as np


def parameterize(distance, velocity, acceleration, jerk, sample_period=0.01, method="auto"):
    """Return scalar time, position, velocity, acceleration, jerk and backend ID."""
    if method not in {"auto", "ruckig", "quintic"}:
        raise ValueError("timing_method must be auto, ruckig or quintic")
    if method != "quintic":
        try:
            import ruckig
        except ImportError:
            if method == "ruckig":
                raise ValueError("Ruckig is unavailable; install planing/requirements.txt")
        else:
            generator = ruckig.Ruckig(1, sample_period)
            inputs = ruckig.InputParameter(1)
            inputs.current_position = [0.0]
            inputs.current_velocity = [0.0]
            inputs.current_acceleration = [0.0]
            inputs.target_position = [float(distance)]
            inputs.target_velocity = [0.0]
            inputs.target_acceleration = [0.0]
            inputs.max_velocity = [float(velocity)]
            inputs.max_acceleration = [float(acceleration)]
            inputs.max_jerk = [float(jerk)]
            trajectory = ruckig.Trajectory(1)
            result = generator.calculate(inputs, trajectory)
            if int(result) < 0:
                raise ValueError(f"Ruckig calculation failed: {result}")
            times = np.append(np.arange(0.0, trajectory.duration, sample_period), trajectory.duration)
            samples = np.asarray([trajectory.at_time(float(t)) for t in times])[:, :, 0]
            # Ruckig's Python at_time returns q/v/a; jerk is piecewise constant.
            jerks = np.gradient(samples[:, 2], times)
            return times, samples[:, 0], samples[:, 1], samples[:, 2], jerks, "ruckig"
    # Dependency-free fallback with analytic extrema of 10u^3-15u^4+6u^5.
    duration = max(1.875 * distance / velocity,
                   np.sqrt((10.0 / np.sqrt(3)) * distance / acceleration),
                   np.cbrt(60.0 * distance / jerk), sample_period)
    times = np.append(np.arange(0.0, duration, sample_period), duration)
    u = times / duration
    s = distance * (10*u**3 - 15*u**4 + 6*u**5)
    v = distance * (30*u**2 - 60*u**3 + 30*u**4) / duration
    a = distance * (60*u - 180*u**2 + 120*u**3) / duration**2
    j = distance * (60 - 360*u + 360*u**2) / duration**3
    return times, s, v, a, j, "quintic"
