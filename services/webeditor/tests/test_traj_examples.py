"""Example clips in `examples/`, read straight off the v2 layout.

The browser owns decoding, so nothing here is a second implementation of it - these cases
hold the *artifacts* to what the spec says they are: which joint each of the first six
segments turns, to what angle, how long the clip is and on which frames the apple is
mounted and released. That is precisely what makes the three scene 3 clips distinguishable
on screen, and it is what a caller reads out of a file it is about to push.

The last three cases belong to `passive_rig.traj` (**Q107**), the one clip that carries a
`role: "passive"` robot: it has to name both robots and put each segment where the two
probe pairs of §12.15 land in a *gap*, because "the arm pose is the same on both frames"
is a statement about the layout, not about a second sampler.

The earlier cases run the generator into a temp directory and compare bytes, so the
committed files cannot drift from `examples/make_scene3_variants.py`.
"""
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

TRAJ_MAGIC = 0x5452414A
TRAJ_VERSION = 2
WORLD_ROOT = 0xFFFFFFFF

# (object_id, pose_dim, segments) plus the two attach events, read from each file.
PUBLISHED = "scene3_9_segments.traj"
WRIST_FIRST = "scene3_wrist_first.traj"
ALTERNATE_JOINTS = "scene3_alternate_joints.traj"
PASSIVE_RIG = "passive_rig.traj"


def read_clip(name: str) -> dict:
    """The v2 layout of §8, unpacked far enough to answer 'which joint moved when'."""
    blob = (EXAMPLES / name).read_bytes()
    magic, version, fps, total_frames, object_count, attach_count, index_at, attach_at = struct.unpack_from("<8I", blob, 0)
    assert magic == TRAJ_MAGIC, f"{name}: magic is 0x{magic:08X}"
    assert version == TRAJ_VERSION, f"{name}: version {version}"

    cursor = index_at
    tracks = []
    for _ in range(object_count):
        object_id, parent_id, pose_dim, segment_count = struct.unpack_from("<IIHH", blob, cursor)
        cursor += 12
        default_pose = list(struct.unpack_from(f"<{pose_dim}f", blob, cursor))
        cursor += pose_dim * 4
        segments = []
        for _ in range(segment_count):
            start_frame, frame_count, offset, step = struct.unpack_from("<IIII", blob, cursor)
            cursor += 16
            stride = pose_dim * 4
            first = list(struct.unpack_from(f"<{pose_dim}f", blob, offset))
            last = list(struct.unpack_from(f"<{pose_dim}f", blob, offset + (frame_count - 1) * stride))
            segments.append({"start": start_frame, "count": frame_count, "step": step,
                             "first": first, "last": last, "end": start_frame + frame_count - 1})
        tracks.append({"object_id": object_id, "parent_id": parent_id, "default_pose": default_pose,
                       "pose_dim": pose_dim, "segments": segments})
    assert cursor == attach_at, f"{name}: the index region ends at {cursor}, attach_offset is {attach_at}"
    attaches = [struct.unpack_from("<IIII", blob, attach_at + 16 * index) for index in range(attach_count)]
    data_bytes = sum(segment["count"] * track["pose_dim"] * 4 for track in tracks for segment in track["segments"])
    assert attach_at + 16 * attach_count + data_bytes == len(blob), f"{name}: the three regions do not tile the file"
    return {"fps": fps, "total_frames": total_frames, "tracks": tracks, "attaches": attaches, "bytes": len(blob)}


def moved_slots(segment: dict) -> list[int]:
    """Which of the seven frame values this segment changes - the joint it turns."""
    return [index for index in range(7)
            if abs(segment["last"][index] - segment["first"][index]) > 1e-6]


class ExampleTrajFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.published = read_clip(PUBLISHED)
        cls.wrist_first = read_clip(WRIST_FIRST)
        cls.alternate = read_clip(ALTERNATE_JOINTS)
        cls.passive = read_clip(PASSIVE_RIG)

    def track(self, clip: dict, object_id: int) -> dict:
        for track in clip["tracks"]:
            if track["object_id"] == object_id:
                return track
        self.fail(f"object {object_id} is not declared in this clip")

    def test_both_new_clips_are_complete_v2_documents(self):
        for clip, name, total in ((self.wrist_first, WRIST_FIRST, 840), (self.alternate, ALTERNATE_JOINTS, 610)):
            with self.subTest(name):
                self.assertEqual(clip["fps"], 60)
                self.assertEqual(clip["total_frames"], total)
                self.assertEqual([track["object_id"] for track in clip["tracks"]], [2, 3, 7],
                                 "the same three objects the published clip drives")
                self.assertEqual([track["parent_id"] for track in clip["tracks"]], [WORLD_ROOT, 2, WORLD_ROOT],
                                 "the gripper hangs on the arm, the apple on the scene root until it is caught")
                self.assertEqual(clip["tracks"][0]["pose_dim"], 7, "six joints plus the recorded gripper value")
                arm = self.track(clip, 2)
                self.assertEqual([segment["step"] for segment in arm["segments"]], list(range(1, 10)),
                                 "nine segments, numbered as the plan numbers them")
                self.assertEqual([segment["count"] for segment in self.track(clip, 3)["segments"]],
                                 [segment["count"] for segment in (arm["segments"][6], arm["segments"][8])],
                                 "the gripper moves in segment 7 and 9 only, for as long as those segments run")

    def test_wrist_first_turns_the_joints_from_the_wrist_inward(self):
        arm = self.track(self.wrist_first, 2)
        opening = arm["segments"][:6]
        self.assertEqual([moved_slots(segment) for segment in opening], [[index] for index in (5, 4, 3, 2, 1, 0)],
                         "distal to proximal - the reverse of the published clip")
        for segment, expected in zip(opening, (1.35, -0.85, 0.60, 1.25, -0.95, 0.55)):
            (slot,) = moved_slots(segment)
            self.assertAlmostEqual(segment["last"][slot], expected, places=5)
        self.assertEqual(moved_slots(arm["segments"][6]), [6], "segment 7 records the gripper closing")
        self.assertAlmostEqual(arm["segments"][6]["last"][6], 0.130556, places=5,
                               msg="the angle that closes onto the apple rather than through it (Q89)")
        self.assertEqual(moved_slots(arm["segments"][7]), [0], "segment 8 re-pannes the loaded arm")
        self.assertAlmostEqual(arm["segments"][7]["last"][0], 1.80, places=5)

    def test_alternate_joints_interleaves_far_and_near_and_is_shorter(self):
        arm = self.track(self.alternate, 2)
        opening = arm["segments"][:6]
        self.assertEqual([moved_slots(segment) for segment in opening], [[index] for index in (2, 3, 0, 5, 1, 4)],
                         "elbow, wrist_1, shoulder_pan, wrist_3, shoulder_lift, wrist_2")
        for segment, expected in zip(opening, (-1.85, 0.95, -0.65, 1.45, 0.70, -1.20)):
            (slot,) = moved_slots(segment)
            self.assertAlmostEqual(segment["last"][slot], expected, places=5)
        self.assertAlmostEqual(arm["segments"][7]["last"][0], -1.20, places=5)
        # A shorter clock as well as a different order: the dropdown labels rows by frame
        # count, so two clips of one length would only differ by name (Q100).
        self.assertEqual([segment["count"] for segment in opening], [45] * 6)
        self.assertEqual(arm["segments"][0]["start"], 25, "a shorter pre-roll")
        self.assertEqual(arm["segments"][1]["start"], 90, "25 + (45 + 20) per step")

    def test_the_two_new_clips_are_distinguishable_from_the_published_one(self):
        published = [moved_slots(segment) for segment in self.track(self.published, 2)["segments"][:6]]
        self.assertEqual(published, [[index] for index in range(6)], "proximal to distal, as §12.4b describes")
        wrist = [moved_slots(segment) for segment in self.track(self.wrist_first, 2)["segments"][:6]]
        alternate = [moved_slots(segment) for segment in self.track(self.alternate, 2)["segments"][:6]]
        self.assertEqual(len({published[0][0], wrist[0][0], alternate[0][0]}), 3,
                         "each clip starts on a different joint")
        self.assertNotEqual(self.wrist_first["total_frames"], self.alternate["total_frames"])
        self.assertEqual(self.wrist_first["total_frames"], self.published["total_frames"],
                         "wrist-first keeps the 840-frame clock, so its own numbers stay verifiable")

    def test_apple_mounts_and_releases_inside_the_gripper_segment(self):
        for clip in (self.wrist_first, self.alternate):
            arm = self.track(clip, 2)
            attach_frame, child, parent, _ = clip["attaches"][0]
            detach_frame = clip["attaches"][1][0]
            self.assertEqual((child, parent), (7, 3), "the apple goes to the gripper")
            self.assertEqual(clip["attaches"][1][2], WORLD_ROOT, "and comes back to the scene root")
            self.assertEqual(attach_frame, arm["segments"][6]["end"] + 1,
                             "mounted when the closing segment ends, not before")
            self.assertEqual(detach_frame, arm["segments"][8]["start"], "released as segment 9 starts")
            apple = self.track(clip, 7)
            for segment in apple["segments"]:
                self.assertEqual(segment["first"], segment["last"],
                                 "the apple holds one pose per segment - it is carried, never slid")
            self.assertEqual(apple["segments"][0]["start"], attach_frame,
                             "the park pose starts on the frame the node is re-parented")
            self.assertAlmostEqual(apple["segments"][0]["first"][2], 0.101727, places=5,
                                   msg="the grasp centre in the driver link (Q89)")
            for value, expected in zip(apple["segments"][1]["first"][:3], (0.38, -0.22, 0.637373)):
                self.assertAlmostEqual(value, expected, places=5,
                                       msg="back to its home position after the unmount")

    def test_the_passive_rig_clip_carries_the_arm_and_its_passive_model(self):
        clip = self.passive
        self.assertEqual(clip["fps"], 60)
        self.assertEqual(clip["total_frames"], 390, "four steps on the 30/60/30 clock")
        self.assertEqual(clip["attaches"], [], "the mount lives in the world file, not in the clip")
        self.assertEqual([track["object_id"] for track in clip["tracks"]], [2, 4],
                         "a clip that carries only one of the two robots could not witness both halves of Q107")
        self.assertEqual([track["parent_id"] for track in clip["tracks"]], [WORLD_ROOT, 2],
                         "the model hangs on the arm, as docs/ur5e_passive_rig.json says")
        for track in clip["tracks"]:
            self.assertEqual(track["pose_dim"], 7)
        arm = self.track(clip, 2)
        self.assertEqual([segment["step"] for segment in arm["segments"]], [1, 3])
        self.assertEqual([moved_slots(segment) for segment in arm["segments"]], [[0], [3]],
                         "shoulder_pan, then wrist_1")
        door = self.track(clip, 4)
        self.assertEqual([segment["step"] for segment in door["segments"]], [2, 4])
        self.assertEqual([moved_slots(segment) for segment in door["segments"]], [[0], [0]],
                         "slot 0 is that joint when the sequence names one joint (Q107: driving unchanged)")
        self.assertAlmostEqual(door["segments"][0]["last"][0], -1.20, places=5)
        self.assertAlmostEqual(door["segments"][1]["last"][0], 0.0, places=5)
        for segment in door["segments"]:
            for value in segment["first"] + segment["last"]:
                self.assertLessEqual(abs(value), 1.5707963,
                                     "inside microwave.urdf's own limit - no frame may be a clamp artefact")

    def test_the_passive_rig_probe_pairs_each_leave_one_joint_still(self):
        """§12.15 ①/④(a) compare frames 100-179 and 179-269; the layout is why those are valid pairs."""
        arm = self.track(self.passive, 2)["segments"]
        door = self.track(self.passive, 4)["segments"]
        # Both 100 and 179 fall between the arm's two segments, where §8.5 holds one sample,
        # so the arm is on the same pose at both and only the door can have moved the box.
        self.assertLess(arm[0]["end"], 100)
        self.assertLess(179, arm[1]["start"])
        # And 269 is the arm's last bent sample while the door has not started closing yet,
        # so the door angle is the same as at 179 and only the wrist can have moved the box.
        self.assertEqual(door[0]["end"], 179)
        self.assertEqual(arm[1]["end"], 269)
        self.assertLess(269, door[1]["start"])

    def test_the_committed_files_match_the_generators(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = subprocess.run([sys.executable, "-X", "utf8", str(EXAMPLES / "make_scene3_variants.py"),
                                     "--out-dir", str(output)],
                                    capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            for name in (WRIST_FIRST, ALTERNATE_JOINTS):
                with self.subTest(name):
                    self.assertEqual((output / name).read_bytes(), (EXAMPLES / name).read_bytes(),
                                     f"{name} is not what make_scene3_variants.py writes")

            result = subprocess.run([sys.executable, "-X", "utf8", str(EXAMPLES / "make_passive_rig_traj.py"),
                                     "--out", str(output / PASSIVE_RIG)],
                                    capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual((output / PASSIVE_RIG).read_bytes(), (EXAMPLES / PASSIVE_RIG).read_bytes(),
                             "passive_rig.traj is not what make_passive_rig_traj.py writes")
            # The header carries this clip's own count, not the importing script's constant (坑 69).
            self.assertNotEqual(self.passive["total_frames"], self.wrist_first["total_frames"])

    def test_the_generator_does_not_touch_the_published_clip(self):
        self.assertEqual(self.published["total_frames"], 840)
        self.assertEqual((EXAMPLES / PUBLISHED).stat().st_size, self.published["bytes"])


if __name__ == "__main__":
    unittest.main()
