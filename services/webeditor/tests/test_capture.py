"""§4.4/§5.9/§5.10: GET /captureScene is a relayed render, not a scene change.

These cases watch the channel layer: the params reach one browser as one JSON
request, the §5.10 body comes back verbatim, the camera's own errors keep their
codes, and nothing about a capture bumps the world version or broadcasts.
"""
import asyncio
import base64
import json
import math
import struct
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from frame_identity import WORLD, identity_fields, image_base64
from local_gateway import DEFAULT_CONSTANT_CONFIG, StandaloneGateway, create_app

WIDTH = 4
HEIGHT = 3
# What a browser-side §5.10 map looks like: metres along the optical axis, NaN where the pass cleared.
DEPTH_METERS = [1.5, 2.5, 3.5, 4.5, float("nan"), 6.5, 7.25, 8.5, 9.5, 10.5, 11.5, 12.5]


def capture_payload(**overrides):
    depth = struct.pack(f"<{len(DEPTH_METERS)}f", *DEPTH_METERS)
    value = {
        "request_id": "unset", "id": 2, "type": "rgbd",
        "width": WIDTH, "height": HEIGHT, "camera_type": "rgbd",
        "image_encoding": "png", "image_base64": image_base64(WIDTH, HEIGHT),
        "depth_encoding": "float32le_meters", "depth_base64": base64.b64encode(depth).decode(),
        "depth_unit": "meter", "depth_invalid": "NaN",
        "captured_at": "2026-10-09T10:00:00.000Z", "frame_index": 340,
        **identity_fields(),
    }
    value.update(overrides)
    return value


RGB_PAYLOAD = capture_payload(type="rgb", camera_type="rgb")
del RGB_PAYLOAD["depth_encoding"]
del RGB_PAYLOAD["depth_base64"]
del RGB_PAYLOAD["depth_unit"]
del RGB_PAYLOAD["depth_invalid"]


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A capture is the slow path, so the two timeouts differ on purpose: the test
        # for the silent browser would also pass at 5 s, but then it would not say
        # which of the two limits (§5.2) the route is under.
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=5,
                                                       capture_timeout_seconds=0.5))
        # §9.1: the identity gate compares against a reading, so this gateway has to hold one
        # before a frame can claim to match it -- the same V0 the real flow does before V2.
        self.gateway.state.record_scene(WORLD)
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/captureScene", self.gateway.capture_scene)
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

    async def assert_no_frame(self, socket):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.2)

    async def capture(self, params=None):
        return await self.client.get("/captureScene", params=params)

    async def answer(self, socket, frame, *, payload=None, error=None):
        body = {"type": "browser.response", "request_id": frame["request_id"]}
        if error is None:
            body.update(ok=True, payload=dict(payload if payload is not None else capture_payload(),
                                              request_id=frame["request_id"]))
        else:
            body.update(ok=False, error=error)
        await socket.send_json(body)

    # ------------------------------------------------------------------- relay

    async def test_a_capture_sends_one_json_request_with_the_camera_params(self):
        socket = await self.connect()
        task = asyncio.create_task(self.capture({"id": "2", "type": "rgbd"}))
        frame = await self.next_request(socket)
        self.assertEqual(frame["action"], "captureScene")
        # §9.1: the bound browser and the lease ride along -- the response has to name who
        # rendered, and only a locked viewer resets its light preset (§7.2's third lock).
        self.assertEqual(frame["payload"], {"id": 2, "type": "rgbd", "browser_id": "browser-1",
                                            "lease_token": None})
        # §5.3 pairs binary frames with updateTraj only; a capture answers in JSON.
        self.assertNotIn("expects_binary", frame)
        await self.answer(socket, frame)
        response = await task
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["id"], 2)
        self.assertEqual(body["request_id"], frame["request_id"])
        self.assertEqual(body["width"], WIDTH)
        self.assertEqual(body["height"], HEIGHT)
        # §5.10: the gateway forwards the browser body without touching it.
        self.assertEqual(body["depth_encoding"], "float32le_meters")
        self.assertEqual(body["scene_version"], 7)
        self.assertEqual(body["frame_index"], 340)

    async def test_the_relayed_depth_is_float32_le_meters_with_nan_for_misses(self):
        socket = await self.connect()
        task = asyncio.create_task(self.capture({"id": "2", "type": "rgbd"}))
        frame = await self.next_request(socket)
        await self.answer(socket, frame)
        body = await (await task).json()
        raw = base64.b64decode(body["depth_base64"])
        self.assertEqual(len(raw), body["width"] * body["height"] * 4)
        values = struct.unpack(f"<{len(raw) // 4}f", raw)
        self.assertEqual(values[0], 1.5)
        self.assertEqual(values[6], 7.25)
        self.assertTrue(math.isnan(values[4]), "a cleared pixel must reach the caller as NaN")
        self.assertEqual(raw[:4], struct.pack("<f", 1.5), "§5.10 asks for little-endian float32")

    async def test_an_rgb_capture_carries_no_depth_fields(self):
        socket = await self.connect()
        task = asyncio.create_task(self.capture({"id": "2", "type": "rgb"}))
        frame = await self.next_request(socket)
        self.assertEqual(frame["payload"], {"id": 2, "type": "rgb", "browser_id": "browser-1",
                                            "lease_token": None})
        await self.answer(socket, frame, payload=RGB_PAYLOAD)
        body = await (await task).json()
        for key in ("depth_base64", "depth_encoding", "depth_unit", "depth_invalid"):
            self.assertNotIn(key, body)

    async def test_a_capture_leaves_the_world_version_alone_and_broadcasts_nothing(self):
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        self.addAsyncCleanup(second.close)
        task = asyncio.create_task(self.capture({"id": "2", "type": "rgbd"}))
        frame = await self.next_request(first)
        # §9.1's 绑定闸: a capture with no `?browser_id=` is addressed to the bound browser, not
        # asked of every tab -- pixels from two viewers are two different frames.
        self.assertEqual(frame["payload"]["browser_id"], "browser-1")
        await self.assert_no_frame(second)
        self.gateway.scene_version = 7
        await self.answer(first, frame)
        response = await task
        self.assertEqual(response.status, 200)
        # §5.4: scene.applied means "the world changed, go re-read it". Pixels changed nothing.
        self.assertEqual(self.gateway.scene_version, 7)
        await self.assert_no_frame(second)

    async def test_browser_id_naming_a_viewer_is_a_refusal_not_a_redirect(self):
        # §5.2/§9.1: browser-2 announced itself as a viewer, and a viewer never receives
        # `browser.request`, so naming it can only be a refusal -- pixels come from the bound
        # tab alone. Nothing reaches either socket, and the caller gets 503 NO_BROWSER_CONNECTED.
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        response = await self.capture({"id": "2", "type": "rgbd", "browser_id": "browser-2"})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        await self.assert_no_frame(first)
        await self.assert_no_frame(second)

    # ------------------------------------------------------------------ params

    async def test_a_missing_type_is_400_and_never_reaches_the_browser(self):
        socket = await self.connect()
        response = await self.capture({"id": "2"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "MISSING_PARAM")
        await self.assert_no_frame(socket)

    async def test_a_type_outside_the_enum_is_400(self):
        # §9 第 4 项：segmentation 走的就是这一支闸——不认的模式就是 `INVALID_PARAM`，不生成占位数据。
        socket = await self.connect()
        for kind in ("depth", "RGB", "", "segmentation"):
            response = await self.capture({"id": "2", "type": kind})
            self.assertEqual(response.status, 400, kind)
            self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        await self.assert_no_frame(socket)

    async def test_a_non_uint_id_is_400(self):
        socket = await self.connect()
        for value in ("-1", "1.5", "cat", "4294967296"):
            response = await self.capture({"id": value, "type": "rgb"})
            self.assertEqual(response.status, 400, value)
        await self.assert_no_frame(socket)

    # ------------------------------------------------------------------ errors

    async def test_a_camera_type_mismatch_is_400_with_the_request_id(self):
        socket = await self.connect()
        task = asyncio.create_task(self.capture({"id": "2", "type": "rgbd"}))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, error={"code": "CAMERA_TYPE_MISMATCH",
                                                "message": "capture asked for rgbd but camera 2 is rgb"})
        response = await task
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "CAMERA_TYPE_MISMATCH")
        self.assertEqual(body["error"]["request_id"], frame["request_id"])

    async def test_an_object_without_a_camera_is_404_and_a_failed_render_is_502(self):
        for code, status in (("CAMERA_NOT_FOUND", 404), ("UPSTREAM_RENDER_FAILED", 502)):
            socket = await self.connect()
            task = asyncio.create_task(self.capture({"id": "9", "type": "rgb"}))
            frame = await self.next_request(socket)
            await self.answer(socket, frame, error={"code": code, "message": "browser says no"})
            response = await task
            self.assertEqual(response.status, status, code)
            self.assertEqual((await response.json())["error"]["code"], code)
            await socket.close()

    async def test_a_browser_that_answers_without_an_image_is_502(self):
        socket = await self.connect()
        task = asyncio.create_task(self.capture({"id": "2", "type": "rgb"}))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, payload={"id": 2, "type": "rgb"})
        response = await task
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "GATEWAY_FRAMING_ERROR")

    async def test_capture_scene_without_a_browser_is_503(self):
        response = await self.capture({"id": "2", "type": "rgb"})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")

    async def test_an_unknown_browser_id_is_503_not_a_broadcast(self):
        socket = await self.connect()
        response = await self.capture({"id": "2", "type": "rgb", "browser_id": "browser-404"})
        self.assertEqual(response.status, 503)
        await self.assert_no_frame(socket)

    async def test_a_silent_browser_times_out_under_the_capture_limit(self):
        await self.connect()
        started = time.monotonic()
        response = await self.capture({"id": "2", "type": "rgb"})
        elapsed = time.monotonic() - started
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())["error"]["code"], "BROWSER_TIMEOUT")
        # §5.2: rendering requests get capture_timeout_seconds (0.5 s here), not the 5 s write limit.
        self.assertLess(elapsed, 2.0, f"waited {elapsed:.2f}s for a 0.5s capture budget")

    async def test_the_route_answers_json_not_the_spa_shell(self):
        app = create_app(config=config())
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/captureScene", params={"id": "2", "type": "rgb"})
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")


if __name__ == "__main__":
    unittest.main()
