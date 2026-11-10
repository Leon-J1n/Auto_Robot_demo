# M2-1 —— `/events` 升级为请求/响应通道

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M0-1 |

## 原因与做了什么（PLAN_STATE 备注）

`local_gateway.py` 的 `/events` 升级为 §5.1~§5.4 的请求/响应通道：`events()` 改 `web.WebSocketResponse(heartbeat=30, max_msg_size=0)`（坑 1，实测 5 MB 的 `state_sync` 文本帧可收）+ `async for message in socket` 分派 TEXT（`browser.hello`→`self.browsers[browser_id]`、`browser.response`→结算挂起请求、`browser.state_sync`→缓存并广播）与 BINARY（§5.3 的二进制只 gateway→浏览器，收到则 warn 忽略），断开时按 socket 反查注销；删除 `{"type":"connected","revision":N}` 问候帧与 `commands()` 末尾的 `scene.changed`+snapshot 广播（§5.4 作废）。新增 `PendingRequests`（`request_id -> Future`、首个响应者胜出、`max_pending_requests` 超限 `429 TOO_MANY_REQUESTS`、超时/失败后槽位可复用）、`BrowserError`（继承 `web.HTTPError`，body 即 §5.6 `{"error":{code,message,request_id}}`，`content_type=application/json`；aiohttp 的 `HTTPError.__init__` 不接 `status`，要在 `super().__init__` 前设 `self.status_code`）、`BROWSER_ERROR_STATUS`（§5.13 浏览器可上报码 → HTTP 状态，未知码 502）、`dispatch(action, payload, *, timeout=None, binary=None, browser_id=None)`：校验 action ∈ `BROWSER_ACTIONS`、无浏览器 `503 NO_BROWSER_CONNECTED`（含定向 `browser_id` 不在线）、默认超时按 §5.2 取 `browser_request_timeout_seconds`/渲染类 `capture_timeout_seconds`、`binary` 时写 `payload.byte_length`+`expects_binary` 并紧随一个 binary 帧、`asyncio.wait_for`（坑 10：绝不轮询）超时 `504 BROWSER_TIMEOUT` 且带 `request_id`；扇出中某个 socket 发送失败时若已送达 ≥1 个浏览器则继续等待（warn），一个都没送达才 `503`。`apply_state_sync()` 按 §5.8 原样缓存 `world_cache` 并广播 `scene.applied{scene_version, action:"updateWorld", source_request_id:null}`，版本语义见 §13.3 Q17（不引入 gateway 计数器）。浏览器侧只补 §5.1 的 hello：`main.ts` 新增 `BROWSER_ID = crypto.randomUUID()`，`connection.onopen` 发 `{type:"browser.hello",browser_id,role:"viewer"}`（`browser.request` 处理分支仍归 M3-1，`binaryType="arraybuffer"` 归 M4-2）。验证：新建 `tests/test_ws_protocol.py` 14 例全绿（DoD 的 hello 登记 / request-response 匹配 / 超时 / 429 / binary 配对，另含多浏览器首响应胜出并 warn 丢弃迟到帧、渲染类走 `capture_timeout`、无 hello 无问候帧且 `503`、定向缺席 `503`、未知 action `400 INVALID_PARAM`、`VERSION_CONFLICT` → 409 且 `request_id` 回填、`state_sync` 缓存+广播、5 MB 帧、畸形帧只 warn 不断链、断开自动注销）；`tests/test_local_assets.py` 10 例（其中原 `test_camera_no_geometry_and_real_event_envelope` 依赖已作废的 `scene.changed` 信封，重写为 `test_camera_has_no_geometry_and_frame_needs_a_browser`，只保留相机无几何 + `/frame` 501 断言）；合计 24 py 用例 OK，`tsc --noEmit` 0 错、vitest 100 例 / 6 文件、`npm run build` 通过。浏览器实连冒烟（gateway 8080 以 DEBUG 跑临时启动器 + vite 5173，日志与文件已删）：页面加载后日志出现 `browser registered id=<uuid>`、0 条 WARNING/Traceable，说明 M1-5 的 `state_sync` 帧在新 handler 下格式正确（畸形会 warn）；再注入 `scene_example.json` 走 Load Scene，状态栏 `Connected · #1 Sample Scene · v1 · 5 objects`，控制台无报错。过渡期副作用（已写进 §1.2）：旧 `/api/commands` 不再推送事件，面板不会因它刷新，该路由本身在 M2-2 删除

## 任务卡原文

#### M2-1 `/events` 升级为请求/响应通道
- **规格**：§5.1、§5.2、§5.4
- **涉及**：`local_gateway.py:events`、`broadcast`、`StandaloneGateway.__init__`、`create_app`
- **改动要点**：
  - `events()`：改为 `async for msg in socket:`，处理 `WSMsgType.TEXT`（`browser.hello`/`browser.response`/`browser.state_sync`）与 `WSMsgType.BINARY`（§5.3 配对）；`web.WebSocketResponse(heartbeat=30, max_msg_size=0)`（**必须**，见 §5-坑1）；登记 `browser_id` 到 `self.browsers`。
  - 新增 `PendingRequests`：`request_id -> asyncio.Future`；`await asyncio.wait_for(fut, timeout)`；超时 `504 BROWSER_TIMEOUT`；无浏览器 `503 NO_BROWSER_CONNECTED`；挂起上限 `max_pending_requests` → `429`。
  - 新增 `dispatch(action, payload, *, timeout, binary=None, browser_id=None) -> dict`：所有新接口的公共实现。
  - 广播 `scene.applied`（带 `source_request_id`）；**删除** `scene.changed` + snapshot 信封（约 228）。
- **DoD**：`tests/test_ws_protocol.py`(新建) 用 aiohttp `TestClient` 的 ws 连一个假浏览器，验证 hello 登记、request/response 匹配、超时、429、binary 帧配对。
- **验证**：`python -X utf8 -m unittest tests.test_ws_protocol -v`。
