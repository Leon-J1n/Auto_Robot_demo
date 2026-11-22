"""HTTP surface: `/v1/conversations`, turns, events and `/health`.

The routes and payload shapes here are fixed by the Gateway's existing transport client
(`web/gateway/agent.py`); any process serving them can host a Web turn. Error bodies carry a
`detail` field because that is what the client surfaces.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import ValidationError
from sim_platform._logging import install_standard_logging

from .schemas import CreateConversationRequest, CreateTurnRequest
from .service import ConversationService, ServiceError
from .store import ConversationStore

LOGGER = logging.getLogger("agent_service.api")

SERVICE_KEY = web.AppKey("agent_service", ConversationService)


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"detail": message, "error": {"code": code, "message": message}},
                             status=status)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as error:  # noqa: BLE001 - malformed body is a client error
        raise ServiceError("INVALID_REQUEST", f"body is not valid JSON: {error}") from error
    if not isinstance(body, dict):
        raise ServiceError("INVALID_REQUEST", "body must be a JSON object")
    return body


def create_app(service: ConversationService) -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app[SERVICE_KEY] = service

    @web.middleware
    async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except ServiceError as error:
            return _error(error.status, error.code, error.message)
        except ValidationError as error:
            return _error(400, "INVALID_REQUEST", error.errors(include_url=False).__str__())

    app.middlewares.append(errors)

    async def health(_: web.Request) -> web.Response:
        # Stays "ok" even when the model is unconfigured, so the Gateway can tell "service
        # online but model unavailable" from "service down" (design doc §4.2).
        model = getattr(service.runner, "model_status", lambda: {"status": "unknown"})()
        return web.json_response({"status": "ok", "model": model})

    async def create_conversation(request: web.Request) -> web.Response:
        body = await _json_body(request)
        payload = CreateConversationRequest.model_validate(body)
        conversation_id = service.create_conversation(
            target=payload.target, binding=payload.sceneBinding,
            presentation_mode=payload.presentationMode)
        return web.json_response(
            service.conversation_view(conversation_id).model_dump(mode="json"), status=201)

    async def list_conversations(request: web.Request) -> web.Response:
        target = request.query.get("target")
        return web.json_response(service.list_conversations(target=target))

    async def get_conversation(request: web.Request) -> web.Response:
        conversation_id = request.match_info["conversation_id"]
        return web.json_response(
            service.conversation_view(conversation_id).model_dump(mode="json"))

    async def delete_conversation(request: web.Request) -> web.Response:
        service.delete_conversation(request.match_info["conversation_id"])
        return web.json_response({"deleted": True})

    async def create_turn(request: web.Request) -> web.Response:
        conversation_id = request.match_info["conversation_id"]
        body = await _json_body(request)
        payload = CreateTurnRequest.model_validate(body)
        view = service.submit_turn(
            conversation_id, request_id=payload.requestId, message=payload.message,
            control_context=payload.controlContext,
            presentation_mode=payload.presentationMode, binding=payload.sceneBinding,
            debug=payload.debug)
        return web.json_response(view.model_dump(mode="json"), status=202)

    async def list_turns(request: web.Request) -> web.Response:
        views = service.turn_views(request.match_info["conversation_id"])
        return web.json_response([view.model_dump(mode="json") for view in views])

    async def get_turn(request: web.Request) -> web.Response:
        view = service.turn_view(request.match_info["conversation_id"],
                                request.match_info["turn_id"])
        return web.json_response(view.model_dump(mode="json"))

    async def cancel_turn(request: web.Request) -> web.Response:
        view = await service.cancel_turn(request.match_info["conversation_id"],
                                         request.match_info["turn_id"])
        return web.json_response(view.model_dump(mode="json"))

    async def list_events(request: web.Request) -> web.Response:
        def _int(name: str, default: int) -> int:
            try:
                return int(request.query.get(name, default))
            except (TypeError, ValueError):
                return default

        views = service.events(request.match_info["conversation_id"],
                              after=_int("after", 0), limit=min(1000, _int("limit", 200)))
        return web.json_response([view.model_dump(mode="json") for view in views])

    app.router.add_get("/health", health)
    app.router.add_post("/v1/conversations", create_conversation)
    app.router.add_get("/v1/conversations", list_conversations)
    app.router.add_get("/v1/conversations/{conversation_id}", get_conversation)
    app.router.add_delete("/v1/conversations/{conversation_id}", delete_conversation)
    app.router.add_post("/v1/conversations/{conversation_id}/turns", create_turn)
    app.router.add_get("/v1/conversations/{conversation_id}/turns", list_turns)
    app.router.add_get("/v1/conversations/{conversation_id}/turns/{turn_id}", get_turn)
    app.router.add_post("/v1/conversations/{conversation_id}/turns/{turn_id}/cancel", cancel_turn)
    app.router.add_get("/v1/conversations/{conversation_id}/events", list_events)

    async def shutdown(_: web.Application) -> None:
        await service.close()
        service.store.close()

    app.on_cleanup.append(shutdown)
    return app


def build_service(*, database: str | Path | None = None) -> ConversationService:
    from .qwen_runner import PlatformAgentRunner

    install_standard_logging("agent_service")
    storage = Path(database) if database is not None else Path(
        os.environ.get("AGENT_SERVICE_DB",
                       str(Path.cwd() / ".sim-cache" / "agent-service" / "conversations.db")))
    store = ConversationStore(storage)
    service = ConversationService(store, PlatformAgentRunner())
    service.recover()
    return service


def main() -> None:
    parser = argparse.ArgumentParser(description="RobotSimPlatform Agent Service")
    parser.add_argument("--host", default="127.0.0.1")
    # 8765 matches the design doc and the Gateway client's default. It does not clash with the
    # Gateway's embedded Live listener, which binds an OS-chosen free port.
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    install_standard_logging("agent_service")
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = build_service(database=args.database)
    LOGGER.info("agent service listening on http://%s:%d", args.host, args.port)
    web.run_app(create_app(service), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
