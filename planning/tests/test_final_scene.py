"""Final scene preserves authoring structure and measured joint/pose state."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

import mujoco

from demo.final_scene import final_scene
from execution.resources import component


class FinalSceneTests(unittest.TestCase):
    def test_measured_poses_joints_and_attachment_structure(self):
        model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody>
          <body name="arm"><joint name="arm_joint"/><geom size=".1"/>
            <body name="tool"><joint name="finger_joint"/><geom size=".01"/></body>
          </body>
          <body name="Cabinet"><joint name="Cabinet__slide" type="slide"/><geom size=".1"/></body>
        </worldbody></mujoco>''')
        data = mujoco.MjData(model); data.qpos[:] = [.3, .4, .2]
        def obj(ident, name, joints=None, parent=None, attachment=False):
            components = [{'component_type': 'Transform', 'properties': {
                'position': [0., 0., 0.], 'quaternion_xyzw': [0., 0., 0., 1.],
                'scale': [1., 1., 1.], 'parent': parent}}]
            if joints:
                components.append({'component_type': 'RobotDriver', 'properties': {'joint_sequence': joints}})
            if attachment:
                components.append({'component_type': 'RobotAttachment', 'properties': {'parent_object_id': 1}})
            return {'object_id_in_scene': ident, 'object_name_in_scene': name, 'components': components}
        source = {'scene_version': '1.0', 'objects': [obj(1, 'Robot', ['arm_joint']),
            obj(2, 'Gripper', ['finger_joint'], parent=1, attachment=True),
            obj(3, 'Cabinet', ['slide']), obj(4, 'Apple'), obj(5, 'Child', parent=4)]}
        original = deepcopy(source)
        poses = {name: {'position': position, 'quaternion_xyzw': [0., 0., 0., 1.]}
                 for name, position in [('Robot', [1., 0., 0.]), ('Cabinet', [0., 2., 0.]),
                                        ('Apple', [3., 0., 1.]), ('Child', [3., 0., 1.5])]}
        backend = SimpleNamespace(model=model, data=data, resources=SimpleNamespace(robot_object=source['objects'][0]))
        exported = final_scene(source, backend, {'objects': poses})
        self.assertEqual(source, original)
        self.assertEqual(component(exported['objects'][3], 'Transform')['position'], [3., 0., 1.])
        self.assertEqual(component(exported['objects'][4], 'Transform')['position'], [0., 0., .5])
        self.assertEqual(component(exported['objects'][1], 'Transform'), component(source['objects'][1], 'Transform'))
        for index, joint, expected in ((0, 'arm_joint', .3), (1, 'finger_joint', .4), (2, 'slide', .2)):
            driver = component(exported['objects'][index], 'RobotDriver')
            self.assertEqual(driver['now_joint_positions_rad'][joint], expected)
            self.assertNotIn('initial_joint_positions_rad', driver)
            self.assertEqual(driver['now_joint_positions_rad'][joint], expected)


if __name__ == '__main__':
    unittest.main()
