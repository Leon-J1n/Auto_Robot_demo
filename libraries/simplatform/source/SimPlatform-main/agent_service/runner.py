"""The `AgentRunner` protocol and the stable domain events a runner may emit.

A runner is deliberately narrow: it takes one turn request and reports progress through the
whitelisted events below. Raw model requests and responses, function-call arguments, internal
reasoning and intermediate files never travel through here -- they belong to restricted
diagnostic logs only (design doc §4.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .schemas import ControlContext

# Stable public event names. Anything not in this set must not reach the event store.
TURN_STARTED = "turn.started"
TURN_PROGRESS = "turn.progress"
PRESENTATION_REQUESTED = "presentation.requested"
TURN_COMPLETED = "turn.completed"
TURN_FAILED = "turn.failed"
TURN_CANCELLED = "turn.cancelled"
# Diagnostic-only, gated on an explicit local `debug=true`. The Gateway forwards these to the
# panel; they carry phase and plan structure, never model internals.
TURN_DEBUG = "turn.debug"

PUBLIC_EVENTS = frozenset({
    TURN_STARTED, TURN_PROGRESS, PRESENTATION_REQUESTED,
    TURN_COMPLETED, TURN_FAILED, TURN_CANCELLED,
})


@dataclass
class TurnRequest:
    conversation_id: str
    turn_id: str
    request_id: str
    message: str
    control_context: ControlContext | None = None
    presentation_mode: str = "real-sync"
    debug: bool = False
    # Prior visible messages, oldest first, for context rebuilding.
    history: list[dict[str, str]] = field(default_factory=list)
    # Private service-level model state. ConversationService persists it across conversations
    # and restarts; it is never exposed through the public conversation/message contract.
    model_context: dict[str, Any] = field(default_factory=dict)
    model_context_updated: bool = False


@dataclass
class TurnOutcome:
    """What a completed turn is allowed to hand back.

    Only user-visible reply text, a structured status, an optional published presentation
    reference and optional affected objects (design doc §7).
    """

    status: str
    message: str
    error_code: str | None = None
    presentation_revision: int | None = None
    affected_objects: list[str] = field(default_factory=list)


# Emitters are injected so the runner never reaches into the store directly.
EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentRunner(Protocol):
    async def run(self, request: TurnRequest, emit: EmitEvent) -> TurnOutcome:
        ...


class TurnCancelled(Exception):
    """Raised inside a runner when the turn was cancelled cooperatively."""


class TurnBlocked(Exception):
    """A turn cannot proceed and must fail with an explicit blocker rather than degrade.

    Used for the cases the design doc calls out: no bound Live session, a missing published
    capability, or an unconfigured model. Never used to silently substitute an alternative.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
