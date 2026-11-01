---
name: pull-push
description: 先完整规划整段路径，再通过执行器进行直线或圆弧物理推拉；执行期间只监测力和接触反馈。
metadata:
  display_name: Pull / Push
  version: 4.0.0
  entrypoint: skill.py
  input_schema: schema.yaml
---

# Pull / Push Skill

技能正式名称为 `pull_push`，入口为 `PullPushSkill`，结果为 `pull_push_result`。

## binding / physical 双模式

输入 `grasp.mode` 可选 `binding` 或 `physical`，与 demo pick 的参数写法相同。binding 要求目标已完成接触确认并启用 weld；physical 要求目标已由夹爪接触保持且没有 weld。底层只验证并使用已有抓持，不创建绑定、不自动切换模式。模式与实际抓持状态不一致时，运动前返回错误。省略模式时读取已有抓持状态；非抓持 `push_contact` 属于物理接触，不能指定 binding。

demo 的 `pull`/`push` 命令在交互位姿先调用 `PullPushSkill.plan()`，再负责 approach、MoveSkill 降级和 grasp；随后调用 `execute_prepared()` 执行同一份路径，不重新规划。默认 binding。`parameters.grasp.mode` 传给 grasp 和 pull_push；`grasp_request` 可按 pick 的规则覆盖抓取配置。physical 抓持丢失或接触力不足超过 `contact_loss_timeout` 时返回 CONTACT_LOST，不回退到绑定。成功及中途停止结果记录 `grasp_mode`、`bound`。

```json
{"skill_name":"pull","parameters":{"target":9,"target_angle_deg":90,"best_effort":true,"grasp":{"mode":"physical"}}}
```

只有两类末端运动策略：`linear_pull_push` 和 `circular_pull_push`。策略由末端运动几何决定，与目标是铰链、滑轨还是自由物体无关。自由物体不再单独作为第三类策略；目标类型只用于读取接触、碰撞和最终状态证据。调用方可用 `motion.path_type` 显式指定末端路径；`manipulation.trajectory` 是兼容旧请求的别名，二者冲突时拒绝请求。

## 两类运动

| 类型 | strategy_params 必要参数 | 运动定义 |
| --- | --- | --- |
| linear | direction、distance | 世界坐标方向向量（自动归一化），正数距离，单位 m；保持起始工具姿态。目标为铰链机构时也允许使用直线末端运动。 |
| circular | arc_center、arc_axis、arc_angle | 世界坐标轴线上一点（m）、轴方向（自动归一化）、右手定则有符号角度（rad）；位置和姿态一起绕轴线旋转 |

圆弧不是仅提供轴方向：还必须确定轴线经过的位置。只有在调用方确实要求末端沿圆弧运动时，圆弧参数才需要与机构轴线一致；直线末端运动不受目标机构运动方式限制。demo 从当前模型读取柜门轴线，绝对目标角度减去实测门角后转换成底层增量。

`constraints.maximum_force` 是可选夹爪接触力反馈阈值，单位 N。不填或 null 不按超力阈值停止；填正数才启用。`constraints.zero_force_threshold` 默认 0 N，接触建立后力反馈达到该值视为接触/抓取丢失。当前测量量为目标接触面上各指垫法向接触力之和，包括夹持预紧力，不是腕部六维传感器读数，也不是夹爪 hold_force 指令。

## 接触准备由调用方完成

`target.object_id` 指实际活动 body 名称，`target.type` 为 mechanism（铰链/滑轨）或 object（自由物体）。技能不自动接近、闭合、松手、换点或回撤。

默认 contact_mode=auto，根据实时抓持状态选择 grasp 或 push_contact。拉拽必须已经抓住物体；非抓持接触只能推。直接执行已准备轨迹时要求起始状态处于交互位姿并满足真实指垫接触；`plan()` 可在 approach/grasp 尚未执行时用交互位姿和允许的目标接触对完成静态规划。可选 contact_geom 限定接触面，否则从当前接触推断。接触力建立阈值 contact_threshold 默认 1 N，与可选的停止力阈值不同。

可选 contact_normal 用于开始时的法向/推动方向校验，不是必须参数。所有输入向量均使用 world 坐标。原有 contact_point（body 局部坐标）仍可用于校验实际接触位置。

## 先完整规划，再执行

任务编排使用以下两个阶段接口：

```python
planned = skill.plan(
    request,
    start_joints=interaction_ik,
    start_pose=interaction_pose,
    allow_unestablished_contact=True,
)
skill.execute_prepared(request, planned["prepared"])
```

`plan()` 只在 MuJoCo 工作副本中求解完整路径，不推进真实物理时间；`execute_prepared()` 只接受同一份路径，并校验当前关节状态和目标机构状态与规划起点一致。起点不一致时直接返回错误，不重新规划。

1. 复制完整 MuJoCo 状态进行整段路径采样、连续 IK、关节限位及插值碰撞检查，真实场景保持原状态。
2. 只有完整路径全部通过验证才开始执行。任意采样点连续 IK 不可达、关节越界、静态碰撞或轨迹验证失败，整次请求直接返回规划错误，不执行安全前缀，也不会把直线/圆弧改成绕障路径。
3. 执行只写机械臂 actuator ctrl，通过 mj_step 推进物理。实际目标物体运动由物理接触或既有抓持约束产生，不写真实目标物体 qpos。
4. 执行中只实时检测力反馈：超过 `maximum_force` 视为疑似碰撞/不安全，已建立接触或抓取后力反馈达到 `zero_force_threshold` 视为接触/抓取丢失。异常时立即停止并返回 `suspend`；停止后保持当前实际状态，不瞬移、不回滚、不自动回撤。执行阶段不再进行几何碰撞检测。
5. 完成后验证实测目标位移/角度。未达到请求行程返回失败和差距；不能把仅完成规划截短段当成任务成功。

路径采样分辨率默认 5 mm / 0.02 rad。路径经连续插值与整段时间参数化执行，中间采样点连续通过，使用 URDF 基准和全局速度/加速度比例。规划阶段不产生可执行的部分路径。


默认距离容差 0.005 m，角度容差 0.02 rad。timeout 默认 30 s，执行的仿真时间和墙钟时间都受限。较长行程应显式配置更长 timeout。maximum_travel / maximum_angle 默认 null，可选配置行程上限；超过上限属于规划失败，不执行部分路径。接触建立后力反馈达到零力阈值都会停止执行，`stop_on_contact_loss` 仅作为兼容字段保留。

## 反馈

成功和运动停止均返回 pull_push_result，包括：

- requested_delta、actual_delta：请求和实测有符号行程。
- remaining_delta：两者之差的绝对值；unit 为 m 或 rad。
- stop_reason：TARGET_REACHED、CONTACT_LOST、FORCE_LIMIT、ACTUATOR_TRACKING_FAILED、TARGET_NOT_REACHED 或 PUSH_PULL_TIMEOUT 等；静态规划失败时使用对应的规划错误码。
- motion_executed、target_reached、peak_force、object_final_state。
- evidence 中包含规划耗时、路径点数和 `collision_policy: static_planning_only`。

结果状态为 `status: success | error | suspend`：完整完成为 success；推拉执行已开始但未完成请求为 suspend（任务中止），包括部分运动、接触丢失、力超限和执行超时；参数、前置条件或规划问题导致没有开始推拉为 error。只有 success 状态的 `success` 布尔字段为 true。suspend 与 error 都保留 `error` 诊断对象供兼容调用方读取原因，状态分类以 `status` 为准，`error.details.result` 同样保存进度。请求格式或控制器准备状态无效时可能没有运动结果。

demo 不再把 best_effort 的部分完成报告为成功或 completed_partial。目标门角验证未通过时也返回 suspend；顶层报告和控制台均保留任务中止状态，停止后续命令。命令行退出码为 success=0、error=1、suspend=2。suspend 只表示此次执行已经中止，不代表自动续跑。

`actual_delta` 是实测 TCP 行程/转角（不是门角）；机构实际状态另见 `object_final_state` 和 demo 的门角验证。规划失败不会执行任何路径。

```json
{
  "target": {"type": "mechanism", "object_id": "door_body_name"},
  "manipulation": {"operation": "pull", "trajectory": "circular"},
  "strategy_params": {
    "arc_center": [0.7, 0.3, 0.8],
    "arc_axis": [0, 0, 1],
    "arc_angle": 1.5707963268
  },
  "constraints": {"timeout": 120, "maximum_force": 30}
}
```

示例中的轴线和 body 名称必须替换为场景实际值；调用前先抓住门。直线请求将 trajectory 改为 linear，并使用 `{"direction": [1, 0, 0], "distance": 0.1}`。完整柜门编排见 demo/skill_command/commands.002.json。
