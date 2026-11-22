"""Scene fingerprints for scenes that are composed rather than written to file.

The source stack hashed the generated ``scene.xml``.  We no longer write one
scene file per task, so the fingerprint is instead a SHA-256 over a canonical,
sorted JSON serialization of the base scene's content digest, the ordered object
placements and the robot id.  The field name and the ``^[0-9a-f]{64}$`` shape are
unchanged, so every protocol validator keeps working.  See README.md.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..protocol import scene_sha256
from ..scene import ScenePlan


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of a canonical JSON encoding: sorted keys, no incidental spacing."""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def scene_plan_fingerprint(plan: ScenePlan, *, base_scene_digest: str | None = None) -> str:
    """Fingerprint a scene plan; identical plans always give identical digests."""
    digest = base_scene_digest if base_scene_digest is not None else base_scene_sha256(plan.base_scene)
    return canonical_digest(plan.canonical_payload(base_scene_digest=digest))


def base_scene_sha256(path: str | Path) -> str:
    """Content digest of the base ``.simstate``, or a stable stand-in if absent.

    A missing base scene must not silently produce the same fingerprint as a
    present one, so the placeholder is derived from the path itself.  Callers
    that require the real file should check for it before compiling.
    """
    scene = Path(path).expanduser()
    if scene.is_file():
        return scene_sha256(scene)
    return canonical_digest({"missing_base_scene": str(scene)})
