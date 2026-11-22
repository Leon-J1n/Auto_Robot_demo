"""Resolve a compiled command's anchor to a world pose for the execution layer.

The compiler names an anchor per move (`grasp`, `interior`, `button_surface`) and the interaction
registry stores that anchor as an offset in the object's own frame. Without this bridge the
executor falls back to the object's origin, which is wrong in a way that looks like a planning
failure: a tray's origin sits on its floor, millimetres above the tabletop, so reaching it drives
the gripper into the table and the goal is reported as a collision.

Offsets are applied to the object's **live** pose rather than the registry's compile-time
`reference_pose`, because objects move during a turn -- a grasped block travels with the gripper.
The reference pose is only a fallback for an object the live scene cannot resolve.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .runtime.transforms import quaternion_to_matrix

LOGGER = logging.getLogger("agent_service.anchors")


def load_registry(source: Any) -> dict[str, Any]:
    """Accept a registry mapping, or a path to the JSON the compiler wrote."""
    if isinstance(source, Mapping):
        return dict(source)
    if source is None:
        return {}
    path = Path(str(source))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        LOGGER.warning("interaction registry could not be read from %s: %s", path, error)
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


class AnchorResolver:
    """Callable in the shape `execute_commands(resolve_target=...)` expects."""

    def __init__(self, registry: Any, runtime: Any):
        document = load_registry(registry)
        objects = document.get("objects")
        self.objects: dict[str, Any] = dict(objects) if isinstance(objects, Mapping) else {}
        self.runtime = runtime
        self._by_name: dict[str, str] = {}
        for key, value in self.objects.items():
            names = {key}
            if isinstance(value, Mapping):
                names.add(str(value.get("object_id") or ""))
                names.add(str(value.get("body_name") or ""))
                names.update(str(alias) for alias in (value.get("aliases") or ()))
            for name in names:
                if name:
                    self._by_name.setdefault(name.casefold(), key)

    def entry(self, target: str) -> Mapping[str, Any] | None:
        key = self._by_name.get(str(target).casefold())
        value = self.objects.get(key) if key else None
        return value if isinstance(value, Mapping) else None

    def __call__(self, target: str, anchor: str | None) -> dict[str, Any] | None:
        entry = self.entry(target)
        if entry is None:
            return None
        spatial = entry.get("spatial")
        if not isinstance(spatial, Mapping):
            return None
        anchors = spatial.get("anchors")
        anchors = dict(anchors) if isinstance(anchors, Mapping) else {}
        name = anchor or spatial.get("default_anchor")
        spec = anchors.get(str(name)) if name else None
        if not isinstance(spec, Mapping):
            # An object with no anchor of this name still has a usable origin; let the caller
            # fall back rather than failing the command here.
            return None

        base = self._base_pose(entry, spatial)
        if base is None:
            return None
        position, rotation = base
        local = np.asarray(spec.get("local_position", (0.0, 0.0, 0.0)), dtype=float)
        if local.shape != (3,):
            LOGGER.warning("anchor %r on %r has a malformed local_position", name, target)
            return None
        pose: dict[str, Any] = {"position": position + rotation @ local}
        orientation = spatial.get("tool_orientation")
        if isinstance(orientation, Mapping):
            # Passed through verbatim: the kinematics layer already understands the registry's
            # rpy-in-degrees form, and reinterpreting it here would risk a unit mismatch.
            pose["orientation"] = dict(orientation)
        return pose

    def _base_pose(self, entry: Mapping[str, Any],
                   spatial: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray] | None:
        body = str(entry.get("body_name") or entry.get("object_id") or "")
        if body and self.runtime is not None:
            try:
                live = self.runtime.object_pose(body)
            except Exception:  # noqa: BLE001 - fall back to the compile-time pose
                live = None
            if live is not None:
                return (np.asarray(live["position"], dtype=float),
                        np.asarray(live["rotation_matrix"], dtype=float))
        reference = spatial.get("reference_pose")
        if not isinstance(reference, Mapping):
            return None
        position = np.asarray(reference.get("position", (0.0, 0.0, 0.0)), dtype=float)
        quaternion = reference.get("quaternion_wxyz") or (1.0, 0.0, 0.0, 0.0)
        return position, quaternion_to_matrix(np.asarray(quaternion, dtype=float))
