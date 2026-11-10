# 2026-11-07 浏览器 profile 隔离

## 原因

同一 gateway 同时服务不同设备、浏览器和浏览器 profile 时，原有 gateway 只把 `browser_id` 当作连接绑定，没有把可变现场、会话和 Agent 任务一起分区，导致 HTTP 请求可能落到另一浏览器的现场。用户还要求 gateway 默认监听 `0.0.0.0`，用于局域网联调。

## 做了什么

- 浏览器继续使用 `localStorage["webeditor.browserID"]` 作为持久身份；安全上下文优先用 `crypto.randomUUID()`，普通 LAN HTTP 回退到 `crypto.getRandomValues()` 的 UUID v4。
- 浏览器发往 gateway 的 HTTP 请求统一带 `X-WebEditor-Browser-ID`。gateway 以该头选择 profile；原有 query/body 字段仍由各路由按原协议校验，不改变 AgentBrain 的报文。
- gateway 将现场、会话、对象 id 水位、租约、任务、幂等表、对话、相机候选、pending request 和事务回执按 profile 隔离；Agent 轮询逐 profile 执行，广播只发给同 profile socket。未完成 `browser.hello` 的 WebSocket 帧被忽略。
- 首个 profile 继续使用 `var/gateway_state.json`；后续 profile 使用 `var/gateway_profiles/<sha256(browserID)>.json`，回执使用同样按 profile 哈希的目录。
- 默认配置、启动命令和规格改为监听 `0.0.0.0`；生产使用边界写明为可信局域网并由主机防火墙限制来源。
- 不引入额外 `profileID`：`browser_id` 已由 origin-scoped localStorage 生成，跨设备、跨浏览器和跨 profile 存储隔离；UUID v4 碰撞概率可忽略。它是路由分区键，不是鉴权凭据。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-07 |
| 落地 | 本轮本地提交 |
| 验证 | `test_browser_profiles.py`（2 项）、`test_gateway_sessions.py`（59 项）、`test_gateway_agent.py`（69 项）、`test_commit_world.py`（25 项）、`test_capture.py`（15 项）、`test_bbox.py`（16 项）、`test_caller_apis.py`（22 项）、`test_capture_identity.py`（15 项）、`test_scene_lease.py`（37 项）、`test_traj_api.py`（19 项）、`test_traj_examples.py`（9 项）、`test_error_mapping.py`（8 项）通过；`tsc`、Vitest（679 项）、Vite build 通过。`test_gateway_state.py` 与 `test_ws_protocol.py` 在 Windows 单模块运行时长时间无输出，依仓库坑清单记录中断；合并运行也因此未能完成。 |
