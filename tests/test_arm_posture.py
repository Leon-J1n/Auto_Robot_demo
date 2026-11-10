"""Raised-arm preferences use world geometry and remain softer than feasibility."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import mujoco
import numpy as np

from planning.arm_posture import ArmPosturePreference, configuration_cost, preference_for
from planning.collision_free_planner import CollisionFreePlanner
from atomic_action.motion.common.action import _MoveExecutor
from demo.interaction.object_library import ObjectLibraryGraspResolver


class ArmPostureTests(unittest.TestCase):
    def setUp(self):
        self.model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <geom name="WorkTable" type="box" size="1 1 .05" pos="0 0 .55"/>
          <body pos="0 0 .6"><joint axis="0 0 1"/><geom size=".03"/>
            <body><joint axis="0 1 0"/><geom size=".03"/>
              <body pos=".3 0 0"><joint axis="0 1 0"/><geom size=".03"/>
                <body pos=".3 0 0"><joint axis="0 1 0"/><geom size=".03"/>
                  <site name="tcp"/>
                </body>
              </body>
            </body>
          </body>
        </worldbody></mujoco>''')
        self.data = mujoco.MjData(self.model)
        mujoco.mj_kinematics(self.model, self.data)
        self.runtime = SimpleNamespace(model=self.model, data=self.data,
            joint_ids=np.arange(4), qpos_indices=np.arange(4))
        config = ArmPosturePreference(self.runtime).config
        self.posture = ArmPosturePreference(self.runtime, {**config, 'enabled': True})
        self.up = np.array([0., -np.pi / 3, 2 * np.pi / 3, -np.pi / 3])
        self.down = -self.up
        self.seed = self.down * .1
        self.kin = SimpleNamespace(arm_posture=self.posture)

    def test_same_tcp_pose_prefers_raised_elbow_without_changing_live_physics(self):
        qpos, time = self.data.qpos.copy(), self.data.time
        up = self.posture.metrics(self.up)
        up_position = self.posture.data.site_xpos[0].copy()
        up_rotation = self.posture.data.site_xmat[0].copy()
        down = self.posture.metrics(self.down)
        np.testing.assert_allclose(up_position, self.posture.data.site_xpos[0], atol=1e-12)
        np.testing.assert_allclose(up_rotation, self.posture.data.site_xmat[0], atol=1e-12)
        self.assertGreater(up['elbow_height_m'], down['elbow_height_m'] + .5)
        self.assertGreater(up['sampled_arm_lowest_z_m'], down['sampled_arm_lowest_z_m'])
        self.assertLess(up['penalty_rad'], down['penalty_rad'])
        self.assertAlmostEqual(up['table_height_m'], .6)
        self.assertLess(configuration_cost(self.kin, self.up, self.seed),
                        configuration_cost(self.kin, self.down, self.seed))
        np.testing.assert_array_equal(qpos, self.data.qpos)
        self.assertEqual(time, self.data.time)

    def test_path_penalty_is_independent_of_waypoint_density(self):
        sparse = np.array([self.seed, self.up])
        dense = np.array([self.seed + alpha * (self.up - self.seed) for alpha in np.linspace(0, 1, 101)])
        self.assertAlmostEqual(self.posture.path_penalty(sparse), self.posture.path_penalty(dense), places=12)

    def make_search(self, collide_up=False):
        e = _MoveExecutor.__new__(_MoveExecutor)
        e.kinematics = self.kin
        e.collision_checker = SimpleNamespace(check=Mock(side_effect=lambda q, **kw: SimpleNamespace(
            in_collision=collide_up and np.allclose(q, self.up), contacts=[])),
            check_path=Mock(return_value=SimpleNamespace(in_collision=False)))
        e.direct_planner = SimpleNamespace(plan=Mock(side_effect=lambda path, *args: {
            'waypoints': np.vstack((path['start'], path['goal'])).tolist()}))
        e.collision_free_planner = SimpleNamespace(plan=Mock())
        request = {'planning': {'planning_time': 5., 'mode': 'collision_free', 'allow_replan': True},
            'motion': {'path_constraint': 'soft'}, 'constraints': {'avoid_collision': True}}
        path = {'path_type': 'joint', 'start': self.seed, 'candidate_goals': [self.down, self.up]}
        return e, request, path

    def test_joint_path_selection_can_choose_longer_raised_configuration(self):
        e, request, path = self.make_search()
        result = e._plan_joint_candidates(request, {}, {}, path)
        np.testing.assert_allclose(result['waypoints'][-1], self.up)
        self.assertEqual(result['diagnostics']['configuration_selection']['objective'], 'joint_travel_plus_arm_posture')

    def test_colliding_raised_configuration_falls_back_to_lower_arm(self):
        e, request, path = self.make_search(collide_up=True)
        result = e._plan_joint_candidates(request, {}, {}, path)
        np.testing.assert_allclose(result['waypoints'][-1], self.down)

    def test_resolver_prefers_raised_ik_but_preserves_actual_joint_travel(self):
        self.kin.solve_all = Mock(return_value=SimpleNamespace(solutions=[self.down, self.up], solver='test'))
        checker = SimpleNamespace(check=lambda *args, **kw: SimpleNamespace(in_collision=False))
        resolved = {'candidates': [{'grasp_index': 0, 'world_from_control': np.eye(4).tolist()}]}
        result = ObjectLibraryGraspResolver.select(resolved, self.kin, checker, self.seed, grasp_index=0)
        np.testing.assert_allclose(result['joint_positions'], self.up)
        self.assertAlmostEqual(result['joint_travel_rad'], np.linalg.norm(self.up - self.seed))
        self.assertIn('elbow_height_m', result['arm_posture'])

    def test_disabled_preference_restores_nearest_solution(self):
        self.kin.arm_posture = ArmPosturePreference(self.runtime)
        self.assertFalse(self.kin.arm_posture.enabled)
        self.assertIsNone(preference_for(self.kin))
        self.assertIsNone(self.kin.arm_posture.data)
        self.assertLess(configuration_cost(self.kin, self.down, self.seed),
                        configuration_cost(self.kin, self.up, self.seed))
        e, request, path = self.make_search()
        result = e._plan_joint_candidates(request, {}, {}, path)
        np.testing.assert_allclose(result['waypoints'][-1], self.down)
        self.assertEqual(result['diagnostics']['configuration_selection']['objective'], 'total_joint_travel_rad')

    def test_rrt_retains_goal_bias_unbiased_sampling_and_preferred_sampling(self):
        planner = CollisionFreePlanner.__new__(CollisionFreePlanner)
        planner.kinematics = self.kin
        planner.joint_limits = np.array([[-3., 3.]] * 4)
        rng = Mock()
        rng.random.return_value = .1
        np.testing.assert_array_equal(planner._sample_joint_configuration(self.up, rng), self.up)
        rng.uniform.assert_not_called()
        rng.random.side_effect = [.5, .9]
        rng.uniform.return_value = self.down
        np.testing.assert_array_equal(planner._sample_joint_configuration(self.up, rng), self.down)
        rng.uniform.reset_mock()
        rng.random.side_effect = [.5, .1]
        rng.uniform.side_effect = [self.down, self.up, self.down]
        np.testing.assert_array_equal(planner._sample_joint_configuration(self.up, rng), self.up)
        self.assertEqual(rng.uniform.call_count, 3)


if __name__ == '__main__':
    unittest.main()
