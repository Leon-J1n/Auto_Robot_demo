"""§5/§6 of docs/gateway与Agent新通信流程.md: the persistent state, the binding and the id water mark.

Four things are pinned here that the rest of the protocol leans on: the state file is the only
authority for the object-id water mark and the session phase; the binding survives a refresh but
not a closed tab; a scene report may only be adopted when the phase allows it; and a commit
receipt (§8.1) is a file that outlives both the browser and this process.
"""
import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import local_gateway
import gateway_state
from digest import scene_digest
from frame_identity import WORLD, identity_fields, image_base64
from gateway_state import (
    MAX_ALLOCATE_COUNT,
    MAX_UINT32,
    STATE_KEYS,
    TRANSACTIONS_DIRNAME,
    GatewayState,
    ObjectIDSpaceExhausted,
    TransactionReceiptError,
    TransactionReceipts,
    adoption_verdict,
    clear_persisted_state,
    is_scene_digest,
    is_transaction_id,
    object_ids_in_document,
    scene_identity,
    transactions_dir_for,
)
from local_gateway import DEFAULT_CONSTANT_CONFIG, StandaloneGateway


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


def world(**overrides):
    """One small legal document: version 3, sub 1, ids 0..2 (§1.1's four-tuple is all we compare)."""
    document = {"scene_schema_version": 1, "scene_id": 7, "scene_name": "state test",
                "scene_version": 3, "scene_sub_version": 1,
                "objects": [{"object_id_in_scene": 0}, {"object_id_in_scene": 1},
                            {"object_id_in_scene": 2}]}
    document.update(overrides)
    return document


def identity_of(document):
    return scene_identity(document)


class StateFileTests(unittest.TestCase):
    """The file itself: what it keeps, what it refuses, and what it does when it is broken."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-state-")
        self.addCleanup(self.workspace.cleanup)
        self.path = Path(self.workspace.name) / "gateway_state.json"

    def state(self):
        return GatewayState(self.path)

    def test_a_state_that_is_never_named_a_file_stays_in_memory(self):
        # The isolation promise: 23 test gateways and any caller that has not decided where the
        # state lives must not read each other's binding off one shared file.
        state = GatewayState()
        state.bind_browser("browser-1")
        state.allocate_object_ids(3)
        self.assertIsNone(state.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(state.next_object_id, 3)

    def test_the_state_survives_a_restart(self):
        state = self.state()
        state.bind_browser("browser-7")
        state.start_session("s-1", camera_id=5)
        state.set_phase("agent_owned")
        state.allocate_object_ids(4)
        reopened = self.state()
        self.assertEqual(reopened.browser_id, "browser-7")
        self.assertEqual(reopened.session_id, "s-1")
        self.assertEqual(reopened.camera_id, 5)
        self.assertEqual(reopened.session_phase, "agent_owned")
        self.assertEqual(reopened.next_object_id, 4)

    def test_the_document_carries_exactly_the_named_fields(self):
        state = self.state()
        state.bind_browser("browser-1")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(stored), sorted(STATE_KEYS))
        self.assertEqual(stored["version"], 1)

    def test_a_torn_state_is_moved_aside_rather_than_forgotten(self):
        # §6 forbids reusing an id, and silently reading a half-written file as "no ids used yet"
        # is exactly a reuse. The bytes stay on disk for whoever notices.
        self.path.write_text('{"next_object_id": 90', encoding="utf-8")
        state = self.state()
        self.assertEqual(state.next_object_id, 0)
        self.assertFalse(self.path.exists())
        asides = [item.name for item in Path(self.workspace.name).iterdir() if "corrupt" in item.name]
        self.assertEqual(len(asides), 1, asides)
        self.assertIn("90", Path(self.workspace.name, asides[0]).read_text(encoding="utf-8"))

    def test_a_state_that_holds_a_nonobject_is_read_as_defaults(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertLogs("web_frontend.gateway_state", level="ERROR"):
            state = self.state()
        self.assertIsNone(state.browser_id)
        self.assertEqual(state.session_phase, "setup")

    def test_an_unknown_phase_is_not_a_phase(self):
        self.path.write_text(json.dumps({"session_phase": "half_owned"}), encoding="utf-8")
        with self.assertLogs("web_frontend.gateway_state", level="WARNING"):
            state = self.state()
        self.assertEqual(state.session_phase, "setup")

    def test_the_water_mark_only_ever_rises(self):
        # §6: 只升不降；删除不回退. The document is the evidence, and a smaller document is not
        # a licence to hand out an id that some scene already used.
        state = GatewayState()
        self.assertEqual(state.raise_water_mark([9]), 10)
        self.assertEqual(state.raise_water_mark([1, 2]), 10)
        self.assertEqual(state.raise_water_mark([]), 10)

    def test_ids_are_handed_out_once_and_never_again(self):
        state = GatewayState()
        first = state.allocate_object_ids(3)
        second = state.allocate_object_ids(2)
        self.assertEqual(first, [0, 1, 2])
        self.assertEqual(second, [3, 4])
        self.assertEqual(state.next_object_id, 5)

    def test_the_last_block_leaves_a_water_mark_that_is_still_a_legal_header(self):
        # §5.1 bounds X-Next-Object-ID at 2**32-1, so the block stops one short: the value that
        # would be the first illegal id is instead the resting water mark.
        state = GatewayState()
        state.raise_water_mark([MAX_UINT32 - 2])
        self.assertEqual(state.next_object_id, MAX_UINT32 - 1)
        self.assertEqual(state.allocate_object_ids(1), [MAX_UINT32 - 1])
        self.assertEqual(state.next_object_id, MAX_UINT32)
        with self.assertRaises(ObjectIDSpaceExhausted):
            state.allocate_object_ids(1)

    def test_an_allocation_that_does_not_fit_leaves_the_water_untouched(self):
        state = GatewayState()
        state.raise_water_mark([MAX_UINT32 - 3])
        with self.assertRaises(ObjectIDSpaceExhausted):
            state.allocate_object_ids(4)
        self.assertEqual(state.next_object_id, MAX_UINT32 - 2)

    def test_an_empty_or_negative_block_is_not_an_allocation(self):
        with self.assertRaises(ValueError):
            GatewayState().allocate_object_ids(0)

    def test_only_an_explicit_new_session_puts_the_phase_back_to_setup(self):
        # §4: 解除 agent_owned 的唯一途径是显式新建／切换会话.
        state = GatewayState()
        state.set_phase("agent_owned")
        with self.assertRaises(ValueError):
            state.set_phase("owned")
        state.start_session("s-2", camera_id=9)
        self.assertEqual(state.session_phase, "setup")
        self.assertEqual(state.camera_id, 9)

    def test_session_view_names_the_bound_browser_and_the_water(self):
        # §5 lists seven fields; the two extras are what a refreshed browser needs to tell
        # "this phase is mine" from "someone else holds the scene", and to seed its allocator.
        state = GatewayState()
        state.bind_browser("browser-1")
        state.record_scene(world())
        view = state.session_view()
        self.assertEqual(view["browser_id"], "browser-1")
        self.assertEqual(view["next_object_id"], 3)
        self.assertEqual(view["scene_id"], 7)
        self.assertEqual(view["scene_version"], 3)
        self.assertEqual(view["scene_sub_version"], 1)
        self.assertEqual(view["session_phase"], "setup")
        self.assertIsNone(view["lease"])
        self.assertIsNone(view["quarantine"])


    def test_the_lease_record_is_the_acquire_answer_and_it_survives_a_restart(self):
        # §7.2: the lock is a record on disk before it is anything in a browser's hands, so a
        # Gateway restart cannot silently free a scene an Agent task is collecting under.
        state = self.state()
        state.bind_browser("browser-7")
        state.start_session("s-1", camera_id=5)
        record = state.acquire_lease({"leaseToken": "L-1", "task_id": 41, "browser_id": "browser-7",
                                      "session_id": "s-1", "scene_identity": identity_of(world())})
        self.assertEqual(record, state.lease)
        reopened = self.state()
        self.assertEqual(reopened.lease, record)
        self.assertEqual(reopened.quarantine, None)

    def test_only_a_keep_locked_release_leaves_a_record(self):
        # §8.2: the release that admits uncertainty is the one that has to be written down, and
        # what it stores is the four-tuple the acquire agreed to -- never a reading of what the
        # scene might hold now.
        state = self.state()
        state.acquire_lease({"leaseToken": "L-1", "task_id": 41, "browser_id": "browser-7",
                             "session_id": "s-1", "scene_identity": identity_of(world())})
        self.assertIsNone(state.release_lease(keep_locked=False))
        self.assertIsNone(state.lease)
        self.assertIsNone(state.quarantine)

        state.acquire_lease({"leaseToken": "L-2", "task_id": 42, "browser_id": "browser-7",
                             "session_id": "s-1", "scene_identity": identity_of(world())})
        quarantined = state.release_lease(keep_locked=True)
        self.assertIsNone(state.lease)
        self.assertEqual(quarantined["retired_lease_token"], "L-2")
        self.assertEqual(quarantined["task_id"], 42)
        self.assertEqual(quarantined["scene_identity"], identity_of(world()))
        self.assertEqual(self.state().quarantine, quarantined)

    def test_a_resolution_writes_its_conclusion_whether_or_not_it_unlocks(self):
        # §8.3 ⑤: an attempt that concluded nothing is still evidence that it was made -- and the
        # one thing it may not do is quietly free the lock it failed to settle.
        state = self.state()
        state.raw["quarantine"] = {"transactionID": "tx-1"}
        state.resolve_lock({"state": "kept_locked", "reason": "the scene could not be read"},
                           cleared=False)
        self.assertIsNotNone(state.quarantine)
        self.assertEqual(state.raw["last_resolution"]["state"], "kept_locked")
        self.assertEqual(self.state().raw["last_resolution"],
                         {"state": "kept_locked", "reason": "the scene could not be read"})

        state.acquire_lease({"leaseToken": "L-3", "task_id": 43, "browser_id": "browser-7",
                             "session_id": "s-1", "scene_identity": identity_of(world())})
        state.resolve_lock({"state": "resolved", "reason": "the ledger and the scene agree"},
                           cleared=True)
        self.assertIsNone(state.quarantine)
        self.assertIsNone(state.lease)

    def test_retiring_the_session_never_moves_the_phase(self):
        # §4 keeps the phase door where it is: a `new_session` conclusion closes the session, it
        # does not hand the scene back to the operator.
        state = self.state()
        state.bind_browser("browser-7")
        state.start_session("s-1", camera_id=5, phase="agent_owned")
        state.raw["quarantine"] = {"transactionID": "tx-2"}
        state.resolve_lock({"state": "resolved", "resolution": "new_session"},
                           cleared=True, retire_session=True)
        self.assertIsNone(state.session_id)
        self.assertEqual(state.session_phase, "agent_owned")
        self.assertEqual(self.state().session_phase, "agent_owned")

    def test_the_lease_token_is_not_a_read_route_field(self):
        # §9.1's 租约闸 authenticates the token and every tab can poll these two reads; the holder's
        # credential stays out of both.
        state = GatewayState()
        state.bind_browser("browser-1")
        state.acquire_lease({"leaseToken": "L-9", "task_id": 44, "browser_id": "browser-1",
                             "session_id": "s-1", "scene_identity": identity_of(world())})
        self.assertEqual(state.lease["leaseToken"], "L-9")
        for view in (state.session_view(), state.session_record()):
            self.assertNotIn("leaseToken", view["lease"])
            self.assertEqual(view["lease"]["task_id"], 44)

    def test_a_state_written_before_the_resolution_key_reads_as_none(self):
        # The file is the protocol's memory, and a key this round added must not make an older one
        # unreadable -- that would reset the water mark's neighbour by accident.
        older = {"version": 1, "browser_id": "browser-7", "session_id": "s-1",
                 "session_phase": "agent_owned", "camera_id": 5, "next_object_id": 12,
                 "lease": None, "quarantine": None, "scene_identity": None}
        self.path.write_text(json.dumps(older), encoding="utf-8")
        state = self.state()
        self.assertIsNone(state.raw["last_resolution"])
        self.assertIsNone(state.lease)
        self.assertEqual(state.next_object_id, 12)
        self.assertEqual(state.session_phase, "agent_owned")


class DirectoryFsyncTests(unittest.TestCase):
    """An atomic write ends with a directory flush, and only POSIX can be asked for one.

    Windows answers `os.open(<dir>, O_RDONLY)` with PermissionError [Errno 13], so the unguarded
    call aborted every persisted write after the file was already on disk -- and on the websocket
    path it took the browser's connection down with it, which the Agent read as
    `NO_BROWSER_CONNECTED`.
    """

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-fsync-")
        self.addCleanup(self.workspace.cleanup)

    def test_windows_is_never_asked_for_a_directory_handle(self):
        directory = Path(self.workspace.name)
        with mock.patch.object(gateway_state.os, "name", "nt"), \
                mock.patch.object(gateway_state.os, "open") as opener:
            gateway_state._fsync_directory(directory)
        opener.assert_not_called()

    def test_posix_flushes_the_directory_and_closes_the_handle(self):
        directory = Path(self.workspace.name)
        with mock.patch.object(gateway_state.os, "name", "posix"), \
                mock.patch.object(gateway_state.os, "open", return_value=7) as opener, \
                mock.patch.object(gateway_state.os, "fsync") as fsync, \
                mock.patch.object(gateway_state.os, "close") as close:
            gateway_state._fsync_directory(directory)
        opener.assert_called_once_with(directory, os.O_RDONLY)
        fsync.assert_called_once_with(7)
        close.assert_called_once_with(7)

    def test_a_state_write_finishes_when_the_platform_has_no_directory_handle(self):
        """The regression this guard exists for: `save()` must not fail after `os.replace`."""
        path = Path(self.workspace.name) / "gateway_state.json"
        with mock.patch.object(gateway_state.os, "name", "nt"):
            GatewayState(path).bind_browser("browser-under-test")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["browser_id"],
                         "browser-under-test")

    def test_a_receipt_write_finishes_when_the_platform_has_no_directory_handle(self):
        directory = Path(self.workspace.name) / "gateway_transactions"
        with mock.patch.object(gateway_state.os, "name", "nt"):
            TransactionReceipts(directory).save("txn-under-test", {"status": "committed"})
        self.assertEqual(json.loads((directory / "txn-under-test.json")
                                    .read_text(encoding="utf-8"))["status"], "committed")


class DocumentReadingTests(unittest.TestCase):
    """§6's water mark comes from the ids the document promises, and from nothing else."""

    def test_the_ids_are_read_without_parsing_the_scene(self):
        document = {"objects": [{"object_id_in_scene": 4}, {"object_id_in_scene": True},
                                 {"object_id_in_scene": "7"}, {"object_name_in_scene": "no id"},
                                 "not even an object"]}
        self.assertEqual(object_ids_in_document(document), [4])

    def test_a_document_without_objects_has_no_ids(self):
        self.assertEqual(object_ids_in_document({"objects": "not a list"}), [])
        self.assertEqual(object_ids_in_document("a string"), [])

    def test_a_missing_sub_version_reads_as_zero_but_the_document_is_untouched(self):
        document = world()
        del document["scene_sub_version"]
        identity = identity_of(document)
        self.assertEqual(identity["scene_sub_version"], 0)
        self.assertNotIn("scene_sub_version", document)

    def test_the_identity_digest_is_the_single_points_digest(self):
        document = world()
        self.assertEqual(identity_of(document)["sceneDigest"], scene_digest(document))

    def test_a_nonobject_document_has_no_identity(self):
        self.assertEqual(identity_of("nope"), {key: None for key in
                                               ("scene_id", "scene_version", "scene_sub_version", "sceneDigest")})


class ReceiptNamingTests(unittest.TestCase):
    """§8.1/§3: the two texts a caller supplies that we build a file name and a CAS from."""

    def test_a_transaction_id_may_only_be_a_file_name(self):
        for allowed in ("tx-1", "Task.7_2-a", "x" * 128):
            self.assertTrue(is_transaction_id(allowed), allowed)
        for refused in ("", "a/b", "..\\escape", "with space", "x" * 129, "emoji-\U0001f30c", 7, None, True):
            self.assertFalse(is_transaction_id(refused), refused)

    def test_a_digest_is_exactly_sixty_four_lower_case_hex(self):
        self.assertTrue(is_scene_digest(scene_digest(world())))
        for refused in ("", scene_digest(world())[:-1], scene_digest(world()) + "0",
                        scene_digest(world()).upper(), "not a digest", None, 7):
            self.assertFalse(is_scene_digest(refused), refused)


class TransactionReceiptTests(unittest.TestCase):
    """§8.1/§9.2: one file per transaction, and a broken file is never read as `unknown`."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-receipts-")
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name) / "gateway_transactions"
        self.receipt = {"transactionID": "tx-1", "status": "committed", "scene_id": 7,
                        "scene_version": 4, "scene_sub_version": 0,
                        "sceneDigest": scene_digest(world(scene_version=4)), "rejected_objects": []}

    def receipts(self):
        return TransactionReceipts(self.directory)

    def test_a_store_without_a_directory_keeps_receipts_in_memory(self):
        store = TransactionReceipts(None)
        self.assertIsNone(store.load("tx-1"))
        store.save("tx-1", self.receipt)
        self.assertEqual(store.load("tx-1"), self.receipt)

    def test_a_saved_receipt_is_one_json_file_named_for_its_transaction(self):
        self.receipts().save("tx-1", self.receipt)
        written = sorted(path.name for path in self.directory.iterdir())
        # The temporary file is renamed into place, so no half-written record is ever listable.
        self.assertEqual(written, ["tx-1.json"])
        self.assertEqual(json.loads((self.directory / "tx-1.json").read_text(encoding="utf-8")),
                         self.receipt)

    def test_a_transaction_that_was_never_settled_here_reads_as_no_receipt(self):
        self.assertIsNone(self.receipts().load("tx-1"))
        # Querying must not leave an empty directory behind: the directory belongs to writes.
        self.assertFalse(self.directory.exists())

    def test_the_directory_holds_one_file_per_transaction(self):
        # The reason this is a directory and not keys in gateway_state.json (§9.2: that file is
        # rewritten whole on every change): one commit must not rewrite every receipt ever issued.
        store = self.receipts()
        store.save("tx-1", self.receipt)
        store.save("tx-2", {**self.receipt, "transactionID": "tx-2", "status": "rejected",
                            "sceneDigest": None})
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["tx-1.json", "tx-2.json"])
        self.assertEqual(store.load("tx-1")["status"], "committed")
        self.assertEqual(store.load("tx-2")["status"], "rejected")

    def test_a_second_write_for_one_transaction_replaces_the_record(self):
        store = self.receipts()
        store.save("tx-1", self.receipt)
        store.save("tx-1", {**self.receipt, "status": "rejected"})
        self.assertEqual(store.load("tx-1")["status"], "rejected")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["tx-1.json"])

    def test_a_receipt_that_is_not_a_commit_record_is_an_error(self):
        # `unknown` is the answer that keeps the session lock (§9.2); reporting a damaged record as
        # though it were simply absent would freeze the session on a statement we cannot support.
        store = self.receipts()
        self.directory.mkdir(parents=True)
        for text in ("{ not json", "[]", json.dumps({**self.receipt, "status": "pending"})):
            (self.directory / "tx-1.json").write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                self.assertRaises(TransactionReceiptError, store.load, "tx-1")

    def test_a_receipt_that_cannot_be_read_is_an_error(self):
        # Not a permissions rehearsal: the file name stands where a directory is, so the OS
        # refuses the read. A reader that cannot tell must say so, not answer `unknown`.
        self.directory.mkdir(parents=True)
        (self.directory / "tx-1.json").mkdir()
        with self.assertRaises(TransactionReceiptError) as caught:
            self.receipts().load("tx-1")
        self.assertIn("tx-1", str(caught.exception))

    def test_a_write_that_does_not_reach_the_disk_is_an_os_error(self):
        # §9.2 ②: the caller turns this into a 500 and writes nothing else; the store does not
        # quietly downgrade to memory, because a receipt the next process cannot find is a lie.
        (Path(self.workspace.name) / "gateway_transactions").write_text("not a directory",
                                                                        encoding="utf-8")
        self.assertRaises(OSError, self.receipts().save, "tx-1", self.receipt)


class AdoptionVerdictTests(unittest.TestCase):
    """§1.2 as one decision, read by `apply_state_sync` (§4's second layer)."""

    def setUp(self):
        self.stored = identity_of(world())

    def test_the_first_report_of_a_scene_is_adopted(self):
        self.assertEqual(adoption_verdict("setup", None, self.stored), "adopt")

    def test_the_same_document_reported_twice_is_accepted_quietly(self):
        # §1.2 rule 1: 词典序不增且内容也没变 ⇒ 幂等接受, never an error.
        self.assertEqual(adoption_verdict("setup", self.stored, identity_of(world())), "idempotent")

    def test_a_sub_version_step_is_the_editing_phase_own_route(self):
        incoming = identity_of(world(scene_sub_version=2))
        self.assertEqual(adoption_verdict("setup", self.stored, incoming), "adopt")

    def test_the_same_step_in_agent_owned_is_a_manual_edit(self):
        incoming = identity_of(world(scene_sub_version=2))
        self.assertEqual(adoption_verdict("agent_owned", self.stored, incoming), "manual_edit_forbidden")

    def test_a_scene_version_step_is_the_agents_own(self):
        # §1.2 rule 2: a commit lands at exactly v+1 with the sub-version re-baselined to 0,
        # and the browser then reports that document -- the phase must not refuse it.
        incoming = identity_of(world(scene_version=4, scene_sub_version=0))
        self.assertEqual(adoption_verdict("agent_owned", self.stored, incoming), "adopt")

    def test_a_silent_content_change_is_a_version_conflict(self):
        incoming = identity_of(world(scene_name="the same numbers, different scene"))
        self.assertEqual(adoption_verdict("setup", self.stored, incoming), "browser_scene_version_conflict")

    def test_an_older_version_is_a_version_conflict(self):
        incoming = identity_of(world(scene_version=2, scene_sub_version=9))
        self.assertEqual(adoption_verdict("setup", self.stored, incoming), "browser_scene_version_conflict")

    def test_a_different_scene_is_adopted_whatever_the_phase(self):
        # §1.2 (revised 2026-11-05): a different `scene_id` is a switch and the browser owns the
        # scene, so it is adopted even in `agent_owned`. The three-layer defence's second layer
        # closes *panel edits* of the current scene here, not a move to another scene; the Agent
        # keeps its real lock through the lease fingerprint, never by refusing the report.
        incoming = identity_of(world(scene_id=8))
        self.assertEqual(adoption_verdict("agent_owned", self.stored, incoming), "adopt")

    def test_a_different_scene_in_setup_moves_the_stored_identity(self):
        # The 2026-10-22 联调 case: setup is the phase in which switching scenes is allowed
        # (§2.4), so this report has to land: refusing it froze the water mark on the old scene --
        # Brain read the live scene, handed that four-tuple to /acquireSceneLease and got
        # `browser_scene_version_conflict`, while the 新建会话 door starved on an empty
        # `camera_candidates`. The switch is the report the session question is asked *from*.
        incoming = identity_of(world(scene_id=8))
        self.assertEqual(adoption_verdict("setup", self.stored, incoming), "adopt")

    def test_a_scene_switch_ignores_the_versions_of_both_scenes(self):
        # Two scenes' `scene_version` / `scene_sub_version` are per-scene counters and never compare,
        # so a switch to a scene with a strictly lower pair is still adopted -- the case the old
        # version arithmetic wrongly refused, which kept a restarted Gateway answering for the scene
        # the browser had already left.
        incoming = identity_of(world(scene_id=8, scene_version=0, scene_sub_version=0))
        self.assertEqual(adoption_verdict("setup", self.stored, incoming), "adopt")

    def test_the_same_scene_step_is_still_judged_by_version(self):
        # `adoption_verdict` no longer takes a hold flag: the switch branch is unconditional, and a
        # same-scene report keeps its own version verdicts (here, a legal sub-version step in setup).
        incoming = identity_of(world(scene_sub_version=2))
        self.assertEqual(adoption_verdict("setup", self.stored, incoming), "adopt")


class PersistentGatewayTests(unittest.IsolatedAsyncioTestCase):
    """The routes and the WS frames, all reading and writing the one file."""

    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-state-routes-")
        self.addCleanup(self.workspace.cleanup)
        self.path = Path(self.workspace.name) / "gateway_state.json"
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=5),
                                         state_path=self.path)
        app = web.Application()
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/api/sessionState", self.gateway.session_state)
        app.router.add_get("/allocateObjectID", self.gateway.allocate_object_id)
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

    async def report(self, socket, document):
        await socket.send_json({"type": "browser.state_sync", "payload": {"world": document}})
        for _ in range(200):
            if self.gateway.state.scene_identity is not None:
                return
            await asyncio.sleep(0.005)

    async def next_frame(self, socket, kind):
        for _ in range(50):
            frame = await socket.receive_json()
            if frame.get("type") == kind:
                return frame
        self.fail(f"no {kind} frame arrived")

    # -- the binding (§5.1: browserID 落盘持久化) ------------------------------------

    async def test_the_first_hello_claims_the_binding_and_the_file_records_it(self):
        await self.connect("browser-7")
        self.assertEqual(self.gateway.state.browser_id, "browser-7")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["browser_id"], "browser-7")

    async def test_a_refreshed_tab_reclaims_its_own_session(self):
        # §4: 刷新不丢阶段. The page reloads, the WS drops, the same id helloes again.
        socket = await self.connect("browser-7")
        await self.report(socket, world())
        self.gateway.state.set_phase("agent_owned")
        await self.disconnect(socket)
        await self.connect("browser-7")
        self.assertEqual(self.gateway.state.browser_id, "browser-7")
        self.assertEqual(self.gateway.state.session_phase, "agent_owned")

    async def test_a_second_tab_does_not_take_the_scene_over(self):
        bound = await self.connect("browser-7")
        other = await self.connect("browser-8")
        self.addAsyncCleanup(other.close)
        self.assertEqual(self.gateway.state.browser_id, "browser-7")
        self.assertEqual(self.gateway.state.session_view()["browser_id"], "browser-7")

    async def test_a_different_id_takes_over_once_the_bound_browser_is_gone(self):
        # Different browser ids now have independent profiles; closing one profile does not
        # rebind its scene to another device.
        socket = await self.connect("browser-7")
        await self.disconnect(socket)
        await self.connect("browser-8")
        self.assertEqual(self.gateway.state.browser_id, "browser-7")
        self.assertEqual(self.gateway.profiles["browser-8"].state.browser_id, "browser-8")

    # -- GET /api/sessionState ------------------------------------------------------

    async def test_session_state_answers_the_persisted_view(self):
        response = await self.client.get("/api/sessionState", params={"browser_id": "browser-1"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(sorted(body), sorted(["session_phase", "lease", "quarantine", "browser_id",
                                               "next_object_id", "scene_id", "scene_version",
                                               "scene_sub_version", "sceneDigest",
                                               "camera_candidates", "recovery_required"]))
        self.assertEqual(body["camera_candidates"], [],
                         "§9 item 3: no browser has reported a list to this process yet")

        self.assertEqual(body["session_phase"], "setup")
        self.assertIsNone(body["browser_id"])

    async def test_session_state_does_not_trust_the_callers_claim(self):
        # The query names the caller; the answer names the bound browser. A viewer that is not
        # the bound one has to be able to see that, or it greys out the wrong panel.
        await self.connect("browser-9")
        body = await (await self.client.get("/api/sessionState", params={"browser_id": "browser-1"})).json()
        self.assertEqual(body["browser_id"], "browser-9")

    # -- browser.state_sync (§6 water mark, §4 layer ②) ------------------------------

    async def test_adopting_a_reported_scene_raises_the_persisted_water(self):
        socket = await self.connect()
        await self.report(socket, world())
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["next_object_id"], 3)
        self.assertEqual(stored["scene_identity"]["sceneDigest"], scene_digest(world()))

    async def test_the_announcement_carries_the_four_tuple_and_the_phase(self):
        source = await self.connect("browser-1")
        watcher = await self.connect("browser-2")
        await source.send_json({"type": "browser.state_sync", "payload": {"world": world()}})
        frame = await self.next_frame(watcher, "scene.applied")
        self.assertEqual(frame["scene_id"], 7)
        self.assertEqual(frame["scene_version"], 3)
        self.assertEqual(frame["scene_sub_version"], 1)
        self.assertEqual(frame["scene_digest"], scene_digest(world()))
        self.assertEqual(frame["session_phase"], "setup")

    async def test_a_sub_version_step_is_refused_once_the_agent_owns_the_scene(self):
        # §4's second layer, and the whole reason the phase is on disk rather than in the page:
        # a refreshed browser with an edited document must not be able to smuggle it in.
        socket = await self.connect()
        await self.report(socket, world())
        self.gateway.state.set_phase("agent_owned")
        watcher = await self.connect("browser-2")
        edited = world(scene_sub_version=2)
        with self.assertLogs("web_frontend.local_gateway", level="WARNING") as logged:
            await socket.send_json({"type": "browser.state_sync", "payload": {"world": edited}})
            for _ in range(20):
                if any("manual_edit_forbidden" in line for line in logged.output):
                    break
                await asyncio.sleep(0.005)
        self.assertTrue(any("manual_edit_forbidden" in line for line in logged.output), logged.output)
        # Not adopted, not cached, not announced, and the water did not move.
        self.assertEqual(self.gateway.world_cache, world())
        self.assertEqual(self.gateway.state.scene_identity["scene_sub_version"], 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["next_object_id"], 3)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(watcher.receive_json(), 0.2)

    async def test_a_scene_switch_in_setup_moves_the_stored_scene_and_its_candidates(self):
        # The 2026-10-22 联调 case, end to end. `setup` is the phase in which switching scenes is
        # allowed (§2.4), so this report has to land: refusing it is exactly what left
        # `/api/sessionState` on scene 7 while `GET /getWorld` answered scene 8, which made the
        # four-tuple Brain read live fail to match the stored one at `/acquireSceneLease` and
        # starved the 新建会话 door on an empty `camera_candidates` -- the door the switch is
        # supposed to walk out of.
        socket = await self.connect()
        await self.report(socket, world())
        watcher = await self.connect("browser-2")
        await socket.send_json({"type": "browser.state_sync",
                                "payload": {"world": world(scene_id=8), "camera_object_ids": [4, 5]}})
        frame = await self.next_frame(watcher, "scene.applied")
        self.assertEqual(frame["scene_id"], 8)
        body = await (await self.client.get("/api/sessionState",
                                            params={"browser_id": "browser-1"})).json()
        self.assertEqual(body["scene_id"], 8)
        self.assertEqual(body["camera_candidates"], [4, 5],
                         "§9 item 3: the list pairs with the scene it was read out of, and switching "
                         "scenes is not a reason to lose it")
        self.assertEqual(self.gateway.world_cache["scene_id"], 8)

    async def test_concurrent_state_sync_reports_persist_in_arrival_order(self):
        baseline = world(scene_sub_version=0)
        older = world(scene_sub_version=1)
        newer = world(scene_sub_version=2)
        self.gateway.state.record_scene(baseline)
        self.gateway._cache_world(baseline)
        self.gateway.state.bind_browser("browser-1")

        older_ensuring = asyncio.Event()
        release_older = asyncio.Event()
        calls = 0

        async def delayed_ensure(browser_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                older_ensuring.set()
                await release_older.wait()

        with mock.patch.object(self.gateway, "_ensure_automatic_session", side_effect=delayed_ensure):
            old_task = asyncio.create_task(self.gateway.apply_state_sync(
                {"payload": {"world": older, "report_seq": 1}}, "browser-1"))
            await asyncio.wait_for(older_ensuring.wait(), 1)
            new_task = asyncio.create_task(self.gateway.apply_state_sync(
                {"payload": {"world": newer, "report_seq": 2}}, "browser-1"))
            await asyncio.sleep(0)
            self.assertFalse(new_task.done(), "the newer report must wait for the in-flight older report")
            release_older.set()
            await asyncio.gather(old_task, new_task)

        self.assertEqual(self.gateway.state.scene_identity["scene_sub_version"], 2)
        self.assertEqual(self.gateway.world_cache["scene_sub_version"], 2)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["scene_identity"]["scene_sub_version"], 2)

    async def test_a_scene_switch_is_adopted_while_a_lease_holds_the_scene(self):
        # 2026-11-05: a lease recovered from disk after a Gateway restart is never released -- the
        # Brain session that held its token is gone -- so the old refusal deadlocked the scene on the
        # abandoned 现场 forever. The report now lands: the stored identity and the cache follow the
        # browser. The Agent's real lock is untouched -- no session is auto-created under a held
        # scene, and a late write is still rejected by that lease's fingerprint elsewhere.
        socket = await self.connect()
        await self.report(socket, world())
        self.gateway.state.acquire_lease({"leaseToken": "L-7", "task_id": 41, "browser_id": "browser-1",
                                          "session_id": "s-1", "scene_identity": identity_of(world()),
                                          "acquired_at": 0})
        self.assertEqual(self.gateway.state.session_phase, "setup")
        watcher = await self.connect("browser-2")
        await socket.send_json({"type": "browser.state_sync",
                                "payload": {"world": world(scene_id=8), "camera_object_ids": [4, 5]}})
        frame = await self.next_frame(watcher, "scene.applied")
        self.assertEqual(frame["scene_id"], 8)
        self.assertEqual(self.gateway.state.scene_identity["scene_id"], 8)
        self.assertEqual(self.gateway.world_cache["scene_id"], 8)
        self.assertIsNone(self.gateway.state.session_id,
                          "adopting the switch must not open a Brain session under a held scene")

    async def test_a_scene_switch_is_adopted_while_the_agent_owns_the_phase(self):
        # §4's second layer closes *edits* of the current scene in `agent_owned`; a move to another
        # scene is a switch the browser is entitled to make, so it lands -- and, as above, it does
        # not churn the session the Agent owns: the phase stays `agent_owned`.
        socket = await self.connect()
        await self.report(socket, world())
        self.gateway.state.set_phase("agent_owned")
        watcher = await self.connect("browser-2")
        await socket.send_json({"type": "browser.state_sync",
                                "payload": {"world": world(scene_id=8), "camera_object_ids": [4, 5]}})
        frame = await self.next_frame(watcher, "scene.applied")
        self.assertEqual(frame["scene_id"], 8)
        self.assertEqual(self.gateway.state.scene_identity["scene_id"], 8)
        self.assertEqual(self.gateway.state.session_phase, "agent_owned")

    # -- GET /allocateObjectID (§6) -------------------------------------------------

    async def test_an_allocation_answers_a_block_and_moves_the_persisted_water(self):
        await self.connect()
        response = await self.client.get("/allocateObjectID", params={"browserID": "browser-1", "count": "3"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"object_ids": [0, 1, 2]})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["next_object_id"], 3)

    async def test_the_seed_of_a_block_is_the_water_the_scene_reported(self):
        # §6: a locally allocated id must never come back as "free" -- the document raised the
        # water to 3, so the first id this gateway may hand out is 3.
        socket = await self.connect()
        await self.report(socket, world())
        body = await (await self.client.get("/allocateObjectID", params={"browserID": "browser-1"})).json()
        self.assertEqual(body["object_ids"], [3])

    async def test_a_block_is_never_handed_out_twice(self):
        await self.connect()
        first = await (await self.client.get("/allocateObjectID", params={"browserID": "browser-1", "count": "2"})).json()
        second = await (await self.client.get("/allocateObjectID", params={"browserID": "browser-1", "count": "2"})).json()
        self.assertEqual(first["object_ids"], [0, 1])
        self.assertEqual(second["object_ids"], [2, 3])

    async def test_a_caller_that_names_nothing_gets_no_ids(self):
        await self.connect()
        response = await self.client.get("/allocateObjectID")
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "MISSING_PARAM")

    async def test_a_browser_that_is_not_connected_gets_the_honest_answer(self):
        response = await self.client.get("/allocateObjectID", params={"browserID": "browser-ghost", "count": "1"})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")

    async def test_a_viewer_that_is_not_the_bound_browser_has_to_rebuild_its_session(self):
        # §1.2 rule 4's own code: the scene belongs to another browser, so this caller's session
        # is the thing that is wrong, not its parameters.
        await self.connect("browser-7")
        other = await self.connect("browser-8")
        self.addAsyncCleanup(other.close)
        response = await self.client.get("/allocateObjectID", params={"browserID": "browser-8", "count": "1"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "browser_new_session_required")

    async def test_a_count_that_is_not_a_block_size_is_a_caller_mistake(self):
        await self.connect()
        for raw in ("0", "not-a-number", str(MAX_ALLOCATE_COUNT + 1)):
            response = await self.client.get("/allocateObjectID",
                                             params={"browserID": "browser-1", "count": raw})
            self.assertEqual(response.status, 400, raw)
            self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM", raw)

    async def test_an_exhausted_id_space_says_so_and_keeps_the_water(self):
        await self.connect()
        self.gateway.state.raise_water_mark([MAX_UINT32 - 1])
        response = await self.client.get("/allocateObjectID", params={"browserID": "browser-1", "count": "2"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "object_id_space_exhausted")
        self.assertEqual(self.gateway.state.next_object_id, MAX_UINT32)

    async def test_a_huge_block_never_becomes_a_list_in_this_process(self):
        # No scene is needed to hit this one: `count` close to the uint32 ceiling would otherwise
        # materialise four billion ids before the space check could refuse them.
        await self.connect()
        response = await self.client.get("/allocateObjectID",
                                        params={"browserID": "browser-1", "count": str(MAX_UINT32 - 1)})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.gateway.state.next_object_id, 0)


class CaptureForwardingTests(unittest.IsolatedAsyncioTestCase):
    """§9.1: a capture is addressed, and it says so in the frame the browser answers."""

    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-state-capture-")
        self.addCleanup(self.workspace.cleanup)
        self.gateway = StandaloneGateway(config=config(browser_request_timeout_seconds=5,
                                                      capture_timeout_seconds=5),
                                         state_path=Path(self.workspace.name) / "gateway_state.json")
        # The identity gate needs a reading to answer to, and this class is about *which* browser the
        # frame went to, so the fastest honest basis is one world adopted before the connects.
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
        self.fail("no browser registered")

    async def answer(self, socket, frame, payload):
        await socket.send_json({"type": "browser.response", "request_id": frame["request_id"],
                                "ok": True, "payload": dict(payload, request_id=frame["request_id"])})

    async def test_the_bound_browser_and_the_lease_ride_into_the_capture(self):
        socket = await self.connect("browser-7")
        task = asyncio.create_task(self.client.get("/captureScene",
                                                   params={"id": "2", "type": "rgb"}))
        frame = await socket.receive_json()
        self.assertEqual(frame["payload"], {"id": 2, "type": "rgb", "browser_id": "browser-7",
                                            "lease_token": None})
        await self.answer(socket, frame, self.frame(8, 6, browser_id="browser-7"))
        self.assertEqual((await task).status, 200)

    async def test_a_capture_is_not_asked_of_a_second_tab(self):
        bound = await self.connect("browser-7")
        other = await self.connect("browser-8")
        self.addAsyncCleanup(other.close)
        task = asyncio.create_task(self.client.get("/captureScene", params={"id": "2", "type": "rgb"}))
        frame = await bound.receive_json()
        self.assertEqual(frame["payload"]["browser_id"], "browser-7")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(other.receive_json(), 0.2)
        await self.answer(bound, frame, self.frame(8, 6, browser_id="browser-7"))
        self.assertEqual((await task).status, 200)

    async def test_the_lease_token_reaches_the_viewer_that_has_to_freeze_its_lights(self):
        # §7.2's third lock is the browser's own action: it can only reset and freeze the light
        # preset if the frame that starts the capture names the lease. Under §9.1's 租约闸 the
        # caller has to present that same token to get the frame at all.
        socket = await self.connect("browser-7")
        self.gateway.state.raw["lease"] = {"leaseToken": "lease-1", "browser_id": "browser-7"}
        task = asyncio.create_task(self.client.get("/get2DAABB", params={"id": "5", "camera_id": "2"},
                                                   headers={"X-Brain-Lease": "lease-1"}))
        frame = await socket.receive_json()
        self.assertEqual(frame["payload"], {"id": 5, "camera_id": 2, "browser_id": "browser-7",
                                            "lease_token": "lease-1"})
        await self.answer(socket, frame, {"visible": True, "width": 8, "height": 6,
                                          **identity_fields(browser_id="browser-7")})
        self.assertEqual((await task).status, 200)

    async def test_a_lease_that_stands_refuses_an_unproved_measurement(self):
        # The token is the lock's only evidence of ownership: after a `keepLocked` release the
        # record is gone, and a late request still carrying the retired token must not reach the
        # browser (§8.3 ③ 排除迟到写入 -- the gate is what makes that step enforceable).
        socket = await self.connect("browser-7")
        self.gateway.state.raw["lease"] = {"leaseToken": "lease-1", "browser_id": "browser-7"}
        self.gateway.state.raw["session_phase"] = "agent_owned"
        refused = await self.client.get("/captureScene", params={"id": "2", "type": "rgb"})
        self.assertEqual(refused.status, 409)
        self.assertEqual((await refused.json())["error"]["code"], "scene_lease_required")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive_json(), 0.2)

    @staticmethod
    def frame(width: int, height: int, *, browser_id: str) -> dict:
        """A §5.10 rgb answer sized the way §9's gate asks: an IHDR that says the same thing twice."""
        return {"image_base64": image_base64(width, height), "width": width, "height": height,
                **identity_fields(browser_id=browser_id)}


class ReleaseBundleTests(unittest.TestCase):
    def test_gateway_state_ships_in_the_bundle(self):
        # A gateway without its state file would restart with a water mark of zero, which is the
        # one thing §6 forbids.
        import pack
        self.assertIn("gateway_state.py", pack.RELEASE_FILES)


class ClearPersistedStateTests(unittest.TestCase):
    """The startup sweep that returns a fresh process to a clean slate (§5/§6, 2026-11-05).

    Clearing is pinned to `main()`, never to the constructors: the finished run's state stays on
    disk for an after-the-fact read, and the next process wipes it. These tests hold both halves.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.state_path = self.directory / "gateway_state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_a_stuck_round(self):
        """A state that would otherwise deadlock a switch: a live scene under an unreleased lease."""
        state = GatewayState(self.state_path)
        state.raw["scene_identity"] = identity_of(world())
        state.raw["next_object_id"] = 50
        state.raw["lease"] = {"leaseToken": "L-7", "task_id": 41, "scene_identity": identity_of(world())}
        state.save()
        transactions = transactions_dir_for(self.state_path)
        transactions.mkdir()
        (transactions / "fcfa320d.json").write_text('{"status": "committed"}', encoding="utf-8")

    def test_it_removes_the_state_file_and_the_receipt_directory(self):
        self._seed_a_stuck_round()
        # The `.corrupt-*` copies _read sets aside and a `.tmp` a crash left mid-write must go too,
        # but a file that merely shares the directory must survive the sweep.
        (self.directory / "gateway_state.json.corrupt-1").write_text("{", encoding="utf-8")
        (self.directory / "gateway_state.jsonabcd.tmp").write_text("{", encoding="utf-8")
        (self.directory / "notes.txt").write_text("keep me", encoding="utf-8")

        removed = clear_persisted_state(self.state_path)

        self.assertFalse(self.state_path.exists())
        self.assertFalse(transactions_dir_for(self.state_path).exists())
        self.assertFalse((self.directory / "gateway_state.json.corrupt-1").exists())
        self.assertFalse((self.directory / "gateway_state.jsonabcd.tmp").exists())
        self.assertEqual((self.directory / "notes.txt").read_text(encoding="utf-8"), "keep me",
                         "unrelated scratch in the same directory must survive")
        for name in ("gateway_state.json", "gateway_state.json.corrupt-1",
                     "gateway_state.jsonabcd.tmp", f"{TRANSACTIONS_DIRNAME}/"):
            self.assertIn(name, removed)

    def test_a_cleared_directory_leaves_the_next_process_on_defaults(self):
        self._seed_a_stuck_round()
        clear_persisted_state(self.state_path)
        fresh = GatewayState(self.state_path)
        self.assertEqual(fresh.session_phase, "setup")
        self.assertIsNone(fresh.scene_identity)
        self.assertIsNone(fresh.lease)
        self.assertEqual(fresh.next_object_id, 0, "a clean round restarts the water mark too")

    def test_clearing_an_absent_state_is_a_noop(self):
        self.assertEqual(clear_persisted_state(self.directory / "nope.json"), [])

    def test_the_constructor_reloads_rather_than_clears(self):
        # The regression that keeps clearing out of GatewayState/StandaloneGateway: recovery and the
        # after-exit troubleshooting window both depend on a named state file surviving a new
        # gateway pointed at it. With an unexpired lease the identity must come back, not be wiped.
        self._seed_a_stuck_round()
        gateway = StandaloneGateway(state_path=self.state_path)
        self.assertEqual(gateway.state.scene_identity, identity_of(world()))
        self.assertIsNotNone(gateway.state.lease)
        self.assertTrue(transactions_dir_for(self.state_path).exists(),
                        "constructing a gateway must not delete the receipt directory")


if __name__ == "__main__":
    unittest.main()
