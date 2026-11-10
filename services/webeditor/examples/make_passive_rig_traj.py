"""One clip that drives an arm and a passive URDF model at the same time (spec **Q107**).

    python -X utf8 examples/make_passive_rig_traj.py
    python -X utf8 examples/make_passive_rig_traj.py --out /tmp/passive_rig.traj

`docs/ur5e_passive_rig.json` hangs a microwave (OLS model 30, one `revolute`
`door_joint`) on the UR5e's wrist, and its driver says `role: "passive"`. The clip
has to carry **both** robots or it proves nothing: without the arm there is nothing
to show that a passive neighbour leaves the arm's decorations alone, and without the
microwave there is nothing to show that a withdrawn robot is still driven.

Four segments on one clock (pre-roll 30, motion 60, pause 30, so 390 frames):

| frames | object 2 (arm) | object 4 (microwave) |
| --- | --- | --- |
| 30-89 | `shoulder_pan_joint` to 0.70 | holds at 0.0 |
| 120-179 | holds | `door_joint` to -1.20 |
| 210-269 | `wrist_1_joint` to -0.90 | holds at -1.20 |
| 300-359 | holds | `door_joint` back to 0.0 |

The gaps hold the previous sample (§8.5), and that is what makes the two probe pairs
readable: **100 vs 179** changes only the door (the arm pose is identical, so a moved
box can only be the door link swinging), while **179 vs 269** changes only the arm
(the door angle is identical, so a moved box can only be the wrist carrying it).
Those are the numbers §12.15 ① and ④(a) read back through `/get2DAABB`.

`encode_clip()` comes from `make_scene3_variants.py`, whose packing takes the frame
count as a parameter - the lesson of that round (**坑 69**) was that a clip's total
frame count belongs to the clip, not to whichever script happens to pack it.
`read_back()` runs on the written bytes for the same reason: a check against
in-memory expectations could not catch a wrong header.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from make_scene3_traj import (
    ARM_HOME,
    GRIPPER_OPEN,
    POSE_DIM,
    ObjectTrack,
    ramp,
    read_back,
)
from make_scene3_variants import encode_clip

FPS = 60
PRE_ROLL = 30
MOTION = 60
PAUSE = 30
SEGMENT_COUNT = 4

ARM_ID = 2
PASSIVE_ID = 4
PASSIVE_PARENT = ARM_ID

PAN_SLOT = 0
WRIST_1_SLOT = 3
DOOR_SLOT = 0
PAN_TARGET = 0.70
WRIST_1_TARGET = -0.90
# `microwave.urdf` limits `door_joint` to [-1.5707963, 0], so opening the door is
# negative - a target outside that range would be clamped and the pair would measure
# the same pose twice.
DOOR_OPEN = -1.20

DEFAULT_OUT = Path(__file__).resolve().parent / "passive_rig.traj"


def segment_start(step: int) -> int:
    return PRE_ROLL + (step - 1) * (MOTION + PAUSE)


TOTAL_FRAMES = segment_start(SEGMENT_COUNT) + MOTION + PAUSE

# The frames §12.15 samples, each one a gap so the pose is the last written sample.
PAN_END = segment_start(1) + MOTION - 1
PAN_AND_DOOR_CLOSED = PAN_END + 11
DOOR_ONLY = segment_start(2) + MOTION - 1
ARM_ONLY = segment_start(3) + MOTION - 1


def arm_frame(values: list[float]) -> list[float]:
    return values + [GRIPPER_OPEN]


def door_frame(angle: float) -> list[float]:
    pose = [0.0] * POSE_DIM
    pose[DOOR_SLOT] = angle
    return pose


def build() -> bytes:
    """Two tracks, one clock, no Attach table: the mount is in the world file."""
    straight = list(ARM_HOME)
    panning = list(ARM_HOME)
    panning[PAN_SLOT] = PAN_TARGET
    bent = list(panning)
    bent[WRIST_1_SLOT] = WRIST_1_TARGET

    arm = ObjectTrack(ARM_ID, None, arm_frame(straight))
    arm.add(segment_start(1), ramp(arm_frame(straight), arm_frame(panning), MOTION), 1)
    arm.add(segment_start(3), ramp(arm_frame(panning), arm_frame(bent), MOTION), 3)

    door = ObjectTrack(PASSIVE_ID, PASSIVE_PARENT, door_frame(0.0))
    door.add(segment_start(2), ramp(door_frame(0.0), door_frame(DOOR_OPEN), MOTION), 2)
    door.add(segment_start(4), ramp(door_frame(DOOR_OPEN), door_frame(0.0), MOTION), 4)

    return encode_clip([arm, door], [], TOTAL_FRAMES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the passive rig trajectory")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    blob = build()
    args.out.write_bytes(blob)
    print(f"passive_rig.traj - fps {FPS}, total_frames {TOTAL_FRAMES} "
          f"({TOTAL_FRAMES / FPS:.1f} s), {SEGMENT_COUNT} steps on a "
          f"{PRE_ROLL}/{MOTION}/{PAUSE} clock")
    print(f"  object {ARM_ID}: shoulder_pan {PAN_TARGET:+.2f} at frames "
          f"{segment_start(1)}-{PAN_END}, wrist_1 {WRIST_1_TARGET:+.2f} at frames "
          f"{segment_start(3)}-{ARM_ONLY}")
    print(f"  object {PASSIVE_ID}: door_joint {DOOR_OPEN:+.2f} at frames "
          f"{segment_start(2)}-{DOOR_ONLY}, back to 0.00 at frames {segment_start(4)}-")
    print(f"  probe pairs: door only {PAN_AND_DOOR_CLOSED} vs {DOOR_ONLY}, "
          f"arm only {DOOR_ONLY} vs {ARM_ONLY}")
    print(f"wrote {args.out} ({len(blob)} bytes)\n")
    read_back(blob)


if __name__ == "__main__":
    main()
