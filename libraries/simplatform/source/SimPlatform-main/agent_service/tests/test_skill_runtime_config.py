import json
from types import SimpleNamespace

import pytest

from agent_service.skills.command_registry import SkillCommandFailure
from agent_service.skills.manipulation.grasp import command as grasp
from agent_service.skills.manipulation.grasp.strategies.default_grasp.default_grasp import DefaultGrasp
from agent_service.skills.motion.move import command as move


@pytest.mark.parametrize("override", [None, 9])
def test_grasp_config_reload_reaches_controller(monkeypatch, tmp_path, override):
    path = tmp_path / "contact.json"
    monkeypatch.setattr(grasp, "CONTACT_CONFIG_PATH", path)
    received = []
    controller = SimpleNamespace(
        open=lambda *a, **kw: None,
        close=lambda *a, **kw: received.append(kw["post_contact_steps"]) or {"contact_detected": False},
        get_state=lambda *a: dict(contact_detected=False, width=0., force=0., holding=False,
                                 object_present=False, slip_detected=False))
    runtime = SimpleNamespace(realtime=False)

    class Skill:
        def __init__(self, **kwargs):
            pass

        def execute(self, request):
            result = DefaultGrasp().execute(request, {"gripper_controller": controller, "runtime": runtime})
            return {"success": True, "grasp_result": result}

    monkeypatch.setattr(grasp, "GraspSkill", Skill)
    context = SimpleNamespace(skill_name="grasp", target="banana_01",
                              services=SimpleNamespace(gripper=controller, runtime=runtime),
                              parameters={} if override is None else {"post_contact_steps": override})
    for steps in (10, 50, 0):
        path.write_text(json.dumps({"post_contact_step": steps}), encoding="utf-8")
        grasp.execute(context)
    assert received == ([10, 50, 0] if override is None else [override] * 3)


@pytest.mark.parametrize("value", [-1, 0, True, "0.5", float("nan"), float("inf")])
def test_move_rejects_invalid_caps(monkeypatch, tmp_path, value):
    path = tmp_path / "carry.json"
    path.write_text(json.dumps(dict(velocity_limit_rad_s=value, acceleration_limit_rad_s2=.5,
                                    jerk_limit_rad_s3=2.)), encoding="utf-8")
    monkeypatch.setattr(move, "CARRY_CONFIG_PATH", path)
    with pytest.raises(SkillCommandFailure) as error:
        move._carry_limits()
    assert error.value.error_code == "INVALID_CONFIG"


@pytest.mark.parametrize("value", [-1, 1.5, True, "10", 10001])
def test_grasp_rejects_invalid_steps(monkeypatch, tmp_path, value):
    path = tmp_path / "contact.json"
    path.write_text(json.dumps({"post_contact_step": value}), encoding="utf-8")
    monkeypatch.setattr(grasp, "CONTACT_CONFIG_PATH", path)
    with pytest.raises(SkillCommandFailure) as error:
        grasp._post_contact_steps()
    assert error.value.error_code == "INVALID_CONFIG"


def test_move_caps_reload(monkeypatch, tmp_path):
    path = tmp_path / "carry.json"
    monkeypatch.setattr(move, "CARRY_CONFIG_PATH", path)
    for speed in (.3, .6):
        path.write_text(json.dumps(dict(velocity_limit_rad_s=speed, acceleration_limit_rad_s2=.1,
                                       jerk_limit_rad_s3=.4)), encoding="utf-8-sig")
        assert move._carry_limits() == (speed, .1, .4)
