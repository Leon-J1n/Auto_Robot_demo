"""Planning contracts: TaskIntent, GroundedTask and SkillPlan.

Ported from ``robot_agent_sim.contracts``. These are the only structures the
model is allowed to shape; coordinates, joint values and trajectories are owned
by the deterministic compile layer.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    ACCEPTED = "accepted"
    UNSUPPORTED_TASK = "unsupported_task"
    DIRECTION_CLARIFICATION_REQUIRED = "direction_clarification_required"
    INVALID = "invalid"


class TaskType(StrEnum):
    LOCATE = "locate"
    SEARCH = "search"
    MOVE = "move"
    GRASP = "grasp"
    RELEASE = "release"
    PICK_AND_PLACE = "pick_and_place"
    PRESS = "press"
    OPEN = "open"
    CLOSE = "close"
    MIXED = "mixed"


class Direction(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    FRONT = "front"
    BACK = "back"
    UP = "up"
    DOWN = "down"


class SpatialRelationType(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    FRONT = "front"
    BACK = "back"
    UP = "up"
    DOWN = "down"
    LEFT_OF = "left_of"
    RIGHT_OF = "right_of"
    FRONT_OF = "front_of"
    BEHIND = "behind"
    ABOVE = "above"
    BELOW = "below"
    INSIDE = "inside"
    NEAREST = "nearest"
    FARTHEST = "farthest"


class TaskEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=r"^[\w-]+$")
    semantic_name: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=50)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    color: str | None = None


class SpatialRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    relation: SpatialRelationType
    reference: str | None = None
    scope: Literal["scene", "selection", "goal"] = "selection"

    @model_validator(mode="after")
    def check_relation(self):
        if self.relation in {SpatialRelationType.NEAREST, SpatialRelationType.FARTHEST} and self.reference is None:
            raise ValueError("selection relation requires reference")
        return self


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^op-[\w-]+$")
    task_type: TaskType
    source: str | None = None
    destination: str | None = None
    target: str | None = None
    reference: str | None = None
    description: str = Field(default="", max_length=300)
    depends_on: list[str] = Field(default_factory=list)


class TaskIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: TaskStatus
    instruction: str = Field(min_length=1, max_length=2000)
    task_types: list[TaskType] = Field(default_factory=list)
    entities: list[TaskEntity] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    spatial_relations: list[SpatialRelation] = Field(default_factory=list)
    raw_direction: Direction | None = None
    explanation: str = ""

    @model_validator(mode="after")
    def validate_refs(self):
        entity_ids = {e.entity_id for e in self.entities}
        if len(entity_ids) != len(self.entities):
            raise ValueError("entity_id must be unique")
        operation_ids = {o.operation_id for o in self.operations}
        if len(operation_ids) != len(self.operations):
            raise ValueError("operation_id must be unique")
        operation_position = {operation.operation_id: index for index, operation in enumerate(self.operations)}
        for op in self.operations:
            refs = [op.source, op.destination, op.target, op.reference]
            missing = {value for value in refs if value and value not in entity_ids}
            if missing:
                raise ValueError(f"operation references unknown entities: {sorted(missing)}")
            if op.operation_id in op.depends_on or not set(op.depends_on) <= operation_ids:
                raise ValueError("invalid operation depends_on")
            if any(operation_position[dependency] >= operation_position[op.operation_id] for dependency in op.depends_on):
                raise ValueError("operation depends_on must point to an earlier operation")
        for rel in self.spatial_relations:
            if rel.subject not in entity_ids or (rel.reference and rel.reference not in entity_ids):
                raise ValueError("spatial relation references unknown entity")
        if self.status == TaskStatus.ACCEPTED:
            if not self.entities or not self.operations or not self.task_types:
                raise ValueError("accepted task intent requires entities, operations, and task_types")
            operation_types = {operation.task_type for operation in self.operations}
            if TaskType.MIXED in operation_types:
                raise ValueError("mixed describes the task as a whole, not an operation")
            if not operation_types <= set(self.task_types):
                raise ValueError("task_types must include every operation task_type")
        return self


class GroundedEntity(BaseModel):
    # ``model_id``/``model_name`` are authored field names; opting out of the
    # protected namespace keeps them without pydantic warnings.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    entity_id: str
    semantic_name: str = Field(min_length=1, max_length=100)
    object_id: str
    # On this platform ``body_name`` carries the scene entity name; the field
    # keeps its authored spelling for compatibility with existing sidecars.
    body_name: str | None = None
    model_id: str | None = None
    model_name: str | None = None
    grounding_method: Literal[
        "asset_scene_binding", "vlm_iou", "detector_iou", "interaction_registry"
    ]
    detection_bbox: tuple[int, int, int, int] | None = None
    instance_bbox: tuple[int, int, int, int] | None = None
    bbox_iou: float | None = Field(default=None, ge=0, le=1)


class GroundedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str
    task_types: list[TaskType]
    entities: list[GroundedEntity]
    operations: list[Operation]
    spatial_relations: list[SpatialRelation]
    scene_id: str

    @model_validator(mode="after")
    def refs(self):
        ids = {entity.entity_id for entity in self.entities}
        objects = {entity.object_id for entity in self.entities}
        if len(ids) != len(self.entities) or len(objects) != len(self.entities):
            raise ValueError("grounded entity and object IDs must be unique")
        for op in self.operations:
            if any(value and value not in ids for value in [op.source, op.destination, op.target, op.reference]):
                raise ValueError("grounded operation references unknown entity")
        return self


class SkillStep(BaseModel):
    """Rich internal step. IDs, dependencies and object refs are Python-owned."""

    model_config = ConfigDict(extra="forbid")
    step_id: str = Field(pattern=r"^step-[0-9]+$")
    operation_id: str = Field(pattern=r"^op-[\w-]+$")
    skill_name: str = Field(min_length=1)
    target_object: str | None = None
    reference_object: str | None = None
    semantic_target: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class SkillPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_types: list[TaskType]
    steps: list[SkillStep]

    @model_validator(mode="after")
    def valid_steps(self):
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step_id must be unique")
        position = {step_id: index for index, step_id in enumerate(ids)}
        for step in self.steps:
            if step.step_id in step.depends_on:
                raise ValueError("step cannot depend on itself")
            if any(dep not in position or position[dep] >= position[step.step_id] for dep in step.depends_on):
                raise ValueError("depends_on must point to previous steps")
        return self
