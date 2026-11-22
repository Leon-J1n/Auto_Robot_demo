import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_service.compile.compiler import _inject_macros
from agent_service.compile.execution_profile import (
    BUNDLED_PROFILE_DIR, PROFILE_ROOT_ENV, load_execution_profile,
)


@pytest.mark.parametrize("disabled", [True, False])
@pytest.mark.parametrize("skill", ["grasp", "release"])
def test_only_added_tool_moves_are_disabled(disabled, skill, monkeypatch):
    monkeypatch.delenv(PROFILE_ROOT_ENV, raising=False)
    profile = replace(load_execution_profile(), disable_end_effector=disabled)
    calls = []
    _inject_macros(lambda *args, **kwargs: calls.append((args, kwargs)),
                   SimpleNamespace(skill_name=skill, step_id="step-3"),
                   SimpleNamespace(task_type="pick_and_place"), profile)
    assert [args[1] for args, _ in calls] == (
        ["home"] if disabled else ["end_effector", "home"])
    assert all(args[2] == "step-3" for args, _ in calls)
    if not disabled:
        assert calls[0][1]["path_type"] == "linear"


@pytest.mark.parametrize("value", [None, True, False, "false", 0])
def test_profile_switch_defaults_and_validation(tmp_path, monkeypatch, value):
    raw = json.loads((BUNDLED_PROFILE_DIR / "default.json").read_text(encoding="utf-8"))
    assert raw.pop("disable_end_effector") is True
    if value is not None:
        raw["disable_end_effector"] = value
    directory = tmp_path / "execution_profiles"
    directory.mkdir()
    (directory / "default.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path))
    if value is not None and not isinstance(value, bool):
        with pytest.raises(ValueError, match="disable_end_effector must be boolean"):
            load_execution_profile()
    else:
        assert load_execution_profile().disable_end_effector is (True if value is None else value)
