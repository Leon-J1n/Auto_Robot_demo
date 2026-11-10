# FIX-2026-10-14-E —— `RobotAttachment` 改按 `parent_link` 局部系挂载

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-14 |
| 落地 | 本卡提交 |
| 依赖 | M1-*,M4-* |

## 原因与做了什么（PLAN_STATE 备注）

FIX-2026-10-14 维护卡（第 ⑤ 张）：`RobotAttachment` 改按 `parent_link` 局部系解释并真的挂到 link 节点（决议 Q73，推翻 Q66 的"覆盖 `Transform`、父级保持机械臂物体"口径；`docs/场景文件描述.md` §4.8 整节改写）。动因是用户 2026-10-14 的判定："transform 数据表示的是该机械臂相对于指定 link 的平移和旋转，而现在的解析结果直接把指定 link 当成了机械臂的根坐标"。触发前提是资产侧完成了重导出：scene 1 物体 3 的挂载数值从机械臂物体自身坐标系的 `[0.2635845, 0.134, 0.6904584]`+`[0.6830127,-0.6830127,0.1830128,-0.1830128]` 变成 link 局域的 `[2.68e-09, 0.1, 9.72e-09]`（即 `[0,0.1,0]`）+ 绕 X 轴 −90°，两版在停放位姿下算出同一个物理点（互为校验），只有新版与 `initial_joint_positions_rad` 无关、臂一动仍对。四处落点（缺一不可）：`world.ts:applyAttachment`（改名自 `applyAttachmentPose`）记 `attachmentMount={objectId,link}`，`mountLink()` 按 `reference_frame` 判 `parent_link`/`parent_object`/未知值三条分支，三条退回路径不写挂载；`renderer.ts:worldParentOf()` 是唯一的父节点解析口，树未到或 link 不存在都退回父物体节点（后者按 `link:<id>:<link>` 只报一次）；`renderer.ts:mountOnLinks()` 在 `loadModel()` 里必须排在 `traverse(child=>child.userData.objectId=…)` 之后（否则夹爪被盖上机械臂 id）、在 `populate()` 里必须排在 `robotTrees.delete()` 之后（否则子物体随旧树被 dispose）；`drivers.ts:restoreParent()` 改走新增的 `view.worldParent()`，播放结束不再撤销挂载。测试：`renderer.test.ts` 新增一组 6 例（树到达后挂到 `wrist_3_link` 且转关节带着走 / 树已在场上时同步挂 / link 缺失报文案并退回 / 不被盖上 id / 重建树前先摘孩子（用 `geometry` 的 `dispose` 事件计数）/ `parent_object` 挂父物体节点）—— 该文件跑在 node 环境，`parseUrdf` 的 `DOMParser` 不可用，夹具照既有做法手工注入 `UrdfRobot` 只让纯 THREE 的 `buildUrdfTree` 真跑；`world.test.ts` 夹具换真实数值并新增 2 例 + 退回用例补断 `attachmentMount` 为 `undefined` + 真实场景用例改 8 物体；`drivers.test.ts` harness 补同规则 `worldParent` 并新增 1 例；`timeline.test.ts` 的 view 桩同步补。vitest 262 → 271 例 / 12 文件，`tsc --noEmit` 0 错。真机：载入 `scene?id=1` 后夹爪 AABB `x 0.231–0.353 / y 0.060–0.208 / z 1.046–1.252`、腕部相机中心 ≈ `(0.29, 0.134, 1.235)`，截图确认 2F-85 长在腕上；取证第一版误判已写成坑 33 —— swiftshader 下 20 个 OBJ（≈12 MB）没加载完时 `worldAabb(2)` 只量到子树里的夹爪与相机，三个 AABB 完全相同，判据是轮询到机械臂 z 跨度 > 0.4 m 再读数。配套数据与冒烟：`docs/ur5e_table_empty.json` 整体替换为 scene 1 真实内容（8 物体、数组顺序 `0、7、1、2、3、4、5、6`、环境光 0.5、新增 `TopDirectionalLight`、`MeshRenderer` 空字典、link 帧挂载值）—— 该文件在 §8 禁令里属"用户维护的输入件"，本次是按用户"以真实数据为准"的口径从 OLS `GET /scene?id=1` 回写，提交说明已点名；`tools/browser_smoke.cjs` 随之改 `8 objects`/树行与 depths/scene ids/`#panel-count` 48→53，并新增 `parentOf(3)===2 && parentOf(4)===3`。同批更正：FIX-A 卡与本表 FIX-A 行的"交接项：产出带 `vt` 的网格与真实贴图"改判为纯色路线：删 `map_Kd` 与占位 PNG（坑 30 补更正段，记录 `robots/1、2、4、9、13、14` 现 0 个 `map_Kd`）。验证：`tsc --noEmit` 0 错、vitest 271 例 / 12 文件全绿、`npm run build` 通过（`index-Bz6osCf0.js` 825.43 kB，chunk>650 kB warn 为既有）、python 测试套 102 例 OK、`audit_parity.py` 52 PASS / gaps []、`parity_inventory.py` 退出码 0、`cmp types.ts contracts/types.ts` 无输出、`browser_smoke.cjs` 8 项全绿（§12.1 状态栏 `#1 ur5e_table_empty · v3 · 8 objects`、世界 `[0..7]`；§4.6 `appleScene` 9 物体；§12.5 深度 2.76%；§12.6 clipped `x_max=640` 贴右）。

## 任务卡原文

### FIX-2026-10-14 —— `RobotAttachment` 改按 `parent_link` 局部系挂载（维护卡；依赖 M1-*、M4-*）

用户 2026-10-14 的判定原话：「RobotAttachment 组件定义了另一个机械臂相对于父机械臂的位置关系，其中的 transform 数据表示的是该机械臂相对于指定 link 的平移和旋转，而现在的解析结果直接把指定 link 当成了机械臂的根坐标」。同时资产侧完成了重导出（纯色路线，见 FIX-A 更正与**坑 30**），scene 1 的数据形状变了。
