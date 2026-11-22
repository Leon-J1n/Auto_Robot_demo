from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from sim_platform import EditorRpcError, EditorSession
from sim_platform.editor import EditorRobot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA = PROJECT_ROOT / "tests" / "data"
EDITOR = Path(os.environ.get("SIM_PLATFORM_EDITOR", ""))


def test_editor_api_import_does_not_require_c_library(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "python")
    environment["SIM_PLATFORM_LIBRARY"] = str(tmp_path / "missing-sim-platform-library")
    result = subprocess.run(
        [sys.executable, "-c", "from sim_platform import EditorSession; print(EditorSession.__name__)"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert result.stdout.strip() == "EditorSession"


def test_editor_robot_get_link_uses_runtime_handle() -> None:
    class FakeSession:
        def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert method == "robot.get_link"
            assert params == {"robot": 7, "name": "tool_link"}
            return {"object": {"runtime_handle": 42, "kind": "link"}}

    robot = EditorRobot(FakeSession(), 7, ("joint_1",))  # type: ignore[arg-type]
    assert robot.get_link("tool_link") == 42


def test_editor_robot_get_link_rejects_empty_name() -> None:
    class FakeSession:
        def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            raise AssertionError("request should not be sent")

    robot = EditorRobot(FakeSession(), 7, ())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        robot.get_link("  ")


@pytest.mark.skipif(not EDITOR.is_file(), reason="SIM_PLATFORM_EDITOR is not configured")
def test_editor_hierarchy_reparent_and_robot_attachment() -> None:
    with EditorSession.launch(EDITOR) as editor:
        parent = editor.create_primitive("cube")
        child = editor.create_primitive("sphere")
        editor.set_transform(parent, translation=(10.0, 0.0, 0.0))
        editor.set_transform(child, translation=(1.0, 2.0, 3.0))

        editor.set_parent(child, parent)
        assert np.allclose(editor.get_transform(child)[0], (1.0, 2.0, 3.0))
        hierarchy = {int(item["entity"]): item for item in editor.scene_hierarchy()}
        assert hierarchy[child]["parent"] == parent
        assert hierarchy[parent]["expanded"] is True
        with pytest.raises(EditorRpcError):
            editor.set_parent(parent, child)

        editor.set_rigid_body(parent, dynamic=True, gravity=False)
        editor.profile_step((parent, child), render=False)
        editor.set_transform(parent, translation=(11.0, 0.0, 0.0))
        assert np.allclose(editor.get_transform(child)[0], (2.0, 2.0, 3.0))
        editor.set_parent(child, None)
        editor.set_transform(parent, translation=(12.0, 0.0, 0.0))
        assert np.allclose(editor.get_transform(child)[0], (2.0, 2.0, 3.0))

        camera_model = editor.load_asset(DATA / "triangle.obj")
        editor.set_transform(camera_model, translation=(4.0, 5.0, 6.0))
        editor.set_parent(camera_model, child)
        assert np.allclose(editor.get_transform(camera_model)[0], (4.0, 5.0, 6.0))
        editor.set_transform(child, translation=(3.0, 2.0, 3.0))
        assert np.allclose(editor.get_transform(camera_model)[0], (5.0, 5.0, 6.0))

        robot = editor.load_robot(DATA / "two_link.urdf")
        hierarchy = {int(item["entity"]): item for item in editor.scene_hierarchy()}
        tool_link = next(
            entity
            for entity, item in hierarchy.items()
            if item["name"] == "tool_link"
            and item["robot_link"]
            and hierarchy[int(item["parent"])]["parent"] == robot.handle
        )
        attachment = editor.load_asset(DATA / "two_link.urdf")
        attachment_world = editor.get_transform(attachment)
        editor.set_parent(attachment, tool_link)
        assert np.allclose(editor.get_transform(attachment)[0], attachment_world[0])
        hierarchy = {int(item["entity"]): item for item in editor.scene_hierarchy()}
        assert hierarchy[attachment]["parent"] == tool_link
        assert hierarchy[tool_link]["expanded"] is True
        with pytest.raises(EditorRpcError):
            editor.set_parent(tool_link, parent)

        robot.command_joint_positions([0.4], gain=20.0, velocity_limit=2.0)
        before = np.asarray(editor.get_transform(attachment)[0])
        for _ in range(100):
            editor.profile_step((), render=False)
        after = np.asarray(editor.get_transform(attachment)[0])
        assert not np.allclose(after, before)
