# 执行后端与同场景验证

本目录将轨迹执行和物理仿真封装为插件。FK / Jacobian、Kinematics、SimPlatform FCL 和 Planning 继续使用现有实现；`MoveSkill` 通过注入接口调用它们。

## 数据流与资源管理

```text
commands.json：scene_id + scene_version + robot
    ↓ SceneResourceCoordinator.resolve()
assets/catalog.json → 场景 JSON + URDF + Mesh
    ├→ URDF Kinematics → MoveSkill / 现有 Planning → SimPlatform FCL → q(t)
    └→ backend_resources["mujoco"] → MuJoCoExecutionBackend.load()
                                      ↓ 从同一 JSON / URDF 生成 MJCF
                                execute_trajectory(q(t))
                                      ↓ 实际关节与物体状态
                           SceneStateSynchronizer → Kinematics / FCL
```

- `SceneResourceCoordinator` 校验任务、目录和场景的 ID / 版本，解析同一资产根目录，核对机器人 URDF、关节顺序、初始状态、基座、TCP 和环境物体位姿。
- `backend_resources` 保存后端专属配置。MuJoCo 使用 `format="generate_from_scene"`，插件每次从源场景生成 MJCF，无需手工维护第二份场景 XML。`options.viewer` 可设置 `lookat`、`distance`、`azimuth`、`elevation`；物理参数当前在插件中固定。
- `SceneStateSynchronizer` 在步骤开始、步骤结束和执行采样帧处，将实际关节状态更新到 Kinematics、物体世界位姿更新到 FCL。执行后的仿真状态是权威状态。
- 场景版本由资源维护者显式更新。报告中的指纹覆盖场景、机器人 URDF、直接引用的环境资产和后端配置；尚未递归覆盖 URDF 的所有 Mesh 依赖，不能作为完整资产缓存键。当前每次重新生成执行场景。

## 主要接口

| 接口 | 输入 / 输出及用途 |
| --- | --- |
| `SceneResourceCoordinator(catalog).resolve(task, backend="mujoco")` | 返回 `ResolvedResources`，包含规范场景、URDF、资产根目录、初始关节和选定后端配置 |
| `validate_consistency(resources, kin, checker, backend)` | 校验初始化后两侧资源、位姿和 FK / TCP 的一致性，返回误差报告 |
| `ExecutionBackendRegistry.register(name, factory)` | 注册执行后端工厂；创建 `mujoco` 时才导入 MuJoCo 插件 |
| `ExecutionBackendRegistry.create(name)` | 返回执行后端；未知名称报错 |
| `backend.load(resources, output_dir)` | 加载机器人、场景和初始状态，生成后端所需文件 |
| `backend.get_state()` | 返回 `joint_names`、`joints`、`base_transform`、`simulation_time`、`objects` |
| `backend.joint_limits` | 与 `joint_names` 同序的 `(DOF, 2)` 关节上下限，单位 rad |
| `backend.get_joint_positions()` | 返回实际关节向量 |
| `backend.get_end_effector_pose()` | 返回世界系 TCP 的 `position`、`rotation_matrix`、`quaternion_wxyz` |
| `backend.get_context(request=None)` | 给现有 MoveSkill 提供当前关节、末端位姿和控制器状态 |
| `backend.execute_trajectory(trajectory, timeout=30.0)` | 执行时间参数化关节轨迹，返回实际终态、误差、物理步数和采样帧 |
| `backend.set_frame_callback(callback)` | 注册 `callback(state)`；每个约 60 Hz 的仿真采样帧调用，`None` 取消 |
| `backend.open_viewer()` / `close()` | 打开可选查看器 / 释放后端资源 |
| `backend.set_status(status)` | 更新查看器左上角状态文字，无窗口时只保存状态 |
| `SceneStateSynchronizer().sync(backend, kin, checker)` | 将实际状态同步到规划侧，并返回该状态 |

`ExecutionBackend` 协议定义在 `registry.py`。其他插件实现同一接口，并在目录里增加相应 `backend_resources`，即可复用资源解析和技能调用。当前只实现了 MuJoCo 后端，尚未验证第二种仿真器。

执行输入中的 `time_parameterized_waypoints` 为 `N × DOF` 关节角数组，`timestamps` 为从 0 开始的秒数数组；两者逐行对应。重复时间戳只有在对应位置一致时才会合并。执行前检查维数、有限值、时间顺序、关节限位及起点是否匹配当前状态。

当前 demo 统一使用 `planning.time_parameterization.prepare_continuous_trajectory()`：平滑关节路径后为整段运动分配连续时间表，只在阶段起止处停稳。每关节速度基准由 URDF limit.velocity 读取，加速度优先读取 limit.acceleration；缺失时按约定采用速度上限数值作为加速度基准，并在报告中标记回退来源。两者分别乘 `velocity_scale` 和 `acceleration_scale`；速度还可以通过 `planning.time_parameterization.joint_velocity_multipliers` 按关节调整，最终限值为 `min(URDF 上限, URDF 上限 × velocity_scale × 关节倍率)`。例如 `[1,1,1,2,2,2]` 会让 J4–J6 在全局比例不变时加速到两倍，超出 URDF 上限则截断。生成的平滑路径在执行前重新校验碰撞。推拉共用同一连续轨迹实现和全局限值；夹爪开合独立控制。当前不约束 jerk，Ruckig 接口仍保留但不在 demo 执行链中使用。

MuJoCo 使用位置执行器、偏置力补偿和 `mj_step`，步长 0.002 s。仅初始化时设置关节位置，运行中通过执行器跟踪轨迹。回调时间是仿真时间，计算耗时可能导致实际运行速度低于实时。

规划和执行前复检通过 FCL 适配器复用满足相同场景状态、规则、路径和采样要求的验证结果；原有 0.04 / 0.01 rad 两套检查都保留。计时中的碰撞成本会更多体现在 `planning`，执行前的回调通常命中缓存。报告的 `collision_cache_stats` 可用于确认命中和失效，详见 [碰撞缓存说明](../collision/README.md)。

## 测试场景与指令

新增 ObjectLibraryServer 交互定位示例：`demo/skill_command/ols_grasp.commands.json` 配合 `--start-object-library`。命令为 `move 101 → move home`；demo 通过场景实例 ID 查询模型的 `grasp_properties` 交互位姿，换算夹持中心与控制 TCP，并尝试生成“到接近点 → 保持姿态直线到目标”两段请求，再通过原有 MoveSkill 接口执行。详细配置见 [服务接入说明](../services/README.md)。底层技能不查询服务，夹爪仍固定张开。

项目里技能清单实际文件名为 `demo/SKILLS.md`。本次测试按其 `locate` / `move` 命令结构编写：

- 场景：`assets/scenes/unified_arm_test.json`。
- 基础布局回归任务：`tests/fixtures/unified_arm.commands.json`。
- 资源目录：`assets/catalog.json`。
- 入口：`demo/run_demo.py`，使用新场景，不使用旧的 `config_002.commands.json`。

历史场景版本 4 曾参考 `world_model/robotsim/scene_000.xml`，该旧场景已移除：机器人基座位于 `[0, 0, 0.5]`，桌子 URDF 原点位于 `[0.525, 0, 0]`，机械臂安装在独立圆柱底座上。底座半径 0.14 m，顶部比原 XML 低 2 mm，以满足碰撞裕量。机械臂携带固定张开的 Robotiq 2F85；不含相机附件、苹果或测试盒。

机械臂与夹爪信息来自初始化时的最终场景：基础场景先应用 `demo/config/*.runtime.json`，再从唯一 `RobotModelRef` 和 `RobotAttachment` / `GripperModel` 读取 URDF 与安装关系。`execution/assembly.py` 在内存中拼接模型树，不生成组合 URDF；旧组合 URDF 和生成脚本已删除。catalog 只负责场景、runtime 配置和后端资源，不再选择机器人型号。

运动学和 FCL 使用原始六轴机械臂模型；MuJoCo 通过内存模型和夹爪适配器恢复活动关节，负责活动夹爪及被抓物体几何的补充碰撞检查。控制 TCP 仍为 `attachment_site`，抓取中心由安装关系和 `grasp_frame` 换算。物理模式支持真实开合与接触携物，绑定模式支持闭合确认后启用 weld。后端仍可输出 MJCF 缓存。

基础布局回归任务仅两步：IK 求解并移动到 `test_move`（世界位置约 `[0.314, 0.278, 1.118]` m）→ 返回 `home`。初始状态及 home 均取原 XML 前六个关节值 `[0, -2.094395, 1.570796, -1.5707963, -1.5707963, 0]`。当前 `demo/skill_command/unified_scene.commands.json` 已切换为苹果场景的 `move 101 → move home`，与 `ols_grasp.commands.json` 一样需要服务，运行时可加 `--start-object-library`。

此前包含定位、直线上抬、碰撞探针和动态方块的五步场景保留在 `tests/fixtures/unified_scene_v2/`，用于更广的回归测试。

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands tests\fixtures\unified_arm.commands.json `
  --library "libraries\simplatform\bin\sim_platform_c.dll"
```

加 `--viewer` 可打开 MuJoCo 窗口。`--collision-probe` 仅适用于包含 `probe_block` 的旧五步测试数据；使用时还需指定 `--catalog tests/fixtures/unified_scene_v2/catalog.json --commands tests/fixtures/unified_scene_v2/commands.json`。当前两步场景不使用该参数。

查看器默认对准桌面机器人、隐藏两侧菜单并显示当前步骤和仿真时间。规划期间姿态保持不动是正常的；状态会显示 `Planning` 或 `Executing`。URDF 的简化碰撞几何分配到默认隐藏的第 3 组，避免覆盖可视 Mesh，物理碰撞仍然生效。回到 home 后姿态与初始时相同。`success` 表示执行完成，报告另记录 `viewer_sync_count`；自动测试还比较实际执行采样的渲染图像，防止只验证数值。

SimPlatform 基本体的尺寸编码在原生 `Transform.scale` 中。FCL 适配器在加载及更新位姿时保留这部分尺度，资源协调器额外核对盒体尺寸。物体状态同步忽略小于约 0.1 微米的平移抖动，以避免静止物体触发昂贵的重复更新；累计变化仍相对上次已发送位姿判断。

其他参数：`--catalog`、`--backend`、`--output`。也可通过 `SIM_PLATFORM_LIBRARY` 指定 DLL。DLL 的依赖文件应保留在其原目录；SDK 来自此前提供的 SimPlatform 压缩包，默认位于 `libraries/simplatform/source/SimPlatform-main/python`，也可用 `SIMPLATFORM_PYTHON_ROOT` 指定。这里没有使用 `python-fcl`。

Windows 下这份 SimPlatform 发布包也包含 `mujoco.dll`。入口先加载所选 MuJoCo 插件，再初始化 SimPlatform 的 `physics="none", collision="fcl"`，避免同名 DLL 的加载顺序冲突。

默认输出在 `tests/output/unified_scene/`：

| 文件 | 内容 |
| --- | --- |
| `report.json` | 整体结果、当前步骤、错误阶段、各步规划轨迹与执行指标、一致性报告 |
| `frames.jsonl` | 每行一个实际执行采样帧：帧号、command_id、仿真时间、关节及物体位姿 |
| `mujoco/scene.mjcf.xml` | 从本次规范场景和资产生成的 MuJoCo 场景 |

失败时停止后续步骤，保存报告，并以非零退出码结束。文件用于本地验证；尚未连接大脑 / 前端网络接口，也未转换为既有播放器的 `.traj` 格式。

## 验证范围

自动测试命令：

```powershell
$env:SIM_PLATFORM_LIBRARY = "libraries\simplatform\bin\sim_platform_c.dll"
.\.venv\Scripts\python.exe -B -m unittest tests.test_unified_execution tests.test_eaik_urdf tests.test_eaik_backend tests.test_kinematics_plugins
```

测试覆盖任务与资源匹配、禁止 MuJoCo 导入时加载独立运动学和 MoveSkill、插件注册、真实 FCL 碰撞反例、初始环境碰撞拒绝、五步任务执行、实际采样轨迹检查、关节和 TCP 到达误差，以及动态方块落地。FCL 仅忽略 URDF 同一连杆及相邻连杆之间的结构接触，不把初始化时的环境碰撞加入允许名单。

本轮场景使用六轴 UR5e 固定基座机械臂和固定张开的夹爪。支持世界坐标下的单位缩放物体、箱体、圆柱及静态 URDF / OBJ / STL 环境资产。箱体 `size` 为完整三维尺寸，圆柱 `size` 为 `[半径, 半径, 完整高度]`。有铰链的环境 URDF、移动基座、可驱动夹爪和抓取附着关系需要后续适配。尚未实现暂停、恢复、取消和完整 TaskOrchestrator。

运动测试走现有直接规划分支，未覆盖 RRT 绕障成功场景。执行期间按约 60 Hz 对实际关节及当前物体位姿做 FCL 检查，额外检查实际采样关节路径；这不是连续碰撞检测，也不预测运动障碍物。MuJoCo 与 FCL 读取相同几何源，但各自的 Mesh 接触算法可能不同（如 MuJoCo 的凸包处理），本测试不声称任意 Mesh 接触结果完全一致。




