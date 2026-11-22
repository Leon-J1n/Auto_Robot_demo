# 第一阶段逐项验收审计

审计依据：《仿真平台架构 v2》第 19–21 节。状态定义：

- **已验证**：当前源码和运行结果直接证明要求成立；
- **部分完成**：接口或部分链路存在，但尚未覆盖要求的完整后端/格式/行为；
- **缺失**：当前没有足够实现或证据；
- **受依赖阻塞**：实现和验证必须使用尚未具备的生产依赖（当前无此类条目）。

## 第 19 节开发内容

| 条目 | 状态 | 当前证据 | 剩余工作 |
|---|---|---|---|
| 19.1 基础工程 | 已验证 | CMake；Windows MinGW、MSVC 2026 与 Ubuntu 22.04/GCC 11 均构建通过；日志、错误、配置、测试、可独立编译的插件 API、Profiler 已实现 | 无关键缺口 |
| 19.2 ECS/Runtime Scene | 已验证 | Entity 生命周期、类型化 Component Pool、全部第一阶段组件、层级/Transform、查询、深拷贝测试通过 | 无关键缺口 |
| 19.3 Sim Core | 已验证 | World、Scheduler、Time、Command、Event、Resource（含异步加载）、Plugin、Backend、State/Snapshot、Headless、Start/Pause/Step/Reset、Real-Time Factor Wall Time 节奏均已测试 | 无关键缺口 |
| 19.4 资源导入 | 已验证 | urdfdom URDF、内置 OBJ、真实 Assimp FBX fixture、材质/层级、源坐标到 X-forward/Z-up、FBX UnitScaleFactor 米制归一、Asset IR、关节/惯性/网格校验、Runtime Asset、缓存/失效/诊断均有测试 | 切线等高级材质数据可在后续扩展 |
| 19.5 Robot/Control | 已验证 | Link/Joint、FK、Frame、Jacobian、限位、状态、Position/Velocity/Effort、基础控制器、MuJoCo 映射通过测试 | 通用 IK 按文档不要求 |
| 19.6 MuJoCo Backend | 部分完成 | 真实 MuJoCo 3.9 模型、固定步、重力、接触、滑动/扭转/滚动摩擦、三种控制、一次性外力/力矩、状态同步/保存/恢复、Distance/Raycast 已测试 | MuJoCo 后端尚为可选核心目标，未独立封装为插件动态库 |
| 19.7 FCL Backend | 已验证（功能） | 真实 FCL 0.7：Box/Sphere/Cylinder/Capsule、Assimp 三角 Mesh BVH、静/动态同步、碰撞/自碰撞、距离、Raycast、Query Context、单/批量 Robot Configuration Check、LiDAR 和能力声明均通过测试 | 后端目前是可选编译模块，后续可进一步独立为动态库 |
| 19.8 Backend 组合 | 已验证 | 三种合法组合和非法组合拒绝测试通过；运行时 MuJoCo Physics + FCL Collision 同步链路通过完整构建 | 无关键缺口 |
| 19.9 Vulkan Backend | 已验证（第一阶段闭环） | Vulkan 1.4.350：设备/队列/命令、Buffer/Image/显存、SPIR-V Shader、Uniform/Descriptor、Graphics Pipeline、真实 Assimp Mesh、Transform、材质/定向光、深度、Offscreen RGB/线性 Depth、Render Snapshot、逐实例绘制与 Qt Viewport 均真实运行 | Qt 当前显示 Offscreen 帧；原生 Swapchain/多实例单 Draw 属后续优化 |
| 19.10 Sensor System | 已验证 | Camera 分辨率/FOV/内参/Near/Far/RGB/线性 Depth/更新率/时间戳/Frame ID、Joint State、LiDAR、Buffer/Scheduler/Event 全链路；MuJoCo/FCL/Vulkan Python 数值测试通过 | 共享内存零拷贝是性能强化项，不影响第一阶段数据闭环 |
| 19.11 状态管理 | 已验证 | ECS（含 Collider 摩擦）、MuJoCo、机器人、仿真时间、传感器调度、随机数、版本化内存/文件状态及跨 Simulator 恢复通过测试；当前文件格式 v2 | 跨版本迁移策略仅有拒绝不兼容版本，尚无升级器 |
| 19.12 C++ API | 已验证 | Simulator/World/资源/Robot/Entity/Transform、Start/Pause/Step/Reset、内存/文件状态、Camera 内参/RGB/Depth、LiDAR、FCL Collision/Distance/配置批检、Event、Backend、外力和错误均有 C++ 或稳定 C API | Query Context 的对象级 C ABI 可后续按需暴露 |
| 19.13 Python SDK | 已验证 | NumPy Camera RGB/Depth/内参、FCL/MuJoCo LiDAR、单/批量碰撞配置、批量 Transform/Joint Target、Start/Pause/Status/Event、异常、状态；ctypes 外部调用释放 GIL，自动化测试通过 | 共享内存零拷贝和并发压力矩阵属后续性能强化 |
| 19.14 Qt GUI | 已验证 | 主窗口、实时 Vulkan 场景 Viewport/传感器帧、自动/F 聚焦、轨道与 WASDQE 飞行相机、点击选择、黄色三维描边、Move/Rotate/Scale 三轴 Gizmo、真实父子场景树、可编辑 Transform/名称/可见性、实体删除、可实际切换的 None/MuJoCo/Bullet3 Physics、FCL Collision 与 Vulkan Renderer 配置、资源、关节目标、仿真/状态/日志/Agent/Sensor 面板；真实 JAKA URDF 与 149.98 万三角形中文路径 FBX GUI 回归通过 | 可升级为原生 Swapchain 视口和严格轴约束拖拽 |
| 19.15 Communication | 已验证 | 进程内 API、TCP JSONL RPC、Agent Schema、场景/仿真/Robot/状态命令、命名共享内存、Headless、错误、日志均有测试 | 传感器大数据写入共享内存需真实渲染数据源 |
| 19.16 CLI | 已验证 | `run/server/bake/validate/plugins list/backends check` 均有实现；核心命令有 CTest，server help 已实测 | `run` 的完整配置文件/场景参数仍可扩展 |
| 19.17 测试 | 已验证 | 核心测试 19/19：含真实 FBX → ECS → Vulkan、FCL Mesh/配置批检、MuJoCo、Vulkan Mesh/Descriptor/RGB/Depth；CTest、Python、RPC、Qt GUI、插件和 10 万 Entity/Snapshot/大型缓存基线均通过 | 更大规模 GPU/并发压力矩阵可在第二阶段持续扩充 |

## 第 21 节十八个验收场景

| # | 验收场景 | 状态 | 证据/缺口 |
|---:|---|---|---|
| 1 | Python 加载 URDF 机器人 | 已验证 | `python/tests/test_sdk.py` |
| 2 | FBX 或 OBJ 加载桌面/障碍 | 已验证 | 官方 Assimp `phong_cube.fbx` fixture 与 OBJ 均端到端通过 |
| 3 | 转换为 ECS Runtime Scene | 已验证 | URDF/OBJ/FBX Runtime 实例化与层级测试 |
| 4 | MuJoCo 创建机器人动力学世界 | 已验证 | `TestMuJoCoPhysicsStateAndRaycast` |
| 5 | MuJoCo 或 FCL 碰撞检查 | 已验证 | MuJoCo Distance/Raycast 与 FCL Collision/Distance/Query Context 均真实运行 |
| 6 | Vulkan 渲染场景 | 已验证 | 真实 FBX/Collider Mesh Pipeline、材质/光照、Descriptor、深度和 RGB 回读均有像素/米制深度断言 |
| 7 | Qt GUI 查看机器人和场景 | 已验证 | Qt 中央视口持续显示 Vulkan Offscreen 场景帧，隐藏启动与持续响应测试通过 |
| 8 | Python 发送关节位置目标 | 已验证 | 单机器人和批量目标测试 |
| 9 | 固定时间步仿真 | 已验证 | C++/Python 时间断言 |
| 10 | RGB/Depth/Joint State/LiDAR | 已验证（数据链路） | C++ Joint State；Python Vulkan RGB/Depth 与 MuJoCo/FCL LiDAR 均有数值/形状断言 |
| 11 | 保存当前状态 | 已验证 | 内存与文件检查点 |
| 12 | 继续运行若干步 | 已验证 | C++/Python 状态测试 |
| 13 | 恢复之前状态 | 已验证 | 内存、文件、跨 Simulator 恢复 |
| 14 | 验证机器人状态和时间恢复 | 已验证 | Python 数值断言 |
| 15 | Agent RPC 创建/移动障碍物 | 已验证 | `python/tests/test_server.py` |
| 16 | GUI 自动显示 Agent 修改 | 已验证 | 本地 Headless RPC 创建/移动实体后，Qt 自动查询并收到匹配 Transform；编辑器更新本地代理场景树 |
| 17 | 错误 Backend 组合拒绝启动 | 已验证 | C++ Backend 组合测试和 CLI check |
| 18 | 二次加载命中 Baking Cache 并加速 | 已验证 | 命中与主文件/依赖哈希失效已验证；6 万顶点 OBJ 首次约 753 ms、缓存约 19.9 ms，约 37.9× 加速 |

## 已执行验证

```text
MinGW core CTest:        4/4 passed
MuJoCo CTest:            4/4 passed
MSVC full-backend CTest: 4/4 passed (sim_core_tests 19/19)
MSVC GUI-backend CTest:  4/4 passed
Ubuntu 22.04 GCC CTest:  4/4 passed
Python SDK/RPC:          6/6 passed
Qt MSVC build:           passed
Qt Windows hidden smoke: exit 0
Qt viewport live check:  running/responding after 5 s
100,000 Entity create:   ~453 ms (最终本机基线)
100,000 Entity snapshot: ~92 ms (最终本机基线)
60,000 vertex OBJ import: ~753 ms
60,000 vertex cache hit:  ~19.9 ms (37.9x)
```

## 当前剩余工作

生产依赖已安装，18 个第一阶段验收场景全部验证通过，当前没有阻塞项。Bullet3 已作为首个插件式 Physics Backend 接入；后续强化方向是 articulated Bullet 约束、原生 Vulkan Swapchain 视口、传感器共享内存零拷贝、状态文件跨版本升级器，以及将 MuJoCo/FCL/Vulkan 从可选核心模块进一步拆成独立动态库。
