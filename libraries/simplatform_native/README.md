# SimPlatform 原生 FCL 优化构建

源码仍使用用户提供的 SimPlatform，位于 `libraries/simplatform/source/SimPlatform-main`。没有替换为 Python FCL。此目录存放可复现构建脚本、编译依赖及独立输出，不覆盖 Downloads 中的原 DLL。

## 修改范围

- 新增 C ABI `sim_robot_configuration_contacts_filtered`，允许调用者传入已有的排除碰撞对（每对两个实体 handle）。C++ 在 FCL 碰撞/距离计算前过滤这些对。
- 带排除对的查询使用变换后的局部 AABB：世界中心为 `R*c+t`，半尺寸为 `abs(R)*h`。它完整包含旋转后的几何体，较 FCL 0.7 的包围球外接立方体更紧；仍使用原安全距离扩张和原 FCL 精确窄相检测。
- 原 `sim_robot_configuration_contacts` 保持行为不变，可用于对照验证。
- Python SDK 检测新符号；旧 DLL 不需要更新也可以继续使用。
- 项目碰撞适配器仅将现有 `baseline_allowed_pairs` 传给原生接口。安全距离、采样精度、几何体、IK 候选筛选与规划算法均保持原有规则。过滤集合纳入配置缓存键，撤销允许规则后重新查询。
- 未实现跨场景的原生查询上下文缓存，避免仅依赖不完整的 revision 导致陈旧场景。
- 带排除对的查询现按当前场景层级只读计算候选 FK，不再复制整个 World；关节排序、限位策略和变换合成与原接口一致。原接口继续作为快照式对照路径。
- 查询只为机器人及挂载物创建临时碰撞对象，静态环境读取当前 FCL 对象；所有临时变换均限于本次调用。世界包围盒之后增加局部包围盒分离检查，仍保留原查询裕量和 FCL 精确检测。
- Python 适配器及 SDK 可复用不可变排除对的编码缓冲区；不缓存查询输出，策略变化立即生效，可变列表不走按对象身份复用的路径。
- 已回退候选提前停止和返回历史路线两项策略改动。

## 构建

需要 Visual Studio 2022 C++、CMake、BqLog 和 vcpkg。依赖放在本目录 `deps/BqLog-main`、`deps/vcpkg-master`。

```powershell
& libraries/simplatform_native/deps/vcpkg-master/vcpkg.exe install fcl:x64-windows assimp:x64-windows urdfdom:x64-windows --classic
& libraries/simplatform_native/build.ps1
```

输出为 `libraries/simplatform/bin/sim_platform_c.dll`，同目录依赖 DLL 必须一起保留。该构建只启用规划需要的 FCL、Assimp、URDF；MuJoCo 执行继续使用项目现有独立 Python 后端。

## 验证与运行

```powershell
.\.venv\Scripts\python.exe -B -m demo.benchmark_fcl_native `
  --library libraries/simplatform/bin/sim_platform_c.dll `
  --output tests/output/response_profile/native_filtered.json

.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo/skill_command/commands.001.json `
  --start-object-library `
  --library libraries/simplatform/bin/sim_platform_c.dll `
  --viewer
```

基准在完全相同的关节姿态上交替调用新旧接口，禁用 Python 缓存，并逐项比较过滤后的接触对与距离。`supports_native_filter` 必须为 `true` 才证明加载到了新 DLL。显式传入旧 Downloads 路径仍会使用旧 DLL。

## 检测器优化复测（2026-10-26）

本轮除上述原生修改外，`demo/collision/grasp_collision.py` 预编译固定几何对拓扑，并仅在同一规划快照内复用随机械臂共同刚性变换的夹爪内部距离。接触策略仍逐次应用，临界距离重新计算，状态变化使缓存失效，最终验证继续重新检测。

原 command、速度、安全距离和采样规则不变，三个场景各串行运行两次。相对于上一轮 `collision_run1/2`，规划耗时均值如下；不包含初始化和运动执行。

| command | 上轮规划 | 本轮规划 | 减少 |
|---|---:|---:|---:|
| 001 | 11.894 s | 9.417 s | 20.8% |
| 002 | 1.177 s | 0.807 s | 31.4% |
| 003 | 5.384 s | 3.993 s | 25.8% |

碰撞检测部分分别减少 34.4%、40.5%、28.7%。原生独立查询暖态均值加速约 5.15 倍（001）及 3.10 倍（003），此数值不代表完整任务加速比。Python 67 项测试、原生 CTest 2 项通过；新旧 DLL 的 120 个姿态碰撞结果一致，距离容差为 1e-9 m。

001 仍因放置目标碰撞返回 error，002 仍在拉门安全前缀末端返回 suspend，003 全部成功。001 仍有约 2 s 的 SugarBox 候选 IK 开销。002 的总墙钟耗时从 18.77 s 增至 27.71 s，增加位于拉门执行阶段，仿真时长未变；本次测量尚未确定其原因，不能据此声称整项任务都变快。前后基准并非同一时刻采集，机器负载未受控。

完整数据及复现脚本：`tests/output/planning_timing_20261025/detector_optimization_summary.json`、同目录 `profile_run.py`、`summarize_detector.py`、`native_compare.py`；最终两轮目录为各场景下的 `detector_verified1/2`。

## 已验证结果（2026-10-13）

- 新 DLL 内相同 60 个姿态：原接口平均 86.45 ms，优化接口平均 16.76 ms，约 5.2 倍加速。60/60 个接触结果相同，其中 33 个姿态碰撞；与用户原 DLL 的接触对也一致，距离比较容差为 1e-9 m。
- `commands.001.json` 完整执行成功，非执行耗时 14.59 s，总耗时 14.90 s。回退两项策略后，上次同场景记录的非执行耗时为 35.48 s。此次接近与接触两段路点完全一致；返回 home 仍走 RRT-Connect + TrajOpt，输出路径不保证与历史运行完全相同。
- 原生 CTest 2/2 通过（含新增接触过滤与安全距离测试）；Python 29 项回归测试通过（包含实际 DLL、场景同步、插入障碍物、实际夹持中心误差、MuJoCo 执行和返回 home）。`atomic_action/` 哈希与回退基线一致。
- 详细计时和报告：`tests/output/response_profile/native_tight_bounds.json`、`tests/output/response_profile/native_tight_pipeline/`。归档、源码与 DLL 的 SHA256 见 `build_manifest.json`。



