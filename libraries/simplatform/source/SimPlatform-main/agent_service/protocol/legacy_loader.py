from __future__ import annotations

import json
from pathlib import Path

from .commands import CommandDocument, ExecutionOptions
from .fingerprint import scene_sha256


def load_legacy_command_document(path: str | Path) -> CommandDocument:
    command_path = Path(path).expanduser().resolve()
    raw = json.loads(command_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("legacy command document must be a JSON object")
    registry_value = raw.get("registry")
    if not isinstance(registry_value, str):
        raise ValueError("legacy command document requires registry")
    registry_path = Path(registry_value)
    if not registry_path.is_absolute():
        registry_path = (command_path.parent / registry_path).resolve()
    registry_raw = json.loads(registry_path.read_text(encoding="utf-8"))
    scene_value = raw.get("scene", registry_raw.get("scene"))
    if not isinstance(scene_value, str):
        raise ValueError("legacy command or registry requires scene")
    scene_path = Path(scene_value)
    if not scene_path.is_absolute():
        base = command_path.parent if "scene" in raw else registry_path.parent
        scene_path = (base / scene_path).resolve()
    commands = []
    for index, item in enumerate(raw.get("commands", []), 1):
        if not isinstance(item, dict):
            raise ValueError(f"legacy command {index} must be an object")
        commands.append({
            "command_id": item.get("command_id", f"command-{index:03d}"),
            "source_skill_step_id": item.get("source_skill_step_id"),
            "skill_name": item.get("skill_name"),
            "parameters": item.get("parameters"),
        })
    runtime = {key: value for key, value in raw.get("runtime", {}).items() if key in ExecutionOptions.model_fields}
    return CommandDocument.model_validate({
        "schema_version": "1.0",
        "robot": raw.get("robot", "ur5e"),
        "scene": str(scene_path),
        "registry": str(registry_path),
        "scene_fingerprint": raw.get("scene_fingerprint", registry_raw.get("scene_fingerprint", scene_sha256(scene_path))),
        "runtime": runtime,
        "request_defaults": raw.get("request_defaults", registry_raw.get("move_defaults", {})),
        "commands": commands,
    })
