# gateway 修改点（现状清单）

> 本文由 2026-10 施工清单提炼（2026-11-05），逐项施工叙述已归档至 docs/works/历史档案/；协议现行定稿见 docs/gateway与Agent新通信流程.md（brain-web/2）。
> 状态词表：已落地 / 部分落地 / 未落地 / 已作废（被 X 取代）/ 待核实。分区沿用原清单 §1–§10，括号为原施工卡 S0–S6（原 §11）。

## §1 规约先行（S1，全部为文档就地修订）

- §3.1/§3.9：新增唯一可选键 `scene_sub_version`（uint32 缺省 0）；四条写路由是 `scene_version` 唯一推进者、浏览器改动只推子版本、`/updateWorld` 改恰好 v+1——已落地
- §4.8 共同口径：按 ①删除/②阶段闸门/③渲染层 三分类重写（Q113 全删稿被 Q114 取代）——已落地
- §4.8.1 光照预设：改语义为渲染层覆盖，不进文档/导出/摘要（Q77 修订）——已落地
- §4.8.2 导出：删光照/位姿两句，改为与 `/getWorld` 逐字节一致；文件名不加子版本（R4）——已落地
- §4.8.3/§4.8.4：gizmo 与 Details 关节输入两节整体撤销，Q79/Q80/Q81 废止；面板纯只读；`RobotAttachment` 覆盖语义保留——已落地
- §4.6 资产面板：保留浏览与加入世界，挂阶段闸门（`setup` 可用、`agent_owned` 置灰+弹窗）——已落地
- §5.2 广播写入：废止，写入只发绑定浏览器；`browser_id` 降级断言、第二 tab 只读——已落地
- §5.4 `scene.applied`：载荷补四元组+`session_phase`；新增帧 `session.phase_changed`——已落地
- §5.5 id 分配权威：按 R7 改写（Gateway 持久水位 + `GET /allocateObjectID` 申领）——已落地
- §5.8 缓存兜底：原写"删 cache 分支"——已作废（被 §6/S3 就地改口取代：缓存保留+龄头，见 §6）
- §5.9 根路径接口：新增 brain-web/2 十项路由 + `/health`、`/api/agent/health`（R9）两张路由表——已落地
- §5.13 错误码：补四枚新码（400/409/409/409）；`browser_scene_revision_conflict` 不实现——已落地
- §5.15 Agent 链路：整节按流程文档 §7 重写（四元组、产物白名单、五态 `outcome`）——已落地
- §5.15 状态映射表：整表作废并替换（R10：`state` 原样回显、`terminal` 唯一终态判据）——已落地
- §5.15 服务健康：新增段（R9：启动即查+15 秒轮询、健康不进 job 视图、不新增 WS 帧）——已落地
- §9.2 常量：新增 `state_dir`；删 `allow_details_joint_angle_edit` 整链；3s/5s/15s 不做常量——已落地
- §10.4 删除清单：gizmo 回到"已删除"；DOM 值订正为 `translate/rotate/scale`——已落地
- §11.2.1 端口 8099：已写入——已落地
- §12 验收：§12.8/§12.8b 重写为三分类验收，新增 §12.17/§12.18，连带订正四处旧 gizmo 判据——已落地
- §13 决议：新增 §13.34（Q109–Q116）；O10/O11 待用户输入、现按"不持久化"走（未实现）——已落地
- `docs/场景文件描述.md` 同步三处（§1 子版本、§4.1 删 gizmo、§4.7 只读）+ §4.5 光照改写——已落地

## §2 三分类入口施工（S0，核心 R8）

- §2.1 ① gizmo 平移/旋转/缩放：renderer/main/world/local_edits/features.json 全链删除；保留 `Box3Helper` 描边与 `worldAabb()`——已落地
- §2.2 ① Details 关节角度输入：全链删除（含 `allow_details_joint_angle_edit` 常量链），面板回只读——已落地
- §2.3 ③ 灯光预设：从"改文档"改为渲染层覆盖（`setLightingOverride`，不触 `World`）——已落地
- §2.3 持锁期冻结灯光：`lightingFreeze()` 只读 `lease`；15 秒 tick 在 `agent_owned` 期重读会话状态（S5-d）——已落地
- §2.4 ② 资产面板/切场景闸门：`chooseLibraryItem`、`Load Scene` 入口加阶段判定 + `#phase-block` 弹窗——已落地
- §2.4 弹窗改"询问"（S6-b，Q122/Q123 收敛文案）：新建会话并重做被拦那一步；不打断 Agent；不用 `disabled`——已落地
- §2.4 `addLocalObject`/`removeLocalObject` 成功后推子版本并 `reportWorld()`；层级面板删除按钮本仓库无 UI 落点、方法保留不造按钮——已落地
- §2.4 `World.scene_sub_version`：本地增删 +1、`toJSON()` >0 才写出、Agent 写回归 0——已落地
- §2.4 阶段三来源接线（S4-b）：`/api/sessionState`、`session.phase_changed`、`X-Session-Phase` 头（只交叉核对）——已落地
- §2.5 保留清单：导出/Load Traj/播放/截图/Auxiliary/相机预览等全部不动；播放不算编辑不推子版本——已落地

## §3 摘要与四元组（S2-a，R5/R6）

- `digest.py`：RFC 8785 规范化 + SHA-256，唯一实现（原稿 TS `digest.ts` 作废，grep 核实无此文件）；ECMAScript 基准校验——已落地
- 摘要输入是浏览器送来的文档对象：不解析语义、不补默认值、不删未知字段——已落地
- `getWorld` 回执扩为 `{world, scene_id, scene_version, scene_sub_version, playback_state}`；`PlaybackState` 三值定稿——已落地
- 与对方成对夹具（AgentBrain §A8.B3）逐条比对——部分落地（我方不等用例与基准 hex 已并入 `test_digest.py`，成对比对待对方夹具交付，是否已比对待核实）

## §4 会话阶段：三层防线（S4）

- S4-a 身份持久化：`BROWSER_ID` 存 localStorage（缺失才生成）+ `clientRequestID` 复用规则（`submissionForSend`/`submissionAfterSend`）——已落地
- 前端持有 `session_phase`：三来源合并，不一致以 `/api/sessionState` 为准（`mergeSessionPhase`）——已落地
- 第 1 层 UI 闸门（S4-c/S6-b）：`CLASS_TWO_ENTRIES` 单选择器清单、`aria-disabled` 非 `disabled`、点击必弹窗——已落地
- 第 2 层 Gateway 权威（S3-a/c）：持久阶段 + 四元组 CAS，`agent_owned` 期子版本推进 ⇒ `manual_edit_forbidden`——已落地
- 第 3 层 Brain 再校验（对方 §A4）——未落地（归对方施工，联调日核；三层防线整体为部分落地）
- 阶段持久化 `var/gateway_state.json`：刷新不丢；转换边只有两条（`/startTask` 受理⇒`agent_owned`、显式新建会话⇒`setup`）；阶段真移动才广播——已落地

## §5 Gateway：身份、持久状态、会话、申领（S3-a/b）

- `gateway_state.py` 落盘 `var/gateway_state.json`：绑定/会话/阶段/相机候选/id 水位/租约/事务回执索引；坏文件挪开再继续——已落地
- `GET`/`POST /sessions`：三道闸（参数→绑定→锁）在转发前；Brain 4xx 原文透传；回话盖四字段不裁剪；恢复同 `sessionID` 不重置阶段——已落地
- `GET /api/sessionState` 与 `GET /brainCapabilities`：只读权威读数、均不转发；前者额外给 `browser_id`/`next_object_id`，后者回八项能力元组常量——已落地
- `GET /allocateObjectID`：原子推进持久水位、号段先写盘再回话；耗尽 409；上限 `2**32-1`；`MAX_ALLOCATE_COUNT=1024`——已落地
- C 档：`/addObject` 未带 id 时 Gateway 先领一个；配对"连接即上报"（onopen 时持有场景才 `reportWorld()`）——已落地
- `apply_state_sync`：抬水位到 max(持久, 文档最大 id+1)；`adoption_verdict` 五规则纯函数——已落地
- 换场景上报不再整条拒（2026-10-22 联调 P1 就地回改）：`setup` 且无锁 ⇒ adopt，`agent_owned`/持锁 ⇒ 拒；不改持久 `camera_id`——已落地

## §6 Gateway：写入定向、原子提交、租约（S3-c/d/e）

- 写入定向：`dispatch` 删广播分支、`captureScene`/`get2DAABB` 补 `browser_id`+`lease_token` 并定向绑定浏览器；未绑定/点名非绑定 ⇒ 503——已落地
- `_write` 广播补四元组+阶段；`scene_digest` 恒 `null`（不复用存量摘要）；`source_browser_id`/`_responders` 删除——已落地
- `get_world` 补全响应头（`X-Scene-Digest`/`X-Scene-Sub-Version`/`X-Session-Phase`/`X-Next-Object-ID`）；live 头与正文同源，不符 ⇒ 502——已落地
- 缓存兜底**已删除**（**Q147**，2026-11-05 改判 S3 那次"就地改口"）：`X-World-Source` 只有 `live` 一个取值，无浏览器 ⇒ `503`、超时 ⇒ `504`，`X-World-Cache-Age` 这个头随之不存在；`world_cache` 仍记录但没有任何读取路由用它——已落地
- `POST /commitWorld`：`transactionID` 幂等 + 四元组 CAS + candidate `strict` 全量校验一次落地 + 恰好 v+1 + 子版本必须 0；回执落 `var/gateway_transactions/`——已落地
- `GET /queryWorldCommit`：只读磁盘不需浏览器在场；查不到 ⇒ `200 {"status":"unknown"}` 不是 404——已落地
- 浏览器先验证再落地：全有或全无只由 candidate 自带 `strict` 触发；普通 `/updateWorld` 逐对象容错不变；不再补发 `state_sync`（D8 半句作废）——已落地
- `POST /acquireSceneLease`/`releaseSceneLease`：四元组指纹、`keepLocked=true`⇒`quarantined`、不实现超时自动解锁；Gateway 只记账，停播/冻结动作在浏览器——已落地
- `POST /resolveUncertain`：六步顺序、结论落盘、任何一步不满足回 `kept_locked`（200，结论不是错误）；现读只抬水位不 `record_scene`——已落地

## §7 Gateway：Agent 转发与结果（S2/S3）

- `start_task` 载荷扩为九字段（+`sceneDigest`/四元组/`sessionID`/`clientRequestID`/`browserID`）——已落地
- `jobs` 路由：`clientRequestID` 幂等缓存；提交前实时取现场（不得用 `state_sync` 缓存顶替）；闸门序 501→形状→幂等→`task_busy`→`session_context_required`→实读——已落地
- 产物白名单代理 `GET /tasks/{taskID}/artifacts/{name}`：四名白名单、禁任意路径、字节/状态码/Content-Type 原样——已落地
- 前端 `clientRequestID` 生成（本轮）+ 复用（S4-a）——已落地

### §7.1 状态词表放开（R10，主体是删除）

- 删 `AGENT_STATES`/`AGENT_TERMINAL_STATES`/`AGENT_UNKNOWN_STATE`/`agent_status_for`；`AGENT_POLL_ERROR_LIMIT=5` 保留（grep 核实已删净）；判据 grep 三处为空并钉成测试——已落地
- `task_state` 回四元组；`terminal is True` 是唯一判据，缺失/非布尔 ⇒ 非终态+warn——已落地
- `start_task` 建 job 不接受 `terminal:true`（忽略+warn，首次轮询为准；原"同样返回 terminal"作废）——已落地
- `poll_agent_tasks_once` 重构：单轮抽 `_poll_one_agent_task`；404 与 5 次上限逻辑不动，落法改 `terminal=True`+`outcome="failed"`——已落地
- job 字段表：`AGENT_JOB_FIELDS` 删 `status` 加 `terminal`/`state_detail`、`message = agent_state or failure_reason or ''`；两份 `types.ts` 的 `AgentJob` 同步 + `AgentOutcome` 五态——已落地
- `agentStatusLabel` 整体删除（S4-d 就地改口取代"退化成回显函数"；grep 核实 logic.ts/main.ts 均无）；`terminalAgentStatuses` 第二份终态表删除，`working` 改 `!job.terminal`——已落地
- `/queryTaskResult` 转发 + `outcome` 五态落地：只有 HTTP 200 算结果；响应体形状我方定稿并已同步流程文档——已落地
- `commit_receipts` 落 job 视图（S3-g 尾巴已收）：账本优先于证词，盘上无回执 ⇒ `unknown`，绝不替 Brain 补——已落地

### §7.2 服务健康转发（R9）

- `GET /health`：转发 Agent 服务，3 秒超时；不可达/超时/非 JSON ⇒ 502 `unreachable`，不冒充 200——已落地
- `GET /api/agent/health`：同一条透传，缓存 ≤5 秒 + `X-Agent-Health-Age`；失败读数同样进缓存（本轮定稿）——已落地
- `GET /api/health` 扩三分项（`agent_service`/`browser`/`assets`）：并发探互不阻塞；`browser_id` 改读持久绑定（S3-b 就地改口）——已落地
- 硬边界：健康读数只进状态条不进 job 视图、不触发 `failed` 落库（三条测试钉住）——已落地

## §8 前端结果展示（S4-d）

- Agent 面板按 `outcome` 分支渲染（`result_view.ts`）：`plan_ready` 固定文案、终态不得出现"执行成功"、澄清不显失败——已落地
- 阶段/锁展示面 `#session-strip`——已作废（2026-10-26 Q123 整条撤下；grep 核实 main.ts 仅剩注释；判据 `newSessionView`/`lightingFreeze` 等保留）
- 任务行文案改上游原文：徽章正文是 `agent_state` 本身，CSS 类由 `terminal`+`outcome` 派生；localStorage 历史行按 `terminal ?? true`——已落地
- 服务状态条：读 `/api/health` 三档，`agentOnline` 删除；stall 计数 N 是本 tab 自己的读数次数（就地改定）——已落地
- `#status-revision`：子版本非 0 时追加 `.{scene_sub_version}`（`sceneRevisionLabel`）——已落地

## §9 相机与采集校验（S5）

- `capture_scene`/`get_2d_aabb` 身份八项逐项校验（`_require_frame_identity`）；身份不符 ⇒ `400 camera_frame_scene_mismatch`（gateway 也产）、字节不符 ⇒ 502——已落地
- 浏览器 `pairedSceneDigest()`：四元组配不上 ⇒ `null` ⇒ 被拒，宁拒不报旧摘要——已落地
- 采集期持锁 + 采集前后摘要相同 + 灯光冻结保证像素可重复——部分落地（gateway 侧 S3-e 齐；V0/V4 摘要对账是 Brain 动作；"真实截图与框同现场"实链断言归 S6 smoke，脚本中未见 ⇒ 待核实）
- 相机候选清单：来自现场 `Camera` 物体、不猜名不取默认、进程内存不落盘；不做 `camera_id ∈ candidates` 闸（选错由 `CAMERA_NOT_FOUND` 404 回答）——已落地
- S6-b 补报：Agent 写入落地后 `adoptWorld` 补一次 `state_sync`（幂等 `idempotent`），修复 `camera_candidates=[]`——已落地
- segmentation 明确拒绝：类型枚举闸 `rgb`|`rgbd` 之外 ⇒ 400，不生成占位数据——已落地

## §10 测试与验收（S6）

- TS 单测删/改/负控制（gizmo/关节组删除、lighting 改覆盖断言、`AgentJob` 无 `status` 类型断言等）——已落地（各卡全绿，最终 vitest 441 支）
- Python 单测（`test_digest`/`test_gateway_state` 71 支/`test_gateway_sessions` 30 支/`test_commit_world`/`test_scene_lease`/`test_capture_identity`/`test_agent_health`/`test_gateway_agent` 改写）——已落地（全量 454+，仅 `test_default_config_file_matches_the_built_in_defaults` 一支已知红等用户裁决）
- 无浏览器探针 `audit_parity.py` 扩 brain-web/2 路由面 62/62 通过；`parity_inventory.py` 清单 28 项——已落地
- 真浏览器 smoke `browser_smoke.cjs` 扩项/清理——部分落地（核实：`/api/health` 断言已在；但脚本仍残留 5 处点击 `[data-tool="translate"]`、2 处 rotate/scale，且无 `.phase-accept`/`#phase-block` 断言，① 类清理与弹窗问句断言未做）
- 一次性栈 `serve_verification.py`（8098，临时状态目录）——已落地
- 发布包：`pack.py` 的 `RELEASE_FILES` 收 `digest.py`/`gateway_state.py`；重打——已落地（核实：distribute/1.0.7-rel 含两文件；该包是否即交付对方核验的 8099 新包 ⇒ 待核实）

## 仍然有效的约束

- 路由注册顺序（坑 2）：`/commitWorld`、`/queryWorldCommit`、租约两条、`/resolveUncertain`、产物代理、健康两条都必须注册在 `/updateTraj` 与静态 catch-all 之前，有测试按名字钉整份路由表。
- 状态持久化位置：唯一真实文件 `var/gateway_state.json`（`main()` 的 `--state-path` 默认值）；事务回执 `var/gateway_transactions/` 一事务一文件；`GatewayState(path=None)` 是易失的，只有 `main()` 与 `serve_verification.py` 两个入口点名文件；一次性栈注入临时目录绝不写真实 `var/`；`var/` 在 `.gitignore`。
- 阶段转换边只有两条且都在 Gateway 受理那一刻：`/startTask` 被 Brain 接受 ⇒ `agent_owned`（幂等只写一次）；`POST /sessions` 显式新建 ⇒ `setup`。恢复同一 `sessionID`、重复 `/startTask` 都不发帧不重置；刷新页面不解禁。
- 会话/任务标识校验：`clientRequestID` 同句重试复用同一 id、受理或 `request_id_reused` 后必换新、其余失败保留；`BROWSER_ID` 持久于 localStorage、读回不重写、空串按没有处理；`transactionID` 白名单形状先于任何磁盘读；`expected` 四元组逐键必填不得缺省成 `None`；`taskID`/`camera_id`/`object_id` 均 uint32；两份 `types.ts`（根与 `contracts/`）必须逐字节相同（坑 6）。
- id 权威（R7/Q112）：Gateway 持久水位 + Brain 增物前申领；`setup` 期浏览器本地分配必须以 `X-Next-Object-ID` 为种子；采纳 `state_sync` 时水位抬到 max(持久, 文档最大 id+1)；`agent_owned` 后浏览器无任何分配点；释放的 id 永不复用；上限 `2**32-1`、单次 `MAX_ALLOCATE_COUNT=1024`、号段回话前写盘。
- 摘要单点与读数纪律（R5）：只有 `digest.py` 一份实现（getWorld 头、jobs 组装、commitWorld 校验三消费者），浏览器不自算，输入不解析语义/不补键；`X-World-Cache-Age` > 1 秒的缓存不得进任何 CAS/写回判定（实读、expected、采集 V0/V4 一律 live），缓存不得冒充 live（浏览器不回仍 504）——前半句今天更硬：**缓存根本不再是一次读数**（**Q147**，2026-11-05：`/getWorld` 只剩 `live`，那个头也不存在了）；写入广播的 `scene_digest` 恒 `null`。
- 版本推进：四条写路由是 `scene_version` 唯一推进者；浏览器 ② 类改动只推 `scene_sub_version`；Agent 写回必把子版本归 0；commit candidate 子版本必须为 0 且目标恰好 v+1（Gateway 产 `gateway_target_version_invalid`）。
- 写入定向（§5.2）：一律定向绑定浏览器，未绑定/点名非绑定/绑定掉线 ⇒ 503；查看者从不收 `browser.request`；第一个 `browser.hello` 认领绑定、同 tab 刷新保留、第二 tab 只读、断开才可接管。
- 闸门形态（R2 + Q123/Q128）：② 类入口置灰不得用 `disabled` 属性（点击委托第一行会吞掉点击），用 `aria-disabled` + 处理器照常跑到弹窗；入口清单以 `CLASS_TWO_ENTRIES` 单一选择器记账；展示面已删但判据仍在（置灰/弹窗/灯光冻结/quarantine 拦答）；两枚显式"新建会话"带 `allowActiveTask`；`POST /api/agent/recover` 浏览器侧无入口、只剩运维直调。
- 终态与轮询（R10/Q116）：`state` 是上游自定义字符串，不得内置枚举/翻译/按名字判结束；唯一终态判据 `terminal` 布尔（缺失⇒非终态+warn）；404 与连续 5 次轮询失败两条上限在 gateway 侧；`outcome` 五态是固定契约；`poll_errors` 不外泄。
- 健康边界（R9/Q115）：健康读数只进服务状态条绝不进 job 视图，探活失败不得判死在跑任务；OLS 探活复用 `GET {library_server_url}/list?type=model` 不新造路由；3s/5s/15s 内置不做配置常量。
- 锁语义：租约闸排在参数闸之后；`X-Brain-Lease` 验票；`keepLocked=true` ⇒ `quarantined`；不实现超时自动解锁；`unknown` 必须保锁不得老化成 `rejected`；把 `unknown` 变成结论是 `/resolveUncertain` 一家的活；回执缺失（UNANSWERED_CODES/502）不写回执。
- 错误码表闭合：四枚新码 `scene_switch_forbidden_after_first_task`/`manual_edit_forbidden`/`gateway_scene_revision_invalid`/`object_id_space_exhausted`（400/409/409/409，Gateway 自产）；`browser_scene_revision_conflict` 不实现；拒绝切换复用 `task_busy` 不开新码；码表相等断言在 `test_error_mapping.py`。
- 相机纪律：候选清单来自现场 `Camera` 物体、不猜名不取第一个；选定与持久化归 Brain；Gateway 不做 ∈candidates 闸；换场景不改持久 `camera_id`、清单换现场即清空；采集要求 `playback_state=stopped` 且持锁，租约复位必须卸 clip（只 pause 不算停）。
- ③ 类灯光预设：只改浏览器怎么画，不进文档/导出/摘要/getWorld；冻结判据只读 `lease`（`lightingFreeze`），`agent_owned` 无租约时照旧可用。
- O10/O11（Gateway 重启后 job 表是否持久、故障矩阵是否涵盖 Gateway 自己）：待用户输入，现按"不持久化"走。
