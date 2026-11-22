from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import ClientSession, ClientTimeout


@dataclass(frozen=True)
class AgentResult:
    conversation_id: str
    turn_id: str
    message: str
    status: str


class AgentTurnError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class AgentClient(Protocol):
    @property
    def connected(self) -> bool: ...

    @property
    def endpoint(self) -> str | None: ...

    async def probe(self) -> bool: ...

    async def ensure_conversation(
        self, *, conversation_id: str | None, scene_id: str,
        sim_session_id: str, scene_generation: int, connect: str,
        presentation_mode: str,
    ) -> str: ...

    async def list_turns(self, conversation_id: str) -> list[dict[str, Any]]: ...

    async def list_conversations(self) -> list[dict[str, Any]]: ...

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]: ...

    async def list_events(self, conversation_id: str) -> list[dict[str, Any]]: ...

    async def create_conversation(
        self, *, scene_id: str, sim_session_id: str, scene_generation: int,
        connect: str, presentation_mode: str,
    ) -> dict[str, Any]: ...

    async def delete_conversation(self, conversation_id: str) -> None: ...

    async def execute(
        self,
        *,
        conversation_id: str | None,
        scene_id: str,
        sim_session_id: str,
        scene_generation: int,
        connect: str,
        text: str,
        presentation_mode: str,
        control_context: dict[str, Any],
        request_id: str,
        debug: bool = False,
        on_debug: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentResult: ...


class AgentServiceClient:
    def __init__(self, host: str | None = None, port: int = 8765, timeout: float = 600.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str | None:
        return f"http://{self.host}:{self.port}" if self.host else None

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        if self.endpoint is None:
            raise RuntimeError("Agent Service is not configured")
        timeout = ClientTimeout(total=self.timeout)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.request(method, f"{self.endpoint}{path}", json=payload) as response:
                    if response.status == 204:
                        self._connected = True
                        return None
                    body = await response.json()
                    if response.status >= 400:
                        detail = body.get("detail") if isinstance(body, dict) else None
                        raise RuntimeError(str(detail or "Agent Service request failed"))
                    self._connected = True
                    return body
        except Exception:
            self._connected = False
            raise

    async def execute(
        self,
        *,
        conversation_id: str | None,
        scene_id: str,
        sim_session_id: str,
        scene_generation: int,
        connect: str,
        text: str,
        presentation_mode: str,
        control_context: dict[str, Any],
        request_id: str,
        debug: bool = False,
        on_debug: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentResult:
        conversation_id = await self.ensure_conversation(
            conversation_id=conversation_id,
            scene_id=scene_id,
            sim_session_id=sim_session_id,
            scene_generation=scene_generation,
            connect=connect,
            presentation_mode=presentation_mode,
        )
        turn = await self._request(
            "POST", f"/v1/conversations/{conversation_id}/turns", {
                "requestId": request_id,
                "message": text,
                "controlContext": control_context,
                "debug": debug,
            })
        if not isinstance(turn, dict):
            raise RuntimeError("Agent Service returned an invalid turn")
        turn_id = str(turn["turnId"])
        event_cursor = 0

        async def drain_debug_events() -> None:
            nonlocal event_cursor
            if (not debug or on_debug is None) and on_progress is None:
                return
            while True:
                events = await self._request(
                    "GET", f"/v1/conversations/{conversation_id}/events"
                    f"?after={event_cursor}&limit=200"
                )
                if not isinstance(events, list):
                    return
                for value in events:
                    if not isinstance(value, dict):
                        continue
                    event_cursor = max(event_cursor, int(value.get("eventId", 0)))
                    payload = value.get("payload")
                    if (value.get("turnId") == turn_id and value.get("type") == "turn.progress"
                            and isinstance(payload, dict) and on_progress is not None):
                        await on_progress(dict(payload))
                    if (debug and value.get("turnId") == turn_id
                            and value.get("type") == "turn.debug"
                            and isinstance(payload, dict)):
                        await on_debug(dict(payload))
                if len(events) < 200:
                    return

        try:
            deadline = asyncio.get_running_loop().time() + self.timeout
            while str(turn.get("status")) in {"queued", "running"}:
                if asyncio.get_running_loop().time() >= deadline:
                    await self._request(
                        "POST",
                        f"/v1/conversations/{conversation_id}/turns/{turn_id}/cancel",
                    )
                    raise TimeoutError("Agent turn timed out")
                await asyncio.sleep(0.5)
                turn = await self._request(
                    "GET", f"/v1/conversations/{conversation_id}/turns/{turn_id}"
                )
                await drain_debug_events()
            await drain_debug_events()
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._request(
                    "POST",
                    f"/v1/conversations/{conversation_id}/turns/{turn_id}/cancel",
                ))
            finally:
                raise
        status = str(turn.get("status", "failed"))
        if status != "succeeded":
            raise AgentTurnError(
                str(turn.get("message", f"Agent turn {status}")),
                code=str(turn.get("errorCode")) if turn.get("errorCode") else None)
        return AgentResult(
            conversation_id=conversation_id,
            turn_id=turn_id,
            message=str(turn.get("message", "Agent completed the request")),
            status=status,
        )

    async def probe(self) -> bool:
        try:
            value = await self._request("GET", "/health")
            return isinstance(value, dict) and str(value.get("status", "ok")) == "ok"
        except Exception:
            return False

    async def ensure_conversation(
        self, *, conversation_id: str | None, scene_id: str,
        sim_session_id: str, scene_generation: int, connect: str,
        presentation_mode: str,
    ) -> str:
        if conversation_id is None:
            created = await self._request("POST", "/v1/conversations", {
                "target": "platform",
                "sceneBinding": {
                    "sceneId": scene_id,
                    "simSessionId": sim_session_id,
                    "sceneGeneration": scene_generation,
                    "connect": connect,
                },
                "presentationMode": presentation_mode,
            })
            if not isinstance(created, dict):
                raise RuntimeError("Agent Service returned an invalid conversation")
            return str(created["conversationId"])
        return conversation_id

    async def list_turns(self, conversation_id: str) -> list[dict[str, Any]]:
        value = await self._request("GET", f"/v1/conversations/{conversation_id}/turns")
        if not isinstance(value, list):
            raise RuntimeError("Agent Service returned an invalid turn list")
        return [dict(item) for item in value if isinstance(item, dict)]

    async def list_conversations(self) -> list[dict[str, Any]]:
        value = await self._request("GET", "/v1/conversations?target=platform")
        if not isinstance(value, list):
            raise RuntimeError("Agent Service returned an invalid conversation list")
        return [dict(item) for item in value if isinstance(item, dict)]

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        value = await self._request(
            "GET", f"/v1/conversations/{conversation_id}"
        )
        if not isinstance(value, dict):
            raise RuntimeError("Agent Service returned an invalid conversation")
        return dict(value)

    async def list_events(self, conversation_id: str) -> list[dict[str, Any]]:
        value = await self._request("GET", f"/v1/conversations/{conversation_id}/events?limit=1000")
        if not isinstance(value, list):
            raise RuntimeError("Agent Service returned an invalid event list")
        return [dict(item) for item in value if isinstance(item, dict)]

    async def create_conversation(
        self, *, scene_id: str, sim_session_id: str, scene_generation: int,
        connect: str, presentation_mode: str,
    ) -> dict[str, Any]:
        value = await self._request("POST", "/v1/conversations", {
            "target": "platform",
            "sceneBinding": {
                "sceneId": scene_id,
                "simSessionId": sim_session_id,
                "sceneGeneration": scene_generation,
                "connect": connect,
            },
            "presentationMode": presentation_mode,
        })
        if not isinstance(value, dict):
            raise RuntimeError("Agent Service returned an invalid conversation")
        return dict(value)

    async def delete_conversation(self, conversation_id: str) -> None:
        await self._request("DELETE", f"/v1/conversations/{conversation_id}")
