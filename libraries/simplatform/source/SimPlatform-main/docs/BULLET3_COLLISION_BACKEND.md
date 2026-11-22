# Bullet3 Collision Backend 构建与维护

## 1. 插件边界

Bullet3 Collision Backend 是独立插件 `sim.collision.bullet3`，产物为
`sim_collision_bullet.dll` 和 `bullet_collision.simplugin.json`。它只实现
`ICollisionBackend`，不创建刚体、不推进时间，也不复用 Physics Backend 的 contact
manifold。`sim.physics.bullet3` 仍是另一套独立的动力学插件，两者可以分别选择。

主要文件：

| 文件 | 职责 |
|---|---|
| `plugins/bullet_collision_backend.cpp` | Collision World、shape、BVH 查询和插件 C ABI |
| `plugins/bullet_collision.simplugin.json.in` | 插件发现 manifest |
| `plugins/bullet_backend.cpp` | 独立的 Bullet3 Physics Backend，不属于本插件 |
| `src/c_api.cpp` | C API backend 枚举到插件名的映射 |
| `src/editor.cpp` | GUI 和 benchmark RPC 的 backend 选择 |

## 2. BVH 结构

查询使用两层加速结构：

```text
World colliders
  -> btDbvtBroadphase                 动态 AABB 树，筛掉不可能相交的物体对
     -> primitive narrow phase       Box/Sphere/Capsule/Cylinder
     -> btGImpactMeshShape BVH       Assimp 三角 Mesh 的内部 GImpact BVH
        -> triangle narrow phase
```

`build()` 从 ECS 创建 `btCollisionObject` 并加入一个纯 `btCollisionWorld`。
`sync_from_scene()` 写入最新 World Transform，并对每个对象调用 `updateSingleAabb()` 更新
DBVT proxy。`check_scene_collisions()` 每批只调用一次
`performDiscreteCollisionDetection()`，然后遍历 dispatcher manifolds 汇总请求实体，不能退化为
逐实体、逐物体 pair test。

Mesh 使用 `btGImpactMeshShape`，并在构造 backend 时注册
`btGImpactCollisionAlgorithm`。不要直接换成 `btBvhTriangleMeshShape`：后者适合静态 concave
对象，作为通用 Collision Backend 时无法可靠覆盖移动 Mesh 和 concave-concave 查询。GImpact
在 `updateBound()` 后构建自己的 Mesh BVH。

## 3. Shape 映射与查询语义

- Box/Sphere/Capsule/Cylinder 映射到 Bullet 对应 primitive shape。
- Mesh 由 Assimp 读取完整索引三角形并应用 Collider world scale；导入失败不应静默伪装成成功。
- 无 Assimp 或空 Mesh 时沿用平台现有约定，以 Collider `size` 创建 Box fallback。
- `check_collision(a,b)` 使用 `contactPairTest()`。
- `check_scene_collision(a)` 走同一个场景 batch 路径，因此会利用 DBVT broad phase。
- `distance(a,b)` 优先读取 Bullet closest contact；算法没有返回 closest point 时，退回两个
  world AABB 的欧氏分离距离。

因此 primitive distance 可作为通常的几何距离使用；复杂 concave Mesh 的正距离可能是 AABB
近似值，不应当用于要求精确 Mesh 最近点的规划算法。精确 Mesh distance 仍使用 FCL。

## 4. 构建

完整 Windows preset 已启用：

```powershell
$vsCMake = 'F:\VS\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin'
& "$vsCMake\cmake.exe" --preset windows-full
& "$vsCMake\cmake.exe" --build build\windows-full --config Release `
  --target sim_collision_bullet sim_c_api sim_core_tests --parallel 2
```

自定义构建使用：

```powershell
cmake -S . -B build\bullet-collision `
  -DSIM_ENABLE_ASSIMP=ON `
  -DSIM_ENABLE_BULLET3_COLLISION_PLUGIN=ON `
  -DCMAKE_PREFIX_PATH="F:\RobotSimPlatform\third_party\vcpkg_installed\x64-windows;F:\RobotSimPlatform\third_party\Bullet3\3.25"
cmake --build build\bullet-collision --config Release --target sim_collision_bullet
```

运行时插件目录必须同时包含 DLL 与 manifest。默认使用构建产物 `plugins` 目录，也可设置
`SIM_PLUGIN_DIR` 或传入 Python `plugin_directory=`。

## 5. 选择 Backend

Python：

```python
from sim_platform import Simulator

with Simulator(physics="mujoco", collision="bullet3") as sim:
    world = sim.create_world()
```

C API：

```c
SimConfig config = {0};
config.physics_backend = SIM_PHYSICS_MUJOCO;
config.collision_backend = SIM_COLLISION_BULLET3;
```

Benchmark：

```powershell
python examples\python\benchmark_collision_grid_headless.py `
  --backend mujoco --collision-backend bullet3
python examples\python\benchmark_collision_grid_gui.py `
  --backend mujoco --collision-backend bullet3
```

## 6. 修改与验证

修改 shape、同步或 batch 算法后至少运行：

```powershell
& build\windows-full\Release\sim_core_tests.exe
& "$vsCMake\ctest.exe" --preset windows-full-release --output-on-failure
python -m pytest -q python\tests
```

核心测试覆盖 primitive pair/distance、DBVT batch、Transform 同步和 Assimp triangle Mesh 的
GImpact BVH。若调整 batch 实现，还应运行 collision grid benchmark，确认查询耗时随物体数增长
时没有重新出现 `N x scene` 的逐对象重复遍历。
