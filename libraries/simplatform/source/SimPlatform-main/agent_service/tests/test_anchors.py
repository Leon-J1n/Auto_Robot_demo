"""Tests for anchor resolution and Route A asset resolution.

Both of these were missing links found by running the pipeline against a live simulator, and
both failed in a way that looked like something else:

* With no asset mapping, Route A composition raised `KeyError` on the first entity, surfacing
  as `SCENE_INVALID: 无法为该指令生成场景`.
* With no anchor resolver, every move targeted the object's *origin*. A tray's origin lies on
  its floor, millimetres above the tabletop, so reaching it drove the gripper into the table
  and the planner reported a goal collision -- nothing pointed at the missing resolver.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.anchors import AnchorResolver, load_registry  # noqa: E402
from agent_service.planning.contracts import (Operation, TaskEntity, TaskIntent,  # noqa: E402
                                              TaskStatus, TaskType)
from agent_service.scene import (build_generated_registry, build_route_a_plan,  # noqa: E402
                                 resolve_entities, resolve_intent)
from agent_service.scene.assets import BUTTON, CONTAINER, CUBE, model_for  # noqa: E402
from agent_service.scene.composer import SceneComposer  # noqa: E402


def _intent() -> TaskIntent:
    return TaskIntent(
        status=TaskStatus.ACCEPTED, instruction="把红色方块放到绿色托盘里",
        task_types=[TaskType.PICK_AND_PLACE],
        entities=[TaskEntity(entity_id="red_block", semantic_name="红色方块",
                             category="object"),
                  TaskEntity(entity_id="green_tray", semantic_name="绿色托盘",
                             category="container")],
        operations=[Operation(operation_id="op-1", task_type=TaskType.PICK_AND_PLACE,
                              source="red_block", destination="green_tray")])


def _registry() -> dict:
    intent = _intent()
    registry = SceneComposer().compose(intent, resolve_intent(intent), robot="ur5e", seed=0)
    return build_generated_registry(build_route_a_plan(registry), scene="C:/base.simstate",
                                    scene_fingerprint="a" * 64)


# -- asset resolution -----------------------------------------------------------------

def test_route_a_composition_works_with_resolved_assets() -> None:
    intent = _intent()
    assets = resolve_intent(intent)
    # Every entity needs a model: the composer looks each one up by id while placing.
    assert set(assets) == {"red_block", "green_tray"}
    registry = SceneComposer().compose(intent, assets, robot="ur5e", seed=0)
    assert len(registry.objects) == 2
    assert set(registry.bindings) == {"red_block", "green_tray"}


def test_composition_without_assets_is_the_failure_we_fixed() -> None:
    with pytest.raises(KeyError):
        SceneComposer().compose(_intent(), {}, robot="ur5e", seed=0)


def test_models_are_chosen_from_role_in_both_languages() -> None:
    def entity(name: str, category: str = "object") -> TaskEntity:
        return TaskEntity(entity_id="e", semantic_name=name, category=category)

    assert model_for(entity("红色方块")) == CUBE
    assert model_for(entity("red block")) == CUBE
    assert model_for(entity("绿色托盘", "container")) == CONTAINER
    assert model_for(entity("green tray", "container")) == CONTAINER
    assert model_for(entity("电源按钮", "button")) == BUTTON
    assert model_for(entity("power button", "button")) == BUTTON
    # Anything unrecognised becomes a graspable cube, the one shape every skill can act on.
    assert model_for(entity("widget")) == CUBE


def test_a_box_being_picked_up_is_a_cube_not_a_container() -> None:
    # "box" appears in both vocabularies; the cube reading has to win when the name also
    # says block, or the thing to pick up becomes a tray.
    assert model_for(TaskEntity(entity_id="e", semantic_name="box block",
                                category="object")) == CUBE


def test_resolved_assets_carry_matching_footprints() -> None:
    assets = resolve_entities([TaskEntity(entity_id="c", semantic_name="cube",
                                          category="object")])
    asset = assets["c"]
    assert asset.model_name == CUBE
    assert asset.dimensions_m == (0.06, 0.06, 0.06)


# -- anchor resolution ----------------------------------------------------------------

def test_container_interior_sits_above_the_floor() -> None:
    resolver = AnchorResolver(_registry(), runtime=None)
    pose = resolver("green_tray_01", "interior")
    assert pose is not None
    # The tray origin is on the tabletop at z=0.60; the interior anchor must clear it, or the
    # gripper is commanded into the table.
    assert pose["position"][2] == pytest.approx(0.655, abs=1e-3)
    assert "orientation" in pose


def test_grasp_anchor_is_the_object_centre() -> None:
    resolver = AnchorResolver(_registry(), runtime=None)
    pose = resolver("red_block_01", "grasp")
    assert pose is not None
    # A 6 cm cube resting at z=0.60 has its centre at 0.63.
    assert pose["position"][2] == pytest.approx(0.63, abs=1e-3)


def test_default_anchor_is_used_when_none_is_named() -> None:
    resolver = AnchorResolver(_registry(), runtime=None)
    assert resolver("green_tray_01", None)["position"] == pytest.approx(
        resolver("green_tray_01", "interior")["position"], abs=1e-9)


def test_unknown_object_or_anchor_returns_none_for_fallback() -> None:
    resolver = AnchorResolver(_registry(), runtime=None)
    # Returning None lets the executor fall back to the object's own pose rather than failing
    # the command outright.
    assert resolver("no_such_object", "grasp") is None
    assert resolver("red_block_01", "no_such_anchor") is None


def test_live_pose_wins_over_the_compile_time_reference() -> None:
    class MovedRuntime:
        def object_pose(self, name: str) -> dict[str, np.ndarray]:
            return {"position": np.array([1.0, 2.0, 3.0]),
                    "rotation_matrix": np.eye(3)}

    resolver = AnchorResolver(_registry(), MovedRuntime())
    pose = resolver("red_block_01", "grasp")
    # Objects move during a turn -- a grasped block travels with the gripper -- so the offset
    # must apply to where the object is now, not where it was composed.
    assert pose["position"][:2] == pytest.approx([1.0, 2.0])
    assert pose["position"][2] == pytest.approx(3.03, abs=1e-3)


def test_a_rotated_object_rotates_its_anchor_offset() -> None:
    # 90 degrees about +z: a local +x offset must come out along world +y.
    rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])

    class RotatedRuntime:
        def object_pose(self, name: str) -> dict[str, np.ndarray]:
            return {"position": np.zeros(3), "rotation_matrix": rotation}

    registry = {"objects": {"thing": {
        "object_id": "thing", "body_name": "thing",
        "spatial": {"default_anchor": "side",
                    "anchors": {"side": {"local_position": [0.1, 0.0, 0.0]}},
                    "reference_pose": {"position": [0.0, 0.0, 0.0]}}}}}
    pose = AnchorResolver(registry, RotatedRuntime())("thing", "side")
    assert pose["position"] == pytest.approx([0.0, 0.1, 0.0], abs=1e-9)


def test_a_failing_runtime_falls_back_to_the_reference_pose() -> None:
    class BrokenRuntime:
        def object_pose(self, name: str) -> dict:
            raise RuntimeError("socket closed")

    resolver = AnchorResolver(_registry(), BrokenRuntime())
    pose = resolver("red_block_01", "grasp")
    # A read failure must not lose the anchor; the compile-time pose still locates it.
    assert pose is not None and pose["position"][2] == pytest.approx(0.63, abs=1e-3)


def test_registry_can_be_loaded_from_the_file_the_compiler_wrote(tmp_path: Path) -> None:
    path = tmp_path / "interaction_registry.json"
    path.write_text(json.dumps(_registry(), ensure_ascii=False), encoding="utf-8")
    resolver = AnchorResolver(path, runtime=None)
    assert resolver("green_tray_01", "interior") is not None
    # An unreadable registry degrades to "no anchors" rather than raising.
    assert load_registry(tmp_path / "missing.json") == {}
