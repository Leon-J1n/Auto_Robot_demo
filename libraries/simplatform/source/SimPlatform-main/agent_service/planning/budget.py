from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageGenerationConfig:
    """Per-stage decoding limits; keeping these explicit prevents an
    accidentally unbounded shared generation setting."""

    temperature: float = 0.0
    max_completion_tokens: int = 256


class ModelCallBudgetExceeded(RuntimeError):
    pass


@dataclass
class ModelCallBudget:
    max_calls: int
    allowed_stages: tuple[str, ...]
    stage_limits: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    stages: list[dict] = field(default_factory=list)

    @classmethod
    def for_route(cls, route_b: bool, planner: str = "qwen"):
        if planner == "recipe":
            stages = ("task_understanding", "vision_grounding") if route_b else ("task_understanding",)
            return cls(2 if route_b else 1, stages, {stage: 1 for stage in stages})
        stages = ("task_understanding", "vision_grounding", "skill_planning") if route_b else ("task_understanding", "skill_planning")
        return cls(3 if route_b else 2, stages, {stage: 1 for stage in stages})

    def consume(self, stage: str) -> None:
        used = sum(1 for item in self.stages if item["stage"] == stage)
        if stage not in self.allowed_stages or self.calls >= self.max_calls or (stage in self.stage_limits and used >= self.stage_limits[stage]):
            raise ModelCallBudgetExceeded(f"model call budget exceeded at {stage}")
        self.calls += 1
        self.stages.append({"stage": stage, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "finish_reason": None})

    def update(self, stage: str, record: dict | None) -> None:
        if not record:
            return
        item = self.stages[-1]
        for key in ("prompt_tokens", "completion_tokens"):
            value = record.get(key)
            if isinstance(value, int):
                item[key] = value
        if record.get("finish_reason") is not None:
            item["finish_reason"] = record["finish_reason"]
        item["total_tokens"] = item["prompt_tokens"] + item["completion_tokens"]

    def summary(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": sum(x["prompt_tokens"] for x in self.stages), "completion_tokens": sum(x["completion_tokens"] for x in self.stages), "total_tokens": sum(x["total_tokens"] for x in self.stages), "stages": self.stages}
