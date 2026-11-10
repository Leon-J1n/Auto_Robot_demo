"""Build a TRAJ v2 clip for scene 3 (`ur5e_table_sorting`, scene_id 3): nine segments.

    python -X utf8 examples/make_scene3_traj.py
    python -X utf8 examples/make_scene3_traj.py --out /tmp/scene3.traj

The nine segments are the arm's, in the order the plan asks for: six joints each
turned once, the gripper closing, `shoulder_pan_joint` turned again, the gripper
opening. The apple is mounted on the gripper when segment 7 ends and unmounted
when segment 9 starts.

Object ids, joint names and the apple's home position are the constants scene 3
declares; they are written down here rather than read from the library, so the
clip stays reproducible with no server running.

Two supporting sets of records exist because of how the format is defined:

* A trajectory frame carries six joint angles plus one gripper value, and
  `drivers.ts:149` only *records* that seventh value - no world file names a
  gripper joint for it. So the closing and opening are actually driven by two
  segments on object 3, whose `joint_sequence` is the single
  `robotiq_2f85_right_driver_joint` (URDF travel 0.0 .. 0.8).
* `attach` only re-parents a node, and a frame's position is always local to
  whatever the parent is at that moment (`drivers.ts:104`). Left without its own
  samples, the apple would keep its 0.38 / -0.22 / 0.64 home offset *inside the
  gripper frame* and appear to float away from the fingers, which reads like a
  broken mount rather than a working one. So object 7 gets one segment that parks
  it on the grasp point while it hangs there, and one that puts its home position
  back after the unmount.

The grasp point is the centre between the two silicone pad faces, in the frame of
the link the apple is re-parented onto. `drivers.ts:attachTarget` resolves through
`urdf.ts:endEffectorNode` to the child link of the last joint named in
`joint_sequence`, which for this gripper is `robotiq_2f85_right_driver`. Walking
the real URDF with the `<mimic>` chain at the closed value puts that centre at
`(0, 0, 0.164287)` in `robotiq_2f85_mount`, i.e. `(0, -0.017507, 0.101727)` in the
driver link (spec **Q89**). The closed value itself follows from the same walk:
the pad faces are 85.40 mm apart at 0.0 and 8.64 mm at 0.72, while `apple.obj` is
74.66 x 74.73 x 71.25 mm, so 0.130556 is the angle that closes *onto* the apple
instead of through it.

Frames are absolute on one shared clock, and poses are interpolated between the
key poses with the same `smoothstep` the v2 layout document describes.
"""
from __future__ import annotations
import argparse
import math
import struct
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent / "scene3_9_segments.traj"

FPS = 60
MOTION_FRAMES = 60
PAUSE_FRAMES = 30
PRE_ROLL_FRAMES = 30
POSE_DIM = 7
WORLD_ROOT = 0xFFFFFFFF

ARM_ID = 2
GRIPPER_ID = 3
APPLE_ID = 7
ARM_PARENT = None
GRIPPER_PARENT = ARM_ID
APPLE_PARENT = None

ARM_HOME = [0.0, -2.094395, 1.570796, -1.5707963, -1.5707963, 0.0]
APPLE_HOME = [0.38, -0.22, 0.637373, 0.0, 0.0, 0.0, 1.0]
APPLE_IN_GRIPPER = [0.0, -0.017507, 0.101727, 0.0, 0.0, 0.0, 1.0]
GRIPPER_OPEN = 0.0
GRIPPER_CLOSED = 0.130556

# Segment i (1-based) runs for MOTION_FRAMES starting here, then a pause follows it.
def segment_start(step: int) -> int:
    return PRE_ROLL_FRAMES + (step - 1) * (MOTION_FRAMES + PAUSE_FRAMES)


TOTAL_FRAMES = segment_start(9) + MOTION_FRAMES + PAUSE_FRAMES
ATTACH_FRAME = segment_start(7) + MOTION_FRAMES
DETACH_FRAME = segment_start(9)

# The joint each of the first six segments turns, and the angle it turns to.
JOINT_MOVES = [
    (0, "shoulder_pan_joint", 0.90),
    (1, "shoulder_lift_joint", -1.60),
    (2, "elbow_joint", 2.40),
    (3, "wrist_1_joint", -0.80),
    (4, "wrist_2_joint", 1.10),
    (5, "wrist_3_joint", 1.60),
]
PANOISE_JOINT = 0
PANOISE_TARGET = 2.20


def smoothstep(t: float) -> float:
    clamped = min(max(t, 0.0), 1.0)
    return clamped * clamped * (3.0 - 2.0 * clamped)


def ramp(frm: list[float], to: list[float], frames: int) -> list[float]:
    """`frames` samples gliding from `frm` to `to`, endpoints included, first sample == frm."""
    out: list[float] = []
    for index in range(frames):
        t = smoothstep(index / max(1, frames - 1))
        out.extend(frm[i] + (to[i] - frm[i]) * t for i in range(len(to)))
    return out


class ObjectTrack:
    """One Index record: a default pose plus the segments that overwrite it.

    `parent_id` of None is the scene root, which the format writes as 0xFFFFFFFF.
    """

    def __init__(self, object_id: int, parent_id: int | None, default_pose: list[float]) -> None:
        self.object_id = object_id
        self.parent_id = parent_id
        self.default_pose = default_pose
        self.segments: list[tuple[int, int, int, list[float]]] = []

    def add(self, start_frame: int, poses: list[float], step: int) -> None:
        frame_count = len(poses) // POSE_DIM
        self.segments.append((start_frame, frame_count, step, poses))


def build_arm() -> ObjectTrack:
    arm = ObjectTrack(ARM_ID, ARM_PARENT, ARM_HOME + [GRIPPER_OPEN])
    joints = list(ARM_HOME)
    gripper = GRIPPER_OPEN
    for step, (index, _joint_name, target) in enumerate(JOINT_MOVES, start=1):
        start = list(joints)
        start[index] = target
        arm.add(segment_start(step), ramp(joints + [gripper], start + [gripper], MOTION_FRAMES), step)
        joints = start
    # Segment 7: the arm holds while the recorded gripper value closes.
    arm.add(segment_start(7), ramp(joints + [gripper], joints + [GRIPPER_CLOSED], MOTION_FRAMES), 7)
    gripper = GRIPPER_CLOSED
    # Segment 8: shoulder_pan again, this time carrying the mounted apple.
    panned = list(joints)
    panned[PANOISE_JOINT] = PANOISE_TARGET
    arm.add(segment_start(8), ramp(joints + [gripper], panned + [gripper], MOTION_FRAMES), 8)
    joints = panned
    # Segment 9: the arm holds while the recorded gripper value opens.
    arm.add(segment_start(9), ramp(joints + [gripper], joints + [GRIPPER_OPEN], MOTION_FRAMES), 9)
    return arm


def build_gripper() -> ObjectTrack:
    """The fingers themselves: object 3 drives one joint, slots 1..5 of the frame stay empty."""
    gripper = ObjectTrack(GRIPPER_ID, GRIPPER_PARENT, [GRIPPER_OPEN] * POSE_DIM)
    for step, (frm, to) in ((7, (GRIPPER_OPEN, GRIPPER_CLOSED)), (9, (GRIPPER_CLOSED, GRIPPER_OPEN))):
        frame = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, frm]
        end = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, to]
        frame[0] = frm
        end[0] = to
        gripper.add(segment_start(step), ramp(frame, end, MOTION_FRAMES), step)
    return gripper


def hold(pose: list[float], frames: int) -> list[float]:
    """`frames` identical samples - the phase where an object simply stays put."""
    return list(pose) * frames


def build_apple() -> ObjectTrack:
    apple = ObjectTrack(APPLE_ID, APPLE_PARENT, APPLE_HOME)
    # Held between the pads for as long as it hangs there, pause included. An attach event
    # re-parents the node and nothing else, so the pose is a constant: a ramp here would
    # animate the apple sliding into the gripper rather than being caught.
    apple.add(ATTACH_FRAME, hold(APPLE_IN_GRIPPER, DETACH_FRAME - ATTACH_FRAME), 8)
    # Unmounted at DETACH_FRAME: back to the home position, read against the scene root.
    apple.add(DETACH_FRAME, hold(APPLE_HOME, MOTION_FRAMES), 9)
    return apple


def encode(objects: list[ObjectTrack], attaches: list[tuple[int, int, int | None]]) -> bytes:
    index_offset = 32
    attach_offset = index_offset + sum(12 + POSE_DIM * 4 + 16 * len(o.segments) for o in objects)
    data_offset = attach_offset + 16 * len(attaches)

    index_part = bytearray()
    data_part = bytearray()
    for object_track in objects:
        index_part += struct.pack("<IIHH", object_track.object_id,
                                  WORLD_ROOT if object_track.parent_id is None else object_track.parent_id,
                                  POSE_DIM, len(object_track.segments))
        index_part += struct.pack(f"<{POSE_DIM}f", *object_track.default_pose)
        for start_frame, frame_count, step, poses in object_track.segments:
            absolute = data_offset + len(data_part)
            index_part += struct.pack("<IIII", start_frame, frame_count, absolute, step)
            data_part += struct.pack(f"<{len(poses)}f", *poses)

    header = struct.pack("<8I", 0x5452414A, 2, FPS, TOTAL_FRAMES, len(objects), len(attaches), index_offset, attach_offset)
    attach_part = b"".join(struct.pack("<IIII", frame, child, WORLD_ROOT if parent is None else parent, 0)
                           for frame, child, parent in attaches)
    return header + bytes(index_part) + attach_part + bytes(data_part)


def read_back(blob: bytes) -> None:
    """Re-parse what was written the way the layout document says to, and print the tables."""
    magic, version, fps, total, objects, attaches, index_at, attach_at = struct.unpack_from("<8I", blob, 0)
    print(f"magic 0x{magic:08X} version {version} fps {fps} total_frames {total} "
          f"objects {objects} attaches {attaches}")
    print(f"index_offset {index_at} attach_offset {attach_at} "
          f"data_offset {attach_at + 16 * attaches} file_bytes {len(blob)}")
    cursor = index_at
    for _ in range(objects):
        object_id, parent_id, pose_dim, segment_count = struct.unpack_from("<IIHH", blob, cursor)
        cursor += 12
        default = struct.unpack_from(f"<{pose_dim}f", blob, cursor)
        cursor += pose_dim * 4
        print(f"\nobject {object_id} parent {parent_id} pose_dim {pose_dim} segments {segment_count}")
        print(f"  default {', '.join(f'{v:.5f}' for v in default)}")
        for index in range(segment_count):
            start, count, offset, step = struct.unpack_from("<IIII", blob, cursor)
            cursor += 16
            first = struct.unpack_from(f"<{POSE_DIM}f", blob, offset)
            last = struct.unpack_from(f"<{POSE_DIM}f", blob, offset + (count - 1) * POSE_DIM * 4)
            print(f"  step {step}: frames {start}-{start + count - 1} ({count}) bytes {offset}+"
                  f"{count * POSE_DIM * 4}\n    first {', '.join(f'{v:.4f}' for v in first)}"
                  f"\n    last  {', '.join(f'{v:.4f}' for v in last)}")
    assert cursor == attach_at, f"index region ends at {cursor} but attach_offset is {attach_at}"
    for index in range(attaches):
        frame, child, parent, _ = struct.unpack_from("<IIII", blob, attach_at + 16 * index)
        print(f"\nattach {index}: frame {frame} child {child} parent {'scene root' if parent == WORLD_ROOT else parent}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the nine-segment scene 3 trajectory")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    blob = encode([build_arm(), build_gripper(), build_apple()],
                  [(ATTACH_FRAME, APPLE_ID, GRIPPER_ID), (DETACH_FRAME, APPLE_ID, None)])
    args.out.write_bytes(blob)
    print(f"scene 3 / nine segments - fps {FPS}, total_frames {TOTAL_FRAMES} "
          f"({TOTAL_FRAMES / FPS:.1f} s), attach at {ATTACH_FRAME}, detach at {DETACH_FRAME}")
    print(f"wrote {args.out} ({len(blob)} bytes)\n")
    read_back(blob)


if __name__ == "__main__":
    main()
