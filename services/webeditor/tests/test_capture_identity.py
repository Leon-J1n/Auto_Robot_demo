"""§9/§9.1's 身份闸: a frame and a box must prove which 现场 they describe before they are relayed.

The gateway cannot judge a picture, but §9 moved the picture's *account* onto this side: the six
identity fields a browser now answers beside its pixels are compared, one by one, against the reading
the lock was taken out over -- and 播放中、旧缓存、错浏览器、尺寸不符、PNG 不完整 are all refused,
from the response alone. `/get2DAABB` is held to the same rule because 量框同样是"对某一份现场的一次
观测"; the two codes split the way the checks do -- a claim that does not match the scene is a 400
`camera_frame_scene_mismatch`, bytes that do not match their own header are a 502 framing error.
"""
import asyncio
import base64
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from frame_identity import WORLD, identity_fields, image_base64, png_bytes
from gateway_state import scene_identity
from local_gateway import (
    DEFAULT_CONSTANT_CONFIG,
    PNG_IEND,
    PNG_IHDR_OFFSET,
    StandaloneGateway,
)

WIDTH = 8
HEIGHT = 6
# A second, honest reading of the same scene: what a lease can be taken out over when the state
# file still names the older one.
OTHER = {**WORLD, "scene_version": 9}
DEPTH_BYTES = bytes(WIDTH * HEIGHT * 4)


def frame_payload(**overrides):
    """A §5.10 rgb answer that clears the gate, so a test can break exactly one thing about it."""
    value = {"id": 2, "type": "rgb", "camera_type": "rgb", "image_encoding": "png",
             "image_base64": image_base64(WIDTH, HEIGHT), "width": WIDTH, "height": HEIGHT,
             "captured_at": "2026-10-09T10:00:00.000Z", "frame_index": None,
             **identity_fields()}
    value.update(overrides)
    return value


def rgbd_payload(**overrides):
    value = frame_payload(type="rgbd", camera_type="rgbd",
                          depth_encoding="float32le_meters",
                          depth_base64=base64.b64encode(DEPTH_BYTES).decode(),
                          depth_unit="meter", depth_invalid="NaN")
    value.update(overrides)
    return value


def box_payload(**overrides):
    """A §5.11 answer with the same six fields, because the same gate reads them."""
    value = {"id": 5, "camera_id": 2, "width": WIDTH, "height": HEIGHT,
             "coordinate_origin": "top_left", "visible": True, "clipped": False,
             "box": {"x_min": 10.5, "y_min": 20.25, "x_max": 30.75, "y_max": 40.0},
             "aabb_world": {"min": [1.96, -0.04, -0.04], "max": [2.04, 0.04, 0.04]},
             "frame_index": None, **identity_fields()}
    value.update(overrides)
    return value


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


class FrameIdentityGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=5,
                                                       capture_timeout_seconds=2))
        # §9.1 V0: the gate compares against a reading, so this gateway has taken one.
        self.gateway.state.record_scene(WORLD)
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/captureScene", self.gateway.capture_scene)
        app.router.add_get("/get2DAABB", self.gateway.get_2d_aabb)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def connect(self, browser_id="browser-1"):
        socket = await self.client.ws_connect("/events")
        await socket.send_json({"type": "browser.hello", "browser_id": browser_id, "role": "viewer"})
        for _ in range(200):
            if browser_id in self.gateway.browsers:
                return socket
            await asyncio.sleep(0.005)
        self.fail("browser.hello did not register " + browser_id)

    async def next_request(self, socket):
        for _ in range(50):
            message = await asyncio.wait_for(socket.receive_json(), timeout=5)
            if message.get("type") == "browser.request":
                return message
        self.fail("no browser.request arrived")

    async def relay(self, path, params, payload, *, headers=None):
        """Ask one route, let the bound tab answer `payload`, and hand back the HTTP response."""
        socket = await self.connect()
        task = asyncio.create_task(self.client.get(path, params=params, headers=headers))
        frame = await self.next_request(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": dict(payload, request_id=frame["request_id"])})
        return await task

    async def capture(self, payload=None, *, kind="rgb"):
        response = await self.relay("/captureScene", {"id": "2", "type": kind},
                                    payload if payload is not None else frame_payload())
        return response, await response.json()

    async def refusal(self, payload, *, kind="rgb"):
        response, body = await self.capture(payload, kind=kind)
        self.assertEqual(response.status, 400, body)
        self.assertEqual(body["error"]["code"], "camera_frame_scene_mismatch")
        return body["error"]["message"]

    # ------------------------------------------------------------- the six fields

    async def test_a_frame_that_names_the_held_scene_is_relayed(self):
        response, body = await self.capture()
        self.assertEqual(response.status, 200, body)
        for key, value in identity_fields().items():
            self.assertEqual(body[key], value, key)

    async def test_each_four_tuple_field_that_is_not_the_held_scene_is_a_refusal(self):
        held = scene_identity(WORLD)
        for key, stale in (("scene_id", 4), ("scene_version", 6), ("scene_sub_version", 1),
                           ("sceneDigest", "0" * 64)):
            message = await self.refusal(frame_payload(**{key: stale}))
            self.assertIn(f"{key}={stale!r}", message, key)
            self.assertIn(repr(held[key]), message, key)

    async def test_a_frame_that_omits_an_identity_field_is_a_refusal(self):
        # §9's 缺项即拒绝: a field the browser did not send is a field it cannot vouch for. Each
        # branch of the gate has its own wording, so each is asked for by name.
        for key in ("scene_id", "scene_version", "scene_sub_version", "sceneDigest"):
            payload = frame_payload()
            del payload[key]
            self.assertIn(key, await self.refusal(payload))
        self.assertIn("bound browser", await self.refusal({k: v for k, v in frame_payload().items()
                                                           if k != "browserID"}))
        self.assertIn("stopped scene", await self.refusal({k: v for k, v in frame_payload().items()
                                                           if k != "playback_state"}))

    async def test_a_frame_from_a_tab_that_is_not_the_bound_one_is_a_refusal(self):
        # §9's 错浏览器: the bound tab is the only subject a capture has, so an answer that claims
        # another browser is refused even though its four-tuple is perfect.
        message = await self.refusal(frame_payload(browserID="browser-2"))
        self.assertIn("browser-2", message)
        self.assertIn("browser-1", message)

    async def test_a_frame_of_a_moving_scene_is_a_refusal(self):
        for state in ("playing", "paused", "seeking"):
            message = await self.refusal(frame_payload(playback_state=state))
            self.assertIn(state, message)

    async def test_a_gateway_with_no_reading_refuses_a_frame_it_cannot_match(self):
        # The refusal has to name the way out: V0's live getWorld, then V1's acquire.
        self.gateway.state.raw["scene_identity"] = None
        response, body = await self.capture()
        self.assertEqual(response.status, 400, body)
        self.assertEqual(body["error"]["code"], "camera_frame_scene_mismatch")
        self.assertIn("no reading", body["error"]["message"])

    # --------------------------------------------------------------- which basis

    async def test_the_lease_fingerprint_is_the_basis_not_the_last_recorded_reading(self):
        # §9.1: V1 stores the fingerprint of V0's *live* read, and that is what a frame answers to
        # for as long as the lock stands -- not whatever the state file happens to remember.
        fingerprint = scene_identity(OTHER)
        self.gateway.state.raw["lease"] = {"leaseToken": "lease-1", "browser_id": "browser-1",
                                           "task_id": 41, "scene_identity": fingerprint}
        headers = {"X-Brain-Lease": "lease-1"}
        response = await self.relay("/captureScene", {"id": "2", "type": "rgb"},
                                    frame_payload(**identity_fields(world=OTHER)), headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        # And the reading this gateway still holds is now the wrong one.
        stale = await self.relay("/captureScene", {"id": "2", "type": "rgb"}, frame_payload(),
                                 headers=headers)
        self.assertEqual(stale.status, 400)
        message = (await stale.json())["error"]["message"]
        self.assertIn("scene_version=7", message)
        self.assertIn("9", message)

    # ------------------------------------------------------------------- the box

    async def test_a_measurement_is_relayed_with_its_identity(self):
        socket = await self.connect()
        task = asyncio.create_task(self.client.get("/get2DAABB", params={"id": "5", "camera_id": "2"}))
        frame = await self.next_request(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": dict(box_payload(), request_id=frame["request_id"])})
        body = await (await task).json()
        self.assertEqual(body["sceneDigest"], scene_identity(WORLD)["sceneDigest"])
        self.assertEqual(body["playback_state"], "stopped")

    async def test_a_stale_or_silent_measurement_is_a_refusal_not_a_relay(self):
        for payload in (box_payload(scene_version=12),
                        {k: v for k, v in box_payload().items() if k != "playback_state"}):
            response = await self.relay("/get2DAABB", {"id": "5", "camera_id": "2"}, payload)
            self.assertEqual(response.status, 400, payload)
            self.assertEqual((await response.json())["error"]["code"], "camera_frame_scene_mismatch")

    async def test_a_box_without_a_size_the_gate_can_read_is_a_framing_error(self):
        for label, payload in (("zero width", box_payload(width=0)),
                               ("a string height", box_payload(height="640")),
                               ("a boolean width", box_payload(width=True)),
                               ("no width declared", {k: v for k, v in box_payload().items()
                                                      if k != "width"})):
            response = await self.relay("/get2DAABB", {"id": "5", "camera_id": "2"}, payload)
            self.assertEqual(response.status, 502, label)
            self.assertEqual((await response.json())["error"]["code"], "GATEWAY_FRAMING_ERROR", label)

    # ------------------------------------------------------------- the bytes

    async def test_a_body_that_is_not_a_whole_png_is_a_framing_error(self):
        whole = png_bytes(WIDTH, HEIGHT)
        labelled = [(b"a picture", "not a png at all"),
                    (whole[:-1], "cut off by one byte"),
                    (b"\x89PNG\r\n\x1a\n" + bytes(32), "magic with no IHDR"),
                    (whole[: PNG_IHDR_OFFSET + 4], "an IHDR that is cut in half"),
                    (whole.replace(PNG_IEND, b"", 1), "no IEND closing chunk")]
        for raw, label in labelled:
            response, body = await self.capture(frame_payload(image_base64=base64.b64encode(raw).decode()))
            self.assertEqual(response.status, 502, label)
            self.assertEqual(body["error"]["code"], "GATEWAY_FRAMING_ERROR", label)
            self.assertIn("png", body["error"]["message"], label)

    async def test_a_png_that_declares_another_size_than_the_frame_is_refused(self):
        # §9's 尺寸不符, and it is the file's own IHDR that says so.
        payload = frame_payload(image_base64=image_base64(WIDTH + 8, HEIGHT))
        response, body = await self.capture(payload)
        self.assertEqual(response.status, 502)
        self.assertEqual(body["error"]["code"], "GATEWAY_FRAMING_ERROR")
        self.assertIn("16x6", body["error"]["message"])
        self.assertIn("8x6", body["error"]["message"])

    async def test_an_rgbd_depth_must_be_width_times_height_float32(self):
        for label, depth in (("short by a pixel", DEPTH_BYTES[:-4]),
                             ("one row long", b"\x00" * (WIDTH * 4)),
                             ("absent", None)):
            payload = rgbd_payload()
            if depth is None:
                del payload["depth_base64"]
            else:
                payload["depth_base64"] = base64.b64encode(depth).decode()
            response, body = await self.capture(payload, kind="rgbd")
            self.assertEqual(response.status, 502, label)
            self.assertEqual(body["error"]["code"], "GATEWAY_FRAMING_ERROR", label)
            self.assertIn(str(len(DEPTH_BYTES)), body["error"]["message"], label)

    async def test_a_correct_rgbd_frame_of_the_held_scene_is_relayed(self):
        response, body = await self.capture(rgbd_payload(), kind="rgbd")
        self.assertEqual(response.status, 200, body)
        self.assertEqual(len(base64.b64decode(body["depth_base64"])), len(DEPTH_BYTES))

    async def test_a_depth_that_is_not_base64_is_a_framing_error(self):
        response, body = await self.capture(rgbd_payload(depth_base64="!!!not base64!!!"), kind="rgbd")
        self.assertEqual(response.status, 502)
        self.assertEqual(body["error"]["code"], "GATEWAY_FRAMING_ERROR")


if __name__ == "__main__":
    unittest.main()
