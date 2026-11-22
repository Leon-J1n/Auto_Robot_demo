# C++ API 速查手册

## 1. 引入与链接

原生 API 位于 `include/sim`，命名空间为 `sim`，C++ 标准为 C++20。CMake 工程内使用：

```cmake
target_link_libraries(my_target PRIVATE sim::core)
```

常用头文件：

```cpp
#include <sim/core.hpp>       // Simulator、World、调度、事件、命令、Backend 配置
#include <sim/assets.hpp>     // URDF/OBJ/Assimp、AssetIR、ResourceManager
#include <sim/robot.hpp>      // Robot、关节控制、FK、Jacobian
#include <sim/physics.hpp>    // IPhysicsBackend、MuJoCoBackend
#include <sim/plugin.hpp>     // PluginManager、插件式 Physics Backend 工厂
#include <sim/collision.hpp>  // FCLBackend、FCLQueryContext
#include <sim/render.hpp>     // VulkanBackend、RenderSnapshot
#include <sim/sensors.hpp>    // Camera/LiDAR/Joint State Sensor
#include <sim/state_io.hpp>   // 版本化状态文件
```

可选后端类仅在对应宏存在时可见：`SIM_HAS_MUJOCO`、`SIM_HAS_BULLET_PLUGIN`、`SIM_HAS_FCL`、`SIM_HAS_VULKAN`、`SIM_HAS_ASSIMP`、`SIM_HAS_URDFDOM`。

## 2. 最小生命周期

```cpp
#include <sim/core.hpp>
#include <sim/assets.hpp>
#include <sim/robot.hpp>

int main() {
    sim::Simulator simulator({
        .physics = sim::BackendKind::MuJoCo,
        .collision = sim::BackendKind::FCL,
        .render = sim::BackendKind::Vulkan,
    });
    auto& world = simulator.create_world(0.002, 42);
    sim::InstallRobotControlSystem(world);  // 仅自行组织纯 Core 控制链路时使用

    sim::ResourceManager resources(".sim-cache");
    const auto asset = resources.load("robot.urdf");
    const sim::EntityId root = resources.instantiate(world, asset.asset);
    sim::Robot robot(world, root);

    robot.command_joint_positions(std::array{0.0, 0.5});
    world.start();
    world.step(100);
}
```

注意：原生 `Simulator` 保存后端配置，但不会自动拥有 C API 那套后端生命周期封装。直接使用 C++ 时，应由调用者创建后端、调用 `build()`，并把 `step/sync` 接到 Scheduler 或显式调用。若只需要完整封装，优先使用稳定 C ABI 或 Python SDK。

## 3. 基础类型与约定

| 类型 | 说明 |
|---|---|
| `EntityId`、`WorldId` | `uint64_t` 稳定句柄；0 为无效值 |
| `Vec3` | 三个 `double`；位置/长度单位为米 |
| `Quaternion` | 顺序为 `w,x,y,z` |
| `Transform` | `translation + rotation + scale`；父子组合用 `operator*` |
| 关节位置/速度 | 弧度、弧度每秒；Prismatic 为米、米每秒 |
| 时间 | 秒；默认固定步长 0.002 s |
| `Error` | 带 `ErrorCode` 的异常；参数、状态、后端、IO 错误均通过异常报告 |

坐标约定为右手系、X 前、Y 左、Z 上。`TransformComponent.local` 相对父节点，`world` 为缓存的世界变换。

## 4. Simulator 与 World

### `sim::Simulator`

| API | 用途 |
|---|---|
| `Simulator(BackendConfig)` | 校验后端组合并创建容器 |
| `create_world(dt, seed)` | 创建 World，返回引用 |
| `destroy_world(id)` | 删除 World |
| `world(id)` | 按句柄获取 World |
| `backend_config()` | 查看构造时配置 |

`BackendKind::Bullet3` 表示 Physics Backend；`BackendKind::Bullet3Collision`、
`BackendKind::Coal` 表示独立 Collision Backend。C API 对应 `SIM_PHYSICS_BULLET3`、
`SIM_COLLISION_BULLET3` 与 `SIM_COLLISION_COAL`，当前 `sim_api_version()` 返回 5。

### `sim::World`

| API | 用途 |
|---|---|
| `create_entity(name, parent)` | 创建带 Identity/Name/Transform/Hierarchy 的实体 |
| `destroy_entity(entity)` | 递归销毁实体及子树 |
| `set_parent(child, parent)` | 修改层级并防止循环 |
| `update_world_transforms()` | 从根节点向下刷新世界变换 |
| `start()` / `pause()` | 修改仿真状态 |
| `step(count)` | 固定时间步执行 Scheduler；即使 Paused 也可显式 Step |
| `reset()` | 恢复最近一次 `capture_initial_state()` |
| `snapshot()` / `restore()` | 内存级完整 World 状态 |
| `capture_initial_state()` | 设置 Reset 基准 |

辅助访问器：`registry()`、`frames()`、`time()`、`events()`、`commands()`、`scheduler()`、`status()`。

## 5. ECS 与组件

### `sim::Registry`

```cpp
auto entity = world.create_entity("ball");
auto& registry = world.registry();

registry.emplace<sim::ColliderComponent>(entity,
    sim::ColliderComponent{sim::ColliderShape::Sphere, {0.5, 0.5, 0.5}});
registry.emplace<sim::RigidBodyComponent>(entity,
    sim::RigidBodyComponent{1.0, {}, {}, true, true});

if (registry.has<sim::RigidBodyComponent>(entity)) {
    registry.get<sim::RigidBodyComponent>(entity).mass = 2.0;
}
for (auto id : registry.view<sim::TransformComponent, sim::ColliderComponent>()) {
    // 类型化查询
}
```

核心方法：`create/create_with_id`、`destroy`、`valid`、`emplace<T>`、`has<T>`、`get<T>`、`remove<T>`、`view<T...>`、`entities`、`clone`。

主要组件：

| 组件 | 关键字段 |
|---|---|
| `TransformComponent` | `local`, `world` |
| `HierarchyComponent` | `parent`, `children` |
| `VisualComponent` | mesh/texture、shape、size、color、`min_lod/max_lod` |
| `LightComponent` | `type`、RGB `color`、`intensity`、`range`、`decay`、Spot 锥角、Directional 角直径、`enabled` |
| `ColliderComponent` | shape、size/mesh、trigger、三参数 friction、restitution、逐物体 `ifps_sphere_count` |
| `RigidBodyComponent` | mass、线/角速度、dynamic、gravity_enabled |
| `RobotComponent` | model_resource、links |
| `JointComponent` | type、父/子 Link、轴、原点、限位、状态 |
| `JointControlComponent` | Disabled/Position/Velocity/Effort、target、gain、速度上限 |
| `Sensor/Camera/LiDAR/JointStateSensor` | 传感器配置与关联实体 |

修改 Transform、Collider、RigidBody 或资产层级后，必须刷新 World Transform，并重建或同步受影响的后端。

### 光照

`LightType` 包括 `Point`、`Spot` 和 `Directional`（平行/天空光）。原生 C++ 通过 ECS
Registry 创建和修改光源：

```cpp
const auto spot = world.create_entity("Spotlight");
world.registry().emplace<sim::LightComponent>(spot, sim::LightComponent{
    .type = sim::LightType::Spot,
    .intensity = 1.0F,
    .range = 10.0F,
    .decay = 2.0F,
    .inner_cone_radians = 0.35F,
    .outer_cone_radians = 0.7F,
});
world.update_world_transforms();
```

`color` 为线性 RGB，`intensity` 为强度，`range`/`decay` 仅影响点光和聚光灯；
`inner_cone_radians` 与 `outer_cone_radians` 是从局部轴量起的半角，必须满足
`0 <= inner <= outer < pi/2`。Spotlight 的局部 `-Z` 是发光正面；Directional 光源沿其
局部 `+X` 发射，`angular_diameter_radians` 控制其角直径。没有启用的光源时渲染结果为黑色。

`IFPSSphereTreeCache::get(collider,count)` 会保留每种几何体的表面采样、可增量扩展的 FPS
前缀和所有请求过的树大小。C ABI 使用 `sim_set_ifps_sphere_count()` /
`sim_get_ifps_sphere_count()` 设置或读取逐实体配置；`0` 表示 backend 默认值 256。

## 6. Frame Tree、命令、事件与 Scheduler

### Frame Tree

```cpp
world.frames().set_frame("camera", "world", camera_pose);
sim::Transform world_from_camera = world.frames().lookup("camera", "world");
```

`set_frame` 会检查父帧存在和循环；`lookup(source,target)` 返回目标帧中的 source 变换。

### 命令队列

`CommandPayload` 支持创建/销毁实体、设置 Transform、Start、Pause、Reset。`submit()` 返回递增序号，`World::step()` 在 CommandUpdate 阶段处理到期命令。

### EventBus

```cpp
auto subscription = world.events().subscribe("sensor.data_ready", handler);
world.events().publish({"custom", world.id(), entity, world.time().state().simulation_time, "detail"});
world.events().dispatch();
```

空订阅类型表示接收全部事件。

### Scheduler 阶段顺序

`PreUpdate -> CommandUpdate -> ControllerUpdate -> PrePhysics -> PhysicsStep -> PostPhysics -> CollisionSync -> SensorUpdate -> RenderSync -> Render -> EventDispatch -> PostUpdate`。

`add_system(phase,name,fn,priority)`；同一阶段按 priority 执行。

## 7. 资源导入与实例化

```cpp
sim::ResourceManager resources(".sim-cache");
sim::ImportResult result = resources.load("model.urdf");
for (const auto& diagnostic : result.diagnostics) { /* 检查告警 */ }
sim::EntityId root = resources.instantiate(world, result.asset, "optional_root_name");
```

| API | 说明 |
|---|---|
| `ResourceManager::load/load_async` | 按扩展名导入并使用 Baking Cache |
| `instantiate` | 将 AssetIR 转为 ECS 层级、Link/Joint、Visual/Collider |
| `UrdfImporter` | URDF、package URI、visual/collision/inertial/joint |
| `ObjImporter` | 内置 OBJ 路径 |
| `AssimpImporter` | FBX/OBJ/STL 等通用网格 |
| `AssetValidator` | 校验并统一单位 |
| `GenerateMeshLod(mesh, 0..7)` | 生成视觉 LOD；0 最精细，7 最粗 |

缓存命中由源文件和依赖内容哈希决定。删除 `.sim-cache` 可强制重新导入。

## 8. Robot API

```cpp
sim::Robot robot(world, robot_root);
auto state = robot.joint_state();
robot.set_joint_positions(targets);                       // 立即设置
robot.command_joint_positions(targets, 20.0, 1.0);       // 位置控制
robot.command_joint_velocities(velocities);               // 速度控制
robot.command_joint_efforts(efforts);                     // 力矩控制
auto pose = robot.forward_kinematics(end_link);
auto jacobian = robot.jacobian(end_link);                  // 每列 [vx,vy,vz,wx,wy,wz]
```

目标数组顺序由 `robot.joints()` / `joint_state().names` 决定，包含导入模型中的 Fixed Joint。位置目标必须满足非 Continuous 关节限位。纯 Core 控制需要安装 `InstallRobotControlSystem(world)`；MuJoCo 后端内部也会处理控制组件。

## 9. 后端 API

### MuJoCo

```cpp
sim::MuJoCoBackend physics;
physics.build(world);
physics.step(world);
physics.apply_body_wrench(entity, force, torque); // 下一步消费一次
auto bytes = physics.save_state();
physics.restore_state(bytes);
physics.sync_to_scene(world);
```

当前机器人 collision 会直接读取 URDF 指定的原始 STL；运行时校验源 STL 面数与 MuJoCo 模型面数一致。平台不应用碰撞 LOD/代理球。MuJoCo 标准 Mesh 碰撞仍遵循引擎自身的凸碰撞语义。

### Bullet3 插件

```cpp
sim::PluginManager plugins;
plugins.discover("plugins");
auto physics = plugins.create_physics_backend("sim.physics.bullet3");
physics->build(world);
physics->step(world);
physics->apply_body_wrench(entity, force, torque);
```

插件服务持有动态库；`physics` 未释放前卸载会返回 `InvalidState`。Bullet3 当前提供普通刚体、运动学 Robot collider、Primitive/Mesh、重力/材质/外力和状态保存恢复，不提供 articulated constraint；精确碰撞查询继续组合 FCL。

### FCL

```cpp
sim::FCLBackend collision;
collision.build(world);
bool hit = collision.check_collision(a, b);
double gap = collision.distance(a, b);
collision.sync_from_scene(world);

auto query = collision.create_query_context();
query.set_transform(candidate, proposed_pose);
bool proposed_hit = query.check_scene_collision(candidate); // 不修改主场景
```

机器人规划使用 `check_robot_configuration` 或扁平数组形式的 `check_robot_configurations`。

### Vulkan

```cpp
sim::VulkanBackend renderer;
renderer.sync_from_scene(world);
renderer.render_camera(camera_pose, camera, rgb, depth);
const auto& snapshot = renderer.snapshot();
```

RGB 为连续 `uint8 RGB`，Depth 为米制线性 `float`。GUI 当前通过离屏 Vulkan 帧显示到 Qt，而非原生 Swapchain。

## 10. Sensor API

```cpp
auto provider = std::shared_ptr<sim::ISensorDataProvider>(/* FCL/MuJoCo/Vulkan 或组合 */);
sim::SensorSystem sensors(world, provider);
auto camera = sensors.create_camera("camera", parent, "camera", 30.0, {});
auto lidar = sensors.create_lidar("lidar", parent, "lidar", 10.0, {});
auto joint_sensor = sensors.create_joint_state_sensor("joints", robot_root, "base", 100.0);
sensors.update();
const auto& frame = sensors.buffer(camera).latest<sim::RGBFrame>();
```

Camera 需要能提供 RGB/Depth 的 Provider；LiDAR 使用 Provider 的 raycast。

## 11. 状态文件

```cpp
sim::SimulationStateArchive archive{1, world.snapshot(), physics.save_state()};
sim::SaveStateFile("scene.simstate", archive);
auto restored = sim::LoadStateFile("scene.simstate");
world.restore(restored.world);
physics.build(world);
physics.restore_state(restored.backend_state);
```

文件包含 ECS、层级、Frame Tree、时间、随机数和可选后端状态。格式有版本检查；不要手工编辑二进制 `.simstate`。

## 12. 稳定 C ABI

头文件为 `include/sim/c_api.h`，库为 Windows 下的 `sim_platform_c.dll`。所有返回 `int` 的函数：0 成功，非 0 失败；失败后立即读取 `sim_last_error()`。`sim_destroy()` 释放总句柄。

函数按领域分组：

- 生命周期：`sim_create`、`sim_create_with_plugins`、`sim_destroy`、`sim_create_world`。`SimPhysicsBackend` 支持 `NONE/MUJOCO/BULLET3`；后者可显式传插件目录。
- 场景：`sim_load_asset`、`sim_create_entity`、`sim_set/get_transform`、`sim_set_transforms`。
- 光照：`sim_create_light`、`sim_set_light_properties`、`sim_get_light_properties`；属性结构为
  `SimLightProperties`，类型为 `SimLightType`（Point/Spot/Directional），所有锥角和角直径使用弧度。
- 机器人：关节数量/UTF-8 名称/状态、位置/速度命令、批量位置命令、配置碰撞。
- 传感器：创建 Camera/LiDAR，读取内参、RGB、Depth、点云。
- 碰撞/动力学：collision、distance、apply_body_wrench。
- 仿真：start、pause、status、step、time、event、reset。
- 状态：内存句柄和文件保存/恢复。

ABI 内部以 mutex 串行保护同一个 `SimHandle`；原生 C++ 对象本身不承诺跨线程安全。
