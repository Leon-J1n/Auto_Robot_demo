"""Resolve the entities an instruction names to composable scene models.

Route A composes a scene from primitives, so every entity the planner produced needs a model
before `SceneComposer` can place it. The mapping is deterministic and semantic only: it reads the
category and name the model already assigned and never invents geometry, positions or poses.

Three primitive models exist, matching the footprints `composer.PRIMITIVE_DIMENSIONS` knows:
a small graspable cube, an open container, and a pressable button. Anything unrecognised becomes
a cube, because a graspable box is the one shape every skill can act on.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .composer import PRIMITIVE_DIMENSIONS, SceneAsset

CUBE = "cube_basic"
CONTAINER = "open_box"
BUTTON = "button_basic"

# Matched against the category and the name, in both Chinese and English. Ordered by
# specificity: a "button" wins over a "box" when a name somehow contains both.
_BUTTON_TERMS = ("button", "key", "switch", "press", "按钮", "按键", "开关", "电源")
_CONTAINER_TERMS = ("container", "tray", "box", "bin", "basket", "bowl", "crate", "holder",
                    "plate", "托盘", "盘", "盒", "箱", "篮", "碗", "容器", "框")
_CUBE_TERMS = ("cube", "block", "brick", "box_small", "方块", "块", "积木", "立方")


def _text(entity: Any) -> str:
    parts = [str(getattr(entity, "category", "") or ""),
             str(getattr(entity, "semantic_name", "") or ""),
             str(getattr(entity, "entity_id", "") or "")]
    parts.extend(str(alias) for alias in (getattr(entity, "aliases", ()) or ()))
    return " ".join(parts).casefold()


def model_for_name(name: str) -> str:
    """Pick a model from a bare name, for an object discovered in a live scene.

    Inference from naming, and only that: a live scene reports geometry but not intent, so a
    tray has to be recognised by what it is called before it can be given an interior anchor
    rather than a grasp point.
    """
    text = str(name or "").casefold()
    if any(term in text for term in _BUTTON_TERMS):
        return BUTTON
    if any(term in text for term in _CONTAINER_TERMS):
        if not any(term in text for term in _CUBE_TERMS):
            return CONTAINER
    return CUBE


def model_for(entity: Any) -> str:
    """Pick the primitive model that can carry this entity's role."""
    text = _text(entity)
    if any(term in text for term in _BUTTON_TERMS):
        return BUTTON
    if any(term in text for term in _CONTAINER_TERMS):
        # A "box" named as the thing being picked up is a cube, not a container.
        if not any(term in text for term in _CUBE_TERMS):
            return CONTAINER
    return CUBE


def asset_for(entity: Any) -> SceneAsset:
    model = model_for(entity)
    entity_id = str(getattr(entity, "entity_id", "") or model)
    return SceneAsset(model_id=f"primitive:{model}", model_name=model,
                      dimensions_m=PRIMITIVE_DIMENSIONS[model],
                      aliases=tuple(str(alias) for alias
                                    in (getattr(entity, "aliases", ()) or ())) or (entity_id,))


def resolve_entities(entities: Iterable[Any]) -> dict[str, SceneAsset]:
    """Map every entity id to a model, which is what `SceneComposer.compose` expects.

    Every entity gets an entry, including the extra instances a nearest/farthest relation
    creates, because the composer looks each one up by id while placing.
    """
    resolved: dict[str, SceneAsset] = {}
    for entity in entities:
        entity_id = str(getattr(entity, "entity_id", "") or "")
        if entity_id:
            resolved[entity_id] = asset_for(entity)
    return resolved


def resolve_intent(intent: Any) -> dict[str, SceneAsset]:
    return resolve_entities(getattr(intent, "entities", ()) or ())


def describe(assets: Mapping[str, SceneAsset]) -> dict[str, str]:
    """Diagnostic view: which model each entity became."""
    return {key: value.model_name for key, value in assets.items()}
