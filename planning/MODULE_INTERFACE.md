# Planning 模块接口说明

## 模块职责

Planning 模块负责把起点和目标构型转换为可执行的关节轨迹：

- 生成直接关节路径；
- 调用 Collision 模块验证路径安全性；
- 在直接路径碰撞时使用 RRT-Connect 搜索绕行路径；
- 对路径进行 shortcut 和可选 TrajOpt 优化；
- 对轨迹进行时间参数化和执行前验证；
- 输出 MuJoCo 或其他执行器可以消费的关节轨迹。

Planning 不负责解析 URDF、求解 IK 或执行物理仿真。IK 由 Kinematics 模块完成，碰撞查询由 Collision 模块完成。

## 主要类

```python
from planning import DirectPlanner, CollisionFreePlanner
```

| 类 | 用途 |
|---|---|
| `DirectPlanner` | 验证并输出策略生成的直接路径 |
| `CollisionFreePlanner` | 使用碰撞检测搜索安全路径，并可进行优化 |
| `TrajOptOptimizer` | 对已有路径进行可选轨迹优化 |

## 输入接口

两个规划器都使用统一的 `plan()` 接口：

```python
trajectory = planner.plan(
    path_request,
    move_request,
    context,
)
```

### `path_request`

基本字段如下：

```python
path_request = {
    "path_type": "joint",
    "start": q_start,
    "goal": q_goal,
    "joint_waypoints": np.vstack((q_start, q_goal)),
}
```

根据路径策略，`path_type` 可以是：

```text
joint
linear
circular
```

线性路径还可以包含 Cartesian 插值后的关节点、姿态约束和诊断信息。

### `move_request`

规划器会读取以下配置：

```python
move_request["constraints"]["avoid_collision"]
move_request["constraints"]["safety_distance"]
move_request["constraints"]["velocity_scale"]
move_request["motion"]["path_constraint"]
move_request["planning"]["planning_time"]
move_request["planning"]["max_attempts"]
```

`move_request` 通常由 Move Skill 根据动作配置和 schema 生成，不建议手工绕过校验层构造复杂请求。

### `context`

运行时上下文通常来自：

```python
context = runtime.get_context(move_request)
```

其中包含当前关节状态、末端位姿、机器人模型和运行时引用等信息。

## 直接规划

```python
planner = DirectPlanner(
    config=config,
    collision_checker=checker,
    joint_limits=runtime.joint_limits,
)

trajectory = planner.plan(path_request, move_request, context)
```

处理过程：

1. 检查输入路径形状和有限性；
2. 检查关节限位；
3. 以最大单关节步长 `0.04 rad` 加密路径；
4. 如果启用避障，调用 `checker.check_path()`；
5. 计算轨迹持续时间并返回轨迹字典。

如果路径发生碰撞，会抛出 `CollisionPlanningError`。

## 碰撞规避规划

```python
planner = CollisionFreePlanner(
    config=config,
    collision_checker=checker,
    joint_limits=runtime.joint_limits,
    kinematics=kin,
)

trajectory = planner.plan(path_request, move_request, context)
```

主要流程：

1. 检查起点和终点是否碰撞；
2. 使用 RRT-Connect 搜索关节空间路径；
3. 尝试 shortcut 简化路径；
4. 根据配置使用 TrajOpt 优化；
5. 对最终轨迹再次调用 `check_path()`；
6. 优化结果不安全时回退到未优化的安全路径。

如果是硬 Cartesian 路径约束，且无法保持该约束，规划器会返回相应的 `PlanningError`，不会静默改变路径要求。

## 轨迹输出

规划成功后返回字典：

```python
{
    "planner": "direct" | "collision_free",
    "path_type": "joint" | "linear" | "circular",
    "waypoints": [[q0...], [q1...], ...],
    "duration": 1.25,
    "planning_time": 0.08,
    "diagnostics": {},
}
```

其中：

- `waypoints` 的形状为 `(N, 6)`，关节单位为 rad；
- `duration` 是按速度缩放估算的轨迹时长；
- `planning_time` 是规划耗时；
- `diagnostics` 保存算法、回退、优化和路径放宽信息。

## 时间参数化

当前执行链暂不启用 Ruckig，使用：

```python
from planning import prepare_continuous_trajectory

timed = prepare_continuous_trajectory(
    trajectory,
    move_request,
    collision_checker=checker,
)
```

结果保留原始几何路径，增加平滑路径的 `time_parameterized_waypoints`、`timestamps` 和 `duration`，诊断后端为 `continuous_precomputed`。中间路点连续通过，整段起止停稳；默认使用自然三次 C2 路径，若该路径未通过 FCL 安全复检则回退到同一路点的 PCHIP，并重新验证稠密几何路径。执行采样点位于同一验证路径上，因此只执行一次 FCL 路径遍历。速度和加速度分别乘全局比例，所有关节使用统一缩放规则。加速度属性缺失时采用速度上限数值作为加速度基准，并记录来源；连续计时入口缺少已解析基准时直接报错。配置 `max_jerk` 时直接按复合轨迹 jerk 上限估算持续时间。旧 `prepare_interpolated_trajectory` 接口仅为兼容保留。

`parameterize_trajectory(trajectory, move_request, parameterizer=None, collision_checker=checker)` 的 Ruckig 接口仍保留，当前测试执行链不调用它。

## 预计算轨迹

`planning.prepared_trajectory` 提供轨迹缓存的数据结构：

```python
from planning.prepared_trajectory import PreparedTrajectory, planning_state_signature
```

缓存必须绑定请求、规划选择和场景状态签名。机器人状态、场景几何或碰撞策略改变后，旧轨迹不应继续复用。

## 与其他模块的边界

Planning 通过两个接口连接上下游：

```text
Kinematics：提供目标构型和 Cartesian 路径中的 IK/Jacobian
Collision：提供 check(q) 和 check_path(waypoints)
```

Planning 不直接读取 FCL 内部实体，也不直接调用 MuJoCo。规划完成后，输出交给运行时执行器：

```python
execution = runtime.execute_trajectory(trajectory)
```

因此完整链路是：

```text
目标位姿
  ↓
Kinematics / IK
  ↓
path_request
  ↓
Planning
  ↓
Collision.check / check_path
  ↓
trajectory["waypoints"]
  ↓
execute_trajectory()
```
