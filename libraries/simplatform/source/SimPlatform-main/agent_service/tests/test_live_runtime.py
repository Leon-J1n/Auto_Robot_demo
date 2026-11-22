"""Tests for how the runtime talks to the live world.

These pin which Live call each kind of motion uses, because the choice is not
cosmetic: a direct joint write costs no simulation steps but simulates nothing, so
using it where contact or a carried object matters would silently break the physics,
and using the controller where it does not matter costs a step burst per waypoint.

A fake session stands in for the platform; nothing here needs a simulator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
# The platform SDK is not installed in the dev tree; it lives beside the service.
sys.path.insert(0, str(_ROOT / "python"))

from agent_service.runtime.live_runtime import (END_EFFECTOR_LINK, UR5E_JOINT_NAMES,  # noqa: E402
                                                LiveSceneRuntime)

ARM = list(UR5E_JOINT_NAMES)
GRIPPER = ["robotiq_2f85_left_driver_joint", "robotiq_2f85_right_driver_joint",
           "robotiq_2f85_left_spring_link_joint", "robotiq_2f85_right_spring_link_joint",
           "robotiq_2f85_left_follower_joint", "robotiq_2f85_right_follower_joint"]
NAMES = sorted(ARM + GRIPPER + ["fixed_a", "fixed_b"])
LINKS = {"base_link": 10, "wrist_3_link": 11, END_EFFECTOR_LINK: 15}


class FakeLink:
    def __init__(self, handle: int) -> None:
        self.handle = handle

    @property
    def transform(self) -> dict[str, tuple[float, ...]]:
        return {"translation": (0.4, 0.0, 0.5), "rotation_wxyz": (1.0, 0.0, 0.0, 0.0),
                "scale": (1.0, 1.0, 1.0)}


class FakeSnapshot:
    revision = 1
    scene_generation = 1

    def __init__(self) -> None:
        self.entities = [
            {"id": 1, "name": "ur5e", "parent": None, "objectId": "obj1",
             "robot": {"jointNames": NAMES}},
            {"id": 10, "name": "base_link", "parent": 1, "objectId": "obj10"},
            {"id": 11, "name": "wrist_3_link", "parent": 10, "objectId": "obj11"},
            {"id": 15, "name": END_EFFECTOR_LINK, "parent": 11, "objectId": "obj15"},
        ]


class FakeRobot:
    handle = 1
    joint_names = tuple(NAMES)

    def __init__(self) -> None:
        self.positions = np.zeros(len(NAMES))
        self.commands: list[tuple[list[float], dict[str, Any]]] = []
        self.writes: list[tuple[list[float], str]] = []
        self.get_link_calls = 0
        self.list_links_calls = 0

    @property
    def joint_count(self) -> int:
        return len(NAMES)

    @property
    def joint_state(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (self.positions.copy(), np.zeros(len(NAMES)), np.zeros(len(NAMES)))

    @property
    def joint_limits(self) -> np.ndarray:
        return np.tile(np.array([-3.2, 3.2]), (len(NAMES), 1))

    def get_link(self, name: str) -> FakeLink:
        self.get_link_calls += 1
        if name not in LINKS:
            raise RuntimeError(f"no link {name}")
        return FakeLink(LINKS[name])

    def list_links(self) -> tuple[tuple[str, FakeLink], ...]:
        self.list_links_calls += 1
        return tuple((name, FakeLink(handle)) for name, handle in sorted(LINKS.items()))

    def command_joint_positions(self, targets: Any, **kwargs: Any) -> None:
        self.commands.append(([float(v) for v in targets], dict(kwargs)))
        self.positions = np.asarray(targets, dtype=float)

    def set_joint_positions(self, positions: Any, *, limits: str = "clamp") -> None:
        self.writes.append(([float(v) for v in positions], limits))
        self.positions = np.asarray(positions, dtype=float)


class LimitedGripperRobot(FakeRobot):
    @property
    def joint_limits(self) -> np.ndarray:
        limits = super().joint_limits
        left_driver = NAMES.index("robotiq_2f85_left_driver_joint")
        limits[left_driver] = (0.0, 0.8)
        return limits


class FakeRobotWithoutListLinks(FakeRobot):
    """A stand-in that only implements get_link, to exercise the fallback."""

    list_links = None  # type: ignore[assignment]


class FakeSession:
    endpoint = "tcp://127.0.0.1:1"

    def __init__(self, robot: FakeRobot) -> None:
        self.robot = robot
        self.steps = 0
        self.syncs = 0
        self.snapshots = 0

    def require_robot(self, alias: str) -> FakeRobot:
        return self.robot

    def scene_snapshot(self) -> FakeSnapshot:
        self.snapshots += 1
        return FakeSnapshot()

    def find_entity(self, object_id: str) -> FakeLink:
        return FakeLink(int(object_id[3:]))

    def step(self, count: int = 1) -> float:
        self.steps += count
        return 0.002 * self.steps

    def sync_physics(self) -> None:
        self.syncs += 1

    def sync_collision(self) -> None:
        self.syncs += 1


def _runtime(*, robot: FakeRobot | None = None, **kwargs: Any) -> LiveSceneRuntime:
    robot = robot or FakeRobot()
    return LiveSceneRuntime(FakeSession(robot), robot_alias="ur5e", **kwargs)


def _path(count: int = 5) -> list[list[float]]:
    return np.linspace(np.zeros(6), np.array([0.3, 0.0, 0.0, 0.0, 0.0, 0.0]),
                       count).tolist()


# -- servo cap -------------------------------------------------------------------

def test_no_cap_leaves_platform_defaults() -> None:
    runtime = _runtime()
    runtime.command_arm(np.zeros(6))
    assert runtime.robot.commands[-1][1] == {}


def test_arm_command_clamps_only_preserved_gripper_joint_targets() -> None:
    robot = LimitedGripperRobot()
    left_driver = NAMES.index("robotiq_2f85_left_driver_joint")
    robot.positions[left_driver] = 0.8000001
    runtime = _runtime(robot=robot)
    target = np.linspace(-0.5, 0.5, len(ARM))

    runtime.command_arm(target)

    commanded = np.asarray(robot.commands[-1][0])
    assert commanded[runtime.qpos_indices] == pytest.approx(target)
    assert commanded[left_driver] == pytest.approx(0.8)


def test_grip_force_reaches_servo_and_arm_motion_preserves_deflected_target():
    from agent_service.runtime.gripper import LiveGripperController
    runtime = _runtime()
    gripper = runtime.gripper_controller = LiveGripperController(runtime)
    gripper._set_force(20.)
    gripper._command_angle(.4)
    targets, tuning = runtime.robot.commands[-1]
    limits = tuning["effort_limits"]
    for index in gripper._coupled_indices:
        assert limits[index] == pytest.approx(20. * .5 * .085 / .8 / 6)
    assert all(limits[index] is None for index in runtime.qpos_indices)
    # Contact stalls the fingers short of the commanded closure.
    runtime.robot.positions[gripper._coupled_indices] = .3 * gripper._multipliers
    runtime.invalidate()
    runtime.command_arm(np.ones(6) * .1)
    targets, tuning = runtime.robot.commands[-1]
    assert np.allclose(np.asarray(targets)[gripper._coupled_indices], .4 * gripper._multipliers)
    gripper.hold(10.)
    assert runtime.robot.commands[-1][1]["effort_limits"][gripper.driver_indices[0]] == pytest.approx(limits[gripper.driver_indices[0]] / 2)
    assert np.allclose(np.asarray(runtime.robot.commands[-1][0])[gripper._coupled_indices], .4 * gripper._multipliers)
    gripper._target_entity = lambda name: None
    gripper.open(.085)
    assert gripper._commanded_angle == 0.
    runtime.command_arm(np.zeros(6))
    assert np.allclose(np.asarray(runtime.robot.commands[-1][0])[gripper._coupled_indices], 0.)


def test_requested_cap_reaches_the_controller_in_radians() -> None:
    runtime = _runtime(max_joint_speed_deg=90.0)
    runtime.command_arm(np.zeros(6))
    kwargs = runtime.robot.commands[-1][1]
    assert set(kwargs) == {"velocity_limit"}
    assert kwargs["velocity_limit"] == pytest.approx(np.radians(90.0))


def test_the_cap_is_applied_to_every_actuator_waypoint() -> None:
    runtime = _runtime(execution_mode="actuator", max_joint_speed_deg=45.0)
    result = runtime.execute_trajectory({"waypoints": _path(), "duration": 0.0})
    assert len(runtime.robot.commands) == 4
    assert all(item[1]["velocity_limit"] == pytest.approx(np.radians(45.0))
               for item in runtime.robot.commands)
    assert result["servo_limit_applied"] is True


def test_a_cap_is_not_claimed_for_a_kinematic_replay() -> None:
    # A direct write has no servo, so reporting a cap as applied would be a false claim.
    runtime = _runtime(execution_mode="kinematic", max_joint_speed_deg=45.0)
    result = runtime.execute_trajectory({"waypoints": _path(), "duration": 0.0})
    assert result["servo_limit_applied"] is False
    assert runtime.robot.commands == []


def test_a_non_positive_cap_is_rejected() -> None:
    with pytest.raises(ValueError):
        _runtime(max_joint_speed_deg=0.0)


# -- which call each mode uses ---------------------------------------------------

def test_kinematic_replay_writes_directly_and_never_steps() -> None:
    runtime = _runtime(execution_mode="kinematic")
    result = runtime.execute_trajectory({"waypoints": _path(), "duration": 0.0})
    assert len(runtime.robot.writes) == 4
    assert runtime.robot.commands == []
    # No clock advance: the replay shows the plan rather than simulating it.
    assert runtime.session.steps == 0
    assert result["mode"] == "kinematic"
    assert np.allclose(result["final_joint_state"], _path()[-1])


def test_kinematic_replay_syncs_the_backends_once_at_the_end() -> None:
    runtime = _runtime(execution_mode="kinematic")
    runtime.execute_trajectory({"waypoints": _path(9), "duration": 0.0})
    # Direct writes leave stale geometry behind; one sync covers the whole replay
    # (sync_physics + sync_collision counted separately by the fake).
    assert runtime.session.syncs == 2


def test_kinematic_replay_observes_every_executed_waypoint() -> None:
    runtime = _runtime(execution_mode="kinematic")
    observed: list[int] = []
    runtime.waypoint_observer = lambda: observed.append(len(observed))
    runtime.execute_trajectory({"waypoints": _path(9), "duration": 0.0})
    assert len(observed) == 8


def test_kinematic_replay_does_not_timeout_on_rpc_wall_time() -> None:
    # Kinematic execution is a series of direct pose writes; a small timeout must not
    # reject a valid path solely because the live RPCs take longer than simulated time.
    runtime = _runtime(execution_mode="kinematic")
    result = runtime.execute_trajectory({"waypoints": _path(9), "duration": 0.0}, timeout=0.0)
    assert result["mode"] == "kinematic"


def test_actuator_mode_commands_and_steps() -> None:
    runtime = _runtime(execution_mode="actuator")
    result = runtime.execute_trajectory({"waypoints": _path(), "duration": 0.0})
    assert len(runtime.robot.commands) == 4
    assert runtime.robot.writes == []
    # Contact and carried objects only behave if the world actually advances.
    assert runtime.session.steps > 0
    assert result["mode"] == "actuator"


def test_actuator_mode_batches_live_step_requests() -> None:
    runtime = _runtime(execution_mode="actuator")
    runtime.execute_trajectory({"waypoints": _path(), "duration": 0.0,
                                "steps_per_waypoint": 25})
    # The fake controller reaches each target immediately, so one batch is enough per
    # waypoint; importantly, the simulator still receives the requested step count.
    assert runtime.session.steps == 4 * 10


def test_timed_actuator_consumes_schedule_without_per_sample_settling():
    runtime = _runtime(execution_mode="actuator")
    runtime.execute_trajectory({"waypoints": _path(101), "duration": 1.,
                                "time_from_start": np.linspace(0, 1, 101)}, timeout=0.)
    assert runtime.session.steps == 500
    assert len(runtime.robot.commands) == 500
    positions = np.asarray([command[0] for command in runtime.robot.commands])
    # The 10 ms waypoints are interpolated into smooth 2 ms servo commands.
    assert np.diff(positions[:, runtime.qpos_indices[0]]) == pytest.approx(
        np.full(499, .3 / 500))


def test_timed_actuator_stalled_final_servo_is_bounded():
    runtime = _runtime(execution_mode="actuator")
    runtime.robot.command_joint_positions = lambda *args, **kwargs: None
    with pytest.raises(TimeoutError, match="final convergence"):
        runtime.execute_trajectory({"waypoints": _path(3), "duration": .02,
                                    "time_from_start": [0, .01, .02]}, timeout=.02)
    assert runtime.session.steps == 20


def test_timed_segment_batches_rpc_and_preserves_gripper_effort():
    runtime = _runtime(execution_mode="actuator")
    from agent_service.runtime.gripper import LiveGripperController
    gripper = runtime.gripper_controller = LiveGripperController(runtime)
    gripper._set_force(40.)
    gripper._commanded_angle = .4
    calls = []
    def segment(start, target, steps, **tuning):
        calls.append((start, target, steps, tuning))
        runtime.robot.positions = np.asarray(target)
        runtime.session.steps += steps
    runtime.robot.execute_joint_segment = segment
    runtime.execute_trajectory({"waypoints": _path(3), "time_from_start": [0, .01, .02]})
    assert len(calls) == 2
    assert runtime.session.steps == 10
    for start, target, steps, tuning in calls:
        assert steps == 5
        assert np.asarray(start)[gripper._coupled_indices] == pytest.approx(.4 * gripper._multipliers)
        assert np.asarray(target)[gripper._coupled_indices] == pytest.approx(.4 * gripper._multipliers)
        assert tuning["effort_limits"] == gripper.joint_effort_limits()


def test_set_arm_positions_leaves_the_gripper_alone() -> None:
    runtime = _runtime()
    gripper_index = runtime.platform_joint_names.index(
        "robotiq_2f85_left_driver_joint")
    runtime.robot.positions[gripper_index] = 0.4
    runtime.invalidate()
    runtime.set_arm_positions(np.full(6, 0.2))
    written = runtime.robot.writes[-1][0]
    assert written[gripper_index] == pytest.approx(0.4)
    assert [written[i] for i in runtime.qpos_indices] == pytest.approx([0.2] * 6)


def test_direct_writes_clamp_out_of_range_targets_by_default() -> None:
    # Iterative solvers want a target pulled into range, not an exception.
    runtime = _runtime()
    runtime.set_arm_positions(np.zeros(6))
    assert runtime.robot.writes[-1][1] == "clamp"


def test_a_write_invalidates_the_cached_joint_state() -> None:
    runtime = _runtime()
    assert np.allclose(runtime.get_joint_positions(), np.zeros(6))
    runtime.set_arm_positions(np.full(6, 0.25))
    assert np.allclose(runtime.get_joint_positions(), np.full(6, 0.25))


# -- link resolution -------------------------------------------------------------

def test_links_are_listed_in_one_request() -> None:
    runtime = _runtime()
    # One list_links for the index; get_link is still used for the named end effector.
    assert runtime.robot.list_links_calls == 1
    assert runtime.robot.get_link_calls == 1
    assert set(runtime.scene.link_names()) == set(LINKS)


def test_link_resolution_falls_back_to_probing_names() -> None:
    runtime = _runtime(robot=FakeRobotWithoutListLinks())
    assert runtime.robot.list_links_calls == 0
    assert runtime.robot.get_link_calls > 1
    assert set(runtime.scene.link_names()) == set(LINKS)


def test_link_proxies_are_cached_across_lookups() -> None:
    runtime = _runtime()
    before = runtime.robot.get_link_calls
    for _ in range(5):
        runtime.link(END_EFFECTOR_LINK)
    assert runtime.robot.get_link_calls == before
