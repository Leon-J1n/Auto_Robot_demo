"""Single-run output lifecycle and compact persisted report views."""
from copy import deepcopy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil


@contextmanager
def output_lock(path):
    """Prevent a second process from clearing an active run's output."""
    output = Path(path).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / f'.{output.name}.lock'
    with lock.open('a+b') as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f'Output directory is already used by another run: {output}') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_output(path, *, protected=()):
    candidate = Path(path).absolute()
    if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
        raise ValueError(f'Output directory must not be a link: {candidate}')
    output = Path(path).resolve()
    workspace = Path(__file__).resolve().parents[1]
    if output == Path(output.anchor) or output == workspace or output in workspace.parents:
        raise ValueError(f'Unsafe output directory: {output}')
    for source in protected:
        if source is None:
            continue
        source = Path(source).resolve()
        if source == output or output in source.parents:
            raise ValueError(f'Output cleanup would remove input: {source}')
    # Validate every target before removing anything; never traverse junctions.
    children = list(output.iterdir()) if output.exists() else []
    for child in children:
        if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
            raise ValueError(f'Output contains a link; refusing cleanup: {child}')
        if output not in child.resolve().parents:
            raise ValueError(f'Output cleanup target escapes directory: {child}')
    for child in children:
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name in ('commands', 'metrics', 'trajectories/planned', 'snapshots', 'runtime'):
        (output / name).mkdir(parents=True, exist_ok=True)
    return output


def command_view(row, output, stem, write_json):
    """Store each trajectory once, retaining diagnostics beside the command."""
    result = deepcopy(row)
    arrays = {}
    seen = {}
    fields = ('waypoints', 'geometric_waypoints', 'time_parameterized_waypoints', 'timestamps')
    def visit(value, location):
        if isinstance(value, dict):
            trajectory = value.get('trajectory')
            if isinstance(trajectory, dict):
                payload = {key: trajectory.pop(key) for key in fields if key in trajectory}
                if payload:
                    key = json.dumps(payload, sort_keys=True, default=lambda value: value.tolist())
                    ident = seen.get(key)
                    if ident is None:
                        ident = f'trajectory_{len(arrays) + 1}'
                        arrays[ident] = payload
                        seen[key] = ident
                    trajectory['data_file'] = f'trajectories/planned/{stem}.json'
                    trajectory['data_key'] = ident
            for key, item in value.items():
                visit(item, f'{location}/{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f'{location}/{index}')
    visit(result, '')
    if arrays:
        write_json(Path(output) / 'trajectories/planned' / f'{stem}.json', arrays)
    return result


def summary_view(report):
    result = {key: report[key] for key in (
        'request_id', 'success', 'status', 'frame_count', 'physics_steps', 'execution_backend',
        'trajectory_file', 'blackboard_file', 'final_scene_file', 'final_scene_export_error',
        'trajectory_export_error', 'execution_started', 'no_effect_verified',
        'state_capture_error') if key in report}
    steps = report.get('steps', [])
    result['commands_file'] = 'commands/results.jsonl'
    result['command_count'] = len(steps)
    result['successful_command_count'] = sum(bool(row.get('success')) for row in steps)
    result['failed_command_count'] = sum(not bool(row.get('success')) for row in steps)
    failed = next((row for row in steps if not row.get('success')), None)
    result['failure'] = None
    if failed is not None or report.get('error'):
        error = (failed or {}).get('error') or report.get('error') or {}
        result['failure'] = {key: failed[key] for key in ('step_index', 'command_id', 'skill_name')
                             if failed is not None and key in failed}
        result['failure'].update({key: error[key] for key in
            ('error_code', 'failed_stage', 'error_message', 'stage', 'message', 'type') if key in error})
    if 'final_state' in report:
        result['final_state_file'] = 'snapshots/state.json'
    return result


def write_command_result(path, entry, serializable):
    """Append and flush completed commands; replace repeated error writes in place."""
    path = Path(path)
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    repeated = any(row['step_index'] == entry['step_index'] for row in rows)
    if repeated:
        rows = [entry if row['step_index'] == entry['step_index'] else row for row in rows]
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, default=serializable) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    else:
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, default=serializable) + '\n')
            handle.flush()
            os.fsync(handle.fileno())


def write_metrics(path, entry, serializable):
    """Replace a repeated command entry rather than appending duplicate failures."""
    path = Path(path)
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    rows = [row for row in rows if row['command_id'] != entry['command_id']]
    rows.append(entry)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(''.join(json.dumps(row, default=serializable) + '\n' for row in rows), encoding='utf-8')
    temporary.replace(path)
