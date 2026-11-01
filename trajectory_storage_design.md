# 仿真场景与轨迹数据存储设计

## 1. 设计目标

同一个仿真场景可能产生多条不同轨迹。每条轨迹中，机械臂、夹爪和场景物体的运动状态可能不同，因此需要满足：

- 场景结构和资源只保存一份；
- 不同轨迹分别独立存储；
- 轨迹文件只记录状态变化；
- 可以校验轨迹与场景是否匹配；
- 支持关键帧、增量帧和随机跳转回放；
- 后续可以增加动态物体和轨迹元数据。

核心关系：

```text
场景清单 = 有哪些对象、对象是什么、如何解释状态数据
轨迹文件 = 这些对象在本次运行中如何变化
```

## 2. 推荐目录结构

```text
scene_warehouse_001/
├── scene.json                 # 场景清单，只保存一份
├── assets/
│   ├── robot.urdf
│   ├── gripper.urdf
│   └── box.glb
├── trajectories/
│   ├── pick_from_left_001.bin
│   ├── pick_from_right_001.bin
│   └── idle_test_001.bin
└── index.json                 # 可选：轨迹列表和元数据
```

场景资源、实体定义和关节顺序由 `scene.json` 统一管理；每条轨迹使用一个独立的二进制文件。

## 3. 场景清单

场景清单保存场景中相对稳定的信息：

- 场景 ID 和版本；
- 实体 ID、名称和实体类型；
- 机器人和夹爪的关节名称及顺序；
- 模型资源引用；
- 初始位姿；
- 坐标系、单位和四元数顺序；
- 可选的父子关系。

示例：

```json
{
  "format": "RobotSceneStream",
  "version": 1,
  "scene_id": "warehouse_001",
  "scene_version": 3,
  "scene_hash": "sha256:...",
  "nominal_fps": 60,
  "byte_order": "little",
  "units": {
    "length": "m",
    "angle": "rad",
    "time": "us"
  },
  "coordinates": {
    "handedness": "right",
    "up_axis": "Z",
    "pose_space": "world",
    "quaternion_order": "xyzw"
  },
  "entities": [
    {
      "id": 1,
      "name": "robot_arm",
      "type": "articulation",
      "asset": "assets/robot.urdf",
      "joint_names": [
        "shoulder_pan_joint",
        "shoulder_lift_joint",
        "elbow_joint",
        "wrist_1_joint",
        "wrist_2_joint",
        "wrist_3_joint"
      ]
    },
    {
      "id": 2,
      "name": "gripper",
      "type": "articulation",
      "parent_id": 1,
      "parent_link": "tool0",
      "asset": "assets/gripper.urdf",
      "joint_names": [
        "left_driver_joint",
        "right_driver_joint"
      ]
    },
    {
      "id": 100,
      "name": "box",
      "type": "rigid_body",
      "asset": "assets/box.glb"
    }
  ]
}
```

后续二进制帧只使用整数 `entity_id`，不再重复传输实体名称。

## 4. 场景版本与哈希

轨迹文件必须绑定某个明确的场景版本。建议在 `scene.json` 中保存：

- `scene_id)：逻辑场景名称；
- `scene_version)：场景结构版本；
- `scene_hash)：场景清单和相关资源的内容哈希。

轨迹文件头也保存 `scene_hash`。加载轨迹时：

```text
1. 加载 scene.json；
2. 校验 scene_version 和 scene_hash；
3. 如果不匹配，拒绝播放或提示用户选择兼容版本；
4. 使用匹配的场景清单解释轨迹数据。
```

如果修改了实体、关节顺序、模型资源或坐标约定，应生成新的场景版本。旧轨迹继续绑定旧版本，不应直接套用新清单。

## 5. 轨迹元数据

轨迹元数据可以放在二进制文件头中，也可以单独保存为 JSON。示例：

```json
{
  "trajectory_id": "pick_from_left_001",
  "scene_id": "warehouse_001",
  "scene_version": 3,
  "scene_hash": "sha256:...",
  "duration_s": 12.5,
  "nominal_fps": 60,
  "description": "从左侧抓取箱子"
}
```

也可以在 `index.json` 中维护多条轨迹的列表：

```json
{
  "scene_id": "warehouse_001",
  "scene_version": 3,
  "trajectories": [
    {
      "id": "pick_from_left_001",
      "file": "trajectories/pick_from_left_001.bin",
      "duration_s": 12.5
    },
    {
      "id": "pick_from_right_001",
      "file": "trajectories/pick_from_right_001.bin",
      "duration_s": 11.8
    }
  ]
}
```

## 6. 二进制轨迹文件

每个轨迹文件独立保存一条完整轨迹：

```text
trajectory_0001.bin
├── 文件头
├── 关键帧
├── 增量帧
└── 关键帧索引
```

### 6.1 文件头

建议字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `magic` | 4 bytes | 文件标识，例如 `RST1` |
| `format_version` | uint16 | 二进制格式版本 |
| `scene_hash` | 32 bytes | 对应场景清单的哈希 |
| `trajectory_id` | uint64 | 轨迹唯一 ID |
| `nominal_fps` | uint16 | 名义采样频率 |
| `start_time_us` | uint64 | 轨迹起始时间 |
| `frame_count` | uint32 | 帧数量 |
| `index_offset` | uint64 | 关键帧索引位置 |

整数使用小端序，浮点数使用 IEEE 754 `float32`。字段按约定顺序写入，不依赖语言结构体的隐式对齐。

### 6.2 帧头

建议使用固定长度帧头：

| 字段 | 类型 | 含义 |
|---|---|---|
| `frame_bytes` | uint32 | 整帧字节数 |
| `frame_id` | uint32 | 连续帧序号 |
| `timestamp_us` | uint64 | 相对轨迹起点的仿真时间 |
| `step_id` | uint32 | 所属任务步骤 |
| `base_frame_id` | uint32 | 增量帧依赖的前一帧 |
| `flags` | uint16 | 关键帧、保持等标志 |
| `record_count` | uint16 | 本帧记录数量 |
| `reserved` | uint32 | 保留字段，写零 |

### 6.3 实体更新记录

每条实体更新包含记录头和可变长度数据：

| 字段 | 类型 | 含义 |
|---|---|---|
| `entity_id` | uint32 | 场景实体 ID |
| `component_type` | uint8 | 状态类型 |
| `field_mask` | uint8 | 当前记录包含的字段 |
| `payload_bytes` | uint16 | 数据长度 |
| `payload` | bytes | 实际状态数据 |

第一版可以定义以下状态类型：

| 类型 | 内容 | 数据布局 |
|---|---|---|
| `1` | 位姿 | 位置 3×float32，四元数 4×float32 |
| `2` | 关节位置 | 按场景清单顺序写入 N×float32 |
| `3` | 夹爪宽度 | 1×float32 |

位姿的 `field_mask` 可以定义为：

```text
bit 0：包含 position
bit 1：包含 rotation
```

## 7. 关键帧与增量帧

轨迹文件同时支持两种帧：

- **关键帧**：保存当前场景的完整状态；
- **增量帧**：只保存相对于上一状态发生变化的实体或字段。

示例：

```text
帧 0：关键帧
  机械臂完整关节位置
  夹爪完整关节位置
  箱子完整位姿
  其他物体完整状态

帧 1：增量帧
  机械臂关节位置

帧 2：增量帧
  机械臂关节位置
  夹爪关节位置

帧 3：增量帧
  机械臂关节位置
  箱子位姿
```

没有出现在增量帧中的实体或字段，接收端沿用上一帧状态。

增量帧中的值建议保存为**新的绝对值**，而不是位置差或角度差。例如：

```text
箱子新位置 = [1.2, 0.3, 0.1]
```

直接保存新位置可以避免增量累积造成的浮点误差。

建议：

- 第一个状态必须是关键帧；
- 每隔 1～2 秒插入一个关键帧；
- 关键帧间隔根据文件大小、随机跳转需求和带宽调整；
- 跳转播放时，从目标时间之前最近的关键帧开始恢复；
- 如果依赖的增量帧缺失，应等待新关键帧或报告数据不完整；
- 判断变化时应与上一次发送状态比较；
- 静止物体不需要每帧发送。

## 8. 状态表达建议

机械臂通常只需传关节角度，由接收端根据相同的运动学模型计算连杆位姿。

夹爪的 `gripper_q` 和 `gripper_width_m` 可能表达同一状态。如果二者可以稳定互相转换，建议选择一个作为权威驱动数据：

- 需要根据模型驱动夹爪：保存驱动关节位置；
- 只关心开口大小：保存夹爪宽度；
- 如果分析工具需要两者，可以同时保存，但必须明确哪一个是权威值。

普通刚体保存世界坐标中的位置和旋转即可。

如果机械臂基座会移动，则额外保存机械臂基座位姿。

## 9. 时间与等待

建议使用 `timestamp_us` 作为播放时间依据，`nominal_fps` 只表示名义采样频率。

这样可以支持：

- 非固定帧率；
- 仿真暂停；
- 不同轨迹使用不同采样频率；
- 避免固定帧间隔取整产生累计误差。

原来的 `wait_time_s` 可以转换为步骤事件，或者直接反映在时间线上：

```text
0.000 秒：初始状态
1.000 秒：开始运动
1.017 秒：下一状态
```

等待期间不需要重复保存相同状态，但需要使用保持标志或事件，避免播放器把等待区间误认为连续运动。

## 10. 动态实体事件

如果场景中的物体从开始到结束始终存在，只需要记录其位姿变化。

如果轨迹过程中会创建、删除或改变父子关系，可以加入事件记录：

```text
SPAWN_ENTITY
DESPAWN_ENTITY
SET_PARENT
SET_ACTIVE
```

基础场景清单仍然描述可能出现的实体和资源，轨迹事件负责描述本次运行期间的动态变化。

如果首版场景实体固定，可以暂不实现这些事件。

## 11. 回放流程

```text
1. 根据 scene_id 和 scene_version 加载场景清单；（这部分我后台和殷振康前端可以公用一个场景清单，我只把轨迹文件传给他，他那边完成回放后更新清单）
2. 校验轨迹文件中的 scene_hash；
3. 加载模型、实体和初始状态；
4. 从目标时间之前最近的关键帧开始；
5. 按 timestamp_us 顺序应用增量帧；
6. 根据机械臂关节和夹爪关节计算模型姿态；
7. 将刚体位姿直接应用到场景；
8. 遇到动态实体事件时更新实体集合。
```

## 12. 数据量估算

假设：

- 六轴机械臂；
- 双关节夹爪；
- 场景中有 100 个刚体；
- 采样频率 60 FPS；
- 每帧只有机械臂、夹爪和 5 个刚体发生变化。

单帧大致为：

```text
帧头                         32 字节
机械臂关节记录          8 + 24 = 32 字节
夹爪关节记录            8 +  8 = 16 字节
5 个刚体位姿记录    5 × (8 + 28) = 180 字节

合计                        260 字节/帧
```

约为：

```text
260 × 60 = 15.6 KB/s
约 56 MB/小时
```

如果每帧都传输 100 个刚体的完整位姿，即使使用二进制，数据量也会显著增加。因此，场景越大、静止物体越多，增量帧的收益越明显。

## 13. 第一版实现范围

建议第一版实现以下内容：

1. JSON 场景清单；
2. 场景版本和 `scene_hash`；
3. 独立的二进制轨迹文件；
4. 二进制关键帧和增量帧；
5. `float32` 状态数据；
6. 显式时间戳；
7. 机械臂关节位置；
8. 夹爪关节位置或夹爪宽度；
9. 普通刚体位姿；
10. 关键帧索引；
11. 基本的轨迹加载和回放校验。

量化编码、逐关节掩码、块压缩、网络分包和动态实体事件可以在完成第一版并测量真实数据后再增加。

