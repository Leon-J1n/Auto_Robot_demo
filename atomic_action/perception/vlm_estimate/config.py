"""Load VLM connection settings independently of the process working directory."""

import json
from pathlib import Path


_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
VLM_CONFIG_PATH = _CONFIG_DIR / "vlm.json"
VLM_EXAMPLE_PATH = _CONFIG_DIR / "vlm.example.json"
_KEYS = {"api_key", "base_url", "model", "timeout_seconds", "max_retries"}


def load_vlm_config():
    path = VLM_CONFIG_PATH if VLM_CONFIG_PATH.is_file() else VLM_EXAMPLE_PATH
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or set(settings) != _KEYS:
        raise ValueError(f"Invalid VLM config keys in {path}; expected {sorted(_KEYS)}")
    if not isinstance(settings["api_key"], str):
        raise ValueError("VLM api_key must be a string")
    for key in ("base_url", "model"):
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValueError(f"VLM {key} must be a nonempty string")
    timeout = settings["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("VLM timeout_seconds must be a positive finite number")
    retries = settings["max_retries"]
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("VLM max_retries must be a nonnegative integer")
    return settings
