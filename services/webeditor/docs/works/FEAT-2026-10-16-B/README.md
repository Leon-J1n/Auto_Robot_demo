# FEAT-2026-10-16-B —— 样例轨迹生成器 `examples/make_scene3_traj.py`：scene3 九段 v2 clip

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-16 |
| 落地 | 本卡提交 |
| 依赖 | M4-*,M4-4 |
| 决议号 | Q41、Q69、Q83（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-16b 新卡（第 ⑮ 张）：样例轨迹生成器 `examples/make_scene3_traj.py` + 九段 v2 clip `examples/scene3_9_segments.traj`。用户原话：「用 v2 版本生成一段使用样例场景 3 的轨迹，让机械臂六个轴各自随便转动，然后让夹爪闭合，然后转动 shoulder_pan_joint 一个角度，然后张开夹爪，总共 9 个小段。在第 7 个段结束时将苹果挂载到夹爪上，并在第 9 段开始时取消挂载」。产物：23912 B / v2 / fps 60 / `total_frames 840`（14.0 s）/ 3 条 Index / 2 条 Attach，`index_offset 32 · attach_offset 360 · data_offset 392`；id 与关节名写成脚本常量，不按路径引用 OLS 的场景文件。九段：`segment_start(i)=30+(i-1)×90`，1-6 各转一个关节（30-89…480-539）、7 闭合（570-629）、8 `shoulder_pan→2.20` 带苹果（660-719）、9 张开（750-809），`ATTACH_FRAME 630`（第 7 段结束那一帧）、`DETACH_FRAME 750`（第 9 段开始）。两处结构性取舍：夹爪开合在 arm 的第 7 分量（`drivers.ts:149` 只记录不驱动，O1）与物体 3 的 slot 0（真驱动）各写一段且数值必须一致；苹果因 §8.3"位姿相对当前父级"+ Q41"Attach 只重挂父"而必须自带两段常量（用 `ramp` 会演成苹果自己滑进夹爪）。验证：离线用仓库真解码器（`decodeTraj/poseAtFrame/stepsFrom`）读回 —— 13/13 段零拷贝窗口、9/2/2 段、帧 620 与 800 苹果读数一致；真机一次性栈 `/updateTraj` 回执 `{object_count:3, attach_count:2, ignored_objects:[], warnings:[]}` + 时间轴 9 个 step 刻度与 2 个 attach 刻度、`pageerror` 空。不做：§8 禁令文件一字不改、不给 v2 加字段、v3 多变体按用户口径继续搁置（草案不提交）。

## 任务卡原文

### FEAT-2026-10-16b —— 样例轨迹生成器 `examples/make_scene3_traj.py`：scene3 九段 v2 clip（新卡；依赖 M4-*、Q41、Q69、Q83）

- **起因（用户原话）**：「现在我希望你用 v2 版本生成一段使用样例场景 3 的轨迹，让机械臂六个轴各自随便转动，然后让夹爪闭合，然后转动 shoulder_pan_joint 一个角度，然后张开夹爪，总共 9 个小段。在第 7 个段结束时将苹果挂载到夹爪上，并在第 9 段开始时取消挂载。我将用这段轨迹验证轨迹播放功能是否正常。」用户要的是**一段能拿去验播放的产物**，不是新接口，因此这两轮零改 §8 禁令文件。
- **交付物**：`examples/make_scene3_traj.py`（243 行，纯 stdlib `struct`/`argparse`，无服务器也能跑）+ 它写出的 `examples/scene3_9_segments.traj`（v2、23912 B、fps 60、`total_frames 840` = 14.0 s、3 条 Index、2 条 Attach，`index_offset 32 / attach_offset 360 / data_offset 392`）。id、关节名、苹果 home 全部写成脚本里的常量（`ARM_ID 2 / GRIPPER_ID 3 / APPLE_ID 7`），**不按路径引用** `F:\ObjectLibraryServer\data\scenes\3`，所以这份文件与真实库解耦。
- **九段的帧表**（`segment_start(i) = 30 + (i-1)×90`，动 60 帧停 30 帧）：1 `shoulder_pan → 0.90`（30-89）、2 `shoulder_lift → -1.60`（120-179）、3 `elbow → 2.40`（210-269）、4 `wrist_1 → -0.80`（300-359）、5 `wrist_2 → 1.10`（390-449）、6 `wrist_3 → 1.60`（480-539）、7 夹爪闭合（570-629）、8 `shoulder_pan → 2.20` 并带着苹果转（660-719）、9 夹爪张开（750-809）。**挂载在第 7 段结束的那一帧**：`ATTACH_FRAME = 630`、`DETACH_FRAME = 750`，正好对上用户给的两句时机。
- **为什么开合要写两处、苹果要占一整条 Index**（本卡的实质内容）：① §8.3 的 7 分量里第 7 个（夹爪值）在机械臂物体上**只记录不驱动**（`drivers.ts:149`，世界文件里没有"夹爪关节"字段可读，见 O1），真正让手指动的是物体 3 的 `joint_sequence = ["robotiq_2f85_right_driver_joint"]`，所以 step 7/9 各写**两段**（arm 的第 7 分量 + gripper 的 slot 0），两边数值必须一致；② Attach 只重挂父、位姿恒为"相对当前父级"（**Q41**），苹果若没有自己的采样就会把 `0.38/-0.22/0.64` 的桌面偏移带进夹爪坐标系、看着像飞走，所以 object 7 用两段**常量**（630-749 停在夹持点、750-809 回到 home）—— 用 `ramp` 会变成"苹果自己滑进夹爪"，那是穿模不是抓取。
- **验证（离线，用仓库真解码器）**：`examples/scene3_9_segments.traj` 交给 `editor/src/traj.ts` 的 `decodeTraj/poseAtFrame/stepsFrom` 读回（探针在仓库外 `scene5probe\traj3\decode_check.ts`）：**13/13 段的 `poses.buffer` 就是输入 buffer**（零拷贝窗口没被复制破坏）、arm 9 段 / gripper 2 段 / apple 2 段、帧 629→749 的苹果位姿从夹持点切回 home 且 `frame 620` 与 `frame 800` 读数一致。
- **验证（真机，一次性栈 gateway 8099 + vite 5188 + OLS 8000）**：`/updateTraj` 回执 `{fps:60, total_frames:840, object_count:3, attach_count:2, ignored_objects:[], warnings:[]}`，时间轴出现 **9 个 step 刻度 + 2 个 attach 刻度**（`Attach #7 to 3 · frame 630` / `Attach #7 to scene root · frame 750`），逐帧 seek 无 `pageerror`。
- **明确不做**：不改 §8 禁令文件（`docs/轨迹文件布局描述.md` 一字未动，v2 布局照抄）；不给格式加新字段（九段全部用现存的 Index/Attach 表达）；v3 多变体仍按用户口径搁置，草案文件不提交。
