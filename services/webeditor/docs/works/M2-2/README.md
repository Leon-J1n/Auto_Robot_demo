# M2-2 —— gateway 删除服务端场景与旧路由

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M0-1 |

## 原因与做了什么（PLAN_STATE 备注）

gateway 按 §10.1~§10.3 瘦身成通道层。删除 17 个 handler 与 `create_app` 中对应注册行（`bootstrap`/`assets`/`commands`/`upload`/`import_remote`/`remote_proxy`/`delete_asset`/`asset_file`/`sensor_frame`/`state_load`/`diagnostics`/`metrics`/`presentation`/`mode_update`/`agent_control`/`timeline_frame`/`timeline_data` + 未被引用的 `json()` echo helper），保留 `health`/`runtimeConfig`/全部 agent 路由/`events`/静态兜底；`git rm local_assets.py local_urdf.py`（`data/uploads/` 再无引用）。`__init__` 删 §10.2 全部状态（`revision`/`entities`/`saved_states`/`undo_stack`/`redo_stack`/`settings`/`mode`/`presentation_revision`/`timeline`/`simulation_time`/`captures`/`assets_root`/`asset_service` 与 `assets_root` 参数），只剩 M2-1 引入的 `browsers`/`pending`/`world_cache`/`scene_version` + `jobs_state`/`conversations`/`features`；`_entity`/`_light`/`_transform`/`snapshot()` 一并删除。顺带修一个既有 bug：`static()` 的 `return await web.FileResponse(...)` 在 aiohttp 3.14 下 `TypeError: object FileResponse can't be used in 'await' expression`，任何 GET 都 500 —— 改为 `return web.FileResponse(...)`，`tests/test_gateway_routes.py` 的静态兜底用例锁死。`conversations_create` 的 `presentationMode` 内联 `"real-sync"`（`self.mode` 已不存在，agent 侧不依赖 snapshot，§1.5）。features 下发通道改变见 §13.3 Q18：`runtime_config` 响应加 `features`，`RuntimeConfig` 契约加必填 `features: FeatureFlags`（`contracts/types.ts` 与根 `types.ts` 已 `cp`+`cmp` 保持一致）。`main.ts` 的旧 UI 数据源随路由一起拆除：删 `refreshSnapshot`/`refreshAssets`/`refreshDiagnostics`/`state.logs`/`renderDiagnostics`/`[data-delete-asset]` 分支/`toggle-diagnostics` 的取数/重连补拉，以及 `onmessage` 里 `scene.changed`+`simulation.tick`+`presentation.` 三个已作废分支（agent 两个分支保留）与 `decideSnapshot` import；`start()` 改为 runtimeConfig → `createLibraryClient` + `state.features` + `applyFeatureVisibility()` → 会话 → `connectWebSocket()`。`sendCommand` 保留但 `state.snapshot` 恒为 `null` ⇒ 旧编辑命令是静默 no-op（面板只剩空列表），Settings 面板 mode 按钮点了会 405，均归 M6-1 删除；`tools/browser_smoke.cjs`/`tools/audit_parity.py`/`docs/api-inventory.json` 仍指向旧路由，按计划在 M7-2/M7-4 重写，本卡不打临时补丁。测试：`tests/test_local_assets.py` → `git mv` 为 `tests/test_gateway_routes.py` 并重写为 9 例（health、runtimeConfig 含常量与 features、agent jobs 501+列表、conversation 全生命周期、被删路由的 POST 得 404/405 而 GET 落 SPA 兜底 HTML、静态兜底、gateway 不再持有场景字段、`load_constant_config()` 与内置默认一致），`tests/test_ws_protocol.py` 的 setup 去掉 `assets_root`/临时目录。验证：DoD grep `api/commands\

## 任务卡原文

#### M2-2 gateway 删除服务端场景与旧路由
- **规格**：§1.2、§1.4、§10.1、§10.2
- **涉及**：`local_gateway.py` 的 `snapshot`/`bootstrap`/`commands`/`upload`/`import_remote`/`remote_proxy`/`delete_asset`/`asset_file`/`sensor_frame`/`state_load`/`diagnostics`/`metrics`/`presentation`/`mode_update`/`agent_control`/`timeline_frame`/`timeline_data`、`self.entities`/`saved_states`/`undo_stack`/`redo_stack`/`simulation_*`；`local_assets.py`(删文件)、`local_urdf.py`(删文件)
- **改动要点**：
  - 删除 §10.1 列出的全部路由与 `create_app` 中对应注册行；保留 `/api/health`、`/events`、`/api/runtimeConfig`、**全部 agent 路由**、静态兜底。
  - 删除 `local_assets.py` / `local_urdf.py` 及其 import；确认 `data/uploads/` 不再被引用。
  - gateway 只保留 `self.world_cache: dict|None` + `self.traj_cache: bytes|None`（原样缓存，不解析，§1.4）。
  - `agent sceneId` 适配：agent 侧不依赖 snapshot（`conversation` 里的 `sceneBinding.sceneId` 保留），仅需确认删除 `snapshot()` 后 agent handler 无残留引用（§1.5）。
- **DoD**：`grep -rn "api/commands\|api/bootstrap\|api/assets\|api/sensors\|api/timelines\|api/presentation\|load-simstate\|agent-control" local_gateway.py` 无结果；`python -X utf8 -m unittest discover -s tests` 中与旧行为绑定的用例已被删除/重写（M7 完成全绿）。
- **验证**：上述 grep + `python local_gateway.py --port 8080` 能启动。
