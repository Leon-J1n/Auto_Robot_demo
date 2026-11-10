# M7-4 —— 文档

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-10 |
| 落地 | 本卡提交 |
| 依赖 | M7-3 |

## 原因与做了什么（PLAN_STATE 备注）

DoD 达成：README 重写 Start、新增"调用方接口用法（含 curl 样例）"、删除作废段落，`docs/api-inventory.json` 由 `python tools/parity_inventory.py` 重生成。① README 四段重写（`Start`/`Caller routes (§5.9)`/`Models and cameras`/`Verification`）：开头钉死 "the browser is the only scene authority"；`Start` 给 `./start-dev.ps1`（默认 8080/5173/8000，可 `-GatewayPort 8099 -FrontendPort 5188 -LibraryPort 8010`）与三终端两条路径，并点明 vite 代理必须逐条登记 §5.9 根路径否则 dev 回 SPA shell（坑 2）、`GET /api/runtimeConfig` 下发 `constant_config.json` 加 `features.json`、"Nothing is uploaded"；删掉的作废段落 = `Load Asset` 上传、`data/uploads/`、`/api/bootstrap`、"PNG endpoint 需要浏览器"。② 八条 curl 样例逐条按实测校准（服务同上，8099/8010/5188）：`/getWorld`、`/updateWorld --data-binary @docs/scene_example.json`、`/addObject` 与 `/updateItem?id=6` 改内联 JSON（原样例引用不存在的文件），`/updateItem` 注明 body id 与 `?id=` 不一致 → 400 `BODY_ID_MISMATCH`（`local_gateway.py:449` 与 `world.ts:382` 双侧都校验）；`/removeObject` 写 POST，并把两种故障并排写清 —— 无浏览器 `POST` → 503 `application/json`，方法用错 `DELETE` → 405 `text/plain`（路由只注册 POST，Q64）；随后一段讲 `expected_version` 与 409、`Content-Type` 严格门与 413、§5.12 摘要字段、`updateTraj` 不 bump `scene_version`（Q33）、深度是 `float32le` 顶行先行且未命中为 `NaN`、镜后物体 200 加 `visible:false, box:null`、无鉴权但必须留 loopback。③ §10.4/§8 遗留清理（决议 Q65，规格 §13.18）：`git rm editor/src/legacy-types.ts`（旧结构 shim 的最后一处，`logic.ts` 改从 `../../contracts/types` 取类型）、契约新增 `GatewayFrame` 判别联合让 `main.ts:handleGatewayFrame()` 只做一次边界解析、`world.test.ts` 去掉可避免的双跳；全仓 `any`/`@ts-ignore` 0，`as unknown as` 只剩 `editor/src/renderer.test.ts:406`（node 环境无 jsdom 的 canvas 替身，理由已登记为显式例外）。两条 grep 为空：旧结构名在 `editor/ contracts/ types.ts local_gateway.py tools docs/feature-inventory.csv` 零命中、README 里 `api/bootstrap\

## 任务卡原文

#### M7-4 文档
- **规格**：§10.5
- **涉及**：`README.md`、`docs/api-inventory.json`、`docs/feature-inventory.csv`、`docs/parity-probes.json`、本文件 §9
- **改动要点**：README 重写 Start / 新增"调用方接口用法（含 curl 样例）" / 删除 "Load Asset 上传" 与"PNG endpoint 需要浏览器"等已作废段落；`api-inventory.json` 用 `python tools/parity_inventory.py` 重生成；**每完成一张任务卡就更新本文件 §9 PLAN_STATE**。
- **DoD**：新会话按 §0 冷启动协议操作时，README + 规格 v2.1 + 本计划三者不矛盾。
