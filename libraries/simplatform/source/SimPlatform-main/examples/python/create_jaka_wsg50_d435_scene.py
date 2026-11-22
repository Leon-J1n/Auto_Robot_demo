"""Create and save the JAKA ZU7 + WSG50 + D435 RGBD test scene."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from sim_platform.editor import EditorSession  # noqa: E402


DEFAULT_EDITOR = ROOT / "build" / "windows-gui" / "Release" / "sim_editor.exe"
DEFAULT_JAKA = Path(r"D:\JakaDisplay\jaka_ros2\src\jaka_description\urdf\jaka_zu7.urdf")
DEFAULT_GRIPPER = ROOT / "examples" / "assets" / "grippers" / "wsg50" / "wsg50.urdf"
DEFAULT_D435 = ROOT / "examples" / "assets" / "sensors" / "intel_d435" / "intel_d435.urdf"
DEFAULT_OUTPUT = ROOT / "example_scene" / "jaka_wsg50_d435_rgbd.simstate"


def entity_named(session: EditorSession, name: str, *, under: int | None = None) -> int:
    matches = [item for item in session.scene_hierarchy()
               if item["name"] == name and (under is None or item["parent"] == under)]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {name!r} entity, found {len(matches)}")
    return int(matches[0]["entity"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--robot", type=Path, default=DEFAULT_JAKA)
    parser.add_argument("--gripper", type=Path, default=DEFAULT_GRIPPER)
    parser.add_argument("--camera-model", type=Path, default=DEFAULT_D435)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    with EditorSession.launch(args.editor) as editor:
        robot = editor.load_robot(args.robot)
        link6 = entity_named(editor, "Link_6")

        gripper = editor.load_asset(args.gripper)
        editor.set_parent(gripper, link6)
        editor.set_transform(
            gripper, translation=(0.0, 0.0, 0.0), rotation_wxyz=(1.0, 0.0, 0.0, 0.0)
        )

        d435 = editor.load_asset(args.camera_model)
        editor.set_parent(d435, gripper)
        editor.set_transform(
            d435, translation=(0.0, 0.04301, 0.02187),
            rotation_wxyz=(-0.5000000000000001, 0.5000000000000001,
                           0.5000000000000001, 0.5000000000000001),
        )

        sensor = editor.create_camera(
            d435, output="rgbd", frame_id="d435_depth_optical_frame",
            update_rate_hz=30.0, width=848, height=480,
            vertical_fov_radians=math.radians(58.0), near_plane=0.1, far_plane=10.0,
        )
        editor.set_transform(
            sensor, translation=(0.014, 0.0, 0.0), rotation_wxyz=(1.0, 0.0, 0.0, 0.0)
        )
        output = editor.save_state(args.output)
        print(f"Saved test scene: {output}")
        print(f"JAKA={robot.handle}, Link_6={link6}, WSG50={gripper}, D435={d435}, RGBD={sensor}")


if __name__ == "__main__":
    main()
