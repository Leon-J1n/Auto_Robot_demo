"""Conversation serialisation, cancellation, recovery and the turn state machine.

One running turn per conversation, a global cap across conversations, and a strict
`queued -> running -> succeeded|failed|cancelled|interrupted` progression (design doc §8).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .runner import (AgentRunner, EmitEvent, PRESENTATION_REQUESTED, PUBLIC_EVENTS,
                     TURN_CANCELLED, TURN_COMPLETED, TURN_DEBUG, TURN_FAILED, TURN_PROGRESS,
                     TURN_STARTED, TurnBlocked, TurnCancelled, TurnOutcome, TurnRequest)
from .schemas import (ControlContext, ConversationView, EventView, MessageView,
                      PHASE_LABELS, SceneBinding, TurnView)
from .store import ConversationStore

LOGGER = logging.getLogger("agent_service.service")
QWEN_RESULT_CONTEXT_KEY = "last_qwen_result"


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ConversationService:
    def __init__(self, store: ConversationStore, runner: AgentRunner, *,
                 max_concurrent_turns: int | None = None):
        self.store = store
        self.runner = runner
        limit = max_concurrent_turns or int(
            os.environ.get("AGENT_SERVICE_MAX_CONCURRENT_TURNS", "4"))
        self._global = asyncio.Semaphore(max(1, limit))
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()

    # -- lifecycle -------------------------------------------------------------------

    def recover(self) -> int:
        """Mark turns that were mid-flight when the process died. They are never replayed."""
        count = self.store.interrupt_running_turns()
        LOGGER.info("service recovery interrupted_turns=%d", count)
        if count:
            LOGGER.warning("marked %d unfinished turn(s) as interrupted after restart", count)
        return count

    async def close(self) -> None:
        LOGGER.info("service shutdown cancelling_tasks=%d", len(self._tasks))
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        self._tasks.clear()

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[conversation_id] = lock
        return lock

    # -- conversations ---------------------------------------------------------------

    def create_conversation(self, *, target: str = "platform",
                            binding: SceneBinding | None = None,
                            presentation_mode: str = "real-sync") -> str:
        conversation_id = self.store.create_conversation(
            target=target,
            scene_id=binding.sceneId if binding else None,
            sim_session_id=binding.simSessionId if binding else None,
            scene_generation=binding.sceneGeneration if binding else None,
            connect=binding.connect if binding else None,
            presentation_mode=presentation_mode)
        LOGGER.info("conversation created id=%s target=%s scene_id=%s mode=%s",
                    conversation_id, target, binding.sceneId if binding else None,
                    presentation_mode)
        return conversation_id

    def require_conversation(self, conversation_id: str) -> dict[str, Any]:
        row = self.store.conversation(conversation_id)
        if row is None:
            raise ServiceError("CONVERSATION_NOT_FOUND", "conversation was not found", 404)
        return row

    def conversation_view(self, conversation_id: str) -> ConversationView:
        row = self.require_conversation(conversation_id)
        binding = None
        if row.get("scene_id") or row.get("sim_session_id"):
            binding = SceneBinding(
                sceneId=str(row.get("scene_id") or ""),
                simSessionId=str(row.get("sim_session_id") or ""),
                sceneGeneration=int(row.get("scene_generation") or 0),
                connect=row.get("connect"))
        return ConversationView(
            conversationId=conversation_id,
            target=str(row.get("target") or "platform"),
            status=str(row.get("status") or "active"),
            createdAt=str(row.get("created_at") or ""),
            sceneBinding=binding,
            presentationMode=str(row.get("presentation_mode") or "real-sync"),  # type: ignore[arg-type]
            messages=[
                MessageView(role=str(item["role"]), content=str(item["content"]),
                            turnId=item.get("turn_id"), createdAt=str(item["created_at"]))
                for item in self.store.messages(conversation_id)
                if str(item["role"]) in {"user", "assistant"}],
            turns=[self._turn_view(item) for item in self.store.turns(conversation_id)])

    def list_conversations(self, *, target: str | None = None) -> list[dict[str, Any]]:
        result = []
        for row in self.store.list_conversations(target=target):
            conversation_id = str(row["conversation_id"])
            turns = self.store.turns(conversation_id)
            messages = self.store.messages(conversation_id)
            result.append({
                "conversationId": conversation_id,
                "target": str(row.get("target") or "platform"),
                "status": str(row.get("status") or "active"),
                "createdAt": str(row.get("created_at") or ""),
                "updatedAt": str(row.get("updated_at") or ""),
                "presentationMode": str(row.get("presentation_mode") or "real-sync"),
                "sceneId": row.get("scene_id"),
                "turnCount": len(turns),
                "messageCount": len(messages),
                "title": next((str(item["content"])[:60] for item in messages
                               if str(item["role"]) == "user"), ""),
            })
        return result

    def delete_conversation(self, conversation_id: str) -> None:
        self.require_conversation(conversation_id)
        task = self._tasks.get(conversation_id)
        if task is not None:
            task.cancel()
        if not self.store.delete_conversation(conversation_id):
            raise ServiceError("CONVERSATION_NOT_FOUND", "conversation was not found", 404)

    # -- turns -----------------------------------------------------------------------

    @staticmethod
    def _turn_view(row: dict[str, Any]) -> TurnView:
        import json

        try:
            affected = json.loads(str(row.get("affected_objects") or "[]"))
        except json.JSONDecodeError:
            affected = []
        return TurnView(
            turnId=str(row["turn_id"]),
            conversationId=str(row["conversation_id"]),
            requestId=str(row.get("request_id") or ""),
            status=str(row.get("status") or "queued"),  # type: ignore[arg-type]
            message=str(row.get("message") or ""),
            errorCode=row.get("error_code"),
            createdAt=str(row.get("created_at") or ""),
            finishedAt=row.get("finished_at"),
            presentationRevision=row.get("presentation_revision"),
            affectedObjects=[str(item) for item in affected] if isinstance(affected, list) else [])

    def turn_view(self, conversation_id: str, turn_id: str) -> TurnView:
        row = self.store.turn(turn_id)
        if row is None or str(row["conversation_id"]) != conversation_id:
            raise ServiceError("TURN_NOT_FOUND", "turn was not found", 404)
        return self._turn_view(row)

    def turn_views(self, conversation_id: str) -> list[TurnView]:
        self.require_conversation(conversation_id)
        return [self._turn_view(row) for row in self.store.turns(conversation_id)]

    def events(self, conversation_id: str, *, after: int = 0,
               limit: int = 200) -> list[EventView]:
        self.require_conversation(conversation_id)
        return [EventView(eventId=int(row["event_id"]),
                          conversationId=conversation_id,
                          turnId=row.get("turn_id"),
                          type=str(row["type"]),
                          payload=dict(row.get("payload") or {}),
                          createdAt=str(row["created_at"]))
                for row in self.store.events(conversation_id, after=after, limit=limit)]

    def submit_turn(self, conversation_id: str, *, request_id: str, message: str,
                    control_context: ControlContext | None = None,
                    presentation_mode: str | None = None,
                    binding: SceneBinding | None = None,
                    debug: bool = False) -> TurnView:
        """Register a turn and start it in the background. Idempotent on `requestId`."""
        row = self.require_conversation(conversation_id)
        existing = self.store.find_turn_by_request(conversation_id, request_id)
        if existing is not None:
            LOGGER.debug("turn submission reused idempotent request conversation_id=%s "
                         "request_id=%s turn_id=%s", conversation_id, request_id,
                         existing.get("turn_id"))
            return self._turn_view(existing)

        effective = binding
        if effective is None and control_context is not None and control_context.sceneId:
            effective = SceneBinding(
                sceneId=str(control_context.sceneId),
                simSessionId=str(control_context.simSessionId or ""),
                sceneGeneration=int(control_context.sceneGeneration or 0),
                connect=control_context.liveEndpoint)
        mode = presentation_mode or (
            control_context.presentationMode if control_context else None) or str(
            row.get("presentation_mode") or "real-sync")
        self.store.update_binding(
            conversation_id,
            scene_id=effective.sceneId if effective else None,
            sim_session_id=effective.simSessionId if effective else None,
            scene_generation=effective.sceneGeneration if effective else None,
            connect=effective.connect if effective else None,
            presentation_mode=mode)

        turn_id = self.store.create_turn(conversation_id, request_id)
        LOGGER.info("turn queued conversation_id=%s turn_id=%s request_id=%s mode=%s",
                    conversation_id, turn_id, request_id, mode)
        # The raw user text is the user message; control fields are never echoed as one.
        self.store.add_message(conversation_id, "user", message, turn_id)
        history = [{"role": str(item["role"]), "content": str(item["content"])}
                   for item in self.store.messages(conversation_id)
                   if str(item["role"]) in {"user", "assistant"}][:-1]
        request = TurnRequest(
            conversation_id=conversation_id, turn_id=turn_id, request_id=request_id,
            message=message, control_context=control_context, presentation_mode=mode,
            debug=debug, history=history)
        task = asyncio.create_task(self._run_turn(request), name=f"turn-{turn_id}")
        self._tasks[conversation_id] = task
        task.add_done_callback(lambda _, key=conversation_id: self._tasks.pop(key, None))
        return self.turn_view(conversation_id, turn_id)

    async def cancel_turn(self, conversation_id: str, turn_id: str) -> TurnView:
        view = self.turn_view(conversation_id, turn_id)
        if view.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return view
        self._cancelled.add(turn_id)
        task = self._tasks.get(conversation_id)
        if task is not None:
            task.cancel()
            # Cancellation is best effort; the scene is reported as it actually is, not rolled
            # back (design doc §10).
            await asyncio.gather(task, return_exceptions=True)
        current = self.store.turn(turn_id)
        if current is not None and str(current.get("status")) in {"queued", "running"}:
            self.store.update_turn(turn_id, status="cancelled",
                                   message="任务已取消。", error_code="EXECUTION_CANCELLED_BY_USER")
            self.store.add_event(conversation_id, TURN_CANCELLED, {"turnId": turn_id}, turn_id)
        return self.turn_view(conversation_id, turn_id)

    def _emitter(self, request: TurnRequest) -> EmitEvent:
        async def emit(type: str, payload: dict[str, Any]) -> None:
            if type == TURN_DEBUG:
                # Diagnostic stream; only surfaced when the caller asked for debug.
                if not request.debug:
                    return
            elif type not in PUBLIC_EVENTS:
                LOGGER.debug("dropping non-public event %r", type)
                return
            if type == TURN_PROGRESS:
                phase = str(payload.get("phase", ""))
                # Only whitelisted coarse phases become user-visible text.
                payload = dict(payload)
                payload["label"] = PHASE_LABELS.get(phase, PHASE_LABELS.get("scene", ""))
            self.store.add_event(request.conversation_id, type, payload, request.turn_id)

        return emit

    async def _run_turn(self, request: TurnRequest) -> None:
        LOGGER.debug("turn worker entered conversation_id=%s turn_id=%s request_id=%s",
                     request.conversation_id, request.turn_id, request.request_id)
        emit = self._emitter(request)
        async with self._global:
            async with self._lock(request.conversation_id):
                stored_model_context = self.store.service_context(QWEN_RESULT_CONTEXT_KEY)
                request.model_context = (stored_model_context
                                         if isinstance(stored_model_context, dict) else {})
                if request.turn_id in self._cancelled:
                    self._cancelled.discard(request.turn_id)
                    self.store.update_turn(request.turn_id, status="cancelled",
                                           message="任务已取消。",
                                           error_code="EXECUTION_CANCELLED_BY_USER")
                    return
                self.store.update_turn(request.turn_id, status="running")
                await emit(TURN_STARTED, {"turnId": request.turn_id})
                try:
                    outcome = await self.runner.run(request, emit)
                except (asyncio.CancelledError, TurnCancelled):
                    LOGGER.warning("turn cancelled conversation_id=%s turn_id=%s",
                                   request.conversation_id, request.turn_id)
                    self.store.update_turn(request.turn_id, status="cancelled",
                                           message="任务已取消。",
                                           error_code="EXECUTION_CANCELLED_BY_USER")
                    await emit(TURN_CANCELLED, {"turnId": request.turn_id})
                    raise
                except TurnBlocked as error:
                    LOGGER.warning("turn blocked turn_id=%s code=%s message=%s",
                                   request.turn_id, error.code, error.message)
                    self.store.update_turn(request.turn_id, status="failed",
                                           message=error.message, error_code=error.code)
                    self.store.add_message(request.conversation_id, "assistant", error.message,
                                           request.turn_id)
                    await emit(TURN_FAILED, {"turnId": request.turn_id, "errorCode": error.code})
                    return
                except Exception as error:  # noqa: BLE001 - reported as a structured failure
                    LOGGER.exception("turn %s failed", request.turn_id)
                    message = f"任务执行失败：{error}"
                    self.store.update_turn(request.turn_id, status="failed", message=message,
                                           error_code="EXECUTION_FAILED")
                    self.store.add_message(request.conversation_id, "assistant", message,
                                           request.turn_id)
                    await emit(TURN_FAILED,
                               {"turnId": request.turn_id, "errorCode": "EXECUTION_FAILED"})
                    return
                finally:
                    if request.model_context_updated:
                        self.store.set_service_context(
                            QWEN_RESULT_CONTEXT_KEY, request.model_context)
                self._finish(request, outcome)
                LOGGER.info("turn finished conversation_id=%s turn_id=%s status=%s",
                            request.conversation_id, request.turn_id, outcome.status)
                await emit(TURN_COMPLETED, {"turnId": request.turn_id,
                                            "status": outcome.status})

    def _finish(self, request: TurnRequest, outcome: TurnOutcome) -> None:
        self.store.update_turn(
            request.turn_id, status=outcome.status, message=outcome.message,
            error_code=outcome.error_code,
            presentation_revision=outcome.presentation_revision,
            affected_objects=outcome.affected_objects)
        if outcome.message:
            self.store.add_message(request.conversation_id, "assistant", outcome.message,
                                   request.turn_id)
