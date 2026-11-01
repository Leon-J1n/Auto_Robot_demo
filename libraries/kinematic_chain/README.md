# 通用关节链模型

`ChainRobotModel` 用 NumPy 实现串联转动关节的 FK 和世界坐标几何雅可比。模型计算不调用 MuJoCo，不依赖 EAIK，也不负责运动执行或碰撞检查。

## 文件与输入

- `model.py`：通用模型、状态提供器、刚体变换校验。
- `mujoco.py`：一次性从编译后的 MJCF 场景提取参数，并绑定运行时关节/基座状态。
- `urdf.py`：选择 URDF 的 base-to-tip 链，将 fixed、revolute/continuous 参数转换成同一种模型。
- `check_model.py`：与 MuJoCo 参考计算对照，保存验证结果。
- `checks/`：本次场景对照和玻璃杯流程结果。

参数沿用此前导出的 schema v1：`H_rows`（N×3 单位关节轴）、`P_rows`（N+1×3 零位位移）、`R_zero`（3×3 工具零位旋转）、`joint_names`、有限 `joint_limits`、可选 `world_from_base_zero`。长度单位 m，转动单位 rad，H/P 均表达在 q=0 的同一个基座坐标系中。

读取已经保存的参数时无需安装 MuJoCo：

```python
from libraries.kinematic_chain import ChainRobotModel
from kinematics import KinematicsService

robot = ChainRobotModel.from_json(
    "libraries/eaik/model_checks/scene_002.eaik.json"
)
kin = KinematicsService(robot)
pose = kin.forward(q)
jacobian = kin.jacobian(q)
result = kin.solve_one(target_pose, seed=q, backend="eaik")
```

`from_json()` 接受参数字典文件，也接受此前检查结果中的 `{"parameters": {...}, "checks": {...}}` 文件。

```python
from libraries.kinematic_chain import UrdfRobotModel

robot = UrdfRobotModel("robot.urdf", base_link="base_link", tip_link="tool0")
kin = KinematicsService(robot)
result = kin.solve_one(target_pose, seed=q, backend="eaik")
```

已有 `libraries.eaik.load_urdf()` 和 URDF CLI 保持原来的调用方式，底层复用这个模型。

## 接口和状态

```python
robot = ChainRobotModel(
    parameters,
    current_joints_provider=None,   # 无参数回调，返回最新 N 个关节角
    base_transform_provider=None, # 回调(q)，返回当前 4×4 T_world_base
    initial_joints=None,
    world_from_base=None,
)
```

`forward(q)` 返回世界位置和旋转矩阵；`jacobian(q)` 返回 6×N 矩阵，位置速度在前三行、角速度在后三行。末端位置包括工具/TCP 固定偏移。`base_transform(q)` 返回世界基座变换，`current_joints()` 返回状态副本。

离线模型可以通过 `set_current_joints(q)` 设置 seed 默认状态；接入外部状态回调时，由回调提供实时状态，不再允许手动覆盖。基座回调表示独立于所选机械臂关节的基座位姿，雅可比不包含移动底盘或额外上游关节的速度列。

`chain_parameters` 返回独立参数副本供求解器和导出使用。`eaik_parameters` 保留为原 URDF/EAIK 入口的兼容属性。

当前支持串联转动链；连续转动关节仍需有限的角度表示窗口。工具安装或机器人几何改变时应重建模型，实时关节和基座位姿变化则由状态回调更新。

## 当前项目装配

`SceneRobotRuntime.kinematics` 首次访问时提取一次几何，然后创建 `KinematicsService(ChainRobotModel(...))`。后续 FK/Jacobian 与各 IK 后端的 FK 复核只使用该模型。运行时仍使用 MuJoCo 维护场景状态、碰撞、执行器和测得的末端位姿。

Move、按压/推拉控制器和技能支持显式 `kinematics=` 注入。默认使用运行时的同一个服务。`SkillRuntime` 也支持 `kinematics_factory(runtime)`，为同一会话组装服务：

```python
from demo.runtime import SkillRuntime
from libraries.kinematic_chain.mujoco import create_scene_kinematics

session = SkillRuntime(registry, config, kinematics_factory=create_scene_kinematics)
```

并行接近检查的每个线程有独立运行时和服务。如果向 `MujocoApproachCollisionChecker` 注入自定义服务，也需提供 `kinematics_factory(runtime)` 来创建工作线程的实例，避免复用主线程状态。

旧 `MujocoKinematics` 保留为显式参考实现，用于对照计算；当前默认执行链不创建它。通用 `kinematics` 服务和协议无需修改，默认 IK 顺序为 EAIK → Jacobian；IKFast 暂不参与 auto，仍可按需显式选择。

## 验证

五个场景各对照 202 个构型（机器人几何相同）：最大 TCP 位置误差 `5.72e-16 m`，旋转元素误差 `9.99e-16`，雅可比元素误差 `1.89e-15`。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_kinematic_chain tests.test_eaik_urdf
.\.venv\Scripts\python.exe -B -m libraries.kinematic_chain.check_model --scenes world_model/robotsim/scene_002.xml
```

测试覆盖阻断 MuJoCo 计算、离线禁用 MuJoCo 导入、三种 IK 后端、动态基座/关节状态、JSON 几何校验、URDF 参考 FK/数值雅可比、技能注入和工作线程隔离。

使用默认 auto 与 EAIK，分别执行 `config_002.commands.json` 前四条 command 转换出的五步，均成功。未跑后续八条 command。结果见 [scene_comparison.json](C:/Users/21474/Desktop/robot-agent/libraries/kinematic_chain/checks/scene_comparison.json)、[config_002.first_four.json](C:/Users/21474/Desktop/robot-agent/libraries/kinematic_chain/checks/config_002.first_four.json)。
