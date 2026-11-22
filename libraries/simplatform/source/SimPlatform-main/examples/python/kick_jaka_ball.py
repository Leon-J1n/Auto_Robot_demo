"""Build a GUI scene through the Python SDK and let JAKA ZU7 kick a ball."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--robot", type=Path, default=DEFAULT_ROBOT)
    parser.add_argument("--duration", type=float, default=3.5)
    parser.add_argument("--velocity-deg-s", type=float, default=60.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration <= 0.0:
        raise ValueError("duration must be positive")

    try:
        with EditorSession.launch(args.editor) as simulator:
            floor = simulator.create_primitive("plane")
            simulator.set_transform(
                floor, translation=(0.0, 0.0, -0.01), scale=(10.0, 10.0, 1.0)
            )
            simulator.set_rigid_body(
                floor, enabled=True, dynamic=False, gravity=False, mass=1.0
            )

            robot = simulator.load_robot(args.robot)
            simulator.set_transform(robot.handle, translation=(0.0, 0.0, 0.0))

            ball = simulator.create_primitive("sphere")
            simulator.set_transform(ball, translation=(0.65, 0.72, 0.5))
            backend = simulator.set_rigid_body(
                ball,
                enabled=True,
                dynamic=True,
                gravity=True,
                mass=1.0,
                restitution=0.5,
                friction=(0.4, 0.005, 0.001),
            )

            joint_index = robot.joint_names.index("joint_1")
            targets = [0.0] * robot.joint_count
            targets[joint_index] = math.radians(args.velocity_deg_s)
            robot.set_joint_continuous("joint_1")
            robot.command_joint_velocities(targets)

            initial = simulator.get_transform(ball)[0]
            simulator.start()
            print(f"Physics backend: {backend}")
            print(f"JAKA source: {args.robot}")
            print("Floor: 10 m x 10 m; ball restitution: 0.5")
            print(f"Initial ball position: {initial}")

            deadline = time.monotonic() + args.duration
            while simulator.is_open and time.monotonic() < deadline:
                time.sleep(0.5)
                position = simulator.get_transform(ball)[0]
                displacement = math.hypot(position[0] - initial[0], position[1] - initial[1])
                print(
                    f"t={simulator.simulation_time:6.3f}s  ball={position}  "
                    f"horizontal displacement={displacement:.3f} m"
                )

            simulator.pause()
            final = simulator.get_transform(ball)[0]
            displacement = math.hypot(final[0] - initial[0], final[1] - initial[1])
            print(f"Final ball position: {final}")
            print(f"Result: {'KICKED' if displacement > 0.15 else 'NOT KICKED'} ({displacement:.3f} m)")
            return 0 if displacement > 0.15 else 1
    except (EditorRpcError, FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
