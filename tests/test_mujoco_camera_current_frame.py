"""MuJoCo camera scene loading and metric-depth regression checks."""

import json
import tempfile
import unittest
from pathlib import Path

import mujoco
import numpy as np

from sensors.camera_mujoco import MujocoCamera
from sensors.camera_mujoco.scene import load_scene_with_cameras


class MujocoCameraTests(unittest.TestCase):
    def test_scene_camera_housings_are_textured_and_follow_mount(self):
        scene = Path(__file__).resolve().parents[1] / "assets/scenes/2/ur5e_table_apple.json"
        with tempfile.TemporaryDirectory() as directory:
            xml = Path(directory) / "scene.xml"
            xml.write_text(
                '<mujoco model="2"><worldbody><body name="robotiq_2f85_mount">'
                '<joint name="mount_slide" type="slide" axis="1 0 0"/>'
                '</body></worldbody></mujoco>', encoding="utf-8",
            )
            model, resolutions = load_scene_with_cameras(xml, scene)
        self.assertEqual(set(resolutions), {"global_camera", "wrist_camera"})
        self.assertEqual(model.ncam, 2)
        self.assertEqual(model.nmesh, 1)
        self.assertEqual(model.ntex, 1)
        geom_ids = [mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, name)
                    for name in ("global_camera_visual", "wrist_camera_visual")]
        self.assertTrue(all(index >= 0 for index in geom_ids))
        self.assertTrue(all(model.geom_contype[index] == 0 and model.geom_conaffinity[index] == 0
                            for index in geom_ids))
        self.assertTrue(all(model.geom_matid[index] >= 0 for index in geom_ids))

        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        overhead_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "global_camera_housing")
        wrist_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "wrist_camera_housing")
        overhead_start = data.xpos[overhead_id].copy()
        wrist_start = data.xpos[wrist_id].copy()
        np.testing.assert_allclose(overhead_start, [0.65, 0, 2.1])
        data.qpos[0] = 0.1
        mujoco.mj_forward(model, data)
        np.testing.assert_allclose(data.xpos[overhead_id], overhead_start)
        np.testing.assert_allclose(data.xpos[wrist_id] - wrist_start, [0.1, 0, 0], atol=1e-8)
        camera_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, "wrist_camera")
        np.testing.assert_allclose(data.cam_xpos[camera_id] - wrist_start, [0.1, 0, 0], atol=1e-8)

    def test_json_camera_renders_current_rgb_and_metric_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            xml = Path(directory) / "scene.xml"
            xml.write_text(
                '<mujoco model="1"><worldbody><geom type="plane" size="2 2 .1"/>'
                '<body name="robotiq_2f85_mount" pos="0 0 1"/></worldbody></mujoco>',
                encoding="utf-8",
            )
            scene = Path(directory) / "scene.json"
            scene.write_text(json.dumps({"objects": [
                {"object_id_in_scene": 1, "object_name_in_scene": "OverheadCameraD435i", "components": [
                    {"component_type": "Transform", "properties": {
                        "position": [0, 0, 2], "quaternion_xyzw": [0.70710678, 0.70710678, 0, 0], "parent": None}},
                    {"component_type": "Camera", "properties": {"resolution": [64, 64], "fov": 60}},
                ]},
                {"object_id_in_scene": 2, "object_name_in_scene": "Robotiq2F85Gripper", "components": []},
                {"object_id_in_scene": 3, "object_name_in_scene": "WristCameraD435i", "components": [
                    {"component_type": "Transform", "properties": {
                        "position": [0, 0, 0], "quaternion_xyzw": [0.70710678, 0.70710678, 0, 0], "parent": 2}},
                    {"component_type": "Camera", "properties": {"resolution": [64, 64], "fov": 60}},
                ]},
            ]}), encoding="utf-8")
            model, resolutions = load_scene_with_cameras(xml, scene)
            self.assertEqual(model.ncam, 2)
            self.assertEqual(resolutions["global_camera"], (64, 64))
            data = mujoco.MjData(model)
            mujoco.mj_forward(model, data)
            camera = MujocoCamera(model, data, "global_camera", width=64, height=64)
            try:
                frame = camera.get_current_frame()
                self.assertEqual(frame.rgb.shape, (64, 64, 3))
                self.assertEqual(frame.depth.shape, (64, 64))
                self.assertEqual(data.time, 0)
                self.assertTrue(np.isclose(frame.depth[32, 32], 2.0, atol=0.02))
            finally:
                camera.close()


if __name__ == "__main__":
    unittest.main()
