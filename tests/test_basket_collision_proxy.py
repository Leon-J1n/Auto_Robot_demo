import json
import unittest
from pathlib import Path

import numpy as np
import trimesh


class BasketCollisionProxyTests(unittest.TestCase):
    def test_scene_uses_small_open_proxy_without_changing_visual_mesh(self):
        root = Path(__file__).resolve().parents[1]
        scene = json.loads((root / 'assets/scenes/3/ur5_table_sorting.json').read_text(encoding='utf-8'))
        basket = next(obj for obj in scene['objects'] if obj['object_name_in_scene'] == 'Basket')
        collider = next(item['properties'] for item in basket['components']
                        if item['component_type'] == 'Collider')
        self.assertEqual(collider['collision_mesh'], 'models/12/basket_collision.obj')

        visual = trimesh.load(root / 'assets/models/12/basket.obj', force='mesh')
        proxy = trimesh.load(root / 'assets' / collider['collision_mesh'], force='mesh')
        np.testing.assert_allclose(proxy.bounds, visual.bounds, atol=1e-9)
        self.assertLess(len(proxy.faces), len(visual.faces) / 1000)
        self.assertFalse(proxy.contains(np.asarray([[0., 0., .1]])).item())
        self.assertTrue(proxy.contains(np.asarray([[.13, 0., .1]])).item())


if __name__ == '__main__':
    unittest.main()
