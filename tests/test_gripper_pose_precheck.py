import unittest
from types import SimpleNamespace

import mujoco
import numpy as np

from demo.collision.grasp_collision import GraspCollisionChecker


class GripperPosePrecheckTests(unittest.TestCase):
    def setUp(self):
        self.model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <body name="robot_base">
            <freejoint name="robot_root"/><site name="tcp"/>
            <body name="robotiq_2f85_finger">
              <geom name="finger" type="sphere" size=".02"/>
            </body>
          </body>
          <body name="obstacle_body" pos=".05 0 0">
            <geom name="obstacle" type="box" size=".01 .1 .1"/>
          </body>
        </worldbody></mujoco>''')
        self.data = mujoco.MjData(self.model)
        mujoco.mj_kinematics(self.model, self.data)
        self.checker = GraspCollisionChecker.__new__(GraspCollisionChecker)
        self.checker.backend = SimpleNamespace(
            model=self.model, data=self.data,
            gripper_controller=SimpleNamespace(held_name=None),
            contact_target=None, contact_target_object_id=None,
            object_ids={}, resources=SimpleNamespace(scene={'objects': []}))
        self.checker.tcp_site_id = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_SITE, 'tcp')
        self.checker.tool_geoms = {mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_GEOM, 'finger')}
        obstacle = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, 'obstacle')
        self.checker.geoms = [*self.checker.tool_geoms, obstacle]
        self.checker.robot_bodies = {
            mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, 'robot_base'),
            mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, 'robotiq_2f85_finger')}
        self.checker.body_names = [mujoco.mj_id2name(
            self.model, mujoco.mjtObj.mjOBJ_BODY, body) or 'world'
            for body in range(self.model.nbody)]
        self.checker._tool_environment_pairs = np.asarray(
            [[next(iter(self.checker.tool_geoms)), obstacle]], dtype=int)
        self.checker.allow_gripper_environment_contact = False

    def test_known_tcp_pose_checks_gripper_against_environment_without_ik(self):
        clear = self.checker.check_gripper_pose(np.eye(4), safety_distance=.003)
        self.assertFalse(clear.in_collision)

        colliding_pose = np.eye(4)
        colliding_pose[0, 3] = .045
        collision = self.checker.check_gripper_pose(colliding_pose, safety_distance=.003)
        self.assertTrue(collision.in_collision)
        self.assertEqual(collision.contacts[0]['geom2'], 'obstacle_body')


if __name__ == '__main__':
    unittest.main()
