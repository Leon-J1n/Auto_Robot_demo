"""Capture the initial RGB-D frame of scene 3 (ur5e_table_sorting).

Run from the repository root with::

    python tests/test_camera_mujoco_scene3.py

The checked-in MuJoCo XML is the compiled scene-3 model produced by the
existing demo pipeline.  The source scene JSON supplies the camera pose and
resolution; the camera is injected into a temporary XML so this example does
not modify the scene artifact.
"""

from __future__ import annotations

import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sensors.camera_mujoco import MujocoCamera
from sensors.camera import save_camera_frame


SCENE_JSON = ROOT / "assets/scenes/3/ur5e_table_sorting.json"
COMPILED_XML = ROOT / "tests/output/commands.003/mujoco/scene.mjcf.xml"
OUTPUT_DIR = ROOT / "tests/output/scene3_camera"


def _look_at_quaternion(position: list[float], target: list[float]) -> list[float]:
    """Return MuJoCo's wxyz quaternion for a camera looking at target."""
    position = np.asarray(position, dtype=float)
    forward = np.asarray(target, dtype=float) - position
    forward /= np.linalg.norm(forward)
    world_up = np.array([0.0, 0.0, 1.0])
    right = np.cross(forward, world_up)
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    rotation = np.column_stack((right, up, -forward))
    quaternion = np.empty(4, dtype=float)
    mujoco.mju_mat2Quat(quaternion, rotation.reshape(-1))
    return quaternion.tolist()


def _capture_scene3_start() -> None:
    scene = json.loads(SCENE_JSON.read_text(encoding="utf-8"))
    assert scene["scene_id"] == 3
    assert scene["scene_name"] == "ur5e_table_sorting"
    camera_object = next(
        obj for obj in scene["objects"] if obj["object_name_in_scene"] == "OverheadCameraD435i"
    )
    components = {item["component_type"]: item["properties"] for item in camera_object["components"]}
    transform = components["Transform"]
    settings = components["Camera"]
    # The source scene stores a Unity-style camera orientation. MuJoCo cameras
    # look along local -Z, so define the intended view explicitly.
    camera_quaternion = _look_at_quaternion(transform["position"], [0.35, 0.0, 0.55])

    root = ET.parse(COMPILED_XML).getroot()
    worldbody = root.find("worldbody")
    if worldbody is None:
        raise RuntimeError("Compiled scene has no worldbody")
    ET.SubElement(
        worldbody,
        "camera",
        name="scene3_overhead_camera",
        pos=" ".join(map(str, transform["position"])),
        quat=" ".join(map(str, camera_quaternion)),
        fovy=str(settings["fov"]),
    )

    with tempfile.TemporaryDirectory(prefix="scene3_camera_") as tmp:
        xml_path = Path(tmp) / "scene3_with_camera.xml"
        ET.ElementTree(root).write(xml_path, encoding="utf-8", xml_declaration=True)
        model = mujoco.MjModel.from_xml_path(str(xml_path))
        data = mujoco.MjData(model)
        if root.get("model") != "3":
            raise RuntimeError(f"Expected compiled scene model '3', got {root.get('model')!r}")
        expected_bodies = {
            obj["object_name_in_scene"]
            for obj in scene["objects"]
            if obj["object_name_in_scene"] not in {
                "AmbientLight", "TopDirectionalLight", "WristCameraD435i",
                "OverheadCameraD435i", "Robotiq2F85Gripper",
            }
        }
        actual_bodies = {
            mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, index)
            for index in range(model.nbody)
        }
        missing_bodies = expected_bodies - actual_bodies
        if missing_bodies:
            raise RuntimeError(f"Compiled scene 3 is missing bodies: {sorted(missing_bodies)}")
        mujoco.mj_forward(model, data)
        camera = MujocoCamera(
            model,
            data,
            "scene3_overhead_camera",
            width=settings["resolution"][0],
            height=settings["resolution"][1],
        )
        try:
            frame = camera.get_current_frame()
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            saved = save_camera_frame(frame, OUTPUT_DIR, prefix="scene3_start")
            print(f"RGB: {saved['rgb']}")
            print(f"Depth: {saved['depth']}")
        finally:
            camera.close()


if __name__ == "__main__":
    _capture_scene3_start()
