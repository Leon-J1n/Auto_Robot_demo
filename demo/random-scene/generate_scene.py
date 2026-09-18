"""Generate scene_999 and its interaction registry from grasp_objects."""
from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OBJECTS = ROOT / "world_model" / "robotsim" / "assets" / "grasp_objects"
REGISTRY = ROOT / "demo" / "common" / "scenes" / "scene_999.interactions.json"
COMMANDS = ROOT / "demo" / "skill_command" / "config_999.commands.json"


def _load_builder():
    spec = importlib.util.spec_from_file_location("random_scene_999", HERE / "random_scene.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _first_pose(name: str) -> list[float]:
    path = OBJECTS / name / "grasp_pose.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    poses = data.get("poses", [])
    if not poses or len(poses[0]) != 6:
        raise ValueError(f"{path} must contain at least one six-value pose")
    return [float(value) for value in poses[0]]


def generate(mode: str = "separated", seed: int | None = None) -> tuple[Path, Path, Path]:
    builder = _load_builder()
    assets = builder.discover_objects()
    placements = builder.place_objects(assets, mode, random.Random(seed))
    scene_path = builder.generate_scene(placements)
    objects = {}
    commands = []
    for index, placement in enumerate(placements):
        name = placement.asset.name
        x, y, z, rx, ry, rz = _first_pose(name)
        body_name = f"grasp_{index}_{name}"
        objects[name] = {
            "interactable": True,
            "object_id": name,
            "body_name": body_name,
            "aliases": [name],
            "spatial": {
                "source": {"type": "site", "name": body_name + "_object_frame"},
                "reference_pose": {"position": [placement.x, placement.y, placement.z], "quaternion_wxyz": [1, 0, 0, 0]},
                "default_anchor": "grasp",
                "anchors": {"grasp": {"target_id": name + "_grasp", "local_position": [x, y, z]}},
                "directions_local": {"at": [0, 0, 0], "above": [0, 0, 1], "below": [0, 0, -1]},
                "default_distances": {"at": 0.0, "above": 0.12, "below": 0.10},
                "tool_orientation": {"representation": "rpy", "roll": math.degrees(rx), "pitch": math.degrees(ry), "yaw": math.degrees(rz)},
            },
            "default_interactions": {"grasp": "grasp"},
            "action_requests": {
                "grasp": {"target": {"type": "object", "object_id": body_name}, "grasp": {"operation": "grasp"}, "constraints": {"verify_grasp": True, "contact_required": True, "hold_on_success": True, "timeout": 5.0}, "strategy_params": {"open_width": 0.08, "close_width": 0.0, "close_speed": 0.5, "grasp_force": 20.0, "hold_force": 10.0}},
                "release": {"target": {"type": "object", "object_id": body_name}, "constraints": {"open_width": 0.08, "timeout": 5.0, "verify_release": True}},
            },
        }
        commands.extend([
            {"skill_name": "move", "parameters": {"target": name}},
            {"skill_name": "grasp", "parameters": {"target": name}},
            {"skill_name": "move", "parameters": {"target": "end_effector", "relation": "above", "distance_m": 0.12, "planning_method": "linear"}},
            {"skill_name": "move", "parameters": {"target": "end_effector", "relation": "below", "distance_m": 0.12, "planning_method": "linear"}},
            {"skill_name": "release", "parameters": {"target": name}},
            {"skill_name": "move", "parameters": {"target": "home"}},
        ])
    registry = {"version": 2, "scene": "world_model/robotsim/scene_999.xml",
                "move_defaults": {
                    "motion": {"path_constraint": "soft", "phase": "transit", "optimization_goal": "balanced"},
                    "planning": {"mode": "auto", "allow_replan": True, "planning_time": 8.0, "max_attempts": 3},
                    "constraints": {"avoid_collision": True, "velocity_scale": 0.8, "acceleration_scale": 1,
                                    "position_tolerance": 0.005, "orientation_tolerance": 2.0, "timeout": 60.0},
                    "strategy_params": {"ik_method": "auto", "cartesian_step": 0.01}
                },
                "named_targets": {"home": {"aliases": ["home"], "request": {"target": {"type": "joint", "joint_positions": [0.0, -2.094395, 1.570796, -1.5707963, -1.5707963, 0.0]}}}}, "objects": objects}
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not COMMANDS.is_file():
        raise FileNotFoundError(f"Fixed command file is missing: {COMMANDS}")
    return scene_path, REGISTRY, COMMANDS


if __name__ == "__main__":
    generate()
