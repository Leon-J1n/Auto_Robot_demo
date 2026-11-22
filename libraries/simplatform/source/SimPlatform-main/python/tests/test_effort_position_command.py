from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sim_platform import Simulator
from sim_platform._control import effort_limit_values
from sim_platform.editor import EditorRobot
from sim_platform.live import RobotProxy
from sim_platform.server import HeadlessService


DATA = Path(__file__).resolve().parents[2] / "tests" / "data"


@pytest.mark.parametrize("limits", [[-1], [float("nan")], [float("inf")], [True], [], [1, 2]])
def test_effort_caps_reject_invalid_values(limits):
    with pytest.raises(ValueError):
        effort_limit_values(limits, 1)


@pytest.mark.parametrize("robot_type", [RobotProxy, EditorRobot])
def test_effort_caps_json_transport(robot_type):
    calls = []
    robot = SimpleNamespace(
        reference=SimpleNamespace(as_dict=lambda: {"id": "test"}),
        handle=1, joint_count=2,
        _session=SimpleNamespace(_request=lambda method, params: calls.append((method, params))),
    )
    robot_type.command_joint_positions(robot, [0, 0], effort_limits=[None, 0.75])
    assert calls[0][1]["effort_limits"] == [None, 0.75]
    with pytest.raises(ValueError):
        robot_type.command_joint_positions(robot, [0, 0], effort_limits=[1])
    assert len(calls) == 1


def test_position_effort_caps_persist_and_reset(tmp_path):
    with Simulator(cache_directory=tmp_path, physics="mujoco") as sim:
        world = sim.create_world(fixed_time_step=0.002)
        robot = world.load_robot(DATA / "two_link.urdf")
        robot.command_joint_positions([0.8], effort_limits=[0.0])
        sim.step(world, 30)
        assert abs(robot.joint_state[0][0]) < 1e-5
        # A malformed command must not replace the previously installed zero ceiling.
        with pytest.raises(Exception):
            robot.command_joint_positions([99.0], effort_limits=[None])
        robot.command_joint_positions([0.8])
        sim.step(world, 30)
        assert abs(robot.joint_state[0][0]) < 1e-5
        robot.command_joint_positions([0.8], effort_limits=[0.01])
        sim.step(world, 30)
        assert 0 < robot.joint_state[0][0] < 0.1
        robot.command_joint_positions([0.8], effort_limits=[None])
        sim.step(world, 30)
        assert robot.joint_state[0][0] > 0.2
        assert np.isfinite(robot.joint_state[0]).all()


def test_server_forwards_effort_caps_to_native_controller(tmp_path):
    service = HeadlessService(cache_directory=str(tmp_path), physics="mujoco")
    try:
        robot = service.world.load_robot(DATA / "two_link.urdf")
        service.robots[robot.handle] = robot
        reply = service.dispatch({"id": 1, "method": "robot.command_joint_positions", "params": {
            "robot": robot.handle, "targets": [0.8], "effort_limits": [0.0],
        }})
        assert reply["ok"], reply
        service.simulator.step(service.world, 30)
        assert abs(robot.joint_state[0][0]) < 1e-5
        reply = service.dispatch({"id": 2, "method": "robot.command_joint_positions", "params": {
            "robot": robot.handle, "targets": [0.8], "effort_limits": [None],
        }})
        assert reply["ok"], reply
        service.simulator.step(service.world, 30)
        assert robot.joint_state[0][0] > 0.2
    finally:
        service.close()
