"""Two more scene 3 clips, built to be told apart from `scene3_9_segments.traj` at a glance.

    python -X utf8 examples/make_scene3_variants.py
    python -X utf8 examples/make_scene3_variants.py --out-dir /tmp

Both files run the same skeleton as `make_scene3_traj.py` - nine segments on the
same three objects of scene 3 (`2=UR5eArm`, `3=Robotiq2F85Gripper`, `7=Apple`), the
gripper closing in segment 7 and opening in segment 9, the apple mounted on the
gripper when segment 7 ends and unmounted when segment 9 starts. What differs is
the three things a viewer actually reads off the screen (spec **Q103**):

* `scene3_wrist_first.traj` (840 frames) turns the joints **distal to proximal**
  (`wrist_3, wrist_2, wrist_1, elbow, shoulder_lift, shoulder_pan`) to
  `1.35, -0.85, 0.60, 1.25, -0.95, 0.55`, and re-pannes `shoulder_pan` to `1.80`
  in segment 8. The original clip goes proximal to distal to
  `0.90, -1.60, 2.40, -0.80, 1.10, 1.60` and re-pannes to `2.20`, so the arm
  unwinds in the opposite direction and reaches visibly different poses.
* `scene3_alternate_joints.traj` (610 frames) alternates **far, near, far, near**
  (`elbow, wrist_1, shoulder_pan, wrist_3, shoulder_lift, wrist_2`) to
  `-1.85, 0.95, -0.65, 1.45, 0.70, -1.20`, re-pannes to `-1.20`, and shortens the
  clock too (`PRE_ROLL 25 / MOTION 45 / PAUSE 20` instead of `30 / 60 / 30`).
  The different length is the point: the toolbar dropdown labels a locally loaded
  clip by file name and frame count (**Q100**), so two clips of the same length
  would only be distinguishable by name.

Timing is the only constant the second clip changes, and it moves the whole
framework with it: `segment_start(i) = PRE_ROLL + (i - 1) * (MOTION + PAUSE)`, so
attach lands on 460 and detach on 545 rather than 630 and 750.

`make_scene3_traj.py` is imported, not edited: it is the source of
`scene3_9_segments.traj`, which three already-published verifications read numbers
out of (11 timeline ticks in §12.4b, the pixel diffs of §12.12, the 85 waypoints of
§12.13). The reuse boundary is "everything that does not know about a particular
clip": the track class, the interpolation helpers, the object ids, the home poses
and the reader that prints them back. The header packing is *not* on that list -
`make_scene3_traj.encode()` writes its own module-level `TOTAL_FRAMES` into word 4,
so a clip of a different length needs its own `encode_clip()` (see below).

Like the original, this script re-reads what it wrote and prints the tables, so
generating and checking are one command.
"""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

from make_scene3_traj import (
    APPLE_HOME,
    APPLE_ID,
    APPLE_IN_GRIPPER,
    APPLE_PARENT,
    ARM_HOME,
    ARM_ID,
    ARM_PARENT,
    GRIPPER_CLOSED,
    GRIPPER_ID,
    GRIPPER_OPEN,
    GRIPPER_PARENT,
    POSE_DIM,
    WORLD_ROOT,
    ObjectTrack,
    hold,
    ramp,
    read_back,
)

FPS = 60
SEGMENT_COUNT = 9
PANOISE_JOINT = 0

MAGIC = 0x5452414A
VERSION = 2
INDEX_OFFSET = 32

DEFAULT_OUT_DIR = Path(__file__).resolve().parent


def encode_clip(objects: list[ObjectTrack], attaches: list[tuple[int, int, int | None]], total_frames: int) -> bytes:
    """The v2 layout again, but with this clip's own frame count in the header.

    `make_scene3_traj.encode()` is otherwise identical; it simply cannot be reused
    because `TOTAL_FRAMES` is baked into it as a module constant.
    """
    attach_offset = INDEX_OFFSET + sum(12 + POSE_DIM * 4 + 16 * len(track.segments) for track in objects)
    data_offset = attach_offset + 16 * len(attaches)

    index_part = bytearray()
    data_part = bytearray()
    for track in objects:
        index_part += struct.pack("<IIHH", track.object_id,
                                  WORLD_ROOT if track.parent_id is None else track.parent_id,
                                  POSE_DIM, len(track.segments))
        index_part += struct.pack(f"<{POSE_DIM}f", *track.default_pose)
        for start_frame, frame_count, step, poses in track.segments:
            index_part += struct.pack("<IIII", start_frame, frame_count, data_offset + len(data_part), step)
            data_part += struct.pack(f"<{len(poses)}f", *poses)

    header = struct.pack("<8I", MAGIC, VERSION, FPS, total_frames, len(objects), len(attaches),
                         INDEX_OFFSET, attach_offset)
    attach_part = b"".join(struct.pack("<IIII", frame, child, WORLD_ROOT if parent is None else parent, 0)
                           for frame, child, parent in attaches)
    return header + bytes(index_part) + attach_part + bytes(data_part)


class Clip:
    """A named recipe: the six joint moves, the segment-8 re-pan, and the clock."""

    def __init__(
        self,
        filename: str,
        moves: list[tuple[int, str, float]],
        panoise_target: float,
        pre_roll: int,
        motion: int,
        pause: int,
    ) -> None:
        self.filename = filename
        self.moves = moves
        self.panoise_target = panoise_target
        self.pre_roll = pre_roll
        self.motion = motion
        self.pause = pause

    def segment_start(self, step: int) -> int:
        return self.pre_roll + (step - 1) * (self.motion + self.pause)

    @property
    def total_frames(self) -> int:
        return self.segment_start(SEGMENT_COUNT) + self.motion + self.pause

    @property
    def attach_frame(self) -> int:
        return self.segment_start(7) + self.motion

    @property
    def detach_frame(self) -> int:
        return self.segment_start(9)

    def build(self) -> bytes:
        objects = [self._arm(), self._gripper(), self._apple()]
        attaches = [(self.attach_frame, APPLE_ID, GRIPPER_ID), (self.detach_frame, APPLE_ID, None)]
        return encode_clip(objects, attaches, self.total_frames)

    def _arm(self) -> ObjectTrack:
        arm = ObjectTrack(ARM_ID, ARM_PARENT, ARM_HOME + [GRIPPER_OPEN])
        joints = list(ARM_HOME)
        gripper = GRIPPER_OPEN
        for step, (index, _joint_name, target) in enumerate(self.moves, start=1):
            moved = list(joints)
            moved[index] = target
            arm.add(self.segment_start(step), ramp(joints + [gripper], moved + [gripper], self.motion), step)
            joints = moved
        # Segment 7: the arm holds while the recorded gripper value closes.
        arm.add(self.segment_start(7), ramp(joints + [gripper], joints + [GRIPPER_CLOSED], self.motion), 7)
        gripper = GRIPPER_CLOSED
        # Segment 8: shoulder_pan again, this time carrying the mounted apple.
        panned = list(joints)
        panned[PANOISE_JOINT] = self.panoise_target
        arm.add(self.segment_start(8), ramp(joints + [gripper], panned + [gripper], self.motion), 8)
        joints = panned
        # Segment 9: the arm holds while the recorded gripper value opens.
        arm.add(self.segment_start(9), ramp(joints + [gripper], joints + [GRIPPER_OPEN], self.motion), 9)
        return arm

    def _gripper(self) -> ObjectTrack:
        """Object 3 drives its one joint; slots 1..5 of the frame stay empty."""
        gripper = ObjectTrack(GRIPPER_ID, GRIPPER_PARENT, [GRIPPER_OPEN] * POSE_DIM)
        for step, (frm, to) in (
            (7, (GRIPPER_OPEN, GRIPPER_CLOSED)),
            (9, (GRIPPER_CLOSED, GRIPPER_OPEN)),
        ):
            frame = [0.0] * POSE_DIM
            frame[0] = frm
            frame[6] = frm
            end = [0.0] * POSE_DIM
            end[0] = to
            end[6] = to
            gripper.add(self.segment_start(step), ramp(frame, end, self.motion), step)
        return gripper

    def _apple(self) -> ObjectTrack:
        apple = ObjectTrack(APPLE_ID, APPLE_PARENT, APPLE_HOME)
        # Parked on the grasp point for as long as it hangs there, pause included, then
        # back at its home position once the unmount re-parents it to the scene root.
        apple.add(self.attach_frame, hold(APPLE_IN_GRIPPER, self.detach_frame - self.attach_frame), 8)
        apple.add(self.detach_frame, hold(APPLE_HOME, self.motion), 9)
        return apple


WRIST_FIRST = Clip(
    filename="scene3_wrist_first.traj",
    moves=[
        (5, "wrist_3_joint", 1.35),
        (4, "wrist_2_joint", -0.85),
        (3, "wrist_1_joint", 0.60),
        (2, "elbow_joint", 1.25),
        (1, "shoulder_lift_joint", -0.95),
        (0, "shoulder_pan_joint", 0.55),
    ],
    panoise_target=1.80,
    pre_roll=30,
    motion=60,
    pause=30,
)

ALTERNATE_JOINTS = Clip(
    filename="scene3_alternate_joints.traj",
    moves=[
        (2, "elbow_joint", -1.85),
        (3, "wrist_1_joint", 0.95),
        (0, "shoulder_pan_joint", -0.65),
        (5, "wrist_3_joint", 1.45),
        (1, "shoulder_lift_joint", 0.70),
        (4, "wrist_2_joint", -1.20),
    ],
    panoise_target=-1.20,
    pre_roll=25,
    motion=45,
    pause=20,
)

CLIPS = [WRIST_FIRST, ALTERNATE_JOINTS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the two variant scene 3 trajectories")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    for clip in CLIPS:
        blob = clip.build()
        out = args.out_dir / clip.filename
        out.write_bytes(blob)
        print(
            f"{clip.filename} - fps {FPS}, total_frames {clip.total_frames} "
            f"({clip.total_frames / FPS:.1f} s), attach at {clip.attach_frame}, "
            f"detach at {clip.detach_frame}"
        )
        print(
            "  order " + ", ".join(name for _index, name, _target in clip.moves)
            + f", targets " + ", ".join(f"{target:+.2f}" for _index, _name, target in clip.moves)
        )
        print(f"wrote {out} ({len(blob)} bytes)\n")
        read_back(blob)
        print()


if __name__ == "__main__":
    main()
