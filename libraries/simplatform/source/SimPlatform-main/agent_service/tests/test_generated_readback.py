import pytest

from agent_service.scene.builder import ScenePlan, ScenePlacement, GeometrySpec
from agent_service.scene_apply import AppliedScene, SceneApplyError, read_back_generated_plan


def test_final_world_pose_preserves_binding_and_removes_mesh_origin_offset():
    item = ScenePlacement(object_id="apple_01", body_name="apple_01", semantic_name="apple",
                          local_position=(0., 0., 0.), world_position=(0.525, 0., .6),
                          dimensions_m=(.1, .1, .1), entity_id="requested_apple",
                          geometry=(GeometrySpec(name="mesh", shape="mesh",
                                                 local_position=(0., 0., .04), mesh_path="apple.obj"),))
    plan = ScenePlan(route="A", scene_id="test", robot="ur5e", base_scene="base",
                     placements=(item,), bindings={"requested_apple": "apple_01"})
    applied = AppliedScene(objects={"apple_01": "stable-apple-id"},
                           entity_handles={"apple_01": 42})
    records = [{"id": 42, "world": {"translation": [.55, .1, .65],
                                    "rotationWxyz": [1., 0., 0., 0.]}}]
    final = read_back_generated_plan(plan, applied, records)
    assert final.bindings == plan.bindings
    assert final.placements[0].entity_id == "requested_apple"
    assert final.placements[0].world_position == pytest.approx((.55, .1, .61))
    assert final.placements[0].local_position == pytest.approx((.025, .1, .01))
    assert plan.placements[0].world_position == (.525, 0., .6)
    # A half turn around X flips the mesh-origin offset in world space.
    records[0]["world"]["rotationWxyz"] = [0., 2., 0., 0.]
    rotated = read_back_generated_plan(plan, applied, records)
    assert rotated.placements[0].world_position == pytest.approx((.55, .1, .69))
    assert rotated.placements[0].orientation_wxyz == (0., 1., 0., 0.)
    with pytest.raises(SceneApplyError, match="missing"):
        read_back_generated_plan(plan, applied, [])
    records[0]["world"]["translation"] = [float("nan"), 0., 0.]
    with pytest.raises(SceneApplyError, match="Invalid"):
        read_back_generated_plan(plan, applied, records)
