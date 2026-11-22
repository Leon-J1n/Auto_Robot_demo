"""Load JAKA ZU7 in the GUI and command joint_1 through joint_6 in degrees."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from sim_platform import EditorRpcError, EditorSession  # noqa: E402


DEFAULT_EDITOR = PROJECT_ROOT / "build" / "windows-gui" / "Release" / "sim_editor.exe"
DEFAULT_ROBOT = Path(
    r"D:\JakaDisplay\jaka_ros2\src\jaka_description\urdf\jaka_zu7.urdf"
)
CONTROLLED_JOINTS = tuple(f"joint_{index}" for index in range(1, 7))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "angles_deg",
        metavar="DEG",
        nargs=6,
        type=float,
        help="joint_1 ... joint_6 target angles in degrees",
    )
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--robot", type=Path, default=DEFAULT_ROBOT)
    parser.add_argument("--speed-deg-s", type=float, default=45.0)
    parser.add_argument("--tolerance-deg", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--close-on-reached",
        action="store_true",
        help="close the GUI after all targets are reached",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.speed_deg_s <= 0.0 or args.tolerance_deg <= 0.0 or args.timeout <= 0.0:
        print("speed, tolerance, and timeout must be positive", file=sys.stderr)
        return 2

    try:
        with EditorSession.launch(args.editor) as simulator:
            robot = simulator.load_robot(args.robot)
            positions, _, _ = robot.joint_state
            targets = positions.tolist()
            for name, angle in zip(CONTROLLED_JOINTS, args.angles_deg, strict=True):
                try:
                    index = robot.joint_names.index(name)
                except ValueError:
                    raise RuntimeError(
                        f"{name} was not found; available joints: {', '.join(robot.joint_names)}"
                    ) from None
                targets[index] = math.radians(angle)

            robot.command_joint_positions(
                targets, velocity_limit=math.radians(args.speed_deg_s)
            )
            simulator.start()
            print(f"Loaded: {args.robot}")
            print("Joint order: " + ", ".join(robot.joint_names))
            print(
                "Targets: "
                + ", ".join(
                    f"{name}={angle:g} deg"
                    for name, angle in zip(CONTROLLED_JOINTS, args.angles_deg, strict=True)
                )
            )

            deadline = time.monotonic() + args.timeout
            reached = False
            while simulator.is_open and time.monotonic() < deadline:
                time.sleep(0.1)
                positions, _, _ = robot.joint_state
                errors = [
                    abs(math.degrees(positions[robot.joint_names.index(name)]) - angle)
                    for name, angle in zip(CONTROLLED_JOINTS, args.angles_deg, strict=True)
                ]
                if max(errors) <= args.tolerance_deg:
                    reached = True
                    break

            simulator.pause()
            positions, _, _ = robot.joint_state
            actual = [
                math.degrees(positions[robot.joint_names.index(name)])
                for name in CONTROLLED_JOINTS
            ]
            print("Actual: " + ", ".join(f"{name}={value:.3f} deg" for name, value in zip(CONTROLLED_JOINTS, actual, strict=True)))
            print("Result: " + ("TARGET REACHED" if reached else "TIMEOUT"))

            if reached and not args.close_on_reached:
                print("The simulation is paused. Close the GUI window or press Ctrl+C to exit.")
                while simulator.is_open:
                    time.sleep(0.25)
            return 0 if reached else 1
    except KeyboardInterrupt:
        return 0
    except (EditorRpcError, FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
