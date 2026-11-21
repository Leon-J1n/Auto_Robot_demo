# Requests 模块

## 1. 职责与边界

`requests` 管理可运行的任务请求、对应的初始场景和单次执行结果。每个请求目录包含一份命令序列及其引用的场景 JSON，由 `demo.run_demo` 统一加载和执行。

边界规则：

1. 请求描述“执行哪些技能、操作哪个场景对象”；技能实现位于 `atomic_skill` / `atomic_action`。
2. 场景描述对象实例、初始位姿、机器人关节、夹爪安装关系及模型引用；机器人、夹爪和物体资产由 `services/object_library_server/data` 管理，不复制到请求目录。
3. 资源装配与实际状态同步属于 `execution`，IK 属于 `kinematics`，路径搜索属于 `planning`，碰撞检查属于 `collision`。
4. 输入文件与生成结果分开存放。`output/` 每次运行前清空，只保留本次结果；需要保留的场景或诊断应先复制到该目录之外。

## 2. 目录结构与职责

```text
requests/
├── README.md
└── commands.<id>/                   # 独立样例目录，编号不表示任务类别
    ├── commands.<id>.json           # 任务身份、场景引用、技能序列
    ├── <scene>.json                 # 初始场景，文件名由样例自行指定
    └── output/                     # 运行时生成，结构见第 5 节
```

现有 `commands.xxx` 均为样例，不构成任务分类。每个样例通过任务中的 `scene_file` 引用自己的场景；不同目录中的同名场景文件也可能具有不同内容。

## 3. 数据流

```text
commands.<id>.json + scene_file
        ↓ demo.run_demo
SceneResourceCoordinator.resolve()
        ↓ 校验身份／版本，解析资产并装配机器人和夹爪
atomic_skill → atomic_action → IK / planning / collision
        ↓ 时间参数化轨迹与执行前验证
MuJoCo execution → 实测关节、物体位姿与抓持状态
        ↓
output/：摘要、命令诊断、耗时、轨迹、末态场景
```

默认自动规划采用 `direct → RRT`。规划与最终复核使用场景和机器人资产中的精细碰撞几何。规划、碰撞与执行配置分别见 [Planning](../planning/README.md)、[Collision](../collision/README.md) 和 [Execution](../execution/README.md)。

## 4. 对外接口

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands requests\commands.003\commands.003.json `
  --start-object-library `
  --viewer
```

| 参数 | 用途 |
| --- | --- |
| `--commands` | 任务 JSON 路径；默认使用 `commands.001` |
| `--output` | 自定义输出目录；同样会在运行前清空 |
| `--viewer` | 打开 MuJoCo 查看器 |
| `--show-collision-geometry` | 显示仿真碰撞几何，同时打开查看器；显示的不是规划代理 |
| `--start-object-library` | 启动本地 OLS 服务 |
| `--object-library-url` | 使用已有 OLS 服务，与上一项互斥 |
| `--library` | 指定 SimPlatform DLL，默认使用项目内 `libraries/simplatform/bin/sim_platform_c.dll` |

支持的技能及参数以 [技能清单](../demo/SKILLS.md) 为准。对象类 `target` 通常引用场景的 `object_id_in_scene`；`home` 等命名目标来自场景的 `named_targets`。命令按数组顺序执行，`command_id` 是唯一标识，不要求连续，也不等于输出文件中的执行序号。

## 5. 配置、输入与输出

### 任务请求

下面是引用现有分拣场景的最小请求示例；场景身份和版本应与实际文件一致：

```json
{
  "schema_version": "2.0",
  "request_id": "return-home-example",
  "scene_id": 3,
  "scene_version": 3,
  "scene_file": "ur5_table_sorting.json",
  "commands": [
    {
      "command_id": "1",
      "skill_name": "move_to",
      "parameters": {
        "target": "home",
        "planning_method": "joint"
      }
    }
  ]
}
```

- `scene_file` 相对任务 JSON 所在目录解析，也可使用绝对路径。
- `schema_version` 当前为字符串 `"2.0"`；`request_id` 必须存在，`commands` 必须为非空数组。
- `scene_id`、`scene_version` 必须与场景一致；场景支持的 `scene_schema_version` 为 1 或 2。
- 全局默认配置位于 [config/global.json](../config/global.json)，参数说明见 [全局配置](../docs/global_config.md)。场景和命令可按各接口支持的字段覆盖默认值。
- IK 位置容差、碰撞安全距离和碰撞采样分辨率是不同参数。携带物的带符号规划安全距离入口见 [技能清单](../demo/SKILLS.md)，不通过修改 IK 容差控制。

### 单次运行输出

默认输出位于对应请求的 `output/`。每次运行前清空旧内容，不生成历史运行子目录；同一输出目录不允许并发使用。清理会拒绝工作区根目录、包含输入文件的目录及不安全的链接目标。

```text
output/
├── summary.json
├── commands/
│   └── results.jsonl
├── metrics/
│   └── planning.jsonl
├── trajectories/
│   ├── planned/step_029_command_33.json
│   ├── frames.jsonl
│   └── execution.traj
├── snapshots/
│   ├── final_scene.json
│   ├── state.json
│   ├── blackboard.json
│   └── final_state.npz
└── runtime/
    └── mujoco/
```

| 文件或目录 | 内容与读取方式 |
| --- | --- |
| `summary.json` | 整体运行结果、已记录命令数量、成功／失败数量、失败位置和结果文件引用；排查时优先打开 |
| `commands/results.jsonl` | 每条命令一行，包含 `step_index`、`command_id`、请求、子步骤、目标解析、IK 分支、碰撞对象与距离、失败原因及执行／抓持验证；执行完立即写入 |
| `metrics/planning.jsonl` | 每条命令一行规划耗时与关键计数，不重复保存完整视觉和候选碰撞详情 |
| `trajectories/planned/*.json` | 规划路径、时间参数化轨迹和时间戳；通过命令轨迹中的 `data_file` 与 `data_key` 定位 |
| `trajectories/frames.jsonl` | 执行过程采样状态，含帧序号、命令、子步骤、仿真时间、关节和物体位姿 |
| `trajectories/execution.traj` | 从执行采样生成的 WebEditor 回放文件 |
| `snapshots/final_scene.json` | 原始场景格式的结束场景，更新实测物体位姿及机器人、夹爪和机构驱动关节，保留资产、安装和相机定义 |
| `snapshots/state.json` | 执行结束时后端返回的实际状态，包含关节、物体、夹爪及抓持信息 |
| `snapshots/blackboard.json` | 目标知识、测量位姿、视觉估计、交互锚点和模型缓存统计 |
| `snapshots/final_state.npz` | MuJoCo 的末态数组，包括 `qpos`、`qvel`、`ctrl`、约束启用状态及仿真时间 |
| `runtime/mujoco/` | 本次运行生成的 MJCF、URDF 导入中间文件等后端资源 |

命令详情集中写入 `commands/results.jsonl`，子步骤嵌在所属命令记录内；同一命令失败后的重复落盘会更新原记录，不增加重复行。规划轨迹仍独立保存，例如 `planned/step_029_command_33.json` 中，`step_029` 表示第 29 步，`command_33` 表示请求中的 `command_id` 为 `33`。命令轨迹通过 `data_file` 和 `data_key` 引用数组，摘要不再列出逐命令详情。进程内 `run()` 仍返回完整报告，供诊断工具使用。

中途失败时保存可用结果；初始化尚未完成时可能只有摘要，未生成的轨迹或末态文件不会补造。旧版的 `report.<pid>.json`、`report.json` 及 `planning_timing.<pid>.json` 不再写入，已有历史输出会在对应请求下次运行时被清理。

## 6. 验证与限制

输出结构与末态场景相关测试：

```powershell
.\.venv\Scripts\python.exe -m unittest `
  planning.tests.test_demo_output_layout `
  planning.tests.test_final_scene -q
```

新增请求时，将任务与初始场景放在新的 `commands.<id>/` 目录，校对场景身份、版本、目标 ID、机器人关节顺序和安装关系，再通过统一入口运行。查看器中的物体位置和关节状态来自实际仿真；规划成功不代表执行时一定保持稳定抓持，需结合执行结果判断。

`final_scene.json` 是结束时的场景状态，不是完整仿真检查点；它不保证恢复速度、控制器内部状态、抓持接触或继续执行中断任务。动态状态诊断应同时参考 `state.json` 和 `final_state.npz`。从 `output/` 复用结束场景前应先复制到输出目录之外，避免下次运行清理时删除。
