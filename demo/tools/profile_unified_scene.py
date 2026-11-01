"""Read-only timing instrumentation around the unified demo; no skill files changed."""
import argparse
from collections import defaultdict
import cProfile
from functools import wraps
import inspect
import io
import json
import os
from pathlib import Path
import pstats
import time


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commands', type=Path, required=True)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('tests/output/response_profile'))
    parser.add_argument('--viewer', action='store_true')
    parser.add_argument('--cprofile', action='store_true')
    parser.add_argument('--legacy-target-ranking', action='store_true',
                        help='Diagnostic only: retain the old final-target direct-path preference')
    args = parser.parse_args()
    os.environ['SIM_PLATFORM_LIBRARY'] = str(args.library.resolve())
    from execution.mujoco import MuJoCoExecutionBackend
    from execution import SceneResourceCoordinator, SceneStateSynchronizer
    from collision import SimPlatformFCLCollisionChecker
    from kinematics import KinematicsService
    import libraries.eaik as eaik
    import planning
    from planning import DirectPlanner
    from services.object_library import ObjectLibraryClient, LocalObjectLibraryService
    from demo.interaction.object_library import ObjectLibraryGraspResolver
    from demo.task_skills.move import DemoInteractionMove
    from demo.task_skills.pick import DemoInteractionPick
    from demo.run_demo import run

    bootstrap = time.perf_counter() - started
    original_select = ObjectLibraryGraspResolver.select
    if args.legacy_target_ranking:
        def legacy_select(*values, **kwargs):
            kwargs['prefer_direct'] = True
            return original_select(*values, **kwargs)
        ObjectLibraryGraspResolver.select = staticmethod(legacy_select)
    rows, checks, paths, events, stack, originals = {}, {}, [], [], [], []
    origin = time.perf_counter()

    def wrap(owner, name, label=None):
        descriptor = inspect.getattr_static(owner, name, None)
        original = getattr(owner, name)
        label = label or f'{owner.__name__}.{name}'
        @wraps(original)
        def measured(*values, **kwargs):
            caller = inspect.currentframe().f_back
            source = f'{Path(caller.f_code.co_filename).name}:{caller.f_code.co_name}:{caller.f_lineno}'
            parent = stack[-1]['label'] if stack else source
            entry = {'label': label, 'children': 0., 'start': time.perf_counter()}
            stack.append(entry)
            result = None
            try:
                result = original(*values, **kwargs)
                return result
            finally:
                elapsed = time.perf_counter() - entry['start']
                stack.pop()
                if stack:
                    stack[-1]['children'] += elapsed
                key = f'{label} <- {parent}'
                row = rows.setdefault(key, {'label': label, 'parent': parent, 'calls': 0, 'inclusive_s': 0., 'exclusive_s': 0.})
                row['calls'] += 1
                row['inclusive_s'] += elapsed
                row['exclusive_s'] += elapsed - entry['children']
                if name == 'check':
                    margin = float(kwargs.get('safety_distance', 0.))
                    key = f'{margin:g} | {parent}'
                    row = checks.setdefault(key, {'margin_m': margin, 'parent': parent, 'calls': 0, 'seconds': 0.})
                    row['calls'] += 1
                    row['seconds'] += elapsed
                if name == 'check_path':
                    paths.append({'source': source, 'parent': parent, 'seconds': elapsed,
                                  'waypoints': len(values[1]), 'resolution': kwargs.get('resolution', .05),
                                  'margin_m': kwargs.get('safety_distance', 0.),
                                  'collision': getattr(result, 'in_collision', None)})
                if not stack or label in {'DemoInteractionMove.prepare', 'ObjectLibraryGraspResolver.select',
                                          'MuJoCoExecutionBackend.execute_trajectory', 'planning.prepare_interpolated_trajectory'}:
                    events.append({'label': label, 'source': source, 'start_s': entry['start'] - origin,
                                   'duration_s': elapsed, 'nested': bool(stack)})
        originals.append((owner, name, descriptor))
        setattr(owner, name, staticmethod(measured) if isinstance(descriptor, staticmethod) else measured)

    for owner, names in [
        (SceneResourceCoordinator, ['resolve', 'validate_consistency']),
        (SceneStateSynchronizer, ['sync']),
        (MuJoCoExecutionBackend, ['load', 'open_viewer', 'execute_trajectory', 'close']),
        (SimPlatformFCLCollisionChecker, ['__init__', 'check', 'check_path', 'set_entity_transform', 'close']),
        (KinematicsService, ['solve_all', 'solve_one']),
        (ObjectLibraryClient, ['get_model']), (LocalObjectLibraryService, ['__init__', 'close']),
        (ObjectLibraryGraspResolver, ['resolve', 'select']), (DemoInteractionMove, ['prepare']), (DemoInteractionPick, ['prepare']),
        (DirectPlanner, ['plan']), (eaik, ['load_urdf']), (planning, ['prepare_interpolated_trajectory'])]:
        for name in names:
            wrap(owner, name)
    args.output.mkdir(parents=True, exist_ok=True)
    profile = cProfile.Profile() if args.cprofile else None
    run_started = time.perf_counter()
    try:
        if profile:
            profile.enable()
        result = run(args.commands, output_dir=args.output, viewer=args.viewer, start_object_library=True)
    finally:
        if profile:
            profile.disable()
        elapsed = time.perf_counter() - run_started
        for owner, name, descriptor in reversed(originals):
            if descriptor is None:
                delattr(owner, name)
            else:
                setattr(owner, name, descriptor)
        ObjectLibraryGraspResolver.select = staticmethod(original_select)
    execution_s = sum(row['inclusive_s'] for row in rows.values() if row['label'] == 'MuJoCoExecutionBackend.execute_trajectory')
    summary = {'success': result['success'], 'error': result.get('error'), 'viewer': args.viewer,
               'legacy_target_ranking': args.legacy_target_ranking,
               'cprofile': args.cprofile, 'bootstrap_s': bootstrap, 'run_s': elapsed,
               'execution_wall_s': execution_s, 'outside_execution_s': elapsed - execution_s,
               'simulation_s': result.get('final_state', {}).get('simulation_time'),
               'rows': sorted(rows.values(), key=lambda row: -row['inclusive_s']),
               'checks': list(checks.values()), 'paths': paths, 'events': events,
               'notes': 'Inclusive times overlap; exclusive times exclude only other instrumented calls. Execution wall time includes frame checks/rendering/pacing.'}
    (args.output / 'profile.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if profile:
        profile.dump_stats(str(args.output / 'profile.pstats'))
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(65)
        pstats.Stats(profile, stream=stream).sort_stats('tottime').print_stats(35)
        (args.output / 'functions.txt').write_text(stream.getvalue(), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k not in {'rows', 'checks', 'paths', 'events', 'notes'}}, indent=2))


if __name__ == '__main__':
    main()


