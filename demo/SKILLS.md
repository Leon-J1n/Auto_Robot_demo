# Skill Command 技能列表

统一使用 `demo.run_demo`，支持以下 8 个 skill。每条命令必须包含唯一的 `command_id`、`skill_name` 和目标参数；`pour` 使用 `source` 与 `target`。

| Skill     | 必填参数 | 可选参数 | 描述          |
|-----------| --- | --- |-------------|
| `put` | `target` | `planning_method`、`keep_end_effector_orientation`、`release_request` | 从上方接近、放置并直线返回 |
| `move`    | `target` | `planning_method`、`keep_end_effector_orientation`、`relation`、`distance_m` | 移动到目标处     |
| `pick` | `target` | `planning_method`、`grasp`、`grasp_request` | 接近、抓取并退回附近点 |
| `grasp` | `target` | `grasp`、`strategy_params`、`constraints` | 原地闭合夹爪并确认抓取 |
| `release` | `target` | `strategy_params`、`constraints` | 解除绑定并松开夹爪 |
| `pour` | `source`、`target` | `outlet_offset_object`、`target_offset_object`、`tilt_axis_tool`、`start_tilt_deg`、`pour_tilt_deg`、`tilt_step_deg` | 已抓持源容器的非接触干式倒水动作，不模拟液体 |

## 通用命令格式

`pick` 选解时按关节变化量排序，并在独立的 MuJoCo 模型和状态副本中模拟夹爪打开、闭合及稳定接触确认；机械臂和静态场景使用 FCL 检查，夹爪、活动机构和携带物体的实时几何由 MuJoCo 距离检查补充。某候选失败时继续尝试下一候选；副本试验不修改现场关节、物体、驱动力矩限制或绑定状态。该预检查不能保证运动到位后的实际状态完全一致，因此执行闭合时仍保留碰撞检查。

抓取碰撞分类：全部候选未通过预检查时为 `GRASP_CLOSURE_INFEASIBLE / grasp_precheck`，详情在 `rejected`；实际闭合碰撞为 `GRASP_CLOSURE_COLLISION / grasp_closure`；打开夹爪时碰撞为 `GRASP_OPENING_COLLISION / grasp_opening`。接触详情包含部件名称、距离及 `geometry_basis=simulation_collision_geometry`，表示仿真碰撞几何的相交，不应直接解读为原始视觉网格精确相交。这些错误可通过更换抓取位姿或修正碰撞资产恢复，不再归为 `INTERNAL_ERROR / unknown`。

携带安全距离由 `pick.parameters.grasp_request.strategy_params.safety_distance` 指定，单位为米，例如 `0.01` 表示 10 mm；独立 `grasp` 命令使用 `parameters.strategy_params.safety_distance`。值必须是有限非负数，省略或设为 0 表示不增加额外余量。此参数由 demo 解析和校验，底层 grasp 技能接口保持原样。

抓取成功后，demo 在使用正安全距离的规划检查中，对“携带物与外部环境”取命令原有距离与该参数的较大值（binding、physical 均适用），覆盖 retreat、后续目标选解和运动规划。机械臂、夹爪内部及夹爪与非持物之间保持原有规则；夹爪与已确认持物的接触按下文规则处理，初始支撑面接触仍使用原有抬离豁免。release 解除携带后取消额外余量，恢复后续命令原有距离（当前场景默认 `0.003 m`）；下一次抓取重新读取自己的参数，不继承上一次设置。执行时零余量的碰撞判定保持不变。报告子步骤的 `payload_safety_distance_m` 记录有效规划距离。

```json
"grasp_request": {
  "strategy_params": {
    "hold_force": 1.0,
    "safety_distance": 0.01
  }
}
```

对象目标的 `move` 在满足终点碰撞与安全距离要求的候选逆解中，优先选择距当前关节角欧氏距离最小的解。直达路径受阻时，默认 `auto` 规划会尝试绕障，不会仅因较远的解可以直达就优先选择远解。此规则优化目标关节变化量，不保证绕障后的路径全局最短；若选定解的规划失败，会报告失败。`home` 使用配置中的固定关节目标。

```json
{
  "command_id": "example-001",
  "skill_name": "技能名称",
  "parameters": {
    "target": "目标"
  }
}
```

`target` 使用场景实例 ID（例如 `101`）；`put.target` 是放置目的地对象 ID，释放的是当前夹持物体。`move` 还支持 `home` 等命名目标和 `end_effector` 相对运动目标。

## 各 skill 示例

### move

在统一场景入口中，`move` 一次运动到目标，不自动插入附近点、抓取或退回动作。对象目标使用场景实例 ID（例如 `101` 或 `"101"`）；demo 查询 ObjectLibraryServer 的 `grasp_properties` 并换算交互位姿，通过既有位姿请求调用运动技能。对象查询和运动编排保留在 `demo/`。`home` 等命名目标继续使用 `move`。

```json
{
  "command_id": "example-004",
  "skill_name": "move",
  "parameters": {
    "target": 101,
    "planning_method": "joint"
  }
}
```

完整统一场景示例见 `demo/task_commands/commands.001.json`。以下为对象目标的直线运动示例：

```json
{
  "command_id": "example-005",
  "skill_name": "move",
  "parameters": {
    "target": 101,
    "planning_method": "linear"
  }
}
```

在`parameters`中`target`为必要变量，其余可省略。

支持的 `planning_method` 值：

- `linear`：直线规划
- `circular`：圆弧规划
- `joint`：关节空间规划

`keep_end_effector_orientation` 用于指定是否保持末端姿态不变：

```json
{
  "command_id": "example-006",
  "skill_name": "move",
  "parameters": {
    "target": 101,
    "planning_method": "linear",
    "keep_end_effector_orientation": true
  }
}
```

当需要执行抓取后的上抬、下降或撤离等末端相对运动时，将目标写为 `end_effector`，并使用 `relation` 与 `distance_m`：

```json
{
  "command_id": "example-007",
  "skill_name": "move",
  "parameters": {
    "target": "end_effector",
    "relation": "above",
    "distance_m": 0.125,
    "planning_method": "linear"
  }
}
```

支持的末端相对方向包括：

- `above`：上抬
- `below`：下降
- `front`：前移
- `behind`：后撤
- `left`：左移
- `right`：右移

### pick

执行：到附近点 → 保持姿态直线到目标 → 闭合确认抓取 → 保持姿态直线返回同一附近点。附近点距离约 10～15 cm；找不到可行点时失败，不回退为单段 move。闭合确认失败时不执行退回。目标必须是对象，不能是 `home` 或 `end_effector`。

```json
{
  "command_id": "pick-001",
  "skill_name": "pick",
  "parameters": {
    "target": 101,
    "planning_method": "joint",
    "grasp": {
      "mode": "physical"
    },
    "grasp_request": {
      "strategy_params": {
        "grasp_force": 20.0,
        "hold_force": 20.0
      }
    }
  }
}
```

`grasp_request` 可覆盖完整 Grasp 请求，但目标必须与 pick 一致。

### put

执行：移动到目标上方靠近点 → 保持姿态直线到目标 → 释放当前夹持物体并松开夹爪 → 保持姿态直线返回同一靠近点。

沿世界坐标系 `+Z` 方向搜索目标上方 10～15 cm 的靠近点，校验逆解、碰撞和下降直线路径；找不到可行点时失败。接近段使用 `planning_method`，下降和返回段固定为严格直线，不允许改成绕障路径。返回段在释放完成后，按实际状态重新规划和检查碰撞。

`target` 为放置目的地对象的场景实例 ID，使用与 `move` 相同的对象服务交互位姿；该位姿表示夹爪交互坐标系的目标位姿。开始时必须已夹持物体，不能使用 `home` 或 `end_effector`。`release_request` 可设置松爪参数，释放目标自动取当前夹持物体；不允许覆盖为其他物体或只验证释放。释放失败时停止，不执行返回。

```json
{
  "command_id": "put-001",
  "skill_name": "put",
  "parameters": {
    "target": 103,
    "planning_method": "joint",
    "release_request": {
      "strategy_params": {"open_width": 0.085}
    }
  }
}
```

完整 `pick → put → move home` 示例见 `demo/task_commands/commands.001.json`，通过 `demo.run_demo` 运行。

### pour

`pour` 是 demo 层的干式倒水编排。命令要求 `source` 和 `target` 使用场景对象名称，且执行前 `source` 已被当前夹爪抓持。它查询两者实时位姿，按虚拟出水口偏移和目标区域偏移生成 0° 到 `pour_tilt_deg` 的分段姿态；每段先尝试保持当前 IK 分支的笛卡尔目标，必要时使用显式配置的腕关节回退。目标容器不建立接触，流程不模拟液体或倒入量。

```json
{
  "command_id": "pour-004",
  "skill_name": "pour",
  "parameters": {
    "source": "Bottle",
    "target": "Mug",
    "outlet_offset_object": [0.0, -0.15, 0.0],
    "target_offset_object": [0.0, 0.0, 0.08],
    "tilt_axis_tool": [1.0, 0.0, 0.0],
    "start_tilt_deg": 0.0,
    "pour_tilt_deg": 140.0,
    "tilt_step_deg": 10.0,
    "clearance_m": 0.08,
    "joint_wrist_fallback": true,
    "joint_index": 5
  }
}
```

报告中的 `mode` 为 `dry_pour`，`fluid_simulated` 为 `false`；成功表示位姿段执行完成，不表示真实液体转移。

### grasp

在当前位置闭合夹爪并确认抓取，不负责移动机械臂。`grasp.mode` 默认 `binding`：

| 模式 | 确认抓取后的携物方式 |
| --- | --- |
| `binding` | 稳定双侧接触确认且结果校验通过后，启用临时 weld |
| `physical` | 仅依靠夹爪电机和接触摩擦，不启用 weld，也不自动回退到绑定 |

两种模式都要求明确的动态目标和 `hold_on_success=true`。确认阶段默认每侧法向力至少 2 N，连续稳定 0.1 秒仿真时间；夹宽变化不超过 0.5 mm，相对位置变化不超过 1 mm，相对转角变化不超过 0.035 rad。重新抓取前应先释放。

```json
{
  "command_id": "grasp-001",
  "skill_name": "grasp",
  "parameters": {
    "target": 101,
    "grasp": {
      "mode": "physical"
    },
    "strategy_params": {
      "grasp_force": 20.0,
      "hold_force": 20.0,
      "close_speed": 0.5,
      "contact_threshold": 2.0,
      "confirmation_time": 0.1
    }
  }
}
```

独立 grasp 使用 `parameters.strategy_params`；pick 内嵌抓取使用 `parameters.grasp_request.strategy_params`。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `grasp_force` | 20.0 | 闭合阶段力度参数 |
| `hold_force` | 10.0 | 物理模式保持阶段左右指垫法向接触力之和的目标值，单位 N |
| `open_width` | 0.08 | 抓取前张开宽度，单位 m |
| `close_width` | 0.0 | 闭合目标宽度，单位 m |
| `close_speed` | 0.5 | 夹爪动作速度参数 |
| `contact_threshold` | 2.0 | 确认抓取时每侧最低法向接触力，单位 N |
| `confirmation_time` | 0.1 | 连续稳定确认时间，单位 s |

物理模式的 `hold_force` 是左右指垫接触法向力之和的目标（不是每侧目标）。保持期间每个物理步读取接触力，经 20 ms 低通滤波后小幅调整闭合位置；死区 0.2 N、调整速度上限 0.2 rad/s，驱动器独立限制为 ±2 N·m。实际力会有瞬态误差，也可能因机构或力矩饱和无法达到目标。缺少任一侧接触时暂停调节，释放时关闭反馈；原有执行结束后的接触丢失反馈规则不变。状态记录 `hold_force_target_n`、`filtered_hold_force_n` 和 `hold_force_torque_saturated`（是否达到驱动力矩上限），实际力看 `left_force_n`、`right_force_n`。闭合阶段的 `grasp_force` 仍按原方式乘以 `0.04` 映射为力矩上限。力度参数受 `constraints.maximum_force` 和控制器上限校验，当前默认上限为 40。绑定模式的 `hold_force` 不改变 weld 强度。

#### 物理模式执行反馈

机械臂在运动和夹爪执行期间补偿自身 `qfrc_bias`；成功持物后，另按独立自由刚体的实际质量、实测质心及夹爪雅可比计算负载重力补偿 `Jᵀ(-mg)`。绑定模式持物期间启用，物理模式还要求双侧指垫接触；释放或接触条件不满足时停用。该项只补偿负载重力，不补偿负载惯性，不改变位置伺服增益、到位阈值或碰撞检查。报告帧中的 `arm_control` 记录关节误差、伺服力矩、伺服是否配置力矩限制及负载补偿力矩。附加补偿通过 `qfrc_applied` 施加，与现有偏置补偿方式一致。

- 偏移仅用于诊断，不依据偏移大小判失败，也不据此中断或修正运动。
- 只有左右指垫与目标接触均消失（两侧法向力均 ≤ 0.1 N）时，记录首次异常，完成当前运动命令后返回 `GRASP_FAILED`，说明抓取失败。
- 单侧接触丢失本身不触发上述错误；首次双侧接触丢失记录不会因随后恢复接触而清除。
- 失败命令标记 `motion_completed=true`，后续命令不执行；机械臂碰撞、超时仍立即中断。
- `execution_result.final_gripper_state` 保存结束时的位置偏移 `relative_position_error_m`、角度偏移 `relative_orientation_error_rad`、接触力及 `contact_lost`。`execution_result.grasp_error` 保存首次异常证据，无错误时为 `null`。

物理携物按实时仿真状态检查碰撞，规划时使用当前测得的物体相对位姿预测占用。掉落物体与桌面、地面的接触不作为携物碰撞，但物体仍是机械臂的动态障碍物。

### release

无需 `mode` 参数。绑定抓取先解除 weld 再张开夹爪；物理抓取直接张开夹爪释放接触。之后验证夹爪开度和释放状态。

```json
{
  "command_id": "release-001",
  "skill_name": "release",
  "parameters": {
    "target": 101,
    "strategy_params": {
      "open_width": 0.085
    }
  }
}
```

## 完整命令文件

资源由 `scene_id` 和 `scene_version` 选择；机械臂和夹爪从初始化时应用 runtime 配置后的最终场景读取，动作按 `commands` 数组顺序执行：

```json
{
  "schema_version": "2.0",
  "request_id": "ols-pick-position-001",
  "scene_id": "scene.001",
  "scene_version": 1,
  "commands": [
    {
      "command_id": "command-001",
      "skill_name": "pick",
      "parameters": {
        "target": 101,
        "planning_method": "joint",
        "grasp": {
          "mode": "physical"
        },
        "grasp_request": {
          "strategy_params": {
            "hold_force": 20.0
          }
        }
      }
    },
    {
      "command_id": "command-003",
      "skill_name": "move",
      "parameters": {
        "target": "home",
        "planning_method": "joint"
      }
    },
    {
      "command_id": "command-002",
      "skill_name": "release",
      "parameters": {
        "target": 101,
        "strategy_params": {
          "open_width": 0.085
        }
      }
    }
  ]
}
```

## 统一场景运行与报告

`assets/scene.001.json` 将夹爪作为独立对象声明，使用 `MetadataRef` 引用夹爪资产，使用 `RobotAttachment` 指定父对象、父 link、子 link 和相对安装变换。运行时从两份原始 URDF 在内存中建立安装关系，不生成也不读取组合 URDF 文件。FCL 负责机械臂和静态场景的候选、接近路径和最终位姿检查；夹爪、活动机构和携带物体使用 MuJoCo 的实时几何距离检查。MuJoCo 负责物理积分、接触力和窗口播放，并输出后端 MJCF 缓存。

完整文件见 `demo/task_commands/commands.001.json`，当前顺序为 `pick → put → pick → put → move home`。命令按数组顺序执行，`command_id` 不决定顺序。

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo\task_commands\commands.001.json `
  --start-object-library `
  --library libraries\simplatform\bin\sim_platform_c.dll `
  --viewer
```

默认输出目录为 `tests/output/unified_scene`，可用 `--output` 修改。`report.json` 保存命令结果和统计，`frames.jsonl` 保存执行帧状态。`pick` / `put` 的运动 `substeps` 为 `approach`、`contact`、`retreat`；抓取结果记录在 `grasp_result`，放置时的释放结果记录在 `release_result`。`put` 的执行帧包含位于 `contact` 和 `retreat` 之间的 `release` 阶段。

使用 `--viewer` 时，执行成功或失败后先保存报告，窗口停留在最终状态，等待手动关闭后程序退出；等待期间不推进物理仿真。

当前 `test` 场景按世界坐标 +Y 为桌面左侧、-Y 为右侧摆放：

| 对象 | 场景 ID | OLS 模型 ID | 初始位置（m） |
| --- | --- | --- | --- |
| Apple | 101 | 1 | `[0.50, 0.22, 0.64]` |
| SugarBox | 102 | 19 | `[0.65, 0.40, 0.68691731]` |
| Basket | 103 | 12 | `[0.55, -0.35, 0.598536]` |

苹果和盒子为动态物体；右侧白色篮筐使用 `models/12/basket.obj`，当前作为静态容器摆放。

## Mesh 碰撞预处理

独立 OBJ/STL 资产自动经过 `collision/mesh_assets.py`：凸模型直接使用凸包，非凸模型由 CoACD 分解成多个凸块。FCL 与 MuJoCo 加载同一组凸块；MuJoCo 仍使用原始 mesh 显示外观。无需按对象名称编写篮筐、杯子等专用碰撞逻辑，也不生成组合 URDF。

首次运行会输出 `Collision preprocessing`，可能需要数分钟。后续复用 `.cache/collision/` 缓存；源文件内容、缩放、算法版本或参数变化会生成新的缓存。缓存可以删除，之后会重新计算。原始资产不会被改写。分解失败或缓存校验失败会明确报错，不会静默退回填满空腔的整体凸包。

依赖安装：`.\.venv\Scripts\python.exe -m pip install -r collision/requirements.txt`。默认采用按模型尺寸归一化的分解阈值 `0.05`，参数集中在 `collision/mesh_assets.py` 的 `SETTINGS` 中；需要米制阈值时可设置 `real_metric=True`。更小的阈值通常需要更多凸块和更长的首次处理时间。设置环境变量 `COACD_LOG_LEVEL=info` 可查看分解阶段日志。凸分解是近似，细缝或薄壁是否保留仍取决于源网格质量与分解精度，需要验证新容器的有效开口。

已有 box/cylinder 等基础碰撞体及大部分 URDF 碰撞定义保持原样。Robotiq 左右 `spring_link` 的凹形 mesh 在运行时使用同一缓存流程拆成凸块，保留原始视觉网格、关节、惯量和碰撞局部变换；源 URDF 不改写，不生成组合 URDF。活动夹爪和活动机构链接由 MuJoCo 提供实时变换，并在 FCL 中使用同步代理几何参与规划和执行碰撞检查。其他 URDF mesh 不自动分解。独立网格可用 `Collider.collision_mesh`（相对于资产根目录的路径）指定碰撞源，外观仍取自 `AssetRef`。动态物体的配置总质量按凸块体积分配，惯量由这些凸块近似计算。


### 场景与运行配置分离

`assets/scene.001.json` 使用与 `assets/scenes/` 一致的 schema 1 组件结构，仅保存场景身份、对象布局、资产引用、装配关系、显示颜色和 RobotDriver 初始关节角。`layout_reference` 和 `assembly_notes` 已删除。

场景文件直接包含机器人、夹爪、碰撞器和物理组件。`assets/catalog.json` 只负责登记场景文件和后端资源，通用运行默认值由执行器自动补充。

资源加载器在内存中合成运行数据，并将同一份解析结果交给仿真与 FCL，不回写场景或生成组合 URDF。运行配置内容参与资源指纹计算。修改运动默认值、home 或物理覆盖参数时编辑 runtime 配置；修改位置和装配关系时编辑场景。旧 schema 2 测试夹具仍受支持。


### 对象 move 的保持姿态模式

对象 `move` 支持 `keep_end_effector_orientation: true`：取 OLS 候选的世界交互点，保持当前控制末端旋转，并使用抓取中心与控制末端之间的完整固定变换重新计算目标位置。报告保留原始服务姿态 `service_world_from_grasp`，实际候选标记 `orientation_source: current_control_pose`。此模式下 joint 请求也保留 pose 目标以供底层保持姿态规划；近解优先与碰撞安全距离不变。`pick` 仍要求服务抓取姿态。开启保持姿态不保证路径可行，规划超时不会自动关闭姿态约束或减小安全距离。


### physical 闭合阶段的结构件接触

物理抓取仅在控制器 `close()` 调用期间允许全部夹爪碰撞几何与当前抓取目标接触；试闭合与实际闭合使用同一规则。MuJoCo 接触响应和摩擦保持启用。接触记录在 `allowed_closure_contacts`，按部件对汇总最小距离和观测次数；该次数不代表独立接触事件。抓取成功仍要求原有双侧指垫力与稳定性确认。

张开、接近和未确认抓取时保持原碰撞规则。binding 成功确认且绑定仍有效后，以及 physical 成功确认并登记持物后，退回和搬运期间允许夹爪全部结构件与当前持物接触；释放清除持物登记后取消放行。physical 双侧指垫接触均消失时也取消放行，单侧接触丢失不改变原有双侧丢失判据。夹爪与其他物体、机械臂与目标、被抓物与环境的碰撞和安全距离仍检查，物理接触力仍生效。闭合抛出异常时恢复原策略。报告 `allowed_carrying_contacts` 汇总持物阶段规划检查与执行检查中放行的接触/安全间距记录，不等于全部是真实执行中发生的碰撞。


### pick 抓取后竖直抬升再回撤

`parameters.retreat_lift_height` 控制先沿世界 Z 竖直抬升的高度，单位米，有限非负数，不填默认 0.01 m；显式 0 跳过抬升。当前 commands.001.json 两次 pick 均设为 0.02 m。

抓取确认后先执行 lift，再沿原抓取接近方向的反方向回撤。回撤独立按 0.15、0.12、0.10、0.14、0.13、0.11 m 串行尝试；仅规划/IK/路径生成失败时改试下一距离，规划通过后即执行，执行失败不重试距离。所有候选使用同一个抬升后实测起点与朝向。`retreat.distance_m` 和 `retreat.attempts` 记录实际尝试距离及结果。两段均保持各自起始姿态、从执行前实测位姿生成目标并独立规划直线与碰撞验证。接近附近点不变；回撤距离与接近距离相同时，理想回撤终点为原附近点上移该高度；距离不同时按实际选中的距离生成终点，不重复叠加抬升。报告保存 lift、retreat 的实际规划目标，并分别验证到位。抬升结束后取消原支撑面的临时放行，后续运动正常检查环境间距。put 不增加抬升段。抬升中若出现抓取失败，按原执行后反馈机制报告，不继续回撤。

### 碰撞检查与试闭合性能

候选碰撞探测仅调用 `mj_kinematics` 更新几何位姿，不在探测副本中求解动力学；查询关节与已更新的实测关节完全一致时，直接只读使用实测几何。携带物体的候选位姿仍按抓取相对变换计算，所有安全距离与允许接触规则保持一致。

直接运动规划使用单次调用范围的精确状态缓存，键包含关节值、安全距离、允许碰撞策略和当前场景/抓取状态；最终插值轨迹验证前关闭缓存，执行帧保持实时检查。报告 `planning_cache_stats` 记录命中、未命中及失效次数。

保留抓取候选试闭合：静止物体仍可能存在夹爪结构干涉或无法形成双侧稳定接触。试闭合每个物理步仍执行碰撞检查，但复用该步已有的几何状态，不重复进行完整动力学求解。闭合接触阈值、确认时长和候选优先顺序未放宽。


## 活动机构与分阶段开柜门

底层技能名称为 `pull_push`（`PullPushSkill`）。抓持式推拉命令与 pick 一样使用 `parameters.grasp.mode: binding|physical`；pull 默认 binding，physical 闭合确认后仅通过接触与摩擦保持，不启用 weld，抓持丢失时停止。`grasp_request` 覆盖规则与 pick 相同。模式会传递给抓取及推拉阶段，已有抓持模式不匹配时拒绝继续，不自动转换。标准反馈为 `pull_push_result`。

所有机械臂阶段共用从机器人 URDF 读取的每关节速度与加速度基准，并使用连续轨迹时间参数化；夹爪开合独立控制。速度读取 `<joint><limit velocity>`，加速度优先读取同一 limit 的 `acceleration` 属性；缺失时按约定采用该关节速度上限的数值作为加速度基准，并记录 `velocity_numeric_fallback`。缺失速度或显式限值无效时直接报错，不使用固定数值兜底。scene.001/002 的速度比例为 0.5、加速度比例为 1.0；`joint_velocity_multipliers` 为 `[1,1,1,2,2,2]`，因此当前 UR5e 的 J1–J3 实际限值为 1.5 rad/s，J4–J6 为 3.0 rad/s（URDF 上限为 3 rad/s），加速度均为 3 rad/s²。报告 `time_parameterization.limit_source` 记录 URDF 路径、关节顺序和每关节加速度来源。

推拉在启动前完成整段 IK 路径、连续关节插值、速度/加速度时间参数化，以及平滑路径的静态碰撞和末端路径容差校验；完整路径未通过时不开始运动。执行循环动态推进已规划轨迹，只读取力反馈：超过最大力阈值或接触建立后力反馈达到零力阈值时停止并返回 suspend；执行阶段不再做几何碰撞检测。报告 `trajectory_profile=continuous_precomputed`、`collision_policy=static_planning_only` 与 `planned_duration`。

`commands.002.json` 对应 `scene.002`，命令使用抽屉目标、活动 `link`、`distance` 和 `contact_mode`。pull 默认启用 `move → grasp → pull` 编排，push 可使用 grasp 或 contact 模式；静态规划碰撞、关节限位或连续 IK 不可达时整段不执行并返回 error。

- `parameters.target`：场景对象 ID（柜子为 9）。只有一个已声明活动 link 时，demo 自动选用该 link；多个活动 link 时必须显式指定，避免选错门。
- 编排式 pull 在省略 `contact` 和 `contact_normal_local` 时，从活动 link 上唯一的把手横杆及支座几何推导交互位姿、外法向和抓取姿态。ApproachSkill 以该交互位姿为目标，沿目标位姿局部 `+Z` 的反方向搜索接近点；搜索范围默认 `3～10 cm`、步长 `5 mm`。把手几何缺失或不唯一时返回错误。
- 显式 move 的 `mechanism_pose.position/rpy` 或 `quaternion_xyzw`：活动 link 局部坐标中的米和姿态，每次调用根据实际 link 姿态换算。默认 frame=grasp，通过已加载模型的控制点—抓取点标定转换为 TCP；frame=control 表示直接指定 TCP。
- 自由换点 motion 使用 joint 规划并允许绕障；接触/撤离的 linear 路径保持直线约束。`allow_contact=true` 仅在该命令中允许指垫接触目标 link，不允许机械臂或夹爪结构撞柜体。
- grasp/release 的 target+link 定位门板，绑定抓取仍必须先通过双侧接触确认。活动关节不转换成 freejoint，释放会关闭门板 weld。
- push/pull 的 `request.manipulation.contact_mode` 为 grasp 或 push_contact。非抓持推送可用 `contact_geom` 指定接触表面；省略时接受所选 link 的真实指垫接触。直线 push_contact 可以先前进、在途中建立接触；grasp 和既有圆弧接触运动仍要求起步前已建立接触。
- `contact_normal_local` 可显式覆盖自动推导的门板局部向外法向，demo 将其转换到世界坐标；`pose_axis` 可覆盖 ApproachSkill 使用的目标位姿局部轴。圆弧轴线的 arc_center / arc_axis 默认从当前铰链读取。
- `target_angle_deg` 仍作为兼容接口保留，用于需要绝对机构角度的调用；当前 scene.002 抽屉命令使用相对 `distance`，不依赖绝对开度。
- 相对推拉用 `distance`（正数，m），默认 `frame: tool`：pull 沿起始 TCP 的 -Z、push 沿 +Z。可显式传 `direction: [x,y,z]` 和 `frame: tool|world`；显式方向就是实际运动方向，不因 push/pull 再反转。圆弧增量可用 `angle_deg`（右手定则有符号角度）、`arc_center_offset`（相对起始 TCP 的轴心偏移，m）、`arc_axis`，向量同样按 frame 解释。目标从当前实测末端位姿计算，不依赖物体开度、类型或滑轨轴向；不能混入绝对机构目标。已抓持时可省略 target/link，接触对象及模式从真实抓持状态解析，没有抓持则报错。scene.002 使用相对拉 0.12 m → release → move home → 保持夹爪张开重新接近 → 连续向前运动，真实接触后推动 0.12 m（contact）；移除了 target_position_m。成功表示请求的末端行程完成，不等同于物体完全打开或关闭。
- 柜门开度、接触力和抓取状态写入 report/frames；推拉失败保留实际物理状态，不回滚现场。普通对象命令及 OLS 流程不变。

本场景已使用已知把手/内侧接触点，不需要启动 OLS 服务。相机与灯光暂不参与控制仿真，源场景结构保留。

底层 push/pull 按末端运动几何只分直线和圆弧两类，目标机构类型不绑定末端轨迹。完整路径静态规划通过后才执行；任意规划点失败都不执行部分路径。动态执行只监测力反馈，超过最大力阈值或已建立接触后达到零力阈值时停止。推拉已执行但未完成目标时，底层、编排和顶层报告统一标记 `status: suspend`（任务中止）、`success: false`，保留实际行程、`remaining_delta`（m/rad）、`stop_reason` 和原始诊断；静态规划未开始运动时为 `status: error`；完整完成为 `status: success`。中止会停止后续命令，命令行退出码为 2（成功 0，错误 1）。maximum_force 不填默认禁用超力停止；`zero_force_threshold` 默认 0 N。


## 推、拉编排模块

统一运行器按命令选择 `task_skills/pull.py::CommandPullExecutor` 或 `task_skills/push.py::CommandPushExecutor`，不再通过一个混合类分支调度。pull 和 push 各自内置所需执行设施；旧共享基类已移除。底层仍共用 `PullPushSkill`。

- **pull**：解析交互位姿 → 求解交互位姿 IK 并静态规划完整推拉轨迹 → 规划并执行 approach；approach 失败时用 MoveSkill 直接到交互位姿 → 未抓持时闭合确认 → 执行已准备好的轨迹 → release → retreat。默认直线方向为交互位姿工具 -Z，拉动必须抓持；已有匹配抓持时跳过抓取准备。
- **push（grasp 模式）**：解析交互位姿 → 求解交互位姿 IK 并静态规划完整 push 轨迹 → 规划并执行 approach；approach 失败时用 MoveSkill 直接到交互位姿 → 可选 grasp → 执行已准备好的轨迹 → grasp 成功时执行对应 release → retreat。默认直线方向为交互位姿工具 +Z。
- **push 的既有接触推动分支**：目标点方式的旧调用仍可不抓取，执行接近、逐步搜索真实指垫接触、直线推动及原有撤离/验证流程。旧 c 点回退辅助代码仍在 push 模块中，但常规 pull 不调用它，不自动从拉切换为推。

当前相对推拉完成后不自动释放或回 home；这些是 command 中独立的 release/move 步骤。


## 抓取前的避障降级

pick 保留自己的候选抓取位姿与多距离预接近搜索；pull / 抓持式 push 保留自己的把手接近编排。它们只复用 `task_skills/move.py` 的已解析位姿移动能力，不重新查询对象或重新选择抓取点。

- pick 已选抓取位姿有效，但所有预接近点 / 直线接触路径都不可行时，使用 collision_free move 直接到该抓取位姿。后续仍执行原来的抓取、抬升和撤离。put 不自动采用这项抓取降级。
- pick、pull、抓持式 push 的 approach/contact 实际规划失败时，只有失败阶段是 planning/kinematics/path_generation 且本次尝试没有任何真实物理步，才允许降级。已经执行部分运动、执行异常、超时或位姿验证失败都不自动重试。
- 降级沿用原目标位置和姿态，以及原有安全距离、全局速度与加速度限制；路径改为关节空间软约束，开启 collision_free 和 allow_replan。MoveSkill 使用当前实测状态规划。
- 到达后单独检查实际 TCP 位姿，位置误差不超过 2 mm、姿态误差不超过 0.02 rad 才允许闭合抓取。仍通过既有闭合与真实接触确认流程。
- 把手接近过程中，原指垫接触放行收窄为：TCP 距目标不超过 1 cm 且姿态误差不超过 0.05 rad，才允许指垫与已命名把手横杆的有限接触；超过 3 mm 穿透仍判碰撞。掌部、腕部、其他环境物体保持碰撞检查。普通 pick 沿用原有的目标碰撞检查，不额外放行整件物体。
- 报告保留原失败尝试、降级原因、实际 move 请求以及 `grasp_pose_verification`。降级失败不抓取；成功后才继续 pick / pull / push，后续运动仍可能正常返回 suspend。


### push 的 grasp / contact 编排方式

`parameters.contact_mode` 可选 `grasp`（默认）或 `contact`。它与 `grasp.mode: binding|physical` 分属不同层次：前者决定是否抓住，后者仅决定抓住时是否绑定。

- `grasp`：避障接近 → 直线接触（失败可降级避障到位）→ 抓取确认 → 相对推动。支持 binding 与 physical。
- `contact`：保持现有夹爪开度 → 避障接近 → 直线接近（失败可降级避障到位）→ 连续向前推进，在途中建立真实指垫接触并继续推动。不调用 grasp，不建立绑定，不执行末尾 release。建立接触后若丢失接触则停止。

003 的 push 使用如下参数（双抽屉需要 link 明确选择上层）：

```json
{
  "target": 9,
  "link": "Nightstand0061_drawer01",
  "contact_mode": "contact",
  "distance": 0.12,
  "pose_axis": "+z"
}
```

contact 当前用于相对直线 push，要求正数 distance；pull 仍要求 grasp。contact 不能携带 grasp/grasp_request 配置，也不能在已有抓持时执行。ApproachSkill 直接以 contact 交互位姿为目标，默认沿目标位姿局部 `+Z` 的反方向搜索接近点；仍保持当前夹爪开度并确认真实接触。

直线 contact push 从首次真实接触位置开始计算有效 distance，空行程不扣减请求行程。起步没有接触时允许先前进；规划从几何预测的接触位置延长同一连续路径，实际接触达到 `request.constraints.contact_threshold`（默认 0.1 N）时记录 TCP 起点并将有效进度归零。完成量和剩余距离依据实测接触起点验证，不把预测接触位置作为完成依据。反馈 `actual_delta` / `travel_distance` 是有效行程，`precontact_distance` 单独记录空行程，`total_travel_distance` 仅用于诊断实际机械臂总位移，`distance_reference` 为 first_contact。

建立接触后，只要真实指垫接触仍存在，减速时力低于建立阈值不单独判为接触丢失；接触对消失仍按 contact_loss_timeout 停止。独立的小步搜索已取消，非允许碰撞、最大力与关节限位检查保留。未建立接触就结束前进时，有效行程为 0，返回 CONTACT_NOT_ESTABLISHED；已经产生运动时状态为 suspend。默认不自动将接触表面限定为把手横杆；显式 contact_geom 仍生效。





