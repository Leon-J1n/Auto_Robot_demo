"""The caller demo of §5.9 (`examples/gateway_client_demo.py`) has to stay true.

The file is a deliverable people copy from, so three things are pinned here
rather than eyeballed: every route of the documented caller surface appears in
it (the same list `docs/api-inventory.json` keeps), the embedded world document
is still a legal one, and the TRAJ bytes it packs follow
`docs/轨迹文件布局描述.md` field for field. The HTTP layer is exercised against a
recording stand-in, the way `tests/test_caller_apis.py` drives the gateway with a
fake viewer: the demo owns the request shape, the gateway owns the semantics.
"""
from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import math
import re
import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = ROOT / "examples" / "gateway_client_demo.py"

spec = importlib.util.spec_from_file_location("gateway_client_demo", DEMO_PATH)
assert spec and spec.loader
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Recorder(BaseHTTPRequestHandler):
    """Replays canned answers in order and records the request line of each call."""

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.records.append((self.command, self.path, self.headers.get("Content-Type"), body))
        index = self.server.cursor
        self.server.cursor += 1
        status, payload, headers = self.server.answers[index] if index < len(self.server.answers) \
            else (200, {"ok": True}, {})
        raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _handle

    def log_message(self, *args) -> None:  # keep the test output clean
        pass


class StubGateway:
    def __init__(self, answers=()) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), Recorder)
        self.server.records = []
        self.server.cursor = 0
        self.server.answers = list(answers)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def client(self) -> demo.GatewayClient:
        port = self.server.server_address[1]
        return demo.GatewayClient(f"http://127.0.0.1:{port}", timeout=5)

    @property
    def records(self) -> list:
        return self.server.records

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RouteCoverageTests(unittest.TestCase):
    def source(self) -> str:
        return DEMO_PATH.read_text(encoding="utf-8")

    def test_every_documented_caller_route_is_demonstrated(self):
        inventory = json.loads((ROOT / "docs" / "api-inventory.json").read_text(encoding="utf-8"))
        source = self.source()
        for entry in inventory["caller_routes"]:
            with self.subTest(route=entry["path"]):
                self.assertIn(entry["path"], source,
                              f'{entry["method"]} {entry["path"]} has no sample in the demo')

    def test_the_browser_free_routes_are_the_first_two_steps(self):
        source = self.source()
        self.assertLess(source.index("steps(\"GET /api/health"), source.index("steps(\"POST /updateWorld"),
                        "a caller has to be able to tell 'gateway down' from 'no viewer' before it writes")

    def test_the_demo_carries_its_world_and_reads_only_the_example_clips(self):
        """It is a distributable file: the world it posts has to come from inside itself.

        §13.30 (Q103) narrowed this to the world: the `/updateTraj` step reads the two
        example clips that ship next to the script, which is a `examples/` directory
        travelling with the file rather than a machine-specific path.
        """
        source = self.source()
        self.assertIsNone(re.search(r"\bopen\(", source), "no file handles in the demo")
        for forbidden in ("read_text", "data/scenes"):
            self.assertNotIn(forbidden, source, f"{forbidden} would tie the demo to one machine")
        self.assertEqual(set(re.findall(r"\.read_\w+\(", source)), {".read_bytes("},
                         "the only disk read is the example clip bytes")
        self.assertIn("EXAMPLE_TRAJS", source, "and it is limited to the two published clips")
        self.assertLess(source.index("SAMPLE_SCENE: dict"), source.index("def tour("),
                        "the world stays inline, ahead of the tour that posts it")

    def test_the_events_listener_is_receive_only(self):
        """Promoting ourselves to a viewer would steal the real tab's writes (§5.1)."""
        source = self.source()
        self.assertIn("ws_connect", source, "the /events sample has to connect")
        for forbidden in ("send_json", "send_str", ".send("):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} would register the listener as a browser and 504 the tour")

    def test_the_step_counter_matches_the_steps_the_tour_prints(self):
        """The printed "step n/total" is the tour's only progress indicator."""
        source = self.source()
        body = source[source.index("def tour("):source.index("def error_samples(")]
        printed = len(re.findall(r"^    steps\(", body, re.MULTILINE))
        self.assertEqual(int(re.search(r"Steps\((\d+)\)", body).group(1)), printed)


class EmbeddedWorldTests(unittest.TestCase):
    def test_the_embedded_scene_is_a_legal_world_document(self):
        world = demo.SAMPLE_SCENE
        self.assertEqual(world["scene_schema_version"], 1)
        self.assertEqual(world["scene_id"], 6)
        ids = [item["object_id_in_scene"] for item in world["objects"]]
        self.assertEqual(len(ids), len(set(ids)), "object_id_in_scene must be unique in the world")
        for item in world["objects"]:
            with self.subTest(object=item["object_id_in_scene"]):
                types = [component["component_type"] for component in item["components"]]
                self.assertEqual(types[0], "Transform", "every object leads with its Transform")
                for component in item["components"]:
                    self.assertIsInstance(component.get("properties"), dict)

    def test_the_ids_the_tour_uses_are_the_ones_the_sample_declares(self):
        used = (demo.AMBIENT_LIGHT_ID, demo.WORK_TABLE_ID, demo.ARM_ID, demo.GRIPPER_ID,
                demo.WRIST_CAMERA_ID, demo.OVERHEAD_CAMERA_ID, demo.MUG_ID)
        declared = {item["object_id_in_scene"] for item in demo.SAMPLE_SCENE["objects"]}
        self.assertLessEqual(set(used), declared)
        # The render and bbox samples need a real rgbd camera, not just any object (§5.10).
        for camera in (demo.WRIST_CAMERA_ID, demo.OVERHEAD_CAMERA_ID):
            self.assertEqual(demo.scene_properties(camera, "Camera")["type"], "rgbd")
        self.assertEqual(len(demo.scene_properties(demo.ARM_ID, "RobotDriver")["joint_sequence"]), 6,
                         "a trajectory frame carries six joints plus the gripper (§8.3)")

    def test_parents_point_at_objects_that_exist(self):
        declared = {item["object_id_in_scene"] for item in demo.SAMPLE_SCENE["objects"]}
        for item in demo.SAMPLE_SCENE["objects"]:
            parent = demo.scene_properties(item["object_id_in_scene"], "Transform").get("parent")
            if parent is not None:
                self.assertIn(parent, declared)


class TrajBytesTests(unittest.TestCase):
    def setUp(self):
        self.payload = demo.build_sample_traj()
        (self.magic, self.version, self.fps, self.total, self.objects,
         self.attaches, self.index_offset, self.attach_offset) = struct.unpack_from("<8I", self.payload, 0)

    def parse_index(self):
        records = []
        cursor = self.index_offset
        while cursor < self.attach_offset:
            object_id, parent, dim, count = struct.unpack_from("<IIHH", self.payload, cursor)
            cursor += 12
            default = struct.unpack_from(f"<{dim}f", self.payload, cursor)
            cursor += dim * 4
            segments = []
            for _ in range(count):
                segments.append(struct.unpack_from("<IIII", self.payload, cursor))
                cursor += 16
            records.append((object_id, parent, dim, default, segments))
        self.assertEqual(cursor, self.attach_offset, "the index must end exactly where the attach table starts")
        return records

    def test_the_header_is_the_documented_eight_uint32s(self):
        self.assertEqual(self.magic, 0x5452414A)
        self.assertEqual(list(self.payload[0:4]), [0x4A, 0x41, 0x52, 0x54],
                         "'TRAJ' becomes a u32, so the bytes on disk read JART")
        self.assertEqual((self.version, self.fps, self.index_offset), (2, 30, 32))
        self.assertEqual(self.attach_offset, 32 + sum(12 + 7 * 4 + 16 * len(segments)
                                                      for _, _, _, _, segments in self.parse_index()))

    def test_pose_dim_is_seven_and_the_root_parent_is_the_sentinel(self):
        for record in self.parse_index():
            object_id, parent, dim, _, _ = record
            with self.subTest(object=object_id):
                self.assertEqual(dim, 7)
                self.assertEqual(parent, 0xFFFFFFFF)

    def test_the_data_region_starts_after_the_attach_table(self):
        data_offset = self.attach_offset + 16 * self.attaches
        frames = sum(count for _, _, _, _, segments in self.parse_index() for _, count, _, _ in segments)
        self.assertEqual(len(self.payload), data_offset + frames * 7 * 4)

    def test_every_segment_offset_points_inside_the_file(self):
        data_offset = self.attach_offset + 16 * self.attaches
        starts = []
        for _, _, _, _, segments in self.parse_index():
            for start, count, offset, step in segments:
                self.assertGreaterEqual(offset, data_offset)
                self.assertLessEqual(offset + count * 7 * 4, len(self.payload))
                self.assertGreater(step, 0, "step 0 is idle; a moving segment must name a step")
                starts.append(start)
        self.assertEqual(starts, sorted(starts), "start_frame is monotonic (§7 of the layout doc)")

    def test_frames_ease_from_home_out_and_back(self):
        data_offset = self.attach_offset + 16 * self.attaches
        first = struct.unpack_from("<7f", self.payload, data_offset)
        turned = struct.unpack_from("<7f", self.payload, data_offset + 39 * 7 * 4)
        parked = struct.unpack_from("<7f", self.payload, data_offset + 89 * 7 * 4)
        self.assertAlmostEqual(first[0], 0.0, places=5)
        self.assertAlmostEqual(turned[0], 0.6, places=5)
        self.assertAlmostEqual(parked[0], 0.0, places=5)
        for frame in (first, turned, parked):
            self.assertFalse(any(math.isnan(value) for value in frame))
        self.assertAlmostEqual(first[1], demo.scene_properties(demo.ARM_ID, "RobotDriver")
                               ["initial_joint_positions_rad"]["shoulder_lift_joint"], places=5,
                               msg="the untouched joints keep the pose the world file declares")

    def test_a_static_object_carries_its_own_world_pose(self):
        arm_record_size = 12 + 28 + 32
        mug_default = struct.unpack_from("<7f", self.payload, self.index_offset + arm_record_size + 12)
        for declared, packed in zip(demo.object_frame(demo.MUG_ID), mug_default):
            self.assertAlmostEqual(declared, packed, places=5,
                                   msg="float32 is lossy, but the frame is the Transform from the world")

    def test_the_attach_table_moves_the_mug_onto_the_arm_and_back(self):
        rows = [struct.unpack_from("<IIII", self.payload, self.attach_offset + 16 * i)
                for i in range(self.attaches)]
        self.assertEqual(rows, [(50, demo.MUG_ID, demo.ARM_ID, 0),
                                (80, demo.MUG_ID, demo.WORK_TABLE_ID, 0)])
        self.assertEqual(self.objects, 3, "the third object is absent on purpose, for ignored_objects (§5.12)")


class TransportTests(unittest.TestCase):
    def test_query_parameters_ids_and_media_types_match_the_spec(self):
        stub = StubGateway()
        try:
            stub.client.get("/getWorld")
            stub.client.get("/get2DAABB", params={"id": 12, "camera_id": 5, "browser_id": None})
            stub.client.post("/updateItem", {"object_id_in_scene": 12}, params={"id": 12, "expected_version": 7})
            stub.client.post("/removeObject", params={"id": 12, "expected_version": 8})
            stub.client.post_trajs("/updateTraj", [b"TRAJ", b"CLIP2"], params={"count": 2})
            records = stub.records
        finally:
            stub.close()
        self.assertEqual(records[0][:2], ("GET", "/getWorld"))
        self.assertEqual(records[1][1], "/get2DAABB?id=12&camera_id=5",
                         "None-valued optional parameters are left out, not sent as 'None' (§5.4)")
        self.assertEqual(records[2][:2], ("POST", "/updateItem?id=12&expected_version=7"))
        self.assertEqual(records[2][2], "application/json")
        self.assertEqual(records[3][1], "/removeObject?id=12&expected_version=8")
        self.assertEqual(records[3][3], b"", "a body-less POST still arrives as a POST")
        self.assertEqual(records[4][1], "/updateTraj?count=2", "count is a query parameter (§5.3)")
        self.assertTrue(records[4][2].startswith("multipart/form-data; boundary="),
                        "a batch is multipart, one `trajs` part per clip (§5.3, Q99)")
        body = records[4][3]
        self.assertEqual(body.count(b'name="trajs"'), 2, "the field name repeats, it is not `traj`")
        self.assertLess(body.index(b"TRAJ"), body.index(b"CLIP2"), "part order is the list order (§7.1)")
        self.assertNotIn(b"base64", body, "clips travel as raw bytes, never base64 (§5.3)")

    def test_the_error_envelope_keeps_code_message_and_request_id(self):
        body = {"error": {"code": "VERSION_CONFLICT", "message": "expected_version 9 != current 3",
                          "request_id": "req-1"}}
        stub = StubGateway([(409, body, {})])
        try:
            with self.assertRaises(demo.GatewayError) as caught:
                stub.client.post("/updateWorld", {"objects": []})
        finally:
            stub.close()
        error = caught.exception
        self.assertEqual((error.status, error.code, error.request_id), (409, "VERSION_CONFLICT", "req-1"))
        self.assertIn("expected_version", error.message)

    def test_a_live_world_is_told_apart_from_the_cache(self):
        stub = StubGateway([(200, {"scene_id": 6, "objects": []}, {"X-World-Source": "cache"})])
        try:
            payload, headers = stub.client.get_raw("/getWorld")
        finally:
            stub.close()
        self.assertEqual(payload["scene_id"], 6)
        self.assertEqual(headers.get("X-World-Source"), "cache",
                         "the header is the only way a caller learns the answer is stale (§5.9)")

    def test_an_answer_that_is_not_json_fails_loudly(self):
        stub = StubGateway([(200, b"<html>the SPA shell</html>", {})])
        try:
            with self.assertRaises(RuntimeError):
                stub.client.get("/getWorld")
        finally:
            stub.close()

    def test_get2daabb_retries_while_the_asset_is_still_loading(self):
        empty = (400, {"error": {"code": "EMPTY_AABB", "message": "no geometry yet",
                                 "request_id": None}}, {})
        box = (200, {"visible": True, "box": {"x_min": 1.0, "y_min": 2.0, "x_max": 3.0, "y_max": 4.0}}, {})
        stub = StubGateway([empty, empty, box])
        try:
            with contextlib.redirect_stdout(io.StringIO()):  # the retry notice is for a human terminal
                answer = demo.get_aabb(stub.client, 12, 5, attempts=4, delay=0.0)
            paths = [record[1] for record in stub.records]
        finally:
            stub.close()
        self.assertTrue(answer["visible"])
        self.assertEqual(len(paths), 3, "two EMPTY_AABB while the mesh builds, then the box")

    def test_get2daabb_gives_up_after_the_attempts_are_used_up(self):
        empty = (400, {"error": {"code": "EMPTY_AABB", "message": "no geometry at all",
                                 "request_id": None}}, {})
        stub = StubGateway([empty] * 3)
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(demo.GatewayError) as caught:
                demo.get_aabb(stub.client, 0, 5, attempts=3, delay=0.0)
        finally:
            stub.close()
        self.assertEqual(caught.exception.code, "EMPTY_AABB",
                         "a camera or light id really is EMPTY_AABB, and that is not retried away")


class JointSweepTests(unittest.TestCase):
    """The animation step drives one joint by calling `/updateItem` over and over."""

    def run_sweep(self, answers=(), **kwargs) -> tuple:
        stub = StubGateway(answers)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                frames, version = demo.sweep_joint(stub.client, demo.scene_object(demo.ARM_ID),
                                                    demo.SPIN_JOINT, **kwargs)
            records = stub.records
        finally:
            stub.close()
        angles = [demo.driver_of(json.loads(record[3].decode("utf-8")))
                  ["now_joint_positions_rad"][demo.SPIN_JOINT] for record in records]
        return frames, version, angles, records

    def test_the_sample_scene_leaves_room_for_a_full_turn(self):
        driver = demo.scene_properties(demo.ARM_ID, "RobotDriver")
        self.assertIn(demo.SPIN_JOINT, driver["joint_sequence"])
        lower, upper = driver["joint_limits"][demo.SPIN_JOINT]
        self.assertLessEqual(lower, 0.0, "the angle the sweep starts from is inside the declared range")
        self.assertLess(math.tau, upper,
                        "the viewer clamps to this range, so a turn has to fit with room to spare "
                        "or the sweep would quietly become a swing")

    def test_a_second_run_starts_from_the_declared_home(self):
        body = inspect.getsource(demo.spin_shoulder_pan)
        self.assertRegex(body, r"home_angle = float\(.*initial_joint_positions_rad",
                         "the sweep starts from the angle the driver declares, not the one the world "
                         "happens to hold; from a held 2*pi the next full turn would not fit the range")
        for line in body.splitlines():
            if "start=" in line:
                self.assertNotIn("now_joint_positions_rad", line,
                                 "reading the held `now` as the start is what made the second run fail")

    def test_the_round_trip_is_measured_between_two_samples_taken_after_the_sweep(self):
        body = inspect.getsource(demo.spin_shoulder_pan)
        self.assertIn("box_shift(full, again)", body,
                      "a full turn has to match home, and both of those boxes are measured once the "
                      "assets are up")
        self.assertNotIn("box_shift(start", body,
                         "the sample taken before the sweep can be a half-attached URDF - the real run "
                         "answered 200 with the base column only, paused, and then kept growing, so "
                         "'it stopped changing' is not a settle signal to compare against")

    def test_object_for_joint_commands_one_joint_and_keeps_the_rest(self):
        arm = demo.scene_object(demo.ARM_ID)
        twice = demo.object_for_joint(demo.object_for_joint(arm, "elbow_joint", 0.2), demo.SPIN_JOINT, 0.5)
        self.assertEqual(demo.driver_of(twice)["now_joint_positions_rad"],
                         {"elbow_joint": 0.2, demo.SPIN_JOINT: 0.5},
                         "each tick merges into the joint it names (Q75) instead of replacing the dictionary")
        self.assertIsNone(demo.driver_of(arm).get("now_joint_positions_rad"),
                          "building a body must not mutate the embedded sample world")
        self.assertEqual([c["component_type"] for c in twice["components"]],
                         [c["component_type"] for c in arm["components"]],
                         "/updateItem replaces the object whole, so all four components travel back")

    def test_box_shift_reads_the_shape_get2daabb_actually_reports(self):
        home = {"min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0]}
        turned = {"min": [0.0, 0.0, 0.5], "max": [1.0, 1.0, 1.5]}
        self.assertEqual(demo.box_shift(home, turned), 0.5,
                         "§5.11 reports aabb_world as {min: [x, y, z], max: [x, y, z]}")
        self.assertEqual(demo.box_shift(home, home), 0.0, "a full turn has to land back on the start")

    def test_the_sweep_ends_exactly_on_the_target_angle(self):
        frames, version, angles, records = self.run_sweep(
            start=0.0, end=math.tau, version=3, speed=1e9, hz=1e9)
        self.assertGreaterEqual(frames, 1)
        self.assertEqual(frames, len(angles))
        self.assertEqual(angles[-1], math.tau, "the last tick lands on the end, never past it")
        self.assertEqual(angles, sorted(angles), "a sweep travels one way; it never oscillates")
        self.assertEqual(version, 3, "an answer with no scene_version is not turned into a number")
        for record in records:
            self.assertEqual(record[:2], ("POST", f"/updateItem?id={demo.ARM_ID}&expected_version=3"))

    def test_the_sweep_takes_a_tick_per_frame_so_the_motion_is_smooth(self):
        frames, _, angles, _ = self.run_sweep(start=0.0, end=0.05, version=1, speed=1.0, hz=1000.0)
        self.assertGreaterEqual(frames, 2,
                                "at 1 rad/s a 0.05 rad travel needs several ticks, "
                                "which is what makes the arm look like it is turning")
        self.assertEqual(angles[-1], 0.05)

    def test_a_concurrent_writer_stops_the_sweep_instead_of_racing_it(self):
        conflict = (409, {"error": {"code": "VERSION_CONFLICT", "message": "expected_version 3 != current 4",
                                    "request_id": "req-1"}}, {})
        frames, version, _, records = self.run_sweep([conflict], start=0.0, end=math.tau,
                                                      version=3, speed=1e9, hz=1e9)
        self.assertEqual(frames, 0, "a lost version race is reported, not retried forever")
        self.assertEqual(len(records), 1, "it gave up on the first tick")
        self.assertEqual(version, 3, "it reports the version it still believes, not the one it lost")


if __name__ == "__main__":
    unittest.main()
