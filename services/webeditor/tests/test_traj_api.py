"""§5.3 multipart framing and §5.12 shaping of POST /updateTraj.

A trajectory is not a world change, so these cases watch the channel layer: N `trajs`
parts are concatenated into one binary frame whose lengths are announced, the text frame
must arrive before it, the response is whatever the browser reports, and no scene
broadcast happens. The gateway never opens a clip, so most parts here are arbitrary bytes
-- the batch bookkeeping is what is under test.
"""
import asyncio
import io
import json
import unittest
from pathlib import Path

from aiohttp import FormData, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from local_gateway import DEFAULT_CONSTANT_CONFIG, PendingRequests, StandaloneGateway, TRAJ_PART_FIELD, create_app

TRAJ_BYTES = (Path(__file__).resolve().parent / "fixtures" / "robot_grasp.traj").read_bytes()

TRAJ_RESULT = {
    "ok": True,
    "count": 1,
    "trajs": [{
        "index": 0, "fps": 60, "total_frames": 720, "duration_seconds": 12.0,
        "object_count": 3, "attach_count": 2, "ignored_objects": [], "warnings": [],
    }],
}

# Large enough for the real 12088 byte fixture, small enough that an oversized
# body is a handful of kilobytes rather than the §11.1 100 MB limit.
MAX_BODY = 16384


def traj_form(*parts: bytes, field: str = TRAJ_PART_FIELD) -> FormData:
    """§5.3: the caller-side shape -- one `trajs` part per clip, in list order."""
    form = FormData()
    for part in parts:
        # BytesIO, not bytes: aiohttp reads a bare bytes value as a file field.
        form.add_field(field, io.BytesIO(part))
    return form


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value["max_payload_bytes"] = MAX_BODY
    value.update(overrides)
    return value


class TrajApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=0.5))
        app = web.Application(client_max_size=MAX_BODY + 4096)
        app.router.add_get("/events", self.gateway.events)
        app.router.add_post("/updateTraj", self.gateway.update_traj)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, browser_id="browser-1"):
        socket = await self.client.ws_connect("/events")
        await socket.send_json({"type": "browser.hello", "browser_id": browser_id, "role": "viewer"})
        for _ in range(200):
            if browser_id in self.gateway.browsers:
                return socket
            await asyncio.sleep(0.005)
        self.fail("browser.hello did not register " + browser_id)

    async def next_request(self, socket):
        message = await asyncio.wait_for(socket.receive_json(), timeout=5)
        self.assertEqual(message.get("type"), "browser.request")
        return message

    async def next_binary(self, socket):
        """The bytes must arrive as the frame right after their text frame (§5.3)."""
        message = await asyncio.wait_for(socket.receive(), timeout=5)
        self.assertEqual(message.type, WSMsgType.BINARY, "the frame after browser.request was not binary")
        return message.data

    async def assert_no_frame(self, socket):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.2)

    async def push(self, parts=(TRAJ_BYTES,), *, params=None, raw=None, content_type=None):
        if raw is not None:
            headers = {"Content-Type": content_type} if content_type else {}
            return await self.client.post("/updateTraj", data=raw, headers=headers, params=params)
        return await self.client.post("/updateTraj", data=traj_form(*parts), params=params)

    # ------------------------------------------------------------------ framing

    async def test_update_traj_announces_the_batch_then_sends_exactly_one_binary_frame(self):
        socket = await self.connect()
        task = asyncio.create_task(self.push())
        frame = await self.next_request(socket)
        self.assertEqual(frame["action"], "updateTraj")
        self.assertIs(frame["expects_binary"], True)
        self.assertEqual(frame["payload"], {
            "count": 1, "byte_lengths": [len(TRAJ_BYTES)], "byte_length": len(TRAJ_BYTES),
        })
        payload = await self.next_binary(socket)
        self.assertEqual(payload, TRAJ_BYTES)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": TRAJ_RESULT})
        response = await task
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), TRAJ_RESULT)

    async def test_two_clips_ride_as_one_frame_split_by_the_announced_lengths(self):
        # The gateway does not parse clips, so the second part is any bytes at all; what
        # matters is that body order becomes frame order and each length is announced.
        first, second = TRAJ_BYTES, b"\x00\x01\x02\x03\x04"
        socket = await self.connect()
        task = asyncio.create_task(self.push(parts=(first, second), params={"count": "2"}))
        frame = await self.next_request(socket)
        self.assertEqual(frame["payload"], {
            "count": 2, "byte_lengths": [len(first), len(second)], "byte_length": len(first) + len(second),
        })
        self.assertEqual(await self.next_binary(socket), first + second)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": {"ok": True, "count": 2, "trajs": []}})
        self.assertEqual((await task).status, 200)

    async def test_a_second_binary_frame_is_not_sent(self):
        socket = await self.connect()
        task = asyncio.create_task(self.push())
        frame = await self.next_request(socket)
        await self.next_binary(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": TRAJ_RESULT})
        await task
        await self.assert_no_frame(socket)

    async def test_update_traj_has_no_expected_version_and_no_scene_broadcast(self):
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        task = asyncio.create_task(self.push(params={"expected_version": "3"}))
        frame = await self.next_request(first)
        self.assertNotIn("expected_version", frame["payload"])
        self.assertEqual(frame["payload"]["count"], 1)
        await self.next_binary(first)
        # §5.2: the push is addressed to the bound browser; a viewer is not even asked to apply it.
        await self.assert_no_frame(second)
        self.gateway.scene_version = 7
        await first.send_json({"type": "browser.response", "request_id": frame["request_id"],
                               "ok": True, "payload": TRAJ_RESULT})
        response = await task
        self.assertEqual(response.status, 200)
        # §5.4/Q33: a trajectory does not change the world -- no `scene.applied`, no version move.
        self.assertEqual(self.gateway.scene_version, 7)
        await self.assert_no_frame(first)

    async def test_the_trajectory_reaches_the_bound_browser_only(self):
        sockets = [await self.connect(f"browser-{index}") for index in (1, 2, 3)]
        task = asyncio.create_task(self.push())
        frame = await self.next_request(sockets[0])
        arrived = await self.next_binary(sockets[0])
        self.assertEqual(len(arrived), len(TRAJ_BYTES))
        for socket in sockets[1:]:
            await self.assert_no_frame(socket)
        await sockets[0].send_json({"type": "browser.response", "request_id": frame["request_id"],
                                    "ok": True, "payload": TRAJ_RESULT})
        response = await task
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), TRAJ_RESULT)

    # ------------------------------------------------------------------ errors

    async def test_a_browser_framing_error_becomes_502_with_the_request_id(self):
        socket = await self.connect()
        task = asyncio.create_task(self.push())
        frame = await self.next_request(socket)
        await self.next_binary(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"], "ok": False,
                                "error": {"code": "GATEWAY_FRAMING_ERROR", "message": "328 of 12088 bytes arrived"}})
        response = await task
        self.assertEqual(response.status, 502)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "GATEWAY_FRAMING_ERROR")
        self.assertEqual(body["error"]["request_id"], frame["request_id"])

    async def test_a_browser_decode_error_keeps_its_own_code(self):
        socket = await self.connect()
        task = asyncio.create_task(self.push())
        frame = await self.next_request(socket)
        await self.next_binary(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"], "ok": False,
                                "error": {"code": "TRAJ_TRUNCATED", "message": "trajs[1]: index region ends early"}})
        response = await task
        self.assertEqual(response.status, 400)
        message = (await response.json())["error"]["message"]
        # §5.13: the batch is all-or-nothing and the failing part is named.
        self.assertIn("trajs[1]", message)

    async def test_a_silent_browser_times_out_and_frees_the_slot(self):
        # One pending slot only, so a future that leaked past its timeout shows up as 429 (§5.7).
        self.gateway.pending = PendingRequests(1)
        socket = await self.connect()
        first = await self.push()
        self.assertEqual(first.status, 504)
        self.assertEqual((await first.json())["error"]["code"], "BROWSER_TIMEOUT")
        frame = await self.next_request(socket)
        await self.next_binary(socket)

        second = await self.push()
        self.assertEqual(second.status, 504)
        second_frame = await self.next_request(socket)
        await self.next_binary(socket)
        self.assertNotEqual(frame["request_id"], second_frame["request_id"])

    async def test_update_traj_without_a_browser_is_503(self):
        response = await self.push()
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")

    # ------------------------------------------------------------- body checks

    async def test_the_old_single_clip_body_is_refused_by_name(self):
        socket = await self.connect()
        response = await self.push(raw=TRAJ_BYTES, content_type="application/octet-stream")
        self.assertEqual(response.status, 400)
        error = (await response.json())["error"]
        self.assertEqual(error["code"], "INVALID_PARAM")
        # The message has to teach the new shape; `traj` was the previous wire format.
        self.assertIn("multipart/form-data", error["message"])
        self.assertIn(TRAJ_PART_FIELD, error["message"])
        await self.assert_no_frame(socket)

    async def test_a_json_body_is_refused_before_the_browser_is_touched(self):
        socket = await self.connect()
        response = await self.push(raw=b"{}", content_type="application/json")
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        await self.assert_no_frame(socket)

    async def test_a_missing_content_type_is_refused(self):
        socket = await self.connect()
        response = await self.client.post("/updateTraj", data=TRAJ_BYTES, skip_auto_headers=["Content-Type"])
        self.assertEqual(response.status, 400)
        await self.assert_no_frame(socket)

    async def test_a_part_named_traj_is_refused(self):
        socket = await self.connect()
        response = await self.client.post("/updateTraj", data=traj_form(TRAJ_BYTES, field="traj"))
        self.assertEqual(response.status, 400)
        error = (await response.json())["error"]
        self.assertEqual(error["code"], "INVALID_PARAM")
        self.assertIn(TRAJ_PART_FIELD, error["message"])
        await self.assert_no_frame(socket)

    async def test_an_empty_part_is_refused(self):
        socket = await self.connect()
        response = await self.push(parts=(b"",))
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        await self.assert_no_frame(socket)

    async def test_a_body_that_is_not_well_formed_multipart_is_400_not_500(self):
        # aiohttp raises ValueError while scanning for the first boundary, which happens
        # after `multipart()` returns, so the whole read loop has to be inside the guard.
        socket = await self.connect()
        response = await self.push(raw=b"--X\r\n--X--\r\n", content_type="multipart/form-data; boundary=X")
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        await self.assert_no_frame(socket)

    async def test_a_count_that_disagrees_with_the_parts_is_refused(self):
        socket = await self.connect()
        response = await self.push(parts=(TRAJ_BYTES, b"\x01"), params={"count": "3"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        response = await self.push(params={"count": "0"})
        self.assertEqual(response.status, 400)
        response = await self.push(params={"count": "abc"})
        self.assertEqual(response.status, 400)
        await self.assert_no_frame(socket)

    async def test_an_oversized_body_is_413_and_never_reaches_the_browser(self):
        socket = await self.connect()
        response = await self.push(parts=(b"\x00" * (MAX_BODY + 1),))
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"]["code"], "PAYLOAD_TOO_LARGE")
        await self.assert_no_frame(socket)

    async def test_a_batch_beyond_1_mib_reaches_the_handler(self):
        # §11.1: aiohttp caps request bodies itself, so `create_app` has to lift that cap to
        # max_payload_bytes or a multi-clip batch dies as a bare 413 before our limit applies.
        app = create_app(config=config(browser_request_timeout_seconds=0.5, max_payload_bytes=64 * 1024 * 1024))
        async with TestClient(TestServer(app)) as client:
            clip = b"\x00" * (1024 * 1024 + 4096)
            response = await client.post("/updateTraj", data=traj_form(clip, clip), params={"count": "2"})
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")

    async def test_the_route_answers_json_not_the_spa_shell(self):
        app = create_app(config=config())
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/updateTraj", data=b"not a trajectory",
                                         headers={"Content-Type": "application/json"})
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")


if __name__ == "__main__":
    unittest.main()
