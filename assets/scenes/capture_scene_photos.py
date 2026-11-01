"""Render every final_models scene with the shared catalog camera.

Run from the repository root:
    python world_model/robotsim/assets/final_models/scenes/capture_scene_photos.py

The camera is intentionally kept in scene_camera_settings.json so the same
view can be reused for later scene captures.  Rendering directly at 128x128
keeps thumbnails sharp without requiring an image-processing dependency.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mujoco

HERE = Path(__file__).resolve().parent
DEFAULT_SETTINGS = HERE / "scene_camera_settings.json"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import view_scene  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def render_one(scene_id: int, settings_path: Path, width: int, height: int, output: Path) -> None:
    scene_dir = HERE / str(scene_id)
    scene_json = [p for p in scene_dir.glob("*.json")
                  if p.name != "scene_index.json" and not p.stem.endswith(("_file", "_files"))]
    if len(scene_json) != 1:
        raise RuntimeError(f"Scene {scene_id} must contain exactly one scene JSON")
    settings = read_json(settings_path)
    model, data, _ = view_scene.build(scene_json[0].resolve(), settings_path)
    camera = mujoco.MjvCamera()
    view = settings.get("view", {})
    camera.lookat[:] = view.get("lookat", [0.48, 0.0, 0.68])
    camera.distance = float(view.get("distance", 3.10))
    camera.azimuth = float(view.get("azimuth", 0.0))
    camera.elevation = float(view.get("elevation", -35.0))
    output.parent.mkdir(parents=True, exist_ok=True)
    with mujoco.Renderer(model, width=width, height=height) as renderer:
        renderer.update_scene(data, camera=camera)
        view_scene.write_png(output, renderer.render())


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture all final_models scene photos")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--scene-id", type=int, nargs="*", help="Only capture selected IDs")
    args = parser.parse_args()
    settings_path = args.settings.resolve()
    settings = read_json(settings_path)
    full_w, full_h = settings.get("full_resolution", [1280, 1280])
    thumb_w, thumb_h = settings.get("thumbnail_resolution", [128, 128])
    index = read_json(HERE / "scene_index.json")
    selected = set(args.scene_id) if args.scene_id else {int(row["id"]) for row in index}
    for row in index:
        scene_id = int(row["id"])
        if scene_id not in selected:
            continue
        scene_dir = HERE / str(scene_id)
        scene_json = [p for p in scene_dir.glob("*.json")
                      if p.name != "scene_index.json" and not p.stem.endswith(("_file", "_files"))]
        if len(scene_json) != 1:
            raise RuntimeError(f"Scene {scene_id} must contain exactly one scene JSON")
        stem = scene_json[0].stem
        render_one(scene_id, settings_path, int(full_w), int(full_h), scene_dir / f"{stem}.png")
        render_one(scene_id, settings_path, int(thumb_w), int(thumb_h), scene_dir / f"{stem}_thumbnail.png")
        print(f"captured scene {scene_id}: {stem}")


if __name__ == "__main__":
    main()
