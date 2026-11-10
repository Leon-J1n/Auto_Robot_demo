# FEAT-2026-10-16-A —— Agent 面板改任务制：gateway 每 5 秒轮询、五个中文状态、失败原因原样回读

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-16 |
| 落地 | 本卡提交 |
| 依赖 | M2-1,M6-1 |
| 决议号 | Q86、Q87（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-16 新卡（第 ⑭ 张）：Agent 面板改任务制 —— gateway 每 5 s 轮询 `queryTaskState`、五个中文状态映射成 `created/queued/running/success/failed`、失败先读 `queryTaskFailureReason` 再落 `failed`（决议 Q86/Q87，口径来源用户自己的 `docs/gateway与Agent服务通信流程.md`，规格新增 §5.15、§1.5 改写）。用户原话：「根据 F:\web-frontend\docs\gateway与Agent服务通信流程.md 调整 Agent 面板的逻辑，Agent 样例程序在 F:\AgentExample」。分工：浏览器只发 `{command, scene}`（`scene = state.world.toJSON()`，§1.1 世界权威）并收 `agent.job_updated` 广播；轮询、状态映射、上游超时全在 gateway（`AgentRelay` + `create_app` 的 `on_startup/on_cleanup` 一个 poller，坑 10）。未知状态不猜结论：表外字符串按非终态 `queued` + 原样 `agent_state` + warn，前端 `agentStatusLabel` 同构回显。三条自造码 `AGENT_UNAVAILABLE/TASK_NOT_FOUND/AGENT_TIMEOUT`（+`UPSTREAM_ERROR`）不进 §5.13 表，双语断言钉住（坑 25 第三条出口）。唯一代码修复：`BrowserError` 继承 `web.HTTPError` 却没有 `message`，第 5 次轮询失败的收尾路径 `AttributeError`（前 4 次全绿，22 例绿、27 例才撞出）。验证：python 131 → 158 例 OK（新 `tests/test_gateway_agent.py` 27 例，直驱 `poll_agent_tasks_once` + 假 Agent 服务）、vitest 327 → 329 例、`npm run build` ✓ 20.86 s（`index-C-qOIrWy.js` 860.93 kB）、`audit_parity.py` 52 PASS / gaps []、`feature-inventory.csv` 零 diff（不增不减 feature 键）。真机（一次性栈 gateway 8097 + vite 5177 + 样例 Agent 8500 + OLS 8000）九项观测全绿：4 指令 → `taskID 1..4`、巡回 `created>queued>running>{success,failed}`、整轮 56 拍且终态后 7 s 0 拍、⑤ 在 `进行中` 的 `/updateItem` 得 200 `scene_version=4`、⑥ 原因为上游原文 `场景数据解析失败：缺失必要的模型路径`、⑦ 4 POST / 0 GET，Q87 一幕用"只丢 `agent.job_updated` 的 `/events` 中继"造出真·错过（`panel-blind.png` 只剩 `已创建`）→ 重连恰好一个 GET 补齐四个终态（`panel-terminal.png`）。新坑 53/54/55（轮询周期是下界、补取必须自己丢帧、验证服务要分离进程起），并就地更正坑 32 的"数 established 连接"判据。不做：浏览器侧轮询/SSE、任务取消（§10.1 无接口）、gateway 撰写中文、任务表持久化、`agent_service_url` 热更新。收尾：`taskkill` 掉 `Start-Process` 起的 8000/8500/8097/5177 并 `netstat` 确认无监听，两份用户自有未跟踪文档不进提交，只本地提交、不 push。

## 任务卡原文

### FEAT-2026-10-16 —— Agent 面板改任务制：gateway 每 5 秒轮询、五个中文状态、失败原因原样回读（新卡；依赖 M2-1、M6-1、Q86、Q87）

- **起因（用户原话）**：「根据 F:\web-frontend\docs\gateway与Agent服务通信流程.md 调整 Agent 面板的逻辑，Agent 样例程序在 F:\AgentExample」。顺序按既定口径：文档（**Q86/Q87** + §1.5 改写 + 新增 §5.15 + §7.1/§9 指针行）→ 实现 → 测试 → 本卡 → 本地提交 → 停服务。那份流程文档与 `docs/服务占用端口清单.md` 是**用户自己的未跟踪文件**，本轮只读不提交。
- **交付物**：`local_gateway.py` +275 行（`AgentRelay` 一类 + `poll_agent_tasks_once` + `_settle_agent_job` + `jobs`/`job` 路由改造）；`config/constant_config.json` 增 **3 个键**（`agent_service_url` / `agent_poll_interval_seconds: 5` / `agent_request_timeout_seconds: 10`）并同步 `DEFAULT_CONSTANT_CONFIG` 与 `types.ts`+`contracts/types.ts` 的 `RuntimeConfig`（**坑 41** 的四处同改，`cmp` 已核 **坑 6**）；`editor/src/logic.ts` 新增 `agentStatusLabel`；`editor/src/main.ts` 的 `submitAgent`/`updateAgentJob`/`refreshAgentJobs`/`terminalAgentStatuses`；**新测试文件 `tests/test_gateway_agent.py`（27 例）**。feature 键**零增删**（`docs/feature-inventory.csv` 本轮无 diff，§9 的那句话就是这么来的）。
- **七步各落在哪**：①起服务是人的事；②浏览器只发 `{command, scene}`，`scene` 就是 `state.world.toJSON()`（§1.1 世界权威 ⇒ 整份发，gateway 不解析、只转手，`test_the_scene_never_travels_back_downstream` 钉住下游拿不到世界）；③`POST /startTask` 回 `taskID`，任务 id 取 `String(taskID)`；④`AgentRelay` 每 5 s 一次 `GET /queryTaskState`，五个中文状态映射成 `created/queued/running/success/failed`（**同一批 token 兼作 CSS 类名**）；⑤任务在 `进行中` 时调用方路由照常生效（真机 ⑤ 就是在这一步打的 `/updateItem`）；⑥失败先 `GET /queryTaskFailureReason` 再落 `failed`，原因与状态**同一条广播**；⑦终态即停问。
- **"未知状态"不许被翻译成结论**：映射表外的字符串一律按**非终态** `queued` 处理，并把上游原文塞进 `agent_state` + `LOGGER.warning`（`test_an_unknown_state_stays_non_terminal_and_keeps_the_upstream_text`）。前端 `agentStatusLabel` 同构：认识的五个 token 出中文，**认不出的原样回显**、空值回显空（`logic.test.ts` 用"正在渲染"钉住）。理由：gateway 一旦把上游没说的状态说成"进行中"，面板就再也分不清"服务在动"和"我们猜的"。
- **每进程一个轮询器**：`create_app` 用 `app.on_startup/on_cleanup` 挂 `start_agent_poller`/`stop_agent_poller`，循环体是 `await asyncio.sleep(max(1.0, interval))`（**坑 10**：绝不用 `time.sleep`）。启动幂等（重复 `start` 不再起第二个 task），停止后 `poller.cancelled()` 为真 —— 这两条由 `test_the_poller_is_owned_by_the_app_hooks` 钉住，因为**测试客户端不会触发 on_startup**，早期版本让测试自己调 poller，结果线上根本没人调它。
- **三条 gateway 自造码不进 §5.13 表**：`AGENT_UNAVAILABLE(501)` / `TASK_NOT_FOUND(404)` / `AGENT_TIMEOUT(504)` / `UPSTREAM_ERROR(502)` 是**上游健康度**，不是世界写入的拒绝码，走的是 `BrowserError`（`web.HTTPError` 子类 + §5.6 信封）。`test_three_agent_codes_are_not_browser_error_status` 与 `error_codes.test.ts` 双向钉"它们不在 `BROWSER_ERROR_STATUS` 里"（**坑 25** 的第三条出口：什么都不做，就得有断言兜着）。
- **本轮修掉的唯一代码缺陷**：`BrowserError` 继承了 `web.HTTPError`，而它**没有 `message` 属性** —— 连续 5 次轮询失败的那条收尾路径（把上游文本重新引号进 `UPSTREAM_ERROR`）在单测里以 `AttributeError: 'BrowserError' object has no attribute 'message'` 暴露。修法是在 `__init__` 里显式 `self.message = message`，让散文能穿过 §5.6 的信封形状。症状值得记住：**只有第 5 次失败才炸**，前 4 次完全正常，所以 22 例绿、加到 27 例才撞出来。
- **验证（单测）**：`python -X utf8 -m unittest discover -s tests` **131 → 158 例 OK**（新增 27 例，全部直接驱动 `poll_agent_tasks_once`，不打桩 gateway 自己）；`tests/test_gateway_routes.py` 的 `agent_service_url = ""` 注释改为指向新文件（那套只测幸存的 HTTP 面）；vitest **327 → 329 例 / 14 文件全绿**；`npm run build`（含 `tsc -p editor/tsconfig.json`）✓ 20.86 s，`index-C-qOIrWy.js` **860.93 kB** / `index-DlU2n5kl.css` 20.57 kB（chunk>650 kB warn 为既有）；`audit_parity.py` **52 PASS / gaps []**；`api-inventory.json` 只有行号漂移（三条 `/api/agent/jobs*` 路由本来就在册）。
- **验证（真机，一次性栈 gateway 8097 + vite 5177 + 样例 Agent 服务 8500 + OLS 8000，脚本在仓库外 `scene5probe\agent_r14\verify.cjs`）**：**九项观测、0 断言失败**。四条中文指令 → `taskID 1..4`，`/listTasks` 与 gateway 侧逐一对齐；状态巡回实测 `1:created>queued>running>failed`、`2/3/4:…>success`，整轮 **56 拍**、终态后再等 7 s 是 **0 拍**；⑤ 在 `进行中` 用 `/updateItem?id=2` 写 `shoulder_pan_joint=0.2` 得 200、`scene_version=4`；⑥ 失败原因是上游原文 `场景数据解析失败：缺失必要的模型路径`，且 `message === failure_reason`、`agent_state === 任务失败`；⑦ 全程 **4 个 POST、0 个 GET**（浏览器不轮询）。Q87 那一幕：把 `/events` 中继设成**只丢 `agent.job_updated`**，页面跑完整轮后转录区仍只有 `已创建`（截图 `panel-blind.png`），随后断开→重连，**恰好一个 GET** `/api/agent/jobs`，四个终态（含 `任务失败` 与原因行）全部回到面板（`panel-terminal.png`）—— 这是"补取"唯一能被证明的写法（见**坑 54**）。
- **明确不做**：不给浏览器加任何轮询或 SSE（§5.15 的分工就是 gateway 轮询、浏览器收广播）；不做任务取消（Agent 服务没有取消接口，§10.1）；不在 gateway 撰写中文文案（五个状态的中文来自上游，面板只是回显）；不给任务表加持久化（进程内 `jobs_state` 足够，重启即空，这与 §1.4"浏览器是世界权威"一致）；不把 `agent_service_url` 做成热更新（`AgentRelay` 构造时读一次，与 `features.json` 同性质，见**坑 22**）。
- **一轮收尾**：`Start-Process` 分离启动的四个服务逐个 `taskkill`（**坑 55**：本轮两次发现从 Bash 后台起的 gateway/Agent 服务会随 shell 重置一起消失），`netstat` 确认 8000/8097/8500/5177 无监听；只本地提交、**不 push**，两份用户自有未跟踪文档不进提交。
