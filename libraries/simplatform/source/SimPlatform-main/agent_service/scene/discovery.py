"""Discover Route B task objects from a scene snapshot.

The source stack walked MJCF ``worldbody`` elements.  The platform has no XML,
so this operates on a flat list of plain entity records taken from a scene
snapshot; the caller supplies them and this module never touches the simulator.

Exclude whole infrastructure subtrees, then keep the topmost remaining geometry
owner. Imported objects may own geometry through descendants of a rigid-body
root; preserve that root's identity rather than exposing its mesh parts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .registry import DiscoveredSceneObject, DiscoveredSceneRegistry

INFRASTRUCTURE_NAMES = frozenset({
    "base", "link0", "robot", "robot_pedestal", "work_table",
    "table", "overhead_camera_mount", "wrist_camera_mount",
    "camera", "scene_camera", "floor", "ground", "fixture",
    "support", "lighting",
})

ROBOT_TOKENS = (
    "link", "joint", "gripper", "robotiq", "camera", "mount",
    "pedestal", "shoulder", "upper_arm", "forearm", "wrist",
)

SCENERY_TOKENS = ("table", "floor", "ground", "light")


@dataclass(frozen=True)
class SceneEntityRecord:
    """One entity from a scene snapshot, in platform terms."""

    entity_id: int | str
    name: str
    parent: int | str | None = None
    has_geometry: bool = False
    position: tuple[float, float, float] | None = None
    dimensions_m: tuple[float, float, float] | None = None
    model_name: str | None = None
    # True when the snapshot marks this entity as a robot root. Structural, so it excludes an
    # arm whose links are named in a way the token lists do not anticipate.
    is_robot: bool = False
    has_rigidbody: bool = False


def coerce_entity_records(records: Iterable[Any]) -> list[SceneEntityRecord]:
    """Accept dicts or objects from a snapshot and normalize them.

    ``has_geometry`` is true when the snapshot reports either a visual or a
    collider component, under any of the spellings the gateway snapshot uses.
    """
    result: list[SceneEntityRecord] = []
    for record in records:
        if isinstance(record, SceneEntityRecord):
            result.append(record)
            continue
        get = record.get if isinstance(record, Mapping) else lambda key, default=None: getattr(record, key, default)
        visual = get("visual")
        collider = get("collider")
        geometry = get("has_geometry")
        if geometry is None:
            geometry = bool(get("has_visual") or get("has_collider") or visual or collider)
        # A robot root carries a "robot" component in the native snapshot; the gateway
        # snapshot additionally reports it through flags.
        flags = get("flags") or ()
        robot = bool(get("is_robot") or get("robot") is not None
                     or "robot" in tuple(flags) or "joint" in tuple(flags))
        result.append(SceneEntityRecord(
            entity_id=get("entity_id", get("id")),
            name=str(get("name") or ""),
            parent=get("parent", get("parent_id")),
            has_geometry=bool(geometry),
            # A live scene measures its objects, so read the pose and size the snapshot
            # actually reports rather than only the flat keys an authored file would carry.
            # Without this both come back None and every discovered object looks dimensionless.
            position=_triple(get("position")) or _world_translation(get("world")),
            # Extents come from the transform scale, not the component size: a platform
            # primitive is unit geometry scaled to its real dimensions, so every
            # `visual.size` reads [1, 1, 1]. This position is the geometric centre the
            # snapshot reports, left as-is; converting to a resting-contact point is the
            # plan layer's job.
            dimensions_m=(_triple(get("dimensions_m", get("dimensions")))
                          or _transform_extents(get("world"))
                          or _transform_extents(get("local"))),
            model_name=get("model_name"),
            is_robot=robot,
            has_rigidbody=bool(get("has_rigidbody")
                               or any(str(flag).casefold().replace("_", "") == "rigidbody" for flag in flags)
                               or get("rigidBody") is not None
                               or get("rigid_body") is not None
                               or get("rigidbody") is not None),
        ))
    return result


def discover_task_objects(records: Iterable[Any]) -> list[SceneEntityRecord]:
    """Return the task-object entities of a saved scene, in snapshot order."""
    entities = coerce_entity_records(records)
    known = {entity.entity_id for entity in entities}
    children: dict[Any, list[SceneEntityRecord]] = {}
    roots: list[SceneEntityRecord] = []
    for entity in entities:
        if entity.parent is None or entity.parent not in known or entity.parent == entity.entity_id:
            roots.append(entity)
        else:
            children.setdefault(entity.parent, []).append(entity)

    found: list[SceneEntityRecord] = []

    def is_excluded(entity: SceneEntityRecord) -> bool:
        low = entity.name.casefold()
        return (
            entity.is_robot or low.startswith("__trajectory")
            # A robot root excludes its whole subtree structurally, which catches links the
            # name tokens miss -- "ur5e_base" matches none of them, and the source heuristic
            # only excluded the exact name "base".
            or low in INFRASTRUCTURE_NAMES
            or any(token in low for token in ROBOT_TOKENS)
            or any(token in low for token in SCENERY_TOKENS)
        )

    def owns_geometry(entity: SceneEntityRecord) -> bool:
        if is_excluded(entity):
            return False
        return entity.has_geometry or any(
            owns_geometry(child) for child in children.get(entity.entity_id, ())
        )

    def walk(entity: SceneEntityRecord, excluded: bool = False, task_parent: bool = False) -> None:
        subtree_excluded = excluded or is_excluded(entity)
        # A direct entity with geometry is a useful task object.  A nested entity
        # under an infrastructure subtree is never task data.
        if (not subtree_excluded and not task_parent
                and (entity.has_geometry or (entity.has_rigidbody and owns_geometry(entity)))):
            found.append(entity)
            task_parent = True
        for child in children.get(entity.entity_id, ()):
            walk(child, subtree_excluded, task_parent)

    for root in roots:
        walk(root)
    return found


def measure_task_objects(records: Iterable[Any]) -> dict[Any, tuple[
        tuple[float, float, float], tuple[float, float, float]]]:
    """Measure each task object's whole subtree, keyed by the root's entity id.

    Returns `(contact_point, extents)` per object, in the convention a composed plan uses:
    centred in x and y, resting on its underside in z. Two things make this more than reading
    one entity. A multi-part object keeps geometry in children -- a tray is a floor slab plus
    four walls, so its root alone measures 8 mm tall instead of 10 cm -- and a snapshot reports
    an entity's geometric centre, while placements record where the object rests.
    """
    entities = coerce_entity_records(records)
    by_id = {entity.entity_id: entity for entity in entities}
    children: dict[Any, list[SceneEntityRecord]] = {}
    for entity in entities:
        if entity.parent is not None and entity.parent in by_id:
            children.setdefault(entity.parent, []).append(entity)

    def subtree(root: SceneEntityRecord) -> list[SceneEntityRecord]:
        collected = [root]
        pending = [root]
        while pending:
            for child in children.get(pending.pop().entity_id, ()):
                collected.append(child)
                pending.append(child)
        return collected

    measured: dict[Any, tuple[tuple[float, float, float],
                              tuple[float, float, float]]] = {}
    for root in discover_task_objects(records):
        box = _aabb(subtree(root))
        if box is not None:
            measured[root.entity_id] = box
    return measured


def discover_scene_registry(
    records: Iterable[Any],
    *,
    scene_id: str | None = None,
    robot: str | None = None,
) -> DiscoveredSceneRegistry:
    """Build a Route B registry from a snapshot; raises when nothing is found."""
    objects = [
        DiscoveredSceneObject(
            object_id=f"scene_object_{index:03d}",
            body_name=entity.name,
            role="target",
            semantic_name=entity.name,
            model_name=entity.model_name,
            position=entity.position or (0.0, 0.0, 0.0),
            dimensions_m=entity.dimensions_m,
        )
        for index, entity in enumerate(discover_task_objects(records), 1)
    ]
    if not objects:
        raise ValueError("no discoverable task objects; author an interaction registry for this scene")
    return DiscoveredSceneRegistry(scene_id=scene_id, robot=robot, objects=objects)


def _world_translation(transform: Any) -> tuple[float, float, float] | None:
    """World position out of a snapshot transform, in either spelling."""
    if not isinstance(transform, Mapping):
        return None
    return _triple(transform.get("translation") or transform.get("position"))


def _transform_extents(transform: Any) -> tuple[float, float, float] | None:
    """Full extents of a primitive from its transform scale.

    A platform primitive is unit-sized geometry scaled to its real dimensions, so a snapshot
    reports `visual.size` as [1, 1, 1] and carries the actual extents in the transform scale.
    Reading the component size instead yields 1 m for every object.
    """
    if not isinstance(transform, Mapping):
        return None
    return _triple(transform.get("scale"))


def _aabb(records: Sequence[SceneEntityRecord]) -> tuple[
        tuple[float, float, float], tuple[float, float, float]] | None:
    """Contact point and extents of a group of entities, as an axis-aligned box.

    A multi-part object -- a tray is a floor slab plus four walls -- keeps its geometry in
    children, so measuring the root alone understates it: that tray would read 8 mm tall
    instead of 10 cm, and its interior anchor would sit inside the floor. Unioning the subtree
    gives the whole object. Extents are axis-aligned, so a rotated object is bounded rather
    than described exactly; that is enough for an anchor offset along the object's own axes.
    """
    boxes = [(record.position, record.dimensions_m) for record in records
             if record.position is not None and record.dimensions_m is not None]
    if not boxes:
        return None
    lows: list[tuple[float, float, float]] = []
    highs: list[tuple[float, float, float]] = []
    for centre, size in boxes:
        half = tuple(abs(float(value)) / 2.0 for value in size)
        lows.append(tuple(float(centre[axis]) - half[axis] for axis in range(3)))
        highs.append(tuple(float(centre[axis]) + half[axis] for axis in range(3)))
    low = tuple(min(item[axis] for item in lows) for axis in range(3))
    high = tuple(max(item[axis] for item in highs) for axis in range(3))
    # The contact point convention: centred in x and y, resting at the bottom in z.
    contact = ((low[0] + high[0]) / 2.0, (low[1] + high[1]) / 2.0, low[2])
    extents = tuple(max(high[axis] - low[axis], 0.0) for axis in range(3))
    return contact, extents  # type: ignore[return-value]


def _triple(value: Any) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = [value.get("x"), value.get("y"), value.get("z")]
    if isinstance(value, Sequence) and len(value) == 3 and all(item is not None for item in value):
        return (float(value[0]), float(value[1]), float(value[2]))
    return None
