"""Vision-grounding contract. The model returns bboxes only, never world poses."""
from __future__ import annotations

from typing import Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisionCandidate(StrictModel):
    detection_id: str | None = Field(default=None, min_length=1, validation_alias=AliasChoices("detection_id", "id"))
    entity_id: str = Field(min_length=1, validation_alias=AliasChoices("entity_id", "entity"))
    bbox: list[int] = Field(min_length=4, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_input(cls, value):
        # Older callers supplied confidence and omitted detection_id.  Keep
        # that input boundary tolerant while never retaining confidence in the
        # minimal model output sent through the pipeline.
        if isinstance(value, dict):
            value = dict(value)
            value.pop("confidence", None)
            if value.get("id") or value.get("detection_id"):
                value.setdefault("detection_id", value.get("id") or value.get("detection_id"))
            value.setdefault("entity_id", value.get("entity") or value.get("entity_id"))
            value.pop("id", None)
            value.pop("entity", None)
        return value

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value):
        if any(item < 0 or item > 1000 for item in value) or value[0] >= value[2] or value[1] >= value[3]:
            raise ValueError("bbox must be normalized yxyx 0..1000")
        return value


class VisionLLMOutput(StrictModel):
    detections: list[VisionCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_detections(self):
        ids = [item.detection_id for item in self.detections if item.detection_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("detection_id must be unique")
        return self


class VisionQuery(StrictModel):
    id: str
    name: str
    category: str
    color: str | None = None
    all: bool = False


class VisionGroundingRequest(StrictModel):
    entities: list[VisionQuery]
    rgb_path: str


class VisionGroundingProvider(Protocol):
    def detect(self, request: VisionGroundingRequest) -> VisionLLMOutput: ...


VisionDetection = VisionCandidate
VisionDetectionResult = VisionLLMOutput
