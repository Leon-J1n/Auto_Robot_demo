# Python API 速查手册

## 1. 三套 Python 入口

| 入口 | 进程模型 | 适用场景 |
|---|---|---|
| `Simulator` / `World` | 当前 Python 进程通过 ctypes 调用 `sim_platform_c.dll` | 无窗口批量仿真、测试、算法、服务端 |
| `EditorSession` / `EditorRobot` | Python 启动 `sim_editor.exe`，通过本地 TCP JSONL 控制 GUI | GUI 演示、可视化脚本、交互验证 |
| `LiveSession` / 代理对象 | 新 Python 进程连接已运行实例，或先启动 GUI/headless 实例 | Agent 多次执行、跨进程增量场景控制 |

三者实体句柄/引用不能互换。无窗口 SDK 实现在 `python/sim_platform/sdk.py`；旧 GUI SDK 在 `python/sim_platform/editor.py`；跨进程 Live SDK 在 `python/sim_platform/live.py`。

## 2. 安装与运行环境

```powershell
cd F:\RobotSimPlatform
python -m pip install -e .
$env:SIM_PLATFORM_LIBRARY='F:\RobotSimPlatform\build\windows-gui\Release\sim_platform_c.dll'
$env:PYTHONPATH='F:\RobotSimPlatform\python'
```

SDK 会按顺序查找环境变量、Python 包目录和常见 build 目录。发生 DLL 加载错误时优先显式设置 `SIM_PLATFORM_LIBRARY`，并确认同目录存在 MuJoCo、FCL、Assimp 等运行库。Bullet3 插件默认位于产物的 `plugins` 子目录，也可用 `SIM_PLUGIN_DIR` 或 `plugin_directory` 指定。

## 3. 无窗口 SDK 快速开始

```python
from sim_platform import Simulator, Transform

with Simulator(physics="mujoco", cache_directory=".sim-cache") as sim:
    world = sim.create_world(fixed_time_step=0.002, seed=42)
    robot = world.load_robot(r"D:\models\robot.urdf")
    robot.command_joint_positions([0.0] * robot.joint_count)

    sim.start(world)
    sim.step(world, 100)
    print(sim.time(world), robot.joint_state)
```

单位：米、秒、弧度、弧度每秒、牛顿、牛顿米；四元数顺序 `w,x,y,z`。

## 4. 数据类

### `Transform`

```python
Transform(
    translation=(0.0, 0.0, 0.0),
    rotation_wxyz=(1.0, 0.0, 0.0, 0.0),
    scale=(1.0, 1.0, 1.0),
)
```

### `Event`

字段：`type`、`world`、`entity`、`simulation_time`、`detail`。

### `State`

内存状态的不透明句柄，只能交给创建它的 Simulator 使用。

## 5. `Simulator`

构造：

```python
Simulator(cache_directory=None,
          physics="none" | "mujoco" | "bullet" | "bullet3",
          collision="fcl" | "bullet3" | "coal" | "ifps-cpu" | "ifps-gpu",
          plugin_directory=None)
```

| 方法/属性 | 返回 | 说明 |
|---|---|---|
| `create_world(fixed_time_step=0.002, seed=0)` | `World` | 创建世界 |
| `step(world, count=1)` | `None` | 固定步推进；自动构建脏后端 |
| `start(world)` / `pause(world)` | `None` | 修改运行状态 |
| `status(world)` | `"stopped"/"running"/"paused"` | 当前状态 |
| `time(world)` | `float` | 仿真时间 |
| `poll_event(world)` | `Event | None` | 非阻塞读取事件 |
| `save_state(world)` | `State` | 内存检查点 |
| `restore_state(world,state)` | `None` | 恢复内存检查点 |
| `save_state_file(world,path)` | `None` | 写 `.simstate` |
| `restore_state_file(world,path)` | `None` | 跨 Simulator 恢复场景 |
| `scene_snapshot(world)` | `SceneSnapshot` | 读取版本化、不可恢复的场景观察快照 |
| `reset(world)` | `None` | 恢复初始状态 |
| `command_joint_positions_batch(world,robots,targets)` | `None` | `targets` 形状 `[robot_count,joint_count]` |
| `close()` | `None` | 释放 C 句柄；推荐 `with` |

## 6. `World`

| 方法 | 返回 | 说明 |
|---|---|---|
| `load_robot(path)` | `Robot` | 加载 URDF 并验证机器人组件 |
| `load_asset(path)` | `Entity` | 加载 OBJ/FBX/URDF 等资产 |
| `create_primitive(name,shape,size,ifps_sphere_count=0)` | `Entity` | 创建 Primitive，可指定逐物体 IFPS 叶球数 |
| `create_entity(name,parent=None)` | `Entity` | 创建空实体 |
| `create_light(name="Point Light",type="point",parent=None,...)` | `Entity` | 创建 Point/Spot/Directional 光源 |
| `entity(handle)` | `Entity` | 包装已知句柄，不验证到首次调用 |
| `check_collision(a,b)` | `bool` | 当前 Collision Backend 的碰撞查询 |
| `distance(a,b)` | `float` | 当前 Collision Backend 的距离；Bullet3 复杂 Mesh 正距离可能为 AABB 近似 |
| `set_transforms(entities,transforms)` | `None` | 批量设置 Transform |
| `create_camera(parent,...)` | `Camera` | 创建 Camera Sensor |
| `create_lidar(parent,...)` | `LiDAR` | 创建 LiDAR Sensor |

`create_entity` 只创建基础 ECS 实体，不会自动添加 Visual/Collider/RigidBody；`create_primitive` 创建匹配的 Visual/Collider，随后用 `set_rigid_body()` 配置静态或动态属性。

光源参数使用线性 RGB 和世界单位：

```python
spot = world.create_light(
    name="Spotlight", type="spot", color=(1.0, 1.0, 1.0),
    intensity=1.0, range=10.0, decay=2.0,
    inner_cone_radians=0.35, outer_cone_radians=0.7,
)
spot.transform = Transform(
    rotation_wxyz=(0.70710678, 0.0, -0.70710678, 0.0)
)
```

类型别名 `parallel`/`sky` 映射到 `directional`。Spotlight 使用局部 `-Z` 作为正面，
内外锥是半角且必须满足 `0 <= inner <= outer < pi/2`；`range` 和 `decay` 仅用于点光/聚光灯，
`angular_diameter_radians` 仅用于平行光。上例四元数把 Spotlight 的局部 `-Z` 旋转到世界 `+X`。

`Entity.set_light(...)` 可修改已有光源的相同参数（`type`、`color`、`intensity`、`range`、
`decay`、`inner_cone_radians`、`outer_cone_radians`、`angular_diameter_radians`、`enabled`）。
只有带 Light 组件的实体应调用它；C ABI 对应 `SimLightProperties`，字段布局与上述参数一致，角度使用弧度。

## 7. `Entity` 与 `Robot`

### Entity

```python
entity.transform = Transform(translation=(1, 2, 3))
pose = entity.transform
entity.ifps_sphere_count = 1024
entity.apply_wrench((10, 0, 0), torque=(0, 0, 1))
```

`ifps_sphere_count` 范围为 0 到 1,000,000；`0` 使用 backend 默认值 256。修改后，下一次
仿真或碰撞查询会重建活动碰撞 backend，并复用相同几何体已经生成的 IFPS 前缀和树缓存。

`apply_wrench` 需要实体在当前 MuJoCo 或 Bullet3 后端中是可动力学求解的刚体；力/矩在下一步消费一次。

Bullet3 用法：

```python
with Simulator(physics="bullet") as sim:
    world = sim.create_world()
    box = world.create_primitive("box", "box", (0.5, 0.5, 0.5))
    box.transform = Transform(translation=(0, 0, 2))
    box.set_rigid_body(dynamic=True, gravity=True)
    sim.step(world, 500)
```

Bullet3 也可仅作为 Collision Backend，与 Physics Backend 独立组合：

```python
with Simulator(physics="mujoco", collision="bullet3") as sim:
    world = sim.create_world()
```

Coal 使用相同的插件选择入口：

```python
with Simulator(physics="mujoco", collision="coal") as sim:
    world = sim.create_world()
```

### Robot

| API | 说明 |
|---|---|
| `joint_count` | 导入的 Joint 数量，可能包含 Fixed Joint |
| `joint_names` | 平台 Joint 顺序；cuRobo 适配器据此移除 Fixed Joint 并重排自由度 |
| `joint_state` | `(positions, velocities, efforts)`，均为 `float64[joint_count]` |
| `command_joint_positions(targets)` | 位置目标 |
| `command_joint_velocities(targets)` | 速度目标 |
| `configuration_collides(positions)` | 单配置 FCL 检查 |
| `configurations_collide(matrix)` | 批量配置，返回 bool NumPy 数组 |

目标数组必须与 `joint_count` 完全一致，模型顺序由 `joint_names` 给出。

### cuRobo Collision Backend

cuRobo 作为可选的 Python/CUDA 机器人配置碰撞后端集成。FCL 仍负责通用 Entity pair、距离、LiDAR 和默认配置查询；绑定 cuRobo 后，当前 `Robot` 的 `configuration_collides()` 与 `configurations_collide()` 改走 GPU，`use_fcl_collision()` 可切回 FCL。

```python
from sim_platform import Simulator

with Simulator(physics="none") as sim:
    world = sim.create_world()
    robot = world.load_robot("robot.urdf")
    curobo = robot.use_curobo_collision(
        "robot-curobo.yml",
        {"cuboid": {"table": {"dims": [1.0, 1.0, 0.1],
                                "pose": [0, 0, 0.5, 0, 0, 0, 1]}}},
    )
    colliding = robot.configurations_collide([[0.0] * robot.joint_count])
    trajectory_mask = curobo.trajectories_collide(
        [[[0.0] * robot.joint_count]], robot.joint_names
    )
```

cuRobo 使用自己的机器人球体近似和 Scene 配置，Scene pose 四元数顺序为 `x,y,z,w`，与平台 Transform 的 `w,x,y,z` 不同。场景不会从 ECS 自动镜像；场景变化后调用 `curobo.update_scene(scene_model)`。`robot-curobo.yml` 应使用 cuRobo `RobotBuilder` 预先生成。官方保证环境为 Ubuntu 20.04+、post-Turing NVIDIA GPU、CUDA 12+ 和 Python 3.10+；当前 Windows 主机构建仍可正常使用 FCL，但不属于 cuRobo 的保证运行环境。

## 8. Camera 与 LiDAR

### Camera

| 属性 | dtype/shape |
|---|---|
| `dimensions` | `(width,height)` |
| `intrinsics` | `float64[3,3]` |
| `rgb` | `uint8[height,width,3]` |
| `depth` | `float32[height,width]`，米制线性深度 |

创建示例：

```python
camera = world.create_camera(
    robot, frame_id="camera", update_rate_hz=30,
    width=640, height=480,
    vertical_fov_radians=1.0472,
    near_plane=0.01, far_plane=100.0,
)
sim.step(world)
rgb, depth, k = camera.rgb, camera.depth, camera.intrinsics
```

### LiDAR

```python
lidar = world.create_lidar(
    robot, horizontal_samples=1024, vertical_channels=16,
    min_distance=0.05, max_distance=100.0,
)
sim.step(world)
points, distances = lidar.get_points()
```

返回 `points: float32[N,3]`、`distances: float32[N]`。

## 9. 状态与事件示例

```python
state = sim.save_state(world)
sim.step(world, 100)
sim.restore_state(world, state)

sim.save_state_file(world, "scene.simstate")
sim.restore_state_file(world, "scene.simstate")

while event := sim.poll_event(world):
    print(event.type, event.entity, event.detail)
```

恢复文件会替换 World 的 ECS，并重建物理/碰撞后端；旧的实体 Python 包装器只有在句柄仍存在时才可继续使用。

## 10. GUI SDK 快速开始

```python
from pathlib import Path
from sim_platform import EditorSession

editor = Path(r"F:\RobotSimPlatform\build\windows-gui\Release\sim_editor.exe")
with EditorSession.launch(editor) as session:
    robot = session.load_robot(r"D:\models\robot.urdf")
    print(robot.joint_names)
    session.start()
```

`EditorSession.launch()` 自动选择本地端口，启动 GUI，并等待控制服务就绪。默认请求超时 60 秒，以允许 MuJoCo 编译完整机器人 collision Mesh。

## 11. `EditorSession`

| API | 说明 |
|---|---|
| `load_robot(path)` | 返回 `EditorRobot` |
| `load_asset(path)` | 加载 URDF/OBJ/FBX，返回场景根实体句柄 |
| `create_primitive(kind)` | `cube/sphere/plane/capsule/cylinder`，返回实体句柄 |
| `scene_hierarchy()` | 返回含 `entity/name/parent/robot_root/robot_link/camera_sensor/expanded` 的扁平层级记录 |
| `set_parent(entity,parent)` | 保持世界姿态修改父节点；`parent=None` 解除父子关系 |
| `set_sensor_enabled(entity,enabled=True)` | 运行时启用或暂停传感器，不修改保存的配置 |
| `set_transform(entity, translation=, rotation_wxyz=, scale=)` | 设置局部 Transform |
| `get_transform(entity)` | 返回 `(translation,rotation_wxyz,scale)` 世界 Transform |
| `set_rigid_body(...)` | 设置静/动态、重力、质量、restitution、三参数 friction；动态体自动启用 MuJoCo |
| `check_collision(first,second)` | 当前 FCL 查询 |
| `start()` / `pause()` | GUI 仿真控制 |
| `simulation_time` | GUI 仿真时间 |
| `performance` | `(render_fps,render_ms)` |
| `is_open` | GUI 进程是否存在 |
| `close()` | 请求正常退出并等待进程 |

编辑器左侧 Scene Hierarchy 支持单实体拖放。拖到另一个实体上会成为其子节点，拖到空白处会回到 World root；操作保持实体当前世界姿态，之后实体随新父节点运动。机器人 Link/Joint 由 articulated 系统管理，不能被单独拖离机器人；机械臂根、夹爪根和普通资产根可以整体移动并挂到 Link 下。

物理参数示例：

```python
floor = session.create_primitive("plane")
session.set_transform(floor, scale=(10, 10, 1), translation=(0, 0, -0.01))
session.set_rigid_body(floor, dynamic=False, gravity=False)

ball = session.create_primitive("sphere")
session.set_transform(ball, translation=(0, 0, 0.5))
session.set_rigid_body(
    ball, dynamic=True, gravity=True, mass=1.0,
    restitution=0.5, friction=(0.4, 0.005, 0.001),
)
```

## 12. `EditorRobot`

| API | 说明 |
|---|---|
| `handle` | GUI 实体句柄 |
| `joint_names` | 关节名元组，JAKA 当前为 `fixed,joint_1..joint_6` |
| `joint_count` | 关节数量 |
| `joint_state` | 三个 NumPy 数组 |
| `command_joint_positions(targets,gain=10,velocity_limit=1)` | 位置控制，单位弧度 |
| `command_joint_velocities(targets)` | 速度控制 |
| `set_joint_continuous(name,enabled=True)` | 运行时切换 Continuous/Revolute |

推荐按名称构造目标，不要假定 Fixed Joint 是否存在：

```python
positions, _, _ = robot.joint_state
targets = positions.tolist()
targets[robot.joint_names.index("joint_1")] = 0.5
robot.command_joint_positions(targets)
```

## 13. 已有 Python 样例

| 文件 | 用途 |
|---|---|
| `examples/python/rotate_jaka_link1.py` | GUI 中按速度持续旋转 joint_1 |
| `examples/python/set_jaka_joint_angles.py` | 指定 JAKA 六轴角度并等待到位 |
| `examples/python/kick_jaka_ball.py` | Python 创建地板/球、启用 MuJoCo并让机械臂踢球 |

```powershell
python examples\python\set_jaka_joint_angles.py 10 -20 30 15 -25 40
python examples\python\kick_jaka_ball.py
```

## 14. 运行中 Python 控制

没有实例时显式启动。未设置 `listen` 时默认使用 `tcp://127.0.0.1:0`，操作系统选择空闲端口：

```python
from sim_platform.live import LaunchSpec, launch

sim = launch(LaunchSpec(
    kind="headless",                 # 或 "editor"
    instance_name="assembly-demo",
    listen=("tcp://127.0.0.1:0",), # 也可省略 listen
    detached=True,
))
print(f"pid:{sim.pid}", sim.endpoints)
sim.close()  # detached 实例继续运行
```

固定端口使用 `tcp://host:8765`；端口被占用时返回 `bind_failed`，不会静默换端口。远程 bind 示例为 `tcp://0.0.0.0:8765`，并应通过 `advertise=("tcp://sim-host.example.com:8765",)` 公布客户端实际可连接的地址。当前没有认证/TLS，不应直接暴露到非可信网络。

命令行等价形式：

```powershell
simulator-python launch --kind headless --name assembly-demo `
  --listen tcp://127.0.0.1:0 --detach
```

命令输出 `pid_endpoint`、`instance_id` 和实际 `endpoints`。后续独立 Python 进程可以按 PID 或 TCP 地址运行脚本：

```powershell
simulator-python run first.py --connect pid:18432
simulator-python run second.py --connect tcp://192.168.1.20:8765
```

Agent 不需要先单独探测再启动；用实例名原子地连接或创建，避免并发启动重复实例：

```python
from sim_platform.live import LaunchSpec, connect_or_launch

with connect_or_launch(
    "assembly-demo",
    LaunchSpec(kind="headless", listen=("tcp://127.0.0.1:0",), detached=True),
) as sim:
    robot = sim.ensure_robot("robot.urdf", alias="robot")
    robot.set_position((1.0, 2.0, 3.0))
```

第二个 Python 进程重新解析服务端 alias，不依赖前一个进程的变量：

```python
robot = require_robot("robot")
gripper = ensure_robot("gripper.urdf", alias="gripper")
gripper.set_parent(robot.get_link("tool_link"), preserve_world=False)
gripper.set_rotation((1.0, 0.0, 0.0, 0.0))
```

`pid:<PID>` 仅用于本机发现；客户端读取跨平台实例描述文件并连接其中的 TCP endpoint。当前每个实例支持一个 TCP listener。完整设计和边界见 `docs/LIVE_PYTHON_CONTROL_DESIGN.md`。

`LiveSession.create_light(type="point", name=None, color=..., intensity=..., range=..., decay=...,
inner_cone_radians=..., outer_cone_radians=..., angular_diameter_radians=..., enabled=True)`
通过 `scene.create_light` 创建光源并返回 `EntityProxy`；光源实体随后可通过代理的场景命令更新。
参数单位和 Spotlight 局部 `-Z` 约定与无窗口 SDK 相同。

跨进程会话除场景与仿真控制外，还提供与无窗口 SDK 对应的查询和位姿能力：

| Live API | 说明 |
|---|---|
| `RobotProxy.kinematics(positions, links, *, jacobian_link=None, limits="ignore")` | 批量 FK，可选 Jacobian；结果按 `[configuration][link]` 索引，Jacobian 形状 `(count,6,joint_count)` |
| `RobotProxy.configuration_contacts(positions, *, distance_threshold=0.0, limits="ignore")` | 单个假设配置下的逐对接触；`distance` 为表面间距，碰撞时为 0.0，不报告穿透深度 |
| `RobotProxy.joint_limits` | `(joint_count,2)` 上下限；Continuous 关节为 ±inf（传输中以 null 表示） |
| `RobotProxy.set_joint_positions(positions, *, limits="clamp")` | 直接摆位，绕过控制器，不消耗仿真步 |
| `RobotProxy.command_joint_positions(targets, *, gain=None, velocity_limit=None)` | 位置控制；两个参数为控制器覆盖项，省略即用平台默认值 |
| `RobotProxy.list_links()` | 一次返回全部命名 Link，避免逐个名字试探 |
| `LiveSession.save_state_file(path)` / `restore_state_file(path)` | 读写 `.simstate`；恢复会替换 ECS 并提升 scene generation，代理对象随后重新解析 |
| `LiveSession.sync_physics()` / `sync_collision()` | 直接写关节后同步后端，否则碰撞查询会使用过期几何 |

`kinematics()` 与 `configuration_contacts()` 在世界的私有副本上求值，不影响正在运行的仿真；
每次调用都是一次网络往返加一次拷贝，因此应批量传入多个配置，而不是循环单个查询。

## 15. Agent RPC 服务

启动：

```powershell
simulator-server --physics mujoco --host 127.0.0.1 --port 8765
```

协议为 UTF-8 JSON Lines。Schema：`schemas/agent-command.schema.json`；分发实现：`python/sim_platform/server.py::HeadlessService._invoke()`。支持场景、仿真、机器人、状态和共享内存命令。默认只监听本机。

## 16. 异常与退出

- 无窗口 SDK：C ABI 非零返回由 `_check()` 转为 `RuntimeError`。
- GUI SDK：服务端错误或连接关闭转为 `EditorRpcError`；用户直接关闭窗口是正常终止条件。
- 始终使用 `with Simulator(...)` / `with EditorSession.launch(...)` / `with LiveSession`，确保 DLL 句柄和 Socket 正常释放。detached Live 实例要退出时显式调用 `session.shutdown()`。
- Windows Unicode 路径通过 `os.fsencode` 或 UTF-8 JSON 传递，不要自行按 GBK 编码。
