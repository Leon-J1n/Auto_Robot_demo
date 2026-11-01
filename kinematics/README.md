# 运动学接口与求解器插件

更新日期：2026-10-09。本文总结本目录的职责边界和调用接口，并保留插件接入说明。

## 职责边界

| 部分 | 职责 | 所在位置 |
| --- | --- | --- |
| 统一服务 | FK/Jacobian 分发、IK 后端选择与回退、候选复核和排序 | `service.py` |
| 协议与结果类型 | RobotModel、IKBackend、IKRequest、IKResult、IKError | `contracts.py` |
| 注册表 | 清单发现、名称/别名解析、按需创建后端 | `registry.py` |
| 位姿和构型处理 | 坐标格式转换、旋转误差、周期归一化、去重和等价构型 | `geometry.py`、`policies.py` |
| 具体机器人模型 | 几何加载、FK/Jacobian 计算、参考状态绑定 | `libraries/kinematic_chain/`；本目录 `models/` 保留 MuJoCo 参考模型 |
| 具体 IK 算法 | 第三方依赖、坐标适配、模型支持检查和候选生成 | `libraries/ikfast/`、`libraries/jacobian_ik/`、`libraries/eaik/` |

本目录输出位姿、雅可比或关节构型。无碰撞轨迹搜索、简化和平滑由 [planning](C:/Users/21474/Desktop/robot-agent/planning/README.md) 负责；碰撞判断由检测器负责；运动执行、实测状态和场景更新由运行时及控制器负责。

`max_joint_step` 只限制单次 IK 相对 seed 的变化；逐点使用上一构型作为 seed 才能检查路点衔接。IK 校验通过不表示无碰撞，最近 IK 解也不保证整条运动路径最短。

`kinematics` 定义固定的 v1 接口，负责分发、验证和选择结果。新增符合接口的 IK 方法库时，
只需在 `libraries` 中添加一个目录和清单，不需要修改 `kinematics`、Move 策略或控制器。
第三方库原本的接口、坐标系、单位和返回值由该目录里的 adapter 转换。
未来如果需要接口中尚未定义的能力，需要扩展接口版本；任意第三方库并不能直接原样接入。

```text
kinematics/
  contracts.py             RobotModel、IKBackend、IKRequest、IKResult、IKError
  registry.py              发现 solver.json，解析名称/别名，按需加载工厂
  service.py               FK/雅可比入口，IK 分发、FK 复核、回退与选择
  policies.py              周期关节角归一化、去重、等价构型
  geometry.py              位姿转换、旋转误差
  models/mujoco_model.py   显式参考模型：MuJoCo FK、雅可比与基座适配
  mujoco_kinematics.py     创建 MuJoCo 参考服务；默认执行链不创建它
libraries/
  kinematic_chain/         独立 FK/Jacobian、JSON/MJCF/URDF 模型与状态绑定
  ikfast/                  adapter.py、solver.json、原生 UR5e IKFast
  jacobian_ik/             adapter.py、solver.json、阻尼最小二乘数值 IK
  eaik/                    EAIK、场景参数提取和 FK/IK 检查；auto 默认优先使用
```

## 调用接口

构造入口为 `KinematicsService(robot, registry=None)`。未指定注册表时发现 `libraries` 清单；场景中直接使用缓存的 `runtime.kinematics`。

| 公开接口 | 返回值/含义 |
| --- | --- |
| `forward(joints)` | 世界坐标 TCP 位姿字典：`position` 为 (3,)，`rotation_matrix` 为 (3,3) |
| `jacobian(joints)` | 世界坐标几何雅可比 (6,N)，线速度在前、角速度在后 |
| `solve_one(target_pose, *, seed=None, backend="auto", backends=None, policy="nearest", timeout=1.0, max_joint_step=None)` | IKResult，`.solution` 为排序后首项 |
| `solve_all(target_pose, *, seed=None, backend="auto", backends=None, policy="nearest", timeout=2.0, max_solutions=8, max_joint_step=None)` | IKResult，`.solutions` 为第一个成功后端的候选，不合并多个库 |
| `equivalent_configurations(joints, reference, max_variants=8)` | 限位内周期等价构型列表，按到 reference 的距离排序 |
| `available_backends()` | 注册名称/别名元组 |
| `robot`、`limits`、`joint_periods` | 模型实例、N×2 限位和长度 N 的周期数组 |

位置单位为米，转动关节角与跳变限制为弧度；目标 RPY 使用度，四元数使用 wxyz。未指定 seed 时读取模型 `current_joints()`。`frame="base"` 目前没有自动坐标变换，保留世界对齐语义；建议使用 `world`。

```python
kin = runtime.kinematics
print(kin.available_backends())  # 注册名称/别名，尚未检查动态库是否可用
pose = {"position": [0.33, 0.46, 0.66],
        "orientation": {"representation": "rpy", "roll": 180, "pitch": -90, "yaw": 0}}
seed = runtime.get_joint_positions()

result = kin.solve_one(pose, seed=seed, backend="ikfast")
q = result.solution
candidates = kin.solve_all(pose, seed=seed, backend="ikfast", max_solutions=8).solutions
result = kin.solve_one(pose, seed=seed, backends=["ikfast", "jacobian"],
                       policy="continuous", max_joint_step=0.4, timeout=1.0)
result = kin.solve_one(pose, seed=seed, backend="jacobian")
result = kin.solve_all(pose, seed=seed, policy="minimum_travel")

# 执行层使用统一结果类型。
q = result.solution
solver_info = result.metadata()  # 供轨迹日志使用，不包含关节数组
```

`solve_one/solve_all` 都返回 `IKResult`。包含选中解 `solution`、候选 `solutions`、
真正使用的 `solver/backend`、可选残差与回退原因、诊断信息。
项目内的 demo、运动策略、按压及推拉控制器均使用这两个接口。
原来的 `solve()`、`solve_candidates()` 及返回关节字典的兼容层已移除。
失败统一抛出 `IKError`；需要继续尝试其他规划方案的调用层应捕获它。
`solve_one` 的解析后端可以保留多个候选，首项是选中解；`solve_all` 按 `max_solutions` 截断。
数值求解只能给出预算内找到的解，不保证枚举所有分支。

| 参数 | 含义 |
| --- | --- |
| `backend` | 单库名称/别名，显式指定时不回退；默认 `auto` |
| `backends` | 调用者指定的有序回退列表，与显式 `backend` 互斥 |
| `policy="nearest"` | 归一化后，选择距 seed 的 L2 距离最小解，保持原行为 |
| `policy="minimum_travel"` | 选择各关节绝对变化之和最小的候选 |
| `policy="continuous"` | 必须指定 `max_joint_step`，过滤后选择距 seed 最近的候选 |
| `max_joint_step` | 每个关节相对 seed 的最大变化，所有策略都可以指定 |
| `timeout` | 秒；回退链共享同一个绝对 deadline，不为下一库重新计时 |

`auto` 使用清单优先级：目前 EAIK（10）→ Jacobian（20），遇到无解、库不可用、模型不匹配、
FK 校验失败或候选全部超出连续性限制时尝试下一库。有有效解析候选时不额外启动数值搜索。
原来的 `trac_ik` 名称仍作为 `jacobian` 的别名；它是已有雅可比数值算法，未接入原生 TRAC-IK。
结果始终报告规范名 `jacobian`。Move JSON 中的 `strategy_params.ik_method` 字段仍可使用，
执行时传给接口的 `backend` 参数，因此已有命令文件不需要改名。

IK 选择只比较终点候选，不能保证整条路径运动幅度最小。现有 JointMove 仍会结合碰撞检查、
直连/RRT 路径规划和累计关节行程选择实际路径；直线分支的连续性限制继续保留。

## 添加一个方法库

创建 `libraries/my_solver/__init__.py`、`adapter.py` 和 `solver.json`。
JSON 清单使用标准库解析，兼容项目 Python 3.10，不需要新增 TOML 依赖。

```json
{
  "api_version": 1,
  "name": "my_solver",
  "aliases": ["my_alias"],
  "factory": "libraries.my_solver.adapter:create_backend",
  "auto_priority": null
}
```

`auto_priority: null` 或不填写表示只接受显式调用；填写整数后加入 auto 回退链，数值越小越优先。
同优先级按名称排序，名称和别名不能重复。添加完成后重新创建运动学服务或重启 demo；
现有服务保留创建时的清单快照。清单发现不导入第三方库。

```python
# libraries/my_solver/adapter.py
import numpy as np
from kinematics import IKError, IKRequest, IKResult, RobotModel

class MyBackend:
    def __init__(self, robot: RobotModel):
        self.robot = robot
        # 可以在这里按需加载第三方库；不能把实例缓存到全局。

    def supports(self, robot: RobotModel) -> bool:
        # 应检查真实的机器人模型是否受支持；IKFast 还会校准/验证 FK。
        return len(robot.limits) == 6

    def solve(self, request: IKRequest) -> IKResult:
        if request.remaining_time <= 0:
            raise IKError("IK budget exhausted")
        # 用你的第三方库替换下一行，并转换到 RobotModel 的关节顺序。
        candidates = your_library.inverse(
            request.position, request.rotation, request.seed,
            timeout=request.remaining_time,
        )
        return IKResult(solutions=[np.asarray(q, dtype=float) for q in candidates],
                        backend="my_solver_native")

def create_backend(robot: RobotModel):
    return MyBackend(robot)
```

此示例中的 `your_library` 是需要替换和导入的占位符。adapter 必须处理第三方库自身的
位姿/工具变换、依赖加载、异常与多解枚举。耗时搜索应定期检查 `request.remaining_time`，
或把剩余预算传给原生库；Python 服务不能强行中断不支持超时的原生函数。

`IKRequest` 提供世界位置（米）、旋转矩阵、关节 seed（转动关节弧度）、deadline、
是否枚举、多解上限和最大关节变化。服务层拒绝非有限/维度错误的结果，进行限位、
周期归一化、连续性和 RobotModel FK 复核，并将结果排序、去重。
位置复核上限为 `2e-4 m`、姿态上限为 `2e-3 rad`；插件可通过结果字段要求更严格的阈值。
IKFast 使用更严格的阈值。插件不能通过填写更大的阈值绕过复核。

Move 请求无需修改代码即可选用新库：

```json
{"strategy_params": {"ik_method": "my_solver"}}
```

## 机器人模型与隔离

| RobotModel 成员 | 约定 |
| --- | --- |
| `limits`、`joint_periods` | 关节上下限和周期；协议允许非周期坐标的周期为 0 |
| `current_joints()` | 长度 N 的参考状态，不执行运动 |
| `forward(q)`、`jacobian(q)` | 与统一服务相同的世界坐标约定 |
| `base_transform(q)` | `T_world_base`，4×4 基座到世界齐次变换 |

| 模型入口 | 返回值与装配 |
| --- | --- |
| `ChainRobotModel(parameters, *, current_joints_provider=None, base_transform_provider=None, initial_joints=None, world_from_base=None)` | 接收 H/P 零位参数，绑定参考关节及基座回调 |
| `ChainRobotModel.from_json(path, **kwargs)` | 从参数 JSON 或含 `parameters` 的导出文件创建同一模型 |
| `UrdfRobotModel(path, *, tip_link, base_link=None, tcp_transform=None, world_from_base=None, initial_joints=None, continuous_limits=None)` | 加载所选 URDF 链，可再传入 `KinematicsService(robot)` |
| `libraries.eaik.load_urdf(...)` | 加载参数与 UrdfRobotModel 相同；返回仅注册 EAIK 的服务，auto 使用 EAIK，加载时检查模型支持情况 |
| `libraries.kinematic_chain.mujoco.create_scene_kinematics(runtime, registry=None)` | 一次 MJCF 几何提取，再绑定实时状态，返回独立模型服务 |
| `libraries.kinematic_chain.mujoco.extract_chain_parameters(runtime)` | 返回可保存的零位模型参数字典 |

```python
from kinematics import KinematicsService
from libraries.kinematic_chain import ChainRobotModel
from libraries.eaik import load_urdf

robot = ChainRobotModel.from_json("robot_parameters.json", initial_joints=seed)
kin = KinematicsService(robot)
kin = load_urdf("robot.urdf", tip_link="tool0", base_link="base_link")
```

通用 ChainRobotModel 当前只实现串联转动关节。URDF 所选链保留 fixed，支持 revolute/continuous；continuous 需提供有限搜索窗口。`tcp_transform` 为 `T_tip_TCP`，`world_from_base` 为 `T_world_base`；Xacro 先展开为 URDF。解析后端另有机器人几何支持范围。

离线模型可通过 `set_current_joints(q)` 更新参考状态；已绑定外部关节回调时禁止调用。模型几何或工具结构变化需重建模型。`chain_parameters` 返回参数深拷贝，可用于保存或后端适配。

`RobotModel` 暴露 `limits`（N×2）、`joint_periods`（N；非周期关节为 0）、
`current_joints()`、`forward(q)`、`jacobian(q)`（6×N，位置在前、旋转在后）和
`base_transform(q)`（4×4）。MuJoCo 实现在 `models` 下，不进入通用服务或算法中。
当前默认模型是 `libraries/kinematic_chain/ChainRobotModel`，MJCF/URDF 共用其 FK/Jacobian，运行时注入实时关节和基座状态。Move、Press、Push/Pull 支持 `kinematics=` 注入；demo 会话支持 `kinematics_factory(runtime)`。其他仿真器/机器人只需提供相同接口并创建 `KinematicsService(robot)`。`MujocoKinematics` 保留为显式参考实现。
当前 Move 技能和运行时仍面向六轴机器人；通用 IK 接口本身不限定六轴。

普通 `import kinematics` 或按名称导入通用接口不加载 MuJoCo；显式导入 `MujocoKinematics` 或使用 `from kinematics import *` 会加载参考实现。JSON/URDF 的独立计算无需 MuJoCo，MJCF 参数提取以及现有场景、碰撞和执行仍使用 MuJoCo。

完整模型协议、JSON/MJCF/URDF 加载参数与执行层注入签名见 [运动学接口清单](C:/Users/21474/Desktop/robot-agent/KINEMATICS_INTERFACES.md)，具体引擎边界见 [MuJoCo 依赖清单](C:/Users/21474/Desktop/robot-agent/MUJOCO_DEPENDENCIES.md)。

目标无姿态时保持模型 `current_joints()` 对应的末端姿态。`rotation_matrix` 可直接传入；
RPY 使用度，四元数使用 wxyz。`base` 标签保留原来的世界对齐坐标约定；本次未改变坐标语义。

每个服务独立持有 RobotModel/FK 工作区与后端实例，并行分支应使用独立服务；
同一个服务实例不提供线程安全保证。损坏的清单记录在 `registry.discovery_errors`，
不会阻止其他库注册；本地清单/adapter 是可信代码，不应加载不受信任的插件。

## 验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_kinematics_plugins.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_linear_continuity.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_fixed_orientation_shortcut.py -v
```
