from types import SimpleNamespace
import json

import numpy as np
import pytest

from agent_service.skills.motion.move import command


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("caps", [(1., .5, 2.), (.3, .1, .4)])
def test_carry_caps_planner_and_servo_and_restores_after_exit(monkeypatch, tmp_path, held, fail, caps):
    path = tmp_path / "carry_config.json"
    path.write_text(json.dumps(dict(zip(
        ("velocity_limit_rad_s", "acceleration_limit_rad_s2", "jerk_limit_rad_s3"), caps))), encoding="utf-8")
    monkeypatch.setattr(command, "CARRY_CONFIG_PATH", path)
    runtime = SimpleNamespace(qpos_indices=[], execution_mode="kinematic",
                              max_joint_speed_deg=180., waypoint_observer=None)
    original = {"trajectory": {"velocity_limits": [3., .5, 3., 3., 3., 3.]}}

    class Skill:
        config = {"trajectory": {"velocity_limits": 3.}}

        def __init__(self, **kwargs):
            pass

        def execute(self, request):
            limits = request["strategy_params"]["trajectory"]["velocity_limits"]
            assert limits == ([caps[0], min(.5, caps[0]), *([caps[0]] * 4)] if held else original["trajectory"]["velocity_limits"])
            assert runtime.max_joint_speed_deg == pytest.approx(np.degrees(caps[0]) if held else 180.)
            if held:
                assert request["constraints"]["safety_distance"] == 0.005
                assert request["constraints"]["avoid_collision"] is True
                config = request["strategy_params"]["trajectory"]
                assert config["acceleration_limits"] * request["constraints"]["acceleration_scale"] == caps[1]
                assert config["jerk_limits"] == caps[2]
            if fail:
                raise RuntimeError("execution failed")
            return {"success": True}

    monkeypatch.setattr(command, "MoveSkill", Skill)
    services = SimpleNamespace(runtime=runtime, planning_time=5., max_attempts=1,
                               velocity_scale=1., safety_distance=0.,
                               held_colliders=lambda: {1} if held else set())
    context = SimpleNamespace(skill_name="move", services=services, target="home",
                              parameters={"strategy_params": original},
                              frames=SimpleNamespace(capture=lambda: None))
    if fail:
        with pytest.raises(RuntimeError, match="execution failed"):
            command.execute(context)
    else:
        command.execute(context)
    assert runtime.max_joint_speed_deg == 180.
    assert runtime.execution_mode == "kinematic"
    assert original["trajectory"]["velocity_limits"][0] == 3.


def test_carry_clearance_reloads_and_validates(monkeypatch, tmp_path):
    path = tmp_path / "clearance.json"
    monkeypatch.setattr(command, "CARRY_CLEARANCE_CONFIG_PATH", path)
    for value in (.08, .03, 0.0):
        path.write_text(json.dumps({"safety_distance_m": value}), encoding="utf-8")
        assert command._carry_clearance() == value
    for value in (-.01, True, "0.1", float("nan"), 1.1):
        path.write_text(json.dumps({"safety_distance_m": value}), encoding="utf-8")
        with pytest.raises(command.SkillCommandFailure):
            command._carry_clearance()
