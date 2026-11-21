"""Replay a demo and compare rejected continuous paths with their polylines."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commands', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ['SIM_PLATFORM_LIBRARY'] = str(ROOT / 'libraries/simplatform/bin/sim_platform_c.dll')
    from planning.common.errors import PlanningError
    from planning.constraints.collision_sampling import spline_collision_samples
    from planning.trajectory.path import ContinuousJointPath
    from planning.trajectory import time_parameterization as timing
    from demo.run_demo import run

    original = timing.prepare_continuous_trajectory
    failures = []

    def diagnosed(trajectory, request, **kwargs):
        try:
            return original(trajectory, request, **kwargs)
        except PlanningError as exc:
            if str(exc) != 'Continuous trajectory failed collision validation':
                raise
            checker = kwargs['collision_checker']
            points = np.asarray(trajectory['waypoints'])
            points = points[np.r_[True, np.any(np.diff(points, axis=0) != 0, axis=1)]]
            fractions = np.r_[0., np.cumsum(np.max(np.abs(np.diff(points, axis=0)), axis=1))]
            row = {'request': request, 'error': exc.to_dict() if hasattr(exc, 'to_dict') else exc.details,
                   'planner_diagnostics': trajectory.get('diagnostics'), 'checks': []}
            arrays = {'polyline': points}
            for name in ('polyline', 'cubic_c2', 'pchip'):
                profile = None if name == 'polyline' else ContinuousJointPath(points, fractions, interpolator=name)
                for resolution in (.04, .01, .002):
                    samples = points if profile is None else spline_collision_samples(profile, resolution, 2)
                    hit = checker.check_path(samples, resolution=resolution,
                        safety_distance=request['constraints']['safety_distance'],
                        allowed_geom_ids=getattr(checker, 'allowed_target_geom_ids', None))
                    row['checks'].append({'representation': name, 'resolution': resolution,
                        'points': len(samples), 'in_collision': bool(hit.in_collision),
                        'minimum_distance': float(hit.minimum_distance), 'contacts': list(hit.contacts)})
                    arrays[f'{name}_{resolution}'] = samples
            row['knot_collisions'] = [i for i,q in enumerate(points)
                if checker.check(q, safety_distance=request['constraints']['safety_distance']).in_collision]
            index = len(failures) + 1
            np.savez(args.output / f'failure_{index}.npz', **arrays)
            failures.append(row)
            (args.output / 'collision_diagnosis.json').write_text(
                json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
            print('COLLISION_DIAGNOSIS', json.dumps(row['checks']), flush=True)
            raise

    timing.prepare_continuous_trajectory = diagnosed
    try:
        result = run(args.commands, output_dir=args.output / 'replay', start_object_library=True)
        print(json.dumps({'success': result['success'], 'diagnosed_failures': len(failures)}), flush=True)
    finally:
        timing.prepare_continuous_trajectory = original


if __name__ == '__main__':
    main()
