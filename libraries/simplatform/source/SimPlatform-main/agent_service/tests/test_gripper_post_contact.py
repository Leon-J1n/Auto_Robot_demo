from __future__ import annotations

import time

import numpy as np
import pytest

from agent_service.runtime.gripper import LiveGripperController
from agent_service.skills.manipulation.grasp.strategies.default_grasp.default_grasp import (
    DefaultGrasp,
)


class _Runtime:
    def __init__(self) -> None:
        self.positions = np.zeros(1, dtype=float)
        self.steps = 0
        self.commands = []
        self.writes = []

    def set_full_positions(self, positions) -> None:
        self.writes.append(positions)
        self.positions = np.asarray(positions, dtype=float)

    def full_joint_positions(self):
        return self.positions.copy()

    def command_full(self, _positions) -> None:
        self.commands.append(np.asarray(_positions, dtype=float).copy())

    def step(self) -> None:
        self.steps += 1
        if self.commands:
            self.positions = self.commands[-1].copy()

    def sync_backends(self) -> None:
        pass

    def invalidate(self) -> None:
        pass


def _controller() -> LiveGripperController:
    controller = object.__new__(LiveGripperController)
    controller.runtime = _Runtime()
    controller.driver_indices = np.array([0])
    controller._coupled_indices = np.array([0])
    controller._multipliers = np.array([1.0])
    controller.closure_steps = 20
    controller.closure_tolerance = 1e-6
    return controller


def test_contact_closure_holds_for_configured_minimum_steps() -> None:
    controller = _controller()

    controller._drive_to(
        0.25, timeout=1.0, started=time.perf_counter(), settle=True,
        minimum_settle_steps=5)

    assert controller.runtime.steps == 5


def test_contact_closure_uses_half_second_full_stroke_at_two_ms_dt() -> None:
    controller = _controller()

    controller._drive_to(
        0.25, timeout=2.0, started=time.perf_counter(), settle=False,
        motion_duration=0.5)

    assert controller.runtime.steps == 251
    assert len(controller.runtime.commands) == 251
    assert not controller.runtime.writes
    assert controller.runtime.commands[0][0] == pytest.approx(0.001)
    assert controller.runtime.commands[-1][0] == pytest.approx(0.25)


@pytest.mark.parametrize("post_steps", [0, 3, 10, 50])
def test_contact_closure_continues_at_same_speed_for_post_contact_interval(post_steps) -> None:
    controller = _controller()
    controller._angle_bounds = (0.0, controller.driver_range)
    controller._driver_angle = lambda: 0.0
    controller._target_colliders = lambda _target: set()
    controller._live_contact_state = lambda _target: (controller.runtime.steps >= 125, False)
    controller._move(0.0, speed=0.5, timeout=1.0,
                     target_object_id="ball", stop_on_contact=True,
                     post_contact_steps=post_steps)

    assert controller.runtime.steps == 125 + post_steps
    angles = np.array(controller.runtime.commands)[:, 0]
    assert np.diff(angles) == pytest.approx(np.full(len(angles) - 1, .0032))
    assert angles[-1] == pytest.approx(.4 + post_steps * .0032)
    assert not controller.runtime.writes


def test_transient_bilateral_contact_does_not_finish_closure():
    controller = _controller()
    controller._angle_bounds = (0., .8)
    controller._target_colliders = lambda _target: set()
    controller._live_contact_state = lambda _target: (
        controller.runtime.steps == 5 or controller.runtime.steps >= 10, False)
    result = controller._move(0., speed=.5, timeout=1., target_object_id="ball",
                              stop_on_contact=True, post_contact_steps=3)
    assert controller.runtime.steps == 13
    assert result["contact_detected"]


def test_live_grasp_uses_loaded_physics_contacts_not_triangle_overlap():
    from types import SimpleNamespace
    controller = _controller()
    controller._pad_side = lambda entity: {1: "left", 2: "right"}.get(entity)
    controller.scene = SimpleNamespace(link_of=lambda entity: None)
    contacts = [
        {"first": 1, "second": 3, "distance": -.000002, "normal_force": 2.7},
        {"first": 3, "second": 2, "distance": -.000002, "normal_force": 2.8},
    ]
    controller.runtime.session = SimpleNamespace(physics_contacts=lambda: contacts)
    assert controller._live_contact_state({3}) == (True, False)
    # A geometric near-contact with no normal force is not a physical grasp.
    contacts[1]["normal_force"] = 0.
    assert controller._live_contact_state({3}) == (False, False)
    assert controller._live_contact_state({4}) == (False, False)


def test_distal_finger_contacts_count_but_base_and_drivers_do_not():
    from types import SimpleNamespace
    controller = _controller()
    controller._pad_side = lambda entity: None
    names = {1: "robotiq_2f85_left_spring_link", 2: "robotiq_2f85_right_follower",
             3: "robotiq_2f85_base", 4: "robotiq_2f85_left_driver", 5: "other_left_follower"}
    controller.scene = SimpleNamespace(link_of=names.get)
    assert [controller._finger_side(i) for i in range(1, 6)] == ["left", "right", None, None, None]


def test_blocked_fingers_keep_preload_target_without_direct_position_write():
    controller = _controller()
    def blocked_step():
        controller.runtime.steps += 1
        controller.runtime.positions[:] = .2
    controller.runtime.step = blocked_step
    controller._drive_to(.3, timeout=1., started=time.perf_counter(), settle=True)
    assert controller.runtime.positions[0] == .2
    assert controller.runtime.commands[-1][0] == .3
    assert controller._commanded_angle == .3
    assert not controller.runtime.writes


@pytest.mark.parametrize("force", [-1, 41, float("nan"), float("inf"), True])
def test_invalid_force_rejected_before_motion(force):
    controller = _controller()
    with pytest.raises(ValueError, match="force"):
        controller.close(0., force=force)
    assert not controller.runtime.commands


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_post_contact_steps_must_be_a_non_negative_integer(value) -> None:
    controller = _controller()

    with pytest.raises(ValueError, match="post_contact_steps"):
        controller.close(0.0, post_contact_steps=value)


class _RecordingGripper:
    def __init__(self) -> None:
        self.close_kwargs = None

    def open(self, *_args, **_kwargs) -> None:
        pass

    def close(self, *_args, **kwargs):
        self.close_kwargs = kwargs
        return {"contact_detected": False}

    def get_state(self, _object_id):
        return {
            "contact_detected": False,
            "width": 0.0,
            "force": 0.0,
            "holding": False,
            "object_present": False,
            "slip_detected": False,
        }


@pytest.mark.parametrize("strategy_params, expected", [({}, 20), ({"post_contact_steps": 9}, 9)])
def test_default_grasp_forwards_post_contact_steps(strategy_params, expected) -> None:
    controller = _RecordingGripper()
    request = {
        "target": {"object_id": "banana_01"},
        "grasp": {"operation": "grasp"},
        "constraints": {"timeout": 1.0, "hold_on_success": False},
        "strategy_params": strategy_params,
    }

    DefaultGrasp().execute(
        request,
        {"gripper_controller": controller, "runtime": type("Runtime", (), {"realtime": False})()},
    )

    assert controller.close_kwargs["post_contact_steps"] == expected


def test_default_grasp_settles_physics_after_switching_to_hold_force():
    controller = _RecordingGripper()
    events = []
    controller.close = lambda *_args, **_kwargs: {"contact_detected": True}
    controller.hold = lambda force: events.append(("hold", force))
    controller.settle = lambda seconds: events.append(("settle", seconds))
    request = {"target": {"object_id": "ball"}, "grasp": {"operation": "grasp"},
               "constraints": {"timeout": 1., "hold_on_success": True},
               "strategy_params": {"hold_force": 10., "settle_time": .3}}
    DefaultGrasp().execute(request, {"gripper_controller": controller,
                                    "runtime": type("Runtime", (), {"realtime": False})()})
    assert events == [("hold", 10.), ("settle", .3)]


def test_live_settle_advances_simulation_without_unloading_fingers():
    controller = _controller()
    controller._commanded_angle = .3
    controller.runtime.positions[:] = .2
    controller.settle(.02)
    assert controller.runtime.steps == 10
    assert controller.runtime.commands[-1][0] == .3
    assert not controller.runtime.writes


def test_release_directly_stages_open_pose_before_physics_settle():
    controller = _controller()
    controller._angle_bounds = (0.0, controller.driver_range)
    controller._clamped_entity = 1

    result = controller.open(controller.maximum_width)

    assert result["contact_detected"] is False
    assert controller.runtime.steps == controller.release_settle_steps
    assert controller.runtime.writes
    assert controller.runtime.writes[0][0] == pytest.approx(0.0)
    assert controller._commanded_angle == pytest.approx(0.0)
