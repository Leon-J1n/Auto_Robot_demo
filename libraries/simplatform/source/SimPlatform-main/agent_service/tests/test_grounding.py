"""Tests for entity grounding and scene application.

The property that matters most here: a nonempty world is never replaced by scene
generation, even when grounding fails. Missing targets must surface as blockers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.grounding import GroundingError, ground_task  # noqa: E402
from agent_service.planning.contracts import (Operation, TaskEntity, TaskIntent,  # noqa: E402
                                              TaskStatus, TaskType)
from agent_service.scene import build_route_b_plan, discover_task_objects  # noqa: E402
from agent_service.scene_apply import SceneApplyError, apply_plan  # noqa: E402
from agent_service.qwen_runner import PlatformAgentRunner  # noqa: E402
from agent_service.runner import TurnBlocked  # noqa: E402


def _intent(*, target: str = "cube", entity_name: str = "红色方块") -> TaskIntent:
    return TaskIntent(
        status=TaskStatus.ACCEPTED,
        instruction="把红色方块拿起来",
        task_types=[TaskType.GRASP],
        entities=[TaskEntity(entity_id=target, semantic_name=entity_name, category="cube")],
        operations=[Operation(operation_id="op-1", task_type=TaskType.GRASP, target=target)])


def _snapshot_entities(names: list[str]) -> list[dict]:
    entities: list[dict] = [
        {"id": 1, "name": "ur5e", "parent": None, "flags": ["robot"]},
        {"id": 2, "name": "work_table", "parent": None, "visual": {"shape": "box"}},
    ]
    for index, name in enumerate(names, start=3):
        entities.append({"id": index, "name": name, "parent": None,
                         "visual": {"shape": "box"}, "collider": {"shape": "box"},
                         "world": {"translation": [0.5, 0.0, 0.65]}})
    return entities


# -- grounding ------------------------------------------------------------------------


@pytest.mark.parametrize("entities", [
    [],
    [{"id": 1, "light": {"type": "ambient"}}],
    [{"id": 1, "flags": ["light"]}, {"id": 2, "light": {"type": "directional"}}],
])
def test_empty_or_light_only_scene_generates_route_a(monkeypatch, entities) -> None:
    runner = PlatformAgentRunner.__new__(PlatformAgentRunner)
    session = SimpleNamespace(scene_snapshot=lambda: SimpleNamespace(entities=entities))
    intent = _intent()
    plan = SimpleNamespace(route="A")
    applied = object()
    calls = []
    monkeypatch.setattr(runner, "_compose", lambda value: ("registry", "assets", "base"))
    monkeypatch.setattr("agent_service.scene.build_route_a_plan", lambda *args, **kw: plan)

    def apply(current_session, current_plan, **options):
        calls.append((current_session, current_plan, options))
        return applied

    monkeypatch.setattr("agent_service.scene_apply.apply_plan", apply)
    monkeypatch.setattr("agent_service.scene_apply.read_back_generated_plan",
                        lambda current, result, records: current)
    assert runner._resolve_scene(session, intent) == (plan, applied)
    assert calls == [(session, plan, {"restore_base": True})]


@pytest.mark.parametrize("entities", [
    _snapshot_entities([]),  # Robot and table, but no requested object.
    [{"id": 1, "name": "work_table", "visual": {"shape": "box"}}],
    [{"id": 1, "name": "camera", "flags": ["camera"], "light": None}],
    [{"id": 1, "name": "user_group", "flags": []}],
    [{"id": 1, "light": {}}, {"id": 2, "name": "cube", "visual": {"shape": "box"}}],
])
def test_nonempty_scene_preserved_even_without_robot_or_target(monkeypatch, entities) -> None:
    runner = PlatformAgentRunner.__new__(PlatformAgentRunner)
    session = SimpleNamespace(scene_snapshot=lambda: SimpleNamespace(entities=entities))

    def forbidden(*args, **kwargs):
        pytest.fail("an existing scene must not be regenerated or restored")

    monkeypatch.setattr(runner, "_compose", forbidden)
    monkeypatch.setattr("agent_service.scene_apply.apply_plan", forbidden)
    intent = _intent(entity_name="missing_object")
    plan, applied = runner._resolve_scene(session, intent)
    assert plan.route == "B"
    assert applied is None
    assert plan.create_tabletop is False
    with pytest.raises(TurnBlocked) as error:
        runner._ground(intent, plan)
    assert error.value.code in {"TARGET_NOT_FOUND", "PERCEPTION_REQUIRED"}

def test_discovered_objects_are_bindable_by_name() -> None:
    # Route B records a discovered object as its scene entity name; grounding must accept that
    # shape, or an in-place turn would look empty and needlessly rebuild the scene.
    plan = build_route_b_plan(_snapshot_entities(["红色方块"]), scene_id="main")
    task = ground_task(_intent(), plan)
    assert [entity.object_id for entity in task.entities] == ["红色方块"]
    assert task.entities[0].grounding_method == "interaction_registry"


def test_grounding_matches_a_latin_name_from_a_chinese_alias() -> None:
    plan = build_route_b_plan(_snapshot_entities(["red_cube_1"]), scene_id="main")
    intent = _intent(entity_name="red_cube")
    task = ground_task(intent, plan)
    assert task.entities[0].object_id == "red_cube_1"


def test_robot_subtree_is_excluded_structurally() -> None:
    # A robot link whose name matches none of the token lists must still not surface as a
    # graspable object. The snapshot marks the robot root, so its whole subtree is excluded.
    records = [
        {"id": 1, "name": "ur5e", "parent": None, "robot": {"jointNames": ["a"]}},
        {"id": 2, "name": "ur5e_base", "parent": 1, "visual": {"shape": "box"}},
        {"id": 3, "name": "red_cube", "parent": None, "visual": {"shape": "box"}},
    ]
    assert [item.name for item in discover_task_objects(records)] == ["red_cube"]


def test_gateway_flag_spelling_excludes_the_robot_too() -> None:
    # The gateway snapshot reports robot membership through flags rather than a component.
    records = [
        {"id": 1, "name": "ur5e", "parent": 0, "flags": ["robot"]},
        {"id": 2, "name": "ur5e_base", "parent": 1, "flags": ["joint", "visual"],
         "visual": {"shape": "box"}},
        {"id": 3, "name": "green_tray", "parent": 0, "flags": ["visual"],
         "visual": {"shape": "box"}},
    ]
    assert [item.name for item in discover_task_objects(records)] == ["green_tray"]


def test_a_scene_without_a_robot_still_discovers_objects() -> None:
    records = [{"id": 1, "name": "red_cube", "parent": None, "visual": {"shape": "box"}},
               {"id": 2, "name": "blue_ball", "parent": None,
                "collider": {"shape": "sphere"}}]
    assert [item.name for item in discover_task_objects(records)] == ["red_cube", "blue_ball"]


def test_infrastructure_is_never_bound() -> None:
    # The table and the robot must not be offered as task objects.
    plan = build_route_b_plan(_snapshot_entities([]), scene_id="main")
    with pytest.raises(GroundingError) as error:
        ground_task(_intent(entity_name="work_table"), plan)
    assert error.value.code in {"TARGET_NOT_FOUND", "PERCEPTION_REQUIRED"}


def test_missing_object_reports_a_structured_blocker() -> None:
    plan = build_route_b_plan(_snapshot_entities(["绿色托盘"]), scene_id="main")
    with pytest.raises(GroundingError) as error:
        ground_task(_intent(), plan)
    # Route B without authored metadata cannot guess a binding.
    assert error.value.code in {"TARGET_NOT_FOUND", "PERCEPTION_REQUIRED"}
    assert "红色方块" in error.value.message


def test_two_entities_bind_to_distinct_objects() -> None:
    plan = build_route_b_plan(_snapshot_entities(["cube_a", "cube_b"]), scene_id="main")
    intent = TaskIntent(
        status=TaskStatus.ACCEPTED, instruction="把 cube_a 放到 cube_b 上",
        task_types=[TaskType.PICK_AND_PLACE],
        entities=[TaskEntity(entity_id="a", semantic_name="cube_a", category="cube"),
                  TaskEntity(entity_id="b", semantic_name="cube_b", category="cube")],
        operations=[Operation(operation_id="op-1", task_type=TaskType.PICK_AND_PLACE,
                              source="a", destination="b")])
    task = ground_task(intent, plan)
    ids = [entity.object_id for entity in task.entities]
    assert sorted(ids) == ["cube_a", "cube_b"]


def test_instruction_without_a_target_is_rejected() -> None:
    plan = build_route_b_plan(_snapshot_entities(["cube"]), scene_id="main")
    # An accepted intent must carry entities and operations, so the empty case to cover is an
    # operation that references no entity at all.
    intent = TaskIntent(
        status=TaskStatus.ACCEPTED, instruction="随便动一下", task_types=[TaskType.MOVE],
        entities=[TaskEntity(entity_id="cube", semantic_name="cube", category="cube")],
        operations=[Operation(operation_id="op-1", task_type=TaskType.MOVE)])
    with pytest.raises(GroundingError) as error:
        ground_task(intent, plan)
    assert error.value.code == "INVALID_REQUEST"


# -- scene application ----------------------------------------------------------------

class FakeEntity:
    def __init__(self, object_id: str):
        self.reference = type("Ref", (), {"object_id": object_id})()
        self.position: tuple[float, float, float] | None = None
        self.rigid_body: dict | None = None
        self.name: str | None = None

    def set_position(self, value) -> None:
        self.position = tuple(float(item) for item in value)

    def set_rotation(self, value) -> None:
        self.rotation = tuple(float(item) for item in value)

    def set_name(self, value) -> None:
        self.name = str(value)

    def set_visual(self, **kwargs) -> None:
        self.visual = kwargs

    def set_rigid_body(self, **kwargs) -> str:
        self.rigid_body = kwargs
        return "ok"

    def set_parent(self, parent, *, preserve_world: bool = False) -> None:
        self.parent = parent


class FakeSession:
    def __init__(self, *, lit: bool = True):
        self.restored: list[str] = []
        self.primitives: list[dict] = []
        self.lights: list[dict] = []
        self.assets: list[str] = []
        self._counter = 0
        self._lit = lit

    def _next(self, prefix: str) -> FakeEntity:
        self._counter += 1
        return FakeEntity(f"{prefix}-{self._counter}")

    def restore_state_file(self, path: str) -> float:
        self.restored.append(path)
        return 0.0

    def create_primitive(self, kind: str, *, name=None, size=None, **_) -> FakeEntity:
        self.primitives.append({"kind": kind, "name": name, "size": size})
        return self._next("prim")

    def load_asset(self, path: str, **_) -> FakeEntity:
        self.assets.append(path)
        return self._next("asset")

    def create_light(self, type: str = "point", **kwargs) -> FakeEntity:
        self.lights.append({"type": type, **kwargs})
        return self._next("light")

    def scene_snapshot(self):
        entities = [{"id": 1, "name": "ur5e", "flags": ["robot"]}]
        if self._lit:
            entities.append({"id": 2, "name": "Sun", "light": {"type": "directional"}})
        return type("Snap", (), {"entities": entities, "scene_generation": 4})()


def test_route_b_plan_is_applied_without_restoring_when_asked() -> None:
    plan = build_route_b_plan(_snapshot_entities(["cube"]), scene_id="main")
    session = FakeSession()
    result = apply_plan(session, plan, restore_base=False)
    # Nothing may be restored or created when the turn operates in place.
    assert session.restored == []
    assert result.restored is None


def test_missing_base_scene_fails_with_a_structured_error() -> None:
    plan = build_route_b_plan(_snapshot_entities(["cube"]), scene_id="main")
    object.__setattr__(plan, "base_scene", Path("F:/does/not/exist.simstate"))
    with pytest.raises(SceneApplyError) as error:
        apply_plan(FakeSession(), plan, restore_base=True)
    assert error.value.code == "SCENE_INVALID"


def test_lights_are_added_only_when_the_scene_has_none() -> None:
    plan = build_route_b_plan(_snapshot_entities(["cube"]), scene_id="main")
    lit = FakeSession(lit=True)
    apply_plan(lit, plan, restore_base=False, ensure_lights=True)
    assert lit.lights == []
    # A world saved headlessly carries no light and would render black in the browser.
    dark = FakeSession(lit=False)
    result = apply_plan(dark, plan, restore_base=False, ensure_lights=True)
    assert dark.lights and result.created_lights
