from types import SimpleNamespace
import mujoco
import numpy as np

from kinematics.models.mujoco import MujocoRobotModel


def test_fk_and_jacobian_do_not_solve_contacts(monkeypatch):
    model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
      <geom type="sphere" size=".3"/>
      <body><joint name="joint" type="hinge" axis="0 1 0" range="-90 90"/>
      <geom type="capsule" size=".1" fromto="0 0 0 0 0 1"/>
      <site name="tip" pos="0 0 1"/></body></worldbody></mujoco>''')
    data = mujoco.MjData(model)
    runtime = SimpleNamespace(model=model, data=data, end_effector_site_id=0,
                              qpos_indices=[0], dof_indices=[0], joint_ids=[0],
                              joint_limits=model.jnt_range.copy())
    robot = MujocoRobotModel(runtime)
    data.qpos[0] = .4
    mujoco.mj_forward(model, data)
    expected_pos = data.site_xpos[0].copy()
    expected_rot = data.site_xmat[0].reshape(3, 3).copy()
    jp, jr = np.zeros((3, model.nv)), np.zeros((3, model.nv))
    mujoco.mj_jacSite(model, data, jp, jr, 0)
    assert data.ncon > 0
    def forbidden(*args):
        raise AssertionError('IK must not build or solve contact constraints')
    monkeypatch.setattr(mujoco, 'mj_forward', forbidden)
    pose = robot.forward([.4])
    np.testing.assert_allclose(pose['position'], expected_pos)
    np.testing.assert_allclose(pose['rotation_matrix'], expected_rot)
    np.testing.assert_allclose(robot.jacobian([.4]), np.vstack((jp, jr)))
    assert robot._work_data.ncon == 0
