"""§8.1: the `commitWorld` transaction pair -- gates first, receipts on disk.

Two halves are pinned here. The gates: a candidate that is not exactly one version ahead of the
现场 the caller named, or that did not re-baseline `scene_sub_version`, never reaches a browser.
The receipts: `committed` only after the strict apply landed, `rejected` only after the browser
said so in its own words, and *no record at all* when the request was never answered -- because
`unknown` is the status that must keep the lock and must never age into a refusal
(流程文档 §8.1, 规格 §9.2 ②).
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
from gateway_state import scene_identity

TRANSFORM = {"component_type": "Transform", "properties": {
    "position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1], "parent": None}}


def object_of(object_id):
    return {"object_id_in_scene": object_id, "object_name_in_scene": f"o{object_id}",
            "components": [TRANSFORM]}


def world(*, scene_version=3, objects=None, **overrides):
    document = {"scene_schema_version": 1, "scene_id": 7, "scene_name": "commit",
                "scene_version": scene_version,
                "objects": objects if objects is not None else [object_of(0), object_of(1)]}
    document.update(overrides)
    return document


BASE = world()
CANDIDATE = world(scene_version=4, objects=[object_of(0), object_of(1), object_of(9)])
CANDIDATE_DIGEST = scene_digest(CANDIDATE)
ACCEPTED = {"ok": True, "scene_id": 7, "scene_version": 4, "object_count": 3, "rejected_objects": []}


class CommitWorldTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_original_branches_use_new_id_version_one(self):
        previous = BASE
        for identifier in (101, 102):
            candidate = world(scene_id=identifier, scene_version=1, scene_sub_version=0)
            body = self.body(f"original-{identifier}", scene=candidate, expected=scene_identity(previous))
            response, receipt, _, _ = await self.commit(body, payload={
                "ok": True, "scene_id": identifier, "scene_version": 1, "rejected_objects": []})
            self.assertEqual(response.status, 200, receipt)
            self.assertEqual(receipt["status"], "committed")
            previous = candidate
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(previous))

    async def test_background_original_commit_does_not_overwrite_visible_session(self):
        context = self.gateway._session_context("background-A", create=True)
        context.update(world_snapshot=BASE, scene_identity=scene_identity(BASE),
            lease={"leaseToken":"A-token", "task_id":9,"session_id":"background-A",
                   "browser_id":"browser-1","scene_identity":scene_identity(BASE),"world_snapshot":BASE})
        self.gateway.conversations["A-row"] = {"sessionID":"background-A"}
        candidate = world(scene_id=101, scene_version=1, scene_sub_version=0)
        response = await self.client.post('/commitWorld', json=self.body('background',scene=candidate),
            headers={"X-Brain-Session":"background-A", "X-Brain-Lease":"A-token"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))
        self.assertEqual(context['world_snapshot'], candidate)
        self.assertEqual(self.gateway.conversations['A-row']['sceneSnapshot'], candidate)

    async def test_replay_fingerprint_includes_session_and_expected(self):
        await self.commit(self.body())
        changed = self.body()
        changed['expected'] = scene_identity(world(scene_version=2))
        response = await self.client.post('/commitWorld', json=changed)
        self.assertEqual(response.status, 409)
        response = await self.client.post('/commitWorld', json=self.body(), headers={'X-Brain-Session':'different'})
        self.assertEqual(response.status, 409)

    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="commit-world-")
        self.addCleanup(self.workspace.cleanup)
        self.state_path = Path(self.workspace.name) / "gateway_state.json"
        self.transactions = Path(self.workspace.name) / "gateway_transactions"
        self.config = self.config_with()
        self.gateway = self.gateway_at(self.state_path)
        # The 现场 the caller will CAS against: what a live /getWorld or a state_sync would leave
        # on disk. Committing has to be judged against the stored identity, never against a body.
        self.gateway.state.record_scene(BASE)
        self.gateway.state.start_session("session-test")
        self.gateway.state.bind_browser("browser-1")
        self.gateway.state.acquire_lease({"leaseToken": "test-lease", "task_id": 1,
            "browser_id": "browser-1", "session_id": "session-test", "scene_identity": scene_identity(BASE)})
        self.clients = []
        self.client = await self.serving(self.gateway)

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    def config_with(self, **overrides):
        value = json.loads(json.dumps(local_gateway.DEFAULT_CONSTANT_CONFIG))
        value.update({"browser_request_timeout_seconds": 0.5, "max_payload_bytes": 65536})
        value.update(overrides)
        return value

    def gateway_at(self, state_path):
        return local_gateway.StandaloneGateway(config=self.config, state_path=state_path)

    async def serving(self, gateway):
        """One test-owned server over the routes under test, tracked for teardown."""
        app = web.Application()
        app.router.add_get("/events", gateway.events)
        app.router.add_post("/commitWorld", gateway.commit_world)
        app.router.add_get("/queryWorldCommit", gateway.query_world_commit)
        app.router.add_get("/getWorld", gateway.get_world)
        app.router.add_post("/releaseSceneLease", gateway.release_scene_lease)
        client = TestClient(TestServer(app))
        await client.start_server()
        client.session.headers.update({"X-Brain-Session": "session-test", "X-Brain-Lease": "test-lease"})
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
            frame = await asyncio.wait_for(socket.receive_json(), timeout=3)
            if frame.get("type") == want:
                return frame
        self.fail(f"no {want} frame arrived")

    async def assert_no_frame(self, socket):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.2)

    def body(self, transaction_id="tx-1", *, scene=CANDIDATE, browser="browser-1", expected=None):
        return {"transactionID": transaction_id, "browserID": browser,
                "expected": expected if expected is not None else scene_identity(BASE),
                "scene": scene}

    async def commit(self, body, *, payload=None, error=None):
        """One commit that reaches a browser: post, answer from the socket, hand back both halves."""
        socket = await self.connect()
        task = asyncio.create_task(self.client.post("/commitWorld", json=body))
        frame = await self.next_frame(socket)
        message = {"type": "browser.response", "request_id": frame["request_id"], "ok": error is None}
        if error is None:
            message["payload"] = ACCEPTED if payload is None else payload
        else:
            message["error"] = error
        await socket.send_json(message)
        response = await task
        return response, await response.json(), frame, socket

    async def query(self, transaction_id):
        response = await self.client.get("/queryWorldCommit", params={"transactionID": transaction_id})
        return response, await response.json()

    async def error_code(self, response):
        return (await response.json())["error"]["code"]

    # ---------------------------------------------------------------- committed

    async def test_commit_lands_the_candidate_and_persists_a_committed_receipt(self):
        self.gateway.state.acquire_lease({"leaseToken": "test-lease", "task_id": 1,
            "browser_id": "browser-1", "session_id": "session-test", "scene_identity": scene_identity(BASE)})
        response, body, frame, socket = await self.commit(self.body())
        self.assertEqual(response.status, 200)
        self.assertNotIn("commit_unconfirmed", self.gateway.state.lease)
        # §8.1/D8: the candidate rides the updateWorld action and asks for whole-or-nothing, and
        # the CAS the caller made against us is forwarded as the browser's own version guard.
        self.assertEqual(frame["action"], "updateWorld")
        self.assertEqual(frame["payload"]["world"], CANDIDATE)
        self.assertIs(frame["payload"]["strict"], True)
        self.assertEqual(frame["payload"]["expected_version"], 3)
        self.assertEqual(body, {"transactionID": "tx-1", "browserID": "browser-1",
                                "status": "committed", "scene_id": 7, "scene_version": 4,
                                "scene_sub_version": 0, "sceneDigest": CANDIDATE_DIGEST,
                                "rejected_objects": []})
        self.assertNotIn("candidate_digest", body)
        # Unlike a /updateWorld relay, this route holds the document that landed, so the identity
        # it stores and announces carries a real digest (§3) and the water mark rises (§6 event ①).
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(CANDIDATE))
        self.assertEqual(self.gateway.state.next_object_id, 10)
        self.assertEqual(self.gateway.world_cache, CANDIDATE)
        self.assertEqual(self.gateway.scene_version, 4)
        self.assertTrue((self.transactions / "tx-1.json").is_file())
        broadcast = await self.next_frame(socket, "scene.applied")
        # Q147: the Agent's commit is not a browser report, so it carries no ack number -- the
        # browser's verdict for this frame is "believe it and apply", exactly as before. The session
        # it names is this fixture's own (`POST /sessions` runs before a commit can be accepted).
        self.assertEqual(broadcast, {"type": "scene.applied", "scene_id": 7, "scene_version": 4,
                                     "scene_sub_version": 0, "scene_digest": CANDIDATE_DIGEST,
                                     "session_phase": "setup", "session_id": "session-test",
                                     "action": "updateWorld",
                                     "source_request_id": frame["request_id"],
                                     "ack_report_seq": None})

    # -------------------------------------------------------------------- gates

    async def test_candidate_that_keeps_a_sub_version_never_reaches_the_browser(self):
        socket = await self.connect()
        response = await self.client.post("/commitWorld", json=self.body(scene=world(scene_version=4,
                                                                                       scene_sub_version=2)))
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.error_code(response), "gateway_scene_revision_invalid")
        await self.assert_no_frame(socket)

    async def test_a_candidate_omitting_the_sub_version_reads_as_zero(self):
        # §3.1: a document may leave the key out, and §1.2 rule 2 reads that as the re-baselined 0.
        _, body, _, _ = await self.commit(self.body(scene=world(scene_version=4)))
        self.assertEqual(body["status"], "committed")
        self.assertEqual(body["scene_sub_version"], 0)

    async def test_a_stale_expected_four_tuple_is_a_conflict_not_a_dispatch(self):
        socket = await self.connect()
        stale = {**scene_identity(BASE), "scene_version": 2}
        response = await self.client.post("/commitWorld", json=self.body(expected=stale))
        self.assertEqual(response.status, 409)
        self.assertEqual(await self.error_code(response), "browser_scene_version_conflict")
        await self.assert_no_frame(socket)
        _, unknown = await self.query("tx-1")
        self.assertEqual(unknown["status"], "rejected")

    async def test_the_target_must_be_exactly_one_version_ahead(self):
        socket = await self.connect()
        for scene, reason in ((world(scene_version=6), "two steps"),
                              (world(scene_version=3), "no step"),
                              (world(scene_id=8, scene_version=4), "another scene")):
            with self.subTest(reason):
                response = await self.client.post("/commitWorld", json=self.body(transaction_id=reason.replace(" ", "-"), scene=scene))
                self.assertEqual(response.status, 400)
                self.assertEqual(await self.error_code(response), "gateway_target_version_invalid")
        await self.assert_no_frame(socket)

    async def test_every_required_field_is_named_when_it_is_missing(self):
        await self.connect()
        cases = [
            ({key: value for key, value in self.body().items() if key != "transactionID"}, "MISSING_PARAM"),
            ({key: value for key, value in self.body().items() if key != "browserID"}, "MISSING_PARAM"),
            ({key: value for key, value in self.body().items() if key != "scene"}, "MISSING_PARAM"),
            ({**self.body(), "expected": None}, "MISSING_PARAM"),
            ({**self.body(), "expected": {k: v for k, v in scene_identity(BASE).items()
                                          if k != "sceneDigest"}}, "MISSING_PARAM"),
            ({**self.body(), "expected": {**scene_identity(BASE), "scene_version": "3"}},
             "web_uint32_required"),
            ({**self.body(), "expected": {**scene_identity(BASE), "sceneDigest": "ABC"}}, "INVALID_PARAM"),
            ({**self.body(), "scene": [1, 2, 3]}, "INVALID_WORLD_SCHEMA"),
            ({**self.body(), "browserID": ""}, "INVALID_PARAM"),
        ]
        for number, (body, code) in enumerate(cases):
            if "transactionID" in body:
                body["transactionID"] = f"invalid-{number}"
            with self.subTest(code=code, body=sorted(set(self.body()) - set(body))):
                response = await self.client.post("/commitWorld", json=body)
                self.assertEqual(response.status, 400)
                self.assertEqual(await self.error_code(response), code)

    async def test_a_transaction_id_that_could_name_a_path_is_refused(self):
        # §8.1: the id is caller-supplied and becomes a file, so the whitelist is the path check.
        await self.connect()
        for raw in ("", "a/b", "..\\escape", "with space", "x" * 129, 7, None):
            with self.subTest(raw=raw):
                response = await self.client.post("/commitWorld",
                                                  json={**self.body(), "transactionID": raw})
                self.assertEqual(response.status, 400)
                self.assertEqual(await self.error_code(response),
                                 "MISSING_PARAM" if raw is None else "INVALID_PARAM")
        self.assertEqual(sorted(path.name for path in Path(self.workspace.name).iterdir()),
                         ["gateway_state.json"])

    # ------------------------------------------------------------- not delivered

    async def test_no_bound_browser_is_a_refusal_that_writes_no_receipt(self):
        response = await self.client.post("/commitWorld", json=self.body())
        self.assertEqual(response.status, 503)
        self.assertEqual(await self.error_code(response), "NO_BROWSER_CONNECTED")
        _, unknown = await self.query("tx-1")
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue((self.transactions / "tx-1.json").exists())

    async def test_held_lease_commits_active_session_snapshot_when_browser_disconnects(self):
        # Once Brain has acquired the lease and read the scene, the gateway owns enough verified
        # evidence to finish a CAS commit after the browser websocket disappears.  A caller that
        # lacks this exact lease still follows the normal NO_BROWSER_CONNECTED path above.
        self.gateway.state.bind_browser("browser-1")
        self.gateway.state.start_session("session-1", phase="agent_owned")
        self.gateway.conversations["conversation-1"] = {"sessionID": "session-1"}
        self.gateway.state.acquire_lease({
            "leaseToken": "lease-1", "task_id": 9, "browser_id": "browser-1",
            "session_id": "session-1", "scene_identity": scene_identity(BASE),
            "world_snapshot": BASE,
        })
        headers = {"X-Brain-Lease": "lease-1", "X-Brain-Session": "session-1"}
        response = await self.client.post("/commitWorld", json=self.body(), headers=headers)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["status"], "committed")
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(CANDIDATE))
        self.assertEqual(self.gateway.state.lease["world_snapshot"], CANDIDATE)
        self.assertEqual(self.gateway.world_cache, CANDIDATE)
        self.assertEqual(self.gateway.session_contexts["session-1"]["world_snapshot"], CANDIDATE)
        self.assertEqual(self.gateway.conversations["conversation-1"]["sceneSnapshot"], CANDIDATE)
        await self.connect()
        restored = await self.client.get("/getWorld", headers=headers)
        self.assertEqual(await restored.json(), CANDIDATE)
        replacement = self.gateway_at(self.state_path)
        self.assertEqual(replacement.state.scene_identity, scene_identity(CANDIDATE))
        self.assertEqual(replacement.state.lease["world_snapshot"], CANDIDATE)
        released = await self.client.post("/releaseSceneLease", json={
            "browserID": "browser-1", "leaseToken": "lease-1", "keepLocked": False,
        }, headers=headers)
        self.assertEqual(released.status, 200)
        self.assertIsNone(self.gateway.state.lease)
        self.assertIsNone(self.gateway.session_contexts["session-1"]["lease"])

    async def test_offline_commit_rejects_missing_evidence_or_uncertain_prior_write(self):
        self.gateway.state.bind_browser("browser-1")
        self.gateway.state.start_session("session-1", phase="agent_owned")
        lease = {"leaseToken": "lease-1", "task_id": 9, "browser_id": "browser-1",
                 "session_id": "session-1", "scene_identity": scene_identity(BASE),
                 "world_snapshot": BASE}
        for changes, headers in [
            ({"world_snapshot": None}, {"X-Brain-Lease": "lease-1", "X-Brain-Session": "session-1"}),
            ({"commit_unconfirmed": True}, {"X-Brain-Lease": "lease-1", "X-Brain-Session": "session-1"}),
            ({}, {"X-Brain-Lease": "wrong", "X-Brain-Session": "session-1"}),
            ({}, {"X-Brain-Lease": "lease-1", "X-Brain-Session": "wrong"}),
        ]:
            with self.subTest(changes=changes, headers=headers):
                self.gateway.state.acquire_lease({**lease, **changes})
                transaction = f"offline-{len(list(self.transactions.glob('*.json'))) if self.transactions.exists() else 0}"
                response = await self.client.post("/commitWorld", json=self.body(transaction_id=transaction), headers=headers)
                expected_status = 409 if headers.get("X-Brain-Lease") == "wrong" or headers.get("X-Brain-Session") == "wrong" else 503
                self.assertEqual(response.status, expected_status)
                self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))
                _, receipt = await self.query(transaction)
                self.assertEqual(receipt["status"], "rejected" if expected_status == 409 else "unknown")

    async def test_a_timeout_writes_no_receipt(self):
        # §8.1: 传输失败／超时不是拒绝 -- the transaction stays unknown, and the caller must not
        # resend, so we must not be the ones to decide it was refused.
        self.gateway.state.acquire_lease({"leaseToken": "test-lease", "task_id": 1,
            "browser_id": "browser-1", "session_id": "session-test", "scene_identity": scene_identity(BASE)})
        socket = await self.connect()
        task = asyncio.create_task(self.client.post("/commitWorld", json=self.body()))
        await self.next_frame(socket)
        response = await task
        self.assertEqual(response.status, 504)
        self.assertTrue(self.gateway.state.lease["commit_unconfirmed"])
        self.assertEqual(await self.error_code(response), "BROWSER_TIMEOUT")
        _, unknown = await self.query("tx-1")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))

    async def test_a_reply_that_disagrees_with_the_candidate_proves_nothing(self):
        response, _, _, _ = await self.commit(
            self.body(), payload={**ACCEPTED, "scene_version": 5})
        self.assertEqual(response.status, 502)
        self.assertEqual(await self.error_code(response), "GATEWAY_FRAMING_ERROR")
        _, unknown = await self.query("tx-1")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))

    # ----------------------------------------------------------------- rejected

    async def test_a_browser_refusal_lands_a_rejected_receipt_and_no_change(self):
        self.gateway.state.acquire_lease({"leaseToken": "test-lease", "task_id": 1,
            "browser_id": "browser-1", "session_id": "session-test", "scene_identity": scene_identity(BASE)})
        response, body, frame, socket = await self.commit(
            self.body(), error={"code": "VERSION_CONFLICT", "message": "expected_version 3 != current 4"})
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "rejected")
        self.assertNotIn("commit_unconfirmed", self.gateway.state.lease)
        self.assertEqual(body["reason"], "VERSION_CONFLICT: expected_version 3 != current 4")
        self.assertEqual(body["rejected_objects"], [])
        # §8.1: `rejected` has to carry the evidence that nothing moved -- the unchanged 现场.
        self.assertEqual({key: body[key] for key in ("scene_id", "scene_version", "scene_sub_version",
                                                     "sceneDigest")}, scene_identity(BASE))
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))
        self.assertEqual(self.gateway.state.next_object_id, 2)
        await self.assert_no_frame(socket)
        _, again = await self.query("tx-1")
        self.assertEqual(again, body)

    async def test_a_strict_candidate_with_any_rejection_lands_nothing(self):
        self.gateway.state.acquire_lease({"leaseToken": "test-lease", "task_id": 1,
            "browser_id": "browser-1", "session_id": "session-test", "scene_identity": scene_identity(BASE)})
        rejections = [{"object_id_in_scene": 9, "index": 2, "reason": "PARENT_NOT_FOUND:42"}]
        response, body, _, socket = await self.commit(
            self.body(), payload={**ACCEPTED, "object_count": 2, "rejected_objects": rejections})
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "rejected")
        self.assertNotIn("commit_unconfirmed", self.gateway.state.lease)
        # The browser's judgement is relayed verbatim (规格 §5.8 Q111): not one entry dropped.
        self.assertEqual(body["rejected_objects"], rejections)
        self.assertIn("objects[2]", body["reason"])
        self.assertEqual(self.gateway.state.scene_identity, scene_identity(BASE))
        self.assertEqual(self.gateway.state.next_object_id, 2)
        self.assertIsNone(self.gateway.world_cache)
        await self.assert_no_frame(socket)

    # ------------------------------------------------------------- idempotency

    async def test_replaying_a_settled_transaction_answers_from_the_receipt(self):
        _, committed, _, socket = await self.commit(self.body())
        # Drain the first commit's own notice; what must not follow is a second request.
        await self.next_frame(socket, "scene.applied")
        response = await self.client.post("/commitWorld", json=self.body())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), committed)
        # A second dispatch would be the resend this protocol forbids -- and it would re-apply.
        await self.assert_no_frame(socket)

    async def test_the_same_transaction_with_different_content_is_a_conflict(self):
        await self.commit(self.body())
        other = world(scene_version=4, objects=[object_of(0), object_of(1)])
        response = await self.client.post("/commitWorld", json=self.body(scene=other))
        self.assertEqual(response.status, 409)
        self.assertEqual(await self.error_code(response), "browser_scene_version_conflict")

    async def test_a_rejected_transaction_never_replays_as_a_commit(self):
        _, rejected, _, _ = await self.commit(
            self.body(), payload={**ACCEPTED, "rejected_objects": [
                {"object_id_in_scene": 9, "index": 2, "reason": "PARENT_NOT_FOUND:42"}]})
        response = await self.client.post("/commitWorld", json=self.body())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), rejected)

    # ------------------------------------------------------------ query half

    async def test_a_missing_receipt_is_200_unknown_and_asserts_nothing_about_the_scene(self):
        response, body = await self.query("never-heard-of-it")
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"transactionID": "never-heard-of-it", "status": "unknown",
                               "reason": "no commit receipt for this transaction"})

    async def test_query_validates_its_id_without_touching_the_disk(self):
        response = await self.client.get("/queryWorldCommit")
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.error_code(response), "MISSING_PARAM")
        response = await self.client.get("/queryWorldCommit", params={"transactionID": "../state"})
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.error_code(response), "INVALID_PARAM")

    async def test_the_receipt_survives_a_refresh_and_a_restarted_gateway(self):
        await self.commit(self.body())
        # The receipt is on the gateway's disk, not in a browser (§8.1), so a process that comes
        # back up with the same state directory answers the same query with the same record.
        replacement = await self.serving(self.gateway_at(self.state_path))
        response = await replacement.get("/queryWorldCommit", params={"transactionID": "tx-1"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["status"], "committed")
        self.assertEqual(body["sceneDigest"], CANDIDATE_DIGEST)

    async def test_an_unreadable_receipt_is_a_500_not_an_unknown(self):
        # §9.2 ②: no-receipt and unreadable-receipt are different facts, and only one of them is
        # allowed to keep a session locked with no explanation.
        self.transactions.mkdir(parents=True)
        (self.transactions / "tx-1.json").write_text("{ not json", encoding="utf-8")
        response, body = await self.query("tx-1")
        self.assertEqual(response.status, 500)
        self.assertEqual(body["error"]["code"], "INTERNAL_ERROR")

    async def test_the_transaction_pair_is_registered_before_the_static_catch_all(self):
        # 坑 2: a route the SPA shell swallows answers 200 with index.html instead of the contract.
        app = local_gateway.create_app(self.config, state_path=self.state_path)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        response = await client.post("/commitWorld", json={})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "MISSING_PARAM")
        response = await client.get("/queryWorldCommit", params={"transactionID": "tx-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
