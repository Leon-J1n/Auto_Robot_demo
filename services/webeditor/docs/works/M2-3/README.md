# M2-3 —— `/getWorld` 与世界缓存

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M2-1 |

## 原因与做了什么（PLAN_STATE 备注）

新增 `local_gateway.py:get_world()` 与 `app.router.add_get("/getWorld", ...)`（注册紧跟 `/api/runtimeConfig`，在通配 static 之前，坑 2 —— `tests/test_gateway_routes.py::test_get_world_is_registered_before_the_spa_catch_all` 用真实 `create_app()` 锁死：无浏览器无缓存时得 503 JSON 而不是 index.html）。语义：`dispatch("getWorld", {})` 向浏览器实时取 → 载荷取 `payload["world"]`，非 dict 则 `502 GATEWAY_FRAMING_ERROR`；成功回 `web.json_response(world, headers={"X-World-Source": "live"})` 并把这份世界写回 `world_cache`；`BrowserError` 状态为 503/504 且缓存非空才回退（`X-World-Source: cache`），429 与浏览器自报的其它错误码原样抛出（绝不用旧数据掩盖调用方错误）。载荷形状/缓存语义已记 §13.3 Q19。`browser.state_sync` 分支沿用 M2-1 的 `apply_state_sync()`，本卡未改。浏览器侧还不会答 `getWorld`（`main.ts` 的 `browser.request` 分派按卡在 M3-1），故 live 路径目前由 fake browser 覆盖。测试：`tests/test_ws_protocol.py` 14→20 例（DoD 两条＝`test_get_world_route_serves_the_live_world_of_a_browser` + `..._falls_back_to_the_cache_without_a_browser`；另加 504 回退、无浏览器且无缓存 503、`SCENE_NOT_LOADED` 在无缓存时透出、缺 `world` 载荷 502；新增 `next_request_frame()` 帮助函数跳过 `scene.applied` 广播帧、`sync_world()` 等缓存落位）。验证：`python -X utf8 -m unittest tests.test_gateway_routes tests.test_ws_protocol` 30 例 OK（10+20）；vitest 100 例 / 6 文件、`tsc --noEmit` 0 错、`npm run build` 通过（前端本卡无改动）

## 任务卡原文

#### M2-3 `/getWorld` 与世界缓存
- **规格**：§5.8、§5.9
- **涉及**：`local_gateway.py:create_app`、新增 `get_world()` handler、`browser.state_sync` 处理分支
- **改动要点**：优先 `dispatch("getWorld")` 向浏览器实时取；`503/504` 时回退 `world_cache`；响应头 `X-World-Source: live|cache`；`application/json`。注册顺序在通配 static 之前。
- **DoD**：`tests/test_ws_protocol.py` 增两条：有浏览器时取 live；无浏览器时取 cache 且头为 `cache`。
- **验证**：`python -X utf8 -m unittest tests.test_ws_protocol -v`。
