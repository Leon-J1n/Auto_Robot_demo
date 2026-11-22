"""User-configurable mesh catalog for Route A scene composition."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

ROUTE_A_MODEL_CATALOG_ENV = "AGENT_SERVICE_ROUTE_A_MODEL_CATALOG"
DEFAULT_ROUTE_A_MODEL_CATALOG = (
    Path(__file__).resolve().parents[1] / "config" / "route_a_models.json"
)

logger = logging.getLogger(__name__)

SUPPORTED_CAPABILITIES = frozenset({"graspable", "container", "pressable"})


@dataclass(frozen=True)
class RouteACollisionShape:
    name: str
    shape: Literal["box", "sphere", "cylinder"]
    local_position_m: tuple[float, float, float]
    half_extents_m: tuple[float, float, float] | None = None
    radius_m: float | None = None
    half_length_m: float | None = None


@dataclass(frozen=True)
class RouteAModel:
    model_id: str
    model_name: str
    visual_obj: Path
    aliases: tuple[str, ...]
    categories: tuple[str, ...]
    dimensions_m: tuple[float, float, float]
    bbox_min_m: tuple[float, float, float]
    bbox_max_m: tuple[float, float, float]
    movable: bool
    capabilities: tuple[str, ...]
    collision_shapes: tuple[RouteACollisionShape, ...]
    source: str = "mesh"
    collision_stls: tuple[Path, ...] = ()
    texture: Path | None = None
    grasp_pose: Path | None = None


@dataclass(frozen=True)
class ResolvedRouteAModels:
    by_entity: Mapping[str, RouteAModel]
    by_model_id: Mapping[str, RouteAModel]


@dataclass(frozen=True)
class RouteAModelCatalog:
    base_scene: Path
    models: tuple[RouteAModel, ...]
    placement_seed: int | None = None

    def resolve_entities(self, entities: Iterable[Any]) -> ResolvedRouteAModels:
        by_entity: dict[str, RouteAModel] = {}
        by_model_id: dict[str, RouteAModel] = {}
        for entity in entities:
            entity_id = str(getattr(entity, "entity_id", "") or "")
            if not entity_id:
                raise ValueError("Route A entity is missing entity_id")
            model = self._resolve_entity(entity)
            by_entity[entity_id] = model
            by_model_id[model.model_id] = model
        logger.debug(
            "Resolved Route A catalog models: entity_count=%d model_ids=%s",
            len(by_entity),
            sorted(by_model_id),
        )
        return ResolvedRouteAModels(by_entity=by_entity, by_model_id=by_model_id)

    def resolve_intent(self, intent: Any) -> ResolvedRouteAModels:
        resolved = self.resolve_entities(getattr(intent, "entities", ()) or ())
        for operation in getattr(intent, "operations", ()) or ():
            source = getattr(operation, "source", None)
            destination = getattr(operation, "destination", None)
            if source:
                self._require_capability(resolved, str(source), "graspable", "source")
            if destination:
                self._require_capability(resolved, str(destination), "container", "destination")
        return resolved

    def _resolve_entity(self, entity: Any) -> RouteAModel:
        names = [
            getattr(entity, "semantic_name", ""),
            getattr(entity, "entity_id", ""),
            *(getattr(entity, "aliases", ()) or ()),
        ]
        requested = {_normalize(value) for value in names if str(value or "").strip()}
        exact = [model for model in self.models if requested & _model_names(model)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError(f"Route A entity {getattr(entity, 'entity_id', '')!r} is ambiguous")

        category_matches = [
            model for model in self.models
            if (requested | {_normalize(getattr(entity, "category", "") or "")})
            & {_normalize(value) for value in model.categories}
        ]
        if len(category_matches) == 1:
            return category_matches[0]
        raise ValueError(f"no Route A model matches entity {getattr(entity, 'entity_id', '')!r}")

    @staticmethod
    def _require_capability(
        resolved: ResolvedRouteAModels, entity_id: str, capability: str, role: str
    ) -> None:
        model = resolved.by_entity.get(entity_id)
        if model is None:
            raise ValueError(f"operation {role} {entity_id!r} is not a resolved entity")
        if capability not in model.capabilities:
            raise ValueError(f"operation {role} {entity_id!r} must be {capability}")


def load_route_a_model_catalog(path: str | Path | None = None) -> RouteAModelCatalog:
    config_path = _catalog_path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load Route A model catalog: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Route A model catalog version must be 1")

    base_scene = _existing_path(data.get("baseScene"), config_path, "baseScene")
    raw_models = data.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("Route A model catalog models must be a non-empty list")

    models = tuple(_parse_model(item, config_path) for item in raw_models)
    _validate_unique_models(models)
    placement_seed = data.get("placementSeed")
    if placement_seed is not None and (not isinstance(placement_seed, int) or isinstance(placement_seed, bool)):
        raise ValueError("placementSeed must be an integer or null")
    logger.debug("Loaded Route A model catalog: model_count=%d", len(models))
    return RouteAModelCatalog(
        base_scene=base_scene,
        models=models,
        placement_seed=placement_seed,
    )


def _catalog_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get(ROUTE_A_MODEL_CATALOG_ENV)
    return Path(override).expanduser() if override else DEFAULT_ROUTE_A_MODEL_CATALOG


def _parse_model(raw: Any, config_path: Path) -> RouteAModel:
    if not isinstance(raw, dict):
        raise ValueError("each Route A model must be an object")
    model_id = _required_text(raw, "id")
    model_name = _required_text(raw, "name")
    dimensions = _positive_vector(raw.get("dimensionsM"), f"model {model_id} dimensionsM")
    bounds = raw.get("boundsM")
    if not isinstance(bounds, dict):
        raise ValueError(f"model {model_id} boundsM must contain min and max")
    minimum = _vector(bounds.get("min"), f"model {model_id} boundsM.min")
    maximum = _vector(bounds.get("max"), f"model {model_id} boundsM.max")
    if any(low >= high for low, high in zip(minimum, maximum)):
        raise ValueError(f"model {model_id} boundsM max must exceed min")
    spans = tuple(high - low for low, high in zip(minimum, maximum))
    if any(abs(span - dimension) > 1e-9 for span, dimension in zip(spans, dimensions)):
        raise ValueError(f"model {model_id} dimensionsM must match boundsM")

    aliases = _text_list(raw.get("aliases"), f"model {model_id} aliases")
    categories = _text_list(raw.get("categories"), f"model {model_id} categories")
    capabilities = _capability_list(
        raw.get("capabilities"), f"model {model_id} capabilities")
    unknown_capabilities = set(capabilities) - SUPPORTED_CAPABILITIES
    if unknown_capabilities:
        raise ValueError(
            f"model {model_id} has unsupported capabilities: {sorted(unknown_capabilities)}")
    collision_shapes = _collision_shapes(raw.get("collisionShapes"), model_id)
    movable = raw.get("movable")
    if not isinstance(movable, bool):
        raise ValueError(f"model {model_id} movable must be boolean")
    collision_stls = tuple(
        _existing_path(item, config_path, f"model {model_id} collision STL")
        for item in raw.get("collisionStls", [])
    )
    texture = (_existing_path(raw["texture"], config_path, f"model {model_id} texture")
               if raw.get("texture") is not None else None)
    grasp_pose = (_existing_path(raw["graspPose"], config_path, f"model {model_id} graspPose")
                  if raw.get("graspPose") is not None else None)
    return RouteAModel(
        model_id=model_id,
        model_name=model_name,
        visual_obj=_existing_path(raw.get("path"), config_path, f"model {model_id} path"),
        aliases=aliases,
        categories=categories,
        dimensions_m=dimensions,
        bbox_min_m=minimum,
        bbox_max_m=maximum,
        movable=movable,
        capabilities=capabilities,
        collision_shapes=collision_shapes,
        collision_stls=collision_stls,
        texture=texture,
        grasp_pose=grasp_pose,
    )


def _collision_shapes(value: Any, model_id: str) -> tuple[RouteACollisionShape, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"model {model_id} collisionShapes must be a non-empty list")
    result: list[RouteACollisionShape] = []
    names: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError(f"model {model_id} collisionShapes[{index}] must be an object")
        name = _required_text(raw, "name")
        if name in names:
            raise ValueError(f"model {model_id} has duplicate collision shape: {name}")
        names.add(name)
        shape = _required_text(raw, "shape").casefold()
        if shape not in {"box", "sphere", "cylinder"}:
            raise ValueError(f"model {model_id} collision shape {name} is unsupported")
        local = _vector(
            raw.get("localPositionM"),
            f"model {model_id} collision shape {name} localPositionM",
        )
        half_extents = None
        radius = None
        half_length = None
        if shape == "box":
            size = _positive_vector(
                raw.get("sizeM"), f"model {model_id} collision shape {name} sizeM")
            half_extents = tuple(item / 2.0 for item in size)
        else:
            raw_radius = raw.get("radiusM")
            if isinstance(raw_radius, bool) or not isinstance(raw_radius, (int, float)) or raw_radius <= 0:
                raise ValueError(
                    f"model {model_id} collision shape {name} radiusM must be positive")
            radius = float(raw_radius)
            if shape == "cylinder":
                raw_length = raw.get("lengthM")
                if isinstance(raw_length, bool) or not isinstance(raw_length, (int, float)) or raw_length <= 0:
                    raise ValueError(
                        f"model {model_id} collision shape {name} lengthM must be positive")
                half_length = float(raw_length) / 2.0
        result.append(RouteACollisionShape(
            name=name,
            shape=shape,  # type: ignore[arg-type]
            local_position_m=local,
            half_extents_m=half_extents,
            radius_m=radius,
            half_length_m=half_length,
        ))
    return tuple(result)


def _validate_unique_models(models: tuple[RouteAModel, ...]) -> None:
    ids: set[str] = set()
    aliases: dict[str, str] = {}
    for model in models:
        if model.model_id in ids:
            raise ValueError(f"duplicate model id: {model.model_id}")
        ids.add(model.model_id)
        for value in (model.model_id, model.model_name, *model.aliases):
            normalized = _normalize(value)
            owner = aliases.get(normalized)
            if owner is not None and owner != model.model_id:
                raise ValueError(f"duplicate model alias: {value}")
            aliases[normalized] = model.model_id


def _model_names(model: RouteAModel) -> set[str]:
    return {_normalize(value) for value in (model.model_id, model.model_name, *model.aliases)}


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("_", " ").split())


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Route A model {key} must be a non-empty string")
    return value.strip()


def _text_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be a non-empty string list")
    return tuple(item.strip() for item in value)


def _capability_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a string list")
    return tuple(item.strip() for item in value)


def _vector(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must have three numbers")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError(f"{label} must have three numbers")
    return tuple(float(item) for item in value)


def _positive_vector(value: Any, label: str) -> tuple[float, float, float]:
    vector = _vector(value, label)
    if any(item <= 0 for item in vector):
        raise ValueError(f"{label} values must be positive")
    return vector


def _existing_path(value: Any, config_path: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    if not candidate.is_file():
        raise ValueError(f"{label} does not exist")
    return candidate
