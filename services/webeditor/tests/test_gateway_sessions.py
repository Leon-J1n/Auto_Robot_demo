"""§4/§5 of docs/gateway与Agent新通信流程.md: `/sessions`, `/brainCapabilities`, the phase edges.

The phase machine has exactly two edges -- an accepted task moves it to `agent_owned`, an explicit
new or switched session moves it back to `setup` -- and both of them are announced once on the WS
rather than left for each tab to guess. Everything this file checks hangs off that: a lease or a
quarantine may not be walked around by creating a session (§8.3), a camera is chosen at creation
and never moved inside one session (§9), and the eight capability names are ours to promise.
"""
import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from local_gateway import (
    BRAIN_WEB2_CAPABILITIES,
    BRAIN_WEB2_PROTOCOL,
    DEFAULT_CONSTANT_CONFIG,
    StandaloneGateway,
)

# Where nothing answers at all. A refused local port costs ~2 s per attempt, so tests that only
# need "the Agent service is not there" use a name that cannot resolve.
DEAD_URL = "http://brain.invalid:8500"

# One small legal world, so a bound browser can report a scene the way the panel does.
WORLD = {"scene_schema_version": 1, "scene_id": 7, "scene_name": "sessions",
         "scene_version": 3, "scene_sub_version": 0,
         "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 4}]}


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


def session_reply(**overrides):
    """Brain's own `/sessions` body: the four fields we share, plus one that is none of our business."""
    value = {"sessionID": "s-1", "session_phase": "setup", "browserID": "browser-7",
             "camera_id": 5, "conversation": {"turns": 0, "title": "new"}}
    value.update(overrides)
    return value


class FakeBrainService:
    """The `web/service.py:306-307` pair this gateway forwards to, plus `/startTask`."""

    def __init__(self) -> None:
        self.sessions_bodies: list[dict] = []
        self.sessions_status = 200
        self.sessions_body: object = session_reply()
        self.start_task_status = 200
        self.start_task_state = "规划中"
        self.start_task_calls = 0
        # §7.4: `terminal` is what frees a session for the next command (§7.1), so a test that
        # wants a second task ends the first one here rather than by naming a state.
        self.state_terminal = False
        self.polled_state = "规划中"

    def add_routes(self, app: web.Application) -> None:
        app.router.add_post("/sessions", self.sessions)
        app.router.add_post("/startTask", self.start_task)
        app.router.add_get("/queryTaskState", self.task_state)
        app.router.add_get("/queryTaskResult", self.task_result)
        app.router.add_get("/queryTaskFailureReason", self.failure_reason)

    async def sessions(self, request: web.Request) -> web.Response:
        self.sessions_bodies.append(await request.json())
        return web.json_response(self.sessions_body, status=self.sessions_status)

    async def start_task(self, request: web.Request) -> web.Response:
        self.start_task_calls += 1
        if self.start_task_status >= 400:
            return web.json_response({"error": {"code": "task_busy", "message": "busy"}},
                                     status=self.start_task_status)
        return web.json_response({"taskID": 12, "state": self.start_task_state, "terminal": False})

    async def task_state(self, request: web.Request) -> web.Response:
        return web.json_response({"taskID": 12, "state": self.polled_state,
                                  "terminal": self.state_terminal})

    async def task_result(self, request: web.Request) -> web.Response:
        # §7.3: a task that wrote no result answers 202, never an empty object.
        return web.json_response({"error": "任务未完成: 12"}, status=202)

    async def failure_reason(self, request: web.Request) -> web.Response:
        return web.json_response({"taskID": 12, "failureReason": None})


class SessionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.brain = FakeBrainService()
        brain_app = web.Application()
        self.brain.add_routes(brain_app)
        brain_server = TestServer(brain_app)
        await brain_server.start_server()
        self.addAsyncCleanup(brain_server.close)
        self.brain_url = str(brain_server.make_url("/")).rstrip("/")

        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-sessions-")
        self.addCleanup(self.workspace.cleanup)
        self.path = Path(self.workspace.name) / "gateway_state.json"
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=5,
                                                       agent_service_url=self.brain_url),
                                         state_path=self.path)
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/sessions", self.gateway.sessions)
        app.router.add_post("/sessions", self.gateway.sessions)
        app.router.add_get("/brainCapabilities", self.gateway.brain_capabilities)
        app.router.add_get("/api/sessionState", self.gateway.session_state)
        app.router.add_post("/api/agent/conversations", self.gateway.conversations_create)
        app.router.add_post("/resolveUncertain", self.gateway.resolve_uncertain)
        app.router.add_post("/api/agent/jobs", self.gateway.jobs)
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
        self.fail(f"browser.hello did not register {browser_id}")

    async def disconnect(self, socket):
        await socket.close()
        for _ in range(200):
            if not self.gateway.browsers:
                return
            await asyncio.sleep(0.005)

    async def report(self, socket, document=WORLD):
        await socket.send_json({"type": "browser.state_sync", "payload": {"world": document}})
        for _ in range(200):
            if self.gateway.state.scene_identity is not None:
                return
            await asyncio.sleep(0.005)

    async def open_session(self, **body):
        payload = {"browserID": "browser-1", "camera_id": 5}
        payload.update(body)
        return await self.client.post("/sessions", json=payload)

    async def submit(self, socket, command="pick the cup", client_id="req-1"):
        """§7.1: one submission, answered from the connected browser's side.

        A submission now begins with a live `getWorld` of the bound browser (§5.15 step 2), and that
        read blocks the POST, so the two run side by side the way they do on a real tab. The payload
        mirrors `scene_requests.ts:case "getWorld"`.
        """
        task = asyncio.create_task(self.client.post(
            "/api/agent/jobs", json={"command": command, "scene": WORLD, "clientRequestID": client_id}))
        frame = await self.next_frame(socket, "browser.request")
        self.assertEqual(frame["action"], "getWorld")
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"], "ok": True,
                                "payload": {"world": WORLD, "scene_id": WORLD["scene_id"],
                                            "scene_version": WORLD["scene_version"],
                                            "scene_sub_version": WORLD["scene_sub_version"],
                                            "playback_state": "idle"}})
        return await task

    async def next_frame(self, socket, kind):
        for _ in range(50):
            frame = await socket.receive_json()
            if frame.get("type") == kind:
                return frame
        self.fail(f"no {kind} frame arrived")

    async def assert_no_frame(self, socket):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.2)

    async def assert_no_phase_change(self, socket):
        # A tab that is busy telling us about its world or its jobs is not a tab that changed phase.
        seen = []
        with contextlib.suppress(asyncio.TimeoutError):
            while True:
                frame = await asyncio.wait_for(socket.receive_json(), timeout=0.2)
                seen.append(frame.get("type"))
        self.assertNotIn("session.phase_changed", seen)

    # ------------------------------------------------------------------ create (§4)

    async def test_a_new_session_resets_the_phase_and_announces_it_once(self):
        socket = await self.connect()
        self.gateway.state.set_phase("agent_owned")
        self.brain.sessions_body = session_reply(sessionID="s-9")
        response = await self.open_session()
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertEqual(body["session_phase"], "setup", "an explicit new session is the ONLY door back (§4)")
        self.assertEqual(body["sessionID"], "s-9")
        self.assertEqual(self.gateway.state.session_id, "s-9")
        self.assertEqual(self.gateway.state.camera_id, 5)
        frame = await self.next_frame(socket, "session.phase_changed")
        self.assertEqual(frame["payload"], {"browser_id": "browser-1", "session_phase": "setup"})

    async def test_the_forwarded_body_names_the_browser_and_the_chosen_camera(self):
        await self.connect()
        await self.open_session(sessionID="s-2", camera_id=4)
        self.assertEqual(self.brain.sessions_bodies,
                         [{"browserID": "browser-1", "sessionID": "s-2", "camera_id": 4, "scene": None}])

    async def test_brains_own_fields_travel_unpruned(self):
        # §5.3's rule for health applies here too: this gateway merges four fields and prunes none.
        await self.connect()
        self.brain.sessions_body = session_reply(conversation={"turns": 3})
        body = await (await self.open_session()).json()
        self.assertEqual(body["conversation"], {"turns": 3})

    async def test_the_four_merged_fields_are_the_gateways(self):
        # §4: the phase is Gateway-authoritative, so a Brain that echoes a stale phase is corrected
        # rather than forwarded -- a panel that greyed itself out from that field would be wrong.
        await self.connect()
        self.gateway.state.set_phase("agent_owned")
        self.brain.sessions_body = session_reply(session_phase="agent_owned", browserID="browser-404",
                                                 sessionID="s-1", camera_id=99)
        body = await (await self.open_session()).json()
        self.assertEqual(body["session_phase"], "setup")
        self.assertEqual(body["browserID"], "browser-1")
        self.assertEqual(body["camera_id"], 99)  # Brain owns camera selection.

    async def test_resuming_the_same_session_keeps_agent_owned_and_says_nothing(self):
        socket = await self.connect()
        self.brain.sessions_body = session_reply(sessionID="s-1")
        await self.open_session()
        self.gateway.state.set_phase("agent_owned")
        # A resume may omit the camera: it is already bound, and §9 forbids moving it.
        response = await self.open_session(sessionID="s-1", camera_id=None)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["session_phase"], "agent_owned")
        self.assertEqual(self.gateway.state.camera_id, 5)
        self.assertEqual(self.brain.sessions_bodies[-1],
                         {"browserID": "browser-1", "sessionID": "s-1", "camera_id": None, "scene": None})
        await self.assert_no_frame(socket)

    # ------------------------------------------------------------------ the gates

    async def test_a_new_session_does_not_require_a_manual_camera(self):
        await self.connect()
        response = await self.open_session(camera_id=None)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["camera_id"], 5)  # Selected by FakeBrainService.
        self.assertIsNone(self.brain.sessions_bodies[-1]["camera_id"])

    async def test_camera_id_has_to_be_a_uint32(self):
        await self.connect()
        for value in ("5", -1, 2 ** 32, True):
            response = await self.open_session(camera_id=value)
            self.assertEqual(response.status, 400, value)
            self.assertEqual((await response.json())["error"]["code"], "web_uint32_required", value)
        self.assertEqual(self.brain.sessions_bodies, [])

    async def test_the_camera_of_a_live_session_is_not_up_for_grabs(self):
        # §9: 换相机 = 新会话. `browser_new_session_required` is the code that says so.
        await self.connect()
        await self.open_session(sessionID=None)
        first = self.gateway.state.session_id
        response = await self.open_session(sessionID=first, camera_id=6)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "browser_new_session_required")
        self.assertEqual(self.gateway.state.camera_id, 5)

    async def test_a_held_lease_refuses_a_switch_and_stays_held(self):
        # §8.3: 新建 /sessions 不是解锁证据 -- so it must not even look like one.
        await self.connect()
        self.gateway.state.raw["lease"] = {"leaseToken": "L-1", "browserID": "browser-1"}
        self.gateway.state.save()
        response = await self.open_session()
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "task_busy")
        self.assertEqual(self.gateway.state.lease["leaseToken"], "L-1")
        self.assertEqual(self.brain.sessions_bodies, [])

    async def test_a_quarantine_is_not_walked_around(self):
        await self.connect()
        self.gateway.state.raw["quarantine"] = {"reason": "uncertain_commit", "transactionID": "t-1"}
        self.gateway.state.save()
        response = await self.open_session()
        self.assertEqual(response.status, 409)
        self.assertIn("resolveUncertain", (await response.json())["error"]["message"])
        self.assertEqual(self.gateway.state.quarantine["transactionID"], "t-1")

    async def test_switching_back_restores_running_task_phase_without_a_lease(self):
        await self.connect()
        self.gateway.state.start_session("s-other")
        self.gateway._session_context("s-running", create=True)["phase"] = "agent_owned"
        self.brain.sessions_body = session_reply(sessionID="s-running")
        response = await self.open_session(sessionID="s-running", allowActiveTask=True)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["session_phase"], "agent_owned")

    async def test_a_live_bound_browser_is_not_a_door(self):
        bound = await self.connect("browser-1")
        await self.open_session()
        second = await self.connect("browser-2")
        response = await self.open_session(browserID="browser-2")
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "browser_new_session_required")
        self.assertEqual(self.gateway.state.browser_id, "browser-1")
        await self.assert_no_frame(second)
        await bound.close()

    async def test_a_closed_bound_tab_hands_the_session_to_the_next_browser(self):
        socket = await self.connect("browser-1")
        await self.open_session()
        await self.disconnect(socket)
        self.brain.sessions_body = session_reply(sessionID="s-2")
        response = await self.open_session(browserID="browser-2")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.gateway.state.browser_id, "browser-2")
        self.assertEqual(self.gateway.state.session_id, "s-2")

    async def test_browserID_is_required_and_has_to_be_text(self):
        await self.connect()
        response = await self.client.post("/sessions", json={"camera_id": 5})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "MISSING_PARAM")
        response = await self.open_session(browserID=7)
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")

    async def test_sessionID_has_to_be_text_when_it_is_given(self):
        await self.connect()
        response = await self.open_session(sessionID=12)
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")

    # ------------------------------------------------------------------ the forward

    async def test_brains_own_refusal_is_passed_through_untouched(self):
        await self.connect()
        refusal = {"error": {"code": "task_busy", "message": "task 12 is running", "request_id": "r-1"}}
        self.brain.sessions_body = refusal
        self.brain.sessions_status = 409
        response = await self.open_session()
        self.assertEqual(response.status, 409)
        self.assertEqual(await response.json(), refusal)
        self.assertIsNone(self.gateway.state.session_id, "a refused switch stores nothing")

    async def test_a_reply_without_a_session_id_is_an_upstream_error(self):
        await self.connect()
        self.brain.sessions_body = {"ok": True}
        response = await self.open_session()
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "UPSTREAM_ERROR")
        self.assertIsNone(self.gateway.state.session_id)
        self.assertEqual(self.gateway.state.session_phase, "setup")

    async def test_an_unreachable_brain_leaves_the_binding_alone(self):
        socket = await self.connect("browser-1")
        await self.report(socket)
        self.gateway.config["agent_service_url"] = DEAD_URL
        response = await self.open_session()
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "UPSTREAM_ERROR")
        self.assertEqual(self.gateway.state.browser_id, "browser-1")
        self.assertIsNone(self.gateway.state.session_id)
        await self.assert_no_phase_change(socket)

    async def test_no_brain_url_at_all_is_501_not_a_silent_session(self):
        # §5.15: an empty `agent_service_url` disables the task route instead of inventing a session.
        await self.connect()
        self.gateway.config["agent_service_url"] = ""
        response = await self.open_session()
        self.assertEqual(response.status, 501)
        self.assertEqual((await response.json())["error"]["code"], "AGENT_UNAVAILABLE")

    # -------------------------------------------------------------------- read outs

    async def test_get_sessions_reads_the_binding_and_the_locks(self):
        await self.connect()
        await self.open_session()
        # §9.1's 租约闸 authenticates `leaseToken`, and every tab can read this route -- so the
        # record is shown without the credential that passes the gate.
        self.gateway.state.raw["lease"] = {"leaseToken": "L-2", "task_id": 7,
                                           "browser_id": "browser-1"}
        self.gateway.state.save()
        asked = len(self.brain.sessions_bodies)
        response = await self.client.get("/sessions", params={"browser_id": "browser-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(),
                         {"sessionID": "s-1", "session_phase": "setup", "browserID": "browser-1",
                          "camera_id": 5, "camera_candidates": [],
                          "lease": {"task_id": 7, "browser_id": "browser-1"}, "quarantine": None})
        self.assertEqual(len(self.brain.sessions_bodies), asked, "a read of our own state asks nobody")

    async def test_capabilities_are_the_eight_names_and_no_others(self):
        # §5.2: the two names from the previous draft may not come back.
        await self.connect()
        self.brain.sessions_body = session_reply(sessionID="s-1")
        await self.open_session()
        response = await self.client.get("/brainCapabilities", params={"browser_id": "browser-404"})
        body = await response.json()
        self.assertEqual(body["protocol"], BRAIN_WEB2_PROTOCOL)
        self.assertEqual(body["capabilities"], list(BRAIN_WEB2_CAPABILITIES))
        self.assertEqual(len(body["capabilities"]), 8)
        self.assertNotIn("versioned_manual_edits", body["capabilities"])
        self.assertNotIn("viewport_read_only", body["capabilities"])
        self.assertEqual(body["browserID"], "browser-1", "the answer names the bound browser, not the caller")

    async def test_the_session_state_readout_and_the_capabilities_agree_about_the_binding(self):
        # §4: three read points, one source -- a browser that refreshes must not be told two things.
        await self.connect("browser-7")
        self.brain.sessions_body = session_reply(browserID="browser-7", sessionID="s-3")
        await self.open_session(browserID="browser-7")
        state = await (await self.client.get("/api/sessionState", params={"browser_id": "browser-7"})).json()
        caps = await (await self.client.get("/brainCapabilities", params={"browser_id": "browser-7"})).json()
        self.assertEqual(state["browser_id"], caps["browserID"])
        self.assertEqual(state["session_phase"], "setup")

    async def test_session_state_reconciles_a_persisted_session_before_the_next_task(self):
        # Brain may have lost the active row while Gateway retained its durable id. A refresh is
        # enough to reactivate the same browser/session; callers must not wait for a task to see
        # the stale `session_browser_not_active` transport error.
        await self.connect("browser-7")
        self.brain.sessions_body = session_reply(browserID="browser-7", sessionID="s-3")
        await self.open_session(browserID="browser-7")
        self.brain.sessions_bodies.clear()
        response = await self.client.get("/api/sessionState", params={"browser_id": "browser-7"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.brain.sessions_bodies[-1],
                         {"browserID": "browser-7", "sessionID": "s-3", "camera_id": None})

    async def test_automatic_response_before_explicit_response_keeps_explicit_session(self):
        await self.connect()
        automatic_started = asyncio.Event()
        explicit_started = asyncio.Event()
        release_automatic = asyncio.Event()
        release_explicit = asyncio.Event()
        request_count = 0

        async def controlled_request(method, path, *, params=None, json_body=None):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                automatic_started.set()
                await release_automatic.wait()
                session_id = "automatic"
            else:
                explicit_started.set()
                await release_explicit.wait()
                session_id = "explicit"
            return 200, session_reply(sessionID=session_id, browserID="browser-1")

        self.gateway.agent_relay.request = controlled_request
        automatic_task = asyncio.create_task(self.sync({"world": WORLD, "camera_object_ids": [5]}))
        explicit_task = None
        try:
            await asyncio.wait_for(automatic_started.wait(), timeout=1)
            explicit_task = asyncio.create_task(self.open_session())
            release_automatic.set()
            await asyncio.wait_for(explicit_started.wait(), timeout=1)
            release_explicit.set()
            response = await explicit_task
            self.assertEqual(response.status, 200, await response.text())
            await automatic_task
        finally:
            release_automatic.set()
            release_explicit.set()
            await automatic_task
            if explicit_task is not None:
                await explicit_task

        self.assertEqual(self.gateway.state.session_id, "explicit")

    async def test_late_automatic_session_response_cannot_replace_explicit_session(self):
        await self.connect()
        automatic_started = asyncio.Event()
        release_automatic = asyncio.Event()
        request_count = 0

        async def controlled_request(method, path, *, params=None, json_body=None):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                automatic_started.set()
                await release_automatic.wait()
                session_id = "automatic"
            else:
                session_id = "explicit"
            return 200, session_reply(sessionID=session_id, browserID="browser-1")

        self.gateway.agent_relay.request = controlled_request
        automatic_task = asyncio.create_task(
            self.sync({"world": WORLD, "camera_object_ids": [5]}))
        explicit_task = None
        try:
            await asyncio.wait_for(automatic_started.wait(), timeout=1)
            explicit_task = asyncio.create_task(self.open_session())
            release_automatic.set()
            response = await explicit_task
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(self.gateway.state.session_id, "explicit")
        finally:
            release_automatic.set()
            await automatic_task
            if explicit_task is not None:
                await explicit_task

        self.assertEqual(self.gateway.state.session_id, "explicit")

    async def test_overlapping_explicit_sessions_are_serialized(self):
        await self.connect()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        request_count = 0

        async def controlled_request(method, path, *, params=None, json_body=None):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                first_started.set()
                await release_first.wait()
                session_id = "stale-explicit"
            else:
                session_id = "current-explicit"
            return 200, session_reply(sessionID=session_id, browserID="browser-1")

        self.gateway.agent_relay.request = controlled_request
        first = asyncio.create_task(self.open_session())
        second = None
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second = asyncio.create_task(self.open_session())
            await asyncio.sleep(0)
            self.assertEqual(request_count, 1, "the second explicit request must wait for the transition lock")
            release_first.set()
            stale_response = await first
            self.assertEqual(stale_response.status, 200)
            current_response = await second
            self.assertEqual(current_response.status, 200, await current_response.text())
        finally:
            release_first.set()
            await first
            if second is not None:
                await second

        self.assertEqual(request_count, 2)
        self.assertEqual(self.gateway.state.session_id, "current-explicit")

    async def test_fresh_state_sync_and_session_state_reconcile_keep_one_binding(self):
        await self.connect()
        automatic_started = asyncio.Event()
        release_automatic = asyncio.Event()
        request_count = 0
        bodies = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            nonlocal request_count
            request_count += 1
            bodies.append(json_body)
            if request_count == 1:
                automatic_started.set()
                await release_automatic.wait()
            return 200, session_reply(sessionID="fresh-session", browserID="browser-1")

        self.gateway.agent_relay.request = controlled_request
        sync_task = asyncio.create_task(self.sync({"world": WORLD, "camera_object_ids": [5]}))
        state_task = None
        try:
            await asyncio.wait_for(automatic_started.wait(), timeout=1)
            state_task = asyncio.create_task(
                self.client.get("/api/sessionState", params={"browser_id": "browser-1"}))
            await asyncio.sleep(0)
            release_automatic.set()
            await sync_task
            state_response = await state_task
            self.assertEqual(state_response.status, 200, await state_response.text())
            self.assertEqual((await state_response.json())["sessionID"], "fresh-session")
        finally:
            release_automatic.set()
            await sync_task
            if state_task is not None:
                await state_task

        self.assertEqual(request_count, 2, "reconciliation should reuse the binding, not create a third session")
        self.assertNotIn("sessionID", bodies[0])
        self.assertEqual(bodies[1]["sessionID"], "fresh-session")
        self.assertEqual(self.gateway.state.session_id, "fresh-session")
        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "stale-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
            "title": "场景会话 stale"})).json()
        self.assertEqual(created["sessionID"], "fresh-session")
        self.assertEqual(self.gateway.active_conversation_id, created["conversationId"])

    async def test_fresh_reconcile_waiting_for_scene_does_not_overwrite_automatic_binding(self):
        await self.connect()
        started, release = asyncio.Event(), asyncio.Event()
        bodies = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            bodies.append(json_body)
            if len(bodies) == 1:
                started.set()
                await release.wait()
                return 200, session_reply(sessionID="stale-bootstrap", browserID="browser-1")
            return 200, session_reply(sessionID="scene-session", browserID="browser-1")

        self.gateway.agent_relay.request = controlled_request
        state_task = asyncio.create_task(self.client.get("/api/sessionState", params={"browser_id": "browser-1"}))
        sync_task = None
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            sync_task = asyncio.create_task(self.sync({"world": WORLD, "camera_object_ids": [5]}))
            await asyncio.sleep(0)
            self.assertEqual(len(bodies), 1)
            release.set()
            await state_task
            await sync_task
        finally:
            release.set()
            await state_task
            if sync_task is not None:
                await sync_task
        self.assertEqual(self.gateway.state.session_id, "scene-session")
        response = await self.client.post("/api/agent/conversations", json={
            "sessionID": "stale-bootstrap", "scene": WORLD})
        self.assertEqual(response.status, 201)
        self.assertEqual((await response.json())["sessionID"], "scene-session")

    async def test_history_activation_transfers_each_sessions_own_lease(self):
        await self.connect()
        self.gateway.state.start_session("old-session", camera_id=5, phase="agent_owned")
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        lease = {"leaseToken": "old-token", "session_id": "old-session",
                 "scene_identity": self.gateway.state.scene_identity, "world_snapshot": WORLD}
        self.gateway.state.acquire_lease(lease)
        old = {"conversationId": "old-row", "sessionID": "old-session"}
        new = {"conversationId": "new-row", "sessionID": "new-session"}
        self.brain.sessions_body = session_reply(sessionID="new-session")
        await self.gateway._activate_conversation_session(new)
        self.assertIsNone(self.gateway.state.lease)
        self.assertEqual(self.gateway.session_contexts["old-session"]["lease"], lease)
        self.brain.sessions_body = session_reply(sessionID="old-session")
        await self.gateway._activate_conversation_session(old)
        self.assertEqual(self.gateway.state.lease, lease)
        self.assertEqual(self.gateway.state.session_phase, "agent_owned")

    async def test_history_activation_restores_quarantine_for_recovery_but_cannot_leave_it(self):
        from local_gateway import BrowserError
        await self.connect()
        self.gateway.state.start_session("old-session")
        target = {"conversationId": "new-row", "sessionID": "new-session"}
        self.gateway.state.raw["quarantine"] = {"transactionID": "uncertain"}
        with self.assertRaises(BrowserError):
            await self.gateway._activate_conversation_session(target)
        self.assertEqual(self.gateway.state.session_id, "old-session")
        self.gateway.state.raw["quarantine"] = None
        context = self.gateway._session_context("new-session", create=True)
        context["world_snapshot"] = WORLD
        context["quarantine"] = {"session_id": "new-session", "retired_lease_token": "retired"}
        await self.gateway._activate_conversation_session(target)
        self.assertEqual(self.gateway.state.session_id, "new-session")
        self.assertEqual(self.gateway.state.quarantine, context["quarantine"])
        self.assertEqual(self.gateway.state.session_phase, "agent_owned")
        self.assertEqual(self.gateway.world_cache, WORLD)
        self.assertEqual(self.brain.sessions_bodies, [])
        evidence = {"receipt_status": "missing", "live_digest": None,
                    "expected_digest": None, "late_write_excluded": False}
        body = {"browserID": "browser-1", "sessionID": "new-session", "transactionID": None,
                "resolution": "resume", "evidence": evidence}
        response = await self.client.post("/resolveUncertain", json=body)
        self.assertEqual(response.status, 200)
        self.assertIn("recovery_task_required", (await response.json())["reason"])
        self.assertIsNotNone(self.gateway.state.quarantine)

        async def live_read(*args, **kwargs):
            return {"world": WORLD}
        self.gateway.dispatch = live_read
        async def authoritative_task(method, path, **kwargs):
            if path == "/queryTaskState":
                return 200, {"terminal": True}
            return 200, {"sessionID": "new-session", "recovery_context": {"write_attempted": False}}
        self.gateway.agent_relay.request = authoritative_task
        body.update(taskID=12, expected_digest=self.gateway.state.scene_identity["sceneDigest"])
        evidence.update(late_write_excluded=True,
                        expected_digest=self.gateway.state.scene_identity["sceneDigest"])
        response = await self.client.post("/resolveUncertain", json=body)
        self.assertEqual((await response.json())["state"], "resolved")
        self.assertIsNone(self.gateway.state.quarantine)
        self.assertIsNone(context["quarantine"])

    async def test_unconfirmed_commit_blocks_history_and_explicit_switches_before_and_after_relay(self):
        from local_gateway import BrowserError
        await self.connect()
        self.gateway.state.start_session("old-session")
        target = {"conversationId": "new-row", "sessionID": "new-session"}
        self.gateway.state.acquire_lease({"leaseToken": "held", "commit_unconfirmed": True})
        with self.assertRaises(BrowserError):
            await self.gateway._activate_conversation_session(target)
        response = await self.open_session(allowActiveTask=True)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.brain.sessions_bodies, [])

        async def begin_commit(method, path, *, params=None, json_body=None):
            self.gateway.state.lease["commit_unconfirmed"] = True
            return 200, session_reply(sessionID="new-session")
        self.gateway.agent_relay.request = begin_commit
        self.gateway.state.lease.pop("commit_unconfirmed")
        with self.assertRaises(BrowserError):
            await self.gateway._activate_conversation_session(target)
        self.gateway.state.lease.pop("commit_unconfirmed")
        response = await self.open_session(allowActiveTask=True)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.gateway.state.session_id, "old-session")
        self.assertEqual(self.gateway.state.lease["leaseToken"], "held")

    async def test_reconcile_replacement_updates_active_history_binding(self):
        await self.connect()
        self.gateway.state.start_session("old-session")
        self.gateway.conversations["row"] = {"conversationId": "row", "sessionID": "old-session"}
        self.gateway.active_conversation_id = "row"
        self.brain.sessions_body = session_reply(sessionID="replacement")
        await self.gateway._reconcile_brain_session("browser-1")
        self.assertEqual(self.gateway.conversations["row"]["sessionID"], "replacement")
        self.assertEqual(self.gateway.state.session_id, "replacement")

    async def test_reconcile_does_not_replace_session_that_acquired_lease_during_request(self):
        await self.connect()
        self.gateway.state.start_session("old-session")

        async def controlled_request(method, path, *, params=None, json_body=None):
            self.gateway.state.acquire_lease({"leaseToken": "held", "session_id": "old-session"})
            return 200, session_reply(sessionID="replacement")

        self.gateway.agent_relay.request = controlled_request
        await self.gateway._reconcile_brain_session("browser-1")
        self.assertEqual(self.gateway.state.session_id, "old-session")
        self.assertEqual(self.gateway.state.lease["leaseToken"], "held")

    async def test_failed_explicit_request_does_not_discard_pending_automatic_rebind(self):
        await self.connect()
        automatic_started = asyncio.Event()
        release_automatic = asyncio.Event()
        request_count = 0

        async def controlled_request(method, path, *, params=None, json_body=None):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                automatic_started.set()
                await release_automatic.wait()
                return 200, session_reply(sessionID="automatic", browserID="browser-1")
            return 409, {"error": {"code": "task_busy", "message": "busy"}}

        self.gateway.agent_relay.request = controlled_request
        automatic_task = asyncio.create_task(self.sync({"world": WORLD, "camera_object_ids": [5]}))
        explicit_task = None
        try:
            await asyncio.wait_for(automatic_started.wait(), timeout=1)
            explicit_task = asyncio.create_task(self.open_session())
            release_automatic.set()
            response = await explicit_task
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"]["code"], "task_busy")
            release_automatic.set()
            await automatic_task
        finally:
            release_automatic.set()
            await automatic_task
            if explicit_task is not None:
                await explicit_task

        self.assertEqual(self.gateway.state.session_id, "automatic")

    async def test_delayed_scene_report_after_explicit_session_preserves_previous_history(self):
        self.brain.sessions_body = session_reply(sessionID="old-session", browserID="browser-1")
        await self.connect()
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        self.gateway.conversations["old-row"] = {
            "conversationId": "old-row", "sessionID": "old-session",
            "sceneBinding": {"sceneId": 7, "simSessionId": "old-session"},
            "messages": [], "sceneSnapshot": WORLD,
        }
        self.gateway.active_conversation_id = "old-row"

        explicit_started = asyncio.Event()
        release_explicit = asyncio.Event()

        async def controlled_request(method, path, *, params=None, json_body=None):
            explicit_started.set()
            await release_explicit.wait()
            return 200, session_reply(sessionID="new-session", browserID="browser-1")

        self.gateway.agent_relay.request = controlled_request
        next_world = {**WORLD, "scene_version": WORLD["scene_version"] + 1}
        explicit_task = asyncio.create_task(self.open_session())
        sync_task = None
        try:
            await asyncio.wait_for(explicit_started.wait(), timeout=1)
            sync_task = asyncio.create_task(self.sync({"world": next_world, "camera_object_ids": [5]}))
            await asyncio.sleep(0)
            release_explicit.set()
            response = await explicit_task
            self.assertEqual(response.status, 200, await response.text())
            await sync_task
        finally:
            release_explicit.set()
            await explicit_task
            if sync_task is not None:
                await sync_task

        self.assertEqual(self.gateway.conversations["old-row"]["sessionID"], "old-session")
        self.assertEqual(self.gateway.conversations["old-row"]["sceneBinding"]["simSessionId"], "old-session")

    async def test_explicit_sync_marker_survives_old_report_until_new_scene_arrives(self):
        await self.connect()
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        previous_session = self.gateway.state.session_id
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": previous_session, "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            return 200, session_reply(sessionID="explicit-session", browserID="browser-1", camera_id=None)

        self.gateway.agent_relay.request = controlled_request
        empty = {"scene_schema_version": 1, "scene_id": 0, "scene_name": "",
                 "scene_version": 0, "scene_sub_version": 0, "objects": []}
        response = await self.open_session(expectedScene=empty)
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(calls[0]["camera_id"],
                         "a reset-to-empty session must not inherit the old scene camera")
        marker = self.gateway._explicit_session_sync_pending
        self.assertEqual(marker["session_id"], "explicit-session")
        self.assertIsNone(marker["camera_id"])

        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        self.assertEqual(self.gateway._explicit_session_sync_pending, marker)
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], previous_session)
        next_world = {**empty, "scene_sub_version": 0}
        await self.sync({"world": next_world, "camera_object_ids": []})
        self.assertIsNotNone(self.gateway._explicit_session_sync_pending)
        self.assertEqual(len(calls), 1, "the expected camera-less scene must keep the new session")
        self.assertEqual(self.gateway.state.session_id, "explicit-session")
        self.assertIsNone(self.gateway.state.camera_id)
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], previous_session)
        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "explicit-session", "sceneID": 0, "scene": next_world,
        })).json()
        self.assertEqual(created["sessionID"], "explicit-session")
        self.assertEqual(self.gateway.active_conversation_id, created["conversationId"])
        self.assertIsNone(self.gateway._explicit_session_sync_pending)

    async def test_history_activation_report_cannot_rebind_old_row_before_selection_finishes(self):
        await self.connect()
        self.gateway.state.start_session("old-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        self.gateway.camera_candidates = [5]
        self.gateway.camera_candidates_scene = self.gateway.state.scene_identity
        old = {"conversationId": "old-row", "sessionID": "old-session", "sceneSnapshot": WORLD}
        target = {"conversationId": "target-row", "sessionID": "target-session", "sceneSnapshot": WORLD}
        self.gateway.conversations.update({"old-row": old, "target-row": target})
        self.gateway.active_conversation_id = "old-row"
        target_context = self.gateway._session_context("target-session", create=True)
        target_context["world_snapshot"] = WORLD
        target_context["scene_identity"] = self.gateway.state.scene_identity
        target_context["camera_id"] = 5

        async def activate_target(method, path, *, params=None, json_body=None):
            return 200, session_reply(sessionID="target-session", camera_id=5)

        self.gateway.agent_relay.request = activate_target
        phase_waiting, finish_phase = asyncio.Event(), asyncio.Event()

        async def pause_activation(_previous_phase):
            phase_waiting.set()
            await finish_phase.wait()

        self.gateway._announce_phase = pause_activation
        activation = asyncio.create_task(self.gateway._activate_conversation_session(target))
        await phase_waiting.wait()
        await self.sync({"world": WORLD, "camera_object_ids": [5]})

        self.assertEqual(old["sessionID"], "old-session")
        self.assertEqual(old["sceneSnapshot"], WORLD)
        self.assertEqual(self.gateway.active_conversation_id, "old-row")

        finish_phase.set()
        await activation
        self.assertEqual(self.gateway.active_conversation_id, "target-row")
        self.assertEqual(target["sessionID"], "target-session")

    async def test_explicit_new_conversation_protects_history_when_brain_reuses_session_id(self):
        await self.connect()
        self.gateway.state.start_session("same-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        self.gateway.camera_candidates = [5]
        self.gateway.camera_candidates_scene = self.gateway.state.scene_identity
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "same-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            return 200, session_reply(sessionID="same-session", camera_id=None)

        self.gateway.agent_relay.request = controlled_request
        empty = {"scene_schema_version": 1, "scene_id": 0, "scene_name": "",
                 "scene_version": 0, "scene_sub_version": 0, "objects": []}
        response = await self.open_session(expectedScene=empty)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.gateway.state.session_id, "same-session")
        await self.sync({"world": empty, "camera_object_ids": []})

        self.assertEqual(len(calls), 2, "the scene change still validates the session camera")
        self.assertEqual(self.gateway.state.session_id, "same-session")
        self.assertEqual(old["sessionID"], "same-session")
        self.assertEqual(old["sceneSnapshot"], WORLD)
        self.assertIsNotNone(self.gateway._explicit_session_sync_pending)

        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "same-session", "sceneID": 0, "scene": empty,
        })).json()
        self.assertEqual(created["sessionID"], "same-session")
        self.assertEqual(self.gateway.active_conversation_id, created["conversationId"])
        self.assertIsNone(self.gateway._explicit_session_sync_pending)

    async def test_same_session_id_creation_without_expected_scene_keeps_old_row_protected(self):
        await self.connect()
        self.gateway.state.start_session("same-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        old = {"conversationId": "old-row", "sessionID": "same-session", "sceneSnapshot": WORLD}
        self.gateway.conversations["old-row"] = old
        self.gateway.active_conversation_id = "old-row"

        async def controlled_request(method, path, *, params=None, json_body=None):
            return 200, session_reply(sessionID="same-session", camera_id=5)

        self.gateway.agent_relay.request = controlled_request
        response = await self.open_session()

        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.gateway.state.session_id, "same-session")
        self.assertEqual(old["sessionID"], "same-session")
        self.assertEqual(old["sceneSnapshot"], WORLD)
        self.assertIsNone(self.gateway._explicit_session_sync_pending["expected"])
        self.assertEqual(self.gateway._explicit_session_sync_pending["protected_conversation_id"], "old-row")

    async def test_explicit_session_adopts_the_scene_selected_after_its_history_row_is_created(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        first = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        self.brain.sessions_body = session_reply(sessionID="second-session", browserID="browser-1")
        response = await self.open_session()
        self.assertEqual(response.status, 200, await response.text())

        # The user may create the new session before the asset panel's pending scene choice is
        # replayed. A delayed report of the previous scene must not rewrite the old transcript.
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        self.assertEqual(self.gateway.conversations[first["conversationId"]]["sessionID"], "first-session")
        self.assertIsNotNone(self.gateway._explicit_session_sync_pending)

        second = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "selected",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 5}]}
        await self.sync({"world": selected_scene, "camera_object_ids": [5]})

        active = self.gateway.conversations[second["conversationId"]]
        self.assertEqual(self.gateway.state.camera_id, 5,
                         "scene adoption must preserve the explicit session's still-present camera")
        self.assertEqual(active["sessionID"], "second-session")
        self.assertEqual(active["sceneBinding"]["sceneId"], 8)
        self.assertEqual(active["sceneSnapshot"], selected_scene)
        context = self.gateway.session_contexts["second-session"]
        self.assertEqual(context["world_snapshot"], selected_scene)
        self.assertEqual(context["scene_identity"]["scene_id"], 8)
        self.assertEqual(self.gateway.state.session_id, "second-session")
        self.assertEqual(len(self.brain.sessions_bodies), 1,
                         "the selected scene must stay in the explicitly created Brain session")

        async def activate_session(method, path, *, params=None, json_body=None):
            return 200, session_reply(sessionID=json_body["sessionID"], browserID="browser-1")

        self.gateway.agent_relay.request = activate_session
        await self.gateway._activate_conversation_session(self.gateway.conversations[first["conversationId"]])
        await self.gateway._activate_conversation_session(active)
        self.assertEqual(self.gateway.state.session_id, "second-session")
        self.assertEqual(self.gateway.world_cache, selected_scene,
                         "returning to session 2 must restore the scene selected for its task")
        self.assertEqual(len(self.brain.sessions_bodies), 1)

    async def test_explicit_session_rebinds_when_selected_scene_loses_bound_camera(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        self.brain.sessions_body = session_reply(sessionID="second-session", camera_id=5)
        self.assertEqual((await self.open_session()).status, 200)
        second = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "other camera",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}
        self.brain.sessions_body = session_reply(sessionID="third-session", camera_id=6)
        await self.sync({"world": selected_scene, "camera_object_ids": [6]})

        self.assertEqual(self.gateway.state.session_id, "third-session")
        self.assertEqual(self.gateway.state.camera_id, 6)
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sessionID"], "third-session")
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sceneSnapshot"], selected_scene)
        self.assertEqual(self.gateway._explicit_session_sync_pending, None)

    async def test_explicit_session_retries_camera_rebind_after_brain_refusal(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            if len(calls) == 1:
                return 200, session_reply(sessionID="second-session", camera_id=5)
            if len(calls) == 2:
                return 409, {"error": {"code": "task_busy", "message": "busy"}}
            return 200, session_reply(sessionID="third-session", camera_id=6)

        self.gateway.agent_relay.request = controlled_request
        self.assertEqual((await self.open_session()).status, 200)
        second = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "other camera",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}

        await self.sync({"world": selected_scene, "camera_object_ids": [6]})
        self.assertEqual(self.gateway.state.session_id, "second-session")
        self.assertTrue(self.gateway._session_rebind_required)
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sessionID"], "second-session")

        await self.sync({"world": selected_scene, "camera_object_ids": [6]})
        self.assertEqual(self.gateway.state.session_id, "third-session")
        self.assertFalse(self.gateway._session_rebind_required)
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sessionID"], "third-session")
        self.assertIsNone(calls[1]["camera_id"])
        self.assertIsNone(calls[2]["camera_id"])

    async def test_repeated_camera_rebind_refusals_preserve_previous_history_until_new_row_exists(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "selected",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            if len(calls) == 1:
                return 200, session_reply(sessionID="second-session", camera_id=None)
            return 409, {"error": {"code": "task_busy", "message": "busy"}}

        self.gateway.agent_relay.request = controlled_request
        response = await self.open_session(expectedScene=selected_scene)
        self.assertEqual(response.status, 200, await response.text())

        await self.sync({"world": selected_scene, "camera_object_ids": [6]})
        await self.sync({"world": selected_scene, "camera_object_ids": [6]})

        self.assertEqual(len(calls), 3)
        self.assertTrue(self.gateway._session_rebind_required)
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], "first-session")
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sceneSnapshot"], WORLD)

        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": selected_scene["scene_id"], "scene": selected_scene,
        })).json()
        self.assertEqual(created["sessionID"], "second-session")
        self.assertEqual(self.gateway.active_conversation_id, created["conversationId"])

    async def test_successful_camera_rebind_does_not_rewrite_previous_history_before_new_row_exists(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "selected",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            if len(calls) == 1:
                return 200, session_reply(sessionID="second-session", camera_id=None)
            return 200, session_reply(sessionID="rebound-session", camera_id=6)

        self.gateway.agent_relay.request = controlled_request
        response = await self.open_session(expectedScene=selected_scene)
        self.assertEqual(response.status, 200, await response.text())
        await self.sync({"world": selected_scene, "camera_object_ids": [6]})

        self.assertEqual(self.gateway.state.session_id, "rebound-session")
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], "first-session")
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sceneSnapshot"], WORLD)
        self.assertIsNotNone(self.gateway._explicit_session_sync_pending)

        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "rebound-session", "sceneID": selected_scene["scene_id"], "scene": selected_scene,
        })).json()
        self.assertEqual(created["sessionID"], "rebound-session")
        self.assertEqual(self.gateway.active_conversation_id, created["conversationId"])
        self.assertIsNone(self.gateway._explicit_session_sync_pending)

    async def test_delayed_history_row_clears_marker_after_a_different_scene_was_reported(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        expected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "expected",
                          "scene_version": 0, "scene_sub_version": 0, "objects": []}
        actual_scene = {"scene_schema_version": 1, "scene_id": 9, "scene_name": "actual",
                        "scene_version": 0, "scene_sub_version": 0,
                        "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            if len(calls) == 1:
                return 200, session_reply(sessionID="second-session", camera_id=None)
            return 200, session_reply(sessionID="third-session", camera_id=6)

        self.gateway.agent_relay.request = controlled_request
        response = await self.open_session(expectedScene=expected_scene)
        self.assertEqual(response.status, 200, await response.text())
        await self.sync({"world": actual_scene, "camera_object_ids": []})

        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], "first-session")
        marker = self.gateway._explicit_session_sync_pending
        self.assertTrue(marker["scene_report_received"])
        self.assertEqual(marker["expected"]["scene_id"], 8)
        self.assertEqual(self.gateway.state.scene_identity["scene_id"], 9)

        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": 9, "scene": actual_scene,
        })).json()
        self.assertIsNone(self.gateway._explicit_session_sync_pending)

        await self.sync({"world": actual_scene, "camera_object_ids": [6]})
        self.assertEqual(self.gateway.state.session_id, "third-session")
        self.assertEqual(self.gateway.conversations[created["conversationId"]]["sessionID"], "third-session")
        self.assertEqual(self.gateway.conversations[created["conversationId"]]["sceneSnapshot"], actual_scene)
        self.assertEqual(len(calls), 2)

    async def test_delayed_history_row_clears_marker_without_expected_scene(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=None)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        self.gateway.camera_candidates = []
        self.gateway.camera_candidates_scene = self.gateway.state.scene_identity
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        actual_scene = {"scene_schema_version": 1, "scene_id": 9, "scene_name": "actual",
                        "scene_version": 0, "scene_sub_version": 0,
                        "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            if len(calls) == 1:
                return 200, session_reply(sessionID="second-session", camera_id=None)
            return 200, session_reply(sessionID="third-session", camera_id=6)

        self.gateway.agent_relay.request = controlled_request
        response = await self.open_session(camera_id=None)
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(self.gateway._explicit_session_sync_pending["expected"])
        await self.sync({"world": WORLD, "camera_object_ids": []})
        await self.sync({"world": actual_scene, "camera_object_ids": []})

        marker = self.gateway._explicit_session_sync_pending
        self.assertTrue(marker["scene_report_received"])
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], "first-session")
        created = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": 9, "scene": actual_scene,
        })).json()
        self.assertIsNone(self.gateway._explicit_session_sync_pending)

        await self.sync({"world": actual_scene, "camera_object_ids": [6]})
        self.assertEqual(self.gateway.state.session_id, "third-session")
        self.assertEqual(self.gateway.conversations[created["conversationId"]]["sessionID"], "third-session")
        self.assertEqual(len(calls), 2)

    async def test_explicit_session_rebinds_when_selected_scene_has_no_camera(self):
        await self.connect()
        self.gateway.state.start_session("first-session", camera_id=5)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "first-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        self.brain.sessions_body = session_reply(sessionID="second-session", camera_id=5)
        self.assertEqual((await self.open_session()).status, 200)
        second = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "second-session", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "no camera",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}]}
        self.brain.sessions_body = session_reply(sessionID="third-session", camera_id=None)
        await self.sync({"world": selected_scene, "camera_object_ids": []})

        self.assertEqual(self.gateway.state.session_id, "third-session")
        self.assertIsNone(self.gateway.state.camera_id)
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sessionID"], "third-session")
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sceneSnapshot"], selected_scene)
        self.assertIsNone(self.gateway._explicit_session_sync_pending)

    async def test_camera_less_session_rebinds_when_same_scene_later_reports_candidates(self):
        await self.connect()
        self.gateway.state.start_session("camera-less", camera_id=None)
        self.gateway.state.record_scene(WORLD)
        self.gateway._cache_world(WORLD)
        self.gateway.camera_candidates = []
        self.gateway.camera_candidates_scene = self.gateway.state.scene_identity
        await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "camera-less", "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()

        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            return 200, session_reply(sessionID="camera-bound", camera_id=5)

        self.gateway.agent_relay.request = controlled_request
        await self.sync({"world": WORLD, "camera_object_ids": [5]})

        self.assertEqual(self.gateway.state.session_id, "camera-bound")
        self.assertEqual(self.gateway.state.camera_id, 5)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["camera_id"])

    async def test_camera_less_explicit_session_rebinds_when_first_scene_report_has_camera(self):
        await self.connect()
        await self.sync({"world": WORLD, "camera_object_ids": [5]})
        previous_session = self.gateway.state.session_id
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": previous_session, "sceneID": WORLD["scene_id"], "scene": WORLD,
        })).json()
        calls = []

        async def controlled_request(method, path, *, params=None, json_body=None):
            calls.append(json_body)
            if len(calls) == 1:
                return 200, session_reply(sessionID="empty-session", camera_id=None)
            return 200, session_reply(sessionID="camera-session", camera_id=6)

        self.gateway.agent_relay.request = controlled_request
        empty = {"scene_schema_version": 1, "scene_id": 0, "scene_name": "",
                 "scene_version": 0, "scene_sub_version": 0, "objects": []}
        response = await self.open_session(expectedScene=empty)
        self.assertEqual(response.status, 200, await response.text())
        second = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "empty-session", "sceneID": 0, "scene": empty,
        })).json()

        selected_scene = {"scene_schema_version": 1, "scene_id": 8, "scene_name": "camera scene",
                          "scene_version": 0, "scene_sub_version": 0,
                          "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 6}]}
        await self.sync({"world": selected_scene, "camera_object_ids": [6]})

        self.assertEqual(calls[0]["camera_id"], None)
        self.assertIsNone(calls[1]["camera_id"])
        self.assertEqual(self.gateway.state.session_id, "camera-session")
        self.assertEqual(self.gateway.state.camera_id, 6)
        self.assertEqual(self.gateway.conversations[old["conversationId"]]["sessionID"], previous_session)
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sessionID"], "camera-session")
        self.assertEqual(self.gateway.conversations[second["conversationId"]]["sceneSnapshot"], selected_scene)

    # --------------------------------------------------------- 相机候选清单 (§9 item 3)

    async def sync(self, payload):
        """One `browser.state_sync` awaited straight through the handler that adopts it.

        A socket send would return before the gateway has read the frame, and these cases are about
        what the read routes show afterwards -- so the await is the test's only sync point.
        """
        await self.gateway.apply_state_sync({"type": "browser.state_sync", "payload": payload},
                                            browser_id="browser-1")

    async def readouts(self):
        state = await (await self.client.get("/api/sessionState",
                                             params={"browser_id": "browser-1"})).json()
        sessions = await (await self.client.get("/sessions", params={"browser_id": "browser-1"})).json()
        return state["camera_candidates"], sessions["camera_candidates"]

    async def test_a_reported_camera_list_shows_on_both_read_routes(self):
        # §9: 只提供候选清单 -- the browser says which objects carry a `Camera`, both readouts say it back.
        socket = await self.connect()
        await socket.send_json({"type": "browser.state_sync",
                                "payload": {"world": WORLD, "camera_object_ids": [4, 0]}})
        for _ in range(200):
            if self.gateway.camera_candidates == [4, 0]:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.gateway.camera_candidates, [4, 0], "no state_sync frame fed the list")
        self.assertEqual(await self.readouts(), ([4, 0], [4, 0]))

    async def test_the_shape_is_ours_but_the_order_is_the_browsers(self):
        await self.sync({"world": WORLD, "camera_object_ids": ["4", True, 2 ** 32, -1, 7, 3, 3, None]})
        self.assertEqual(await self.readouts(), ([7, 3], [7, 3]))

    async def test_a_report_without_a_list_empties_it(self):
        await self.sync({"world": WORLD, "camera_object_ids": [4]})
        self.assertEqual((await self.readouts())[0], [4])
        # A re-report of the same scene that names no cameras is a scene with no cameras, not a
        # missing field: keeping the old list would be the 旧值 this clause refuses.
        await self.sync({"world": WORLD})
        self.assertEqual(await self.readouts(), ([], []))

    async def test_a_list_about_another_scene_is_not_shown(self):
        await self.sync({"world": WORLD, "camera_object_ids": [4]})
        self.gateway.state.record_scene({**WORLD, "scene_version": 4})
        self.assertEqual(await self.readouts(), ([], []))

    async def test_a_re_report_of_the_stored_scene_repairs_the_list_after_a_commit_lands(self):
        # §5.8 (S6-b): a commit moves the stored four-tuple inside the gateway, and nobody reports
        # the new document -- so the list, which only a browser can read out of a world, pairs to
        # nothing and the 新建会话 door goes dead exactly while the Agent owns the scene. The
        # browser's re-report is the repair, and §1.2 has to read it as `idempotent`: the same
        # document cannot "advance" a version that was just recorded from it.
        await self.sync({"world": WORLD, "camera_object_ids": [4]})
        committed = {**WORLD, "scene_version": 4, "objects": WORLD["objects"] + [{"object_id_in_scene": 9}]}
        self.gateway.state.set_phase("agent_owned")
        self.gateway.state.record_scene(committed)
        self.assertEqual(await self.readouts(), ([], []), "the commit left the list paired to v3")
        await self.sync({"world": committed, "camera_object_ids": [4, 9]})
        self.assertEqual(await self.readouts(), ([4, 9], [4, 9]))
        self.assertEqual(self.gateway.state.scene_identity["scene_version"], 4)
        self.assertEqual(self.gateway.state.scene_identity["scene_sub_version"], 0,
                         "an idempotent re-report is not a ② 类 edit and must not bump the counter")

    async def test_the_shortlist_is_not_a_gate(self):
        # 选定归 Brain: a `camera_id` the browser never reported still opens a session here -- the
        # scene itself answers with `CAMERA_NOT_FOUND`, and a second opinion from the gateway would
        # be a guess about a document it has not read.
        await self.sync({"world": WORLD, "camera_object_ids": [4]})
        response = await self.open_session(camera_id=9)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.brain.sessions_bodies[-1]["camera_id"], 9)
        self.assertEqual(self.gateway.camera_candidates, [4])

    # ----------------------------------------------------------- the other edge (§4)

    async def test_an_accepted_task_flips_the_phase_and_a_second_task_says_it_nothing(self):
        socket = await self.connect()
        response = await self.submit(socket)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.gateway.state.session_phase, "agent_owned")
        frame = await self.next_frame(socket, "session.phase_changed")
        self.assertEqual(frame["payload"], {"browser_id": "browser-1", "session_phase": "agent_owned"})
        # The acceptance's own job frame, so the silence check below starts from an empty queue.
        frame = await self.next_frame(socket, "agent.job_updated")
        self.assertEqual(frame["payload"]["command"], "pick the cup")
        # §7.1: one session holds one running task, so a second command is refused before the
        # browser is asked anything again -- and a refusal moves no phase.
        busy = await self.client.post("/api/agent/jobs", json={"command": "and again", "scene": WORLD,
                                                              "clientRequestID": "req-2"})
        self.assertEqual(busy.status, 409)
        self.assertEqual((await busy.json())["error"]["code"], "task_busy")
        self.assertEqual(self.brain.start_task_calls, 1)
        await self.assert_no_frame(socket)
        # §7.4: terminal releases the first lease back to setup. A later task in the same
        # session therefore announces the setup -> agent_owned edge again.
        self.brain.state_terminal = True
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        again = await self.submit(socket, command="and again", client_id="req-2")
        self.assertEqual(again.status, 200, await again.text())
        self.assertEqual(self.brain.start_task_calls, 2)
        frame = await self.next_frame(socket, "session.phase_changed")
        self.assertEqual(frame["payload"], {"browser_id": "browser-1", "session_phase": "agent_owned"})

    async def test_a_task_that_was_never_accepted_does_not_own_the_scene(self):
        socket = await self.connect()
        self.brain.start_task_status = 500
        response = await self.submit(socket)
        self.assertEqual(response.status, 502)
        self.assertEqual(self.gateway.state.session_phase, "setup")
        await self.assert_no_frame(socket)


class HealthBindingReadoutTests(unittest.IsolatedAsyncioTestCase):
    """§5.3's promise that S3 would make: the service status bar names the bound browser."""

    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-sessions-health-")
        self.addCleanup(self.workspace.cleanup)
        self.gateway = StandaloneGateway(config=config(agent_service_url=DEAD_URL,
                                                       library_server_url="http://ols.invalid:8000"),
                                         state_path=Path(self.workspace.name) / "gateway_state.json")
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/api/health", self.gateway.health)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def connect(self, browser_id):
        socket = await self.client.ws_connect("/events")
        await socket.send_json({"type": "browser.hello", "browser_id": browser_id, "role": "viewer"})
        for _ in range(200):
            if browser_id in self.gateway.browsers:
                return socket
            await asyncio.sleep(0.005)
        self.fail(f"browser.hello did not register {browser_id}")

    async def browser_item(self):
        body = await (await self.client.get("/api/health")).json()
        return body["browser"]

    async def test_two_tabs_still_name_one_bound_browser(self):
        # §7.2 left this as "null while the table is ambiguous"; the binding on disk settled it.
        first = await self.connect("browser-1")
        await self.connect("browser-2")
        self.assertEqual(await self.browser_item(), {"connected": True, "browser_id": "browser-1"})
        await first.close()

    async def test_a_browser_that_never_helloed_is_not_connected(self):
        socket = await self.client.ws_connect("/events")
        self.assertEqual(await self.browser_item(), {"connected": False, "browser_id": None})
        await socket.close()


if __name__ == "__main__":
    unittest.main()
