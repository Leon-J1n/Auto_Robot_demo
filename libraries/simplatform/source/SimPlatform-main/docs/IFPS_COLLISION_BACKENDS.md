# IFPS Collision Backend 构建与维护指南

本文面向需要构建、调试或继续修改 IFPS 的维护者。平台提供两个插件：

| Backend | 插件名 | 运行时遍历 |
|---|---|---|
| IFPS-CPU | `sim.collision.ifps-cpu` | C++ CPU 层级遍历 |
| IFPS-GPU | `sim.collision.ifps-gpu` | CUDA Driver API + NVRTC 批量遍历 |

两者共用 `src/ifps.cpp` 生成的确定性离线球壳和 CPU 基础实现。GPU 插件只替换大批量
scene query 的运行时遍历，不在 GPU 上生成 IFPS 球。

原始方法参考 `E:/AutoResearch/old-codes/IFPS`。修改离线生成算法前，必须先对照该仓库，
不能用普通 FPS、包围球或其他近似算法替换参考实现的球心、半径和层级构造规则。原仓库是
算法对照资料，不是 RobotSimPlatform 的构建依赖。

## 1. 源码与产物地图

| 文件 | 职责 |
|---|---|
| `include/sim/ifps.hpp` | 球/树数据结构、增量缓存、CPU backend 公共接口 |
| `src/ifps.cpp` | 表面采样、inverse FPS、树构造、缓存、CPU 遍历和近似距离 |
| `plugins/ifps_cpu_backend.cpp` | CPU 插件 C ABI 工厂 |
| `plugins/ifps_gpu_backend.cpp` | NVRTC kernel、显存镜像和 GPU batch query |
| `plugins/ifps_*.simplugin.json.in` | 插件 manifest 模板 |
| `include/sim/components.hpp` | 逐 Collider 的 `ifps_sphere_count` |
| `src/state_io.cpp` | `ifps_sphere_count` 状态序列化 |
| `src/editor.cpp` | 球数编辑、层级选择和球壳可视化 |
| `include/sim/c_api.h`, `src/c_api.cpp` | C API 球数读写 |
| `python/sim_platform/sdk.py` | Python 球数属性和创建参数 |
| `tests/test_core.cpp` | 树、缓存、CPU/GPU 一致性与覆盖回归 |

Windows Release 构建产物位于：

```text
build/windows-full/Release/plugins/
  sim_collision_ifps_cpu.dll
  ifps_cpu.simplugin.json
  sim_collision_ifps_gpu.dll
  ifps_gpu.simplugin.json
```

manifest 和对应动态库必须放在同一目录。插件加载器递归扫描 `*.simplugin.json`，然后根据
manifest 的 `library` 字段加载同目录动态库。

## 2. 构建要求

### 2.1 IFPS-CPU

CPU 插件本身只依赖 Core。要为真实 Mesh 生成球壳，还必须启用 Assimp：

```text
SIM_ENABLE_IFPS_CPU_PLUGIN=ON
SIM_ENABLE_ASSIMP=ON
```

如果关闭 Assimp，Primitive 仍可生成 IFPS；Mesh 会进入现有 Box fallback，不能用于验证
真实 Mesh 碰撞精度。

顶层 CMake 默认开启 IFPS-CPU、关闭 IFPS-GPU；`windows-full/windows-gui` preset 会显式
同时开启两者。

### 2.2 IFPS-GPU

GPU 插件额外要求：

- NVIDIA GPU 和可用的 NVIDIA Driver；
- CUDA Toolkit，且 CMake 能找到 `CUDAToolkit`；
- Toolkit 中的 CUDA Driver import library 和 NVRTC runtime；
- `SIM_ENABLE_IFPS_GPU_PLUGIN=ON`。

该目标不使用 nvcc 编译独立 `.cu` 文件。插件由宿主 C++ 编译器构建，CUDA kernel 在创建
backend 时由 NVRTC 编译为 PTX。这是当前 Windows + Visual Studio 2026 + CUDA 11.8
组合的兼容路径。

## 3. 构建

### 3.1 Windows

当前已验证 preset 同时启用 Assimp、IFPS-CPU 和 IFPS-GPU：

```powershell
cd F:\RobotSimPlatform
$vsCMake = 'F:\VS\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin'

& "$vsCMake\cmake.exe" --preset windows-full
& "$vsCMake\cmake.exe" --build build\windows-full --config Release `
  --target sim_collision_ifps_cpu sim_collision_ifps_gpu sim_core_tests --parallel 2
```

需要编辑器和可视化时使用 GUI preset：

```powershell
& "$vsCMake\cmake.exe" --preset windows-gui
& "$vsCMake\cmake.exe" --build build\windows-gui --config Release `
  --target sim_editor sim_collision_ifps_cpu sim_collision_ifps_gpu --parallel 2
```

只需要 CPU 插件或机器没有 CUDA 时，可建立独立构建目录：

```powershell
& "$vsCMake\cmake.exe" -S . -B build\ifps-cpu `
  -DSIM_ENABLE_ASSIMP=ON `
  -DSIM_ENABLE_IFPS_CPU_PLUGIN=ON `
  -DSIM_ENABLE_IFPS_GPU_PLUGIN=OFF `
  -DSIM_BUILD_TESTS=ON

& "$vsCMake\cmake.exe" --build build\ifps-cpu --config Release `
  --target sim_collision_ifps_cpu sim_core_tests --parallel 2
```

如果 CMake 找不到 CUDA，可在 configure 时显式传入：

```powershell
-DCUDAToolkit_ROOT="C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.x"
```

不要把机器相关 CUDA 路径写死到业务源码。

### 3.2 Linux 或其他生成器

CPU 插件使用普通 C++20；GPU 插件仍通过 `find_package(CUDAToolkit)` 获取 Driver API 和
NVRTC，不要求把 CUDA 加入 CMake project language：

```bash
cmake -S . -B build/ifps -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DSIM_ENABLE_ASSIMP=ON \
  -DSIM_ENABLE_IFPS_CPU_PLUGIN=ON \
  -DSIM_ENABLE_IFPS_GPU_PLUGIN=ON \
  -DSIM_BUILD_TESTS=ON

cmake --build build/ifps \
  --target sim_collision_ifps_cpu sim_collision_ifps_gpu sim_core_tests
ctest --test-dir build/ifps --output-on-failure
```

没有 NVIDIA/CUDA 的机器将 GPU 开关设为 `OFF`。manifest 当前声明 IFPS-CPU 支持
`windows-x64/linux-x64/linux-arm64`，IFPS-GPU 支持 `windows-x64/linux-x64`；
新增平台时要同步 `PluginManager::current_platform()` 和 manifest。

### 3.3 安装

```powershell
& "$vsCMake\cmake.exe" --install build\windows-full `
  --config Release --prefix F:\RobotSimPlatform\install
```

Windows 插件安装到 `install/bin/plugins`；Linux shared library 安装到
`install/lib/sim-platform/plugins`，manifest 安装规则当前使用 `bin/plugins`。打包时要
确认运行入口扫描的目录同时包含 manifest 和动态库，必要时在部署阶段将二者复制到同一目录。

## 4. 插件发现与运行

运行程序按以下优先级确定插件目录：

1. C/Python 调用显式传入的 plugin directory；
2. 环境变量 `SIM_PLUGIN_DIR`；
3. 构建时写入的默认插件目录。

从非标准目录运行时：

```powershell
$env:SIM_PLUGIN_DIR='F:\RobotSimPlatform\build\windows-gui\Release\plugins'
.uild\windows-gui\Release\simulator.exe backends check
```

编辑器在 **Backends** 面板中选择 `IFPS-CPU` 或 `IFPS-GPU`。Python 无窗口 SDK：

```python
from sim_platform import Simulator

with Simulator(
    physics="none",
    collision="ifps-cpu",
    plugin_directory=r"F:\RobotSimPlatform\build\windows-gui\Release\plugins",
) as simulator:
    world = simulator.create_world()
```

benchmark 的 physics 和 collision backend 独立选择：

```powershell
python examples/python/benchmark_collision_grid_headless.py `
  --backend mujoco --collision-backend ifps-cpu

python examples/python/benchmark_collision_grid_gui.py `
  --backend mujoco --collision-backend ifps-gpu
```

## 5. IFPS 原理

IFPS 将碰撞检测分成离线球壳生成和运行时层级查询。它近似的是物体表面，不是实体体积的
精确 signed-distance field。

### 5.1 确定性表面采样

`ICollisionBackend::build()` 触发离线阶段：

1. 使用固定随机种子 `0` 生成 1,000,000 个表面点；
2. Mesh 按三角形面积加权采样；
3. Box、Cylinder、Capsule 按各表面面积采样；
4. 在有序点列表开头插入局部原点；
5. 继续选择 `N` 个最远点，最终得到 `N + 1` 个有序点。

第一个局部原点参与距离更新。第 `N + 1` 个点不作为叶球中心，但用于决定最后一级的覆盖
半径，因此不能只生成 `N` 个 ordered points。

### 5.2 inverse FPS 半径

对第 `i` 个有序点，先计算它到此前点集合 `[0, i)` 的最近平方距离 `d_i^2`，然后按
参考实现计算：

```text
r_i = sqrt(1.05 * d_i^2) + 0.001
```

`1.05` 是平方距离安全系数，`0.001` 是局部空间 margin。不要误写成
`1.05 * sqrt(d_i^2)`，两者数值不同。

解析 Sphere 不执行表面采样，始终使用一个位于局部原点、半径为 Collider radius 的精确球。

### 5.3 层级树

层级使用 ordered prefix：

```text
1 -> 8 -> 64 -> 512 -> ... -> N
```

`N` 可以是任意正整数，最后一级允许未填满。每一级节点分配给最近的上一级中心，层级半径
继续使用参考仓库 `calCuTreeRadius()` 对应的扩张规则。节点保存连续的
`first_child/child_count` 区间，CPU 和 GPU 都依赖这个布局。

### 5.4 运行时查询

`sync_from_scene()` 只更新世界空间球：

- 球心应用 Entity world translation、rotation 和逐轴 scale；
- 半径乘 `max(abs(scale.x), abs(scale.y), abs(scale.z))`；
- 非均匀缩放使用最大轴是保守策略，避免缩放导致漏包围。

两个对象先检查根球。根球相交时，从半径更大的非叶节点一侧展开子节点；球对不相交就剪枝；
两个叶球仍相交则报告碰撞。CPU batch query 先做根球 broad phase，并避免对请求集合中的对称
对象对重复计算。

`distance()` 返回叶球之间的最小近似间距，不是 Mesh 精确距离。IFPS 不提供 raycast，
Camera/LiDAR 仍应使用 Vulkan、FCL 或 MuJoCo 对应能力。

## 6. 增量缓存

`IFPSSphereTreeCache` 按几何版本保存：

- Collider shape 和 size；
- Mesh resource 路径；
- Mesh 文件大小和最后修改时间；
- 1,000,000 个表面采样点；
- 已生成的 ordered FPS 前缀；
- 每个表面点到当前已选集合的最近平方距离；
- 每个请求过的叶球数对应的完整树。

`ifps_sphere_count`、Transform、摩擦和 restitution 不属于几何键。相同几何、不同球数或不同
世界 Transform 的 Collider 可以复用同一生成状态。

缓存行为：

- 已缓存精确球数：直接返回树副本；
- 请求更少球：从已有 ordered prefix 组装并保存新树，不重新采样或运行 FPS；
- 请求更多球：从当前最大 prefix 继续更新最近距离并选择新点；
- `256 -> 1024 -> 256` 会同时保留 256 和 1024 两棵树；
- Mesh 文件大小或修改时间改变时建立新几何版本，避免复用旧球壳。

缓存是 backend 实例内的内存缓存，不写磁盘，也不在 IFPS-CPU 与 IFPS-GPU 实例之间共享。
销毁 backend 会释放缓存。当前没有 eviction；若引入长期运行且几何版本很多的服务，应同时
设计内存预算、LRU 和可观测指标，不能只删除旧树而保留无界表面采样。

## 7. GPU 路径

创建 IFPS-GPU backend 时：

1. 初始化 CUDA Driver API 和 primary context；
2. NVRTC 编译内嵌 kernel，目标为 `compute_52`；
3. 加载 PTX 并查询 `TraverseSceneBatch`。

第一次大查询包含 NVRTC、显存分配和上传成本，benchmark 应先 warm up。

GPU scene 数据：

- local-space spheres 和 object table 仅在 topology generation 改变时上传；
- Transform 单独上传；
- query 只上传请求对象索引，结果每个请求对象返回一个标志；
- kernel 每个 work item 处理一个“请求对象/场景对象”对；
- block size 为 256 threads。

当 `query_count * scene_object_count < 32768` 时，IFPS-GPU 主动走 CPU traversal。小场景中
GPU 启动与同步成本通常高于遍历成本，这不是 GPU 插件失效。

GPU kernel 使用 `float` 和容量为 64 的局部遍历栈。栈容量不足时保守返回碰撞，允许误检但
避免因丢弃待处理节点造成漏检。修改树分支、深度或 kernel stack 时，必须保持这一保守性质。

## 8. 逐物体球数

`ColliderComponent::ifps_sphere_count` 控制叶球数：

- `0`：使用 backend 默认值 256；
- `1..1,000,000`：使用指定数量；
- 解析 Sphere 始终退化为一个精确球。

C API：

```c
uint32_t count = 0;
sim_set_ifps_sphere_count(simulator, world, entity, 1024);
sim_get_ifps_sphere_count(simulator, world, entity, &count);
```

Python：

```python
box = world.create_primitive(
    "box", "box", (1.0, 1.0, 1.0), ifps_sphere_count=256)
box.ifps_sphere_count = 1024
box.ifps_sphere_count = 256
```

修改球数会令活动 collision backend 在下一次 rebuild 时选择对应缓存。状态文件会保存该字段。

## 9. 编辑器可视化

选中带 Collider 的 Entity，打开 **IFPS Visualization**：

- **Visible for selected object**：开启球壳；
- **Leaf Spheres**：输入正整数叶球数；
- **Levels**：可同时选择多个层级，每层颜色不同；
- **Wireframe**：默认模式，绘制精确透视切触轮廓和三组 64 段大圆；
- **Solid**：填充与 Wireframe 相同的透视切触轮廓。

可视化调用同一个 `IFPSSphereTreeCache` 生成树，但它与 collision backend 各自持有缓存。
球壳绘制是 Qt overlay，不参与碰撞判定；判断算法问题时，应同时检查树数据和 backend 查询，
不能只依据屏幕像素。

## 10. 如何修改

### 修改离线球心或半径

首先修改 `src/ifps.cpp` 中的：

- `SurfacePoints()`：表面采样；
- `IFPSSphereTreeCache::get()`：增量 inverse FPS；
- `AssembleSphereTree()`：层级和父子布局。

必须满足：

1. 与原 IFPS 仓库同输入时保持确定性；
2. 冷生成和从较小 prefix 增量生成得到相同 ordered points/radii；
3. 任意非 2 次幂叶球数仍能构树；
4. 若新参数影响采样或球壳，必须加入 cache geometry key；
5. CPU/GPU 消费的 `first_child/child_count` 布局不变，或同步修改两端。

### 修改 CPU 查询

修改 `IFPSCollisionBackend::hierarchical_collision()`、
`batch_candidates()` 和 `check_scene_collisions()`。不要把 offline generation 时间混入
`collision_check` benchmark；backend rebuild 应单独计量。

### 修改 GPU 查询

修改 `plugins/ifps_gpu_backend.cpp` 的宿主镜像结构和 `kKernelSource` 时：

1. 同步 `DeviceSphere/DeviceObject/DeviceTransform` 两份定义；
2. 保留或更新 `static_assert(sizeof(...))`；
3. 同步 host upload 和 kernel field 顺序；
4. 对 stack overflow、索引溢出采取保守结果或显式错误；
5. 同时验证 CPU fallback 和超过 32768 work items 的真实 GPU 路径。

### 修改插件 ABI 或 manifest

插件入口位于 `plugins/ifps_*_backend.cpp`。动态库必须导出：

```text
SimPluginApiVersion
SimCreatePlugin
SimDestroyPlugin
SimCreateCollisionBackend
SimDestroyCollisionBackend
```

改变公共插件 ABI 时，需要同步：

- `SIM_PLUGIN_API_VERSION`；
- 两个 manifest 的 `plugin_api_version`；
- PluginManager、样例插件和插件测试。

只修改 IFPS 算法或内部类不应提升插件 ABI 版本。

### 修改球数接口

逐层检查：

1. `ColliderComponent` 和 `.simstate` 版本兼容；
2. 编辑器 Inspector 与可视化 cache invalidation；
3. C API get/set 和 `sim_api_version()`；
4. Python ctypes `argtypes`、属性验证和创建参数；
5. backend rebuild 标志；
6. C++ round-trip 和 Python SDK 测试。

## 11. 验证矩阵

### C++ 与插件

```powershell
$env:MSBUILDDISABLENODEREUSE='1'

& "$vsCMake\cmake.exe" --build build\windows-full --config Release `
  --target sim_core_tests sim_collision_ifps_cpu sim_collision_ifps_gpu --parallel 2

& "$vsCMake\ctest.exe" --preset windows-full-release
```

`TestIFPSTreeInvariants` 应覆盖：

- 非 2 次幂末层；
- 节点 child range；
- `100 -> 64 -> 128 -> 100` 缓存保留与增量扩展。

插件测试应覆盖 CPU/GPU 对分离、相交、near-contact 和 batch query 的一致结果。GPU 测试要
包含超过 threshold 的 batch，否则只验证了 GPU plugin 的 CPU fallback。

### Python

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH='F:\RobotSimPlatform\python'
$env:SIM_PLATFORM_LIBRARY='F:\RobotSimPlatform\build\windows-full\Release\sim_platform_c.dll'
$env:SIM_PLUGIN_DIR='F:\RobotSimPlatform\build\windows-full\Release\plugins'
python -m pytest -q python/tests
```

### Benchmark 判读

benchmark 将成本拆成：

- `physics_tick`：physics backend；
- `collision_sync`：world transform 到 collision backend；
- `collision_check`：查询本身；
- `render`：GUI/Vulkan。

比较 IFPS 与 FCL 时必须保持 physics、球数、帧数、场景和渲染模式一致。首次 IFPS build、
GPU NVRTC 编译和 topology upload 不应被误当成稳定态 query 性能。

## 12. 常见问题

### CMake 找不到 CUDAToolkit

确认 Toolkit 安装包含 `include/cuda.h`、NVRTC 和 Driver import library，然后设置
`CUDAToolkit_ROOT`。只有 NVIDIA Driver 而没有 Toolkit 不能构建 GPU 插件。

### IFPS-GPU manifest 存在但 plugin creation failed

常见原因是 CUDA Driver 不可用、NVRTC DLL 不在搜索路径、PTX 编译失败或 GPU 不支持当前
环境。先运行 `nvidia-smi`，再检查程序日志中的 NVRTC/Driver 错误。插件工厂当前将构造
异常转换为 creation failure，因此必要时在 `CompileKernel()` 周围增加临时日志定位。

### IFPS-GPU 比 CPU 慢

先计算 `query_count * scene_object_count`。小于 32768 时设计上使用 CPU；刚超过阈值时也
可能因上传、launch 和同步成本更慢。使用复杂 Mesh、大 batch、warm-up 后的多帧统计判断。

### Mesh 结果像 Box

检查 configure cache 中 `SIM_ENABLE_ASSIMP=ON`，并确认 Mesh 路径可读。Assimp 未启用或
导入失败时会进入 Box fallback。

### 修改球数后第一次卡顿

新球数第一次需要组装树；若超过已生成的最大 prefix，还需要继续扫描一百万表面点。切回已
请求过的数量会直接使用缓存。当前缓存不持久化到磁盘，重启进程后仍是冷生成。

### 插件找不到或 API mismatch

确认 DLL 与 `.simplugin.json` 同目录、manifest 中库名正确、平台列表包含当前平台，并且
`plugin_api_version` 与 `SIM_PLUGIN_API_VERSION` 一致。不要把 C API 的
`sim_api_version()` 与插件 API 版本混为一谈。

### Release 链接报 LNK1104

`sim_editor.exe` 或插件 DLL 正被运行中的编辑器占用。关闭该进程后重新构建；不要删除整个
构建目录来掩盖文件锁问题。

## 13. 已知边界

- IFPS 是近似表面球壳，允许少量精度损失和保守误检；
- 极端高速离散运动仍需 physics backend 的 CCD 或更小 time step，IFPS 本身不做连续碰撞；
- `distance()` 不是精确 Mesh distance；
- 不支持 raycast；
- GPU 离线生成仍在 CPU；
- CPU/GPU backend 实例不共享缓存，也没有磁盘缓存；
- 缓存当前无 eviction，球数和几何版本很多时会增加内存；
- GPU kernel 使用单精度，CPU 使用 double，边界接触处允许出现浮点差异。
