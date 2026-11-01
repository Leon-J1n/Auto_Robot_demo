# Task Skills 接口说明

本目录提供 demo 层的任务技能接口。输入为 JSON command，负责解析目标、组织动作并调用底层技能。支持 `move`、`grasp`、`release`、`approach`、`retreat`、`pick`、`put`、`pull`、`push`、`pour`。

## 1. Command 结构

### 单条 command

```json
{
  "command_id": "1",
  "skill_name": "move",
  "parameters": {
    "target": 1,
    "planning_method": "linear"
  }
}
```

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `command_id` | string | 是 | 命令标识，同一任务内唯一 |
| `skill_name` | string | 是 | 要调用的任务技能名称 |
| `parameters` | object | 是 | 该技能的输入参数 |

### 任务文件

```json
{
  "schema_version": "...",
  "request_id": "...",
  "scene_id": "...",
  "scene_version": "...",
  "commands": [
    {
      "command_id": "1",
      "skill_name": "move",
      "parameters": {"target": "home"}
    }
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 命令格式版本 |
| `request_id` | 本次任务标识 |
| `scene_id` / `scene_version` | 已注册的场景及版本 |
| `commands` | 按顺序执行的 command 数组 |

机械臂和夹爪由初始化时的最终场景决定：先读取基础场景，再应用 `demo/config/*.runtime.json`，然后从合并后的唯一 `RobotModelRef` 和 `RobotAttachment` / `GripperModel` 读取模型信息。

## 2. Skill 用途

| skill_name | 用途 | 包含的动作 |
| --- | --- | --- |
| `move` | 移动机械臂到指定目标 | 规划并移动，不自动抓取或释放 |
| `grasp` | 在当前位置抓住目标 | 闭合夹爪并确认抓持，不移动机械臂 |
| `release` | 在当前位置释放目标 | 解除抓持并打开夹爪，不移动机械臂 |
| `approach` | 为交互动作搜索接近位姿 | 沿目标位姿轴反向搜索可执行的接近点；由 push/pull 内部调用 |
| `retreat` | 为交互动作搜索撤退位姿 | 从交互位姿向外搜索可执行的撤退点；由 push/pull 内部调用 |
| `pick` | 拿起目标物体 | 接近、抓取确认、上抬、回撤 |
| `put` | 将已抓物体放到目的地 | 从上方接近、下降、释放、回撤 |
| `pull` | 抓住目标后拉动 | 必要时接近并抓取，然后执行相对运动 |
| `push` | 抓持推动或直接接触推动 | grasp 先抓持再推动；contact 向前运动并在途中建立真实接触 |
| `pour` | 干式倒水动作 | 要求已抓持源容器，保持虚拟出水口位于目标区域上方并逐步倾斜，不模拟液体 |

## 3. 参数接口

以下字段均位于 `command.parameters` 中。表中列出常用命令接口；`request` 类字段用于覆盖对应阶段的底层请求。

### move

| 参数 | 类型 | 必填 / 默认 | 含义                                              |
| --- | --- | --- |-------------------------------------------------|
| `target` | integer / string | 必填 | 场景对象 ID、已配置的命名目标（如 home），或 end_effector 末端执行器自身 |
| `planning_method` | string | 普通目标 joint；相对末端目标 linear | 路径类型，常用 joint / linear                          |
| `relation` | string | 位姿目标的相对偏移 | above / below / front / behind / left / right   |
| `distance_m` | number | 相对位姿偏移使用，默认 0 | 沿 relation 方向的移动距离，m                            |
| `keep_end_effector_orientation` | boolean | 默认 false | 是否约束末端姿态保持不变；joint 路径目前不落实全程姿态约束                |
| `request` | object | 当前不开放 | 预留为底层 MoveSkill 的高级覆盖接口；当前使用默认设置 |

`request` 预留用于覆盖底层 MoveSkill 的运动、规划、约束和策略参数。当前版本不向任务调用方开放这些配置，实际执行使用场景和技能的默认设置；后续开放时再增加字段白名单、范围校验和权限控制。

在内部执行链中，`request` 会与场景的 `move_defaults` 深度合并；当前任务文件不应依赖这一覆盖入口。`target` 通常由 `target`、`relation` 和 `distance_m` 生成，不应在 `request` 中重复覆盖。`runtime_context` 由运行时填充，不能作为普通命令参数使用。抓取参数使用独立的 `grasp_request` 字段。

普通对象目标解析为交互位姿。命名位姿目标和 end_effector 都支持使用世界坐标的 relation：above/below 为 ±Z，front/behind 为 ±X，left/right 为 ±Y。joint 表示关节空间路径；是否启用避障由 request.planning 配置决定。通用 move 还接受 circular，但需要配套圆弧请求；机构 link 接近接口仅接受 joint/linear。

```json
{
  "command_id": "1",
  "skill_name": "move",
  "parameters": {
    "target": "end_effector",
    "relation": "above",
    "distance_m": 0.05,
    "planning_method": "linear",
    "keep_end_effector_orientation": true
  }
}
```

该命令保持执行开始时的末端朝向，沿世界 +Z 移动 5 cm。保持的是末端姿态，机械臂各关节仍会协调转动。

当前需要保持姿态的直线运动，应同时设置 `planning_method: linear` 和 `keep_end_effector_orientation: true`。对象 ID 目标的 move 也支持采用当前末端朝向到达目标交互位置；如果该姿态下目标不可达，运动仍可能规划失败。

**当前限制：** `planning_method: joint` 尚未落实全程姿态约束，即使设置上述参数，也不能保证途中末端姿态不变。起点和终点朝向相同不代表整条路径都保持该朝向，回 home 等关节目标同样不能据此保证姿态不变。

### grasp

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 必填 | 要抓取的场景对象 ID |
| `link` | string | 操作机构部件时指定 | 要抓取的活动 link |
| `grasp.mode` | string | binding | binding / physical |
| `strategy_params` | object | 可选 | grasp_force、hold_force 等抓取参数；力单位 N |
| `constraints` | object | 当前不开放 | 抓取验证、重试和超时等内部约束 |

前置条件：机械臂已经到达可抓取位置。独立 grasp 的 strategy_params 直接位于 parameters 下，不再嵌套 grasp_request。

```json
{
  "command_id":"1",
  "skill_name":"grasp",
  "parameters":{
    "target":10,
    "grasp":{
      "mode":"physical"
    },
    "strategy_params":{
      "grasp_force":20,
      "hold_force":15
    }
  }
}
```

### release

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 必填 | 要释放的场景对象 ID |
| `link` | string | 释放机构部件时指定 | 对应的活动 link |
| `strategy_params` | object | 可选 | open_width 等松爪参数；宽度单位 m |
| `constraints` | object | 当前不开放 | 释放验证、重试和超时等内部约束 |

```json
{
  "command_id": "6",
  "skill_name": "release",
  "parameters": {
    "target": 11,
    "strategy_params": {
      "open_width": 0.085
    }
  }
}
```

### approach

`approach` 可作为顶层 command 独立执行，也可由 push/pull 编排内部调用。顶层使用场景对象 `target`，执行时通过 OLS 解析目标交互位姿；当前独立调用面向普通物体目标。

```json
{
  "command_id": "1",
  "skill_name": "approach",
  "parameters": {
    "target": 1,
    "search": {
      "min_distance": 0.03,
      "max_distance": 0.10,
      "step": 0.005
    },
    "pose_axis": "+z"
  }
}
```

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 必填 | 场景中的普通物体 ID；目标位姿由 OLS 解析 |
| `search.min_distance` | number | `0.03` | 最小接近距离，m |
| `search.max_distance` | number | `0.10` | 最大接近距离，m |
| `search.step` | number | `0.005` | 搜索步长，m |
| `pose_axis` | string / number[3] | `+z` | 目标位姿局部接近轴；支持带符号轴名或非零三维向量 |

`search` 和 `pose_axis` 均可省略；省略时使用上表中的默认值。

候选距离从 `max_distance` 向 `min_distance` 递减，找到第一个可执行的接近位姿后结束。只有规划失败且没有产生实际运动时才尝试下一个距离；执行异常或已发生运动时立即失败。目标位姿和运动请求由 task skill 内部生成。

### retreat

`retreat` 可作为顶层 command 独立执行，也可由 push/pull 编排内部调用。顶层使用场景对象 `target`，执行时通过 OLS 解析目标交互位姿；当前独立调用面向普通物体目标。

```json
{
  "command_id": "r1",
  "skill_name": "retreat",
  "parameters": {
    "target": 1,
    "search": {
      "min_distance": 0.03,
      "max_distance": 0.10,
      "step": 0.005
    },
    "pose_axis": "+z"
  }
}
```

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 必填 | 场景中的普通物体 ID；目标位姿由 OLS 解析 |
| `search.min_distance` | number | `0.03` | 最小撤退距离，m |
| `search.max_distance` | number | `0.10` | 最大撤退距离，m |
| `search.step` | number | `0.005` | 搜索步长，m |
| `pose_axis` | string / number[3] | `+z` | 目标位姿局部撤退参考轴；候选点沿该轴反向生成 |

`search` 和 `pose_axis` 均可省略；省略时使用上表中的默认值。

撤退候选从最长距离向最短距离搜索，执行方向为交互位姿到候选撤退位姿。

### pick

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 必填 | 要抓取的场景对象 ID |
| `planning_method` | string | joint | 接近段路径类型 |
| `grasp.mode` | string | binding | binding / physical |
| `grasp_request` | object | 当前不开放 | 预留的内部抓取请求覆盖，例如抓取力、保持力 |
| `retreat_lift_height` | number | 0.01 | 抓取后沿世界 +Z 上抬的高度，m；0 表示不上抬 |
| `request` | object | 当前不开放 | 预留的内部机械臂运动请求覆盖 |

```json
{
  "command_id":"1",
  "skill_name":"pick",
  "parameters":{
    "target":3,
    "grasp":{
      "mode":"physical"
    },
    "retreat_lift_height":0.02
  }
}
```

`pick` 的 `request` 和 `grasp_request` 均为内部预留配置，当前使用场景和技能默认设置。

`pick` 的接近和撤离候选位姿复用 `approach` / `retreat` 的公共位姿生成逻辑；接触路径验证、抓取确认、抬升和携物安全由 `pick` 自身编排。

### put

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 必填 | 放置目的地的场景对象 ID |
| `planning_method` | string | joint | 接近段路径类型 |
| `keep_end_effector_orientation` | boolean | 可选 | 是否保持末端姿态 |
| `release_request` | object | 当前不开放 | 预留的内部释放请求覆盖，例如松爪宽度 |
| `request` | object | 当前不开放 | 预留的内部机械臂运动请求覆盖 |

前置条件：当前已有抓持对象。释放对象从实际抓持状态取得，target 指目的地。

`put` 的 `request` 和 `release_request` 均为内部预留配置，当前使用场景和技能默认设置。

`put` 复用 `pick` 的接近与撤离流程，并使用 `approach` / `retreat` 的公共候选位姿逻辑；下降接触、释放和释放后的撤离时序由 `put` 自身编排。

```json
{
  "command_id":"1",
  "skill_name":"put",
  "parameters":{
    "target":103,
    "release_request":{
      "strategy_params":{
        "open_width":0.085
      }
    }
  }
}
```

### pull / push

两者共用相对运动参数，接触方式有所不同。

| 参数 | 类型 | 必填 / 默认 | 含义 |
| --- | --- | --- | --- |
| `target` | integer / string | 首次接近必填 | 场景对象 ID；已抓持注册的活动 link 时可省略 |
| `link` | string | 目标有多个活动 link 时必填 | 选择操作部件；唯一活动 link 可自动解析 |
| `contact_mode` | string | grasp | pull 仅支持 grasp；push 支持 grasp / contact |
| `grasp.mode` | string | binding；省略目标时可继承已有抓持模式 | 抓持方式 binding / physical，contact 时不填写 |
| `distance` | number | 直线运动必填 | 正数，相对末端行程，m；contact 从首次真实接触处开始计量，不含空行程 |
| `direction` | number[3] | pull 为 [0,0,-1]；push 为 [0,0,1] | 直线运动方向，非零向量 |
| `frame` | string | tool | direction、圆弧偏移与轴向所在坐标系：tool / world |
| `angle_deg` | number | 相对圆弧必填 | 按右手定则带符号的角度增量，度 |
| `arc_center_offset` | number[3] | 相对圆弧必填 | 相对起始 TCP 原点的轴心偏移，m |
| `arc_axis` | number[3] | 相对圆弧必填 | 旋转轴方向，非零向量 |
| `contact` | object | 可自动生成 | 交互目标位姿；ApproachSkill 从该位姿反向搜索接近位姿 |
| `approach_search` | object | `3～10 cm`，步长 `5 mm` | 覆盖 ApproachSkill 的搜索区间和步长 |
| `retreat_search` | object | `3～10 cm`，步长 `5 mm` | 覆盖 RetreatSkill 的搜索区间和步长 |
| `pose_axis` | string / number[3] | `+z` | 目标位姿局部接近运动轴；接近点沿该轴反向推导 |
| `grasp_request` | object | 可选，仅 grasp 方式 | 内部抓取请求覆盖 |
| `request` | object | 可选 | 推拉本身的约束，例如 maximum_force、timeout |
| `request.constraints.contact_threshold` | number | contact 编排默认 0.1 | 运动中真实指垫接触的确认阈值，N |
| `contact_geom` | string | 可选 | 显式限定目标接触表面；省略时接受所选 link 上的真实指垫接触 |

接口约定：

- 相对运动使用 distance 或 angle_deg，二者只能选一个。contact 编排当前只支持 distance 直线 push。
- tool 指运动开始时的 TCP 坐标系；显式 direction 就是实际运动方向，不因 pull 再次反转。
- contact 保持现有夹爪开度，不主动张开或闭合；要求当前没有抓持对象，不能同时填写 grasp / grasp_request。
- contact 自动接近沿用原始把手抓取位姿，不额外增加末端旋转、指垫尺寸补偿或初始间隙偏移；接触方式不同本身不改变目标位姿。
- contact 不要求起步前已有接触。接近完成后先向前运动，首次真实接触建立时将有效行程归零，然后继续推动 distance。空行程不扣减 distance，也不计入 actual_delta / travel_distance。规划用几何接触预测延长同一连续路径，成功与剩余行程依据实际接触位置验证。
- contact 不使用独立的 1 mm 小步 / 15 mm 搜索。建立接触需要达到接触力阈值；之后实际接触仍存在但力降低，不单独判为接触丢失。真实指垫接触消失后，仍按丢失超时停止；非允许碰撞、最大力与关节限位检查保持有效。前进结束仍未接触时返回 CONTACT_NOT_ESTABLISHED，有效行程为 0、请求行程全部未完成，已发生运动则为 suspend。
- 标准相对 pull 和 push 编排在有交互位姿时都先尝试 ApproachSkill；接近规划失败且没有产生物理运动时，降级为 MoveSkill 直接到交互位姿。到达交互位姿后，以实测状态静态规划完整推拉轨迹，随后执行 grasp（pull 必需，push 可选）和已准备轨迹，不在执行阶段重新规划。grasp 成功时执行对应 release，最后调用 RetreatSkill 搜索撤退位姿。
- push 的 contact 模式不抓取、不闭合，也不执行 release；先执行 ApproachSkill，再静态规划允许执行中建立接触的 push 轨迹。接触力达到阈值后开始计量有效行程，接触丢失或力反馈异常时停止并返回 suspend。
- `contact.mechanism_pose` 可以由命令显式提供；当前场景省略时由 demo 的机构几何 fallback 生成。push/pull 不通过 `approach` 或 `retreat` 布尔参数触发这两个 task skill，存在交互位姿时由编排流程直接调用。
- 现有 `target_angle_deg` 是柜门兼容接口中的绝对机构目标角度，与相对 angle_deg 不同，不能混用。`best_effort` 用于部分执行反馈，不会将未完成任务标为成功。
- 当前 demo 的推拉对象解析面向已注册活动 link。target/link 选择接触对象，相对运动量由末端几何参数定义。

#### pull 执行流程

```text
交互位姿 → ApproachSkill
         → 失败且无实际运动时由 MoveSkill 到交互位姿
         → 以实测交互状态静态规划完整 pull 路径
         → grasp → 执行已准备路径 → release → RetreatSkill
```

pull 只支持 `contact_mode: grasp`，必须建立抓持；力反馈异常或路径执行未完成时停止并返回 `suspend`。

#### push 执行流程

`contact_mode: grasp` 时流程与 pull 相同，但 grasp 是可选的；只有实际 grasp 成功时才执行对应 release。`contact_mode: contact` 时不抓取、不闭合、不 release：

```text
交互位姿 → ApproachSkill
         → 以实测交互状态静态规划 contact push 路径
         → 执行中建立真实接触并完成 push → RetreatSkill
```

contact push 的有效行程从首次真实接触开始计量；接触力异常或接触丢失时停止并返回 `suspend`。

抓持式拉动：

```json
{"command_id":"pull-1","skill_name":"pull","parameters":{"target":9,"link":"Nightstand0061_drawer01","contact_mode":"grasp","grasp":{"mode":"physical"},"distance":0.12}}
```

直接接触推动：

```json
{"command_id":"push-1","skill_name":"push","parameters":{"target":9,"link":"Nightstand0061_drawer01","contact_mode":"contact","distance":0.12}}
```


## 4. 返回接口

每条命令结果包含 `command_id`、`skill_name`、`success` 和对应技能结果；复合动作通过 `substeps` 记录内部阶段结果。

| 字段 | 含义 |
| --- | --- |
| `success` | 命令是否成功 |
| `status` | 执行状态 |
| `error` | 失败时的错误信息 |
| `substeps` | 接近、抓取、执行和撤退等阶段结果 |
