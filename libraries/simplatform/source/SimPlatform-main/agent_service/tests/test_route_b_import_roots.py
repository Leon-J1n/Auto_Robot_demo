"""Imported mesh descendants retain their task object's semantic identity."""

import pytest

from agent_service.scene.discovery import discover_task_objects


def test_imported_apple_and_compound_box_keep_rigidbody_root_identity():
    records = [
        {"id": 1, "name": "apple_01", "rigidBody": {"motionType": "dynamic"}},
        {"id": 2, "name": "textured", "parent": 1},
        {"id": 3, "name": "textured", "parent": 2, "visual": {"mesh": "apple.obj"}},
        {"id": 4, "name": "green_box_01", "rigidBody": {"motionType": "static"}},
        {"id": 5, "name": "wall_left", "parent": 4, "has_geometry": True},
        {"id": 6, "name": "wall_right", "parent": 4, "has_geometry": True},
    ]
    assert [item.name for item in discover_task_objects(records)] == [
        "apple_01", "green_box_01",
    ]


def test_generic_scene_container_does_not_collapse_distinct_objects():
    records = [
        {"id": 1, "name": "imported_scene"},
        {"id": 2, "name": "apple_01", "parent": 1, "rigidBody": {}},
        {"id": 3, "name": "textured", "parent": 2, "has_geometry": True},
        {"id": 4, "name": "red_ball_01", "parent": 1, "has_geometry": True},
        {"id": 5, "name": "empty_object", "parent": 1, "rigidBody": {}},
    ]
    assert [item.name for item in discover_task_objects(records)] == [
        "apple_01", "red_ball_01",
    ]


def test_trajectory_markers_and_robot_descendants_are_not_task_objects():
    records = [
        {"id": 1, "name": "__trajectory_move_0", "has_geometry": True},
        {"id": 2, "name": "dot", "parent": 1, "has_geometry": True},
        {"id": 3, "name": "arm", "robot": {}, "rigidBody": {}},
        {"id": 4, "name": "tool", "parent": 3, "has_geometry": True},
        {"id": 5, "name": "apple_01", "rigidBody": {}},
        {"id": 6, "name": "__trajectory_nested", "parent": 5, "has_geometry": True},
    ]
    assert discover_task_objects(records) == []


@pytest.mark.parametrize("component", ["rigidBody", "rigid_body", "rigidbody"])
def test_rigidbody_component_aliases(component):
    records = [
        {"id": 1, "name": "apple_01", component: {}},
        {"id": 2, "name": "mesh", "parent": 1, "has_geometry": True},
    ]
    assert [item.entity_id for item in discover_task_objects(records)] == [1]


def test_primitive_geometry_root_still_owns_child_geometry():
    records = [
        {"id": 1, "name": "green_box_01", "has_geometry": True},
        {"id": 2, "name": "wall", "parent": 1, "has_geometry": True},
    ]
    assert [item.entity_id for item in discover_task_objects(records)] == [1]


@pytest.mark.parametrize("destination_name", ["绿色盒子", "盒子", "box"])
@pytest.mark.parametrize("box_name", ["green_box_01", "box_01"])
def test_second_turn_grounds_chinese_apple_and_compiles_existing_scene(tmp_path, destination_name, box_name):
    from agent_service.grounding import ground_task
    from agent_service.planning.contracts import TaskIntent, TaskEntity, Operation, TaskType, TaskStatus
    from agent_service.planning.recipe_planner import RecipePlanner
    from agent_service.scene import build_route_b_plan, build_generated_registry
    from agent_service.compile.compiler import compile_execution_bundle

    records = []
    for i, name in enumerate(("rubik_cube_01", box_name, "apple_01", "sugar_box_01")):
        root = 10 + i * 3
        pose = {"translation": [.5, i * .2, .65], "scale": [1, 1, 1],
                "rotationWxyz": [1, 0, 0, 0]}
        records.extend([
            {"id": root, "name": name, "flags": ["rigidBody"], "world": pose},
            {"id": root + 1, "name": "green_box" if name == box_name else "textured", "parent": root, "world": pose},
            {"id": root + 2, "name": "textured", "parent": root + 1,
             "visual": {"shape": "mesh"}, "world": pose},
        ])
    intent = TaskIntent(status=TaskStatus.ACCEPTED, instruction="把苹果放进绿色盒子里",
                        task_types=[TaskType.PICK_AND_PLACE], entities=[
        TaskEntity(entity_id="source", semantic_name="苹果", category="object"),
        TaskEntity(entity_id="destination", semantic_name=destination_name, category="container"),
    ], operations=[Operation(operation_id="op-1", task_type=TaskType.PICK_AND_PLACE,
                             source="source", destination="destination")])
    plan = build_route_b_plan(records)
    task = ground_task(intent, plan)
    assert [item.object_id for item in task.entities] == ["apple_01", box_name]
    assert plan.placement("apple_01").dimensions_m == pytest.approx((.074655, .074732, .071247))
    assert plan.placement(box_name).movable is False
    metadata = build_generated_registry(plan, scene_fingerprint="a" * 64)
    assert metadata["objects"][box_name]["spatial"]["anchors"]["interior"]["local_position"][2] == pytest.approx(.11)
    skill_plan = RecipePlanner().plan(task)
    compile_execution_bundle(skill_plan, task, scene_plan=plan, output_dir=tmp_path)
    from agent_service.protocol import load_command_document
    commands = load_command_document(tmp_path / "commands.json").commands
    assert not any(c.skill_name == "move" and c.parameters.get("target") == "end_effector"
                   for c in commands)
    assert [c.command_id for c in commands] == [
        f"command-{i:03d}" for i in range(1, len(commands) + 1)]


def test_generic_box_with_two_containers_requires_disambiguation():
    from types import SimpleNamespace
    from agent_service.grounding import GroundingError, SceneObjectView, _match_by_name

    views = [SceneObjectView(object_id=name, aliases=("绿色盒子",),
                             capabilities=frozenset({"container"}))
             for name in ("green_box_01", "green_box_02")]
    with pytest.raises(GroundingError, match="匹配到多个物体") as error:
        _match_by_name(SimpleNamespace(semantic_name="盒子", aliases=[]), views,
                       container_only=True)
    assert error.value.code == "TARGET_AMBIGUOUS"
