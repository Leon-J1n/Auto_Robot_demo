from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FEATURE_CONFIG = PROJECT_ROOT / "web" / "config" / "features.json"
FEATURE_KEY = re.compile(r"^[a-z][A-Za-z0-9]*(?:\.[a-z][A-Za-z0-9]*)*$")


def load_feature_config(path: Path = DEFAULT_FEATURE_CONFIG) -> dict[str, bool]:
    resolved = path.resolve()
    try:
        document: Any = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"feature config could not be read: {resolved}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"feature config is not valid JSON: {resolved}: {error}") from error
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("feature config must be an object with version 1")
    features = document.get("features")
    if not isinstance(features, dict) or not features:
        raise ValueError("feature config must contain a non-empty features object")
    invalid_keys = [key for key in features if not isinstance(key, str) or not FEATURE_KEY.fullmatch(key)]
    if invalid_keys:
        raise ValueError(f"feature config contains invalid keys: {invalid_keys}")
    invalid_values = [key for key, value in features.items() if type(value) is not bool]
    if invalid_values:
        raise ValueError(f"feature switches must be boolean: {invalid_values}")
    return dict(features)
