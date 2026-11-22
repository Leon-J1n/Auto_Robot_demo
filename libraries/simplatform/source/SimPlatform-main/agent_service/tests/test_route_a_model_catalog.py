from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.scene.model_catalog import (  # noqa: E402
    ROUTE_A_MODEL_CATALOG_ENV,
    load_route_a_model_catalog,
)
from agent_service.planning.contracts import (  # noqa: E402
    Operation,
    TaskEntity,
    TaskIntent,
    TaskStatus,
    TaskType,
)
from agent_service.scene import (  # noqa: E402
    GeometrySpec,
    ScenePlacement,
    build_generated_registry,
    build_route_a_plan,
)
from agent_service.scene.composer import MIN_GAP_M, SceneComposer  # noqa: E402
from agent_service.scene_apply import apply_plan  # noqa: E402


def _intent() -> SimpleNamespace:
    return SimpleNamespace(
        entities=[
            SimpleNamespace(
                entity_id="red_ball", semantic_name="红色球", category="object", aliases=[]
            ),
            SimpleNamespace(
                entity_id="green_box", semantic_name="绿色盒子", category="container", aliases=[]
            ),
        ],
        operations=[SimpleNamespace(source="red_ball", destination="green_box")],
    )


def test_default_catalog_resolves_chinese_pick_and_place_models() -> None:
    catalog = load_route_a_model_catalog()

    resolved = catalog.resolve_intent(_intent())

    assert resolved.by_entity["red_ball"].model_id == "mesh:red_ball"
    assert resolved.by_entity["green_box"].model_id == "mesh:green_box"
    assert resolved.by_model_id == {
        "mesh:red_ball": resolved.by_entity["red_ball"],
        "mesh:green_box": resolved.by_entity["green_box"],
    }
    assert resolved.by_entity["red_ball"].movable is True
    assert "graspable" in resolved.by_entity["red_ball"].capabilities
    assert resolved.by_entity["red_ball"].bbox_min_m == (-0.025, -0.025, -0.025)
    assert resolved.by_entity["red_ball"].bbox_max_m == (0.025, 0.025, 0.025)
    assert resolved.by_entity["green_box"].movable is False
    assert "container" in resolved.by_entity["green_box"].capabilities
    assert catalog.base_scene.name == "ur5e_robotiq_2f85_pose.simstate"


def test_grasp_object_meshes_are_compatible_with_platform_texture_import() -> None:
    """The platform importer requires OBJ position, UV, and normal indices to align."""
    catalog = load_route_a_model_catalog()
    grasp_models = [model for model in catalog.models if model.model_id.startswith("grasp:")]

    assert {model.model_name for model in grasp_models} == {
        "apple", "banana", "baseball", "rubiks_cube", "sugar_box",
    }
    for model in grasp_models:
        lines = model.visual_obj.read_text(encoding="utf-8").splitlines()
        positions = sum(line.startswith("v ") for line in lines)
        texcoords = sum(line.startswith("vt ") for line in lines)
        normals = sum(line.startswith("vn ") for line in lines)
        corners = [
            corner
            for line in lines if line.startswith("f ")
            for corner in line.split()[1:]
        ]

        assert positions == texcoords == normals
        assert corners
        assert all(
            len(indices := corner.split("/")) == 3
            and indices[0] == indices[1] == indices[2]
            for corner in corners
        )
        material = model.visual_obj.with_name("textured.mtl").read_text(encoding="utf-8")
        assert model.texture is not None
        assert "Kd 1.0 1.0 1.0" in material
        assert f"map_Kd {model.texture.name}" in material


def test_unknown_object_is_not_substituted_by_its_generic_category() -> None:
    catalog = load_route_a_model_catalog()
    unknown = SimpleNamespace(
        entity_id="widget", semantic_name="未知部件", category="object", aliases=[])

    with pytest.raises(ValueError, match="no Route A model matches"):
        catalog.resolve_entities([unknown])


def _write_catalog(tmp_path: Path, models: list[dict]) -> Path:
    base_scene = tmp_path / "base.simstate"
    base_scene.touch()
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps({"version": 1, "baseScene": str(base_scene), "models": models}),
        encoding="utf-8",
    )
    return path


def _model(asset_path: Path, **overrides: object) -> dict:
    asset_path.touch(exist_ok=True)
    value = {
        "id": "mesh:item",
        "name": "item",
        "path": str(asset_path),
        "aliases": ["item alias"],
        "categories": ["object"],
        "dimensionsM": [0.1, 0.1, 0.1],
        "boundsM": {"min": [-0.05, -0.05, 0.0], "max": [0.05, 0.05, 0.1]},
        "movable": True,
        "capabilities": ["graspable"],
        "collisionShapes": [{
            "name": "body",
            "shape": "box",
            "localPositionM": [0.0, 0.0, 0.05],
            "sizeM": [0.1, 0.1, 0.1],
        }],
    }
    value.update(overrides)
    return value


def test_catalog_path_can_be_overridden_by_environment(tmp_path: Path, monkeypatch) -> None:
    model = _model(tmp_path / "item.obj", aliases=["测试物体"])
    config = _write_catalog(tmp_path, [model])
    monkeypatch.setenv(ROUTE_A_MODEL_CATALOG_ENV, str(config))

    catalog = load_route_a_model_catalog()

    assert catalog.models[0].model_id == "mesh:item"
    assert catalog.models[0].visual_obj == tmp_path / "item.obj"


def test_unique_category_fallback_keeps_exact_name_priority(tmp_path: Path) -> None:
    config = _write_catalog(tmp_path, [
        _model(tmp_path / "item.obj", categories=["fruit"]),
        _model(tmp_path / "box.obj", id="mesh:box", name="box",
               aliases=["box alias"], categories=["container"]),
    ])
    catalog = load_route_a_model_catalog(config)
    generic = SimpleNamespace(entity_id="requested", semantic_name="unknown",
                              category="fruit", aliases=[])
    assert catalog.resolve_entities([generic]).by_entity["requested"].model_id == "mesh:item"
    generic.semantic_name = "box"
    assert catalog.resolve_entities([generic]).by_entity["requested"].model_id == "mesh:box"


@pytest.mark.parametrize(
    ("models", "message"),
    [
        (
            lambda root: [
                _model(root / "one.obj", id="mesh:duplicate"),
                _model(root / "two.obj", id="mesh:duplicate", aliases=["other"]),
            ],
            "duplicate model id",
        ),
        (
            lambda root: [
                _model(root / "one.obj", aliases=["shared"]),
                _model(root / "two.obj", id="mesh:two", name="two", aliases=["SHARED"]),
            ],
            "duplicate model alias",
        ),
        (
            lambda root: [_model(root / "one.obj", dimensionsM=[0.1, 0.0, 0.1])],
            "dimensionsM",
        ),
        (
            lambda root: [_model(root / "one.obj", path=str(root / "missing.obj"))],
            "does not exist",
        ),
    ],
)
def test_invalid_catalog_is_rejected(tmp_path: Path, models, message: str) -> None:
    config = _write_catalog(tmp_path, models(tmp_path))

    with pytest.raises(ValueError, match=message):
        load_route_a_model_catalog(config)


def test_operation_roles_require_compatible_capabilities(tmp_path: Path) -> None:
    model = _model(tmp_path / "item.obj", aliases=["source"], capabilities=["container"])
    destination = _model(
        tmp_path / "destination.obj",
        id="mesh:destination",
        name="destination",
        aliases=["destination"],
        categories=["container"],
        movable=False,
        capabilities=["container"],
    )
    catalog = load_route_a_model_catalog(_write_catalog(tmp_path, [model, destination]))
    intent = SimpleNamespace(
        entities=[
            SimpleNamespace(entity_id="source", semantic_name="source", category="object", aliases=[]),
            SimpleNamespace(
                entity_id="destination",
                semantic_name="destination",
                category="container",
                aliases=[],
            ),
        ],
        operations=[SimpleNamespace(source="source", destination="destination")],
    )

    with pytest.raises(ValueError, match="source.*graspable"):
        catalog.resolve_intent(intent)


def _pick_and_place_intent() -> TaskIntent:
    return TaskIntent(
        status=TaskStatus.ACCEPTED,
        instruction="将红色球放入绿色盒子中。",
        task_types=[TaskType.PICK_AND_PLACE],
        entities=[
            TaskEntity(entity_id="red_ball", semantic_name="红色球", category="object"),
            TaskEntity(entity_id="green_box", semantic_name="绿色盒子", category="container"),
        ],
        operations=[Operation(
            operation_id="op-1",
            task_type=TaskType.PICK_AND_PLACE,
            source="red_ball",
            destination="green_box",
        )],
    )


def test_catalog_models_compose_into_an_executable_non_overlapping_scene() -> None:
    catalog = load_route_a_model_catalog()
    resolved = catalog.resolve_intent(_pick_and_place_intent())
    registry = SceneComposer().compose(
        _pick_and_place_intent(), resolved.by_entity, robot="ur5e", seed=42)
    plan = build_route_a_plan(
        registry, assets=resolved.by_model_id, base_scene=catalog.base_scene)

    assert plan.base_scene == str(catalog.base_scene)
    placements = {item.model_name: item for item in plan.placements}
    ball = placements["red_ball"]
    box = placements["green_box"]
    dx = abs(ball.local_position[0] - box.local_position[0])
    dy = abs(ball.local_position[1] - box.local_position[1])
    assert (
        dx >= (ball.dimensions_m[0] + box.dimensions_m[0]) / 2 + MIN_GAP_M
        or dy >= (ball.dimensions_m[1] + box.dimensions_m[1]) / 2 + MIN_GAP_M
    )
    assert ball.movable is True and ball.capabilities == frozenset({"graspable"})
    assert box.movable is False and box.capabilities == frozenset({"container"})
    assert ball.geometry[0].mesh_path == str(resolved.by_entity["red_ball"].visual_obj)
    assert box.geometry[0].mesh_path == str(resolved.by_entity["green_box"].visual_obj)
    assert [item.shape for item in ball.geometry[1:]] == ["sphere"]
    assert [item.shape for item in box.geometry[1:]] == ["box"] * 5

    interactions = build_generated_registry(
        plan, scene_fingerprint="a" * 64)
    ball_metadata = interactions["objects"][ball.object_id]
    box_metadata = interactions["objects"][box.object_id]
    assert ball_metadata["spatial"]["default_anchor"] == "grasp"
    # Mesh collision geometry is offset above the body contact point.  The body
    # pose itself remains at the tabletop, so the grasp anchor must target the
    # ball centre rather than subtracting that child-shape offset a second time.
    assert ball_metadata["spatial"]["anchors"]["grasp"]["local_position"] == pytest.approx(
        [0.0, 0.0, 0.025])
    assert box_metadata["spatial"]["default_anchor"] == "interior"


def test_runner_composes_only_task_entities_reproducibly():
    from agent_service.qwen_runner import PlatformAgentRunner
    from agent_service.scene.composer import WORKSPACE_X, WORKSPACE_Y, _overlap

    runner = object.__new__(PlatformAgentRunner)
    intent = _pick_and_place_intent()
    # A grasp-only instruction must not inject an unrelated container.
    grasp_only = SimpleNamespace(entities=intent.entities[:1], operations=[], spatial_relations=[])
    for task in (grasp_only, intent):
        registry, models, base = runner._compose(task, placement_seed=42)
        assert registry == runner._compose(task, placement_seed=42)[0]
        assert len(registry.objects) == len(task.entities)
        assert sum(obj.model_name == "green_box" for obj in registry.objects) == (task is intent)
        assert sum(obj.model_name == "red_ball" for obj in registry.objects) == 1
        plan = build_route_a_plan(registry, assets=models, base_scene=base)
        assert all(item.geometry[0].mesh_path for item in plan.placements)
        for index, obj in enumerate(registry.objects):
            for axis, bounds in enumerate((WORKSPACE_X, WORKSPACE_Y)):
                assert obj.position[axis] - obj.dimensions_m[axis] / 2 >= bounds[0] - 1e-6
                assert obj.position[axis] + obj.dimensions_m[axis] / 2 <= bounds[1] + 1e-6
            for other in registry.objects[index + 1:]:
                assert not _overlap(obj.position, obj.dimensions_m, other.position, other.dimensions_m)


class _FakeEntity:
    def __init__(self, object_id: str) -> None:
        self.reference = SimpleNamespace(object_id=object_id)
        self.name = None
        self.position = None
        self.rigid_body = None
        self.parent = None
        self.visible = True

    def set_name(self, value) -> None:
        self.name = str(value)

    def set_position(self, value) -> None:
        self.position = tuple(value)

    def set_rotation(self, _value) -> None:
        pass

    def set_rigid_body(self, **value) -> None:
        self.rigid_body = value

    def set_visual(self, **_value) -> None:
        if "visible" in _value:
            self.visible = bool(_value["visible"])

    def set_parent(self, parent, *, preserve_world: bool = False) -> None:
        self.parent = parent


class _FakeSession:
    def __init__(self) -> None:
        self.restored = None
        self.loaded: list[tuple[str, _FakeEntity]] = []
        self.primitives: list[tuple[str, str | None, tuple | None, _FakeEntity]] = []
        self._index = 0

    def restore_state_file(self, value: str) -> None:
        self.restored = value

    def create_primitive(self, _kind: str, **_value) -> _FakeEntity:
        self._index += 1
        entity = _FakeEntity(f"primitive-{self._index}")
        entity.reference.runtime_handle = self._index
        self.primitives.append((_kind, _value.get("name"), _value.get("size"), entity))
        return entity

    def load_asset(self, path: str) -> _FakeEntity:
        self._index += 1
        entity = _FakeEntity(f"asset-{self._index}")
        entity.reference.runtime_handle = self._index
        self.loaded.append((path, entity))
        return entity

    def scene_snapshot(self):
        return SimpleNamespace(entities=[], scene_generation=1)


def test_applying_catalog_scene_restores_base_and_sets_mesh_physics() -> None:
    catalog = load_route_a_model_catalog()
    intent = _pick_and_place_intent()
    resolved = catalog.resolve_intent(intent)
    registry = SceneComposer().compose(intent, resolved.by_entity, robot="ur5e", seed=42)
    plan = build_route_a_plan(
        registry, assets=resolved.by_model_id, base_scene=catalog.base_scene)
    session = _FakeSession()

    applied = apply_plan(session, plan, restore_base=True, ensure_lights=False)

    assert session.restored == str(catalog.base_scene)
    loaded = {Path(path).name: entity for path, entity in session.loaded}
    ball = loaded["red_ball_body.obj"]
    box = loaded["green_box.obj"]
    assert ball.rigid_body == {
        "enabled": True, "dynamic": True, "gravity": True,
        "mass": 0.2,
        "friction": (1.0, 1.0, 1.0),
    }
    assert box.rigid_body == {"enabled": True, "dynamic": False, "gravity": False}
    assert {ball.name, box.name} == {item.body_name for item in plan.placements}
    assert set(applied.objects.values()) == {
        ball.reference.object_id,
        box.reference.object_id,
    }
    # The OBJ is centred around z=0, so its +2.5 cm mesh offset rests its lowest vertex on z=.6.
    assert ball.position[2] == pytest.approx(0.625)
    assert box.position[2] == pytest.approx(0.6)
    collision_proxies = [(kind, name, size, entity)
                         for kind, name, size, entity in session.primitives
                         if name != "work_table"]
    assert collision_proxies == []
    assert ball.parent is None and box.parent is None
    assert ball.reference.object_id != box.reference.object_id
    # Exercise creation -> snapshot readback with distinct stable IDs and handles.
    from agent_service.scene_apply import read_back_generated_plan
    records = [{"id": entity.reference.runtime_handle, "name": entity.name,
                "world": {"translation": entity.position,
                          "rotationWxyz": [1., 0., 0., 0.]}}
               for _, entity in session.loaded]
    measured = read_back_generated_plan(plan, applied, records)
    for original, actual in zip(plan.placements, measured.placements):
        assert applied.entity_handles[original.object_id] in {r["id"] for r in records}
        assert actual.world_position == pytest.approx(original.world_position)


def test_applying_primitive_placement_keeps_primitive_as_physical_root() -> None:
    placement = ScenePlacement(
        object_id="cube_01",
        body_name="cube_01",
        semantic_name="cube",
        local_position=(0.0, 0.0, 0.0),
        world_position=(0.5, 0.0, 0.65),
        dimensions_m=(0.1, 0.1, 0.1),
        movable=True,
        geometry=(GeometrySpec(
            name="cube_01_body",
            shape="box",
            local_position=(0.0, 0.0, 0.05),
            half_extents=(0.05, 0.05, 0.05),
        ),),
    )
    session = _FakeSession()

    applied = apply_plan(
        session,
        SimpleNamespace(placements=(placement,), create_tabletop=False),
        restore_base=False,
        ensure_lights=False,
    )

    assert session.loaded == []
    assert len(session.primitives) == 1
    kind, name, size, root = session.primitives[0]
    assert (kind, name, size) == ("box", "cube_01", (0.1, 0.1, 0.1))
    assert root.rigid_body == {
        "enabled": True, "dynamic": True, "gravity": True,
        "mass": 0.2,
        "friction": (1.0, 1.0, 1.0),
    }
    assert applied.objects == {"cube_01": root.reference.object_id}


def test_configured_model_without_grasp_capability_gets_no_grasp_request() -> None:
    placement = ScenePlacement(
        object_id="decoration_01",
        body_name="decoration_01",
        semantic_name="decoration",
        local_position=(0.0, 0.0, 0.0),
        world_position=(0.5, 0.0, 0.6),
        dimensions_m=(0.1, 0.1, 0.1),
        model_id="mesh:decoration",
        model_name="decoration",
        movable=False,
        capabilities=frozenset(),
    )
    plan = SimpleNamespace(placements=(placement,), base_scene="base.simstate")

    metadata = build_generated_registry(plan, scene_fingerprint="b" * 64)
    decoration = metadata["objects"]["decoration_01"]

    assert decoration["interactable"] is False
    assert "default_anchor" not in decoration["spatial"]
    assert decoration["action_requests"] == {}
