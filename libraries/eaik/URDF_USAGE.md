# EAIK URDF 调用入口

入口位于 `libraries/eaik`，不需要改动 `kinematics`。使用当前项目 `.venv`，其中已安装 EAIK 1.2.2 和 Urchin。

## Python 调用

```python
import numpy as np
from libraries.eaik import load_urdf

kin = load_urdf(
    r"C:\robot_models\my_robot.urdf",
    base_link="base_link",
    tip_link="tool0",
)

print(kin.robot.joint_names)  # 所有向量均使用这里列出的基座到末端顺序
q = np.zeros(len(kin.robot.joint_names))
pose = kin.forward(q)
jac = kin.jacobian(q)

target = {
    "frame": "world",
    "position": {"x": 0.3, "y": 0.2, "z": 0.5},
    "orientation": {
        "representation": "rpy", "roll": 180, "pitch": -90, "yaw": 0,
    },
}
result = kin.solve_one(target, seed=q)  # 此服务的 auto 默认就是 EAIK
print(result.solution)
print(result.metadata())
candidates = kin.solve_all(target, seed=q, max_solutions=8).solutions
```

上述机器人路径和 link 名是占位示例，目标是否可达取决于实际机器人。位置单位 m、关节单位 rad；目标的 RPY 延续项目约定，单位 degree。

入口在加载时验证关节链、EAIK 的结构分解和初始构型的 FK/IK。无法解析的结构或不完整的输入会直接报错。`load_urdf()` 返回现有 `KinematicsService`，求解结果仍为 `IKResult`。

完整接口：

```python
load_urdf(
    path,
    *,
    tip_link,                   # 必填：末端 link 名
    base_link=None,            # 默认 URDF 根 link
    tcp_transform=None,        # 4×4 T_tip_TCP；默认单位矩阵
    world_from_base=None,      # 4×4 T_world_base；默认单位矩阵
    initial_joints=None,       # 初始参考状态，按 joint_names 顺序，rad
    continuous_limits=None,    # continuous 关节的有限角度表示窗口
)
```

不传 `world_from_base` 时，所选基座坐标系与本入口的 world 坐标系重合。若选取 URDF 内部的某个 base_link，基座以上的安装链不自动算作世界安装变换；需要显式传入 `world_from_base`。目标建议使用 `frame="world"`，遵循项目当前接口约定。

`tip_link` 可以直接指向 URDF 已定义的 TCP link，固定安装链会自动保留。若仅有法兰 link，可以用额外工具变换：

```python
tcp = np.eye(4)
tcp[:3, 3] = [0, 0, 0.15]  # 在 tip_link 坐标系中定义 TCP
world = np.eye(4)
world[:3, 3] = [0, 0, 0.5]
kin = load_urdf(path, base_link="base_link", tip_link="flange",
                tcp_transform=tcp, world_from_base=world)
```

## 关节状态和连续性

独立服务不会读取真实机器人关节。每次求解可以传入最新关节状态作为 `seed`，也可以更新保存的参考状态：

```python
kin.robot.set_current_joints(actual_q)
result = kin.solve_one(target)  # 缺省 seed 使用刚刚保存的 actual_q
result = kin.solve_one(target, seed=actual_q, policy="continuous", max_joint_step=0.4)
```

求解不改变保存的参考状态，也不执行实际运动。服务继续负责限位、周期角表示、FK 精度检查、近构型排序和指定的连续性检查。

URDF 的 `continuous` 关节没有物理上下限，但当前服务的周期角枚举需要有限表示窗口，因此必须显式配置，不会暗中限制为 ±π：

```python
kin = load_urdf(path, base_link="base_link", tip_link="tool0",
                continuous_limits={"joint_1": [-4*np.pi, 4*np.pi]})
```

这个窗口是本入口的求解/角度表示范围，并非从 URDF 推导的物理限位；需要覆盖你打算使用的实际关节角。`revolute` 关节使用 URDF 的 lower/upper。

## 命令行调用

检查模型并打印关节顺序和当前末端位姿：

```powershell
.\.venv\Scripts\python.exe -B -m libraries.eaik.urdf_cli `
  --urdf C:\robot_models\my_robot.urdf `
  --base-link base_link --tip-link tool0
```

读取目标 JSON 并求解：

```powershell
.\.venv\Scripts\python.exe -B -m libraries.eaik.urdf_cli `
  --urdf C:\robot_models\my_robot.urdf `
  --base-link base_link --tip-link tool0 `
  --target C:\robot_models\target.json `
  --seed 0 -1 1 0 0 0 --all
```

目标 JSON 采用 Python 示例里的 target 格式，也可以使用 `position: [x,y,z]` 和 `rotation_matrix: [[...],[...],[...]]`。

可选参数：

| 参数 | 用途 |
| --- | --- |
| `--export parameters.json` | 导出所选关节链的 EAIK 参数 |
| `--tcp-transform tcp.json` | 读取 JSON 4×4 工具变换 |
| `--world-from-base base.json` | 读取 JSON 4×4 基座安装变换 |
| `--continuous-limits limits.json` | 读取 continuous 关节的 `[lower, upper]` 窗口映射 |
| `--all --max-solutions 8` | 返回多个候选，按离 seed 的距离排序 |
| `--max-joint-step 0.4` | 限制相对于 seed 的最大单关节变化 |
| `--timeout 2` | 此次目标求解的协作式时间预算，秒 |

命令失败返回非零状态，错误信息包含原因。

## 可立即运行的示例与验证

提供了与当前场景运动学一致的简化 UR5e 测试 URDF，以及已知可达目标：

```powershell
.\.venv\Scripts\python.exe -B -m libraries.eaik.urdf_cli `
  --urdf tests/fixtures/eaik_ur5e_chain.urdf `
  --base-link base_link --tip-link tool0 `
  --target tests/fixtures/eaik_ur5e_target.json `
  --seed 0.2 -0.7 1 -0.4 0.5 -0.2 --all

.\.venv\Scripts\python.exe -B -m unittest tests.test_eaik_urdf
```

测试 URDF 仅描述运动学，不是用于动力学执行的完整机器人模型。

8 项入口测试覆盖：与 Urchin/项目场景独立 FK 对照、数值差分验证世界雅可比、工具和基座变换、旁支排除、奇异零位和连续性、非法/耦合关节、continuous 窗口、网格缺失时的运动学读取，以及 CLI 求解/导出与禁用 MuJoCo 的独立运行。

## 范围

当前入口提取从 base_link 到 tip_link 的唯一串联转动链，支持 fixed 安装链、revolute 和显式配置表示窗口的 continuous。选定链内的 prismatic、floating、planar 和 mimic 关节会被明确拒绝。当前入口未暴露冗余机器人锁关节参数；可解析性由 EAIK 判断。

视觉/碰撞网格采用延迟加载，运动学计算无需读取网格。这个入口不装配仿真、碰撞检查或实际运动执行器。Xacro 需要先展开为 URDF，本入口目前不自动展开。

独立 URDF 和默认 MJCF 场景服务的 FK/Jacobian 均复用独立的 `ChainRobotModel`。MJCF 只在初始化提取几何时需要 MuJoCo；当前场景、碰撞和执行仍使用 MuJoCo。默认 IK 顺序为 EAIK → Jacobian，IKFast 暂不参与 `auto`。
