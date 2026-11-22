# Agent 服务性能报告

测试数据来自本地 SQLite 运行记录：`.sim-cache/agent-service/conversations.db`。
选取最近一次完整成功的真实 MuJoCo 任务：2026-09-20 10:43:57.469 至
10:46:37.768，棒球放入盒子，10 个命令，最终 timeline 2173 帧。总墙钟耗时
为 **160.299 s**。数据来自 `turn.debug`、`presentation.requested` 和
`turns` 事件，不是估算值。

## 1. 主阶段耗时

阶段耗时由同一阶段的 `in_progress` 和 `completed` 事件时间戳相减。阶段事件
之间存在事件写入和调度间隙，因此阶段相加不一定精确等于 turn 总耗时。

| 阶段 | 耗时 (s) | 占总耗时 | 说明 |
|---|---:|---:|---|
| 检查场景 scene | 1.982 | 1.2% | 场景/Route 检查 |
| 理解指令 understand | 4.568 | 2.8% | 模型理解 |
| 分解步骤 plan | 7.495 | 4.7% | 任务 grounding/步骤规划 |
| 编译命令 compile | 0.700 | 0.4% | 命令生成 |
| 执行动作 execute | 125.142 | 78.1% | 10 个运行时命令 |
| 同步场景 publish | 14.745 | 9.2% | 2173 帧 timeline 发布 |
| 未被阶段计时覆盖的事件间隔 | 5.667 | 3.5% | 事件写入、队列调度等 |
| **总计** | **160.299** | **100%** | turn 墙钟时间 |

## 2. 执行阶段逐命令耗时

以下是每个命令的墙钟耗时。`trajectory duration` 是轨迹时间参数化后的仿真
时长，不是服务计算耗时；`planning_time` 是规划器计算耗时。

| 命令 | 技能/目标 | 墙钟 (s) | 规划器 | 路径 | 路点 | planning (s) | trajectory (s) |
|---|---|---:|---|---|---:|---:|---:|
| command-001 | locate baseball_01 | 0.507 | - | - | - | - | - |
| command-002 | move baseball_01 | 14.742 | collision_free | joint | 290 | 4.663 | 2.877 |
| command-003 | grasp baseball_01 | 7.432 | - | - | - | - | - |
| command-004 | move end_effector | 31.757 | direct | linear | 652 | 0.001 | 6.503 |
| command-005 | move home | 16.738 | direct | joint | 465 | 2.023 | 4.633 |
| command-006 | locate box_01 | 0.308 | - | - | - | - | - |
| command-007 | move box_01 | 9.960 | direct | joint | 328 | 0.321 | 3.268 |
| command-008 | release baseball_01 | 3.459 | - | - | - | - | - |
| command-009 | move end_effector | 28.110 | collision_free | joint | 238 | 1.037 | 2.356 |
| command-010 | move home | 6.439 | direct | joint | 176 | 0.086 | 1.749 |
| **合计** |  | **118.637** |  |  | **2149** | **8.131** | **21.386** |

`execute` 阶段为 125.142 s，而命令墙钟合计为 118.637 s，差值 6.505 s 是命令
事件间隔、执行器调度和失败/完成状态写入等开销。

## 3. 分类耗时

| 分类 | 已观测耗时 (s) | 统计口径 |
|---|---:|---|
| Qwen/指令理解 | 4.568 | understand 阶段 |
| 任务分解与 grounding | 7.495 | plan 阶段 |
| 场景检查 | 1.982 | scene 阶段 |
| 命令编译 | 0.700 | compile 阶段 |
| IK/规划器 | 8.131 | 8 个 move 的 `planning_time` 之和；包含规划器内部 IK/碰撞查询，当前日志未拆开 IK 与碰撞 |
| 轨迹仿真时长 | 21.386 | 8 个 move 的 `trajectory duration` 之和，属于仿真时间，不是 CPU 耗时 |
| 执行命令墙钟 | 118.637 | 所有 10 个命令的墙钟时间 |
| 执行器/仿真/最终收敛剩余 | **至少 89.120** | 命令墙钟减去已记录 planning；包含 MuJoCo stepping、执行器控制、最终收敛、未拆分的 IK/碰撞和调度 |
| timeline/场景发布 | 14.745 | publish 阶段；2173 帧 |
| 事件与调度间隔 | 5.667 | 总耗时减去六个主阶段 |

### 重要限制

当前生产日志没有为每个 move 输出独立的 `ik_time`、`collision_check_time`、
`postprocess_time`、`mujoco_step_time` 和 `final_convergence_time`。因此不能从
这次历史记录中合法地给出这些子类的精确数值。上表的“执行器/仿真/最终收敛
剩余”是合并下界，不应解读为单一模块耗时。

## 4. 优化优先级

1. **执行器/最终收敛（最高优先级）**：至少 89.120 s，占命令墙钟的主要部分。
   应增加 MuJoCo stepping、控制器等待、误差收敛循环和 timeout 分段计时，重点
   检查是否以过小的控制步长或过严的收敛阈值等待。
2. **持物路径的碰撞规划**：command-002 的规划占 4.663 s，是单次规划最高值，
   且使用 collision_free。应分别统计 IK、每次碰撞检查和 RRT 采样。
3. **timeline 发布**：14.745 s、2173 帧，约 6.78 ms/帧（按阶段墙钟平均）。
   应继续关注 JSON 编码、帧压缩/采样和网关传输；不能只看浏览器播放 FPS。
4. **模型理解与任务分解**：合计 12.063 s，占总耗时 7.5%；若需要降低端到端
   延迟，可利用 DEBUG 结果复用或缓存，但它不是当前主要瓶颈。
5. **低收益项**：locate、compile 合计约 1.515 s，不建议优先优化。

## 5. 建议的下一轮基准

在 `move` 和执行器返回结果中补充以下字段，并保持现有 `timing` 结构：

```text
timing.ik
timing.collision_check
timing.path_search
timing.postprocess
timing.mujoco_step
timing.final_convergence
timing.actuator_command
timing.execution_total
```

同时在 Qwen runner 的每个 phase 完成事件中写入 `duration_seconds`。这样下一轮
可以直接得到不重叠的分类总和，而不必用“剩余时间”归并多个模块。
