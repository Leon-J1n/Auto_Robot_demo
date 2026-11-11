"""Short error explanations that retain the cause and measured evidence."""
import math
from collections import Counter
from numbers import Real


_CAUSES = {
    'VLM grasp depth is outside the target model bounds': 'VLM 抓取点深度超出目标模型范围',
    'VLM grasp depth is on the hidden side; selected depth may belong to the table/background':
        'VLM 抓取点落在目标背面，疑似选到了桌面或背景深度',
    'VLM placement point is outside the target model bounds': 'VLM 放置点超出目标模型范围',
    'VLM placement point is too close to the target rim': 'VLM 放置点太靠近容器边缘',
    'VLM placement point is on the target wall or rim': 'VLM 放置点落在容器侧壁或边缘',
    'No visible basket floor depth inside the target model': '目标内部没有可见底面的有效深度',
    'Target is behind the requested camera': '目标位于所选相机后方',
    'Target projects outside the requested camera image': '目标超出所选相机视野',
    'RGB-D surface point is outside the model grasp depth': 'RGB-D 表面点没有可用的抓取深度',
    'VLM placement requires a confirmed held object': '尚未确认抓住物体，无法计算放置位姿',
    'RobotModelRef.grasp_frame calibration is required': '缺少机械臂抓取坐标系标定',
    'No collision-free IK solution exists for the relative move_to target': '相对移动目标没有无碰撞的逆解',
    'VLM response must start with a JSON object.': 'VLM 返回内容不是有效的 JSON 对象',
    'VLM response must be a JSON object.': 'VLM 返回内容不是 JSON 对象',
    'No IK configuration reaches the target.': '目标位姿没有可用逆解',
    'Start state is in collision.': '运动起点存在碰撞或安全间距不足',
    'Goal state is in collision.': '运动终点存在碰撞或安全间距不足',
    'Grasp lost: sustained contact loss or measured payload slip.': '持物持续丢失接触，或检测到物体相对夹爪滑移',
    'Grasp failed: the payload lost stable contact.': '持物失去稳定接触',
}
_REASONS = {
    'prepoint_ik': '预接近位姿无逆解', 'prepoint_collision': '预接近位姿间距不足或碰撞',
    'contact_ik': '接触路径无逆解', 'contact_collision': '接触路径间距不足或碰撞',
    'contact_planning': '接触路径规划失败', 'ik_failed': '目标位姿无逆解',
    'goal_collision': '目标位姿间距不足或碰撞',
    'grasp_confirmation_failed': '夹爪闭合后未确认抓住目标',
    'grasp_closure_collision': '夹爪闭合或张开时发生碰撞',
    'grasp_closure_failed': '夹爪闭合预检失败',
    'grasp_below_table_floor': '闭爪前张开夹爪最低点未高于桌面底限',
    'grasp_geometry_collision': '抓取位姿下夹爪静态张开构型与环境碰撞或安全间距不足',
}


def _number(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def error_from_exception(exc, *, stage=None, existing=None):
    """Retain both structured diagnostics and the original exception cause."""
    payload = getattr(exc, 'details', None)
    result = {}
    if isinstance(payload, dict):
        if any(key in payload for key in ('error_code', 'error_message', 'failed_stage')):
            result.update(payload)
        else:
            result['details'] = payload
    elif payload is not None:
        result['details'] = payload
    code = getattr(exc, 'error_code', None) or getattr(exc, 'code', None)
    if code:
        result.setdefault('error_code', getattr(code, 'value', code))
    if isinstance(existing, dict):
        result.update(existing)
    if not result.get('error_message'):
        result['error_message'] = str(exc) or type(exc).__name__
    result.setdefault('failed_stage', stage)
    return result


# Rejections and attempts are diagnostic history, never the current event.
def _contacts(error):
    if isinstance(error, dict):
        if ((error.get('geom1') and error.get('geom2')) or
                (error.get('body1') and error.get('body2'))):
            yield error
        direct = list(_contacts(error.get('contacts')))
        if direct:
            yield from direct
            return
        details = list(_contacts(error.get('details')))
        if details:
            yield from details
            return
        yield from _contacts(error.get('error'))
    elif isinstance(error, (list, tuple)):
        for item in error:
            yield from _contacts(item)


_SEARCH_FAILURES = {
    'GRASP_DEPTH_SEARCH_FAILED': '抓深候选全部不可行',
    'PLACEMENT_HEIGHT_SEARCH_FAILED': '0～10 cm 的 11 档放置高度均不可行',
    'NO_VALID_APPROACH': '预接近候选全部不可行',
    'NO_VALID_TARGET_POSE': '目标位姿候选全部不可用',
    'TARGET_POSE_COLLISION': '目标位姿候选全部被碰撞或间距检查淘汰',
    'TARGET_POSE_UNREACHABLE': '目标位姿候选均无可用逆解',
    'GRASP_CLOSURE_INFEASIBLE': '抓取候选几何或接触路径检查全部失败',
    'GRASP_PRECHECK_FAILED': '抓取候选几何或接触路径检查全部失败',
    'JOINT_CONFIGURATION_SEARCH_FAILED': '关节构型搜索未找到可用路径',
    'PREPOINT_UNREACHABLE': '预接近候选均无可用逆解',
    'PREPOINT_COLLISION': '预接近候选均被碰撞或间距检查淘汰',
    'CONTACT_PATH_UNREACHABLE': '接触路径候选均无可用逆解',
    'CONTACT_PATH_COLLISION': '接触路径候选均被碰撞或间距检查淘汰',
    'CONTACT_PATH_PLANNING_FAILED': '接触路径候选规划均失败',
}
_REASONS.update({
    'COLLISION_DETECTED': '碰撞或安全间距检查失败',
    'PATH_CONSTRAINT_INFEASIBLE': '指定路径约束检查失败',
    'PLANNING_TIMEOUT': '规划超时', 'PATH_NOT_FOUND': '规划预算内未找到路径',
    'PLANNING_FAILED': '路径规划失败', 'IK_FAILED': '逆运动学求解失败',
    'JOINT_LIMIT_VIOLATION': '超出关节限位',
    'contact_validation_failed': '接触路径预检失败',
    'unknown': '未提供具体原因',
})

_ERROR_CATEGORY_BY_CODE = {
    # Request and precondition checks.
    'INVALID_REQUEST': '请求与状态检查', 'UNKNOWN_STRATEGY': '请求与状态检查',
    'NOT_IMPLEMENTED': '请求与状态检查', 'SCENE_CHANGED': '请求与状态检查',
    'CONTROLLER_NOT_READY': '请求与状态检查', 'PUT_TARGET_IS_HELD_OBJECT': '请求与状态检查',
    'OBJECT_NOT_HELD': '请求与状态检查', 'RETREAT_REFERENCE_MISSING': '请求与状态检查',
    # Observation and perception.
    'TARGET_NOT_FOUND': '观测与感知', 'TARGET_DESCRIPTION_AMBIGUOUS': '观测与感知',
    'MODEL_NOT_FOUND': '观测与感知', 'INSTANCE_AMBIGUOUS': '观测与感知',
    'SENSOR_DATA_UNAVAILABLE': '观测与感知', 'DEPTH_UNAVAILABLE': '观测与感知',
    'INSUFFICIENT_POINTS': '观测与感知', 'PROJECTION_FAILED': '观测与感知',
    'ORIENTATION_ESTIMATION_FAILED': '观测与感知', 'POSE_ESTIMATION_FAILED': '观测与感知',
    'GRASP_POSE_PREDICTION_FAILED': '观测与感知', 'LOW_CONFIDENCE': '观测与感知',
    'LOCALIZATION_TIMEOUT': '观测与感知', 'VLM_DEPTH_INVALID': '观测与感知',
    'HANDLE_POSE_ESTIMATION_FAILED': '观测与感知', 'PLACEMENT_PERCEPTION_FAILED': '观测与感知',
    'AABB_FACE_AMBIGUOUS': '观测与感知',
    # Target parsing and pose construction.
    'TARGET_RESOLUTION_FAILED': '目标解析', 'FRAME_TRANSFORM_FAILED': '目标解析',
    'REFERENCE_INVALID': '目标解析', 'DIRECTION_UNRESOLVED': '目标解析',
    'SEMANTIC_DIRECTION_AMBIGUOUS': '目标解析', 'INVALID_OFFSET': '目标解析',
    'NO_VALID_CANDIDATE': '目标解析',
    'WRIST_GRASP_REFINEMENT_FAILED': '目标解析', 'PLACEMENT_REGION_UNOBSERVED': '目标解析',
    # Target pose feasibility.
    'TARGET_UNREACHABLE': '目标可行性', 'TARGET_POSE_UNREACHABLE': '目标可行性',
    'TARGET_POSE_COLLISION': '目标可行性', 'NO_VALID_TARGET_POSE': '目标可行性',
    'GRASP_DEPTH_SEARCH_FAILED': '目标可行性', 'PLACEMENT_HEIGHT_SEARCH_FAILED': '目标可行性',
    'IK_FAILED': '目标可行性',
    'JOINT_LIMIT_VIOLATION': '目标可行性', 'SINGULARITY_RISK': '目标可行性',
    'CLEARANCE_INSUFFICIENT': '目标可行性', 'GRASP_CLOSURE_INFEASIBLE': '目标可行性',
    'GRASP_CLOSURE_COLLISION': '目标可行性', 'GRASP_OPENING_COLLISION': '目标可行性',
    'GRASP_PRECHECK_FAILED': '目标可行性', 'PREPOINT_UNREACHABLE': '目标可行性',
    'PREPOINT_COLLISION': '目标可行性', 'CONTACT_PATH_UNREACHABLE': '目标可行性',
    'CONTACT_PATH_COLLISION': '目标可行性', 'NO_VALID_APPROACH': '目标可行性',
    # Path search and trajectory construction.
    'PATH_CONSTRAINT_INFEASIBLE': '路径规划', 'PLANNING_FAILED': '路径规划',
    'PATH_NOT_FOUND': '路径规划', 'PLANNING_TIMEOUT': '路径规划',
    'TRAJECTORY_INVALID': '路径规划', 'CONTACT_PATH_PLANNING_FAILED': '路径规划',
    'JOINT_CONFIGURATION_SEARCH_FAILED': '路径规划',
    # Motion execution and safety stops.
    'EXECUTION_FAILED': '运动执行', 'EXECUTION_TIMEOUT': '运动执行',
    'STOPPED_BY_SAFETY': '运动执行', 'ACTUATOR_TRACKING_FAILED': '运动执行',
    'PUSH_PULL_TIMEOUT': '运动执行', 'PUSH_PULL_EXECUTION_ERROR': '运动执行',
    'CONTACT_NOT_ESTABLISHED': '运动执行', 'CONTACT_LOST': '运动执行',
    'FORCE_LIMIT': '运动执行', 'ROBOT_JOINT_LIMIT': '运动执行',
    'JOINT_LIMIT': '运动执行', 'TRAVEL_LIMIT': '运动执行',
    'COLLISION_DETECTED': '路径规划', 'TARGET_NOT_REACHED': '运动执行',
    'DOOR_TARGET_NOT_REACHED': '运动执行', 'DRAWER_TARGET_NOT_REACHED': '运动执行',
    # Post-action outcome checks.
    'GRASP_FAILED': '结果验证', 'GRASP_NOT_CONFIRMED': '结果验证',
    'POSE_VERIFICATION_FAILED': '结果验证', 'RELEASE_FAILED': '结果验证',
    'POUR_OUTLET_DRIFT': '结果验证', 'POUR_OPENING_NOT_DOWN': '结果验证',
}

_ERROR_CATEGORY_BY_STAGE = {
    'validation': '请求与状态检查', 'state_sync': '请求与状态检查',
    'conversion': '请求与状态检查', 'initialization': '请求与状态检查',
    'perception': '观测与感知', 'localization': '观测与感知',
    'vlm': 'VLM 交互', 'vlm_request': 'VLM 交互', 'vlm_response': 'VLM 交互',
    'target_resolution': '目标解析', 'kinematics': '目标可行性',
    'interaction_position_verification': '目标可行性',
    'planning': '路径规划', 'path_generation': '路径规划',
    'planning_and_execution': '运动执行', 'execution': '运动执行',
    'grasp': '结果验证', 'release': '结果验证',
    'post_command_verification': '结果验证',
    'wrist_refinement': '目标解析', 'grasp_precheck': '目标可行性',
    'pour': '结果验证',
}


def error_category(error, *, stage=None):
    """Return the broad lifecycle category for a structured runtime error."""
    error = error if isinstance(error, dict) else {}
    code = str(error.get('error_code') or '').upper()
    message = str(error.get('error_message') or error.get('message') or '').lower()
    failed_stage = str(error.get('failed_stage') or stage or '').lower()

    if code.startswith(('VLM_REQUEST_', 'VLM_RESPONSE_')):
        return 'VLM 交互'
    if code in _ERROR_CATEGORY_BY_CODE:
        if code == 'COLLISION_DETECTED' and failed_stage in {'target_resolution', 'kinematics', 'interaction_position_verification'}:
            return '目标可行性'
        return _ERROR_CATEGORY_BY_CODE[code]
    if 'vlm' in message or 'vlm' in failed_stage:
        return 'VLM 交互'
    return _ERROR_CATEGORY_BY_STAGE.get(failed_stage, '请求与状态检查')


def _rejections(error):
    """Use one authoritative rejection list; never count flattened copies twice."""
    if not isinstance(error, dict):
        return []
    items = error.get('rejected')
    if isinstance(items, (list, tuple)) and items:
        return [item for item in items if isinstance(item, dict) and item.get('success') is not True]
    details = error.get('details')
    if isinstance(details, dict):
        return _rejections(details)
    return []


def _rejection_reason(item):
    reason = item.get('category') or item.get('reason') or item.get('error_code')
    if isinstance(reason, str) and reason in _REASONS:
        return reason
    nested = item.get('error')
    if isinstance(nested, dict) and nested.get('error_code') in _REASONS:
        return nested['error_code']
    # A contact report proves this candidate failed its collision check; it
    # does not establish why other candidates, or the whole search, failed.
    if list(_contacts(item)):
        return 'COLLISION_DETECTED'
    return 'unknown'


def _search_summary(error, fallback):
    code = error.get('error_code')
    details = error.get('details') if isinstance(error.get('details'), dict) else {}
    attempts = details.get('depth_attempts')
    rejected = _rejections(error)
    if code == 'GRASP_DEPTH_SEARCH_FAILED' and isinstance(attempts, (list, tuple)) and attempts:
        # Each depth owns its rejection records. The outer list is a copy.
        rejected = [item for attempt in attempts if isinstance(attempt, dict)
                    for item in _rejections(attempt.get('error') or attempt)]
        prefix = f'{len(attempts)} 档抓深均不可行'
    else:
        prefix = _SEARCH_FAILURES.get(code, fallback)
    center_fallback = details.get('center_fallback')
    if code == 'PLACEMENT_HEIGHT_SEARCH_FAILED' and isinstance(center_fallback, dict):
        prefix = '未找到可用放置位姿'
    if not rejected:
        if details.get('budget_exhausted'):
            return prefix + '：规划预算已耗尽，未证明不存在其他可行路径'
        if code in _SEARCH_FAILURES or (code == 'PLACEMENT_HEIGHT_SEARCH_FAILED' and isinstance(center_fallback, dict)):
            return prefix
        return None
    counts = Counter(_rejection_reason(item) for item in rejected)
    reasons = [f'{_REASONS[key]} {count} 次' for key, count in counts.items()]
    summary = prefix + '：' + '；'.join(reasons)
    fallback = details.get('floor_fallback')
    if isinstance(fallback, dict) and fallback.get('success') is False:
        counts = Counter(_rejection_reason(item) for item in _rejections(fallback.get('error')))
        causes = '；'.join(f'{_REASONS[key]} {count} 次' for key, count in counts.items())
        summary += '；桌面底限兜底失败' + ('：' + causes if causes else '')
    if details.get('budget_exhausted'):
        summary += '；规划预算已耗尽，未证明不存在其他可行路径'
    return summary


def describe_error(error, fallback):
    error = error if isinstance(error, dict) else {}
    message = ' '.join(str(error.get('error_message') or error.get('message') or '').split())
    details = error.get('details') if isinstance(error.get('details'), dict) else {}
    code = error.get('error_code')
    category = error_category(error)
    if category == 'VLM 交互':
        if code == 'VLM_REQUEST_TIMEOUT':
            return 'VLM 请求超时'
        if code == 'VLM_REQUEST_FAILED':
            return 'VLM 请求失败' + (f'：{message}' if message else '')
        if code == 'VLM_RESPONSE_FAILED':
            cause = _CAUSES.get(message, message)
            return 'VLM 返回失败' + (f'：{cause}' if cause else '')
        if 'vlm response' in message.lower() or message.lower().startswith('vlm '):
            cause = _CAUSES.get(message, message)
            return 'VLM 返回失败：' + (cause if len(cause) <= 220 else cause[:217] + '…')
    if code == 'PLACEMENT_PERCEPTION_FAILED':
        return '放置感知失败，无法获得有效放置点'
    if code == 'PLACEMENT_HEIGHT_SEARCH_FAILED' and details.get('relative_region', {}).get('relation') in {'above', 'below'}:
        return message or '指定上下相对放置位姿不可行'
    if code == 'PATH_NOT_FOUND':
        planner = details.get('planner')
        budget = details.get('planning_time')
        if planner == 'rrt_connect':
            limit = f'{budget:g} s 规划预算内' if _number(budget) else '规划预算内'
            cause = f'RRT-Connect 在{limit}未找到无碰撞路径'
            preceding = details.get('preceding_contact_error')
            if isinstance(preceding, dict):
                return ('直线下探失败：' + describe_error(preceding, '下探规划失败') +
                        '；关节避障失败：' + cause)
            return cause
    contacts = list(_contacts(error))
    if code in _SEARCH_FAILURES or (not contacts and not message and not code):
        summary = _search_summary(error, fallback)
        if summary:
            return summary
    # Explicit non-collision failures keep their own evidence/message even
    # when a diagnostic payload also contains incidental contact records.
    if code in {'GRASP_FAILED', 'EXECUTION_TIMEOUT', 'PLANNING_TIMEOUT',
                'IK_FAILED', 'JOINT_LIMIT', 'ROBOT_JOINT_LIMIT',
                'JOINT_LIMIT_VIOLATION', 'TRAVEL_LIMIT', 'POSE_VERIFICATION_FAILED'}:
        contacts = []
    if contacts:
        # Prefer actual penetration, otherwise report the smallest clearance.
        contacts.sort(key=lambda c: c['distance'] if _number(c.get('distance')) else float('inf'))
        pair = contacts[0]
        first = pair.get('geom1', pair.get('body1', '?'))
        second = pair.get('geom2', pair.get('body2', '?'))
        distance = pair.get('distance')
        if _number(distance) and distance < 0:
            cause = f'{first} 与 {second} 穿透 {-distance * 1000:.3f} mm'
        elif _number(distance) and distance > 0:
            cause = f'{first} 与 {second} 安全间距不足，当前 {distance * 1000:.3f} mm'
        else:
            cause = f'{first} 与 {second} 接触或碰撞'
        margin = details.get('safety_distance', details.get('safety_distance_m'))
        if _number(margin) and _number(distance) and distance >= 0:
            cause += f'，要求 {margin * 1000:.3f} mm'
        if code == 'PATH_CONSTRAINT_INFEASIBLE':
            cause += '，无法按指定路径运动'
        return cause

    joint = details.get('joint')
    if joint and code == 'PATH_CONSTRAINT_INFEASIBLE':
        step, maximum = details.get('step'), details.get('maximum_step')
        if _number(step) and _number(maximum):
            return f'{joint} 单步变化 {step:.4f} rad，超过允许 {maximum:.4f} rad'
    joints = details.get('joints') or ([details] if joint else [])
    if joints and code in {'ROBOT_JOINT_LIMIT', 'JOINT_LIMIT', 'JOINT_LIMIT_VIOLATION'}:
        item = joints[0]
        value = item.get('current', item.get('value'))
        lower, upper = item.get('lower'), item.get('upper')
        if all(_number(v) for v in (value, lower, upper)):
            return f"{item.get('joint', '?')} 当前 {value:.4f} rad，允许范围 [{lower:.4f}, {upper:.4f}]"
    if code == 'TRAVEL_LIMIT' and all(_number(details.get(k)) for k in ('requested', 'limit')):
        unit = details.get('unit', '')
        return f"请求行程 {details['requested']:.4f} {unit}，超过允许 {details['limit']:.4f} {unit}"
    if code == 'GRASP_FAILED':
        if error.get('failed_stage') == 'post_command_verification' and message:
            return message
        evidence = error.get('evidence') or details.get('evidence') or {}
        evidence = evidence if isinstance(evidence, dict) else {}
        if _number(evidence.get('relative_position_error_m')) and evidence['relative_position_error_m'] > .005:
            return f"持物相对夹爪滑移 {evidence['relative_position_error_m'] * 1000:.2f} mm"
        if _number(error.get('contact_loss_duration_s')):
            return f"持物接触持续丢失 {error['contact_loss_duration_s']:.3f} s"
    if message:
        cause = _CAUSES.get(message, message)
        # Preserve a useful original cause for unknown errors, without dumping a report.
        return cause if len(cause) <= 240 else cause[:237] + '…'
    return fallback + '；上游未提供具体失败原因'
