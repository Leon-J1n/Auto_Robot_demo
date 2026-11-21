"""Regression tests for OLS serial-arm joint mapping and switching."""
from pathlib import Path
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace
import unittest

import numpy as np

from collision.backends.simplatform_fcl import SimPlatformFCLCollisionChecker
from demo.scene.assembler import SceneAssembler
from planning import DirectPlanner
from planning.planners.curobo.planner import CuroboPlanner
from atomic_action.motion.common.utils.validation import MoveValidationError, validate_move_request
from execution.resources.resolver import allow_arm_pedestal_collisions, component
from execution.backends.mujoco.scene_compiler import _add_robot_contact_exclusions


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OLS_ROOT = PROJECT_ROOT / 'services' / 'object_library_server' / 'data'


class RobotSwitchingTests(unittest.TestCase):
    def test_mujoco_excludes_structural_contacts_for_switched_arms_only(self):
        for robot_id, robot_name in ((5, 'ur3'), (1, 'ur5'), (6, 'ur10')):
            with self.subTest(robot=robot_name):
                arm_urdf = OLS_ROOT / 'robots' / str(robot_id) / f'{robot_name}.urdf'
                arm_xml = ET.parse(arm_urdf).getroot()
                gripper_path = OLS_ROOT / 'robots' / '2' / 'robotiq_2f85.urdf'
                gripper_xml = ET.parse(gripper_path).getroot()
                robot_object = {'components': [{
                    'component_type': 'RobotModelRef',
                    'properties': {
                        'allowed_collision_link_pairs': [],
                        'allowed_arm_environment_names': ['RobotPedestal'],
                    },
                }]}
                scene = [robot_object, {
                    'object_name_in_scene': 'Robotiq',
                    'components': [{'component_type': 'GripperModel',
                                    'properties': {'urdf': 'robots/2/robotiq_2f85.urdf'}}],
                }, {
                    'object_name_in_scene': 'RobotPedestal', 'components': [],
                }, {
                    'object_name_in_scene': 'WorkTable', 'components': [],
                }]
                resources = SimpleNamespace(
                    robot_xml=arm_xml, robot_urdf=arm_urdf, robot_object=robot_object,
                    scene={'objects': scene},
                    asset=lambda value: OLS_ROOT / value,
                )
                root = ET.Element('mujoco')
                world = ET.SubElement(root, 'worldbody')
                for link in (*arm_xml.findall('link'), *gripper_xml.findall('link')):
                    ET.SubElement(world, 'body', name=link.get('name'))
                ET.SubElement(world, 'body', name='RobotPedestal')
                ET.SubElement(world, 'body', name='WorkTable')

                _add_robot_contact_exclusions(root, resources)

                exclusions = {frozenset((item.get('body1'), item.get('body2')))
                              for item in root.findall('./contact/exclude')}
                adjacent = {frozenset((joint.find('parent').get('link'),
                                       joint.find('child').get('link')))
                            for joint in arm_xml.findall('joint')}
                self.assertTrue(adjacent.issubset(exclusions))
                self.assertIn(frozenset(('upper_arm_link', 'RobotPedestal')), exclusions)
                nonadjacent = next(frozenset((a.get('name'), b.get('name')))
                                   for i, a in enumerate(arm_xml.findall('link'))
                                   for b in arm_xml.findall('link')[i + 1:]
                                   if frozenset((a.get('name'), b.get('name'))) not in adjacent)
                self.assertNotIn(nonadjacent, exclusions)
                self.assertNotIn(frozenset(('robotiq_2f85_base', 'RobotPedestal')), exclusions)
                self.assertNotIn(frozenset(('upper_arm_link', 'WorkTable')), exclusions)

    def test_mounting_pedestal_exemption_is_independent_of_arm_and_object_name(self):
        for model in ('ur5', 'ur10', 'piper'):
            robot = {'object_name_in_scene': model, 'components': [
                {'component_type': 'RobotModelRef', 'properties': {}}]}
            pedestal = {'object_name_in_scene': 'CustomMount', 'components': [
                {'component_type': 'Semantic', 'properties': {'semantic_name': 'robot_pedestal'}}]}
            table = {'object_name_in_scene': 'WorkTable', 'components': [
                {'component_type': 'Semantic', 'properties': {'category': 'support'}}]}
            scene = {'objects': [robot, pedestal, table]}
            allow_arm_pedestal_collisions(scene, robot)
            allow_arm_pedestal_collisions(scene, robot)
            self.assertEqual(component(robot, 'RobotModelRef')['allowed_arm_environment_names'],
                             ['CustomMount'])

    def test_ols_scene_can_select_piper_and_build_matching_driver_contract(self):
        class LocalOLS:
            def get_robot(self, ident):
                name = {2: 'robotiq_2f85', 8: 'piper'}[ident]
                return json.loads((OLS_ROOT / 'robots' / str(ident) / f'{name}.json').read_text(encoding='utf-8'))

        scene = {'scene_schema_version': 1, 'scene_id': 91, 'scene_version': 1,
            'objects': [{
                'object_id_in_scene': 1, 'object_name_in_scene': 'Piper',
                'components': [
                    {'component_type': 'Transform', 'properties': {'position': [0.,0.,0.],
                        'quaternion_xyzw': [0.,0.,0.,1.], 'scale': [1.,1.,1.]}},
                    {'component_type': 'MetadataRef', 'properties': {'path': '$LIBRARY_SERVER/8',
                        'type': 'robot'}},
                    {'component_type': 'RobotDriver', 'properties': {
                        'joint_sequence': ['shoulder_pan_joint', 'shoulder_lift_joint',
                            'elbow_joint', 'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint'],
                        'initial_joint_positions_rad': {'shoulder_pan_joint': 0.2}}},
                ],
            }, {
                'object_id_in_scene': 2, 'object_name_in_scene': 'Robotiq',
                'components': [
                    {'component_type': 'Transform', 'properties': {'position': [0.,0.,0.],
                        'quaternion_xyzw': [0.,0.,0.,1.], 'scale': [1.,1.,1.], 'parent': 1}},
                    {'component_type': 'MetadataRef', 'properties': {'path': '$LIBRARY_SERVER/2',
                        'type': 'robot'}},
                    {'component_type': 'RobotAttachment', 'properties': {
                        'parent_object_id': 1, 'parent_joint': 'joint6', 'parent_link': 'link6',
                        'child_link': 'robotiq_2f85_mount', 'connection_type': 'fixed',
                        'transform': {'position': [0.,0.,0.], 'quaternion_xyzw': [0.,0.,0.,1.],
                                      'reference_frame': 'parent_link'}}},
                ],
            }]}
        assembler = SceneAssembler(OLS_ROOT, LocalOLS())

        result = assembler.assemble(scene)

        robot = next(obj for obj in result['objects'] if obj['object_name_in_scene'] == 'Piper')
        driver = next(item['properties'] for item in robot['components']
                      if item['component_type'] == 'RobotDriver')
        model = next(item['properties'] for item in robot['components']
                     if item['component_type'] == 'RobotModelRef')
        self.assertEqual(model['urdf'], 'robots/8/piper.urdf')
        self.assertEqual(model['base_link'], 'world')
        self.assertEqual(model['tip_link'], 'link6')
        attachment = next(obj for obj in result['objects'] if obj['object_name_in_scene'] == 'Robotiq')
        mount = next(item['properties'] for item in attachment['components']
                     if item['component_type'] == 'RobotAttachment')
        self.assertEqual(mount['parent_link'], 'link6')
        self.assertEqual(driver['joint_sequence'], [f'joint{i}' for i in range(1,7)])
        self.assertEqual(len(result['named_targets']['home']['request']['target']['joint_positions']), 6)

    def test_scene_assembly_replaces_stale_driver_sequence_from_selected_urdf(self):
        assembler = SceneAssembler(OLS_ROOT, object())
        robot = {'components': [{
            'component_type': 'RobotDriver',
            'properties': {
                'joint_sequence': [
                    'shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint',
                    'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint',
                ],
                'initial_joint_positions_rad': {'shoulder_pan_joint': .2},
                'available_connect_joints': ['wrist_3_joint'],
            },
        }]}

        assembler._refresh_robot_driver(robot, 'robots/8/piper.urdf')

        driver = robot['components'][0]['properties']
        self.assertEqual(driver['joint_sequence'], [f'joint{i}' for i in range(1, 7)])
        self.assertEqual(driver['available_connect_joints'], ['joint6'])
        self.assertEqual(driver['initial_joint_positions_rad'], {
            'joint1': 0., 'joint2': 0., 'joint3': 0.,
            'joint4': 0., 'joint5': 0., 'joint6': 0.,
        })
        self.assertEqual(driver['joint_limits']['joint1'], [-2.6179938, 2.6179938])

        assembler._refresh_robot_driver(robot, 'robots/9/nero.urdf')
        self.assertEqual(driver['joint_sequence'], [f'joint{i}' for i in range(1, 8)])
        self.assertEqual(len(driver['initial_joint_positions_rad']), 7)

    def test_fcl_adapter_maps_selected_joint_order_to_sdk_order(self):
        checker = SimPlatformFCLCollisionChecker.__new__(SimPlatformFCLCollisionChecker)
        checker._project_joint_names = tuple(f'joint{i}' for i in range(1, 8))
        checker._project_to_sdk = np.array([3, 2, 4, 6, 5, 1, 7])
        checker._fixed_sdk_joints = np.array([0])
        checker.robot = SimpleNamespace(joint_count=8)

        result = checker._to_sdk_joints(np.arange(1., 8.))

        np.testing.assert_array_equal(result, [0., 6., 2., 1., 3., 5., 4., 7.])

    def test_direct_planner_accepts_non_ur5_joint_count(self):
        limits = np.tile([-2., 2.], (7, 1))
        planner = DirectPlanner(joint_limits=limits)
        start, goal = np.zeros(7), np.ones(7) * .1
        request = {'constraints': {
            'avoid_collision': False, 'safety_distance': 0., 'velocity_scale': 1.,
        }}

        trajectory = planner.plan(
            {'path_type': 'joint', 'joint_waypoints': np.vstack((start, goal))},
            request, {},
        )

        self.assertEqual(len(trajectory['waypoints'][0]), 7)
        self.assertEqual(trajectory['waypoints'][0], start.tolist())
        self.assertEqual(trajectory['waypoints'][-1], goal.tolist())

    def test_move_request_and_curobo_snapshot_use_selected_dof(self):
        request = {'target': {'type': 'joint', 'joint_positions': [0.] * 7}}
        self.assertEqual(len(validate_move_request(request, joint_count=7)['target']['joint_positions']), 7)
        with self.assertRaises(MoveValidationError):
            validate_move_request(request, joint_count=6)

        planner = object.__new__(CuroboPlanner)
        planner.robot_runtime = None
        snapshot = {'_runtime_collision': {'joint_names': [f'joint{i}' for i in range(7)]}}
        self.assertEqual(planner._joint_count(snapshot), 7)


if __name__ == '__main__':
    unittest.main()
