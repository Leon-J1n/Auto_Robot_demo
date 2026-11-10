# Agent 服务集成（brain-web/2）：本仓库修改计划

- 依据：`docs/webeditor_integration.md`（同事的接口契约）+ `docs/gateway与Agent新通信流程.md`（brain-web/2 定稿）
- 对外沟通：`docs/SendToTeammate.md`、`docs/AgentBrain修改点.md`；我方施工清单：`docs/gateway修改点.md`
- 协议版本：来函原口径是 `brain-web/1`，2026-10-20 因**摘要对象／会话阶段／现场身份四元组**三项语义变更升级为 `brain-web/2`；
  同日另两条定稿（**Q115** 服务健康可见、**Q116** 运行状态词表归 Brain）**不升版本**：
  它们只新增响应字段（`terminal`／`browser_id`／`tasks`）与删除我方内部映射表，不改任何既有字段的含义
  （与 `scene_sub_version` 同理——那个键从未上线）。
  本文余下段落里写 `brain-web/1` 的地方按新版本读
- 状态：**P0 已完成（端口迁移）**，P1–P8 待施工
- 写作时间：2026-10-19

约定：每阶段独立成卡（文档 → 代码 → 测试 → 任务卡 → 本地提交，**不 push**），
收尾必须停掉本轮起的服务并 `netstat` 确认。

---

## 1. 现状与目标差距（取证）

| 同事要求 | 本仓库现状 | 证据 |
|---|---|---|
| `/sessions` 会话绑定 | **完全没有**；只有内存 `conversations` 字典，且不是会话绑定 | `local_gateway.py:339,369-390` |
| `/startTask` 带 sessionID／clientRequestID／browserID／sceneDigest | 只发 `{command, scene}` | `local_gateway.py:289-300` |
| `/queryTaskResult` 展示回答／澄清／计划／产物 | 只轮询 `queryTaskState` + `queryTaskFailureReason`，无 result 路由 | `local_gateway.py:302-318` |
| `/commitWorld`、`/queryWorldCommit` 原子提交 | 只有 `updateWorld` 全量替换，且 `applyWorld` 会 `max(v+1, 提交版本+1)` 自增 | `world.ts:459-468` |
| `/acquireSceneLease`、`/releaseSceneLease` 编辑锁 | 无任何锁概念 | — |
| `/brainCapabilities` | 无 | — |
| `getWorld` 实时 + 身份头 | 只有 `X-World-Source: live/cache`，503/504 会回缓存 | `local_gateway.py:853-871` |
| `captureScene`/`get2DAABB` 带浏览器／版本／摘要／尺寸 | gateway 原样转发浏览器回执，不校验身份字段 | `local_gateway.py:802-835` |
| 写入只发一个浏览器 | `dispatch` 无 `browser_id` 时**向全部浏览器广播**，首个响应为准 | `local_gateway.py:595-649,837-851` |
| ID 高水位持久化、删除不复用 | 有单调 `watermark`，**但只在内存，刷新即丢** | `world.ts:382-383,596,603,648` |
| browserID 稳定身份 | `crypto.randomUUID()` **每次页面加载重新生成，无持久化** | `main.ts:116-117` |
| sceneDigest | 全仓零命中 | — |
| 本地编辑与版本刻度 | **口径已三改**：① "本地编辑不落服务端"→② 2026-10-20 "浏览器现场只读、四入口删除"（Q113）→③ 同日 **Q114** 三分类：位姿／关节删除，面板增删保留并挂**阶段闸门**，灯光预设改**渲染层**。子版本与四元组**保留** | `world.ts:505-518`（`addLocal*`/`removeLocal*`，保留并改为推进子版本 + 上报）、`:520-577`（`setLocal*`，待删） |
| Agent 服务健康对浏览器可见（**Q115**） | Gateway 的 `/api/health` 只报自己 `{status,mode,uptimeSeconds}`，**不含 Agent 可达性**；页面上那个 `#agent-state` Connected／Offline 徽章**不是探活**，它只在 `refreshAgentJobs()` 或提交成功时被置 `true`（`main.ts:1130`）、提交失败时置 `false`（`:1137`）。对方 `GET /health` 其实已经存在（含 `qwen`／`browser`／`assets` 真实探测），只是我方与浏览器都不读它 | `local_gateway.py:361-362`、`main.ts:82,526,1130,1137`；对方 `web/service.py:177-199,299-309` |
| 运行状态词表归 Brain + `terminal`（**Q116**） | 我方**三处**按名字判结束：`AGENT_STATES`+`AGENT_TERMINAL_STATES`（`local_gateway.py:67-74`）、`agent_status_for`（`:126-138`）、前端 `terminalAgentStatuses`（`main.ts:1119-1121`）与 `agentStatusLabel` 的五 token 映射（`logic.ts:20`）；类型层还有 `AgentJob.status`（`types.ts:185-195`）。对方今天也只回 `{taskID, state}`，词表写死在 `web/task_store.py:11` | 上述各处；`tests/test_gateway_agent.py:159,267,298,320,341,388,411-419` |

## 2. 本轮已定的五条口径（覆盖同事原文）

1. **广播写入废止**：写入只发给会话绑定的浏览器；其余 viewer 收到 `scene.applied` 后主动 `getWorld` 刷新，不再自动 apply。
2. **`getWorld` 缓存兜底废止**：无绑定浏览器 ⇒ 503，不再伪装现场。
3. **浏览器入口三分类**（2026-10-20 第三次定稿，决议 **Q114**；修正同日 Q113 的"四入口全删、三元组"）：
   **① 删除** gizmo 平移/旋转/缩放与 Details 关节角度输入；
   **② 阶段闸门** 资产面板加入／删除物体与切场景——`setup` 期可用，**向 Agent 发出消息后禁用并弹窗**；
   **③ 渲染层本地** 灯光预设只改浏览器渲染，不进文档／导出／摘要／`getWorld`。
   CAS 判据是 `scene_id + scene_version + scene_sub_version + sceneDigest` **四元组**：
   `scene_version` 只由落到现场的 Agent 写操作推进（目标恰好 `previous + 1`，candidate 子版本必为 0），
   `scene_sub_version` 只由 `setup` 期的 ② 类改动推进；`scene_sub_version` 是**可选顶层 uint32**（缺失即 0），
   历史场景文件一律合法。对方的版本判据必须改词典序（`docs/AgentBrain修改点.md` §A3）。
   落点见 `docs/gateway修改点.md` §2（施工步骤 S0）与 `docs/gateway与Agent新通信流程.md` §1.3、§14。
4. **Agent 服务的健康由 Gateway 汇总暴露**（2026-10-20 第四次定稿，决议 **Q115**）：对方 `GET /health`
   保留并定稿字段（六键不动 + 新增 `browser_id`／`tasks`，**进程活着即 200**）；我方新增
   `GET /health`（转发，3 秒超时）与 `GET /api/agent/health`（透传，缓存 ≤5 秒），并把
   `agent_service`／`browser`／`assets` 三分项并进浏览器启动即查的 `GET /api/health`。
   **边界**：健康读数只进服务状态条，**不得进 job 视图**，也不得作为解除编辑锁或 `quarantined` 的依据。
5. **任务运行状态词表归 Brain，终态判据改 `terminal`**（同日决议 **Q116**）：`/queryTaskState` 的
   `state` 是上游自定义的任意非空字符串，我方**删除全部按名字的代码**（三处映射表 + 类型字段
   `AgentJob.status`），面板原样回显上游文案；唯一停轮询依据是响应里的 **`terminal` 布尔**
   （缺失 ⇒ 非终态 + warn）。**404 与"连续 5 次传输失败"上限不动**；`outcome` 五态是结果分类，
   仍然是固定契约。落点 `docs/gateway修改点.md` §7.1/§7.2，协议 §7.4/§5.3，对方工单 §A11。

## 3. 规约修订清单（先改文档再动代码）

`docs/Web前端需求规格v2.md`：

- **§5.2** 广播语义 → 单写入方 + 只读 viewer 刷新；`browser_id` 从"可选定向"升格为**必填**。
- **§5.4** `scene.applied` 广播载荷补 `browserID/scene_id/scene_version/scene_sub_version/sceneDigest` 与 `session_phase`，语义改为"通知刷新"而非"已各自应用"；新增帧类型 `session.phase_changed`。
- **§5.8** 删除缓存兜底段；`X-World-Source` 只剩 `live`，新增 `X-Browser-ID`/`X-Playback-State`/`X-Scene-Digest`/`X-Next-Object-ID`。
- **§5.9** 新增根路径 `/commitWorld`、`/queryWorldCommit`、`/acquireSceneLease`、`/releaseSceneLease`、
  `/resolveUncertain`、`/brainCapabilities`、`/sessions`、`/allocateObjectID`、`/api/sessionState`，
  以及 **`/health`（转发对方 Agent 服务）与 `/api/agent/health`（浏览器用透传）**（Q115）；
  `GET /api/health` 扩 `agent_service`／`browser`／`assets` 三分项。
- **§4.8**（含 §4.8.1–§4.8.4）改写为"**浏览器现场改动与导出**"，按三分类落笔：
  gizmo（§4.8.3）与 Details 关节输入（§4.8.4）两节撤销，**Q79/Q80/Q81 就地废止**；
  光照预设（§4.8.1）**保留但改语义**为渲染层覆盖（**Q77 就地修订**，废止的是它的持久化与上报）；
  资产面板"加入世界"**保留并挂阶段闸门**。规格 §9.2 的 `allow_details_joint_angle_edit` 常量整条删除。
  §3.1 新增可选顶层键 `scene_sub_version`。逐项落点见 `docs/gateway修改点.md` §1/§2。
- **§5.15** Agent 任务路由改写为 brain-web/2：`/sessions`、上下文**四元组**、`/queryTaskResult`、产物白名单下载、终态 `outcome` 与 `executed=false`。
- **§5.15 状态映射表**（规格 `:368-379`）**整表作废**（Q116）：删"Agent `state` → `status` → 中文文案 → CSS 类"
  四列，改为"`terminal` 是唯一停轮询依据、`agent_state` 原样显示、CSS 类按 `terminal`＋`outcome`"；
  表下"未知 `state` 按非终态"（`:377`）改写为"`terminal` 缺失即非终态"，
  "轮询期错误与 5 次上限"（`:379`）**原样保留**。同时新增一小段服务健康（Q115）。
- **§11.2.1** 端口分配（**已落**）。
- **§12** 验收补 brain-web/2 联合验收条目。
- 新决议号从 **Q109** 起（Q107=`RobotDriver.role`、Q108=视口截图按钮已被占用，2026-10-19/20 落地；
  §12 验收现有条目排到 **§12.16**，本期新增从 **§12.17** 起）。

## 4. 阶段划分

### P0 · 端口迁移 8080 → 8099 ✅ 已完成（本轮）

改动落点（已全部同步并验证）：

| 文件 | 内容 |
|---|---|
| `local_gateway.py:39` | 内置默认 `gateway.port` |
| `config/constant_config.json:4` |  shipped 默认 |
| `start-dev.ps1:2` | `-GatewayPort` 默认 |
| `editor/vite.config.ts:4` | `WEB_GATEWAY_URL` 兜底 |
| `tools/serve_verification.py:14,51` | 一次性验证栈 **8099 → 8098**（让位） |
| `tools/browser_smoke.cjs:9` | 冒烟注释指向 8098 |
| `README.md` 18 处 | 启动／curl／发布／验证段落 |
| `examples/gateway_client_demo.py:13,26,70,1168-1169` | `DEFAULT_GATEWAY` 与用例 |
| `docs/Web前端需求规格v2.md` §6.9、§9.2、**新增 §11.2.1** | 规约正文 |
| `docs/Web前端实施计划.md` §5 坑 2/3/22、§6 命令速查 | **只改可执行配方，历史卡片正文不动** |
| `docs/服务占用端口清单.md` | 用户自有文档，已同步（**保持不提交**） |
| `docs/parity-probes.json` | 复跑 `audit_parity.py` 重生成，`runtimeConfig` 探针实值 8080→8099（**这次要提交**） |

验证：`audit_parity.py` **53/53**、`parity_inventory.py` 21 路由／84 键／gaps `[]`、
`tsc --noEmit` 0 错、vitest **380 例全绿**、`unittest` **184 例中 1 失败** ——
该失败（`test_default_config_file_matches_the_built_in_defaults`）源于
`config/constant_config.json` 里**用户自有的** `traj_waypoint_stride_frames: 10 → 5` 与内置默认不符，
**先于本轮存在**，不在本轮修复范围（要么改回 10，要么把默认也改成 5；留给下一轮定夺）。

> **P0 的隐含前置**：端口改动**必须与 runtimeConfig 同步**才有效，这条同事特别提醒过；
> 我方四处默认已一致，`GET /api/runtimeConfig` 的 `gateway.port` 即 8099。

> **P1 的准入门（2026-10-20 定）**：Brain 侧 §A1 的**解析器重写**（按 `docs/场景文件描述.md` + 规格 §3
> 的宽容口径）加 §A2 的摘要对象改造是联调准入门，判据是 `docs/AgentBrain修改点.md` §A1.8 的**七类**真实输入。
> 我方 S0（入口三分类施工）、S1（规约改写）与 S2（digest）可与它并行；在它落地之前，任何"协议跑不通"的归因都先落到这一条上，
> 不要怀疑我方导出格式 —— 格式的真值在我方规约里，逐字节保真是规格 §4.8.2 的既有契约。

### P1 · 身份与现场的真值（第一步的地基）

1. `main.ts:116` `BROWSER_ID` 改**持久化**（`localStorage` 存 `webeditor.browserID`，缺失才生成）；
   新增"新建／恢复／切换绑定"的最小 UI 入口。
2. Gateway 落盘面 `var/gateway_state.json`：当前绑定 browserID、会话阶段、**对象 ID 高水位**、事务回执目录。
   高水位所有写入口（HTTP 写、`setup` 期浏览器本地新增的 `state_sync` 上报、Agent 申领）共用，
   **只升不降**，采纳上报时抬到 `max(持久, 文档最大 id + 1)`，上限 **`2^32 - 1`**。
3. `dispatch` 写入路径改**定向**：只发绑定的浏览器；未绑定 ⇒ 明确错误，不再隐式广播。
4. `get_world`（`local_gateway.py:853`）删除缓存分支，补 `X-Browser-ID`/`X-Playback-State`/`X-Scene-Digest`/**`X-Scene-Sub-Version`**/`X-Next-Object-ID`/`X-Session-Phase`。
5. ~~浏览器 `getWorld` 回执补身份字段（`scene_requests.ts:70-71` 目前只回 `{world}`）~~
   —— **我方已落地（S2-a）**：`GetWorldResult` 回 `world`/`scene_id`/`scene_version`/`scene_sub_version`/
   `playback_state` 五项，回执里**不带** `scene_digest`（R5 单点：摘要只有 Gateway 算）。

退出判据：刷新页面 browserID 不变；两个 tab 时第二个只读且提示现场由另一浏览器持有；
`getWorld` 在无浏览器时稳定 503，绝不返回缓存。

### P2 · sceneDigest（JCS + SHA256）

Gateway 侧新增 `digest.py`（**S2-a 改口：只有这一份，Python；原稿的"浏览器 `digest.ts` + Gateway 同算法 Python 实现"两份一起作废** ——
Gateway 本身就是 Python，摘要的三个消费者全在它手里，TS 那份没有消费者，留着就是施工单 §0 R5 禁止的第二处实现）：
JSON Canonicalization Scheme（键按码位升序、数组保序、数值规范形按 `Number::toString`）→ UTF-8 → SHA-256 → 小写 hex。
浏览器只把 `getWorld` 回执里的身份字段交出去，**不自算摘要**。

前置：同事的 fixtures（`docs/AgentBrain修改点.md` §A8.B3）。**摘要范围已定**：覆盖整份收到的文档，
**包含** `scene_version` 与 `scene_sub_version`，不排除任何字段、不补默认值（§2 决定 3、流程文档 §3）。
测试：成对 fixture 双向断言 + `1`/`1.0` 一致 + 数组顺序不同 ⇒ 摘要不同 +
"仅 `scene_sub_version` 变"与"仅 `now_joint_positions_rad` 变"两例必得不同摘要。

### P3 · 会话、任务转发与结果展示（同事第一步）

- Gateway：`/sessions`（POST 创建/恢复/显式切换、GET 查看绑定与 uncertain）、`/brainCapabilities`；
  `AgentRelay.start_task` 载荷补 `sessionID/clientRequestID/browserID/sceneDigest/scene`，
  幂等键 `clientRequestID` 由前端生成并复用（重复点击不新建任务）。
- Gateway：`/queryTaskResult`、`GET /tasks/{id}/artifacts/{name}` 白名单代理（4 个文件名），
  任务终态把 `outcome/reply/plan/metrics.model_calls/artifacts/commitReceipts` 落到 job 视图并广播。
- 前端：Agent 面板显示 回答／澄清问题／技能顺序（plan 的 Commands）／Qwen 调用数／产物下载；
  `plan_ready` 文案固定"**计划已生成，尚未执行**"，任何终态都不得显示"执行成功"。
- **状态映射表整块删除**（Q116，取代上一稿"扩展 `AGENT_STATES`"的写法）：`AGENT_STATES`／
  `AGENT_TERMINAL_STATES`／`AGENT_UNKNOWN_STATE`（`local_gateway.py:67-76`）、`agent_status_for`（`:126-138`）、
  前端 `terminalAgentStatuses`（`main.ts:1119-1121`）与 `agentStatusLabel` 的 token 分支（`logic.ts:20`）、
  类型字段 `AgentJob.status`（`types.ts:185-195`，两份类型文件同步）全部去掉；
  job 视图改 `{agent_state, terminal, state_detail}`，停轮询只看 `terminal`，
  `AGENT_POLL_ERROR_LIMIT`（`:79`）与 404 分支保持原样。
- **服务健康转发**（Q115）：新增 `GET /health`（转发，3 秒超时，不可达 ⇒ `502 unreachable`）、
  `GET /api/agent/health`（透传 + ≤5 秒缓存 + `X-Agent-Health-Age`），`GET /api/health`（`:361-362`）
  扩三分项；前端 `#agent-state` 徽章改由 `agent_service.status` 驱动（今天它是提交路径的副作用，
  `main.ts:1130,1137`），15 秒轮询，**不可达只变状态条、不碰 job**。

### P4 · 原子场景提交

- 新增 `/commitWorld`（`transactionID` 幂等 + `expected` **四元组** CAS + 全量先验证后一次应用 +
  目标版本必须旧版 +1、candidate 的 `scene_sub_version` 必为 `0`，**不额外自增**）与 `/queryWorldCommit`（读持久回执）。
- 浏览器侧新增"验证一遍再落地"的 dry-run 路径：`world.ts` 现在**先改状态再返回拒绝**（`replaceObjects`
  直接落到 `this`），必须改成 probe 全绿才 `replaceState`，保证任何拒绝零落地。
- 回执落 Gateway 磁盘，按 `transactionID` 去重；同事务异内容 ⇒ 冲突；刷新后仍可核对。
- 保留 `updateWorld` 给旧调用方，Brain 侧禁用。

### P5 · 场景租约与编辑锁

- `/acquireSceneLease`（校验绑定 + 现场**四元组**，停止轨迹播放，冻结切场景与 ② 类增删入口，
  灯光预设复位并冻结，返回 `leaseToken`）、
  `/releaseSceneLease`（`keepLocked` ⇒ `quarantined`）；后续 Brain 请求带 `X-Brain-Lease` 并由 Gateway 验归属。
- 前端：租约期间禁 `Load Scene`／预设场景切换并停播；**② 类增删的禁用绑在会话阶段上而不是租约上**
  （`agent_owned` 全程禁用 + 点击弹窗，见 §2 决定 3 与 `docs/gateway修改点.md` §2.4）。
  状态不明（断线、超时、unknown 事务）保持待确认，不自动回到可写。
- 不实现"超时自动解锁"。

### P6 · 相机现场与尺寸校验

- `capture_scene`/`get_2d_aabb`（`local_gateway.py:802-835`）：强制 `browser_id` 绑定，
  要求回执带 `browserID/scene_id/scene_version/scene_sub_version/sceneDigest/playback_state/width/height`，逐项校验后才转发；
  播放中／旧缓存／错浏览器／尺寸不符／PNG 不完整 ⇒ 拒绝。
- 采集期间持有租约；capture、AABB、前后 `getWorld` 必须同一现场（同一 `sceneDigest`）。
- 深度：`width*height` 个小端 float32；PNG 必须是真实渲染（沿用 §12.6 出图判据，不接受占位图）。
- segmentation 明确拒绝。

### P7 · 端口与部署收尾

`tools/serve_verification.py`（8098）、`launch_gateway.bat`、`pack.py` 包内 `VERSION.txt` 与
`config/constant_config.json` 随包发布；新一次联调文档写明 8099 真实栈 / 8098 验证栈。

### P8 · 真浏览器联合验收（替身不算，规约 §12.15 起）

`tools/browser_smoke.cjs` 扩 brain-web/2 项（对 8099 真实栈 + 真实 OLS 8000 + Brain 桩服务）：
`setup` 期用户面板加物（子版本 +1、`scene_version` 不动）→ 发任务 → Agent 加物（申领 id、子版本归 0）→
Agent 改蓝 → 查询任务（四元组不动）→ `agent_owned` 期切场景被拒 + **点面板加物弹出文案** →
Agent 删除续聊 → 连续两次 original → 真实 Qwen commands v2 → 真实浏览器视觉消歧；
负控制三项：页面上找不到 `[data-tool=translate|rotate|scale]` 与 `[data-joint]`（但 **`#lighting-preset` 必须存在**）、
切换灯光预设后两次 `getWorld` 摘要必等、无写回时两次读取摘要必等。
**Q115／Q116 两支**：停掉 Brain 后状态条显示"不可达"而**正在跑的 job 文案与颜色不变**（只有连续 5 次
轮询失败才落失败），重启后 `/api/health` 的 `agent_service` 回 `ready`；用桩服务喂 `"规划中"`
这一我方从未见过的字符串 ⇒ 面板原样显示这四个字且任务保持非终态，桩服务改 `terminal:true` ⇒ 立刻停轮询出结果。
（依赖对方 §A8.B7 桩服务具备"喂任意状态字符串"的能力，`docs/AgentBrain修改点.md` §A11.5。）
**替身/无浏览器探针（`audit_parity.py`）不能替代这一步**，缺外部资源必须显式 skip 而非 PASS。

## 5. 每阶段验证链

```bash
npx tsc --noEmit -p editor/tsconfig.json
npx vitest run --config editor/vitest.config.ts
python -X utf8 -m unittest discover -s tests
python -X utf8 tools/audit_parity.py && python -X utf8 tools/parity_inventory.py
cmp types.ts contracts/types.ts
npm run build
# 需要真浏览器时：python tools/serve_verification.py（8098）+ vite 5188 + node tools/browser_smoke.cjs
```

## 6. 明确不做（本期）

- 不接 Control、不发轨迹、不执行机器人；`executed` 恒 `false`。
- 不做 segmentation。
- **不做位姿与关节的手动编辑**（① 类，gizmo 与 Details 关节输入删除），**不做撤销／重做**；
  ② 类只到"面板加／删物体"为止且受阶段闸门约束，③ 类灯光预设留在渲染层
  （三分类落点见 `docs/gateway修改点.md` §2，决议 Q114）。
- 不改 Qwen（8080）与 OLS（8000）端口。
- 不做多用户／鉴权／用户隔离（见 §8）。
- **不做任务运行状态的翻译表**（Q116）：我方不持有 Agent 词表、不按名字判结束、不产出中文状态文案，
  面板原样回显上游 `state`；也**不做健康驱动的自动降级**（Q115）——探活失败只改状态条，不改 job、不解锁。

## 7. 决议（2026-10-20 定稿，原"待确认"六条已收敛，第 7、8 条为当日第四次定稿新增）

| 原编号 | 议题 | 决定 | 依据 |
|---|---|---|---|
| 1 | 手动编辑刻度与摘要范围 | 同日两改后定稿（**Q114**，修正 Q113）：入口三分类——位姿／关节删除、面板增删挂阶段闸门、灯光预设留渲染层；身份是**四元组**，`scene_sub_version` 保留为**可选顶层 uint32**；摘要**包含**全部版本字段，对象为**收到的那份文档**（不删字段，除该键外不加字段） | 流程文档 §1/§3/§12.D1 |
| 2 | JCS 跨语言 fixtures | 由 Brain 交付**非空成对夹具**（含"仅 `now_joint_positions_rad` 变"与"仅 `scene_sub_version` 变"各一例），我方 `tests/test_digest.py` 逐条比对 | `AgentBrain修改点.md` §A2/§A8.B3 |
| 3 | 事务回执／ID 落盘与 unknown 收敛 | 回执与高水位落 **Gateway 磁盘**；ID 改为**增物前申领**；`rejected` 必须有"未部分修改现场"的证据，超时**永不**算拒绝 | 流程文档 §6/§8 |
| 4 | quarantine 解除入口 | 选 **B**：`POST /resolveUncertain`，字段与六步顺序已定稿 | 流程文档 §8.3 |
| 5 | Brain 桩服务与双臂错误码 | 桩服务必须标 `synthetic`；`web_platform_profile_unsupported` 必须附人类可读原因字段 | `AgentBrain修改点.md` §A7.5/§A8.B7 |
| 6 | 第二标签页行为 | **允许连接 `/events`，只读**并提示现场归属；不拒绝握手 | 流程文档 §4/§9 |
| 7 ★ | Agent 服务健康谁来暴露（**Q115**） | **Gateway 汇总暴露**：`/health` 转发（3 秒超时）+ `/api/agent/health` 透传（≤5 秒缓存）+ `/api/health` 补 `agent_service`／`browser`／`assets` 三分项；对方 `/health` 已存在，只需字段定稿与"进程活着即 200"。**健康读数不进 job 视图、不作解锁依据** | 流程文档 §5.3、`gateway修改点.md` §7.2 |
| 8 ★ | 任务运行状态词表归谁（**Q116**） | **归 Brain**：`state` 是任意非空字符串，我方删掉三处按名字的终态判断与类型字段 `AgentJob.status`，面板原样回显；**`terminal` 是唯一停轮询依据**（缺失⇒非终态 + warn），404 与 5 次传输上限不动，`outcome` 五态仍是固定契约 | 流程文档 §7.4、`gateway修改点.md` §7.1、对方工单 §A11 |

## 8. 多用户／鉴权／用户隔离的排期结论：**不提前**

- 部署形态未变：全部服务本机回环、单操作者；规约 §11.2 的"无鉴权（内网调用方）+ 默认 `127.0.0.1`"仍是本期唯一访问控制口径。
- 本期同事要求的"单活动浏览器写入 + 租约 + 事务幂等"已经覆盖了多人共用最容易踩的坑（互踩场景），
  做完之后多用户的紧迫性**下降**而非上升。
- 提前做的成本穿透三层，且会污染联合验收的归因：OLS `uid` 透传（`docs/OLS接口.md:5-11` 已设计，
  但 `editor/src/library.ts` 从不带 `uid`，全部落 `uid=0`）、Gateway 鉴权中间件（现状零机制）、
  Brain 的会话归属校验。在 brain-web/1 尚未跑通时叠加身份体系，出问题时无法区分是协议不一致还是鉴权拦截。
- **最小前身已在本期完成**：browserID 持久化 + Gateway 侧"当前绑定浏览器"概念（P1）。
  这两项不是用户体系，但正是未来接入 `uid` 与多用户隔离的挂载点。
- 将来若要启动，建议独立一卡做三件小事：browserID ↔ `uid` 绑定、`library.ts` 全量透传 `uid`、
  Gateway 写接口按 `uid` 判权限；不自建登录体系。
