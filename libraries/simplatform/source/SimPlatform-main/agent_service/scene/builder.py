"""Build plain-data scene plans for the platform scene API.

This module replaces the source stack's MJCF emission.  The stack wrote a
``scene.xml`` per task and recentered every top-level body by ``(-0.525, 0,
-0.6)`` so that ``SceneObject.position`` denoted a contact point on the tabletop.
We keep the same public tabletop-local frame but go the other way: a saved
platform scene supplies the robot, and composed objects are lifted into the
platform world frame by ``LOCAL_TO_WORLD_OFFSET``, which is the exact inverse of
that recentering.  If one changes, so must the other.

Nothing here touches the simulator.  A ``ScenePlan`` is inert data: the runtime
layer decides how to restore the base scene and create the entities.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from .assets import model_for_name
from .discovery import SceneEntityRecord, discover_task_objects, measure_task_objects
from .registry import SceneObject, SceneRegistry

# External reference assets, not repository content.  Both are saved platform
# scenes owned by the demo2 reference project; override them per deployment
# rather than depending on these paths.
ROUTE_A_BASE_SCENE_ENV = "AGENT_SERVICE_ROUTE_A_BASE_SCENE"
ROUTE_B_DEFAULT_SCENE_ENV = "AGENT_SERVICE_ROUTE_B_SCENE"

# Robot + two lights only.  There is no table in this file, so a Route A plan
# has to create the tabletop itself before placing any task object.
ROUTE_A_BASE_SCENE = Path(r"F:\jinliang-agent\demo2\tmp_state\ur5e_robotiq_2f85_pose.simstate")
ROUTE_B_DEFAULT_SCENE = Path(r"F:\jinliang-agent\demo2\tmp_state\world_with_wall.simstate")

# Tabletop geometry as used by the reference demo2 scene.  The robot base is at
# the world origin; the tabletop upper surface is the z=0 plane of the public
# tabletop-local frame.
TABLETOP_CENTER_XY = (0.525, 0.0)
TABLETOP_HALF_EXTENTS = (0.375, 0.75, 0.015)
TABLETOP_SURFACE_Z = 0.60
TABLETOP_RGBA = (0.48, 0.28, 0.12, 1.0)
TABLETOP_NAME = "work_table"

# world = tabletop_local + LOCAL_TO_WORLD_OFFSET.  Inverse of the stack's
# MJCF recentering by (-0.525, 0, -0.6).
LOCAL_TO_WORLD_OFFSET = (TABLETOP_CENTER_XY[0], TABLETOP_CENTER_XY[1], TABLETOP_SURFACE_Z)

# Containers and buttons stay static scene geometry; ordinary generated objects
# are movable payloads because the gripper's attachment model requires it.
STATIC_MODELS = frozenset({"open_box", "button_basic"})

# A generated button travels along its own axis so a press can depress and
# restore it without inventing an actuator or a sensor.
BUTTON_TRAVEL_RANGE = (-0.02, 0.0)
BUTTON_TRAVEL_DAMPING = 1.0

OPEN_BOX_WALL_THICKNESS = 0.008


@dataclass(frozen=True)
class GeometrySpec:
    """One primitive or mesh piece of an object, in object-local coordinates."""

    name: str
    shape: Literal["box", "cylinder", "mesh", "sphere"]
    local_position: tuple[float, float, float]
    half_extents: tuple[float, float, float] | None = None
    radius: float | None = None
    half_length: float | None = None
    mesh_path: str | None = None
    collision_mesh_paths: tuple[str, ...] = ()
    texture_path: str | None = None
    rgba: tuple[float, float, float, float] | None = None
    collidable: bool = True
    visible: bool = True


@dataclass(frozen=True)
class TabletopSpec:
    name: str = TABLETOP_NAME
    center: tuple[float, float, float] = (
        TABLETOP_CENTER_XY[0],
        TABLETOP_CENTER_XY[1],
        TABLETOP_SURFACE_Z - TABLETOP_HALF_EXTENTS[2],
    )
    half_extents: tuple[float, float, float] = TABLETOP_HALF_EXTENTS
    surface_z: float = TABLETOP_SURFACE_Z
    rgba: tuple[float, float, float, float] = TABLETOP_RGBA


@dataclass(frozen=True)
class ScenePlacement:
    """A task object to create, with both frames recorded for auditability."""

    object_id: str
    body_name: str
    semantic_name: str | None
    local_position: tuple[float, float, float]
    world_position: tuple[float, float, float]
    dimensions_m: tuple[float, float, float]
    model_id: str | None = None
    model_name: str | None = None
    entity_id: str | None = None
    candidate_for: str | None = None
    movable: bool = True
    capabilities: frozenset[str] | None = None
    runtime_origin_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    travel_axis: tuple[float, float, float] | None = None
    travel_range: tuple[float, float] | None = None
    travel_damping: float | None = None
    geometry: tuple[GeometrySpec, ...] = ()
    orientation_wxyz: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenePlan:
    """Everything needed to realize a task scene, as inert data."""

    route: Literal["A", "B"]
    scene_id: str
    robot: str
    base_scene: str
    tabletop: TabletopSpec | None = None
    create_tabletop: bool = False
    placements: tuple[ScenePlacement, ...] = ()
    bindings: Mapping[str, str] = field(default_factory=dict)
    discovered_objects: tuple[str, ...] = ()

    def placement(self, object_id: str) -> ScenePlacement:
        return next(item for item in self.placements if item.object_id == object_id)

    def canonical_payload(self, *, base_scene_digest: str) -> dict[str, Any]:
        """Stable identity of this plan, for ``scene_fingerprint``.

        Only what actually changes the realized world is included: the base
        scene's content digest, the ordered placements with their world
        positions and footprints, and the robot.  Output paths, task ids and
        binding labels are deliberately excluded so recompiling the same plan in
        a different directory keeps the same fingerprint.
        """
        return {
            "base_scene_digest": base_scene_digest,
            "robot": self.robot,
            "route": self.route,
            "create_tabletop": self.create_tabletop,
            "tabletop": None if self.tabletop is None else {
                "center": list(self.tabletop.center),
                "half_extents": list(self.tabletop.half_extents),
                "surface_z": self.tabletop.surface_z,
            },
            "placements": [
                {
                    "object_id": item.object_id,
                    "body_name": item.body_name,
                    "model_id": item.model_id,
                    "model_name": item.model_name,
                    "world_position": [round(value, 6) for value in item.world_position],
                    "dimensions_m": [round(value, 6) for value in item.dimensions_m],
                    "orientation_wxyz": list(item.orientation_wxyz),
                    "movable": item.movable,
                    "capabilities": sorted(item.capabilities or ()),
                    "runtime_origin_offset": list(item.runtime_origin_offset),
                }
                for item in self.placements
            ],
            "discovered_objects": list(self.discovered_objects),
        }


def route_a_base_scene(override: str | Path | None = None) -> Path:
    return _resolve_scene(override, ROUTE_A_BASE_SCENE_ENV, ROUTE_A_BASE_SCENE)


def route_b_default_scene(override: str | Path | None = None) -> Path:
    return _resolve_scene(override, ROUTE_B_DEFAULT_SCENE_ENV, ROUTE_B_DEFAULT_SCENE)


def local_to_world(position: Sequence[float]) -> tuple[float, float, float]:
    return (
        round(position[0] + LOCAL_TO_WORLD_OFFSET[0], 6),
        round(position[1] + LOCAL_TO_WORLD_OFFSET[1], 6),
        round(position[2] + LOCAL_TO_WORLD_OFFSET[2], 6),
    )


def world_to_local(position: Sequence[float]) -> tuple[float, float, float]:
    return (
        round(position[0] - LOCAL_TO_WORLD_OFFSET[0], 6),
        round(position[1] - LOCAL_TO_WORLD_OFFSET[1], 6),
        round(position[2] - LOCAL_TO_WORLD_OFFSET[2], 6),
    )


def build_route_a_plan(
    registry: SceneRegistry,
    assets: Mapping[str, Any] | None = None,
    *,
    base_scene: str | Path | None = None,
    tabletop: TabletopSpec | None = None,
) -> ScenePlan:
    """Compose a Route A plan: restore the base scene, add a tabletop, place objects.

    ``assets`` is keyed by ``model_id`` and only consulted for mesh objects; the
    three generated primitives need nothing beyond their footprint.
    """
    by_model = dict(assets or {})
    placements = tuple(
        _placement(item, by_model.get(item.model_id))
        for item in registry.objects
    )
    return ScenePlan(
        route="A",
        scene_id=registry.scene_id,
        robot=registry.robot,
        base_scene=str(route_a_base_scene(base_scene)),
        tabletop=tabletop or TabletopSpec(),
        create_tabletop=True,
        placements=placements,
        bindings=dict(registry.bindings),
    )


def build_route_b_plan(
    entities: Iterable[Any],
    *,
    scene_file: str | Path | None = None,
    robot: str = "ur5e",
    scene_id: str | None = None,
) -> ScenePlan:
    """Load a Route B plan: restore a saved scene and discover its task objects.

    Nothing is composed and no tabletop is created; the saved scene already owns
    its furniture.  ``entities`` is a snapshot of that scene.
    """
    scene = route_b_default_scene(scene_file)
    entities = list(entities)
    discovered = discover_task_objects(entities)
    measured = measure_task_objects(entities)
    # Imported roots retain their instance name but not catalog metadata in the
    # snapshot. Recover identity/size from exact catalog names, never generic
    # mesh child names such as "textured" or the mesh's unit transform scale.
    import re
    from .model_catalog import load_route_a_model_catalog
    try:
        catalog_models = load_route_a_model_catalog().models
    except ValueError:
        catalog_models = ()
    records = {str(item.get("id", item.get("entity_id"))): item
               for item in entities if isinstance(item, dict)}
    placements: list[ScenePlacement] = []
    bindings: dict[str, str] = {}
    for entity in discovered:
        box = measured.get(entity.entity_id)
        if box is None:
            # Geometry is what an anchor is derived from. An object the scene does not
            # measure can still be named, but it cannot be given interaction metadata.
            continue
        contact, extents = box
        model = entity.model_name or model_for_name(entity.name)
        instance_name = re.sub(r"_\d+$", "", entity.name).casefold()
        known = next((item for item in catalog_models
                      if instance_name in {item.model_name.casefold(),
                                           *(alias.casefold().replace(" ", "_") for alias in item.aliases)}), None)
        if known is None:
            # Generated instance names may use the user's generic noun (box_01).
            # The imported subtree still carries the exact asset node name.
            descendants = {str(entity.entity_id)}
            pending = [str(entity.entity_id)]
            while pending:
                parent = pending.pop()
                for key, child in records.items():
                    if key not in descendants and str(child.get("parent", child.get("parent_id"))) == parent:
                        descendants.add(key)
                        pending.append(key)
            names = {str(records[key].get("name", "")).casefold()
                     for key in descendants if key in records}
            matches = [item for item in catalog_models if item.model_name.casefold() in names]
            if len(matches) == 1:
                known = matches[0]
        record = records.get(str(entity.entity_id), {})
        transform = record.get("world") or {}
        orientation = tuple(transform.get("rotationWxyz", transform.get("rotation_wxyz", (1., 0., 0., 0.))))
        if known is not None:
            model = known.model_name
            scale = transform.get("scale", (1., 1., 1.))
            extents = tuple(abs(float(scale[i])) * known.dimensions_m[i] for i in range(3))
            contact = entity.position or contact
        placements.append(ScenePlacement(
            object_id=entity.name,
            body_name=entity.name,
            semantic_name=entity.name,
            # A discovered scene has no separate tabletop-local frame; its own frame is the
            # world frame, so both record the same measured contact point. That point is where
            # the object rests, matching what a composed plan records, so the interaction
            # templates place anchors identically on either route.
            local_position=contact,
            world_position=contact,
            dimensions_m=extents,
            model_name=model,
            model_id=known.model_id if known is not None else None,
            aliases=known.aliases if known is not None else (),
            capabilities=frozenset(known.capabilities) if known is not None else None,
            orientation_wxyz=orientation,
            # Marks the object as already present, so applying this plan does not
            # re-create what the live scene already holds.
            entity_id=str(entity.entity_id) if entity.entity_id is not None else None,
            movable=known.movable if known is not None else model not in STATIC_MODELS,
        ))
        bindings[entity.name] = entity.name
    return ScenePlan(
        route="B",
        scene_id=scene_id or scene.stem,
        robot=robot,
        base_scene=str(scene),
        tabletop=None,
        create_tabletop=False,
        placements=tuple(placements),
        bindings=bindings,
        discovered_objects=tuple(entity.name for entity in discovered),
    )


def _resolve_scene(override: str | Path | None, variable: str, default: Path) -> Path:
    if override is not None:
        return Path(override).expanduser()
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else default


def _placement(item: SceneObject, asset: Any | None) -> ScenePlacement:
    dimensions = item.dimensions_m or (0.06, 0.06, 0.06)
    movable = (bool(getattr(asset, "movable")) if asset is not None
               and getattr(asset, "movable", None) is not None
               else item.model_name not in STATIC_MODELS)
    raw_capabilities = getattr(asset, "capabilities", None)
    capabilities = (None if raw_capabilities is None else
                    frozenset(str(value) for value in raw_capabilities))
    collision_shapes = tuple(getattr(asset, "collision_shapes", ()) or ())
    runtime_origin_offset = (
        tuple(collision_shapes[0].local_position_m)
        if collision_shapes else (0.0, 0.0, 0.0)
    )
    travel = item.model_name == "button_basic"
    return ScenePlacement(
        object_id=item.object_id,
        body_name=item.body_name,
        semantic_name=item.semantic_name,
        local_position=tuple(item.position),
        world_position=local_to_world(item.position),
        dimensions_m=tuple(dimensions),
        model_id=item.model_id,
        model_name=item.model_name,
        entity_id=item.entity_id,
        candidate_for=item.candidate_for,
        movable=movable,
        capabilities=capabilities,
        runtime_origin_offset=runtime_origin_offset,
        travel_axis=(0.0, 0.0, 1.0) if travel else None,
        travel_range=BUTTON_TRAVEL_RANGE if travel else None,
        travel_damping=BUTTON_TRAVEL_DAMPING if travel else None,
        geometry=_geometry(item, dimensions, asset),
    )


def _geometry(item: SceneObject, dimensions: tuple[float, float, float], asset: Any | None) -> tuple[GeometrySpec, ...]:
    """Object-local geometry, with the contact point at local z=0."""
    if asset is not None and getattr(asset, "source", None) == "mesh":
        minimum = getattr(asset, "bbox_min_m", None) or (0.0, 0.0, 0.0)
        maximum = getattr(asset, "bbox_max_m", None) or dimensions
        # The placement is the mesh footprint centre on the tabletop: centre the
        # XY bounding box on the body and drop the lowest vertex onto z=0.
        offset = (
            -(minimum[0] + maximum[0]) / 2,
            -(minimum[1] + maximum[1]) / 2,
            -minimum[2],
        )
        collisions = tuple(str(path) for path in getattr(asset, "collision_stls", ()) or ())
        texture = getattr(asset, "texture", None)
        visual = GeometrySpec(
            name=f"{item.object_id}_mesh",
            shape="mesh",
            local_position=offset,
            mesh_path=str(getattr(asset, "visual_obj", "")) or None,
            collision_mesh_paths=collisions,
            texture_path=str(texture) if texture else None,
            collidable=False,
        )
        collision_geometry = tuple(
            GeometrySpec(
                name=f"{item.object_id}_{shape.name}",
                shape=shape.shape,
                local_position=tuple(shape.local_position_m),
                half_extents=(tuple(shape.half_extents_m)
                              if shape.half_extents_m is not None else None),
                radius=shape.radius_m,
                half_length=shape.half_length_m,
                visible=False,
            )
            for shape in (getattr(asset, "collision_shapes", ()) or ())
        )
        return (visual, *collision_geometry)

    rgba = _rgba(item.semantic_name or "")
    if item.model_name == "open_box":
        x, y, height = dimensions
        thickness = OPEN_BOX_WALL_THICKNESS
        pieces = (
            ("floor", (0.0, 0.0, thickness / 2), (x / 2, y / 2, thickness / 2)),
            ("left", (-x / 2 + thickness / 2, 0.0, height / 2), (thickness / 2, y / 2, height / 2)),
            ("right", (x / 2 - thickness / 2, 0.0, height / 2), (thickness / 2, y / 2, height / 2)),
            ("front", (0.0, y / 2 - thickness / 2, height / 2), (x / 2, thickness / 2, height / 2)),
            ("back", (0.0, -y / 2 + thickness / 2, height / 2), (x / 2, thickness / 2, height / 2)),
        )
        return tuple(
            GeometrySpec(
                name=f"{item.object_id}_{name}", shape="box",
                local_position=position, half_extents=half, rgba=rgba,
            )
            for name, position, half in pieces
        )
    if item.model_name == "button_basic":
        return (GeometrySpec(
            name=f"{item.object_id}_button", shape="cylinder",
            local_position=(0.0, 0.0, 0.032), radius=0.05, half_length=0.012,
            rgba=(0.8, 0.1, 0.1, 1.0),
        ),)
    return (GeometrySpec(
        name=f"{item.object_id}_geom", shape="box",
        local_position=(0.0, 0.0, dimensions[2] / 2),
        half_extents=tuple(value / 2 for value in dimensions), rgba=rgba,
    ),)


def _rgba(name: str) -> tuple[float, float, float, float]:
    low = name.casefold()
    if "red" in low or "红" in low:
        return (0.8, 0.08, 0.08, 1.0)
    if "blue" in low or "蓝" in low:
        return (0.08, 0.25, 0.8, 1.0)
    if "yellow" in low or "黄" in low:
        return (0.9, 0.72, 0.08, 1.0)
    return (0.65, 0.65, 0.65, 1.0)
