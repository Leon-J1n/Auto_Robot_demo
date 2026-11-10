# FEAT-2026-10-14-A —— `RobotDriver.now_joint_positions_rad`：让 `/updateItem` 能刷新关节位姿

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-14 |
| 落地 | 本卡提交 |
| 依赖 | M1-4,M3-2,M4-3 |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-14 新卡（第 ⑦ 张）：`RobotDriver` 增 `now_joint_positions_rad`，`/updateItem` 从此能刷新机械臂的关节位姿（决议 Q75；用户要求"先改文档、再实现、追加任务卡、本地提交"，按此顺序完成。同日用户三条改判已一并落地：合并口径、Details 面板显示、写入即退出播放 = 新决议 Q76）。口径（改判后）：两份位姿按关节合并 —— `initial_joint_positions_rad` 打底，`now_joint_positions_rad` 只覆盖它点名的那几条关节，两份都没点名的关节保持当前角度不动（新建树时即 URDF 零位）；`now` 缺席、空对象、`null`、字符串、数字或数组时完全不参与（`drivers.ts:isDictionary`）。第一版定的是"整份取代"，被用户 2026-10-14 推翻：「now角度只覆盖他提供的关节，没提到的还是initial，没点名的保持不变」。为什么仍不直接改写 `initial`：它的名字与文档都说的是"场景文件声明的停放位姿"，是静态量；挪用会让 `/getWorld` 回读与文件不一致，并消灭"删掉 `now` 即回初始位姿"这条恢复路径 —— 这条理由与合并口径不冲突。落点七处：`types.ts`+`contracts/types.ts`（坑 6，两份逐字节相同）、`drivers.ts:declaredJointPositions()`（合并的唯一单点，返回逐关节 `{name,value,source}`，先 `initial` 建序再被 `now` 就地覆盖）、`drivers.ts:applyDeclaredJointPositions()`（改名自 `applyInitialJointPositions`，告警文案带来源 `robot <id>: <initial

## 任务卡原文

### FEAT-2026-10-14 —— `RobotDriver.now_joint_positions_rad`：让 `/updateItem` 能刷新关节位姿（新卡；依赖 M1-4、M3-2、M4-3）

用户 2026-10-14 的要求原话：「在RobotDriver组件中添加now_joint_positions_rad属性，如果场景文件提供了该属性，显示时以该属性为准，否则默认按initial_joint_positions_rad显示。通过添加这个属性，我们可以在调用/updateItem时刷新机械臂（或其他物体）的关节位置。**先修改相关文档，然后再实现具体内容，并追加任务卡**。任务完成后在本地进行git提交」。顺序按此执行：文档 → 实现 → 测试 → 本卡。

**同日改判（用户对本卡第一版的三条裁决，本卡已按新口径重写）**：① 优先级不是"整份取代"而是**逐关节合并** ——「now角度只覆盖他提供的关节，没提到的还是initial，没点名的保持不变」；② Details 面板要**显示**这份位姿（第一版把它列进"明确不做"，现撤销）；③ **写入即退出播放** ——「正常情况下/updateItem和轨迹播放不会同时出现，如果播放状态时出现了updateItem，应当退出播放态，回到正常场景展示状态并刷新机械臂关节。如果updateItem后出现了轨迹播放相关的调用，场景中物体和机械臂就归轨迹管了」，这条推翻 §7.3/Q42 的 item 写分支，定为决议 **Q76**。
