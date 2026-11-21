# Planning 模块

## 1. 职责与边界

`planning` 将运动策略生成的路径请求转换为经过关节限位和碰撞检查的关节轨迹。当前主流程先验证直接路径；直接路径碰撞且允许改道时，由 RRT-Connect 搜索自由空间轨迹，再由宿主碰撞检查器复核。不负责任务解析、IK 服务实现、场景资产加载、仿真推进或实际执行。

边界规则：

1. 任务命令由 `atomic_skill` 和 `atomic_action` 解析为规范化 Move 请求。
2. 直接路径、RRT 扩展和最终复核通过注入的碰撞检查器执行。
3. cuRobo 作为显式规划后端保留；其适配器位于 `planning/planners/curobo/`，官方运行时和 GPU worker 位于 `libraries/curobo/`。
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
│   ├── collision_free_planner.py # RRT-Connect 避障及 TrajOpt 优化
│   ├── interaction_planners.py   # 接触/交互路径辅助规划
│   ├── trajopt_optimizer.py   # 已有可行路径的局部优化
│   └── curobo/                # cuRobo 规划器及避障适配
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
        └─ joint/linear/circular → DirectPlanner / interaction planner
                                 ├─ 直接路径可行 → 直接轨迹
                                 └─ 碰撞且允许改道 → CollisionFreePlanner → RRT-Connect
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

direct = DirectPlanner(config, collision_checker=checker, joint_limits=limits)
avoidance = CollisionFreePlanner(
    config,
    collision_checker=checker,
    joint_limits=limits,
)
trajectory = direct.plan(path_request, move_request, context)
```

| 接口 | 用途 |
| --- | --- |
| `DirectPlanner.plan(path_request, move_request, context)` | 验证或整理直接关节/笛卡尔路径，检查限位和碰撞，不搜索绕行路径 |
| `CollisionFreePlanner.plan(path_request, move_request, context)` | 使用 RRT-Connect 搜索自由空间避障轨迹，优化后再用宿主碰撞检查器复核 |
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

1. `planner_type: auto` 或未填写时，先验证直接路径；碰撞且 `allow_replan` 允许时使用 RRT-Connect 避障。
2. 显式 `planner_type: curobo` 在 demo 入口直接调用 cuRobo；失败时不回退 RRT。`rrt` 使用 RRT-Connect 避障。
3. 硬性几何路径和全程固定姿态在碰撞后不能由 cuRobo 放宽。实时 articulated 检查器已支持持物和阶段碰撞规则；普通 JSON 入口仍有相应限制。
4. `planning.mode: direct` 禁止避障回退；`planning.mode: collision_free` 在直接路径碰撞后允许使用 RRT-Connect。

典型流程为：

```text
DirectPlanner
    ├─ 路径可行 → 路点复核与时间参数化
    └─ 碰撞且允许回退 → RRT-Connect + TrajOpt
                         → 宿主最终碰撞复核（启用简化代理时使用同一代理，否则原检查器）
```

默认碰撞采样从根目录 `config/global.json` 读取：

- `planning.collision_validation.resolution_rad`：最终碰撞复核采样分辨率；
- `planning.collision_free.rrt_resolution_rad`：RRT 扩展边的碰撞采样分辨率。

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

RRT 启用代理碰撞检查时，一次同步规划共享状态签名，进入和退出各校验一次；
规划中发生场景或规则变化则拒绝结果。耗时因素、参数默认值和真实配置入口见
[`docs/rrt_performance.md`](../docs/rrt_performance.md)。

所有规划器和最终复核均使用场景与机器人资产解析出的原始精细碰撞几何，
关节数量与顺序取自选定机械臂的 `RobotDriver.joint_sequence`，cuRobo 运行时模型也从当前 MuJoCo 几何生成。详见 `collision/README.md`。
极短 Direct 动作的状态校验可能比原热检查更耗时，
不能将长路径或避障回放的加速比例当作每个技能的收益。

## 6. 验证与限制

规划器验证包括：

- 关节维度、有限值和 URDF 限位；
- 起点、终点和整条路径的碰撞检查；
- 软/硬路径约束和连续 IK 连续性；
- cuRobo 输出和时间参数化后的最终轨迹复核。

当前限制：

1. 离散碰撞采样可能漏掉采样点之间的短暂碰撞，需要更高可靠性时应缩小采样间隔或使用碰撞后端的连续检查能力。
2. Direct 不自行求 IK；IK 候选由运动策略和 Kinematics 提供。
3. cuRobo 避障适配使用关节路径；配有实时 articulated 检查器时，已接入持物、允许碰撞对和阶段距离规则。球近似与宿主几何可能有差异，最终仍以宿主复核为准。全程姿态约束尚未接入，硬性几何路径在碰撞时返回失败。
4. 规划结果的 `duration` 是轨迹估计值，执行时的实际耗时由 `execution` 后端决定。

规划失败只返回结构化错误，不会推进仿真或执行机器人运动。



## 抓取接触预检查的 IK 搜索顺序

`PickContactPlanner` 对每个抓深候选执行两轮接近距离搜索。默认距离依次为 100、70、40、30、20 mm；第一轮仅使用当前几何适用的 EAIK/SSIK，检查预接近点 IK、构型碰撞、整段直线接触路径的 IK 连续性与碰撞。第一轮所有距离均未通过后，第二轮按同样距离顺序恢复完整自动后端链，允许 Jacobian 回退；路径不同采样点仍可使用不同后端。没有解析后端的机器人直接进入允许数值求解的轮次。显式 `strategy_params.ik_method` 保持指定的求解策略。失败记录与成功证据的 `ik_search_pass` 标明 `analytic` / `numerical_fallback`（显式或旧接口为 `configured`）。

这项顺序优化作用于接触预检查；最终抓取目标的解仍由统一服务校验，实际 `ApproachSkill` 的运动距离搜索另行进行。实际接近默认从 80 mm 到 20 mm，每次缩短 10 mm，使用有界的解析可达性提示把暂未找到解析解的点后置，不据此删除候选。有腕部观测时优先尝试相机对准目标的预接近点，失败且未发生运动才改试普通夹爪预接近点；所有接近点失败后复用已解析目标做 move_to 回退。整次接近共用运动请求的 timeout（本场景为 60 秒），执行或运动后的失败不继续尝试。预检查 100/70/40/30/20 mm 与实际运动 80/70/.../20 mm 是不同搜索，不应混为一组。


默认视觉抓取深度为 30、20、10、0 mm（从深到浅）。普通放置高度偏移为 10、25、40、55、70、85、100 mm。相对放置使用相同的 10～100 mm 区间与 15 mm 步长；放置仍使用离散二分搜索最低可行档。
