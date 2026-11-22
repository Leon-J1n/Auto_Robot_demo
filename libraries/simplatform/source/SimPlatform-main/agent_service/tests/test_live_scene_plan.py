"""Tests for building an executable plan from a scene that already exists.

A turn on a populated world takes the in-place path, which produces a Route B plan. Three
things made that path unexecutable, and each failed in a way that pointed elsewhere:

* Discovery read `position`/`dimensions_m`, but a live snapshot reports `world.translation`
  and carries extents in the transform *scale* -- a platform primitive is unit geometry
  scaled to size, so every `visual.size` reads [1, 1, 1]. Both came back None.
* Route B plans carried no placements, and both the registry builder and the compiler
  refused to generate metadata for a non-A route, so compilation failed with
  `EXECUTION_METADATA_MISSING`. Since a Route A turn leaves the world populated, *every*
  subsequent turn in a conversation hit this.
* A multi-part object keeps geometry in children. Measuring a tray's root alone gives its
  8 mm floor slab, so its interior anchor would sit inside the floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.scene import (build_generated_registry,  # noqa: E402
                                 build_route_b_plan)
from agent_service.scene.assets import BUTTON, CONTAINER, CUBE  # noqa: E402
from agent_service.scene.discovery import (coerce_entity_records,  # noqa: E402
                                           measure_task_objects)

ROBOT = {"id": 1, "name": "UR5e", "robot": {}, "parent": None,
         "world": {"translation": [0.0, 0.0, 0.5], "scale": [1.0, 1.0, 1.0]}}
# A 6 cm cube: unit geometry, extents in the scale, centre reported at 0.63.
BLOCK = {"id": 5, "name": "red_block_01", "parent": None,
         "world": {"translation": [0.5, 0.2, 0.63], "scale": [0.06, 0.06, 0.06]},
         "visual": {"shape": "box", "size": [1.0, 1.0, 1.0]}}
# A tray: an 8 mm floor slab with wall children reaching 0.10 total.
TRAY = {"id": 6, "name": "green_tray_01", "parent": None,
        "world": {"translation": [0.4, -0.3, 0.604], "scale": [0.18, 0.18, 0.008]},
        "visual": {"shape": "box", "size": [1.0, 1.0, 1.0]}}
TRAY_WALLS = [
    {"id": 7 + index, "name": f"green_tray_01_{index + 1}", "parent": 6,
     "world": {"translation": translation, "scale": scale},
     "visual": {"shape": "box", "size": [1.0, 1.0, 1.0]}}
    for index, (translation, scale) in enumerate([
        ([0.4, -0.212, 0.65], [0.18, 0.008, 0.1]),
        ([0.4, -0.388, 0.65], [0.18, 0.008, 0.1]),
        ([0.312, -0.3, 0.65], [0.008, 0.18, 0.1]),
        ([0.488, -0.3, 0.65], [0.008, 0.18, 0.1]),
    ])
]
LIVE_SCENE = [ROBOT, BLOCK, TRAY, *TRAY_WALLS]


# -- reading a live snapshot ------------------------------------------------------------

def test_extents_come_from_the_transform_scale() -> None:
    record = next(r for r in coerce_entity_records([BLOCK]) if r.name == "red_block_01")
    # Reading visual.size would give 1 m for every object.
    assert record.dimensions_m == pytest.approx((0.06, 0.06, 0.06))
    assert record.position == pytest.approx((0.5, 0.2, 0.63))


def test_a_flat_authored_record_still_works() -> None:
    record = next(iter(coerce_entity_records([
        {"entity_id": 9, "name": "cube", "position": [1.0, 2.0, 3.0],
         "dimensions_m": [0.1, 0.1, 0.1]}])))
    assert record.position == pytest.approx((1.0, 2.0, 3.0))
    assert record.dimensions_m == pytest.approx((0.1, 0.1, 0.1))


def test_the_robot_is_not_a_task_object() -> None:
    assert 1 not in measure_task_objects(LIVE_SCENE)


# -- measuring whole objects -----------------------------------------------------------

def test_a_simple_object_is_measured_at_its_resting_point() -> None:
    contact, extents = measure_task_objects(LIVE_SCENE)[5]
    # A placement records where the object rests, not its centre: 0.63 - 0.03 = 0.60.
    assert contact == pytest.approx((0.5, 0.2, 0.60))
    assert extents == pytest.approx((0.06, 0.06, 0.06))


def test_a_multipart_object_includes_its_children() -> None:
    contact, extents = measure_task_objects(LIVE_SCENE)[6]
    assert contact[2] == pytest.approx(0.60, abs=1e-6)
    # The whole tray is 10 cm tall; its floor slab alone is 8 mm.
    assert extents[2] == pytest.approx(0.10, abs=1e-6)
    # The footprint covers the floor plus the wall thickness standing on its edge, so it is
    # the floor's 0.18 widened by one wall (0.008), not the floor alone.
    assert extents[0] == pytest.approx(0.184, abs=1e-6)
    assert extents[1] == pytest.approx(0.184, abs=1e-6)


def test_an_unmeasurable_object_is_skipped_not_guessed() -> None:
    nameless = {"id": 20, "name": "mystery", "parent": None,
                "visual": {"shape": "mesh", "resource": "thing.obj"}}
    assert 20 not in measure_task_objects([ROBOT, nameless])


# -- the plan --------------------------------------------------------------------------

def test_an_in_place_plan_carries_measured_placements() -> None:
    plan = build_route_b_plan(LIVE_SCENE, scene_id="main")
    assert plan.route == "B"
    placements = {item.object_id: item for item in plan.placements}
    assert set(placements) == {"red_block_01", "green_tray_01"}
    # Without placements the compiler cannot generate metadata and the turn fails.
    assert placements["red_block_01"].world_position == pytest.approx((0.5, 0.2, 0.60))
    assert placements["green_tray_01"].dimensions_m[2] == pytest.approx(0.10, abs=1e-6)


def test_existing_objects_are_marked_so_they_are_not_recreated() -> None:
    plan = build_route_b_plan(LIVE_SCENE, scene_id="main")
    # Applying this plan must not duplicate what the world already holds.
    assert all(item.entity_id is not None for item in plan.placements)


def test_models_are_inferred_from_the_object_names() -> None:
    plan = build_route_b_plan(LIVE_SCENE, scene_id="main")
    models = {item.object_id: item.model_name for item in plan.placements}
    assert models["red_block_01"] == CUBE
    assert models["green_tray_01"] == CONTAINER
    button = {"id": 30, "name": "power_button", "parent": None,
              "world": {"translation": [0.4, 0.0, 0.62], "scale": [0.08, 0.08, 0.04]},
              "visual": {"shape": "box", "size": [1.0, 1.0, 1.0]}}
    plan = build_route_b_plan([ROBOT, button], scene_id="main")
    assert plan.placements[0].model_name == BUTTON


def test_a_container_is_static_and_a_cube_is_movable() -> None:
    plan = build_route_b_plan(LIVE_SCENE, scene_id="main")
    movable = {item.object_id: item.movable for item in plan.placements}
    assert movable["red_block_01"] is True
    assert movable["green_tray_01"] is False


# -- metadata generation ---------------------------------------------------------------

def test_an_in_place_plan_yields_usable_interaction_metadata() -> None:
    plan = build_route_b_plan(LIVE_SCENE, scene_id="main")
    document = build_generated_registry(plan, scene="C:/live.simstate",
                                        scene_fingerprint="b" * 64)
    assert set(document["objects"]) == {"red_block_01", "green_tray_01"}
    tray = document["objects"]["green_tray_01"]["spatial"]
    assert tray["default_anchor"] == "interior"
    block = document["objects"]["red_block_01"]["spatial"]
    assert block["default_anchor"] == "grasp"


def test_a_plan_with_no_measured_geometry_is_refused() -> None:
    plan = build_route_b_plan([ROBOT], scene_id="main")
    assert plan.placements == ()
    with pytest.raises(ValueError, match="authored interaction registry"):
        build_generated_registry(plan, scene="C:/live.simstate", scene_fingerprint="b" * 64)


def test_both_routes_describe_the_same_scene_the_same_way() -> None:
    """A composed plan and a plan read back from that same scene must agree.

    They record the same convention -- resting contact point plus full extents -- so anchors
    land in the same place whichever path produced them. When Route B recorded raw snapshot
    centres instead, its anchors were off by half a metre.
    """
    from agent_service.anchors import AnchorResolver
    from agent_service.planning.contracts import (Operation, TaskEntity, TaskIntent,
                                                  TaskStatus, TaskType)
    from agent_service.scene import build_route_a_plan, resolve_intent
    from agent_service.scene.composer import SceneComposer

    intent = TaskIntent(
        status=TaskStatus.ACCEPTED, instruction="x", task_types=[TaskType.PICK_AND_PLACE],
        entities=[TaskEntity(entity_id="red_block", semantic_name="red block",
                             category="object"),
                  TaskEntity(entity_id="green_tray", semantic_name="green tray",
                             category="container")],
        operations=[Operation(operation_id="op-1", task_type=TaskType.PICK_AND_PLACE,
                              source="red_block", destination="green_tray")])
    registry = SceneComposer().compose(intent, resolve_intent(intent), robot="ur5e", seed=0)
    plan_a = build_route_a_plan(registry)

    # Rebuild the same objects as a live scene would report them: centres, unit geometry.
    entities = [ROBOT]
    for index, placement in enumerate(plan_a.placements):
        x, y, z = placement.world_position
        width, depth, height = placement.dimensions_m
        entities.append({
            "id": 50 + index, "name": placement.object_id, "parent": None,
            "world": {"translation": [x, y, z + height / 2.0],
                      "scale": [width, depth, height]},
            "visual": {"shape": "box", "size": [1.0, 1.0, 1.0]}})
    plan_b = build_route_b_plan(entities, scene_id="main")

    by_id_a = {item.object_id: item for item in plan_a.placements}
    by_id_b = {item.object_id: item for item in plan_b.placements}
    assert set(by_id_a) == set(by_id_b)
    for object_id, placement in by_id_a.items():
        assert by_id_b[object_id].world_position == pytest.approx(placement.world_position)
        assert by_id_b[object_id].dimensions_m == pytest.approx(placement.dimensions_m)

    resolver_a = AnchorResolver(build_generated_registry(
        plan_a, scene="C:/x.simstate", scene_fingerprint="a" * 64), runtime=None)
    resolver_b = AnchorResolver(build_generated_registry(
        plan_b, scene="C:/x.simstate", scene_fingerprint="b" * 64), runtime=None)
    for object_id, anchor in (("red_block_01", "grasp"), ("green_tray_01", "interior")):
        assert resolver_b(object_id, anchor)["position"] == pytest.approx(
            resolver_a(object_id, anchor)["position"], abs=1e-9)
