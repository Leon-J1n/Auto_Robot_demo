# MIMIC-2026-10-13 —— MIMIC-2026-10-13

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-13 |
| 落地 | 本卡提交 |
| 依赖 | OLS-2026-10-12b |

## 原因与做了什么（PLAN_STATE 备注）

维护卡（非 §2 任务卡）：实现 URDF `<mimic>`，决议记在规格 §13.19 Q69，同时补 §6.4 与 §8.4 各一条。动因：`F:\ObjectLibraryServer` 的 robot 2/10/13/14 四条资产带 `<mimic>`（4/5/6/4 条），而 `urdf.ts` 原先不读这个标签，`drivers.ts` 只写序列点名的关节，于是驱动 `robotiq_2f85_right_driver_joint` 只有右手指动。实现形态：解析层 `UrdfJoint.mimic`（`multiplier` 默认 1、`offset` 默认 0）+ `checkMimics`（源必须存在且可动、链条不成环，否则 `INVALID_URDF` 400 走 §6.8 单条诊断）；写入层 `drivers.ts:writeJoint` 成为唯一写关节口，`driveRobot` 与 `applyInitialJointPositions` 共用，写完源沿「谁模仿我」向下逐层推，派生值取裁剪之后的源值。真机取证：四个真实 URDF 用临时脚本读原文件跑通（每条 mimic 的 follower 都动）后删除；顺带证实单引号 XML 声明在 Chromium 里解析正常（`parsererror` 为空，`joint` 元素 14/13/128/22，pr2 只有 87 条是 `<robot>` 直接子节点），而 happy-dom 会拒绝该声明——没有为测试环境改生产代码，`urdf.test.ts`/`drivers.test.ts` 改用自建夹具 `MIRROR_URDF`/`GRIPPER_URDF`。测试：vitest 254→260 例 / 12 文件（`drivers.test.ts` +4、`urdf.test.ts` +2 例与 6 条拒绝用例、`renderer.test.ts` 两处夹具补 `mimic: null`）。验证：`npx tsc -p editor/tsconfig.json --noEmit` 0 错、`npx vitest run --config editor/vitest.config.ts` 260 全绿、`python -X utf8 -m unittest discover -s tests` 102 例 OK、`cmp types.ts contracts/types.ts` 无输出、`npm run build` 通过、`python -X utf8 tools/audit_parity.py` 与 `python -X utf8 tools/parity_inventory.py` 干净。遗留已清（2026-10-13）：本卡当时 `joint_sequence` 还写着 `robotiq2f85_*` 那套不存在的关节名，同事已在 `087dc0e` 把样本改成 `robotiq_2f85_right_driver_joint`，现在载入场景即两侧指根联动

## 任务卡原文

（本卡在原 `Web前端实施计划.md` §4 无独立卡体，决策与验收记录散见于当时的规格决议；原文已整体归档至 `docs/works/历史档案/`。）
