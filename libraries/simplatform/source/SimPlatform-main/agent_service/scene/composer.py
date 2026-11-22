"""Seeded Route A scene composition in the tabletop-local frame.

The placement algorithm is reproducible: the same ``seed`` with the same intent
and assets always yields the same positions.  Every numeric constant here is
part of that contract, so none of them may be tuned without invalidating
previously recorded scenes.

Intent objects are duck-typed on purpose.  ``planning/`` owns the ``TaskIntent``
contracts; this module only needs ``entities`` (``entity_id``, ``semantic_name``)
and ``spatial_relations`` (``subject``, ``relation``, ``reference``).  Relations
are compared as strings so both a ``StrEnum`` member and a plain string work.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

from .registry import SceneObject, SceneRegistry

# Keep generated objects in the central 70% of the tabletop.  The original
# tabletop-local bounds are +/-0.375m by +/-0.75m; using 70% of each full span
# leaves a 15% perimeter margin while still retaining the same origin.
TABLETOP_X = (-0.375, 0.375)
TABLETOP_Y = (-0.75, 0.75)
WORKSPACE_X = tuple(value * 0.70 for value in TABLETOP_X)
WORKSPACE_Y = tuple(value * 0.70 for value in TABLETOP_Y)
MIN_GAP_M = 0.04

# Unary direction relations and their presets, in the tabletop-local frame.
LEFT = "left"
RIGHT = "right"
FRONT = "front"
BACK = "back"
UP = "up"
DOWN = "down"

# Binary relations place a subject relative to a reference.
LEFT_OF = "left_of"
RIGHT_OF = "right_of"
FRONT_OF = "front_of"
BEHIND = "behind"
ABOVE = "above"
BELOW = "below"

# Selection relations pick one of two instances rather than placing one.
NEAREST = "nearest"
FARTHEST = "farthest"

BINARY_RELATIONS = frozenset({LEFT_OF, RIGHT_OF, FRONT_OF, BEHIND, ABOVE, BELOW})
SELECTION_RELATIONS = frozenset({NEAREST, FARTHEST})

DIRECTION_PRESETS = {
    LEFT: (-0.22, 0.0, 0.0),
    RIGHT: (0.22, 0.0, 0.0),
    FRONT: (0.0, 0.38, 0.0),
    BACK: (0.0, -0.38, 0.0),
    UP: (0.0, 0.0, 0.20),
    DOWN: (0.0, 0.0, 0.02),
}

UNARY_RELATIONS = frozenset(DIRECTION_PRESETS)

PRIMITIVE_DIMENSIONS = {
    "cube_basic": (0.06, 0.06, 0.06),
    "open_box": (0.18, 0.18, 0.10),
    "button_basic": (0.08, 0.08, 0.04),
}

# Random fallback sampling budget; with the preferred and unary candidates this
# gives at most 202 attempts before composition fails.
RANDOM_SAMPLES = 200


class SceneAssetLike(Protocol):
    model_id: str | None
    model_name: str | None
    dimensions_m: tuple[float, float, float] | None


@dataclass(frozen=True)
class SceneAsset:
    """Minimal asset record for callers without the planner's asset registry."""

    model_id: str
    model_name: str
    dimensions_m: tuple[float, float, float] | None = None
    aliases: tuple[str, ...] = field(default=())


class SceneComposer:
    def compose(
        self,
        intent: Any,
        assets: Mapping[str, SceneAssetLike],
        robot: str = "ur5e",
        seed: int = 0,
        distractor_assets: Mapping[str, SceneAssetLike] | None = None,
        distractor_count: int = 2,
    ) -> SceneRegistry:
        """Compose a scene and optionally add seeded, non-target distractors.

        ``assets`` contains models resolved for entities in the task intent.
        Callers that want a populated tabletop can pass the complete model pool
        as ``distractor_assets``; two distinct models are then selected from
        that pool after excluding all task models.  Distractors are deliberately
        unbound, so grounding and execution continue to address only intent
        entities.
        """
        if distractor_count < 0:
            raise ValueError("distractor_count must be non-negative")
        rng = random.Random(seed)
        selection_subjects = {
            relation.subject
            for relation in intent.spatial_relations
            if _relation(relation) in SELECTION_RELATIONS
        }
        objects: list[SceneObject] = []
        bindings: dict[str, str] = {}

        by_entity = {entity.entity_id: entity for entity in intent.entities}
        preferred_by_entity: dict[str, tuple[float, float, float]] = {}
        for relation in intent.spatial_relations:
            if not relation.reference or _relation(relation) not in BINARY_RELATIONS:
                continue
            subject_dimensions = _dimensions(assets[relation.subject])
            reference_dimensions = _dimensions(assets[relation.reference])
            subject_position, reference_position = self._relation_pair_positions(
                _relation(relation), subject_dimensions, reference_dimensions
            )
            preferred_by_entity.setdefault(relation.subject, subject_position)
            preferred_by_entity.setdefault(relation.reference, reference_position)
        placed: set[str] = set()
        placing: set[str] = set()

        def place(entity_id: str) -> None:
            if entity_id in placed or entity_id in selection_subjects:
                return
            entity = by_entity[entity_id]
            if entity_id in placing:
                raise ValueError("cyclic spatial relations are not supported")
            placing.add(entity_id)
            relation = next(
                (
                    relation for relation in intent.spatial_relations
                    if relation.subject == entity_id
                    and relation.reference
                    and _relation(relation) in BINARY_RELATIONS
                ),
                None,
            )
            preferred = preferred_by_entity.get(entity_id)
            if relation is not None:
                # Place the reference first so "on/in" relations resolve against
                # an object that already occupies the workspace.
                place(relation.reference)
            item = self._make_object(entity, assets[entity_id], 1, objects, rng, intent, preferred=preferred)
            objects.append(item)
            bindings[entity.entity_id] = item.object_id
            placed.add(entity_id)
            placing.discard(entity_id)

        for entity in intent.entities:
            place(entity.entity_id)

        for entity in [e for e in intent.entities if e.entity_id in selection_subjects]:
            relation = next(
                r for r in intent.spatial_relations
                if r.subject == entity.entity_id and _relation(r) in SELECTION_RELATIONS
            )
            reference = next(item for item in objects if item.object_id == bindings[relation.reference])
            # Put the selected (near) candidate at a deterministic offset
            # that is guaranteed to be closer than the distractor.
            # Build both candidates against the already-placed objects.  The
            # near candidate's preferred offset is tried first; if clipping
            # or workspace bounds prevent it, the seeded sampler still finds
            # a valid location and ranking below preserves the selection.
            near = self._make_object(
                entity, assets[entity.entity_id], 1, objects, rng, intent,
                preferred=(reference.position[0] + 0.12, reference.position[1]),
            )
            objects.append(near)
            far = self._make_object(
                entity, assets[entity.entity_id], 2, objects, rng, intent,
                preferred=(reference.position[0] - 0.26, reference.position[1]),
            )
            objects.append(far)
            ranked = sorted(
                [near, far],
                key=lambda item: _distance(item.position, reference.position),
                reverse=_relation(relation) == FARTHEST,
            )
            # Preserve the natural-language selection in the registry; the
            # distractor stays in the scene as an unbound object.
            bindings[entity.entity_id] = ranked[0].object_id

        if distractor_assets is not None and distractor_count:
            task_model_ids = {str(asset.model_id) for asset in assets.values()}
            candidates = [
                (model_id, asset) for model_id, asset in distractor_assets.items()
                if str(asset.model_id) not in task_model_ids
                and getattr(asset, "movable", False)
                and "graspable" in getattr(asset, "capabilities", ())
            ]
            if len(candidates) < distractor_count:
                raise ValueError(
                    f"model pool has only {len(candidates)} non-target models; "
                    f"{distractor_count} required"
                )
            for index, (model_id, asset) in enumerate(
                rng.sample(candidates, distractor_count), start=1
            ):
                distractor = _GeneratedEntity(
                    entity_id=f"distractor_{index:02d}",
                    semantic_name=getattr(asset, "model_name", None) or str(model_id),
                )
                item = self._make_object(
                    distractor, asset, 1, objects, rng, intent,
                )
                objects.append(item.model_copy(update={
                    "role": "distractor", "entity_id": None, "candidate_for": None,
                }))

        return SceneRegistry(scene_id=f"{robot}_generated_{seed}", robot=robot, objects=objects, bindings=bindings)

    @staticmethod
    def _relation_pair_positions(
        relation: str,
        subject_dimensions: tuple[float, float, float],
        reference_dimensions: tuple[float, float, float],
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        gap = MIN_GAP_M + 0.01
        subject = [0.0, 0.0, 0.0]
        reference = [0.0, 0.0, 0.0]
        if relation == LEFT_OF:
            separation = reference_dimensions[0] / 2 + subject_dimensions[0] / 2 + gap
            subject[0], reference[0] = -separation / 2, separation / 2
        elif relation == RIGHT_OF:
            separation = reference_dimensions[0] / 2 + subject_dimensions[0] / 2 + gap
            subject[0], reference[0] = separation / 2, -separation / 2
        elif relation == FRONT_OF:
            separation = reference_dimensions[1] / 2 + subject_dimensions[1] / 2 + gap
            subject[1], reference[1] = separation / 2, -separation / 2
        elif relation == BEHIND:
            separation = reference_dimensions[1] / 2 + subject_dimensions[1] / 2 + gap
            subject[1], reference[1] = -separation / 2, separation / 2
        elif relation == ABOVE:
            subject[2] = reference_dimensions[2] + gap
        elif relation == BELOW:
            reference[2] = subject_dimensions[2] + gap
        return tuple(subject), tuple(reference)

    def _make_object(self, entity, asset, index, existing, rng, intent, preferred=None) -> SceneObject:
        dimensions = _dimensions(asset)
        position = self._position(entity.entity_id, dimensions, existing, rng, intent, preferred)
        base = _slug(entity.semantic_name or asset.model_name)
        if base == "scene_object":
            # Non-Latin semantic names collapse to the generic slug.  Prefer
            # the model-provided stable entity id so two Chinese entities do
            # not both become scene_object_01.
            base = _slug(entity.entity_id or asset.model_name)
        object_id = f"{base}_{index:02d}"
        return SceneObject(
            object_id=object_id, body_name=object_id, role="task_object",
            semantic_name=entity.semantic_name, position=position, dimensions_m=dimensions,
            entity_id=entity.entity_id if index == 1 else None, candidate_for=entity.entity_id,
            model_id=asset.model_id, model_name=asset.model_name,
        )

    def _position(self, entity_id, dimensions, existing, rng, intent, preferred) -> tuple[float, float, float]:
        lower_x = math.ceil((WORKSPACE_X[0] + dimensions[0] / 2) * 1e6) / 1e6
        upper_x = math.floor((WORKSPACE_X[1] - dimensions[0] / 2) * 1e6) / 1e6
        lower_y = math.ceil((WORKSPACE_Y[0] + dimensions[1] / 2) * 1e6) / 1e6
        upper_y = math.floor((WORKSPACE_Y[1] - dimensions[1] / 2) * 1e6) / 1e6
        if lower_x > upper_x or lower_y > upper_y:
            raise ValueError(f"object {entity_id!r} does not fit within the tabletop placement region")
        unary = [
            _relation(r) for r in intent.spatial_relations
            if r.subject == entity_id and _relation(r) in UNARY_RELATIONS
        ]
        candidates: list[tuple[float, float, float]] = []
        if preferred is not None:
            candidates.append((preferred[0], preferred[1], preferred[2] if len(preferred) == 3 else 0.0))
        if unary:
            combined = [0.0, 0.0, 0.0]
            for direction in unary:
                for axis, value in enumerate(DIRECTION_PRESETS[direction]):
                    if value:
                        combined[axis] = value
            candidates.append(tuple(combined))
        # Consume the whole sample budget eagerly: the number of rng draws per
        # object is part of the seeded reproducibility contract.
        candidates.extend(
            (rng.uniform(*WORKSPACE_X), rng.uniform(*WORKSPACE_Y), 0.0) for _ in range(RANDOM_SAMPLES)
        )
        for x, y, z in candidates:
            x = max(lower_x, min(upper_x, x))
            y = max(lower_y, min(upper_y, y))
            candidate = (round(x, 6), round(y, 6), round(z, 6))
            if all(
                not _overlap(candidate, dimensions, item.position, item.dimensions_m or (0, 0, 0))
                for item in existing
            ):
                return candidate
        raise ValueError("unable to find a valid non-overlapping placement")


def _relation(relation: Any) -> str:
    """Relation name as a plain string, for StrEnum members and raw strings."""
    value = relation.relation if hasattr(relation, "relation") else relation
    return value.value if hasattr(value, "value") else str(value)


def _dimensions(asset: SceneAssetLike) -> tuple[float, float, float]:
    return asset.dimensions_m or PRIMITIVE_DIMENSIONS[asset.model_name]


def _overlap(a, ad, b, bd) -> bool:
    xy_overlap = (
        abs(a[0] - b[0]) < (ad[0] + bd[0]) / 2 + MIN_GAP_M
        and abs(a[1] - b[1]) < (ad[1] + bd[1]) / 2 + MIN_GAP_M
    )
    z_separated = a[2] >= b[2] + bd[2] + MIN_GAP_M or b[2] >= a[2] + ad[2] + MIN_GAP_M
    return xy_overlap and not z_separated


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug or "scene_object"


@dataclass(frozen=True)
class _GeneratedEntity:
    entity_id: str
    semantic_name: str
