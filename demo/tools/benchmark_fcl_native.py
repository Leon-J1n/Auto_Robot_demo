"""Compare native filtered queries with the original query on identical poses."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=60)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    os.environ['SIM_PLATFORM_LIBRARY'] = str(args.library.resolve())
    # Matches the execution plugin's DLL loading order for the original bundle.
    import mujoco
    from collision.simplatform_fcl import SimPlatformFCLCollisionChecker
    from execution import SceneResourceCoordinator
    root = Path(__file__).resolve().parents[2]
    resources = SceneResourceCoordinator(root / 'assets/catalog.json').resolve(
        json.loads((root / 'demo/task_commands/commands.001.json').read_text(encoding='utf-8')))
    checker = SimPlatformFCLCollisionChecker(scene_file=resources.scene_file, scene_data=resources.scene,
                                            robot_urdf=resources.robot_urdf,
                                            initial_joints=resources.initial_joints)
    checker.cache_enabled = False
    rng = np.random.default_rng(1701)
    poses = np.vstack([resources.initial_joints,
                       rng.uniform(-np.pi, np.pi, (args.samples - 1, 6))])
    rows, times = [], {'legacy': 0., 'filtered': 0.}

    def contacts(result):
        return sorted((p['geom1'], p['geom2'], p['self_collision'], p['distance']) for p in result.contacts)

    try:
        initial_scene = checker._scene_cache_stamp()
        for index, q in enumerate(poses):
            results = {}
            for mode in ('legacy', 'filtered') if index % 2 == 0 else ('filtered', 'legacy'):
                checker.native_filter_enabled = mode == 'filtered'
                started = time.perf_counter()
                result = checker.check(q, safety_distance=.003)
                times[mode] += time.perf_counter() - started
                results[mode] = contacts(result)
            if results['legacy'] != results['filtered']:
                raise AssertionError(f'Native filtering changed collision result at sample {index}: {results}')
            rows.append({'q': q.tolist(), 'contacts': results['legacy']})
        assert checker._scene_cache_stamp() == initial_scene, 'Query changed live scene'
        report = {'library': str(args.library.resolve()),
                  'supports_native_filter': checker.robot.supports_filtered_contacts,
                  'samples': len(poses), 'matching_samples': len(rows),
                  'colliding_samples': sum(bool(row['contacts']) for row in rows),
                  'seconds': times, 'mean_ms': {k: v * 1000 / len(poses) for k, v in times.items()},
                  'rows': rows}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, indent=2))
    finally:
        checker.close()


if __name__ == '__main__':
    main()
