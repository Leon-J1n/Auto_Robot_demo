import unittest
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from demo.scene.assembler import _flatten_world_transforms, SceneAssemblyError


class SceneImportQuaternionTest(unittest.TestCase):
    def test_root_normalizes_like_child_without_changing_rotation(self):
        source = [{'object_id_in_scene': 8, 'components': [
            {'component_type': 'Transform', 'properties': {
                'position': [0.4, 0.1, 0.6], 'parent': None,
                'quaternion_xyzw': [0, 0, -0.678907, -0.8766]}}]}]
        runtime = deepcopy(source)
        _flatten_world_transforms(runtime)
        original = source[0]['components'][0]['properties']
        actual = runtime[0]['components'][0]['properties']
        self.assertEqual(original['quaternion_xyzw'], [0, 0, -0.678907, -0.8766])
        self.assertAlmostEqual(np.linalg.norm(actual['quaternion_xyzw']), 1)
        np.testing.assert_allclose(Rotation.from_quat(actual['quaternion_xyzw']).as_matrix(),
                                   Rotation.from_quat(original['quaternion_xyzw']).as_matrix())
        self.assertEqual(actual['position'], original['position'])

    def test_invalid_quaternion_is_rejected(self):
        for value in ([0, 0, 0, 0], [0, 0, float('nan'), 1], [0, 0, 1]):
            objects = [{'object_id_in_scene': 8, 'components': [
                {'component_type': 'Transform', 'properties': {'quaternion_xyzw': value}}]}]
            with self.assertRaises(SceneAssemblyError):
                _flatten_world_transforms(objects)


if __name__ == '__main__':
    unittest.main()
