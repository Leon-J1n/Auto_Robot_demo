import mujoco
import pytest
from execution.backends.mujoco.joint_restore import restore_dependent_joints
from execution.backends.mujoco.gripper_controller import BindingGripperController


def test_restore_dependency_without_overwriting_measured_joints():
    model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
      <body><joint name="master"/><geom size=".1"/></body>
      <body pos="1 0 0"><joint name="child"/><geom size=".1"/></body>
      </worldbody><equality><joint joint1="child" joint2="master" polycoef="0 2 0 0 0"/></equality></mujoco>''')
    data = mujoco.MjData(model)
    data.qpos[0] = .2
    restore_dependent_joints(model, data, {0})
    assert data.qpos[1] == pytest.approx(.4)
    data.qpos[1] = .39
    restore_dependent_joints(model, data, {0, 1})
    assert data.qpos[1] == .39
    assert data.time == 0


def test_gripper_recognizes_free_payload_with_numpy_joint_type():
    model = mujoco.MjModel.from_xml_string('<mujoco><worldbody><body><freejoint/><geom size=".1"/></body></worldbody></mujoco>')
    controller = BindingGripperController.__new__(BindingGripperController)
    controller.model = model
    assert controller._movable_body(1)
