# cuRobo 可选运动模块

规划器适配器为 `planning.planners.curobo.CuroboPlanner`，官方运行时实现位于本目录。在统一 `demo.run_demo` 入口中，未指定规划器的 joint 请求（包括 approach、retreat）会优先选择 cuRobo；也可以通过 `planning.planner_type: curobo` 显式选择。自动选择时，如果 CuRobo 因当前碰撞模型、起终点状态或不支持的接触约束无法规划，会回退到原有 joint planner；显式选择则直接返回 CuRobo 错误。严格直线/圆弧请求继续使用原有规划器。OLS 和原始资产均只读。

## 文件职责

- `source/`：未经修改的 NVlabs/cuRobo 官方源码，固定提交 `78fd485fa82d9b9a063fb4985e371814587e666a`。`UPSTREAM.json` 记录来源和下载包校验值；`verify_upstream.py` 按 `UPSTREAM.files.json` 核对全部 890 个文件。
- `conversion/`：通过 OLS 下载资产及引用文件，读取当前 JSON，计算关节、层级、挂载与碰撞几何；生成独立装配 URDF。
- `config/`：转换设置、必要差异和已测试 Windows/CUDA 依赖版本。官方规划任务 YAML 在 `source/` 中原样使用。
- `client.py`、`worker.py`：隔离依赖的持久 GPU 工作进程；官方 RobotBuilder 拟合及保存机器人 YAML，官方 Scene.create 解析场景，官方 MotionPlanner 规划。
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

`demo/run_demo.py` 延迟创建并复用 `CuroboPlanner`，读取执行后端当前关节、夹爪、机构和物体状态，规划后调用原 `execute_trajectory()`。CuRobo 优先策略覆盖所有 joint 请求；不支持的几何路径或接触请求自动回到原有动作。新增动作不会把官方轨迹重新交给原 Ruckig/RRT 计算，防止重复规划与几何变化；时间与导数转换为原项目字段后直接交给执行器。若提供原项目碰撞检查器，执行前按原检查器复检一次，耗时单独记为 `host_collision_validation`。

当前支持自由空间移动。严格直线/圆弧、全程姿态约束、接触目标例外、持物运动尚未接入本适配层，会明确拒绝。使用这些功能时继续使用原动作，不会因为安装本模块而受影响。不得把抓持中的物体仅当静态障碍物后继续规划，因此当前会拒绝持物请求。

本次保持原项目六轴 Move 校验和执行协议；七轴机器人的通用转换与六轴项目执行入口是不同范围，本入口不宣称已经支持七轴执行。

## 返回值

`success/status/selection/trajectory/execution_result/timing/error` 与现有 Move 对齐。`plan()` 和没有 robot_runtime 的 `execute()` 只规划，`planned_only=true`；给定 robot_runtime 时，`execute()` 才执行。

轨迹包含：

- `joint_names`：机械臂原关节名称，顺序与执行器一致；锁定的夹爪关节不混入输出。
- `waypoints`、`time_parameterized_waypoints`：同一批官方插值关节位置。
- `timestamps`：从零开始的秒数，与每行位置对应；另有 duration、velocities、accelerations、jerks。
- `planning_time`：官方规划调用及 GPU 完成等待时间。
- `diagnostics`：官方版本、工具、转换设置、碰撞规模、当前 TCP/基座、官方求解时间及缓存使用情况。

全局和请求速度/加速度比例按原项目读取。比例通过时间拉伸实现，位置不变，速度/加速度/jerk 按相应幂次缩放。规划的尝试次数使用官方 max_attempts；planning_time 是结果预算检查，官方接口没有逐次调用的硬中断参数，超过预算的结果不会执行。首次下载、拟合和预热另外记录在 timing.conversion/preparation 中。工作进程有独立总等待超时，超时会终止进程。

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

### 安全间距的已知限制

`safety_distance` 通过机器人球半径生效，也会影响未被官方/项目装配例外排除的自碰撞对。本机 scene/1 张开夹爪在额外 3 mm 间距下，夹爪 base 与 coupler 的拟合球会发生内部冲突，官方规划器拒绝；同一完整场景以 0 额外间距可规划并交原执行器完成运动。0 表示保留原始拟合边界，不表示关闭碰撞检查。

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

## 本机验收记录

最终全套 unittest 215 项通过，新增测试 9 项。890 个官方文件逐项校验一致。完整 scene/4 连续纯轨迹规划成功，热调用约 335～434 ms（官方规划约 301～398 ms）；模型缓存已有时，新进程加载和预热本机约 25 秒，首次拟合可能数分钟。重试和不同场景可能明显更慢。

原演示入口也已无窗口完成 cuRobo → 原 FCL 路径复检 → 原 MuJoCo 执行；21 个轨迹点，规划约 460 ms，原复检约 41 ms，执行后 TCP 误差约 0.035 mm。该测试显式使用 0 额外间距。

柜门/夹爪状态更新、圆柱新增、完全阻挡的盒体和圆柱体拒绝、障碍数超过缓存容量时重新构建，均已实测。当前示例不是对任意场景成功率或真实硬件安全性的验收。

原演示入口需要原项目自身依赖正常。此机器的 auto_robot 缺少部分原依赖，实测时另外在 `.cache/curobo/host-test-deps` 放入 coacd 1.0.14、urchin 0.0.31、eaik 1.2.2，仅测试进程加载；未改主环境。正常业务请按原项目环境说明安装这些宿主依赖。纯轨迹 `demo_plan.py` 不要求 EAIK/FCL/MuJoCo 执行入口初始化。

