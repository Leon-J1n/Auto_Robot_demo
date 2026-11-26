"""Manual lifecycle commands for the local cuRobo planning daemon."""
from __future__ import annotations

import argparse

from .client import start_persistent_daemon, stop_persistent_daemon


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop'))
    parser.add_argument('--cache-dir', default=None)
    args = parser.parse_args()
    config = {'cache_dir': args.cache_dir} if args.cache_dir else {}
    if args.action == 'start':
        path = start_persistent_daemon(config)
        print(f'cuRobo daemon started: {path}')
    else:
        stopped = stop_persistent_daemon(config)
        print('cuRobo daemon stopped' if stopped else 'cuRobo daemon was not running')


if __name__ == '__main__':
    main()
