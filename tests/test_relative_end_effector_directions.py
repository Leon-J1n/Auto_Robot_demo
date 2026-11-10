import unittest

from scipy.spatial.transform import Rotation

from demo.task_commands.converter import SkillCommandConverter


class RelativeEndEffectorDirectionsTest(unittest.TestCase):
    def test_directions_follow_robot_base_rotation(self):
        rotation = Rotation.from_euler('z', 90, degrees=True).as_matrix()
        expected = {
            'front': (0, 1, 0),
            'behind': (0, -1, 0),
            'left': (-1, 0, 0),
            'right': (1, 0, 0),
            'below': (0, 0, -1),
        }
        for relation, displacement in expected.items():
            with self.subTest(relation=relation):
                converter = SkillCommandConverter(
                    {'scene': 'test', 'objects': {}}, end_effector_rotation=rotation)
                converter._last_pose = {
                    'type': 'pose', 'frame': 'world',
                    'position': {'x': 0, 'y': 0, 'z': 0},
                }
                result = converter.convert_command({
                    'skill_name': 'move_to',
                    'parameters': {'target': 'end_effector', 'relation': relation, 'distance_m': 1},
                })
                position = result['request']['target']['position']
                for axis, value in zip('xyz', displacement):
                    self.assertAlmostEqual(position[axis], value)


if __name__ == '__main__':
    unittest.main()
