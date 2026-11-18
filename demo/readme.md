# 统一场景演示

唯一运行入口是 `demo.run_demo`。场景来自 `requests/<request>/commands.<id>.json` 和 `requests/commands.001/ur5_table_empty.json`，交互位姿通过 OLS 查询。

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands requests\commands.001\commands.001.json `
  --start-object-library `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --viewer
```

执行结束后保存报告，窗口手动关闭。默认输出到命令文件同级的 `output/`，可用 `--output` 修改。
每次运行前清空指定输出目录，仅保留本次结果。相同输出目录不允许并发运行；
输入文件所在目录和工作区根目录不能作为清空目标。

输出分类：

| 路径 | 内容 |
| --- | --- |
| `summary.json` | 整体结果、命令数量、失败位置与结果文件引用 |
| `commands/results.jsonl` | 每条命令一行，含执行序号、命令 ID、请求、子步骤、IK／碰撞／抓持诊断；执行完立即写入 |
| `metrics/planning.jsonl` | 每条命令一行耗时与关键计数，不复制完整视觉或候选碰撞详情 |
| `trajectories/planned/step_029_command_33.json` | 规划路径、时间参数化轨迹及时间戳，由命令文件引用 |
| `trajectories/frames.jsonl` | 执行过程的逐帧状态 |
| `trajectories/execution.traj` | WebEditor 回放 |
| `snapshots/` | 黑板、最终运行状态 JSON 和 MuJoCo 末态 NPZ |
| `snapshots/final_scene.json` | 原场景格式的结束场景：更新实测物体位姿和驱动关节，保留资产、安装和相机定义；成功或中途失败均保存可用末态 |
| `runtime/mujoco/` | 本次编译的仿真场景 |

进程内 `run()` 返回的完整报告结构保留，便于现有诊断工具使用；磁盘仅写分类后的视图。
不再生成旧的 `report.json`、`report.<pid>.json` 或逐命令 `planning_timing.<pid>.json`。

控制台耗时格式为 `timing|command=16|planning=0.562s|vlm_request=5.400s`。
`planning` 汇总目标解析与候选选择、IK、碰撞检查、路径搜索和轨迹准备，
包括失败候选和重试的准备时间，排除 VLM 请求与实际运动执行；
命令及指标记录中的 `planning_preparation_s` 使用同一口径。
仅在本条命令实际调用 VLM 时显示 `vlm_request`，数值为所有客户端请求的完整响应
等待时间之和，包含失败调用、显式重试及 SDK 内部重试等待，不含客户端初始化、
图像编码、响应解析、深度投影或 IK。粗估计与腕部精估计的请求均计入当前命令，
复用缓存不计时；命令与耗时记录同时保存请求次数和客户端请求异常次数。


## 文件分工

| 文件 | 用途 |
| --- | --- |
| `run_demo.py` | 场景执行入口、状态同步、报告、窗口管理；分派任务模块 |
| `task_commands/converter.py` | 命令转换类及参数合并工具 |
| `object_library.py` | OLS 查询、位姿换算和抓取选解 |
| `atomic_skill/runner.py` | `move`、`pick`、`put`、`grasp`、`release`、`pour` 的分段执行和结果校验 |
| `atomic_skill/move.py` | 一次运动到目标 |
| `atomic_skill/pick.py` | 接近、抓取、抬升、离散距离回撤 |
| `atomic_skill/put.py` | 接近、放置、释放、回撤 |
| `atomic_action/manipulation/grasp/action.py` | 直接抓取原子动作：开指、闭合与抓取结果 |
| `atomic_action/manipulation/release/action.py` | 直接释放原子动作：张开夹爪与释放结果 |
| `atomic_skill/pull.py` | 拉动编排入口 `CommandPullExecutor` |
| `atomic_skill/push.py` | 推动编排入口 `CommandPushExecutor`，以及既有接触推动流程 |
| `atomic_skill/pour.py` | 非接触干式倒水编排：虚拟出水口对准、逐步倾斜和回退结果 |
| `atomic_skill/pull.py`、`atomic_skill/push.py` | 各自负责推拉编排、接近、抓取、执行、反馈与三态结果 |
| `interaction/wrist_observation.py` | 腕部相机对准接近位姿的几何计算 |
| `grasp_collision.py` | 夹爪及携带物碰撞检查、规划缓存 |
| `closure_validation.py` | 推拉等接触技能复用的闭合验证辅助 |
| `carrying_safety.py` | 携带安全距离 |
| `config/scene.001.runtime.json` | 运行组件、资源路径、质量、机器人标定 |
| `atomic_action.md` | 命令参数及行为说明 |

## 命令示例

- `task_commands/commands.001.json`：抓取苹果和糖盒并放入篮子，最后回 home；也是默认命令文件。
- `../requests/commands.002/commands.002.json`：拉开上层抽屉、释放把手后直接抓取并放入苹果、关闭抽屉，最后回 home。
- `../requests/commands.004/commands.004.json`：抓取瓶子，执行不模拟水流的干式倒水动作，抬高末端后释放瓶子。

pick 先接近、闭合确认，再沿世界 Z 抬升，最后独立按 15、12、10、14、13、11 cm 尝试回撤。详细语义以 [atomic_action.md](atomic_action.md) 为准。

## 诊断工具

```powershell
.\.venv\Scripts\python.exe -B -m demo.tools.profile_unified_scene `
  --commands requests\commands.001\commands.001.json `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --output tests\output\unified_profile

.\.venv\Scripts\python.exe -B -m demo.tools.benchmark_fcl_native `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --output tests\output\fcl_benchmark.json
```

输出放在 `tests/output/`，不放在 demo 源码目录。性能工具当前的 `outside_execution_s` 仅扣除机械臂轨迹执行，仍包含真实夹爪开合，不能当作严格的非执行总耗时；inclusive 时间包含子调用，不能直接相加。最近的细分统计位于 `tests/output/nonexecution_profile*/`。

旧运行器、交互注册表、随机场景生成器、日志导出工具及旧命令已移除。当前入口与通用运动学、轨迹缓存测试保留。

## 场景与命令配对

- `requests/commands.001/ur5_table_empty.json` ↔ `requests/commands.001/commands.001.json`：原 test 场景。
- `../requests/commands.002/ur5_table_apple.json` ↔ `../requests/commands.002/commands.002.json`：抽屉场景。命令编排拉开并推动上层抽屉，成功后回 home。

该场景已接通活动抽屉与推拉技能。统一入口命令：

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands requests\commands.002\commands.002.json `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --viewer
```

## 002 抽屉开合

`requests/commands.002/ur5_table_apple.json` 使用桌子、机械臂、夹爪及基底布局，演示抽屉交互。

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands requests\commands.002\commands.002.json `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --output tests\output\scene002\interactive `
  --viewer
```

流程：先为 pull 和 push 在交互位姿求解 IK 并静态规划完整推拉路径，再执行各自的接近与抓取编排；接近不可行时使用同目标的 _MoveExecutor 直接到交互位姿，然后执行已准备路径。pull 默认 `contact_mode: grasp`，push 使用 `contact_mode: contact` 时不闭合、不抓持、不建立绑定，因此不执行 release。contact 沿用原始把手抓取位姿，不额外旋转或按指垫尺寸偏移接触目标；实际接触仍须确认。

两次运动都使用 `distance: 0.12`，不读取抽屉绝对开度来计算行程，也不以抽屉是否完全关闭判定运动成功。直线方向使用起始推拉坐标系：+Z 沿 tool 原点指向 grasp_center，+X 由抓取 X 的垂直投影确定，+Y = Z × X；默认 pull 沿 -Z、push 沿 +Z，运动中保持起始姿态。contact push 可以先向前运动再建立接触；distance 从首次真实接触时的实测 TCP 开始计量，空行程单独记录，不占用请求的 0.12 m 有效推进行程。完整路径依据几何接触预测规划并连续执行，最终完成量和目标位置按真实接触位姿验证。目标有两个活动抽屉，因此 target/link 用于选择接触对象，不参与行程计算。碰撞、力限制与接触丢失检查保留；未完成请求的推拉行程返回 suspend。


抓取准备的直线接近不可行时，pick、pull、抓持式 push 可以降级为 atomic_skill/move 的避障位姿移动：保持原抓取目标，取消直线路径要求，达到并验证实测位姿后才闭合。只有规划失败且该次尝试没有实际运动时才重试；执行中的失败不自动切换。失败记录及降级结果都写入报告，详见 atomic_action.md 的“抓取前的避障降级”。










