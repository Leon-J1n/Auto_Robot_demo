"""Atomic skill catalog. Deterministic: the model may only pick names from here."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label: str
    description: str
    prompt_signature: str
    requires_target: bool = True
    allowed_regions: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    required_affordances: tuple[str, ...] = ()


class AtomicSkillRegistry:
    def __init__(self, definitions: tuple[SkillDefinition, ...]):
        self._items = {item.name: item for item in definitions}
        if len(self._items) != len(definitions):
            raise ValueError("skill names must be unique")

    def list(self) -> list[SkillDefinition]:
        return list(self._items.values())

    def require(self, name: str) -> SkillDefinition:
        if name not in self._items:
            raise ValueError(f"unknown skill_name: {name}")
        return self._items[name]

    def prompt_catalog(self) -> str:
        entries = []
        for item in self._items.values():
            lines = [item.prompt_signature, f"description: {item.description}"]
            if item.required_affordances:
                lines.append(f"requires: {'|'.join(item.required_affordances)}")
            if item.allowed_regions:
                lines.append(f"regions: {'|'.join(item.allowed_regions)}")
            if item.preconditions:
                lines.append(f"preconditions: {'; '.join(item.preconditions)}")
            if item.effects:
                lines.append(f"effects: {'; '.join(item.effects)}")
            entries.append("\n".join(lines))
        return "\n\n".join(entries)

    def describe(self, name: str, target: str | None = None, reference: str | None = None,
                 region: str | None = None) -> str:
        self.require(name)
        suffix = target or "目标"
        if reference:
            suffix += f"（参考 {reference}）"
        if region:
            suffix += f" / {region}"
        return f"{self.require(name).label}{suffix}"


REGISTRY = AtomicSkillRegistry((
    SkillDefinition(name="locate", label="定位", description="定位已知语义目标", prompt_signature="locate(target)", effects=("target becomes located",)),
    SkillDefinition(name="search", label="搜索", description="搜索尚未定位的任务目标", prompt_signature="search(target)", effects=("target becomes located",)),
    SkillDefinition(name="move", label="移动到", description="移动末端到目标的语义交互区域", prompt_signature="move(target,reference?,region?)", allowed_regions=("grasp_region", "container_interior", "button_surface", "relative_region", "semantic_region"), preconditions=("target is located",), effects=("end effector reaches the requested semantic region",)),
    SkillDefinition(name="grasp", label="抓取", description="抓取已定位且可抓取的目标", prompt_signature="grasp(target)", required_affordances=("graspable",), preconditions=("target is located", "end effector has reached a valid grasp region"), effects=("target becomes held",)),
    SkillDefinition(name="release", label="释放", description="释放当前已抓取的目标", prompt_signature="release(target,reference?,region?)", allowed_regions=("container_interior", "relative_region", "semantic_region"), preconditions=("target is held",), effects=("target is no longer held",)),
    SkillDefinition(name="press", label="按压", description="按压具有可按压能力的目标", prompt_signature="press(target)", required_affordances=("pressable",), preconditions=("target is located", "end effector has reached the button surface"), effects=("target press interaction is executed",)),
    SkillDefinition(name="pull", label="拉动", description="拉动支持拉动交互的关节机构", prompt_signature="pull(target,reference?)", required_affordances=("pullable",), preconditions=("required grasp or contact is established",), effects=("target articulated mechanism moves through pull interaction",)),
    SkillDefinition(name="push", label="推动", description="推动支持推动交互的关节机构", prompt_signature="push(target,reference?)", required_affordances=("pushable",), preconditions=("required grasp or contact is established",), effects=("target articulated mechanism moves through push interaction",)),
))
