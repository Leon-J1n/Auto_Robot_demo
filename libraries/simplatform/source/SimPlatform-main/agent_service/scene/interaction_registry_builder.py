"""Bake interaction metadata for a scene plan.

Route A generates metadata from the plan; Route B binds an authored file to the
scene it was made for.  Both are deterministic and never load a simulator: the
source stack read reference poses back from a compiled MuJoCo model, and we take
them from ``ScenePlacement.world_position`` instead, which is the same value the
runtime will use when it creates the entity.

Only ordinary graspables, generated open boxes and generated buttons are
supported for Route A.  Articulated doors and drawers require authored metadata.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .builder import ScenePlan, ScenePlacement

# Route A convention: +x is "front", away from the robot base toward the table.
# NOTE: authored Route B registries use front = [-1, 0, 0], the exact opposite.
# The two conventions are NOT unified: authored files were measured by hand
# against real scenes and rewriting their sign would silently move every
# approach pose.  ``AUTHORED_FRONT`` documents the difference; whichever
# registry a route uses is the one its runtime reads.
GENERATED_FRONT = [1.0, 0.0, 0.0]
AUTHORED_FRONT = [-1.0, 0.0, 0.0]

DIRECTIONS = {
    "above": [0.0, 0.0, 1.0],
    "below": [0.0, 0.0, -1.0],
    "front": GENERATED_FRONT,
    "behind": [-1.0, 0.0, 0.0],
    "left": [0.0, 1.0, 0.0],
    "right": [0.0, -1.0, 0.0],
    "at": [0.0, 0.0, 0.0],
}

DEFAULT_DISTANCES = {
    "above": 0.12,
    "below": 0.08,
    "front": 0.10,
    "behind": 0.10,
    "left": 0.10,
    "right": 0.10,
    "at": 0.0,
}

TOOL_ORIENTATION = {
    "representation": "rpy",
    "roll": 180.0,
    "pitch": 0.0,
    "yaw": 0.0,
}

MOVE_DEFAULTS = {
    "planning": {
        "mode": "auto",
        "allow_replan": True,
        "planning_time": 8.0,
        "max_attempts": 3,
    },
    "constraints": {
        "avoid_collision": True,
        "velocity_scale": 0.8,
        "acceleration_scale": 0.8,
        "position_tolerance": 0.005,
        "orientation_tolerance": 2.0,
        "timeout": 30.0,
    },
    # The runtime's portable numerical IK is selected by its public ``auto``
    # profile; backend-specific names stay internal.
    "strategy_params": {"ik_method": "auto", "cartesian_step": 0.01},
}

NAMED_TARGETS = {
    "home": {
        "aliases": ["home", "初始位"],
        "request": {"target": {"type": "joint"}},
    }
}

PLATFORM_WORLD_FRAME = "platform_world"
ENTITY_SOURCE_TYPE = "entity"


def build_generated_registry(
    plan: ScenePlan,
    *,
    scene_fingerprint: str,
    scene: str | None = None,
) -> dict[str, Any]:
    """Generate execution metadata for a plan whose objects have measured geometry.

    Route A qualifies because it composed the objects itself. A plan built from a live scene
    qualifies too: the platform reports each object's world pose and extents, which is exactly
    what an anchor needs. What cannot be described is a plan carrying only object *names* --
    an opaque uploaded scene -- and that still requires an authored registry.
    """
    if not plan.placements:
        raise ValueError(
            "generated interaction metadata needs measured placements; "
            "an opaque scene requires an authored interaction registry")
    objects = {
        placement.object_id: _object_entry(placement)
        for placement in plan.placements
    }
    return {
        "version": 2,
        "registry_version": 1,
        "coordinate_frame": PLATFORM_WORLD_FRAME,
        "scene": scene if scene is not None else plan.base_scene,
        "scene_fingerprint": scene_fingerprint,
        "move_defaults": deepcopy(MOVE_DEFAULTS),
        "named_targets": deepcopy(NAMED_TARGETS),
        "objects": objects,
    }


def bind_authored_registry(
    authored: Mapping[str, Any],
    *,
    scene: str,
    scene_fingerprint: str,
) -> dict[str, Any]:
    """Bind authored metadata to the exact scene it was made for.

    Authored ``front`` vectors are left alone; see ``AUTHORED_FRONT``.
    """
    if not isinstance(authored, Mapping) or not isinstance(authored.get("objects"), Mapping):
        raise ValueError("authored interaction registry is invalid")
    expected = authored.get("scene_fingerprint")
    if expected is not None and expected != scene_fingerprint:
        raise ValueError(
            f"authored interaction registry does not match scene: expected {expected}, got {scene_fingerprint}"
        )
    result = deepcopy(dict(authored))
    result["registry_version"] = 1
    result["coordinate_frame"] = PLATFORM_WORLD_FRAME
    result["scene"] = scene
    result["scene_fingerprint"] = scene_fingerprint
    return result


def _object_entry(placement: ScenePlacement) -> dict[str, Any]:
    dimensions = placement.dimensions_m or (0.06, 0.06, 0.06)
    reference_position = [
        # The live body pose is anchored at the placement contact point.  Geometry
        # offsets belong to child visual/collision shapes and must not be added to
        # the body's reference pose (or anchors would be shifted twice).
        float(placement.world_position[index])
        for index in range(3)
    ]
    aliases = list(dict.fromkeys(filter(None, (
        placement.semantic_name,
        placement.object_id,
        placement.model_name,
        placement.entity_id,
    ))))
    common: dict[str, Any] = {
        "object_id": placement.object_id,
        "body_name": placement.body_name,
        "aliases": aliases,
        "spatial": {
            "source": {"type": ENTITY_SOURCE_TYPE, "name": placement.body_name},
            "reference_pose": {
                "position": reference_position,
                "quaternion_wxyz": [float(value) for value in placement.orientation_wxyz],
            },
            "directions_local": deepcopy(DIRECTIONS),
            "default_distances": deepcopy(DEFAULT_DISTANCES),
            "tool_orientation": deepcopy(TOOL_ORIENTATION),
        },
        "default_interactions": {},
        "action_requests": {},
    }

    configured_capabilities = getattr(placement, "capabilities", None)
    capabilities = frozenset(configured_capabilities or ())
    is_container = "container" in capabilities or placement.model_name == "open_box"
    is_pressable = "pressable" in capabilities or placement.model_name == "button_basic"

    if is_container:
        common["interactable"] = False
        common["spatial"].update({
            "default_anchor": "interior",
            "anchors": {
                "interior": {
                    "target_id": f"{placement.object_id}_interior",
                    "aliases": ["interior", "inside", "内部", "里面"],
                    "local_position": [
                        0.0,
                        0.0,
                        max(0.02, dimensions[2] * 0.55),
                    ],
                }
            },
        })
    elif is_pressable:
        common["interactable"] = True
        common["spatial"].update({
            "default_anchor": "button_surface",
            "anchors": {
                "button_surface": {
                    "target_id": f"{placement.object_id}_surface",
                    "aliases": ["button surface", "按钮表面", "按压点"],
                    # Stop above the physical cap.  The press controller owns
                    # the final displacement; placing the move pose inside the
                    # button geometry is rejected as a local collision during
                    # preflight.
                    "local_position": [
                        0.0, 0.0, 0.10],
                }
            },
        })
        common["default_interactions"] = {"press": "button_surface"}
        common["action_requests"] = {
            "press": {
                "target": {
                    "type": "button",
                    "object_id": placement.object_id,
                    "surface_normal": [0.0, 0.0, -1.0],
                },
                "press": {
                    "strategy": "displacement_press",
                    "control_mode": "displacement",
                    "operation": "press",
                    "direction_source": "surface_normal",
                    "retract_after_press": True,
                    "verification_mode": "position",
                },
                "constraints": {
                    "maximum_force": 100.0,
                    "maximum_travel": 0.10,
                    "contact_threshold": 0.2,
                    "press_speed": 0.01,
                    "retract_speed": 0.03,
                    "hold_time": 0.1,
                    "verify_press": True,
                    "retry_count": 0,
                    "timeout": 10.0,
                    "require_contact": False,
                    "emergency_retract": True,
                },
                "strategy_params": {
                    "press_depth": 0.06,
                    "stop_on_contact": False,
                },
            }
        }
    elif configured_capabilities is None or "graspable" in capabilities:
        common["interactable"] = True
        common["interaction_metadata"] = {
            "grasp_source": "generic_default",
            "grasp_confidence": None,
        }
        common["spatial"].update({
            "default_anchor": "grasp",
            "anchors": {
                "target": {
                    "target_id": f"{placement.object_id}_target",
                    "aliases": ["target", "目标处"],
                    "local_position": [
                        0.0,
                        0.0,
                        dimensions[2] * 0.5,
                    ],
                },
                "grasp": {
                    "target_id": f"{placement.object_id}_grasp",
                    "aliases": ["grasp", "抓取位姿"],
                    "local_position": [
                        0.0,
                        0.0,
                        dimensions[2] * 0.5,
                    ],
                },
            },
        })
        expected_width = min(float(dimensions[0]), float(dimensions[1]))
        common["default_interactions"] = {"grasp": "grasp"}
        common["action_requests"] = {
            "grasp": {
                "target": {
                    "type": "object",
                    "object_id": placement.object_id,
                    "expected_width": expected_width,
                },
                "grasp": {
                    "strategy": "auto",
                    "operation": "regrasp",
                    "verification_mode": "auto",
                },
                "constraints": {
                    "verify_grasp": True,
                    "retry_count": 1,
                    "hold_on_success": True,
                    "timeout": 5.0,
                    "maximum_force": 40.0,
                    "contact_required": False,
                    "slip_check": False,
                },
                "strategy_params": {
                    "open_width": 0.08,
                    "close_width": 0.0,
                    "close_speed": 0.5,
                    "grasp_force": 20.0,
                    "hold_force": 10.0,
                    "position_tolerance": 0.01,
                    "settle_time": 0.0,
                },
            },
            "release": {
                "target": {"type": "object", "object_id": placement.object_id},
                "constraints": {
                    "open_width": 0.08,
                    "timeout": 5.0,
                    "verify_release": False,
                },
            },
        }
    else:
        common["interactable"] = False
    return common
