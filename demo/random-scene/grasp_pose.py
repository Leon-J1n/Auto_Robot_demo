"""Convert object-frame grasp JSON data to robot-base-frame TCP poses."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import mujoco
import numpy as np


def _rotvec_to_matrix(rotvec: list[float]) -> np.ndarray:
    vector = np.asarray(rotvec, dtype=float)
    angle = float(np.linalg.norm(vector))
    if angle < 1e-12:
        return np.eye(3)
    axis = vector / angle
    x, y, z = axis
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) * math.cos(angle) + (1.0 - math.cos(angle)) * np.outer(axis, axis) + math.sin(angle) * skew


def _euler_xyz_to_matrix(euler_xyz: list[float]) -> np.ndarray:
    """Convert JSON rx/ry/rz Euler angles using Rz @ Ry @ Rx.

    With this convention, [pi, 0, yaw] keeps a downward-facing TCP vertical
    while yaw rotates its finger-opening direction around the object's Z axis.
    """
    rx, ry, rz = np.asarray(euler_xyz, dtype=float)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rxm = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=float)
    rym = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=float)
    rzm = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=float)
    return rzm @ rym @ rxm


def _pose_matrix(position: Any, rotation: np.ndarray) -> np.ndarray:
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = np.asarray(position, dtype=float)
    return result


def _find_object_body(model: mujoco.MjModel, keyword: str) -> int:
    needle = keyword.lower()
    candidates = []
    for body_id in range(1, model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body_id) or ""
        if name.lower() == needle or name.lower().endswith("_" + needle):
            candidates.append(body_id)
    if not candidates:
        raise KeyError(f"No object body matches {keyword!r}")
    if len(candidates) > 1:
        raise KeyError(f"Object keyword {keyword!r} is ambiguous: {candidates}")
    return candidates[0]


def load_grasp_json(object_dir: Path) -> dict[str, Any]:
    preferred = object_dir / "grasp_pose.json"
    json_files = [preferred] if preferred.is_file() else sorted(object_dir.glob("*.json"))
    if not json_files:
        # Some legacy checkouts keep the catalogue JSON under project/models.
        # Prefer the grasp_objects copy whenever it exists, but remain compatible
        # with that layout until the files are migrated.
        project_models = object_dir.parents[4] / "models" / object_dir.name
        json_files = sorted(project_models.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No JSON grasp file in {object_dir}")
    with json_files[0].open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    # Current schema: {"object_meta": {...}, "poses": [[x,y,z,rx,ry,rz], ...]}.
    # Keep a read-only compatibility path for older files while users migrate.
    poses = payload.get("poses")
    if poses is not None:
        grasps = []
        for index, values in enumerate(poses):
            if not isinstance(values, list) or len(values) != 6:
                raise ValueError(f"Invalid grasp pose at index {index} in {json_files[0]}")
            grasps.append({"tcp_relative_pos": values[:3], "tcp_relative_euler_xyz": values[3:]})
    else:
        grasps = payload.get("grasps")
    if grasps is None:
        legacy = payload.get("grasp")
        grasps = [legacy] if legacy else []
    if not grasps:
        raise ValueError(f"No grasp candidates in {json_files[0]}")
    payload["grasps"] = grasps
    payload["grasp_count"] = len(grasps)
    payload["pose_count"] = len(grasps)
    if payload["grasp_count"] != len(grasps):
        raise ValueError(f"grasp_count does not match grasps in {json_files[0]}")
    return payload


def object_to_base_grasp(
    model: mujoco.MjModel,
    data: mujoco.MjData,
    object_keyword: str,
    object_dir: Path,
    *,
    grasp_index: int = 0,
    base_body_name: str = "base",
    tcp_site_name: str = "robotiq_2f85_pinch",
) -> dict[str, Any]:
    """Return a grasp target whose position/orientation are expressed in base frame."""
    payload = load_grasp_json(object_dir)
    grasp = payload["grasps"][grasp_index]
    body_id = _find_object_body(model, object_keyword)
    frame_name = (mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body_id) or "") + "_object_frame"
    frame_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, frame_name)
    if frame_id < 0:
        raise KeyError(f"Scene has no object frame site {frame_name!r}")
    tcp_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, tcp_site_name)
    if tcp_id < 0:
        raise KeyError(f"Scene has no TCP site {tcp_site_name!r}")
    base_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, base_body_name)
    if base_id < 0:
        raise KeyError(f"Scene has no robot base body {base_body_name!r}")

    t_world_frame = _pose_matrix(data.site_xpos[frame_id], data.site_xmat[frame_id].reshape(3, 3))
    t_world_base = _pose_matrix(data.xpos[base_id], data.xmat[base_id].reshape(3, 3))
    angles = grasp.get("tcp_relative_euler_xyz")
    rotation = _euler_xyz_to_matrix(angles) if angles is not None else _rotvec_to_matrix(grasp["tcp_relative_rotvec"])
    t_frame_tcp = _pose_matrix(grasp["tcp_relative_pos"], rotation)
    t_base_tcp = np.linalg.inv(t_world_base) @ t_world_frame @ t_frame_tcp
    rotation = t_base_tcp[:3, :3]
    quaternion = np.empty(4)
    mujoco.mju_mat2Quat(quaternion, rotation.reshape(-1))
    return {
        "object_name": payload.get("object_meta", {}).get("object_name", object_keyword),
        "grasp_index": grasp_index,
        "position": t_base_tcp[:3, 3].tolist(),
        "quaternion_wxyz": quaternion.tolist(),
        "rotation_matrix": rotation.tolist(),
        "source_frame": frame_name,
        "target_frame": base_body_name,
    }


def convert_scene_grasp(scene_path: str | Path, object_keyword: str, object_dir: str | Path, grasp_index: int = 0) -> dict[str, Any]:
    model = mujoco.MjModel.from_xml_path(str(Path(scene_path).resolve()))
    data = mujoco.MjData(model)
    key = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, "home")
    if key >= 0:
        mujoco.mj_resetDataKeyframe(model, data, key)
    mujoco.mj_forward(model, data)
    return object_to_base_grasp(model, data, object_keyword, Path(object_dir).resolve(), grasp_index=grasp_index)
