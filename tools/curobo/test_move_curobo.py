"""Adapter contract and geometry regressions; these tests do not require CUDA."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import numpy as np

from libraries.curobo.conversion.assets import ConversionError, fingerprint
from libraries.curobo.conversion.scene import SceneConverter,goal_in_base
from libraries.curobo.conversion.urdf import URDFModel,pose
from planning.planners.curobo import CuroboPlanner


def obj(ident,kind,asset_id,driver=None,transform=None,attachment=None):
    pairs=[('MetadataRef',{'type':kind,'path':f'$LIBRARY_SERVER/{asset_id}'}),
           ('Transform',transform or {})]
    if driver is not None:
        pairs.append(('RobotDriver',driver))
    if attachment is not None:
        pairs.append(('RobotAttachment',attachment))
    return {'object_id_in_scene':ident,'object_name_in_scene':f'object_{ident}',
            'components':[{'component_type':k,'properties':v} for k,v in pairs]}


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.arm=self.root/'arm.urdf'
        self.arm.write_text('''<robot name="arm"><link name="base"/><link name="l1"/><link name="attachment_site"/>
          <joint name="j1" type="revolute"><parent link="base"/><child link="l1"/><axis xyz="0 0 1"/><limit lower="-3" upper="3" velocity="1" effort="1"/></joint>
          <joint name="j2" type="prismatic"><parent link="l1"/><child link="attachment_site"/><axis xyz="1 0 0"/><limit lower="0" upper="1" velocity="1" effort="1"/></joint></robot>''')
        self.cabinet=self.root/'cabinet.urdf'
        self.cabinet.write_text('''<robot name="cabinet"><link name="body"><collision><origin xyz="0 0 .5"/><geometry><box size=".1 1 1"/></geometry></collision></link>
          <link name="door"><collision><origin xyz=".5 0 .5"/><geometry><box size="1 .02 1"/></geometry></collision></link>
          <joint name="hinge" type="revolute"><parent link="body"/><child link="door"/><axis xyz="0 0 1"/><limit lower="0" upper="2" velocity="1" effort="1"/></joint></robot>''')
        assets={('robot',1):self.arm,('model',2):self.cabinet}
        class Store:
            def resolve(_,kind,ident):
                path=assets[(kind,ident)]
                return {'entry':path,'folder':path.parent,'digest':fingerprint(path.read_text()),'metadata':{}}
        self.converter=SceneConverter(Store(),self.root/'cache')
        self.driver={'role':'arm','joint_sequence':['j1','j2'],
            'initial_joint_positions_rad':{'j1':0.,'j2':.1}}
        self.scene={'objects':[obj(10,'robot',1,self.driver),
            obj(20,'model',2,{'role':'passive','joint_sequence':['hinge'],
                'initial_joint_positions_rad':{'hinge':0}}, {'position':[2,0,0]})]}

    def tearDown(self):
        self.temp.cleanup()

    def test_now_state_is_read_without_changing_initial_and_prismatic_is_metres(self):
        scene=deepcopy(self.scene)
        scene['objects'][0]['components'][2]['properties']['now_joint_positions_rad']={'j2':.4}
        original=deepcopy(scene)
        result=self.converter.convert(scene)
        self.assertEqual(scene,original)
        self.assertAlmostEqual(result['positions']['o10_j2'],.4)
        self.assertAlmostEqual(result['current_tool_pose'][0],.4)
        self.assertEqual(result['positions']['o10_j1'],0.)

    def test_open_door_moves_only_its_collision_shapes_and_preserves_cavity(self):
        first=self.converter.convert(self.scene)
        scene=deepcopy(self.scene)
        scene['objects'][1]['components'][2]['properties']['now_joint_positions_rad']={'hinge':np.pi/2}
        second=self.converter.convert(scene)
        self.assertEqual(len(second['world']['cuboid']),2)
        self.assertEqual(first['world']['cuboid']['o20_body_0'],second['world']['cuboid']['o20_body_0'])
        self.assertFalse(np.allclose(first['world']['cuboid']['o20_door_0']['pose'],second['world']['cuboid']['o20_door_0']['pose']))

    def test_parent_link_attachment_uses_joint_fk_instead_of_plain_transform(self):
        scene=deepcopy(self.scene)
        scene['objects'].append(obj(30,'model',2,{'role':'passive'},
            {'parent':10,'position':[99,99,99]},
            {'parent_object_id':10,'parent_link':'attachment_site','connection_type':'fixed',
             'transform':{'position':[.2,0,0],'reference_frame':'parent_link'}}))
        result=self.converter.convert(scene)
        tree=ET.parse(result['urdf']).getroot()
        mount=tree.find("joint[@name='o30_mount']/origin")
        np.testing.assert_allclose([float(x) for x in mount.get('xyz').split()],[.2,0,0])
        self.assertIn('o30_hinge',result['locked_joints'])

    def test_unknown_or_out_of_limit_state_is_an_error(self):
        for values in ({'unknown':1.},{'j2':2.}):
            scene=deepcopy(self.scene)
            scene['objects'][0]['components'][2]['properties']['now_joint_positions_rad']=values
            with self.assertRaises(ConversionError):
                self.converter.convert(scene)

    def test_host_allowed_collision_pairs_are_resolved_and_missing_links_fail(self):
        scene=deepcopy(self.scene)
        scene['objects'][0]['components'].append({'component_type':'RobotModelRef',
            'properties':{'allowed_collision_link_pairs':[['base','attachment_site']]}})
        result=self.converter.convert(scene)
        self.assertEqual(result['collision_ignores'],{
            'o10_base':['o10_attachment_site'],'o10_attachment_site':['o10_base']})
        scene['objects'][0]['components'][-1]['properties']['allowed_collision_link_pairs']=[['base','missing']]
        with self.assertRaises(ConversionError):
            self.converter.convert(scene)

    def test_multilevel_mimic_offsets_are_composed_exactly(self):
        path=self.root/'mimic.urdf'
        path.write_text('''<robot name="m"><link name="a"/><link name="b"/><link name="c"/><link name="d"/>
         <joint name="driver" type="continuous"><parent link="a"/><child link="b"/></joint>
         <joint name="one" type="continuous"><parent link="b"/><child link="c"/><mimic joint="driver" multiplier="2" offset=".1"/></joint>
         <joint name="two" type="continuous"><parent link="c"/><child link="d"/><mimic joint="one" multiplier="-3" offset=".2"/></joint></robot>''')
        model=URDFModel(path)
        root,a,b=model.mimic_relation('two')
        self.assertEqual(root,'driver')
        self.assertAlmostEqual(a,-6.)
        self.assertAlmostEqual(b,-.1)
        node=model.namespaced('x_').find("joint[@name='x_two']/mimic")
        self.assertEqual(node.get('joint'),'x_driver')
        self.assertAlmostEqual(float(node.get('offset')),-.1)


class ActionContractTests(unittest.TestCase):
    def test_scene_provider_accepts_runtime_scene_tuple(self):
        scene={'objects':[], 'move_defaults':{}}
        action=CuroboPlanner(
            scene_provider=lambda: (scene, 7),
            client=Mock(),
            robot_object_id=1,
        )
        snapshot, robot_id=action._snapshot()
        self.assertEqual(snapshot, scene)
        self.assertEqual(robot_id, 7)

    def test_world_target_with_rotated_base_and_omitted_orientation(self):
        base=pose({'position':[1,2,3],'quaternion_xyzw':[0,0,np.sqrt(.5),np.sqrt(.5)]})
        target=goal_in_base({'frame':'world','position':[1,3,3]},base,[0,0,0,1,0,0,0])
        np.testing.assert_allclose(target[:3,3],[1,0,0],atol=1e-7)
        np.testing.assert_allclose(target[:3,:3],np.eye(3),atol=1e-7)
        target=goal_in_base({'frame':'world','position':[1,3,3],
            'orientation':{'representation':'rpy','roll':0,'pitch':0,'yaw':90}},base,[0,0,0,1,0,0,0])
        np.testing.assert_allclose(target[:3,:3],np.eye(3),atol=1e-7)

    def test_planning_result_matches_host_fields_and_does_not_start_gpu_on_import(self):
        client=Mock()
        trajectory={'planning_time':.1,'joint_names':['j']*6,'duration':1.,
                    'waypoints':[[0.]*6,[.1]*6],'timestamps':[0.,1.]}
        client.plan.return_value=trajectory
        scene={'objects':[]}
        action=CuroboPlanner(scene=scene,client=client)
        result=action.plan({'target':{'type':'joint','joint_positions':[.1]*6}})
        self.assertTrue(result['success'],result.get('error'))
        self.assertTrue(result['planned_only'])
        self.assertIsNone(result['execution_result'])
        self.assertEqual(result['trajectory'],trajectory)

    def test_unsupported_path_constraint_does_not_call_planner(self):
        client=Mock()
        action=CuroboPlanner(scene={'objects':[]},client=client)
        result=action.plan({'target':{'type':'joint','joint_positions':[0.]*6},
                            'motion':{'path_type':'linear'}})
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['error_code'],'INVALID_REQUEST')
        client.plan.assert_not_called()


if __name__=='__main__':
    unittest.main()
