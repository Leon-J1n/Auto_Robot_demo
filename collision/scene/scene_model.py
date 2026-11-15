"""Canonical scene types shared by collision backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CollisionScene:
    """Validated, backend-neutral scene document."""

    source: Path
    document: Mapping[str, Any]

    @property
    def objects(self) -> list[Mapping[str, Any]]:
        return list(self.document["objects"])


def load_scene(path: str | Path) -> CollisionScene:
    import json

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Collision scene not found: {source}")
    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("objects"), list):
        raise ValueError("Collision scene must be an object with an objects array")
    return CollisionScene(source, document)
