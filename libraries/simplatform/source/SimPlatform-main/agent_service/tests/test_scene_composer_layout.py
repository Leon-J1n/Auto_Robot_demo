"""Generated tabletop placements retain footprint margins and distinct distractors."""

from itertools import combinations
from types import SimpleNamespace

import pytest

from agent_service.scene.builder import TABLETOP_HALF_EXTENTS
from agent_service.scene.composer import MIN_GAP_M, WORKSPACE_X, WORKSPACE_Y, SceneComposer
from agent_service.scene.model_catalog import load_route_a_model_catalog


def _intent(*model_names):
    return SimpleNamespace(
        entities=[SimpleNamespace(entity_id=name, semantic_name=name) for name in model_names],
        spatial_relations=[],
    )


@pytest.mark.parametrize("target_name", ["baseball", "banana", "rubiks_cube"])
def test_four_objects_fit_central_region_without_touching_for_many_seeds(target_name):
    catalog = load_route_a_model_catalog()
    models = {model.model_name: model for model in catalog.models}
    assert WORKSPACE_X == pytest.approx(tuple(sign * TABLETOP_HALF_EXTENTS[0] * 0.7 for sign in (-1, 1)))
    assert WORKSPACE_Y == pytest.approx(tuple(sign * TABLETOP_HALF_EXTENTS[1] * 0.7 for sign in (-1, 1)))
    task_assets = {name: models[name] for name in (target_name, "green_box")}
    pool = {model.model_id: model for model in catalog.models}
    chosen_pairs = set()
    for seed in range(100):
        registry = SceneComposer().compose(
            _intent(target_name, "green_box"), task_assets, seed=seed,
            distractor_assets=pool,
        )
        assert len(registry.objects) == 4
        assert set(registry.bindings) == set(task_assets)
        assert len({item.model_id for item in registry.objects}) == 4
        distractors = [item for item in registry.objects if item.role == "distractor"]
        assert len(distractors) == 2
        assert all(item.entity_id is None and item.candidate_for is None for item in distractors)
        assert all(pool[item.model_id].movable for item in distractors)
        chosen_pairs.add(tuple(sorted(item.model_id for item in distractors)))
        for item in registry.objects:
            for axis, limits in enumerate((WORKSPACE_X, WORKSPACE_Y)):
                half_extent = item.dimensions_m[axis] / 2
                assert item.position[axis] - half_extent >= limits[0] - 1e-12
                assert item.position[axis] + half_extent <= limits[1] + 1e-12
            assert item.position[2] == 0
        for first, second in combinations(registry.objects, 2):
            assert any(
                abs(first.position[axis] - second.position[axis])
                >= (first.dimensions_m[axis] + second.dimensions_m[axis]) / 2 + MIN_GAP_M
                for axis in (0, 1)
            )
    assert len(chosen_pairs) > 1


def test_seed_preserves_distractor_selection_and_placement():
    catalog = load_route_a_model_catalog()
    intent = _intent("baseball", "green_box")
    assets = catalog.resolve_entities(intent.entities).by_entity
    pool = {model.model_id: model for model in catalog.models}
    assert SceneComposer().compose(intent, assets, seed=42, distractor_assets=pool) == (
        SceneComposer().compose(intent, assets, seed=42, distractor_assets=pool)
    )


def test_impossible_placement_fails_instead_of_clamping_outside_region():
    asset = SimpleNamespace(model_id="large", model_name="large", dimensions_m=(0.6, 0.1, 0.1))
    with pytest.raises(ValueError, match="does not fit"):
        SceneComposer().compose(_intent("large"), {"large": asset})


def test_missing_distractors_fails_instead_of_silently_generating_incomplete_scene():
    asset = SimpleNamespace(model_id="target", model_name="target", dimensions_m=(0.1, 0.1, 0.1))
    with pytest.raises(ValueError, match="0 non-target models"):
        SceneComposer().compose(_intent("target"), {"target": asset}, distractor_assets={})
