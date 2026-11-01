# 统一场景演示

唯一运行入口是 `demo.run_demo`。场景来自 `assets/catalog.json` 和 `assets/scene.001.json`，交互位姿通过 OLS 查询。

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo\task_commands\commands.001.json `
  --start-object-library `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --viewer
```

执行结束后保存报告，窗口手动关闭。默认输出 `tests/output/unified_scene/`，可用 `--output` 修改。


## 文件分工

| 文件 | 用途 |
| --- | --- |
| `run_demo.py` | 场景执行入口、状态同步、报告、窗口管理；分派任务模块 |
| `task_commands/converter.py` | 命令转换类及参数合并工具 |
| `object_library.py` | OLS 查询、位姿换算和抓取选解 |
| `task_skills/runner.py` | `move`、`pick`、`put`、`grasp`、`release`、`pour` 的分段执行和结果校验 |
| `task_skills/move.py` | 一次运动到目标 |
| `task_skills/pick.py` | 接近、抓取、抬升、离散距离回撤 |
| `task_skills/put.py` | 接近、放置、释放、回撤 |
| `task_skills/grasp.py` | 直接抓取命令目标解析与闭合确认 |
| `task_skills/release.py` | 直接释放命令目标解析与释放执行 |
| `task_skills/pull.py` | 拉动编排入口 `CommandPullExecutor` |
| `task_skills/push.py` | 推动编排入口 `CommandPushExecutor`，以及既有接触推动流程 |
| `task_skills/pour.py` | 非接触干式倒水编排：虚拟出水口对准、逐步倾斜和回退结果 |
| `task_skills/pull.py`、`task_skills/push.py` | 各自负责推拉编排、接近、抓取、执行、反馈与三态结果 |
| `grasp_collision.py` | 夹爪及携带物碰撞检查、规划缓存 |
| `closure_validation.py` | 候选试闭合与实际开合碰撞检查 |
| `carrying_safety.py` | 携带安全距离 |
| `config/scene.001.runtime.json` | 运行组件、资源路径、质量、机器人标定 |
| `SKILLS.md` | 命令参数及行为说明 |

## 命令示例

- `task_commands/commands.001.json`：抓取苹果和糖盒并放入篮子，最后回 home；也是默认命令文件。
- `task_commands/commands.002.json`：拉开上层抽屉、释放把手后直接抓取并放入苹果、关闭抽屉，最后回 home。
- `task_commands/commands.004.json`：抓取瓶子，执行不模拟水流的干式倒水动作，抬高末端后释放瓶子。

pick 先接近、闭合确认，再沿世界 Z 抬升，最后独立按 15、12、10、14、13、11 cm 尝试回撤。详细语义以 [SKILLS.md](SKILLS.md) 为准。

## 诊断工具

```powershell
.\.venv\Scripts\python.exe -B -m demo.tools.profile_unified_scene `
  --commands demo\task_commands\commands.001.json `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --output tests\output\unified_profile

.\.venv\Scripts\python.exe -B -m demo.tools.benchmark_fcl_native `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --output tests\output\fcl_benchmark.json
```

输出放在 `tests/output/`，不放在 demo 源码目录。性能工具当前的 `outside_execution_s` 仅扣除机械臂轨迹执行，仍包含真实夹爪开合，不能当作严格的非执行总耗时；inclusive 时间包含子调用，不能直接相加。最近的细分统计位于 `tests/output/nonexecution_profile*/`。

旧运行器、交互注册表、随机场景生成器、日志导出工具及旧命令已移除。当前入口与通用运动学、轨迹缓存测试保留。

## 场景与命令配对

- `assets/scene.001.json` ↔ `demo/task_commands/commands.001.json`：原 test 场景。
- `assets/scene.002.json` ↔ `demo/task_commands/commands.002.json`：抽屉场景。命令编排拉开并推动上层抽屉，成功后回 home。

该场景已接通活动抽屉与推拉技能。统一入口命令：

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo\task_commands\commands.002.json `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --viewer
```

## 002 抽屉开合

`assets/scene.002.json` 使用桌子、机械臂、夹爪及基底布局。桌面摆放模型 26 的双层抽屉柜和模型 1 的苹果，演示操作上层 `Nightstand0061_drawer01`。两个活动抽屉都保留物理关节，开、关两次接近都用 `link` 明确选择上层。

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo\task_commands\commands.002.json `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --output tests\output\scene002\interactive `
  --viewer
```

流程：先为 pull 和 push 在交互位姿求解 IK 并静态规划完整推拉路径，再执行各自的接近与抓取编排；接近不可行时使用同目标的 MoveSkill 直接到交互位姿，然后执行已准备路径。pull 默认 `contact_mode: grasp`，push 使用 `contact_mode: contact` 时不闭合、不抓持、不建立绑定，因此不执行 release。contact 沿用原始把手抓取位姿，不额外旋转或按指垫尺寸偏移接触目标；实际接触仍须确认。

两次运动都使用 `distance: 0.12`，不读取抽屉绝对开度来计算行程，也不以抽屉是否完全关闭判定运动成功。默认 frame=tool，以推拉开始时的 TCP 为准，pull 沿 -Z、push 沿 +Z，运动中保持起始姿态。contact push 可以先向前运动再建立接触；distance 从首次真实接触时的实测 TCP 开始计量，空行程单独记录，不占用请求的 0.12 m 有效推进行程。完整路径依据几何接触预测规划并连续执行，最终完成量和目标位置按真实接触位姿验证。目标有两个活动抽屉，因此 target/link 用于选择接触对象，不参与行程计算。碰撞、力限制与接触丢失检查保留；未完成请求的推拉行程返回 suspend。


抓取准备的直线接近不可行时，pick、pull、抓持式 push 可以降级为 task_skills/move 的避障位姿移动：保持原抓取目标，取消直线路径要求，达到并验证实测位姿后才闭合。只有规划失败且该次尝试没有实际运动时才重试；执行中的失败不自动切换。失败记录及降级结果都写入报告，详见 SKILLS.md 的“抓取前的避障降级”。








