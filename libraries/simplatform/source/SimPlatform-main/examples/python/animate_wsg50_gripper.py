"""Load the saved JAKA scene and repeatedly open and close its WSG50 gripper."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "python" / "sim_platform"))

from editor import EditorRobot, EditorRpcError, EditorSession  # noqa: E402


DEFAULT_EDITOR = PROJECT_ROOT / "build" / "windows-gui" / "Release" / "sim_editor.exe"
DEFAULT_SCENE = PROJECT_ROOT / "example_scene" / "jaka_wsg50_d435_rgbd.simstate"
GRIPPER_NAME = "wsg50"
GRIPPER_JOINTS = ("left_finger_joint", "right_finger_joint")
OPEN_POSITION = 0.0
CLOSED_POSITION = -0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="seconds to wait after each open or close command (default: 0.5)",
    )
    parser.add_argument(
        "--suspend-sensors",
        action="store_true",
        help="suspend RGB/RGBD capture while animating the gripper",
    )
    return parser.parse_args()


def find_gripper(session: EditorSession) -> EditorRobot:
    matches = [
        item
        for item in session.scene_hierarchy()
        if item["name"] == GRIPPER_NAME and item["robot_root"]
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {GRIPPER_NAME!r} robot root, found {len(matches)}"
        )
    gripper = EditorRobot(session, int(matches[0]["entity"]), GRIPPER_JOINTS)
    if gripper.joint_state[0].shape != (len(GRIPPER_JOINTS),):
        raise RuntimeError("the WSG50 scene robot does not have the expected two joints")
    return gripper


def main() -> int:
    args = parse_args()
    if args.delay <= 0.0:
        print("delay must be positive", file=sys.stderr)
        return 2

    try:
        with EditorSession.launch(args.editor) as session:
            entity_count = session.load_state(args.scene)
            gripper = find_gripper(session)
            camera_sensors = [
                int(item["entity"])
                for item in session.scene_hierarchy()
                if item.get("camera_sensor", False)
            ]
            if args.suspend_sensors:
                for sensor in camera_sensors:
                    session.set_sensor_enabled(sensor, False)
            session.start()
            print(f"Loaded {args.scene} ({entity_count} entities)")
            if camera_sensors and args.suspend_sensors:
                print(f"Suspended {len(camera_sensors)} camera sensor(s) for GUI performance")
            print(f"Opening and closing WSG50 every {args.delay:g} seconds")
            print("Close the simulator window or press Ctrl+C to stop.")

            actions = (
                (CLOSED_POSITION, "closed"),
                (OPEN_POSITION, "open"),
            )
            action_index = 0
            action_delay = threading.Event()
            while session.is_open:
                position, label = actions[action_index]
                gripper.command_joint_positions(
                    [position] * gripper.joint_count, velocity_limit=0.2
                )
                print(f"WSG50: {label}")
                action_index = (action_index + 1) % len(actions)
                action_delay.wait(args.delay)
        return 0
    except KeyboardInterrupt:
        return 0
    except (EditorRpcError, FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
