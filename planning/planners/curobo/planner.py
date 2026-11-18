"""Official cuRobo planning with the host project's Move request/result format."""
from __future__ import annotations

from copy import deepcopy
import time


class CuroboPlanner:
    path_type='joint'

    def __init__(self,config=None,*,robot_runtime=None,scene=None,scene_provider=None,
                 robot_object_id=None,tool=None,trajectory_callback=None,kinematics=None,
                 collision_checker=None,client=None):
        from libraries.curobo import CuroboClient
        self.config=dict(config or {})
        self.robot_runtime=robot_runtime
        self.scene=deepcopy(scene)
        self.scene_provider=scene_provider
        self.robot_object_id=robot_object_id
        self.tool=deepcopy(tool)
        self.trajectory_callback=trajectory_callback
        self.kinematics=kinematics
        self.collision_checker=collision_checker
        self.client=client or CuroboClient(self.config.get('runtime'))
        self._own_client=client is None

    def _snapshot(self,request=None):
        from libraries.curobo.runtime_scene import runtime_snapshot
        explicit=(request or {}).get('scene')
        if explicit is not None:
            return deepcopy(explicit),self.robot_object_id
        if self.scene_provider is not None:
            provided=self.scene_provider()
            # Runtime scene bridges may return (scene, robot_object_id), while
            # ordinary providers return only the scene mapping.
            if isinstance(provided,tuple) and len(provided)==2:
                return deepcopy(provided[0]),provided[1]
            return deepcopy(provided),self.robot_object_id
        if self.scene is not None:
            return deepcopy(self.scene),self.robot_object_id
        if self.robot_runtime is not None:
            from demo.interaction.command_converter import _deep_merge
            from atomic_action.motion.common.utils.validation import validate_move_request
            defaults = getattr(getattr(self.robot_runtime, 'resources', None), 'scene', {}).get('move_defaults', {})
            defaults = _deep_merge(defaults, self.config.get('move_defaults', {}))
            current_request = request or {'target': {'type': 'joint',
                'joint_positions': self.robot_runtime.get_joint_positions().tolist()}}
            constraints = validate_move_request(_deep_merge(defaults, current_request), self.config)['constraints']
            return runtime_snapshot(self.robot_runtime, collision_checker=self.collision_checker,
                constraints=constraints, cache_dir=getattr(self.client, 'cache', None))
        raise ValueError('Provide the current scene JSON or a scene_provider')

    def prepare(self,scene=None,request=None):
        snapshot,ident=self._snapshot({'scene':scene} if scene is not None else request)
        from demo.interaction.command_converter import _deep_merge
        from atomic_action.motion.common.utils.validation import validate_move_request
        defaults=_deep_merge(snapshot.get('move_defaults',{}),self.config.get('move_defaults',{}))
        normalized=validate_move_request(_deep_merge(defaults,request or {
            'target':{'type':'joint','joint_positions':[0.]*6}}))
        return self.client.prepare(snapshot,robot_object_id=ident,tool=self.tool,request=normalized)

    def plan(self,request):
        return self._run(request,execute=False)

    def execute(self,request,**kwargs):
        # RRT-specific prepared trajectories are never reused by this backend.
        return self._run(request,execute=self.robot_runtime is not None)

    def _run(self,request,*,execute):
        checker = self.collision_checker
        target = request.get('target_body_name')
        previous = None
        if target and checker is not None and hasattr(checker, 'set_allowed_target_names'):
            previous = set(getattr(checker, 'allowed_target_names', ()))
            checker.set_allowed_target_names({str(target)})
        try:
            return self._run_scoped(request, execute=execute)
        finally:
            if previous is not None:
                checker.set_allowed_target_names(previous)

    def _run_scoped(self,request,*,execute):
        started=time.perf_counter()
        timing={}
        trajectory=None
        stage='validation'
        selection={'path_strategy':'joint_move','planning_mode':'collision_free',
                   'planner_type':'curobo','fallback_used':False,
                   'selection_reason':'Explicit official cuRobo backend selection'}
        try:
            from demo.interaction.command_converter import _deep_merge
            from atomic_action.motion.common.utils.validation import validate_move_request
            snapshot,ident=self._snapshot(request)
            defaults=_deep_merge(snapshot.get('move_defaults',{}),self.config.get('move_defaults',{}))
            normalized=validate_move_request(_deep_merge(defaults,request))
            if normalized['motion']['path_type'] not in {'auto','joint'}:
                raise ValueError('CuRobo is a free-space joint planner; use existing linear/circular planners for geometric paths')
            if normalized['constraints'].get('keep_end_effector_orientation'):
                raise ValueError('Fixed orientation along the entire path is not implemented by this cuRobo adapter')
            runtime_policy = snapshot.get('_runtime_collision', {}).get('policy')
            if runtime_policy is not None and (
                    runtime_policy['safety_distance'] != normalized['constraints']['safety_distance']
                    or runtime_policy['avoid_collision'] != normalized['constraints']['avoid_collision']):
                raise ValueError('Runtime collision policy disagrees with normalized planning constraints')
            if not normalized['constraints']['avoid_collision'] and runtime_policy is None:
                raise ValueError('CuRobo always checks collision; avoid_collision=false is unsupported')
            if normalized.get('target_body_name') and runtime_policy is None:
                raise ValueError('Contact/allowed-target planning is unsupported by CuRobo; use existing contact planners')
            if normalized['target']['type']=='named_state':
                name=normalized['target']['state_name']
                target=snapshot.get('named_targets',{}).get(name,{})
                target=target.get('request',target).get('target')
                if not target:
                    raise ValueError(f'Unknown named robot state: {name}')
                normalized['target']=deepcopy(target)
            stage='planning'
            trajectory=self.client.plan(normalized,snapshot,robot_object_id=ident,tool=self.tool)
            if runtime_policy is not None and self.robot_runtime is not None:
                from collision.planning_policy import planning_signature
                if planning_signature(self.robot_runtime, self.collision_checker) != snapshot['_runtime_collision']['state_signature']:
                    raise ValueError('Runtime geometry, grasp or collision policy changed during cuRobo planning; request a fresh plan')
            timing['planning']=trajectory['planning_time']
            timing['conversion']=trajectory.get('diagnostics',{}).get('conversion_time',0.)
            timing['preparation']=trajectory.get('diagnostics',{}).get('preparation_time',0.)
            if trajectory['planning_time']>normalized['planning']['planning_time']:
                raise TimeoutError('cuRobo returned after the planning budget; trajectory was not executed')
            if self.collision_checker is not None and normalized['constraints']['avoid_collision']:
                stage='validation'
                check_started=time.perf_counter()
                from planning.constraints.collision_sampling import collision_sampling_settings
                resolution,_=collision_sampling_settings(normalized)
                checked=self.collision_checker.check_path(trajectory['waypoints'],resolution=min(resolution,.01),
                    safety_distance=float(normalized['constraints']['safety_distance']))
                timing['host_collision_validation']=time.perf_counter()-check_started
                if checked.in_collision:
                    raise ValueError(f'Host collision validation rejected trajectory: {checked.contacts}')
            if execute:
                stage='validation'
                import numpy as np
                runtime=self.robot_runtime
                if list(runtime.joint_names)!=trajectory['joint_names']:
                    raise ValueError('cuRobo and execution backend joint names/order differ')
                if not np.allclose(runtime.get_joint_positions(),trajectory['waypoints'][0],atol=1e-3,rtol=0):
                    raise ValueError('Runtime moved after the planning snapshot; request a fresh plan')
                limits=np.asarray(runtime.joint_limits)
                points=np.asarray(trajectory['waypoints'])
                if np.any(points<limits[:,0]-1e-5) or np.any(points>limits[:,1]+1e-5):
                    raise ValueError('Trajectory violates execution backend joint limits')
                from scipy.spatial.transform import Rotation
                diagnostic=trajectory['diagnostics']['curobo']
                converted_pose=diagnostic['current_tool_pose']
                actual=runtime.get_end_effector_pose()
                base=np.asarray(diagnostic['base_world'])
                expected_position=base[:3,:3] @ np.asarray(converted_pose[:3])+base[:3,3]
                expected_rotation=base[:3,:3] @ Rotation.from_quat(np.asarray(converted_pose[3:])[[1,2,3,0]]).as_matrix()
                if (not np.allclose(actual['position'],expected_position,atol=2e-4,rtol=0) or
                    Rotation.from_matrix(expected_rotation.T @ actual['rotation_matrix']).magnitude()>2e-3):
                    raise ValueError('OLS TCP/FK disagrees with the live execution model; refusing execution')
                if self.trajectory_callback:
                    self.trajectory_callback(deepcopy(trajectory),normalized,selection)
                stage='execution'
                execution_input=deepcopy(trajectory)
                execution_input['goal_tolerances']={'position_tolerance':normalized['constraints']['position_tolerance'],
                    'orientation_tolerance':float(np.deg2rad(normalized['constraints']['orientation_tolerance']))}
                execute_started=time.perf_counter()
                execution=runtime.execute_trajectory(execution_input,timeout=normalized['constraints']['timeout'])
                timing['execution']=time.perf_counter()-execute_started
                if execution.get('grasp_error') or execution.get('joint_error',0.)>.02:
                    raise RuntimeError('Execution backend reported tracking/grasp failure')
                execution['planning_time']=trajectory['planning_time']
                execution['planned_duration']=trajectory['duration']
            else:
                execution=None
            timing['total']=time.perf_counter()-started
            return {'success':True,'status':'completed','selection':selection,'trajectory':trajectory,
                    'execution_result':execution,'planned_only':not execute,'timing':timing,
                    'motion_time':execution.get('execution_time',0.) if execution else 0.,'error':None}
        except Exception as exc:
            from atomic_action.motion.common.utils.error_codes import failure_result
            code='INVALID_REQUEST' if stage=='validation' else 'CUROBO_FAILED' if stage=='planning' else 'EXECUTION_FAILED'
            runtime_error = getattr(exc, 'curobo_error', None)
            stopped = isinstance(runtime_error, dict) and runtime_error.get('worker_restart_required')
            if stopped:
                code = runtime_error.get('error_code', code)
            result=failure_result(code,str(exc),failed_stage=stage,recoverable=not stopped,
                                  details=runtime_error)
            if isinstance(exc,TimeoutError):
                result['status']='timeout'
            result.update(selection=selection,timing={**timing,'total':time.perf_counter()-started})
            if trajectory is not None:
                result['trajectory']=trajectory
            return result

    def close(self):
        if self._own_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()


MoveCuroboAction = CuroboPlanner
MoveCuroboSkill = CuroboPlanner

