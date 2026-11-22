"""Public request, response and event schemas for the Agent Service HTTP contract.

The shapes here are the contract the Web Gateway's `AgentServiceClient` already speaks
(`web/gateway/agent.py`); they are deliberately independent of the model runner and of the
simulation runtime, so any process serving these routes can host a Web turn.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TurnStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
PresentationMode = Literal["real-sync", "sync-and-animate"]

# Coarse, whitelisted phase labels. Anything the model produced verbatim is barred from this
# stream by design doc §4.1 -- only these reach a user-visible message.
PHASE_LABELS: dict[str, str] = {
    "scene": "正在检查场景",
    "understand": "正在理解指令",
    "plan": "正在分解步骤",
    "compile": "正在编译命令",
    "execute": "正在执行动作",
    "publish": "正在同步场景",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SceneBinding(StrictModel):
    sceneId: str
    simSessionId: str
    sceneGeneration: int
    # The Live endpoint the turn must operate. Connect-only: the service never launches a
    # substitute instance when this is absent or unreachable.
    connect: str | None = None


class CreateConversationRequest(StrictModel):
    target: str = "platform"
    sceneBinding: SceneBinding | None = None
    presentationMode: PresentationMode = "real-sync"


class ControlContext(BaseModel):
    """Gateway-issued control plane for one turn. Never assembled from user text."""

    # Forgiving on purpose: the Gateway may add fields ahead of this service.
    model_config = ConfigDict(extra="allow")

    gatewayBaseUrl: str | None = None
    controlToken: str | None = None
    jobId: str | None = None
    sceneId: str | None = None
    simSessionId: str | None = None
    sceneGeneration: int | None = None
    liveEndpoint: str | None = None
    presentationMode: PresentationMode | None = None
    robotAssets: list[dict[str, Any]] = Field(default_factory=list)


class CreateTurnRequest(StrictModel):
    requestId: str
    message: str
    controlContext: ControlContext | None = None
    presentationMode: PresentationMode | None = None
    sceneBinding: SceneBinding | None = None
    debug: bool = False


class TurnView(StrictModel):
    turnId: str
    conversationId: str
    requestId: str
    status: TurnStatus
    message: str = ""
    errorCode: str | None = None
    createdAt: str
    finishedAt: str | None = None
    presentationRevision: int | None = None
    affectedObjects: list[str] = Field(default_factory=list)


class MessageView(StrictModel):
    role: Literal["user", "assistant"]
    content: str
    turnId: str | None = None
    createdAt: str


class ConversationView(StrictModel):
    conversationId: str
    target: str
    status: str
    createdAt: str
    sceneBinding: SceneBinding | None = None
    presentationMode: PresentationMode = "real-sync"
    messages: list[MessageView] = Field(default_factory=list)
    turns: list[TurnView] = Field(default_factory=list)


class EventView(StrictModel):
    eventId: int
    conversationId: str
    turnId: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    createdAt: str
