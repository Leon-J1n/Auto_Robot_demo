"""Global collision defaults must reach demo requests and both RRT stages."""
from copy import deepcopy
import unittest

from execution.config import load_global_config
from execution.resources.resolver import apply_default_scene_settings
from planning import CollisionFreePlanner
from planning.constraints.collision_sampling import collision_sampling_settings


class GlobalCollisionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.config = deepcopy(load_global_config())
        self.config['planning']['collision_validation']['resolution_rad'] = .017

    def test_global_resolution_reaches_empty_and_partial_scene_defaults(self):
        for scene in ({}, {'move_defaults': {'planning': {
                'collision_validation': {'spline_subdivisions': 3}}}}):
            with self.subTest(scene=scene):
                result = apply_default_scene_settings(scene, self.config)
                self.assertEqual(collision_sampling_settings(result['move_defaults'])[0], .017)
                self.assertNotIn('resolution_rad', scene.get('move_defaults', {}).get(
                    'planning', {}).get('collision_validation', {}))

    def test_explicit_scene_and_request_resolution_are_preserved(self):
        scene = {'move_defaults': {'planning': {
            'collision_validation': {'resolution_rad': .009}}}}
        result = apply_default_scene_settings(scene, self.config)
        self.assertEqual(collision_sampling_settings(result['move_defaults'])[0], .009)

    def test_current_global_values_match_rrt_and_final_validation(self):
        scene = apply_default_scene_settings({})
        planner = CollisionFreePlanner(joint_limits=[[-1., 1.]] * 6)
        resolution = collision_sampling_settings(scene['move_defaults'])[0]
        self.assertEqual(resolution, load_global_config()['planning']['collision_validation']['resolution_rad'])
        self.assertEqual(planner.rrt_resolution_rad, resolution)

    def test_proxy_is_enabled_by_default_and_scene_overrides_are_preserved(self):
        default = apply_default_scene_settings({})['planning_collision_proxy']
        self.assertTrue(default['enabled'])
        self.assertEqual(default['sphere_cell_size'], .035)
        scene = {'planning_collision_proxy': {'enabled': False, 'batch_size': 16}}
        resolved = apply_default_scene_settings(scene)['planning_collision_proxy']
        self.assertFalse(resolved['enabled'])
        self.assertEqual(resolved['batch_size'], 16)
        self.assertEqual(resolved['sphere_cell_size'], .035)
        resolved['container_names'].append('AnotherContainer')
        self.assertEqual(apply_default_scene_settings({})['planning_collision_proxy']['container_names'],
                         ['Tray', 'Basket'])


if __name__ == '__main__':
    unittest.main()
