"""Callbacks into the Gateway's `/api/agent-control/*` surface.

Every route here is authenticated with the per-job control token the Gateway put into
`controlContext`; it is never logged and never echoed into a user-visible message.

This module also owns the trajectory presentation. The editor-only `scene.draw_trajectories`
RPC is not reachable from a Live session, so a path is shown instead by injecting visual-only
marker entities into the snapshot frames we publish. The Gateway rebuilds browser entities from
whatever entity list a frame carries (`web/gateway/server.py::presentation_frame`), and the
browser renders any entity generically from `visual.shape`, so markers need no frontend change
and never touch physics, collision or the authoritative world.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import dataclass, field
from itertools import chain
from typing import Any, Iterable, Sequence

import aiohttp

# Marker entity ids live far above anything the simulator allocates so they cannot collide with
# a real entity id in the same frame.
MARKER_ID_BASE = 900_000_000
DEFAULT_MARKER_RADIUS = 0.008
# Okabe-Ito, colourblind safe. Matches the reference implementation's segment palette.
PALETTE: tuple[tuple[float, float, float], ...] = (
    (0.902, 0.624, 0.000), (0.337, 0.706, 0.914), (0.000, 0.620, 0.451),
    (0.941, 0.894, 0.259), (0.000, 0.447, 0.698), (0.835, 0.369, 0.000),
    (0.800, 0.475, 0.655), (0.400, 0.400, 0.400),
)


def segment_color(index: int) -> tuple[float, float, float]:
    return PALETTE[index % len(PALETTE)]


@dataclass
class TrajectorySegment:
    """One planned path segment, as plain data produced by the runtime layer."""

    id: str
    points: list[tuple[float, float, float]]
    color: tuple[float, float, float] | None = None
    radius: float = DEFAULT_MARKER_RADIUS
    label: str | None = None


@dataclass
class GatewayError(RuntimeError):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _identity_transform() -> dict[str, list[float]]:
    return {"translation": [0.0, 0.0, 0.0], "rotationWxyz": [1.0, 0.0, 0.0, 0.0],
            "scale": [1.0, 1.0, 1.0]}


def marker_entities(segments: Sequence[TrajectorySegment], *,
                    id_base: int = MARKER_ID_BASE) -> list[dict[str, Any]]:
    """Visual-only sphere entities tracing each segment, one per sampled waypoint.

    Deliberately carries no collider and no rigidBody: a drawn path must never obstruct the arm
    nor fall under gravity. Segments are told apart by colour, and each marker keeps the segment
    id in its name so the hierarchy stays readable.
    """
    entities: list[dict[str, Any]] = []
    next_id = id_base
    for index, segment in enumerate(segments):
        color = segment.color or segment_color(index)
        radius = max(1e-4, float(segment.radius))
        rgba = [float(color[0]), float(color[1]), float(color[2]), 1.0]
        for point_index, point in enumerate(segment.points):
            translation = [float(point[0]), float(point[1]), float(point[2])]
            transform = {"translation": translation,
                         "rotationWxyz": [1.0, 0.0, 0.0, 0.0],
                         "scale": [1.0, 1.0, 1.0]}
            entities.append({
                "id": next_id,
                "name": f"__trajectory_{segment.id}_{point_index}",
                "parent": None,
                "local": transform,
                "world": copy.deepcopy(transform),
                "visual": {
                    "visible": True, "minLod": 0, "maxLod": 0, "shape": "sphere",
                    # A unit sphere scaled by the marker radius, matching the editor's own
                    # trajectory markers.
                    "size": [radius, radius, radius],
                    "color": rgba, "resource": "",
                },
            })
            next_id += 1
    return entities


def with_trajectories(frame: dict[str, Any], segments: Sequence[TrajectorySegment], *,
                      id_base: int = MARKER_ID_BASE) -> dict[str, Any]:
    """Copy of `frame` with trajectory markers appended to its entity list."""
    if not segments:
        return frame
    result = dict(frame)
    source_entities = frame.get("entities")
    entities = list(source_entities) if isinstance(source_entities, list) else None
    if not isinstance(entities, list):
        raise GatewayError("INVALID_FRAME", "snapshot frame has no entity list")
    result["entities"] = entities
    entities.extend(marker_entities(segments, id_base=id_base))
    return result


@dataclass
class GatewayClient:
    """Typed async client for the agent-control callback surface."""

    base_url: str
    control_token: str
    job_id: str
    timeout: float = 60.0
    # aiohttp rather than a second HTTP library: the service already requires it for its own
    # HTTP surface, so this adds no dependency.
    _session: "aiohttp.ClientSession | None" = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> "GatewayClient":
        self._session = aiohttp.ClientSession(
            base_url=self.base_url.rstrip("/"),
            json_serialize=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Agent-Job-Id": self.job_id, "X-Agent-Control-Token": self.control_token}

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            raise GatewayError("NOT_CONNECTED", "gateway client is not open")
        async with self._session.post(path, json=payload or {},
                                      headers=self.headers) as response:
            body_text = await response.text()
            if response.status >= 400:
                code, message = "GATEWAY_ERROR", body_text
                try:
                    body = json.loads(body_text) if body_text else None
                    error = body.get("error") if isinstance(body, dict) else None
                    if isinstance(error, dict):
                        code = str(error.get("code", code))
                        message = str(error.get("message", message))
                except json.JSONDecodeError:
                    pass
                raise GatewayError(code, message)
            if response.status == 204 or not body_text:
                return None
            return json.loads(body_text)

    async def resolve_asset(self, asset: str) -> dict[str, Any]:
        """Asset paths must come from the Gateway, never from a local sample path."""
        return await self._post("/api/agent-control/assets/resolve", {"asset": asset})

    async def publish_snapshot(self) -> dict[str, Any]:
        """Replace the static projection with the authoritative world's current state.

        By contract this publishes what the bound world holds right now; the Gateway reads it
        itself and ignores any snapshot in the request body. So a static publish cannot carry
        trajectory markers — those ride the timeline path, which does accept supplied frames.
        """
        return await self._post("/api/agent-control/presentation/snapshot", {})

    async def publish_timeline(self, frames: Sequence[dict[str, Any]]) -> dict[str, Any]:
        if len(frames) < 2:
            raise GatewayError("INVALID_TIMELINE", "a timeline needs at least two frames")
        return await self._post("/api/agent-control/presentation/timeline",
                                {"frames": list(frames)})

    async def capture_start(self) -> str:
        result = await self._post("/api/agent-control/captures", {})
        if not isinstance(result, dict):
            raise GatewayError("INVALID_CAPTURE", "gateway returned no capture")
        return str(result["captureId"])

    async def capture_frame(self, capture_id: str, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"/api/agent-control/captures/{capture_id}/frames",
                                {"snapshot": frame})

    async def capture_finish(self, capture_id: str, *, publish: bool = True) -> dict[str, Any]:
        return await self._post(f"/api/agent-control/captures/{capture_id}/finish",
                                {"publish": bool(publish)})

    async def capture_abort(self, capture_id: str) -> dict[str, Any]:
        return await self._post(f"/api/agent-control/captures/{capture_id}/abort")

    async def publish_capture(self, frames: Iterable[dict[str, Any]], *,
                              segments: Sequence[TrajectorySegment] = ()) -> dict[str, Any]:
        """Publish a static base once and all sampled transforms in one atomic request."""
        started = time.perf_counter()
        iterator = iter(frames)
        first = next(iterator, None)
        if first is None:
            raise GatewayError("INVALID_TIMELINE", "a timeline needs at least two frames")
        base = with_trajectories(first, segments)
        base_entities = {entity["id"]: entity for entity in first["entities"]}
        compact = []
        for frame in chain((first,), iterator):
            value = {key: frame[key] for key in (
                "schemaVersion", "sceneId", "worldId", "sceneGeneration", "revision",
                "simulationTime", "status") if key in frame}
            transforms = []
            if {entity["id"] for entity in frame["entities"]} != base_entities.keys():
                raise GatewayError("INVALID_TIMELINE", "timeline entity set differs from base snapshot")
            for entity in frame["entities"]:
                original = base_entities.get(entity["id"])
                if original is None:
                    raise GatewayError("INVALID_TIMELINE", "timeline entity is absent from base snapshot")
                changes = {key: entity[key] for key in ("parent", "local", "world")
                           if key in entity and entity[key] != original.get(key)}
                if changes:
                    transforms.append({"id": entity["id"], **changes})
            value["entities"] = transforms
            compact.append(value)
        if len(compact) < 2:
            raise GatewayError("INVALID_TIMELINE", "a timeline needs at least two frames")
        result = await self._post("/api/agent-control/presentation/timeline",
                                  {"baseSnapshot": base, "frames": compact})
        logging.getLogger(__name__).debug(
            "Sparse compact timeline published frames=%d entities=%d elapsed_seconds=%.3f",
            len(compact), len(base["entities"]), time.perf_counter() - started)
        return result


def normalize_frame_times(frames: Sequence[dict[str, Any]], *,
                          fps: float = 50.0) -> list[dict[str, Any]]:
    """Force strictly increasing simulationTime across frames.

    The Gateway rejects a capture frame whose time is not finite and strictly greater than the
    previous one. Kinematic replay writes joint targets without stepping the clock, so several
    frames can share a simulation time; synthesise a monotonic timeline in that case rather than
    dropping frames.
    """
    step = 1.0 / max(1e-6, float(fps))
    result: list[dict[str, Any]] = []
    previous: float | None = None
    for index, frame in enumerate(frames):
        # Only the timestamp changes; captured entities remain read-only throughout
        # publication. Copying their meshes/materials here repeats work per frame.
        value = dict(frame)
        try:
            current = float(value.get("simulationTime", 0.0))
        except (TypeError, ValueError):
            current = 0.0
        if previous is not None and not current > previous:
            current = previous + step
        if current != current or current in (float("inf"), float("-inf")):
            current = (previous or 0.0) + step
        value["simulationTime"] = current
        previous = current
        result.append(value)
    return result
