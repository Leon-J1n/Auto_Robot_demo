"""Wire contracts shared by planning, compile and runtime.

Pure pydantic models: this package must stay free of simulator imports so both
the deterministic compiler and the Live execution layer can depend on it.
"""
from .commands import (
    CommandDocument,
    ExecutionOptions,
    SkillCommand,
    StrictModel,
    ViewerMode,
    load_command_document,
)
from .errors import ErrorCode
from .execution import (
    ExecutionBundle,
    ExecutionFailure,
    ExecutionReport,
    ExecutionStepReport,
    ProcessFailure,
    RuntimeStepReport,
    validate_bundle_consistency,
)
from .fingerprint import scene_sha256
from .interactions import (
    ENTITY_SOURCE_TYPE,
    LEGACY_SOURCE_TYPES,
    LEGACY_WORLD_FRAMES,
    PLATFORM_WORLD_FRAME,
    InteractionRegistry,
    InteractionRegistryDocument,
    InteractionRegistryError,
    InteractionRegistryModel,
    normalize_spatial_source,
)
from .legacy_loader import load_legacy_command_document
from .version import SCHEMA_VERSION

__all__ = [
    "CommandDocument", "ExecutionBundle", "ExecutionFailure", "ExecutionOptions",
    "ExecutionReport", "ExecutionStepReport", "ProcessFailure", "RuntimeStepReport",
    "SkillCommand", "StrictModel", "ViewerMode", "InteractionRegistry",
    "InteractionRegistryDocument", "InteractionRegistryError", "InteractionRegistryModel",
    "ErrorCode", "SCHEMA_VERSION", "scene_sha256", "validate_bundle_consistency",
    "load_command_document", "load_legacy_command_document",
    "normalize_spatial_source", "PLATFORM_WORLD_FRAME",
    "LEGACY_WORLD_FRAMES", "ENTITY_SOURCE_TYPE", "LEGACY_SOURCE_TYPES",
]
