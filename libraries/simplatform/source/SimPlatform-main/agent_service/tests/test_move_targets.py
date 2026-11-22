"""Tests for how a move's goal is resolved: symbolic targets and the `home` fallback.

Both cases were found by running real turns against a live simulator, and both had failure modes
that pointed away from the cause:

* The compiler emits `end_effector` as the target of its post-grasp lift and post-release
  retreat -- a direction and a distance, not a point, because only the runtime knows the live
  tool pose. The executor resolved scene objects only, so the lift failed with
  "end_effector is not in the live scene and no pose was supplied".
* `home` fell back to an all-zeros configuration when nothing supplied one. Zeros is not a
  neutral pose: for a UR5e it stretches the arm out horizontally, through the table it stands
  on. Measured on the real scene, that configuration is in collision, while the base scene's
  own authored pose is clean.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.runtime.executor import (_RELATIVE_TARGETS, CommandFailure,  # noqa: E402
                                            _relative_pose, _resolve_goal_configuration,
                                            _target_pose)
from agent_service.skills.motion.move.command import _target  # noqa: E402
from agent_service.skills.motion.move.skill import _joint_limit_execution_error  # noqa: E402

# The pose the UR5e base scene is authored in. Verified collision-free on the real scene.
SCENE_POSE = np.array([0.0, -2.094, 1.571, -1.571, -1.571, 0.0])
LIMITS = np.tile(np.array([-6.28, 6.28]), (6, 1))
TOOL_DOWN = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])


class FakeRuntime:
    def __init__(self, *, home: Any = None, tcp: Any = (0.4, 0.1, 0.7),
                 rotation: Any = None, objects: dict[str, Any] | None = None) -> None:
        if home is not None:
            self.home_configuration = np.asarray(home, dtype=float)
        self._tcp = np.asarray(tcp, dtype=float)
        self._rotation = np.eye(3) if rotation is None else np.asarray(rotation, dtype=float)
        self._objects = objects or {}

    def get_end_effector_pose(self) -> dict[str, np.ndarray]:
        return {"position": self._tcp.copy(), "rotation_matrix": self._rotation.copy()}

    def object_pose(self, name: str) -> dict[str, np.ndarray] | None:
        return self._objects.get(name)


class FakeServices:
    """Only the attributes these resolvers read; the real _Services builds live adapters."""

    def __init__(self, runtime: Any, *, resolve_target: Any = None) -> None:
        self.runtime = runtime
        self.resolve_target = resolve_target
        self.joint_limits = LIMITS


class LimitRuntime:
    platform_joint_names = (
        "shoulder_pan_joint", "robotiq_2f85_left_driver_joint",
    )
    all_joint_limits = np.array([[-6.28, 6.28], [0.0, 0.8]])

    def full_joint_positions(self) -> np.ndarray:
        return np.array([0.1, 0.9])


def test_platform_gripper_limit_error_is_structured() -> None:
    error = RuntimeError(
        "joint target violates limits: robotiq_2f85_left_driver_joint")
    result = _joint_limit_execution_error(error, LimitRuntime())
    assert result is not None
    assert result["details"]["joint_name"] == "robotiq_2f85_left_driver_joint"
    assert result["details"]["joint_group"] == "gripper"
    assert result["details"]["limits"] == pytest.approx([0.0, 0.8])
    assert result["details"]["actual_position"] == pytest.approx(0.9)


def test_unrelated_runtime_error_is_not_misclassified() -> None:
    assert _joint_limit_execution_error(RuntimeError("controller offline"), LimitRuntime()) is None


# -- relative (tool-referenced) targets ------------------------------------------------

def test_end_effector_is_recognised_as_a_relative_target() -> None:
    assert "end_effector" in _RELATIVE_TARGETS
    # The compiler and older stacks use these names interchangeably for the tool.
    assert {"tcp", "tool"} <= _RELATIVE_TARGETS


def test_a_lift_offsets_the_current_tool_pose_upward() -> None:
    services = FakeServices(FakeRuntime(tcp=(0.4, 0.1, 0.7)))
    pose = _target_pose(services, {"relation": "above", "distance_m": 0.12}, "end_effector")
    assert pose["position"] == pytest.approx([0.4, 0.1, 0.82])
    # No orientation is imposed, so the IK layer holds the tool's current rotation -- which is
    # what a straight lift wants.
    assert "orientation" not in pose


def test_each_named_direction_moves_the_expected_way() -> None:
    services = FakeServices(FakeRuntime(tcp=(0.0, 0.0, 0.0)))

    def offset(relation: str) -> list[float]:
        pose = _relative_pose(services, {"relation": relation, "distance_m": 1.0},
                              "end_effector")
        return [round(float(v), 6) for v in pose["position"]]

    assert offset("above") == [0.0, 0.0, 1.0]
    assert offset("below") == [0.0, 0.0, -1.0]
    # +x points away from the robot base, matching the generated registry's convention.
    assert offset("front") == [1.0, 0.0, 0.0]
    assert offset("behind") == [-1.0, 0.0, 0.0]
    assert offset("left") == [0.0, 1.0, 0.0]
    assert offset("right") == [0.0, -1.0, 0.0]
    # "at" means no displacement, which a zero-distance retreat relies on.
    assert offset("at") == [0.0, 0.0, 0.0]


def test_a_tool_frame_offset_follows_the_wrist() -> None:
    # Wrist turned 90 degrees about +z: a tool-frame +x offset comes out along world +y.
    rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    services = FakeServices(FakeRuntime(tcp=(0.0, 0.0, 0.0), rotation=rotation))
    pose = _relative_pose(services, {"relation": "front", "distance_m": 0.2,
                                     "frame": "tool"}, "end_effector")
    assert pose["position"] == pytest.approx([0.0, 0.2, 0.0], abs=1e-9)


def test_a_world_frame_offset_ignores_the_wrist() -> None:
    rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    services = FakeServices(FakeRuntime(tcp=(0.0, 0.0, 0.0), rotation=rotation))
    pose = _relative_pose(services, {"relation": "front", "distance_m": 0.2}, "end_effector")
    # A lift or retreat expressed in world axes must not rotate with the tool.
    assert pose["position"] == pytest.approx([0.2, 0.0, 0.0], abs=1e-9)


def test_an_explicit_orientation_is_kept() -> None:
    services = FakeServices(FakeRuntime())
    pose = _relative_pose(services, {"relation": "above", "distance_m": 0.05,
                                     "orientation": {"rpy_deg": [180.0, 0.0, 0.0]}},
                          "end_effector")
    assert pose["orientation"] == {"rpy_deg": [180.0, 0.0, 0.0]}


def test_an_unknown_relation_or_frame_is_rejected() -> None:
    services = FakeServices(FakeRuntime())
    for parameters in ({"relation": "sideways", "distance_m": 0.1},
                       {"relation": "above", "distance_m": 0.1, "frame": "galactic"}):
        with pytest.raises(CommandFailure) as error:
            _relative_pose(services, parameters, "end_effector")
        # Guessing a direction would move the arm somewhere the plan never asked for.
        assert error.value.error_code == "INVALID_REQUEST"


def test_a_relative_target_never_consults_the_scene() -> None:
    calls: list[str] = []

    def resolver(target: str, anchor: Any) -> None:
        calls.append(target)
        return None

    services = FakeServices(FakeRuntime(), resolve_target=resolver)
    _target_pose(services, {"relation": "above", "distance_m": 0.1}, "end_effector")
    # "end_effector" is not an object; looking for one is how the original failure arose.
    assert calls == []


def test_a_scene_object_still_goes_through_the_resolver() -> None:
    seen: list[tuple[str, Any]] = []

    def resolver(target: str, anchor: Any) -> dict[str, Any]:
        seen.append((target, anchor))
        return {"position": np.array([0.5, 0.0, 0.66])}

    services = FakeServices(FakeRuntime(), resolve_target=resolver)
    pose = _target_pose(services, {"anchor": "interior"}, "green_tray_01")
    assert seen == [("green_tray_01", "interior")]
    assert pose["position"] == pytest.approx([0.5, 0.0, 0.66])


def test_move_adapter_uses_the_compiled_anchor_pose() -> None:
    runtime = FakeRuntime(objects={"red_ball_01": {
        "position": np.array([0.5, 0.0, 0.6]),
        "quaternion_wxyz": np.array([1.0, 0.0, 0.0, 0.0]),
    }})
    calls: list[tuple[str, Any]] = []

    def resolver(target: str, anchor: Any) -> dict[str, Any]:
        calls.append((target, anchor))
        return {"position": np.array([0.5, 0.0, 0.625]),
                "orientation": {"representation": "rpy", "roll": 180.0,
                                "pitch": 0.0, "yaw": 0.0}}

    target = _target(runtime, "red_ball_01", {"anchor": "grasp"}, resolver)
    assert calls == [("red_ball_01", "grasp")]
    assert target["position"] == pytest.approx({"x": 0.5, "y": 0.0, "z": 0.625})
    assert target["orientation"]["roll"] == 180.0


def test_move_adapter_converts_end_effector_relation_to_pose() -> None:
    runtime = FakeRuntime(tcp=(0.4, 0.1, 0.7))
    target = _target(runtime, "end_effector",
                     {"relation": "above", "distance_m": 0.125,
                      "frame": "world", "path_type": "linear"})
    assert target["type"] == "pose"
    assert target["position"] == pytest.approx({"x": 0.4, "y": 0.1, "z": 0.825})
    # Relative TCP moves preserve the live orientation; they must not acquire an
    # identity quaternion merely because the target is being converted to a pose.
    assert "orientation" not in target


# -- the home configuration ------------------------------------------------------------

def test_home_uses_the_scene_pose_the_runtime_captured() -> None:
    services = FakeServices(FakeRuntime(home=SCENE_POSE))
    goal = _resolve_goal_configuration(services, {}, "home", np.zeros(6))
    assert goal == pytest.approx(SCENE_POSE)


def test_home_holds_position_when_nothing_supplies_one() -> None:
    services = FakeServices(FakeRuntime())          # no home_configuration attribute
    seed = np.array([0.1, -1.2, 1.0, -0.5, -1.5, 0.2])
    goal = _resolve_goal_configuration(services, {}, "home", seed)
    # Holding position is safe; all-zeros is a fabricated pose that is in collision on the
    # real scene, with the arm horizontal and through the table.
    assert goal == pytest.approx(seed)
    assert not np.allclose(goal, np.zeros(6))


def test_home_does_not_alias_the_seed() -> None:
    services = FakeServices(FakeRuntime())
    seed = np.array([0.1, -1.2, 1.0, -0.5, -1.5, 0.2])
    goal = _resolve_goal_configuration(services, {}, "home", seed)
    goal[0] = 99.0
    assert seed[0] == pytest.approx(0.1)


def test_an_explicit_home_in_the_command_wins() -> None:
    services = FakeServices(FakeRuntime(home=SCENE_POSE))
    override = [0.0, -1.5, 1.0, -1.0, -1.5, 0.0]
    goal = _resolve_goal_configuration(services, {"home_configuration": override},
                                       "home", np.zeros(6))
    assert goal == pytest.approx(override)


def test_a_home_pose_outside_the_limits_is_clamped() -> None:
    services = FakeServices(FakeRuntime(home=np.full(6, 99.0)))
    goal = _resolve_goal_configuration(services, {}, "home", np.zeros(6))
    assert goal == pytest.approx(np.full(6, LIMITS[0, 1]))


def test_explicit_joint_values_take_priority_over_home() -> None:
    services = FakeServices(FakeRuntime(home=SCENE_POSE))
    wanted = [0.2, -1.0, 0.5, -0.5, -1.0, 0.1]
    goal = _resolve_goal_configuration(services, {"joint_configuration": wanted},
                                       "home", np.zeros(6))
    assert goal == pytest.approx(wanted)


def test_a_wrong_length_joint_configuration_is_rejected() -> None:
    services = FakeServices(FakeRuntime(home=SCENE_POSE))
    with pytest.raises(CommandFailure) as error:
        _resolve_goal_configuration(services, {"joint_configuration": [0.0, 0.0]},
                                    "home", np.zeros(6))
    assert error.value.error_code == "INVALID_REQUEST"
