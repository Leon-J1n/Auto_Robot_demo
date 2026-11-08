"""Write backend execution samples in the WebEditor TRAJ v2 format."""
from __future__ import annotations

import json
import struct
import argparse
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

MAGIC = 0x5452414A
WORLD_ID = 0xFFFFFFFF


def _component(obj: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    for entry in obj.get("components", ()):
        if entry.get("component_type") == kind:
            return entry.get("properties", {})
    return {}


def _pose_default(obj: Mapping[str, Any], robot_id: int, gripper_id: int | None,
                  initial_joints: Iterable[float]) -> list[float]:
    if int(obj["object_id_in_scene"]) == robot_id:
        return [*map(float, initial_joints), 0.0]
    if gripper_id is not None and int(obj["object_id_in_scene"]) == gripper_id:
        driver = _component(obj, "RobotDriver")
        initial = driver.get("initial_joint_positions_rad", {})
        joint = driver.get("joint_sequence", [None])[0]
        return [float(initial.get(joint, 0.0)), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    transform = _component(obj, "Transform")
    return [*map(float, transform.get("position", (0.0, 0.0, 0.0))),
            *map(float, transform.get("quaternion_xyzw", (0.0, 0.0, 0.0, 1.0)))]


def _frame_pose(state: Mapping[str, Any], name: str, robot_id: int, gripper_id: int | None,
                object_ids: Mapping[str, int], *, gripper_open_width: float | None = None,
                gripper_closed_width: float | None = None) -> list[float] | None:
    if object_ids.get(name) == robot_id:
        joints = state.get("joints") or state.get("joint_positions_rad")
        if joints is None:
            return None
        gripper = state.get("gripper") or {}
        value = gripper.get("grasp_value")
        if value is None:
            width = gripper.get("width")
            substep = state.get("substep")
            if (substep in {"grasp", "release"} and
                    isinstance(width, (int, float)) and np.isfinite(width) and
                    gripper_open_width is not None and gripper_closed_width is not None and
                    gripper_open_width > gripper_closed_width):
                # The physical controller may stop above zero opening when an
                # object is held. Normalize the observed stroke to TRAJ's
                # canonical 0.0=open, 0.72=closed range.
                value = float(np.clip(
                    (gripper_open_width - width) /
                    (gripper_open_width - gripper_closed_width) * 0.72,
                    0.0, 0.72))
            elif gripper.get("holding") or gripper.get("object_present"):
                value = 0.72
            else:
                value = 0.0
        return [*map(float, joints), float(value)]
    if gripper_id is not None and object_ids.get(name) == gripper_id:
        gripper = state.get("gripper") or {}
        width = gripper.get("width")
        if not isinstance(width, (int, float)) or not np.isfinite(width):
            return None
        angle = float(np.clip(0.8 * (1.0 - width / 0.085), 0.0, 0.8))
        return [angle, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    item = (state.get("objects") or {}).get(name)
    if not item:
        return None
    return [*map(float, item.get("position", (0.0, 0.0, 0.0))),
            *map(float, item.get("quaternion_xyzw", (0.0, 0.0, 0.0, 1.0)))]


def write_traj(path: str | Path, scene: Mapping[str, Any], frames: Iterable[Mapping[str, Any]],
               *, initial_joints: Iterable[float], fps: int = 60) -> Path:
    """Serialize execution frames so WebEditor's ``parseTrajectory`` can read them."""
    path = Path(path)
    objects = list(scene.get("objects", ()))
    robots = [obj for obj in objects if _component(obj, "RobotModelRef") or
              len(_component(obj, "RobotDriver").get("joint_sequence", ())) == 6]
    if len(robots) != 1:
        raise ValueError("TRAJ export requires exactly one robot object")
    robot_id = int(robots[0]["object_id_in_scene"])
    grippers = [obj for obj in objects if _component(obj, "RobotAttachment") or
                len(_component(obj, "RobotDriver").get("joint_sequence", ())) == 1]
    gripper_id = int(grippers[0]["object_id_in_scene"]) if grippers else None
    object_ids = {str(obj["object_name_in_scene"]): int(obj["object_id_in_scene"]) for obj in objects}
    rows = list(frames)
    rows.sort(key=lambda row: int(row.get("frame_index", 0)))
    widths = [float((row.get("gripper") or {}).get("width"))
              for row in rows
              if isinstance((row.get("gripper") or {}).get("width"), (int, float)) and
              np.isfinite(float((row.get("gripper") or {}).get("width")))]
    gripper_open_width = max(widths) if widths else None
    gripper_closed_width = min(widths) if widths else None
    total_frames = max((int(row.get("frame_index", 0)) for row in rows), default=-1) + 1
    if not total_frames:
        total_frames = 1
    defaults = {int(obj["object_id_in_scene"]): _pose_default(obj, robot_id, gripper_id, initial_joints) for obj in objects}
    pose_rows: dict[int, list[list[float]]] = {oid: [list(defaults[oid]) for _ in range(total_frames)] for oid in defaults}
    # Backend samples contain world-space object poses. Do not synthesize
    # attachment events from gripper state because the WebEditor would then
    # reinterpret those world poses as parent-local poses.
    attach_events: list[tuple[int, int, int, int]] = []
    for row in rows:
        frame = int(row.get("frame_index", 0))
        if frame < 0 or frame >= total_frames:
            continue
        state = row
        for name, oid in object_ids.items():
            pose = _frame_pose(state, name, robot_id, gripper_id, object_ids,
                               gripper_open_width=gripper_open_width,
                               gripper_closed_width=gripper_closed_width)
            if pose is not None:
                pose_rows[oid][frame] = pose
    for oid, values in pose_rows.items():
        for frame in range(1, len(values)):
            if values[frame] == defaults[oid] and values[frame - 1] != defaults[oid]:
                values[frame] = values[frame - 1]
    index_size = sum(12 + 7 * 4 + (16 if pose_rows[oid] else 0) for oid in defaults)
    attach_offset = 32 + index_size
    data_offset = attach_offset
    index_parts: list[bytes] = []
    data_parts: list[bytes] = []
    for obj in objects:
        oid = int(obj["object_id_in_scene"])
        values = pose_rows[oid]
        parent = _component(obj, "Transform").get("parent")
        parent_id = WORLD_ID if parent in (None, "", WORLD_ID) else int(parent)
        index_parts.append(struct.pack("<IIHH", oid, parent_id, 7, 1 if values else 0))
        index_parts.append(struct.pack("<7f", *defaults[oid]))
        if values:
            raw = np.asarray(values, dtype="<f4").tobytes()
            index_parts.append(struct.pack("<IIII", 0, len(values), data_offset, 0))
            data_parts.append(raw)
            data_offset += len(raw)
    header = struct.pack("<8I", MAGIC, 2, int(fps), total_frames, len(objects), len(attach_events), 32, attach_offset)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"".join(index_parts) + b"".join(struct.pack("<IIII", *event) for event in attach_events) + b"".join(data_parts))
    return path


def write_traj_from_jsonl(path: str | Path, scene: Mapping[str, Any], frames_path: str | Path,
                          *, initial_joints: Iterable[float], fps: int = 60) -> Path:
    source = Path(frames_path)
    rows = (json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()) if source.exists() else ()
    return write_traj(path, scene, rows, initial_joints=initial_joints, fps=fps)


def validate_traj(path: str | Path) -> dict[str, Any]:
    """Validate a TRAJ v2 file and return parsed summary information."""
    raw = Path(path).read_bytes()
    errors: list[str] = []
    if len(raw) < 32:
        raise ValueError("file is shorter than the 32-byte TRAJ header")
    magic, version, fps, total_frames, object_count, attach_count, index_offset, attach_offset = struct.unpack_from("<8I", raw)
    if magic != MAGIC:
        errors.append(f"invalid magic 0x{magic:08X}")
    if version != 2:
        errors.append(f"unsupported version {version}")
    if fps == 0 or total_frames == 0:
        errors.append("fps and total_frames must be positive")
    if index_offset != 32 or attach_offset < index_offset or attach_offset > len(raw):
        errors.append("invalid index/attach offsets")
    cursor = index_offset
    objects = []
    for index in range(object_count):
        if cursor + 12 > len(raw):
            errors.append(f"object {index}: truncated object header")
            break
        object_id, parent_id, pose_dim, segment_count = struct.unpack_from("<IIHH", raw, cursor)
        cursor += 12
        if pose_dim == 0 or cursor + pose_dim * 4 > len(raw):
            errors.append(f"object {object_id}: invalid pose dimension")
            break
        default = struct.unpack_from(f"<{pose_dim}f", raw, cursor)
        cursor += pose_dim * 4
        segments = []
        for segment_index in range(segment_count):
            if cursor + 16 > len(raw):
                errors.append(f"object {object_id}: truncated segment table")
                break
            start, count, data_offset, step = struct.unpack_from("<IIII", raw, cursor)
            cursor += 16
            byte_count = count * pose_dim * 4
            if count == 0 or start + count > total_frames:
                errors.append(f"object {object_id} segment {segment_index}: frame range is invalid")
            if data_offset % 4 or data_offset < 0 or data_offset + byte_count > len(raw):
                errors.append(f"object {object_id} segment {segment_index}: data range is invalid")
            elif byte_count:
                values = struct.unpack_from(f"<{count * pose_dim}f", raw, data_offset)
                if not np.isfinite(values).all():
                    errors.append(f"object {object_id} segment {segment_index}: contains non-finite values")
            segments.append({"start_frame": start, "frame_count": count, "step_count": step})
        objects.append({"object_id": object_id, "parent_id": parent_id, "pose_dim": pose_dim,
                        "default_pose": list(default), "segments": segments})
    expected_data_start = attach_offset + attach_count * 16
    if expected_data_start > len(raw):
        errors.append("attachment table extends past end of file")
    attachments = []
    for index in range(attach_count):
        offset = attach_offset + index * 16
        if offset + 16 > len(raw):
            break
        frame, child, parent, _reserved = struct.unpack_from("<IIII", raw, offset)
        if frame >= total_frames:
            errors.append(f"attachment {index}: frame is outside total_frames")
        attachments.append({"frame": frame, "child_id": child, "parent_id": parent})
    if cursor > attach_offset:
        errors.append("index region overlaps attachment region")
    if errors:
        raise ValueError("TRAJ validation failed: " + "; ".join(errors))
    return {"version": version, "fps": fps, "total_frames": total_frames,
            "object_count": object_count, "attach_count": attach_count,
            "file_size": len(raw), "objects": objects, "attachments": attachments}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate a WebEditor TRAJ v2 file")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        summary = validate_traj(args.path)
    except (OSError, ValueError) as exc:
        print(f"INVALID: {exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
