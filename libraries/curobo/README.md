# cuRobo 可选运动模块

规划器适配器为 `planning.planners.curobo.CuroboPlanner`，官方运行时实现位于本目录。在统一 `demo.run_demo` 入口中，默认先验证直接路径；仅在直接路径碰撞且允许改道时使用 cuRobo。也可以通过 `planning.planner_type: curobo` 显式直接选择。cuRobo 失败时不会退回 RRT。OLS 和原始资产均只读。

## 文件职责

- `source/`：未经修改的 NVlabs/cuRobo 官方源码，固定提交 `78fd485fa82d9b9a063fb4985e371814587e666a`。`UPSTREAM.json` 记录来源和下载包校验值；`verify_upstream.py` 按 `UPSTREAM.files.json` 核对全部 890 个文件。
- `conversion/`：通过 OLS 下载资产及引用文件，读取当前 JSON，计算关节、层级、挂载与碰撞几何；生成独立装配 URDF。
- `config/`：转换设置、必要差异和已测试 Windows/CUDA 依赖版本。官方规划任务 YAML 在 `source/` 中原样使用。
- `client.py`、`worker.py`：隔离依赖的持久 GPU 工作进程；官方 RobotBuilder 拟合及保存机器人 YAML，官方 Scene.create 解析场景，官方 MotionPlanner 规划。

daemon 会按机器人模型和规划配置缓存多个 `MotionPlanner`。默认最多保留 2 个 planner；切换回仍在缓存中的机器人时不会再次创建或 warmup。可在 `config/defaults.json` 中调整 `planner_cache_size`。缓存淘汰只销毁对应 planner，不会停止 worker；场景变化仍通过 `update_world` 应用，只有碰撞体数量超过已分配容量时才会重建该 planner。
- `runtime_scene.py`：将本项目执行后端的实时状态读成当前场景副本。
- `demo_plan.py`：无可视化、只输出轨迹的示例。
- 生成物统一在项目 `.cache/curobo/`：下载资产、装配 URDF、YAML、拟合报告、GPU 编译缓存、工作进程日志和隔离 Python 依赖。原始资产路径不会作为交付依赖。

## 只规划，不执行

```python
import json
from planning.planners.curobo import CuroboPlanner

scene = json.load(open('current_scene.json', encoding='utf-8'))
request = {
    'target': {
        'type': 'pose', 'frame': 'world',
        'position': {'x': 0.3, 'y': 0.1, 'z': 0.8},
        'orientation': {'representation': 'quaternion', 'w': 1, 'x': 0, 'y': 0, 'z': 0},
    },
    'motion': {'path_type': 'joint'},
    'planning': {'planner_type': 'curobo', 'max_attempts': 5},
    'constraints': {'safety_distance': 0.003},
}
with CuroboPlanner(scene=scene, robot_object_id=2) as move:
    move.prepare(request=request)       # 启动时转换、拟合、初始化和预热，不属于每次规划
    result = move.plan(request)          # 只返回轨迹，不创建仿真，不执行运动
    # 下一次从调用方获取完整当前场景：request['scene'] = new_current_scene
    # 或初始化时使用 scene_provider=read_current_scene，每次调用自动读取。
```

位姿、目标是否可达取决于实际场景，示例坐标不是所有场景通用的成功目标。`request['scene']` 优先于构造时的 scene；调用方传入的 JSON 不会被写回，也不会用 OLS 的初始场景替换它。OLS 只用于补齐模型资产。

支持现有 Move 的 pose、joint 和 named_state 目标；joint 使用场景 `joint_sequence` 顺序。姿态接口与项目一致：四元数用命名的 x/y/z/w 分量；RPY 用度。场景文件自身的关节转角仍为弧度，移动关节为米。世界坐标和机器人基座坐标可通过 `target.frame` 选择。

多机械臂必须指定 `robot_object_id`。工具优先使用本项目 `RobotModelRef.tip_link` 和 `tcp_transform`，与执行后端的 control/TCP 一致；该定义不是 `grasp_frame`。原始 UR5 场景没有这些扩展时暂用 `attachment_site` 并在诊断中注明；其他机器人必须提供工具定义。例如构造参数：

```python
tool={'object_id': 3, 'link': '实际工具link',
      'position': [0, 0, 0.12], 'quaternion_xyzw': [0, 0, 0, 1]}
```

## 接入已有演示入口

在原命令的 `parameters.request` 中可以显式增加：

```json
{"planning": {"planner_type": "curobo"}, "motion": {"path_type": "joint"}}
```

`demo/run_demo.py` 延迟创建并复用显式选择的 `CuroboPlanner`，读取执行后端当前关节、夹爪、机构和物体状态，规划后调用原 `execute_trajectory()`。直接路径碰撞时也可经 `CollisionFreePlanner` 调用 cuRobo。cuRobo worker 在 GPU 上验收最终参考轨迹，宿主核对实时状态签名，不再重复几何终验或重新拟合轨迹。

GPU worker 支持手动管理的本地 Unix-socket daemon。先执行 `python -m libraries.curobo.daemon start`，再从多个独立的 `python -m demo.run_demo --gpu` 进程运行任务；这些进程会复用同一个 worker。未启动 daemon 时，`run_demo` 自动退回临时 worker，因此行为仍与原来一致，只是每次运行都会冷启动。任务仍向 worker 发送完整运行时场景；可复用时更新已有 planner，机器人模型或规划配置变化时由 worker 重建 planner。结束后执行 `python -m libraries.curobo.daemon stop` 释放 CUDA context 和显存。

也可以用运行模式命令控制桥接层或其他未携带命令行参数的任务入口：

```bash
python -m libraries.curobo.mode gpu     # 写入 GPU 默认模式并自动启动/复用 worker
python -m libraries.curobo.mode cpu     # 写入 CPU 默认模式并关闭 worker、释放 CUDA context
python -m libraries.curobo.mode status
```

模式保存在 `.cache/curobo/mode.json`。`demo.run_demo` 未指定 `--gpu` 或 `--cpu` 时读取该模式；显式传入命令行参数仍只对当前任务生效。

GPU 直线路径逐点以上一路点关节解和附近扰动求多种子 IK，优先采用满足 `max_joint_step`（默认 0.4 rad）的近邻候选。若时间参数化的整条轨迹未通过 GPU 碰撞／关节约束或 TCP 直线校验，会回溯改选构型（最多校验 32 条完整路径、求解 128 次 IK，受规划时间预算限制）。不使用 TrajOpt。

实时代理保留冻结策略中的结构接触豁免、持物规则和逐对阈值。上臂和 Robotiq 垫片的相关凸网格使用网格局部坐标铺球，剔除能证明完全在凸包外的单元；针对已复现误报的上臂凸块和垫片使用更密的覆盖。GPU 检查所有未豁免的自碰撞及世界碰撞。终验按关节步长离散采样，最多 0.01 rad，`gpu_collision_validation` 记录样本数和分辨率。球近似仍可能受形状影响，离散检查不等于连续碰撞检测。

运行时配有 `GraspCollisionChecker` 时，适配层读取实测夹爪构型、持物相对变换及当前接触策略，支持持物的关节空间避障。严格直线按上述逐点 IK 和全程复核处理；圆弧和自由空间运动的全程姿态约束仍未接入 cuRobo 避障适配层，碰撞后不会放宽硬路径。没有实时几何及策略的普通 JSON 入口仍不支持持物／目标接触豁免。

### 运行时碰撞策略

`collision/planning_policy.py` 从原检查器的有效 articulated pair builder 冻结允许对、支撑面阈值和 lift 初始距离，不把 FCL 向实时几何交接的过滤项当成真实豁免。`runtime_policy.py` 和 `conversion/runtime.py` 生成逐碰撞几何标记的运行时 URDF／障碍物，抓住的自由物体及子树按实测变换固定到夹爪；释放后的下一份快照自动恢复世界障碍物。非自由关节机构仍按原规划探针的静态快照处理。

当前保留既有行为：pick 全程夹爪—目标接触；持物时夹爪—持物接触；独立 retreat 夹爪—全部环境接触；lift_up 全部持物碰撞排除及初始接触不加深；支撑面不足约 1 mm 的穿透容许量。这些宽泛豁免没有被迁移代码收窄。

当前 sorting 场景还显式允许机械臂主体与安装底座 `RobotPedestal` 碰撞。源场景在 `RobotDriver.allowed_arm_environment_names` 声明对象名，装配后进入 `RobotModelRef` 同名字段；原 FCL／MuJoCo 检查器和 cuRobo 使用相同的逐几何允许对。范围包含底座对象的碰撞子树，但不包含机械臂挂载的夹爪或持物。夹爪／持物与底座仍按当前技能的其他既有规则检查，不会因这条新规则获得豁免。

当前场景离线装配及原生 FCL 检查器验证得到 36 对原生允许对、9 对 MuJoCo／cuRobo 几何允许对（两者碰撞几何拆分不同）。肩部 geom 20 与底座 geom 143 明确豁免；80 个夹爪几何及 72 个持物几何与底座仍保留 3 mm 阈值。验证未发送运动命令。源配置在场景重新加载时生效。

`policy_checker.py`／`policy_kernels.py` 在 GPU 的普通及 swept 查询中应用逐几何对距离表，IK、图搜索、轨迹优化和内部可行性检查使用同一个 SceneCollision。不使用默认姿态碰撞自动排除。策略表原位更新；几何容量或自碰撞矩阵改变时重新建立规划器，策略改变时清空图搜索缓存。场景／抓持／策略签名在捕获和规划后校验。

查询按完整轨迹批次分批发射，避免超大场景的扁平线程索引溢出；AABB 预筛覆盖整个 swept 中心线段。规划前使用官方直接 metrics rollout 检查起点及关节目标；不可行时明确拒绝，不自动缩小安全距离、扩大允许对或删除几何。若当前策略允许全部自碰撞对，配置为空检查集合，避免调用不支持空集合的官方自碰撞内核。

完整运行时场景在通用 pose IK／LBFGS 预热的 CUDA Graph 回放中复现非法内存访问，因此该入口默认使用 GPU eager 执行（`conversion.runtime_cuda_graphs=false`），跳过额外通用预热（`runtime_warmup_iterations=0`），在实际请求中初始化所需求解路径。显式要求预热时，关节目标只做 cspace 预热。原 OLS JSON 入口保留官方 CUDA Graph 和预热设置。cuRobo 的路径图搜索仍可使用，关闭的仅是 CUDA Graph 执行加速。

运行时轨迹优化默认每批 1 个种子（`runtime_num_trajopt_seeds=1`），保留请求的 `max_attempts`；这是搜索规模调整，可能影响其他场景的成功率，未改变任何几何、碰撞阈值或后验检查。可显式提高种子数，但必须相应评估耗时。执行模式、种子数和预热策略均写入诊断。重新打开运行时 CUDA Graph 仅用于进一步验证，当前完整 pose IK 捕获路径尚未修复或验收。

运行时球体几何直接使用精确单球；其他逐几何拟合默认使用官方 VOXEL，避免对大量小型凸分解块逐一做 MorphIt 多次优化，可用 `conversion.runtime_sphere_fit_type` 选择官方拟合类型。模板按几何形状缓存，改变抓持变换无需重新拟合同一形状。普通 OLS JSON 转换仍使用下述原默认值。

规则语义一致不等于几何距离逐点相同。机器人及持物仍由球近似；体素／薄片 surface fallback 可能产生漏覆盖或突出，可能拒绝原检查器允许的窄缝或接触路径。原检查器的离散复核也不是连续碰撞保证。无限碰撞平面等尚不支持的运行时几何直接报错，不静默省略。当前 GPU 策略入口仅支持单规划环境。

GPU 距离比较使用 `1e-7 m`（0.1 µm）数值容差，处理精确 3 mm 边界在 float32 下约 17 nm 的正误差；1 µm 的实际间距不足仍拒绝。请求的 3 mm 间距与宿主最终检查阈值保持不变。

诊断字段 `trajectory.diagnostics.curobo.collision_policy` 包含策略指纹、允许对／距离覆盖数量、持物名称、持物装配 body、实测几何来源及 CUDA Graph 标志。

验证命令：

```powershell
.\.venv\Scripts\python.exe -B -m unittest planning.tests.test_curobo_runtime_control planning.tests.test_curobo_policy planning.tests.test_curobo_policy_adapter planning.tests.test_curobo_avoidance -v
$env:ROBOT_AGENT_CUROBO_GPU_TESTS='1'
.\.venv\Scripts\python.exe -B -m unittest planning.tests.test_curobo_policy_gpu -v
```

GPU 测试在隔离工作进程规划六关节测试模型，不向机器人或演示执行器发送运动命令。

本次运行时策略验收与下文的历史 OLS 验收分开：38 项宿主测试及 10 项 GPU 测试覆盖碰撞策略、距离边界、梯度、CUDA Graph 查询、持物规划、场景恢复、进程替换及分阶段硬超时。另在记录的完整场景（161 个机器人／持物几何、250 个环境几何、1234 个拟合球）恢复末帧关节及物体状态，通过公共规划入口向记录的 Tray 目标做冷、热两次离线规划：均生成 81 点轨迹，并通过原 FCL／MuJoCo 检查器复核；求解分别约 5.42 s、4.08 s，保持 10 s 求解预算。冷启动含初始化及后验检查总计约 43.38 s，复用后约 9.60 s，首次仍有初始化等待。两次均保留 3 mm 阈值和 9 对机械臂—底座豁免。只验证了该记录快照，未执行运动或验收任意 Tray 状态，也不覆盖此前的单关节大幅运动问题。

本次保持原项目六轴 Move 校验和执行协议；七轴机器人的通用转换与六轴项目执行入口是不同范围，本入口不宣称已经支持七轴执行。

## 返回值

`success/status/selection/trajectory/execution_result/timing/error` 与现有 Move 对齐。`plan()` 和没有 robot_runtime 的 `execute()` 只规划，`planned_only=true`；给定 robot_runtime 时，`execute()` 才执行。

轨迹包含：

- `joint_names`：机械臂原关节名称，顺序与执行器一致；锁定的夹爪关节不混入输出。
- `waypoints`、`time_parameterized_waypoints`：同一批官方插值关节位置。
- `timestamps`：从零开始的秒数，与每行位置对应；另有 duration、velocities、accelerations、jerks。
- `planning_time`：官方规划调用及 GPU 完成等待时间。
- `diagnostics`：官方版本、工具、转换设置、碰撞规模、当前 TCP/基座、官方求解时间及缓存使用情况。

全局和请求速度/加速度比例按原项目读取。比例通过时间拉伸实现，位置不变，速度/加速度/jerk 按相应幂次缩放。规划的尝试次数使用官方 max_attempts；父进程收到 planning 阶段事件后按 `planning_time` 硬限制求解等待，原结果预算检查也保留。初始化默认最多 60 s（`runtime.initialization_timeout`），单次 worker 操作默认总上限 120 s（`runtime.request_timeout`），请求含 `constraints.timeout` 时取更严格的总上限；阶段事件不会延长截止时间。首次下载、拟合和初始化另外记录在 timing.conversion/preparation 中，阶段及累计耗时写入 worker 日志。超时会终止整个 worker 进程树（包括 Windows venv 启动器的解释器子进程）；CUDA 致命错误先报告实际失败阶段，再直接退出，下一次调用必须建立新进程，不复用已损坏的 CUDA 上下文。不会自动放宽策略或切换 RRT。

## 转换约定和必要差异

1. `initial` 与 `now` 原字段分别保留，不修改输入；只在派生状态中按关节读取当前有效值。未知关节、非法数值、越界状态均报错，不悄悄夹紧或跳过。未声明值按初始值/新建 URDF 零位处理，不保留未知历史状态。
2. 有效 RobotAttachment 决定挂载位置与姿态；对象父链和 URDF link FK 一并计算。多级 mimic 在装配缓存中等价合成为原独立驱动关节的直接关系，乘数和偏移一起合成，原文件不变。
3. 当前非计划机械臂、柜门和抽屉按当前关节状态作为静态障碍物；下一次调用重新计算。保留逐 link 碰撞几何、网格和空腔，无整物体包围盒退化。缺 collision 的环境 link 使用该 link 的 visual 并记录；几何缺失/不支持时报错。含几何资产的相机仍参与碰撞。纯灯光/无模型相机不会凭空生成碰撞盒。
4. 机器人球拟合使用官方默认 density=1、visual 几何、默认拟合方法；自碰撞矩阵使用 prune_collisions=True。官方保存器完整生成 cspace，包括两项权重，不用不完整 cspace 覆盖它。
5. 当前验证过的 OLS robot/1 基座网格最低点为局部 z=0。为避免拟合球伸入安装面，使用官方 clip_links 功能，并在首次生成时再次检查实际网格不伸到平面下。只约束固定基座，不裁移动连杆。请求的安全距离加到移动 link 球半径；固定根 link 在安装接触处保留零额外间距，仍保留其碰撞球和底座障碍物。
6. 源模型和场景的碰撞表示与真实物体可能仍有误差；官方球拟合也是近似表示。细手指触发官方 voxel/surface fallback 的报告保存在 fit_*.json/worker.log；不能据此宣称任意窄缝均已验证。
7. 已有资产在会话内缓存。只有场景当前位姿变化时不用重新下载/拟合；OLS 资产文件本身变更后调用 `move.client.refresh_assets()` 或重启动作。改变机器人装配、工具、碰撞边界或规划容差会重新初始化对应规划器；日志/计时会体现，不会假装为一次热规划。
8. 原项目 `RobotModelRef.allowed_collision_link_pairs` 在装配中严格解析到唯一 link，通过官方 `compute_collision_matrix(custom_ignore=...)` 保留。不存在或含糊的名称会报错，不扩大例外范围。
9. `Scene.create()` 之后调用官方 `get_collision_check_world()`，把该版本底层尚未直接检查的 sphere/cylinder 等几何转成 mesh，保留几何后再交规划器。柜门/抽屉按当前关节做逐 link 变换；不预测本次运动期间的外部物体运动。

### 安全间距的已知限制（原 OLS JSON 入口）

`safety_distance` 通过机器人球半径生效，也会影响未被官方/项目装配例外排除的自碰撞对。本机 scene/1 张开夹爪在额外 3 mm 间距下，夹爪 base 与 coupler 的拟合球会发生内部冲突，官方规划器拒绝；同一完整场景以 0 额外间距可规划并交原执行器完成运动。0 表示保留原始拟合边界，不表示关闭碰撞检查。

`python -m demo.run_demo --commands commands.001.json --gpu` 将自动规划请求交给 cuRobo（joint/pose PTP 和 pose 直线）；不加 `--gpu` 时仍由 CPU direct→RRT 处理。显式 `planning.planner_type` 覆盖自动路由。GPU 直线按距离和 `cartesian_step` 动态选取 IK 路点（至少 2 点，不超过 `trajopt_solver.action_horizon`），依次求解碰撞感知 waypoint IK，选取最靠近上一个关节解的候选；不调用 TrajOpt。相邻关节解以每段起止速度和加速度为零的五次曲线定时，在稠密采样上用当前 cuRobo 世界、自碰撞和豁免规则验证，并检查 TCP 的直线与姿态。速度、加速度、jerk 按机器人限制和请求缩放；离散采样不是数学上的连续碰撞证明。不满足直线/姿态容差、碰撞或关节限制时直接失败，不退回 CPU。当前单次直线最多 `trajopt_solver.action_horizon-1` 个 `cartesian_step`（默认 0.02 m）；长距离请拆成多段。圆弧仍只支持 CPU。

GPU 撤退有多个候选距离时，先分批用 cuRobo 检查终点 IK。按请求优先级一次只为一个通过终点筛选的候选生成并求解直线路点，整段未通过才尝试下一个；终点 IK 成功不代表整段可行。诊断字段 `linear_target_batch` 分别记录终点筛选批数、检查的终点数量，以及实际推进的候选和各自的路点 IK 用量。

GPU `approach` 用张开夹爪的快速几何预检选定抓取位姿，只规划和验证实际执行的接近轨迹，不提前求抓取终点 IK 或预演接触路径。随后对同一目标的 GPU `pick` 从实测关节状态规划并验证接触直线；规划失败时不切换关节路径、不闭爪。接近成功不代表抓取轨迹可行。

GPU 关节目标先构造受速度、加速度及 jerk 约束的关节直达轨迹，按关节步长采样并用当前 cuRobo 场景与碰撞策略验证；通过则跳过 TrajOpt，GPU 检查失败或采样容量不足时才调用 `plan_cspace`。直达和绕行共用最终轨迹复核。

MuJoCo demo 的 `--gpu` 默认采用 RoboTwin 式固定场景（`--collision-probe` 除外）：首次 GPU 规划固定机器人和夹爪的碰撞模型，环境只保留 `Table` 或 `WorkTable`；后续沿用该模型，只刷新机械臂关节角和 TCP。GPU 仍验证整条轨迹，但不会检查持物、夹爪开度变化或其他场景物体，因此此模式仅用于仿真，不代表真实场景可安全执行。

固定场景模式在全局感知前初始化并预热单目标位姿规划和四路批量 IK，不执行预热轨迹。启动阶段的总耗时单独打印为 `timing|curobo_startup_warmup=...`，不计入首条命令的规划时间。CUDA Graph 设置不变。

运行时代理默认关闭 CUDA Graph，使直线与关节直达共用同一个 cuRobo 模型，避免切换目标类型时重新初始化；需要为复杂 `plan_cspace` 单独启用 Graph 时可覆写 `runtime_proxy_cuda_graphs`。

运行时 URDF 的安装位姿按位置 1 µm、角度 0.0001 rad 量化后作为 cuRobo 模型缓存键，避免夹爪的微小仿真抖动触发每条指令重新拟合与初始化；超出该精度的真实几何变化仍生成新模型。宿主碰撞终验仍使用未量化的运行时几何。

模块不会自动降低原项目默认的 3 mm。调用方需要结合真实模型选择间距；若必须保证更大间距，需要另行校准夹爪碰撞表示/内部碰撞规则。本次没有通过增加忽略对来消除此问题。

## 环境与换显卡

Windows 运行环境使用 Python 3.10、PyTorch 2.9.1+cu128 和 Warp 1.18.0。CUDA 12.8 构建用于覆盖 RTX 50 系列的 Blackwell `sm_120` 架构；旧的 CUDA 12.4 构建不包含该架构。实际完整依赖记录在 `.cache/curobo/environment.json` 和 installed-requirements.txt。不修改原 conda 环境；独立依赖在 `.cache/curobo/site-packages`，只供常驻 GPU 子进程加载。

在另一台 Windows 电脑中，先使用 Python 3.10 运行：

```powershell
python libraries/curobo/setup_runtime.py
python libraries/curobo/check_runtime.py
python libraries/curobo/verify_upstream.py
```

需要 pip 能访问 PyPI、NVIDIA 和 PyTorch CUDA 软件包源。该安装文件固定的是本机测试版本，不保证覆盖未来显卡架构。新显卡可能要求更新的驱动及 CUDA PyTorch/Warp；升级时应在隔离依赖中重新验证固定官方源码，不能直接覆盖项目主环境。Linux 应重新安装对应平台依赖，不能复制 Windows 二进制。

同一受支持 NVIDIA 架构范围内，通常不用改场景、转换或规划代码。换机器时不复制 .cache 中的 GPU 编译缓存；让新机器重新编译和预热。显卡显存较小时可能不足，不能自动靠删碰撞球、删障碍物或关闭检查解决。AMD/Intel 显卡和纯 CPU 不支持本 CUDA 入口。

多卡选择可在 `.cache/curobo/runtime.json` 加 `"cuda_visible_devices": "1"`；子进程内部仍使用 cuda:0，该编号映射到选择的物理卡。runtime.json 中 python 是机器本地路径，新电脑运行 setup_runtime.py 会重新生成。OLS 地址在 scene.object_library.base_url，或传 `config={'runtime':{'ols':{'base_url':'http://服务器:8000','uid':0}}}`。

命令行规划（场景由调用方提供，不自动从 OLS 取初始场景）：

```powershell
python libraries/curobo/demo_plan.py --scene current_scene.json --request move_request.json --output trajectory_result.json
```

可直接试用已验证的 OLS scene/4 快照（柜门当前开角 1 rad，资产仍从 OLS 读取）：

```powershell
python libraries/curobo/demo_plan.py --scene libraries/curobo/examples/current_scene.json --request libraries/curobo/examples/move_request.json --output .cache/curobo/example_trajectory.json --iterations 3
```

示例请求显式使用 0 额外间距，并保留完整碰撞检查。调用方实际使用时应传入最新完整场景，不能把示例快照当实时场景。第一次拟合和每次新工作进程预热可能较慢；应用应复用同一动作实例，并在接收任务前调用 `prepare()`。

## 统一简化代理试用（2026-11-18）

sorting 场景的 `planning_collision_proxy.enabled=true` 使 CPU／GPU 共用同一套
规划球和环境盒体。当前持物快照由 411 个源几何（161 个机器人／持物几何、250 个环境几何）
变为 42 个代理几何、350 个机器人／持物球和 20 个环境盒。夹爪细机构保留已验证的
272 个球模板，其他部分简化；未用大盒填掉夹爪空隙或容器内腔。

仅对这类代理的**关节目标**默认启用 `runtime_proxy_cuda_graphs=true`。完整精细
运行时模型和 pose 入口仍保留原 `runtime_cuda_graphs=false`。目标类型切换会按实际
执行模式重建规划器，不复用错误模式的捕获结果。可显式将代理 Graph 开关设为 false。

同一记录 `approach(Tray)` 目标，离线回放的完整热调用约 0.5 s，其中 GPU 求解约
0.14 s、首次代理路径复核约 0.18～0.20 s；每轮清空整路径缓存后测量，外层第二次
复核可复用同状态结果。首次代理生成约 1 s；新 worker 的首次完整调用约 10 s，含
初始化和捕获，模型／抓持装配变化也可能触发冷初始化。不是所有技能或场景的亚秒保证。

重新规划得到的新轨迹额外通过了原精细 FCL／articulated 的离线复核（约 5.2 s），
该精细复核不在默认规划关键路径。旧轨迹在简化包络下靠 Basket 过近，明确拒绝后
重新规划，没有通过放宽距离或增加豁免接受它。未执行机器人运动。

详细模型、包络误差及策略规则见 `collision/README.md`；实测数据位于本机生成的
`.cache/shared_proxy_verification.json` 和 `.cache/shared_proxy_state_verification.json`。

### approach 搜索预算修复（2026-11-18）

运行 `106744` 暴露了固定关节目标回放未覆盖的上层重试：10 s 被按 IK 构型数切分，
首次绕障只得到约 2.46 s 求解时间；超时杀掉 worker 后，下一个构型／距离又冷启动。
失败阶段还同时累加 `planning` 和包含它的 `total`，把实际约 132.7 s 显示成 255.7 s。

现在首次绕障使用剩余规划搜索预算，不按尚未尝试的 IK 构型数量切碎。新建规划器的
初始化时间单独记录在 `configuration_selection.initialization_s`，与 client 的初始化／
求解预算一致；整个请求仍受 `constraints.timeout` 限制，宿主最终复核超时也拒绝返回。
worker 超时／致命错误需要重启时，当前构型与 approach 搜索立即终止，下一次独立命令
才可重新启动。普通不可行失败仍保留健康 worker，并允许在剩余时间内尝试其他候选。

approach 的全部距离、相机对齐尝试和最终 move_to 回退共享一次有效 Move 请求的
`constraints.timeout`，后续尝试只获得剩余时间，且不会修改原请求。这个时限从
approach 距离搜索开始；此前的目标解析／放置高度选择仍单独计时。终端阶段计时优先使用
`planning`；只有缺少该字段的旧记录才使用 `curobo_attempt` 或失败阶段 `total`。

离线验证从运行 `106744` 的第 1211 帧重建机械臂／物体位姿及夹爪开口，指爪 mimic
关节由开口重建，没有回放物理接触或执行运动。两次完整距离搜索都在第一档 100 mm
用 cuRobo 成功，只有一次绕障调用，没有 worker 超时重启。冷启动仍需十余秒，热调用
约 0.6～1.0 s；这不是所有技能、精确物理状态或任意场景的亚秒保证。本机结果在
`.cache/approach_search_budget_verification.json`，CPU 回归测试位于
`planning/tests/test_planning_search_budget.py`。

## 精细运行时持物场景性能记录（简化代理前，2026-11-18）

原 OLS 小场景的亚秒记录不能代表完整 `approach(Tray)`。最新离线回放包含 161 个机器人／持物几何 link、1,234 个球和 250 个环境几何，保留 3 mm 间距及现有阶段策略。没有执行机器人运动。

运行时策略查询先用 GPU BVH／包围盒收集候选，再执行原距离和扫掠查询；包围盒向外扩张只用于保守筛选。候选队列有容量上限，溢出时在 GPU 上完整回退到密集检查，不截断候选。没有减少碰撞球、障碍物或最终宿主复核。世界位姿、障碍物启用状态或策略更新后，必须先调用 `PolicyCollisionChecker.configure()` 更新边界和 refit BVH，再进行查询；worker 在每次请求更新世界后执行此步骤。

相同记录目标下，之前热调用的 cuRobo 求解约 4.08 s，优化后约 2.37 s（下降约 42%）。完整避障适配层的两次热调用为 8.17 s／8.07 s，其中首次宿主复核 5.44 s／5.36 s，外层再次复核通过现有逐配置缓存仅约 0.005 s。`planning_stages_s` 分别记录 initial、外层及 total 宿主复核，避免只看外层缓存命中时间。此次冷调用约 30.43 s，含约 20.20 s 初始化；编译缓存会影响冷调用，不能把它当作稳定延迟。

目前没有达到完整场景 1 s。宿主耗时主要在原生 FCL 查询，当前 DLL 没有查询前过滤豁免对的接口；简单包围盒预筛在此轨迹 273 个采样点全部仍需精确查询，未作为生产优化启用。完整运行时模型的 CUDA Graph 规划仍会触发设备错误，继续使用 eager GPU 模式，不用不稳定 Graph 换取性能。下一步需要可构建的原生 FCL 源码／支持过滤与批量查询的后端，并重新验证所有豁免和距离边界。

离线复测数据位于 `.cache/approach_performance_verification.json`，该文件由本机测试生成，不是通用性能保证。

## 本机验收记录（原 OLS JSON 入口）

最终全套 unittest 215 项通过，新增测试 9 项。890 个官方文件逐项校验一致。完整 scene/4 连续纯轨迹规划成功，热调用约 335～434 ms（官方规划约 301～398 ms）；模型缓存已有时，新进程加载和预热本机约 25 秒，首次拟合可能数分钟。重试和不同场景可能明显更慢。

原演示入口也已无窗口完成 cuRobo → 原 FCL 路径复检 → 原 MuJoCo 执行；21 个轨迹点，规划约 460 ms，原复检约 41 ms，执行后 TCP 误差约 0.035 mm。该测试显式使用 0 额外间距。

柜门/夹爪状态更新、圆柱新增、完全阻挡的盒体和圆柱体拒绝、障碍数超过缓存容量时重新构建，均已实测。当前示例不是对任意场景成功率或真实硬件安全性的验收。

原演示入口需要原项目自身依赖正常。此机器的 auto_robot 缺少部分原依赖，实测时另外在 `.cache/curobo/host-test-deps` 放入 coacd 1.0.14、urchin 0.0.31、eaik 1.2.2，仅测试进程加载；未改主环境。正常业务请按原项目环境说明安装这些宿主依赖。纯轨迹 `demo_plan.py` 不要求 EAIK/FCL/MuJoCo 执行入口初始化。
