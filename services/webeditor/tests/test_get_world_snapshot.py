"""§5.8 (Q160): the refreshed page reads back the scene it had before the refresh.

`start()` reads `/getWorld` before it opens its socket, so that read always finds no browser
connected. Q111 deleted the cache behind it and the page was left holding an empty world, which
it then reported -- and an unconditional scene switch adopted that empty world over the scene on
disk. These tests pin the one reader the 503 now makes an exception for, and every way the
exception must refuse to fire.
"""
import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway_state import scene_identity
from local_gateway import DEFAULT_CONSTANT_CONFIG, StandaloneGateway

TRANSFORM = {"component_type": "Transform", "properties": {
    "position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1], "parent": None}}

WORLD = {
    "scene_schema_version": 1, "scene_version": 4, "scene_id": 3, "scene_name": "before-refresh",
    "objects": [
        {"object_id_in_scene": 0, "object_name_in_scene": "Table", "components": [TRANSFORM]},
        {"object_id_in_scene": 7, "object_name_in_scene": "Cup", "components": [TRANSFORM]},
    ],
}

LATER = json.loads(json.dumps(WORLD))
LATER["scene_version"] = 5

IDENTITY_HEADER = "X-WebEditor-Browser-ID"


class GetWorldSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
        config["browser_request_timeout_seconds"] = 0.5
        self.gateway = StandaloneGateway(config=config)
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/getWorld", self.gateway.get_world)
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

    async def report(self, socket, world=WORLD):
        await socket.send_json({"type": "browser.state_sync",
                                "payload": {"world": world, "report_seq": 1}})
        for _ in range(200):
            if self.gateway.world_cache is not None:
                return
            await asyncio.sleep(0.005)
        self.fail("the report was never adopted")

    async def disconnect(self, socket, browser_id="browser-1"):
        await socket.close()
        for _ in range(200):
            if browser_id not in self.gateway.browsers:
                return
            await asyncio.sleep(0.005)
        self.fail("the socket never left the browser table")

    async def refreshed(self, world=WORLD, *, headers=None):
        """Report a scene, then take the page away -- the state a refresh starts from."""
        socket = await self.connect()
        await self.report(socket, world)
        await self.disconnect(socket)
        claimed = {IDENTITY_HEADER: "browser-1"} if headers is None else headers
        return await self.client.get("/getWorld", headers=claimed)

    async def assert_refused(self, response):
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        self.assertNotIn("X-World-Source", response.headers)

    # ------------------------------------------------------------ the reclaim

    async def test_a_refreshed_browser_reads_back_the_scene_it_reported(self):
        response = await self.refreshed()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-World-Source"], "snapshot")
        self.assertEqual(await response.json(), WORLD)

    async def test_the_snapshot_headers_describe_the_snapshot_body(self):
        # §5.8: the integers, the digest and the body come out of one reading -- here that reading
        # is the stored document, so its own four-tuple is what the headers must carry.
        response = await self.refreshed()
        identity = scene_identity(WORLD)
        self.assertEqual(response.headers["X-Scene-ID"], "3")
        self.assertEqual(response.headers["X-Scene-Version"], "4")
        self.assertEqual(response.headers["X-Scene-Sub-Version"], str(identity["scene_sub_version"]))
        self.assertEqual(response.headers["X-Scene-Digest"], identity["sceneDigest"])
        self.assertEqual(response.headers["X-Browser-ID"], "browser-1")
        self.assertEqual(response.headers["X-Playback-State"], "stopped")
        self.assertEqual(response.headers["X-Next-Object-ID"], str(self.gateway.state.next_object_id))

    async def test_the_reclaim_adopts_nothing_and_announces_nothing(self):
        # A read is not a write: the water mark, the persisted identity and the other tab's
        # silence must all be exactly what they were before the page asked.
        socket = await self.connect()
        await self.report(socket)
        watcher = await self.connect("browser-2")
        await self.disconnect(socket)
        before_identity = self.gateway.state.scene_identity
        before_water = self.gateway.state.next_object_id
        response = await self.client.get("/getWorld", headers={IDENTITY_HEADER: "browser-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.gateway.state.scene_identity, before_identity)
        self.assertEqual(self.gateway.state.next_object_id, before_water)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(watcher.receive_json(), timeout=0.2)

    async def test_an_empty_scene_is_reclaimed_as_an_empty_scene(self):
        empty = {"scene_schema_version": 1, "scene_version": 0, "scene_id": 0, "objects": []}
        response = await self.refreshed(empty)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), empty)

    # ------------------------------------------------------------ the refusals

    async def test_a_caller_that_does_not_claim_the_binding_still_gets_503(self):
        # This is Q111 unchanged: Brain, a script, any reader without the browser's own identity
        # header is told there is no authority right now.
        await self.assert_refused(await self.refreshed(headers={}))

    async def test_a_header_naming_someone_else_still_gets_503(self):
        await self.assert_refused(await self.refreshed(headers={IDENTITY_HEADER: "browser-9"}))

    async def test_asking_for_another_browser_still_gets_503(self):
        socket = await self.connect()
        await self.report(socket)
        await self.disconnect(socket)
        response = await self.client.get("/getWorld", params={"browser_id": "gone"},
                                         headers={IDENTITY_HEADER: "browser-1"})
        await self.assert_refused(response)

    async def test_a_connected_but_silent_browser_is_a_timeout_not_a_snapshot(self):
        # 504 means "the authority is there and did not answer". A stored document must never
        # stand in for that, or a hung page would silently read its own past.
        socket = await self.connect()
        await self.report(socket)
        response = await self.client.get("/getWorld", headers={IDENTITY_HEADER: "browser-1"})
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())["error"]["code"], "BROWSER_TIMEOUT")
        self.assertNotIn("X-World-Source", response.headers)

    async def test_a_live_browser_is_still_read_live(self):
        socket = await self.connect()
        await self.report(socket)
        task = asyncio.create_task(self.client.get("/getWorld", headers={IDENTITY_HEADER: "browser-1"}))
        frame = await socket.receive_json()
        while frame.get("type") != "browser.request":
            frame = await socket.receive_json()
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": {"world": LATER, "playback_state": "stopped"}})
        response = await task
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-World-Source"], "live")
        self.assertEqual(await response.json(), LATER)

    async def test_a_cache_that_left_the_persisted_identity_is_not_served(self):
        # Q147's actual hazard: the stored document and the identity on disk describe different
        # scenes. Nobody agreed to this pair, so it is not a 现场 the page may reclaim.
        socket = await self.connect()
        await self.report(socket)
        await self.disconnect(socket)
        self.gateway.state.record_scene(LATER)
        await self.assert_refused(await self.client.get("/getWorld",
                                                        headers={IDENTITY_HEADER: "browser-1"}))

    async def test_a_gateway_that_never_saw_a_world_still_gets_503(self):
        socket = await self.connect()
        await self.disconnect(socket)
        await self.assert_refused(await self.client.get("/getWorld",
                                                        headers={IDENTITY_HEADER: "browser-1"}))


if __name__ == "__main__":
    unittest.main()