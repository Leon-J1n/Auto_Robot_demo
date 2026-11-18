"""Opt-in tests for real CUDA queries, gradients and captured policy updates.

Run in an isolated process with ROBOT_AGENT_CUROBO_GPU_TESTS=1. No robot moves.
"""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ENABLED = os.environ.get('ROBOT_AGENT_CUROBO_GPU_TESTS') == '1'
if ENABLED:
    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT / '.cache/curobo/site-packages'))
    sys.path.insert(0, str(ROOT / 'libraries/curobo/source'))
    os.environ['WARP_CACHE_PATH'] = str(ROOT / '.cache/curobo/runtime_cache/warp')
    import torch
    import warp
    warp.config.use_precompiled_headers = False
    from curobo.scene import Scene
    from curobo.types import DeviceCfg
    from curobo._src.geom.collision.collision_scene import SceneCollision, SceneCollisionCfg
    from curobo._src.geom.collision.buffer_collision import CollisionBuffer
    from libraries.curobo.policy_checker import PolicyCollisionChecker


@unittest.skipUnless(ENABLED, 'Set ROBOT_AGENT_CUROBO_GPU_TESTS=1 in an isolated CUDA process')
class GPUPolicyTests(unittest.TestCase):
    def setUp(self):
        self.device = DeviceCfg()
        world = Scene.create({'cuboid': {'target': {'pose': [0, 0, 0, 1, 0, 0, 0], 'dims': [2, 2, 2]}}})
        self.scene = SceneCollision.from_config(SceneCollisionCfg(device_cfg=self.device,
            scene_model=world.get_collision_check_world(), cache={'cuboid': 2}))
        self.checker = PolicyCollisionChecker(self.device)
        self.scene.checker = self.checker
        params = SimpleNamespace(link_name_to_idx_map={'tool': 0, 'arm': 1},
            link_sphere_idx_map=torch.tensor([0, 1], device='cuda'))
        self.kinematics = SimpleNamespace(config=SimpleNamespace(kinematics_config=params))
        self.converted = {'geometry_links': {'tool': 0, 'arm': 1}, 'world_geoms': {'target': 2}}
        self.weight = torch.ones(1, device='cuda')
        self.eta = torch.zeros(1, device='cuda')
        self.dt = torch.full((1,), .02, device='cuda')
        self.configure({(0, 2)}, {})

    def configure(self, allowed, overrides):
        self.checker.configure(self.scene, self.kinematics, self.converted,
            {'allowed': allowed, 'overrides': overrides, 'margin': 0.})

    def query(self, x=1.05, *, swept=False, grad=False):
        spheres = torch.tensor([[[[x, 0, 0, .1], [x, 0, 0, .1]]]], device='cuda', requires_grad=grad)
        if swept:
            spheres = spheres.repeat(1, 3, 1, 1)
        buffer = CollisionBuffer.from_shape(spheres.shape, self.device)
        if swept:
            result = self.checker.get_swept_sphere_distance(self.scene.data, spheres, buffer,
                self.weight, self.eta, self.dt, return_loss=True)
        else:
            result = self.checker.get_sphere_distance(self.scene.data, spheres, buffer,
                self.weight, self.eta, return_loss=True)
        return result, spheres, buffer

    def test_tool_target_allowed_but_arm_target_checked(self):
        for swept in (False, True):
            result, _, _ = self.query(swept=swept)
            self.assertTrue(torch.all(result[..., 0] == 0))
            self.assertTrue(torch.all(result[..., 1] > .04))

    def test_exported_arm_pedestal_exemption_preserves_tool_and_payload(self):
        from planning.tests.test_curobo_policy import scene
        from libraries.curobo.runtime_policy import export_runtime
        from libraries.curobo.conversion.runtime import convert_runtime
        from libraries.curobo.policy import compile_policy
        with scene() as (runtime, host, g), tempfile.TemporaryDirectory() as directory:
            host._arm_link_names = frozenset({'base', 'arm'})
            host._allowed_arm_environment_names = frozenset({'RobotPedestal'})
            host._bind_arm_environment_policy()
            runtime.gripper_controller.held_name = 'payload'
            exported = export_runtime(runtime, host, {'safety_distance': .003}, directory)
            compiled = compile_policy(convert_runtime({'_runtime_collision': exported}, directory))
            params = SimpleNamespace(link_name_to_idx_map={'arm': 0, 'tool': 1, 'payload': 2},
                link_sphere_idx_map=torch.tensor([0, 1, 2], device='cuda'))
            kinematics = SimpleNamespace(config=SimpleNamespace(kinematics_config=params))
            converted = {'geometry_links': {name: g(name) for name in ('arm', 'tool', 'payload')},
                         'world_geoms': {'target': g('pedestal')}}
            self.checker = PolicyCollisionChecker(self.device)
            self.checker.configure(self.scene, kinematics, converted, compiled)
            for swept in (False, True):
                spheres = torch.tensor([[[[1.05, 0, 0, .1]] * 3]], device='cuda')
                if swept:
                    spheres = spheres.repeat(1, 3, 1, 1)
                buffer = CollisionBuffer.from_shape(spheres.shape, self.device)
                if swept:
                    result = self.checker.get_swept_sphere_distance(self.scene.data, spheres, buffer,
                        self.weight, self.eta, self.dt, return_loss=True)
                else:
                    result = self.checker.get_sphere_distance(self.scene.data, spheres, buffer,
                        self.weight, self.eta, return_loss=True)
                self.assertTrue(torch.all(result[..., 0] == 0))
                self.assertTrue(torch.all(result[..., 1:] > .04))

    def test_support_penetration_threshold(self):
        self.configure(set(), {(0, 2): -.001, (1, 2): -.001})
        result, _, _ = self.query(x=1.0995)
        self.assertTrue(torch.all(result == 0))
        result, _, _ = self.query(x=1.0985)
        self.assertTrue(torch.all(result > .0004))

    def test_clearance_boundary_roundoff_but_not_micrometre_violation(self):
        # A sphere at 3 mm clearance from the unit box can round to a small
        # positive overlap in float32. One micrometre below the limit must fail.
        self.checker.configure(self.scene, self.kinematics, self.converted,
            {'allowed': set(), 'overrides': {}, 'margin': .003})
        for swept in (False, True):
            result, _, _ = self.query(x=1.103, swept=swept)
            self.assertTrue(torch.all(result == 0))
            result, _, _ = self.query(x=1.103 - .000001, swept=swept)
            self.assertTrue(torch.all(result > .0000008))

    def test_initial_penetration_cannot_deepen(self):
        self.configure(set(), {(0, 2): -.002001})
        result, _, _ = self.query(x=1.098)
        self.assertEqual(result[0, 0, 0].item(), 0.)
        result, _, _ = self.query(x=1.097)
        self.assertGreater(result[0, 0, 0].item(), .0009)

    def test_gradient_matches_finite_difference(self):
        result, spheres, _ = self.query(grad=True)
        result.sum().backward()
        numerical = (self.query(x=1.051)[0].sum() - self.query(x=1.049)[0].sum()) / .002
        self.assertAlmostEqual(spheres.grad[..., 0].sum().item(), numerical.item(), places=3)
        self.assertEqual(spheres.grad[0, 0, 0].abs().sum().item(), 0.)

    def test_policy_update_is_visible_in_cuda_graph(self):
        spheres = torch.tensor([[[[1.05, 0, 0, .1], [1.05, 0, 0, .1]]]], device='cuda')
        buffer = CollisionBuffer.from_shape(spheres.shape, self.device)
        def query():
            return self.checker.get_sphere_distance(self.scene.data, spheres, buffer,
                                                   self.weight, self.eta)
        for _ in range(3):
            query()
        torch.cuda.synchronize()
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            result = query()
        graph.replay()
        torch.cuda.synchronize()
        self.assertEqual(result[0, 0, 0].item(), 0.)
        self.configure(set(), {})
        graph.replay()
        torch.cuda.synchronize()
        self.assertGreater(result[0, 0, 0].item(), .04)
        self.assertGreater(result[0, 0, 1].item(), .04)

    def test_mesh_queries_preserve_masks_thresholds_and_sweep(self):
        import trimesh
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'target.obj'
            trimesh.creation.box(extents=[2., 2., 2.]).export(path)
            world = Scene.create({'mesh': {'target': {'pose': [0, 0, 0, 1, 0, 0, 0], 'file_path': str(path)}}})
            self.scene = SceneCollision.from_config(SceneCollisionCfg(device_cfg=self.device,
                scene_model=world.get_collision_check_world(), cache={'mesh': 2}))
            self.checker = PolicyCollisionChecker(self.device)
            self.configure({(0, 2)}, {})
            for swept in (False, True):
                result, _, _ = self.query(swept=swept)
                self.assertTrue(torch.all(result[..., 0] == 0))
                self.assertTrue(torch.all(result[..., 1] > .04))
            self.configure(set(), {(0, 2): -.001, (1, 2): -.001})
            result, _, _ = self.query(x=1.0995, swept=True)
            self.assertTrue(torch.all(result == 0))
            result, _, _ = self.query(x=1.0985, swept=True)
            self.assertTrue(torch.all(result > .0004))
            self.checker.configure(self.scene, self.kinematics, self.converted,
                {'allowed': set(), 'overrides': {}, 'margin': .003})
            for swept in (False, True):
                result, _, _ = self.query(x=1.103, swept=swept)
                self.assertTrue(torch.all(result == 0))
                result, _, _ = self.query(x=1.103 - .000001, swept=swept)
                self.assertTrue(torch.all(result > .0000008))

    def test_chunked_batches_preserve_sweeps_gradients_and_cuda_graph(self):
        from unittest.mock import patch
        self.configure(set(), {})
        spheres = torch.tensor([[[[1.05, 0, 0, .1], [1.04, 0, 0, .1]],
                                 [[1.06, 0, 0, .1], [1.03, 0, 0, .1]],
                                 [[1.07, 0, 0, .1], [1.02, 0, 0, .1]]]], device='cuda').repeat(5, 1, 1, 1)
        spheres[:, :, :, 0] += torch.arange(5, device='cuda')[:, None, None] * .001
        for swept in (False, True):
            def query(limit):
                values = spheres.clone().requires_grad_(True)
                buffer = CollisionBuffer.from_shape(values.shape, self.device)
                with patch('libraries.curobo.policy_checker.MAX_QUERY_PAIRS', limit):
                    result = self.checker._query(self.scene.data, values, buffer,
                        self.weight, self.eta, return_loss=True, swept=swept,
                        dt=self.dt, speed_metric=swept)
                result.sum().backward()
                return result.clone(), values.grad.clone()
            expected, expected_gradient = query(1 << 25)
            actual, actual_gradient = query(24)  # Two complete batch elements per launch.
            torch.testing.assert_close(actual, expected)
            torch.testing.assert_close(actual_gradient, expected_gradient)
            buffer = CollisionBuffer.from_shape(spheres.shape, self.device)
            with patch('libraries.curobo.policy_checker.MAX_QUERY_PAIRS', 24):
                def captured_query():
                    return self.checker._query(self.scene.data, spheres, buffer,
                        self.weight, self.eta, swept=swept, dt=self.dt, speed_metric=swept)
                for _ in range(3):
                    captured_query()
                torch.cuda.synchronize()
                graph = torch.cuda.CUDAGraph()
                with torch.cuda.graph(graph):
                    captured = captured_query()
                graph.replay()
                torch.cuda.synchronize()
                torch.testing.assert_close(captured, expected)

    def test_compaction_matches_dense_costs_gradients_and_crossing_sweeps(self):
        import trimesh
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'box.obj'
            trimesh.creation.box(extents=[2., 2., 2.]).export(path)
            for kind in ('cuboid', 'mesh'):
                obstacles = {'near': {'pose': [0, 0, 0, 1, 0, 0, 0]},
                             'far': {'pose': [20, 0, 0, 1, 0, 0, 0]}}
                for obstacle in obstacles.values():
                    obstacle.update({'dims': [2, 2, 2]} if kind == 'cuboid' else {'file_path': str(path)})
                world = Scene.create({kind: obstacles})
                self.scene = SceneCollision.from_config(SceneCollisionCfg(device_cfg=self.device,
                    scene_model=world.get_collision_check_world(), cache={kind: 64}))
                self.checker = PolicyCollisionChecker(self.device)
                self.converted['world_geoms'] = {'near': 2, 'far': 3}
                for allowed in (set(), {(0, 2)}, {(0, 2), (1, 2)}):
                    self.configure(allowed, {(1, 2): -.001})
                    # Endpoints outside, segment through the box: broad phase
                    # must retain swept candidates, not only endpoint contacts.
                    spheres = torch.tensor([[[[-1.15, 0, 0, .1], [-1.04, 0, 0, .1]],
                                              [[1.15, 0, 0, .1], [1.03, 0, 0, .1]],
                                              [[1.3, 0, 0, .1], [1.2, 0, 0, .1]]]], device='cuda').repeat(3, 1, 1, 1)
                    for swept in (False, True):
                        outputs = []
                        for compact, queue_capacity in ((False, 1 << 20), (True, 1 << 20), (True, 1)):
                            self.checker.compact_queries = compact
                            query = spheres.clone().requires_grad_(True)
                            buffer = CollisionBuffer.from_shape(query.shape, self.device)
                            self.checker.query_workspaces.clear()
                            with patch('libraries.curobo.policy_checker.MAX_QUERY_PAIRS', 48), patch(
                                    'libraries.curobo.policy_checker.MAX_COMPACT_CANDIDATES', queue_capacity):
                                result = self.checker._query(self.scene.data, query, buffer,
                                    self.weight, self.eta, swept=swept, dt=self.dt,
                                    speed_metric=swept, return_loss=True)
                            result.sum().backward()
                            outputs.append((result.detach().clone(), query.grad.clone()))
                        for output in outputs[1:]:
                            for actual, expected in zip(output, outputs[0]):
                                torch.testing.assert_close(actual, expected)

    def test_bvh_refit_policy_updates_and_disable_are_visible_to_captured_query(self):
        from curobo.types import Pose
        world = Scene.create({'cuboid': {'target': {
            'pose': [0, 0, 0, 1, 0, 0, 0], 'dims': [2, 2, 2]}}})
        self.scene = SceneCollision.from_config(SceneCollisionCfg(device_cfg=self.device,
            scene_model=world.get_collision_check_world(), cache={'cuboid': 64}))
        self.checker = PolicyCollisionChecker(self.device)
        self.configure(set(), {})
        spheres = torch.tensor([[[[1.05, 0, 0, .1], [1.05, 0, 0, .1]]]], device='cuda')
        buffer = CollisionBuffer.from_shape(spheres.shape, self.device)
        def query():
            return self.checker.get_swept_sphere_distance(self.scene.data, spheres, buffer,
                self.weight, self.eta, self.dt)
        for _ in range(3):
            query()
        torch.cuda.synchronize()
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            result = query()
        graph.replay()
        torch.cuda.synchronize()
        self.assertTrue(torch.all(result > .04))
        self.scene.update_obstacle_pose('target', Pose.from_list([10, 0, 0, 1, 0, 0, 0], self.device))
        self.configure(set(), {})
        graph.replay()
        torch.cuda.synchronize()
        self.assertTrue(torch.all(result == 0))
        self.scene.update_obstacle_pose('target', Pose.from_list([0, 0, 0, 1, 0, 0, 0], self.device))
        self.configure({(0, 2)}, {})
        graph.replay()
        torch.cuda.synchronize()
        self.assertEqual(result[0, 0, 0].item(), 0.)
        self.assertGreater(result[0, 0, 1].item(), .04)
        self.scene.enable_obstacle('target', False)
        self.configure(set(), {})
        graph.replay()
        torch.cuda.synchronize()
        self.assertTrue(torch.all(result == 0))


@unittest.skipUnless(ENABLED, 'Set ROBOT_AGENT_CUROBO_GPU_TESTS=1 in an isolated CUDA process')
class WorkerPolicyTests(unittest.TestCase):
    def test_shared_proxy_graph_replay_world_policy_and_pose_mode(self):
        import numpy as np
        from libraries.curobo import CuroboClient
        bodies=[{'id':0,'parent':None}]
        transform=np.eye(4)
        for i in range(6):
            local=np.eye(4);local[:3,3]=[.1,0,.1]
            bodies.append(dict(id=i+1,parent=i,matrix=local.tolist(),pivot=np.eye(4).tolist(),
                joint_name=f'joint_{i}',axis=[0,0,1],kind='revolute',limit=[-3.14,3.14]))
            transform=transform@local
        obstacle=np.eye(4);obstacle[0,3]=10.
        snapshot={'_runtime_collision':dict(schema_version=1,bodies=bodies,root=0,payload_bodies=[],
            base_world=np.eye(4).tolist(),joint_names=[f'joint_{i}' for i in range(6)],joints=[0.]*6,
            tcp_body=6,tcp_local=np.eye(4).tolist(),tcp_world=transform.tolist(),
            geometries=[dict(id=0,body=6,robot=True,kind='sphere_set',size=[0,0,0],matrix=np.eye(4).tolist(),
                             spheres=[dict(center=[0,0,0],radius=.02)]),
                        dict(id=1,body=7,robot=False,kind='box',size=[.1,.1,.1],matrix=obstacle.tolist())],
            planning_proxy=dict(schema_version=1,geometry_source='shared_simplified_proxy'),
            policy=dict(schema_version=1,safety_distance=.003,avoid_collision=True,allowed_pairs=[],
                        distance_overrides=[],held_name=None,fingerprint='proxy_graph_test'))}
        request={'target':{'type':'joint','joint_positions':[.02]+[0.]*5},
                 'constraints':{'safety_distance':.003},'planning':{'max_attempts':2}}
        with CuroboClient({'request_timeout':120}) as client:
            for _ in range(3):
                result=client.plan(request,snapshot)
                self.assertTrue(result['diagnostics']['curobo']['cuda_graph_enabled'])
                self.assertEqual(result['diagnostics']['curobo']['collision_policy']['geometry_source'],'shared_simplified_proxy')
            snapshot['_runtime_collision']['geometries'][1]['matrix']=transform.tolist()
            with self.assertRaisesRegex(RuntimeError,'Runtime start is infeasible'):
                client.plan(request,snapshot)
            snapshot['_runtime_collision']['policy'].update(allowed_pairs=[[0,1]],fingerprint='explicit_target_pair')
            result=client.plan(request,snapshot)
            self.assertTrue(result['diagnostics']['curobo']['cuda_graph_enabled'])
            prepared=client.prepare(snapshot,request={'target':{'type':'pose'},'constraints':{'safety_distance':.003}})
            self.assertFalse(prepared['cuda_graph_enabled'])

    def test_six_joint_planning_with_attached_payload(self):
        import numpy as np
        from scipy.spatial.transform import Rotation
        from libraries.curobo import CuroboClient
        bodies = [{'id': 0, 'parent': None}]
        transform = np.eye(4)
        names = [f'joint_{i}' for i in range(6)]
        for index, name in enumerate(names):
            origin = np.eye(4); origin[:3, 3] = [.15, 0., .1]
            axis = [0., 0., 1.] if index % 2 else [0., 1., 0.]
            bodies.append({'id': index + 1, 'parent': index, 'matrix': origin.tolist(),
                'pivot': np.eye(4).tolist(), 'joint_name': name, 'axis': axis,
                'kind': 'revolute', 'limit': [-3.14, 3.14]})
            rotation = np.eye(4); rotation[:3, :3] = Rotation.from_rotvec(np.array(axis) * .2).as_matrix()
            transform = transform @ origin @ rotation
        mount = np.eye(4); mount[0, 3] = .08
        bodies.append({'id': 7, 'parent': 6, 'matrix': mount.tolist()})
        tcp = np.eye(4); tcp[2, 3] = .05
        geoms = [{'id': i, 'body': body, 'robot': True, 'matrix': np.eye(4).tolist(),
                  'kind': 'sphere', 'size': [.02, 0., 0.]} for i, body in enumerate((0, 6, 7))]
        obstacle = np.eye(4); obstacle[0, 3] = 10.
        geoms.append({'id': 3, 'body': 8, 'robot': False, 'matrix': obstacle.tolist(),
                      'kind': 'box', 'size': [.1, .1, .1]})
        snapshot = {'_runtime_collision': {'schema_version': 1, 'bodies': bodies,
            'geometries': geoms, 'root': 0, 'payload_bodies': [7], 'base_world': np.eye(4).tolist(),
            'joint_names': names, 'joints': [.2] * 6, 'tcp_body': 6, 'tcp_local': tcp.tolist(),
            'tcp_world': (transform @ tcp).tolist(), 'policy': {
                'schema_version': 1, 'allowed_pairs': [[1, 2]], 'distance_overrides': [],
                'safety_distance': 0., 'avoid_collision': True,
                'held_name': 'test_payload', 'fingerprint': 'test_six_joint_payload'}}}
        with CuroboClient({'conversion': {'warmup_iterations': 1}, 'request_timeout': 240}) as client:
            trajectory = client.plan({'target': {'type': 'joint', 'joint_positions': [.23] + [.2] * 5},
                'constraints': {'safety_distance': 0.}, 'planning': {'max_attempts': 2}}, snapshot)
            self.assertGreater(len(trajectory['waypoints']), 1)
            policy = trajectory['diagnostics']['curobo']['collision_policy']
            self.assertEqual(policy['held_name'], 'test_payload')
            self.assertEqual(policy['scope'], 'ik_graph_trajopt_validation')
            self.assertFalse(policy['cuda_graph_enabled'])
            self.assertEqual(trajectory['diagnostics']['curobo']['execution_mode'], 'eager')
            self.assertEqual(trajectory['diagnostics']['curobo']['warmup_strategy'], 'lazy')
            self.assertEqual(trajectory['diagnostics']['curobo']['num_trajopt_seeds'], 1)
            # Same planner/capacity, changed world: reject before search and
            # restore a feasible scene without stale cached graph permissions.
            snapshot['_runtime_collision']['geometries'][-1]['matrix'][0][3] = 0.
            with self.assertRaisesRegex(RuntimeError, 'Runtime start is infeasible'):
                client.plan({'target': {'type': 'joint', 'joint_positions': [.23] + [.2] * 5},
                    'constraints': {'safety_distance': 0.}}, snapshot)
            snapshot['_runtime_collision']['policy']['safety_distance'] = .003
            with self.assertRaisesRegex(RuntimeError, 'Runtime start is infeasible'):
                client.plan({'target': {'type': 'joint', 'joint_positions': [.23] + [.2] * 5},
                    'constraints': {'safety_distance': .003}}, snapshot)
            snapshot['_runtime_collision']['policy']['safety_distance'] = 0.
            snapshot['_runtime_collision']['geometries'][-1]['matrix'][0][3] = 10.
            restored = client.plan({'target': {'type': 'joint', 'joint_positions': [.23] + [.2] * 5},
                'constraints': {'safety_distance': 0.}}, snapshot)
            self.assertGreater(len(restored['waypoints']), 1)
            from itertools import combinations
            snapshot['_runtime_collision']['policy'].update(
                avoid_collision=False, allowed_pairs=[list(p) for p in combinations(range(4), 2)],
                fingerprint='explicit_disable_collision')
            snapshot['_runtime_collision']['geometries'][-1]['matrix'][0][3] = 0.
            exempted = client.plan({'target': {'type': 'joint', 'joint_positions': [.23] + [.2] * 5},
                'constraints': {'safety_distance': 0., 'avoid_collision': False}}, snapshot)
            self.assertGreater(len(exempted['waypoints']), 1)


if __name__ == '__main__':
    unittest.main()
