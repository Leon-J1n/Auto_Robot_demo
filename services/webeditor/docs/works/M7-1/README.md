# M7-1 —— Python 测试重写

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-10 |
| 落地 | 本卡提交 |
| 依赖 | M2-2,M3-1 |

## 原因与做了什么（PLAN_STATE 备注）

新建 `tests/test_error_mapping.py`（8 例）把 §5.13 的码表变成可执行契约：`assertEqual(BROWSER_ERROR_STATUS, SPEC_ERROR_STATUS)` 钉住 25 个浏览器可上报码，subTest 对每码真跑一次 `/updateItem` ↔ `/events` 往返并断 status/`code`/原始 message/`error.request_id` 四项；三条兜底（未知码 → 502 保留原话、缺 `code` → `UPSTREAM_RENDER_FAILED`、`error` 非对象 → 502 不抛）+ "`ok` 是唯一成功标志"（`ok:false` 带的 `payload` 不转发）+ 两条缓存退避边界。全表遍历抓到一个真实缺陷并修掉：gateway 表漏了 `INTERNAL_ERROR`，`get(code, 502)` 把浏览器自己的未预期异常读成"上游坏了" ⇒ `local_gateway.py` 补 `"INTERNAL_ERROR": 500`（本卡唯一生产改动，决议 Q57）。退避边界定为"只有 503/504 可退缓存、任何 4xx 判定原样回给调用方"，并说明 `SCENE_NOT_LOADED` 虽由浏览器上报仍是 503、故分流只能看状态码（Q58）。卡片原文"新增覆盖：400/404/409/413/429/503/504 全错误码路径、`expected_version`、级联 vs `detach_children`、binary 配对失败"四项里三项已由 M2-M5 就地覆盖（逐条点名见卡片"已完成"段），本卡只补整表一致性。`tests/test_local_assets.py` 一项按实际作废 —— 已在 M2-2 `git mv` 为 `test_gateway_routes.py`（§3 盘点表第 39 行就地标注）；`tests/fixtures/*` 无需重写（夹具已在 M2/M4/M6 就位）；agent 断言一字未改。验证：`python -X utf8 -m unittest discover -s tests` 8 模块 101 → 109 例全绿；`local_gateway.py` 改动不触及前端，`tsc --noEmit` 0 错、vitest 228 例 / 11 文件、`npm run build` 通过；真机未重跑（无 UI/协议形状变更，新映射只在错误路径生效，该路径由 `/events` 夹具覆盖比手工注入异常更可复现）。规格同步：§5.13 表后新增"表就是契约"段 + 新增 §13.15（Q57/Q58）

## 任务卡原文

#### M7-1 Python 测试重写
- **规格**：§10.6
- **涉及**：`tests/test_local_assets.py`(删除)、`tests/test_ws_protocol.py`/`test_caller_apis.py`/`test_traj_api.py`/`test_capture.py`/`test_bbox.py`(M2-M5 已建，此处补全)、`tests/fixtures/*`
- **改动要点**：agent 相关断言保留（conversation/jobs 的 501 语义不变）；新增覆盖：400/404/409/413/429/503/504 全错误码路径、`expected_version`、级联 vs `detach_children`、binary 配对失败。
- **DoD**：`python -X utf8 -m unittest discover -s tests` 全绿。
- **已完成（2026-10-10）**：**盘点结论先说：卡片列的四项"新增覆盖"里有三项在 M2-M5 就地测过了，本卡只补第四项**。① `expected_version`：`test_caller_apis.py:104`（查询串 → `payload.expected_version` 为 int）+ `test_traj_api.py:104`（`updateTraj` **不带** `expected_version` 且不广播）；② 级联 vs `detach_children`：`test_caller_apis.py` 的三条 `remove_object_*`（`"true"` → 布尔转发、缺省 → `detach_children: False` 即级联、`"maybe"` → 400 且**不给浏览器发帧**）与前端侧 `world.test.ts:219`/`scene_requests.test.ts:131`（§5.14 子树收集与"只重挂直接子节点"的**权威**断言在浏览器，gateway 只是通道，所以级联语义必须在 TS 侧）；③ binary 配对失败：`test_traj_api.py:148`（`GATEWAY_FRAMING_ERROR` 带"328 of 12088 bytes arrived"）+ `test_capture.py:225`/`test_bbox.py:216`；④ 413/429/503/504：`test_caller_apis.py:125`、`test_ws_protocol.py`（429/503/504）、`test_traj_api.py:210`。**真正缺的是"整张 §5.13 表与实现一致"这一条**，于是新建 `tests/test_error_mapping.py`（8 例）：一条 `assertEqual(BROWSER_ERROR_STATUS, SPEC_ERROR_STATUS)` 把 gateway 的读码表钉成规格表的镜像（25 个浏览器可上报码），加一条 subTest 遍历全表、每码真跑一次 `/updateItem` ↔ `/events` 往返，断 status/`code`/**原始 message**/`error.request_id` 四项；三条兜底各一例（未知码 → 502 且保留浏览器原话、缺 `code` → `UPSTREAM_RENDER_FAILED`、`error` 不是对象 → 502 而非 gateway 抛异常）；再加"`ok` 是唯一成功标志"（`ok:false` 附带 `payload` 时不得转发该 payload，断 409 且响应体无 `scene_version`）与两条缓存退避边界。**遍历首轮即抓到一个真实缺陷**：§5.13 的 `500 INTERNAL_ERROR` 一行在 gateway 表里漏了，`get(code, 502)` 静默把浏览器自己的未预期异常读成"上游坏了"（`scene_requests.ts:73` 无处理器、`main.ts:755` 处理器内异常都走这个码）——本卡唯一的生产改动就是补 `"INTERNAL_ERROR": 500`（决议 **Q57**）。退避那条边界写进 **Q58**：只有 503/504 能退回 `world_cache`，任何 4xx 判定必须原样回给调用方；`SCENE_NOT_LOADED` 虽由**浏览器**上报却仍是 503，所以分流只能看状态码、不能看"错误来自谁"。**卡片原文的"涉及"两处按实际作废**：`tests/test_local_assets.py` 早在 M2-2 就 `git mv` 成 `test_gateway_routes.py`（§3 盘点表第 39 行已就地标注）；`tests/fixtures/*` 无需重写，夹具已在 M2/M4/M6 就位（`scene_example.json`、`robot_grasp.traj`、`phong_cube.fbx`、`transformed.dae`、`ols_stub.py` + `ols_data/`，且 `test_ols_stub.py` 已锁 stub 自身）。agent 断言一字未改（`test_gateway_routes.py` 的 jobs 501 / conversation 全生命周期仍在）。测试：**`python -X utf8 -m unittest discover -s tests` 8 模块 101 → 109 例全绿**；`local_gateway.py` 改动不触及前端，`tsc --noEmit` 0 错、vitest **228 例 / 11 文件**、`npm run build` 通过；真机未重跑（本卡无 UI 与协议形状变更，且新映射只在"浏览器报错"这条错误路径上生效，错误路径的实时验证由测试套的 `/events` 夹具承担，比手动注入异常更可复现）。
