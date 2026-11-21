# Kinematics 模块

## 1. 职责与边界

`kinematics` 提供机器人运动学的统一入口：正向运动学（FK）、几何 Jacobian、逆运动学（IK）后端发现与分发、候选解校验和排序。核心 API 与仿真器无关；机器人模型及 IK 算法通过协议接入。

运动学只计算末端位姿、雅可比和关节解，不验证环境或自碰撞，不生成、平滑或时间参数化路径，也不执行机器人。碰撞检查由 `collision` 负责，路径搜索与轨迹准备由 `planning` 负责，真实执行状态由 `execution` 提供。

IK 的终点解通过验证不代表连接到该解的整条路径可行。`max_joint_step` 只限制解相对 seed 的单关节变化；连续路径需要规划层沿路径逐点求解并执行碰撞检查。

## 2. 目录结构与职责

```text
kinematics/
├── core/                    # 仿真无关的 API 和服务
│   ├── contracts.py         # RobotModel、IKBackend、IKRequest、IKResult、IKError
│   ├── registry.py          # solver.json 发现、名称解析和后端工厂
│   ├── service.py           # FK/Jacobian、IK 分发、候选校验与选择
│   ├── geometry.py          # 位姿解析、坐标/旋转误差计算
│   └── policies.py          # 周期角归一化、等价构型和候选去重
├── models/mujoco.py         # MuJoCo 机器人模型适配
└── backends/mujoco.py       # 从 MuJoCo runtime 创建运动学服务

libraries/
├── kinematic_chain/         # 串联链 FK/Jacobian、URDF 与 MJCF 模型
├── eaik/                    # EAIK 后端和 URDF 加载入口
├── ssik/                    # SSIK 6R/7R 后端和 URDF 加载入口
├── jacobian_ik/             # 阻尼最小二乘数值 IK 后端
└── ikfast/                  # IKFast 后端
```

每个 `KinematicsService` 持有自己的 RobotModel 和后端实例。注册表读取 `libraries/*/solver.json`，发现阶段不导入可选求解器依赖；后端在首次使用时创建并检查模型支持情况。

## 3. 数据流

```text
URDF / 串联链参数 / MuJoCo runtime
                 ↓
       RobotModel + KinematicsService
          ├── forward(q) / jacobian(q)
          └── solve_one / solve_all(target, seed)
                    ↓
        solver.json 解析后端回退顺序
                    ↓
       IK 候选 → 关节限位与连续性检查
                    ↓
       周期角归一化 → RobotModel FK 复核
                    ↓
       去重、按 seed 距离排序 → IKResult
                    ↓
             planning 后续处理
```

所有后端共享一次 IK 调用的绝对 deadline。某个后端无解、不可用、不支持当前模型或候选未通过校验时，服务可在剩余预算内继续下一个后端。返回的是首个产生有效结果的后端候选，不会合并多个后端的解。

## 4. 对外接口

主要公开类型从 `kinematics` 导出：`KinematicsService`、`RobotModel`、`IKBackend`、`IKRequest`、`IKResult`、`IKError`、`SolverRegistry` 和 `SolverDescriptor`。

| 接口 | 输入 / 输出及用途 |
| --- | --- |
| `KinematicsService(robot, registry=None)` | 创建统一服务；默认发现仓库 `libraries/` 下的求解器清单 |
| `forward(joints)` | 返回世界坐标 TCP 位姿，含 `position`（米）和 `rotation_matrix` |
| `jacobian(joints)` | 返回世界坐标几何 Jacobian，形状 `(6, DOF)`，线速度在前、角速度在后 |
| `solve_one(target_pose, *, seed=None, backend="auto", backends=None, policy="nearest", timeout=1.0, max_joint_step=None)` | 返回 `IKResult`，`.solution` 为选中解 |
| `solve_all(target_pose, *, seed=None, backend="auto", backends=None, policy="nearest", timeout=2.0, max_solutions=8, max_joint_step=None)` | 返回首个成功后端找到的候选，最多 `max_solutions` 个 |
| `available_backends()` | 返回已注册名称与别名，不代表可选原生库当前可加载 |
| `equivalent_configurations(joints, reference, max_variants=8)` | 返回限位内的周期等价关节构型，并按参考构型距离排序 |

位置单位为米，关节角和 `max_joint_step` 单位为弧度。目标姿态可用 `rotation_matrix`、RPY（角度制）或 wxyz 四元数表示；未提供姿态时使用当前关节状态对应的末端姿态。`frame` 接受 `world` 或 `base`，其中 `base` 沿用现有世界对齐语义，不做基座到世界的自动转换，推荐显式使用 `world`。

```python
from kinematics import KinematicsService
from libraries.kinematic_chain import ChainRobotModel

robot = ChainRobotModel.from_json("robot_parameters.json", initial_joints=seed)
kin = KinematicsService(robot)
pose = {
    "frame": "world",
    "position": [0.33, 0.46, 0.66],
    "orientation": {
        "representation": "rpy",
        "roll": 180,
        "pitch": -90,
        "yaw": 0,
    },
}
result = kin.solve_one(pose, seed=seed, policy="continuous",
                       max_joint_step=0.4, timeout=1.0)
q_goal = result.solution
solver_info = result.metadata()  # 不包含关节数组
```

策略 `nearest` 按 L2 距离选择离 seed 最近的候选；`minimum_travel` 按各关节绝对变化总和选择；`continuous` 要求提供 `max_joint_step` 并剔除超限候选。`IKResult` 保存候选、规范 solver/backend 名、可选残差、回退原因及诊断信息。失败抛出 `IKError`。

## 5. 配置、输入与输出

IK 精度读取根目录 `config/global.json` 的 `planning.ik_validation`：当前位置容差为 `0.003 m`，姿态容差为 `0.01 rad`。服务用同一配置生成后端请求并复核候选 FK；后端可以使用更严格的阈值，不能放宽全局上限。修改配置后需重新创建服务或重启任务。

当前清单将 `auto` 顺序设为 EAIK（优先级 10）→ SSIK（优先级 15）→ Jacobian（优先级 20）。首次自动调用会按实际链几何准备 EAIK/SSIK 并复核 FK，不使用资产中的 `arm_configuration` 字符串；不支持该几何或依赖不可用的解析后端会被排除，并在当前服务实例中缓存。EAIK 不支持的 6R/7R 链自动选择 SSIK，支持移动关节等其他模型可继续使用适用的数值后端。解析后端对某个目标无解时仍按顺序回退，不改变几何识别结果。`automatic_backend_info()` 返回后端顺序和排除原因，`quick_reachability()` 使用适用的 EAIK/SSIK 链，不启用数值回退。显式 `backend` 或 `backends` 保持调用者指定顺序。准备阶段在目标求解 deadline 建立前执行，首次调用可能额外耗时；解析算法运行中无法强制中断。更换链几何后应创建新的服务实例。IKFast（优先级为空）不参加自动回退，但可显式指定。`trac_ik` 是 `jacobian` 的兼容别名，实际使用的是项目内 Jacobian 数值求解器，并非原生 TRAC-IK。

服务接受 `RobotModel`：`limits` 为 `(DOF, 2)` 关节限位，`joint_periods` 为每关节周期（非周期坐标用 0）；模型实现 `current_joints()`、`forward(q)`、`jacobian(q)` 和 `base_transform(q)`。项目提供的入口包括：

| 入口 | 用途 |
| --- | --- |
| `ChainRobotModel(parameters, ...)` / `ChainRobotModel.from_json(path, ...)` | 从串联链参数构建独立运动学模型 |
| `UrdfRobotModel(path, *, tip_link, base_link=None, ...)` | 从 URDF 选择机器人链并适配为 `RobotModel` |
| `libraries.eaik.load_urdf(...)` | 创建只注册 EAIK 的 URDF 运动学服务 |
| `create_scene_kinematics(runtime, registry=None)` | 提取 MuJoCo 链几何并绑定 runtime 的实时状态 |

新增求解器时，在 `libraries/<solver>/` 提供 adapter 和 `solver.json`。清单 `api_version` 必须为 1，`factory` 采用 `module.path:callable`；`aliases` 可选，`auto_priority` 为空表示仅显式调用，设置整数后进入自动链（数字越小越优先，同优先级按名称排序）。adapter 实现 `supports(robot)` 和 `solve(request)`，将库自身的坐标系、单位、关节顺序及结果格式转换为统一协议。`IKRequest.remaining_time` 提供共享 deadline 的剩余秒数；不可中断的原生调用无法由服务强制终止。新清单需在创建新服务时发现，现有服务使用创建时的注册表快照。

服务会验证候选形状和有限性、归一化周期关节、检查限位与连续性，并通过 RobotModel FK 复核位置和姿态误差，再去重并排序。`solve_all` 只返回第一个成功后端在预算内找到的结果，数值方法不保证枚举全部 IK 分支。单个服务实例不保证线程安全；并行求解应使用独立服务实例。

## 6. 验证与限制

从仓库根目录运行 IK 容差配置及候选校验测试：

```powershell
.\.venv\Scripts\python.exe -B -m unittest planning.tests.test_ik_tolerances
```

模型目前覆盖串联转动关节；URDF 链支持 fixed、revolute 和 continuous 关节，continuous 关节需给出有限搜索窗口。第三方解析器和数值算法各有机器人结构、模型和超时支持范围。IK 候选校验不包含碰撞或路径可达性；最终运动必须由 planning 和 collision 继续验证。
