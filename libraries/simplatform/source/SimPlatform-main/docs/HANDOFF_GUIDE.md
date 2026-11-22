# RobotSimPlatform 项目接手指南

## 1. 接手者先读什么

建议顺序：

1. 本文：能构建、测试和运行；
2. [文档索引](README.md)；
3. [关键实现结构](IMPLEMENTATION_STRUCTURE.md)；
4. 根据任务阅读 [C++ API](API_CPP_QUICK_REFERENCE.md) 或 [Python API](API_PYTHON_QUICK_REFERENCE.md)；
5. 只有开始修改时才打开导航表指定的实现文件。

不要先通读 `src/editor.cpp`、`vulkan_backend.cpp` 或整个 `tools/vcpkg-partial`。

## 2. 当前可工作的基准环境

项目根目录：

```text
F:\RobotSimPlatform
```

当前 Windows 完整 GUI 产物：

```text
F:\RobotSimPlatform\build\windows-gui\Release\sim_editor.exe
F:\RobotSimPlatform\build\windows-gui\Release\simulator.exe
F:\RobotSimPlatform\build\windows-gui\Release\sim_platform_c.dll
F:\RobotSimPlatform\build\windows-gui\Release\sim_core_tests.exe
```

当前依赖布局：

```text
third_party/vcpkg_installed/x64-windows   Assimp/FCL/urdfdom 及其依赖
third_party/Bullet3/3.25                  本机 Bullet3 fallback 安装（标准路径为 vcpkg）
third_party/Coal/3.0.4                     Coal 3.0.4 headers/lib/DLL/CMake config
third_party/MuJoCo/3.9.0                  MuJoCo headers + DLL
third_party/VulkanSDK/1.4.350.0           Vulkan headers/lib/glslc
D:/Anaconda/Library                       当前 Qt 5.15.2 CMake prefix
F:/VS                                     当前 Visual Studio/CMake
```

`D:/Anaconda` 与 `F:/VS` 是当前机器配置，不是架构要求。换机器时修改 CMake preset 或命令行 prefix。

## 3. 目录地图

### 必须理解的源码目录

| 路径 | 内容 | 通常何时查看 |
|---|---|---|
| `include/sim/` | 所有公开 C++ 接口、组件和 C ABI | 使用/扩展 API、理解数据结构 |
| `src/` | Core、后端、CLI、C API、Qt 编辑器实现 | 修改运行逻辑 |
| `python/sim_platform/` | 无窗口 SDK、GUI SDK、Agent 服务 | 修改 Python/通信接口 |
| `examples/python/` | 可运行 GUI 自动化样例 | 验证典型用户流程 |
| `tests/` | C++ 测试、插件 fixture、性能基线 | 修改功能时找回归入口 |
| `python/tests/` | Python SDK 与 Agent RPC 测试 | 修改 C ABI/Python/服务端 |
| `shaders/` | Vulkan GLSL 顶点/片段 Shader | 修改材质、光照、顶点格式 |
| `schemas/` | Agent JSONL 命令 Schema | 修改 Agent 协议 |
| `docs/` | 本文档集 | 接手、API、结构导航 |

### 工程与依赖目录

| 路径 | 内容/处理方式 |
|---|---|
| `CMakeLists.txt` | 目标、功能开关、依赖发现、DLL 部署、安装规则 |
| `CMakePresets.json` | `windows-full`、`windows-gui` 的已验证配置 |
| `vcpkg.json` | Assimp/FCL/urdfdom manifest |
| `pyproject.toml` | Python editable package 和 `simulator-server` 入口 |
| `third_party/` | 项目本地依赖和 vcpkg 安装树；不要当业务源码修改 |
| `tools/vcpkg-partial/` | 项目本地 vcpkg 工具副本；体积大，日常无需浏览 |
| `cmake/` | CMake 辅助模块 |
| `plugins/` | Bullet3 Physics/Collision、Coal、IFPS-CPU/GPU 插件入口与 manifest 模板 |

`third_party/` 细分：`downloads/` 是下载缓存，`binary-cache/` 是 vcpkg 二进制缓存，`vcpkg_installed/` 是实际 CMake prefix，`MuJoCo/` 与 `VulkanSDK/` 是项目本地 SDK，`vcpkg/` 是较完整的 vcpkg 工作副本。日常构建只应引用安装结果，不要修改第三方源码来修平台逻辑。

### 运行数据与生成目录

| 路径 | 说明 |
|---|---|
| `build/windows-gui/` | 当前正式 GUI 构建目录 |
| `build/windows-full/` | 无 Qt 的完整后端构建目录 |
| `build-all/`、`build-baseline/` | 历史/替代生成目录，不是源码 |
| `build-deps/` | 依赖准备过程的本地目录 |
| `.sim-cache/`、`examples/python/.sim-cache/` | Baking Cache，可删除后重建 |
| `.pytest_cache/` | pytest 生成数据 |
| `example_scene/` | 示例 `.simstate` 场景 |
| `MUJOCO_LOG.TXT` | MuJoCo 运行日志，可用于崩溃排查 |

不要在生成目录中修代码；修改 `src/include/python/shaders/tests/docs` 后重新构建。

### 根目录文件

| 文件 | 内容 |
|---|---|
| `README.md` | 项目概览与短构建入口 |
| `FIRST_STAGE_STATUS.md` | 第一阶段逐项验收记录，部分数字可能滞后于最新测试 |
| `仿真平台架构v2.md` | 原始目标架构，不等同于当前代码 |
| `CMakeLists.txt` | 唯一主构建定义 |
| `CMakePresets.json` | Windows 完整/GUI 预设 |
| `vcpkg.json` | C++ 包依赖 manifest |
| `pyproject.toml` | Python 包元数据 |
| `.gitignore` | 生成文件忽略规则 |
| `MUJOCO_LOG.TXT` | 运行日志，不是配置文件 |

## 4. 关键文件一览

| 文件 | 一句话职责 |
|---|---|
| `include/sim/types.hpp` | 数学、错误、诊断 |
| `include/sim/components.hpp` | ECS 组件定义 |
| `include/sim/ecs.hpp` | Registry 和 Frame Tree |
| `include/sim/core.hpp`, `src/core.cpp` | World、时间、调度、命令、事件、快照 |
| `include/sim/assets.hpp`, `src/assets.cpp` | AssetIR、导入、缓存、实例化 |
| `include/sim/robot.hpp`, `src/robot.cpp` | 机器人状态、控制、FK/Jacobian |
| `include/sim/sensors.hpp`, `src/sensors.cpp` | Camera/LiDAR/Joint Sensor |
| `src/mujoco_backend.cpp` | MJCF 生成、动力学、接触、控制、状态同步 |
| `src/fcl_backend.cpp` | 精确碰撞/距离/规划查询 |
| `include/sim/ifps.hpp`, `src/ifps.cpp` | IFPS 球壳生成、增量缓存和 CPU 碰撞查询 |
| `plugins/ifps_gpu_backend.cpp` | IFPS CUDA/NVRTC 批量查询 |
| `src/vulkan_backend.cpp` | GPU 资源、视觉 Mesh/LOD、离屏渲染/readback |
| `src/state_io.cpp` | `.simstate` 二进制格式 v8（兼容读取 v2-v7） |
| `include/sim/c_api.h`, `src/c_api.cpp` | 稳定 ABI 和无窗口完整编排 |
| `python/sim_platform/sdk.py` | ctypes + NumPy OO 包装 |
| `python/sim_platform/editor.py` | GUI TCP 控制 SDK |
| `python/sim_platform/server.py` | Headless Agent JSONL 服务 |
| `src/editor.cpp` | Qt 主窗口、视口、Inspector、Gizmo、GUI 控制服务 |
| `src/cli.cpp` | `simulator` CLI |
| `tests/test_core.cpp` | Core/后端集成回归主入口 |

## 5. 新机器依赖安装

### 5.1 必备工具

- Windows 10/11 x64；
- Visual Studio，安装“使用 C++ 的桌面开发”、MSVC、Windows SDK、CMake；
- Git（若需要重新获取依赖）；
- Python 3.10+；
- CMake 3.22+。

当前 preset 使用 `Visual Studio 18 2026`。若机器只有 VS 2022，应将 preset generator 改为对应版本，或新建本地 preset，不要改业务代码。

### 5.2 vcpkg 依赖

仓库已带 `tools/vcpkg-partial/vcpkg.exe` 和 manifest。安装到项目本地目录：

```powershell
cd F:\RobotSimPlatform
.\tools\vcpkg-partial\vcpkg.exe install `
  --triplet x64-windows `
  --x-install-root=F:\RobotSimPlatform\third_party\vcpkg_installed
```

目标依赖由 `vcpkg.json` 声明：`assimp`、Boost filesystem/serialization、`bullet3`、`fcl`、`urdfdom`。若本地工具副本不可用，可使用完整官方 vcpkg，并保持相同 install root。Windows preset 也识别 `third_party/Bullet3/3.25` 和 `third_party/Coal/3.0.4`。Coal 的独立构建及 Boost MSVC 自动链接处理见 [Coal Collision Backend](COAL_COLLISION_BACKEND.md)。

### 5.3 MuJoCo

下载与目标版本一致的 Windows x64 MuJoCo 3.9.0，解压后应满足：

```text
third_party/MuJoCo/3.9.0/include/mujoco/mujoco.h
third_party/MuJoCo/3.9.0/mujoco.dll
```

CMake 会在 Windows 下从 DLL 导出表生成 MSVC/MinGW 导入库，不需要手工修改 MuJoCo 目录。

### 5.4 Vulkan

安装 Vulkan SDK 或把 SDK 放入项目：

```text
third_party/VulkanSDK/1.4.350.0/Include
third_party/VulkanSDK/1.4.350.0/Lib/vulkan-1.lib
third_party/VulkanSDK/1.4.350.0/Bin/glslc.exe
```

版本可升级，但要同步 `CMakePresets.json` 路径并执行 Vulkan/GUI 测试。

### 5.5 Qt

Qt 5.15+ 或 Qt 6 均可；CMake 优先 Qt6，找不到再使用 Qt5.15。把包含 `Qt5Config.cmake`/`Qt6Config.cmake` 的安装前缀加入 `CMAKE_PREFIX_PATH`。

当前机器通过 `D:/Anaconda/Library` 找到 Qt5。另一机器可执行：

```powershell
cmake --preset windows-gui `
  -DCMAKE_PREFIX_PATH="F:/RobotSimPlatform/third_party/vcpkg_installed/x64-windows;C:/Qt/6.x/msvcXXXX_64"
```

`windeployqt` 找到时会自动部署 GUI DLL；否则需把 Qt runtime 放在 exe 同目录或 PATH。

### 5.6 Python

```powershell
cd F:\RobotSimPlatform
python -m pip install -e .
python -m pip install pytest
```

运行依赖只有 NumPy；测试额外需要 pytest。

cuRobo Collision Backend 是可选 Python/CUDA 能力，不参与 Windows C++ 构建。官方保证环境为 Ubuntu 20.04+、post-Turing NVIDIA GPU、CUDA 12+、Python 3.10+。按 CUDA 主版本安装：

```bash
python -m pip install -e ".[curobo-cu12]"
# CUDA 13 使用 .[curobo-cu13]
```

Windows 原生环境不在 cuRobo 官方保证范围；建议在 Linux/WSL2 GPU 环境使用。缺少 cuRobo 或 GPU 不影响默认 FCL 后端。

## 6. 标准构建

### 当前机器推荐命令

PATH 中的 CLion CMake 不识别当前 VS 2026 generator，优先使用 VS 随附 CMake：

```powershell
cd F:\RobotSimPlatform
$vsCMake = 'F:\VS\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin'

& "$vsCMake\cmake.exe" --preset windows-gui
& "$vsCMake\cmake.exe" --build --preset windows-gui-release
& "$vsCMake\ctest.exe" --preset windows-gui-release
```

完整产物在 `build/windows-gui/Release`。

### 只重建某个目标

```powershell
& "$vsCMake\cmake.exe" --build build\windows-gui --config Release --target sim_editor -- /m:1
& "$vsCMake\cmake.exe" --build build\windows-gui --config Release --target sim_core_tests -- /m:1
& "$vsCMake\cmake.exe" --build build\windows-gui --config Release --target sim_c_api -- /m:1
```

关键目标：

| Target | 产物 |
|---|---|
| `sim_core` | Core library |
| `sim_physics_bullet` | Bullet3 插件 DLL 与 manifest |
| `sim_collision_bullet` | Bullet3 Collision Backend 插件 DLL 与 manifest |
| `sim_collision_coal` | Coal Collision Backend 插件 DLL 与 manifest |
| `sim_collision_ifps_cpu` | IFPS-CPU Collision Backend 插件 DLL 与 manifest |
| `sim_collision_ifps_gpu` | IFPS-GPU Collision Backend 插件 DLL 与 manifest |
| `simulator` | CLI exe |
| `sim_c_api` | `sim_platform_c.dll` |
| `sim_editor` | Qt GUI exe |
| `sim_core_tests` | C++ 测试 exe |
| `sim_test_plugin` | 测试动态插件 |
| `sim_vulkan_shaders` | SPIR-V Shader |

为降低 MSBuild/内存冲突风险，本项目本机调试时常用 `/m:1`；标准 preset jobs=2。

### 无 GUI 构建

```powershell
cmake --preset windows-full
cmake --build --preset windows-full-release
ctest --preset windows-full-release
```

### 核心最小构建

默认所有外部后端开关为 OFF，可用普通 CMake 创建轻量 Core：

```powershell
cmake -S . -B build\core-only -DSIM_BUILD_TESTS=ON
cmake --build build\core-only --config Release
ctest --test-dir build\core-only -C Release --output-on-failure
```

## 7. 测试命令

### C++/CTest

```powershell
.\build\windows-gui\Release\sim_core_tests.exe
ctest --test-dir build\windows-gui -C Release --output-on-failure
```

验收要求：`sim_core_tests` 全部通过且 CTest 4/4；Bullet3 Physics/Collision 和 IFPS 同时由 C++ 插件集成
测试覆盖。

### Python

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH='F:\RobotSimPlatform\python'
$env:SIM_PLATFORM_LIBRARY='F:\RobotSimPlatform\build\windows-gui\Release\sim_platform_c.dll'
python -m pytest python\tests -q
```

验收要求：pytest 除环境条件明确不满足的用例外全部通过；层级拖放 GUI 集成测试需要设置
`SIM_PLATFORM_EDITOR`，未设置时会跳过该项。

### GUI smoke

```powershell
.\build\windows-gui\Release\sim_editor.exe --smoke-test
```

修改 GUI/渲染/物理交互后，还要运行一个真实样例；smoke 只证明窗口能启动。

## 8. 运行入口

### GUI

```powershell
.\build\windows-gui\Release\sim_editor.exe
```

### CLI

```powershell
.\build\windows-gui\Release\simulator.exe --help
.\build\windows-gui\Release\simulator.exe validate tests\data\triangle.obj
.\build\windows-gui\Release\simulator.exe bake tests\data\triangle.obj
.\build\windows-gui\Release\simulator.exe plugins list
.\build\windows-gui\Release\simulator.exe backends check
```

### Python GUI 样例

```powershell
python examples\python\rotate_jaka_link1.py
python examples\python\set_jaka_joint_angles.py 10 -20 30 15 -25 40
python examples\python\kick_jaka_ball.py
```

三个样例的默认 JAKA 路径为：

```text
D:\JakaDisplay\jaka_ros2\src\jaka_description\urdf\jaka_zu7.urdf
```

换机器用 `--asset` 或 `--robot` 指定实际 URDF。

### Agent RPC

```powershell
simulator-server --physics bullet3 --host 127.0.0.1 --port 8765
```

或：

```powershell
.\build\windows-gui\Release\simulator.exe server --physics mujoco --host 127.0.0.1 --port 8765
```

若 CLI 找不到 Python，设置 `SIM_PYTHON_EXECUTABLE`。

## 9. 修改后的最小验证矩阵

| 修改区域 | 至少运行 |
|---|---|
| Core/ECS/状态 | `sim_core_tests` + CTest |
| C ABI/Python SDK | 构建 `sim_c_api` + pytest |
| MuJoCo/机器人 | Core tests + 相关 Python GUI/无窗口样例 |
| Bullet3 插件/刚体 | 构建 `sim_physics_bullet` + `pytest python/tests/test_sdk.py::test_bullet3_plugin_rigid_body_state_and_wrench` |
| Bullet3 碰撞/BVH | 构建 `sim_collision_bullet sim_core_tests` + Core 中 `Bullet3 collision plugin` + [维护文档](BULLET3_COLLISION_BACKEND.md) |
| Coal 碰撞/BVH | 构建 `sim_collision_coal sim_core_tests` + Core 中 `Coal collision plugin` + [维护文档](COAL_COLLISION_BACKEND.md) |
| IFPS 球壳/缓存/插件 | 构建两个 `sim_collision_ifps_*` 目标 + Core tests + [IFPS 验证矩阵](IFPS_COLLISION_BACKENDS.md#11-验证矩阵) |
| FCL | Core tests 中碰撞/配置测试 |
| cuRobo Python 适配 | `pytest python/tests/test_curobo_backend.py`；受支持 GPU 环境再跑真实机器人配置 smoke |
| Vulkan/Shader | 构建 editor + Vulkan offscreen test + GUI 实测 |
| Qt GUI | 构建 editor + smoke + 手工/脚本交互 |
| Agent RPC/Schema | pytest `test_server.py` |
| 资源导入/缓存 | Core import tests；必要时删除 `.sim-cache` 重测冷/热路径 |

## 10. 常见问题

### `could not create CMAKE_GENERATOR "Visual Studio 18 2026"`

命中了不支持该生成器的旧/CLion CMake。使用 VS 随附的 `cmake.exe`，或把 preset generator 改为本机 VS 版本。

### 编译时 exe/DLL 被占用

先关闭 GUI。当前任务约定允许在构建前直接结束 `sim_editor`：

```powershell
Get-Process sim_editor -ErrorAction SilentlyContinue | Stop-Process -Force
```

关闭后继续原构建即可，不需要清空重编译。

### Python 找不到 `sim_platform_c.dll`

显式设置 `SIM_PLATFORM_LIBRARY`；确认 DLL 及依赖 DLL 都在 `build/.../Release`。修改 Core 后记得重新构建 `sim_c_api`，仅构建 `sim_editor` 不会更新 C ABI DLL。

### GUI 加载资产仍显示旧结果

删除项目/样例目录中的 `.sim-cache`，重新加载。缓存按源和依赖哈希失效，但格式/导入逻辑开发中仍建议验证冷路径。

### MuJoCo 切换或加载 JAKA 变慢

JAKA 七个 URDF collision STL 合计 127,772 个三角形。当前要求是不使用应用层碰撞 LOD，MuJoCo 会读取完整 STL 并审计面数，首次 build 变慢是预期行为。

### Mesh 碰撞外形与凹面不完全一致

平台传入完整 URDF STL，但 MuJoCo 标准 Mesh 碰撞内部使用凸碰撞语义。精确三角 Mesh 查询由 FCL 提供；动力学若要求精确非凸，需要新增凸分解或 SDF 方案。

### 球穿过地面/机械臂

依次检查：Collider 是否存在、RigidBody 是否 dynamic、MuJoCo 是否启用、Transform scale 后是否重建、collision group、时间步是否过大。相关回归在 `TestMuJoCoPhysicsStateAndRaycast`。

### 机器人启用物理后垂下或抽动

不要把 Robot 根或 Link 手工设为普通 Dynamic。机器人由 articulated-joint 系统管理：固定根、关节驱动、重力补偿、Robot-Robot 接触过滤。GUI 已阻止该错误设置。

## 11. 当前设计约束/后续工作

- Bullet3 Physics、Bullet3 Collision 与 Coal Collision 是独立插件 DLL；MuJoCo/FCL/Vulkan 仍是编译期可选 Core 模块；
- Bullet3 Collision 使用 DBVT broad phase 和 GImpact Mesh BVH；复杂 Mesh 的正 distance 可能退回 AABB 近似，精确 Mesh distance/LiDAR 继续使用 FCL；
- Coal Collision 使用 Dynamic AABB Tree broad phase 和 OBBRSS Mesh BVH，提供精确 pair/distance 与批量场景查询；
- cuRobo 是懒加载的 Python/CUDA 机器人配置碰撞适配层，不进入 C++ `BackendKind`，也不自动镜像 ECS Scene；
- GUI 主体集中在 `src/editor.cpp`，继续增长时应有计划拆分；
- Scene Hierarchy 支持保持世界姿态的拖放重设父节点；普通资产/完整机器人根可挂到机器人 Link，下层 articulated Link 本体禁止单独移出；
- Qt 视口显示 Vulkan 离屏帧，尚非原生 Swapchain；
- 状态格式有版本兼容读取，但没有通用迁移工具；
- 无窗口 C/Python API 尚未公开 GUI 那套 Primitive/材质编辑接口；
- 传感器大数据共享内存零拷贝仍是强化项；
- 编辑器支持创建 RGB/RGBD 相机、调整内参与彩色/深度预览；相机局部 `+X` 为光轴；
- MuJoCo 非凸 Mesh 动力学需要凸分解或 SDF；
- 视觉 LOD 已实现 per-entity 0..7，物理 URDF STL 不使用该 LOD。

## 12. 提交/交接前检查清单

- 修改只发生在源码/文档，而非 build 产物；
- 新组件字段同步 state IO；
- 新 C ABI 同步 `c_api.h/c_api.cpp/sdk.py`；
- 新 GUI RPC 同步 capabilities、dispatch、`editor.py`；
- 新 Agent RPC 同步 Schema 和 server tests；
- 构建正式目标，不只运行旧二进制；
- CTest 和 pytest 通过；
- GUI/物理/渲染修改有真实场景验证；
- 文档中的命令、默认路径和已知限制同步更新。
