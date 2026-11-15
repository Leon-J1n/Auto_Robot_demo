# Planning 模块

## 1. 职责与边界

`planning` 将运动策略生成的路径请求转换为经过关节限位、碰撞检查和路径质量处理的关节轨迹。它负责直接路径、RRT 避障、路径简化、轨迹优化和时间参数化；不负责任务解析、IK 服务实现、场景资产加载、仿真推进或实际执行。

边界规则：

1. 任务命令由 `atomic_skill` 和 `atomic_action` 解析为规范化 Move 请求。
2. 碰撞查询统一通过 `collision` 注入，规划器不直接依赖具体仿真平台。
3. CuRobo 是本模块中的自由空间规划器，适配器位于 `planning/planners/curobo/`；官方运行时和 GPU worker 位于 `libraries/curobo/`。
4. 规划输出交给 `execution` 执行；执行阶段不重新规划，也不由本模块推进物理仿真。
5. 规划配置从请求、场景默认值和根目录 `config/global.json` 合并得到，不在策略代码中重复定义。

## 2. 目录结构与职责

```text
planning/
├── common/                    # 规划错误和共享协议
│   └── errors.py
├── constraints/               # 限位、采样和姿态约束
│   ├── arm_posture.py
│   ├── collision_sampling.py
│   └── motion_limits.py
├── planners/                  # 具体路径规划算法
│   ├── direct_planner.py      # 直接路径验证与路点加密
│   ├── collision_free_planner.py # RRT-Connect、shortcut 和复核
│   ├── interaction_planners.py   # 接触/交互路径辅助规划
│   ├── trajopt_optimizer.py   # 已有可行路径的局部优化
│   └── curobo/                # CuRobo 规划器适配和规划协议
├── trajectory/                # 路径、预规划结果和时间参数化
│   ├── path.py
│   ├── prepared.py
│   └── time_parameterization.py
├── __init__.py                # 对外导出
└── requirements.txt            # 本模块的可选依赖
```

官方 CuRobo 源码、场景转换和 GPU worker 独立放在 `libraries/curobo/`，规划器适配器保留在 `planning/planners/curobo/`。

## 3. 数据流

```text
Task command / skill request
        ↓
atomic_action.motion.common 规范化请求、IK 候选和 path_request
        ├─ joint + auto → CuRobo 优先
        │                  ├─ 成功 → CuRobo 轨迹
        │                  └─ 失败 → DirectPlanner → RRT fallback
        └─ linear/circular/contact → DirectPlanner / interaction planner
                           └─ 允许回退时 → CollisionFreePlanner
        ↓
碰撞复核、路径优化、时间参数化
        ↓
execution.execute_trajectory()
```

规划过程只使用当前同步的机器人、环境和夹持状态。每个规划结果都包含规划器名称、路点、耗时和诊断信息；失败不会进入执行阶段。

## 4. 对外接口

调用方应通过根包导入稳定接口：

```python
from planning import DirectPlanner, CollisionFreePlanner
from planning.planners.curobo import CuroboPlanner

direct = DirectPlanner(config, collision_checker=checker, joint_limits=limits)
rrt = CollisionFreePlanner(
    config,
    collision_checker=checker,
    joint_limits=limits,
    kinematics=kinematics,
)
trajectory = direct.plan(path_request, move_request, context)
```

| 接口 | 用途 |
| --- | --- |
| `DirectPlanner.plan(path_request, move_request, context)` | 验证或整理直接关节/笛卡尔路径，检查限位和碰撞，不搜索绕行路径 |
| `CollisionFreePlanner.plan(path_request, move_request, context)` | 使用 RRT-Connect 搜索避障路径，并执行 shortcut、TrajOpt 和最终复核 |
| `CuroboPlanner.plan(request)` | 使用官方 CuRobo 规划自由空间 joint 轨迹；不支持接触、严格直线/圆弧和全程姿态约束 |
| `TrajOptOptimizer.optimize(path, safety_distance=...)` | 固定起终点优化已有可行路径；失败时保留输入路径 |
| `prepare_continuous_trajectory(...)` | 对已验证路点建立连续路径并进行时间参数化 |
| `PlanningError` / `CollisionPlanningError` | 传递错误代码、可恢复状态和诊断细节 |

规划器接收规范化 Move 请求，不直接接收简化的 `commands.json`。主要字段如下：

| 字段 | 说明 |
| --- | --- |
| `target` | pose、joint 或 named state 目标 |
| `motion.path_type` | `joint`、`linear` 或 `circular` |
| `motion.path_constraint` | `soft` 允许回退，`hard` 必须保持几何路径 |
| `planning.mode` | `direct`、`collision_free` 或 `auto` |
| `planning.planning_time` | 规划预算参考值；不是所有后处理阶段的硬中断时间 |
| `constraints.avoid_collision` | 是否进行碰撞约束检查 |
| `constraints.safety_distance` | 碰撞安全距离，单位 m |
| `strategy_params` | IK、笛卡尔步长和关节步长等策略参数 |

## 5. 配置、输入与输出

规划器选择规则：

1. `joint` 路径且 `planner_type` 为 `auto` 或未填写时，先尝试 CuRobo。
2. CuRobo 失败时，自动回退到当前 Direct/RRT 流程；显式 `planner_type: curobo` 不自动回退。
3. `linear`、`circular` 和接触路径由 Direct 或交互规划器处理；直接路径碰撞且允许回退时使用 RRT。
4. `planning.mode: direct` 禁止 RRT 回退；`planning.mode: collision_free` 直接进入避障规划。

Direct/RRT 的典型流程为：

```text
DirectPlanner
    ├─ 路径可行 → 路点复核与时间参数化
    └─ 碰撞且允许回退 → RRT-Connect
                         → shortcut
                         → TrajOpt
                         → 最终碰撞复核
```

默认碰撞采样和 RRT 采样从根目录 `config/global.json` 读取：

- `planning.collision_validation.resolution_rad`：最终碰撞复核采样分辨率；
- `planning.collision_free.rrt_resolution_rad`：RRT 扩展采样分辨率；
- shortcut、后处理和姿态偏好位于 `planning` 配置段。

规划输出至少包含：

```text
planner
path_type
waypoints
duration
planning_time
diagnostics
```

Demo 还会在每个请求的 `output/commands/<序号>_<command_id>/planning_timing.json` 中写出各阶段耗时、规划器、候选构型、碰撞检查和路径质量信息。

## 6. 验证与限制

规划器验证包括：

- 关节维度、有限值和 URDF 限位；
- 起点、终点和整条路径的碰撞检查；
- 软/硬路径约束和连续 IK 连续性；
- shortcut、TrajOpt 和时间参数化后的最终轨迹复核。

当前限制：

1. 离散碰撞采样可能漏掉采样点之间的短暂碰撞，需要更高可靠性时应缩小采样间隔或使用碰撞后端的连续检查能力。
2. Direct 和 RRT 不自行求 IK；IK 候选由运动策略和 Kinematics 提供。
3. CuRobo 当前主要支持 joint 规划；持物接触、允许碰撞对、严格直线/圆弧和全程姿态约束仍由原有规划器处理。
4. 规划结果的 `duration` 是轨迹估计值，执行时的实际耗时由 `execution` 后端决定。

规划失败只返回结构化错误，不会推进仿真或执行机器人运动。

