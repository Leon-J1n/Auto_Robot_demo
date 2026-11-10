# OLS-2026-10-12b —— OLS-2026-10-12b

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-12 |
| 落地 | 本卡提交 |
| 依赖 | OLS-2026-10-12,M7-4 |

## 原因与做了什么（PLAN_STATE 备注）

维护卡（非 §2 任务卡）：三件事收束，决议记在规格 §13.19 Q68 并关闭 O2，`/listCategory` 是最终决议。① 文档里所有"曾经笔误"的叙述删干净：规格 §6.11/§13.2 O7/Q20/Q54/Q67 与本文 §1.4、§5 坑 8、OLS-2026-10-09、M6-2、M7-4、OLS-2026-10-12 各行只留正面规范（路由名 `listCategory`），不再解释历史上少写的那个 `e`；顺手删掉 stub 时代残留的 `tests/__pycache__/test_ols_stub*.pyc` 与 `tests/fixtures/__pycache__/ols_stub*.pyc`。② 退役 `/api/library` 代理形式：`RuntimeConfig`（`types.ts` 与 `contracts/types.ts` 同步删 `library_via_proxy`，两份仍逐字节相同）、`config/constant_config.json`、`local_gateway.py:DEFAULT_CONSTANT_CONFIG` 三处删键；`library.ts:olsUrl()` 删代理分支与私有 `origin()`（连带那份只为代理存在的白名单正则，SSRF 面归零），`library.test.ts` 的代理用例换成"七条路由的直链等于 `library_server_url`"，`browser_smoke.cjs` 去掉 `library_via_proxy === false` 断言，README 的 runtimeConfig 段随之改写；文档侧 §6.9/§6.11/§9.2/O2/Q51/Q67 与本文 §1.4/坑 8/M1-2/M6-2/M7-4/OLS-2026-10-12 全部同步。没有兼容期：不再识别 `/api/library/robot?id=` 这类前缀形式。③ smoke 按实际安装的库重标定（上一卡遗留 ①）：`SCENE_FIXTURE` 由 `tests/fixtures/scene_example.json` 换成 `docs/ur5e_table_empty.json`（OLS scene 1 的同一份文档）；§12.1 状态栏 `#1 ur5e_table_empty · v3 · 7 objects`、树 `ids [0..6]`、`depth [0,0,0,1,2,0,0]`（夹爪挂机械臂、腕部相机挂夹爪）；§4.6 `#panel-count` 10→48（5 场景 + 29 模型 + 14 机械臂），点 scene 2 得 8 物体、点 scene 1 回 7 物体；`/file` 断言改成真实请求（`type=model&id=1&path=apple.obj` 与 `apple.mtl`、`type=robot&id=1&path=ur5e_arm.urdf`、`type=model&id=29&path=table.urdf`），本卡实测 108 条 `/file` 全 200；`clipWorld()` 的机械臂改 `$LIBRARY_SERVER/1` + `type:"robot"`、`joint_sequence` 用真实 ur5e 关节名（`shoulder_pan_joint`、`shoulder_lift_joint`、`elbow_joint`、`wrist_1_joint`、`wrist_2_joint`、`wrist_3_joint`；stub 的 `joint1..6` 在真实 URDF 里根本不存在），apple = model 1、box = basket(model 12)、probe = apple(model 1)；§12.4 的厘米数字重标：340 帧苹果中心 `[-0.701,-0.038,0.200]` 且离 0 帧超过 1 m（"被机械臂驮着"的判据），530 帧 `[0.600,0.300,0.088]` 且离篮筐中心 < 0.3 m；§12.5 实测深度 2.966 m 对 aabb 中心 3.056 m ⇒ 2.95% < 5%、NaN 306 065；§12.6 三态与两条 404 形状不变。CORS 是本卡新查出来的一条硬前置：OLS 原版响应没有 `Access-Control-Allow-Origin`，真机取证页面内 `fetch` 到 8000 直接 `TypeError: Failed to fetch`，补头之后同一条请求 200 —— 于是 OLS 侧补头、O2 关闭、smoke 开跑前把这条头当硬断言。验证：`node tools/browser_smoke.cjs` 8 项全绿（chromium 走 `playwright-core@1.62` + 机器上的 `chromium-1234`，`PLAYWRIGHT_MODULE` 指临时安装目录）、`npx tsc -p editor/tsconfig.json --noEmit` 0 错、`npx vitest run --config editor/vitest.config.ts` 254 例 / 12 文件、`python -X utf8 -m unittest discover -s tests` 102 例 OK、`python -X utf8 tools/audit_parity.py` 与 `python -X utf8 tools/parity_inventory.py` 重生成两份清单且退出码 0、`cmp types.ts contracts/types.ts` 无输出。

## 任务卡原文

（本卡在原 `Web前端实施计划.md` §4 无独立卡体，决策与验收记录散见于当时的规格决议；原文已整体归档至 `docs/works/历史档案/`。）
