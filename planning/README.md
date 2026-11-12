# 运动规划职责边界与调用接口

更新日期：2026-10-09。本文对应规划实现迁入根目录、旧兼容目录移除后的代码。

## 职责边界

`planning` 接收运动策略生成的路径请求，输出经过限位和碰撞检查的关节路点及诊断信息。它负责直接路径整理、避障搜索、路径简化和平滑；实际运动由运行时执行。

| 部分 | 负责内容 | 边界 |
| --- | --- | --- |
| `DirectPlanner` | 验证策略关节路径、加密路点、检查碰撞 | 不自行求 IK，不搜索绕障路径 |
| `CollisionFreePlanner` | 普通关节空间 RRT-Connect，或保持姿态的位置空间 RRT；路径重建、简化和复核 | 依赖注入的限位、碰撞检测器；保持姿态还需运动学服务 |
| `TrajOptOptimizer` | 固定起终点，优化已有可行关节路径 | 优化失败保留输入路径 |
| `CollisionFreePlanner` | 当前执行层使用的避障规划器 | 采用项目内 RRT-Connect、路径简化与平滑 |
| `PlanningError`、`CollisionPlanningError` | 传递失败代码、可恢复标记及细节 | 上层决定如何报告或改用其他方案 |
| `prepared_trajectory.py` | 预规划结果及场景状态签名 | _MoveExecutor 负责复用条件和最终验证 |

与相邻模块的边界：

- Move 策略解析目标、求 IK 并生成 `path_request`；[kinematics](C:/Users/21474/Desktop/robot-agent/kinematics/README.md) 提供 FK/Jacobian/IK 和候选校验。
- _MoveExecutor 决定直接规划到避障规划的回退，以及多构型路径之间的行程比较。
- 碰撞检测器负责机器人、障碍物、夹持物体与允许接触规则。planner 算法自身不直接调用 MuJoCo。
- 运行时负责实时状态、轨迹执行、控制器和仿真。`duration` 是规划层估计值，不是完整时间参数化。

## 文件与公开接口

所有接口可从 `planning` 导入。旧 `atomic_action/motion/common/planners/` 和未使用的 `libraries/motion_planning/` 已移除。

| 文件 | 构造接口 | 调用接口 |
| --- | --- | --- |
| `direct_planner.py` | `DirectPlanner(config=None, *, collision_checker=None, joint_limits=None)` | `plan(path_request, move_request, context)` |
| `collision_free_planner.py` | `CollisionFreePlanner(config=None, *, collision_checker=None, joint_limits=None, kinematics=None)` | `plan(path_request, move_request, context)` |
| `collision_free_planner.py` | `CollisionFreePlanner` | `plan(...)` 接口 |
| `trajopt_optimizer.py` | `TrajOptOptimizer(*, collision_checker, joint_limits, config=None)` | `optimize(path, *, safety_distance=0.0)` → `(ndarray, diagnostics)` |
| `errors.py` | `PlanningError(message, *, code="PLANNING_FAILED", recoverable=False, details=None)`；`CollisionPlanningError(message, *, details=None)` | 异常成员：`code`、`recoverable`、`details` |

实际规划需要正确的限位；避障需要可用碰撞检测器；保持姿态避障还需要运动学服务。当前 DirectPlanner 明确验证六轴关节路点。

## 输入约定

`plan()` 接收技能层规范化的请求，不直接接收简化 `commands.json`。

| 输入 | 主要字段 |
| --- | --- |
| `path_request` | `path_type`；直接规划使用 `joint_waypoints`，避障使用 `start`、`goal` |
| 保持姿态请求 | `orientation_constraint={"enabled": True, "orientation": ...}` |
| `move_request.motion` | `path_constraint`：hard/soft；hard 的非 joint 路径不能放宽 |
| `move_request.planning` | `planning_time`、`max_attempts`；mode/allow_replan 由 _MoveExecutor 调度 |
| `move_request.constraints` | `avoid_collision`、`safety_distance`（米）、`velocity_scale` |
| `move_request.strategy_params` | `ik_method`、`cartesian_step`（米）、`max_joint_step`（弧度，默认 0.4） |
| `context` | 保持姿态分支读取 `current_pose.position` |

关节路点/限位单位为弧度，末端位置和安全距离为米。`candidate_goals` 由 _MoveExecutor 展开成多个单目标请求，比较累计关节行程后选取结果。

## 手臂抬高偏好

`config/arm_posture.json` 当前设为 `enabled=false`，规划按关节行程选择构型，不再考虑手臂高度，RRT 也不再偏向抬高手臂的采样。关节限位和规划碰撞规则继续生效。

以下为重新启用时的可选软偏好：综合关节行程与上臂/肘部/前臂高度选择构型。参考高度取肩关节与已识别静态桌面顶面中的较高者；肘部期望高于参考面 25 cm，长臂段采样点扣除 5 cm 半径后期望高于参考面 15 cm。这些是评分目标，不是必须达到的高度限制。手腕末端与夹爪不参与抬高评分，可以接近桌面。

`weight_rad_per_m` 控制高度不足的惩罚（默认 12），增大后更偏向抬高，减小后更偏向缩短运动；`enabled=false` 恢复原有行程优先规则。关节解/路径比较、接触预接近分支和 RRT 采样共用该偏好。RRT 保留普通随机探索；简化和平滑若使综合代价变差，会保留此前的路径。

指定的关节目标不会被更换，直线接触路径保留末端路径及关节连续性约束，不为抬肘强制翻转构型。评分采用隔离的运动学工作区，只调用 `mj_kinematics`，不推进物理仿真，不增加执行碰撞检查，不修改资产文件。轨迹报告 `diagnostics.arm_posture` 记录起终点肘高、长臂段近似最低高度与整条路径的偏好惩罚；它们是规划评分指标，精确碰撞可行性仍由原规划检查器判定。

## 输出与异常

`plan()` 返回轨迹字典：`planner`、`path_type`、`waypoints`、`duration`、`planning_time` 和 `diagnostics`。避障结果还包含 `original_path_type`；DirectPlanner 对 linear 路径保留 `cartesian_joint_waypoints`。

直接路径碰撞抛出 `CollisionPlanningError`。避障可能抛出 `PlanningError`，例如 `PATH_CONSTRAINT_INFEASIBLE`、`COLLISION_DETECTED`、`PATH_NOT_FOUND`、`TRAJECTORY_INVALID`。异常不会触发实际运动。

## 当前调用流程

```text
_MoveExecutor → 策略生成 path_request → DirectPlanner.plan()
    ├─ 成功 → 运行时执行
    └─ 碰撞且允许回退 → CollisionFreePlanner.plan()
        ├─ 普通：关节空间 RRT-Connect → Shortcut → TrajOpt 优化 → 最终复核
        └─ 保持姿态：位置空间 RRT + 连续 IK → 简化 → 平滑
```

普通调用先试直接路径。回退条件为 mode=`collision_free`，或 mode=`auto` 且 `allow_replan=True`；hard 的非 joint 路径禁止放宽。普通分支不保持沿途末端姿态。

保持姿态分支每条位置边通过连续 IK 生成关节路点，并限制相邻单关节变化。简化要求重新接回原构型；平滑调整中间末端位置，再求固定姿态 IK。只有平滑目标改善、路径长度和累计关节行程不增加、跳变合规且无碰撞时才接受更新。失败保留已验证路径。

普通优化目标为关节一阶差分平方和，加权二阶差分平方和。优化或最终复核失败时退回已验证的 RRT 路径。搜索和后处理有独立时间预算，`planning_time` 不是整个 `plan()` 的严格耗时上限。

普通关节路径简化通过规划器配置 `planning.collision_free.shortcut_time` 控制时间预算，默认 0.5 秒（设为 0 跳过简化）。已开始的碰撞检查会完整结束，因此这是软上限。每次简化仅在本次调用内缓存方向、端点完全一致的线段检查结果，要求同步规划期间场景和检查策略固定；缓存不会跨执行或规划调用复用。目标选解、安全距离、碰撞采样精度和最终路径复核保持不变。`trajectory.diagnostics.shortcut` 记录预算、实际耗时、检查次数、缓存命中和接受捷径次数。

若碰撞检测器提供 `planning_cache()`，普通关节分支在 RRT、Shortcut 和 TrajOpt 期间开启单次规划状态缓存。demo 检测器按精确关节值、安全距离和允许接触集合区分结果；实时关节/物体位置、控制状态、抓取模式、绑定关系、支撑接触或场景变换更新时失效。该作用域要求同步规划期间不并发修改模型或碰撞策略。退出作用域（包括异常退出）即释放缓存，规划器最终验证、执行前验证及执行帧检查仍重新计算。诊断字段 `collision_state_cache` 记录命中、实际查询和失效次数。

## 依赖注入与调用示例

```python
from planning import CollisionFreePlanner, DirectPlanner, CollisionPlanningError

direct = DirectPlanner(config, collision_checker=checker, joint_limits=limits)
bypass = CollisionFreePlanner(
    config, collision_checker=checker, joint_limits=limits, kinematics=kin,
)
try:
    trajectory = direct.plan(path_request, move_request, context)
except CollisionPlanningError:
    trajectory = bypass.plan(path_request, move_request, context)
```

碰撞检测器需提供 `check(q, *, safety_distance=...)`、`check_path(waypoints, *, resolution=..., safety_distance=..., allowed_geom_ids=...)`；返回对象至少有 `in_collision`、`contacts`。保持姿态服务需提供 `forward(q)`、`solve_one(...)`，结果通过 `.solution` 读取。

当前默认碰撞检查按关节插值采样，`resolution` 表示单关节采样增量（弧度）。普通 RRT 扩展默认 0.02；直接规划、普通避障规划最终复核及平滑轨迹验证默认使用 `planning.collision_validation.resolution_rad=0.02`。平滑轨迹每区间至少二等分，按局部导数上界增加采样，保留全部样条节点。终点和碰撞裕量不变；采样放宽可能漏掉采样点间短暂碰撞，详细配置见 [碰撞说明](../collision/README.md)。

## 扩展边界

目前采用普通类和依赖注入，没有动态规划器插件注册表。保留 `plan(...)` / `optimize(...)` 边界即可；未来接入其他规划算法时增加适配器。以下划线方法属于内部实现，不是稳定公共接口：`_rrt_connect`、`_shortcut`、`_plan_fixed_orientation`、`_fixed_orientation_edge`、`_shortcut_fixed_orientation`、`_smooth_fixed_orientation`。

相关接口：[_MoveExecutor](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/skill.py)、[Skill 接口](C:/Users/21474/Desktop/robot-agent/SKILL_INTERFACES.md)、[MuJoCo 依赖清单](C:/Users/21474/Desktop/robot-agent/MUJOCO_DEPENDENCIES.md)。

## 时间参数化

当前 demo 统一使用 `planning.time_parameterization.prepare_continuous_trajectory()`：先对已经通过 RRT、shortcut 和 TrajOpt 检查的关节路点建立自然三次 C2 路径，再为整段运动分配连续时间表，只在阶段起止处停稳。时间估算直接计算复合轨迹的速度、加速度和 jerk；配置了 `max_jerk` 时会满足 jerk 上限。三次路径在狭窄空间内若超出 FCL 安全走廊，会自动回退到同一路点的 PCHIP，并重新做 FCL 复检。每关节速度基准由 URDF limit.velocity 读取，加速度优先读取 limit.acceleration；缺失时按约定采用速度上限数值作为加速度基准，并在报告中标记回退来源。两者分别乘 `velocity_scale` 和 `acceleration_scale`，所有关节使用同一全局缩放规则。执行采样点位于同一条稠密几何验证路径上，因此只进行一次 FCL 路径检查，避免重复遍历。推拉共用同一连续轨迹实现和全局限值；夹爪开合独立控制。Ruckig 接口仍保留为显式调用的本地后端，不会在默认 demo 执行链中把每个路点停住。

Ruckig 接口保留在 `planning/time_parameterization.py`，仅显式调用 `parameterize_trajectory()` 时使用。重新启用时，轨迹可包含：

- `geometric_waypoints`：简化/平滑后的原始关节路点；
- `waypoints`：最终确认的几何关节路点；
- `time_parameterized_waypoints`：启用 Ruckig 后按时间轴采样的执行关节位置；
- `timestamps`：每个位置的累计时间；
- `velocities`、`accelerations`：对应的关节速度和加速度；
- `duration`：时间参数化后的轨迹时长。

公开接口为：

```python
from planning import parameterize_trajectory
from libraries.ruckig import RuckigTimeParameterizer

parameterizer = RuckigTimeParameterizer(
    control_cycle=0.01,
    max_velocity=[1.0] * 6,
    max_acceleration=[2.0] * 6,
    max_jerk=[10.0] * 6,
)
timed = parameterize_trajectory(trajectory, move_request, parameterizer)
```

未提供每关节限制时，适配器默认使用 `1 rad/s`、`2 rad/s²` 和 `10 rad/s³`。重新启用时，`velocity_scale` 与 `acceleration_scale` 会缩放对应上限。每个相邻路点段独立计算，段首和段末速度、加速度为零。

旧入口的 `SceneRobotRuntime` 不读取时间参数化字段；`kinematic` 模式继续按原几何路点和 `playback_speed` 回放，`actuator` 模式继续按几何路点跟踪。新入口的 `MuJoCoExecutionBackend` 读取 `time_parameterized_waypoints` 和 `timestamps`。
