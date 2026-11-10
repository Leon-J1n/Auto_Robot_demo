"""Automatic registration of authoring scenes in the execution catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_BACKEND_RESOURCES = {
    "mujoco": {"format": "generate_from_scene", "options": {}}
}


def _scene_identity(path: Path) -> tuple[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Scene document must be an object: {path}")
    scene_id = data.get("scene_id")
    version = data.get("scene_version")
    if scene_id is None or isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"Scene must define scene_id and integer scene_version: {path}")
    return str(scene_id), version


def register_scenes(
    catalog_path: str | Path,
    scenes_root: str | Path,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """Discover scene JSON files and register them by scene identity.

    Existing backend resources and runtime_config entries are preserved. A
    scene's path is always stored relative to the catalog directory.
    """
    catalog_path = Path(catalog_path).resolve()
    scenes_root = Path(scenes_root).resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else {
        "asset_root": ".", "scenes": {}
    }
    catalog.setdefault("asset_root", ".")
    scenes = catalog.setdefault("scenes", {})
    discovered = 0
    for path in sorted(scenes_root.rglob("*.json")):
        if path.name.endswith("_files.json") or path.name.endswith("_file.json"):
            continue
        try:
            scene_id, version = _scene_identity(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        relative = path.relative_to(catalog_path.parent).as_posix()
        previous = scenes.get(scene_id, {})
        entry = {
            "version": version,
            "path": relative,
            "backend_resources": previous.get("backend_resources", DEFAULT_BACKEND_RESOURCES),
        }
        if "runtime_config" in previous:
            entry["runtime_config"] = previous["runtime_config"]
        scenes[scene_id] = entry
        discovered += 1
    if write:
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalog["_registration"] = {"discovered": discovered, "scenes_root": str(scenes_root)}
    return catalog


def register_scene(
    catalog_path: str | Path,
    scenes_root: str | Path,
    scene_id: str | int,
    scene_version: int,
) -> dict[str, Any]:
    """Register only the scene identity requested by one commands document."""
    catalog_path = Path(catalog_path).resolve()
    scenes_root = Path(scenes_root).resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else {
        "asset_root": ".", "scenes": {}
    }
    catalog.setdefault("asset_root", ".")
    catalog.setdefault("scenes", {})
    wanted_id, wanted_version = str(scene_id), int(scene_version)
    candidates = sorted(scenes_root.joinpath(wanted_id).glob("*.json"))
    if not candidates:
        candidates = sorted(scenes_root.rglob("*.json"))
    previous_path = catalog["scenes"].get(wanted_id, {}).get("path")
    if previous_path:
        preferred = (catalog_path.parent / previous_path).resolve()
        if preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)
    for path in candidates:
        if path.name.endswith("_files.json") or path.name.endswith("_file.json"):
            continue
        try:
            found_id, found_version = _scene_identity(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if found_id != wanted_id or found_version != wanted_version:
            continue
        previous = catalog["scenes"].get(wanted_id, {})
        catalog["scenes"][wanted_id] = {
            "version": wanted_version,
            "path": path.relative_to(catalog_path.parent).as_posix(),
            "backend_resources": previous.get("backend_resources", DEFAULT_BACKEND_RESOURCES),
            **({"runtime_config": previous["runtime_config"]} if "runtime_config" in previous else {}),
        }
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return catalog
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("assets/catalog.json"))
    parser.add_argument("--scenes", type=Path, default=Path("assets/scenes"))
    args = parser.parse_args()
    result = register_scenes(args.catalog, args.scenes)
    print(f"registered {result['_registration']['discovered']} scenes in {args.catalog}")


if __name__ == "__main__":
    main()
