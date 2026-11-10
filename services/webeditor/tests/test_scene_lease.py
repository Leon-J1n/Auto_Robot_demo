"""§7.2/§8.3: the scene lease, the gate it puts in front of a collection, and the one way out.

Three things are pinned here, in the order the protocol walks them. The lock: an acquire is only
granted against the 现场 the caller can name, it is idempotent for its own holder, and it is not
walkable around -- while it stands, no second task takes it and no quarantine lets one in.
The gate (§9.1): a capture or a measurement under an Agent-owned session must present the token,
and a refusal there dispatches nothing, which is what "并保锁" has to mean for a route that reads
pixels. The resolution (§8.3): the conclusion is drawn from *our* ledger (the receipt on disk plus
a live read), the caller's `evidence` is a witness that can be contradicted, `kept_locked` is a
200 rather than an error, and nothing a resolution touches may change the 现场 or the phase.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import local_gateway
from digest import scene_digest
from frame_identity import identity_fields, image_base64
from gateway_state import GatewayState, scene_identity

TRANSFORM = {"component_type": "Transform", "properties": {
    "position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1], "parent": None}}


def object_of(object_id):
    return {"object_id_in_scene": object_id, "object_name_in_scene": f"o{object_id}",
            "components": [TRANSFORM]}


def world(*, scene_version=3, objects=None, **overrides):
    document = {"scene_schema_version": 1, "scene_id": 7, "scene_name": "lease",
                "scene_version": scene_version,
                "objects": objects if objects is not None else [object_of(0), object_of(1)]}
    document.update(overrides)
    return document


BASE = world()
CANDIDATE = world(scene_version=4, objects=[object_of(0), object_of(1), object_of(9)])


def frame_payload(**overrides):
    """A §5.10 rgb answer that clears §9's identity gate: BASE's four-tuple, from browser-1, parked.

    The image is an 8x6 png because the gate asks the file what size it claims; a header that
    disagrees with `width`/`height` is a 502 before the lease question is ever interesting.
    """
    value = {"image_base64": image_base64(8, 6), "width": 8, "height": 6,
             **identity_fields(world=BASE)}
    value.update(overrides)
    return value


def box_payload(**overrides):
    value = {"visible": True, "width": 8, "height": 6, **identity_fields(world=BASE)}
    value.update(overrides)
    return value


class LeaseHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="scene-lease-")
        self.addCleanup(self.workspace.cleanup)
        self.state_path = Path(self.workspace.name) / "gateway_state.json"
        self.config = json.loads(json.dumps(local_gateway.DEFAULT_CONSTANT_CONFIG))
        self.config.update({"browser_request_timeout_seconds": 0.5, "max_payload_bytes": 65536})
        self.gateway = local_gateway.StandaloneGateway(config=self.config, state_path=self.state_path)
        # §9.1 V0: the acquire is judged against the 现场 a live read left on disk, never against
        # whatever body the caller brings.
        self.gateway.state.record_scene(BASE)
        self.gateway.state.start_session("s-1", camera_id=2, phase="agent_owned")
        self.clients = []
        self.client = await self.serving()

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    async def serving(self):
        gateway = self.gateway
        app = web.Application()
        app.router.add_get("/events", gateway.events)
        app.router.add_post("/acquireSceneLease", gateway.acquire_scene_lease)
        app.router.add_post("/releaseSceneLease", gateway.release_scene_lease)
        app.router.add_post("/resolveUncertain", gateway.resolve_uncertain)
        app.router.add_get("/captureScene", gateway.capture_scene)
        app.router.add_get("/get2DAABB", gateway.get_2d_aabb)
        app.router.add_get("/getWorld", gateway.get_world)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client

    async def connect(self, browser_id="browser-1"):
        socket = await self.client.ws_connect("/events")
        await socket.send_json({"type": "browser.hello", "browser_id": browser_id, "role": "viewer"})
        for _ in range(200):
            if browser_id in self.gateway.browsers:
                return socket
            await asyncio.sleep(0.005)
        self.fail("browser.hello did not register " + browser_id)

    async def next_frame(self, socket, want="browser.request"):
        for _ in range(50):
            frame = await socket.receive_json()
            if frame.get("type") == want:
                return frame
        self.fail(f"no {want} frame arrived")

    async def assert_no_frame(self, socket):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.2)

    async def code(self, response):
        self.assertGreaterEqual(response.status, 400)
        return (await response.json())["error"]["code"]

    def fingerprint(self, document=BASE, **overrides):
        body = {"taskID": 41, "browserID": "browser-1", **scene_identity(document)}
        body.update(overrides)
        return body

    async def acquire(self, body=None, **overrides):
        response = await self.client.post("/acquireSceneLease",
                                          json=body if body is not None else self.fingerprint(**overrides))
        return response, await response.json()

    async def acquire_leased(self, *, task_id=41):
        socket = await self.connect()
        response, body = await self.acquire(self.fingerprint(taskID=task_id))
        self.assertEqual(response.status, 200, body)
        return socket, body["leaseToken"]

    async def release(self, body):
        response = await self.client.post("/releaseSceneLease", json=body)
        return response, await response.json()

    def write_receipt(self, transaction_id, *, status, document):
        """A commit receipt on our disk, in exactly the shape `commitWorld` leaves one."""
        identity = scene_identity(document)
        self.gateway.receipts.save(transaction_id, {
            "transactionID": transaction_id, "browserID": "browser-1", "status": status,
            **identity, "rejected_objects": [], "candidate_digest": identity["sceneDigest"]})
        return identity

    def evidence(self, *, status="missing", live=None, expected=None, excluded=True):
        return {"receipt_status": status,
                "live_digest": scene_digest(live) if live is not None else None,
                "expected_digest": scene_digest(expected) if expected is not None else None,
                "late_write_excluded": excluded}

    def resolution(self, *, transaction_id=None, evidence=None, resolution="resume",
                   browser="browser-1", session="s-1"):
        return {"browserID": browser, "sessionID": session, "transactionID": transaction_id,
                "evidence": evidence if evidence is not None else self.evidence(),
                "resolution": resolution}

    async def resolve(self, body):
        response = await self.client.post("/resolveUncertain", json=body)
        return response, await response.json()

    def nothing_locked(self):
        self.gateway.state.raw["lease"] = None
        self.gateway.state.raw["quarantine"] = None

    def quarantined(self, transaction_id):
        # A hand-made record stands in for the one `keepLocked` writes, so a test can speak about a
        # transaction this gateway was never asked to settle. Step ① of §8.3 needs a lock to
        # resolve, and every conclusion that reaches the live read is standing on one.
        self.gateway.state.raw["lease"] = None
        self.gateway.state.raw["quarantine"] = {"transactionID": transaction_id}

    async def resolve_reading(self, body, live=BASE):
        """One resolution that reaches step ④: the live read is answered from the socket."""
        socket = await self.connect()
        task = asyncio.create_task(self.client.post("/resolveUncertain", json=body))
        frame = await self.next_frame(socket)
        self.assertEqual(frame["action"], "getWorld")
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": {"world": live, "request_id": frame["request_id"]}})
        response = await task
        return response, await response.json(), socket


class AcquireSceneLeaseTests(LeaseHarness):
    async def test_a_fingerprint_matching_the_现场_grants_a_token(self):
        await self.connect()
        response, body = await self.acquire()
        self.assertEqual(response.status, 200)
        self.assertEqual(sorted(body), sorted(["browserID", "leaseToken"]))
        self.assertEqual(body["browserID"], "browser-1")
        self.assertEqual(len(body["leaseToken"]), 32)
        record = self.gateway.state.lease
        self.assertEqual(record["leaseToken"], body["leaseToken"])
        self.assertEqual(record["task_id"], 41)
        self.assertEqual(record["browser_id"], "browser-1")
        self.assertEqual(record["session_id"], "s-1")
        self.assertEqual(record["scene_identity"], scene_identity(BASE))

    async def test_the_record_outlives_the_process(self):
        await self.connect()
        await self.acquire()
        reopened = GatewayState(self.state_path)
        self.assertEqual(reopened.lease, self.gateway.state.lease)

    async def test_the_same_task_re_reading_the_same_scene_keeps_its_token(self):
        await self.connect()
        _, first = await self.acquire()
        response, again = await self.acquire()
        self.assertEqual(response.status, 200)
        self.assertEqual(again["leaseToken"], first["leaseToken"])

    async def test_a_second_task_cannot_take_a_standing_lease(self):
        await self.connect()
        _, first = await self.acquire()
        response, body = await self.acquire(self.fingerprint(taskID=42))
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "task_busy")
        self.assertEqual(self.gateway.state.lease["leaseToken"], first["leaseToken"])

    async def test_a_quarantine_is_not_walked_around_by_a_new_lease(self):
        # §8.3: 新建会话、切换浏览器、重启都不是解锁证据 -- and neither is a fresh acquire.
        await self.connect()
        _, body = await self.acquire()
        await self.release({"browserID": "browser-1", "leaseToken": body["leaseToken"],
                            "keepLocked": True})
        response, refused = await self.acquire(self.fingerprint(taskID=42))
        self.assertEqual(response.status, 409)
        self.assertEqual(refused["error"]["code"], "task_busy")
        self.assertIsNone(self.gateway.state.lease)
        self.assertIsNotNone(self.gateway.state.quarantine)

    async def test_a_fingerprint_that_is_not_the_现场_is_a_conflict(self):
        await self.connect()
        response, body = await self.acquire(self.fingerprint(CANDIDATE))
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "browser_scene_version_conflict")
        self.assertIsNone(self.gateway.state.lease)

    async def test_no_stored_scene_means_no_lease(self):
        await self.connect()
        self.gateway.state.raw["scene_identity"] = None
        response, body = await self.acquire()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "browser_scene_version_conflict")

    async def test_a_browser_that_did_not_claim_the_session_cannot_lock_it(self):
        await self.connect("browser-1")
        response, body = await self.acquire(self.fingerprint(browserID="browser-9"))
        self.assertEqual(response.status, 503)
        self.assertEqual(body["error"]["code"], "NO_BROWSER_CONNECTED")
        self.assertIsNone(self.gateway.state.lease)

    async def test_a_bound_browser_that_disconnected_cannot_lock_it(self):
        socket = await self.connect()
        await socket.close()
        for _ in range(200):
            if "browser-1" not in self.gateway.browsers:
                break
            await asyncio.sleep(0.005)
        response, body = await self.acquire()
        self.assertEqual(response.status, 503)
        self.assertEqual(body["error"]["code"], "NO_BROWSER_CONNECTED")

    async def test_the_shape_gate_names_the_field_that_failed(self):
        broken = [
            ("taskID is missing", {k: v for k, v in self.fingerprint().items() if k != "taskID"}),
            ("taskID is negative", self.fingerprint(taskID=-1)),
            ("taskID is a bool", self.fingerprint(taskID=True)),
            ("taskID is Brain's own string", self.fingerprint(taskID="41")),
            ("browserID is missing", {k: v for k, v in self.fingerprint().items() if k != "browserID"}),
            ("browserID is empty", self.fingerprint(browserID="")),
            ("scene_id is missing", {k: v for k, v in self.fingerprint().items() if k != "scene_id"}),
            ("scene_id is above uint32", self.fingerprint(scene_id=2**32)),
            ("scene_version is missing",
             {k: v for k, v in self.fingerprint().items() if k != "scene_version"}),
            ("scene_version is text", self.fingerprint(scene_version="3")),
            ("scene_sub_version is missing",
             {k: v for k, v in self.fingerprint().items() if k != "scene_sub_version"}),
            ("sceneDigest is missing",
             {k: v for k, v in self.fingerprint().items() if k != "sceneDigest"}),
            ("sceneDigest is uppercase", self.fingerprint(sceneDigest=scene_digest(BASE).upper())),
            ("sceneDigest is short", self.fingerprint(sceneDigest="0" * 63)),
        ]
        await self.connect()
        for explanation, body in broken:
            with self.subTest(explanation):
                response = await self.client.post("/acquireSceneLease", json=body)
                self.assertEqual(response.status, 400, explanation)
                self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
                self.assertIsNone(self.gateway.state.lease)

    async def test_a_body_that_is_not_json_is_refused_before_the_lock(self):
        response = await self.client.post("/acquireSceneLease",
                                          data="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(await self.code(response), "INVALID_PARAM")
        self.assertIsNone(self.gateway.state.lease)


class ReleaseSceneLeaseTests(LeaseHarness):
    async def test_a_clean_release_leaves_no_record_and_no_lock(self):
        _, token = await self.acquire_leased()
        response, body = await self.release({"browserID": "browser-1", "leaseToken": token,
                                             "keepLocked": False})
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"browserID": "browser-1", "state": "released"})
        self.assertIsNone(self.gateway.state.lease)
        self.assertIsNone(self.gateway.state.quarantine)

    async def test_keep_locked_quarantines_the_现场_acquire_agreed_to(self):
        _, token = await self.acquire_leased()
        response, body = await self.release({"browserID": "browser-1", "leaseToken": token,
                                             "keepLocked": True})
        self.assertEqual(response.status, 200)
        self.assertEqual(body["state"], "quarantined")
        record = self.gateway.state.quarantine
        self.assertIsNone(self.gateway.state.lease)
        self.assertEqual(record["retired_lease_token"], token)
        self.assertEqual(record["scene_identity"], scene_identity(BASE))
        self.assertEqual(record["task_id"], 41)
        self.assertEqual(record["session_id"], "s-1")

    async def test_a_release_never_moves_the_phase(self):
        # §4: POST /sessions is the only door back to `setup`.
        _, token = await self.acquire_leased()
        await self.release({"browserID": "browser-1", "leaseToken": token, "keepLocked": False})
        self.assertEqual(self.gateway.state.session_phase, "agent_owned")

    async def test_keep_locked_defaults_to_false(self):
        _, token = await self.acquire_leased()
        response, body = await self.release({"browserID": "browser-1", "leaseToken": token})
        self.assertEqual(response.status, 200)
        self.assertEqual(body["state"], "released")

    async def test_a_token_that_is_not_the_holder_is_a_400_naming_it(self):
        _, token = await self.acquire_leased()
        for explanation, body in [
            ("wrong token", {"browserID": "browser-1", "leaseToken": "not-" + token}),
            ("wrong browser", {"browserID": "browser-2", "leaseToken": token}),
            ("no token", {"browserID": "browser-1"}),
            ("token is not text", {"browserID": "browser-1", "leaseToken": 7}),
            ("no browser", {"leaseToken": token}),
            ("keepLocked is text", {"browserID": "browser-1", "leaseToken": token,
                                    "keepLocked": "true"}),
        ]:
            with self.subTest(explanation):
                response = await self.client.post("/releaseSceneLease", json=body)
                self.assertEqual(response.status, 400, explanation)
                self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
                self.assertIsNotNone(self.gateway.state.lease)

    async def test_a_retired_token_matches_no_holder(self):
        _, token = await self.acquire_leased()
        await self.release({"browserID": "browser-1", "leaseToken": token, "keepLocked": True})
        response, body = await self.release({"browserID": "browser-1", "leaseToken": token})
        self.assertEqual(response.status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_PARAM")
        self.assertIsNotNone(self.gateway.state.quarantine)

    async def test_a_second_task_gets_a_second_token_after_a_release(self):
        _, first = await self.acquire_leased(task_id=41)
        await self.release({"browserID": "browser-1", "leaseToken": first})
        _, second = await self.acquire_leased(task_id=42)
        self.assertNotEqual(first, second)


class CollectionLeaseGateTests(LeaseHarness):
    """§9.1's 租约闸 on /captureScene and /get2DAABB."""

    async def test_a_setup_phase_tour_needs_no_lease(self):
        # The operator walking their own scene is not an Agent collection; the gate that fires here
        # would refuse the demo tour for a lock nobody ever took out.
        self.gateway.state.set_phase("setup")
        socket = await self.connect()
        task = asyncio.create_task(self.client.get("/captureScene", params={"id": "2", "type": "rgb"}))
        frame = await self.next_frame(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": dict(frame_payload(), request_id=frame["request_id"])})
        self.assertEqual((await task).status, 200)

    async def test_an_agent_owned_capture_without_a_lease_is_refused(self):
        socket = await self.connect()
        response = await self.client.get("/captureScene", params={"id": "2", "type": "rgb"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "scene_lease_required")
        await self.assert_no_frame(socket)

    async def test_the_standing_token_opens_both_reads(self):
        socket, token = await self.acquire_leased()
        headers = {"X-Brain-Lease": token}
        task = asyncio.create_task(self.client.get("/captureScene",
                                                   params={"id": "2", "type": "rgb"}, headers=headers))
        frame = await self.next_frame(socket)
        self.assertEqual(frame["payload"]["lease_token"], token)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": dict(frame_payload(), request_id=frame["request_id"])})
        self.assertEqual((await task).status, 200)
        task = asyncio.create_task(self.client.get("/get2DAABB",
                                                   params={"id": "5", "camera_id": "2"}, headers=headers))
        frame = await self.next_frame(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True,
                                "payload": dict(box_payload(), request_id=frame["request_id"])})
        self.assertEqual((await task).status, 200)

    async def test_a_wrong_token_is_refused_and_reaches_no_browser(self):
        socket, _ = await self.acquire_leased()
        response = await self.client.get("/captureScene", params={"id": "2", "type": "rgb"},
                                         headers={"X-Brain-Lease": "guessed"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "scene_lease_required")
        await self.assert_no_frame(socket)

    async def test_a_retired_token_is_refused_after_the_lock_went_quarantined(self):
        # §8.3 step ③ made executable: the release keeps the token as `retired_lease_token`, and a
        # late collection carrying it must not be able to render the scene it no longer owns.
        socket, token = await self.acquire_leased()
        await self.release({"browserID": "browser-1", "leaseToken": token, "keepLocked": True})
        response = await self.client.get("/get2DAABB", params={"id": "5", "camera_id": "2"},
                                         headers={"X-Brain-Lease": token})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "scene_lease_required")
        await self.assert_no_frame(socket)

    async def test_a_lease_taken_outside_the_phase_still_gates(self):
        # §7.2 promises 租约期必然已在 agent_owned 内; if a caller breaks that promise, the lease
        # itself is the stronger fact and the capture still has to prove it.
        socket, token = await self.acquire_leased()
        self.gateway.state.set_phase("setup")
        task = asyncio.create_task(self.client.get("/captureScene", params={"id": "2", "type": "rgb"},
                                                    headers={"X-Brain-Lease": token}))
        frame = await self.next_frame(socket)
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": dict(frame_payload(), request_id=frame["request_id"])})
        self.assertEqual((await task).status, 200)
        refused = await self.client.get("/captureScene", params={"id": "2", "type": "rgb"})
        self.assertEqual(refused.status, 409)

    async def test_a_broken_parameter_is_still_judged_first(self):
        await self.connect()
        for explanation, params in [
            ("missing type", {"id": "2"}),
            ("unknown type", {"id": "2", "type": "depth"}),
            ("id is not a uint32", {"id": "-2", "type": "rgb"}),
        ]:
            with self.subTest(explanation):
                response = await self.client.get("/captureScene", params=params)
                self.assertIn(response.status, (400, 409), explanation)
                self.assertNotEqual((await response.json())["error"]["code"], "scene_lease_required")


class ResolveUncertainTests(LeaseHarness):
    """Recovery trusts server evidence, never the caller's late_write_excluded flag."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.result = {"sessionID": "s-1", "recovery_context": {"write_attempted": False}}
        self.terminal = True
        async def request(method, path, **kwargs):
            return (200, {"terminal": self.terminal}) if path == "/queryTaskState" else (200, self.result)
        self.gateway.agent_relay.request = request

    def resolution(self, **kwargs):
        body = super().resolution(**kwargs)
        body.update(taskID=41, expected_digest=scene_digest(BASE))
        return body

    def write_receipt(self, transaction_id, **kwargs):
        identity = super().write_receipt(transaction_id, **kwargs)
        receipt = self.gateway.receipts.load(transaction_id)
        receipt["request_fingerprint"] = {"sessionID": "s-1"}
        self.gateway.receipts.save(transaction_id, receipt)
        return identity

    async def test_brain_only_uncertain_recovers_and_fences_late_acquire(self):
        _, body, _ = await self.resolve_reading(self.resolution())
        self.assertEqual(body["state"], "resolved")
        self.assertIn("s-1:41", GatewayState(self.state_path).raw["task_fences"])
        response, result = await self.acquire()
        self.assertEqual(response.status, 409)
        self.assertEqual(result["lease_status"], "not_granted")

    async def test_rejected_receipt_resolves_unchanged_world(self):
        self.write_receipt("tx", status="rejected", document=BASE)
        self.quarantined("tx")
        _, body, _ = await self.resolve_reading(self.resolution(transaction_id="tx"))
        self.assertEqual(body["state"], "resolved")
        self.assertIsNone(self.gateway.state.quarantine)

    async def test_committed_receipt_requires_actual_candidate(self):
        self.write_receipt("tx", status="committed", document=CANDIDATE)
        self.quarantined("tx")
        body = self.resolution(transaction_id="tx")
        body["expected_digest"] = scene_digest(CANDIDATE)
        _, result, _ = await self.resolve_reading(body, live=CANDIDATE)
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))

    async def test_prepared_unknown_and_unconfirmed_do_not_unlock(self):
        await self.connect()
        for status in ("prepared", "unknown"):
            self.write_receipt("tx", status=status, document=BASE)
            self.quarantined("tx")
            _, body = await self.resolve(self.resolution(transaction_id="tx"))
            self.assertEqual(body["state"], "kept_locked")
            self.assertIn("write_unconfirmed", body["reason"])
        self.gateway.state.raw["quarantine"] = {"commit_unconfirmed": True}
        _, body = await self.resolve(self.resolution())
        self.assertEqual(body["state"], "kept_locked")

    async def test_no_receipt_and_no_write_proof_keeps_lock_despite_caller_claim(self):
        await self.connect()
        self.result["recovery_context"] = {}
        self.quarantined("lost")
        _, result = await self.resolve(self.resolution(transaction_id="lost", evidence=self.evidence(excluded=True)))
        self.assertEqual(result["state"], "kept_locked")
        self.assertIn("evidence_missing", result["reason"])

    async def test_legacy_caller_boolean_without_task_is_not_evidence(self):
        await self.connect()
        body = self.resolution()
        body.pop("taskID")
        _, result = await self.resolve(body)
        self.assertEqual(result["state"], "kept_locked")
        self.assertIn("task_required", result["reason"])

    async def test_running_or_wrong_session_task_cannot_recover(self):
        await self.connect()
        self.terminal = False
        _, result = await self.resolve(self.resolution())
        self.assertIn("not_terminal", result["reason"])
        self.terminal = True
        self.result["sessionID"] = "other"
        _, result = await self.resolve(self.resolution())
        self.assertIn("session_mismatch", result["reason"])

    async def test_other_browser_or_lease_cannot_recover(self):
        await self.connect()
        _, result = await self.resolve(self.resolution(browser="other"))
        self.assertIn("context_mismatch", result["reason"])
        self.gateway.state.raw["lease"] = {"task_id": 42}
        _, result = await self.resolve(self.resolution())
        self.assertIn("other_task", result["reason"])

    async def test_live_conflict_keeps_lock_and_persists_fence(self):
        self.quarantined(None)
        _, result, _ = await self.resolve_reading(self.resolution(), live=CANDIDATE)
        self.assertEqual(result["state"], "kept_locked")
        self.assertIsNotNone(self.gateway.state.quarantine)
        self.assertIn("s-1:41", GatewayState(self.state_path).raw["task_fences"])

    async def test_live_timeout_does_not_unlock(self):
        await self.connect()
        self.quarantined(None)
        _, result = await self.resolve(self.resolution())
        self.assertEqual(result["state"], "kept_locked")
        self.assertIn("unavailable", result["reason"])

    async def test_legacy_receipt_without_session_provenance_does_not_unlock(self):
        super().write_receipt("legacy", status="rejected", document=BASE)
        await self.connect()
        _, result = await self.resolve(self.resolution(transaction_id="legacy"))
        self.assertIn("unverified", result["reason"])

    async def test_recovery_retires_token(self):
        _, token = await self.acquire_leased()
        _, result, _ = await self.resolve_reading(self.resolution())
        self.assertEqual(result["state"], "resolved")
        self.assertIsNone(self.gateway.state.lease)
        fence = GatewayState(self.state_path).raw["task_fences"]["s-1:41"]
        self.assertEqual(fence["retired_lease_token"], token)


if __name__ == "__main__":
    unittest.main()
