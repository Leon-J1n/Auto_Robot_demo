"""Private JSON-line worker. Official cuRobo source is loaded only in this process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import traceback

PACKAGE=Path(__file__).resolve().parent
PROJECT=PACKAGE.parents[1]
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


class Engine:
    def __init__(self, progress=None):
        import numpy as np
        self.np=np
        from libraries.curobo.conversion.assets import AssetStore
        from libraries.curobo.conversion.scene import SceneConverter
        self.AssetStore,self.SceneConverter=AssetStore,SceneConverter
        self.converters={}
        self.planner=None
        self.planner_key=None
        self.locked=None
        self.world_key=None
        self.defaults=json.loads((PACKAGE/'config/defaults.json').read_text('utf-8'))
        self.defaults.update(CONFIG.get('conversion',{}))
        self.gpu_loaded=False
        self.progress=progress
        self.stage='initialization'

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
            return convert_runtime(scene, CACHE)
        ols={**scene.get('object_library',{}), **CONFIG.get('ols',{})}
        base=ols.get('base_url','http://127.0.0.1:8000')
        uid=ols.get('uid',0)
        key=(base,uid)
        if key not in self.converters:
            self.converters[key]=self.SceneConverter(self.AssetStore(base,uid,CACHE),CACHE)
        return self.converters[key].convert(scene,robot_object_id=payload.get('robot_object_id'),tool=payload.get('tool'))

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

    def prepare(self,converted,request=None,*,check_endpoints=False):
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
                         margin,position_tolerance,orientation_tolerance,self_rules])
        world_key=fingerprint(converted['world'])
        world_changed=self.world_key!=world_key
        required={'cuboid':len(converted['world'].get('cuboid',{})),
            'mesh':sum(len(converted['world'].get(kind,{})) for kind in ('mesh','sphere','cylinder','capsule'))}
        if self.planner is not None and any(count>self.capacity.get(kind,0) for kind,count in required.items()):
            self.planner_key=None  # Rebuild/capture graphs rather than invalidate their GPU storage.
        report={'planner_reused':self.planner_key==key}
        if self.planner_key!=key:
            self.planner_key=None
            if self.planner is not None:
                self.planner.destroy()
            self.planner=None
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
            self.planner=MotionPlanner(MotionPlannerCfg.create(robot=model,scene_model=world,
                collision_cache=counts or None,**kwargs))
            if self.compiled_policy is not None:
                from libraries.curobo.policy_checker import PolicyCollisionChecker
                scene = self.planner.scene_collision_checker
                scene.checker = PolicyCollisionChecker(device_cfg=scene.device_cfg)
                scene.checker.configure(scene, self.planner.kinematics, converted, self.compiled_policy)
                if check_endpoints:
                    self.set_stage('endpoint_validation')
                    self.validate_runtime_endpoints(converted, request)
            self.set_stage('warmup')
            warmup_iterations=int(self.defaults.get('runtime_warmup_iterations',0)
                                  if is_runtime else self.defaults['warmup_iterations'])
            if warmup_iterations < 0:
                raise ValueError('Warmup iterations cannot be negative')
            self.warmup_strategy='lazy'
            if warmup_iterations:
                if is_runtime and (request or {}).get('target',{}).get('type')=='joint':
                    self.warmup_strategy='cspace'
                    state=self.joint_state(converted)
                    for _ in range(warmup_iterations):
                        self.planner.plan_cspace(state.clone(),state.clone(),max_attempts=1)
                        self.planner.reset_seed()
                else:
                    self.warmup_strategy='pose'
                    self.planner.warmup(enable_graph=self.cuda_graphs,num_warmup_iterations=warmup_iterations)
            if self.cuda_graphs and self.compiled_policy is not None and self.planner.graph_planner is not None:
                # The pinned runtime's warmup feasibility capture reproduced
                # an illegal-instruction replay on the full articulated scene.
                # Recapture on the first real search, keeping CUDA graphs and
                # all collision checks enabled (no upstream source changes).
                self.planner.graph_planner.feasibility_rollout.reset_cuda_graph()
            self.planner_key=key
            self.locked=dict(converted['locked_joints'])
            self.world_key=world_key
        else:
            if self.locked!=converted['locked_joints']:
                updated=KinematicsCfg.from_robot_yaml_file(self.robot_model,lock_joints=converted['locked_joints'])
                self.planner.kinematics.update_kinematics_config(updated.kinematics_config)
                self.planner.reset_seed()
                self.locked=dict(converted['locked_joints'])
            if self.world_key!=world_key:
                self.planner.update_world(Scene.create(converted['world']).get_collision_check_world())
                self.world_key=world_key
            if self.compiled_policy is not None:
                scene = self.planner.scene_collision_checker
                scene.checker.configure(scene, self.planner.kinematics, converted, self.compiled_policy)
                if getattr(self, 'policy_key', None) != self.compiled_policy['fingerprint']:
                    self.planner.reset_seed()
                    if self.planner.graph_planner is not None:
                        self.planner.graph_planner.reset_buffer()
                if check_endpoints:
                    self.set_stage('endpoint_validation')
                    self.validate_runtime_endpoints(converted, request)
        self.policy_key = None if self.compiled_policy is None else self.compiled_policy['fingerprint']
        report.update(robot_yaml=self.robot_path,conversion_settings=self.settings,
            collision_spheres=sum(len(x) for x in self.robot_model['kinematics']['collision_spheres'].values()),
            world_counts={k:len(v) for k,v in converted['world'].items()},
            gpu=self.torch.cuda.get_device_name(0),tool=converted['tool_spec'],
            current_tool_pose=converted['current_tool_pose'],base_world=converted['base_world'],
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
                'payload_bodies': converted['payload_bodies'], 'geometry_source': 'measured_runtime',
                'sphere_approximation': True, 'cuda_graph_enabled': self.cuda_graphs,
                'distance_numerical_tolerance_m': GPU_DISTANCE_EPS_M}
        import yaml
        scene_path=Path(converted['robot_dir'])/'scene.yml'
        if world_changed or not scene_path.exists():
            scene_path.write_text(yaml.safe_dump(converted['world'],sort_keys=False),encoding='utf-8')
        report['scene_yaml']=str(scene_path)
        self.report=report
        return report

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
        metrics=rollout.compute_metrics_from_action(samples)
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

    def plan(self,converted,request):
        self.set_stage('planning')
        from libraries.curobo.conversion.urdf import pose, rigid,vector
        from libraries.curobo.conversion.scene import official_pose,goal_in_base
        from curobo.types import GoalToolPose
        np=self.np
        torch=self.torch
        q=self.joint_state(converted)
        goal=request['target']
        target_type=goal['type']
        if target_type=='joint':
            goal_state=self.joint_state(converted,goal['joint_positions'])
            call=lambda:self.planner.plan_cspace(goal_state,q,max_attempts=request.get('planning',{}).get('max_attempts',5))
        elif target_type=='pose':
            matrix=goal_in_base(goal,converted['base_world'],converted['current_tool_pose'])
            transformed=official_pose(matrix)
            target=GoalToolPose(tool_frames=self.planner.tool_frames,
                position=torch.tensor([[[[transformed[:3]]]]],device='cuda',dtype=torch.float32),
                quaternion=torch.tensor([[[[transformed[3:]]]]],device='cuda',dtype=torch.float32))
            call=lambda:self.planner.plan_pose(target,q,max_attempts=request.get('planning',{}).get('max_attempts',5))
        else:
            raise ValueError('cuRobo target must resolve to pose or joint')
        torch.cuda.synchronize()
        start=time.perf_counter()
        result=call()
        torch.cuda.synchronize()
        elapsed=time.perf_counter()-start
        if result is None or not bool(result.success.any()):
            raise RuntimeError('Official cuRobo did not find a feasible trajectory for this current scene/start/goal')
        plan=result.get_interpolated_plan()
        indices=[plan.joint_names.index(n) for n in converted['active_names']]
        positions=plan.position.detach().cpu().numpy().reshape(-1,len(plan.joint_names))[:,indices]
        if len(positions)<2 or not np.isfinite(positions).all():
            raise RuntimeError('Official planner returned an invalid interpolated trajectory')
        dt_value=getattr(plan,'dt',None)
        if dt_value is None:
            dt_value=getattr(result,'interpolation_dt',.025)
        dt=float(torch.as_tensor(dt_value).flatten()[0])
        if not np.isfinite(dt) or dt<=0:
            raise RuntimeError('Official trajectory has an invalid time step')
        # Time dilation preserves every collision-checked position; no second optimizer is run.
        constraints=request.get('constraints',{})
        dilation=max(1.,1./float(constraints.get('velocity_scale',1.)),
                     1./np.sqrt(float(constraints.get('acceleration_scale',1.))))
        stamps=np.arange(len(positions))*dt*dilation
        start_positions=np.array([converted['positions'][n] for n in converted['active_names']])
        if not np.allclose(positions[0],start_positions,atol=1e-3,rtol=0):
            raise RuntimeError('Trajectory start disagrees with current scene joints')
        for column,name in enumerate(converted['joint_names']):
            limit=converted['scene_limits'].get(name)
            if limit and (np.min(positions[:,column])<float(limit[0])-1e-5 or np.max(positions[:,column])>float(limit[1])+1e-5):
                raise ValueError(f'Trajectory violates scene joint limits: {name}')
        trajectory={'path_type':'joint','planning_mode':'collision_free','joint_names':converted['joint_names'],
            'waypoints':positions.tolist(),'time_parameterized_waypoints':positions.tolist(),
            'timestamps':stamps.tolist(),'duration':float(stamps[-1]),'planning_time':elapsed,
            'diagnostics':{'backend':'curobo','curobo':self.report,'time_dilation':float(dilation),
                           'solve_time':float(result.solve_time),'total_time':float(result.total_time)}}
        for name,power in [('velocity',1),('acceleration',2),('jerk',3)]:
            value=getattr(plan,name,None)
            if value is not None:
                array=value.detach().cpu().numpy().reshape(-1,len(plan.joint_names))[:,indices]/dilation**power
                trajectory[{'velocity':'velocities','acceleration':'accelerations','jerk':'jerks'}[name]]=array.tolist()
        return trajectory


def main():
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
                    report=engine.prepare(converted,request,check_endpoints=payload['op']=='plan')
                    preparation_time=time.perf_counter()-start
                    if payload['op']=='prepare':
                        result=report
                    elif payload['op']=='plan':
                        result=engine.plan(converted,request)
                        result['diagnostics'].update(conversion_time=conversion_time,preparation_time=preparation_time)
                    else:
                        raise ValueError('Unknown worker operation')
            reply={'ok':True,'result':result}
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
