# Kinematics 模块接口说明

## 模块职责

Kinematics 模块负责机器人关节链的运动学计算和逆运动学求解：

- 从 URDF 构造串联关节链；
- 计算正向运动学（FK）；
- 计算几何 Jacobian；
- 根据末端目标位姿求解关节角（IK）；
- 检查关节限位、候选解连续性和 FK 复核误差；
- 按配置选择 IK 后端并执行回退。

当前 `auto` 后端顺序为：

```text
EAIK → Jacobian
```

IKFast 暂不参与 `auto`。

## 主要入口

URDF 加载入口：

```python
from libraries.eaik import load_urdf

kin = load_urdf(
    "assets/models/robots/30/ur5e_arm.urdf",
    base_link="base",
    tip_link="attachment_site",
)
```

`load_urdf()` 返回统一的 `KinematicsService`。常用参数如下：

| 参数 | 说明 |
|---|---|
| `path` | URDF 文件路径 |
| `base_link` | 运动学基座 link |
| `tip_link` | 末端 link |
| `tcp_transform` | tip link 到 TCP 的 4×4 变换 |
| `world_from_base` | 基座到世界坐标系的 4×4 变换 |
| `current_joints_provider` | 获取当前关节角的回调 |
| `base_transform_provider` | 获取当前基座位姿的回调 |

关节角使用弧度，位置使用米。关节顺序由 URDF 中从 base 到 tip 的活动关节顺序决定。

## FK 和 Jacobian

```python
q = [0.0, -1.0, 1.2, -0.8, 0.5, 0.0]

pose = kin.forward(q)
J = kin.jacobian(q)
```

`forward(q)` 返回：

```python
{
    "position": np.ndarray,          # shape=(3,)
    "rotation_matrix": np.ndarray,   # shape=(3, 3)
}
```

`jacobian(q)` 返回形状为 `(6, 6)` 的世界坐标系 Jacobian，前 3 行是线速度部分，后 3 行是角速度部分。

## IK

```python
target = {
    "frame": "world",
    "position": [0.35, 0.20, 0.80],
    "orientation": {
        "representation": "rpy",
        "roll": 180.0,
        "pitch": 0.0,
        "yaw": 90.0,
    },
}

result = kin.solve_one(
    target,
    seed=q,
    backend="auto",
    policy="nearest",
    timeout=1.0,
)
q_goal = result.solution
```

多解求解：

```python
result = kin.solve_all(target, seed=q, max_solutions=8)
solutions = result.solutions
```

常用参数：

- `seed`：当前或参考关节构型；
- `backend`：指定单个后端，例如 `"eaik"` 或 `"jacobian"`；
- `policy`：解的选择策略，例如 `"nearest"`、`"minimum_travel"`、`"continuous"`；
- `max_joint_step`：连续运动时限制单关节最大变化；
- `timeout`：本次 IK 的时间预算。

`KinematicsService` 会对后端返回的解进行限位检查、FK 误差复核、周期角归一化和重复解过滤。

## 运行时绑定

`SceneRobotRuntime.kinematics` 默认使用项目 URDF 创建运动学服务。FK/Jacobian 的几何计算独立于 MuJoCo；运行时只通过回调提供当前关节角和基座状态。
