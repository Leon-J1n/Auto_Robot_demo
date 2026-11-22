"""Bind the entities a user named to the objects that actually exist in the scene.

Route A is deterministic and needs no perception: the service composed the scene, so the
entity -> object mapping is already recorded in the scene plan and is simply read back
(`asset_scene_binding`). Route B starts from a scene nobody here authored, so the binding comes
from an authored interaction registry when one exists, and otherwise fails with an explicit
blocker -- visual grounding is not available on this platform yet, and guessing a binding would
silently act on the wrong object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .planning.contracts import GroundedEntity, GroundedTask, TaskIntent


class GroundingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SceneObjectView:
    """The minimum a scene object must expose to be bindable."""

    object_id: str
    body_name: str | None = None
    semantic_name: str | None = None
    model_id: str | None = None
    model_name: str | None = None
    aliases: tuple[str, ...] = ()
    capabilities: frozenset[str] | None = None


def _views(plan: Any) -> list[SceneObjectView]:
    result: list[SceneObjectView] = []
    for placement in list(getattr(plan, "placements", ()) or ()):
        result.append(SceneObjectView(
            object_id=str(getattr(placement, "object_id", "")),
            body_name=getattr(placement, "body_name", None),
            semantic_name=getattr(placement, "semantic_name", None),
            model_id=getattr(placement, "model_id", None),
            model_name=getattr(placement, "model_name", None),
            aliases=tuple(getattr(placement, "aliases", ())),
            capabilities=getattr(placement, "capabilities", None)))
    for discovered in list(getattr(plan, "discovered_objects", ()) or ()):
        if isinstance(discovered, str):
            # A discovered Route B object is recorded as its scene entity name, which is also
            # the only id the rest of the pipeline can address it by.
            result.append(SceneObjectView(object_id=discovered, body_name=discovered,
                                          semantic_name=discovered))
        elif isinstance(discovered, Mapping):
            name = discovered.get("body_name") or discovered.get("name")
            result.append(SceneObjectView(
                object_id=str(discovered.get("object_id") or discovered.get("id")
                              or name or ""),
                body_name=name,
                semantic_name=discovered.get("semantic_name") or name))
        else:
            name = getattr(discovered, "body_name", None) or getattr(discovered, "name", None)
            result.append(SceneObjectView(
                object_id=str(getattr(discovered, "object_id", None) or name or ""),
                body_name=name,
                semantic_name=getattr(discovered, "semantic_name", None) or name))
    return [view for view in result if view.object_id]


def _normalize(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _match_by_name(entity: Any, views: Iterable[SceneObjectView], *,
                   container_only: bool = False) -> SceneObjectView | None:
    names = {_normalize(entity.semantic_name)}
    names.update(_normalize(alias) for alias in getattr(entity, "aliases", ()) or ())
    names.discard("")
    # Placements and discovered_objects may describe the same body. Prefer the
    # placement, which retains catalog aliases and capability information.
    unique: dict[str, SceneObjectView] = {}
    for view in views:
        unique.setdefault(view.object_id, view)
    candidates = [view for view in unique.values()
                  if not container_only or view.capabilities is None
                  or "container" in view.capabilities]
    matches = []
    for view in candidates:
        haystack = {_normalize(value) for value in
                    (view.semantic_name, view.body_name, view.object_id, view.model_name, *view.aliases)
                    if value}
        if names & haystack:
            matches.append(view)
    if matches:
        return _unique_match(entity, matches)
    # Fall back to containment so "红色方块" still finds "red_cube_1" style names.
    for view in candidates:
        for name in names:
            for value in (view.semantic_name, view.body_name, view.object_id, view.model_name, *view.aliases):
                if value and (name in _normalize(value) or _normalize(value) in name):
                    matches.append(view)
                    break
    return _unique_match(entity, matches)


def _unique_match(entity: Any, matches: list[SceneObjectView]) -> SceneObjectView | None:
    unique = {view.object_id: view for view in matches}
    if len(unique) > 1:
        raise GroundingError("TARGET_AMBIGUOUS",
                             f"“{entity.semantic_name}”匹配到多个物体：{'、'.join(unique)}，请指定具体名称。")
    return next(iter(unique.values()), None)


def ground_task(intent: TaskIntent, plan: Any, *,
                authored_registry: Mapping[str, Any] | None = None) -> GroundedTask:
    """Resolve every entity the instruction referenced to a concrete scene object."""
    route = str(getattr(plan, "route", "A")).upper()
    scene_id = str(getattr(plan, "scene_id", "main"))
    bindings = dict(getattr(plan, "bindings", {}) or {})
    views = _views(plan)
    by_object = {view.object_id: view for view in views}

    # Only entities an operation actually references need a binding; a distractor left in the
    # scene deliberately has none.
    referenced = {value for operation in intent.operations
                  for value in (operation.source, operation.destination,
                                operation.target, operation.reference) if value}
    if not referenced:
        raise GroundingError("INVALID_REQUEST", "指令中没有可执行的目标对象。")

    registry_objects = dict((authored_registry or {}).get("objects", {}) or {})
    method = "asset_scene_binding" if route == "A" else "interaction_registry"
    entities: list[GroundedEntity] = []
    used: set[str] = set()
    for entity in intent.entities:
        if entity.entity_id not in referenced:
            continue
        view: SceneObjectView | None = None
        object_id = bindings.get(entity.entity_id)
        if object_id:
            view = by_object.get(str(object_id))
        if view is None and registry_objects:
            for key in registry_objects:
                if _normalize(key) == _normalize(entity.semantic_name):
                    view = by_object.get(key) or SceneObjectView(object_id=key,
                                                                 semantic_name=key)
                    break
        if view is None:
            view = _match_by_name(entity, [item for item in views
                                           if item.object_id not in used],
                                  container_only=any(operation.destination == entity.entity_id
                                                     for operation in intent.operations))
        if view is None:
            if route == "B" and not registry_objects:
                # Route B without an authored registry cannot be grounded here; say so rather
                # than act on a guess.
                raise GroundingError(
                    "PERCEPTION_REQUIRED",
                    f"场景中找不到“{entity.semantic_name}”，且该场景缺少可用的交互元数据"
                    "（.interactions.json）。")
            raise GroundingError(
                "TARGET_NOT_FOUND", f"场景中找不到“{entity.semantic_name}”。")
        used.add(view.object_id)
        entities.append(GroundedEntity(
            entity_id=entity.entity_id,
            semantic_name=entity.semantic_name,
            object_id=view.object_id,
            body_name=view.body_name or view.object_id,
            model_id=view.model_id,
            model_name=view.model_name,
            grounding_method=method))

    return GroundedTask(
        instruction=intent.instruction,
        task_types=list(intent.task_types),
        entities=entities,
        operations=list(intent.operations),
        spatial_relations=list(intent.spatial_relations),
        scene_id=scene_id)
