# M7-2 —— 前端测试

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-10 |
| 落地 | 本卡提交 |
| 依赖 | M1-*,M4-*,M5-* |

## 原因与做了什么（PLAN_STATE 备注）

按审计执行，不按卡片原文抄写：卡片点的"投影 / 裁剪 / attach 重放 / 空隙帧 / `package://` 重写"五项在 M1-M5 已就地断言（`logic.test.ts` 6 条 `projectBox`、`capture.test.ts` 的 clipped rectangle、`timeline.test.ts`+`drivers.test.ts` 的 attach 全量重放与 4 条 attach、`traj.test.ts`+`drivers.test.ts` 的 §8.5 空隙帧、`urdf.test.ts`+`library.test.ts` 4 条 `package://`），夹具早已统一读 `tests/fixtures/`（6 个文件共用 `scene_example.json`/`robot_grasp.traj`），"删除被删模块的用例"随 M6-1/M6-2 完成 ⇒ 本卡只补真实缺口。① 新建 `editor/src/error_codes.test.ts`（4 例）把 §5.13 的反向对账钉住：正则扫 `editor/src/*.ts`（排除 `*.test.ts`）的 `new WorldError\

## 任务卡原文

#### M7-2 前端测试
- **规格**：§10.4、§10.6
- **涉及**：`editor/src/{world,library,urdf,traj,timeline,logic,renderer}.test.ts`
- **改动要点**：删除被删模块的用例；新增投影/裁剪/attach 重放/空隙帧/`package://` 重写等纯函数单测；夹具统一用 `tests/fixtures/`。
- **DoD**：`npm test` 全绿。
- **已完成（2026-10-10）**：**先纠卡片口径**：列的"新增投影/裁剪/attach 重放/空隙帧/`package://` 重写"五项在 M1-M5 落地时就已各自就地断言（`logic.test.ts` 6 条 `projectBox` 含"全在镜后/角落出画"两支、`capture.test.ts:"passes a clipped rectangle through untouched"`、`timeline.test.ts` 的 attach 全量重放 + `drivers.test.ts` 4 条 attach、`traj.test.ts` 的"holds the previous segment's last frame across a gap" + `drivers.test.ts` §8.5、`urdf.test.ts`+`library.test.ts` 共 4 条 `package://`），夹具也早已统一读 `tests/fixtures/scene_example.json` 与 `robot_grasp.traj`（6 个测试文件共用）；"删除被删模块的用例"在 M6-1/M6-2 做完（`tsc` 0 错即证据）。所以本卡按**审计→补真实缺口**执行，而不是复制已有断言。审计用机械办法做了一遍"浏览器能抛的码 vs §5.13 表"（M7-1 只钉了"表 vs 文档"这一半），得到新建 `editor/src/error_codes.test.ts`（4 例，正则扫 `editor/src/*.ts` 的 `new WorldError|LibraryError|TrajError("CODE"` 与 `local_gateway.py:BROWSER_ERROR_STATUS` 对账，三条名单全按相等断言：抛出的码 ⊆ 表 ∪ `RENDER_ONLY`、`RENDER_ONLY` 里的码必须仍在被抛、表里没被抛又不在 `RESERVED_BY_SPEC` 的算漂移），**决议 Q60**。同时补三处"只断到类/只断到 message"的弱断言：`urdf.test.ts` 的 18 条畸形文档用例改断 `code:"INVALID_URDF"+status:400`、`library.test.ts` 的 422 用例改断 `MODEL_METADATA_INVALID`（新增同步 helper `expectLibraryFailure()`）、`renderer.test.ts` 新增一条把 `INVALID_URDF` 的**收口**钉成真行为（预置 `robots` 缓存为 rejected ⇒ 只出 1 条诊断、物体节点仍挂上但没有内容、换 signature 重跑不再报第二次；node 环境无 DOM，故不能真跑 `parseUrdf`，用例注释里写明了这一点与它与 `urdf.test.ts` 的分工）。**审计顺手修掉一个真实缺陷（Q59）**：`world.ts:replaceObjects()` 的 `rejected_objects[].index` 混用两套下标 —— 条目级拒绝用提交数组下标，层级拒绝（重复 id / 未知父 / 环下游）却用"已过滤掉畸形条目的节点列表"下标，真机 `[kept, duplicate, null, orphan, descendant]` 因此把 `PARENT_NOT_FOUND:99`/`PARENT_REJECTED:3` 报成 `2,3` 而正确答案是 `3,4`；`index` 是无 id 条目唯一的定位手段（`contracts/types.ts:185` 就這麼写的），改法是 `replaceObjects()` 建 `positions: Map<WorldObjectNode, number>` 传给 `buildHierarchy(nodes, positions)`。新用例 `world.test.ts:"names the drop path of every rejected object, not only the first one"` 一次锁死四条拒绝路径与其顺序。验证：vitest **228 → 234 例 / 11 → 12 文件**全绿、`npx tsc -p editor/tsconfig.json --noEmit` 0 错、`npm run build` 通过、python 8 模块 **109 例 OK**（`world.ts` 不跨语言，但 `rejected_objects` 要过网关所以整套重跑）。**真机**（vite 5173 + gateway 8080 + OLS stub 8000，页面内 `fetch`）：`POST /updateWorld` 那份五条目文档 → `200 {object_count:1, rejected_objects:[{index:2,null,"OBJECT_NOT_AN_OBJECT"},{index:1,1,"DUPLICATE_OBJECT_ID"},{index:3,3,"PARENT_NOT_FOUND:99"},{index:4,4,"PARENT_REJECTED:3"}], scene_version:2}`，`GET /getWorld` → `X-World-Source: live` 且只剩 `[1]`，控制台 0 error/0 warning（只有 vite 的两条 connected 调试）。文档：规格 §3.6 补 `index` 口径、§5.13 补"表的另一头也要钉住"段、新增 **§13.16（Q59/Q60）**；§5 新增**坑 25**。
