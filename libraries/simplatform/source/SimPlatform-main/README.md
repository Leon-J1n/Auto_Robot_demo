# 机器人仿真平台（第一阶段实现）

本工程根据《仿真平台架构 v2》第 19–21 节建立第一阶段实现。核心采用 C++20，提供稳定 C API、Python SDK、CLI、Agent RPC 与可选 Qt 编辑器；外部后端均为显式构建选项，缺少依赖时不会报告虚假能力。

逐项完成证据和缺口见 [`FIRST_STAGE_STATUS.md`](FIRST_STAGE_STATUS.md)。

接手、API 和实现导航请从 [`docs/README.md`](docs/README.md) 开始：包含 C++/Python API
速查、关键实现控制流、依赖安装、构建运行和排障指南。IFPS 插件开发见
[`docs/IFPS_COLLISION_BACKENDS.md`](docs/IFPS_COLLISION_BACKENDS.md)。
Bullet3 碰撞插件的 BVH 结构、构建和维护见
[`docs/BULLET3_COLLISION_BACKEND.md`](docs/BULLET3_COLLISION_BACKEND.md)。
Coal 碰撞插件的 Dynamic AABB Tree、OBBRSS Mesh BVH 和构建说明见
[`docs/COAL_COLLISION_BACKEND.md`](docs/COAL_COLLISION_BACKEND.md)。

## 已实现并验证

- CMake 工程，可使用 MinGW/GCC 与 MSVC 构建；
- Entity/Component 注册、查询、创建/递归销毁、场景深拷贝；
- Transform、Hierarchy、Frame Tree 与循环检查；
- World、固定时间步、仿真阶段调度、命令队列、事件总线、Start/Pause/Step/Reset；
- ECS/仿真时间/随机数内存快照和恢复；
- 版本化文件状态：完整内置组件、Frame Tree、随机数、仿真时间及后端状态，支持跨 Simulator 恢复；
- 后端能力声明及三种合法组合校验；
- 插件 C ABI v2、JSON 清单发现、依赖/平台/API 校验、动态加载和卸载，以及物理/碰撞后端工厂与服务生命周期保护；
- Asset IR、urdfdom URDF 导入、Assimp 通用网格/真实 FBX 导入、内置 OBJ 导入、坐标轴/单位归一、层级、关节树/限位/网格校验；
- 二进制 Baking Cache、内容哈希失效与原子更新；
- Robot/Link/Joint、直接状态、位置/速度/力矩目标、基础控制器、正运动学和 Jacobian；
- RGB/Depth/Joint State/LiDAR 数据结构、传感器缓冲、多频率调度和数据就绪事件；
- 真实 MuJoCo 3.9 后端：MJCF 运行时模型、固定步长、三参数摩擦、控制、一次性外力/力矩、状态同步、状态保存/恢复、距离与射线查询；
- Bullet3 3.25 插件式 Physics Backend：独立 `sim_physics_bullet` 动态库、刚体/运动学体、Primitive/Mesh、重力、材质、外力、状态同步与保存/恢复；
- Bullet3 3.25 插件式 Collision Backend：独立 `sim_collision_bullet` 动态库、DBVT 场景 broad phase、GImpact Mesh BVH、pair/batch/距离查询和逐帧场景同步；
- Coal 3.0.4 插件式 Collision Backend：独立 `sim_collision_coal` 动态库、Dynamic AABB Tree broad phase、OBBRSS Mesh BVH、pair/batch/距离查询和逐帧场景同步；
- 稳定句柄式 C API 与 NumPy Python SDK；
- FCL Primitive/Assimp 三角 Mesh BVH、碰撞/距离、独立查询上下文、单/批量机器人配置检查、场景同步与射线查询；无物理后端也可生成 LiDAR；
- IFPS-CPU/IFPS-GPU 插件式 Collision Backend：确定性分层表面球壳、逐物体球数、增量缓存、CPU 与 CUDA/NVRTC batch query、编辑器分层可视化；
- 可选 cuRobo Python/CUDA Collision Backend：按关节名映射平台配置，支持单个、批量和轨迹机器人碰撞查询；
- Vulkan 真实设备/队列/命令缓冲、Shader、Uniform/Descriptor、基础材质/光照、真实 FBX/OBJ Mesh Pipeline、深度测试、Render Snapshot、离屏 RGB/线性 Depth 回读；
- C/Python Camera API（分辨率、内参、RGB、Depth），RGB/Depth 以 NumPy 数组读取；
- C/Python Start/Pause/Status、碰撞/距离、事件轮询 API；
- Python MuJoCo/FCL LiDAR 点云/距离访问；
- Agent RPC：场景查询/修改、仿真、机器人、状态命令、稳定错误和 JSONL 命令日志；
- Live Python Control：运行后按 PID/远程 TCP 连接，跨 Python 进程复用场景对象 alias，可启动 GUI/headless 空场景并选择固定或动态端口；
- 命名共享内存分配、描述、释放及服务器生命周期清理；
- Qt 主窗口、实时 Vulkan 场景 Viewport/传感器画面、场景层级、属性查看、资产加载、仿真控制、实时因子、日志和后端状态显示；
- CLI：`run`、`server`、`bake`、`validate`、`plugins list`、`backends check`；
- C++ 单元/集成测试、Python 端到端测试、Qt 无窗口启动测试和 Entity/Snapshot 性能基线。
- 大型 OBJ Baking Cache 基准：本机约 37.9 倍加速，并以至少 20% 提速作为验收阈值。

## 外部依赖状态

| 模块 | 当前状态 | 原因 |
|---|---|---|
| MuJoCo | 已接入并通过真实运行测试 | 3.9.0 位于 `third_party/MuJoCo`；MSVC/MinGW 自动从 DLL 生成导入库 |
| Bullet3 | 已作为可切换 Physics 与 Collision Backend 插件接入；碰撞插件使用 DBVT + GImpact BVH | vcpkg manifest 声明 `bullet3`；本机 fallback 位于 `third_party/Bullet3/3.25` |
| Coal | 已作为可切换 Collision Backend 插件接入；使用 Dynamic AABB Tree + OBBRSS Mesh BVH | 3.0.4 安装到 `third_party/Coal/3.0.4`；依赖 Boost、Eigen、Assimp |
| FCL | 已接入 Primitive/Mesh BVH、碰撞、距离、查询上下文、配置批检、同步和 LiDAR | vcpkg 安装树位于 `third_party/vcpkg_installed` |
| IFPS | IFPS-CPU/GPU 已作为可切换 Collision Backend 插件接入 | CPU 生成确定性球壳；GPU 通过 CUDA Driver API/NVRTC 加速大 batch 运行时遍历 |
| cuRobo | 已接入可选 Python/CUDA 机器人配置 Collision Backend；默认不加载 | 官方保证 Ubuntu 20.04+、post-Turing NVIDIA GPU 和 CUDA 12+；Windows Core 继续使用 FCL |
| Vulkan | 已接入真实 Mesh Pipeline、Descriptor、基础材质/光照、离屏 RGB/Depth 和 Qt Viewport | SDK 1.4.350.0 位于 `third_party/VulkanSDK` |
| Assimp/FBX | 已接入并以真实 FBX fixture 完成 Assimp → Asset IR → ECS → Vulkan 测试 | Assimp 6.0.4 位于项目 vcpkg 安装树 |
| urdfdom | 已作为默认完整构建的 URDF 解析器 | urdfdom 3.1.1 位于项目 vcpkg 安装树 |
| Qt GUI | 已编译、部署到 F 盘产物目录并完成隐藏启动/持续响应测试 | 构建使用本机 Qt 5.15.2，运行库由 `windeployqt` 部署 |

第 21 节的 18 个第一阶段验收场景已全部形成可运行闭环。后续强化项包括原生 Swapchain 视口（当前 Qt 显示 Vulkan Offscreen 帧）、传感器共享内存零拷贝视图，以及将可选后端进一步拆分为独立动态库。

## Windows 完整构建（项目本地依赖）

依赖已安装在 `F:\RobotSimPlatform\third_party`。标准预设不依赖 vcpkg 工具本体，也不要求修改全局 `PATH`：

```powershell
cmake --preset windows-full
cmake --build --preset windows-full-release
ctest --preset windows-full-release
```

预设使用 Visual Studio 2026 生成器。若当前 `PATH` 中先命中不支持该生成器的旧版/IDE 内置 CMake，可直接使用 Visual Studio 随附版本（本机路径如下）：

```powershell
$vsCMake = 'F:\VS\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin'
& "$vsCMake\cmake.exe" --preset windows-full
& "$vsCMake\cmake.exe" --build --preset windows-full-release
& "$vsCMake\ctest.exe" --preset windows-full-release
```

产物位于 `build/windows-full/Release`，所需第三方 DLL 会自动复制到产物目录。

包含 Qt 编辑器的构建：

```powershell
cmake --preset windows-gui
cmake --build --preset windows-gui-release
ctest --preset windows-gui-release
build\windows-gui\Release\sim_editor.exe
```

## 构建核心与测试（MinGW）

```powershell
cmake -S . -B build -G "MinGW Makefiles" `
  -DCMAKE_CXX_COMPILER=g++ -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

本机 MuJoCo 构建示例：

```powershell
cmake -S . -B build-mujoco -G "MinGW Makefiles" `
  -DSIM_ENABLE_MUJOCO=ON `
  -DSIM_MUJOCO_ROOT="C:/path/to/site-packages/mujoco"
cmake --build build-mujoco --parallel
ctest --test-dir build-mujoco --output-on-failure
```

MinGW 下 CMake 会从 `mujoco.dll` 导出表生成导入库；不会修改 MuJoCo 安装目录。

## Python SDK

```powershell
pip install -e .
$env:SIM_PLATFORM_LIBRARY="F:/RobotSimPlatform/build/windows-full/Release/sim_platform_c.dll"
$env:PYTHONPATH="F:/RobotSimPlatform/python"
pytest python/tests -q
```

```python
from sim_platform import Simulator

with Simulator(physics="mujoco") as sim:
    world = sim.create_world(fixed_time_step=0.002)
    robot = world.load_robot("robot.urdf")
    robot.apply_wrench((10.0, 0.0, 0.0))
    robot.command_joint_positions([0.5])
    state = sim.save_state(world)
    sim.step(world, 20)
    sim.restore_state(world, state)

    camera = world.create_camera(robot, width=640, height=480)
    sim.step(world)
    rgb = camera.rgb       # uint8[H,W,3]
    depth = camera.depth   # float32[H,W]
    K = camera.intrinsics  # float64[3,3]
```

刚体场景可切换到 Bullet3；默认从构建产物的 `plugins` 目录发现插件，也可设置 `SIM_PLUGIN_DIR` 或传 `plugin_directory=`：

```python
with Simulator(physics="bullet") as sim:
    world = sim.create_world()
    body = world.create_primitive("box", "box", (0.5, 0.5, 0.5))
    body.set_rigid_body(dynamic=True)
    sim.step(world, 100)
```

动力学与碰撞可以独立组合，例如 MuJoCo 推进刚体、Bullet3 执行碰撞查询：

```python
with Simulator(physics="mujoco", collision="bullet3") as sim:
    world = sim.create_world()
```

同一入口可选择 Coal：`Simulator(physics="mujoco", collision="coal")`。

## Agent RPC

安装 Python 包后启动：

```powershell
simulator-server --physics mujoco --host 127.0.0.1 --port 8765
# 或由 C++ CLI 安全转发到同一模块：
simulator server --physics mujoco --host 127.0.0.1 --port 8765
```

如 `python` 不在 PATH，可设置 `SIM_PYTHON_EXECUTABLE` 为解释器绝对路径。

协议为 UTF-8、逐行 JSON。命令 Schema 位于 `schemas/agent-command.schema.json`。默认仅监听本机；`shared_memory.allocate/describe/release` 提供跨进程命名缓冲区。

## Live Python Control

没有已运行实例时，可启动 detached 空场景；端口 `0` 由操作系统分配，命令会输出实际 endpoint 和 `pid:<PID>`：

```powershell
simulator-python launch --kind headless --name assembly-demo `
  --listen tcp://127.0.0.1:0 --detach
```

后续独立 Python 进程连接同一实例：

```powershell
simulator-python run first.py --connect pid:18432
simulator-python run second.py --connect tcp://192.168.1.20:8765
```

固定端口使用 `--listen tcp://0.0.0.0:8765`，远程部署同时设置 `--advertise tcp://host.example.com:8765`。当前 Live 协议尚无认证/TLS，只应部署在可信网络或受控代理后。API、Agent alias 工作流和当前限制见 [`docs/API_PYTHON_QUICK_REFERENCE.md`](docs/API_PYTHON_QUICK_REFERENCE.md) 与 [`docs/LIVE_PYTHON_CONTROL_DESIGN.md`](docs/LIVE_PYTHON_CONTROL_DESIGN.md)。

## WebEditor

旧 `WebEditor/` 与 `WebServer/` 验证实现已经移除。新 WebEditor 位于 `web/`，设计基线见 `docs/WebEditor设计稿V2.md`。完成一次原生构建和前端依赖安装后，开发环境可直接启动，不执行前端生产构建：

```powershell
.\Start-WebEditor.ps1
```

结束由启动脚本管理的 WebEditor 和 Gateway 服务：

```powershell
.\Stop-WebEditor.ps1
```

WebEditor 的全部可见功能由 `web/config/features.json` 控制。修改配置后需要重启 Gateway；
可通过环境变量 `SIM_WEB_FEATURE_CONFIG` 指定其他配置文件。功能键和扩展约束见
`web/README.md`。

## Qt 编辑器

使用上面的 `windows-gui` 预设即可构建。自动 smoke 可运行：

```powershell
build\windows-gui\Release\sim_editor.exe --smoke-test
```

编辑器通过 Vulkan Offscreen Rendering 获取场景 RGB 帧，再显示到 Qt Viewport；资源加载、仿真与 Agent 变更会在定时刷新中同步。

视口操作：左键拖动旋转相机或操作当前 Gizmo，中键/右键拖动平移，滚轮缩放，单击选择实体；点击视口后用 `W/A/S/D/Q/E` 前后左右上下移动相机，按 `F` 聚焦所选实体。`Move / Rotate / Scale` 操作条切换三种可视化变换工具，所选实体会显示黄色三维包围描边与红/绿/蓝三轴 Gizmo；`Frame Scene` 按真实场景包围盒重新取景。左侧层级树支持把普通资产根或完整机器人根拖到另一个实体/机械臂 Link 下，拖到空白处可解除父子关系，操作保持当前世界姿态；机器人 Link 本体不能被拆离 articulated 结构。右侧可直接编辑名称、Position、Rotation、Scale 和 Visual 可见性，也可删除实体。

`Backend Configuration` 面板可实际切换 Physics（None/MuJoCo/Bullet3）、Collision（None/FCL/Bullet3/IFPS-CPU/IFPS-GPU）和 Renderer（None/Vulkan），应用结果和失败原因会同时显示在面板与日志中。Bullet3 Physics 与 Bullet3 Collision 是两个独立插件；FCL 仍提供精确 Mesh distance 和 LiDAR。GUI 只在场景或相机发生视觉变化时重绘；本机 JAKA 暂停场景 5 秒 CPU 时间由约 4.44 秒降至约 0.61 秒。FBX 使用索引缓冲而不是展开三角顶点，并通过内存导入支持 Windows 中文路径；真实 `SM_Fountain.fbx`（约 89.3 万顶点、149.98 万三角形）已完成 GUI 渲染回归。

URDF 会读取 `<visual>`、材质颜色、视觉原点、网格缩放及 `package://` 资源；FBX 会按节点绑定对应 Mesh。资源缓存格式升级后旧缓存会自动失效并重新导入。
