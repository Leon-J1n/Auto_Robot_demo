"""§5/§6 of docs/gateway与Agent新通信流程.md: the gateway's persistent state.

One JSON file holds what must survive a page refresh or a gateway restart: which browser is
bound, which session and phase that browser is in, the object-id high-water mark, the lease
and quarantine records. It is the only authority for the id water mark (§6) and for the
session phase (§4), which is why both live here instead of in a browser. Commit receipts
(§8.1) sit beside it as one file per transaction, in `var/gateway_transactions/`.

Writes are atomic (`tmp` + `os.replace`) because a half-written state file would read as a
fresh install, and a fresh install hands out ids the live scene is already using.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from digest import scene_digest

LOGGER = logging.getLogger("web_frontend.gateway_state")

# The file carries a version so a future shape change can say which shape it found.
GATEWAY_STATE_VERSION = 1
# §4: the only two phases. A third would need a decision about what the panel may do in it.
SESSION_PHASES = ("setup", "agent_owned")
# §6: ids are uint32, so this is the id space.
MAX_UINT32 = 0xFFFFFFFF
# One allocation call may not ask for more ids than a commit could ever carry. Without a cap
# `count=4_000_000_000` would pass the space check at an empty water mark and materialise the
# whole block in this process -- a hole that needs no scene to hit. Brain's largest candidate
# is a few hundred objects, so 1024 is roomy and still refuses the malformed call.
MAX_ALLOCATE_COUNT = 1024
# §8.1: a commit receipt is named by the caller's `transactionID`, and that name becomes a file.
# The whitelist is the whole path check -- no separator survives it, and the `.json` suffix the
# writer appends turns even "." and ".." into ordinary file names. 128 chars is roomier than any
# id Brain issues (a uuid is 36) while still refusing a body that names a 4 KB file.
TRANSACTION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
RECEIPT_STATUSES = ("prepared", "unknown", "committed", "rejected")
# §8.1/§9.2: the commit-receipt directory is a fixed sibling of the state file -- named once here
# so the writer (`StandaloneGateway`), the reader (`TransactionReceipts`) and the startup sweep
# (`clear_persisted_state`) cannot drift apart on a string literal.
TRANSACTIONS_DIRNAME = "gateway_transactions"
# §3: SHA-256 over canonical JSON, printed as 64 lowercase hex characters. The one place both
# sides can compare notes is the text of a digest, so the shape is a checked thing, not a habit.
SCENE_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

STATE_KEYS = ("version", "browser_id", "session_id", "session_phase", "camera_id", "camera_selection_reason",
              "next_object_id", "lease", "quarantine", "scene_identity", "last_resolution", "task_fences", "lease_outcomes")
SCENE_IDENTITY_KEYS = ("scene_id", "scene_version", "scene_sub_version", "sceneDigest")


class ObjectIDSpaceExhausted(RuntimeError):
    """§6: the persistent water mark has no room left below the uint32 ceiling."""


def _uint(value: Any, *, maximum: int = MAX_UINT32) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        return 0
    return value


def object_ids_in_document(document: Any) -> list[int]:
    """Every `object_id_in_scene` the document carries, in document order.

    Deliberately shallow: this file's job is the water mark, so it reads the ids it is
    promised and no scene semantics at all (spec §5.15.1 -- the gateway never parses a world).
    """
    objects = document.get("objects") if isinstance(document, dict) else None
    if not isinstance(objects, list):
        return []
    return [item["object_id_in_scene"] for item in objects
            if isinstance(item, dict) and isinstance(item.get("object_id_in_scene"), int)
            and not isinstance(item["object_id_in_scene"], bool)]


def is_scene_digest(value: Any) -> bool:
    """§3: a digest is exactly the 64 lowercase hex characters of one canonical document.

    `expected.sceneDigest` is compared as text and it is what a CAS stands or falls on, so the
    shape is checked before the value is ever trusted as evidence about the 现场.
    """
    return isinstance(value, str) and SCENE_DIGEST_PATTERN.fullmatch(value) is not None


def scene_identity(document: Any) -> dict[str, Any]:
    """The four-tuple of one world document, with the digest of that same document (§3).

    A missing `scene_sub_version` stays `0` here because that is what §1.2 reads it as when
    comparing; the document itself is never rewritten.
    """
    if not isinstance(document, dict):
        return {key: None for key in SCENE_IDENTITY_KEYS}
    return {
        "scene_id": document.get("scene_id") if isinstance(document.get("scene_id"), int) else None,
        "scene_version": _uint(document.get("scene_version")),
        "scene_sub_version": _uint(document.get("scene_sub_version")),
        "sceneDigest": scene_digest(document),
    }


def adoption_verdict(phase: str, stored: dict[str, Any] | None, incoming: dict[str, Any]) -> str:
    """§1.2 as one decision: may this reported document be adopted, given the persisted phase?

    Returns `"adopt"` or `"idempotent"` when the document may land, otherwise the §10 code that
    says why it may not. The four-tuple is compared first because it is cheap and it is what the
    rules are written against; the digest only decides "the dictionary order did not increase and
    the content did change", which is the one case where two identical-looking scenes differ.

    A different `scene_id` is a scene switch, not a version edit, so it is *always* adopted and the
    `scene_version` / `scene_sub_version` of the two documents are never compared: they are per-scene
    counters and say nothing about each other. This replaced the earlier gate that refused a switch
    while `phase != "setup"` or a lease/quarantine stood (added after the 2026-10-22 联调, when
    switching in `setup` was first allowed): after a Gateway restart recovers a lease or quarantine
    from disk the lock is never released -- the Brain session that held its token is gone -- so every
    later switch was refused forever. The stored identity and the world cache then stayed on the scene
    the browser had already left, `/getWorld` kept handing the API that abandoned document (a task's
    collision check ran against a scene no longer on screen), `camera_candidates` -- paired to the
    stale identity -- read empty, and the browser flashed straight back on its next authoritative
    re-read (the two 2026-11-05 同事联调 bugs). Adopting the identity does not smuggle a session in
    under a held scene: `_ensure_automatic_session` still refuses to open one while a lease or
    `agent_owned` stands. The §9 lock keeps its teeth where they matter -- a live task really
    interrupted by a switch loses only its commit, to the fingerprint check, which is the honest
    verdict for a 现场 that has genuinely moved.
    """
    if stored is None:
        return "adopt"
    if incoming["scene_id"] != stored["scene_id"]:
        return "adopt"
    if incoming == stored:
        return "idempotent"
    if (incoming["scene_version"], incoming["scene_sub_version"]) > (
        stored["scene_version"], stored["scene_sub_version"]):
        # A `scene_version` step is the Agent's own route (it lands through /commitWorld);
        # a sub-version step at the same version is a panel edit, and §4 closes that in
        # `agent_owned` -- that is exactly the second layer of the three-layer defence.
        if incoming["scene_version"] == stored["scene_version"] and phase != "setup":
            return "manual_edit_forbidden"
        return "adopt"
    return "browser_scene_version_conflict"


def _fsync_directory(path: Any) -> None:
    """Flush the directory so the rename is on the journal; Windows has no such handle and journals metadata anyway."""
    if os.name != "posix":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def transactions_dir_for(state_path: Path) -> Path:
    """The commit-receipt directory that lives beside a state file (§8.1/§9.2)."""
    return state_path.parent / TRANSACTIONS_DIRNAME


def clear_persisted_state(state_path: Path | str) -> list[str]:
    """Wipe the previous round's durable artifacts so a fresh process starts clean.

    The gateway persists exactly two things: the state file (which browser is bound, the phase,
    the lease/quarantine locks, the scene identity, the object-id water mark) and the receipt
    directory beside it. Everything else -- conversations, jobs, the world cache -- is process
    memory and already resets on its own. The sweep runs at *startup*, never at exit: a finished
    run leaves its state on disk for whoever has to read it after the fact, and the next `main()`
    is what returns the workspace to a clean slate. Doing it on the way in, rather than on the way
    out, is what keeps the post-mortem window open while still guaranteeing a clean start -- and it
    also retires the stale-lease-on-restart hazard the switch-adoption fix leans on, because a
    normal restart now simply has no lease to read back.

    Scoped to this state file's own name so unrelated scratch that shares the directory survives:
    the state file, the `.corrupt-*` copies `_read` sets aside, any `.tmp` a crash left mid-write,
    and the whole receipt directory. Returns the removed names for logging; an absent path is a
    no-op. Only a process-startup caller owns this -- `GatewayState` and `StandaloneGateway`
    deliberately keep reading, so recovery and the troubleshooting window still work.
    """
    path = Path(state_path)
    removed: list[str] = []
    for stale in sorted(path.parent.glob(f"{path.name}*")):
        if stale.is_file():
            stale.unlink()
            removed.append(stale.name)
    transactions = transactions_dir_for(path)
    if transactions.is_dir():
        shutil.rmtree(transactions)
        removed.append(f"{transactions.name}/")
    profiles = path.parent / "gateway_profiles"
    if profiles.is_dir():
        shutil.rmtree(profiles)
        removed.append(f"{profiles.name}/")
    return removed


class GatewayState:
    """The disk-backed half of a brain-web/2 session: binding, phase, water mark, locks.

    `path` is `None` for an ephemeral state: every real server entry point names a file, while a
    process built by a test (or by any caller that has not decided where the state lives) keeps
    the same object in memory. That is the whole isolation story -- 23 test gateways must not
    read each other's binding off a shared `var/gateway_state.json`.
    """

    def __init__(self, path: Path | str | None = None, browser_id: str | None = None) -> None:
        # Preserve an already-constructed PathLike object.  Besides avoiding an
        # unnecessary conversion, this keeps a PosixPath usable in the tests
        # that temporarily patch ``os.name`` to exercise the Windows fsync
        # branch on a POSIX host.
        self.path = path if isinstance(path, Path) else (Path(path) if path is not None else None)
        self.profile_id = browser_id
        self.raw: dict[str, Any] = self._read()

    # -- persistence -------------------------------------------------------------

    def _defaults(self) -> dict[str, Any]:
        return {
            "version": GATEWAY_STATE_VERSION,
            "browser_id": None,
            "session_id": None,
            "session_phase": "setup",
            "camera_id": None,
            "camera_selection_reason": None,
            "next_object_id": 0,
            "lease": None,
            "quarantine": None,
            "scene_identity": None,
            "last_resolution": None,
            "task_fences": {},
            "lease_outcomes": {},
        }

    def _read(self) -> dict[str, Any]:
        state = self._defaults()
        if self.path is None:
            return state
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return state
        except OSError as error:
            LOGGER.error("gateway state at %s is unreadable (%s); starting from defaults", self.path, error)
            return state
        try:
            stored = json.loads(raw)
        except ValueError as error:
            # §6 forbids reusing an id, and starting from 0 after a torn write is exactly that.
            # Set the file aside rather than delete it, so the water mark is still recoverable
            # by whoever notices, then start clean -- the live scene raises it again (§6).
            aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.path, aside)
                LOGGER.error("gateway state at %s is not JSON (%s); moved aside to %s", self.path, error, aside)
            except OSError:
                LOGGER.error("gateway state at %s is not JSON (%s); starting from defaults", self.path, error)
            return state
        if not isinstance(stored, dict):
            LOGGER.error("gateway state at %s holds a %s, not an object; starting from defaults",
                         self.path, type(stored).__name__)
            return state
        if self.profile_id is not None and isinstance(stored.get("profiles"), dict):
            stored = stored["profiles"].get(self.profile_id, {})
            if not isinstance(stored, dict):
                stored = {}
        elif self.profile_id is not None and isinstance(stored.get("browser_id"), str):
            # A legacy single-profile file is claimed by its persisted browser id. New profile
            # files are written in the map form below, but old --keep-state deployments remain
            # readable without a migration command.
            if stored.get("browser_id") != self.profile_id:
                stored = {}
        for key in STATE_KEYS:
            if key in stored:
                state[key] = stored[key]
        state["version"] = GATEWAY_STATE_VERSION
        state["next_object_id"] = _uint(state["next_object_id"])
        if state["session_phase"] not in SESSION_PHASES:
            LOGGER.warning("gateway state carries unknown session_phase %r; reading it as setup",
                           state["session_phase"])
            state["session_phase"] = "setup"
        if state["browser_id"] is not None and not isinstance(state["browser_id"], str):
            state["browser_id"] = None
        if state["session_id"] is not None and not isinstance(state["session_id"], str):
            state["session_id"] = None
        if state["camera_id"] is not None and (isinstance(state["camera_id"], bool)
                                               or not isinstance(state["camera_id"], int)
                                               or not 0 <= state["camera_id"] <= MAX_UINT32):
            LOGGER.warning("gateway state carries unusable camera_id %r; reading it as unbound",
                           state["camera_id"])
            state["camera_id"] = None
        return state

    def save(self) -> None:
        """Write atomically: a reader must see either the old state or the new one."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
        try:
            document = {key: self.raw[key] for key in STATE_KEYS}
            if self.profile_id is not None:
                profiles: dict[str, Any] = {}
                with contextlib.suppress(OSError, ValueError):
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(existing, dict) and isinstance(existing.get("profiles"), dict):
                        profiles = dict(existing["profiles"])
                    elif isinstance(existing, dict) and isinstance(existing.get("browser_id"), str):
                        profiles[str(existing["browser_id"])] = {
                            key: existing[key] for key in STATE_KEYS if key in existing}
                profiles[self.profile_id] = document
                document = {"version": GATEWAY_STATE_VERSION, "profiles": profiles}
            with os.fdopen(handle, "w", encoding="utf-8") as target:
                json.dump(document, target, ensure_ascii=False, indent=2)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, self.path)
            _fsync_directory(self.path.parent)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- binding and session (§4, §5.1 of the flow doc) ---------------------------

    @property
    def browser_id(self) -> str | None:
        return self.raw["browser_id"]

    @property
    def session_phase(self) -> str:
        return self.raw["session_phase"]

    @property
    def session_id(self) -> str | None:
        return self.raw["session_id"]

    @property
    def camera_id(self) -> int | None:
        return self.raw["camera_id"]

    @property
    def lease(self) -> dict[str, Any] | None:
        return self.raw["lease"]

    @property
    def quarantine(self) -> dict[str, Any] | None:
        return self.raw["quarantine"]

    @property
    def scene_held(self) -> bool:
        """§1.2 rule 4's second half: does anything still claim this scene?

        One of the two states that outlive a task's *execution* -- a lease that has not been
        released, or a quarantine that has not been resolved -- and both mean the same thing for
        an incoming scene switch: the Agent side has an open claim on a specific four-tuple, so
        re-pointing the stored identity would erase the very thing a later conclusion is judged
        against. Only `setup` with neither is free to change scenes.
        """
        return self.lease is not None or self.quarantine is not None

    @property
    def scene_identity(self) -> dict[str, Any] | None:
        return self.raw["scene_identity"]

    def bind_browser(self, browser_id: str) -> bool:
        """Claim the binding for one browser; returns whether the disk changed.

        A refresh re-helloes with the same id and must not touch the session (§4: 刷新不解禁、
        也不丢绑定); a *different* id may only take over while the bound id is not connected,
        which the caller knows from its socket table.
        """
        if self.raw["browser_id"] == browser_id:
            return False
        self.raw["browser_id"] = browser_id
        self.save()
        return True

    def start_session(self, session_id: str, *, camera_id: int | None = None,
                      phase: str = "setup", camera_reason: str | None = None) -> None:
        """§4: only an explicit new/switched session moves the phase back to `setup`."""
        if phase not in SESSION_PHASES:
            raise ValueError(f"unknown session phase {phase!r}")
        self.raw["session_id"] = session_id
        self.raw["session_phase"] = phase
        # `None` is meaningful: a new session for a scene without camera candidates must clear
        # the previous scene's binding rather than silently carrying its id forward.
        self.raw["camera_id"] = camera_id
        self.raw["camera_selection_reason"] = camera_reason
        self.save()

    def set_phase(self, phase: str) -> None:
        if phase not in SESSION_PHASES:
            raise ValueError(f"unknown session phase {phase!r}")
        if self.raw["session_phase"] == phase:
            return
        self.raw["session_phase"] = phase
        self.save()

    def bind_camera(self, camera_id: int, *, camera_reason: str | None = None) -> None:
        """§9: the camera a session is bound to, recorded once and never guessed."""
        self.raw["camera_id"] = camera_id
        self.raw["camera_selection_reason"] = camera_reason
        self.save()

    def clear_camera(self) -> None:
        """Forget a camera when the newly adopted scene reports no camera candidate."""
        if self.raw["camera_id"] is not None:
            self.raw["camera_id"] = None
            self.raw["camera_selection_reason"] = None
            self.save()

    def reset_after_gateway_restart(self) -> None:
        """Forget the old live binding when the process has no cached Scene to verify it against."""
        if self.lease is not None or self.quarantine is not None:
            return
        self.raw["session_id"] = None
        self.raw["session_phase"] = "setup"
        self.raw["camera_id"] = None
        self.raw["scene_identity"] = None
        self.save()

    # -- the edit lock (§7.2) and its one way out (§8.3) ---------------------------

    def acquire_lease(self, record: dict[str, Any]) -> dict[str, Any]:
        """§7.2: the lock is held by whoever the last successful acquire named.

        There is no re-grant and no queue: a second acquire while a lease stands is refused by
        the caller, because a second token would silently retire the first one's holder and leave
        Brain holding a piece of paper the gateway has already forgotten.
        """
        self.raw["lease"] = record
        self.save()
        return self.raw["lease"]

    def release_lease(self, *, keep_locked: bool) -> dict[str, Any] | None:
        """Hand the lease back; `keepLocked` quarantines the 现场 it was guarding (§7.2/§8.2).

        The release that says "I am not sure what landed" is the one that has to leave a record:
        the four-tuple stored here is the one acquire time agreed to, because after a timeout
        nobody knows what the scene holds, and a record that named a *guessed* state would be
        exactly the invention §8.1 forbids. The retired token rides along so §8.3's third step
        ("排除迟到写入") has something to recognise a stale request by.
        """
        held = self.lease or {}
        self.raw["lease"] = None
        if not keep_locked:
            self.save()
            return None
        self.raw["quarantine"] = {"browser_id": held.get("browser_id"),
                                  "session_id": held.get("session_id"),
                                  "task_id": held.get("task_id"),
                                  "retired_lease_token": held.get("leaseToken"),
                                  "scene_identity": held.get("scene_identity"),
                                  "commit_unconfirmed": held.get("commit_unconfirmed", False),
                                  "quarantined_at": int(time.time())}
        self.save()
        return self.raw["quarantine"]

    def resolve_lock(self, record: dict[str, Any], *, cleared: bool,
                     retire_session: bool = False) -> dict[str, Any]:
        """§8.3 ⑤: the conclusion and its evidence are written down either way.

        A `kept_locked` answer has to be on disk too -- §8.3's rule that "no new session, no tab
        switch, no restart is evidence" only bites if the attempts themselves are auditable.
        `cleared` is step ⑥'s unlock, and it drops both records because a `resolved` conclusion
        means the uncertainty is over: leaving a lease standing would force the holder to re-acquire
        with a token it may have lost in the very timeout this route is settling.
        `retire_session` is the `new_session` branch: the old session id goes, so the next
        `/sessions` cannot be read as a resume of the session this conclusion just closed. Neither
        touches `session_phase` -- POST /sessions stays the only door back to `setup`.
        """
        self.raw["last_resolution"] = record
        if cleared:
            self.raw["lease"] = None
            self.raw["quarantine"] = None
        if retire_session:
            self.raw["session_id"] = None
        self.save()
        return self.raw["last_resolution"]

    # -- object id water mark (§6) -------------------------------------------------

    @property
    def next_object_id(self) -> int:
        return _uint(self.raw["next_object_id"])

    def raise_water_mark(self, ids: list[int]) -> int:
        """`max(持久, 文档最大 id + 1)`; only ever up, never down (§6)."""
        highest = self.next_object_id
        for value in ids:
            if value + 1 > highest:
                highest = value + 1
        if highest != self.next_object_id:
            self.raw["next_object_id"] = highest
            self.save()
        return self.next_object_id

    def allocate_object_ids(self, count: int) -> list[int]:
        """Hand out `count` ids and advance the water atomically (§6).

        The last block stops one short of `2**32 - 1` so the water mark itself stays a legal
        `X-Next-Object-ID` (§5.1) after the hand-out; the id it leaves unused is unreachable
        in practice -- four billion objects do not fit in a 100 MB payload.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        first = self.next_object_id
        if first + count > MAX_UINT32:
            raise ObjectIDSpaceExhausted(
                f"{count} ids needed from {first}, but the uint32 space ends at {MAX_UINT32 - 1}")
        self.raw["next_object_id"] = first + count
        self.save()
        return list(range(first, first + count))

    # -- what the browser reads back ------------------------------------------------

    def record_scene(self, document: Any) -> dict[str, Any]:
        """Adopt one reported world: raise the water from its ids and keep its four-tuple."""
        self.raise_water_mark(object_ids_in_document(document))
        identity = scene_identity(document)
        self.raw["scene_identity"] = identity
        self.save()
        return identity

    def public_lease(self) -> dict[str, Any] | None:
        """§7.2's lease as a read route may show it: the token stays with its holder.

        `leaseToken` is the credential its holder presents in `X-Brain-Lease` (§9.1's 租约闸). An
        endpoint every tab polls would otherwise hand each of them the ability to pass that gate as
        if it held the lock. Everything a reader genuinely needs -- who holds it, over which 现场,
        since when -- survives the mask, and the browser still learns the token where it must act on
        it: inside the `captureScene`/`get2DAABB` frame it is answered with.
        """
        lease = self.lease
        if lease is None:
            return None
        return {key: value for key, value in lease.items() if key != "leaseToken"}

    def session_view(self) -> dict[str, Any]:
        """GET /api/sessionState: the persisted half of the session, plus the last identity.

        §5 of the flow doc lists seven fields; `browser_id` and `next_object_id` ride along
        because the route answers a browser that has just refreshed, and it cannot decide
        whether the phase it is reading is *its own* without naming the bound browser, nor seed
        a local allocator without the water mark (§6: `X-Next-Object-ID` is the only seed).
        """
        identity = self.scene_identity or {key: None for key in SCENE_IDENTITY_KEYS}
        view = {"session_phase": self.session_phase, "lease": self.public_lease(),
                "quarantine": self.quarantine, "browser_id": self.browser_id,
                "next_object_id": self.next_object_id, **identity}
        if self.session_id is not None:
            view["sessionID"] = self.session_id
        if self.camera_id is not None:
            view["camera_id"] = self.camera_id
            if self.raw.get("camera_selection_reason"):
                view["camera_selection_reason"] = self.raw["camera_selection_reason"]
        return view

    def session_record(self) -> dict[str, Any]:
        """GET /sessions (§5): the binding view, without the scene -- that read is /getWorld's job.

        `session_view` answers "what phase am I in and what was the last scene I agreed to",
        which is what a refreshed browser needs; this answers "who holds the scene right now",
        which is what Brain needs before it dares to switch. The two stay separate on purpose:
        a caller that only wants the lock state should not have to carry a digest it did not ask for.
        """
        return {"sessionID": self.session_id, "session_phase": self.session_phase,
                "browserID": self.browser_id, "camera_id": self.camera_id,
                "lease": self.public_lease(), "quarantine": self.quarantine}


def is_transaction_id(value: Any) -> bool:
    """Whether one caller-supplied `transactionID` may name a receipt file (§8.1)."""
    return isinstance(value, str) and TRANSACTION_ID_PATTERN.fullmatch(value) is not None


class TransactionReceiptError(RuntimeError):
    """A receipt file that exists but does not read as a commit record.

    This is deliberately not folded into "no receipt": spec §9.2 makes `unknown` the answer that
    *keeps the lock*, so reporting a corrupt record as `unknown` would freeze a session on a
    statement we cannot support, while reporting it as 500 says which of the two facts we hold.
    """


class TransactionReceipts:
    """§8.1/§9.2: `var/gateway_transactions/`, one receipt file per transactionID.

    A directory rather than keys inside `gateway_state.json` because that file is rewritten whole
    on every change (spec §9.2 says so outright): one commit would otherwise rewrite every
    receipt ever issued, and the receipt has to outlive both the browser and this process.
    `directory=None` keeps the same object in memory for a caller that has not decided where the
    disk is (the test gateways); both real entry points name a directory, and the disposable
    verification stack must get a temporary one, never the shipped `var/`.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = directory if isinstance(directory, Path) else (Path(directory) if directory is not None else None)
        self.memory: dict[str, dict[str, Any]] = {}

    def load(self, transaction_id: str) -> dict[str, Any] | None:
        """The persisted receipt, or None when this transaction was never settled here."""
        if self.directory is None:
            return self.memory.get(transaction_id)
        target = self.directory / f"{transaction_id}.json"
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise TransactionReceiptError(f"receipt for {transaction_id} is unreadable ({error})") from error
        try:
            stored = json.loads(raw)
        except ValueError as error:
            raise TransactionReceiptError(f"receipt for {transaction_id} is not JSON ({error})") from error
        if not isinstance(stored, dict) or stored.get("status") not in RECEIPT_STATUSES:
            raise TransactionReceiptError(
                f"receipt for {transaction_id} holds a {type(stored).__name__} without a known status")
        return stored

    def save(self, transaction_id: str, receipt: dict[str, Any]) -> None:
        """Write the receipt atomically, so a reader never sees half a commit record."""
        if self.directory is None:
            self.memory[transaction_id] = receipt
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{transaction_id}.json"
        handle, tmp = tempfile.mkstemp(dir=str(self.directory), prefix=target.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(receipt, out, ensure_ascii=False, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
            _fsync_directory(self.directory)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
