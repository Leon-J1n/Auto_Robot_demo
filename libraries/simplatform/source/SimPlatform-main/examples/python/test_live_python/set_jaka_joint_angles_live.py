"""Command JAKA joint_1 through joint_6 in an existing Live session."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from sim_platform.live import (  # noqa: E402
    LiveControlError,
    LiveSession,
    connect,
    connect_named,
    list_instances,
)


DEFAULT_SESSION_NAME = "live-python-jaka-gui"
ARM_JOINTS = tuple(f"joint_{index}" for index in range(1, 7))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "angles_deg",
        metavar="DEG",
        nargs="+",
        type=float,
        help="one angle for all six joints, or six joint angles in degrees",
    )
    parser.add_argument(
        "--session",
        help="instance name, pid:<PID>, or tcp:// endpoint; defaults to session discovery",
    )
    return parser.parse_args()


def candidate_sessions(requested: str | None) -> list[str | int]:
    if requested:
        if requested.startswith("pid:") or "://" in requested:
            return [requested]
        return [requested]

    descriptors = sorted(
        list_instances(),
        key=lambda item: (
            item.get("instance_name") != DEFAULT_SESSION_NAME,
            -int(item.get("pid", 0)),
        ),
    )
    return [int(item["pid"]) for item in descriptors]


def find_jaka(requested: str | None):
    for candidate in candidate_sessions(requested):
        session: LiveSession | None = None
        try:
            if isinstance(candidate, str) and not (
                candidate.startswith("pid:") or "://" in candidate
            ):
                session = connect_named(candidate)
            else:
                session = connect(candidate)
            robot = session.require_robot("robot")
            names = robot.joint_names
            if all(name in names for name in ARM_JOINTS):
                return session, robot
        except (LiveControlError, OSError, ValueError):
            pass
        if session is not None:
            session.close()
    return None


def main() -> int:
    args = parse_args()
    if len(args.angles_deg) not in {1, 6}:
        raise SystemExit("provide either one angle or six joint angles")
    angles = args.angles_deg * 6 if len(args.angles_deg) == 1 else args.angles_deg
    found = find_jaka(args.session)
    if found is None:
        return 0

    session, robot = found
    try:
        names = robot.joint_names
        positions, _, _ = robot.joint_state
        targets = positions.tolist()
        for name, angle in zip(ARM_JOINTS, angles, strict=True):
            targets[names.index(name)] = math.radians(angle)
        robot.command_joint_positions(targets)
        session.start()
        print(
            "Commanded JAKA: "
            + ", ".join(
                f"{name}={angle:g} deg"
                for name, angle in zip(ARM_JOINTS, angles, strict=True)
            )
        )
        return 0
    except (LiveControlError, OSError, ValueError):
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
