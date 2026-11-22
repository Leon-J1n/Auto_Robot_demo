# Coal Collision Backend 构建与维护

## 1. 插件边界

Coal Collision Backend 是独立插件 `sim.collision.coal`，产物为
`sim_collision_coal.dll` 和 `coal_collision.simplugin.json`。插件只实现
`ICollisionBackend`，不负责刚体动力学。它可以与 MuJoCo、Bullet3 Physics 或无 Physics
模式组合。

主要文件：

| 文件 | 职责 |
|---|---|
| `plugins/coal_collision_backend.cpp` | Shape/Mesh 转换、查询、broad phase 和插件 C ABI |
| `plugins/coal_collision.simplugin.json.in` | 插件发现 manifest |
| `src/c_api.cpp` | C API 枚举和 `sim.collision.coal` 映射 |
| `src/editor.cpp` | GUI 和 benchmark RPC 的 backend 选择 |

## 2. 查询结构

```text
World colliders
  -> coal::DynamicAABBTreeCollisionManager   动态 AABB broad phase
     -> primitive narrow phase              Box/Ellipsoid/Capsule/Cylinder
     -> coal::BVHModel<coal::OBBRSS>         Assimp 三角 Mesh BVH
        -> Coal GJK/EPA 与 BVH traversal
```

`build()` 从 ECS 创建 `coal::CollisionObject`，批量注册到 Dynamic AABB Tree。
`sync_from_scene()` 更新 Transform 和 AABB，最后调用 manager `update()`。
`check_scene_collisions()` 对整批请求只执行一次 manager `collide()`；自定义
`CollisionCallBackBase` 在 broad phase 候选对上执行 Coal narrow phase并标记请求实体。不要把该
路径改成逐实体扫描整个场景，否则会丢失 broad phase 的批量收益。
布尔碰撞查询使用 `CollisionRequest(NO_REQUEST, 1)`，避免生成 benchmark 和场景批检不需要的
接触位置、法向及穿透深度。

Mesh 使用 `BVHModel<OBBRSS>`。顶点在构建时应用 world scale；FBX 沿用平台现有坐标转换约定。
无 Assimp、空 Mesh 或无 Mesh resource 时，以 Collider `size` 的 Box 作为 fallback。

## 3. Coal 3.0.4 构建

项目使用 Coal `v3.0.4`。先拉取官方源码和 submodule，然后安装到项目本地目录：

```powershell
git clone --branch v3.0.4 --depth 1 https://github.com/coal-library/coal.git coal-3.0.4
git -C coal-3.0.4 submodule update --init cmake

$vsCMake = 'F:\VS\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
& $vsCMake -S coal-3.0.4 -B coal-3.0.4\build -G 'Visual Studio 18 2026' -A x64 `
  -DCMAKE_INSTALL_PREFIX='F:/RobotSimPlatform/third_party/Coal/3.0.4' `
  -DCMAKE_PREFIX_PATH='F:/RobotSimPlatform/third_party/vcpkg_installed/x64-windows;D:/Anaconda/Library' `
  -DCMAKE_CXX_FLAGS='/DBOOST_ALL_NO_LIB /EHsc /MP4' `
  -DBUILD_TESTING=OFF -DBUILD_PYTHON_INTERFACE=OFF `
  -DINSTALL_DOCUMENTATION=OFF -DCOAL_HAS_QHULL=OFF
& $vsCMake --build coal-3.0.4\build --config Release --parallel 1
& $vsCMake --install coal-3.0.4\build --config Release
```

构建时通过 `BOOST_ROOT` 或 CMake package 配置提供 Boost 1.82。`BOOST_ALL_NO_LIB` 很重要：
某些预编译发行版的库名与 MSVC Boost 头文件自动链接默认请求的命名不同。
禁用自动链接后，Coal 的 CMake target 会链接已经发现的实际库。把运行时所需的
`boost_filesystem.dll` 和 `boost_serialization.dll` 复制到 `third_party/Coal/3.0.4/bin`，使
平台现有的 runtime DLL 部署规则能够复制它们。

## 4. 构建和选择插件

完整 Windows preset 已启用 `SIM_ENABLE_COAL_COLLISION_PLUGIN`：

```powershell
& $vsCMake --preset windows-full
& $vsCMake --build build\windows-full --config Release `
  --target sim_collision_coal sim_c_api sim_core_tests --parallel 4
```

Python：

```python
with Simulator(physics="mujoco", collision="coal") as sim:
    world = sim.create_world()
```

C API：

```c
SimConfig config = {0};
config.physics_backend = SIM_PHYSICS_MUJOCO;
config.collision_backend = SIM_COLLISION_COAL;
```

Benchmark：

```powershell
python examples\python\benchmark_collision_grid_headless.py `
  --backend mujoco --collision-backend coal
```

## 5. 验证和修改约束

```powershell
& build\windows-full\Release\sim_core_tests.exe
& $vsCMake --build build\windows-gui --config Release --target sim_editor
python -m pytest -q python\tests
python examples\python\benchmark_collision_grid_gui.py `
  --backend mujoco --collision-backend coal --grid-size 2 --frames 2 --skip-render
```

Core 测试覆盖 primitive pair、精确分离距离、Dynamic AABB Tree batch、Transform 更新和
Assimp triangle Mesh 的 OBBRSS BVH。改动 batch callback、Mesh 坐标或同步逻辑后必须保留这些
覆盖，并用较大的 collision grid 检查是否意外退化成 `N x scene` 查询。
