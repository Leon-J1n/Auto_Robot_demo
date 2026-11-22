"""Transport-level tests for the agent-control callback client.

`test_publish_integration.py` drives the real Gateway endpoints directly; this covers the client
headers, error translation, and atomic compact uploads preserving every sampled frame.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.gateway_client import (GatewayClient, GatewayError,  # noqa: E402
                                          TrajectorySegment)


class FakeGateway:
    """Minimal stand-in for the Gateway's `/api/agent-control/*` surface."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.frames: list[dict[str, Any]] = []
        self.aborted = False
        self.finished = False
        self.fail_on_frame: int | None = None
        self.app = web.Application()
        self.app.router.add_post("/api/agent-control/presentation/snapshot", self._snapshot)
        self.app.router.add_post("/api/agent-control/presentation/timeline", self._timeline)
        self.app.router.add_post("/api/agent-control/assets/resolve", self._asset)
        self.app.router.add_post("/api/agent-control/captures", self._start)
        self.app.router.add_post("/api/agent-control/captures/{id}/frames", self._frame)
        self.app.router.add_post("/api/agent-control/captures/{id}/finish", self._finish)
        self.app.router.add_post("/api/agent-control/captures/{id}/abort", self._abort)
        self.app.router.add_post("/api/agent-control/empty", self._empty)

    async def _record(self, request: web.Request) -> dict[str, Any]:
        body = await request.json()
        self.requests.append((request.path, body, dict(request.headers)))
        return body

    async def _snapshot(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"presentationRevision": 7, "mode": "sync-and-animate"})

    async def _timeline(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        return web.json_response({"presentationRevision": 8,
                                  "timeline": {"frameCount": len(body.get("frames", []))}})

    async def _asset(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"assetId": "builtin:cube", "path": "C:/assets/cube.obj"})

    async def _start(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"captureId": "cap-1"}, status=201)

    async def _frame(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        if self.fail_on_frame is not None and len(self.frames) == self.fail_on_frame:
            return web.json_response(
                {"error": {"code": "CAPTURE_LIMIT", "message": "byte limit reached"}},
                status=409)
        self.frames.append(body["snapshot"])
        return web.json_response({"captureId": "cap-1", "frameCount": len(self.frames)})

    async def _finish(self, request: web.Request) -> web.Response:
        await self._record(request)
        self.finished = True
        return web.json_response({"presentationRevision": 9})

    async def _abort(self, request: web.Request) -> web.Response:
        await self._record(request)
        self.aborted = True
        return web.json_response({"aborted": True})

    async def _empty(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.Response(status=204)


async def _with_client(gateway: FakeGateway, body) -> Any:
    server = TestServer(gateway.app)
    await server.start_server()
    try:
        base = f"http://127.0.0.1:{server.port}"
        async with GatewayClient(base_url=base, control_token="tok-1",
                                 job_id="job-1") as client:
            return await body(client)
    finally:
        await server.close()


def _frame(index: int) -> dict[str, Any]:
    return {"schemaVersion": "1.0", "sceneId": "main", "worldId": 1, "sceneGeneration": 0,
            "revision": index, "simulationTime": float(index), "status": "paused",
            "entities": [{"id": 1, "name": "cube", "parent": None, "local": {}, "world": {}}]}


def test_every_request_carries_the_control_headers() -> None:
    gateway = FakeGateway()

    async def body(client: GatewayClient) -> None:
        await client.publish_snapshot()
        await client.resolve_asset("builtin:cube")

    asyncio.run(_with_client(gateway, body))
    assert len(gateway.requests) == 2
    for _, _, headers in gateway.requests:
        # The per-job token authenticates every publish; the browser never sends these.
        assert headers["X-Agent-Job-Id"] == "job-1"
        assert headers["X-Agent-Control-Token"] == "tok-1"


def test_static_publish_sends_no_snapshot() -> None:
    gateway = FakeGateway()
    result = asyncio.run(_with_client(gateway, lambda c: c.publish_snapshot()))
    assert result["presentationRevision"] == 7
    # The Gateway reads the authoritative world itself, so a body would be ignored anyway.
    assert gateway.requests[0][1] == {}


def test_error_body_becomes_a_structured_gateway_error() -> None:
    gateway = FakeGateway()
    gateway.fail_on_frame = 0

    async def body(client: GatewayClient) -> None:
        capture = await client.capture_start()
        await client.capture_frame(capture, _frame(0))

    with pytest.raises(GatewayError) as error:
        asyncio.run(_with_client(gateway, body))
    assert error.value.code == "CAPTURE_LIMIT"
    assert "byte limit" in error.value.message


def test_capture_submits_every_frame_in_order_then_publishes() -> None:
    gateway = FakeGateway()
    frames = [_frame(index) for index in range(5)]
    result = asyncio.run(_with_client(gateway, lambda c: c.publish_capture(frames)))
    assert result["presentationRevision"] == 8
    assert len(gateway.requests) == 1
    published = gateway.requests[0][1]["frames"]
    # Sampling is forbidden for a sequence that will be published.
    assert len(published) == 5
    assert [frame["revision"] for frame in published] == [0, 1, 2, 3, 4]


def test_capture_sends_markers_once_in_static_base() -> None:
    gateway = FakeGateway()
    segments = [TrajectorySegment(id="segment_1",
                                 points=[(0.0, 0.0, 0.0), (0.1, 0.0, 0.0)])]
    asyncio.run(_with_client(
        gateway, lambda c: c.publish_capture([_frame(0), _frame(1)], segments=segments)))
    payload = gateway.requests[0][1]
    markers = [entity for entity in payload["baseSnapshot"]["entities"]
               if str(entity["name"]).startswith("__trajectory_")]
    assert len(markers) == 2
    assert all(frame["entities"] == [] for frame in payload["frames"])


def test_invalid_capture_does_not_publish_partial_frames() -> None:
    gateway = FakeGateway()
    frames = [_frame(index) for index in range(5)]
    frames[2]["entities"][0]["id"] = 99
    with pytest.raises(GatewayError):
        asyncio.run(_with_client(gateway, lambda c: c.publish_capture(frames)))
    # An incomplete sequence must never become visible.
    assert gateway.requests == []


def test_compact_capture_only_sends_fields_changed_from_base() -> None:
    gateway = FakeGateway()
    base = _frame(0)
    moved = copy.deepcopy(base)
    moved["simulationTime"] = 0.02
    moved["entities"][0]["world"] = {"translation": [1, 2, 3]}
    returned = copy.deepcopy(base)
    returned["simulationTime"] = 0.04
    asyncio.run(_with_client(gateway, lambda c: c.publish_capture([base, moved, returned])))
    frames = gateway.requests[0][1]["frames"]
    assert frames[0]["entities"] == []
    assert frames[1]["entities"] == [{"id": 1, "world": {"translation": [1, 2, 3]}}]
    assert frames[2]["entities"] == []


def test_timeline_needs_at_least_two_frames() -> None:
    gateway = FakeGateway()
    with pytest.raises(GatewayError) as error:
        asyncio.run(_with_client(gateway, lambda c: c.publish_timeline([_frame(0)])))
    assert error.value.code == "INVALID_TIMELINE"
    # Rejected client-side, so nothing reached the Gateway.
    assert gateway.requests == []


def test_no_content_response_is_handled() -> None:
    gateway = FakeGateway()
    result = asyncio.run(_with_client(
        gateway, lambda c: c._post("/api/agent-control/empty")))
    assert result is None


def test_using_a_closed_client_is_an_explicit_error() -> None:
    async def body() -> None:
        client = GatewayClient(base_url="http://127.0.0.1:1", control_token="t", job_id="j")
        with pytest.raises(GatewayError) as error:
            await client.publish_snapshot()
        assert error.value.code == "NOT_CONNECTED"

    asyncio.run(body())
