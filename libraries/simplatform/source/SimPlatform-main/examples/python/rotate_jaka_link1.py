"""Control JAKA ZU7 in the GUI through the Simulation Platform Python SDK."""

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
DEFAULT_ASSET = Path(
    r"D:\JakaDisplay\jaka_ros2\src\jaka_description\urdf\jaka_zu7.urdf"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load JAKA ZU7 and rotate Link_1 through the Python SDK."
    )
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--asset", type=Path, default=DEFAULT_ASSET)
    parser.add_argument("--joint", default="joint_1")
    parser.add_argument("--velocity-deg-s", type=float, default=30.0)
    parser.add_argument("--duration", type=float,
                        help="automatically close after this many wall-clock seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    velocity = math.radians(args.velocity_deg_s)
    try:
        with EditorSession.launch(args.editor) as simulator:
            robot = simulator.load_robot(args.asset)
            try:
                joint_index = robot.joint_names.index(args.joint)
            except ValueError:
                available = ", ".join(robot.joint_names)
                raise RuntimeError(f"joint {args.joint!r} was not found; available: {available}")

            targets = [0.0] * robot.joint_count
            targets[joint_index] = velocity
            robot.set_joint_continuous(args.joint)
            robot.command_joint_velocities(targets)
            simulator.start()

            print(f"Loaded through SDK: {args.asset}")
            print(f"Joint order: {', '.join(robot.joint_names)}")
            print(f"Commanded {args.joint}: {args.velocity_deg_s:g} deg/s ({velocity:.9f} rad/s)")
            print("Close the simulator window or press Ctrl+C to stop.")
            deadline = time.monotonic() + args.duration if args.duration else None
            while simulator.is_open:
                time.sleep(1.0)
                try:
                    positions, velocities, _ = robot.joint_state
                    render_fps, render_ms = simulator.performance
                    print(
                        f"t={simulator.simulation_time:7.3f}s  "
                        f"position={math.degrees(positions[joint_index]):8.3f} deg  "
                        f"velocity={math.degrees(velocities[joint_index]):7.3f} deg/s  "
                        f"render={render_fps:5.1f} FPS/{render_ms:5.1f} ms"
                    )
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                except (EditorRpcError, OSError, ConnectionError):
                    break
    except KeyboardInterrupt:
        print("Stopping simulator...")
    except (FileNotFoundError, RuntimeError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
