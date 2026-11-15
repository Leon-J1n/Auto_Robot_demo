"""Resolve collision assets from the OLS data tree."""
from __future__ import annotations

from pathlib import Path


class AssetResolver:
    def __init__(self, data_root: str | Path):
        self.root = Path(data_root).expanduser().resolve()

    def robot(self, robot_id: int | str, relative: str | None = None) -> Path:
        return self._resolve("robots", robot_id, relative, ("*.urdf", "*.obj", "*.stl"))

    def model(self, model_id: int | str, relative: str | None = None) -> Path:
        return self._resolve("models", model_id, relative, ("*.obj", "*.stl", "*.urdf"))

    def _resolve(self, kind, ident, relative, patterns):
        directory = self.root / kind / str(ident)
        if relative:
            candidate = (directory / relative).resolve()
            if candidate.is_file() and directory in candidate.parents:
                return candidate
        for pattern in patterns:
            matches = sorted(directory.glob(pattern))
            if matches:
                return matches[0]
        raise FileNotFoundError(f"No {kind[:-1]} asset found in {directory}")
