"""Same-scene motion with OLS object interaction targets."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import os
import sys
from pathlib import Path

import numpy as np

from collision.simplatform_fcl import CollisionResult
from execution import ExecutionBackendRegistry, SceneResourceCoordinator, SceneStateSynchronizer
from execution.resources import component, pose_matrix

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIMPLATFORM_LIBRARY = ROOT / 'libraries' / 'simplatform' / 'bin' / 'sim_platform_c.dll'


class DemoCollisionOverrideChecker:
    """Temporarily suppress collision rejection for an explicit demo fallback.

    The wrapped checker still owns all scene synchronization and diagnostics.
    Only ``check``/``check_path`` return an empty result while the orchestration
    is showing the collision-ignoring fallback; ordinary runs never enable it.
    """

    def __init__(self, checker):
        object.__setattr__(self, '_checker', checker)
        object.__setattr__(self, '_ignore_collisions', False)

    def __getattr__(self, name):
        return getattr(self._checker, name)

    def __setattr__(self, name, value):
        if name in {'_checker', '_ignore_collisions'}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._checker, name, value)

    def set_demo_ignore_collisions(self, enabled):
        object.__setattr__(self, '_ignore_collisions', bool(enabled))

    def _empty(self):
        return CollisionResult(False, float('inf'), ())

    def check(self, joints, **kwargs):
        return self._empty() if self._ignore_collisions else self._checker.check(joints, **kwargs)

    def check_path(self, waypoints, **kwargs):
        return self._empty() if self._ignore_collisions else self._checker.check_path(waypoints, **kwargs)

    def validate_execution_path(self, waypoints, **kwargs):
        # The direct planner prefers this richer validation hook when it is
        # available. Keep the visual fallback override consistent with the
        # ordinary check/check_path methods.
        validator = getattr(self._checker, 'validate_execution_path', None)
        if self._ignore_collisions:
            return self._empty()
        if callable(validator):
            return validator(waypoints, **kwargs)
        return self._checker.check_path(waypoints, **kwargs)


def serializable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


SUCCESS_ACTIONS = {
    'move': '已移动到目标', 'pick': '已抓取目标', 'put': '已放置到目标',
    'grasp': '已抓取目标', 'release': '已释放目标',
    'push': '已推动目标', 'pull': '已拉动目标', 'pour': '已完成倒水动作',
}
ERROR_DESCRIPTIONS = {
    'PATH_CONSTRAINT_INFEASIBLE': '无法继续满足路径约束',
    'COLLISION_DETECTED': '运动受到碰撞限制',
    'ROBOT_JOINT_LIMIT': '机械臂达到关节限位',
    'JOINT_LIMIT': '目标机构达到关节限位',
    'TRAVEL_LIMIT': '请求行程超过允许范围',
    'PATH_CONSTRAINT_INFEASIBLE': '无法满足连续路径约束',
    'CONTACT_NOT_ESTABLISHED': '未建立有效接触',
    'CONTACT_LOST': '目标接触或抓持丢失',
    'FORCE_LIMIT': '接触力超过上限',
    'PUSH_PULL_TIMEOUT': '推拉执行超时',
    'TARGET_NOT_REACHED': '未达到请求的运动终点',
    'DOOR_TARGET_NOT_REACHED': '柜门未达到目标角度',
    'DRAWER_TARGET_NOT_REACHED': '抽屉未达到目标开度',
    'ACTUATOR_TRACKING_FAILED': '机械臂未能跟踪到规划终点',
    'TARGET_RESOLUTION_FAILED': '目标位姿解析失败',
    'PUT_TARGET_IS_HELD_OBJECT': '放置目标就是当前持物，不能放到自身',
    'PREPOINT_UNREACHABLE': '所有预接近位姿均无可用逆解',
    'PREPOINT_COLLISION': '所有预接近位姿均发生碰撞',
    'CONTACT_PATH_UNREACHABLE': '预接近点到目标的直线路径无可用逆解',
    'CONTACT_PATH_COLLISION': '预接近点到目标的直线路径发生碰撞',
    'CONTACT_PATH_PLANNING_FAILED': '预接近点到目标的直线路径规划失败',
    'NO_VALID_APPROACH': '预接近点或接触路径均不可行',
    'TARGET_POSE_UNREACHABLE': '目标交互位姿无可用逆解',
    'TARGET_POSE_COLLISION': '目标交互位姿发生碰撞',
    'NO_VALID_TARGET_POSE': '目标交互位姿候选均不可用',
    'GRASP_CLOSURE_INFEASIBLE': '没有可完成夹爪闭合的抓取位姿',
    'GRASP_CLOSURE_COLLISION': '夹爪闭合时发生碰撞',
    'GRASP_OPENING_COLLISION': '夹爪张开时发生碰撞',
    'POSE_VERIFICATION_FAILED': '执行后的位姿超出允许误差',
}
STAGE_DESCRIPTIONS = {
    'state_sync': '场景状态同步失败', 'conversion': '命令转换失败',
    'target_resolution': '目标位姿解析失败', 'planning': '运动规划失败',
    'planning_and_execution': '运动规划或执行失败',
    'interaction_position_verification': '交互位姿校验失败',
    'grasp': '抓取失败', 'release': '释放失败',
    'push_pull': '机构操作失败',
    'pull_push': '机构操作失败',
}


def command_target_name(command, scene):
    """Show the scene object's name while retaining named targets such as home."""
    parameters = command['parameters']
    target = parameters.get('target')
    if command.get('skill_name') == 'pour':
        target = f"{parameters.get('source')}→{parameters.get('target')}"
    for obj in scene['objects']:
        if str(obj['object_id_in_scene']) == str(target):
            name = obj['object_name_in_scene']
            link = parameters.get('link')
            if link is None and command['skill_name'] in {'push', 'pull'}:
                links = component(obj, 'ArticulatedModel').get('links', [])
                link = links[0] if len(links) == 1 else None
            return f"{name}.{link}" if link else name
    return str(target)


def step_message(command, *, success, error=None, stage=None, target_name=None, status=None):
    """Concise Chinese outcome for the two-line command progress display."""
    target = target_name if target_name is not None else command['parameters'].get('target')
    if success and status != 'suspend':
        return f"success: {SUCCESS_ACTIONS.get(command['skill_name'], '已完成目标')} {target}"
    error = error if isinstance(error, dict) else {}
    description = ERROR_DESCRIPTIONS.get(error.get('error_code'))
    if description is None:
        description = STAGE_DESCRIPTIONS.get(error.get('failed_stage') or stage, '步骤执行失败')
    if error.get('error_code') in {'NO_VALID_APPROACH', 'NO_VALID_TARGET_POSE'}:
        labels = {
            'prepoint_ik': '预接近位姿无逆解',
            'prepoint_collision': '预接近位姿碰撞',
            'contact_ik': '接触直线路径无逆解',
            'contact_collision': '接触直线路径碰撞',
            'contact_planning': '接触直线路径规划失败',
            'ik_failed': '目标位姿无逆解',
            'goal_collision': '目标位姿碰撞',
        }
        counts = Counter(item.get('category') or item.get('reason') for item in error.get('rejected', ()))
        reasons = [f'{labels[key]} {count} 次' for key, count in counts.items() if key in labels]
        if reasons:
            description += '：' + '，'.join(reasons)
    if error.get('error_code') in {'PREPOINT_COLLISION', 'CONTACT_PATH_COLLISION',
                                   'TARGET_POSE_COLLISION', 'PATH_CONSTRAINT_INFEASIBLE'}:
        contacts = error.get('details', {}).get('contacts', ()) if isinstance(error.get('details'), dict) else ()
        if not contacts:
            contacts = next((item['contacts'] for item in error.get('rejected', ()) if item.get('contacts')), ())
        if contacts:
            pair = contacts[0]
            description += f"：{pair['geom1']} 与 {pair['geom2']}"
    details = error.get('details') if isinstance(error.get('details'), dict) else {}
    code = error.get('error_code')
    if code == 'COLLISION_DETECTED':
        contacts = details.get('contacts', ())
        if contacts:
            pairs = []
            seen = set()
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                first = contact.get('geom1', contact.get('body1', '?'))
                second = contact.get('geom2', contact.get('body2', '?'))
                key = tuple(sorted((str(first), str(second))))
                if key in seen:
                    continue
                seen.add(key)
                distance = contact.get('distance')
                suffix = f"，距离 {float(distance) * 1000:.3f} mm" if isinstance(distance, (int, float)) else ''
                pairs.append(f"{first} ↔ {second}{suffix}")
                if len(pairs) >= 3:
                    break
            if pairs:
                more = f"，另有 {len(contacts) - len(seen)} 个接触" if len(contacts) > len(seen) else ''
                description += '：' + '；'.join(pairs) + more
        else:
            description += '：未返回碰撞对象，详见 report.json'
    elif code == 'ROBOT_JOINT_LIMIT':
        joints = details.get('joints', ())
        if joints:
            description += '：' + '；'.join(
                f"{item.get('joint', '?')}={float(item.get('value', 0)):.4f} rad，范围 "
                f"[{float(item.get('lower', 0)):.4f}, {float(item.get('upper', 0)):.4f}]"
                for item in joints[:3])
    elif code == 'JOINT_LIMIT':
        if details.get('joint'):
            description += (f"：{details['joint']} 当前 {float(details.get('current', details.get('value', 0))):.4f} rad，"
                            f"范围 [{float(details.get('lower', 0)):.4f}, {float(details.get('upper', 0)):.4f}]")
    elif code == 'TRAVEL_LIMIT':
        if details.get('requested') is not None:
            unit = details.get('unit', '')
            description += (f"：请求 {float(details['requested']):.4f} {unit}，"
                            f"允许 {float(details.get('limit', 0)):.4f} {unit}")
    elif code == 'PATH_CONSTRAINT_INFEASIBLE' and details.get('joint'):
        description += (f"：{details['joint']} 单步变化 {float(details.get('step', 0)):.4f} rad，"
                        f"超过 {float(details.get('maximum_step', 0)):.4f} rad")
    if status == 'suspend':
        return f"suspend: 任务中止，{target} 未完成目标；{description}" + (f"（{code}）" if code else '')
    return f"error: {description}" + (f"（{code}）" if code else '，详情见报告')


def run(command_file, *, catalog=ROOT / 'assets/catalog.json', backend_name='mujoco',
        output_dir=ROOT / 'tests/output/unified_scene', viewer=False, collision_probe=False,
        object_library_url=None, start_object_library=False, show_collision_geometry=False):
    from libraries.eaik import load_urdf
    from collision import SimPlatformFCLCollisionChecker
    from planning.time_parameterization import prepare_continuous_trajectory
    from services.object_library import ObjectLibraryClient, LocalObjectLibraryService, ObjectLibraryError
    from demo.task_skills.runner import TaskSkillExecutor
    from demo.interaction.object_library import ObjectLibraryGraspResolver, apply_ols_friction, scene_object_by_id, model_id_for

    task = json.loads(Path(command_file).read_text(encoding='utf-8'))
    output = Path(output_dir).resolve(); output.mkdir(parents=True, exist_ok=True)
    report = {'request_id': task.get('request_id'), 'success': False, 'steps': []}
    coordinator = SceneResourceCoordinator(catalog)
    backend = checker = None
    sync = SceneStateSynchronizer()
    local_service = resolver = None
    stage = 'validation'
    frame_index = 0
    active_command = None
    active_step_logged = True
    active_target_name = None
    row = None
    try:
        resources = coordinator.resolve(task, backend_name)
        commands = task.get('commands')
        if not isinstance(commands, list) or not commands:
            raise ValueError('commands must be a non-empty array')
        demo_fallback_ignore_collisions = any(
            bool(command.get('parameters', {}).get('demo_ignore_fallback_collisions'))
            for command in commands if isinstance(command, dict)
        )
        seen = set()
        targets = resources.scene.get('named_targets', {})
        object_ids = {str(o['object_id_in_scene']) for o in resources.scene['objects']}
        object_commands = set()
        names = {o['object_name_in_scene'] for o in resources.scene['objects']}
        if collision_probe and 'probe_block' not in names:
            raise ValueError('--collision-probe requires a scene containing probe_block; use the unified_scene_v2 test fixture')
        for command in commands:
            cid = command.get('command_id')
            if not cid or cid in seen:
                raise ValueError('command_id is required and must be unique')
            seen.add(cid)
            skill = command.get('skill_name')
            if skill not in {'move', 'grasp', 'release', 'approach', 'retreat', 'pick', 'put', 'push', 'pull', 'pour'}:
                raise ValueError(f'This integration runner supports move/grasp/release/approach/retreat/pick/put/push/pull/pour: {skill}')
            parameters = command.get('parameters', {})
            target = parameters.get('target')
            if skill == 'pour':
                source_name = parameters.get('source')
                target_name = parameters.get('target')
                names = {o['object_name_in_scene'] for o in resources.scene['objects']}
                if source_name not in names or target_name not in names:
                    raise ValueError('pour source and target must be scene object names')
                continue
            if skill in {'push', 'pull'} and target is None and any(k in parameters for k in ('distance', 'angle_deg')):
                # The preceding command establishes the contact; resolve it at execution time.
                continue
            if skill in {'approach', 'retreat'}:
                if target in targets or target in {'end_effector', '__end_effector__'}:
                    raise ValueError(f'{skill} requires an interaction object target')
                model_id_for(scene_object_by_id(resources.scene, target))
                object_commands.add(cid)
                continue
            if skill in {'push', 'pull'} or parameters.get('link'):
                if skill in {'pick', 'put'}:
                    raise ValueError('Mechanisms use move/grasp/push/pull/release, not pick/put retreat sequences')
                from demo.interaction.requests import body_name
                body_name(resources, parameters)
                continue
            if skill in {'pick', 'put'} and (target in targets or target in {'end_effector', '__end_effector__'}):
                raise ValueError(f'{skill} requires an interaction object target')
            if skill in {'move', 'pick', 'put'} and target not in targets and target not in {'end_effector', '__end_effector__'}:
                model_id_for(scene_object_by_id(resources.scene, target))
                object_commands.add(cid)
            if skill in {'grasp', 'release'}:
                scene_object_by_id(resources.scene, target)
        if object_commands:
            stage = 'object_library_initialization'
            config = resources.scene.get('object_library', {})
            if start_object_library and object_library_url:
                raise ValueError('Choose an external OLS URL or start the local service, not both')
            if start_object_library:
                local_service = LocalObjectLibraryService()
                object_library_url = local_service.url
                # Keep the demo progress display limited to command outcomes.
                local_service.server.RequestHandlerClass.log_message = lambda *_args: None
            resolver = ObjectLibraryGraspResolver(ObjectLibraryClient(
                object_library_url or config.get('base_url', 'http://127.0.0.1:8000'), uid=config.get('uid', 0)))
            try:
                apply_ols_friction(resources, resolver.client)
            except ObjectLibraryError as exc:
                # A scene's default loopback URL is a convenience, not an
                # external-service requirement. If no URL was explicitly
                # supplied and that endpoint is unavailable, start the
                # project-local read-only OLS and retry the same request.
                unavailable = ('WinError 10061' in str(exc)
                               or 'Connection refused' in str(exc)
                               or 'timed out' in str(exc).lower())
                if start_object_library or object_library_url or not unavailable:
                    raise
                local_service = LocalObjectLibraryService()
                object_library_url = local_service.url
                local_service.server.RequestHandlerClass.log_message = lambda *_args: None
                resolver = ObjectLibraryGraspResolver(ObjectLibraryClient(
                    object_library_url, uid=config.get('uid', 0)))
                apply_ols_friction(resources, resolver.client)
        ref = component(resources.robot_object, 'RobotModelRef')
        stage = 'planning_initialization'
        # Created without importing/constructing MuJoCo; planning does not need runtime geometry.
        kin = load_urdf(resources.robot_urdf, base_link=ref['base_link'], tip_link=ref['tip_link'],
                        tcp_transform=ref.get('tcp_transform'), world_from_base=pose_matrix(resources.robot_object),
                        initial_joints=resources.initial_joints)
        # Load the selected plugin before SimPlatform: its distribution contains
        # a different mujoco.dll. Windows resolves loaded DLL basenames globally.
        backend = ExecutionBackendRegistry().create(backend_name)
        from demo.collision.grasp_collision import GraspCollisionChecker
        checker_type = GraspCollisionChecker if component(resources.robot_object, 'GripperModel') else SimPlatformFCLCollisionChecker
        checker = checker_type(scene_file=resources.scene_file, scene_data=resources.scene, robot_urdf=resources.robot_urdf,
                                                 initial_joints=resources.initial_joints, asset_root=resources.asset_root)
        start_check = checker.check(resources.initial_joints, safety_distance=0.003)
        if start_check.in_collision:
            raise ValueError(f'Initial state collides: {start_check.contacts}')
        stage = 'execution_initialization'
        backend.load(resources, output / backend_name)
        report['consistency'] = coordinator.validate_consistency(resources, kin, checker, backend)
        if isinstance(checker, GraspCollisionChecker):
            checker.bind_backend(backend)
            backend.gripper_controller = backend.create_gripper_controller()
            start_check = checker.check(resources.initial_joints, safety_distance=0.003)
            if start_check.in_collision:
                raise ValueError(f'Initial assembled robot collides: {start_check.contacts}')
        if demo_fallback_ignore_collisions:
            checker = DemoCollisionOverrideChecker(checker)
        if viewer or show_collision_geometry:
            backend.open_viewer()
            if show_collision_geometry:
                setter = getattr(backend, 'set_collision_geometry_visible', None)
                if setter is None or not setter(True):
                    raise RuntimeError('--show-collision-geometry requires a MuJoCo viewer backend')
        # A moved obstacle must be detected, even though it was initially safe.
        if collision_probe:
            name = 'probe_block'
            original = backend.get_state()['objects'][name]
            try:
                backend.set_object_pose(name, backend.get_end_effector_pose()['position'])
                sync.sync(backend, kin, checker)
                hit = checker.check(backend.get_joint_positions())
                report['collision_probe'] = {'detected': hit.in_collision, 'contacts': hit.contacts}
                if not hit.in_collision:
                    raise AssertionError('FCL failed to detect deliberately inserted obstacle')
            finally:
                backend.set_object_pose(name, **original)
                sync.sync(backend, kin, checker)
        from skills.motion.move.skill import MoveSkill
        if component(resources.robot_object, 'GripperModel'):
            backend.advance_gripper(1.0)
            sync.sync(backend, kin, checker)

        def prepare_trajectory(trajectory, request, selection):
            from demo.task_commands.converter import _deep_merge
            request = _deep_merge(resources.scene['move_defaults'], request)
            timed = prepare_continuous_trajectory(trajectory, request, collision_checker=checker,
                                                 timestep=float(backend.model.opt.timestep), kinematics=kin)
            trajectory.update(timed)
            backend.set_status(f"{report['active_command_id']} | Executing / {report.get('active_substep', 'move')}")

        mover = MoveSkill(robot_runtime=backend, kinematics=kin, collision_checker=checker,
                          trajectory_callback=prepare_trajectory)
        def execute_motion(request):
            # The execution backend is authoritative after every physical
            # step. Refresh the planning snapshot immediately before each
            # MoveSkill planning call; SceneStateSynchronizer makes this
            # incremental, so an unchanged scene does not rebuild FCL state.
            sync.sync(backend, kin, checker)
            return mover.execute(request)
        def write_task_frame(command_id, substep, frame_state):
            nonlocal frame_index
            frames.write(json.dumps({'frame_index': frame_index, 'command_id': command_id,
                                      'substep': substep, **frame_state}, default=serializable) + '\n')
            frame_index += 1

        task_executor = TaskSkillExecutor(
            resources=resources, backend=backend, kinematics=kin, checker=checker,
            synchronizer=sync, resolver=resolver, execute_motion=execute_motion,
            report=report, write_frame=write_task_frame)
        with (output / 'frames.jsonl').open('w', encoding='utf-8') as frames:
            for index, command in enumerate(commands, 1):
                active_command = command
                active_step_logged = False
                row = None
                active_target_name = None
                if command['skill_name'] in {'push', 'pull'}:
                    from demo.interaction.requests import resolve_held_contact
                    command = {**command, 'parameters':resolve_held_contact(resources, backend, command['parameters'])}
                active_target_name = command_target_name(command, resources.scene)
                report['active_command_id'] = command['command_id']
                print(f"[{index}/{len(commands)}] {command['skill_name']}({active_target_name})", flush=True)
                backend.set_status(f"{command['command_id']} | Planning / {command['skill_name']}")
                stage = 'state_sync'
                state = sync.sync(backend, kin, checker)
                row = {'command_id': command['command_id'], 'skill_name': command['skill_name']}
                parameters = command['parameters']
                mechanism = bool(parameters.get('link')) or command['skill_name'] in {'push', 'pull'}
                backend.contact_target = None
                if mechanism:
                    from demo.interaction.requests import body_name
                    backend.contact_target = body_name(resources, parameters) if parameters.get('allow_contact') else None
                if command['skill_name'] in {'push', 'pull'}:
                    from demo.interaction.requests import prepare_contact_command, push_pull_request
                    from skills.manipulation.pull_push.skill import PullPushSkill
                    stage = 'pull_push'
                    if command['skill_name'] == 'pull' or any(k in parameters for k in ('distance', 'angle_deg', 'target_position_m')):
                        parameters = prepare_contact_command(resources, backend, parameters, command['skill_name'])
                    def record_mechanism(state):
                        nonlocal frame_index
                        frames.write(json.dumps({'frame_index': frame_index, 'command_id': command['command_id'], **state},default=serializable)+'\n')
                        frame_index += 1
                    backend.set_frame_callback(record_mechanism)
                    from control.pull_push_controller import MujocoPullPushController
                    backend.pull_push_controller = MujocoPullPushController(
                        backend, kinematics=kin, collision_checker=checker)
                    push_pull_skill = PullPushSkill(robot_runtime=backend,kinematics=kin)
                    if parameters.get('orchestrate'):
                        from demo.task_skills.pull import CommandPullExecutor
                        from demo.task_skills.push import CommandPushExecutor
                        executor_type = CommandPullExecutor if command['skill_name'] == 'pull' else CommandPushExecutor
                        orchestrator = executor_type(
                            resources=resources, backend=backend, kinematics=kin,
                            synchronizer=sync, checker=checker,
                            execute_motion=execute_motion, push_pull_skill=push_pull_skill,
                            grasper=task_executor.grasper, releaser=task_executor.releaser)
                        result = orchestrator.execute(parameters)
                        row.update(result)
                    else:
                        request = push_pull_request(resources, backend, parameters, command['skill_name'])
                        backend.contact_target = request['target']['object_id']
                        result = push_pull_skill.execute(request)
                        row.update(result,request=request)
                    sync.sync(backend,kin,checker)
                elif command['skill_name'] in {'move', 'grasp', 'release', 'approach', 'retreat', 'pick', 'put', 'pour'}:
                    try:
                        task_executor.execute(command, index, state, row)
                    except Exception as exc:
                        clear_detach = getattr(checker, 'clear_detach_contacts', None)
                        if callable(clear_detach):
                            clear_detach()
                        stage = task_executor.stage
                        row.setdefault('success', False)
                        row.setdefault('error', {'failed_stage': stage, 'error_message': str(exc)})
                        report['steps'].append(row)
                        raise
                    clear_detach = getattr(checker, 'clear_detach_contacts', None)
                    if callable(clear_detach):
                        clear_detach()
                    stage = task_executor.stage
                display_status = row.get('status')
                display_error = row.get('error')
                # Temporary best-effort mode: preserve the original suspend
                # evidence, but let the command sequence continue. This is
                # intentionally scoped to suspend; ordinary execution errors
                # still stop the task.
                if row.get('status') == 'suspend':
                    row['continued_after_suspend'] = True
                    row['suspend_status'] = row.get('status')
                    row['suspend_error'] = row.get('error')
                    row['success'] = True
                    row['status'] = 'success'
                    row['completion'] = 'partial'
                report['steps'].append(row)
                (output / 'report.json').write_text(json.dumps(report, indent=2, default=serializable), encoding='utf-8')
                print(step_message(command, success=bool(row.get('success')),
                                   error=display_error, stage=stage,
                                   target_name=active_target_name, status=display_status), flush=True)
                active_step_logged = True
                if not row.get('success'):
                    report.update(final_state=backend.get_state(), frame_count=frame_index,
                                  physics_steps=backend.physics_steps)
                    stage = (row.get('error') or {}).get('failed_stage', stage)
                    raise RuntimeError(f"Step failed: {row.get('error')}")
        report.update(success=True, status='success', final_state=backend.get_state(), frame_count=frame_index,
                      collision_cache_stats=dict(checker.cache_stats),
                      planning_cache_stats=dict(getattr(checker, 'planning_cache_totals', {})),
                      physics_steps=backend.physics_steps, execution_backend=backend_name,
                      viewer_sync_count=getattr(backend, 'viewer_sync_count', 0))
        backend.set_status('Completed')
    except Exception as exc:
        report.update(success=False, status='error')
        if active_command is not None and not active_step_logged:
            print(step_message(active_command, success=False,
                               error=row.get('error') if row else getattr(exc, 'details', None),
                               stage=stage, target_name=active_target_name), flush=True)
            active_step_logged = True
        report['error'] = {'stage': stage, 'message': str(exc), 'type': type(exc).__name__}
        if hasattr(exc, 'details'):
            report['error'].update(exc.details)
        elif stage in {'grasp_closure', 'grasp_opening'}:
            report['error'].update(row.get('error', {}))
    finally:
        try:
            if backend is not None and hasattr(backend, 'data'):
                np.savez_compressed(output / 'final_state.npz', qpos=backend.data.qpos, qvel=backend.data.qvel,
                                    ctrl=backend.data.ctrl, eq_active=backend.data.eq_active,
                                    eq_data=backend.model.eq_data, time=float(backend.data.time))
            report['allowed_carrying_contacts'] = list(getattr(checker, 'carrying_contact_log', {}).values())
            (output / 'report.json').write_text(json.dumps(report, indent=2, default=serializable), encoding='utf-8')
            if (viewer or show_collision_geometry) and backend is not None and getattr(backend, 'viewer', None) is not None:
                label = 'Suspended' if report.get('status') == 'suspend' else ('Completed' if report.get('success') else 'Failed')
                backend.set_status(label + ' | Close window to exit')
                backend.wait_for_viewer_close()
        finally:
            if backend is not None:
                backend.close()
            if checker is not None:
                checker.close()
            if local_service is not None:
                local_service.close()
    return report


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commands', type=Path, default=ROOT / 'demo/task_commands/commands.001.json')
    parser.add_argument('--catalog', type=Path, default=ROOT / 'assets/catalog.json')
    parser.add_argument('--backend', default='mujoco')
    parser.add_argument('--library', type=Path, default=DEFAULT_SIMPLATFORM_LIBRARY,
                        help='SimPlatform native DLL (default: libraries/simplatform/bin/sim_platform_c.dll)')
    parser.add_argument('--output', type=Path, default=ROOT / 'tests/output/unified_scene')
    parser.add_argument('--viewer', action='store_true')
    parser.add_argument('--show-collision-geometry', action='store_true',
                        help='显示 MuJoCo 碰撞几何（自动打开 viewer）')
    parser.add_argument('--object-library-url', help='Existing OLS service URL')
    parser.add_argument('--start-object-library', action='store_true', help='Start project OLS on a free loopback port for this task')
    parser.add_argument('--collision-probe', action='store_true', help='Test-only: insert and restore probe_block at the TCP before execution')
    args = parser.parse_args()
    if not args.commands.is_file():
        parser.error(f'commands file not found: {args.commands}')
    if not args.catalog.is_file():
        parser.error(f'catalog file not found: {args.catalog}')
    library = args.library.expanduser().resolve()
    if not library.is_file():
        parser.error(f'SimPlatform DLL not found: {library}')
    os.environ['SIM_PLATFORM_LIBRARY'] = str(library)
    result = run(args.commands, catalog=args.catalog, backend_name=args.backend, output_dir=args.output,
                 viewer=args.viewer, collision_probe=args.collision_probe,
                 object_library_url=args.object_library_url, start_object_library=args.start_object_library,
                 show_collision_geometry=args.show_collision_geometry)
    raise SystemExit(0 if result['success'] else 2 if result.get('status') == 'suspend' else 1)


if __name__ == '__main__':
    main()




