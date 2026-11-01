from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .live import LaunchSpec, LiveControlError, connect, connect_or_launch, launch


def _add_launch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", choices=["headless", "editor"], default="headless")
    parser.add_argument("--name")
    parser.add_argument("--listen", action="append", default=[])
    parser.add_argument("--advertise", action="append", default=[])
    parser.add_argument("--physics", choices=["none", "mujoco", "bullet3"], default="none")
    parser.add_argument("--collision",
                        choices=["fcl", "bullet3", "coal", "ifps-cpu", "ifps-gpu"],
                        default="fcl")
    parser.add_argument("--cache-directory")
    parser.add_argument("--registry-dir")
    parser.add_argument("--executable")
    parser.add_argument("--startup-timeout", type=float, default=15.0)


def _launch_spec(args: argparse.Namespace, *, detached: bool) -> LaunchSpec:
    return LaunchSpec(
        kind=args.kind,
        instance_name=args.name,
        listen=tuple(args.listen),
        advertise=tuple(args.advertise),
        detached=detached,
        executable=args.executable,
        physics=args.physics,
        collision=args.collision,
        cache_directory=args.cache_directory,
        registry_directory=args.registry_dir,
        startup_timeout=args.startup_timeout,
    )


def _run_script(path: str | os.PathLike[str], session: Any) -> None:
    source_path = Path(path).expanduser().resolve()
    source = source_path.read_text(encoding="utf-8")
    namespace = {
        "__file__": str(source_path),
        "__name__": "__main__",
        "sim": session,
        "load_robot": session.load_robot,
        "ensure_robot": session.ensure_robot,
        "require_robot": session.require_robot,
    }
    exec(compile(source, str(source_path), "exec"), namespace)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Python against a live simulation instance")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_parser = subparsers.add_parser("launch", help="start a new simulation instance")
    _add_launch_arguments(launch_parser)
    launch_parser.add_argument("--detach", action="store_true")
    launch_parser.add_argument("--workspace", default="default")

    run_parser = subparsers.add_parser("run", help="execute a Python file against an instance")
    run_parser.add_argument("script")
    connection = run_parser.add_mutually_exclusive_group(required=True)
    connection.add_argument("--connect")
    connection.add_argument("--launch-if-missing", action="store_true")
    run_parser.add_argument("--workspace", default="default")
    _add_launch_arguments(run_parser)

    args = parser.parse_args()
    try:
        if args.command == "launch":
            session = launch(_launch_spec(args, detached=args.detach), workspace=args.workspace)
            try:
                print(json.dumps({"pid_endpoint": f"pid:{session.pid}",
                                  "instance_id": session.instance_id,
                                  "endpoints": session.endpoints}, ensure_ascii=False))
            finally:
                session.close()
            return

        if args.connect:
            session = connect(args.connect, workspace=args.workspace,
                              registry_directory=args.registry_dir)
        else:
            if not args.name:
                parser.error("--name is required with --launch-if-missing")
            session = connect_or_launch(
                args.name, _launch_spec(args, detached=True), workspace=args.workspace
            )
        try:
            _run_script(args.script, session)
        finally:
            session.close()
    except (LiveControlError, OSError, ValueError) as error:
        parser.exit(1, f"simulator-python: {error}\n")


if __name__ == "__main__":
    main()
