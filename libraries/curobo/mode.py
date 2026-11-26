"""Switch the default planning mode used by task runner processes."""
from __future__ import annotations

import argparse
import json

from .client import PROJECT, start_persistent_daemon, stop_persistent_daemon

MODE_PATH = PROJECT / '.cache/curobo/mode.json'


def get_mode() -> str:
    try:
        value = json.loads(MODE_PATH.read_text(encoding='utf-8')).get('mode')
    except (FileNotFoundError, OSError, ValueError, AttributeError):
        value = None
    return value if value in {'cpu', 'gpu'} else 'cpu'


def set_mode(mode: str) -> str:
    mode = mode.lower()
    if mode not in {'cpu', 'gpu'}:
        raise ValueError('mode must be cpu or gpu')
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if mode == 'gpu':
        path = start_persistent_daemon()
        status = f'gpu mode enabled; cuRobo daemon ready: {path}'
    else:
        stop_persistent_daemon()
        status = 'cpu mode enabled; cuRobo daemon stopped'
    MODE_PATH.write_text(json.dumps({'mode': mode}, indent=2) + '\n', encoding='utf-8')
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('cpu', 'gpu', 'status'))
    args = parser.parse_args()
    print(f'planning mode: {get_mode()}' if args.mode == 'status' else set_mode(args.mode))


if __name__ == '__main__':
    main()
