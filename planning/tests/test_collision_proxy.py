"""Shared-proxy geometry, conservative filtering, cache and policy tests."""
from copy import deepcopy
from itertools import product
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

import mujoco
import numpy as np

from collision.proxy import PlanningProxy, _cover_box, simplify_snapshot
from libraries.curobo.conversion.runtime import convert_runtime


def geometry(ident, body, *, robot=False, kind='box', size=(.1,.1,.1), center=(0,0,0), name=None):
    pose = np.eye(4); pose[:3,3] = center
    return dict(id=ident, body=body, robot=robot, kind=kind, size=list(size),
                matrix=pose.tolist(), body_name=name or f'body_{body}', name=f'g_{ident}')


def snapshot(geoms, allowed=(), overrides=()):
    return dict(schema_version=1, geometries=geoms,
        body_world={str(g['body']):np.eye(4).tolist() for g in geoms},
        policy=dict(schema_version=1, safety_distance=.003, avoid_collision=True,
                    fingerprint='source', allowed_pairs=list(allowed), distance_overrides=list(overrides),
                    arm_environment_pairs=[], held_name=None, initial_clearance_pairs=[]))


class ProxyGeometryTests(unittest.TestCase):
    def test_box_cover_contains_entire_cells(self):
        low, high = np.array([-.025,-.016,-.01]), np.array([.025,.016,.01])
        spheres = _cover_box(low, high, .02)
        centers = np.array([s['center'] for s in spheres])
        radii = np.array([s['radius'] for s in spheres])
        for point in product(*[np.linspace(a,z,13) for a,z in zip(low,high)]):
            self.assertTrue(np.any(np.linalg.norm(centers-point,axis=1) <= radii+1.e-12))

    def test_merges_only_equivalent_policy_rows_and_preserves_thresholds(self):
        geoms = [geometry(10,1,robot=True), geometry(11,1,robot=True,center=(.1,0,0)), geometry(12,2)]
        raw = snapshot(geoms, allowed=[[10,11]])
        self.assertEqual(len(simplify_snapshot(raw)['geometries']),2)
        raw['policy']['allowed_pairs'].append([10,12])
        result = simplify_snapshot(raw)
        self.assertEqual(len(result['geometries']),3)
        self.assertIn([0,2],result['policy']['allowed_pairs'])
        self.assertNotIn([1,2],result['policy']['allowed_pairs'])
        raw['policy']['distance_overrides'] = [[11,12,-.001]]
        self.assertIn([1,2,-.001],simplify_snapshot(raw)['policy']['distance_overrides'])

    def test_open_container_slabs_cover_parts_without_filling_center(self):
        geoms=[geometry(0,1,robot=True,kind='sphere',size=(.01,0,0),center=(0,0,1))]
        specs=[((0,0,-.1),(.2,.15,.01)), ((-.2,0,0),(.01,.15,.1)),
               ((.2,0,0),(.01,.15,.1)), ((0,-.15,0),(.2,.01,.1)), ((0,.15,0),(.2,.01,.1))]
        for i,(center,size) in enumerate(specs,1):
            geoms.append(geometry(i,2,center=center,size=size,name='Tray'))
        result=simplify_snapshot(snapshot(geoms))
        world=[g for g in result['geometries'] if not g['robot']]
        self.assertEqual(len(world),5)
        self.assertFalse(any(np.all(np.abs(np.asarray(g['matrix'])[:3,3]) <= g['size']) for g in world))
        for source in geoms[1:]:
            proxy=next(g for g in world if source['id'] in g['source_geoms'])
            center=np.asarray(proxy['matrix'])[:3,3]
            for signs in product((-1,1),repeat=3):
                corner=np.asarray(source['matrix'])[:3,3]+np.asarray(source['size'])*signs
                self.assertTrue(np.all(np.abs(corner-center) <= np.asarray(proxy['size'])+1.e-12))

    def test_unknown_geometry_fails_instead_of_omitting_it(self):
        with self.assertRaisesRegex(ValueError,'No conservative'):
            simplify_snapshot(snapshot([geometry(0,1,robot=True,kind='plane')]))

    def test_gripper_template_asset_is_finite_and_positive(self):
        data=json.loads(Path('collision/proxies/robotiq_2f85.json').read_text())
        self.assertEqual(data['schema_version'],1)
        self.assertGreater(len(data['templates']),0)
        for template in data['templates'].values():
            for sphere in template['spheres']:
                self.assertEqual(len(sphere['center']),3)
                self.assertTrue(np.isfinite([*sphere['center'],sphere['radius']]).all())
                self.assertGreater(sphere['radius'],0)


class ProxyQueryTests(unittest.TestCase):
    def setUp(self):
        xml='<mujoco><worldbody><body name="base">'
        for i in range(6):
            xml+=f'<body name="link{i}"><joint name="j{i}" type="hinge" axis="0 0 1" range="-180 180"/><inertial pos="0 0 0" mass="1" diaginertia="1 1 1"/>'
        xml+='<site name="tcp"/><geom name="robot" type="sphere" size=".05" pos="1 0 0"/>'+'</body>'*7
        xml+='<body name="obstacle" pos="0 1 0"><geom name="box" type="box" size=".05 .05 .1"/></body></worldbody></mujoco>'
        self.xml=xml
        model=mujoco.MjModel.from_xml_string(xml)
        data=mujoco.MjData(model); mujoco.mj_kinematics(model,data)
        self.runtime=SimpleNamespace(model=model,data=data,qpos_indices=np.arange(6),joint_ids=np.arange(6),
            joint_limits=np.tile([-np.pi,np.pi],(6,1)), joint_names=[f'j{i}' for i in range(6)],
            gripper_controller=SimpleNamespace(held_name=None,mode='attached',relative_pose=np.eye(4)),object_ids={},
            get_joint_positions=lambda:data.qpos.copy())
        self.runtime.base_id=1
        self.checker=SimpleNamespace(backend=self.runtime, robot_bodies=set(range(1,8)), tcp_site_id=0)
        self.proxy=PlanningProxy(self.checker)
        self.checker.planning_proxy=self.proxy
        self.allow=False
        from libraries.curobo.runtime_policy import export_runtime
        def exported(*args, **kwargs):
            if kwargs.get('_template') is not None:
                return export_runtime(*args, **kwargs)
            return self.raw(kwargs.get('gripper_width'), args[2].get('safety_distance',0.))
        self.patcher=patch('libraries.curobo.runtime_policy.export_runtime',side_effect=exported)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def raw(self, width=None, margin=.003):
        model,data=self.runtime.model,self.runtime.data
        mujoco.mj_kinematics(model,data)
        body=int(model.geom_bodyid[0]); env=int(model.geom_bodyid[1])
        geoms=[geometry(0,body,robot=True,kind='sphere',size=(model.geom_size[0,0],0,0),center=model.geom_pos[0]),
               geometry(1,env,size=model.geom_size[1],center=data.geom_xpos[1])]
        result=snapshot(geoms, allowed=[[0,1]] if self.allow else [])
        result['policy']['safety_distance']=margin
        transform=np.asarray(result['geometries'][1]['matrix'])
        transform[:3,:3]=data.geom_xmat[1].reshape(3,3)
        result['geometries'][1]['matrix']=transform.tolist()
        result['body_world']={str(i):np.block([[data.xmat[i].reshape(3,3),data.xpos[i,:,None]],[np.zeros((1,3)),np.ones((1,1))]]).tolist() for i in range(model.nbody)}
        result.update(root=1,base_world=np.eye(4).tolist(),payload_bodies=[],tcp_body=body,tcp_local=np.eye(4).tolist(),
            tcp_world=result['body_world'][str(body)],joint_names=self.runtime.joint_names,joints=data.qpos.tolist())
        result['bodies']=[dict(id=1,parent=None)]+[dict(id=i+2,parent=i+1,matrix=np.eye(4).tolist(),pivot=np.eye(4).tolist(),
            joint_name=f'j{i}',axis=[0,0,1],kind='revolute',limit=[-np.pi,np.pi]) for i in range(6)]
        return result

    def test_path_detects_midsegment_collision(self):
        start=np.zeros(6); goal=start.copy();goal[0]=np.pi
        self.assertFalse(self.proxy.check(start).in_collision)
        self.assertFalse(self.proxy.check(goal).in_collision)
        self.assertTrue(self.proxy.check_path([start,goal],resolution=.04).in_collision)

    def test_cylinder_end_cap_proxy_is_confirmed_against_source_geometry(self):
        model = self.runtime.model
        model.geom_type[0] = mujoco.mjtGeom.mjGEOM_CYLINDER
        model.geom_size[0] = [.06, .12, 0]
        model.geom_pos[0] = [1, 0, .623]
        model.geom_size[1] = [.1, .1, .25]
        model.body_pos[-1] = [1, 0, .25]
        original = self.raw
        def raw(*args, **kwargs):
            result = original(*args, **kwargs)
            result['geometries'][0].update(kind='cylinder', size=model.geom_size[0].tolist())
            return result
        self.raw = raw
        zero = np.zeros(6)
        result = self.proxy.check(zero, safety_distance=.003)
        self.assertFalse(result.in_collision)  # Actual gap 3 mm; sphere envelope overlaps by 42 mm.
        self.assertAlmostEqual(result.minimum_distance, .003)
        self.assertTrue(self.proxy.check(zero, safety_distance=.004).in_collision)
        model.body_pos[-1, 2] += .004
        far = zero.copy(); far[0] = np.pi
        self.proxy.prepare(dict(safety_distance=.003))
        self.assertFalse(self.proxy._evaluate([far]).in_collision)
        self.assertTrue(self.proxy._evaluate([far, zero]).in_collision)
        self.allow = True
        self.checker.allowed_target_names = {'obstacle'}
        self.assertFalse(self.proxy.check(zero, safety_distance=.003).in_collision)

    def test_three_mm_boundary_and_rotated_box(self):
        self.runtime.model.body_pos[-1]=[1.103,0,0]
        self.assertFalse(self.proxy.check(np.zeros(6),safety_distance=.003-1.e-6).in_collision)
        self.runtime.model.body_pos[-1,0]-=2.e-6
        self.assertTrue(self.proxy.check(np.zeros(6),safety_distance=.003).in_collision)

    def test_rotated_environment_box(self):
        self.runtime.model.body_pos[-1]=[1.1,0,0]
        self.runtime.model.geom_size[1]=[.02,.08,.1]
        self.assertFalse(self.proxy.check(np.zeros(6)).in_collision)
        self.runtime.model.body_quat[-1]=[np.sqrt(.5),0,0,np.sqrt(.5)]
        self.assertTrue(self.proxy.check(np.zeros(6)).in_collision)

    def test_broad_phase_matches_dense_collision_classification(self):
        random=np.random.default_rng(18)
        self.proxy.prepare(dict(safety_distance=.003))
        for _ in range(30):
            configurations=random.uniform(-1,1,(4,6))
            self.proxy.broadphase=True
            actual=self.proxy._evaluate(configurations)
            self.proxy.broadphase=False
            expected=self.proxy._evaluate(configurations)
            self.assertEqual(actual.in_collision,expected.in_collision)
        self.proxy.broadphase=True

    def test_path_cache_is_invalidated_by_world_and_policy(self):
        points=np.zeros((3,6))
        self.assertFalse(self.proxy.check_path(points).in_collision)
        self.assertFalse(self.proxy.check_path(points).in_collision)
        self.assertEqual(self.proxy.stats['path_hits'],1)
        self.runtime.model.body_pos[-1]=[1,0,0]
        self.assertTrue(self.proxy.check_path(points).in_collision)
        self.allow=True
        self.checker.allowed_target_names={'obstacle'}
        self.assertFalse(self.proxy.check_path(points).in_collision)

    def test_validation_rejects_bad_input(self):
        for points in ([],[[np.nan]*6],[[10]*6],[[0]*5]):
            with self.assertRaises(ValueError):
                self.proxy.check_path(points)
        with self.assertRaises(ValueError):
            self.proxy.check_path([[0]*6],resolution=0)

    def test_curobo_export_uses_exact_sphere_coordinates(self):
        frozen=self.proxy.export_runtime(dict(safety_distance=.003))
        with tempfile.TemporaryDirectory() as directory:
            converted=convert_runtime({'_runtime_collision':frozen},directory)
            robot=next(g for g in frozen['geometries'] if g['robot'])
            self.assertEqual(converted['primitive_spheres'][f'runtime_geom_{robot["id"]}'],robot['spheres'])
            self.assertEqual(converted['planning_proxy'],frozen['planning_proxy'])

    def test_scene_drift_during_validation_is_rejected(self):
        original=self.proxy._evaluate
        def drift(*args,**kwargs):
            result=original(*args,**kwargs)
            self.runtime.model.body_pos[-1,0]+=.1
            return result
        self.proxy._evaluate=drift
        with self.assertRaisesRegex(RuntimeError,'changed during'):
            self.proxy.check_path(np.zeros((2,6)))

    def test_lift_bounds_are_measured_on_proxy_and_do_not_follow_motion(self):
        self.runtime.model.body_pos[-1]=[1.08,0,0]
        self.checker._lift_clearance_pairs={frozenset((0,1)):.001}
        self.proxy.capture_lift_clearance(.003)
        self.assertTrue(self.proxy.lift_bounds)
        self.assertFalse(self.proxy.check(np.zeros(6),safety_distance=.003).in_collision)
        self.runtime.model.body_pos[-1,0]-=.001
        self.assertTrue(self.proxy.check(np.zeros(6),safety_distance=.003).in_collision)
        self.runtime.model.body_pos[-1,0]+=.01
        self.assertFalse(self.proxy.check(np.zeros(6),safety_distance=.003).in_collision)

    def test_broad_phase_matches_dense_with_multiple_robot_groups(self):
        original=self.raw
        def multiple(*args,**kwargs):
            result=original(*args,**kwargs)
            result['geometries'].append(geometry(2,1,robot=True,kind='sphere',size=(.12,0,0),center=(.9,0,0)))
            return result
        self.raw=multiple
        self.test_broad_phase_matches_dense_collision_classification()

    def test_query_scope_guards_once_before_and_after_multiple_checks(self):
        from collision.planning_policy import planning_signature
        self.proxy.prepare(dict(safety_distance=.003))
        with patch('collision.planning_policy.planning_signature',wraps=planning_signature) as guard:
            with self.proxy.query_scope():
                self.proxy.check(np.zeros(6),safety_distance=.003)
                self.proxy.check_path(np.zeros((3,6)),safety_distance=.003)
            self.assertEqual(guard.call_count,2)

    def test_query_scope_rejects_drift_after_last_check(self):
        with self.assertRaisesRegex(RuntimeError,'changed during'):
            with self.proxy.query_scope():
                self.proxy.check(np.zeros(6))
                self.runtime.model.body_pos[-1,0]+=.1

    def rrt_inputs(self):
        from planning import CollisionFreePlanner
        self.checker.check=self.proxy.check
        self.checker.check_path=self.proxy.check_path
        planner=CollisionFreePlanner(
            {'planning':{'collision_free':{'optimize_path':False,'shortcut_time':0.}}},
            collision_checker=self.checker,joint_limits=self.runtime.joint_limits)
        path={'path_type':'joint','start':np.zeros(6),'goal':np.full(6,.02)}
        request={'motion':{'path_constraint':'soft'},
                 'planning':{'planning_time':2.,'max_attempts':1},
                 'constraints':{'safety_distance':.003,'velocity_scale':.4}}
        return planner,path,request

    def test_rrt_guards_once_and_matches_unscoped_collision_queries(self):
        from collision.planning_policy import planning_signature
        planner,path,request=self.rrt_inputs()
        self.proxy.prepare(request['constraints'])
        original=self.proxy._evaluate
        queries=[]
        def record(*args,**kwargs):
            result=original(*args,**kwargs)
            queries.append((np.asarray(args[0]).copy(),result.in_collision))
            return result
        with patch.object(self.proxy,'_evaluate',side_effect=record):
            with patch('collision.planning_policy.planning_signature',wraps=planning_signature) as guard:
                trajectory=planner.plan(path,request,{})
                self.assertEqual(guard.call_count,2)
        self.assertGreater(len(queries),3)
        for points,hit in queries:
            self.assertEqual(original(points).in_collision,hit)
        self.assertIsNone(self.proxy._scope_state)
        stages=trajectory['diagnostics']['planning_stages_s']
        self.assertGreaterEqual(stages['proxy_state_guards'],0.)
        self.assertEqual(trajectory['diagnostics']['planning_proxy_cache']['builds'],0)

    def test_rrt_rejects_scene_drift_during_search_and_clears_scope(self):
        planner,path,request=self.rrt_inputs()
        original=planner._rrt_connect
        def drift(*args,**kwargs):
            result=original(*args,**kwargs)
            self.runtime.model.body_pos[-1,0]+=.1
            return result
        with patch.object(planner,'_rrt_connect',side_effect=drift):
            with self.assertRaisesRegex(RuntimeError,'changed during'):
                planner.plan(path,request,{})
        self.assertIsNone(self.proxy._scope_state)
        self.assertFalse(self.proxy._paths)

    def test_rrt_rejects_policy_drift_and_clears_scope_on_failure(self):
        planner,path,request=self.rrt_inputs()
        def drift(*args,**kwargs):
            self.checker.scene_revision=1
            raise ValueError('failed search')
        with patch.object(planner,'_rrt_connect',side_effect=drift):
            with self.assertRaisesRegex(RuntimeError,'changed during'):
                planner.plan(path,request,{})
        self.assertIsNone(self.proxy._scope_state)

    def test_arm_motion_refreshes_state_without_rebuilding_geometry(self):
        self.proxy.check(np.zeros(6), safety_distance=.003)
        pairs=self.proxy.world_pairs
        self.runtime.data.qpos[0]=.4
        actual=self.proxy.check(self.runtime.data.qpos, safety_distance=.003)
        self.assertEqual(self.proxy.stats['builds'],1)
        self.assertEqual(self.proxy.stats['state_refreshes'],1)
        self.assertIs(self.proxy.world_pairs,pairs)
        refreshed=deepcopy(self.proxy.snapshot)
        self.proxy._structure_key=None
        self.proxy._key=None
        self.proxy._compiled.clear()
        expected=self.proxy.check(self.runtime.data.qpos, safety_distance=.003)
        self.assertEqual(actual.in_collision,expected.in_collision)
        np.testing.assert_allclose(refreshed['tcp_world'],self.proxy.snapshot['tcp_world'])
        for old,new in zip(refreshed['bodies'],self.proxy.snapshot['bodies']):
            if 'matrix' in old:
                np.testing.assert_allclose(old['matrix'],new['matrix'],atol=1.e-12)

    def test_policy_or_shape_change_rebuilds_after_state_refresh(self):
        self.proxy.check(np.zeros(6), safety_distance=.003)
        self.runtime.data.qpos[0]=.2
        self.proxy.check(np.zeros(6), safety_distance=.003)
        self.runtime.model.geom_size[0,0]=.2
        self.proxy.check(np.zeros(6), safety_distance=.003)
        self.assertEqual(self.proxy.stats['builds'],2)
        self.allow=True
        self.checker.allowed_target_names={'obstacle'}
        self.proxy.check(np.zeros(6), safety_distance=.003)
        self.assertEqual(self.proxy.stats['builds'],3)

    def test_scene_sync_refreshes_metadata_and_invalidates_path_results(self):
        self.proxy.check_path(np.zeros((2,6)), safety_distance=.003)
        self.checker.scene_revision=9
        self.proxy.check_path(np.zeros((2,6)), safety_distance=.003)
        self.assertEqual(self.proxy.stats['builds'],1)
        self.assertEqual(self.proxy.stats['state_refreshes'],1)
        self.assertEqual(self.proxy.stats['path_hits'],0)
        self.assertEqual(self.proxy.snapshot['policy']['scene_revision'],9)

    def test_returning_policy_reuses_only_compilation_not_path_results(self):
        points=np.zeros((2,6))
        self.proxy.check_path(points, safety_distance=.003)
        self.allow=True
        self.checker.allowed_target_names={'obstacle'}
        self.proxy.check_path(points, safety_distance=.003)
        self.allow=False
        self.checker.allowed_target_names=None
        self.runtime.data.qpos[0]=np.pi/2
        result=self.proxy.check(np.r_[np.pi/2,np.zeros(5)], safety_distance=.003)
        self.assertTrue(result.in_collision)
        self.assertEqual(self.proxy.stats['builds'],2)
        self.assertEqual(self.proxy.stats['structure_hits'],1)
        self.assertEqual(self.proxy.stats['path_hits'],0)

    def test_compiled_policy_cache_is_bounded(self):
        for revision in range(7):
            self.proxy.prepare(dict(safety_distance=revision * .001))
        self.assertEqual(len(self.proxy._compiled),4)

    def test_free_object_pose_refreshes_world_and_rejects_new_collision(self):
        model=mujoco.MjModel.from_xml_string(self.xml.replace(
            '<body name="obstacle" pos="0 1 0">',
            '<body name="obstacle" pos="0 1 0"><freejoint/>'))
        data=mujoco.MjData(model)
        self.runtime.model=model
        self.runtime.data=data
        self.runtime.get_joint_positions=lambda:data.qpos[:6].copy()
        self.assertFalse(self.proxy.check_path(np.zeros((2,6))).in_collision)
        data.qpos[6:9]=[1,0,0]
        data.qpos[9:13]=[np.sqrt(.5),0,0,np.sqrt(.5)]
        self.assertTrue(self.proxy.check_path(np.zeros((2,6))).in_collision)
        self.assertEqual(self.proxy.stats['builds'],1)
        self.assertEqual(self.proxy.stats['state_refreshes'],1)
        self.assertEqual(self.proxy.stats['path_hits'],0)
        np.testing.assert_allclose(self.proxy.world_centers,[[1,0,0]],atol=1.e-12)

    def test_mocap_pose_refreshes_world_without_reusing_path_result(self):
        model=mujoco.MjModel.from_xml_string(self.xml.replace(
            '<body name="obstacle" pos="0 1 0">',
            '<body name="obstacle" mocap="true" pos="0 1 0">'))
        data=mujoco.MjData(model)
        self.runtime.model=model
        self.runtime.data=data
        self.runtime.get_joint_positions=lambda:data.qpos.copy()
        self.assertFalse(self.proxy.check_path(np.zeros((2,6))).in_collision)
        data.mocap_pos[0]=[1,0,0]
        self.assertTrue(self.proxy.check_path(np.zeros((2,6))).in_collision)
        self.assertEqual(self.proxy.stats['builds'],1)
        self.assertEqual(self.proxy.stats['state_refreshes'],1)
        self.assertEqual(self.proxy.stats['path_hits'],0)

    def test_direct_diagnostics_report_build_and_state_refresh(self):
        from planning.planners.direct_planner import DirectPlanner
        self.checker.check=self.proxy.check
        self.checker.check_path=self.proxy.check_path
        planner=DirectPlanner(collision_checker=self.checker,joint_limits=self.runtime.joint_limits)
        path={'path_type':'joint','joint_waypoints':np.zeros((2,6))}
        request={'constraints':{'avoid_collision':True,'safety_distance':.003,'velocity_scale':.4}}
        first=planner.plan(path,request,{})
        self.assertEqual(first['diagnostics']['planning_proxy_cache']['builds'],1)
        self.runtime.data.qpos[0]=.2
        second=planner.plan(path,request,{})
        self.assertEqual(second['diagnostics']['planning_proxy_cache']['builds'],0)
        self.assertEqual(second['diagnostics']['planning_proxy_cache']['state_refreshes'],1)


if __name__=='__main__':
    unittest.main()
