"""Attach scene-JSON cameras and visual housings to a MuJoCo model in memory."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import mujoco
import numpy as np


CAMERA_NAMES = {"OverheadCameraD435i": "global_camera", "WristCameraD435i": "wrist_camera"}
ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets"


def _camera_visual_asset(obj: dict) -> tuple[Path, Path | None] | None:
    components = {item["component_type"]: item["properties"] for item in obj["components"]}
    ref = components.get("MetadataRef", {})
    match = re.fullmatch(r"\$LIBRARY_SERVER/(\d+)", str(ref.get("path", "")))
    if ref.get("type") != "robot" or match is None:
        return None
    directory = ASSET_ROOT / "robots" / match.group(1)
    manifests = list(directory.glob("*_files.json"))
    if len(manifests) != 1:
        raise FileNotFoundError(f"Expected one camera asset manifest in {directory}")
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    mesh = directory / manifest["path"]
    if not mesh.is_file():
        raise FileNotFoundError(f"Camera visual mesh not found: {mesh}")
    texture = next((directory / name for name in manifest.get("reference", [])
                    if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg"}), None)
    if texture is not None and not texture.is_file():
        raise FileNotFoundError(f"Camera visual texture not found: {texture}")
    return mesh, texture


def find_camera_scene_json(mjcf_path: Path, scene_json_path: str | Path | None = None) -> Path | None:
    if scene_json_path is not None:
        path = Path(scene_json_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Scene JSON not found: {path}")
        return path
    scene_id = ET.parse(mjcf_path).getroot().get("model")
    if not scene_id or not scene_id.isdecimal():
        return None
    directory = Path(__file__).resolve().parents[2] / "assets" / "scenes" / scene_id
    candidates = sorted(directory.glob("ur5e_*.json"))
    candidates = [p for p in candidates if not p.stem.endswith(("_file", "_files"))]
    if len(candidates) > 1:
        raise ValueError(f"Multiple scene JSON files for scene {scene_id}; provide scene_json_path.")
    return candidates[0] if candidates else None


def load_scene_with_cameras(
    mjcf_path: str | Path, scene_json_path: str | Path | None = None,
) -> tuple[mujoco.MjModel, dict[str, tuple[int, int]]]:
    """Compile an MJCF with global/wrist RGB-D cameras and their visual meshes."""
    mjcf_path = Path(mjcf_path).expanduser().resolve()
    spec = mujoco.MjSpec.from_file(str(mjcf_path))
    camera_json = find_camera_scene_json(mjcf_path, scene_json_path)
    resolutions: dict[str, tuple[int, int]] = {}
    if camera_json is None:
        return spec.compile(), resolutions
    scene = json.loads(camera_json.read_text(encoding="utf-8"))
    objects = {obj["object_id_in_scene"]: obj for obj in scene["objects"]}
    existing = {camera.name for camera in spec.cameras}
    visual_assets = {}
    for obj in scene["objects"]:
        camera_name = CAMERA_NAMES.get(obj.get("object_name_in_scene"))
        if camera_name is None:
            continue
        components = {item["component_type"]: item["properties"] for item in obj["components"]}
        transform = components["Transform"]
        settings = components["Camera"]
        resolution = tuple(map(int, settings["resolution"]))
        resolutions[camera_name] = resolution
        parent_id = transform.get("parent")
        if parent_id is None:
            body = spec.worldbody
        else:
            parent_object = objects[parent_id]["object_name_in_scene"]
            if parent_object != "Robotiq2F85Gripper":
                raise ValueError(f"Unsupported camera parent object: {parent_object}")
            body = spec.body("robotiq_2f85_mount")
            if body is None:
                raise ValueError("Wrist camera parent body robotiq_2f85_mount is absent from MJCF.")
        xyzw = np.asarray(transform["quaternion_xyzw"], dtype=float)
        if xyzw.shape != (4,) or not np.isfinite(xyzw).all() or np.linalg.norm(xyzw) == 0:
            raise ValueError(f"Invalid camera quaternion for {camera_name}.")
        scene_quat = xyzw[[3, 0, 1, 2]] / np.linalg.norm(xyzw)
        visual_asset = _camera_visual_asset(obj)
        if visual_asset is not None and spec.body(f"{camera_name}_housing") is None:
            if visual_asset not in visual_assets:
                mesh, texture = visual_asset
                suffix = str(len(visual_assets))
                mesh_name = f"camera_visual_mesh_{suffix}"
                material_name = f"camera_visual_material_{suffix}"
                spec.add_mesh(name=mesh_name, file=str(mesh))
                if texture is not None:
                    texture_name = f"camera_visual_texture_{suffix}"
                    spec.add_texture(name=texture_name, type=mujoco.mjtTexture.mjTEXTURE_2D,
                                     file=str(texture))
                    spec.add_material(name=material_name, textures=[texture_name])
                else:
                    spec.add_material(name=material_name, rgba=[0.55, 0.58, 0.62, 1])
                visual_assets[visual_asset] = (mesh_name, material_name)
            mesh_name, material_name = visual_assets[visual_asset]
            housing = body.add_body(name=f"{camera_name}_housing",
                                    pos=transform["position"], quat=scene_quat)
            housing.add_geom(name=f"{camera_name}_visual", type=mujoco.mjtGeom.mjGEOM_MESH,
                             meshname=mesh_name, material=material_name, group=1,
                             contype=0, conaffinity=0)
        if camera_name in existing:
            continue
        # Scene cameras use local +Z forward; MuJoCo cameras use local -Z.
        mujoco_quat = np.empty(4)
        mujoco.mju_mulQuat(mujoco_quat, scene_quat, np.array([0.0, 1.0, 0.0, 0.0]))
        body.add_camera(
            name=camera_name, pos=transform["position"], quat=mujoco_quat,
            fovy=float(settings["fov"]),
        )
    return spec.compile(), resolutions
