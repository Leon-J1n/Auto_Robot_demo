from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SceneObject(BaseModel):
    # ``model_id``/``model_name`` are asset fields, not pydantic internals.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    object_id: str
    body_name: str
    role: str
    semantic_name: str
    # Public tabletop-local frame: the upper tabletop surface is z=0, so this is
    # the object's contact point.  ``scene.builder`` converts it to the platform
    # world frame; nothing in this module knows about world coordinates.
    position: tuple[float, float, float] = Field(default=(0.0, 0.0, 0.0), min_length=3, max_length=3)
    dimensions_m: tuple[float, float, float] | None = None
    entity_id: str | None = None
    candidate_for: str | None = None
    model_id: str | None = None
    model_name: str | None = None
    source: str = "generated"
    expected_visible: bool = True


class SceneRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    scene_id: str
    robot: str
    coordinate_frame: str = "tabletop_center"
    objects: list[SceneObject]
    bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_objects(self):
        object_ids = [item.object_id for item in self.objects]
        body_names = [item.body_name for item in self.objects]
        if len(object_ids) != len(set(object_ids)) or len(body_names) != len(set(body_names)):
            raise ValueError("scene object_id and body_name must be unique")
        known = set(object_ids)
        if not set(self.bindings.values()) <= known:
            raise ValueError("scene binding references unknown object")
        return self

    def by_object_id(self, object_id: str) -> SceneObject:
        return next(item for item in self.objects if item.object_id == object_id)


class DiscoveredSceneObject(BaseModel):
    """A task object found in an already-saved scene (Route B)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    object_id: str
    body_name: str
    role: str = "target"
    semantic_name: str | None = None
    expected_visible: bool = True
    model_id: str | None = None
    model_name: str | None = None
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    dimensions_m: tuple[float, float, float] | None = None


class DiscoveredSceneRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    scene_id: str | None = None
    robot: str | None = None
    objects: list[DiscoveredSceneObject]
