"""The caller write routes of §4.2/§5.9/§5.13/§5.14: gateway relay behaviour.

The browser owns the scene, so these tests drive a fake viewer over /events and
check only what the channel layer is responsible for: parameter and size
validation, payload shaping, §5.13 status mapping and the §5.4 broadcast.
"""
import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from local_gateway import DEFAULT_CONSTANT_CONFIG, StandaloneGateway, create_app

TRANSFORM = {"component_type": "Transform", "properties": {
    "position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1], "parent": None}}

WORLD = {
    "scene_schema_version": 1, "scene_version": 1, "scene_id": 3, "scene_name": "caller",
    "objects": [
        {"object_id_in_scene": 0, "object_name_in_scene": "Table", "components": [TRANSFORM]},
        {"object_id_in_scene": 1, "object_name_in_scene": "Cup",
         "components": [{"component_type": "Transform", "properties": {
             "position": [0, 0, 1], "quaternion_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1], "parent": 0}}]},
    ],
}

OBJECT_BODY = {"object_name_in_scene": "Cup", "components": [TRANSFORM]}


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


def applied_result(**overrides):
    result = {"ok": True, "scene_id": 3, "scene_version": 2, "object_count": 2, "rejected_objects": []}
    result.update(overrides)
    return result


class CallerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=0.5, max_payload_bytes=4096))
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/getWorld", self.gateway.get_world)
        app.router.add_post("/updateWorld", self.gateway.update_world)
        app.router.add_post("/updateItem", self.gateway.update_item)
        app.router.add_post("/addObject", self.gateway.add_object)
        app.router.add_post("/removeObject", self.gateway.remove_object)
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
        """Return the next browser.request frame, skipping any scene.applied broadcast."""
        for _ in range(50):
            frame = await socket.receive_json()
            if frame.get("type") == "browser.request":
                return frame
        self.fail("no browser.request frame arrived")

    async def answer(self, socket, frame, *, payload=None, error=None):
        message = {"type": "browser.response", "request_id": frame["request_id"], "ok": error is None}
        if error is None:
            message["payload"] = payload if payload is not None else applied_result()
        else:
            message["error"] = error
        await socket.send_json(message)

    async def post_and_answer(self, path, *, body=None, params=None, payload=None, error=None, browser=None):
        socket = browser or await self.connect()
        task = asyncio.create_task(self.client.post(path, json=body, params=params))
        frame = await self.next_request(socket)
        await self.answer(socket, frame, payload=payload, error=error)
        response = await task
        return response, await response.json(), frame

    # ---------------------------------------------------------------- updateWorld

    async def test_update_world_relays_the_document_and_returns_the_browser_result(self):
        response, body, frame = await self.post_and_answer("/updateWorld", body=WORLD)
        self.assertEqual(response.status, 200)
        self.assertEqual(body, applied_result())
        self.assertEqual(frame["action"], "updateWorld")
        self.assertEqual(frame["payload"]["world"], WORLD)
        self.assertNotIn("expected_version", frame["payload"])
        self.assertEqual(self.gateway.scene_version, 2)

    async def test_update_world_forwards_expected_version_from_the_query(self):
        _, _, frame = await self.post_and_answer("/updateWorld", body=WORLD, params={"expected_version": "7"})
        self.assertEqual(frame["payload"]["expected_version"], 7)

    async def test_update_world_broadcasts_the_scene_identity_to_the_viewer(self):
        # §5.2/§5.4 (Q111): the write is addressed to the bound browser alone; what the other
        # connection gets is a *notice* carrying the four-tuple, never a request to apply.
        # `source_browser_id` is gone -- under one binding it could only name the bound browser.
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        task = asyncio.create_task(self.client.post("/updateWorld", json=WORLD))
        frame = await self.next_request(first)
        await self.answer(first, frame)
        response = await task
        self.assertEqual(response.status, 200)
        # The viewer's first frame since hello is a notice: had the write been fanned out, this
        # would have been a `browser.request` it was expected to apply.
        arrived = await asyncio.wait_for(second.receive_json(), timeout=2)
        self.assertEqual(arrived["type"], "scene.applied")
        broadcast = await self.next_broadcast(first)
        # Q147: a caller's write is no tab's report, so the ack is None and the reader treats this
        # frame as a landing to believe -- `session_id` is the durable session it landed in.
        self.assertEqual(broadcast, {"type": "scene.applied", "scene_id": 3, "scene_version": 2,
                                     "scene_sub_version": 0, "scene_digest": None,
                                     "session_phase": "setup", "session_id": None,
                                     "action": "updateWorld",
                                     "source_request_id": frame["request_id"],
                                     "ack_report_seq": None})

    async def test_update_world_refuses_a_body_over_the_size_limit(self):
        socket = await self.connect()
        huge = {"scene_schema_version": 1, "scene_version": 1, "objects": [{"blob": "x" * 200} for _ in range(60)]}
        response = await self.client.post("/updateWorld", json=huge)
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"]["code"], "PAYLOAD_TOO_LARGE")
        await self.assert_no_frame(socket)

    async def test_update_world_requires_an_application_json_body(self):
        await self.connect()
        response = await self.client.post("/updateWorld", data=json.dumps(WORLD),
                                          headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")

    async def test_update_world_rejects_a_json_that_is_not_an_object(self):
        await self.connect()
        response = await self.client.post("/updateWorld", json=[1, 2, 3])
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_WORLD_SCHEMA")

    # ------------------------------------------------------------- updateItem

    async def test_update_item_sends_the_query_id_and_the_body_object(self):
        body = dict(OBJECT_BODY, object_id_in_scene=1)
        _, _, frame = await self.post_and_answer("/updateItem", body=body, params={"id": "1"})
        self.assertEqual(frame["action"], "updateItem")
        self.assertEqual(frame["payload"]["id"], 1)
        self.assertEqual(frame["payload"]["object"], body)

    async def test_update_item_validates_the_id_before_dispatching(self):
        socket = await self.connect()
        for params, code in [({}, "MISSING_PARAM"), ({"id": "abc"}, "INVALID_PARAM"),
                             ({"id": "-1"}, "INVALID_PARAM"), ({"id": "4294967296"}, "INVALID_PARAM")]:
            response = await self.client.post("/updateItem", json=OBJECT_BODY, params=params)
            self.assertEqual(response.status, 400, params)
            self.assertEqual((await response.json())["error"]["code"], code, params)
        await self.assert_no_frame(socket)

    async def test_update_item_refuses_a_body_id_that_disagrees_with_the_query(self):
        socket = await self.connect()
        response = await self.client.post("/updateItem", json=dict(OBJECT_BODY, object_id_in_scene=9), params={"id": "2"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "BODY_ID_MISMATCH")
        await self.assert_no_frame(socket)

    # ------------------------------------------------------------- addObject

    async def test_add_object_preclaims_the_next_id_when_the_caller_omits_it(self):
        # §5.5 (Q112): a fresh gateway's water mark is 0, so an id-free body is stamped with 0 and
        # the mark advances -- the browser receives the id, it does not choose one.
        allocated = {"ok": True, "object_id_in_scene": 0, "scene_version": 4}
        response, body, frame = await self.post_and_answer("/addObject", body=OBJECT_BODY, payload=allocated)
        self.assertEqual(response.status, 200)
        self.assertEqual(body, allocated)
        self.assertEqual(frame["payload"], {"object": dict(OBJECT_BODY, object_id_in_scene=0)})
        self.assertEqual(self.gateway.state.next_object_id, 1)
        self.assertEqual(self.gateway.scene_version, 4)

    async def test_add_object_passes_a_caller_id_through_unchanged(self):
        # A caller that names an id is not re-stamped; the browser judges collisions, and the
        # water mark still rises from the id the reply echoes back (§6 event ①).
        declared = dict(OBJECT_BODY, object_id_in_scene=9)
        _, _, frame = await self.post_and_answer("/addObject", body=declared,
                                                 payload={"ok": True, "object_id_in_scene": 9, "scene_version": 4})
        self.assertEqual(frame["payload"], {"object": declared})
        self.assertEqual(self.gateway.state.next_object_id, 10)

    async def test_add_object_passes_a_duplicate_id_back_as_409(self):
        response, body, frame = await self.post_and_answer(
            "/addObject", body=dict(OBJECT_BODY, object_id_in_scene=0),
            error={"code": "ID_ALREADY_EXISTS", "message": "object 0 already exists"})
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "ID_ALREADY_EXISTS")
        # §5.6: the caller gets the request id back so it can be correlated in logs.
        self.assertEqual(body["error"]["request_id"], frame["request_id"])

    # ------------------------------------------------------------- removeObject

    async def test_remove_object_parses_id_and_detach_children(self):
        removed = {"ok": True, "removed_object_ids": [0], "reparented_object_ids": [1], "scene_version": 5}
        _, _, frame = await self.post_and_answer("/removeObject", params={"id": "0", "detach_children": "true"}, payload=removed)
        self.assertEqual(frame["payload"], {"id": 0, "detach_children": True})

    async def test_remove_object_defaults_to_a_cascade(self):
        _, _, frame = await self.post_and_answer("/removeObject", params={"id": "0"},
                                                 payload={"ok": True, "removed_object_ids": [0, 1],
                                                          "reparented_object_ids": [], "scene_version": 3})
        self.assertEqual(frame["payload"], {"id": 0, "detach_children": False})

    async def test_remove_object_reports_a_bad_boolean_without_bothering_the_browser(self):
        socket = await self.connect()
        response = await self.client.post("/removeObject", params={"id": "0", "detach_children": "maybe"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        await self.assert_no_frame(socket)

    async def test_remove_object_maps_object_not_found_to_404_and_stays_silent(self):
        # §5.2/§5.4: the write reaches the bound browser alone, and a write that failed announces
        # no scene -- the other tab is neither asked to apply it nor told it happened.
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        task = asyncio.create_task(self.client.post("/removeObject", params={"id": "42"}))
        frame = await self.next_request(first)
        await self.answer(first, frame, error={"code": "OBJECT_NOT_FOUND", "message": "object 42 is not in the scene"})
        response = await task
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"]["code"], "OBJECT_NOT_FOUND")
        await self.assert_no_frame(second)

    # ------------------------------------------------------------- the channel itself

    async def test_every_write_route_needs_a_browser(self):
        calls = [("/updateWorld", {"world": WORLD}, {"id": "0"}), ("/updateItem", OBJECT_BODY, {"id": "0"}),
                 ("/addObject", OBJECT_BODY, None), ("/removeObject", None, {"id": "0"})]
        for path, body, params in calls:
            response = await self.client.post(path, json=body, params=params)
            self.assertEqual(response.status, 503, path)
            body_json = await response.json()
            self.assertEqual(body_json["error"]["code"], "NO_BROWSER_CONNECTED", path)

    async def test_write_routes_answer_json_instead_of_the_spa_shell(self):
        real = TestClient(TestServer(create_app(config=config(browser_request_timeout_seconds=0.5))))
        await real.start_server()
        try:
            response = await real.post("/updateWorld", json=WORLD)
            self.assertEqual(response.status, 503)
            self.assertIn("application/json", response.headers["Content-Type"])
            self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        finally:
            await real.close()

    async def test_a_silent_browser_times_the_write_out_at_504(self):
        await self.connect()
        response = await self.client.post("/addObject", json=OBJECT_BODY)
        self.assertEqual(response.status, 504)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "BROWSER_TIMEOUT")
        self.assertTrue(body["error"]["request_id"])
        # The slot is freed even though the viewer never answered (§5.7).
        self.assertEqual(self.gateway.pending._futures, {})

    async def test_get_world_reads_the_bound_browser_and_names_nobody_else(self):
        # §5.2/§5.8: `?browser_id=` is a check on the binding, not a way to reach another tab --
        # a viewer's world is not the session's world, so naming one is a refusal, and the read
        # that does happen carries the browser it came from (§5.1).
        first = await self.connect("browser-1")
        second = await self.connect("browser-2")
        refused = await self.client.get("/getWorld", params={"browser_id": "browser-2"})
        self.assertEqual(refused.status, 503)
        self.assertEqual((await refused.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        task = asyncio.create_task(self.client.get("/getWorld"))
        frame = await self.next_request(first)
        await self.answer(first, frame, payload={"world": WORLD, "scene_id": 3,
                                                "scene_version": 1, "scene_sub_version": 0,
                                                "playback_state": "stopped"})
        response = await task
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-World-Source"], "live")
        self.assertEqual(response.headers["X-Browser-ID"], "browser-1")
        self.assertEqual(await response.json(), WORLD)
        await self.assert_no_frame(second)

    async def test_targeting_an_absent_browser_is_503_even_with_a_warm_cache(self):
        # §5.2: a `browser_id` that is not the bound browser is an assertion that failed, and since
        # 流程文档 §5.1 (2026-11-05) there is no cache behind it any more -- the caller is told the
        # truth about who could answer, instead of being served the last thing somebody reported.
        socket = await self.connect()
        await socket.send_json({"type": "browser.state_sync",
                                "payload": {"world": WORLD, "report_seq": 1}})
        for _ in range(100):
            if self.gateway.world_cache is not None:
                break
            await asyncio.sleep(0.005)
        response = await self.client.get("/getWorld", params={"browser_id": "gone"})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        self.assertNotIn("X-World-Source", response.headers)

    async def test_write_announcement_never_reuses_a_stored_digest(self):
        # §5.1/§5.4: a write is the Agent's route and the gateway has not read the resulting
        # document, so its scene.applied carries a null digest -- even when the gateway already
        # holds a real digest from an earlier state_sync. Pairing that stored digest with the
        # write's version would announce a content hash the write never produced.
        socket = await self.connect()
        await socket.send_json({"type": "browser.state_sync", "payload": {"world": WORLD}})
        seeded = await self.next_broadcast(socket)
        self.assertEqual(seeded["type"], "scene.applied")
        self.assertIsNotNone(seeded["scene_digest"])
        task = asyncio.create_task(self.client.post("/updateWorld", json=WORLD))
        frame = await self.next_request(socket)
        await self.answer(socket, frame)
        self.assertEqual((await task).status, 200)
        broadcast = await self.next_broadcast(socket)
        self.assertIsNone(broadcast["scene_digest"])

    async def next_broadcast(self, socket):
        for _ in range(50):
            frame = await socket.receive_json()
            if frame.get("type") == "scene.applied":
                return frame
        self.fail("no scene.applied broadcast arrived")

    async def assert_no_frame(self, socket):
        """A rejected call must not reach the viewer at all."""
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.2)


if __name__ == "__main__":
    unittest.main()
