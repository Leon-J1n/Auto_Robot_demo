"""OpenAI-compatible Qwen chat client.

Plain HTTP over ``urllib.request``: the repo pins only numpy in
``pyproject.toml``, so no SDK and no third-party HTTP dependency is introduced.
Configuration comes from the environment (design doc 4.2); a missing base URL or
model name is reported as ``MODEL_UNAVAILABLE`` and never raised at import time.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..protocol.errors import ErrorCode
from .budget import StageGenerationConfig
from .prompts import (
    SKILL_PLANNING_PROMPT,
    TASK_UNDERSTANDING_PROMPT,
    VISION_GROUNDING_PROMPT,
    prompt_payload,
)
from .skill_planning import SkillPlanLLMOutput
from .task_understanding import TaskParseLLMOutput
from .vision_grounding import VisionLLMOutput

# Known-good endpoint, used only as a default when the environment is silent.
DEFAULT_BASE_URL = "https://llm-4kt7tgvbu4ffd711.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-27b"

# API key env vars in precedence order; ALIYUN_API_KEY is what this machine sets.
API_KEY_ENV_VARS = ("QWEN_API_KEY", "ALIYUN_API_KEY")

STRUCTURED_OUTPUT_MODES = ("json_schema", "json_object", "off")

# HTTP statuses an OpenAI-compatible server returns when it does not implement
# the requested response_format.  Only these trigger a capability downgrade;
# transport errors and contract violations still fail the turn.
_CAPABILITY_REJECTION_STATUSES = frozenset({400, 404, 415, 422, 501})


class QwenProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    """Resolved model configuration. ``api_key`` is never logged or echoed."""

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 120.0

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class ModelAvailability:
    """Startup self-check result; injectable so tests can replace it."""

    available: bool
    base_url: str
    model: str
    has_api_key: bool
    error_code: str | None = None
    blockers: tuple[str, ...] = ()

    def as_public_dict(self) -> dict[str, Any]:
        """Diagnostics safe to log or expose; carries no credential material."""
        return {
            "available": self.available,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": self.has_api_key,
            "error_code": self.error_code,
            "blockers": list(self.blockers),
        }


def resolve_api_key(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    for name in API_KEY_ENV_VARS:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return ""


def load_model_config(env: dict[str, str] | None = None, *, use_defaults: bool = True) -> ModelConfig:
    """Read QWEN_BASE_URL / QWEN_MODEL / QWEN_API_KEY (or ALIYUN_API_KEY)."""
    source = os.environ if env is None else env
    base_url = (source.get("QWEN_BASE_URL") or "").strip()
    model = (source.get("QWEN_MODEL") or "").strip()
    if use_defaults:
        base_url = base_url or DEFAULT_BASE_URL
        model = model or DEFAULT_MODEL
    timeout_raw = (source.get("QWEN_TIMEOUT_SECONDS") or "").strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else 120.0
    except ValueError:
        timeout = 120.0
    return ModelConfig(base_url=base_url, model=model, api_key=resolve_api_key(source), timeout=timeout)


def check_model_availability(config: ModelConfig | None = None) -> ModelAvailability:
    """Configuration self-check. Never raises: callers stay startable."""
    resolved = config if config is not None else load_model_config()
    blockers: list[str] = []
    if not resolved.base_url:
        blockers.append("QWEN_BASE_URL is not configured")
    if not resolved.model:
        blockers.append("QWEN_MODEL is not configured")
    return ModelAvailability(
        available=not blockers,
        base_url=resolved.base_url,
        model=resolved.model,
        has_api_key=resolved.has_api_key,
        error_code=None if not blockers else ErrorCode.MODEL_UNAVAILABLE.value,
        blockers=tuple(blockers),
    )


class QwenHTTPProvider:
    """OpenAI-compatible fixed-call provider; retries are intentionally disabled.

    ``use_structured_output`` names the preferred mode. When the endpoint rejects
    it as unsupported, the provider degrades json_schema -> json_object -> off
    (validated plain text) and keeps using the mode that worked.
    """

    provider_id = "qwen_http"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 120.0,
                 stage_max_completion_tokens: dict[str, int] | None = None,
                 stage_generation: dict[str, StageGenerationConfig] | None = None,
                 use_structured_output: str | bool = "json_schema",
                 allow_structured_output_fallback: bool = True):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []
        # Structured final responses are retained for observability even when
        # the subsequent Pydantic contract validation rejects them.
        self.last_raw_values: dict[str, Any] = {}
        self.stage_max_completion_tokens = stage_max_completion_tokens or {
            # Sized for a reasoning model. `qwen3.8-27b` spends
            # `completion_tokens_details.reasoning_tokens` before emitting any content, and
            # those count against `max_completion_tokens`; the source stack's caps (768/384/
            # 1024) were tuned for a non-reasoning endpoint, so a real skill-planning prompt
            # exhausted the budget during reasoning and returned empty content. Verified
            # against the live endpoint: 1024 fails on skill_planning, 4096 succeeds. These
            # are upper bounds on a handful of calls per turn, not per-call cost.
            "task_understanding": 4096, "vision_grounding": 2048, "skill_planning": 8192,
        }
        self.stage_generation = stage_generation or {}
        self.use_structured_output = "json_object" if use_structured_output is True else ("off" if use_structured_output is False else use_structured_output)
        if self.use_structured_output not in set(STRUCTURED_OUTPUT_MODES):
            raise ValueError("structured output mode must be json_schema, json_object, or off")
        self.allow_structured_output_fallback = allow_structured_output_fallback

    @classmethod
    def from_config(cls, config: ModelConfig, **kwargs: Any) -> "QwenHTTPProvider":
        return cls(config.base_url, config.model, api_key=config.api_key,
                   timeout=config.timeout, **kwargs)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **kwargs: Any) -> "QwenHTTPProvider":
        return cls.from_config(load_model_config(env), **kwargs)

    def _fallback_modes(self) -> list[str]:
        if not self.allow_structured_output_fallback:
            return [self.use_structured_output]
        start = STRUCTURED_OUTPUT_MODES.index(self.use_structured_output)
        return list(STRUCTURED_OUTPUT_MODES[start:])

    def _build_payload(self, stage: str, mode: str, prompt: str,
                       user_content: str | list[dict[str, Any]]) -> dict[str, Any]:
        if mode in {"json_object", "off"}:
            prompt = prompt + "\n只输出一个 JSON 对象，不要 markdown 或额外文字。"
        config = self.stage_generation.get(stage)
        payload: dict[str, Any] = {"model": self.model, "temperature": config.temperature if config else 0, "messages": [
            {"role": "system", "content": prompt}, {"role": "user", "content": user_content}
        ]}
        max_tokens = config.max_completion_tokens if config else self.stage_max_completion_tokens.get(stage)
        if max_tokens is not None:
            payload["max_completion_tokens"] = max_tokens
        # OpenAI-compatible Qwen servers that implement guided JSON accept this
        # parameter.  It is deliberately optional for older servers.
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": stage, "strict": True, "schema": _stage_schema(stage)},
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise QwenProviderError("chat completion response must be a JSON object")
        return value

    def _call(self, stage: str, prompt: str, user_content: str | list[dict[str, Any]]) -> str:
        status: int | None = None
        modes = self._fallback_modes()
        for index, mode in enumerate(modes):
            try:
                body = self._post(self._build_payload(stage, mode, prompt, user_content))
            except urllib.error.HTTPError as exc:
                status = exc.code
                detail = _read_error_body(exc)
                unsupported = mode != "off" and exc.code in _CAPABILITY_REJECTION_STATUSES
                if unsupported and index + 1 < len(modes):
                    self.calls.append({"stage": stage, "status": "degraded", "model": self.model,
                                       "structured_output": mode, "http_status": exc.code,
                                       "finish_reason": None, "error": detail})
                    continue
                self.calls.append({"stage": stage, "status": "failed", "model": self.model,
                                   "structured_output": mode, "http_status": exc.code,
                                   "finish_reason": None, "error": detail})
                raise QwenProviderError(f"{stage}: HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError, KeyError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                self.calls.append({"stage": stage, "status": "failed", "model": self.model,
                                   "structured_output": mode, "http_status": status,
                                   "finish_reason": None, "error": str(exc)})
                raise QwenProviderError(f"{stage}: {type(exc).__name__}: {exc}") from exc

            try:
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
                if not isinstance(content, str) or not content.strip():
                    # A reasoning model can spend the whole completion budget thinking and
                    # return no content at all. Say so, because the fix is a larger cap rather
                    # than a prompt or schema change.
                    usage = body.get("usage") if isinstance(body, dict) else None
                    detail = ""
                    if isinstance(usage, dict):
                        reasoning = (usage.get("completion_tokens_details") or {}).get(
                            "reasoning_tokens")
                        if reasoning:
                            cap = self.stage_max_completion_tokens.get(stage)
                            detail = (f" (spent {reasoning} reasoning tokens against a"
                                      f" {cap} cap; raise"
                                      f" stage_max_completion_tokens[{stage!r}])")
                    raise QwenProviderError(f"{stage}: empty model content{detail}")
                extracted = _extract_json(content)
            except (QwenProviderError, KeyError, IndexError, TypeError, ValueError) as exc:
                self.calls.append({"stage": stage, "status": "failed", "model": self.model,
                                   "structured_output": mode, "http_status": None,
                                   "finish_reason": _finish_reason(body), "error": str(exc)})
                if isinstance(exc, QwenProviderError):
                    raise
                raise QwenProviderError(f"{stage}: {type(exc).__name__}: {exc}") from exc

            usage = body.get("usage") or {}
            # A working mode is remembered so later stages skip the rejected one.
            self.use_structured_output = mode
            self.calls.append({"stage": stage, "status": "succeeded", "model": self.model,
                               "structured_output": mode,
                               "prompt_tokens": usage.get("prompt_tokens"),
                               "completion_tokens": usage.get("completion_tokens"),
                               "finish_reason": _finish_reason(body)})
            return extracted
        raise QwenProviderError(f"{stage}: no structured output mode available")

    def understand(self, request):
        content = prompt_payload({"instruction": request.instruction})
        value = json.loads(self._call("task_understanding", TASK_UNDERSTANDING_PROMPT, content))
        # Accept the prototype's echoed instruction for compatibility, but do
        # not expose it as part of the minimal parser contract.
        if isinstance(value, dict):
            value.pop("instruction", None)
        return TaskParseLLMOutput.model_validate(value)

    def detect(self, request):
        image = Path(request.rgb_path)
        if not image.is_file():
            raise QwenProviderError(f"vision_grounding: RGB not found: {image}")
        mime = mimetypes.guess_type(image.name)[0] or "image/png"
        content = [
            {"type": "text", "text": prompt_payload({"entities": [item.model_dump(mode="json") for item in request.entities]})},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image.read_bytes()).decode('ascii')}"}},
        ]
        return VisionLLMOutput.model_validate(json.loads(self._call("vision_grounding", VISION_GROUNDING_PROMPT, content)))

    def plan(self, request):
        content = prompt_payload({
            "operations": [item.model_dump(mode="json") for item in request.context.operations],
            "entities": [item.model_dump(mode="json") for item in request.context.entities],
            "goals": [item.model_dump(mode="json") for item in request.context.goals],
            "skills": request.skill_catalog,
        })
        raw_text = self._call("skill_planning", SKILL_PLANNING_PROMPT, content)
        raw_value = json.loads(raw_text)
        self.last_raw_values["skill_planning"] = raw_value
        return SkillPlanLLMOutput.model_validate(raw_value)


_VISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"detections": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"entity": {"type": "string"},
                       "bbox": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}},
        "required": ["entity", "bbox"]}}},
    "required": ["detections"],
}


def _stage_schema(stage: str) -> dict[str, Any]:
    if stage == "vision_grounding":
        return _VISION_SCHEMA
    schema_models = {
        "task_understanding": TaskParseLLMOutput,
        "skill_planning": SkillPlanLLMOutput,
    }
    try:
        return schema_models[stage].model_json_schema()
    except KeyError as exc:
        raise QwenProviderError(f"no response schema for stage {stage}") from exc


def _finish_reason(body: dict[str, Any]) -> Any:
    choices = body.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    return first.get("finish_reason")


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:2000]
    except Exception:  # noqa: BLE001 - diagnostics must not mask the HTTP error
        return exc.reason if isinstance(exc.reason, str) else str(exc.reason)


def _extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text[3:].strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
    raise QwenProviderError("model output does not contain a JSON object")
