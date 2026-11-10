"""§4.5/§5.9/§5.11: GET /get2DAABB is a relayed measurement, not a scene change.

The rectangle is what the browser's own camera sees, so this file watches the
channel: both ids reach one browser as one JSON request, the §5.11 body comes back
verbatim, and a box that is off-frame is a 200 with `visible: false` rather than a
404 -- the object is really there, it is only not in the picture.
"""
import asyncio
import json
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from frame_identity import WORLD, identity_fields
from local_gateway import DEFAULT_CONSTANT_CONFIG, StandaloneGateway, create_app


def box_payload(**overrides):
    """A §5.11 body as the browser sends it: pixels from the top left, metres from the file."""
    value = {
        "request_id": "unset", "id": 5, "camera_id": 2,
        "width": 640, "height": 480, "coordinate_origin": "top_left",
        "visible": True, "clipped": False,
        "box": {"x_min": 308.69, "y_min": 231.52, "x_max": 331.31, "y_max": 248.48},
        "aabb_world": {"min": [1.96, -0.04, -0.04], "max": [2.04, 0.04, 0.04]},
        "frame_index": None,
        **identity_fields(),
    }
    value.update(overrides)
    return value


OFF_FRAME = box_payload(visible=False, clipped=True, box=None)
CLIPPED = box_payload(clipped=True, frame_index=340,
                      box={"x_min": 0, "y_min": 231.52, "x_max": 14.34, "y_max": 248.48})
BOTH_IDS = {"id": "5", "camera_id": "2"}


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


class BboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The same two-timeout split as /captureScene: get2DAABB is in RENDER_ACTIONS
        # (local_gateway.py:104), so a slow browser must hit capture_timeout_seconds,
        # not the 5 s limit that a write gets.
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=5,
                                                       capture_timeout_seconds=0.5))
        # §9.1: the same identity gate /captureScene runs, so this gateway needs a reading of its
        # own before a box can claim to describe it.
        self.gateway.state.record_scene(WORLD)
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/get2DAABB", self.gateway.get_2d_aabb)
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

    async def ask(self, params=None):
        return await self.client.get("/get2DAABB", params=params)

    async def answer(self, socket, frame, *, payload=None, error=None):
        body = {"type": "browser.response", "request_id": frame["request_id"]}
        if error is None:
            body.update(ok=True, payload=dict(payload if payload is not None else box_payload(),
                                              request_id=frame["request_id"]))
        else:
            body.update(ok=False, error=error)
        await socket.send_json(body)

    # ------------------------------------------------------------------- relay

    async def test_a_box_request_carries_both_ids_and_nothing_else(self):
        socket = await self.connect()
        task = asyncio.create_task(self.ask(BOTH_IDS))
        frame = await self.next_request(socket)
        self.assertEqual(frame["action"], "get2DAABB")
        # §9.1: the box response proves its identity the same way a frame does, so the request
        # names the bound browser and the lease it is measured under.
        self.assertEqual(frame["payload"], {"id": 5, "camera_id": 2, "browser_id": "browser-1",
                                            "lease_token": None})
        # §5.3's binary pairing belongs to updateTraj; this answers in JSON.
        self.assertNotIn("expects_binary", frame)
        await self.answer(socket, frame)
        response = await task
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["request_id"], frame["request_id"])
        # §5.11: forwarded verbatim, in the file's own units and pixel order.
        self.assertEqual(body["coordinate_origin"], "top_left")
        self.assertEqual(body["width"], 640)
        self.assertEqual(body["aabb_world"], {"min": [1.96, -0.04, -0.04], "max": [2.04, 0.04, 0.04]})
        self.assertLess(body["box"]["x_min"], body["box"]["x_max"])
        self.assertLess(body["box"]["y_min"], body["box"]["y_max"])

    async def test_a_box_outside_the_picture_is_a_200_not_a_404(self):
        socket = await self.connect()
        task = asyncio.create_task(self.ask(BOTH_IDS))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, payload=OFF_FRAME)
        response = await task
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body["visible"])
        self.assertTrue(body["clipped"])
        self.assertIsNone(body["box"])
        self.assertIsNotNone(body["aabb_world"], "the box is still measured, only not in frame")

    async def test_a_clipped_rectangle_reaches_the_caller_as_the_browser_cut_it(self):
        socket = await self.connect()
        task = asyncio.create_task(self.ask(BOTH_IDS))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, payload=CLIPPED)
        body = await (await task).json()
        self.assertEqual(body["box"]["x_min"], 0, "the frame edge is where the caller may draw")
        self.assertEqual(body["frame_index"], 340)

    async def test_a_box_request_leaves_the_world_version_alone_and_broadcasts_nothing(self):
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        task = asyncio.create_task(self.ask(BOTH_IDS))
        frame = await self.next_request(first)
        # §9.1: a measurement is addressed to the bound browser and to nobody else.
        self.assertEqual(frame["payload"]["browser_id"], "browser-1")
        self.gateway.scene_version = 7
        await self.answer(first, frame)
        self.assertEqual((await task).status, 200)
        # §5.4: scene.applied means "the world changed, go re-read it". A box changed nothing.
        self.assertEqual(self.gateway.scene_version, 7)
        await self.assert_no_frame(second)

    async def test_browser_id_naming_a_viewer_is_a_refusal_not_a_redirect(self):
        # §5.2/§9.1: browser-2 announced itself as a viewer, and a viewer never receives
        # `browser.request`, so naming it can only be a refusal -- the box comes from the bound
        # tab alone. Nothing reaches either socket, and the caller gets 503 NO_BROWSER_CONNECTED.
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        response = await self.ask({**BOTH_IDS, "browser_id": "browser-2"})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        await self.assert_no_frame(first)
        await self.assert_no_frame(second)

    async def test_an_empty_browser_id_means_the_bound_viewer(self):
        # `?browser_id=` with no value must not select the browser called ""; it falls through to
        # the bound browser (§9.1).
        socket = await self.connect()
        task = asyncio.create_task(self.ask({**BOTH_IDS, "browser_id": ""}))
        frame = await self.next_request(socket)
        await self.answer(socket, frame)
        self.assertEqual((await task).status, 200)

    # ------------------------------------------------------------------ params

    async def test_a_missing_id_or_camera_id_is_400_and_never_reaches_the_browser(self):
        socket = await self.connect()
        for params in ({"camera_id": "2"}, {"id": "5"}, {}):
            response = await self.ask(params)
            self.assertEqual(response.status, 400, params)
            self.assertEqual((await response.json())["error"]["code"], "MISSING_PARAM")
        await self.assert_no_frame(socket)

    async def test_a_non_uint_id_is_400_for_either_param(self):
        socket = await self.connect()
        for key in ("id", "camera_id"):
            for value in ("-1", "1.5", "cat", "4294967296"):
                response = await self.ask({**BOTH_IDS, key: value})
                self.assertEqual(response.status, 400, f"{key}={value}")
                self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        await self.assert_no_frame(socket)

    # ------------------------------------------------------------------ errors

    async def test_an_empty_aabb_is_400_with_the_request_id(self):
        socket = await self.connect()
        task = asyncio.create_task(self.ask({"id": "6", "camera_id": "2"}))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, error={"code": "EMPTY_AABB",
                                                "message": "object 6 has no geometry measurable against camera 2"})
        response = await task
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "EMPTY_AABB")
        self.assertEqual(body["error"]["request_id"], frame["request_id"])

    async def test_an_absent_object_and_an_absent_camera_are_both_404(self):
        for code in ("OBJECT_NOT_FOUND", "CAMERA_NOT_FOUND"):
            socket = await self.connect()
            task = asyncio.create_task(self.ask({"id": "99", "camera_id": "98"}))
            frame = await self.next_request(socket)
            await self.answer(socket, frame, error={"code": code, "message": "browser says no"})
            response = await task
            self.assertEqual(response.status, 404, code)
            self.assertEqual((await response.json())["error"]["code"], code)
            await socket.close()

    async def test_a_browser_that_answers_without_a_visibility_flag_is_502(self):
        socket = await self.connect()
        task = asyncio.create_task(self.ask(BOTH_IDS))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, payload={"id": 5, "camera_id": 2, "width": 640, "height": 480})
        response = await task
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "GATEWAY_FRAMING_ERROR")

    async def test_a_visibility_flag_that_is_not_a_boolean_is_502(self):
        socket = await self.connect()
        task = asyncio.create_task(self.ask(BOTH_IDS))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, payload=box_payload(visible="true"))
        self.assertEqual((await task).status, 502)

    async def test_get_2d_aabb_without_a_browser_is_503(self):
        response = await self.ask(BOTH_IDS)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")

    async def test_an_unknown_browser_id_is_503_not_a_broadcast(self):
        socket = await self.connect()
        response = await self.ask({**BOTH_IDS, "browser_id": "browser-404"})
        self.assertEqual(response.status, 503)
        await self.assert_no_frame(socket)

    async def test_a_silent_browser_times_out_under_the_capture_limit(self):
        await self.connect()
        started = time.monotonic()
        response = await self.ask(BOTH_IDS)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())["error"]["code"], "BROWSER_TIMEOUT")
        self.assertLess(elapsed, 2.0, f"waited {elapsed:.2f}s for a 0.5s render budget")

    async def test_the_route_answers_json_not_the_spa_shell(self):
        app = create_app(config=config())
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/get2DAABB", params=BOTH_IDS)
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")


if __name__ == "__main__":
    unittest.main()
