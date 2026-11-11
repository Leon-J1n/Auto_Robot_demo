"""Render scene 3's overhead RGB view and the current AABB crops.

Run from the repository root with:
    .venv/Scripts/python.exe tests/visualize_scene3_aabb_crops.py
"""

import json
from pathlib import Path
import sys

import mujoco
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo.interaction.vlm_grasp import _target_crop
from execution.resources import component
from sensors.camera_mujoco import MujocoCamera
from sensors.camera_mujoco.scene import load_scene_with_cameras


SCENE_PATH = ROOT / "assets/scenes/3/ur5_table_sorting.json"
MODEL_PATH = ROOT / "tests/output/commands003_vlm_place/mujoco/scene.mjcf.xml"
OUTPUT_DIR = ROOT / "tests/output/scene3_aabb_crops"
TARGETS = ((7, "Apple", "#e53935"), (13, "Tray", "#177dcb"),
           (12, "Basket", "#14a46b"), (10, "Screwdriver", "#d58b00"))


def main():
    scene = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    model, resolutions = load_scene_with_cameras(MODEL_PATH, SCENE_PATH)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    camera = MujocoCamera(model, data, "global_camera", width=resolutions["global_camera"][0],
                          height=resolutions["global_camera"][1])
    try:
        frame = camera.get_current_frame()
    finally:
        camera.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source = Image.fromarray(frame.rgb)
    source.save(OUTPUT_DIR / "overhead_original.png")
    overview = source.copy()
    draw = ImageDraw.Draw(overview)
    font = ImageFont.load_default()
    report = []
    for target_id, label, color in TARGETS:
        obj = next(item for item in scene["objects"] if item["object_id_in_scene"] == target_id)
        ref = component(obj, "MetadataRef")
        model_id = int(ref["path"].rsplit("/", 1)[-1])
        metadata_path = next((ROOT / "assets/models" / str(model_id)).glob("*.json"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        body_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, label)
        if body_id < 0:
            raise ValueError(f"Missing MuJoCo body: {label}")
        position = data.xpos[body_id].copy()
        quaternion = data.xquat[body_id][[1, 2, 3, 0]].copy()
        authored = np.asarray(component(obj, "Transform")["position"], dtype=float)
        if not np.allclose(position, authored, atol=1e-4):
            raise ValueError(f"Compiled model position differs from scene JSON for {label}")
        cropped, (left, top) = _target_crop(frame, {
            "position": position, "quaternion_xyzw": quaternion}, metadata)
        height, width = cropped.rgb.shape[:2]
        Image.fromarray(cropped.rgb).save(OUTPUT_DIR / f"{target_id}_{label.lower()}_crop.png")
        draw.rectangle((left, top, left + width - 1, top + height - 1), outline=color, width=3)
        text = f"{label}  ID {target_id}"
        label_bounds = draw.textbbox((0, 0), text, font=font)
        text_width = label_bounds[2] - label_bounds[0]
        text_height = label_bounds[3] - label_bounds[1]
        label_y = max(0, top - text_height - 7)
        draw.rectangle((left, label_y, left + text_width + 8, label_y + text_height + 6), fill=color)
        draw.text((left + 4, label_y + 3), text, fill="white", font=font)
        report.append({"scene_id": 3, "target_id": target_id, "name": label,
                       "crop_xywh": [left, top, width, height],
                       "model_id": model_id, "camera": "global_camera"})

    overview.save(OUTPUT_DIR / "overhead_aabb_boxes.png")
    (OUTPUT_DIR / "crop_boxes.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    canvas = Image.new("RGB", (1400, 610), "#f3f5f5")
    canvas_draw = ImageDraw.Draw(canvas)
    try:
        heading_font = ImageFont.truetype("arial.ttf", 22)
        detail_font = ImageFont.truetype("arial.ttf", 15)
    except OSError:
        heading_font = detail_font = font
    canvas_draw.text((24, 18), "Scene 3 / overhead camera / projected AABB crops",
                     fill="#202528", font=heading_font)
    canvas.paste(overview, (24, 75))
    for index, (target_id, label, color) in enumerate(TARGETS):
        x = 690 + (index % 2) * 350
        y = 75 + (index // 2) * 265
        item = report[index]
        canvas_draw.text((x, y), f"{label}  ID {target_id}  "
                         f"{item['crop_xywh'][2]}x{item['crop_xywh'][3]} px",
                         fill=color, font=detail_font)
        crop = Image.open(OUTPUT_DIR / f"{target_id}_{label.lower()}_crop.png")
        scaled = ImageOps.contain(crop, (320, 225), Image.Resampling.NEAREST)
        canvas.paste(scaled, (x, y + 27))
        canvas_draw.rectangle((x - 1, y + 26, x + scaled.width,
                               y + 27 + scaled.height), outline=color, width=2)
    canvas.save(OUTPUT_DIR / "aabb_crop_comparison.png")
    for item in report:
        print(f"{item['name']} ID {item['target_id']}: xywh={item['crop_xywh']}")
    print(OUTPUT_DIR)


if __name__ == "__main__":
    main()
