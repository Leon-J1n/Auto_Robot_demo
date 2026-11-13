"""Standalone local gateway for the WebEditor.

The browser owns the scene (spec v2 §1.1); this service is the channel layer
between HTTP callers and the viewers on /events, plus the Agent task relay of
§5.15 (the only party that talks to the Agent service). It intentionally does
not import RobotSimPlatform or sim_platform, so UI work can be debugged on a
clean Python environment.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
from contextvars import ContextVar
import hashlib
import json
import logging
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.http_exceptions import HttpProcessingError

from digest import scene_digest
from speech_relay import SpeechRelay
from gateway_state import (
    MAX_ALLOCATE_COUNT,
    MAX_UINT32,
    SCENE_IDENTITY_KEYS,
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

LOGGER = logging.getLogger("web_frontend.local_gateway")
ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "constant_config.json"
GATEWAY_APP_KEY = web.AppKey("gateway", object)

# The only actions a browser answers (contracts/types.ts BrowserAction).
BROWSER_ACTIONS = frozenset({
    "updateWorld", "updateItem", "addObject", "removeObject",
    "updateTraj", "captureScene", "get2DAABB", "getWorld",
})

# §8.1: the two answers that mean "the browser never settled the request". A browser cannot emit
# either code -- its write handler only ever reports a WorldError code -- so this is the whole test
# for "write no receipt: the transaction is unknown, and unknown keeps the lock". Everything the
# browser does answer is a refusal it actually made, which is worth a persisted `rejected` record.
UNANSWERED_CODES = frozenset({"NO_BROWSER_CONNECTED", "BROWSER_TIMEOUT"})

# Runtime constants are read from config/constant_config.json at startup and
# handed to the browser through GET /api/runtimeConfig (spec v2.1 9.1/9.2).
DEFAULT_CONSTANT_CONFIG: dict[str, Any] = {
    "gateway": {"host": "0.0.0.0", "port": 8099},
    "library_server_url": "http://127.0.0.1:8000",
    "speech_service_url": "http://127.0.0.1:8600",
    "cos_base_url": "",
    "max_payload_bytes": 104857600,
    "browser_request_timeout_seconds": 10,
    "capture_timeout_seconds": 15,
    "max_pending_requests": 8,
    "camera_preview": {"max_width": 640, "max_height": 480, "max_hz": 30},
    # §7.3/Q97: how opaque the segment-end ghosts are. The browser is the only consumer, so
    # unlike the agent trio below this one is a field of RuntimeConfig.
    "ghost_pose_opacity": 0.5,
    # §7.3/Q101: the trajectory waypoint spheres -- radius in world metres and one point every
    # N frames. Browser-only, like the opacity above.
    "traj_waypoint_radius": 0.01,
    "traj_waypoint_stride_frames": 10,
    # §4.8.5/Q118: whether the stage projects shadows when `viewport.shadows` has hidden the
    # toolbar checkbox. Browser-only, like the three above, and the browser's own default is
    # `true` -- see `logic.ts:shadowsEnabled()`.
    "shadows_enabled": True,
    # §4.8.6/Q149: whether a URDF object's wireframe shows one box per link or the one box the
    # links union into. Defaults to per-link boxes for a tighter visual fit; `/get2DAABB` still
    # answers with the union either way, and
    # browser-only like the constants around it, so it is a field of RuntimeConfig.
    "urdf_bounding_box_per_link": True,
    # §4.4/Q145: the colour the viewport clears to, as a CSS hex string (#RGB or #RRGGBB).
    # Browser-only, like the four above -- the gateway never renders -- and the browser's own
    # default is black; see `logic.ts:viewportBackground()`.
    "viewport_background_color": "#000000",
    # §5.15/Q86: the three Agent task constants. Only the gateway consumes them -- the
    # browser never calls the Agent service, so they are not fields of RuntimeConfig.
    "agent_service_url": "http://127.0.0.1:8500",
    "agent_poll_interval_seconds": 5,
    "disable_agent_details": False,
    "agent_request_timeout_seconds": 10,
}
NESTED_CONFIG_KEYS = ("gateway", "camera_preview")

# §7.4 (Q116): the Agent service owns its running-state vocabulary. The gateway stores
# `state` verbatim and never judges a task finished by its name -- `terminal` is the
# only terminal criterion, and a missing or non-boolean one reads as "not finished".
AGENT_POLL_ERROR_LIMIT = 5
# §5.15: the one line every disabled-route refusal says, from the submission gate and from the
# relay itself, so `agent_service_url` being empty has a single wording in this build.
AGENT_DISABLED_MESSAGE = "agent_service_url is empty; the Agent task route is disabled (§5.15)"
# §7.4 rule 3: `outcome` is the one fixed enumeration brain-web/2 keeps. Anything else
# is a contract break and is published as `None` rather than guessed at.
AGENT_OUTCOMES = ("scene_updated", "answered", "clarification_required", "plan_ready", "failed")
# §7.3: the only artifacts a job view may name. `debug/` and server paths stay invisible.
AGENT_ARTIFACT_NAMES = ("scene_config", "scene_patch", "commands", "query_result")
# §8.1/§9.2: the one sentence for "our disk has no receipt for this transaction", said both by
# `queryWorldCommit` and by a job view's `commit_receipts`. A caller that has to compare the text
# should not find two spellings of the same absence in one build.
NO_RECEIPT_REASON = "no commit receipt for this transaction"
# §5.3 (Q115): the four self-graded fields of Brain's `/health` and the value each has to
# carry for the service to count as ready. These are Brain's words, quoted here for one
# comparison -- unlike the task `state` (§7.4) this vocabulary is part of the health
# contract, so a new value means the service is telling us it is not ready, and `degraded`
# is the right reading of that.
AGENT_HEALTH_GOOD = {"service": "ready", "qwen": "ready", "browser": "bound", "assets": "ok"}
# §5.3/Q115: the two built-in health numbers. They are deliberately not fields of the
# constant config -- every configurable value is one more place to deploy wrong, and no
# caller reads them except these two routes.
HEALTH_PROBE_TIMEOUT_SECONDS = 3.0
AGENT_HEALTH_CACHE_SECONDS = 5.0
# §5.2: the eight capability names this gateway promises, spelled out because Brain checks
# **names** and never infers behaviour from a version number. The order is the flow doc's.
BRAIN_WEB2_PROTOCOL = "brain-web/2"
BRAIN_WEB2_CAPABILITIES = ("atomic_commit", "sub_versioned_scene_identity", "setup_only_local_edits",
                           "bound_browser", "task_edit_lock", "canonical_scene",
                           "agent_owned_phase", "uncertain_recovery")
# §5.15: what GET /api/agent/jobs[/{id}] returns per task. The scene the browser sent is
# deliberately absent -- a world can be 100 MB (§11.1) and this route is a list.
AGENT_JOB_FIELDS = ("id", "task_id", "command", "agent_state", "terminal", "state_detail",
                    "outcome", "reply", "plan", "metrics", "artifacts", "commit_receipts",
                    "message", "failure_reason", "created_at", "updated_at", "conversation_id")


class ConfigError(RuntimeError):
    """Constant config file exists but cannot be used."""


def _merge_config_section(defaults: Any, override: Any, name: str) -> dict[str, Any]:
    if not isinstance(override, dict):
        raise ConfigError(f'config section "{name}" must be an object')
    merged = dict(defaults)
    merged.update(override)
    return merged


def load_constant_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Return runtime config, falling back to defaults only when the file is absent."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        LOGGER.warning("constant config %s not found; using built-in defaults", target)
        return json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    except OSError as error:
        raise ConfigError(f"cannot read {target}: {error}") from error
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ConfigError(f"invalid JSON in {target}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"{target} must contain a JSON object")
    config = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    config.update({key: item for key, item in value.items() if key not in NESTED_CONFIG_KEYS})
    for key in NESTED_CONFIG_KEYS:
        section = value.get(key)
        default_section = DEFAULT_CONSTANT_CONFIG[key]
        if section is None:
            config[key] = dict(default_section)
        else:
            config[key] = _merge_config_section(default_section, section, key)
    return config


def agent_state_text(state: Any) -> str:
    """§7.4 (Q116): the Agent's own state string, verbatim. No enum, no translation, no fallback word."""
    return state if isinstance(state, str) else ""


def agent_terminal(payload: dict[str, Any]) -> bool:
    """§7.4 rule 2: `terminal` is the only terminal criterion, and only `true` counts.

    Missing, `null`, `"true"` and `1` all read as "still running". Saying it ended on the
    Agent's behalf would freeze the task halfway forever, and a service with a richer
    vocabulary than ours converges on its own terms.
    """
    value = payload.get("terminal")
    if value is True:
        return True
    if value is not False:
        LOGGER.warning("Agent reply carries no boolean `terminal` (%r); keeping the task running", value)
    return False


def agent_state_detail(payload: dict[str, Any]) -> str | None:
    """§7.4 rule 3: Brain's optional progress line, passed through as it arrived."""
    value = payload.get("state_detail")
    return value if isinstance(value, str) and value.strip() else None


def agent_outcome(value: Any) -> str | None:
    """§7.3: one of the five, or `None`. An outcome the contract does not name is a break, not a guess."""
    if isinstance(value, str) and value in AGENT_OUTCOMES:
        return value
    if value not in (None, ""):
        LOGGER.warning("Agent result reports outcome %r, outside the five of §7.3", value)
    return None


def agent_reply(value: Any) -> str | None:
    """§7.3: the answer or clarification text, when the result has one."""
    return value if isinstance(value, str) and value.strip() else None


def agent_artifact_names(value: Any) -> list[str]:
    """§7.3: only the four whitelisted artifact names reach a browser.

    Brain hands `artifacts` as a list of `{name, …}` objects; anything that is not one of
    the four -- a `debug/` entry, a server path -- is dropped here rather than published
    and re-filtered by every viewer.
    """
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for entry in value:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name in AGENT_ARTIFACT_NAMES and name not in names:
            names.append(name)
    return names


def agent_transaction_ids(value: Any) -> list[str]:
    """§7.3 rule 6: which transactions a finished task says it committed -- names only.

    `commitReceipts` reaches us either as bare ids or as the §8.1 receipt objects, and both
    spellings answer the same question, "which ledgers to open". Which ledgers say what is a
    second question, and only this gateway's own disk answers it, so nothing here but the name
    is read from upstream. An entry that cannot name a file (§8.1's whitelist) is dropped.
    """
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for entry in value:
        transaction_id = entry.get("transactionID") if isinstance(entry, dict) else entry
        if isinstance(transaction_id, str) and is_transaction_id(transaction_id) and transaction_id not in ids:
            ids.append(transaction_id)
    return ids


def agent_task_id(value: Any) -> int | None:
    """§5.15: the `taskID` /startTask hands back. Same ASCII-digit trap as ids (see _is_uint_text)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def agent_error_text(payload: Any, fallback: str) -> str:
    """§5.15: the Agent service's own error text, verbatim -- the gateway writes no prose for it."""
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return fallback


# §5.13: the HTTP status of an error a browser reports back in browser.response.
# Codes the spec does not list stay 502 — the browser could not produce a result.
BROWSER_ERROR_STATUS: dict[str, int] = {
    "MISSING_PARAM": 400, "INVALID_PARAM": 400, "INVALID_WORLD_SCHEMA": 400,
    "BODY_ID_MISMATCH": 400, "MISSING_TRANSFORM_COMPONENT": 400, "INVALID_PARENT_CYCLE": 400,
    "CAMERA_TYPE_MISMATCH": 400, "EMPTY_AABB": 400,
    "TRAJ_INVALID_MAGIC": 400, "TRAJ_UNSUPPORTED_VERSION": 400, "TRAJ_TRUNCATED": 400,
    "TRAJ_POSE_DIM_UNSUPPORTED": 400,
    "OBJECT_NOT_FOUND": 404, "CAMERA_NOT_FOUND": 404, "MODEL_NOT_FOUND": 404, "SCENE_NOT_FOUND": 404,
    "ID_ALREADY_EXISTS": 409, "VERSION_CONFLICT": 409,
    "MODEL_METADATA_INVALID": 422, "DEPENDENCY_FETCH_FAILED": 424,
    # The browser answers INTERNAL_ERROR for an action this build has no handler for,
    # and for a failure inside its own handler (§5.13: 未预期异常). Reading it as 502
    # would tell the caller "OLS or rendering broke upstream" when the viewer crashed.
    "INTERNAL_ERROR": 500,
    "SCENE_NOT_LOADED": 503,
    "UPSTREAM_ERROR": 502, "UPSTREAM_RENDER_FAILED": 502, "GATEWAY_FRAMING_ERROR": 502,
}

# §5.2: rendering requests get the longer timeout.
RENDER_ACTIONS = frozenset({"captureScene", "get2DAABB"})

# §9: "播放中、旧缓存、错浏览器、尺寸不符、PNG 不完整一律拒绝". The last two are checkable without
# decoding a pixel: the magic, the size the file declares in its own IHDR, and the IEND chunk that
# closes a png. The gateway still never reads the image's contents.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
# magic + IHDR's length field + its type field, then the two uint32 that are the size.
PNG_IHDR_OFFSET = 16

# §5.9: caller-supplied bodies are capped by max_payload_bytes (100 MB by default).
# `MAX_UINT32` is imported from `gateway_state` -- one id space for the query gate and the
# persistent water mark, so the two can never disagree about the ceiling.
# §5.3 (Q99): the multipart field name every trajectory part must carry.
TRAJ_PART_FIELD = "trajs"


def decode_frame_bytes(raw: Any) -> bytes | None:
    """One base64 field as bytes, or None when it is absent or not base64 at all."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError:
        return None


def png_frame_size(image: bytes) -> tuple[int, int] | None:
    """The size a png declares for itself, or None when it is not a whole png.

    Reads the header and the trailer and nothing between them. §9 asks for "尺寸不符／PNG 不完整",
    and both are answerable from the file's own IHDR and its closing IEND chunk; decoding pixels is
    the one thing this gateway must not start doing on the way past.
    """
    if not image.startswith(PNG_MAGIC) or image[12:16] != b"IHDR":
        return None
    if len(image) < PNG_IHDR_OFFSET + 8 or not image.endswith(PNG_IEND):
        return None
    width, height = struct.unpack(">II", image[PNG_IHDR_OFFSET:PNG_IHDR_OFFSET + 8])
    if width < 1 or height < 1:
        return None
    return width, height


def camera_candidate_ids(reported: Any) -> list[int]:
    """§9 item 3: the candidate cameras a browser says its scene holds, as we will publish them.

    The list is the browser's, never ours -- deciding that an entity carries a `Camera` component
    means reading the document, and this gateway does not read documents (§3). What we do decide is
    the shape: a non-uint32 entry is dropped, the reported order is kept, and a report without a
    list empties it. Empting on a missing key is the point -- a 候选清单 left over from the scene
    before this one is exactly the 旧值 that §9 refuses on the two collection routes.
    """
    if not isinstance(reported, list):
        return []
    ids: list[int] = []
    for value in reported:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_UINT32:
            continue
        if value not in ids:
            ids.append(value)
    return ids


class WriteOutcome(NamedTuple):
    """What a browser request gives a caller: its payload plus the id it answered under.

    §5.4 used to carry "who answered" here so viewers could re-source that browser; with one
    binding that responder is always the bound tab and `source_browser_id` is a drifting
    redundancy, so it is gone -- and with it the per-request responder bookkeeping that only
    `dispatch_with_source` ever drained (non-write responses leaked an entry each).
    """

    payload: dict[str, Any]
    request_id: str


class HealthProbe(NamedTuple):
    """§5.3 (Q115): one reading of the Agent service's own `/health`.

    `body` is the bytes upstream sent, so a passthrough route can hand them over without
    ever parsing them; `payload` is the same thing decoded for `/api/health`'s sub-checks;
    `detail` is set only when there is no reading at all -- no service, no connection, no
    answer in 3 s, or a body that is not JSON. `status` is upstream's, which is 0 when
    `detail` says why there is no upstream answer.
    """

    status: int
    body: bytes
    payload: Any
    detail: str | None


class TaskContext(NamedTuple):
    """§7.1 (C3): what rides with a command besides its text -- one live read, named.

    `start_task_fields` is the only place these seven wire names are spelled, so a field the flow
    doc adds or drops cannot be half-converted. Two of them are the nullable halves the protocol
    already accepts: `scene_id`/`scene_version` for a browser that has never reported a scene, and
    `session_id` until Brain opens a session of its own through `POST /sessions` (spec §5.15 step 3)
    -- a session id invented here would be the one number Brain cannot match against its own route.
    """

    scene_digest: str
    scene_id: int | None
    scene_version: int | None
    scene_sub_version: int
    session_id: str | None
    client_request_id: str
    browser_id: str

    def start_task_fields(self) -> dict[str, Any]:
        return {"sceneDigest": self.scene_digest, "scene_id": self.scene_id,
                "scene_version": self.scene_version, "scene_sub_version": self.scene_sub_version,
                "sessionID": self.session_id, "clientRequestID": self.client_request_id,
                "browserID": self.browser_id}


class LiveWorld(NamedTuple):
    """One `getWorld` of one browser: the document, the four-tuple read out of it, its play state.

    §5.15 step 2 and §5.8's route both need exactly these three, and the identity is the body's own
    (§5.1) rather than the flat fields the browser happened to send beside it.
    """

    document: dict[str, Any]
    identity: dict[str, Any]
    playback_state: Any


def agent_service_health(probe: HealthProbe) -> dict[str, Any]:
    """§5.3: the `agent_service` sub-item of GET /api/health, out of one Brain `/health` reading.

    `ready` only when every field Brain grades itself with grades itself well. The words are
    Brain's (§5.3), so this reads them and says nothing about why -- the service status bar
    shows the raw /health passthrough next to it, and the viewer has the upstream text.
    """
    if probe.detail is not None:
        return {"status": "unreachable", "detail": probe.detail}
    payload = probe.payload if isinstance(probe.payload, dict) else None
    if payload is None:
        return {"status": "degraded", "detail": f"the Agent service answered /health with HTTP {probe.status} and no object"}
    bad = [f"{key}={payload.get(key)}" for key, good in AGENT_HEALTH_GOOD.items() if payload.get(key) != good]
    if bad:
        return {"status": "degraded", "detail": ", ".join(bad)}
    return {"status": "ready", "detail": None}


class BrowserError(web.HTTPError):
    """A gateway error in the §5.6 body shape; aiohttp returns it to the caller as-is."""

    def __init__(self, status: int, code: str, message: str, *, request_id: str | None = None) -> None:
        self.code = code
        # §5.15: the poller re-quotes an upstream message inside another code, so the
        # prose has to survive the trip into the §5.6 body shape below.
        self.message = message
        self.request_id = request_id
        # aiohttp reads status_code inside HTTPException.__init__.
        self.status_code = status
        super().__init__(
            text=json.dumps({"error": {"code": code, "message": message, "request_id": request_id}}),
            content_type="application/json",
        )


class PendingRequests:
    """§5.2 bookkeeping: request_id -> Future, first response wins, §5.7 concurrency cap."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def open(self, request_id: str) -> asyncio.Future[dict[str, Any]]:
        if len(self._futures) >= self.limit:
            raise BrowserError(429, "TOO_MANY_REQUESTS", f"{self.limit} requests are already pending")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._futures[request_id] = future
        return future

    def resolve(self, request_id: str, payload: dict[str, Any]) -> bool:
        return self._settle(request_id, lambda future: future.set_result(payload))

    def reject(self, request_id: str, error: BrowserError) -> bool:
        return self._settle(request_id, lambda future: future.set_exception(error))

    def abandon(self, request_id: str) -> None:
        self._futures.pop(request_id, None)

    def _settle(self, request_id: str, action: Any) -> bool:
        future = self._futures.pop(request_id, None)
        if future is None or future.done():
            return False
        action(future)
        return True


class AgentRelay:
    """§5.15: the only party in this build that talks to the Agent service.

    It returns the Agent's own status code and body so the caller can tell a
    "task unknown" (404) from a broken transport, and raises the §5.6 envelope
    for the failures a caller has to see as HTTP: `501 AGENT_UNAVAILABLE` when
    `agent_service_url` is empty, `502 UPSTREAM_ERROR` when unreachable or
    answered in a shape we cannot read, `504 AGENT_TIMEOUT` on the client timeout.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @property
    def base_url(self) -> str:
        return str(self.config.get("agent_service_url") or "").rstrip("/")

    async def request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                      json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        base = self.base_url
        if not base:
            raise BrowserError(501, "AGENT_UNAVAILABLE", AGENT_DISABLED_MESSAGE)
        timeout = float(self.config.get("agent_request_timeout_seconds", 10))
        try:
            client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
            async with client:
                async with client.request(method, f"{base}{path}", params=params, json=json_body) as response:
                    status = response.status
                    raw = await response.read()
        except asyncio.TimeoutError as error:
            raise BrowserError(504, "AGENT_TIMEOUT",
                               f"the Agent service did not answer {method} {path} in {timeout}s") from error
        except aiohttp.ClientError as error:
            raise BrowserError(502, "UPSTREAM_ERROR",
                               f"the Agent service at {base} is unreachable: {error}") from error
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError as error:
            raise BrowserError(502, "UPSTREAM_ERROR",
                               f"the Agent service answered {path} with a body that is not JSON: {error}") from error

    async def start_task(self, command: str, scene: dict[str, Any],
                         context: TaskContext) -> tuple[int, str, str | None]:
        """§5.15 step 2/3: hand over one command and the one read that describes its 现场.

        The context is the four-tuple plus the digest of `scene` itself, taken from the same
        `getWorld` this `scene` came out of (§7.1, §14.6 rule 1). It is not decoration: Brain
        answers C4/C5 and later asks `/acquireSceneLease` over these numbers, so a version read
        from a cache would open a lease over a scene nobody is looking at.

        A reply that claims `terminal` is warned about and dropped (§7.4 rule 8): a task
        that was just created is running, and the first `/queryTaskState` is the only
        thing that may say otherwise -- so this returns no terminal flag at all.
        """
        status, payload = await self.request("POST", "/startTask",
                                            params=None,
                                            json_body={"command": command, "scene": scene,
                                                       **context.start_task_fields()})
        if status >= 400 or not isinstance(payload, dict):
            detail = agent_error_text(payload, f"Agent service answered /startTask with HTTP {status}")
            # Preserve Brain's semantic 4xx refusal.  Unsupported/empty platforms must not be
            # rewritten as UPSTREAM_ERROR, otherwise the UI cannot distinguish a safe refusal
            # from a transport failure and may leave the editor in agent_owned.
            if 400 <= status < 500 and isinstance(payload, dict):
                error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else detail
                if isinstance(code, str) and code:
                    raise BrowserError(status, code, str(message or detail))
            raise BrowserError(502, "UPSTREAM_ERROR", detail)
        task_id = agent_task_id(payload.get("taskID"))
        if task_id is None:
            raise BrowserError(502, "UPSTREAM_ERROR",
                               f"Agent /startTask replied without a usable taskID: {payload.get('taskID')!r}")
        if payload.get("terminal") is True:
            LOGGER.warning("Agent /startTask replied terminal:true for task %s; the first poll decides (§7.4)", task_id)
        return task_id, agent_state_text(payload.get("state")), agent_state_detail(payload)

    async def task_state(self, task_id: int) -> tuple[int, str, bool, str | None]:
        """§5.15 step 3: one /queryTaskState. A 404 comes back as (404, text, False, None) for the caller."""
        status, payload = await self.request("GET", "/queryTaskState", params={"taskID": str(task_id)})
        if status >= 400:
            return status, agent_error_text(payload, f"Agent /queryTaskState replied HTTP {status}"), False, None
        if not isinstance(payload, dict):
            return 502, "Agent /queryTaskState answered without a task object", False, None
        return status, agent_state_text(payload.get("state")), agent_terminal(payload), agent_state_detail(payload)

    async def task_result(self, task_id: int) -> dict[str, Any] | None:
        """§7.4: one /queryTaskResult, asked only after the task reported itself terminal.

        Only HTTP 200 is a result. Brain answers 202 while a task is still running (§7.3),
        and a 202 body is a statement about the task, not its outcome -- reading it as a
        result would publish "no result" for a task that merely has not written one yet.
        An unreadable result is not a failure: it answers `None`, and the job settles on the
        state text with no `outcome`. A transport failure is left to the caller, which counts
        it towards the poll limit like any other.
        """
        status, payload = await self.request("GET", "/queryTaskResult", params={"taskID": str(task_id)})
        if status != 200 or not isinstance(payload, dict):
            LOGGER.warning("Agent /queryTaskResult for task %s replied HTTP %s, no result read", task_id, status)
            return None
        return payload

    async def failure_reason(self, task_id: int) -> str | None:
        """§5.15 step 5: the reason line, fetched before the failed state is published."""
        status, payload = await self.request("GET", "/queryTaskFailureReason", params={"taskID": str(task_id)})
        if status >= 400 or not isinstance(payload, dict):
            LOGGER.warning("Agent /queryTaskFailureReason for task %s failed with HTTP %s", task_id, status)
            return None
        reason = payload.get("failureReason")
        return reason if isinstance(reason, str) and reason.strip() else None

    async def artifact(self, task_id: int, name: str) -> tuple[int, bytes, str]:
        """§5.15 (E2): one whitelisted artifact, brought back as the bytes Brain sent.

        `request()` cannot serve this: an artifact is a file, and reading it as JSON would hand the
        caller our framing where Brain's own bytes belong. The status travels back as itself -- a
        404 is Brain saying it has forgotten the task, which is a fact the caller has to be able to
        tell from a broken pipe, and this process's job table gets no vote on what may still be
        downloaded (§13 O10: the table is memory, the artifact is disk).
        """
        base = self.base_url
        if not base:
            raise BrowserError(501, "AGENT_UNAVAILABLE", AGENT_DISABLED_MESSAGE)
        # `name` is one of AGENT_ARTIFACT_NAMES before this is reached, so no separator can be in
        # the path we build here -- the whitelist is the whole path check.
        path = f"/tasks/{task_id}/artifacts/{name}"
        timeout = float(self.config.get("agent_request_timeout_seconds", 10))
        try:
            client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
            async with client:
                async with client.get(f"{base}{path}") as response:
                    return response.status, await response.read(), response.headers.get("Content-Type", "")
        except asyncio.TimeoutError as error:
            raise BrowserError(504, "AGENT_TIMEOUT",
                               f"the Agent service did not answer GET {path} in {timeout}s") from error
        except aiohttp.ClientError as error:
            raise BrowserError(502, "UPSTREAM_ERROR",
                               f"the Agent service at {base} is unreachable: {error}") from error

    async def service_health(self) -> HealthProbe:
        """§5.3 (Q115): one `GET /health` upstream, on its own 3-second clock.

        The body travels back as the bytes Brain sent, because this reading is Brain's and
        the gateway is only the pipe that brings it here -- the §5.3 rule "不裁剪、不改写、
        不重排" is easiest to keep by never touching it. `detail` is set only when there is
        nothing to bring back: no `agent_service_url`, no connection, no answer in 3 s, or a
        body that is not JSON.
        """
        base = self.base_url
        if not base:
            return HealthProbe(0, b"", None, "agent_service_url is empty; there is no Agent service to ask")
        try:
            client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HEALTH_PROBE_TIMEOUT_SECONDS))
            async with client:
                async with client.get(f"{base}/health") as response:
                    status = response.status
                    body = await response.read()
        except asyncio.TimeoutError:
            return HealthProbe(0, b"", None,
                               f"the Agent service at {base} did not answer /health in "
                               f"{HEALTH_PROBE_TIMEOUT_SECONDS}s")
        except aiohttp.ClientError as error:
            return HealthProbe(0, b"", None, f"the Agent service at {base} is unreachable: {error}")
        try:
            payload = json.loads(body)
        except ValueError as error:
            return HealthProbe(status, body, None, f"the Agent service answered /health with a body that is not JSON: {error}")
        return HealthProbe(status, body, payload, None)


class BrowserProfile:
    """All mutable gateway state owned by one persistent browser id.

    The original gateway kept these fields directly on ``StandaloneGateway``.  Keeping them in a
    small runtime object lets request handlers select a browser profile without changing the
    existing business methods' field names; ``StandaloneGateway`` exposes profile-aware
    properties below for compatibility with the established tests.
    """

    def __init__(self, state: GatewayState, *, pending_limit: int = 8) -> None:
        self.state = state
        self.pending = PendingRequests(pending_limit)
        self.world_cache: dict[str, Any] | None = None
        self.session_contexts: dict[str, dict[str, Any]] = {}
        self.camera_candidates: list[int] = []
        self.camera_candidates_scene: dict[str, Any] | None = None
        self.scene_version = 0
        self.jobs_state: dict[str, dict[str, Any]] = {}
        self.agent_requests: dict[tuple[str | None, str], dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.active_conversation_id: str | None = None
        self.conversation_restore_session_id: str | None = None
        self.explicit_session_sync_pending: dict[str, Any] | None = None
        self.session_transition_generation = 0
        self.session_transition_lock = asyncio.Lock()
        self.state_sync_lock = asyncio.Lock()
        self.session_rebind_required = state.session_id is not None
        self.agent_health_probe: HealthProbe | None = None
        self.agent_health_at = 0.0


class StandaloneGateway:
    _PROFILE_FIELDS = {
        "world_cache": "world_cache", "session_contexts": "session_contexts",
        "pending": "pending",
        "camera_candidates": "camera_candidates", "camera_candidates_scene": "camera_candidates_scene",
        "scene_version": "scene_version", "jobs_state": "jobs_state",
        "agent_requests": "agent_requests", "conversations": "conversations",
        "active_conversation_id": "active_conversation_id",
        "_conversation_restore_session_id": "conversation_restore_session_id",
        "_explicit_session_sync_pending": "explicit_session_sync_pending",
        "_session_transition_generation": "session_transition_generation",
        "_session_transition_lock": "session_transition_lock",
        "_state_sync_lock": "state_sync_lock",
        "_session_rebind_required": "session_rebind_required",
        "agent_health_probe": "agent_health_probe", "agent_health_at": "agent_health_at",
    }

    def __getattr__(self, name: str) -> Any:
        field = self._PROFILE_FIELDS.get(name)
        profiles = self.__dict__.get("profiles")
        profile_var = self.__dict__.get("_profile_var")
        if field is not None and profiles is not None and profile_var is not None:
            return getattr(self._profile(), field)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        field = self._PROFILE_FIELDS.get(name)
        profiles = self.__dict__.get("profiles")
        profile_var = self.__dict__.get("_profile_var")
        if field is not None and profiles is not None and profile_var is not None:
            setattr(self._profile(), field, value)
            return
        object.__setattr__(self, name, value)

    @web.middleware
    async def profile_middleware(self, request: web.Request, handler):
        stateful_paths = {
            "/api/sessionState", "/api/agent/recover", "/sessions", "/brainCapabilities",
            "/allocateObjectID", "/getWorld", "/captureScene", "/get2DAABB", "/updateWorld",
            "/updateItem", "/addObject", "/removeObject", "/commitWorld", "/queryWorldCommit",
            "/acquireSceneLease", "/releaseSceneLease", "/resolveUncertain", "/updateTraj",
        }
        is_agent_stateful = (request.path.startswith("/api/agent/conversations")
                             or request.path.startswith("/api/agent/jobs")
                             or request.path.startswith("/tasks/"))
        if request.path not in stateful_paths and not is_agent_stateful:
            return await handler(request)
        # The dedicated header selects the browser profile. Existing protocol fields remain
        # business inputs (for example, a requested target browser) and keep their route-level
        # validation semantics. Older direct callers without the header may still identify one
        # profile through an established query/body field.
        browser_id = request.headers.get("X-WebEditor-Browser-ID")
        if browser_id is None:
            browser_id = request.query.get("browser_id") or request.query.get("browserID")
        if (browser_id is None and request.can_read_body
                and request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json"):
            with contextlib.suppress(Exception):
                body = await request.json()
                if isinstance(body, dict):
                    candidate = body.get("browserID") or body.get("browser_id")
                    browser_id = candidate if isinstance(candidate, str) else None
        if browser_id is None:
            session_id = request.headers.get("X-Brain-Session")
            if session_id:
                matches = [key for key, profile in self.profiles.items()
                           if key is not None and profile.state.session_id == session_id]
                if len(matches) == 1:
                    browser_id = matches[0]
        if browser_id is None:
            # Legacy callers and route-shape checks may omit the new header while this gateway
            # still has one anonymous profile. The fallback is deliberately disabled as soon as
            # more than one profile exists; an ambiguous request must never select another
            # browser's scene.
            keys = [key for key in self.profiles if key is not None]
            if len(keys) == 0 and len(self.profiles) == 1:
                # Keep the anonymous bootstrap context for legacy route-shape requests. No
                # browser profile exists yet, so selecting another profile is impossible.
                return await handler(request)
            elif len(keys) == 1 and self._bootstrap_profile_id in keys:
                browser_id = self._bootstrap_profile_id
            else:
                return web.json_response({"error": {"code": "MISSING_BROWSER_ID",
                                      "message": "X-WebEditor-Browser-ID is required when multiple browser profiles are active",
                                      "request_id": None}}, status=400)
        if not isinstance(browser_id, str) or not browser_id:
            return web.json_response({"error": {"code": "INVALID_BROWSER_ID",
                                  "message": "browser id must be a non-empty string",
                                  "request_id": None}}, status=400)
        self._ensure_profile(browser_id)
        token = self._profile_var.set(browser_id)
        try:
            return await handler(request)
        finally:
            self._profile_var.reset(token)

    def _profile(self, browser_id: str | None = None) -> BrowserProfile:
        selected = self._profile_var.get() if browser_id is None else browser_id
        if selected not in self.profiles:
            if selected is None:
                selected = self._bootstrap_profile_id
            else:
                selected = self._ensure_profile(selected)
        return self.profiles[selected]

    def _new_receipts(self, state: GatewayState) -> TransactionReceipts:
        path = None if state.path is None else transactions_dir_for(state.path)
        return TransactionReceipts(path)

    def _ensure_profile(self, browser_id: str) -> str:
        if browser_id in self.profiles:
            return browser_id
        # The first hello claims the old single-profile state. Every later identity gets a
        # separate persistence file, so watermarks and locks cannot bleed across browsers.
        anonymous = self.profiles.get(None)
        if (self._bootstrap_profile_id is None and anonymous is not None
                and anonymous.state.browser_id in (None, browser_id)):
            self.profiles[browser_id] = self.profiles.pop(None)
            self._receipts[browser_id] = self._receipts.pop(None)
            self._bootstrap_profile_id = browser_id
            return browser_id
        base = self._state_path
        profile_path = None
        if base is not None:
            key = hashlib.sha256(browser_id.encode("utf-8")).hexdigest()
            profile_path = base.parent / "gateway_profiles" / f"{key}.json"
        # Each secondary profile already has its own file, so keep that file in the ordinary
        # single-profile shape. The browser id is stored inside it by bind_browser(); the
        # hash only names the file and never becomes protocol data.
        state = GatewayState(profile_path)
        if state.browser_id is None:
            state.bind_browser(browser_id)
        if state.scene_identity is not None and state.lease is None and state.quarantine is None:
            state.reset_after_gateway_restart()
        self.profiles[browser_id] = BrowserProfile(
            state, pending_limit=int(self.config["max_pending_requests"]))
        receipt_key = hashlib.sha256(browser_id.encode("utf-8")).hexdigest()
        receipt_dir = (None if state.path is None else
                       state.path.parent / "gateway_transactions" / receipt_key)
        self._receipts[browser_id] = TransactionReceipts(receipt_dir)
        return browser_id

    @property
    def state(self) -> GatewayState:
        return self._profile().state

    @property
    def receipts(self) -> TransactionReceipts:
        key = self._profile_var.get()
        if key not in self._receipts:
            profile = self._profile()
            key = next((item for item, value in self.profiles.items() if value is profile), self._bootstrap_profile_id)
        return self._receipts[key]

    @receipts.setter
    def receipts(self, value: TransactionReceipts) -> None:
        key = self._profile_var.get()
        if key not in self.profiles:
            key = self._bootstrap_profile_id
        self._receipts[key] = value

    def __init__(self, *, config: dict[str, Any] | None = None,
                 state_path: Path | str | None = None) -> None:
        supplied = load_constant_config() if config is None else config
        # A caller that builds its own dict (the tests do) may leave out a key the shipped
        # file carries; fill from the defaults so `deepcopy` below is the only copy step.
        merged = {**DEFAULT_CONSTANT_CONFIG, **supplied}
        self.config: dict[str, Any] = json.loads(json.dumps(merged))
        self.started = time.time()
        # §5/§6: the persistent half of a session -- which browser is bound, which phase it is
        # in, the object-id water mark, the lease and quarantine records. A caller that does not
        # name a file gets the same object in memory: the two real entry points (`main()` and
        # the disposable verification stack) both name one, and 23 test gateways must not read
        # each other's binding off a shared `var/gateway_state.json`.
        self._state_path = Path(state_path) if state_path is not None else None
        initial_state = GatewayState(state_path)
        # Existing state files remain readable. The saved binding selects the initial profile;
        # otherwise this is the anonymous bootstrap state claimed by the first browser. New
        # browser ids receive independent state files under state_dir/profiles/.
        self._bootstrap_profile_id = initial_state.browser_id
        self._profile_var: ContextVar[str | None] = ContextVar("gateway_browser_profile", default=None)
        self.profiles: dict[str | None, BrowserProfile] = {}
        self.profiles[self._bootstrap_profile_id] = BrowserProfile(
            initial_state, pending_limit=int(self.config["max_pending_requests"]))
        if initial_state.scene_identity is not None and initial_state.lease is None and initial_state.quarantine is None:
            initial_state.reset_after_gateway_restart()
        # §8.1/§9.2: commit receipts live in a directory beside the state file -- one file per
        # transaction, because the state file is rewritten whole on every change and a receipt has
        # to outlive the browser, the caller's timeout and this process. Same isolation rule as
        # the state file: no named directory means in-memory, so a test gateway never writes into
        # the shipped `var/`, and the disposable verification stack names a temporary one.
        self._receipts: dict[str | None, TransactionReceipts] = {
            self._bootstrap_profile_id: self._new_receipts(initial_state)}
        self.clients: set[web.WebSocketResponse] = set()
        # browser_id -> socket, filled by browser.hello (§5.1); only these answer requests.
        self.browsers: dict[str, web.WebSocketResponse] = {}
        self.socket_browser_ids: dict[web.WebSocketResponse, str] = {}
        # §1.4: the last world the browsers reported, cached verbatim without parsing. It records
        # what this gateway saw; since 流程文档 §5.1 (2026-11-05) no read route answers from it, so
        # the age and origin that used to make a cache reading judgeable are gone with the fallback.
        # §5.15: the one Agent task poller of this process, started by create_app().
        self.agent_relay = AgentRelay(self.config)
        self.agent_poller: asyncio.Task[None] | None = None
        # §5.3: the last /health reading this gateway brought back, and when it was taken.
        self.features = self._load_features()
        # A browser id change (for example a new tab after the old tab closed) must not
        # accidentally reuse a Brain session that belongs to the previous browser binding.
        # A Gateway restart may outlive Brain's in-memory/session store. Reconcile the persisted
        # id on the first current-browser scene report instead of sending that stale id to /startTask.

    @staticmethod
    def _copy_json(value: Any) -> Any:
        return json.loads(json.dumps(value))

    def _brain_session_id(self, request: web.Request) -> str | None:
        value = request.headers.get("X-Brain-Session")
        return value if isinstance(value, str) and value else None

    def _session_context(self, session_id: str | None, *, create: bool = False) -> dict[str, Any] | None:
        if not session_id:
            return None
        context = self.session_contexts.get(session_id)
        if context is None and create:
            context = {"world_snapshot": None, "scene_identity": None,
                       "lease": None, "quarantine": None, "camera_id": None,
                       "phase": "setup"}
            self.session_contexts[session_id] = context
        return context

    def _background_context(self, request: web.Request) -> tuple[str, dict[str, Any]] | None:
        session_id = self._brain_session_id(request)
        if not session_id or session_id == self.state.session_id:
            return None
        context = self._session_context(session_id)
        return (session_id, context) if context is not None else None

    def _load_features(self) -> dict[str, bool]:
        try:
            value = json.loads((ROOT / "features.json").read_text(encoding="utf-8"))
            return dict(value.get("features", {}))
        except (OSError, ValueError, TypeError):
            LOGGER.debug("feature config unavailable; using empty feature set")
            return {}

    async def broadcast(self, event: dict[str, Any]) -> None:
        profile_id = self._profile_var.get()
        recipients = list(self.clients)
        if profile_id is not None:
            recipients = [socket for socket in recipients if self._browser_id_for(socket) == profile_id]
        for socket in recipients:
            try:
                await socket.send_json(event)
            except (ConnectionError, RuntimeError):
                self.clients.discard(socket)

    async def health(self, request: web.Request) -> web.Response:
        """§5.3 (Q115): the three sub-items, probed side by side, and this route still 200.

        Its subject is "the gateway is alive", so a dependency that is down or slow shows up
        in its own sub-item and never as a 5xx here -- and none of the three may touch a job
        (§7.4): a failed probe is not evidence about any task.
        """
        agent_view, assets_view = await asyncio.gather(self._agent_service_view(), self._assets_view())
        # Brain reports `browser=not_bound` until the first session is created. Once this
        # Gateway has a live browser websocket, that is an expected pre-session state rather
        # than a degraded Agent service: the next task binds the same browser/session.
        if (agent_view.get("status") == "degraded"
                and "browser=not_bound" in str(agent_view.get("detail") or "")
                and self.browsers):
            agent_view = {"status": "ready", "detail": None}
        # §5.3/Q115 said "name the browser only when the table leaves no doubt"; with the persistent
        # binding (§6) landed there is no longer any doubt to resolve -- the bound browser is named
        # on disk, and a second tab does not make that name ambiguous, it makes it a viewer.
        bound = self.state.browser_id
        return web.json_response({
            "status": "ok",
            "mode": "local",
            "uptimeSeconds": round(time.time() - self.started, 3),
            "agent_service": agent_view,
            "browser": {"connected": bool(self.browsers),
                        "browser_id": bound if bound in self.browsers else None},
            "assets": assets_view,
        })

    async def _agent_service_view(self) -> dict[str, Any]:
        return agent_service_health(await self.agent_relay.service_health())

    async def _assets_view(self) -> dict[str, Any]:
        """§5.3: OLS is probed through the route the asset panel already uses, not a new one."""
        base = str(self.config.get("library_server_url") or "").rstrip("/")
        if not base:
            return {"reachable": False}
        try:
            client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HEALTH_PROBE_TIMEOUT_SECONDS))
            async with client:
                async with client.get(f"{base}/list", params={"type": "model"}) as response:
                    return {"reachable": response.status == 200}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return {"reachable": False}

    async def agent_health(self, request: web.Request) -> web.Response:
        """§5.3: GET /health -- the Agent service's own words, or a 502 that says they are missing."""
        return self._health_view(await self.agent_relay.service_health())

    async def agent_health_cached(self, request: web.Request) -> web.Response:
        """§5.3: GET /api/agent/health -- the same reading, kept for at most 5 seconds.

        The panel asks every 15 s, so the cache never hides a change a poll would have seen;
        what it stops is a restart-time flood of probes reaching Brain through this gateway.
        A failed reading is cached like any other, because "unreachable" is also something
        the service said about itself -- and an uncached one is exactly what a dying
        service cannot answer twice in a row.
        """
        cached = self.agent_health_probe
        age = None
        if cached is not None and time.monotonic() - self.agent_health_at <= AGENT_HEALTH_CACHE_SECONDS:
            age = round(time.monotonic() - self.agent_health_at, 3)
        else:
            cached = await self.agent_relay.service_health()
            self.agent_health_probe = cached
            self.agent_health_at = time.monotonic()
        return self._health_view(cached, age=age)

    @staticmethod
    def _health_view(probe: HealthProbe, *, age: float | None = None) -> web.Response:
        if probe.detail is not None:
            # 坑2 of this route: a 200 here would read as "healthy" to every viewer.
            response = web.json_response({"status": "unreachable", "detail": probe.detail}, status=502)
        else:
            response = web.Response(status=probe.status, body=probe.body, content_type="application/json")
        if age is not None:
            # A cached reading says how old it is whichever way the reading came out --
            # a viewer that sees "unreachable, 4 s old" knows the service is not newly down.
            response.headers["X-Agent-Health-Age"] = f"{age}"
        return response

    async def runtime_config(self, request: web.Request) -> web.Response:
        # features ride along: the old scene snapshot route is gone and this is
        # the surviving startup channel (§9.1).
        return web.json_response({**self.config, "features": self.features})

    async def session_state(self, request: web.Request) -> web.Response:
        """§5: GET /api/sessionState -- the phase and identity a refreshed browser reads back.

        The query `browser_id` is the caller saying who it is; the answer names the bound browser
        instead of trusting that claim, which is what lets the panel tell "this phase is mine"
        from "someone else holds the scene". Nine of these fields are the disk file and are never
        cached; `camera_candidates` is the tenth and is the opposite -- it is a browser's report
        about a document, so it lives in memory and empties when the scene moves (§9 item 3).
        """
        caller = request.query.get("browser_id") or request.query.get("browserID")
        # A persisted Gateway session can outlive Brain's active-session row (Brain restart,
        # another stale tab, or a previous runtime).  Reconcile on the authoritative read so the
        # UI does not remain in `browser=not_bound` until the user submits a task.  This is best
        # effort: session state remains readable while Brain is down, and the next task still
        # surfaces the dependency error instead of inventing a binding.
        recovery_required = bool(self.state.quarantine)
        if (caller and caller == self.state.browser_id and caller in self.browsers
                and self.state.session_phase == "setup"):
            try:
                await self._reconcile_brain_session(caller)
            except BrowserError as error:
                recovery_required = recovery_required or error.code == "session_reconciliation_required"
                LOGGER.warning("session-state Brain reconciliation deferred (%s): %s", error.code, error)
        return web.json_response({**self.state.session_view(),
                                  "recovery_required": recovery_required,
                                  "camera_candidates": self._camera_candidate_view()})

    async def recover_agent_session(self, request: web.Request) -> web.Response:
        body = await self._json_body(request, code="INVALID_PARAM")
        if body.get("browserID") != self.state.browser_id or body.get("sessionID") != self.state.session_id:
            raise BrowserError(409, "recovery_context_mismatch", "只能核验当前绑定会话，不能解锁其他会话")
        status, payload = await self.agent_relay.request("POST", "/resolveUncertain", json_body={
            "browserID": self.state.browser_id, "sessionID": self.state.session_id,
            "transactionID": body.get("transactionID"), "resolution": "resume"})
        return web.json_response(payload, status=status)

    async def allocate_object_id(self, request: web.Request) -> web.Response:
        """§6: GET /allocateObjectID -- the only id allocation path an Agent may use.

        The water mark is advanced before the answer is sent, so a caller that crashes between
        the two still cannot get these ids back, which is the point of "释放的 id 永不复用".
        """
        browser_id = request.query.get("browserID") or request.query.get("browser_id") or ""
        if not browser_id:
            raise BrowserError(400, "MISSING_PARAM", "browserID is required")
        if browser_id not in self.browsers:
            raise BrowserError(503, "NO_BROWSER_CONNECTED", f"browser {browser_id} is not connected")
        bound = self.state.browser_id
        if bound != browser_id:
            # §1.2 rule 4's own code: the browser that holds the session is a different one, so
            # this caller's session has to be rebuilt before it may allocate for that browser.
            raise BrowserError(409, "browser_new_session_required",
                               f"browser {browser_id} is not the bound browser {bound!r}")
        count = self._uint_query(request, "count", required=False)
        if count is None:
            count = 1
        if count < 1 or count > MAX_ALLOCATE_COUNT:
            raise BrowserError(400, "INVALID_PARAM",
                               f"count must be between 1 and {MAX_ALLOCATE_COUNT}, got {count}")
        try:
            ids = self.state.allocate_object_ids(count)
        except ObjectIDSpaceExhausted as error:
            raise BrowserError(409, "object_id_space_exhausted", str(error)) from error
        LOGGER.info("allocated %d object ids from %d for browser %s", count, ids[0], browser_id)
        return web.json_response({"object_ids": ids})

    async def sessions(self, request: web.Request) -> web.Response:
        """§4/§5: GET reads the binding view, POST creates, resumes or switches a session."""
        if request.method == "GET":
            # §9 item 3: the candidate list rides here too, because this is the route a caller reads
            # before it dares to switch -- and switching without a camera to switch to is a guess.
            return web.json_response({**self.state.session_record(),
                                      "camera_candidates": self._camera_candidate_view()})
        return await self.create_session(request)

    async def create_session(self, request: web.Request) -> web.Response:
        body = await self._json_body(request, code="INVALID_PARAM")
        async with self._session_transition_lock:
            return await self._create_session_under_lock(body)

    async def _create_session_under_lock(self, body: dict[str, Any]) -> web.Response:
        """§4: the only door back to `setup`, and the reason a lock cannot be walked around.

        The three gates below all answer *before* Brain is asked: a refusal that reached the Agent
        service would have left Brain holding a session record for a scene this gateway refuses to
        hand over. And when Brain does answer, its own `task_busy` still applies -- this gateway
        owns the phase and the locks, Brain owns its task queue.
        """
        browser_id = body.get("browserID")
        if browser_id is None or browser_id == "":
            raise BrowserError(400, "MISSING_PARAM", "browserID is required")
        if not isinstance(browser_id, str):
            raise BrowserError(400, "INVALID_PARAM",
                               f"browserID must be a string, got {type(browser_id).__name__}")
        session_id = body.get("sessionID")
        if session_id is not None and not isinstance(session_id, str):
            raise BrowserError(400, "INVALID_PARAM", "sessionID must be a string when it is given")
        camera_id = body.get("camera_id")
        if camera_id is not None and (isinstance(camera_id, bool) or not isinstance(camera_id, int)
                                      or not 0 <= camera_id <= MAX_UINT32):
            raise BrowserError(400, "web_uint32_required", f"camera_id must be a uint32, got {camera_id!r}")
        expected_scene = body.get("expectedScene")
        if expected_scene is not None and not isinstance(expected_scene, dict):
            raise BrowserError(400, "INVALID_PARAM", "expectedScene must be a world object or null")
        bound = self.state.browser_id
        if bound is not None and bound != browser_id and bound in self.browsers:
            raise BrowserError(409, "browser_new_session_required",
                               f"browser {bound!r} still holds the session; it has to disconnect first")
        held = "quarantine" if self.state.quarantine else ("lease" if self.state.lease else None)
        allow_active_task = body.get("allowActiveTask") is True
        if self.state.lease is not None and self.state.lease.get("commit_unconfirmed"):
            raise BrowserError(409, "task_busy", "a browser commit is still unconfirmed; session switching must wait")
        target_context = self._session_context(session_id)
        if target_context is not None and target_context.get("quarantine") is not None:
            raise BrowserError(409, "task_busy", "resolveUncertain is required before activating a quarantined session")
        if held == "quarantine" or (held == "lease" and not allow_active_task):
            # §8.3: 新建 /sessions、切换浏览器、重启服务都不是解锁证据 -- so none of them may look
            # like one to the caller. The refusal leaves both records exactly as they were.
            raise BrowserError(409, "task_busy",
                               f"the {held} record still holds the scene; only POST /resolveUncertain clears it")
        resuming = session_id is not None and session_id == self.state.session_id
        if resuming and camera_id is not None and self.state.camera_id is not None \
                and camera_id != self.state.camera_id:
            raise BrowserError(409, "browser_new_session_required",
                               f"session {session_id!r} is bound to camera {self.state.camera_id}, "
                               f"not {camera_id}; a different camera is a new session (§9)")
        # Camera choice is a Brain/Gateway concern, not a UI interaction.  If the caller does not
        # provide one, use the current browser report deterministically.  The old manual picker is
        # still accepted when a caller explicitly supplies camera_id, but it is no longer required.
        candidates = self._camera_candidate_view()
        if camera_id is None:
            if resuming:
                # A resumed session keeps its previously bound camera even when the browser has
                # not re-reported candidates yet (for example during a reconnect).
                merged_camera = self.state.camera_id if self.state.camera_id is not None else None
            elif candidates:
                merged_camera = None
            else:
                # A camera is optional at session creation. Brain may still bind the browser and
                # then return the real platform/profile error for a scene that cannot support a
                # camera-dependent task; the UI must not be trapped behind a manual picker.
                merged_camera = None
        else:
            merged_camera = camera_id
        transition_baseline = self.state.scene_identity
        expected_scene_changes_baseline = (expected_scene is not None and not resuming
                                           and scene_identity(expected_scene) != transition_baseline)
        if expected_scene_changes_baseline:
            # A fresh conversation can request a reset to the empty world while the browser still
            # reports the previous scene's camera candidates. Do not pin that new Brain session to
            # a camera that its expected first scene will not contain.
            merged_camera = None
            LOGGER.debug("new session expected scene differs from current scene; deferring camera binding")
        session_body = {"browserID": browser_id, "sessionID": session_id,
                        "camera_id": None if expected_scene_changes_baseline else camera_id,
                        "scene": expected_scene if expected_scene is not None else self.world_cache}
        if allow_active_task:
            session_body["allowActiveTask"] = True
        status, payload = await self.agent_relay.request("POST", "/sessions", json_body=session_body)
        if self.state.lease is not None and self.state.lease.get("commit_unconfirmed"):
            raise BrowserError(409, "task_busy", "a browser commit became unconfirmed during session creation")
        if (self.state.quarantine is not None or (self.state.lease is not None and not allow_active_task)
                or (target_context is not None and target_context.get("quarantine") is not None)):
            raise BrowserError(409, "task_busy", "the scene became held during session creation")
        if status >= 400:
            if not isinstance(payload, dict):
                raise BrowserError(502, "UPSTREAM_ERROR",
                                   agent_error_text(payload, f"Agent service answered /sessions with HTTP {status}"))
            # Brain's refusal is its own body, verbatim: `task_busy` and friends are its judgement
            # about its task queue, and rewriting them here would teach callers a second error table.
            return web.json_response(payload, status=status)
        if not isinstance(payload, dict) or not isinstance(payload.get("sessionID"), str) or not payload["sessionID"]:
            raise BrowserError(502, "UPSTREAM_ERROR",
                               f"Agent /sessions replied without a usable sessionID: "
                               f"{payload.get('sessionID') if isinstance(payload, dict) else payload!r}")
        returned = payload["sessionID"]
        merged_camera = payload.get("camera_id")
        self._session_transition_generation += 1
        previous_phase = self.state.session_phase
        previous_session = self.state.session_id
        # A user may switch conversations while an older Brain task is still running.  Move that
        # task's lease out of the single visible-browser slot before binding the new session; the
        # old task then continues against its own snapshot and cannot poison the new session with
        # a global 409/quarantine.
        if allow_active_task and previous_session and returned != previous_session:
            context = self._session_context(previous_session, create=True)
            if self.state.lease is not None:
                context["lease"] = self._copy_json(self.state.lease)
                context["world_snapshot"] = self._copy_json(self.state.lease.get("world_snapshot")
                                                            or self.world_cache)
                context["scene_identity"] = self._copy_json(self.state.lease.get("scene_identity"))
            elif context.get("world_snapshot") is None and isinstance(self.world_cache, dict):
                context["world_snapshot"] = self._copy_json(self.world_cache)
                context["scene_identity"] = scene_identity(self.world_cache)
            context["camera_id"] = self.state.camera_id
            if self.state.lease is not None:
                self.state.release_lease(keep_locked=False)
        self.state.bind_browser(browser_id)
        if returned != self.state.session_id:
            # New or switched: §4's only edge back to `setup`, and the camera comes along with it.
            self.state.start_session(returned, camera_id=merged_camera, camera_reason=payload.get("camera_selection_reason"))
            context = self._session_context(returned, create=True)
            if context.get("world_snapshot") is not None:
                self.state.record_scene(context["world_snapshot"])
                self._cache_world(context["world_snapshot"])
            if context.get("lease") is not None:
                self.state.acquire_lease(self._copy_json(context["lease"]))
            if context.get("lease") is not None or context.get("phase") == "agent_owned":
                self.state.set_phase("agent_owned")
        elif camera_id is not None and camera_id != self.state.camera_id:
            self.state.bind_camera(camera_id)
        # Even if Brain resumes the same durable session ID, the New conversation button creates
        # a distinct transcript row. Keep the previously selected row protected until that row is
        # active; the browser may report the reset world before it creates the transcript.
        if returned != previous_session or not resuming:
            returned_context = self._session_context(returned, create=True)
            self._explicit_session_sync_pending = {
                "session_id": returned,
                "baseline": transition_baseline,
                "expected": (scene_identity(expected_scene) if expected_scene is not None
                              else returned_context.get("scene_identity")),
                "camera_id": self.state.camera_id,
                "protected_conversation_id": self.active_conversation_id,
                "scene_report_received": False,
            }
        await self._announce_phase(previous_phase)
        context = self._session_context(returned)
        if context and isinstance(context.get("world_snapshot"), dict):
            # Tell the browser that a history/session switch has a concrete world to restore.
            # The frame is only an identity; the browser follows it with GET /getWorld and gets
            # the per-session snapshot below, so a late A commit is never replaced by B's live
            # pixels while A is being re-selected.
            await self.broadcast(self._scene_applied_frame(
                context.get("scene_identity") or scene_identity(context["world_snapshot"]),
                action="session_switch", source_request_id=None))
        LOGGER.info("session %s ready for browser %s camera=%s phase=%s",
                    returned, browser_id, self.state.camera_id, self.state.session_phase)
        # §5: the merge is this gateway's four fields laid over Brain's body -- Brain may echo
        # `session_phase`, but it does not own it (§4), and a caller that read a stale phase out of
        # that field would grey out the wrong panel. Everything else Brain said still travels.
        return web.json_response({**payload, "sessionID": returned,
                                  "session_phase": self.state.session_phase,
                                  "browserID": self.state.browser_id,
                                  "camera_id": self.state.camera_id})

    async def brain_capabilities(self, request: web.Request) -> web.Response:
        """§5.2: the eight names this gateway promises, answered from the binding on disk.

        `?browser_id=` names the caller and changes nothing: the capabilities are this process's
        own promise, and `browserID` in the answer is the bound browser -- the same rule
        `GET /api/sessionState` uses, so the two readouts cannot disagree about who holds the scene.
        """
        return web.json_response({"protocol": BRAIN_WEB2_PROTOCOL,
                                  "browserID": self.state.browser_id,
                                  "capabilities": list(BRAIN_WEB2_CAPABILITIES)})

    async def _announce_phase(self, previous_phase: str) -> bool:
        """§4/C5: broadcast `session.phase_changed` when the phase actually moved, never twice."""
        phase = self.state.session_phase
        if phase == previous_phase:
            return False
        await self.broadcast({"type": "session.phase_changed",
                              "payload": {"browser_id": self.state.browser_id, "session_phase": phase}})
        LOGGER.info("session phase %s -> %s", previous_phase, phase)
        return True

    async def conversations_list(self, request: web.Request) -> web.Response:
        # History rows stay light; the full scene snapshot is returned by the detail route when a
        # user actually activates a row.
        summaries = []
        for conversation in self.conversations.values():
            summaries.append({key: value for key, value in conversation.items()
                              if key not in {"messages", "sceneSnapshot"}})
        return web.json_response({"activeConversationId": self.active_conversation_id,
                                  "conversations": summaries})

    async def conversations_create(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        if request.content_length not in (None, 0):
            body = await self._json_body(request, code="INVALID_PARAM")
        conversation_id = f"local-{uuid.uuid4().hex[:8]}"
        session_id = body.get("sessionID")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise BrowserError(400, "INVALID_PARAM", "sessionID must be a non-empty string or null")
        scene_id = body.get("sceneID", "main")
        if scene_id is not None and not isinstance(scene_id, (str, int)):
            raise BrowserError(400, "INVALID_PARAM", "sceneID must be a string, integer or null")
        title = body.get("title", "Local conversation")
        if not isinstance(title, str) or not title.strip():
            raise BrowserError(400, "INVALID_PARAM", "title must be a non-empty string")
        scene_snapshot = body.get("scene")
        if scene_snapshot is not None and not isinstance(scene_snapshot, dict):
            raise BrowserError(400, "INVALID_PARAM", "scene must be a world object or null")
        if (session_id is not None and self.state.browser_id is not None
                and session_id != self.state.session_id):
            if self.state.session_id is None:
                raise BrowserError(409, "session_context_required",
                                   "the active Agent session is not ready for a conversation")
            LOGGER.info("remapping conversation creation from stale session %s to active session %s",
                        session_id, self.state.session_id)
            session_id = self.state.session_id
            if isinstance(self.world_cache, dict):
                scene_snapshot = self._copy_json(self.world_cache)
                scene_id = (self.state.scene_identity or {}).get("scene_id", scene_id)
            if title.startswith("场景会话 "):
                title = f"场景会话 {session_id[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        conversation = {"conversationId": conversation_id, "sessionID": session_id,
                        "target": "platform", "sceneBinding": {"sceneId": scene_id,
                        "simSessionId": session_id or "local"}, "presentationMode": "real-sync",
                        "createdAt": now, "updatedAt": now, "title": title.strip(), "messages": [],
                        "sceneSnapshot": json.loads(json.dumps(scene_snapshot)) if scene_snapshot is not None else None}
        self.conversations[conversation_id] = conversation
        if session_id:
            context = self._session_context(session_id, create=True)
            if scene_snapshot is not None:
                context["world_snapshot"] = self._copy_json(scene_snapshot)
                context["scene_identity"] = scene_identity(scene_snapshot)
        self.active_conversation_id = conversation_id
        marker = self._explicit_session_sync_pending
        marker_camera = marker.get("camera_id") if marker is not None else None
        marker_camera_valid = (marker_camera in self.camera_candidates if marker_camera is not None
                               else not self.camera_candidates)
        active_conversation = (self.conversations.get(self.active_conversation_id)
                               if self.active_conversation_id else None)
        if (marker is not None and marker.get("session_id") == self.state.session_id
                and session_id == self.state.session_id
                and (marker.get("scene_report_received")
                     or (marker.get("expected") is not None
                         and marker.get("expected") == self.state.scene_identity))
                and marker.get("protected_conversation_id") != self.active_conversation_id
                and active_conversation is not None
                and active_conversation.get("sessionID") == self.state.session_id
                and marker_camera_valid
                and not self._session_rebind_required):
            self._explicit_session_sync_pending = None
        return web.json_response(conversation, status=201)

    async def conversation(self, request: web.Request) -> web.Response:
        conversation_id = request.match_info["conversationId"]
        if request.method == "DELETE":
            self.conversations.pop(conversation_id, None)
            if self.active_conversation_id == conversation_id:
                self.active_conversation_id = next(reversed(self.conversations), None) if self.conversations else None
            return web.json_response({"ok": True})
        if request.method == "POST" and request.path.endswith("/activate"):
            conversation = self.conversations.get(conversation_id)
            if conversation is None: raise web.HTTPNotFound()
            await self._activate_conversation_session(conversation)
            return web.json_response(conversation)
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise web.HTTPNotFound()
        return web.json_response(conversation)

    async def _activate_conversation_session(self, conversation: dict[str, Any]) -> None:
        async with self._session_transition_lock:
            await self._activate_conversation_session_locked(conversation)
            self.active_conversation_id = conversation["conversationId"]

    async def _activate_conversation_session_locked(self, conversation: dict[str, Any]) -> None:
        """Switch the Gateway/Brain binding before the browser restores a history snapshot."""
        target = conversation.get("sessionID")
        if not isinstance(target, str) or not target:
            return
        if target == self.state.session_id:
            self._conversation_restore_session_id = target
            return
        # A transcript-only Gateway test (and a freshly opened page before browser.hello) has no
        # live Brain binding to switch. The browser can still select the row; its next world report
        # will establish the binding normally.
        if self.state.session_id is None or not self.state.browser_id:
            self._conversation_restore_session_id = target
            return
        browser_id = self.state.browser_id
        if not browser_id:
            raise BrowserError(409, "session_browser_not_active",
                               "no browser is bound to restore this conversation")
        target_context = self._session_context(target)
        if self.state.quarantine is not None:
            raise BrowserError(409, "task_busy", "resolveUncertain is required before switching a quarantined session")
        if self.state.lease is not None and self.state.lease.get("commit_unconfirmed"):
            raise BrowserError(409, "task_busy", "a browser commit is still unconfirmed; session switching must wait")
        # Do not reuse the camera from the currently displayed scene. Brain keeps the target
        # session's own binding when camera_id is omitted; the browser will report the restored
        # scene's candidates immediately afterwards.
        camera_id = None
        if target_context is not None and target_context.get("quarantine") is not None:
            # Recovery needs this session's evidence in the active slot. Brain refuses activation
            # of uncertain sessions, so restore locally while keeping the quarantine intact;
            # normal reconciliation may reactivate Brain only after resolveUncertain succeeds.
            status, payload = 200, {"sessionID": target, "camera_id": target_context.get("camera_id")}
        else:
            status, payload = await self.agent_relay.request(
                "POST", "/sessions",
                json_body={"browserID": browser_id, "sessionID": target, "camera_id": camera_id,
                           "allowActiveTask": True})
        if status >= 400:
            error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else payload
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise BrowserError(status or 502, str(code or "UPSTREAM_ERROR"),
                               str(message or f"Brain refused conversation session {target}"))
        if not isinstance(payload, dict) or not isinstance(payload.get("sessionID"), str):
            raise BrowserError(502, "UPSTREAM_ERROR", "Brain returned no sessionID while restoring conversation")
        returned = payload["sessionID"]
        if returned != target:
            raise BrowserError(409, "conversation_session_mismatch",
                               "Brain activated a different session than the selected conversation")
        if self.state.quarantine is not None:
            raise BrowserError(409, "task_busy", "resolveUncertain is required before switching a quarantined session")
        if self.state.lease is not None and self.state.lease.get("commit_unconfirmed"):
            raise BrowserError(409, "task_busy", "a browser commit became unconfirmed during history activation")
        previous_phase = self.state.session_phase
        transition_baseline = self.state.scene_identity
        previous_session = self.state.session_id
        if previous_session:
            previous_context = self._session_context(previous_session, create=True)
            previous_context["phase"] = previous_phase
            previous_context["camera_id"] = self.state.camera_id
            if self.state.lease is not None:
                previous_context["lease"] = self._copy_json(self.state.lease)
                previous_context["world_snapshot"] = self._copy_json(
                    self.state.lease.get("world_snapshot") or self.world_cache)
                previous_context["scene_identity"] = self._copy_json(self.state.lease.get("scene_identity"))
                self.state.release_lease(keep_locked=False)
            elif isinstance(self.world_cache, dict):
                previous_context["world_snapshot"] = self._copy_json(self.world_cache)
                previous_context["scene_identity"] = scene_identity(self.world_cache)
        accepted_camera = payload.get("camera_id") if isinstance(payload.get("camera_id"), int) else None
        self.state.bind_browser(browser_id)
        self.state.start_session(returned, camera_id=accepted_camera, camera_reason=payload.get("camera_selection_reason"))
        target_context = self._session_context(returned)
        if target_context is not None:
            if isinstance(target_context.get("world_snapshot"), dict):
                self.state.record_scene(target_context["world_snapshot"])
                self._cache_world(self._copy_json(target_context["world_snapshot"]))
            if target_context.get("lease") is not None:
                self.state.acquire_lease(self._copy_json(target_context["lease"]))
            if target_context.get("quarantine") is not None:
                self.state.raw["quarantine"] = self._copy_json(target_context["quarantine"])
                self.state.save()
        if target_context is not None and (target_context.get("phase") == "agent_owned"
                                           or target_context.get("lease") is not None
                                           or target_context.get("quarantine") is not None):
            self.state.set_phase("agent_owned")
        self._session_rebind_required = False
        self._conversation_restore_session_id = returned
        self._session_transition_generation += 1
        self._explicit_session_sync_pending = {
            "session_id": returned,
            "baseline": transition_baseline,
            "expected": target_context.get("scene_identity") if target_context is not None else None,
            "camera_id": accepted_camera,
            "protected_conversation_id": self.active_conversation_id,
            "scene_report_received": False,
        }
        await self._announce_phase(previous_phase)

    async def conversation_messages(self, request: web.Request) -> web.Response:
        """Append one browser transcript message to the in-process conversation journal."""
        conversation_id = request.match_info["conversationId"]
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise web.HTTPNotFound()
        body = await self._json_body(request, code="INVALID_PARAM")
        role = body.get("role")
        content = body.get("content")
        turn_id = body.get("turnId")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            raise BrowserError(400, "INVALID_PARAM", "role and non-empty content are required")
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = uuid.uuid4().hex
        existing = next((item for item in conversation["messages"]
                         if item["role"] == role and item["turnId"] == turn_id), None)
        if existing is not None:
            return web.json_response(conversation)
        now = datetime.now(timezone.utc).isoformat()
        conversation["messages"].append({"role": role, "content": content,
                                          "turnId": turn_id, "createdAt": now})
        conversation["updatedAt"] = now
        return web.json_response(conversation)

    async def jobs(self, request: web.Request) -> web.Response:
        """§5.15: GET lists the tasks this gateway knows, POST starts a new one."""
        if request.method == "GET":
            conversation_id = request.query.get("conversationId")
            if conversation_id is not None and not conversation_id:
                raise BrowserError(400, "INVALID_PARAM", "conversationId must be a non-empty string")
            jobs = (job for job in self.jobs_state.values()
                    if conversation_id is None or job.get("conversation_id") == conversation_id)
            return web.json_response({"jobs": [self._agent_job_view(job) for job in jobs]})
        if not self.agent_relay.base_url:
            # §5.15 lists this refusal first because it is a fact about the deployment, not about
            # the body: a build with no Agent service says so before it reads a 现场 for a task
            # that could never be handed to anyone.
            raise BrowserError(501, "AGENT_UNAVAILABLE", AGENT_DISABLED_MESSAGE)
        body = await self._json_body(request, code="INVALID_PARAM")
        command = body.get("command")
        if not isinstance(command, str) or not command.strip():
            raise BrowserError(400, "INVALID_PARAM", "command must be a non-empty string")
        scene = body.get("scene")
        if not isinstance(scene, dict):
            raise BrowserError(400, "MISSING_PARAM",
                               f"scene must be the world JSON object the command acts on, got {type(scene).__name__}")
        explicit_conversation_id = "conversationId" in body
        requested_conversation_id = body.get("conversationId", self.active_conversation_id)
        if (requested_conversation_id is not None
                and (not isinstance(requested_conversation_id, str) or not requested_conversation_id)):
            raise BrowserError(400, "INVALID_PARAM", "conversationId must be a non-empty string")
        client_request_id = self._client_request_id(body.get("clientRequestID"))
        requested_session = body.get("sessionID")
        if "sessionID" in body and (not isinstance(requested_session, str) or not requested_session):
            raise BrowserError(400, "INVALID_PARAM", "sessionID must be a non-empty string")
        if requested_session is not None and requested_session != self.state.session_id:
            raise BrowserError(409, "conversation_session_mismatch", "the submitted session is no longer active")
        # §7.1: one clientRequestID names one submission. The browser generates it and reuses it
        # across retries, so a resend of the same payload owes back the task it already started, and
        # a same-id-different-content resend is a bug on the sender's side that `request_id_reused`
        # says out loud. The comparison is over the **body**, never over the live read: the 现场 is
        # allowed to move between two retries, and folding it in would turn a legitimate retry into
        # a conflict over a panel edit the user made in between.
        key = (self.state.session_id, client_request_id)
        payload_digest = scene_digest({"command": command.strip(), "scene": scene})
        seen = self.agent_requests.get(key)
        if seen is not None:
            if seen["payload_digest"] != payload_digest:
                raise BrowserError(409, "request_id_reused",
                                   f"clientRequestID {client_request_id!r} already started task "
                                   f"{seen['job_id']} on a different payload; a new submission needs a new id")
            LOGGER.info("submission %s replayed as task %s", client_request_id, seen["job_id"])
            return web.json_response(self._agent_job_view(self.jobs_state[seen["job_id"]]))
        # Both refusals below are raised before the browser is asked anything, so a rejected
        # submission neither reads the 现场 nor reaches Brain nor leaves a job behind.
        running = next((job for job in self.jobs_state.values()
                        if not job["terminal"] and job.get("session_id") == self.state.session_id), None)
        if running is not None:
            raise BrowserError(409, "task_busy",
                               f"task {running['task_id']} has not reported terminal yet; one session "
                               f"holds one running task (§7.1)")
        browser_id = self.state.browser_id
        if browser_id is None:
            raise BrowserError(409, "session_context_required",
                               "no browser is bound to this session, so there is no 现场 to read for the task")
        # Brain owns the durable session table.  A Gateway restart, an old test client, or a
        # stale browser can leave the persisted Gateway id present but no longer active in Brain;
        # forwarding that id would surface the misleading `session_browser_not_active` for an
        # otherwise valid browser (especially visible on an empty scene).  Reconcile the binding
        # before the live-world read; invalid scene/platform errors are then reported as their
        # actual semantic code instead of as a session transport error.
        await self._reconcile_brain_session(browser_id)
        submission_session = self.state.session_id
        if requested_session is not None and requested_session != submission_session:
            raise BrowserError(409, "conversation_session_mismatch", "the submitted session changed during reconciliation")
        # §14.6 rule 1: the scene, the four-tuple and the digest all come out of THIS one read.
        # Neither the body's copy nor world_cache may stand in -- the user can add an object with the
        # panel after pressing send, and a cached document is the last thing that was reported.
        read = await self._read_live_world(browser_id)
        if self.state.session_id != submission_session:
            raise BrowserError(409, "conversation_session_mismatch", "the session changed while reading the scene")
        context = TaskContext(scene_digest=str(read.identity["sceneDigest"]),
                              scene_id=read.identity["scene_id"],
                              scene_version=read.identity["scene_version"],
                              scene_sub_version=read.identity["scene_sub_version"],
                              session_id=self.state.session_id,
                              client_request_id=client_request_id,
                              browser_id=browser_id)
        # §5.15: a failed relay stores no task -- without a taskID there is nothing to poll.
        previous_phase = self.state.session_phase
        requested_conversation = (self.conversations.get(requested_conversation_id)
                                  if requested_conversation_id else None)
        if (requested_conversation is not None
                and requested_conversation.get("sessionID") == context.session_id):
            conversation_id = requested_conversation_id
        elif explicit_conversation_id and requested_conversation_id is not None:
            raise BrowserError(409, "conversation_session_mismatch",
                               "the selected conversation does not belong to the active Agent session")
        else:
            # A stale active row must not steal a terminal reply.  Keep the newest matching row as
            # a compatibility fallback only for older clients that omit conversationId.
            conversation_id = next((key for key in reversed(self.conversations)
                                    if self.conversations[key].get("sessionID") == context.session_id), None)
        task_id, agent_state, state_detail = await self.agent_relay.start_task(command.strip(),
                                                                             read.document, context)
        # §4: the phase moves when the task is *accepted*, not when the user clicks send -- and it
        # moves once. A second task in the same session reads `agent_owned` already and rebroadcasts
        # nothing, which is why the ② class entries grey out on the first acceptance and stay grey.
        if self.state.session_id == context.session_id:
            self.state.set_phase("agent_owned")
            await self._announce_phase(previous_phase)
        session_context = self._session_context(context.session_id, create=True)
        if session_context is not None:
            session_context["phase"] = "agent_owned"
        job: dict[str, Any] = {
            "id": str(task_id),
            "task_id": task_id,
            "session_id": context.session_id,
            "conversation_id": conversation_id,
            "command": command.strip(),
            "agent_state": agent_state,
            # §7.4 rule 8: a new job is never settled -- `start_task` drops an upstream
            # `terminal:true` on the spot, and the first poll is what decides.
            "terminal": False,
            "state_detail": state_detail,
            "outcome": None,
            "reply": None,
            "plan": None,
            "metrics": None,
            "artifacts": [],
            "commit_receipts": [],
            "message": agent_state,
            "failure_reason": None,
            "created_at": time.time(),
            "updated_at": time.time(),
            "poll_errors": 0,
        }
        self.jobs_state[job["id"]] = job
        self.agent_requests[key] = {"job_id": job["id"], "payload_digest": payload_digest}
        await self.broadcast({"type": "agent.job_updated", "payload": self._agent_job_view(job)})
        return web.json_response(self._agent_job_view(job))

    async def _reconcile_brain_session(self, browser_id: str) -> None:
        async with self._session_transition_lock:
            await self._reconcile_brain_session_locked(browser_id)

    async def _reconcile_brain_session_locked(self, browser_id: str) -> None:
        """Make the Gateway's persisted session the active Brain session for this browser.

        This is deliberately limited to setup/no-lease submissions.  An agent-owned session is
        already guarded by the task/lease protocol and must not be silently switched underneath a
        running task.  A missing Brain row gets a fresh session; an uncertain row remains a hard
        recovery error and is never guessed around.
        """
        if self.state.session_phase == "agent_owned" or self.state.scene_held:
            return
        expected_browser = self.state.browser_id
        expected_session = self.state.session_id
        expected_scene = self.state.scene_identity
        candidates = self._camera_candidate_view()
        camera_id = None
        session_id = self.state.session_id
        if session_id is None:
            # A freshly bound browser has no session row yet.  Create it here so opening the page
            # (and an empty-scene command) never requires a manual camera picker or a hidden
            # "new session" click before Brain can accept the first task.
            try:
                status, payload = await self.agent_relay.request(
                    "POST", "/sessions", json_body={"browserID": browser_id, "camera_id": None, "scene": self.world_cache})
            except BrowserError as error:
                # A legacy/test relay may not expose /sessions.  Keep the old submission path
                # usable there; a real brain-web/2 service will answer JSON and bind here.
                LOGGER.warning("automatic session creation deferred (%s): %s", error.code, error)
                return
            if (self.state.browser_id != expected_browser
                    or self.state.session_id != expected_session
                    or self.state.scene_identity != expected_scene
                    or self.state.scene_held or self.state.session_phase == "agent_owned"):
                LOGGER.info("discarding stale fresh-session reconciliation for %s", browser_id)
                return
            if status < 400 and isinstance(payload, dict) and isinstance(payload.get("sessionID"), str):
                self.state.bind_browser(browser_id)
                self.state.start_session(payload["sessionID"], camera_id=payload.get("camera_id"), camera_reason=payload.get("camera_selection_reason"))
                self._session_rebind_required = False
                self._session_transition_generation += 1
                self._sync_active_conversation_binding()
                return
            return
        if session_id is not None:
            session_body = {"browserID": browser_id, "sessionID": session_id, "camera_id": camera_id}
            if any(not job["terminal"] and job.get("session_id") != session_id
                   for job in self.jobs_state.values()):
                session_body["allowActiveTask"] = True
            status, payload = await self.agent_relay.request("POST", "/sessions", json_body=session_body)
            if (self.state.browser_id != expected_browser
                    or self.state.session_id != expected_session
                    or self.state.scene_identity != expected_scene
                    or self.state.scene_held or self.state.session_phase == "agent_owned"):
                LOGGER.info("discarding stale existing-session reconciliation for %s", browser_id)
                return
            if status < 400 and isinstance(payload, dict) and isinstance(payload.get("sessionID"), str):
                returned = payload["sessionID"]
                self.state.bind_browser(browser_id)
                brain_camera = payload.get("camera_id")
                accepted_camera = brain_camera if brain_camera is not None else camera_id
                if returned != self.state.session_id:
                    self.state.start_session(returned, camera_id=accepted_camera, camera_reason=payload.get("camera_selection_reason"))
                elif accepted_camera is not None:
                    self.state.bind_camera(accepted_camera, camera_reason=payload.get("camera_selection_reason"))
                else:
                    self.state.clear_camera()
                self._session_rebind_required = False
                if returned != session_id:
                    self._session_transition_generation += 1
                    self._sync_active_conversation_binding()
                return
            if isinstance(payload, dict):
                error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
                code = error.get("code") if isinstance(error, dict) else None
                if code == "session_reconciliation_required":
                    raise BrowserError(status or 409, code, str(error.get("message") or code))
                if code not in {"session_not_found", "session_browser_not_active"}:
                    raise BrowserError(status or 502, str(code or "UPSTREAM_ERROR"),
                                       str(error.get("message") or "Brain session rebind failed"))
        if (self.state.browser_id != expected_browser
                or self.state.session_id != expected_session
                or self.state.scene_identity != expected_scene
                or self.state.scene_held or self.state.session_phase == "agent_owned"):
            LOGGER.info("discarding stale fallback reconciliation for %s", browser_id)
            return
        status, payload = await self.agent_relay.request(
            "POST", "/sessions", json_body={"browserID": browser_id, "camera_id": None, "scene": self.world_cache})
        if (self.state.browser_id != expected_browser
                or self.state.session_id != expected_session
                or self.state.scene_identity != expected_scene
                or self.state.scene_held or self.state.session_phase == "agent_owned"):
            LOGGER.info("discarding stale fallback reconciliation response for %s", browser_id)
            return
        if status >= 400 or not isinstance(payload, dict) or not isinstance(payload.get("sessionID"), str):
            detail = agent_error_text(payload, f"Brain session rebind failed with HTTP {status}")
            if isinstance(payload, dict):
                error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
                code = error.get("code") if isinstance(error, dict) else None
                if isinstance(code, str) and code:
                    raise BrowserError(status or 502, code, str(error.get("message") or detail))
            raise BrowserError(502, "UPSTREAM_ERROR", detail)
        returned = payload["sessionID"]
        previous_phase = self.state.session_phase
        self.state.bind_browser(browser_id)
        brain_camera = payload.get("camera_id")
        accepted_camera = brain_camera if brain_camera in candidates else camera_id
        self.state.start_session(returned, camera_id=accepted_camera)
        self._session_rebind_required = False
        self._session_transition_generation += 1
        self._sync_active_conversation_binding()
        await self._announce_phase(previous_phase)
        LOGGER.info("reconciled Brain session %s to browser %s before task submission", returned, browser_id)

    async def task_artifact(self, request: web.Request) -> web.Response:
        """§5.15 (E2): the four whitelisted artifacts, proxied -- never an arbitrary path."""
        raw_task_id = request.match_info["taskID"]
        if not self._is_uint_text(raw_task_id) or int(raw_task_id) > MAX_UINT32:
            raise BrowserError(400, "INVALID_PARAM", f"taskID must be a uint32, got {raw_task_id!r}")
        name = request.match_info["name"]
        if name not in AGENT_ARTIFACT_NAMES:
            # This one comparison is the entire path check: a name carrying a separator is not one of
            # the four, so `..%2Fscene_config` is refused here and never enters the URL we build.
            raise BrowserError(400, "INVALID_PARAM",
                               f"name must be one of {', '.join(AGENT_ARTIFACT_NAMES)}, got {name!r}")
        status, body, content_type = await self.agent_relay.artifact(int(raw_task_id), name)
        # Brain's own status, bytes and content type, unchanged (§5.9's `/health` passthrough rule).
        return web.Response(status=status, body=body,
                            headers={"Content-Type": content_type or "application/octet-stream"})

    async def job(self, request: web.Request) -> web.Response:
        """§5.15: one task by id; the id is the Agent's taskID as text."""
        job = self.jobs_state.get(request.match_info["jobId"])
        if job is None:
            raise BrowserError(404, "TASK_NOT_FOUND", f"no Agent task {request.match_info['jobId']!r} in this gateway")
        return web.json_response(self._agent_job_view(job))

    @staticmethod
    def _agent_job_view(job: dict[str, Any]) -> dict[str, Any]:
        """§5.15: exactly the documented keys, so `poll_errors` and never the scene travel."""
        return {key: job[key] for key in AGENT_JOB_FIELDS}

    async def poll_agent_tasks_once(self) -> int:
        """§5.15 step 3/5: one round over every task still running; returns how many were asked."""
        asked = 0
        for job in list(self.jobs_state.values()):
            if job["terminal"] is True:
                continue
            asked += 1
            try:
                await self._poll_one_agent_task(job)
            except BrowserError as error:
                # §7.4 rule 6: any transport failure in the round counts towards the limit --
                # including one on the terminal reads -- and the loop survives it.
                await self._note_agent_poll_error(job, error)
        return asked

    async def _poll_one_agent_task(self, job: dict[str, Any]) -> None:
        """§5.15/§7.4: one /queryTaskState for one task, and the terminal reads after it."""
        task_id = int(job["task_id"])
        status, state_text, terminal, state_detail = await self.agent_relay.task_state(task_id)
        if status == 404:
            # The Agent service restarted and no longer knows this task (§7.4 rule 5).
            # "Task unknown" is a fact about the service, not one of its state words, and
            # nothing further can be read from it.
            await self._settle_agent_job(job, agent_state=state_text, terminal=True, outcome="failed",
                                         message=state_text, failure_reason=state_text)
            return
        if status >= 400:
            await self._note_agent_poll_error(job, BrowserError(502, "UPSTREAM_ERROR", state_text))
            return
        job["poll_errors"] = 0
        if not terminal:
            if state_text == job["agent_state"] and state_detail == job["state_detail"]:
                return
            await self._settle_agent_job(job, agent_state=state_text, terminal=False,
                                         state_detail=state_detail, message=state_text)
            return
        # §7.4: the task says it ended, so read its result and its reason once each and stop
        # asking. An unreadable result settles the job with no `outcome`.
        result = await self.agent_relay.task_result(task_id) or {}
        outcome = agent_outcome(result.get("outcome"))
        reason = await self.agent_relay.failure_reason(task_id)
        plan, metrics = result.get("plan"), result.get("metrics")
        await self._settle_agent_job(job, agent_state=state_text, terminal=True, state_detail=state_detail,
                                     outcome=outcome, reply=agent_reply(result.get("reply")),
                                     plan=plan if isinstance(plan, dict) else None,
                                     metrics=metrics if isinstance(metrics, dict) else None,
                                     artifacts=agent_artifact_names(result.get("artifacts")),
                                     commit_receipts=self.job_commit_receipts(result.get("commitReceipts")),
                                     message=reason or state_text, failure_reason=reason)

    async def _note_agent_poll_error(self, job: dict[str, Any], error: BrowserError) -> None:
        """§5.15: a transport hiccup keeps the task running; the limit finalizes it."""
        job["poll_errors"] = int(job["poll_errors"]) + 1
        LOGGER.warning("Agent poll for task %s failed (%d in a row): %s", job["task_id"], job["poll_errors"], error.code)
        if job["poll_errors"] >= AGENT_POLL_ERROR_LIMIT:
            # §7.4 rule 6: five in a row (~25s) is the only transport-side ending, and the
            # upstream text is stored as the reason because we have no state word for it.
            await self._settle_agent_job(job, agent_state="", terminal=True, outcome="failed",
                                         message=f"{error.code}: {error.message}",
                                         failure_reason=f"{error.code}: {error.message}")

    async def _settle_agent_job(self, job: dict[str, Any], *, agent_state: str, terminal: bool, message: str,
                                state_detail: str | None = None, outcome: str | None = None,
                                reply: str | None = None, plan: dict[str, Any] | None = None,
                                metrics: dict[str, Any] | None = None, artifacts: list[str] | None = None,
                                commit_receipts: list[dict[str, Any]] | None = None,
                                failure_reason: str | None = None) -> None:
        job["agent_state"] = agent_state
        job["terminal"] = terminal
        job["state_detail"] = state_detail
        job["outcome"] = outcome
        job["reply"] = reply
        job["plan"] = plan
        job["metrics"] = metrics
        job["artifacts"] = artifacts or []
        job["commit_receipts"] = commit_receipts or []
        job["message"] = message
        job["failure_reason"] = failure_reason
        job["updated_at"] = time.time()
        if terminal:
            session_context = self._session_context(job.get("session_id"))
            if (session_context is not None and session_context.get("lease") is None
                    and session_context.get("quarantine") is None):
                session_context["phase"] = "setup"
            conversation = self.conversations.get(job.get("conversation_id"))
            if conversation is None and job.get("session_id") is not None:
                conversation = next((item for item in self.conversations.values()
                                     if item.get("sessionID") == job["session_id"]), None)
            if conversation is not None:
                # The task id is also the browser's assistant turnId. Journal before broadcast
                # so completion while the tab is closed is restored exactly once on reopening.
                text = reply or failure_reason or state_detail or agent_state or message
                if text:
                    now = datetime.now(timezone.utc).isoformat()
                    entry = next((item for item in conversation["messages"]
                                  if item["role"] == "assistant" and item["turnId"] == job["id"]), None)
                    if entry is None:
                        conversation["messages"].append({"role": "assistant", "content": text,
                                                         "turnId": job["id"], "createdAt": now})
                    else:
                        entry["content"] = text
                    conversation["updatedAt"] = now
        # A terminal Agent result means the Brain has released its browser lease.  Return the
        # Gateway UI to setup as well so semantic failures (including clarification and grounding
        # errors) do not leave the asset panel permanently disabled.  Never do this while a lease
        # or quarantine is still present: an uncertain commit must remain explicitly recoverable.
        if (terminal and job.get("session_id") == self.state.session_id
                and self.state.session_phase == "agent_owned" and not self.state.scene_held):
            previous_phase = self.state.session_phase
            self.state.set_phase("setup")
            await self._announce_phase(previous_phase)
        await self.broadcast({"type": "agent.job_updated", "payload": self._agent_job_view(job)})

    async def start_agent_poller(self, app: web.Application | None = None) -> None:
        """§5.15: the every-5-second round. Started with the app, never from a handler."""
        if self.agent_poller is None:
            self.agent_poller = asyncio.create_task(self._agent_poll_loop())

    async def stop_agent_poller(self, app: web.Application | None = None) -> None:
        poller, self.agent_poller = self.agent_poller, None
        if poller is None:
            return
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller

    async def _agent_poll_loop(self) -> None:
        interval = max(1.0, float(self.config["agent_poll_interval_seconds"]))
        while True:
            await asyncio.sleep(interval)
            if not self.agent_relay.base_url:
                continue
            for browser_id in list(self.profiles):
                token = self._profile_var.set(browser_id)
                try:
                    await self.poll_agent_tasks_once()
                except Exception:  # noqa: BLE001 - one bad round must not kill the poller (坑 10)
                    LOGGER.exception("Agent poll round failed for browser profile %r", browser_id)
                finally:
                    self._profile_var.reset(token)

    async def events(self, request: web.Request) -> web.WebSocketResponse:
        # max_msg_size=0 is required: a 100 MB state_sync/TRAJ frame must not be dropped (§5.3, §11.1, 坑1).
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await socket.prepare(request)
        self.clients.add(socket)
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    browser_id = self.socket_browser_ids.get(socket)
                    if browser_id is None:
                        with contextlib.suppress(ValueError, TypeError):
                            parsed = json.loads(message.data)
                            if isinstance(parsed, dict) and parsed.get("type") == "browser.hello":
                                candidate = parsed.get("browser_id")
                                if isinstance(candidate, str) and candidate:
                                    browser_id = candidate
                                    # Remove a closed/replaced socket before the test/client side
                                    # observes the new hello. Otherwise a reconnect can see the
                                    # old map entry and send its first request to that stale socket.
                                    if self.browsers.get(candidate) is not None and self.browsers.get(candidate) is not socket:
                                        self.browsers.pop(candidate, None)
                        if browser_id is None:
                            LOGGER.warning("browser sent %s before browser.hello; ignored", message.type)
                            continue
                    token = self._profile_var.set(browser_id)
                    try:
                        await self._handle_browser_message(socket, message.data)
                    finally:
                        self._profile_var.reset(token)
                elif message.type == WSMsgType.BINARY:
                    # §5.3 pairs binary frames gateway -> browser only.
                    LOGGER.warning("unexpected binary frame from a browser; ignored")
        finally:
            self.clients.discard(socket)
            browser_id = self.socket_browser_ids.pop(socket, None)
            if browser_id is not None and self.browsers.get(browser_id) is socket:
                self.browsers.pop(browser_id)
        return socket

    async def _handle_browser_message(self, socket: web.WebSocketResponse, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            LOGGER.warning("browser sent a non-JSON text frame; ignored")
            return
        if not isinstance(message, dict):
            LOGGER.warning("browser sent a JSON %s; ignored", type(message).__name__)
            return
        kind = message.get("type")
        if kind == "browser.hello":
            self._register_browser(socket, message)
        elif kind == "browser.response":
            self._apply_browser_response(message)
        elif kind == "browser.state_sync":
            await self.apply_state_sync(message, self._browser_id_for(socket))
        else:
            LOGGER.warning("unknown browser message type %r; ignored", kind)

    def _browser_id_for(self, socket: web.WebSocketResponse) -> str | None:
        mapped = self.socket_browser_ids.get(socket)
        if mapped is not None:
            return mapped
        for key, value in self.browsers.items():
            if value is socket:
                return key
        return None

    def _register_browser(self, socket: web.WebSocketResponse, message: dict[str, Any]) -> None:
        browser_id = message.get("browser_id")
        if not isinstance(browser_id, str) or not browser_id:
            LOGGER.warning("browser.hello without browser_id; socket stays unregistered")
            return
        existing = self.socket_browser_ids.get(socket)
        if existing is not None and existing != browser_id:
            LOGGER.warning("browser.hello attempted to change identity from %s to %s", existing, browser_id)
            return
        self._ensure_profile(browser_id)
        token = self._profile_var.set(browser_id)
        try:
            self._register_browser_for_profile(socket, browser_id)
        finally:
            self._profile_var.reset(token)

    def _register_browser_for_profile(self, socket: web.WebSocketResponse, browser_id: str) -> None:
        bound = self.state.browser_id
        # §5.1: the binding is on disk, so a refresh re-claims the same session and a second tab
        # that arrives while the bound browser is connected stays a viewer. A different id may
        # take over only once the bound one is not connected -- otherwise a closed tab would
        # lock the scene out of ever being written to again.
        if bound is None or bound == browser_id or bound not in self.browsers:
            if self.state.bind_browser(browser_id):
                if bound is not None and bound != browser_id:
                    self._session_rebind_required = True
                LOGGER.info("browser bound id=%s (persisted to %s)", browser_id, self.state.path)
        self.socket_browser_ids[socket] = browser_id
        # The newest connection is the active writer for this profile. All sockets still carry
        # the same profile id and receive broadcasts through ``socket_browser_ids``; this keeps
        # refresh/reconnect behaviour compatible while preventing any other profile from seeing it.
        self.browsers[browser_id] = socket
        LOGGER.debug("browser registered id=%s", browser_id)

    def _apply_browser_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            LOGGER.warning("browser.response without request_id; dropped")
            return
        if message.get("ok") is True:
            payload = message.get("payload")
            settled = self.pending.resolve(request_id, payload if isinstance(payload, dict) else {})
        else:
            error = message.get("error")
            error = error if isinstance(error, dict) else {}
            code = str(error.get("code", "UPSTREAM_RENDER_FAILED"))
            settled = self.pending.reject(
                request_id,
                BrowserError(BROWSER_ERROR_STATUS.get(code, 502), code, str(error.get("message", "browser reported a failure")), request_id=request_id),
            )
        if not settled:
            # §5.2: one request has one target and settles once, so a second response for the
            # same id -- a duplicate, or a late one after a 504 -- is the browser's, not ours to use.
            LOGGER.warning("browser.response for %s is stale or unknown; dropped", request_id)

    async def apply_state_sync(self, message: dict[str, Any], browser_id: str | None = None) -> None:
        if self._state_sync_lock.locked():
            LOGGER.debug("queueing browser.state_sync behind an in-flight scene report")
        async with self._state_sync_lock:
            await self._apply_state_sync(message, browser_id)

    async def _apply_state_sync(self, message: dict[str, Any], browser_id: str | None = None) -> None:
        """§5.8: cache the world a browser just loaded and announce it to the other browsers."""
        payload = message.get("payload")
        world = payload.get("world") if isinstance(payload, dict) else None
        if not isinstance(world, dict):
            LOGGER.warning("browser.state_sync without a world object; ignored")
            return
        # Q147: the report's own number, kept exactly as it came. A browser that has not learned to
        # number yet reports `None`, which is what every frame looked like before this key existed.
        reported_seq = payload.get("report_seq") if isinstance(payload, dict) else None
        report_seq = (reported_seq if isinstance(reported_seq, int) and not isinstance(reported_seq, bool)
                      else None)
        # §1.2 and §4's second layer: the persisted phase and any open claim on the scene decide
        # whether this document may be adopted, and the four-tuple plus its digest is the whole
        # judgement. The gateway still never parses the scene -- it compares identities it
        # computed itself (§3).
        incoming = scene_identity(world)
        previous_identity = self.state.scene_identity
        # §1.2/§4: a different `scene_id` is a switch and is always adopted (the two scenes'
        # versions are per-scene counters); a same-scene report is judged by its dictionary order.
        # The Agent's lock is enforced where it belongs -- a late write under an open lease is
        # still rejected by that lease's fingerprint, and `_ensure_automatic_session` refuses to
        # open a session while a lease or `agent_owned` stands -- never by refusing the report.
        verdict = adoption_verdict(self.state.session_phase, self.state.scene_identity, incoming)
        if verdict not in ("adopt", "idempotent"):
            # §10: a refused report is not adopted, not cached and not announced. The frame table
            # (§14) has no "rejected" frame and inventing one would break its closure, so the
            # browser finds its own document out of date through GET /api/sessionState (S4).
            LOGGER.warning("browser.state_sync refused (%s): reported %s against persisted %s",
                           verdict, incoming, self.state.scene_identity)
            return
        self._cache_world(world)
        # §9 item 3: the list is the browser's, the pairing is ours -- it travels with the
        # four-tuple it was read out of, which is what lets the readout refuse a stale one.
        self.camera_candidates = camera_candidate_ids(payload.get("camera_object_ids"))
        self.camera_candidates_scene = incoming
        # A browser report is authoritative for the current document.  If the new document has
        # no camera (or no longer contains the previously bound camera), discard the old binding
        # even when the four-tuple happens to be unchanged after a Gateway restart.  Otherwise an
        # empty scene can display and forward a stale camera id from the previous scene.
        if self.state.camera_id is not None and self.state.camera_id not in self.camera_candidates:
            self.state.clear_camera()
        # §6: adopting a reported scene is one of the three events that push the water mark,
        # which is what stops a locally allocated id from being handed out again after a restart.
        identity = self.state.record_scene(world)
        scene_changed = previous_identity != identity
        if scene_changed:
            # A camera id is scoped to the previous scene; never carry it into a new document.
            self.state.clear_camera()
        version = world.get("scene_version")
        if isinstance(version, int) and version >= 0:
            self.scene_version = version
        # A new scene (or a new browser binding) cannot safely continue with the old Brain
        # session.  Once the browser has reported its camera candidates, create a fresh session
        # automatically; no UI camera picker or manual recovery click is needed.
        restoring = (self._conversation_restore_session_id is not None
                     and self.state.session_id == self._conversation_restore_session_id)
        marker = self._explicit_session_sync_pending
        marker_for_session = (marker is not None and marker.get("session_id") == self.state.session_id)
        marker_baseline = marker.get("baseline") if marker_for_session else None
        marker_expected = marker.get("expected") if marker_for_session else None
        marker_camera = marker.get("camera_id") if marker_for_session else None
        marker_camera_valid = (marker_camera in self.camera_candidates if marker_camera is not None
                               else not self.camera_candidates)
        camera_rebind_pending = marker_for_session and not marker_camera_valid
        # With no target snapshot yet, only the transition baseline can be identified as stale;
        # the first different report is the scene the user picked for this session. When the
        # target has a known scene, a report equal to that scene is accepted even if it equals the
        # baseline (for example, resuming a session on the already-visible scene).
        delayed_old_sync = (marker_for_session and marker_baseline == incoming
                            and marker_expected != incoming)
        if marker_for_session and not delayed_old_sync:
            marker["scene_report_received"] = True
        camera_rebind_required = (bool(self.camera_candidates) and self.state.session_id is not None
                                  and self.state.camera_id is None and not delayed_old_sync)
        if camera_rebind_required:
            self._session_rebind_required = True
            LOGGER.debug("camera-less session received current-scene candidates; requesting rebind")
        explicit_sync_pending = marker_for_session and not delayed_old_sync and marker_camera_valid
        if delayed_old_sync:
            LOGGER.debug("preserving explicit session transition after baseline scene report")
        if marker_for_session and not delayed_old_sync and not marker_camera_valid:
            # Brain pins a session to its camera. A selected scene that no longer contains that
            # camera needs a fresh binding before the first task, even though this scene followed
            # an explicit session creation.
            self._session_rebind_required = True
            LOGGER.debug("explicit session camera is absent from the selected scene; rebinding")
        elif marker_for_session and not delayed_old_sync and marker_camera is not None:
            # Scene changes clear the Gateway camera as a general safety rule. This is the
            # exception: the explicit session's pinned camera is present in the incoming scene.
            self.state.bind_camera(marker_camera)
            LOGGER.debug("retaining explicit session camera %s for selected scene", marker_camera)
        camera_rebind_for_restore = camera_rebind_pending and not delayed_old_sync
        if (browser_id and (scene_changed or self._session_rebind_required)
                and (not restoring or camera_rebind_for_restore)
                and not explicit_sync_pending and not delayed_old_sync):
            await self._ensure_automatic_session(browser_id)
        # _ensure_automatic_session may wait while an explicit request is in flight. That request
        # can establish this one-time marker during the await, so re-read it before changing the
        # active history row; a delayed old-scene report must not rebind the previous transcript.
        marker = self._explicit_session_sync_pending
        marker_for_session = (marker is not None and marker.get("session_id") == self.state.session_id)
        marker_baseline = marker.get("baseline") if marker_for_session else None
        marker_expected = marker.get("expected") if marker_for_session else None
        marker_camera = marker.get("camera_id") if marker_for_session else None
        marker_camera_valid = (marker_camera in self.camera_candidates if marker_camera is not None
                               else not self.camera_candidates)
        delayed_old_sync = (marker_for_session and marker_baseline == incoming
                            and marker_expected != incoming)
        explicit_sync_pending = explicit_sync_pending or (
            marker_for_session and not delayed_old_sync and marker_camera_valid)
        explicit_history_pending = self._explicit_session_history_pending()
        if explicit_sync_pending:
            # This is the first report for the selected session's new document. Clear the marker
            # once its history row is active. Before then, keep the previous transcript protected.
            if explicit_history_pending:
                LOGGER.debug("preserving previous history row until the new session transcript exists")
            else:
                self._explicit_session_sync_pending = None
                LOGGER.debug("adopting first non-baseline scene for the explicit session")
        if restoring:
            self._conversation_restore_session_id = None
        if (not delayed_old_sync and not (camera_rebind_pending and self._session_rebind_required)
                and not explicit_history_pending):
            # A delayed report must not rewrite the previous active history row. Once the selected
            # scene arrives, synchronize only a row that already belongs to this session; history
            # activation and the explicit new-session flow create that row before reporting it.
            active = self.conversations.get(self.active_conversation_id) if self.active_conversation_id else None
            if not explicit_sync_pending or (active is not None and active.get("sessionID") == self.state.session_id):
                self._sync_active_conversation_binding()
            elif explicit_sync_pending:
                LOGGER.debug("preserving previous history row until the new session transcript exists")
        # Q147: this frame is the acknowledgement the reporting tab is waiting for, so it carries
        # that tab's own number back. Every connection gets it -- a second tab under the same
        # `browser_id` is a reader like any other -- and only the reporter can tell it apart from a
        # write that came from elsewhere, which is the whole point of echoing rather than omitting.
        await self.broadcast(self._scene_applied_frame(identity, action="updateWorld",
                                                      source_request_id=None,
                                                      ack_report_seq=report_seq))

    def _scene_applied_frame(self, identity: dict[str, Any], *, action: str,
                             source_request_id: str | None,
                             ack_report_seq: int | None = None) -> dict[str, Any]:
        """§5.4: the announcement is the scene's four-tuple, and nothing else.

        Its key is `scene_digest` in snake_case because this is a WS frame, not an HTTP body
        (spec §5.4 vs §5.9); the same value is `sceneDigest` in `/api/sessionState` and in
        `/startTask`. Readers treat the frame as "go re-read", never as a world (Q111), and
        `source_browser_id` is gone: under one binding it could only ever name the bound browser,
        which is a fact that drifts the moment a second gateway starts (§5.4).

        Q147 adds the two keys that make the frame an acknowledgement for the tab that caused it:
        `session_id` is the durable session this landing happened in, and `ack_report_seq` repeats
        back the reporting tab's own `report_seq` -- `None` for every landing that was not a report
        (a caller's write, a commit, a session switch). The gateway never compares that number, so
        the Agent side of the protocol does not change shape anywhere else.
        """
        return {"type": "scene.applied", "scene_id": identity["scene_id"],
                "scene_version": identity["scene_version"],
                "scene_sub_version": identity["scene_sub_version"],
                "scene_digest": identity["sceneDigest"],
                "session_phase": self.state.session_phase,
                "session_id": self.state.session_id,
                "action": action, "source_request_id": source_request_id,
                "ack_report_seq": ack_report_seq}

    def _cache_world(self, document: dict[str, Any]) -> None:
        """§1.4: keep the last world this gateway saw, for the record and for restart diagnostics.

        It is not a read source for callers. 流程文档 §5.1 (2026-11-05) closed that `/getWorld`
        fallback: a late browser report can move the persisted identity onto an older scene, and
        replaying the cache would hand that older scene to a reader that is looking at something
        else. Q160 reopened it for exactly one reader -- the bound browser reclaiming its own last
        report after a refresh (`_reclaimed_world`).
        """
        self.world_cache = document

    def _camera_candidate_view(self) -> list[int]:
        """§9 item 3: the reported 候选清单, but only while it still describes the stored scene.

        An empty answer has two causes and they read the same on purpose: no browser has reported a
        list yet, or the list on hand belongs to a scene this state no longer points at. Neither is
        a scene without cameras -- that is why the routes publish a list and never a verdict.
        """
        if self.camera_candidates_scene != self.state.scene_identity:
            return []
        return list(self.camera_candidates)

    async def _ensure_automatic_session(self, browser_id: str) -> None:
        """Bind the current scene to a fresh Brain session after a scene/browser change."""
        expected_browser = self.state.browser_id
        expected_session = self.state.session_id
        expected_scene = self.state.scene_identity
        transition_generation = self._session_transition_generation
        async with self._session_transition_lock:
            if (transition_generation != self._session_transition_generation
                    or self.state.browser_id != expected_browser
                    or self.state.session_id != expected_session
                    or self.state.scene_identity != expected_scene
                    or expected_browser != browser_id):
                LOGGER.info("discarding automatic session for a superseded binding or scene")
                return
            if self.state.scene_held or self.state.session_phase == "agent_owned":
                LOGGER.warning("browser_new_session_required: cannot auto-create session while scene is held (%s)", browser_id)
                return
            candidates = self._camera_candidate_view()
            if not candidates and not self._session_rebind_required:
                LOGGER.info("no camera candidate; retaining the current session until a scene reports one")
                return
            # A scene with no camera still gets a fresh Brain session, so an old session bound to a
            # different browser/scene can never leak into the next task. Brain will reject an invalid
            # platform explicitly; that is preferable to session_browser_not_active.
            camera_id = None  # Brain selects by explicit overhead semantics, not array order.
            status, payload = await self.agent_relay.request(
                "POST", "/sessions", json_body={"browserID": browser_id, "camera_id": None,
                                                "scene": self.world_cache})
            if (transition_generation != self._session_transition_generation
                    or self.state.browser_id != expected_browser
                    or self.state.session_id != expected_session
                    or self.state.scene_identity != expected_scene
                    or self.state.scene_held or self.state.session_phase == "agent_owned"):
                LOGGER.info("discarding stale automatic session response")
                return
            if status >= 400 or not isinstance(payload, dict) or not isinstance(payload.get("sessionID"), str):
                LOGGER.warning("automatic Brain session creation failed: HTTP %s %r", status, payload)
                return
            preserve_previous_history = self._explicit_session_history_pending(expected_session)
            camera_id = payload.get("camera_id")
            previous_phase = self.state.session_phase
            self.state.bind_browser(browser_id)
            self.state.start_session(payload["sessionID"], camera_id=camera_id, camera_reason=payload.get("camera_selection_reason"))
            self._session_transition_generation += 1
            if camera_id is None:
                self.state.clear_camera()
            self._session_rebind_required = False
            if preserve_previous_history:
                marker = self._explicit_session_sync_pending
                if marker is not None:
                    marker.update({"session_id": payload["sessionID"], "baseline": expected_scene,
                                   "expected": expected_scene, "camera_id": camera_id})
                LOGGER.debug("keeping the previous history row isolated during explicit camera rebind")
            else:
                self._explicit_session_sync_pending = None
                self._sync_active_conversation_binding()
            await self._announce_phase(previous_phase)
            LOGGER.info("automatically bound Brain session %s to camera %s", payload["sessionID"], camera_id)

    def _sync_active_conversation_binding(self) -> None:
        """Keep the active history row tied to the Gateway's authoritative scene/session.

        Scene reports can arrive before the browser refreshes the conversation detail. Updating
        this binding prevents a history row created for the bootstrap scene from advertising
        scene 0 after a preset scene is adopted.
        """
        conversation_id = self.active_conversation_id
        conversation = self.conversations.get(conversation_id) if conversation_id else None
        if conversation is None:
            return
        scene = self.state.scene_identity or {}
        scene_id = scene.get("scene_id", 0)
        session_id = self.state.session_id
        conversation["sessionID"] = session_id
        conversation["sceneBinding"] = {"sceneId": scene_id,
                                         "simSessionId": session_id or "local"}
        if isinstance(self.world_cache, dict):
            conversation["sceneSnapshot"] = json.loads(json.dumps(self.world_cache))
            context = self._session_context(session_id, create=True)
            context["world_snapshot"] = self._copy_json(self.world_cache)
            context["scene_identity"] = scene_identity(self.world_cache)
            context["camera_id"] = self.state.camera_id
            if self.state.lease is not None:
                context["lease"] = self._copy_json(self.state.lease)
        # `updatedAt` travels to the browser as the ISO text its conversation type declares; a
        # `time.time()` here renders the row as January 1970 and desyncs it from the message stamps.
        conversation["updatedAt"] = datetime.now(timezone.utc).isoformat()

    def _explicit_session_history_pending(self, session_id: str | None = None) -> bool:
        """Whether an explicit session transition still has the prior conversation selected."""
        marker = self._explicit_session_sync_pending
        conversation_id = self.active_conversation_id
        conversation = self.conversations.get(conversation_id) if conversation_id else None
        expected_session = session_id if session_id is not None else self.state.session_id
        return (marker is not None and marker.get("session_id") == expected_session
                and marker.get("protected_conversation_id") == conversation_id
                and conversation is not None)

    def _adopt_live_world(self, document: dict[str, Any], reported: dict[str, Any]) -> dict[str, Any]:
        """§5.1/§6: one live reading -- judge it, let it move what may move, and return its identity.

        The identity comes out of the **document**, never out of the flat fields the browser sent
        beside it: §5.1 requires header and body to describe the same read, and the body is the
        only half that can be re-checked by a digest. The flat fields are therefore a cross-check,
        and a disagreement voids the read -- a browser that reports two versions of one moment is
        broken, and serving either answer would be a guess.
        """
        incoming = scene_identity(document)
        for key in ("scene_id", "scene_version", "scene_sub_version"):
            value = reported.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value != incoming[key]:
                raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                                   f"browser answered getWorld with {key}={value} beside a document "
                                   f"whose {key} is {incoming[key]}")
        # §6: a live read is one of the three events that push the water mark -- the ids in this
        # document exist, whether or not the stored four-tuple is allowed to move to them (§1.2).
        self.state.raise_water_mark(object_ids_in_document(document))
        if adoption_verdict(self.state.session_phase, self.state.scene_identity, incoming) in ("adopt", "idempotent"):
            return self.state.record_scene(document)
        LOGGER.warning("live getWorld reading (%s) does not advance the stored scene (%s); "
                       "headers report the read, the water mark still rises",
                       incoming, self.state.scene_identity)
        return incoming

    async def _read_live_world(self, browser_id: str | None, *, use_lease_snapshot: bool = False) -> LiveWorld:
        """§5.15 step 2 (C2): one live `getWorld`, judged, adopted, and kept as §1.4's cache.

        `/getWorld` and the task submission share this because both need the same three things: the
        document, a four-tuple taken out of that document rather than out of the flat fields beside
        it, and a refusal of a browser that reports two versions of one moment.
        """
        held = self.state.lease
        if use_lease_snapshot and browser_id and isinstance(held, dict) and isinstance(held.get("world_snapshot"), dict):
            document = json.loads(json.dumps(held["world_snapshot"]))
            identity = scene_identity(document)
            self._cache_world(document)
            return LiveWorld(document, identity, "stopped")
        reported = await self.dispatch("getWorld", {}, browser_id=browser_id)
        document = reported.get("world")
        if not isinstance(document, dict):
            raise BrowserError(502, "GATEWAY_FRAMING_ERROR", "browser answered getWorld without a world object")
        identity = self._adopt_live_world(document, reported)
        # A live read is the freshest world there is, so it keeps §1.4's cache usable for the
        # callers that arrive later with no browser open.
        self._cache_world(document)
        return LiveWorld(document, identity, reported.get("playback_state"))

    def _world_response(self, document: dict[str, Any], *,
                        identity: dict[str, Any], browser_id: str | None,
                        playback_state: Any = None, source: str = "live") -> web.Response:
        """§5.1: every header describes this body, from this read -- live, or Q160's self-claimed snapshot."""
        headers = {"X-World-Source": source,
                   "X-Scene-Sub-Version": f"{identity['scene_sub_version']}",
                   "X-Scene-Digest": str(identity["sceneDigest"]),
                   "X-Next-Object-ID": f"{self.state.next_object_id}",
                   "X-Session-Phase": self.state.session_phase}
        for key, name in (("scene_id", "X-Scene-ID"), ("scene_version", "X-Scene-Version")):
            if identity[key] is not None:
                headers[name] = f"{identity[key]}"
        if browser_id:
            headers["X-Browser-ID"] = browser_id
        if isinstance(playback_state, str) and playback_state:
            headers["X-Playback-State"] = playback_state
        return web.json_response(document, headers=headers)

    def _browser_targets(self, browser_id: str | None) -> list[web.WebSocketResponse]:
        """§5.2/§6: every `browser.request` has exactly one address, and it is the bound browser.

        The old branch here answered `browser_id=None` with *all* connected browsers and let the
        first reply win. With the binding on disk that is no longer a fallback, it is a bug: the
        winner of that race decides what "the scene" means for every later CAS, which is exactly
        the failure this repo's 坑 32 recorded (a second viewer won a `getWorld` race with an empty
        world and rolled back a freshly loaded scene). Spec §5.2 says it plainly: a second tab is a
        read-only observer, it does not claim the binding and it never receives `browser.request`
        -- so a caller that names a browser other than the bound one is asking for something this
        gateway will not do, and 503 is the honest answer.
        """
        bound = self.state.browser_id
        if bound is None:
            raise BrowserError(503, "NO_BROWSER_CONNECTED",
                               "no browser has claimed the session on /events")
        if browser_id is not None and browser_id != bound:
            raise BrowserError(503, "NO_BROWSER_CONNECTED",
                               f"browser {browser_id!r} is not the bound browser {bound!r}; "
                               f"viewers never receive browser.request (§5.2)")
        socket = self.browsers.get(bound)
        if socket is None:
            raise BrowserError(503, "NO_BROWSER_CONNECTED", f"browser {bound} is not connected")
        return [socket]

    async def dispatch(self, action: str, payload: dict[str, Any], *, timeout: float | None = None,
                       binary: bytes | None = None, browser_id: str | None = None) -> dict[str, Any]:
        outcome = await self.dispatch_with_source(action, payload, timeout=timeout, binary=binary, browser_id=browser_id)
        return outcome.payload

    async def dispatch_with_source(self, action: str, payload: dict[str, Any], *, timeout: float | None = None,
                                   binary: bytes | None = None, browser_id: str | None = None) -> WriteOutcome:
        """Open one browser request, wait for the one matching response (§5.1 - §5.3, §6)."""
        if action not in BROWSER_ACTIONS:
            raise BrowserError(400, "INVALID_PARAM", f"unknown browser action {action!r}")
        targets = self._browser_targets(browser_id)
        if not targets:
            raise BrowserError(503, "NO_BROWSER_CONNECTED", "no viewer has announced itself on /events")
        if timeout is None:
            key = "capture_timeout_seconds" if action in RENDER_ACTIONS else "browser_request_timeout_seconds"
            timeout = float(self.config[key])
        request_id = str(uuid.uuid4())
        body = dict(payload)
        if binary is not None:
            body["byte_length"] = len(binary)
        frame: dict[str, Any] = {"type": "browser.request", "request_id": request_id, "action": action, "payload": body}
        if binary is not None:
            frame["expects_binary"] = True
        future = self.pending.open(request_id)
        sent = 0
        try:
            for socket in targets:
                await socket.send_json(frame)
                # §5.3: the raw TRAJ bytes always follow their text frame.
                if binary is not None:
                    await socket.send_bytes(binary)
                sent += 1
        except (ConnectionError, RuntimeError) as error:
            if sent == 0:
                self.pending.abandon(request_id)
                raise BrowserError(503, "NO_BROWSER_CONNECTED",
                                   f"the browser socket broke: {error}") from error
            # A browser that did get the request may still answer, so keep waiting.
            LOGGER.warning("browser socket broke while sending %s (%d of %d delivered)",
                           action, sent, len(targets))
        try:
            answered = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as error:
            self.pending.abandon(request_id)
            raise BrowserError(504, "BROWSER_TIMEOUT", f"browser did not answer {action} in {timeout}s",
                               request_id=request_id) from error
        return WriteOutcome(answered, request_id)

    @staticmethod
    def _is_uint_text(raw: str) -> bool:
        # isdigit() alone accepts non-ASCII digits such as "²" that int() then rejects.
        return raw.isascii() and raw.isdigit()

    def _uint_query(self, request: web.Request, name: str, *, required: bool = True) -> int | None:
        """§2.1/§5.13: ids and versions are uint32 query parameters."""
        raw = request.query.get(name)
        if raw is None:
            if required:
                raise BrowserError(400, "MISSING_PARAM", f"{name} is required")
            return None
        if not self._is_uint_text(raw) or int(raw) > MAX_UINT32:
            raise BrowserError(400, "INVALID_PARAM", f"{name} must be a uint32, got {raw!r}")
        return int(raw)

    def _bool_query(self, request: web.Request, name: str) -> bool:
        raw = request.query.get(name)
        if raw is None or raw == "":
            return False
        lowered = raw.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise BrowserError(400, "INVALID_PARAM", f"{name} must be a boolean, got {raw!r}")

    async def _json_body(self, request: web.Request, *, code: str) -> dict[str, Any]:
        """§11.1/§11.2: the gateway checks size and JSON shape, never scene semantics."""
        limit = int(self.config["max_payload_bytes"])
        declared = request.content_length
        if declared is not None and declared > limit:
            raise BrowserError(413, "PAYLOAD_TOO_LARGE", f"{declared} bytes exceeds the {limit} byte limit")
        content_type = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise BrowserError(400, "INVALID_PARAM", f"body must be application/json, got {content_type or 'none'!r}")
        raw = await request.read()
        if len(raw) > limit:
            raise BrowserError(413, "PAYLOAD_TOO_LARGE", f"{len(raw)} bytes exceeds the {limit} byte limit")
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise BrowserError(400, code, f"body is not valid JSON: {error}") from error
        if not isinstance(value, dict):
            raise BrowserError(400, code, f"body must be a JSON object, got {type(value).__name__}")
        return value

    @staticmethod
    def _client_request_id(value: Any) -> str:
        """§7.1: the browser's own idempotency id, missing or malformed being a missing context field.

        It names a key in a dict, never a path, but it is checked against the shape §8.1 gave a
        `transactionID` so one rule covers both identifiers a caller gets to choose -- and a caller
        that retries with a uuid-shaped id it made up cannot smuggle a second submission past the map.
        """
        if not is_transaction_id(value):
            raise BrowserError(409, "session_context_required",
                               f"clientRequestID must be 1-128 characters of [A-Za-z0-9._-], got {value!r}")
        return value

    async def update_world(self, request: web.Request) -> web.Response:
        """§4.2/§5.9: hand the whole world document to a browser and relay its result."""
        world = await self._json_body(request, code="INVALID_WORLD_SCHEMA")
        return await self._write("updateWorld", {"world": world}, request)

    async def update_item(self, request: web.Request) -> web.Response:
        """§4.2: full replacement of one object, id from the query."""
        object_id = self._uint_query(request, "id")
        body = await self._json_body(request, code="INVALID_PARAM")
        declared = body.get("object_id_in_scene")
        if declared is not None and declared != object_id:
            raise BrowserError(400, "BODY_ID_MISMATCH", f"body id {declared} does not match query id {object_id}")
        return await self._write("updateItem", {"id": object_id, "object": body}, request)

    async def add_object(self, request: web.Request) -> web.Response:
        """§5.5 (Q112): the gateway pre-claims the id when the caller omits it.

        The water mark only knows the ids it has already seen -- a loaded document (state_sync),
        a live `getWorld`, an Agent write -- and `X-Next-Object-ID` is the seed every reader
        trusts. So an id-free `/addObject` takes the next one here rather than letting a browser
        whose local mark is behind hand back an id the scene already holds. The browser still
        judges: it answers `ID_ALREADY_EXISTS` if the claimed id is taken, and that id stays spent
        (§6: a released id is never reused).
        """
        body = await self._json_body(request, code="INVALID_PARAM")
        bound = self.state.browser_id
        if (body.get("object_id_in_scene") is None and bound is not None
                and bound in self.browsers):
            body = {**body, "object_id_in_scene": self.state.allocate_object_ids(1)[0]}
        return await self._write("addObject", {"object": body}, request)

    async def remove_object(self, request: web.Request) -> web.Response:
        """§5.14: cascade by default, detach_children re-parents the direct children."""
        object_id = self._uint_query(request, "id")
        detach = self._bool_query(request, "detach_children")
        return await self._write("removeObject", {"id": object_id, "detach_children": detach}, request)

    def _expected_identity(self, value: Any) -> dict[str, Any]:
        """The CAS four-tuple of a `commitWorld` body, in the shape `scene_identity` returns.

        Checked as a set of required keys rather than defaulted: a missing `sceneDigest` read as
        `None` would match a stored `None` and let a caller commit against a 现场 it never named.
        """
        if not isinstance(value, dict):
            raise BrowserError(400, "MISSING_PARAM",
                               "expected{scene_id, scene_version, scene_sub_version, sceneDigest} is required")
        for name in SCENE_IDENTITY_KEYS:
            if name not in value:
                raise BrowserError(400, "MISSING_PARAM", f"expected.{name} is required")
        checked: dict[str, Any] = {}
        for name in ("scene_id", "scene_version", "scene_sub_version"):
            raw = value[name]
            if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_UINT32:
                raise BrowserError(400, "web_uint32_required", f"expected.{name} must be a uint32, got {raw!r}")
            checked[name] = raw
        digest = value["sceneDigest"]
        if not is_scene_digest(digest):
            raise BrowserError(400, "INVALID_PARAM", "expected.sceneDigest must be 64 lowercase hex characters")
        checked["sceneDigest"] = digest
        return checked

    @staticmethod
    def _commit_receipt(transaction_id: str, browser_id: str, *, status: str,
                        identity: dict[str, Any], rejected_objects: list[Any],
                        candidate_digest: str, reason: str | None = None) -> dict[str, Any]:
        """§8.1's回执, plus the one internal key the HTTP response never carries.

        `candidate_digest` is what makes "same transaction, same content" checkable on a *rejected*
        receipt: the four-tuple of a rejection is the unchanged 现场, not the document that was
        refused, so without it a resend with different content could not be told apart from a
        resend of the same one.
        """
        receipt: dict[str, Any] = {"transactionID": transaction_id, "browserID": browser_id,
                                   "status": status, "scene_id": identity["scene_id"],
                                   "scene_version": identity["scene_version"],
                                   "scene_sub_version": identity["scene_sub_version"],
                                   "sceneDigest": identity["sceneDigest"],
                                   "rejected_objects": rejected_objects,
                                   "candidate_digest": candidate_digest}
        if reason is not None:
            receipt["reason"] = reason
        return receipt

    @staticmethod
    def _receipt_view(receipt: dict[str, Any]) -> dict[str, Any]:
        """The persisted record with the internal idempotency key taken back off."""
        value = {key: value for key, value in receipt.items() if key not in {"candidate_digest", "request_fingerprint"}}
        if value.get("status") == "prepared":
            value["status"] = "unknown"
        return value

    def job_commit_receipts(self, value: Any) -> list[dict[str, Any]]:
        """§7.3 rule 6: the transactions a finished task names, read from this gateway's own disk.

        Both absences answer `unknown` -- no receipt file, and a file that does not read as a
        commit record. A job view is a display rather than a decision, so it must not raise the
        500 that `queryWorldCommit` gives for the same unreadable record: turning an `unknown`
        into a verdict is `resolveUncertain`'s work alone (§8.3), and a poll round that died on
        a half-written file would end a task that never ended.
        """
        receipts: list[dict[str, Any]] = []
        for transaction_id in agent_transaction_ids(value):
            try:
                settled: dict[str, Any] | None = self.receipts.load(transaction_id)
                reason = NO_RECEIPT_REASON
            except TransactionReceiptError as error:
                LOGGER.error("%s", error)
                settled, reason = None, "the stored receipt does not read as a commit record"
            receipts.append(self._receipt_view(settled) if settled is not None else
                            {"transactionID": transaction_id, "status": "unknown", "reason": reason})
        return receipts

    def _persist_receipt(self, receipt: dict[str, Any]) -> None:
        """§9.2 ②: a receipt that cannot be written is a 500, never a quiet memory-only fallback.

        The 500 says what happened honestly on both branches: a `committed` record that failed to
        land means the world *did* change and we cannot prove it, which is exactly the `unknown`
        reading that keeps the lock -- and answering as if nothing happened would be the one answer
        that is wrong.
        """
        fingerprint = getattr(self, "_active_commit_fingerprint", None)
        if fingerprint is not None:
            receipt = {**receipt, "request_fingerprint": fingerprint}
        try:
            self.receipts.save(receipt["transactionID"], receipt)
        except OSError as error:
            LOGGER.error("commit receipt for %s could not be written (%s)", receipt["transactionID"], error)
            raise BrowserError(500, "INTERNAL_ERROR",
                               f"the commit receipt for {receipt['transactionID']} could not be persisted") from error

    async def commit_world(self, request: web.Request) -> web.Response:
        # A strict browser write marks the lease uncertain before awaiting the browser. Keep the
        # transition lock for that whole round so session/history activation cannot pass its check
        # in the gap before `commit_unconfirmed` is persisted.
        async with self._session_transition_lock:
            body = await self._json_body(request, code="INVALID_WORLD_SCHEMA")
            transaction = body.get("transactionID")
            browser = body.get("browserID")
            # Malformed/unattributable requests are not transactions.
            if not is_transaction_id(transaction) or not isinstance(browser, str) or not browser:
                return await self._commit_world_locked(request)
            expected = self._expected_identity(body.get("expected"))
            fingerprint = {"sessionID": request.headers.get("X-Brain-Session"),
                           "browserID": browser, "expected": expected, "scene": body.get("scene")}
            settled = self.receipts.load(transaction)
            if settled is not None:
                if settled.get("request_fingerprint") != fingerprint:
                    raise BrowserError(409, "browser_scene_version_conflict",
                                       "transaction request fingerprint differs or legacy receipt lacks full evidence")
                if settled["status"] in {"prepared", "unknown"}:
                    return web.json_response({**self._receipt_view(settled), "status": "unknown"})
                return web.json_response(self._receipt_view(settled))
            self._active_commit_fingerprint = fingerprint
            self._commit_dispatched = False
            record = {"transactionID": transaction, "browserID": browser,
                      "sessionID": request.headers.get("X-Brain-Session"),
                      "status": "prepared", **expected}
            try:
                self._persist_receipt(record)
                return await self._commit_world_locked(request)
            except BrowserError as error:
                if not self._commit_dispatched and 400 <= error.status < 500:
                    context = self._background_context(request)
                    current = context[1].get("scene_identity") if context else self.state.scene_identity
                    rejected = {**record, **(current or expected), "status": "rejected",
                                "reason": f"{error.code}: {error.message}", "rejected_objects": [],
                                "error": {"code": error.code, "message": error.message}}
                    self._persist_receipt(rejected)
                    return web.json_response(self._receipt_view(rejected), status=error.status)
                self._persist_receipt({**record, "status": "unknown", "reason": error.code})
                raise
            finally:
                self._active_commit_fingerprint = None

    async def _commit_world_locked(self, request: web.Request) -> web.Response:
        """§8.1: the Agent's whole-or-nothing write, settled by a receipt on our disk.

        Four gates run before anything reaches a browser, and each answers a different question:
        the CAS asks whether the caller still means *this* 现场, the sub-version gate whether the
        candidate re-baselined the ② 类 counter (rule 2 -- a candidate that carries one would fold
        the panel's edits into the Agent's count), the target gate whether the step is exactly one
        (a commit never advances by two and the gateway never invents a step), and the idempotency
        gate whether this transaction already settled.
        """
        body = await self._json_body(request, code="INVALID_WORLD_SCHEMA")
        transaction_id = body.get("transactionID")
        if transaction_id is None:
            raise BrowserError(400, "MISSING_PARAM", "transactionID is required")
        if not is_transaction_id(transaction_id):
            raise BrowserError(400, "INVALID_PARAM",
                               f"transactionID must be 1-128 characters of [A-Za-z0-9._-], got {transaction_id!r}")
        browser_id = body.get("browserID")
        if browser_id is None:
            raise BrowserError(400, "MISSING_PARAM", "browserID is required")
        if not isinstance(browser_id, str) or not browser_id:
            raise BrowserError(400, "INVALID_PARAM", "browserID must be a non-empty string")
        expected = self._expected_identity(body.get("expected"))
        scene = body.get("scene")
        if scene is None:
            raise BrowserError(400, "MISSING_PARAM", "scene is required")
        if not isinstance(scene, dict):
            raise BrowserError(400, "INVALID_WORLD_SCHEMA",
                               f"scene must be a world document object, got {type(scene).__name__}")
        # The digest of exactly the bytes we were handed: this route holds the whole document, so
        # unlike a `/updateWorld` relay it never has to announce a version with a null digest.
        candidate = scene_identity(scene)
        sub_version = scene.get("scene_sub_version")
        if not (sub_version is None or (isinstance(sub_version, int) and not isinstance(sub_version, bool)
                                        and sub_version == 0)):
            raise BrowserError(400, "gateway_scene_revision_invalid",
                               f"a commit candidate must re-baseline scene_sub_version to 0, got {sub_version!r}")

        settled = self.receipts.load(transaction_id)
        if settled is not None and settled["status"] in {"committed", "rejected"}:
            if settled.get("candidate_digest") == candidate["sceneDigest"]:
                # §8.1: 同事务同内容只生效一次. The first answer is still the answer -- and re-
                # dispatching here would be the resend this protocol forbids.
                LOGGER.info("commit %s replayed from its receipt (%s)", transaction_id, settled["status"])
                return web.json_response(self._receipt_view(settled))
            raise BrowserError(409, "browser_scene_version_conflict",
                               f"transaction {transaction_id} already settled as {settled['status']} "
                               f"for a different document; a transaction is never re-committed")

        background = self._background_context(request)
        if background is not None:
            session_id, context = background
            held = context.get("lease")
            if held is None or held.get("leaseToken") != request.headers.get("X-Brain-Lease"):
                raise BrowserError(409, "scene_lease_required",
                                   "the background session does not hold the supplied Agent lease")
            if held.get("browser_id") != browser_id or context.get("quarantine") is not None:
                raise BrowserError(409, "scene_lease_required", "background lease browser mismatch or quarantined")
            current = context.get("scene_identity")
            if current is None or expected != current:
                raise BrowserError(409, "browser_scene_version_conflict",
                                   "the background session scene changed before commit")
            target_version = expected["scene_version"] + 1 if candidate["scene_id"] == expected["scene_id"] else 1
            if candidate["scene_version"] != target_version:
                raise BrowserError(400, "gateway_target_version_invalid",
                                   f"background commit must advance scene version by one, got {candidate['scene_version']}")
            self._commit_dispatched = True
            context["world_snapshot"] = self._copy_json(scene)
            context["scene_identity"] = candidate
            held["scene_identity"] = candidate
            held["world_snapshot"] = self._copy_json(scene)
            self.state.raise_water_mark(object_ids_in_document(scene))
            for conversation in self.conversations.values():
                if conversation.get("sessionID") == session_id:
                    conversation["sceneSnapshot"] = self._copy_json(scene)
                    conversation["updatedAt"] = datetime.now(timezone.utc).isoformat()
            receipt = self._commit_receipt(transaction_id, browser_id, status="committed",
                                           identity=candidate, rejected_objects=[],
                                           candidate_digest=candidate["sceneDigest"])
            self._persist_receipt(receipt)
            LOGGER.info("background commit %s landed for session %s", transaction_id, session_id)
            return web.json_response(self._receipt_view(receipt))

        current = self.state.scene_identity
        if current is None or expected != current:
            raise BrowserError(409, "browser_scene_version_conflict",
                               f"expected {expected} is not the 现场 this gateway holds ({current}); "
                               f"re-read /getWorld live and rebuild the candidate")
        held = self.state.lease
        if (held is None or held.get("leaseToken") != request.headers.get("X-Brain-Lease")
                or held.get("browser_id") != browser_id
                or held.get("session_id") != request.headers.get("X-Brain-Session")
                or self.state.quarantine is not None):
            raise BrowserError(409, "scene_lease_required", "commitWorld requires the target session's active lease")
        target_version = expected["scene_version"] + 1 if candidate["scene_id"] == expected["scene_id"] else 1
        if candidate["scene_version"] != target_version:
            raise BrowserError(400, "gateway_target_version_invalid",
                               f"scene {expected['scene_id']} is at version {expected['scene_version']}, "
                               f"so a commit must be exactly {expected['scene_version'] + 1}, "
                               f"got {candidate['scene_version']}")

        # A task that already acquired the lease has a verified world snapshot. If the bound
        # browser disappears after that point, commit the CAS candidate into the gateway-owned
        # active-session snapshot; a reconnect can then restore this world without inventing a
        # scene for a caller that never held the lease.
        held = self.state.lease
        offline_session = (held is not None
                           and held.get("browser_id") == browser_id
                           and held.get("leaseToken") == request.headers.get("X-Brain-Lease")
                           and self.state.browser_id == browser_id
                           and browser_id not in self.browsers
                           and self.state.quarantine is None
                           and not held.get("commit_unconfirmed")
                           and bool(self.state.session_id)
                           and held.get("session_id") == self.state.session_id
                           and request.headers.get("X-Brain-Session") == self.state.session_id
                           and isinstance(held.get("world_snapshot"), dict)
                           and scene_identity(held["world_snapshot"]) == expected)
        if offline_session:
            self._commit_dispatched = True
            held["commit_unconfirmed"] = True
            self.state.save()
            identity = self.state.record_scene(scene)
            held["scene_identity"] = candidate
            held["world_snapshot"] = self._copy_json(scene)
            self.state.save()
            self._cache_world(scene)
            self.scene_version = candidate["scene_version"]
            context = self._session_context(self.state.session_id, create=True)
            if context is not None:
                context["world_snapshot"] = self._copy_json(scene)
                context["scene_identity"] = identity
                context["lease"] = self._copy_json(held)
            for conversation in self.conversations.values():
                if conversation.get("sessionID") == self.state.session_id:
                    conversation["sceneSnapshot"] = self._copy_json(scene)
                    conversation["updatedAt"] = datetime.now(timezone.utc).isoformat()
            receipt = self._commit_receipt(transaction_id, browser_id, status="committed",
                                           identity=candidate, rejected_objects=[],
                                           candidate_digest=candidate["sceneDigest"])
            self._persist_receipt(receipt)
            held.pop("commit_unconfirmed", None)
            self.state.save()
            if context is not None:
                context["lease"] = self._copy_json(held)
            LOGGER.info("offline commit %s landed for active session %s", transaction_id,
                        self.state.session_id)
            return web.json_response(self._receipt_view(receipt))

        payload = {"world": scene, "strict": True, "expected_version": expected["scene_version"]}
        if held is not None:
            held["commit_unconfirmed"] = True
            self.state.save()
        try:
            self._commit_dispatched = True
            outcome = await self.dispatch_with_source("updateWorld", payload, browser_id=browser_id)
        except BrowserError as error:
            if error.code in UNANSWERED_CODES:
                if held is not None:
                    held["commit_unconfirmed"] = True
                    self.state.save()
                # Never delivered, or delivered and unanswered: no receipt, so /queryWorldCommit
                # says `unknown` and the lock stays. Writing `rejected` here would be the one
                # claim this branch cannot support -- we do not know whether it landed.
                raise
            # The browser refused in its own words, and `strict` means its 现场 did not move: the
            # unchanged four-tuple is the evidence §8.1 asks a `rejected` receipt to carry.
            receipt = self._commit_receipt(transaction_id, browser_id, status="rejected",
                                           identity=current, rejected_objects=[],
                                           candidate_digest=candidate["sceneDigest"],
                                           reason=f"{error.code}: {error.message}")
            self._persist_receipt(receipt)
            if held is not None:
                held.pop("commit_unconfirmed", None)
                self.state.save()
            return web.json_response(self._receipt_view(receipt))

        rejected = outcome.payload.get("rejected_objects")
        if not isinstance(rejected, list):
            raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                               "browser answered a strict commit without a rejected_objects list")
        if rejected:
            first = rejected[0] if isinstance(rejected[0], dict) else {}
            receipt = self._commit_receipt(
                transaction_id, browser_id, status="rejected", identity=current,
                rejected_objects=rejected, candidate_digest=candidate["sceneDigest"],
                reason=f"{len(rejected)} object(s) of the candidate were refused, so none of it "
                       f"landed; first is objects[{first.get('index')}] ({first.get('reason')})")
            self._persist_receipt(receipt)
            if held is not None:
                held.pop("commit_unconfirmed", None)
                self.state.save()
            return web.json_response(self._receipt_view(receipt))
        for name in ("scene_id", "scene_version"):
            value = outcome.payload.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value != candidate[name]:
                # The candidate landed but the browser says it holds a different scene than the one
                # we handed it. Nothing may be recorded from a reading that disagrees with itself;
                # the transaction becomes `unknown`, which is the answer that keeps the lock.
                raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                                   f"browser committed {name}={value} beside a candidate whose {name} "
                                   f"is {candidate[name]}")

        # §6 event ①/③: the candidate's ids exist now, and the world the browser just confirmed is
        # the stored 现场 -- with a digest of the document itself, computed here (§3).
        identity = self.state.record_scene(scene)
        self._cache_world(scene)
        self.scene_version = candidate["scene_version"]
        receipt = self._commit_receipt(transaction_id, browser_id, status="committed",
                                       identity=identity, rejected_objects=[],
                                       candidate_digest=candidate["sceneDigest"])
        # Persisted before the announcement: Brain may already be querying this id while the
        # broadcast is still going out, and a receipt that arrives after a notice is a window in
        # which the transaction looks `unknown` after it committed.
        self._persist_receipt(receipt)
        if held is not None:
            held.pop("commit_unconfirmed", None)
            self.state.save()
        await self.broadcast(self._scene_applied_frame(identity, action="updateWorld",
                                                      source_request_id=outcome.request_id))
        LOGGER.info("commit %s landed as scene %s version %s", transaction_id,
                    identity["scene_id"], identity["scene_version"])
        return web.json_response(self._receipt_view(receipt))

    async def query_world_commit(self, request: web.Request) -> web.Response:
        """§8.1/§9.2: the one caller answer that needs no browser -- the receipt is on disk."""
        raw = request.query.get("transactionID")
        if raw is None or raw == "":
            raise BrowserError(400, "MISSING_PARAM", "transactionID is required")
        if not is_transaction_id(raw):
            raise BrowserError(400, "INVALID_PARAM",
                               f"transactionID must be 1-128 characters of [A-Za-z0-9._-], got {raw!r}")
        try:
            settled = self.receipts.load(raw)
        except TransactionReceiptError as error:
            LOGGER.error("%s", error)
            raise BrowserError(500, "INTERNAL_ERROR", str(error)) from error
        if settled is None:
            # Not a 404, and no四-tuple: the absence of a receipt is a fact about our disk, not a
            # statement about the scene, and `unknown` is the status that must keep the lock and
            # must never age into `rejected` (§8.1).
            return web.json_response({"transactionID": raw, "status": "unknown",
                                      "reason": NO_RECEIPT_REASON})
        return web.json_response(self._receipt_view(settled))

    def _lease_fingerprint(self, body: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
        """The `(taskID, browserID, 四元组)` an acquire has to name before a lock is granted.

        `commitWorld` reads the same four values out of a nested `expected` and answers three
        different 400 codes for three different failures; this route's spec row names only
        `INVALID_PARAM`, so every shape failure here is that one code, and its message names the
        field. Nothing is defaulted: a fingerprint read from absent keys would "match" a 现场 that
        happens to lack them, which is the opposite of what §7.2 locks.
        """
        task_id = body.get("taskID")
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 0:
            raise BrowserError(400, "INVALID_PARAM",
                               f"taskID must be a non-negative integer, got {task_id!r}")
        browser_id = body.get("browserID")
        if not isinstance(browser_id, str) or not browser_id:
            raise BrowserError(400, "INVALID_PARAM",
                               f"browserID must be a non-empty string, got {browser_id!r}")
        scene_id = body.get("scene_id")
        if "scene_id" not in body:
            raise BrowserError(400, "INVALID_PARAM",
                               "scene_id is required; null is the answer for a 现场 that has none")
        if scene_id is not None and (isinstance(scene_id, bool) or not isinstance(scene_id, int)
                                     or not 0 <= scene_id <= MAX_UINT32):
            raise BrowserError(400, "INVALID_PARAM", f"scene_id must be a uint32 or null, got {scene_id!r}")
        checked: dict[str, Any] = {"scene_id": scene_id}
        for name in ("scene_version", "scene_sub_version"):
            raw = body.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_UINT32:
                raise BrowserError(400, "INVALID_PARAM", f"{name} must be a uint32, got {raw!r}")
            checked[name] = raw
        digest = body.get("sceneDigest")
        if not is_scene_digest(digest):
            raise BrowserError(400, "INVALID_PARAM",
                               f"sceneDigest must be 64 lowercase hex characters, got {digest!r}")
        checked["sceneDigest"] = digest
        return task_id, browser_id, checked

    async def acquire_scene_lease(self, request: web.Request) -> web.Response:
        async with self._session_transition_lock:
            body = await self._json_body(request, code="INVALID_PARAM")
            task_id, browser_id, _ = self._lease_fingerprint(body)
            session_id = request.headers.get("X-Brain-Session") or self.state.session_id
            key = f"{session_id}:{task_id}"
            if key in self.state.raw["task_fences"]:
                return web.json_response({"error": {"code": "task_fenced", "message": "旧任务已失效，不可重新获取租约。"},
                    "lease_status": "not_granted", "taskID": task_id, "browserID": browser_id,
                    "sessionID": session_id}, status=409)
            try:
                result = await self._acquire_scene_lease_locked(request)
            except BrowserError as error:
                context = self._background_context(request)
                held = context[1].get("lease") if context else self.state.lease
                if held and held.get("task_id") == task_id:
                    raise  # a failed reply must not erase evidence of an earlier grant
                evidence = {"lease_status": "not_granted", "taskID": task_id,
                            "browserID": browser_id, "sessionID": session_id}
                self.state.raw["lease_outcomes"][key] = evidence
                self.state.save()
                return web.json_response({**json.loads(error.text), **evidence}, status=error.status)
            self.state.raw["lease_outcomes"][key] = {"lease_status": "granted", "taskID": task_id,
                "sessionID": session_id, "browserID": browser_id}
            self.state.save()
            return result

    async def _acquire_scene_lease_locked(self, request: web.Request) -> web.Response:
        """§7.2: take the edit lock, held by a token only this answer carries.

        The lock is three promises (no playback, no scene switch, no light preset), and the gateway
        keeps only the bookkeeping for them: the freezes themselves are the browser's, and they
        reach it through the phase and the lease it reads back (§5 的 /api/sessionState) rather
        than through a frame type this protocol does not have. Two branches answer before the
        fingerprint is ever compared -- an existing lease (idempotent or `task_busy`) and a
        standing quarantine -- because §8.3 makes both of them un-walkable-around, and a caller
        that re-acquires under them must hear about the lock, not about a version mismatch.
        """
        body = await self._json_body(request, code="INVALID_PARAM")
        task_id, browser_id, fingerprint = self._lease_fingerprint(body)
        bound = self.state.browser_id
        if bound is None or bound != browser_id or browser_id not in self.browsers:
            raise BrowserError(503, "NO_BROWSER_CONNECTED",
                               f"the lease guards the scene browser {browser_id!r} is looking at; "
                               f"this gateway is bound to {bound!r}"
                               f"{'' if bound else ' and no browser has claimed the session'}")
        background = self._background_context(request)
        if background is not None:
            session_id, context = background
            held = context.get("lease")
            if held is not None:
                if (held.get("task_id") == task_id and held.get("browser_id") == browser_id
                        and held.get("scene_identity") == fingerprint):
                    return web.json_response({"browserID": browser_id, "leaseToken": held["leaseToken"]})
                raise BrowserError(409, "task_busy", f"session {session_id} already holds a lease")
            current = context.get("scene_identity")
            if current is None or current != fingerprint:
                raise BrowserError(409, "browser_scene_version_conflict",
                                   f"the lease fingerprint is not the stored session {session_id} scene")
            record = {"leaseToken": uuid.uuid4().hex, "task_id": task_id, "browser_id": browser_id,
                      "session_id": session_id, "scene_identity": fingerprint,
                      "acquired_at": int(time.time()),
                      "world_snapshot": self._copy_json(context.get("world_snapshot"))}
            context["lease"] = record
            return web.json_response({"browserID": browser_id, "leaseToken": record["leaseToken"]})
        held = self.state.lease
        if held is not None:
            if (held.get("task_id") == task_id and held.get("browser_id") == browser_id
                    and held.get("scene_identity") == fingerprint):
                # The same task asking again for the same 现场 already holds the lock; a second
                # token would silently retire the first one and leave Brain holding paper we forgot.
                LOGGER.info("task %s re-acquired its lease on %s", task_id, browser_id)
                return web.json_response({"browserID": browser_id, "leaseToken": held["leaseToken"]})
            raise BrowserError(409, "task_busy",
                               f"task {held.get('task_id')} still holds the lease for {held.get('browser_id')}; "
                               f"task {task_id} cannot take it over -- release it or resolve it first")
        if self.state.quarantine is not None:
            raise BrowserError(409, "task_busy",
                               "the scene is quarantined after an uncertain write; "
                               "only POST /resolveUncertain clears it (§8.3)")
        current = self.state.scene_identity
        if current is None or current != fingerprint:
            raise BrowserError(409, "browser_scene_version_conflict",
                               f"the lease fingerprint {fingerprint} is not the 现场 this gateway holds "
                               f"({current}); read /getWorld live first (§9.1 V0)")
        record = {"leaseToken": uuid.uuid4().hex, "task_id": task_id, "browser_id": browser_id,
                  "session_id": self.state.session_id, "scene_identity": fingerprint,
                  "acquired_at": int(time.time())}
        if isinstance(self.world_cache, dict):
            record["world_snapshot"] = json.loads(json.dumps(self.world_cache))
        self.state.acquire_lease(record)
        LOGGER.info("task %s holds the scene lease %s over scene %s v%s", task_id,
                    record["leaseToken"][:8], fingerprint["scene_id"], fingerprint["scene_version"])
        return web.json_response({"browserID": browser_id, "leaseToken": record["leaseToken"]})

    async def release_scene_lease(self, request: web.Request) -> web.Response:
        """§7.2/§8.2: hand the lock back, or say "I do not know what landed" and keep it shut.

        `keepLocked=true` is the quarantine branch: the release that admits uncertainty must leave
        a record behind, and the four-tuple in it is the one acquire agreed to -- never a guessed
        reading of what the scene might hold now (§8.1 forbids inventing that). A token that does
        not match the holder is a 400 naming the field rather than a new code, because the only
        honest facts here are "you are not the holder" and "nothing is held", and both are the
        caller's mistake about `leaseToken`.
        """
        body = await self._json_body(request, code="INVALID_PARAM")
        browser_id = body.get("browserID")
        if not isinstance(browser_id, str) or not browser_id:
            raise BrowserError(400, "INVALID_PARAM",
                               f"browserID must be a non-empty string, got {browser_id!r}")
        token = body.get("leaseToken")
        if not isinstance(token, str) or not token:
            raise BrowserError(400, "INVALID_PARAM",
                               f"leaseToken must be the token /acquireSceneLease answered with, got {token!r}")
        keep_locked = body.get("keepLocked", False)
        if not isinstance(keep_locked, bool):
            raise BrowserError(400, "INVALID_PARAM", f"keepLocked must be a boolean, got {keep_locked!r}")
        background = self._background_context(request)
        if background is not None:
            session_id, context = background
            held = context.get("lease")
            if held is None:
                raise BrowserError(400, "INVALID_PARAM", "no lease is held for this session")
            if held.get("browser_id") != browser_id or held.get("leaseToken") != token:
                raise BrowserError(400, "INVALID_PARAM", "leaseToken does not match this session holder")
            context["lease"] = None
            if keep_locked:
                context["quarantine"] = {"session_id": session_id, "scene_identity": held.get("scene_identity"),
                                          "task_id": held.get("task_id"),
                                          "commit_unconfirmed": held.get("commit_unconfirmed", False),
                                          "created_at": int(time.time())}
                state = "quarantined"
            else:
                context["quarantine"] = None
                state = "released"
            return web.json_response({"browserID": browser_id, "state": state})
        held = self.state.lease
        if held is None:
            raise BrowserError(400, "INVALID_PARAM",
                               "no lease is held, so this leaseToken matches no holder")
        if held.get("browser_id") != browser_id:
            raise BrowserError(400, "INVALID_PARAM",
                               f"browserID names {browser_id!r} but the lease is held for "
                               f"{held.get('browser_id')!r}")
        if held.get("leaseToken") != token:
            raise BrowserError(400, "INVALID_PARAM",
                               f"leaseToken does not match the token held by {browser_id!r}")
        quarantine = self.state.release_lease(keep_locked=keep_locked)
        context = self._session_context(self.state.session_id)
        if context is not None:
            context["lease"] = None
            context["quarantine"] = self._copy_json(quarantine)
        state = "quarantined" if quarantine is not None else "released"
        LOGGER.info("task %s released its lease as %s", held.get("task_id"), state)
        return web.json_response({"browserID": browser_id, "state": state})

    @staticmethod
    def _digest_or_none(value: Any, name: str) -> str | None:
        if value is None:
            return None
        if not is_scene_digest(value):
            raise BrowserError(400, "INVALID_PARAM",
                               f"evidence.{name} must be 64 lowercase hex characters or null, got {value!r}")
        return str(value)

    def _uncertain_evidence(self, body: dict[str, Any]) -> dict[str, Any]:
        """The shape §8.3 pins: a receipt claim, two digests, and a late-write verdict.

        Every field is required because the six steps read them one by one; `null` is allowed only
        where the doc allows it -- a caller that never read the live scene cannot fill `live_digest`,
        and saying so with null is honest, while an absent key is a caller that did not answer.
        """
        evidence = body.get("evidence")
        if not isinstance(evidence, dict):
            raise BrowserError(400, "INVALID_PARAM",
                               f"evidence{{receipt_status, live_digest, expected_digest, "
                               f"late_write_excluded}} is required, got {evidence!r}")
        for name in ("receipt_status", "live_digest", "expected_digest", "late_write_excluded"):
            if name not in evidence:
                raise BrowserError(400, "INVALID_PARAM", f"evidence.{name} is required")
        receipt_status = evidence["receipt_status"]
        if receipt_status not in ("committed", "rejected", "missing"):
            raise BrowserError(400, "INVALID_PARAM",
                               f"evidence.receipt_status must be committed, rejected or missing, "
                               f"got {receipt_status!r}")
        late = evidence["late_write_excluded"]
        if not isinstance(late, bool):
            raise BrowserError(400, "INVALID_PARAM",
                               f"evidence.late_write_excluded must be a boolean, got {late!r}")
        return {"receipt_status": receipt_status,
                "live_digest": self._digest_or_none(evidence["live_digest"], "live_digest"),
                "expected_digest": self._digest_or_none(evidence["expected_digest"], "expected_digest"),
                "late_write_excluded": late}

    def _resolution_answer(self, state: str, session_id: str | None,
                           identity: dict[str, Any], reason: str) -> dict[str, Any]:
        """§8.3's response: the conclusion, the session it speaks for, and the 现场 we read.

        The four-tuple always rides along -- including on `kept_locked`, where what the caller needs
        most is the scene we could not certify. `sessionID` is null only on the `new_session`
        branch, because that conclusion closes the session it was asked about.
        """
        return {"state": state, "sessionID": session_id, **identity, "reason": reason}

    async def resolve_uncertain(self, request: web.Request) -> web.Response:
        body = await self._json_body(request, code="INVALID_PARAM")
        # Recovery is now a server-verified terminal-task operation. Caller booleans alone
        # cannot prove that a timed-out request will not arrive later.
        if "taskID" not in body:
            return web.json_response({"state": "kept_locked", "sessionID": body.get("sessionID"),
                                      "reason": "recovery_task_required: 请提供终态任务，由服务核验恢复证据。"})
        async with self._session_transition_lock:
            return await self._resolve_terminal_task(body)

    async def _resolve_terminal_task(self, body: dict[str, Any]) -> web.Response:
        session_id, browser_id, task_id = body.get("sessionID"), body.get("browserID"), body.get("taskID")
        def refused(reason):
            self.state.resolve_lock({"state": "kept_locked", "sessionID": session_id,
                "browserID": browser_id, "taskID": task_id, "reason": reason}, cleared=False)
            return web.json_response({"state": "kept_locked", "sessionID": session_id, "reason": reason})
        if (not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1
                or session_id != self.state.session_id or browser_id != self.state.browser_id):
            return refused("recovery_context_mismatch: 请先恢复目标会话的现场，不能解锁其他会话。")
        resolution = body.get("resolution", "resume")
        if resolution not in {"resume", "new_session"}:
            return refused("resolution_invalid")
        try:
            status, task = await self.agent_relay.request("GET", "/queryTaskState", params={"taskID": str(task_id)})
        except BrowserError:
            return refused("recovery_task_evidence_unavailable")
        if status != 200 or not isinstance(task, dict) or task.get("terminal") is not True:
            return refused("recovery_task_not_terminal")
        try:
            status, result = await self.agent_relay.request("GET", "/queryTaskResult", params={"taskID": str(task_id)})
        except BrowserError:
            return refused("recovery_task_evidence_unavailable")
        if status != 200 or not isinstance(result, dict) or result.get("sessionID") != session_id:
            return refused("recovery_task_session_mismatch")
        held = self.state.lease or self.state.quarantine
        if held and held.get("task_id") not in {None, task_id}:
            return refused("recovery_other_task_holds_lease")
        transaction = body.get("transactionID")
        if transaction is not None and not is_transaction_id(transaction):
            return refused("transaction_id_invalid")
        receipt = self.receipts.load(transaction) if transaction else None
        if receipt and receipt.get("request_fingerprint", {}).get("sessionID") != session_id:
            return refused("recovery_receipt_session_unverified")
        if (receipt and receipt["status"] not in {"committed", "rejected"}) or (held and held.get("commit_unconfirmed")):
            return refused("recovery_write_unconfirmed: 仍可能存在已发送但未确认的浏览器写入。")
        recovery = result.get("recovery_context") or {}
        if receipt is None and recovery.get("write_attempted") is not False:
            return refused("recovery_write_evidence_missing: 缺少权威的未发送写请求记录，不能凭超时解锁。")
        # Serialize against acquire/commit; fence BEFORE live read. A failed recovery retains
        # the fence and lock. Old requests can never reopen this terminal task's lease.
        key = f"{session_id}:{task_id}"
        self.state.raw["task_fences"][key] = {"taskID": task_id, "sessionID": session_id,
            "retired_lease_token": held.get("leaseToken") if held else None, "created_at": int(time.time())}
        self.state.save()
        try:
            payload = await self.dispatch("getWorld", {}, browser_id=browser_id)
            live = scene_identity(payload["world"])
        except (BrowserError, KeyError, ValueError):
            return refused("recovery_live_scene_unavailable")
        expected = ({k: receipt[k] for k in SCENE_IDENTITY_KEYS} if receipt else self.state.scene_identity)
        if live != expected or live.get("sceneDigest") != body.get("expected_digest"):
            return refused("recovery_scene_evidence_conflict")
        resolution_id = uuid.uuid4().hex
        record = {"state": "resolved", "resolutionID": resolution_id, "sessionID": session_id,
                  "browserID": browser_id, "taskID": task_id, "transactionID": transaction,
                  **live, "reason": "terminal_task_fenced_and_scene_verified"}
        record["retired"] = resolution == "new_session"
        self.state.resolve_lock(record, cleared=True, retire_session=record["retired"])
        self.state.raw["session_phase"] = "setup"
        self.state.save()
        context = self._session_context(session_id)
        if context is not None:
            context.update(lease=None, quarantine=None, session_phase="setup")
        return web.json_response(record)


    async def _traj_parts(self, request: web.Request) -> list[bytes]:
        """§5.3 (Q99): read the `trajs` parts in order, checking media type and size only.

        The multipart framing is the caller's job; this only refuses a body that is not
        multipart, a part named anything else (`traj`, singular, is the old wire format
        and the most likely caller mistake), an empty part, a body whose multipart
        structure cannot be parsed, and a batch that overruns max_payload_bytes in total.
        Every one of those is a 400/413 for the caller before the socket is touched.
        """
        limit = int(self.config["max_payload_bytes"])
        declared = request.content_length
        if declared is not None and declared > limit:
            raise BrowserError(413, "PAYLOAD_TOO_LARGE", f"{declared} bytes exceeds the {limit} byte limit")
        content_type = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "multipart/form-data":
            raise BrowserError(400, "INVALID_PARAM",
                               f"body must be multipart/form-data with {TRAJ_PART_FIELD} parts, "
                               f"got {content_type or 'none'!r}")
        try:
            reader = await request.multipart()
            parts: list[bytes] = []
            total = 0
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name != TRAJ_PART_FIELD:
                    raise BrowserError(400, "INVALID_PARAM",
                                       f"every trajectory part must be named {TRAJ_PART_FIELD!r}, "
                                       f"got {part.name!r}")
                data = await part.read(decode=False)
                if not data:
                    raise BrowserError(400, "INVALID_PARAM", f"{TRAJ_PART_FIELD}[{len(parts)}] is empty")
                total += len(data)
                if total > limit:
                    raise BrowserError(413, "PAYLOAD_TOO_LARGE", f"{total} bytes exceeds the {limit} byte limit")
                parts.append(data)
        except (ValueError, HttpProcessingError) as error:
            # A body that is not well-formed multipart surfaces two ways in aiohttp: the
            # boundary scan raises ValueError, the per-part header parse raises
            # HttpProcessingError. Both are the caller's mistake, so both are 400 here
            # rather than a 500 against the gateway (§5.13 INVALID_PARAM).
            raise BrowserError(400, "INVALID_PARAM", f"multipart body is unreadable: {error}") from error
        return parts

    def _traj_count(self, request: web.Request, actual: int) -> int:
        """§5.3: `?count=` is the caller's own tally of what it sent.

        Only the gateway sees both that number and the part count, so this comparison is
        here rather than in the browser (**Q34**'s "who sees what, judges it").
        """
        raw = request.query.get("count")
        if raw is None:
            return actual
        if not self._is_uint_text(raw) or int(raw) < 1:
            raise BrowserError(400, "INVALID_PARAM", f"count must be a positive uint32, got {raw!r}")
        if int(raw) != actual:
            raise BrowserError(400, "INVALID_PARAM",
                               f"count={raw} but the body holds {actual} {TRAJ_PART_FIELD} parts")
        return actual

    async def update_traj(self, request: web.Request) -> web.Response:
        """§5.3 (Q99): N clips arrive as N parts, ride the socket as one binary frame.

        The parts are concatenated in body order and their lengths announced in
        `byte_lengths`, which is what the browser slices by -- never a header read out of
        a clip, because a broken clip is exactly the case where those numbers lie. No
        base64 (unchanged from before): one clip is already allowed to be 100 MB.

        A trajectory is not a world change: no `expected_version`, no bumped
        `scene_version` and no `scene.applied`, because `dispatch` already made
        every browser apply it (§5.2).
        """
        parts = await self._traj_parts(request)
        count = self._traj_count(request, len(parts))
        payload = {"count": count, "byte_lengths": [len(part) for part in parts]}
        relayed = await self.dispatch("updateTraj", payload, binary=b"".join(parts))
        return web.json_response(relayed)

    def _require_brain_lease(self, request: web.Request, *, route: str) -> None:
        """§9.1's 租约闸: a collection read is only honest under the lock that froze the scene.

        The gate fires while the Agent owns the session or a lease stands, which is the same window
        §7.2 describes (租约期必然已在 `agent_owned` 内) and the only one a human-driven tour has
        no part in -- `setup` with no lease is the operator looking at their own scene, and refusing
        that capture would invent a lock nobody asked for. Inside the window the header must name
        the standing lease: after a `keepLocked` release the token is retired, so a late request
        carrying it is refused by exactly the check §8.3 step ③ calls 排除迟到写入 -- and the
        refusal dispatches nothing, which is what "并保锁" has to mean for a route that reads pixels.
        """
        lease = self.state.lease
        if self.state.session_phase != "agent_owned" and lease is None:
            return
        token = request.headers.get("X-Brain-Lease")
        held = lease.get("leaseToken") if lease else None
        if isinstance(token, str) and token and token == held:
            return
        detail = ("no lease stands for this session -- POST /acquireSceneLease first"
                  if held is None else
                  f"the header is {'absent' if not token else 'not the standing lease token'}")
        raise BrowserError(409, "scene_lease_required",
                           f"{route} under an Agent-owned session needs X-Brain-Lease ({detail}); "
                           f"the scene stays locked (§9.1)")

    def _frame_basis(self) -> dict[str, Any] | None:
        """§9.1's 比对基准: the four-tuple the lock was taken out over, else our own last reading.

        Under a lease that is the fingerprint V1 stored, which came from V0's *live* read -- never
        from a cached world, because §9.1 refuses to compare a frame against a reading that may be
        stale. With no lease -- or a lease record written before V1 kept a fingerprint -- the same
        question is still answerable: the state file's four-tuple is what the last adopted document
        said, and that is the only 现场 this gateway can describe.
        """
        lease = self.state.lease
        if isinstance(lease, dict) and isinstance(lease.get("scene_identity"), dict):
            return lease["scene_identity"]
        return self.state.scene_identity

    def _require_frame_identity(self, payload: dict[str, Any], *, route: str) -> None:
        """§9.1's 身份闸: this frame has to say which 现场 it is, and the same one we are holding.

        One field at a time, and the first disagreement names both sides. The browser reports the
        six beside its pixels (§9's "缺项或旧值即拒绝"), so a missing key is a refusal rather than a
        pass -- a frame that does not claim a scene cannot be matched to one. `playback_state` is
        checked last because it is the browser's own word about its play bar and the only one of the
        six the gateway cannot recompute; a moving scene has no honest digest to compare to.
        """
        basis = self._frame_basis()
        if not isinstance(basis, dict):
            raise BrowserError(400, "camera_frame_scene_mismatch",
                               f"{route} cannot be matched to a 现场: this gateway holds no reading to "
                               f"compare against (read GET /getWorld live, then acquire §9.1's V1)")
        bound = self.state.browser_id
        if payload.get("browserID") != bound:
            raise BrowserError(400, "camera_frame_scene_mismatch",
                               f"{route} was answered by {payload.get('browserID')!r} but the bound "
                               f"browser is {bound!r} -- a frame from another tab is not a frame of "
                               f"this 现场")
        for key in SCENE_IDENTITY_KEYS:
            if payload.get(key) != basis[key]:
                raise BrowserError(400, "camera_frame_scene_mismatch",
                                   f"{route} reports {key}={payload.get(key)!r} against the "
                                   f"{basis[key]!r} this gateway holds; 缺项或旧值即拒绝 (§9)")
        if payload.get("playback_state") != "stopped":
            raise BrowserError(400, "camera_frame_scene_mismatch",
                               f"{route} needs a stopped scene but the browser was at "
                               f"{payload.get('playback_state')!r}; a frame of a moving scene cannot "
                               f"be evidence (§9.1's V2)")

    def _require_frame_size(self, payload: dict[str, Any], *, route: str) -> None:
        """§9's 尺寸不符 for the answer itself: a frame and a box both have to declare one."""
        for name in ("width", "height"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_UINT32:
                raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                                   f"{route} answered {name}={value!r}; a uint32 above zero is required")

    async def capture_scene(self, request: web.Request) -> web.Response:
        """§4.4/§5.9: the browser renders, the gateway relays -- and now checks what it was told (§9).

        The gateway still cannot tell a good frame from a blank one, so `CAMERA_TYPE_MISMATCH` and
        friends stay the browser's call: only it knows what that entity's `Camera` says. What §9
        moved onto this side is the frame's *account* of the 现场: the identity block is compared
        against the reading the lock was taken out over, and the png is asked for its own size --
        "播放中、旧缓存、错浏览器、尺寸不符、PNG 不完整一律拒绝", all five from the response alone.
        """
        object_id = self._uint_query(request, "id")
        kind = request.query.get("type")
        if kind is None:
            raise BrowserError(400, "MISSING_PARAM", "type is required")
        if kind not in ("rgb", "rgbd"):
            raise BrowserError(400, "INVALID_PARAM", f"type must be rgb or rgbd, got {kind!r}")
        if self._background_context(request) is not None:
            raise BrowserError(409, "session_browser_not_active",
                               "the requested session is not the scene currently displayed by the browser")
        # §9.1's 参数闸 runs before the 租约闸, so a caller with a broken parameter is told about
        # the parameter and not about a lock it may never have needed.
        self._require_brain_lease(request, route="/captureScene")
        # §9.1: `?browser_id=` with no value means "the bound browser", not "any browser" --
        # the binding is on disk now, so a capture has one addressable subject. The id and the
        # lease ride into the payload: the response has to name the browser that rendered
        # (§9's identity fields), and the viewer only freezes its light preset while it holds
        # a lease, which it can only know from this frame (§7.2's third lock).
        browser_id = request.query.get("browser_id") or self.state.browser_id
        body = {"id": object_id, "type": kind, "browser_id": browser_id,
                "lease_token": (self.state.lease or {}).get("leaseToken")}
        payload = await self.dispatch("captureScene", body, browser_id=browser_id)
        if not isinstance(payload.get("image_base64"), str) or not payload["image_base64"]:
            raise BrowserError(502, "GATEWAY_FRAMING_ERROR", "browser answered captureScene without an image")
        self._require_frame_identity(payload, route="/captureScene")
        self._require_frame_size(payload, route="/captureScene")
        image = decode_frame_bytes(payload.get("image_base64"))
        declared = png_frame_size(image) if image is not None else None
        if declared is None:
            raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                               f"captureScene answered with bytes that are not a complete png "
                               f"(image_base64={'missing' if image is None else 'truncated or not a png'})")
        if declared != (payload["width"], payload["height"]):
            raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                               f"the png is {declared[0]}x{declared[1]} but the frame reported "
                               f"{payload['width']}x{payload['height']} (§9's 尺寸不符)")
        if kind == "rgbd":
            depth = decode_frame_bytes(payload.get("depth_base64"))
            wanted = payload["width"] * payload["height"] * 4
            if depth is None or len(depth) != wanted:
                raise BrowserError(502, "GATEWAY_FRAMING_ERROR",
                                   f"rgbd depth must be {wanted} little-endian float32 bytes "
                                   f"(width*height*4), got {0 if depth is None else len(depth)}")
        return web.json_response(payload)

    async def get_2d_aabb(self, request: web.Request) -> web.Response:
        """§4.5/§5.9: the browser measures, the gateway relays (§5.11 body).

        Which object has renderable geometry, and what a corner behind the camera
        means, are scene-graph questions; `EMPTY_AABB` and `OBJECT_NOT_FOUND` are the
        browser's call. What this side does answer for is §9.1's identity: a box is one
        observation of one 现场 exactly as a frame is, so it carries the same six fields
        and meets the same gate.
        """
        object_id = self._uint_query(request, "id")
        camera_id = self._uint_query(request, "camera_id")
        if self._background_context(request) is not None:
            raise BrowserError(409, "session_browser_not_active",
                               "the requested session is not the scene currently displayed by the browser")
        self._require_brain_lease(request, route="/get2DAABB")
        browser_id = request.query.get("browser_id") or self.state.browser_id
        body = {"id": object_id, "camera_id": camera_id, "browser_id": browser_id,
                "lease_token": (self.state.lease or {}).get("leaseToken")}
        payload = await self.dispatch("get2DAABB", body, browser_id=browser_id)
        if not isinstance(payload.get("visible"), bool):
            raise BrowserError(502, "GATEWAY_FRAMING_ERROR", "browser answered get2DAABB without a visibility flag")
        self._require_frame_identity(payload, route="/get2DAABB")
        self._require_frame_size(payload, route="/get2DAABB")
        return web.json_response(payload)

    async def _write(self, action: str, payload: dict[str, Any], request: web.Request) -> web.Response:
        """Relay one write to the bound browser, then announce the scene that landed (§5.4)."""
        expected = self._uint_query(request, "expected_version", required=False)
        if expected is not None:
            payload["expected_version"] = expected
        outcome = await self.dispatch_with_source(action, payload)
        version = outcome.payload.get("scene_version")
        if isinstance(version, int) and version >= 0:
            self.scene_version = version
        identity = await self._identity_after_write(action, payload, outcome)
        await self.broadcast(self._scene_applied_frame(identity, action=action,
                                                      source_request_id=outcome.request_id))
        return web.json_response(outcome.payload)

    async def _identity_after_write(self, action: str, sent: dict[str, Any],
                                    outcome: WriteOutcome) -> dict[str, Any]:
        """§5.4: what the announcement can honestly say after one write landed.

        The version pair comes from the browser's own reply, and `scene_sub_version` is `0` because
        a write is the Agent's route -- an Agent write-down resets it (spec §3.9). The digest is
        `null` on purpose: the gateway has not read a document, and §5.1/§3 forbid pairing a stored
        digest with a version that did not come from it. Re-reading the world here instead would
        move up to 100 MB twice per write (§11.1) to announce something every reader is required to
        re-read anyway (Q111: the frame means "go re-read", not "here is the world").

        What the write does cost the gateway is one water-mark raise: §6's event ① -- an object the
        Agent just created exists whether or not anybody read the document it sits in, and an id
        that never reaches the持久水位 is an id `/allocateObjectID` will hand out twice.
        """
        reported = outcome.payload
        ids = object_ids_in_document(sent.get("world")) if action == "updateWorld" else []
        candidate = reported.get("object_id_in_scene")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            ids = ids + [candidate]
        if ids:
            self.state.raise_water_mark(ids)
        stored = self.state.scene_identity or {}
        identity = {key: stored.get(key) for key in SCENE_IDENTITY_KEYS}
        for key in ("scene_id", "scene_version"):
            value = reported.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                identity[key] = value
        identity["scene_sub_version"] = 0
        identity["sceneDigest"] = None
        return identity

    async def get_world(self, request: web.Request) -> web.Response:
        """§5.8/§5.9: read the world back, live from the bound browser -- live first, always.

        §5.1's headers are the point of this route: an integer header, a digest and a body have to
        come out of the *same* read. There is no cache branch for a caller (流程文档 §5.1,
        2026-11-05): a browser report that arrives late can move the persisted identity onto an
        older scene (换场景是无条件采纳，§1.2 第 ④ 条), and the cache would then answer the reader
        that is looking at something else with that older scene. Q160's `_reclaimed_world` is the
        one exception, and its reader is the bound browser itself.
        """
        background = self._background_context(request)
        if background is not None:
            session_id, context = background
            document = context.get("world_snapshot")
            if not isinstance(document, dict):
                raise BrowserError(409, "session_browser_not_active",
                                   f"session {session_id} is not the active browser scene")
            return self._world_response(self._copy_json(document),
                                        identity=context.get("scene_identity") or scene_identity(document),
                                        browser_id=self.state.browser_id, playback_state="stopped")
        current_context = self._session_context(self.state.session_id)
        if (current_context is not None and isinstance(current_context.get("world_snapshot"), dict)
                and current_context.get("scene_identity") == self.state.scene_identity):
            document = self._copy_json(current_context["world_snapshot"])
            return self._world_response(document,
                                        identity=current_context["scene_identity"],
                                        browser_id=self.state.browser_id, playback_state="stopped")
        # The read is addressed to the bound browser unless the caller names one (§5.4: a viewer
        # that just adopted a write is a legitimate thing to ask). Absent or silent is an answer in
        # itself -- 503/504 travel to the caller, which is what "the scene has no authority right
        # now" means; nothing here pretends a stored document is one.
        browser_id = request.query.get("browser_id") or self.state.browser_id
        use_lease_snapshot = request.query.get("lease_snapshot") == "1"
        try:
            read = await self._read_live_world(browser_id, use_lease_snapshot=use_lease_snapshot)
        except BrowserError as error:
            snapshot = self._reclaimed_world(request, browser_id, error)
            if snapshot is None:
                raise
            return snapshot
        return self._world_response(read.document, identity=read.identity,
                                    browser_id=browser_id, playback_state=read.playback_state)

    def _reclaimed_world(self, request: web.Request, browser_id: str | None,
                         error: BrowserError) -> web.Response | None:
        """§5.8 (Q160): the one reader a 503 must not silence -- the bound browser itself.

        `start()` reads the authoritative world *before* it opens its socket, so that read always
        finds no browser connected. Q111 removed the cache behind it, which left a refresh with an
        empty world that it then reported, and an unconditional scene switch adopted that empty
        world over the scene on disk. The document handed back here is the one this very browser
        last reported, so there is no other 现场 it could be standing in for.

        Every guard is a way for this to stay 503: a caller carries no `X-WebEditor-Browser-ID`, a
        browser that is connected but silent is a 504, and a cache whose four-tuple has drifted from
        the persisted identity describes something nobody agreed to.
        """
        if error.status != 503 or error.code != "NO_BROWSER_CONNECTED":
            return None
        if browser_id is None or browser_id != self.state.browser_id:
            return None
        if request.headers.get("X-WebEditor-Browser-ID") != browser_id:
            return None
        document = self.world_cache
        if not isinstance(document, dict):
            return None
        identity = scene_identity(document)
        if identity != self.state.scene_identity:
            return None
        return self._world_response(self._copy_json(document), identity=identity,
                                    browser_id=browser_id, playback_state="stopped",
                                    source="snapshot")

    async def static(self, request: web.Request) -> web.StreamResponse:
        # §11.7 (Q95): a release bundle serves the built SPA. Real files under dist/
        # win, the shell answers everything else, and nothing may resolve outside
        # dist/ -- otherwise this route would hand out config/ and features.json.
        dist = (ROOT / "dist").resolve()
        shell = dist / "index.html"
        if not shell.is_file():
            return web.Response(status=404, text="dist/index.html is missing; run `npm run build`")
        candidate = (dist / request.match_info.get("route", "")).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return web.FileResponse(candidate)
        return web.FileResponse(shell)


def create_app(config: dict[str, Any] | None = None, *,
               state_path: Path | str | None = None) -> web.Application:
    gateway = StandaloneGateway(config=config, state_path=state_path)
    # §11.1: aiohttp would otherwise cap every body at 1 MiB, so the configured
    # max_payload_bytes -- and the 12 KB-plus clips a /updateTraj batch is made of --
    # could never be reached. The handler still re-checks the limit itself.
    app = web.Application(client_max_size=int(gateway.config["max_payload_bytes"]))
    app[GATEWAY_APP_KEY] = gateway
    # Select the browser profile before every stateful HTTP handler. Health, runtime config and
    # the WebSocket handshake are intentionally profile-neutral; /events selects its profile only
    # after the first browser.hello frame has been validated.
    app.middlewares.append(gateway.profile_middleware)
    app.router.add_get("/api/health", gateway.health)
    # §5.3 (Q115): both health passthroughs, registered before the catch-all below (坑 2) --
    # as static-file routes they would answer the browser's index.html with a 200.
    app.router.add_get("/health", gateway.agent_health)
    app.router.add_get("/api/agent/health", gateway.agent_health_cached)
    # Registered before the catch-all static route below, otherwise it is swallowed by it.
    app.router.add_get("/api/runtimeConfig", gateway.runtime_config)
    # §5: the refreshed browser reads its phase here; registered before the catch-all (坑 2).
    app.router.add_get("/api/sessionState", gateway.session_state)
    app.router.add_post("/api/agent/recover", gateway.recover_agent_session)
    # §4/§5: the session door -- GET reads the binding, POST creates/resumes/switches it.
    app.router.add_get("/sessions", gateway.sessions)
    app.router.add_post("/sessions", gateway.sessions)
    # §5.2: the eight capability names, answered by the gateway that promises them.
    app.router.add_get("/brainCapabilities", gateway.brain_capabilities)
    # §6: Brain's only id allocation path, and the water mark it advances is on disk.
    app.router.add_get("/allocateObjectID", gateway.allocate_object_id)
    app.router.add_get("/getWorld", gateway.get_world)
    # Caller render routes (§4.4/§4.5): GET, because the caller only reads pixels.
    app.router.add_get("/captureScene", gateway.capture_scene)
    app.router.add_get("/get2DAABB", gateway.get_2d_aabb)
    # Caller write routes (§4.2/§5.9): relay-only, the browser stays the scene authority.
    app.router.add_post("/updateWorld", gateway.update_world)
    app.router.add_post("/updateItem", gateway.update_item)
    app.router.add_post("/addObject", gateway.add_object)
    app.router.add_post("/removeObject", gateway.remove_object)
    # §8.1: the transaction pair. `/commitWorld` is the only route whose answer is a record on our
    # disk rather than a browser's reply, and `/queryWorldCommit` reads that record with no browser
    # in the loop at all -- both must be registered before the catch-all below (坑 2).
    app.router.add_post("/commitWorld", gateway.commit_world)
    app.router.add_get("/queryWorldCommit", gateway.query_world_commit)
    # §7.2/§8.3: the lock and its one way out. All three speak for the persisted state, not for a
    # browser, so none of them may be swallowed by the catch-all below (坑 2).
    app.router.add_post("/acquireSceneLease", gateway.acquire_scene_lease)
    app.router.add_post("/releaseSceneLease", gateway.release_scene_lease)
    app.router.add_post("/resolveUncertain", gateway.resolve_uncertain)
    app.router.add_post("/updateTraj", gateway.update_traj)
    app.router.add_get("/api/agent/conversations", gateway.conversations_list)
    app.router.add_post("/api/agent/conversations", gateway.conversations_create)
    app.router.add_post("/api/agent/conversations/{conversationId}/messages", gateway.conversation_messages)
    app.router.add_get("/api/agent/conversations/{conversationId}", gateway.conversation)
    app.router.add_delete("/api/agent/conversations/{conversationId}", gateway.conversation)
    app.router.add_post("/api/agent/conversations/{conversationId}/activate", gateway.conversation)
    app.router.add_get("/api/agent/jobs", gateway.jobs)
    app.router.add_post("/api/agent/jobs", gateway.jobs)
    app.router.add_get("/api/agent/jobs/{jobId}", gateway.job)
    # §5.15 (E2): the artifact whitelist proxy, outside /api/agent because Brain names the path.
    # Registered before the catch-all below, or the SPA shell would answer it with index.html (坑 2).
    app.router.add_get("/tasks/{taskID}/artifacts/{name}", gateway.task_artifact)
    # §5.15/§10.1: no cancel route -- the Agent service has no cancel interface.
    speech = SpeechRelay(gateway.config)
    app.router.add_get("/speech", speech.handle)
    app.on_shutdown.append(speech.cleanup)
    app.router.add_get("/events", gateway.events)
    app.router.add_get("/", gateway.static)
    app.router.add_get("/{route:.*}", gateway.static)
    # §5.15: one poller per process, driven by asyncio.sleep so it never blocks the
    # event loop (坑 10).
    app.on_startup.append(gateway.start_agent_poller)
    app.on_cleanup.append(gateway.stop_agent_poller)
    return app


def _version_note() -> str:
    # §11.6: pack.py stamps VERSION.txt into the bundle, so its presence is what tells
    # a release apart from a source checkout. Startup prints one or the other.
    try:
        first = (ROOT / "VERSION.txt").read_text("utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return "unversioned checkout"
    return first or "unversioned checkout"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Standalone WebEditor local gateway")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    # Two gateways on one machine (8099 and the disposable 8098 stack) must not share a binding:
    # the second would read the first's browser as its own authority.
    parser.add_argument("--state-path", default=None)
    # The real server wipes the previous round's state on the way in and starts clean (see
    # clear_persisted_state). This flag opts out: keep the on-disk state to resume a binding or to
    # read the last run through the live routes instead of the file.
    parser.add_argument("--keep-state", action="store_true",
                        help="do not wipe the previous round's persisted state at startup")
    args = parser.parse_args()
    try:
        config = load_constant_config(args.config)
    except ConfigError as error:
        LOGGER.error("%s", error)
        raise SystemExit(1) from error
    # Command line wins over the config file (same rule as ObjectLibraryServer).
    host = args.host if args.host is not None else str(config["gateway"]["host"])
    port = args.port if args.port is not None else int(config["gateway"]["port"])
    LOGGER.info("WebEditor gateway (%s) on http://%s:%s", _version_note(), host, port)
    # §5/§6: the real server always names a file; only a caller that decides otherwise gets
    # an ephemeral state. `ROOT` is read here, not at import, so a bundled gateway keeps the
    # state next to itself.
    state_path = Path(args.state_path) if args.state_path else ROOT / "var" / "gateway_state.json"
    # Clearing is a *startup* concern, so it lives here in the process entry -- not in
    # GatewayState/StandaloneGateway, which keep reading so recovery and the after-exit
    # troubleshooting window still work. The finished run's state is left on disk for whoever has
    # to read it; this sweep is what returns the workspace to a clean slate before we serve.
    if args.keep_state:
        LOGGER.info("persistent state: %s (--keep-state: carrying over the previous round)", state_path)
    else:
        removed = clear_persisted_state(state_path)
        if removed:
            LOGGER.info("cleared previous round's state in %s: %s", state_path.parent, ", ".join(removed))
        else:
            LOGGER.info("persistent state: %s (already clean)", state_path)
    web.run_app(create_app(config, state_path=state_path), host=host, port=port)


if __name__ == "__main__":
    main()
