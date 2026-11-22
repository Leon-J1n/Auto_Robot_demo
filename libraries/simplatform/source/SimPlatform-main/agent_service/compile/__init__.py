"""Deterministic SkillPlan -> CommandDocument compilation. Never calls a model."""

from .compiler import (
    MANIPULATION_SKILLS,
    REGION_TO_ANCHOR,
    SKILL_TO_CONTROL_SKILL,
    compile_directory,
    compile_execution_bundle,
    control_skill_name,
)
from .execution_profile import ExecutionProfile, load_execution_profile
from .fingerprint import base_scene_sha256, canonical_digest, scene_plan_fingerprint
from .inputs import (
    CompilerGroundedTask,
    CompilerOperation,
    CompilerSkillPlan,
    CompilerSkillStep,
    grounded_task_from_mapping,
    load_grounded_task,
    load_skill_plan,
    skill_plan_from_mapping,
)

__all__ = [
    "CompilerGroundedTask",
    "CompilerOperation",
    "CompilerSkillPlan",
    "CompilerSkillStep",
    "ExecutionProfile",
    "MANIPULATION_SKILLS",
    "REGION_TO_ANCHOR",
    "SKILL_TO_CONTROL_SKILL",
    "base_scene_sha256",
    "canonical_digest",
    "compile_directory",
    "compile_execution_bundle",
    "control_skill_name",
    "grounded_task_from_mapping",
    "load_execution_profile",
    "load_grounded_task",
    "load_skill_plan",
    "scene_plan_fingerprint",
    "skill_plan_from_mapping",
]
