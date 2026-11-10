# Skill 接口与调用流程

更新日期：2026-10-07，已同步独立运动学模型和服务注入接口。

本文按当前代码说明机器人原子技能的调用边界、演示命令的转换流程，以及技能依赖的方法库接口。参数的完整约束和默认值以各技能目录中的 `schema.yaml`、`config.yaml` 和实现代码为准。

## 1. 接口层次

```text
commands.json（演示用简化命令）
  → SceneRegistry + SkillCommandConverter
  → 结构化 Skill request
  → Skill.execute(request)
  → 校验 / 运行时上下文 / 策略选择 / 执行 / 结果验证
  → control、kinematics、planning、collision、sensors、world_model
  → 结构化结果
```

需要区分两种接口：`demo/SKILLS.md` 定义的是演示命令格式；`atomic_action/*/*/schema.yaml` 定义的是 Skill 本身接收的结构化请求。转换器会利用场景注册表，把简化命令中的目标名称解析为位姿或预设动作请求。

## 2. Skill 公共接口

各 Skill 以 `execute(request: Mapping[str, Any]) -> Dict[str, Any]` 为调用入口。通常可通过 `Skill(config=None, *, robot_runtime=runtime)` 创建实例；`_MoveExecutor` 还支持 `scene_path` 和 `trajectory_callback`。Move、Press、PushPull 支持 `kinematics=` 服务注入，默认使用 `runtime.kinematics`。`runtime_context` 在 schema 中标为内部数据，由运行时读取和填充，不应作为普通命令参数依赖。

| Skill | 实现类 | 结构化请求的主要字段 | 结果字段 | 策略 |
| --- | --- | --- | --- | --- |
| Move | `atomic_action.motion.move_linear.skill.MoveLinearAction`、`atomic_action.motion.move_joint.skill.MoveJointAction`、`atomic_action.motion.move_circular.skill.MoveCircularAction` | `target`、`motion`、`planning`、`constraints`、`strategy_params` | `execution_result`、`trajectory`、`timing` | `joint_move`、`linear_move`、`circular_move`；规划模式为 `direct` 或 `collision_free` |
| Locate | `atomic_action.perception.location.skill.LocateAction` | `target`、`reference`、`offset`、`localization`、`constraints`、`strategy_params` | `localization_result` | 已知模型/类别、抓取位姿预测、未知物体、接近点、相对点定位 |
| Search | `atomic_action.perception.search.skill.SearchAction` | `target`、`observation`、`camera`、`search`、`search_region`、`history`、`constraints` | `search_result` | 系统扫描、先验引导、遮挡感知、局部视角优化 |
| Grasp | `atomic_action.manipulation.grasp.skill.GraspAction` | `target`、`grasp`、`constraints`、`strategy_params` | `grasp_result` | `default_grasp` |
| Release | `atomic_action.manipulation.release.skill.ReleaseAction` | `target`、`release`、`constraints`、`strategy_params` | `release_result` | `default_release` |
| Press | 已移除；上层改用 `MoveLinearAction` | `target`、`motion`、`planning`、`constraints` | `execution_result` | 直线位移 |
| PullPush | 已移除；上层改用 `MoveLinearAction` 或 `MoveCircularAction` | `target`、`motion`、`planning`、`constraints` | `execution_result` | 直线/圆弧几何运动 |

结构化结果一般包含 `success`、`status`、`selection`、对应的 `*_result` 和 `error`。错误对象通常给出 `error_code`、`error_message`、`failed_stage`、`recoverable` 和 `recommended_action`。各字段是否存在以及状态枚举以对应 schema 和实际实现为准。

`pull_push` 的 `status` 为 `success | error | suspend`：完整完成、执行前失败、已执行但未完成（任务中止）。中止时 `success=false`，保留 `error` 中的停止诊断及 `pull_push_result` 中的进度。demo 将该状态传递到顶层报告并停止后续命令，不把 best_effort 部分完成当作成功。

### Move 请求示例

```python
result = move_skill.execute({
    "target": {
        "type": "pose",
        "frame": "base",
        "position": {"x": 0.4, "y": 0.1, "z": 0.3},
        "orientation": {"representation": "quaternion", "w": 1, "x": 0, "y": 0, "z": 0},
    },
    "motion": {"path_type": "linear"},
    "planning": {"mode": "auto"},
})
```

Move 的 `target.type` 可取 `pose`、`joint`、`named_state`。位姿的长度单位为米，关节角为弧度。其他技能的字段、枚举、单位和条件必填规则参见各自的 `schema.yaml`。

## 3. 演示命令接口与执行流程

演示命令放在 JSON 文件的 `commands` 数组中，按数组顺序执行：

```json
{
  "registry": "../common/scenes/scene_001.interactions.json",
  "commands": [
    {"skill_name": "move", "parameters": {"target": "red_ball"}},
    {"skill_name": "grasp", "parameters": {"target": "red_ball"}}
  ]
}
```

当前转换器识别 `locate`、`move`、`grasp`、`release`、`press`、`push`、`pull`。其中 `push` 和 `pull` 都转换为 `pull_push` 执行步骤（旧 `push_pull` 名称保留兼容）。`locate` 在演示转换器中不生成可执行步骤。`SearchAction` 虽有独立结构化接口，但未接入当前演示命令分发。简化命令的 `parameters.target` 使用对象 ID、别名、命名目标或 `end_effector`；具体可用目标取决于场景注册表。

**当前 demo 的 push / pull 原子技能**直接使用统一的已抓持运动流程：检查 `holding` → 规划并执行普通移动 action → 成功后 release。两者均不解析 target/link、不自动接近或抓取、不读取铰链。直线参数必须提供正数 `distance` 和 `direction`；圆弧参数必须提供世界坐标 `arc_center`、`arc_axis` 和有符号 `angle_deg`。`frame` 为 `tool`（默认）或 `world`，用于方向和轴向；圆心始终是世界坐标。push 不会再额外反转方向或角度。运动失败时不自动释放。旧 contact 推送、机构目标和 fallback 编排不再是 demo 的 push 入口；需要先执行 pick/grasp。commands.004 用 +90° pull 开门，重新抓持后用 -90° push 关门。

`move` 还可用 `planning_method`/`path_type`（`joint`、`linear`、`circular`）、`keep_end_effector_orientation` 等参数。目标为 `end_effector` 时，可以用 `relation` 和 `distance_m` 表示相对位移，例如 `above` 表示向上移动。

运行入口是 `demo/run_demo.py` 中的 `run_demo(command_file, *, generated_config=None, log_file=..., route_file=...)`，也可通过 `python -m demo.run_demo --commands <文件>` 启动。主流程如下：

1. 读取命令文件，并由 `SceneRegistry(path)` 加载和校验场景对象、别名、空间锚点、动作请求与 affordance。
2. 创建一个持久化的 `SkillRuntime`，共享同一份 MuJoCo `model/data` 状态及独立运动学服务；支持 `kinematics_factory(runtime)` 替换服务。
3. `SkillCommandConverter.convert_command(command, index)` 根据注册表及当前场景位姿生成执行步骤。交互目标在碰撞检查通过时可拆成接近步骤和直线接触步骤。
4. `SkillRuntime.execute_step(step, defaults)` 将步骤分发给 `_MoveExecutor`、`GraspAction`、`ReleaseAction`、`MoveLinearAction` 或 `MoveLinearAction` 的 `execute()`。
5. 执行后调用 `mujoco.mj_forward()` 同步状态；抓取或释放成功后重建 Move Skill，使后续规划使用更新后的持物状态，并继续复用同一运动学服务。
6. 记录结果、轨迹样本和日志；演示运行器对失败对象执行跳过及复位处理。

`SkillCommandConverter.convert(source)` 可离线转换整个命令文件；逐条实时执行时使用 `convert_command()`，目标位姿由注入的 `pose_provider` 从当前场景解析。`SceneRegistry` 的常用接口为 `scene_path`、`objects`、`named_targets`、`object(object_key)`、`action_request(object_key, action)`。

## 4. Skill 内部的典型流程

```text
请求校验和规范化
  → 读取运行时上下文
  → StrategySelector.select(...)
  → 前置条件校验
  → 具体 Strategy 执行
  → 结果验证及必要的重试/回退
  → 返回结构化结果或结构化错误
```

Move 在策略生成路径请求后，还会运行直接规划或避障规划、逆运动学、碰撞检查和轨迹执行。Grasp/Release 调用夹爪控制器并验证物体状态；Press 与 PushPull 调用各自的接触控制器。Locate 和 Search 分别生成目标位姿或下一观察视角，其感知、变换、可达性等能力需要相应运行时服务支持。

## 5. 方法库与适配接口

以下列出供 Skill 和演示层调用的主要公共方法。省略的参数应以源码签名为准。

| 模块 | 类或函数 | 主要接口与用途 |
| --- | --- | --- |
| `utils/simulation_runtime.py` | `SceneRobotRuntime(scene_path, ...)` | 持有 MuJoCo `model/data`；`joint_limits`、`kinematics` 属性；`get_joint_positions()`、`get_gripper_width()`、`get_end_effector_pose()`、`get_context(request=None)`、`execute_trajectory(trajectory, *, timeout=30.0)`、`attach_viewer(viewer)`、`close()` |
| `kinematics/service.py` | `KinematicsService(robot, registry=None)`；默认由 `runtime.kinematics` 提供 | `forward(joints)`、`jacobian(joints)`、`solve_one(target_pose, *, seed=None, backend="auto", backends=None, policy="nearest", timeout=1.0, max_joint_step=None)`、`solve_all(target_pose, *, seed=None, backend="auto", backends=None, policy="nearest", timeout=2.0, max_solutions=8, max_joint_step=None)`、`equivalent_configurations(joints, reference, max_variants=8)`、`available_backends()` |
| `libraries/kinematic_chain/model.py` | `ChainRobotModel(parameters, ...)`、`ChainRobotModel.from_json(path, **kwargs)` | 独立 NumPy FK/Jacobian；`current_joints()`、`set_current_joints(q)`、`base_transform(q)`、`chain_parameters`；可绑定实时状态回调 |
| `libraries/kinematic_chain/mujoco.py` | `create_scene_kinematics(runtime, registry=None)`、`extract_chain_parameters(runtime)` | MJCF 一次性参数提取，以及运行时状态绑定；默认服务使用 `ChainRobotModel` |
| `libraries/eaik/urdf.py` | `load_urdf(path, *, tip_link, base_link=None, ...)` | 创建独立 URDF 运动学服务，默认 EAIK；不装配仿真、碰撞或实际运动执行 |
| `kinematics/mujoco_kinematics.py` | `MujocoKinematics(runtime)` | 显式 MuJoCo 参考服务，使用统一服务接口；默认执行链不创建它 |
| `planning/collision_free_planner.py` | `CollisionFreePlanner` | Move 的避障规划器；通过 Move 的规划流程使用 |
| `control/gripper_controller.py` | `MujocoGripperController(runtime)` | `open(width, *, speed=0.5, timeout=5.0)`、`close(width, *, speed=0.5, force=20.0, timeout=5.0)`、`hold(force)`、`release(*, width=None, timeout=5.0)`、`get_state()`、`width` 属性 |
| `control/press_controller.py` | `MujocoPressController(runtime, *, kinematics=None)` | `get_state(target_object_id=None)`、`execute_displacement(...)`、`execute_force(...)`、`verification_snapshot(target_object_id, *, strategy)`；执行接口控制方向、目标力/深度、速度、接触阈值、保持和回撤 |
| `control/pull_push_controller.py` | `MujocoPullPushController(runtime, *, kinematics=None)` | `get_state(object_id, *, interaction_mode="mechanism")`、`plan(request, start_joints=..., start_pose=...)`、`execute_prepared(request, prepared)`；`execute(request)` 保留为直接规划并执行的兼容入口 |
| `sensors/camera/interface.py` | `Camera` | 抽象属性 `camera_id`；`get_current_frame()`、`close()` |
| `sensors/camera/manager.py` | `CameraManager` | `manager[camera_id]`、`get_current_frame(camera_id)`、`close()` |
| `utils/simulation_runtime.py` | `SceneRobotRuntime` 相机接口 | `cameras` 属性、`get_camera_frame(camera_id)`、`save_camera_frame(camera_id, output_dir, *, prefix=None)`；支持 `global`、`wrist` 别名 |
| `libraries/ikfast/ur5e_backend.py` | `solve(position, rotation)`、`forward(joints)` | UR5e 原生 IKFast 后端；业务层通过 `KinematicsService` 和插件 adapter 调用 |

`libraries/motion_planning/` 包含 RRT-Connect、路径简化和轨迹优化适配代码。`libraries/trac_ik/` 保留第三方源码；当前 `trac_ik` 是 `jacobian` 后端的别名，使用 RobotModel 提供的 FK/Jacobian，未接入原生 TRAC-IK。库目录的约定是业务代码不直接从第三方原始源码目录导入，而通过项目适配接口调用。

### 运动学注入与依赖边界

```python
kin = runtime.kinematics
move = _MoveExecutor(robot_runtime=runtime, kinematics=kin)
press = MoveLinearAction(robot_runtime=runtime, kinematics=kin)
push_pull = MoveLinearAction(robot_runtime=runtime, kinematics=kin)
```

默认模型的 FK/Jacobian 独立计算，MuJoCo 仍负责场景状态、碰撞及执行。切换 IK 库使用 `backend`/`backends`；Move 请求中的 `strategy_params.ik_method` 会映射到 `backend`。`solve_one()`/`solve_all()` 返回 `IKResult`，旧执行接口 `solve()`/`solve_candidates()` 已移除；上表原生 IKFast 的 `solve()` 是库内部接口。

`SkillRuntime(registry, runtime_config=None, *, kinematics_factory=None)` 把主服务传入相关技能和接近点验证。工厂应为每个传入运行时创建独立服务，不能让并行验证线程共享一个全局实例；直接注入自定义接近点服务时，也需提供工厂创建工作实例。

模型协议、完整加载/注入签名和返回值见 [运动学接口清单](C:/Users/21474/Desktop/robot-agent/KINEMATICS_INTERFACES.md)；引擎边界见 [MuJoCo 依赖清单](C:/Users/21474/Desktop/robot-agent/MUJOCO_DEPENDENCIES.md)。

## 6. 相关源码

- 演示命令说明：[`demo/SKILLS.md`](demo/SKILLS.md)
- 命令转换与分发：[`demo/task_commands/converter.py`](demo/task_commands/converter.py)、[`demo/runtime.py`](demo/runtime.py)、[`demo/run_demo.py`](demo/run_demo.py)
- 场景注册表：[`demo/common/registry.py`](demo/common/registry.py)
- Skill 定义：`atomic_action/motion/common/`、`atomic_action/perception/location/`、`atomic_action/perception/search/`、`atomic_action/manipulation/`
- 第三方适配约定：[`libraries/README.md`](libraries/README.md)


