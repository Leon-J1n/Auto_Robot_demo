"""Same-scene motion with OLS object interaction targets."""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

from demo.error_messages import describe_error, error_category, error_from_exception
from collision.backends.simplatform_fcl import CollisionResult
from execution import ExecutionBackendRegistry, SceneResourceCoordinator, SceneStateSynchronizer
from execution.resources import component, pose_matrix
from demo.output_layout import prepare_output, command_view, summary_view, write_metrics, output_lock, write_command_result
from atomic_action.perception.vlm_estimate.timing import command_timing_scope, start_command, stamp_command

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


def _write_json(path, value):
    """Write a report atomically so Windows readers cannot interrupt it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    payload = json.dumps(value, indent=2, default=serializable)
    try:
        temporary.write_text(payload, encoding='utf-8')
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_command_report(output_dir, index, row):
    """Persist one command per JSONL line, with separately stored trajectory arrays."""
    output = Path(output_dir) / 'commands'
    output.mkdir(parents=True, exist_ok=True)
    stamp_command(row)
    command_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(row.get('command_id', index)))
    stem = f'step_{int(index):03d}_command_{command_id}'
    target = output / 'results.jsonl'
    row['step_index'] = int(index)
    write_command_result(target, command_view(row, output_dir, stem, _write_json), serializable)
    return target


SUCCESS_ACTIONS = {
    'approach': '已接近目标', 'retreat': '已远离目标',
    'move_to': '已移动到目标', 'lift_up': '已竖直抬升',
    'put_down': '已竖直下降', 'shift_down': '已竖直下降',
    'pick': '已抓取目标', 'put': '已放置到目标',
    'grasp': '已抓取目标', 'release': '已释放目标',
    'push': '已推动目标', 'pull': '已拉动目标', 'pour': '已完成倒水动作',
}
ERROR_DESCRIPTIONS = {
    'GRASP_FAILED': '未能维持稳定抓持',
    'IK_FAILED': '逆运动学求解失败',
    'TARGET_UNREACHABLE': '目标超出机械臂可达范围',
    'PATH_NOT_FOUND': '未找到满足约束的运动路径',
    'PLANNING_FAILED': '未能生成满足约束的轨迹',
    'EXECUTION_TIMEOUT': '执行超时，未在规定时间内到达目标',
    'EXECUTION_FAILED': '轨迹执行失败',
    'TRAJECTORY_INVALID': '轨迹数据不满足执行要求',
    'JOINT_LIMIT_VIOLATION': '关节位置超出允许范围',
    'INVALID_REQUEST': '请求参数不符合要求',
    'CONTROLLER_NOT_READY': '控制器尚未就绪',
    'SCENE_CHANGED': '场景状态已变化，当前规划失效',
    'STOPPED_BY_SAFETY': '运动被安全检查中止',
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
    'GRASP_PRECHECK_FAILED': '抓取候选几何或接触路径检查全部失败',
    'JOINT_CONFIGURATION_SEARCH_FAILED': '关节构型搜索未找到可用路径',
    'GRASP_DEPTH_SEARCH_FAILED': '0～30 mm 抓深搜索及桌面底限兜底均失败',
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
    if command['skill_name'] in {'push', 'pull'}:
        return 'end_effector'
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
        if command['skill_name'] in {'move_to', 'put', 'approach'}:
            relation = command['parameters'].get('relation', 'at')
            direction = {'front': '前方', 'behind': '后方', 'left': '左侧',
                         'right': '右侧', 'above': '上方', 'below': '下方'}.get(relation)
            if direction:
                target = f'{target} {direction}'
        return f"success: {SUCCESS_ACTIONS.get(command['skill_name'], '已完成目标')} {target}"
    error = error if isinstance(error, dict) else {}
    description = ERROR_DESCRIPTIONS.get(error.get('error_code'))
    if description is None:
        description = STAGE_DESCRIPTIONS.get(error.get('failed_stage') or stage, '步骤执行失败')
    description = describe_error(error, description)
    description = description.replace('|', '｜')
    if status == 'suspend':
        description = f'任务中止，{target} 未完成目标；{description}'
    return f"error|{error_category(error, stage=stage)}|{description}"


def _default_output_dir(command_file):
    """Return the per-command output directory used when none is specified."""
    command_path = Path(command_file).expanduser().resolve()
    # Accept both the new layout (commands.xxx/commands.xxx.json) and a
    # legacy flat command file, while always placing output in commands.xxx/output.
    if command_path.parent.name == command_path.stem:
        return command_path.parent / 'output'
    return command_path.parent / command_path.stem / 'output'


def _write_command_planning_timing(output_dir, index, row):
    """Write compact planning-stage timings for one command and its motion phases."""
    stamp_command(row)
    phases = row.get('substeps')
    candidates = phases if isinstance(phases, list) else [row]
    motions = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        timing = item.get('timing')
        if not isinstance(timing, dict):
            continue
        trajectory = item.get('trajectory') or {}
        diagnostics = trajectory.get('diagnostics', {}) if isinstance(trajectory, dict) else {}
        timing_s = {
            key: value for key, value in timing.items()
            if key not in {'execution', 'total'} and isinstance(value, (int, float))
        }
        planner_details = {
            key: diagnostics[key] for key in
            ('algorithm', 'planning_stages_s', 'planning_proxy_cache', 'shortcut', 'trajopt', 'smoothing', 'collision_state_cache', 'path_quality',
             'configuration_selection')
            if key in diagnostics
        }
        ik_details = diagnostics.get('ik_branch_selection')
        if isinstance(ik_details, dict):
            planner_details['ik_branch_selection'] = {
                key: ik_details[key] for key in
                ('candidate_count', 'evaluated_count', 'selected_branch', 'explicit_seed',
                 'search_mode', 'ik_cache_hits', 'collision_cache_hits')
                if key in ik_details
            }
        timing_details = diagnostics.get('time_parameterization')
        if isinstance(timing_details, dict):
            planner_details['time_parameterization'] = {
                key: timing_details[key] for key in
                ('backend', 'path_interpolator', 'collision_fallback', 'trajectory_sampling_s',
                 'linear_path_validation_s', 'collision_validation_s', 'circular_path_validation_s',
                 'validation_waypoint_count', 'planner_waypoint_count',
                 'collision_resolution_rad', 'spline_subdivisions', 'path_validation_cache_enabled',
                 'validation_waypoints_identical_to_planner', 'path_validation_cache')
                if key in timing_details
            }
        motions.append({
            'phase': item.get('phase', 'move'),
            'planner': trajectory.get('planner'),
            'planning_mode': (item.get('selection') or {}).get('planning_mode'),
            'timing_s': timing_s,
            'planner_diagnostics': planner_details,
        })
    command_id = str(row.get('command_id', 'command'))
    relative_path = Path('metrics') / 'planning.jsonl'
    target = Path(output_dir) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target_resolution_timing = row.get('target_resolution_timing_s')
    target_resolution = row.get('target_resolution')
    if not isinstance(target_resolution_timing, dict) and isinstance(target_resolution, dict):
        target_resolution_timing = target_resolution.get('resolution_timing_s')
    entry = {
        'command_id': command_id,
        'skill_name': row.get('skill_name'),
        'success': row.get('success'),
        'planning_preparation_s': _command_planning_time(row),
        'target_preparation_s': row.get('target_preparation_s', 0.),
        'target_resolution_timing_s': target_resolution_timing or {},
        'vlm_request_s': _command_vlm_request_time(row),
        'vlm_request_count': row.get('vlm_request_count', 0),
        'vlm_failed_request_count': row.get('vlm_failed_request_count', 0),
        'pose_search_precheck_count': len(row.get('pose_search_precheck', [])),
        'successful_distance_m': row.get('successful_distance_m'),
        'motions': motions,
    }
    for motion in motions:
        details = motion['planner_diagnostics']
        selection = details.get('configuration_selection')
        if isinstance(selection, dict):
            details['configuration_selection'] = {key: value for key, value in selection.items()
                                                  if key != 'candidate_timings'}
        if isinstance(details.get('ik_branch_selection'), dict):
            details['ik_branch_selection'].pop('rejected', None)
    write_metrics(target, entry, serializable)
    row['planning_timing_file'] = relative_path.as_posix()
    return relative_path


def _command_vlm_request_time(row):
    """Read request-entry measurements, never infer time from perception results."""
    stamp_command(row)
    value = row.get('vlm_request_s')
    if isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value) and value >= 0:
        return float(value)
    return None


def _command_planning_time(row):
    """Sum disjoint preparation intervals; inclusive subtotals take precedence."""
    phases = row.get('substeps')
    candidates = phases if isinstance(phases, list) else [row]
    planning_s = float(row.get('target_preparation_s', 0.))
    planning_s += sum(float(item.get('elapsed_s', 0.)) for item in row.get('pose_search_precheck', []))
    for item in candidates:
        if not isinstance(item, dict):
            continue
        timing = item.get('timing')
        if not isinstance(timing, dict):
            continue
        value = timing.get('pre_execution_total')
        if isinstance(value, (int, float)):
            planning_s += float(value)
            continue
        fields = ('validation', 'context', 'selection', 'path_generation',
                  'planning', 'trajectory_preparation', 'posture_scoring')
        measured = [float(timing[key]) for key in fields if isinstance(timing.get(key), (int, float))]
        if measured:
            planning_s += sum(measured)
            continue
        attempt = timing.get('curobo_attempt')
        if isinstance(attempt, (int, float)):
            planning_s += float(attempt)
        elif ((timing.get('failed_stage') == 'planning') or
              (isinstance(item.get('error'), dict) and item['error'].get('failed_stage') == 'planning')) \
                and isinstance(timing.get('total'), (int, float)):
            planning_s += float(timing['total'])
    return planning_s


def _print_command_timing(row):
    """Print all motion preparation, excluding VLM calls and execution."""
    planning_s = _command_planning_time(row)
    row['planning_preparation_s'] = planning_s
    parts = [f"timing|command={row.get('command_id', 'unknown')}",
             f"planning={planning_s:.3f}s"]
    vlm_request_s = _command_vlm_request_time(row)
    if vlm_request_s is not None:
        parts.append(f'vlm_request={vlm_request_s:.3f}s')
    print('|'.join(parts), flush=True)


def _resolve_command_file(command_file):
    """Resolve a command path, including a bare commands.xxx.json filename."""
    command_path = Path(command_file).expanduser()
    if command_path.is_file():
        return command_path
    if not command_path.is_absolute() and command_path.parent == Path('.'):
        for request_dir in sorted((ROOT / 'requests').glob(f'{command_path.stem}')):
            organized = request_dir / command_path.name
            if organized.is_file():
                return organized
    return command_path


def run(command_file, *, catalog=None, scene_file=None, backend_name='mujoco',
        output_dir=None, viewer=False, collision_probe=False,
        object_library_url=None, start_object_library=False, show_collision_geometry=False):
    output = output_dir if output_dir is not None else _default_output_dir(command_file)
    with output_lock(output), command_timing_scope():
        return _run(command_file, catalog=catalog, scene_file=scene_file, backend_name=backend_name,
                    output_dir=output, viewer=viewer, collision_probe=collision_probe,
                    object_library_url=object_library_url, start_object_library=start_object_library,
                    show_collision_geometry=show_collision_geometry)


def _run(command_file, *, catalog=None, scene_file=None, backend_name='mujoco',
         output_dir=None, viewer=False, collision_probe=False,
         object_library_url=None, start_object_library=False, show_collision_geometry=False):
    from libraries.eaik import load_urdf
    from collision import SimPlatformFCLCollisionChecker
    from planning.trajectory.time_parameterization import prepare_continuous_trajectory
    from services.object_library import ObjectLibraryClient, LocalObjectLibraryService, ObjectLibraryError
    from atomic_skill.runner import TaskSkillExecutor
    from demo.interaction.object_library import ObjectLibraryGraspResolver, apply_ols_friction, scene_object_by_id, model_id_for

    task = json.loads(Path(command_file).read_text(encoding='utf-8'))
    from demo.interaction.visual_targets import (
        infer_command_point_types, infer_command_place_cycles, infer_command_place_parameters,
        infer_visual_targets)
    commands = task.get('commands')
    if not isinstance(commands, list) or not commands:
        raise ValueError('commands must be a non-empty array')
    visual_estimates = infer_visual_targets(commands)
    visual_command_point_types = infer_command_point_types(commands)
    visual_place_cycles = infer_command_place_cycles(commands)
    visual_place_parameters = infer_command_place_parameters(commands)
    if output_dir is None:
        output_dir = _default_output_dir(command_file)
    source_scene = scene_file or task.get('scene_file')
    output = prepare_output(output_dir, protected=(command_file, catalog,
        (Path(command_file).parent / source_scene) if source_scene else None))
    report = {'request_id': task.get('request_id'), 'success': False, 'steps': [],
              'execution_started': False,
              'collision_policy': 'planning_only'}
    if scene_file is None:
        scene_file = task.get('scene_file')
    if scene_file:
        scene_path = (Path(command_file).parent / scene_file).resolve()
        asset_root = ROOT / 'services' / 'object_library_server' / 'data'
        if catalog:
            # The integration catalog may select a matching local OLS revision.
            catalog_path = Path(catalog).resolve()
            configured_root = json.loads(catalog_path.read_text(encoding='utf-8')).get('asset_root')
            if configured_root:
                asset_root = (catalog_path.parent / configured_root).resolve()
        coordinator = SceneResourceCoordinator(
            scene_file=scene_path,
            asset_root=asset_root,
        )
    else:
        coordinator = SceneResourceCoordinator(catalog)
    backend = checker = None
    curobo_action = None
    from execution.runtime.state import TargetBlackboard
    blackboard = TargetBlackboard()
    sync = SceneStateSynchronizer(blackboard=blackboard)
    local_service = resolver = None
    stage = 'validation'
    frame_index = 0
    active_command = None
    active_step_logged = True
    active_target_name = None
    row = None
    try:
        resources = coordinator.resolve(task, backend_name)
        for item in visual_estimates:
            scene_object_by_id(resources.scene, item['target_id'])
        report['effective_defaults'] = {
            'motion': {key: resources.scene['move_defaults']['constraints'][key]
                       for key in ('velocity_scale', 'acceleration_scale')},
            'trajectory': dict(resources.scene['trajectory']),
        }
        blackboard.register_scene(resources.scene)
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
            if skill not in {'move_to', 'lift_up', 'put_down', 'shift_down', 'grasp', 'release', 'approach', 'retreat', 'pick', 'put', 'push', 'pull', 'pour'}:
                raise ValueError(f'This integration runner supports move_to/lift_up/put_down/grasp/release/approach/retreat/pick/put/push/pull/pour: {skill}')
            parameters = command.get('parameters', {})
            target = parameters.get('target')
            if skill == 'pour':
                source_name = parameters.get('source')
                target_name = parameters.get('target')
                names = {o['object_name_in_scene'] for o in resources.scene['objects']}
                if source_name not in names or target_name not in names:
                    raise ValueError('pour source and target must be scene object names')
                continue
            if skill in {'push', 'pull'}:
                # Both commands move the held tool without a scene-object target.
                continue
            if skill in {'approach', 'retreat'}:
                if target is None or target in targets or target in {'end_effector', '__end_effector__'}:
                    raise ValueError(f'{skill} requires an interaction object target')
                model_id_for(scene_object_by_id(resources.scene, target))
                object_commands.add(cid)
                continue
            if skill in {'grasp', 'release'}:
                if 'link' in parameters or 'allow_contact' in parameters:
                    raise ValueError(f'{skill} no longer accepts link or allow_contact')
                grasp_options = parameters.get('grasp')
                if isinstance(grasp_options, dict) and 'mode' in grasp_options:
                    raise ValueError(f'{skill} no longer accepts grasp.mode')
            if skill in {'push', 'pull'} or parameters.get('link'):
                if skill in {'pick', 'put'}:
                    raise ValueError('Mechanisms use move/grasp/push/pull/release, not pick/put retreat sequences')
                from demo.interaction.requests import body_name
                body_name(resources, parameters)
                continue
            if skill in {'pick', 'put'} and (target in targets or target in {'end_effector', '__end_effector__'}):
                raise ValueError(f'{skill} requires an interaction object target')
            if skill in {'move_to', 'pick', 'put'} and target not in targets and target not in {'end_effector', '__end_effector__'}:
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
                object_library_url or config.get('base_url', 'http://127.0.0.1:8000'), uid=config.get('uid', 0)), blackboard=blackboard)
            try:
                apply_ols_friction(resources, resolver.client, blackboard)
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
                    object_library_url, uid=config.get('uid', 0)), blackboard=blackboard)
                apply_ols_friction(resources, resolver.client, blackboard)
        ref = component(resources.robot_object, 'RobotModelRef')
        stage = 'planning_initialization'
        # Created without importing/constructing MuJoCo; planning does not need runtime geometry.
        kin = load_urdf(resources.robot_urdf, base_link=ref['base_link'], tip_link=ref['tip_link'],
                        tcp_transform=ref.get('tcp_transform'), world_from_base=pose_matrix(resources.robot_object),
                        initial_joints=resources.initial_joints, validation_backend="auto")
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
        # From backend initialization onward, effects cannot be ruled out by
        # the absence of completed commands (settling/gripper startup included).
        report['execution_started'] = True
        if visual_estimates:
            if backend_name != 'mujoco':
                raise ValueError('Visual estimates require the MuJoCo backend')
            backend.load(resources, output / 'runtime' / backend_name, camera_scene_json=resources.scene_file)
            from demo.interaction.vlm_grasp import VLMGraspResolver
            resolver = VLMGraspResolver(
                resolver.client, blackboard, backend,
                visual_targets=visual_estimates,
                command_point_types=visual_command_point_types,
                command_place_cycles=visual_place_cycles,
                command_place_parameters=visual_place_parameters,
            )
        else:
            backend.load(resources, output / 'runtime' / backend_name)
        report['consistency'] = coordinator.validate_consistency(resources, kin, checker, backend)
        if isinstance(checker, GraspCollisionChecker):
            checker.bind_backend(backend)
            backend.gripper_controller = backend.create_gripper_controller()
            proxy_config = resources.scene.get('planning_collision_proxy', {})
            if not isinstance(proxy_config, dict) or not isinstance(proxy_config.get('enabled', False), bool):
                raise ValueError('planning_collision_proxy requires an enabled boolean')
            if proxy_config.get('enabled'):
                checker.enable_planning_proxy({k:v for k,v in proxy_config.items() if k != 'enabled'})
            start_check = checker.check(resources.initial_joints, safety_distance=0.003)
            if start_check.in_collision:
                raise ValueError(f'Initial assembled robot collides: {start_check.contacts}')
            if getattr(checker, 'planning_proxy', None) is not None:
                report['planning_collision_proxy'] = checker.planning_proxy.snapshot['planning_proxy']
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
        from atomic_action.motion.move_linear.action import MoveLinearAction
        from atomic_action.motion.move_joint.action import MoveJointAction
        from atomic_action.motion.move_circular.action import MoveCircularAction
        startup_frames = [backend.get_state()]
        if component(resources.robot_object, 'GripperModel'):
            backend.set_frame_callback(startup_frames.append)
            try:
                backend.advance_gripper(1.0)
            finally:
                backend.set_frame_callback(None)
            sync.sync(backend, kin, checker)

        def prepare_trajectory(trajectory, request, selection):
            from demo.interaction.command_converter import _deep_merge
            request = _deep_merge(resources.scene['move_defaults'], request)
            timed = prepare_continuous_trajectory(trajectory, request, collision_checker=checker,
                                                 timestep=float(backend.model.opt.timestep), kinematics=kin)
            trajectory.update(timed)
            backend.set_status(f"{report['active_command_id']} | Executing / {report.get('active_substep', 'move')}")

        from planning.planners.curobo import CuroboPlanner
        curobo_action = CuroboPlanner(
            robot_runtime=backend,
            robot_object_id=resources.robot_object.get('object_id_in_scene'),
            collision_checker=checker,
        )
        motion_actions = {
            "linear": MoveLinearAction(robot_runtime=backend, kinematics=kin,
                                       collision_checker=checker, trajectory_callback=prepare_trajectory,
                                       curobo_planner=curobo_action),
            "joint": MoveJointAction(robot_runtime=backend, kinematics=kin,
                                     collision_checker=checker, trajectory_callback=prepare_trajectory,
                                     curobo_planner=curobo_action),
            "circular": MoveCircularAction(robot_runtime=backend, kinematics=kin,
                                           collision_checker=checker, trajectory_callback=prepare_trajectory,
                                           curobo_planner=curobo_action),
        }
        def execute_motion(request):
            # Select the concrete motion action from the request geometry.
            # Merge before action validation inserts its own missing defaults.
            from demo.interaction.command_converter import _deep_merge
            request = _deep_merge(resources.scene['move_defaults'], request)
            sync.sync(backend, kin, checker)
            # Validation applies the same default later, but planner selection
            # happens before action validation. Treat omitted values as auto.
            # Auto uses the host planner's direct -> cuRobo fallback chain.
            requested_planner = request.get('planning', {}).get('planner_type', 'auto')
            if requested_planner == 'curobo':
                curobo_result = curobo_action.execute(request)
                return curobo_result
            path_type = request.get("motion", {}).get("path_type", "joint")
            action = motion_actions.get(path_type, motion_actions["joint"])
            return action.execute(request)
        def write_task_frame(command_id, substep, frame_state):
            nonlocal frame_index
            blackboard.observe(frame_state)
            frames.write(json.dumps({'frame_index': frame_index, 'command_id': command_id,
                                      'substep': substep, **frame_state}, default=serializable) + '\n')
            frame_index += 1

        task_executor = TaskSkillExecutor(
            resources=resources, backend=backend, kinematics=kin, checker=checker,
            synchronizer=sync, resolver=resolver, execute_motion=execute_motion,
            report=report, write_frame=write_task_frame)
        with (output / 'trajectories/frames.jsonl').open('w', encoding='utf-8') as frames:
            for frame_state in startup_frames:
                write_task_frame('setup', 'initial_state', frame_state)
            for index, command in enumerate(commands, 1):
                start_command(command['command_id'])
                active_command = command
                active_step_logged = False
                row = None
                active_target_name = None
                active_target_name = command_target_name(command, resources.scene)
                report['active_command_id'] = command['command_id']
                print(f"[{index}/{len(commands)}] {command['skill_name']}({active_target_name})", flush=True)
                backend.set_status(f"{command['command_id']} | Planning / {command['skill_name']}")
                stage = 'state_sync'
                with sync.command_state(backend, kin, checker) as state:
                    # Every command has a recorder, even when a new skill does
                    # not install its own phase-specific callback.
                    report['active_substep'] = command['skill_name']
                    backend.set_frame_callback(lambda frame_state: write_task_frame(
                        command['command_id'], report['active_substep'], frame_state))
                    row = {'command_id': command['command_id'], 'skill_name': command['skill_name']}
                    parameters = command['parameters']
                    mechanism = command['skill_name'] not in {'push', 'pull'} and bool(parameters.get('link'))
                    backend.contact_target = None
                    backend.contact_target_object_id = None
                    backend.grasp_motion_phase = False
                    if mechanism:
                        from demo.interaction.requests import body_name
                        backend.contact_target = body_name(resources, parameters) if parameters.get('allow_contact') else None
                    elif command['skill_name'] == 'pick':
                        # Pick permits gripper contact with its selected object
                        # throughout motion and closure. Other scene contacts
                        # remain subject to the normal collision policy.
                        backend.contact_target = active_target_name
                        backend.contact_target_object_id = parameters.get('target')
                    if command['skill_name'] in {'push', 'pull'}:
                        stage = 'pull_push'
                        from atomic_skill.pull import CommandPullExecutor
                        from atomic_skill.push import CommandPushExecutor
                        executor_type = CommandPullExecutor if command['skill_name'] == 'pull' else CommandPushExecutor
                        orchestrator = executor_type(
                            resources=resources, backend=backend, kinematics=kin,
                            synchronizer=sync, checker=checker,
                            execute_motion=execute_motion, push_pull_skill=None,
                            grasper=task_executor.grasper, releaser=task_executor.releaser)
                        result = orchestrator.execute(parameters)
                        row.update(result)
                        sync.sync(backend,kin,checker)
                        from execution.runtime.grasp_verification import verify_command_grasp
                        verify_command_grasp(backend, row)
                    elif command['skill_name'] in {'move_to', 'lift_up', 'put_down', 'shift_down', 'grasp', 'release', 'approach', 'retreat', 'pick', 'put', 'pour'}:
                        try:
                            task_executor.execute(command, index, state, row)
                        except Exception as exc:
                            clear_detach = getattr(checker, 'clear_detach_contacts', None)
                            if callable(clear_detach):
                                clear_detach()
                            stage = task_executor.stage
                            row.setdefault('success', False)
                            row['error'] = error_from_exception(exc, stage=stage, existing=row.get('error'))
                            _write_command_planning_timing(output, index, row)
                            report['steps'].append(row)
                            _write_command_report(output, index, row)
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
                _write_command_planning_timing(output, index, row)
                report['steps'].append(row)
                _write_command_report(output, index, row)
                print(step_message(command, success=bool(row.get('success')),
                                   error=display_error, stage=stage,
                                   target_name=active_target_name, status=display_status), flush=True)
                _print_command_timing(row)
                active_step_logged = True
                if not row.get('success'):
                    report.update(final_state=backend.get_state(), frame_count=frame_index,
                                  physics_steps=backend.physics_steps)
                    stage = (row.get('error') or {}).get('failed_stage', stage)
                    raise RuntimeError(f"Step failed: {row.get('error')}")
        report.update(success=True, status='success', final_state=backend.get_state(), frame_count=frame_index,
                      collision_cache_stats=dict(checker.cache_stats),
                      path_validation_cache_stats=dict(getattr(checker, 'path_validation_cache_stats', {})),
                      planning_cache_stats=dict(getattr(checker, 'planning_cache_totals', {})),
                      physics_steps=backend.physics_steps, execution_backend=backend_name,
                      viewer_sync_count=getattr(backend, 'viewer_sync_count', 0))
        backend.set_status('Completed')
    except Exception as exc:
        report.update(success=False, status='error')
        if active_command is not None and not active_step_logged:
            error = error_from_exception(exc, stage=stage, existing=row.get('error') if row else None)
            if row is None:
                row = {'command_id': active_command.get('command_id'),
                       'skill_name': active_command.get('skill_name')}
            row.setdefault('success', False)
            row.setdefault('error', error)
            _write_command_planning_timing(output, index, row)
            _write_command_report(output, index, row)
            if not any(step.get('command_id') == active_command.get('command_id')
                       for step in report['steps']):
                report['steps'].append(row)
            print(step_message(active_command, success=False,
                               error=error,
                               stage=stage, target_name=active_target_name), flush=True)
            _print_command_timing(row)
            active_step_logged = True
        elif active_command is None:
            initial_error = error_from_exception(exc, stage=stage)
            cause = describe_error(initial_error, '初始化失败')
            cause = cause.replace('|', '｜')
            print(f"error|{error_category(initial_error, stage=stage)}|初始化失败：{cause}", flush=True)
        report['error'] = {'stage': stage, 'message': str(exc), 'type': type(exc).__name__}
        report['error'].update(error_from_exception(
            exc, stage=stage, existing=row.get('error') if row else None))
    finally:
        try:
            if curobo_action is not None:
                curobo_action.close()
            if backend is not None and hasattr(backend, 'data'):
                final_state = None
                try:
                    final_state = backend.get_state()
                    blackboard.observe(final_state)
                    report['final_state'] = final_state
                except Exception as state_error:
                    # A stale earlier snapshot must not certify an interrupted run.
                    report.pop('final_state', None)
                    report['state_capture_error'] = str(state_error)
                np.savez_compressed(output / 'snapshots/final_state.npz', qpos=backend.data.qpos, qvel=backend.data.qvel,
                                    ctrl=backend.data.ctrl, eq_active=backend.data.eq_active,
                                    eq_data=backend.model.eq_data, time=float(backend.data.time))
                try:
                    from demo.final_scene import final_scene
                    if final_state is None:
                        raise ValueError('Fresh final state unavailable; scene export refused')
                    authored_scene = json.loads(Path(resources.scene_file).read_text(encoding='utf-8'))
                    _write_json(output / 'snapshots/final_scene.json', final_scene(authored_scene, backend, final_state))
                    report['final_scene_file'] = 'snapshots/final_scene.json'
                except Exception as exc:
                    report['final_scene_export_error'] = str(exc)
            if backend is not None and 'resources' in locals() and (output / 'trajectories/frames.jsonl').exists():
                try:
                    from execution.output.trajectory_export import write_traj_from_jsonl
                    trajectory_scene = resources.scene
                    # The runtime scene fuses the attached gripper into the
                    # robot for MuJoCo. WebEditor still has a separate
                    # RobotAttachment object, so restore that object in the
                    # exported scene to drive its URDF finger joint.
                    source_scene = json.loads(Path(resources.scene_file).read_text(encoding='utf-8'))
                    existing_ids = {int(obj['object_id_in_scene']) for obj in trajectory_scene.get('objects', ())}
                    attachments = [obj for obj in source_scene.get('objects', ())
                                   if any(component.get('component_type') == 'RobotAttachment'
                                          for component in obj.get('components', ())) and
                                   int(obj['object_id_in_scene']) not in existing_ids]
                    if attachments:
                        trajectory_scene = dict(trajectory_scene)
                        trajectory_scene['objects'] = [*trajectory_scene.get('objects', ()), *attachments]
                    traj_path = write_traj_from_jsonl(
                        output / 'trajectories/execution.traj', trajectory_scene, output / 'trajectories/frames.jsonl',
                        initial_joints=resources.initial_joints)
                    report['trajectory_file'] = str(traj_path)
                except Exception as exc:
                    report['trajectory_export_error'] = str(exc)
            blackboard_path = output / 'snapshots/blackboard.json'
            blackboard_path.write_text(json.dumps(blackboard.snapshot(), indent=2, default=serializable), encoding='utf-8')
            report['blackboard_file'] = str(blackboard_path)
            report['allowed_carrying_contacts'] = list(getattr(checker, 'carrying_contact_log', {}).values())
            if 'final_state' in report:
                _write_json(output / 'snapshots/state.json', report['final_state'])
            _write_json(output / 'summary.json', summary_view(report))
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
    parser.add_argument('--commands', type=Path,
                        default=ROOT / 'requests/commands.001/commands.001.json')
    parser.add_argument('--catalog', type=Path, default=None,
                        help='Legacy scene catalog; new requests use scene_file in the task')
    parser.add_argument('--backend', default='mujoco')
    parser.add_argument('--library', type=Path, default=DEFAULT_SIMPLATFORM_LIBRARY,
                        help='SimPlatform native DLL (default: libraries/simplatform/bin/sim_platform_c.dll)')
    parser.add_argument('--output', type=Path, default=None,
                        help='输出目录（默认：对应 commands.xxx/output）')
    parser.add_argument('--viewer', action='store_true')
    parser.add_argument('--show-collision-geometry', action='store_true',
                        help='显示 MuJoCo 碰撞几何（自动打开 viewer）')
    parser.add_argument('--object-library-url', help='Existing OLS service URL')
    parser.add_argument('--start-object-library', action='store_true', help='Start project OLS on a free loopback port for this task')
    parser.add_argument('--collision-probe', action='store_true', help='Test-only: insert and restore probe_block at the TCP before execution')
    args = parser.parse_args()
    args.commands = _resolve_command_file(args.commands)
    if not args.commands.is_file():
        parser.error(f'commands file not found: {args.commands}')
    if args.catalog is not None and not args.catalog.is_file():
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
