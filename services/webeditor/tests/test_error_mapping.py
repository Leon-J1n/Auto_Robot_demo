"""§5.13: the code a browser reports decides the HTTP status the caller gets.

The browser is the scene authority, so every semantic rejection arrives here as a
`browser.response` with `ok:false`. The channel layer's only job is to translate that
code into the documented status without inventing a scene verdict of its own (§5.13),
and to keep `request_id` so the caller can correlate the failure in logs (§5.6).
"""
import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from local_gateway import BROWSER_ERROR_STATUS, DEFAULT_CONSTANT_CONFIG, StandaloneGateway

# §5.13, browser-reported rows only. The gateway's own 413/429/503/504 never travel
# through this table: they are raised before (or instead of) a browser answer.
SPEC_ERROR_STATUS = {
    "MISSING_PARAM": 400,
    "INVALID_PARAM": 400,
    "INVALID_WORLD_SCHEMA": 400,
    "BODY_ID_MISMATCH": 400,
    "MISSING_TRANSFORM_COMPONENT": 400,
    "INVALID_PARENT_CYCLE": 400,
    "CAMERA_TYPE_MISMATCH": 400,
    "EMPTY_AABB": 400,
    "TRAJ_INVALID_MAGIC": 400,
    "TRAJ_UNSUPPORTED_VERSION": 400,
    "TRAJ_TRUNCATED": 400,
    "TRAJ_POSE_DIM_UNSUPPORTED": 400,
    "OBJECT_NOT_FOUND": 404,
    "CAMERA_NOT_FOUND": 404,
    "MODEL_NOT_FOUND": 404,
    "SCENE_NOT_FOUND": 404,
    "ID_ALREADY_EXISTS": 409,
    "VERSION_CONFLICT": 409,
    "MODEL_METADATA_INVALID": 422,
    "DEPENDENCY_FETCH_FAILED": 424,
    "INTERNAL_ERROR": 500,
    "UPSTREAM_ERROR": 502,
    "UPSTREAM_RENDER_FAILED": 502,
    "GATEWAY_FRAMING_ERROR": 502,
    "SCENE_NOT_LOADED": 503,
}

TRANSFORM = {"component_type": "Transform", "properties": {
    "position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1], "parent": None}}
OBJECT_BODY = {"object_name_in_scene": "Cup", "components": [TRANSFORM]}
CACHED_WORLD = {"scene_schema_version": 1, "scene_version": 4, "scene_id": 3,
                "scene_name": "cached", "objects": [{"object_id_in_scene": 0, "components": [TRANSFORM]}]}


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


class BrowserErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=0.5))
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/getWorld", self.gateway.get_world)
        app.router.add_post("/updateItem", self.gateway.update_item)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.socket = await self.connect()

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

    async def next_request(self):
        """The next browser.request frame, skipping any scene.applied broadcast."""
        for _ in range(50):
            frame = await self.socket.receive_json()
            if frame.get("type") == "browser.request":
                return frame
        self.fail("no browser.request frame arrived")

    async def publish(self, world):
        """§5.8: state_sync is handled as a task, so the cache appears a moment later."""
        await self.socket.send_json({"type": "browser.state_sync", "payload": {"world": world}})
        for _ in range(200):
            if self.gateway.world_cache is not None:
                return
            await asyncio.sleep(0.005)
        self.fail("browser.state_sync did not fill the world cache")

    async def report(self, error):
        """Let the browser reject one /updateItem and return what the caller sees."""
        task = asyncio.create_task(
            self.client.post("/updateItem", params={"id": "0"}, json=dict(OBJECT_BODY, object_id_in_scene=0)))
        frame = await self.next_request()
        await self.socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                     "ok": False, "error": error})
        response = await task
        return response, await response.json(), frame["request_id"]

    def test_the_gateway_table_is_the_specification_table(self):
        self.assertEqual(BROWSER_ERROR_STATUS, SPEC_ERROR_STATUS)

    async def test_every_documented_code_keeps_its_http_status(self):
        for code, status in sorted(SPEC_ERROR_STATUS.items()):
            with self.subTest(code=code):
                response, body, request_id = await self.report({"code": code, "message": f"{code} from the world"})
                self.assertEqual(response.status, status)
                self.assertEqual(body["error"]["code"], code)
                self.assertEqual(body["error"]["message"], f"{code} from the world")
                self.assertEqual(body["error"]["request_id"], request_id)

    async def test_a_code_the_table_does_not_know_is_an_upstream_failure(self):
        # §5.13 closes with "codes the spec does not list stay 502": an unrecognised code
        # must not be read as success, and the browser's own words survive.
        response, body, _ = await self.report({"code": "SOMETHING_NEW", "message": "a future build"})
        self.assertEqual(response.status, 502)
        self.assertEqual(body["error"]["code"], "SOMETHING_NEW")
        self.assertEqual(body["error"]["message"], "a future build")

    async def test_an_error_envelope_without_a_code_defaults_to_upstream(self):
        response, body, _ = await self.report({"message": "no code at all"})
        self.assertEqual(response.status, 502)
        self.assertEqual(body["error"]["code"], "UPSTREAM_RENDER_FAILED")

    async def test_an_error_envelope_that_is_not_an_object_still_answers(self):
        task = asyncio.create_task(
            self.client.post("/updateItem", params={"id": "0"}, json=dict(OBJECT_BODY, object_id_in_scene=0)))
        frame = await self.next_request()
        await self.socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                     "ok": False, "error": "the browser meant to send an object"})
        response = await task
        body = await response.json()
        self.assertEqual(response.status, 502)
        self.assertEqual(body["error"]["code"], "UPSTREAM_RENDER_FAILED")

    async def test_a_payload_next_to_ok_false_is_ignored(self):
        # `ok` is the only success flag. A browser that answers a conflict while smuggling
        # a payload must not have that payload relayed to the caller as if it applied.
        task = asyncio.create_task(
            self.client.post("/updateItem", params={"id": "0"}, json=dict(OBJECT_BODY, object_id_in_scene=0)))
        frame = await self.next_request()
        await self.socket.send_json({"type": "browser.response", "request_id": frame["request_id"], "ok": False,
                                     "payload": {"scene_version": 99},
                                     "error": {"code": "VERSION_CONFLICT", "message": "scene is v9"}})
        response = await task
        body = await response.json()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "VERSION_CONFLICT")
        self.assertNotIn("scene_version", body)

    async def test_a_browser_refusal_is_not_papered_over_by_the_world_cache(self):
        # A browser that answered and refused is a scene verdict. There has never been a fallback
        # for it -- and since 流程文档 §5.1 (2026-11-05) there is none for the "nobody answered"
        # cases either, so the cache is not part of this route's behaviour at all.
        await self.publish(CACHED_WORLD)
        response, body, _ = await self.report({"code": "SCENE_NOT_FOUND", "message": "no such scene"})
        self.assertEqual(response.status, 404)
        self.assertEqual(body["error"]["code"], "SCENE_NOT_FOUND")

    async def test_a_browser_without_a_world_yet_reports_its_own_refusal(self):
        # SCENE_NOT_LOADED travels to the caller as the 503 it is. The warm cache used to win here,
        # which is the shape that let an old scene answer a reader looking at a new one.
        await self.publish(CACHED_WORLD)
        task = asyncio.create_task(self.client.get("/getWorld"))
        frame = await self.next_request()
        self.assertEqual(frame["action"], "getWorld")
        await self.socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                     "ok": False, "error": {"code": "SCENE_NOT_LOADED", "message": "no world"}})
        response = await task
        body = await response.json()
        self.assertEqual(response.status, 503)
        self.assertEqual(body["error"]["code"], "SCENE_NOT_LOADED")
        self.assertNotIn("X-World-Source", response.headers)
        self.assertEqual(self.gateway.world_cache, CACHED_WORLD)


if __name__ == "__main__":
    unittest.main()
