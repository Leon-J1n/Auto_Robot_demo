# SimPlatform FCL collision adapter

## 查询复用

适配器默认启用有界缓存：最多 4096 个带裕量构型查询、128 条已通过的路径。构型键使用未舍入的关节值和安全距离，缓存原始接触对，每次按当前允许碰撞规则重新过滤。路径键包含完整路径、采样分辨率、裕量和允许碰撞规则。

每次复用前读取 SDK 实际实体层级、局部/世界变换和 Collider 描述；场景发生变化会清空缓存，包括绕过适配器直接修改 SDK 实体的情况。不能只依赖 SDK revision，因为部分位姿写入不更新该值。零裕量实时构型检查继续直接查询原生库。机器人模型/关节定义更换仍需重建 checker；同一个实例应串行使用。

`validate_execution_path(points, resolution=.02, safety_distance=..., allowed_geom_ids=...)` 默认按 0.02 rad 的单关节最大增量进行离散碰撞验证；直接规划、普通 RRT 规划最终复核和平滑轨迹验证共用 `planning.collision_validation.resolution_rad`（默认 `0.04`）。RRT 扩展查询另用 0.03 rad，TrajOpt 局部查询保留原有分辨率。平滑轨迹保留原有每区间至少二分（`spline_subdivisions=2`），只按新的采样分辨率调整碰撞验证点密度。安全间距和终点检查不变。离散采样不能保证发现采样点之间的短暂碰撞，增大步长会降低碰撞检出能力。

`checker.cache_stats` 记录 `native_queries`、`configuration_hits`、`path_hits`、`invalidations`；统一 demo 的报告包含 `collision_cache_stats`。诊断时可设置 `checker.cache_enabled=False` 禁止缓存复用。

带夹爪的 `GraspCollisionChecker` 完整路径精确缓存现已默认关闭：直接调用原路径检查，不生成完整场景签名。此前实测命中率为零且签名成本高。单构型查询缓存不受此开关影响。仅用于对照时可显式设置 `path_validation_cache_enabled=True`，启用最多 128 条的精确整段缓存；其复用要求路径、分辨率、裕量、场景状态、模型几何与策略完全相同。时间参数化诊断记录 `path_validation_cache_enabled`、采样分辨率和路点数。

规划末尾验证关节折线，连续时间参数化则验证 cubic/PCHIP 样条；两者通常具有不同几何路径和采样点，不能因共享端点就跳过后者的碰撞验证。

`simplatform_fcl.py` uses the SimPlatform checkout in `libraries/simplatform/source/SimPlatform-main`.
The adapter creates `Simulator(physics="none", collision="fcl")`, loads the UR5e URDF and
scene assets, and exposes the existing planner protocol (`check` and `check_path`). MuJoCo is
not imported by this planning checker.

Build the supplied SimPlatform archive with `SIM_ENABLE_FCL=ON` and `SIM_BUILD_C_API=ON`, then
point the Python SDK at the resulting native library:

```powershell
$env:SIM_PLATFORM_LIBRARY = 'C:\path\to\SimPlatform-main\build\windows-full\Release\sim_platform_c.dll'
```

例如当前已提供的 DLL：

```powershell
$env:SIM_PLATFORM_LIBRARY = 'C:\Users\21474\Downloads\v0.1\v0.1\build\windows-full\Release\sim_platform_c.dll'
```

The SDK directory can be overridden when the archive is moved:

```powershell
$env:SIMPLATFORM_PYTHON_ROOT = 'C:\path\to\SimPlatform-main\python'
```

The defaults can be replaced for another scene and robot without changing the planner:

```powershell
$env:FCL_SCENE_FILE = 'C:\path\to\scene.json'
$env:FCL_ROBOT_URDF = 'C:\path\to\robot.urdf'
```

The planning scene is [fcl_scene.json](../assets/scenes/fcl_scene.json). Every collidable object
must provide an `AssetRef`/`Collider` mesh or primitive. `MeshRenderer` alone is visual metadata
and is intentionally ignored. The robot is loaded from its `RobotModelRef` URDF; the gripper is
parented to the UR5e `attachment_site` so SimPlatform's FCL robot query treats its colliders as
robot-side geometry.

Scene 3's Basket keeps its detailed visual asset and uses `models/12/basket_collision.obj` as a
coarser floor-and-wall collision proxy. This preserves the open interior and outer dimensions while
replacing 176 convex collision parts with 19. The continuous proxy walls intentionally ignore
weave gaps, so very narrow gaps near the basket wall are treated as occupied.

SimPlatform 返回的 URDF 关节列表可能包含 `attachment_site_fixed`，并且顺序与项目关节顺序不同。
适配器会将项目的六维顺序
`shoulder_pan_joint, shoulder_lift_joint, elbow_joint, wrist_1_joint, wrist_2_joint, wrist_3_joint`
映射到 SDK 顺序，并为固定关节填入零值；上层 `check()` 和 `check_path()` 始终接收六维项目关节向量。

本地优化构建说明见 [SimPlatform 原生构建](../libraries/simplatform_native/README.md)。新 DLL 支持在 FCL 距离计算前过滤已有的结构允许对；旧 DLL 自动使用原接口。`native_filter_enabled=False` 仅用于性能对照，不改变最终允许碰撞规则。排除对集合纳入配置缓存键，规则撤销后不会沿用此前已过滤的查询结果。


平滑轨迹按每个样条区间的精确关节导数上界分配碰撞采样点，保留全部样条节点，每区间至少 `planning.collision_validation.spline_subdivisions`（默认 `2`）等分。运动执行采样率、速度/加速度限制及直线/圆弧几何精度检查不受该设置影响。若需细采样对照，在场景 `move_defaults.planning` 或命令 `parameters.request.planning` 中设置：

```json
{"collision_validation": {"resolution_rad": 0.01, "spline_subdivisions": 4}}
```

该设置恢复较细的检查密度，但新实现按区间分配采样，采样位置不与旧版全局均匀网格完全一致。
