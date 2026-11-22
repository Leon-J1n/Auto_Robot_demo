"""Tests for the model runner's HTTP behaviour, without touching a real endpoint.

`_post` is the single HTTP seam, so everything here stubs it. The reasoning-token case is the
important one: `qwen3.8-27b` spends `completion_tokens_details.reasoning_tokens` before emitting
any content, and those count against `max_completion_tokens`. The source stack's caps were sized
for a non-reasoning endpoint, so a real skill-planning prompt came back with empty content and
failed the turn. These tests pin the caps and the diagnosis.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.planning.qwen_client import (API_KEY_ENV_VARS,  # noqa: E402
                                                STRUCTURED_OUTPUT_MODES, ModelConfig,
                                                QwenHTTPProvider, QwenProviderError,
                                                check_model_availability, load_model_config,
                                                resolve_api_key)
from agent_service.planning.task_understanding import TaskUnderstandingRequest  # noqa: E402

PARSED = {
    "status": "accepted",
    "entities": [{"id": "cube", "name": "红色方块", "category": "object"},
                 {"id": "tray", "name": "绿色托盘", "category": "container"}],
    "operations": [{"type": "pick_and_place", "source": "cube", "destination": "tray"}],
    "relations": [],
    "raw_direction": None,
    "raw_task": None,
}


def _provider(**kwargs: Any) -> QwenHTTPProvider:
    return QwenHTTPProvider("http://127.0.0.1:1/v1", "test-model", api_key="", **kwargs)


def _completion(content: str, *, reasoning: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if reasoning is not None:
        body["usage"] = {"completion_tokens_details": {"reasoning_tokens": reasoning}}
    return body


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/v1/chat/completions", status, "rejected", {},  # type: ignore[arg-type]
                                  io.BytesIO(b'{"error":{"message":"unsupported"}}'))


# -- stage caps (the regression) -------------------------------------------------------

def test_default_stage_caps_allow_room_for_reasoning() -> None:
    caps = _provider().stage_max_completion_tokens
    # Verified against the live endpoint: skill_planning fails at 1024 and succeeds at 4096,
    # because the reasoning phase consumes the budget before any content is produced.
    assert caps["skill_planning"] >= 4096
    assert caps["task_understanding"] >= 2048
    assert caps["vision_grounding"] >= 1024


def test_stage_cap_reaches_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    seen: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return _completion(json.dumps(PARSED))

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.understand(TaskUnderstandingRequest(instruction="把红色方块放到绿色托盘里"))
    assert seen[0]["max_completion_tokens"] == (
        provider.stage_max_completion_tokens["task_understanding"])
    assert seen[0]["temperature"] == 0


def test_empty_content_names_the_reasoning_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    monkeypatch.setattr(provider, "_post",
                        lambda payload: _completion("", reasoning=4096))
    with pytest.raises(QwenProviderError) as error:
        provider.understand(TaskUnderstandingRequest(instruction="go"))
    message = str(error.value)
    # The fix for this failure is a larger cap, so the error has to say so; a bare
    # "empty model content" sent me looking at prompts and schemas instead.
    assert "reasoning tokens" in message
    assert "stage_max_completion_tokens" in message
    assert str(provider.stage_max_completion_tokens["task_understanding"]) in message


def test_empty_content_without_usage_still_fails_cleanly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    monkeypatch.setattr(provider, "_post", lambda payload: _completion(""))
    with pytest.raises(QwenProviderError) as error:
        provider.understand(TaskUnderstandingRequest(instruction="go"))
    assert "empty model content" in str(error.value)


def test_explicit_stage_caps_override_the_defaults() -> None:
    provider = _provider(stage_max_completion_tokens={"skill_planning": 999})
    assert provider.stage_max_completion_tokens["skill_planning"] == 999


# -- structured output ----------------------------------------------------------------

def test_json_schema_is_the_preferred_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(provider, "_post",
                        lambda payload: (seen.append(payload),
                                         _completion(json.dumps(PARSED)))[1])
    provider.understand(TaskUnderstandingRequest(instruction="go"))
    assert seen[0]["response_format"]["type"] == "json_schema"


def test_capability_rejection_falls_back_through_the_chain(
        monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    modes: list[str] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        mode = (payload.get("response_format") or {}).get("type", "off")
        modes.append(mode)
        if mode != "off":
            # An endpoint that does not implement guided JSON rejects the parameter.
            raise _http_error(400)
        return _completion(json.dumps(PARSED))

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.understand(TaskUnderstandingRequest(instruction="go"))
    assert modes == list(STRUCTURED_OUTPUT_MODES)


def test_fallback_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(allow_structured_output_fallback=False)
    calls: list[int] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(provider, "_post", fake_post)
    with pytest.raises(QwenProviderError):
        provider.understand(TaskUnderstandingRequest(instruction="go"))
    assert len(calls) == 1


def test_a_server_error_is_not_treated_as_a_capability_rejection(
        monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    calls: list[int] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise _http_error(500)

    monkeypatch.setattr(provider, "_post", fake_post)
    with pytest.raises(QwenProviderError):
        provider.understand(TaskUnderstandingRequest(instruction="go"))
    # 500 means the endpoint broke, not that it lacks guided JSON, so retrying in a weaker
    # mode would just hide the fault.
    assert len(calls) == 1


def test_a_contract_violation_fails_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    # "accepted" with no entities or operations violates the contract; nothing is guessed.
    bad = dict(PARSED, entities=[], operations=[])
    monkeypatch.setattr(provider, "_post",
                        lambda payload: _completion(json.dumps(bad)))
    with pytest.raises(Exception):
        provider.understand(TaskUnderstandingRequest(instruction="go"))


# -- configuration --------------------------------------------------------------------

def test_api_key_chain_prefers_qwen_then_aliyun() -> None:
    assert API_KEY_ENV_VARS == ("QWEN_API_KEY", "ALIYUN_API_KEY")
    assert resolve_api_key({"QWEN_API_KEY": "first", "ALIYUN_API_KEY": "second"}) == "first"
    assert resolve_api_key({"ALIYUN_API_KEY": "second"}) == "second"
    assert resolve_api_key({"QWEN_API_KEY": "   "}) == ""
    assert resolve_api_key({}) == ""


def test_an_empty_key_is_allowed_for_a_local_endpoint() -> None:
    config = load_model_config({"QWEN_BASE_URL": "http://127.0.0.1:8000/v1",
                                "QWEN_MODEL": "local"})
    assert config.api_key == ""
    # An unauthenticated local endpoint is legitimate, so a missing key must not block.
    assert check_model_availability(config).available


def test_missing_base_url_or_model_marks_the_model_unavailable() -> None:
    for config in (ModelConfig(base_url="", model="m"),
                   ModelConfig(base_url="http://x/v1", model="")):
        availability = check_model_availability(config)
        assert not availability.available
        assert availability.blockers


def test_availability_never_exposes_the_key() -> None:
    config = ModelConfig(base_url="http://x/v1", model="m", api_key="super-secret-value")
    public = check_model_availability(config).as_public_dict()
    assert "super-secret-value" not in json.dumps(public)
    # Whether a key is configured is useful; its value is not.
    assert public.get("has_api_key") is True
