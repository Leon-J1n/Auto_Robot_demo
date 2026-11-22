from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from agent_service.skills.motion.move import command
from agent_service.skills.motion.move.skill import MoveSkill
from agent_service.skills.platform_runtime import quaternion_to_matrix, rpy_to_matrix


class _Runtime:
    def object_pose(self, _name):
        return {"position": np.array([1.0, 2.0, 3.0]), "rotation_matrix": np.eye(3)}


def test_grasp_pose_requests_are_world_transformed_and_ordered(tmp_path, monkeypatch):
    root = tmp_path / "grasp_objects" / "apple"
    root.mkdir(parents=True)
    (root / "grasp_pose.json").write_text(json.dumps({
        "object_meta": {"object_name": "apple"},
        "poses": [[0.1, 0.0, 0.0, 0, 0, 0], [0.2, 0.0, 0.0, 0, 0, 0]],
    }), encoding="utf-8")
    monkeypatch.setattr(command, "_ASSET_ROOTS", (tmp_path / "grasp_objects",))
    monkeypatch.setenv(command.ROUTE_A_MODEL_CATALOG_ENV, str(tmp_path / "catalog.json"))

    requests = command._grasp_pose_requests(_Runtime(), "apple_body", {
        "target": {"type": "pose"}, "constraints": {"keep_end_effector_orientation": True},
    })

    assert [request["grasp_pose"]["index"] for request in requests] == [0, 1]
    assert requests[0]["target"]["position"] == [1.1, 2.0, 3.0]
    assert "keep_end_effector_orientation" not in requests[0]["constraints"]


def test_unknown_object_keeps_single_request():
    request = {"target": {"type": "named_state"}, "constraints": {}}
    assert command._grasp_pose_requests(_Runtime(), "unknown", request) == [request]


def test_composed_object_instance_name_matches_model_asset(tmp_path, monkeypatch):
    root = tmp_path / "grasp_objects" / "banana"
    root.mkdir(parents=True)
    (root / "grasp_pose.json").write_text(json.dumps({
        "object_meta": {"object_name": "banana"},
        "poses": [[0.0, 0.0, 0.0, 0, 0, 0]],
    }), encoding="utf-8")
    monkeypatch.setattr(command, "_ASSET_ROOTS", (tmp_path / "grasp_objects",))
    monkeypatch.setenv(command.ROUTE_A_MODEL_CATALOG_ENV, str(tmp_path / "catalog.json"))

    poses = command._grasp_poses("banana_01")

    assert len(poses) == 1


def test_grasp_failures_are_aggregated():
    result = command._aggregate_grasp_failures([
        {"success": False, "status": "failed", "error": {"error_code": "IK_FAILED"}},
        {"success": False, "status": "failed", "error": {"error_code": "PATH_NOT_FOUND"}},
    ], "apple")
    assert result["error"]["error_code"] == "ALL_GRASP_POSES_UNREACHABLE"
    assert result["error"]["details"]["pose_count"] == 2


def test_move_skill_preserves_preconfigured_held_object_exemption(monkeypatch):
    held = {101, 102}
    observed = []

    class _Checker:
        allowed_target_geom_ids = held.copy()

    class _Selector:
        def select(self, _request, _context):
            observed.append(skill.collision_checker.allowed_target_geom_ids.copy())
            raise RuntimeError("stop after collision policy check")

    skill = object.__new__(MoveSkill)
    skill.config = {}
    skill.collision_checker = _Checker()
    skill.selector = _Selector()
    skill.robot_runtime = object()
    monkeypatch.setattr(skill, "_get_runtime_context", lambda _request: {})

    result = skill.execute({
        "target": {"type": "pose", "position": {"x": 0.0, "y": 0.0, "z": 0.1}},
        "motion": {}, "planning": {}, "constraints": {}, "strategy_params": {},
    })

    assert result["error"]["error_code"] == "EXECUTION_FAILED"
    assert observed == [held]
    assert skill.collision_checker.allowed_target_geom_ids == held


def test_catalog_alias_reads_latest_pose_and_uses_meters_and_radians(tmp_path, monkeypatch):
    source = tmp_path / "grasp_pose.json"
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [{"name": "rubiks_cube",
        "aliases": ["rubik cube"], "graspPose": "grasp_pose.json"}]}), encoding="utf-8")
    monkeypatch.setenv(command.ROUTE_A_MODEL_CATALOG_ENV, str(catalog))
    monkeypatch.setattr(command, "_ASSET_ROOTS", ())
    runtime = _Runtime()
    rotation = rpy_to_matrix(0, 0, np.pi / 2)
    runtime.object_pose = lambda _: {"position": np.array([1, 2, 3]), "rotation_matrix": rotation}
    for offset in (0.1, 0.2):
        source.write_text(json.dumps({"poses": [[offset, 0, 0, np.pi, 0, 0]]}), encoding="utf-8")
        request = command._grasp_pose_requests(runtime, "rubik_cube_01", {"constraints": {}})[0]
        assert request["target"]["position"] == pytest.approx([1, 2 + offset, 3])
        orientation = request["target"]["orientation"]
        actual = quaternion_to_matrix([orientation[key] for key in "wxyz"])
        assert actual == pytest.approx(rotation @ rpy_to_matrix(np.pi, 0, 0))
        assert request["grasp_pose"]["source"] == str(source.resolve())


def test_installed_cube_alias_points_to_user_editable_asset():
    poses = command._grasp_poses("rubik_cube_01")
    expected = Path(__file__).resolve().parents[1] / "objects/grasp_objects/rubiks_cube/grasp_pose.json"
    assert poses
    assert poses[0][0] == expected
    assert [pose for _, pose in poses] == json.loads(expected.read_text(encoding="utf-8"))["poses"]


def test_adapter_tries_poses_in_order_and_reports_selected_pose(monkeypatch):
    seen = []
    runtime = _Runtime()
    runtime.qpos_indices = [0]
    monkeypatch.setattr(command, "_grasp_poses", lambda _: [
        (Path("test.json"), [index * 0.1, 0, 0, 0, 0, 0]) for index in range(3)])

    class Skill:
        def __init__(self, **kwargs):
            pass

        def execute(self, request):
            seen.append(request["grasp_pose"]["index"])
            return {"success": len(seen) == 2, "error": {"error_code": "IK_FAILED"}}

    monkeypatch.setattr(command, "MoveSkill", Skill)
    services = SimpleNamespace(runtime=runtime, planning_time=5, max_attempts=3,
                               velocity_scale=0.5, safety_distance=0, held_colliders=lambda: ())
    context = SimpleNamespace(skill_name="move", services=services, target="rubik_cube_01",
                              parameters={}, frames=SimpleNamespace(capture=lambda: None))
    result = command.execute(context)
    assert seen == [0, 1]
    assert result["grasp_pose"]["index"] == 1
    assert result["grasp_pose"]["world_target"]["position"] == pytest.approx([1.1, 2, 3])
