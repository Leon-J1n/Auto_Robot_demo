"""Planning layer: natural-language understanding, grounding and SkillPlan.

This is the only layer in ``agent_service`` allowed to call the model
(design doc 4.2). It produces structured TaskIntent / GroundedTask / SkillPlan
objects; joint values, trajectories and actuator commands belong to the
deterministic compile and runtime layers.
"""
from .budget import ModelCallBudget, ModelCallBudgetExceeded, StageGenerationConfig
from .context_builder import (
    PlannerContext,
    PlannerEntity,
    PlannerGoal,
    PlannerOperation,
    build_planner_context,
)
from .contracts import (
    Direction,
    GroundedEntity,
    GroundedTask,
    Operation,
    SkillPlan,
    SkillStep,
    SpatialRelation,
    SpatialRelationType,
    TaskEntity,
    TaskIntent,
    TaskStatus,
    TaskType,
)
from .prompts import (
    SKILL_PLANNING_PROMPT,
    TASK_UNDERSTANDING_PROMPT,
    VISION_GROUNDING_PROMPT,
    prompt_payload,
)
from .qwen_client import (
    API_KEY_ENV_VARS,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ModelAvailability,
    ModelConfig,
    QwenHTTPProvider,
    QwenProviderError,
    check_model_availability,
    load_model_config,
    resolve_api_key,
)
from .recipe_planner import RecipePlanner
from .recipes import RECIPE_DEFINITIONS, RecipeDefinition, validate_plan
from .semantic_validator import validate_semantic_plan
from .skill_planning import (
    LLMOperationPlan,
    LLMPlanStep,
    SkillPlanLLMOutput,
    SkillPlanningProvider,
    SkillPlanningRequest,
    enrich_skill_plan,
)
from .skills import REGISTRY, AtomicSkillRegistry, SkillDefinition
from .task_understanding import (
    ParseEntity,
    ParseOperation,
    ParseRelation,
    TaskParseLLMOutput,
    TaskUnderstandingProvider,
    TaskUnderstandingRequest,
    enrich_task,
)
from .vision_grounding import (
    VisionCandidate,
    VisionGroundingProvider,
    VisionGroundingRequest,
    VisionLLMOutput,
    VisionQuery,
)

__all__ = [
    # contracts
    "Direction", "GroundedEntity", "GroundedTask", "Operation", "SkillPlan", "SkillStep",
    "SpatialRelation", "SpatialRelationType", "TaskEntity", "TaskIntent", "TaskStatus", "TaskType",
    # model access
    "QwenHTTPProvider", "QwenProviderError", "ModelConfig", "ModelAvailability",
    "load_model_config", "check_model_availability", "resolve_api_key",
    "DEFAULT_BASE_URL", "DEFAULT_MODEL", "API_KEY_ENV_VARS",
    # stage contracts
    "ParseEntity", "ParseOperation", "ParseRelation", "TaskParseLLMOutput",
    "TaskUnderstandingProvider", "TaskUnderstandingRequest", "enrich_task",
    "LLMPlanStep", "LLMOperationPlan", "SkillPlanLLMOutput", "SkillPlanningProvider",
    "SkillPlanningRequest", "enrich_skill_plan",
    "VisionCandidate", "VisionGroundingProvider", "VisionGroundingRequest",
    "VisionLLMOutput", "VisionQuery",
    # deterministic planning
    "PlannerContext", "PlannerEntity", "PlannerGoal", "PlannerOperation",
    "build_planner_context", "RecipePlanner", "RecipeDefinition", "RECIPE_DEFINITIONS",
    "validate_plan", "validate_semantic_plan",
    "REGISTRY", "AtomicSkillRegistry", "SkillDefinition",
    # prompts and budget
    "TASK_UNDERSTANDING_PROMPT", "VISION_GROUNDING_PROMPT", "SKILL_PLANNING_PROMPT",
    "prompt_payload", "ModelCallBudget", "ModelCallBudgetExceeded", "StageGenerationConfig",
]
