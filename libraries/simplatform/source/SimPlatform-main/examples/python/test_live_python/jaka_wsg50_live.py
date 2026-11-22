"""Build and animate a JAKA ZU7 + WSG50 scene through Live Python Control."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from sim_platform.live import LaunchSpec, LiveControlError, LiveSession, launch  # noqa: E402


DEFAULT_EDITOR = PROJECT_ROOT / "build" / "windows-gui" / "Release" / "sim_editor.exe"
DEFAULT_ROBOT = Path(
    r"D:\JakaDisplay\jaka_ros2\src\jaka_description\urdf\jaka_zu7.urdf"
)
DEFAULT_GRIPPER = (
    PROJECT_ROOT / "examples" / "assets" / "grippers" / "wsg50" / "wsg50.urdf"
)
ARM_JOINTS = tuple(f"joint_{index}" for index in range(1, 7))
OPEN_POSITION = 0.0
CLOSED_POSITION = -0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("headless", "gui"), default="headless")
    parser.add_argument("--robot", type=Path, default=DEFAULT_ROBOT)
    parser.add_argument("--gripper", type=Path, default=DEFAULT_GRIPPER)
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument(
        "--cycles", type=int,
        help="number of gripper commands; defaults to 6 headless and unlimited in GUI",
    )
    parser.add_argument("--target-timeout", type=float, default=20.0)
    parser.add_argument("--tolerance-deg", type=float, default=1.0)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.interval <= 0.0 or args.target_timeout <= 0.0 or args.tolerance_deg <= 0.0:
        raise ValueError("interval, target timeout, and tolerance must be positive")
    if args.cycles is not None and args.cycles < 0:
        raise ValueError("cycles cannot be negative")
    for label, path in (("robot", args.robot), ("gripper", args.gripper)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} URDF was not found: {path}")
    if args.kind == "gui" and not args.editor.is_file():
        raise FileNotFoundError(f"editor executable was not found: {args.editor}")


def create_scene(sim: LiveSession, robot_path: Path, gripper_path: Path):
    base = sim.create_primitive("cube")
    base.set_scale((0.4, 0.4, 0.2))
    base.set_position((0.0, 0.0, -0.1))
    sim.objects.bind("base", base)

    robot = sim.ensure_robot(robot_path, alias="robot")
    names = robot.joint_names
    missing = [name for name in ARM_JOINTS if name not in names]
    if missing:
        raise RuntimeError(
            f"JAKA joints were not found: {', '.join(missing)}; available: {', '.join(names)}"
        )
    positions, _, _ = robot.joint_state
    targets = positions.tolist()
    for name in ARM_JOINTS:
        targets[names.index(name)] = math.radians(60.0)
    robot.command_joint_positions(targets)

    gripper = sim.ensure_robot(gripper_path, alias="gripper")
    if set(gripper.joint_names) != {"left_finger_joint", "right_finger_joint"}:
        raise RuntimeError(
            "WSG50 joints do not match left_finger_joint and right_finger_joint"
        )
    gripper.set_parent(robot.get_link("Link_6"), preserve_world=False)
    gripper.set_position((0.0, 0.0, 0.0))
    gripper.set_rotation((1.0, 0.0, 0.0, 0.0))
    return robot, gripper


def wait_for_arm(sim: LiveSession, robot, timeout: float, tolerance_deg: float) -> None:
    sim.start()
    names = robot.joint_names
    indices = [names.index(name) for name in ARM_JOINTS]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        positions, _, _ = robot.joint_state
        errors = [
            abs(math.degrees(positions[index]) - 60.0) for index in indices
        ]
        if max(errors) <= tolerance_deg:
            actual = [math.degrees(positions[index]) for index in indices]
            print("JAKA reached: " + ", ".join(f"{value:.2f} deg" for value in actual))
            return
        time.sleep(0.1)
    raise RuntimeError("JAKA did not reach 60 degrees before the target timeout")


def animate_gripper(gripper, interval: float, cycles: int) -> None:
    actions = ((CLOSED_POSITION, "closed"), (OPEN_POSITION, "open"))
    command_index = 0
    while cycles == 0 or command_index < cycles:
        position, label = actions[command_index % len(actions)]
        gripper.command_joint_positions([position] * gripper.joint_count)
        print(f"WSG50: {label}", flush=True)
        command_index += 1
        time.sleep(interval)


def main() -> int:
    args = parse_args()
    sim: LiveSession | None = None
    try:
        validate_args(args)
        cycles = args.cycles if args.cycles is not None else (6 if args.kind == "headless" else 0)
        spec = LaunchSpec(
            kind="headless" if args.kind == "headless" else "editor",
            instance_name=f"live-python-jaka-{args.kind}",
            listen=("tcp://127.0.0.1:0",),
            detached=True,
            executable=None if args.kind == "headless" else args.editor,
            physics="mujoco",
            startup_timeout=30.0,
        )
        sim = launch(spec)
        print(f"Connected to {args.kind} instance pid={sim.pid}: {sim.endpoint}")
        robot, gripper = create_scene(sim, args.robot.resolve(), args.gripper.resolve())
        print("Created Cube base and attached WSG50 to JAKA Link_6")
        wait_for_arm(sim, robot, args.target_timeout, args.tolerance_deg)
        animate_gripper(gripper, args.interval, cycles)
        if args.kind == "headless":
            sim.pause()
            status, simulation_time = sim.status()
            print(f"Headless verification complete: status={status}, time={simulation_time:.3f}s")
        return 0
    except KeyboardInterrupt:
        return 0
    except (FileNotFoundError, LiveControlError, OSError, RuntimeError, ValueError) as error:
        if args.kind == "gui" and isinstance(error, LiveControlError) and error.code == "connection_lost":
            return 0
        print(error, file=sys.stderr)
        return 2
    finally:
        if sim is not None:
            if args.kind == "headless":
                try:
                    sim.shutdown()
                except LiveControlError:
                    pass
            sim.close()


if __name__ == "__main__":
    raise SystemExit(main())
