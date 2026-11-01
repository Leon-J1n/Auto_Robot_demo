"""Measure whole-path timing bottlenecks without changing motion parameters."""
import argparse
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

import numpy as np
from scipy.optimize import brentq


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commands', required=True)
    parser.add_argument('--library', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['SIM_PLATFORM_LIBRARY'] = str(Path(args.library).resolve())
    from control.continuous_trajectory import ContinuousJointPath
    from control.pull_push_controller import MujocoPullPushController
    from demo.run_demo import run
    import mujoco

    args.output.mkdir(parents=True, exist_ok=True)
    profiles, timings = [], {}
    original = ContinuousJointPath.samples

    def samples(self, limit, travel, speed, max_speed, max_acceleration, timestep):
        refs, duration = original(self, limit, travel, speed, max_speed, max_acceleration, timestep)
        knots = np.unique(np.r_[self.spline.x[self.spline.x < limit], limit])/limit
        clock = np.array([brentq(lambda u: u**3*(10+u*(-15+6*u))-s, 0, 1) for s in knots])
        lo, hi = clock[:-1], clock[1:]
        mid = np.clip(.5, lo, hi)
        v = 30*mid**2*(1-mid)**2
        extrema = np.stack([lo, hi, np.clip((3-np.sqrt(3))/6, lo, hi), np.clip((3+np.sqrt(3))/6, lo, hi)])
        a = np.max(abs(60*extrema*(1-extrema)*(1-2*extrema)), axis=0)
        slopes = self.slopes[:len(lo)]*limit
        curvature = self.curvatures[:len(lo)]*limit**2
        speed_bound = slopes*v[:,None]/np.asarray(max_speed)
        accel_bound = np.sqrt((curvature*v[:,None]**2+slopes*a[:,None])/np.asarray(max_acceleration))
        interval, joint = np.unravel_index(np.argmax(accel_bound), accel_bound.shape)
        dq = np.diff(refs,axis=0)/timestep
        ddq = np.diff(dq,axis=0)/timestep
        points = self.spline(self.spline.x)
        row = {'duration_s':duration, 'velocity_only_duration_s':float(np.max(speed_bound)),
               'acceleration_duration_s':float(np.max(accel_bound)), 'joint_index':int(joint),
               'limiting_progress_interval':knots[interval:interval+2].tolist(),
               'limiting_time_interval_s':(clock[interval:interval+2]*duration).tolist(),
               'joint_travel_rad':np.sum(abs(np.diff(points,axis=0)),axis=0).tolist(),
               'peak_reference_velocity':np.max(abs(dq),axis=0).tolist(),
               'peak_reference_acceleration':np.max(abs(ddq),axis=0).tolist(),
               'neighbor_knots':self.spline.x[max(0,interval-2):interval+4].tolist(),
               'neighbor_joints':points[max(0,interval-2):interval+4].tolist(),
               'max_velocity':np.asarray(max_speed).tolist(), 'max_acceleration':np.asarray(max_acceleration).tolist()}
        index = len(profiles)
        np.savez_compressed(args.output/f'profile_{index}.npz', points=points, knots=self.spline.x,
                            references=refs, timestep=timestep)
        profiles.append(row)
        return refs, duration

    def measured(name, function):
        def call(*a, **kw):
            start = time.perf_counter()
            try:
                return function(*a, **kw)
            finally:
                row = timings.setdefault(name, {'calls':0, 'wall_s':0.})
                row['calls'] += 1
                row['wall_s'] += time.perf_counter()-start
        return call

    from contextlib import ExitStack
    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(patch.object(ContinuousJointPath, 'samples', samples))
        for owner, name in [(MujocoPullPushController,'_collision'), (MujocoPullPushController,'_contacts'),
                            (mujoco,'mj_step'), (mujoco,'mj_forward')]:
            stack.enter_context(patch.object(owner,name,measured(name,getattr(owner,name))))
        report = run(args.commands, output_dir=args.output)
    summary = {'wall_s':time.perf_counter()-started, 'simulation_s':report.get('final_state',{}).get('simulation_time'),
               'profiles':profiles, 'timings':timings,
               'joint_names':report.get('final_state',{}).get('joint_names'),
               'notes':'Wall times include instrumentation; nested function times must not be added together.'}
    (args.output/'timing_diagnosis.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()

