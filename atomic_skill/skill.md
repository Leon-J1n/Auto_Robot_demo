# 原子技能与任务装配速查

本文面向 `demo.run_demo` 的任务级 command 接口。距离单位为 **m**，力单位为 **N**；场景对象用其场景 ID 指定，`home` 为场景配置中的命名目标。

## 1. Command 格式

```json
{"command_id": "1", "skill_name": "approach", "parameters": {"target": 7}}
```

| 字段 | 含义 |
| --- | --- |
| `command_id` | 本次任务内唯一的字符串标识 |
| `skill_name` | 技能名称，如 `move_to`、`pick`、`put`、`approach`、`retreat` |
| `parameters` | 技能参数对象；所有技能参数均放在此处 |

## 2. 技能职责与参数

### move_to：移动到目标

规划并移动到目标，保持当前夹持状态。对象目标默认使用抓取交互位姿；`home` 使用已配置的回位目标。

```json
{"command_id": "1", "skill_name": "move_to", "parameters": {"target": "home", "planning_method": "joint"}}
```

| 参数 | 必填 / 默认 | 含义 |
| --- | --- | --- |
| `target` | 必填 | 场景对象 ID、命名目标（如 `home`），或 `end_effector` |
| `planning_method` | 普通目标 `joint`；`end_effector` 为 `linear` | 常用 `joint`（关节运动）或 `linear`（直线运动） |
| `relation` | 可选 | 相对方向：`above`、`below`、`front`、`behind`、`left`、`right` |
| `distance_m` | 相对偏移时使用，默认 `0` | 沿 `relation` 的偏移距离 |
| `keep_end_effector_orientation` | `false` | 保持执行开始时的末端姿态；需要全程保持时配合 `linear` |

`home` 与 `end_effector` 是两种不同用途的目标：

| 目标 | 作用与使用方式 |
| --- | --- |
| `home` | 预先配置的回位或中转目标，从最终场景的 `named_targets.home` 读取。可配置为固定关节角或末端位姿，执行 `move_to(home)` 即规划到该配置目标；适合空手回位，也可用于夹持物体后的中转。 |
| `end_effector` | 以本条 command 开始时的实际末端位姿为基准，结合 `relation` 和 `distance_m` 生成相对移动目标；适合在当前位置抬高、下降或平移。每次调用都从当时的末端位置重新计算。 |

两者均保持当前夹持状态。上面的 `move_to(home)` 示例用于回到已配置的目标；下面的 `move_to(end_effector)` 示例用于从当前位置沿世界 +Z 移动 5 cm，并保持当前末端朝向：

```json
{"command_id": "1", "skill_name": "move_to", "parameters": {"target": "end_effector", "relation": "above", "distance_m": 0.05, "planning_method": "linear", "keep_end_effector_orientation": true}}
```

### pick：到达抓取位姿并抓住物体

移动到目标的抓取位姿，闭合夹爪并确认抓持；接触力不足、无法确认抓持时返回错误。通常先执行 `approach`；抬升与撤退由后续的 `lift_up`、`retreat` 完成。

```json
{"command_id": "2", "skill_name": "pick", "parameters": {"target": 7, "hold_force": 15.0}}
```

| 参数 | 必填 / 默认 | 含义 |
| --- | --- | --- |
| `target` | 必填 | 要抓取的场景对象 ID |
| `hold_force` | `10.0` | 抓持后的保持力 |

### put：到达放置位姿并释放物体

在已夹持物体的前提下，移动到目的地的放置位姿并打开夹爪。释放的是当前夹持物体，`target` 表示放置目的地；后续单独执行 `retreat`。

```json
{"command_id": "7", "skill_name": "put", "parameters": {"target": 12}}
```

| 参数 | 必填 / 默认 | 含义 |
| --- | --- | --- |
| `target` | 必填 | 放置目的地的场景对象 ID，例如篮子或托盘 |
| `relation` | `at` | `at` 表示原有内部放置；`front`、`behind`、`left`、`right` 表示放在目标外侧 |
| `distance_m` | `0` | 从目标对应方向的包围盒边界向外偏移的距离，必须为大于或等于 0 的有限数值 |

例如，放到托盘 12 前面 10 cm，只需在 `put` 上设置相对参数；前面的 `approach` 自动继承：

```json
[
  {"command_id": "6", "skill_name": "approach", "parameters": {"target": 12}},
  {"command_id": "7", "skill_name": "put", "parameters": {"target": 12, "relation": "front", "distance_m": 0.10}},
  {"command_id": "8", "skill_name": "retreat", "parameters": {"target": 12}}
]
```

前后左右采用机器人基座方向在桌面平面的投影，与对象相对 `move_to` 的方向约定一致：前为基座 +X、左为基座 +Y，后和右取反向。先将目标自身包围盒变换到世界坐标，求对应方向的边界，再向外偏移指定距离。VLM 在该位置附近半径 2 cm 的区域选择支撑表面，深度取自当前 RGB-D；因此 10 cm 表示区域中心距边界的距离，最终选点允许在该区域内变化。仍沿世界 Z 轴搜索 0～10 cm 放置高度，同轮 approach/put 共用结果。相对区域不可见、深度无效或无可行位姿时返回具体错误。当前 `put` 的相对关系限于上述四个水平方向；`above`、`below` 不在此接口中。

### approach：移动到交互前的接近位姿

先选择目标交互位姿，再沿接近轴反向偏移，搜索可到达的接近点。竖直抓放时，接近点通常位于交互点上方。它负责接近移动，夹爪状态保持不变。

```json
{"command_id": "1", "skill_name": "approach", "parameters": {"target": 7, "search": {"min_distance": 0.02, "max_distance": 0.10, "step": 0.005}}}
```

| 参数 | 必填 / 默认 | 含义 |
| --- | --- | --- |
| `target` | 必填 | 要接近的场景对象 ID |
| `search.min_distance` | `0.02` | 最小接近距离，即 2 cm |
| `search.max_distance` | `0.10` | 最大接近距离，即 10 cm |
| `search.step` | `0.005` | 搜索步长，即 5 mm |

`search` 可整体省略。默认按 10、9.5、9……2 cm，从远到近尝试，找到可执行路径后执行。只有尚未产生真实运动的规划失败才继续尝试下一档。

### retreat：沿原交互方向撤离

读取 `target` 最近一次成功的 `approach`、`pick` 或 `put` 的交互朝向，结合当前实际末端位置与撤退距离生成终点，保持当前末端朝向并直线撤离。它沿用原交互方向，从当前状态出发；没有成功交互记录时无法确定方向。

```json
{"command_id": "4", "skill_name": "retreat", "parameters": {"target": 7, "search": {"min_distance": 0.03, "max_distance": 0.10, "step": 0.005}}}
```

| 参数 | 必填 / 默认 | 含义 |
| --- | --- | --- |
| `target` | 必填 | 用于查询原交互记录的场景对象 ID |
| `search.min_distance` | `0.03` | 最小撤退距离，即 3 cm |
| `search.max_distance` | `0.10` | 最大撤退距离，即 10 cm |
| `search.step` | `0.005` | 搜索步长，即 5 mm |
| `pose_axis` | 默认使用工具标定轴 | 可选的原交互局部参考轴，如 `+z`、`-z`；实际撤退沿其反向 |

`search` 可整体省略。默认从当前实际位置依次尝试撤退 10～3 cm。

## 3. Commands 如何装配

任务文件包含场景信息和一个按执行顺序排列的 `commands` 数组。下面以“把物体 7 放到容器 12”为例；ID 和场景版本须对应实际注册场景，`home` 须已配置。

```json
{
  "schema_version": "2.0",
  "request_id": "pick-and-place-example",
  "scene_id": 3,
  "scene_version": 3,
  "commands": [
    {"command_id": "1", "skill_name": "approach", "parameters": {"target": 7}},
    {"command_id": "2", "skill_name": "pick", "parameters": {"target": 7, "hold_force": 15.0}},
    {"command_id": "3", "skill_name": "lift_up", "parameters": {"distance": 0.02}},
    {"command_id": "4", "skill_name": "retreat", "parameters": {"target": 7}},
    {"command_id": "5", "skill_name": "move_to", "parameters": {"target": "home", "planning_method": "joint"}},
    {"command_id": "6", "skill_name": "approach", "parameters": {"target": 12}},
    {"command_id": "7", "skill_name": "put", "parameters": {"target": 12}},
    {"command_id": "8", "skill_name": "retreat", "parameters": {"target": 12}}
  ]
}
```

装配时遵循以下规则：

- 数组顺序决定执行顺序；`command_id` 用于标识和报告，建议按顺序编号。
- 抓取阶段的 `approach`、`pick`、`retreat` 使用被抓物体 ID；放置阶段的 `approach`、`put`、`retreat` 使用目的地 ID。
- 同一目标的 `pick` / `put` 会关联其前最近的 `approach` 与后最近的 `retreat`，确定抓取或放置语义；同一目标可分别保存两种交互位姿。
- 当前 VLM 流程根据 commands 自动推断感知类型，默认相机为 `global`，无需额外填写 `visual_estimates`。
- 抓取位姿在首次需要且没有缓存时感知，后续随目标运动维护；每轮放置在对应 `approach` 开始时重新感知，同轮 `put` 共用结果。直接调用 `put` 时在其开始时感知。
- `lift_up` 从当前末端位姿沿世界 +Z 直线抬升，保持当前朝向；参数为 `distance`，默认 `0.02`，必须大于 0，无需 `target`。

## 4. 常用任务级 Skill Graph

`object` 表示被抓物体，`destination` 表示目的地；箭头表示成功后进入下一步。

### 抓起来某物

```text
approach(object) → pick(object) → lift_up(distance) → retreat(object)
```

### 放置到某处

前置条件：已经夹持物体。

```text
approach(destination) → put(destination) → retreat(destination)
```

### 把某物放置某处

```text
approach(object) → pick(object) → lift_up(distance) → retreat(object)
    → move_to(home)
    → approach(destination) → put(destination) → retreat(destination)
```

把图中的每个节点依次写成一条 command 即可；完整装配示例见上一节。
