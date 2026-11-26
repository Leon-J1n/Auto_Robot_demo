"""Private JSON-line worker. Official cuRobo source is loaded only in this process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
from collections import OrderedDict

PACKAGE=Path(__file__).resolve().parent
PROJECT=PACKAGE.parents[1]
DAEMON_SOCKET = None
if len(sys.argv) >= 3 and sys.argv[1] == '--daemon-socket':
    DAEMON_SOCKET = Path(sys.argv[2]).expanduser().resolve()
    CONFIG = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
else:
    CONFIG=json.loads(sys.argv[1]) if len(sys.argv)>1 else {}
CACHE=Path(CONFIG.get('cache_dir',PROJECT/'.cache/curobo')).resolve()
for env,sub in {'WARP_CACHE_PATH':'warp','TORCH_EXTENSIONS_DIR':'torch','CUDA_CACHE_PATH':'cuda',
                'MPLCONFIGDIR':'mpl','TEMP':'temp','TMP':'temp'}.items():
    directory=CACHE/'runtime_cache'/sub
    directory.mkdir(parents=True,exist_ok=True)
    os.environ[env]=str(directory)
packages=CONFIG.get('dependency_path')
if packages:
    packages=Path(packages)
    if not packages.is_absolute():
        packages=PROJECT/packages
    sys.path.insert(0,str(packages))
    torch_lib=packages/'torch/lib'
    os.environ['PATH']=str(torch_lib)+os.pathsep+os.environ['PATH']
    if (packages/'nvidia/cuda_nvrtc').is_dir():
        os.environ['CUDA_PATH']=str(packages/'nvidia/cuda_nvrtc')
sys.path.insert(0,str(PROJECT))
sys.path.insert(0,str(PACKAGE/'source'))
PROTOCOL=sys.stdout
sys.stdout=sys.stderr


class GpuPathRejected(ValueError):
    pass


class CuroboNoPath(RuntimeError):
    pass


class Engine:
    LINEAR_IK_BATCH_SIZE=4

    def __init__(self, progress=None):
        import numpy as np
        self.np=np
        from libraries.curobo.conversion.assets import AssetStore
        from libraries.curobo.conversion.scene import SceneConverter
        self.AssetStore,self.SceneConverter=AssetStore,SceneConverter
        self.converters={}
        self.planner=None
        # The active fields below are a view onto one entry in this cache.
        # Keeping the entries alive lets the daemon switch back to a robot
        # without rebuilding its MotionPlanner and CUDA graphs.
        self.planner_cache=OrderedDict()
        self.linear_batch_ik=None
        self.planner_key=None
        self.locked=None
        self.world_key=None
        self.fixed_scene_template=None
        self.fixed_scene_signature=None
        self.defaults=json.loads((PACKAGE/'config/defaults.json').read_text('utf-8'))
        self.defaults.update(CONFIG.get('conversion',{}))
        self.gpu_loaded=False
        self.progress=progress
        self.stage='initialization'

    def _activate_cached_planner(self, key):
        entry=self.planner_cache.pop(key)
        self.planner_cache[key]=entry
        for name,value in entry.items():
            setattr(self,name,value)

    def _save_active_planner(self):
        if self.planner_key is None or self.planner is None:
            return
        entry={name:getattr(self,name,None) for name in (
            'planner','linear_batch_ik','planner_key','locked','world_key',
            'capacity','robot_model','robot_path','settings','warmup_strategy',
            'policy_key','convex_self_pairs','cuda_graphs')}
        self.planner_cache[self.planner_key]=entry

    def _destroy_planner_entry(self, entry):
        linear=entry.get('linear_batch_ik')
        if linear is not None:
            linear.destroy()
        planner=entry.get('planner')
        if planner is not None:
            planner.destroy()

    def _trim_planner_cache(self):
        limit=self.defaults.get('planner_cache_size',2)
        if isinstance(limit,bool) or not isinstance(limit,int) or limit < 1:
            raise ValueError('planner_cache_size must be a positive integer')
        while len(self.planner_cache)>limit:
            key,entry=self.planner_cache.popitem(last=False)
            if key==self.planner_key:
                self.planner_cache[key]=entry
                break
            self._destroy_planner_entry(entry)
            self.torch.cuda.empty_cache()

    def set_stage(self, stage):
        self.stage=stage
        if self.progress is not None:
            self.progress(stage)

    def load_gpu(self):
        if self.gpu_loaded:
            return
        self.set_stage('gpu_initialization')
        import torch
        import warp
        warp.config.use_precompiled_headers=False
        import curobo
        import curobo._src.runtime as runtime
        runtime.cache_dir=str(CACHE/'runtime_cache/curobo')
        if not Path(curobo.__file__).resolve().is_relative_to(PACKAGE/'source'):
            raise RuntimeError('An unexpected cuRobo installation was loaded')
        if not torch.cuda.is_available():
            raise RuntimeError('cuRobo requires an NVIDIA GPU accessible to CUDA PyTorch')
        self.torch=torch
        self.gpu_loaded=True

    def convert(self,payload):
        scene=payload['scene']
        if '_runtime_collision' in scene:
            from libraries.curobo.conversion.runtime import convert_runtime
            converted = convert_runtime(scene, CACHE)
            if converted.get('fixed_simulation_scene'):
                converted = self._normalize_fixed_scene(converted)
            return converted
        ols={**scene.get('object_library',{}), **CONFIG.get('ols',{})}
        base=ols.get('base_url','http://127.0.0.1:8000')
        uid=ols.get('uid',0)
        key=(base,uid)
        if key not in self.converters:
            self.converters[key]=self.SceneConverter(self.AssetStore(base,uid,CACHE),CACHE)
        return self.converters[key].convert(scene,robot_object_id=payload.get('robot_object_id'),tool=payload.get('tool'))

    def _normalize_fixed_scene(self, converted):
        """Reuse the first fixed robot/table model across independent tasks.

        Runtime export includes current joint transforms in its assembly key.
        That is correct for a standalone snapshot, but would make every new
        task look like a different robot model to the persistent planner. Keep
        static geometry and world data from the first fixed scene while
        carrying forward state-dependent fields from the new snapshot.
        """
        from copy import deepcopy
        from libraries.curobo.conversion.assets import fingerprint
        signature = fingerprint({key: converted.get(key) for key in (
            'geometry_links', 'shape_keys', 'world', 'base_world', 'tool_spec',
            'joint_names', 'active_names', 'collision_ignores', 'convex_self')})
        if self.fixed_scene_template is None or self.fixed_scene_signature != signature:
            self.fixed_scene_template = deepcopy(converted)
            self.fixed_scene_signature = signature
            return converted
        static = self.fixed_scene_template
        result = deepcopy(converted)
        for key in ('robot_key', 'robot_dir', 'urdf', 'base_link', 'world',
                    'base_world', 'tool_spec', 'joint_names', 'active_names',
                    'geometry_links', 'shape_keys', 'world_geoms',
                    'primitive_spheres', 'scene_limits', 'convex_self'):
            if key in static:
                result[key] = deepcopy(static[key])
        # Keep current task state and current collision policy from result.
        return result

    def robot_config(self,converted,margin):
        if 'collision_policy' in converted:
            from libraries.curobo.runtime_robot import build_runtime_robot
            return build_runtime_robot(converted, self.defaults, CACHE, self.compiled_policy)
        from libraries.curobo.conversion.assets import asset_reference, fingerprint
        from curobo.robot_builder import RobotBuilder
        import yaml
        directory=Path(converted['robot_dir'])
        clips=dict(self.defaults.get('clip_links',{}))
        verified=self.defaults.get('verified_mount_clip')
        # This is an explicit asset-specific mounting constraint, never a global z=0 guess.
        if verified:
            model_path=Path(converted['urdf'])
            import xml.etree.ElementTree as ET
            base=converted['base_link']
            asset_is_verified=any('/robot/1/' in n.get('filename','').replace('\\','/')
                                  for n in ET.parse(model_path).getroot().findall(f"link[@name='{base}']/visual/geometry/mesh"))
            if base.endswith('_'+verified['base_link']) and asset_is_verified:
                from libraries.curobo.conversion.urdf import origin
                import trimesh
                minimum=float('inf')
                for visual in ET.parse(model_path).getroot().findall(f"link[@name='{base}']/visual"):
                    mesh_node=visual.find('geometry/mesh')
                    if mesh_node is None:
                        raise RuntimeError('The verified base now contains a non-mesh visual; recheck its mounting plane')
                    mesh=trimesh.load(mesh_node.get('filename'),force='mesh',process=False)
                    scale=self.np.asarray(mesh_node.get('scale','1 1 1').split(),dtype=float)
                    vertices=self.np.c_[self.np.asarray(mesh.vertices)*scale,self.np.ones(len(mesh.vertices))]
                    vertices=vertices @ origin(visual.find('origin')).T
                    minimum=min(minimum,float(vertices[:,2].min()))
                if minimum < verified['offset']-1e-6:
                    raise RuntimeError('The base mesh extends below the configured mounting plane; refusing to clip real geometry')
                clips[base]=(verified['axis'],verified['offset'])
        settings={k:self.defaults[k] for k in ('sphere_density','prune_collisions','use_collision_mesh')}
        settings['clip_links']=clips
        if converted.get('collision_ignores'):
            settings['custom_ignore']=converted['collision_ignores']
        key=fingerprint(settings)[:16]
        robot_file=directory/f'robot_{key}.yml'
        if not robot_file.is_file():
            builder=RobotBuilder(converted['urdf'],str(directory),tool_frames=[converted['tool_frame']])
            builder.fit_collision_spheres(sphere_density=settings['sphere_density'],
                use_collision_mesh=settings['use_collision_mesh'],clip_links=clips,compute_metrics=True)
            builder.compute_collision_matrix(prune_collisions=settings['prune_collisions'],
                custom_ignore=settings.get('custom_ignore'))
            builder.save(builder.build(),str(robot_file))
            report={'official_commit':self.defaults['official_commit'],'settings':settings,
                    'metrics':{name:vars(value) for name,value in builder.link_metrics.items()}}
            (directory/f'fit_{key}.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
        model=yaml.safe_load(robot_file.read_text('utf-8'))
        kin=model['kinematics']
        # Keep the complete official cspace, including weights. Do not replace it with a partial dict.
        if any(kin.get('cspace',{}).get(name) is None for name in ('cspace_distance_weight','null_space_weight')):
            raise RuntimeError('Official cspace weights are missing; refusing a partial planning model')
        kin['lock_joints']=converted['locked_joints']
        names=kin['cspace']['joint_names']
        kin['cspace']['default_joint_position']=[converted['positions'].get(n,0.) for n in names]
        kin['collision_sphere_buffer']={name:(0. if name==converted['base_link'] else margin)
                                        for name in kin['collision_link_names']}
        return model,str(robot_file),settings

    def prepare(self,converted,request=None,*,check_endpoints=False,startup_warmup=False):
        self.load_gpu()
        import curobo._src.runtime as runtime
        from libraries.curobo.conversion.assets import fingerprint
        from curobo.motion_planner import MotionPlanner,MotionPlannerCfg
        from curobo.kinematics import KinematicsCfg
        from curobo.scene import Scene
        from curobo.types import DeviceCfg
        constraints=(request or {}).get('constraints',{})
        is_runtime='collision_policy' in converted
        # The full articulated model reproduces an illegal-address replay in
        # IK/LBFGS. Use eager GPU execution for this bridge until that capture
        # path is qualified; the asset-only entry retains upstream graphs.
        graph_setting=self.defaults.get('runtime_cuda_graphs',False)
        if converted.get('planning_proxy') and (request or {}).get('target',{}).get('type') == 'joint':
            graph_setting=self.defaults.get('runtime_proxy_cuda_graphs', True)
        if not isinstance(graph_setting,bool):
            raise ValueError('runtime_cuda_graphs must be a boolean')
        self.cuda_graphs=graph_setting if is_runtime else True
        runtime.cuda_graphs=self.cuda_graphs
        margin=float(constraints.get('safety_distance',0.))
        position_tolerance=float(constraints.get('position_tolerance',.005))
        orientation_tolerance=float(constraints.get('orientation_tolerance',self.np.rad2deg(.05)))
        self.compiled_policy = None
        if 'collision_policy' in converted:
            from libraries.curobo.policy import compile_policy
            self.compiled_policy = compile_policy(converted)
        self_rules = None if self.compiled_policy is None else self.compiled_policy['self_ignores']
        key=fingerprint([converted['robot_key'],converted.get('collision_ignores',{}),self.defaults,
                         margin,position_tolerance,orientation_tolerance,self_rules,self.cuda_graphs,
                         converted.get('convex_self')])
        world_key=fingerprint(converted['world'])
        previous_world_key=self.world_key
        world_changed=previous_world_key!=world_key
        required={'cuboid':len(converted['world'].get('cuboid',{})),
            'mesh':sum(len(converted['world'].get(kind,{})) for kind in ('mesh','sphere','cylinder','capsule'))}
        cached=self.planner_cache.get(key)
        if cached is not None and any(count>cached.get('capacity',{}).get(kind,0)
                                      for kind,count in required.items()):
            # Collision buffers have fixed capacity. Rebuild only this entry.
            self.planner_cache.pop(key, None)
            self._destroy_planner_entry(cached)
            self.torch.cuda.empty_cache()
            cached=None
        planner_reused=cached is not None
        if cached is not None:
            self._activate_cached_planner(key)
        else:
            self.planner=None
            self.linear_batch_ik=None
            self.planner_key=None
            import gc
            gc.collect()
            self.torch.cuda.empty_cache()
            self.set_stage('robot_model')
            model,robot_path,settings=self.robot_config(converted,margin)
            self.robot_model=model
            self.robot_path=robot_path
            self.settings=settings
            counts={kind:max(8,count) for kind,count in required.items()}
            self.capacity=counts
            self.set_stage('planner_initialization')
            # Native scene creation is also used for validation before the planner is built.
            world=Scene.create(converted['world']).get_collision_check_world()
            kwargs={'position_tolerance':position_tolerance,
                    'orientation_tolerance':float(self.np.deg2rad(orientation_tolerance)),
                    'use_cuda_graph':self.cuda_graphs}
            if self.compiled_policy is not None:
                from itertools import combinations
                seed_count=self.defaults.get('runtime_num_trajopt_seeds',1)
                if isinstance(seed_count,bool) or not isinstance(seed_count,int) or seed_count < 1:
                    raise ValueError('runtime_num_trajopt_seeds must be a positive integer')
                kwargs['num_trajopt_seeds']=seed_count
                # The upstream self kernel requires a nonempty pair list.
                # An empty list is valid when the explicit host policy exempts
                # every self pair, including avoid_collision=false.
                ignores=self.compiled_policy['self_ignores']
                kwargs['self_collision_check']=any(
                    second not in ignores[first] for first,second in combinations(ignores,2))
            robot=model
            self.convex_self_pairs=0
            if converted.get('convex_self') and self.compiled_policy is not None:
                from curobo._src.types.robot import RobotCfg
                from libraries.curobo.convex_self import ConvexSelf
                robot=RobotCfg.create(model)
                convex=ConvexSelf(converted['convex_self'],robot,converted,self.compiled_policy)
                self.convex_self_pairs=convex.pair_count
            self.planner=MotionPlanner(MotionPlannerCfg.create(robot=robot,scene_model=world,
                collision_cache=counts or None,**kwargs))
            if self.compiled_policy is not None:
                from libraries.curobo.policy_checker import PolicyCollisionChecker
                scene = self.planner.scene_collision_checker
                scene.checker = PolicyCollisionChecker(device_cfg=scene.device_cfg)
                scene.checker.configure(scene, self.planner.kinematics, converted, self.compiled_policy)
            self.warmup_strategy=None
            self.planner_key=key
            self.locked=dict(converted['locked_joints'])
            self.world_key=world_key
        if cached is not None:
            if self.locked!=converted['locked_joints']:
                updated=KinematicsCfg.from_robot_yaml_file(self.robot_model,lock_joints=converted['locked_joints'])
                self.planner.kinematics.update_kinematics_config(updated.kinematics_config)
                self.planner.reset_seed()
                if self.linear_batch_ik is not None:
                    self.linear_batch_ik.kinematics.update_kinematics_config(updated.kinematics_config)
                    self.linear_batch_ik.reset_seed()
                self.locked=dict(converted['locked_joints'])
            if self.world_key!=world_key:
                self.planner.update_world(Scene.create(converted['world']).get_collision_check_world())
                self.world_key=world_key
            if self.compiled_policy is not None:
                scene = self.planner.scene_collision_checker
                scene.checker.configure(scene, self.planner.kinematics, converted, self.compiled_policy)
                if getattr(self, 'policy_key', None) != self.compiled_policy['fingerprint']:
                    self.planner.reset_seed()
                    if self.linear_batch_ik is not None:
                        self.linear_batch_ik.reset_seed()
                    if self.planner.graph_planner is not None:
                        self.planner.graph_planner.reset_buffer()
        self.policy_key = None if self.compiled_policy is None else self.compiled_policy['fingerprint']
        report={'planner_reused':planner_reused}
        if self.compiled_policy is not None and check_endpoints:
            self.set_stage('endpoint_validation')
            self.validate_runtime_endpoints(converted, request)
        if self.warmup_strategy is None:
            self.set_stage('warmup')
            try:
                self.warmup_strategy=self.warmup(converted, request, is_runtime)
            except Exception:
                self.planner_key=None
                raise
        if startup_warmup and converted.get('fixed_simulation_scene') and self.warmup_strategy!='startup':
            self.set_stage('warmup')
            try:
                self.warmup_fixed_scene(converted)
            except Exception:
                self.planner_key=None
                raise
            self.warmup_strategy='startup'
        report.update(robot_yaml=self.robot_path,conversion_settings=self.settings,
            collision_spheres=sum(len(x) for x in self.robot_model['kinematics']['collision_spheres'].values()),
            world_counts={k:len(v) for k,v in converted['world'].items()},
            gpu=self.torch.cuda.get_device_name(0),tool=converted['tool_spec'],
            current_tool_pose=converted['current_tool_pose'],base_world=converted['base_world'],
            fixed_simulation_scene=bool(converted.get('fixed_simulation_scene')),
            joint_names=converted['joint_names'],notes=converted['notes'],
            official_commit=self.defaults['official_commit'],cuda_graph_enabled=self.cuda_graphs,
            execution_mode='cuda_graph' if self.cuda_graphs else 'eager',warmup_strategy=self.warmup_strategy,
            num_trajopt_seeds=self.planner.trajopt_solver.config.num_seeds)
        if self.compiled_policy is not None:
            from libraries.curobo.policy import GPU_DISTANCE_EPS_M
            report['collision_policy'] = {
                'fingerprint': self.compiled_policy['fingerprint'], 'scope': 'ik_graph_trajopt_validation',
                'allowed_pair_count': len(self.compiled_policy['allowed']),
                'arm_environment_pair_count': len(converted['collision_policy'].get('arm_environment_pairs', ())),
                'distance_override_count': len(self.compiled_policy['overrides']),
                'held_name': converted['collision_policy']['held_name'],
                'payload_bodies': converted['payload_bodies'],
                'convex_self_geometry_pairs': self.convex_self_pairs,
                'geometry_source': ('shared_simplified_proxy' if converted.get('planning_proxy') else 'measured_runtime'),
                'sphere_approximation': True, 'cuda_graph_enabled': self.cuda_graphs,
                'distance_numerical_tolerance_m': GPU_DISTANCE_EPS_M}
            if converted.get('planning_proxy'):
                report['planning_proxy'] = converted['planning_proxy']
        self._save_active_planner()
        self._trim_planner_cache()
        import yaml
        scene_path=Path(converted['robot_dir'])/'scene.yml'
        if world_changed or not scene_path.exists():
            scene_path.write_text(yaml.safe_dump(converted['world'],sort_keys=False),encoding='utf-8')
        report['scene_yaml']=str(scene_path)
        self.report=report
        return report

    def warmup(self,converted,request,is_runtime):
        iterations=int(self.defaults.get('runtime_warmup_iterations',0)
                       if is_runtime else self.defaults['warmup_iterations'])
        if iterations < 0:
            raise ValueError('Warmup iterations cannot be negative')
        strategy='lazy'
        if iterations:
            if is_runtime and (request or {}).get('target',{}).get('type')=='joint':
                strategy='cspace'
                state=self.joint_state(converted)
                for _ in range(iterations):
                    self.planner.plan_cspace(state.clone(),state.clone(),max_attempts=1)
                    self.planner.reset_seed()
            else:
                strategy='pose'
                self.planner.warmup(enable_graph=self.cuda_graphs,num_warmup_iterations=iterations)
        if self.cuda_graphs and self.compiled_policy is not None and self.planner.graph_planner is not None:
            # Recapture on the first real search to avoid an illegal-instruction
            # replay in the pinned runtime's feasibility graph.
            self.planner.graph_planner.feasibility_rollout.reset_cuda_graph()
        return strategy

    def warmup_fixed_scene(self,converted):
        from curobo.types import JointState

        self.planner.warmup(enable_graph=self.cuda_graphs,num_warmup_iterations=1)
        size=self.LINEAR_IK_BATCH_SIZE
        solver=self.linear_ik_solver(size)
        position=self.joint_state(converted).position.repeat(size,1)
        state=JointState.from_position(position,joint_names=self.planner.joint_names)
        goal=self.planner.compute_kinematics(state).tool_poses.as_goal()
        seed_count=min(32,self.planner.ik_solver.config.num_seeds)
        basis=.08*self.torch.eye(position.shape[-1],device=position.device)
        offsets=self.torch.cat((self.torch.zeros_like(position[:1]),basis,-basis))[:seed_count]
        bounds=self.planner.kinematics.config.kinematics_config.joint_limits.position
        seeds=self.torch.clamp(position[:,None]+offsets,min=bounds[0],max=bounds[1])
        solver.solve_pose(goal,current_state=state,seed_config=seeds,return_seeds=seed_count)
        solver.reset_seed()
        self.torch.cuda.synchronize()

    def validate_runtime_endpoints(self,converted,request):
        """Reject infeasible sphere-model endpoints before optimizer warmup/search.

        Use the graph planner's direct metrics rollout: it includes the same
        world pair policy, self-collision matrix and joint bounds as its search.
        This also avoids capturing a large feasibility graph just to reject an
        already invalid start. No collision thresholds are relaxed here.
        """
        points=[self.joint_state(converted).position]
        labels=['start']
        target=(request or {}).get('target',{})
        if target.get('type')=='joint':
            points.append(self.joint_state(converted,target['joint_positions']).position)
            labels.append('goal')
        samples=self.torch.cat(points,dim=0).unsqueeze(1)
        rollout=self.planner.graph_planner.auxiliary_rollout
        # Endpoint checks receive a tiny, variable-size batch.  A previously
        # captured action graph may have a different batch size (for example
        # 1108 trajectory points), so run this small check eagerly and restore
        # the graph setting afterwards.
        use_graph=getattr(rollout,'_use_cuda_graph',False)
        rollout._use_cuda_graph=False
        try:
            metrics=rollout.compute_metrics_from_action(samples)
        finally:
            rollout._use_cuda_graph=use_graph
        feasible=metrics.costs_and_constraints.get_feasible(
            include_all_hybrid=False,sum_horizon=True).reshape(-1).cpu().tolist()
        for label,valid in zip(labels,feasible):
            if not valid:
                index=labels.index(label)
                collection=metrics.costs_and_constraints.constraints
                violations={name:float(value[index].max().detach().cpu())
                    for name,value in zip(collection.names,collection.values)
                    if bool((value[index]>0).any())}
                raise ValueError(f'Runtime {label} is infeasible under the frozen collision policy, '
                    f'joint bounds and cuRobo sphere approximation; violations={violations}; '
                    'no trajectory was generated')

    def joint_state(self,converted,values=None):
        from curobo.types import JointState
        mapping=dict(converted['positions'])
        if values is not None:
            if len(values)!=len(converted['active_names']):
                raise ValueError('Joint target dimension disagrees with selected arm')
            mapping.update(zip(converted['active_names'],values))
        return JointState.from_position(self.torch.tensor([[mapping[n] for n in self.planner.joint_names]],
             device='cuda',dtype=self.torch.float32),joint_names=self.planner.joint_names)

    def joint_line_limits(self,converted,request):
        limits=self.planner.kinematics.config.kinematics_config.joint_limits
        arrays={name:getattr(limits,name).detach().cpu().numpy().copy()
            for name in ('velocity','acceleration','jerk')}
        timing=request.get('planning',{}).get('time_parameterization',{})
        for name,key in [('velocity','max_velocity'),('acceleration','max_acceleration'),('jerk','max_jerk')]:
            if key in timing and timing[key] is not None:
                requested=self.np.asarray(timing[key],dtype=float)
                if requested.shape != (len(converted['active_names']),) or not self.np.isfinite(requested).all() or (requested<=0).any():
                    raise ValueError(f'{key} must contain positive limits for every active joint')
                for joint,limit in zip(converted['active_names'],requested):
                    column=self.planner.joint_names.index(joint)
                    arrays[name][0,column]=max(arrays[name][0,column],-limit)
                    arrays[name][1,column]=min(arrays[name][1,column],limit)
        return arrays

    def plan_joint_direct(self,converted,request,current_state,goal_state):
        start=current_state.position[0].detach().cpu().numpy()
        goal=goal_state.position[0].detach().cpu().numpy()
        return self.plan_joint_waypoints(converted,request,self.np.stack((start,goal)))

    def plan_joint_waypoints(self,converted,request,waypoints):
        from libraries.curobo.linear import timed_joint_line
        from planning.constraints.collision_sampling import collision_sampling_settings
        constraints=request['constraints']
        resolution,_=collision_sampling_settings(request)
        try:
            samples,stamps,velocities,accelerations,jerks=timed_joint_line(
                waypoints,self.joint_line_limits(converted,request),
                velocity_scale=min(1.,float(constraints.get('velocity_scale',1.))),
                acceleration_scale=float(constraints.get('acceleration_scale',1.)),
                resolution_rad=min(resolution,.01),cartesian_step=.02)
        except ValueError as error:
            if 'exceeds collision validation capacity' not in str(error):
                raise
            return None
        joint_samples=self.torch.as_tensor(samples,device='cuda',dtype=self.torch.float32)
        metrics=self.planner.graph_planner.auxiliary_rollout.compute_metrics_from_action(
            joint_samples.unsqueeze(1))
        feasible=metrics.costs_and_constraints.get_feasible(
            include_all_hybrid=False,sum_horizon=True).reshape(-1)
        if not bool(feasible.all()):
            return None
        return {'joint_names':self.planner.joint_names,'positions':samples,'timestamps':stamps,
            'velocity':velocities,'acceleration':accelerations,'jerk':jerks}

    def linear_ik_solver(self,batch_size):
        if self.linear_batch_ik is None:
            from curobo._src.solver.solver_ik import IKSolver
            from curobo._src.solver.solver_ik_cfg import IKSolverCfg
            base=self.planner.ik_solver.config
            config=IKSolverCfg.create(robot=base.robot_config,optimizer_configs=['ik/lbfgs_ik.yml'],
                device_cfg=base.device_cfg,num_seeds=base.num_seeds,
                position_tolerance=base.position_tolerance,orientation_tolerance=base.orientation_tolerance,
                self_collision_check=base.self_collision_check,use_cuda_graph=self.cuda_graphs,
                max_batch_size=batch_size)
            config.exit_early=False
            self.linear_batch_ik=IKSolver(config,self.planner.scene_collision_checker)
        return self.linear_batch_ik

    def plan_linear(self,converted,request,current_state):
        from curobo.types import GoalToolPose, JointState
        from libraries.curobo.linear import (LinearBranchNotFound, LinearCandidateRejected, interpolate_line, line_spec,
                                             offset_world_target, search_ik_branches, search_linear_targets,
                                             timed_joint_line, validate_line)
        from planning.constraints.collision_sampling import collision_sampling_settings
        torch=self.torch
        horizon=self.planner.trajopt_solver.action_horizon
        ik_time=0.
        branch_batch_size=self.LINEAR_IK_BATCH_SIZE
        offsets_world=request.get('strategy_params',{}).get('linear_target_offsets_world',[])
        candidate_requests=[request,*({**request,'target':offset_world_target(request['target'],offset)}
                                      for offset in offsets_world)]
        candidates={}
        rejected=[]
        for index,candidate in enumerate(candidate_requests):
            try:
                candidates[index]=line_spec(converted,candidate,horizon)
            except LinearCandidateRejected as error:
                if not offsets_world:
                    raise
                rejected.append(str(error))
                continue
        seed_count=min(32,self.planner.ik_solver.config.num_seeds)
        joint_limits=self.planner.kinematics.config.kinematics_config.joint_limits
        joint_bounds=joint_limits.position
        def tool_goal(position,quaternion):
            return GoalToolPose(tool_frames=self.planner.tool_frames,
                position=torch.as_tensor(position,device='cuda',dtype=torch.float32).view(1,1,1,1,3),
                quaternion=torch.as_tensor(quaternion,device='cuda',dtype=torch.float32).view(1,1,1,1,4))

        dof=current_state.position.shape[-1]
        basis=.08*torch.eye(dof,device='cuda')
        offsets=torch.cat((torch.zeros((1,dof),device='cuda'),basis,-basis))[:seed_count]

        def solve_goals(goals,seeds):
            nonlocal ik_time
            batch_size=len(seeds)
            goal=goals[0]
            solver=self.planner.ik_solver
            if batch_size > 1:
                solver=self.linear_ik_solver(branch_batch_size)
                if batch_size < branch_batch_size:
                    seeds=self.np.concatenate((seeds,self.np.repeat(seeds[:1],branch_batch_size-batch_size,axis=0)))
                    goals=[*goals,*([goal]*(branch_batch_size-batch_size))]
                goal=GoalToolPose(tool_frames=goal.tool_frames,
                    position=torch.cat([item.position for item in goals]),
                    quaternion=torch.cat([item.quaternion for item in goals]))
            previous=JointState.from_position(torch.as_tensor(seeds,device='cuda',dtype=torch.float32),
                                               joint_names=self.planner.joint_names)
            seed_configs=torch.clamp(previous.position.unsqueeze(1)+offsets.unsqueeze(0),
                                     min=joint_bounds[0],max=joint_bounds[1])
            solved=solver.solve_pose(goal,current_state=previous,
                                                     seed_config=seed_configs,return_seeds=seed_count)
            ik_time+=float(solved.solve_time)
            solutions=solved.solution[:batch_size].detach().cpu().numpy()
            success=solved.success[:batch_size].detach().cpu().numpy()
            return [row[mask] for row,mask in zip(solutions,success)]

        constraints=request['constraints']
        limit_arrays=self.joint_line_limits(converted,request)
        resolution,_=collision_sampling_settings(request)
        rollout=self.planner.graph_planner.auxiliary_rollout

        def validate_path(targets,joints):
            try:
                samples,stamps,velocities,accelerations,jerks=timed_joint_line(
                    joints,limit_arrays,velocity_scale=min(1.,float(constraints['velocity_scale'])),
                    acceleration_scale=float(constraints['acceleration_scale']),resolution_rad=min(resolution,.01))
            except ValueError as error:
                if 'exceeds collision validation capacity' not in str(error):
                    raise
                raise LinearCandidateRejected(str(error)) from error
            joint_samples=torch.as_tensor(samples,device='cuda',dtype=torch.float32)
            metrics=rollout.compute_metrics_from_action(joint_samples.unsqueeze(1))
            feasible=metrics.costs_and_constraints.get_feasible(
                include_all_hybrid=False,sum_horizon=True).reshape(-1)
            if not bool(feasible.all()):
                failed=int(torch.nonzero(~feasible)[0,0])
                violations={name:float(value[failed].max().detach().cpu())
                    for name,value in zip(metrics.costs_and_constraints.constraints.names,
                                          metrics.costs_and_constraints.constraints.values)
                    if bool((value[failed]>0).any())}
                raise LinearCandidateRejected(f'cuRobo linear trajectory collides or exceeds joint bounds at sample {failed}: {violations}')
            state=JointState.from_position(joint_samples,joint_names=self.planner.joint_names)
            tool=self.planner.kinematics.compute_kinematics(state).tool_poses
            try:
                validation=validate_line(tool.position.detach().cpu().numpy().reshape(-1,3),
                    tool.quaternion.detach().cpu().numpy().reshape(-1,4),targets,
                    position_tolerance=float(constraints['position_tolerance']),
                    orientation_tolerance=float(self.np.deg2rad(constraints['orientation_tolerance'])))
            except ValueError as error:
                raise LinearCandidateRejected(str(error)) from error
            return {'joint_names':self.planner.joint_names,'positions':samples,'timestamps':stamps,
                'velocity':velocities,'acceleration':accelerations,'jerk':jerks,
                'validation':validation}

        deadline=time.monotonic()+float(request['planning']['planning_time'])
        strategy=request.get('strategy_params',{})
        backtrack_budget=strategy.get('linear_ik_backtrack_budget')
        if backtrack_budget is not None and (type(backtrack_budget) is not int or backtrack_budget < 0):
            raise ValueError('linear_ik_backtrack_budget must be a nonnegative integer')
        initial=current_state.position[0].detach().cpu().numpy()
        max_joint_step=float(strategy.get('max_joint_step',0.4))
        def plan_target(index,branch_diagnostics):
            targets=interpolate_line(candidates[index])
            waypoints=[tool_goal(position,quaternion)
                       for position,quaternion in zip(targets[0][1:],targets[1][1:])]
            max_ik_calls=128 if backtrack_budget is None else min(128,len(waypoints)+backtrack_budget)
            return search_ik_branches(initial,len(waypoints),
                lambda waypoint,seed:solve_goals([waypoints[waypoint]],seed[None])[0],
                lambda joints:validate_path(targets,joints),
                max_joint_step=max_joint_step,max_ik_calls=max_ik_calls,
                deadline=deadline,diagnostics=branch_diagnostics,
                solve_batch=lambda waypoint,seeds:solve_goals([waypoints[waypoint]]*len(seeds),seeds),
                batch_size=branch_batch_size)

        if not offsets_world:
            branch_diagnostics={}
            result=plan_target(0,branch_diagnostics)
            return dict(result,solve_time=ik_time,branch_search=branch_diagnostics)

        def solve_endpoints(indices):
            goals=[tool_goal(candidates[index][2],candidates[index][3].as_quat()[[3,0,1,2]])
                   for index in indices]
            return solve_goals(goals,self.np.repeat(initial[None],len(goals),axis=0))

        batch_diagnostics={'candidate_count':len(candidate_requests)}
        selected=search_linear_targets(list(candidates),solve_endpoints,plan_target,
            deadline=deadline,batch_size=branch_batch_size,diagnostics=batch_diagnostics)
        if selected is None:
            reason='no collision-free endpoint IK' if candidates else rejected[-1]
            raise LinearBranchNotFound(f'No cuRobo linear target passed GPU validation: {reason}')
        index,result=selected
        batch_diagnostics['selected_index']=index
        return dict(result,solve_time=ik_time,target_batch=batch_diagnostics,
                    branch_search=batch_diagnostics['waypoint_searches'][-1])

    def plan_pose_candidates(self,converted,request,current_state):
        from curobo.types import GoalToolPose, JointState
        from libraries.curobo.conversion.scene import goal_in_base, official_pose

        targets=[request['target'],*request['strategy_params']['pose_target_candidates_world']]
        poses=[official_pose(goal_in_base(target,converted['base_world'],converted['current_tool_pose']))
               for target in targets]
        torch=self.torch
        size=self.LINEAR_IK_BATCH_SIZE
        start=time.perf_counter()
        deadline=start+request['planning']['planning_time']
        seed_state=JointState.from_position(current_state.position.repeat(size,1),
                                           joint_names=self.planner.joint_names)
        solver=self.linear_ik_solver(size)
        batches=0
        for first in range(0,len(poses),size):
            if time.perf_counter()>=deadline:
                raise TimeoutError('cuRobo pose candidate planning exceeded its budget')
            group=poses[first:first+size]
            pose=torch.as_tensor(group+[group[0]]*(size-len(group)),device='cuda',dtype=torch.float32)
            goal=GoalToolPose(tool_frames=self.planner.tool_frames,
                              position=pose[:,:3].reshape(size,1,1,1,3).contiguous(),
                              quaternion=pose[:,3:].reshape(size,1,1,1,4).contiguous())
            solved=solver.solve_pose(goal,current_state=seed_state,return_seeds=1)
            batches+=1
            for row in range(len(group)):
                if not bool(solved.success[row,0]):
                    continue
                state=JointState.from_position(solved.solution[row,:1],joint_names=self.planner.joint_names)
                direct=self.plan_joint_direct(converted,request,current_state,state)
                if direct is not None:
                    return direct, {'selected_index':first+row,'candidate_count':len(targets),'ik_batches':batches,
                                    'used_trajopt':False}

        strategy={key:value for key,value in request['strategy_params'].items() if key!='pose_target_candidates_world'}
        for index,target in enumerate(targets):
            remaining=deadline-time.perf_counter()
            if remaining<=0.:
                raise TimeoutError('cuRobo pose candidate planning exceeded its budget')
            try:
                result=self.plan(converted,{**request,'target':target,'strategy_params':strategy,
                    'planning':{**request['planning'],'planning_time':remaining}})
            except (GpuPathRejected, CuroboNoPath):
                continue
            elapsed=time.perf_counter()-start
            result['planning_time']=elapsed
            result['diagnostics']['pose_target_batch']={'selected_index':index,'candidate_count':len(targets),
                'ik_batches':batches,'used_trajopt':True}
            return result,None
        raise CuroboNoPath('No cuRobo approach candidate passed GPU validation')

    def plan(self,converted,request):
        self.set_stage('planning')
        from libraries.curobo.conversion.urdf import pose, rigid,vector
        from libraries.curobo.conversion.scene import official_pose,goal_in_base
        from curobo.types import GoalToolPose
        np=self.np
        torch=self.torch
        q=self.joint_state(converted)
        from planning.constraints.collision_sampling import collision_sampling_settings
        resolution=min(collision_sampling_settings(request)[0],.01)
        joint_validation_samples=None
        goal=request['target']
        target_type=goal['type']
        linear=request.get('motion',{}).get('path_type')=='linear'
        pose_batch=None
        if target_type=='pose' and not linear and request.get('strategy_params',{}).get('pose_target_candidates_world'):
            torch.cuda.synchronize()
            start=time.perf_counter()
            planned,pose_batch=self.plan_pose_candidates(converted,request,q)
            if pose_batch is None:
                return planned
        elif linear:
            if target_type!='pose':
                raise ValueError('cuRobo linear motion requires a pose target')
            call=lambda:self.plan_linear(converted,request,q)
        elif target_type=='joint':
            goal_state=self.joint_state(converted,goal['joint_positions'])
            def call():
                nonlocal joint_validation_samples
                direct=self.plan_joint_direct(converted,request,q,goal_state)
                if direct is not None:
                    return direct
                attempts=request.get('planning',{}).get('max_attempts',5)
                rejected=None
                deadline=start+request.get('planning',{}).get('planning_time',10.)
                for attempt in range(attempts):
                    if attempt and time.perf_counter()>=deadline:
                        raise TimeoutError('cuRobo joint planning exceeded its planning budget')
                    if attempt and rejected is not None:
                        graph=self.planner.graph_planner.find_path(q.position.clone(),goal_state.position.clone(),
                                                                  interpolate_waypoints=False)
                        if bool(graph.success[0]):
                            nodes=graph.plan_waypoints[0].detach().cpu().numpy()
                            graph_path=self.plan_joint_waypoints(converted,request,nodes)
                            if graph_path is not None:
                                return dict(graph_path,graph_path=True,solve_time=float(graph.solve_time))
                    result=self.planner.plan_cspace(goal_state,q,max_attempts=1,
                        enable_graph_attempt=0 if attempt else 1)
                    if result is None or not bool(result.success.any()):
                        continue
                    plan=result.get_interpolated_plan()
                    indices=[plan.joint_names.index(name) for name in converted['active_names']]
                    positions=plan.position.detach().cpu().numpy().reshape(-1,len(plan.joint_names))[:,indices]
                    if len(positions)<2 or not np.isfinite(positions).all():
                        raise RuntimeError('Official planner returned an invalid interpolated trajectory')
                    try:
                        joint_validation_samples=self.validate_gpu_path(positions,converted['active_names'],resolution)
                    except GpuPathRejected as error:
                        rejected=error
                        continue
                    return result
                if rejected is not None:
                    raise rejected
                return None
        elif target_type=='pose':
            matrix=goal_in_base(goal,converted['base_world'],converted['current_tool_pose'])
            transformed=official_pose(matrix)
            target=GoalToolPose(tool_frames=self.planner.tool_frames,
                position=torch.tensor([[[[transformed[:3]]]]],device='cuda',dtype=torch.float32),
                quaternion=torch.tensor([[[[transformed[3:]]]]],device='cuda',dtype=torch.float32))
            call=lambda:self.planner.plan_pose(target,q,max_attempts=request.get('planning',{}).get('max_attempts',5))
        else:
            raise ValueError('cuRobo target must resolve to pose or joint')
        if pose_batch is None:
            torch.cuda.synchronize()
            start=time.perf_counter()
            planned=call()
        torch.cuda.synchronize()
        elapsed=time.perf_counter()-start
        timed_joint=(target_type=='joint' or pose_batch is not None) and isinstance(planned,dict)
        joint_direct=timed_joint and not planned.get('graph_path')
        if linear or timed_joint:
            indices=[planned['joint_names'].index(n) for n in converted['active_names']]
            positions=planned['positions'][:,indices]
            stamps=planned['timestamps']
            dilation=1.
            linear_validation=planned['validation'] if linear else None
            solve_time=planned.get('solve_time',0.)
        else:
            result=planned
            if result is None or not bool(result.success.any()):
                raise CuroboNoPath('Official cuRobo did not find a feasible trajectory for this current scene/start/goal')
            selected=self.select_pose_seed(result,converted)
            if selected is not None:
                # The solver remains cuRobo; only the final feasible seed is
                # selected with the host project's motion-balance preference.
                planned={'joint_names':converted['joint_names'],
                         'positions':selected['positions'],
                         'timestamps':np.arange(len(selected['positions']))*selected['dt'],
                         'velocity':selected['velocities'],
                         'acceleration':selected['accelerations'],
                         'jerk':selected['jerks'],
                         'solve_time':float(result.solve_time),
                         'seed_selection':selected}
                timed_joint=True
                joint_direct=True
                indices=[converted['joint_names'].index(n) for n in converted['active_names']]
                positions=selected['positions']
                stamps=planned['timestamps']
                dilation=1.
                linear_validation=None
                solve_time=planned['solve_time']
            else:
                plan=result.get_interpolated_plan()
                indices=[plan.joint_names.index(n) for n in converted['active_names']]
                positions=plan.position.detach().cpu().numpy().reshape(-1,len(plan.joint_names))[:,indices]
                dt_value=getattr(plan,'dt',None)
                if dt_value is None:
                    dt_value=getattr(result,'interpolation_dt',.025)
                dt=float(torch.as_tensor(dt_value).flatten()[0])
                if not np.isfinite(dt) or dt<=0:
                    raise RuntimeError('Official trajectory has an invalid time step')
                constraints=request.get('constraints',{})
                dilation=max(1.,1./float(constraints.get('velocity_scale',1.)),
                             1./np.sqrt(float(constraints.get('acceleration_scale',1.))))
                stamps=np.arange(len(positions))*dt*dilation
                linear_validation=None
                solve_time=float(result.solve_time)
        if len(positions)<2 or not np.isfinite(positions).all():
            raise RuntimeError('Official planner returned an invalid interpolated trajectory')
        start_positions=np.array([converted['positions'][n] for n in converted['active_names']])
        if not np.allclose(positions[0],start_positions,atol=1e-3,rtol=0):
            raise RuntimeError('Trajectory start disagrees with current scene joints')
        for column,name in enumerate(converted['joint_names']):
            limit=converted['scene_limits'].get(name)
            if limit and (np.min(positions[:,column])<float(limit[0])-1e-5 or np.max(positions[:,column])>float(limit[1])+1e-5):
                raise ValueError(f'Trajectory violates scene joint limits: {name}')
        sample_count=joint_validation_samples if joint_validation_samples is not None else len(positions)
        if not (linear or timed_joint) and joint_validation_samples is None:
            validation_started=time.perf_counter()
            sample_count=self.validate_gpu_path(positions,converted['active_names'],resolution)
            elapsed+=time.perf_counter()-validation_started
        trajectory={'path_type':'linear' if linear else 'joint',
            'planning_mode':'direct' if linear or joint_direct else 'collision_free','joint_names':converted['joint_names'],
            'waypoints':positions.tolist(),'time_parameterized_waypoints':positions.tolist(),
            'timestamps':stamps.tolist(),'duration':float(stamps[-1]),'planning_time':elapsed,
            'diagnostics':{'backend':'curobo','curobo':self.report,'time_dilation':float(dilation),
                           'solve_time':solve_time,'total_time':elapsed if linear or timed_joint else float(result.total_time)}}
        if joint_direct:
            trajectory['diagnostics']['joint_direct_validation_samples']=len(planned['positions'])
            if pose_batch is not None:
                trajectory['diagnostics']['pose_target_batch']=pose_batch
            if isinstance(planned,dict) and planned.get('seed_selection') is not None:
                selection=planned['seed_selection']
                trajectory['diagnostics']['curobo_seed_selection']={
                    'selected_index':int(selection['seed_index']),
                    'candidate_count':int(selection['seed_count']),
                    'score':[float(value) for value in selection['score'][:3]],
                }
        elif timed_joint:
            trajectory['diagnostics']['joint_graph_validation_samples']=len(planned['positions'])
        if linear_validation is not None:
            trajectory['diagnostics']['linear_path_validation']=linear_validation
            if 'branch_search' in planned:
                trajectory['diagnostics']['linear_branch_search']=planned['branch_search']
            if 'target_batch' in planned:
                trajectory['diagnostics']['linear_target_batch']=planned['target_batch']
        trajectory['diagnostics']['gpu_collision_validation']={
            'samples':sample_count,'resolution_rad':resolution}
        for name,power in [('velocity',1),('acceleration',2),('jerk',3)]:
            value=planned[name] if linear or timed_joint else getattr(plan,name,None)
            if value is not None:
                array=(value if linear or timed_joint else
                       value.detach().cpu().numpy().reshape(-1,len(plan.joint_names)))[:,indices]/dilation**power
                trajectory[{'velocity':'velocities','acceleration':'accelerations','jerk':'jerks'}[name]]=array.tolist()
        return trajectory

    def validate_gpu_path(self,positions,joint_names,resolution):
        np,torch=self.np,self.torch
        indices=[joint_names.index(name) for name in self.planner.joint_names]
        positions=positions[:,indices]
        rollout=self.planner.graph_planner.auxiliary_rollout

        def samples():
            yield positions[0]
            for start,end in zip(positions[:-1],positions[1:]):
                steps=max(1,int(np.ceil(np.max(np.abs(end-start))/resolution)))
                for step in range(1,steps+1):
                    yield end if step==steps else start+(end-start)*(step/steps)

        from itertools import islice
        path=samples()
        checked=0
        while batch:=list(islice(path,64)):
            actions=torch.as_tensor(np.asarray(batch),device='cuda',dtype=torch.float32)
            metrics=rollout.compute_metrics_from_action(actions.unsqueeze(1))
            feasible=metrics.costs_and_constraints.get_feasible(
                include_all_hybrid=False,sum_horizon=True).reshape(-1)
            if not bool(feasible.all()):
                index=int(torch.nonzero(~feasible)[0,0])
                constraints=metrics.costs_and_constraints.constraints
                violations={name:float(value[index].max().detach().cpu())
                            for name,value in zip(constraints.names,constraints.values)
                            if bool((value[index]>0).any())}
                raise GpuPathRejected(f'cuRobo GPU collision or joint constraint at trajectory sample '
                                      f'{checked+index}: {violations}')
            checked+=len(batch)
        return checked

    def select_pose_seed(self, result, converted):
        """Select a feasible cuRobo seed using the host's motion preference.

        cuRobo still generates and optimizes every candidate.  This only
        changes the final tie-break among its feasible trajectories, avoiding
        a result whose travel is concentrated in one joint.
        """
        trajectory=getattr(result, 'interpolated_trajectory', None)
        success=getattr(result, 'success', None)
        if trajectory is None or success is None or trajectory.position.ndim != 4:
            return None
        positions=trajectory.position.detach().cpu().numpy()
        feasible=success.detach().cpu().numpy().reshape(-1)
        if positions.shape[0] != 1 or positions.shape[1] != len(feasible):
            return None
        indices=[trajectory.joint_names.index(name) for name in converted['active_names']]
        limits=[]
        for name in converted['active_names']:
            limit=converted.get('scene_limits',{}).get(name)
            limits.append(limit if limit is not None else (-self.np.pi,self.np.pi))
        widths=self.np.asarray(limits,dtype=float)[:,1]-self.np.asarray(limits,dtype=float)[:,0]
        widths=self.np.where((widths>1e-9)&self.np.isfinite(widths),widths,2*self.np.pi)
        scores=[]
        for seed_index,ok in enumerate(feasible):
            if not ok:
                continue
            path=positions[0,seed_index,:,indices]
            travel=self.np.sum(self.np.abs(self.np.diff(path,axis=0)),axis=0)
            normalized=travel/(widths*.25)
            total=float(normalized.sum())
            balance=float(self.defaults.get('runtime_joint_balance_weight',1.0)) * (
                float(self.np.dot(normalized,normalized))/total if total>1e-12 else 0.)
            score=(float(travel.sum())+float(travel.max())+balance,
                   float(travel.sum()),float(travel.max()),seed_index)
            scores.append((score,seed_index))
        if not scores:
            return None
        _,selected=min(scores,key=lambda item:item[0])
        def field(name):
            value=getattr(trajectory,name,None)
            return None if value is None else value.detach().cpu().numpy()[0,selected]
        dt=getattr(trajectory,'dt',None)
        dt_value=float(dt.detach().cpu().numpy()[0,selected]) if dt is not None else .025
        path=positions[0,selected,:,indices]
        return {'positions':path,'velocities':field('velocity'),'accelerations':field('acceleration'),
                'jerks':field('jerk'),'dt':dt_value,'seed_index':selected,
                'seed_count':len(feasible),'score':scores[[item[1] for item in scores].index(selected)][0]}


def _process_payload(payload, engine, request_started):
    """Process one protocol request and return a response dictionary."""
    fatal=False
    def progress(stage):
        elapsed=time.monotonic()-request_started
        print('@@CUROBO@@'+json.dumps({'event':'stage','stage':stage,
            'elapsed_s':elapsed,'request_id':payload.get('request_id')}),file=PROTOCOL,flush=True)
        print(f'cuRobo stage={stage} elapsed_s={elapsed:.3f} request_id={payload.get("request_id")}',
            file=sys.stderr,flush=True)
    try:
        engine.progress = progress
        if payload['op']=='doctor':
            engine.load_gpu()
            engine.torch.cuda.synchronize()
            kernel_error=None
            try:
                probe=engine.torch.ones((1,),device='cuda')
                probe.add_(1)
                engine.torch.cuda.synchronize()
            except Exception as probe_exc:
                kernel_error=str(probe_exc)
            result={'python':sys.version,'torch':engine.torch.__version__,
                'torch_cuda':engine.torch.version.cuda,'gpu':engine.torch.cuda.get_device_name(0),
                'compute_capability':engine.torch.cuda.get_device_capability(0),
                'torch_arches':engine.torch.cuda.get_arch_list(),
                'cuda_kernel_test': 'ok' if kernel_error is None else 'failed',
                'cuda_kernel_error': kernel_error,
                'official_commit':engine.defaults['official_commit'],'source':str(PACKAGE/'source')}
        elif payload['op']=='refresh':
            for converter in engine.converters.values():
                converter.store.refresh()
            result={'refreshed':True}
        elif payload['op']=='shutdown':
            result={'shutdown':True}
        else:
            started=time.perf_counter()
            engine.set_stage('conversion')
            converted=engine.convert(payload)
            conversion_time=time.perf_counter()-started
            if payload['op']=='inspect':
                result=converted
            else:
                request=payload.get('request')
                start=time.perf_counter()
                report=engine.prepare(converted,request,check_endpoints=payload['op']=='plan',
                    startup_warmup=payload['op']=='prepare')
                preparation_time=time.perf_counter()-start
                if payload['op']=='prepare':
                    result=report
                elif payload['op']=='plan':
                    result=engine.plan(converted,request)
                    result['diagnostics'].update(conversion_time=conversion_time,preparation_time=preparation_time)
                else:
                    raise ValueError('Unknown worker operation')
        return {'ok':True,'result':result}, False
    except Exception as exc:
        from libraries.curobo.runtime_control import fatal_cuda_error
        fatal=fatal_cuda_error(exc)
        traceback.print_exc(file=sys.stderr)
        return {'ok':False,'error':{'error_code':'CUROBO_CUDA_FATAL' if fatal else 'CUROBO_FAILED',
            'error_message':str(exc),'failed_stage':getattr(engine,'stage','initialization'),
            'recoverable':not fatal,'worker_restart_required':fatal,
            'details':{'log_file':str(CACHE/'worker.log')}}}, fatal


def _serve_daemon():
    """Serve line-delimited requests over a local Unix domain socket."""
    if DAEMON_SOCKET is None:
        raise RuntimeError('daemon socket path is missing')
    DAEMON_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    try:
        DAEMON_SOCKET.unlink()
    except FileNotFoundError:
        pass
    server=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(DAEMON_SOCKET))
    os.chmod(DAEMON_SOCKET, 0o600)
    server.listen(8)
    engine=None
    try:
        while True:
            conn,_=server.accept()
            with conn:
                reader=conn.makefile('r', encoding='utf-8')
                writer=conn.makefile('w', encoding='utf-8')
                for line in reader:
                    payload=json.loads(line)
                    if engine is None:
                        engine=Engine()
                    global PROTOCOL
                    PROTOCOL=writer
                    reply,fatal=_process_payload(payload,engine,time.monotonic())
                    reply['request_id']=payload.get('request_id')
                    writer.write('@@CUROBO@@'+json.dumps(reply,allow_nan=False,default=str)+'\n')
                    writer.flush()
                    if fatal:
                        return
                    if payload.get('op')=='shutdown':
                        return
    finally:
        try:
            server.close()
        finally:
            try:
                DAEMON_SOCKET.unlink()
            except FileNotFoundError:
                pass


def main():
    if DAEMON_SOCKET is not None:
        _serve_daemon()
        return
    engine=None
    for line in sys.stdin:
        payload={}
        fatal=False
        request_started=time.monotonic()
        def progress(stage):
            elapsed=time.monotonic()-request_started
            print('@@CUROBO@@'+json.dumps({'event':'stage','stage':stage,
                'elapsed_s':elapsed,'request_id':payload.get('request_id')}),file=PROTOCOL,flush=True)
            print(f'cuRobo stage={stage} elapsed_s={elapsed:.3f} request_id={payload.get("request_id")}',
                file=sys.stderr,flush=True)
        try:
            payload=json.loads(line)
            if engine is None:
                engine=Engine(progress=progress)
            reply,fatal=_process_payload(payload,engine,request_started)
        except Exception as exc:
            from libraries.curobo.runtime_control import fatal_cuda_error
            fatal=fatal_cuda_error(exc)
            traceback.print_exc(file=sys.stderr)
            reply={'ok':False,'error':{'error_code':'CUROBO_CUDA_FATAL' if fatal else 'CUROBO_FAILED',
                'error_message':str(exc),'failed_stage':getattr(engine,'stage','initialization'),
                'recoverable':not fatal,'worker_restart_required':fatal,
                'details':{'log_file':str(CACHE/'worker.log')}}}
        reply['request_id']=payload.get('request_id')
        print('@@CUROBO@@'+json.dumps(reply,allow_nan=False,default=str),file=PROTOCOL,flush=True)
        if fatal:
            # CUDA cleanup itself can crash or spin after an illegal address.
            # Flush the failure and let the OS release the poisoned context.
            sys.stderr.flush()
            os._exit(70)


if __name__=='__main__':
    main()
