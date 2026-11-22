# 关键实现结构

## 1. 总览

当前实现是“单一 Core 静态/动态库 + 编译期可选 Core 后端 + 动态 Physics/Collision 插件 + 两个外部控制入口”。

```text
                         +-------------------------+
                         |     C++ sim::core       |
                         | World / ECS / Asset IR  |
                         | Robot / Sensor / State  |
                         +-----------+-------------+
                                     |
              +----------------------+----------------------+
              |                      |                      |
       MuJoCo physics          FCL collision          Vulkan render
       or Bullet3 plugin       or Bullet3/Coal/IFPS    vulkan_backend.cpp
              |                      |                      |
              +----------------------+----------------------+
                                     |
          +--------------------------+--------------------------+
          |                                                     |
  C ABI / Headless Python                                  Qt Editor
  c_api.cpp -> sdk.py -> cuRobo (optional CUDA)    editor.cpp <- editor.py
          |                                                     |
  server.py Agent JSONL                                 local TCP JSONL
```

## 2. 分层边界

| 层 | 职责 | 主要文件 |
|---|---|---|
| 基础类型 | 数学、错误、诊断、日志、配置、Profiler | `types.hpp`, `foundation.hpp/.cpp` |
| ECS/场景 | Entity、组件池、层级、Frame Tree、World Transform | `ecs.hpp`, `components.hpp`, `core.cpp` |
| 仿真内核 | 固定步长、阶段调度、命令、事件、快照、World 生命周期 | `core.hpp/.cpp` |
| 资源 | AssetIR、URDF/OBJ/Assimp、校验、缓存、ECS 实例化 | `assets.hpp/.cpp` |
| 机器人 | Joint 顺序、状态、位置/速度/力矩控制、FK/Jacobian | `robot.hpp/.cpp` |
| 传感器 | Camera/LiDAR/Joint State 调度和缓冲 | `sensors.hpp/.cpp` |
| 物理 | 通用后端接口、ECS -> MuJoCo 或 Bullet3、接触、状态同步 | `physics.hpp`, `mujoco_backend.cpp`, `plugins/bullet_backend.cpp` |
| 碰撞 | ECS -> FCL/Bullet3/Coal/IFPS、距离、场景批检；可选 cuRobo GPU 配置批检 | `collision.hpp`, `fcl_backend.cpp`, `plugins/*_collision_backend.cpp`, `plugins/ifps_*`, `curobo_backend.py` |
| 渲染 | ECS -> RenderSnapshot、Mesh/LOD、Vulkan 离屏帧 | `render.hpp`, `vulkan_backend.cpp`, `shaders/*` |
| 状态文件 | 组件序列化、版本检查、后端字节块 | `state_io.hpp/.cpp` |
| 集成边界 | C ABI、Python ctypes、Agent RPC、CLI、Qt GUI | `c_api.cpp`, `sdk.py`, `server.py`, `cli.cpp`, `editor.cpp`, `editor.py` |

Core 不依赖 Qt；GUI 只是一种应用层组合。Python `sdk.py` 不导入 Qt，也不经过 GUI。

## 3. ECS 与 World 的真实结构

### 实体最小组件

`Registry::create()` 自动添加：

```text
IdentityComponent
NameComponent
TransformComponent { local, world }
HierarchyComponent { parent, children }
```

其余 Visual、Collider、RigidBody、Robot、Joint、Control、Sensor 均按需添加。Registry 使用 `type_index -> Pool<T>`，Entity 销毁时从所有 Pool 擦除。

### 层级与 Transform

`World::update_world_transforms()` 找到 parent=0 的根节点，递归执行：

```text
child.world = parent.world * child.local
```

不要直接长期修改 `TransformComponent.world`；编辑或控制应修改 `local`，然后刷新。后端同步回场景是例外：动态刚体由物理世界写回 World Transform。

### World 初始化

`World` 构造函数安装：

- CommandUpdate：执行到期命令；
- EventDispatch：分发 EventBus；
- 默认 Frame Tree 根 `world`；
- 固定时间步和随机数状态。

Robot 基础控制器不是 World 内建；由 `InstallRobotControlSystem()` 安装，或由 MuJoCo 后端控制链处理。

## 4. 一次固定步的控制流

`World::step(count)` 每步的核心顺序：

```text
TimeManager.advance()
  -> SimulationScheduler.run(dt)
       PreUpdate
       CommandUpdate        执行命令队列
       ControllerUpdate     基础 Robot 控制器
       PrePhysics
       PhysicsStep          C API 模式下挂 MuJoCoBackend::step
       PostPhysics
       CollisionSync        C API 模式下挂当前 Collision Backend::sync_from_scene
       SensorUpdate         SensorSystem::update
       RenderSync
       Render
       EventDispatch
       PostUpdate
```

### C ABI 无窗口路径

`src/c_api.cpp` 中每个 World 有一个 `PhysicsInstance`：通用 `IPhysicsBackend` dynamics、`ICollisionBackend` collision、FCL 专用传感器引用、CompositeSensorProvider、SensorSystem。MuJoCo/FCL 直接构造，Bullet3 Physics、Bullet3 Collision 与 IFPS 由 `PluginManager` 工厂创建。修改资产/Transform 后设置 `dirty=true`；下一次需要后端时 `EnsurePhysics()` 执行 `build()`。

```text
Python Simulator.step
 -> sim_step
 -> EnsurePhysics (若 dirty，重建当前 Physics/Collision Backend)
 -> World.step
 -> Scheduler PhysicsStep: 当前 Physics Backend step + sync_to_scene
 -> Scheduler CollisionSync: 当前 Collision Backend sync
 -> SensorSystem update
```

`SimHandle::mutex` 保护 C ABI 调用，错误由 `Guard()` 捕获到线程局部 `sim_last_error()`。

### GUI 路径

`src/editor.cpp` 的 Qt 定时器调用 `advance_running_simulation()`：按 wall-clock 和 GUI real-time factor 累积应执行的固定步，之后仅在场景/相机签名变化时重绘。

```text
QTimer timeout
 -> advance_running_simulation
 -> step_simulation(N, unpaced)
 -> world.step + physics.step
 -> 当前 Collision Backend sync
 -> scene signature / render dirty 判断
 -> render_viewport
 -> inspector/performance/status 更新
```

GUI 的通用 Physics、Collision、Vulkan 对象由 `apply_backend_configuration()` 创建；Bullet3 Physics、Bullet3 Collision 与 IFPS 通过 `PluginManager` 工厂获得，场景结构变化后 `rebuild_active_backends()`。

## 5. 资源导入控制流

```text
ResourceManager::load(path)
 -> BakingCache::load
    -> 命中：返回 AssetIR
    -> 未命中：AssetImporterRegistry::importer_for(extension)
       -> UrdfImporter / ObjImporter / AssimpImporter
       -> AssetValidator::normalize + validate
       -> BakingCache::store
 -> ImportResult

ResourceManager::instantiate(world, AssetIR)
 -> Robot root / Link Entity
 -> JointComponent 与 Joint local transform
 -> collision 子实体 + ColliderComponent
 -> visual 子实体 + VisualComponent
 -> FBX Node/mesh 层级
 -> world.update_world_transforms
```

### URDF

`UrdfImporter` 负责 package URI 解析、Link/Joint、visual/collision、origin、mesh scale、材质与 inertial。`instantiate()` 为 collision 和 visual 建独立子实体，因此 Scene Hierarchy 中能看到真实层级。

### FBX/OBJ/STL

Assimp 导入为 `AssetIR.nodes/meshes/materials`。FBX 做米制和坐标归一；Vulkan 根据 `#mesh=N` 选择一个源文件内的具体 Mesh。

### 缓存

Baking Cache 是项目自定义二进制格式，键由绝对路径生成，记录源与依赖指纹。修改缓存格式时必须同步版本常量和测试；旧缓存会失效重建。

## 6. Robot 与控制

`Robot::joints()` 从 `RobotComponent.links` 中选择具有 `JointComponent` 的实体，保持导入顺序；因此 Fixed Joint 也可能在数组中。

### 基础控制器

`InstallRobotControlSystem()` 在 ControllerUpdate 遍历 Joint + JointControl：

- Position：比例误差生成速度，受 velocity_limit 限制，越过目标时钳到目标；
- Velocity：每步积分位置，非 Continuous 关节受上下限约束；
- Effort：写入 effort；
- 每步调用 `ApplyJointTransform()` 更新 Joint 子树 local transform。

### MuJoCo 控制

`MuJoCoBackend::step()` 映射 ECS Joint 和 MuJoCo qpos/qvel。当前策略：

- 未命令的机器人关节保持导入姿态；
- Position/Velocity 使用确定性目标更新，保证 GUI 与 SDK 状态一致；
- Continuous 不设置 MuJoCo range；
- Effort 进入物理求解；
- 机器人子树默认重力补偿，根不生成 freejoint，防止整机坠落；
- 普通 dynamic RigidBody 使用 freejoint。

## 7. MuJoCo 模型生成与碰撞

`MuJoCoBackend::build()` 动态生成内存 MJCF：

```text
ECS root/body hierarchy
 -> body local pos/quat
 -> JointComponent -> hinge/slide joint
 -> RigidBody dynamic -> freejoint（机器人子树除外）
 -> ColliderComponent -> geom
 -> mesh asset
 -> mj_loadXML -> mjModel/mjData
 -> 建立 Entity -> body/joint/geom id 映射
```

关键规则：

- Box/Sphere/Capsule/Cylinder 使用 MuJoCo primitive；ECS world scale 会进入尺寸。
- URDF STL collision 直接以原文件路径交给 MuJoCo，不做应用层 LOD。
- 二进制 STL 源三角形数会与 `mjModel.mesh_facenum` 审计，不一致则 build 失败。
- 单面非 STL 网格会沿法线挤出约 0.1 mm，形成可碰撞薄体。
- 机器人 geom 使用 contact group 1，普通场景对象 group 2：Robot-Robot 不接触，Robot-Scene 和 Scene-Scene 接触。
- Trigger 或无法解析的 Mesh 禁用接触；缺失网格只保留无接触质量占位。
- MuJoCo 标准 Mesh 碰撞内部使用凸碰撞语义；需要精确非凸时应设计凸分解或 SDF 路径。

## 8. FCL 碰撞

`FCLBackend::build()` 为每个 Collider 创建 `fcl::CollisionObjectd`：

- Primitive 直接映射；
- Mesh 用 Assimp 全拓扑创建 `BVHModel<OBBRSS>`；
- `sync_from_scene()` 更新 Transform；
- `FCLQueryContext` 深拷贝对象，用于候选配置测试，不污染主场景；
- Robot configuration 检查克隆 World，设置关节，刷新层级后查询。

FCL 负责精确网格碰撞/距离/规划查询；MuJoCo 负责动力学接触。双后端模式必须在物理步后同步 FCL。

### Bullet3 Collision BVH

`sim.collision.bullet3` 是独立于 `sim.physics.bullet3` 的查询插件：

- `btDbvtBroadphase` 为全部场景对象维护动态 AABB 树；
- Primitive 使用 Bullet 标准 narrow phase；
- Assimp 三角 Mesh 使用 `btGImpactMeshShape` 的内部 BVH；
- `check_scene_collisions()` 一次更新 Collision World，再遍历 manifolds 汇总 batch；
- `sync_from_scene()` 更新 Transform 和 DBVT proxy；
- 复杂 Mesh 的正 distance 在 Bullet 未给出 closest point 时退回 AABB 近似。

详细构建和修改约束见 [Bullet3 Collision Backend](BULLET3_COLLISION_BACKEND.md)。

### Coal Collision BVH

`sim.collision.coal` 为所有对象维护 `DynamicAABBTreeCollisionManager`。Primitive 直接使用 Coal
narrow phase，Assimp Mesh 构建 `BVHModel<OBBRSS>`。场景 batch 通过一次 broad-phase
`collide()` 和自定义 callback 汇总，Transform 同步后统一更新 AABB Tree。详见
[Coal Collision Backend](COAL_COLLISION_BACKEND.md)。

### cuRobo Python/CUDA 适配

`python/sim_platform/curobo_backend.py` 懒加载官方 `nvidia-curobo` 的 `RobotCollisionChecker`。`Robot.use_curobo_collision()` 绑定后，单个/批量配置查询改走 cuRobo，适配器使用 C ABI 暴露的 `joint_names` 将平台数组重排为 cuRobo active joints，因此 Fixed Joint 不会误占 GPU 输入维度。轨迹查询通过 `trajectories_collide()` 直接保留 `[batch,horizon,dof]` 批维。

该适配层不改变 C++ `BackendKind`：cuRobo 是 Python/CUDA 运行时，不是 MSVC 可链接的 Core 后端。它拥有独立的 cuRobo Robot/Scene 配置和 GPU Cache；ECS 场景变化不会自动同步，调用方需执行 `update_scene()`。FCL 继续负责 Entity pair、距离和 LiDAR。

## 9. Vulkan 渲染与 LOD

`VulkanBackend::sync_from_scene()` 遍历可见 Visual/Collider，生成 `RenderSnapshot`、场景 bounds 和 revision。网格资源按路径缓存 GPU Buffer。

LOD 规则位于 `vulkan_backend.cpp`：

- 视觉 `min_lod/max_lod` 约束 0..7；
- 依据投影占屏选择 LOD；占屏较大时使用精细模型；
- `GenerateMeshLod()` 只影响视觉/碰撞线框显示，不再影响 MuJoCo URDF STL collision；
- Qt 视口和 Sensor Camera 都调用 Vulkan 离屏 render/readback。

Shader 源位于 `shaders/basic.vert`、`basic.frag`，CMake 用 `glslc` 生成 SPIR-V。

## 10. GUI 结构

`src/editor.cpp` 当前是单文件 Qt 应用，主要职责块：

| 区域 | 关键函数/状态 |
|---|---|
| 窗口和 Dock | `EditorWindow` 构造函数 |
| Primitive | `create_primitive()` |
| 仿真步 | `step_simulation()`、`advance_running_simulation()` |
| 后端切换 | `apply_backend_configuration()`、`rebuild_active_backends()` |
| 资产/状态 | `load_asset_path()`、save/restore |
| TCP 控制 | `dispatch_control_request()` |
| Scene Tree/Inspector | `refresh_scene()`、inspector apply/refresh |
| 相机/选择/Gizmo | viewport event filter、projection、hit test、drag logic |
| Collider overlay | `collision_wireframe()`、`draw_collider_overlay()` |
| 渲染 | `render_viewport()`、`decorate_viewport()` |

如果 GUI 继续扩大，优先拆分 Viewport、Inspector、ControlServer、BackendController；当前修改小功能时仍应局部补丁，避免无关重构。

## 11. Python 与通信

### 无窗口 SDK

`sdk.py` 只负责 ctypes 类型、参数校验、NumPy Buffer 与面向对象包装。业务语义都在 C++ `c_api.cpp` 和 Core。

### GUI SDK

`editor.py` 启动 `sim_editor.exe --control-port N`，逐行发送：

```json
{"id":1,"method":"scene.set_transform","params":{"entity":1,"translation":[0,0,1]}}
```

服务端在 `editor.cpp::dispatch_control_request()` 处理。新增 GUI SDK 方法必须同时修改：capabilities 列表、C++ dispatch、Python wrapper、样例/测试。

### Agent 服务

`server.py::HeadlessService` 拥有无窗口 Simulator/World，通过 `_invoke()` 分发 Agent JSONL 方法。Schema 位于 `schemas/agent-command.schema.json`。命名共享内存由 Python `multiprocessing.shared_memory` 管理，服务关闭时清理。

## 12. 状态管理

`state_io.cpp` 当前文件格式版本为 8（兼容读取 v2-v7）。每个 Entity 写：句柄、名称、
local/world transform、parent、组件位图、可选组件数据；随后写 Frame Tree 和后端字节块。

修改组件字段时必须：

1. 提升 `kFormatVersion`；
2. 更新 WriteEntity/ReadEntity，并保留旧版本兼容分支或明确拒绝；
3. 更新 `TestVersionedStateFileRoundTrip`；
4. 检查 GUI 与 C API save/restore。

## 13. 插件系统

`PluginManager` 扫描 `*.simplugin.json`，校验平台、API 版本和依赖，通过系统动态库 API
加载 `SimCreatePlugin/SimDestroyPlugin`。测试插件在 `tests/sample_plugin.cpp`。

Bullet3 是首个通过 `PluginManager` 服务工厂创建的后端：manifest 类型为 `physics_backend`，DLL 导出 `SimCreatePhysicsBackend/SimDestroyPhysicsBackend`。返回的 `shared_ptr<IPhysicsBackend>` 持有 DLL 生命周期，服务存活时禁止卸载。

IFPS-CPU 和 IFPS-GPU 的 manifest 类型为 `collision_backend`，通过
`SimCreateCollisionBackend/SimDestroyCollisionBackend` 创建。两者共用 Core 中的确定性
球壳和缓存，GPU 插件用 CUDA/NVRTC 处理大批量运行时遍历。详见
[IFPS 构建与维护](IFPS_COLLISION_BACKENDS.md)。

Coal 同样使用 `collision_backend` manifest 和服务工厂，但其几何及 broad phase 完全由官方
Coal 3.0.4 实现，不与 FCL 或 IFPS 共享运行时状态。

MuJoCo/FCL/Vulkan 仍是 CMake 可选源码模块。Bullet3 插件负责刚体动力学和运动学 Robot
collider；FCL 继续负责精确 Entity pair、距离、规划配置与 LiDAR。插件目录按显式参数、
`SIM_PLUGIN_DIR`、编译默认目录的顺序解析。

## 14. 修改导航表

| 要修改的功能 | 首先查看 |
|---|---|
| 新组件/组件字段 | `components.hpp`、`state_io.cpp`、`editor.cpp`、测试 |
| Entity/层级/步进 | `ecs.hpp`、`core.cpp` |
| URDF/FBX/材质/缓存 | `assets.cpp`、`vulkan_backend.cpp` |
| 关节控制/FK | `robot.cpp`；若涉及物理再看 `mujoco_backend.cpp` |
| 重力/接触/摩擦/反弹 | `mujoco_backend.cpp`、`components.hpp` |
| IFPS 球壳/缓存/CPU/GPU 遍历 | `ifps.hpp`、`ifps.cpp`、`plugins/ifps_*_backend.cpp`、IFPS 维护指南 |
| Coal Dynamic AABB/OBBRSS 查询 | `plugins/coal_collision_backend.cpp`、Coal 维护指南 |
| 碰撞查询/规划 | `fcl_backend.cpp`；GPU 配置批检再看 `curobo_backend.py` |
| 渲染/LOD/纹理/性能 | `vulkan_backend.cpp`、`shaders/*` |
| Camera/LiDAR 数据 | `sensors.cpp`、对应 Provider、`c_api.cpp`/`sdk.py` |
| Python 无窗口 API | `c_api.h`、`c_api.cpp`、`sdk.py`、`test_sdk.py` |
| Python GUI API | `editor.cpp::dispatch_control_request`、`editor.py` |
| GUI 操作与面板 | `editor.cpp` |
| Agent RPC | `server.py`、Schema、`test_server.py` |
| 保存/恢复 | `state_io.cpp`、后端 save/restore |
| 构建依赖/目标 | `CMakeLists.txt`、`CMakePresets.json`、`vcpkg.json` |
