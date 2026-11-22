"""Local conversation and execution entry point that does not need the Gateway.

Same ConversationService and runner as the HTTP surface -- the REPL is not a second
implementation. Local mode has no Web presentation and never calls the Gateway publish APIs, so
it is for functional testing and triage, not a replacement for the Gateway's auth, write lease
or sync protocol (design doc §4.4).

    python -m agent_service.cli chat "在已有仿真会话中创建一个红色方块" \
      --connect tcp://127.0.0.1:9100 --conversation <optional-id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from sim_platform._logging import install_standard_logging

from .schemas import ControlContext, SceneBinding
from .service import ConversationService, ServiceError

LOGGER = logging.getLogger("agent_service.cli")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def _binding(connect: str) -> SceneBinding:
    return SceneBinding(sceneId="local", simSessionId="local", sceneGeneration=0,
                        connect=connect)


async def _run_once(service: ConversationService, *, conversation_id: str, message: str,
                    connect: str, debug: bool) -> int:
    binding = _binding(connect)
    view = service.submit_turn(
        conversation_id, request_id=uuid.uuid4().hex, message=message,
        control_context=ControlContext(liveEndpoint=connect, sceneId="local",
                                       simSessionId="local", sceneGeneration=0),
        binding=binding, debug=debug)
    seen = 0
    while view.status not in TERMINAL:
        await asyncio.sleep(0.2)
        for event in service.events(conversation_id, after=seen, limit=200):
            seen = max(seen, event.eventId)
            label = event.payload.get("label") or event.payload.get("phase")
            if event.type == "turn.progress" and label:
                print(f"  · {label}", flush=True)
            elif debug and event.type == "turn.debug":
                print(f"  [debug] {event.payload}", flush=True)
        view = service.turn_view(conversation_id, view.turnId)
    print(f"\n[{view.status}] {view.message}")
    if view.errorCode:
        print(f"error code: {view.errorCode}", file=sys.stderr)
    return 0 if view.status == "succeeded" else 1


async def _repl(service: ConversationService, *, conversation_id: str, connect: str,
                debug: bool) -> int:
    print(f"conversation: {conversation_id}")
    print("type an instruction, or 'exit' to leave")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"exit", "quit"}:
            return 0
        await _run_once(service, conversation_id=conversation_id, message=line,
                        connect=connect, debug=debug)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_service.cli",
        description="Local Agent Service conversation and execution entry point")
    parser.add_argument("command", choices=["chat", "repl"])
    parser.add_argument("message", nargs="?", default="")
    # Connect-only: an explicit endpoint is required and no substitute instance is ever started.
    parser.add_argument("--connect", required=True,
                        help="Live endpoint to operate, e.g. tcp://127.0.0.1:9100 or pid:18432")
    parser.add_argument("--conversation")
    parser.add_argument("--database")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    if args.command == "chat" and not str(args.message).strip():
        parser.error("chat needs a message")

    install_standard_logging("agent_service")
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(levelname)s %(name)s %(message)s")

    from .api import build_service

    service = build_service(database=Path(args.database) if args.database else None)
    try:
        conversation_id = args.conversation
        if conversation_id is None:
            conversation_id = service.create_conversation(
                target="local", binding=_binding(args.connect))
            print(f"created conversation {conversation_id}")
        else:
            try:
                service.require_conversation(conversation_id)
            except ServiceError as error:
                print(f"{error.code}: {error.message}", file=sys.stderr)
                return 2
        if args.command == "chat":
            return asyncio.run(_run_once(
                service, conversation_id=conversation_id, message=str(args.message),
                connect=args.connect, debug=bool(args.debug)))
        return asyncio.run(_repl(
            service, conversation_id=conversation_id, connect=args.connect,
            debug=bool(args.debug)))
    finally:
        service.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
