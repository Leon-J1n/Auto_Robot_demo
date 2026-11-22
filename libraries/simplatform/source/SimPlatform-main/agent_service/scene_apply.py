"""Apply a `ScenePlan` to the authoritative Live world.

`agent_service.scene` deliberately produces a plan as pure data; this module is the only place
that turns one into scene writes. It talks to the world exclusively through published Live Python
capabilities.

Restoring a base scene replaces the bound world's contents, so it only happens when the plan
actually asks for it. A turn that merely operates on what the user already has must leave the
scene alone -- that is why `apply_plan` takes an explicit `restore_base` decision rather than
inferring one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("agent_service.scene_apply")

# Manipulable objects use the same unit coefficient on every contact friction axis.
MOVABLE_OBJECT_FRICTION = (1.0, 1.0, 1.0)
MOVABLE_OBJECT_MASS = 0.2

# A world saved from a headless process carries no light, so it renders black in the browser.
# These match the two lights the reference base scene ships with.
DEFAULT_LIGHTS: tuple[dict[str, Any], ...] = (
    {"type": "directional", "name": "Sun", "intensity": 2.5,
     "color": (1.0, 0.98, 0.94)},
    {"type": "point", "name": "Fill", "intensity": 12.0, "range": 12.0,
     "color": (0.9, 0.94, 1.0), "position": (0.6, 0.6, 2.0)},
)


class SceneApplyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AppliedScene:
    """What the world now contains, keyed the way the rest of the pipeline refers to objects."""

    restored: str | None = None
    tabletop: str | None = None
    # object_id -> live object id, so grounding results stay resolvable after the writes.
    objects: dict[str, str] = field(default_factory=dict)
    created_lights: list[str] = field(default_factory=list)
    scene_generation: int | None = None
    # Snapshot entity IDs are runtime handles, not Live stable object IDs.
    entity_handles: dict[str, int] = field(default_factory=dict)


def read_back_generated_plan(plan: Any, applied: AppliedScene, entities: list[dict]) -> Any:
    """Bind composed identities to final Live transforms before generating metadata."""
    import numpy as np
    from .scene.builder import world_to_local

    def rotate(vector, quaternion):
        w, x, y, z = quaternion
        q = np.asarray((x, y, z), dtype=float)
        v = np.asarray(vector, dtype=float)
        return v + 2.0 * (w * np.cross(q, v) + np.cross(q, np.cross(q, v)))

    records = {str(item.get("id", item.get("entity_id"))): item for item in entities}
    placements = []
    for placement in plan.placements:
        live_id = applied.entity_handles.get(placement.object_id,
                                            applied.objects.get(placement.object_id))
        record = records.get(str(live_id)) if live_id is not None else None
        if record is None:
            raise SceneApplyError("SCENE_INVALID", f"Generated object missing from scene: {placement.object_id}")
        world = record.get("world") or {}
        position = np.asarray(world.get("translation"), dtype=float)
        quaternion = np.asarray(world.get("rotationWxyz", world.get("rotation_wxyz")), dtype=float)
        if (position.shape != (3,) or quaternion.shape != (4,)
                or not np.all(np.isfinite(position)) or not np.all(np.isfinite(quaternion))
                or np.linalg.norm(quaternion) < 1e-12):
            raise SceneApplyError("SCENE_INVALID", f"Invalid generated world transform: {placement.object_id}")
        quaternion /= np.linalg.norm(quaternion)
        # The importer root is translated by the visual offset. The plan and
        # its anchors use the normalized tabletop contact frame, not raw OBJ origin.
        mesh = next((spec for spec in placement.geometry if spec.mesh_path), None)
        offset = mesh.local_position if mesh else (
            placement.geometry[0].local_position if placement.geometry else (0., 0., 0.))
        contact = tuple(float(value) for value in position - rotate(offset, quaternion))
        placements.append(replace(placement, world_position=contact,
                                  local_position=world_to_local(contact),
                                  orientation_wxyz=tuple(float(value) for value in quaternion)))
    return replace(plan, placements=tuple(placements))


def _quaternion(placement: Any) -> tuple[float, float, float, float]:
    value = getattr(placement, "orientation_wxyz", None) or (1.0, 0.0, 0.0, 0.0)
    packed = tuple(float(item) for item in value)
    return packed if len(packed) == 4 else (1.0, 0.0, 0.0, 0.0)


def _primitive_size(spec: Any, placement: Any) -> tuple[str, tuple[float, float, float]]:
    """Map a geometry spec onto a Live primitive kind and its full-extent size."""
    shape = str(getattr(spec, "shape", "") or "box").lower()
    half = getattr(spec, "half_extents", None)
    radius = getattr(spec, "radius", None)
    half_length = getattr(spec, "half_length", None)
    if shape in {"sphere", "ball"} and radius:
        value = float(radius)
        return "sphere", (value, value, value)
    if shape in {"cylinder", "capsule"} and radius:
        value = float(radius)
        length = float(half_length or value)
        return shape, (value, value, length)
    if half:
        packed = tuple(float(item) * 2.0 for item in half)
        if len(packed) == 3:
            return "box", packed
    dimensions = getattr(placement, "dimensions_m", None)
    if dimensions:
        packed = tuple(float(item) for item in dimensions)
        if len(packed) == 3:
            return "box", packed
    return "box", (0.06, 0.06, 0.06)


def apply_plan(session: Any, plan: Any, *, restore_base: bool = True,
               ensure_lights: bool = True) -> AppliedScene:
    """Realise `plan` in the world behind `session` and report what was created."""
    result = AppliedScene()
    LOGGER.debug("apply_plan started restore_base=%s placement_count=%d",
                 restore_base, len(getattr(plan, "placements", ()) or ()))

    if restore_base:
        base = getattr(plan, "base_scene", None)
        if not base:
            raise SceneApplyError("SCENE_INVALID", "场景方案缺少基础场景文件。")
        path = Path(str(base))
        if not path.is_file():
            raise SceneApplyError(
                "SCENE_INVALID", f"基础场景文件不存在：{path}")
        try:
            session.restore_state_file(str(path))
        except Exception as error:  # noqa: BLE001 - surfaced as a structured failure
            raise SceneApplyError("SCENE_INVALID",
                                  f"无法载入基础场景 {path.name}：{error}") from error
        result.restored = str(path)

    tabletop = getattr(plan, "tabletop", None)
    if getattr(plan, "create_tabletop", False) and tabletop is not None:
        result.tabletop = _create_tabletop(session, tabletop)

    for placement in list(getattr(plan, "placements", ()) or ()):
        object_id = str(getattr(placement, "object_id", ""))
        if not object_id:
            continue
        if getattr(placement, "entity_id", None) is not None and not restore_base:
            # The placement describes an object the live scene already holds -- it exists to
            # carry measured geometry for interaction metadata, not to be created again.
            # Restoring a base scene wipes the world, so there the object does need creating.
            result.objects[object_id] = str(placement.entity_id)
            continue
        try:
            entity = _create_placement(session, placement)
        except SceneApplyError:
            raise
        except Exception as error:  # noqa: BLE001
            raise SceneApplyError(
                "SCENE_INVALID",
                f"无法在场景中创建 {object_id}：{error}") from error
        if entity is not None:
            result.objects[object_id] = str(entity.reference.object_id)
            handle = getattr(entity.reference, "runtime_handle", None)
            if handle is not None:
                result.entity_handles[object_id] = int(handle)

    if ensure_lights and not _has_light(session):
        result.created_lights = _create_lights(session)

    snapshot = _snapshot(session)
    if snapshot is not None:
        result.scene_generation = snapshot
    LOGGER.info("scene plan applied object_count=%d light_count=%d snapshot=%s",
                len(result.objects), len(result.created_lights), result.scene_generation)
    return result


def _create_tabletop(session: Any, spec: Any) -> str:
    half = tuple(float(item) for item in getattr(spec, "half_extents", (0.375, 0.75, 0.015)))
    center = tuple(float(item) for item in getattr(spec, "center", (0.525, 0.0, 0.0)))
    surface = float(getattr(spec, "surface_z", 0.60))
    name = str(getattr(spec, "name", "work_table") or "work_table")
    entity = session.create_primitive(
        "box", name=name, size=(half[0] * 2.0, half[1] * 2.0, half[2] * 2.0))
    # The plan gives the surface height; a box is positioned by its centre.
    entity.set_position((center[0], center[1], surface - half[2]))
    rgba = getattr(spec, "rgba", None)
    if rgba:
        _set_color(entity, rgba)
    # A table must never be dynamic, or the first contact sends the scene flying.
    entity.set_rigid_body(enabled=True, dynamic=False, gravity=False)
    return str(entity.reference.object_id)


def _create_placement(session: Any, placement: Any) -> Any | None:
    position = tuple(float(item) for item in
                     (getattr(placement, "world_position", None) or (0.0, 0.0, 0.0)))
    name = str(getattr(placement, "body_name", None)
               or getattr(placement, "object_id", "object"))
    geometry = list(getattr(placement, "geometry", ()) or ())
    movable = bool(getattr(placement, "movable", False))

    mesh = next((getattr(spec, "mesh_path", None) for spec in geometry
                 if getattr(spec, "mesh_path", None)), None)
    if mesh:
        mesh_spec = next(spec for spec in geometry if getattr(spec, "mesh_path", None))
        collider_specs = [
            spec for spec in geometry
            if getattr(spec, "shape", None) != "mesh" and getattr(spec, "collidable", True)
        ]
        mesh_local = tuple(float(item) for item in
                           (getattr(mesh_spec, "local_position", None)
                            or (0.0, 0.0, 0.0)))
        texture = getattr(mesh_spec, "texture_path", None)
        LOGGER.debug(
            "loading mesh body with imported collision topology: object=%s mesh=%s "
            "texture=%s declared_collision_meshes=%d ignored_primitive_proxies=%d",
            name,
            Path(str(mesh)).name,
            Path(str(texture)).name if texture else None,
            len(tuple(getattr(mesh_spec, "collision_mesh_paths", ()) or ())),
            len(collider_specs),
        )
        # The OBJ importer creates the visual and its mesh-collider subtree. Keep the imported
        # root as the physical root, matching the platform's direct-upload behaviour; adding a
        # bounding primitive around it makes every grasp goal collide with the target itself.
        root = session.load_asset(str(mesh))
        root.set_name(name)
        root.set_position((position[0] + mesh_local[0],
                           position[1] + mesh_local[1],
                           position[2] + mesh_local[2]))
        root.set_rotation(_quaternion(placement))
        rigid_body = {"enabled": True, "dynamic": movable, "gravity": movable}
        if movable:
            rigid_body.update({"mass": MOVABLE_OBJECT_MASS, "friction": MOVABLE_OBJECT_FRICTION})
        root.set_rigid_body(**rigid_body)
        return root

    if not geometry:
        kind, size = "box", tuple(
            float(item) for item in (getattr(placement, "dimensions_m", None)
                                     or (0.06, 0.06, 0.06)))
        entity = session.create_primitive(kind, name=name, size=size)
        entity.set_position(position)
        entity.set_rotation(_quaternion(placement))
        rigid_body = {"enabled": True, "dynamic": movable, "gravity": movable}
        if movable:
            rigid_body.update({"mass": MOVABLE_OBJECT_MASS, "friction": MOVABLE_OBJECT_FRICTION})
        entity.set_rigid_body(**rigid_body)
        return entity

    root: Any | None = None
    for index, spec in enumerate(geometry):
        kind, size = _primitive_size(spec, placement)
        part_name = name if index == 0 else f"{name}_{index}"
        entity = session.create_primitive(kind, name=part_name, size=size)
        local = tuple(float(item) for item in
                      (getattr(spec, "local_position", None) or (0.0, 0.0, 0.0)))
        entity.set_position((position[0] + local[0], position[1] + local[1],
                             position[2] + local[2]))
        entity.set_rotation(_quaternion(placement))
        rgba = getattr(spec, "rgba", None)
        if rgba:
            _set_color(entity, rgba)
        if getattr(spec, "collidable", True):
            # A dynamic body must be a top-level entity, so only the root carries one; the
            # remaining parts stay static children of it.
            dynamic = movable and index == 0
            rigid_body = {"enabled": True, "dynamic": dynamic, "gravity": dynamic}
            if dynamic:
                rigid_body.update({"mass": MOVABLE_OBJECT_MASS, "friction": MOVABLE_OBJECT_FRICTION})
            entity.set_rigid_body(**rigid_body)
        if root is None:
            root = entity
        else:
            try:
                entity.set_parent(root, preserve_world=True)
            except Exception:  # noqa: BLE001 - parenting is cosmetic here
                LOGGER.debug("could not parent %s under %s", part_name, name)
    return root


def _set_color(entity: Any, rgba: Any) -> None:
    packed = [float(item) for item in rgba]
    if len(packed) == 3:
        packed.append(1.0)
    try:
        entity.set_visual(color=packed[:4])
    except Exception:  # noqa: BLE001 - colour is cosmetic, never fail a turn for it
        LOGGER.debug("could not set colour on %r", entity)


def _has_light(session: Any) -> bool:
    try:
        snapshot = session.scene_snapshot()
    except Exception:  # noqa: BLE001
        return True  # Assume lit rather than add duplicates we cannot verify.
    for entity in getattr(snapshot, "entities", ()) or ():
        if isinstance(entity, dict) and ("light" in entity or "light" in
                                         (entity.get("flags") or ())):
            return True
    return False


def _create_lights(session: Any) -> list[str]:
    created: list[str] = []
    for spec in DEFAULT_LIGHTS:
        params = {key: value for key, value in spec.items() if key != "position"}
        try:
            entity = session.create_light(**params)
        except Exception:  # noqa: BLE001 - an unlit scene is bad but not fatal
            LOGGER.warning("could not create light %r", spec.get("name"))
            continue
        position = spec.get("position")
        if position:
            entity.set_position(tuple(float(item) for item in position))
        created.append(str(entity.reference.object_id))
    return created


def _snapshot(session: Any) -> int | None:
    try:
        return int(session.scene_snapshot().scene_generation)
    except Exception:  # noqa: BLE001
        return None
