from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pydantic import Field, model_validator

from .commands import StrictModel


# The platform world frame replaces the stack's MJCF-derived frame name.  Files
# authored against the old stack still say "mujoco_world"; they are accepted and
# normalized so downstream layers only ever see one frame name.
PLATFORM_WORLD_FRAME = "platform_world"
LEGACY_WORLD_FRAMES = frozenset({"mujoco_world"})

# The stack distinguished MJCF source kinds (body/site/geom/joint).  The
# platform only has named entities, so legacy kinds are accepted on input and
# collapsed to a single "entity" concept.  See README.md in this package.
ENTITY_SOURCE_TYPE = "entity"
LEGACY_SOURCE_TYPES = frozenset({"body", "site", "geom", "joint"})


def normalize_spatial_source(source: dict[str, Any]) -> dict[str, Any]:
    """Collapse a legacy MJCF spatial source onto the platform entity concept."""
    kind = source.get("type")
    if kind in (None, ENTITY_SOURCE_TYPE):
        return {**source, "type": ENTITY_SOURCE_TYPE}
    if kind not in LEGACY_SOURCE_TYPES:
        raise ValueError(
            f"spatial source type {kind!r} is not a platform entity or a legacy MJCF kind"
        )
    return {**source, "type": ENTITY_SOURCE_TYPE, "legacy_type": kind}


class InteractionRegistryDocument(StrictModel):
    version: int | None = None
    registry_version: int | None = None
    schema_version: str = "1.0"
    coordinate_frame: str = PLATFORM_WORLD_FRAME
    scene: str
    scene_fingerprint: str | None = None
    move_defaults: dict[str, Any] = Field(default_factory=dict)
    named_targets: dict[str, Any] = Field(default_factory=dict)
    objects: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_authoring(cls, value):
        if not isinstance(value, dict):
            return value
        value = deepcopy(value)
        if value.get("coordinate_frame") in LEGACY_WORLD_FRAMES:
            value["coordinate_frame"] = PLATFORM_WORLD_FRAME
        if value.get("coordinate_frame") is None:
            value.pop("coordinate_frame", None)
        objects = value.get("objects")
        if isinstance(objects, dict):
            for obj in objects.values():
                if not isinstance(obj, dict):
                    continue
                spatial = obj.get("spatial")
                if isinstance(spatial, dict) and isinstance(spatial.get("source"), dict):
                    spatial["source"] = normalize_spatial_source(spatial["source"])
        return value


class InteractionRegistryError(ValueError):
    pass


class InteractionRegistry:
    """Validated read-only access to scene interaction metadata."""

    def __init__(self, path: str | Path, *, scene_path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.document = InteractionRegistryDocument.model_validate(raw)
        self.data = self.document.model_dump(exclude_none=True)
        self._scene_path_override = Path(scene_path).resolve() if scene_path else None
        self._validate_semantics()

    @property
    def scene_path(self) -> Path:
        if self._scene_path_override is not None:
            return self._scene_path_override
        value = Path(self.document.scene)
        return (value if value.is_absolute() else self.path.parent / value).resolve()

    @property
    def coordinate_frame(self) -> str:
        return self.document.coordinate_frame

    @property
    def objects(self) -> Mapping[str, Any]:
        return self.data["objects"]

    @property
    def named_targets(self) -> Mapping[str, Any]:
        return self.data.get("named_targets", {})

    def object(self, key: str) -> dict[str, Any]:
        try:
            return deepcopy(self.objects[key])
        except KeyError as exc:
            raise InteractionRegistryError(f"scene has no object {key!r}") from exc

    def action_request(self, key: str, action: str) -> dict[str, Any]:
        try:
            return deepcopy(self.objects[key]["action_requests"][action])
        except KeyError as exc:
            raise InteractionRegistryError(f"object {key!r} does not support {action!r}") from exc

    def _validate_semantics(self) -> None:
        for key, obj in self.objects.items():
            if not isinstance(obj, dict) or not obj.get("aliases"):
                raise InteractionRegistryError(f"object {key!r} requires aliases")
            spatial = obj.get("spatial")
            if not isinstance(spatial, dict) or not spatial.get("source") or not spatial.get("reference_pose"):
                raise InteractionRegistryError(f"object {key!r} requires spatial source and reference pose")
            anchors = spatial.get("anchors")
            if not isinstance(anchors, dict) or spatial.get("default_anchor") not in anchors:
                raise InteractionRegistryError(f"object {key!r} has invalid default anchor")
            if not spatial.get("tool_orientation"):
                raise InteractionRegistryError(f"object {key!r} requires tool orientation")
        for owner, obj in self.objects.items():
            for action, spec in obj.get("affordances", {}).items():
                acting = str(spec.get("acting_target", owner))
                contact = spec.get("contact_target")
                if acting not in self.objects or contact not in self.objects:
                    raise InteractionRegistryError(f"object {owner!r} affordance {action!r} references unknown object")
                if action not in self.objects[acting].get("action_requests", {}):
                    raise InteractionRegistryError(f"acting target {acting!r} lacks {action!r} request")


# Requested public name.
InteractionRegistryModel = InteractionRegistryDocument
