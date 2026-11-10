# Gateway / WebEditor 侧施工清单（brain-web/2）

- 我方（WebEditor／Gateway，仓库 `F:\web-frontend`）的施工单
- 协议依据：`docs/gateway与Agent新通信流程.md`；场景格式依据：`docs/场景文件描述.md` + 规格 §3/§4.8/§5
- 对方施工单：`docs/AgentBrain修改点.md`（我方是决策方，本文不再出现"等对方确认"的分支）
- 已完成：P0 端口迁移 8080 → 8099（见 `docs/Agent服务集成修改计划.md` §4）

标记：**[ ] 待做**、**[x] 已做**。规约正文一律**就地修订**，历史留在任务卡。
本文每条给"落点（文件:行）→ 改成什么 → 判据"，可直接开工。

> **本轮头条决定（R8，决议 Q114，当日第三次定稿）：浏览器入口按"三分类"处置。**
> 上一稿（Q113）曾把四类入口一律删除并把身份收回三元组；现在的最终口径是：
> **① 删除** gizmo 平移/旋转/缩放与 Details 关节角度输入；
> **② 阶段闸门** 资产面板加入／删除物体与切场景——`setup` 期可用，**用户向 Agent 发出消息后禁用并弹窗**；
> **③ 渲染层本地** 灯光预设——只改浏览器怎么画，**不进文档、不进导出、不进摘要、不出浏览器**。
> 因为 ② 还在，**`scene_sub_version` 与四元组身份保留**，`X-Scene-Sub-Version` 头、
> 三层防线、`manual_edit_forbidden`／`gateway_scene_revision_invalid` 两枚码**一并恢复**。

> **当日第四次定稿（决议 Q115、Q116，R9／R10）**，两条都在任务侧，与场景格式无关：
> **R9 服务健康必须可见**——Brain 的 `GET /health` 我方已核对**存在**（`web/service.py:299-309`），
> 要做的是我方新增 `GET /health` 转发、`GET /api/agent/health` 透传，并把 `agent_service`／`browser`／
> `assets` 三分项并进浏览器启动即查的 `GET /api/health`（今天只有 `{status,mode,uptimeSeconds}`）。
> **R10 运行状态词表归 Brain**——`/queryTaskState` 的 `state` 是上游自定义字符串，
> 我方**删除按名字的映射表**（`local_gateway.py:67-73,126-138`、`editor/src/logic.ts:agentStatusLabel`），
> 终态判据改为响应里的 **`terminal` 布尔**，面板原样回显上游文案。
> 这两条是**净删除 + 一层转发**，不新增错误码（四枚新码不变），也不动 `outcome` 五态。

---

## 0. 本轮决定（原来写成 Q1–Q5 待确认，现按决策定稿）

| 编号 | 议题 | 决定 |
|---|---|---|
| **R1** | `agent_owned` 期是否禁 `Load Traj`／播放 | **不禁**。轨迹不改动世界（规格 **Q33**／§8.5），禁它没有安全收益；但采集与任务执行期必须 `playback_state=stopped` 并持锁（§9）。冻结的是**切场景与面板增删**，不是查看 |
| **R2** | "后续手动修改场景会被禁止"的范围 | **按字面实现**（不再改成"删入口"）：② 类入口在 `agent_owned` 期**置灰 + 点击弹窗**，新会话恢复。① 类才是真删除。不做 feature 开关，不加"仅本地"的第二形态 |
| **R3** | 什么算"切换场景" | `Load Scene`（本地文件）、资产面板的场景预设、`/updateWorld` 换成另一 `scene_id` —— **都算**，`agent_owned` 期一律拒。导出、`Load Traj`、`/getWorld` 回读、视口截图、Auxiliary layers、灯光预设 **都不算** |
| **R4** | 导出文件名 | **不加子版本**，维持 `{scene_name}_v{scene_version}.json`（代码今天就是这个形状，`editor/src/local_edits.ts:97-101`，**零改动**）。子版本只体现在**文件内容**里：`scene_sub_version > 0` 时 `toJSON()` 写出该键，为 `0` 时省略（§3.1 的可选字段口径） |
| **R5** | digest 实现放哪 | **只放 Gateway 一处**（**S2-a 改口：Python `digest.py`**，原稿的 TS 作废 —— Gateway 本身就是 Python，`getWorld` 回头、`/api/agent/jobs` 组装、`/commitWorld` 校验都由它算，TS 那份没有消费者，留着就是死代码或第二处实现）。浏览器**不自算**；与对方的对齐按同一批夹具做，规范形则由 ECMAScript 侧生成的基准 hex 校验。我方内部绝不允许出现第二份摘要实现 |
| **R6** | 是否做 `canonical.ts` | **不做**。协议已定死"Brain 按 `docs/场景文件描述.md` + 规格 §3 的宽容口径重写解析器，摘要对象是我方送出的那份文档"（流程文档 §2/§3，对方施工单 §A1/§A2）。我方导出必须继续**逐字节保真**（规格 §4.8.2），既不为对方补字段，也不为对方删字段 |
| **R7** | 对象 id 权威 | **Gateway 持久水位 + Agent 增物前申领**（流程文档 §6）。规格 §5.5 就地修订。`setup` 期浏览器面板加物**仍本地分配**，但① 必须以 `X-Next-Object-ID` 为种子（不得只从文档现有 id 推），② Gateway 采纳 `browser.state_sync` 时把水位抬到 `max(持久, 文档最大 id + 1)`，③ 进入 `agent_owned` 后浏览器无任何分配点。"新增→删除→刷新复用旧 id"（对方回函 §3.1 反例）由 ①② 关闭 |
| **R8** | 浏览器是否可以改现场 | **可以，但只在 `setup` 期、只能通过 ② 类入口**（决议 **Q114**，2026-10-20；取代当日 Q113 的"完全只读"）。三条理由：① 用户搭现场是本期主用途，全删会把资产面板变成纯浏览；② 位姿与关节的手动改动才是真正危险的（会让 Agent 的 FK 与截图对不上），所以 ① 类删除；③ 灯光只影响像素不影响现场，做成渲染层覆盖就不需要进协议。身份是四元组 `(scene_id, scene_version, scene_sub_version, sceneDigest)` |
| **R9** | Agent 服务的健康状态谁来暴露（**Q115**） | **Gateway 暴露，浏览器只问 Gateway**。对方 `GET /health` 已存在（不是新增接口），我方做三件事：`GET /health` 转发（3 秒超时）、`GET /api/agent/health` 透传（缓存 ≤5 秒、带 `X-Agent-Health-Age`）、`GET /api/health` 扩三个分项（`agent_service`／`browser`／`assets`）。**硬边界**：健康读数**不得进 job 视图**，一次探活失败绝不能把在跑的任务判成失败；OLS 探活复用 `GET {library_server_url}/list?type=model`，不新造路由 |
| **R10** | 任务运行状态词表归谁（**Q116**） | **归 Brain**。`state` 是任意非空字符串，我方不得内置枚举、不得翻译、不得按名字判结束；**唯一终态判据是响应里的 `terminal` 布尔**（缺失 ⇒ 按非终态 + warn）。落点是**净删除**：`AGENT_STATES`、`agent_status_for` 的映射语义、`agentStatusLabel` 的五 token 分支全部去掉，面板显示上游原文，CSS 类改由 `terminal` + `outcome` 派生。404 与"连续 5 次轮询失败"两条既有上限**不动**。`outcome` 五态是结果分类，仍是固定契约 |

---

## 1. 规约先行（改文档再动代码，S1 的全部内容）

| 条目 | 位置 | 改动 |
|---|---|---|
| §3.1 顶层字段 | `docs/Web前端需求规格v2.md` §3.1、`docs/场景文件描述.md` §1 | **[x]** **新增唯一可选字段 `scene_sub_version`（uint32，缺省 0，缺失即合法）**，并写明"只有 `setup` 阶段的浏览器本地增删会推进它；Agent 写回必把它归 0"。`scene_schema_version` 仍为 1，除此之外不加任何顶层字段 |
| §3.9 版本推进 | 同上 §3.9 | **[x]** 四条推进路径（`/updateWorld`／`/updateItem`／`/addObject`／`/removeObject`）**加一句限定**："这四条是 `scene_version` 的唯一推进者；浏览器侧的内容改动一律只推进 `scene_sub_version`"。`/updateWorld` 的 `max(v+1, 提交+1)` 改为 commit 语义（恰好 `v+1`，**多余自增删除**）。手动 Load Scene 仍取文件里的值 |
| §4.8 共同口径 | 同上 §4.8 第一段 | **[x]** 标题改为"**浏览器现场改动与导出**"；四条共同口径按 ①②③ 三分类重写；就地修订记录写"本节曾规定四类本地编辑（Q77/Q79/Q80/Q81），2026-10-20 **Q113** 全删、同日 **Q114** 收敛为三分类" |
| §4.8.1 光照预设 | 同上 §4.8.1、`docs/场景文件描述.md` §4.5 末段 | **[x]** **不撤销，改语义**：从"替换世界里的光照物体"改为"**渲染层覆盖**：预设只决定 `three` 场景里实际建哪些灯，不写 `World`，因此不进 `toJSON()`、不进导出、不进 `/getWorld`、不进摘要；持锁期内冻结为「跟随场景」"。**Q77 就地修订**（废止的是它的持久化与上报，不是可用性） |
| §4.8.2 导出 | 同上 §4.8.2 | **[x]** "因此包含 4.8.1 的光照替换"这半句删除（光照不再进文档）；"包含 4.8.3 的位姿拖动"同样删除（① 类已不存在）。改成"包含当前现场文档（含 `scene_sub_version`，>0 时），与 `/getWorld` 逐字节一致"。文件名规则不变（R4） |
| §4.8.3 gizmo | 同上 §4.8.3、`docs/场景文件描述.md` §4.1 末段 | **[x]** **撤销该小节**。**Q79 与 Q80 就地废止**（Q80 的"任何物体都可以拖"连同它的挂载写回口径一起作废）。`场景文件描述.md` §4.1 那段"gizmo 就地写回 / 带 `RobotAttachment` 的物体被拖时同步写回 `attachment.transform`"改写为"`RobotAttachment.transform` 覆盖 `Transform.position/quaternion`，且**没有任何入口会去改写它**"——注意 §4.8 的**覆盖语义本身不变**（那是文件格式，不是编辑功能），对方 §A1.4 就靠这两段活着 |
| §4.8.4 Details 关节输入 | 同上 §4.8.4、`docs/场景文件描述.md` §4.7 的"前端 Details 面板也能写 `now`"那条 | **[x]** **撤销该小节**，面板回到纯只读读数（显示 `drivers.ts:declaredJointPositions` 那份合并位姿，`(now)` 标记保留）。**Q81 就地废止**；§4.7 第 7 条里"仍能在 Details 里逐关节编辑"改"仍能在 Details 里逐关节**只读查看**" |
| §4.6 资产面板 | 同上 §4.6／§12.7 | **[x]** **保留浏览与"加入世界"**，但整块挂阶段闸门：`setup` 可用，`agent_owned` 置灰 + 点击弹窗（文案见 §2.4）。层级面板的本地删除同样保留并挂闸门 |
| §5.2 广播写入 | 同上 §5.2 | **[x]** 废止广播，写入只发绑定浏览器（**已授权**）；`browser_id` 降级为断言、第二 tab 只读 |
| §5.4 `scene.applied` | 同上 | **[x]** 载荷补**四元组**与 `session_phase`，语义改"通知刷新"；新增帧类型 `session.phase_changed{browser_id, session_phase}` |
| §5.5 id 分配权威 | 同上 §5.5 | **[x]** 按 R7 改写：Gateway 持久高水位 + `GET /allocateObjectID` 申领；`setup` 期浏览器本地分配以 `X-Next-Object-ID` 为种子；释放 id 不复用不变；上限 `2**32-1` |
| §5.8 缓存兜底与 `browser.state_sync` | 同上 §5.8 | **[x]** 删除 cache 分支（**已授权**）；`X-World-Source` 只剩 `live`。**上报载荷仍是整份世界文档**（② 类编辑靠它把子版本与新 id 交给 Gateway，不能降成元数据）；Gateway 采纳时抬 id 水位（R7②），`scene.applied` 广播四元组。连带就地修订 §1.4（缓存→持久化，用途三条、不得回退）与 §5.13 里 Q58 那半句 |
| §5.9 根路径接口 | 同上 §5.9 | **[x]** 新增 `/commitWorld`、`/queryWorldCommit`、`/acquireSceneLease`、`/releaseSceneLease`、`/resolveUncertain`、`/brainCapabilities`、`/sessions`、`/allocateObjectID`、`/api/sessionState`、**`/health`（转发 Agent 服务）与 `/api/agent/health`（浏览器用透传）**（R9）。落点：规约新增两张路由表（brain-web/2 十项 + 前端用的两条），根 `GET /health` 与 `/api/agent/health` 各写成一段；`/queryWorldCommit` 未知事务回 `200 {"status":"unknown"}` 不是 404，`/resolveUncertain` 的 `kept_locked` 同理是**结论不是错误**；并写明四条写路由的响应现在多返回 `scene_sub_version` 与 `scene_digest` |
| §5.13 错误码 | 同上 §5.13 | **[x]** 补**四枚**新码：`scene_switch_forbidden_after_first_task`、`manual_edit_forbidden`、`gateway_scene_revision_invalid`、`object_id_space_exhausted`（流程文档 §10）。`browser_scene_revision_conflict` **不实现**（语义并回 `browser_scene_version_conflict`）。`tests/test_error_mapping.py` 的"表就是契约"相等断言同步扩（**实装随 S2/S3**，规约里已把第四份名单写进 M7-1 那段：四码 ∈ 表、∉ `BROWSER_ERROR_STATUS`，状态码钉死 400/409/409/409——它们是 Gateway 自己raised 的，浏览器不可能上报） |
| §5.15 Agent 链路 | 同上 §5.15 | **[x]** 整节按流程文档 §7 重写（四元组、`session_phase`、产物白名单、五态 `outcome`）；第 1 步"本地编辑过但从未回传的位姿也在里面"改述为"发送时取的就是浏览器当时的文档，② 类改动一定在里面，③ 类改动一定不在里面" |
| §5.15 状态映射表 | 同上 §5.15 第 4 步的表（`:368-379`） | **[x]** **整表作废并替换**（R10）：删"Agent `state` → `status` → 中文文案 → CSS 类"四列映射，改为"`/queryTaskState` 回 `{taskID, state, terminal}`；`state` 原样写入 `agent_state` 显示；`terminal` 是**唯一**停轮询依据；CSS 类按 `terminal`＋`outcome` 取 `is-pending`／`is-done`／`is-failed`"。表下方那两条既有规则里，"未知 `state` 按非终态处理"（`:377`）**改写**为"`terminal` 缺失或非布尔按非终态处理"；"轮询期错误与 5 次上限"（`:379`）**原文保留**。规约里换成一张**字段来源表**（`agent_state`／`terminal`／`state_detail`／`outcome`／`failure_reason`），`AgentJob` 载荷删掉 `status`、`message` 定为 `agent_state or failure_reason or ''` |
| §5.15 服务健康 | 同上 §5.15 开头 + §5.9 | **[x]** 新增一小段（R9）：浏览器启动即 `GET /api/health`，此后 15 秒轮询；`agent_service.status ∈ {ready, degraded, unreachable}`；**健康读数不进 job 视图**，Brain 不可达时正在跑的 job 只按 §7.4 的轮询上限走。规约里的措辞是"健康读数**只进服务状态条，绝不进 job 视图**"，并明确**不新增 WS 帧** |
| §9.2 常量 | 同上 §9.2 | **[x]** `gateway` 段新增 `state_dir`（持久状态目录，默认 `var/`）；一次性验证栈注入临时目录。**`allow_details_joint_angle_edit` 整条删除**（① 类，实装已在 S0-2 删，本次撤的是文档里那段 json 与那条 bullet）；**不新增**灯光相关常量（预设表今天已有，改的是它的作用对象）；顺带写明健康转发的 3 s／5 s／15 s **不做成常量** |
| §10.4 删除清单 | 同上 §10.4 | **[x]** gizmo 那行的"2026-10-15 部分撤销"再撤销一次：gizmo 回到"已删除"状态并注明日期与决议（Q114）。**就地改口两处措辞**：DOM 值是 `translate/rotate/scale`（原文写作 `move/rotate/scale`，那是图标名）；同一行补上 ① 类第二项（Details 关节输入 + `allow_details_joint_angle_edit` 全链）与保留项（`Box3Helper` 描边、`worldAabb()`） |
| §11.2.1 端口 | 同上 | **[x]** 已写入 |
| §12 验收 | 同上 §12.8、§12.8b、新增 §12.17、§12.18 | **[x]** §12.8 的"平移/旋转/缩放自 2026-10-15 起恢复"改回"已删除"；**§12.8b 重写为三分类验收**（含一对成对判据：② 类加物 ⇒ 子版本 +1、摘要变；③ 类切灯光 ⇒ 四元组与摘要全不变），并按 R2 把"禁用不得用 `disabled` 属性"写成正向判据；§12.17 新增 brain-web/2 联合验收 ①–⑥（digest 同源与不动点、四元组推进与归零、阶段三条读数点同源、CAS 与幂等、申领 id 不相交、持久化与 `unknown`/`kept_locked` 回 200）；§12.18 新增服务健康与状态词表验收 ①–⑤（3 秒透传与 502 视图、三分项与"只停 Qwen ⇒ `degraded` 不是 `unreachable`"、未见过的状态字符串原样显示、健康不进 job、浏览器请求面与缓存龄）。**连带就地改口四处仍然生效的旧 gizmo/关节判据**：§12.13 ⑥（原来要求"clip 在场时开 gizmo 仍给 Q80 那句拒绝"，现在是"入口根本不存在"）、§12.14 ⑦（"gizmo 三模式照常"从清单里去掉）、§12.15 ①（"Details 照样能逐关节编辑"改"只读显示"）、§12.16 与 §4.4 的 `hideOverlays()` 清单（去掉 gizmo 一项）；§4.4 与 §12.13 的路点重算叙述一并跟上 |
| §13 决议 | 同上 | **[x]** 新增 **§13.34**（Q109–Q116 八行，每行给"为什么"而不只是"是什么"）：**Q109** 四元组身份与摘要对象；**Q110** 对方按 §3 重写解析器（格式真值在我方，`SceneConfig` 严格 Schema 不是格式）；**Q111** 首任务后冻结切场景与 ② 类增删、阶段由 Gateway 持久持有、刷新不解禁；**Q112** id 申领制与 `2**32-1` 上限；**Q113**（当日二次定稿，按记录保留并注明被 Q114 部分取代）；**Q114** 入口三分类与子版本保留（含"为什么③ 类是 CAS 判据完整的前提"）；**Q115** Agent 健康由 Gateway 汇总暴露、`/api/health` 补三分项且不得影响 job；**Q116** 运行状态词表归 Brain、终态判据改 `terminal`、我方删除状态名常量。**就地标注两处废止关系**：§13.20 标题（Q79/Q80/Q81 废止、Q77 修订、Q78 判据作废）与 §13.21 标题（Q86/Q87 的映射表两半作废，轮询归属与整份发 `scene` 仍成立）。§13.2 追加 **O10/O11**（Gateway 重启后 job 表是否持久、故障矩阵是否涵盖 Gateway 自己）—— 两条**均待用户输入，本轮不实现**，现按"不持久化"走。（Q107/Q108 已被占用，编号从 Q109 起，2026-10-20 核对） |

`docs/场景文件描述.md` 同步三处：§1（新增可选顶层键 `scene_sub_version` 与其推进者）、§4.1（删 gizmo 段）、
§4.7（Q81 那条改为只读查看）。**§4.5 光照段保留但改写为渲染层覆盖**，
**§4.8 的覆盖语义与 §4.7 的合并口径一字不改**——那是文件格式，与被撤掉的编辑功能无关。

## 2. 三分类入口的施工（核心，R8）

### 2.1 ① 类：gizmo（平移／旋转／缩放）——删除

- **[x]** `editor/src/renderer.ts`：删 `PoseTool`（`:42-43`）、`PoseGizmoHandlers`/`PoseGizmo`/`createTransformGizmo`
  （`:46-84`）、`AdapterCallbacks.onPoseEdit`（`:31-32`）、字段 `gizmo`/`poseTool`/`gizmoObjectId`/
  `gizmoNode`/`gizmoDragging`（`:399-407`）、构造期接线（`:461-464`）、`setPoseTool`（`:1270-1273`）、
  attach/detach 逻辑（`:1289-1309`）、`setGizmoDragging`（`:1313-1314`）、`commitGizmoPose`（`:1320-1330`）、
  `orbit.enabled` 里的 `!gizmoDragging`（`:1336`）、`isEditorOnly` 的 `gizmo.helper` 身份比较（`:1375-1377`）、
  `hoveredAxis()` 早退（`:1407-1408`）、`dispose` 里的 `gizmo.dispose()`（`:1774`）、`LocalPose` 导入（`:25`）。
  **保留**：选中描边（`Box3Helper`，Q105）与 `worldAabb()`——它们不是编辑工具。
- **[x]** `editor/src/main.ts`：删 `state.tool`（`:75-76`）、`onPoseEdit` 回调（`:145-149`）、
  `poseContext`/`poseEditable`/`notifyPoseBlock`/`choosePoseTool`（`:249-271`，`:243` 的调用点一并删）、
  `data-tool` 点击分支（`:1159-1160`）、工具栏三把工具的标记（`:126`，`select` 键保留）。
  三个随之无用的 lucide 图标（`Move`/`RotateCw`/`Maximize`）也从导入和 `usedIcons` 里删掉。
- **[x]** `editor/src/world.ts`：删 `setLocalTransform`（`:526-558`，含那段"挂载物体同时写回
  `attachment.transform`"的 Q80 逻辑）。
- **[x]** `editor/src/local_edits.ts`：删 `LocalPose`（`:18-23`）、`poseEditBlock`（`:42-56`）。
- **[x]** `features.json`：删 `viewport.tools.translate`、`viewport.tools.rotate`、`viewport.tools.scale`
  （`:41-44`）；`contracts/types.ts`／根 `types.ts` 的 feature 键类型同步（**坑 6**）——两侧都用
  递归 `Record` 形状，没有逐键联合类型，故本项无需改动，`cmp types.ts contracts/types.ts` 仍全等。

### 2.2 ① 类：Details 关节角度输入——删除

- **[x]** `main.ts`：`editJointAngle`（`:381-395`）、`detailsJointEditEnabled`（`:376-379`）、
  `renderDetails` 的 `joint-edit` 分支（`:364-368`，只留 `readout("Parked pose (rad)", poseText)` 那一支）、
  `data-joint` change 分支（`:1179-1180`）、`local_edits` 导入里的 `jointEditEnabled`/`jointEditorRows`（`:20`）。
  顺带删掉只服务于那排输入框的 `.joint-edit`/`.joint-row` 样式（`styles.css`）。
- **[x]** `world.ts`：删 `setLocalJointPosition`（`:560-577`）。
- **[x]** `local_edits.ts`：删 `jointEditEnabled`（`:70-72`）、`JointEditorRow` 与 `jointEditorRows`（`:58-93`）
  ——只读面板用的是 `drivers.ts:declaredJointPositions`，它给的名字与 `(now)` 标记已经够诊断用。
- **[x]** 常量整链删除：`config/constant_config.json:17`、`local_gateway.py:49`（内置默认）、
  `contracts/types.ts:374` + `types.ts:374`、`README.md:37,187`、`tests/test_gateway_routes.py`
  里"配置文件等于内置默认"那条断言的字段表、`examples/gateway_client_demo.py` 的 runtimeConfig 用例
  （**注意**：该文件有另一条工作流的端口 hunk，按既有做法**只提交本轮这一处**）。
  实测该断言是整字典比较、没有逐键字段表，故 `tests/` 无需改动；README 另有三处 gizmo 叙述随 S0-1 一并订正。

### 2.3 ③ 类：灯光预设——从"改文档"改成"渲染层覆盖"

今天的实现是**改文档**的，必须改造，否则它会推进子版本、进导出、进 `/captureScene` 的"现场"：

- **[x]** `main.ts:306-315 chooseLightingPreset`：不再调 `applyLightingPreset(state.world, preset)`，
  改调 `renderer.setLightingOverride(preset)`；状态栏那行 `showError(...removed/added...)` 文案改成
  "仅渲染层，不改现场"。空选项（"Scene lights"）今天被 `if (!preset) return;` 吞掉，现在它
  就是 `null` = 跟随场景。
- **[x]** `local_edits.ts:30-40 applyLightingPreset` **删除**（它用 `removeLocalObject`/`addLocalObject`
  真删真加灯，是 ② 类通路的冒牌货）；文件头注释（`:1-10`）同步改写。
- **[x]** `lighting.ts`：**保留**常量表与 interface；`presetLightRequest`（`:93`）改为产出
  渲染层灯参数（不再产出 `ObjectRequest`），`beamQuaternion`（`:29`）保留。
  `PresetLight.name` 随 `ObjectRequest` 一起删——不再有物体被创建，名字没有读者。
- **[x]** `renderer.ts`：新增 `setLightingOverride(preset | null)`——覆盖态下**不建**世界文档里的
  `Light` 节点、改建预设的灯；`syncScene()` 每次重建后重新应用覆盖；`null` = 「跟随场景」（默认）。
  覆盖**不得**触碰 `World`，因此 `toJSON()`／导出／摘要／`getWorld` 天然不受影响（这是本条的判据）。
  实现口径：文档灯的 `THREE.Light` 打 `userData.docLight` 并按覆盖态置 `visible`（渲染器的灯表跟
  `visible` 走），预设灯挂在 `simWorld` 下一个**不属于 `nodes`** 的组里；marker 与标签照旧，
  因为文档确实还带着那盏灯。
- **[x]** 持锁期冻结：`acquireSceneLease` 成功 → 浏览器把覆盖复位为 `null` 并冻结下拉框；
  `releaseSceneLease` 后解冻（流程文档 §7.2、§9）。**S0 顺延、S5-d 落地**——租约与阶段在 S3 才存在，S0 没有可挂的状态；
  `applyWorld()` 里的复位先顶住"换现场就回到默认光照"。
  **S3-e 更正本条的到达方式**：原稿写"Gateway 广播阶段/锁状态"，但本协议只有
  `scene.applied`／`agent.job_updated`／`session.phase_changed` 三种广播帧、本期不新增，
  所以锁状态只能由 S4 的 `/api/sessionState` 轮询读出（该读数的 `lease` **不含 `leaseToken`**，
  见流程文档 §7.2）；浏览器要行动时用的那枚票随它自己的 `captureScene`／`get2DAABB` 响应帧到。
  **S5-d 落点（行号核对到 2026-10-21）**：判据只有一处——`session_state.ts:92 lightingFreeze()`，
  它**只读 `lease`**，因此 `agent_owned` 而没有租约时预设照旧可用（正是 §10 真浏览器那行的 R1 负控制），
  而别的 tab 持有的租约同样冻结（要可重复的是像素，与谁点的不相干）。
  `main.ts:333 chooseLightingPreset()` 在预设生效之前先答一句为什么；`main.ts:355 renderLightingFreeze()`
  把已开着的预设收回 `null`（不留"冻结前那一眼"的私有光照）并给下拉框挂 `aria-disabled`——
  **不是 `disabled`**，同 §4 第 1 层那条 R2 口径；它挂在 `refreshSessionState()`（`main.ts:1318`）里，
  即权威读数落地的唯一时刻，解冻走的也是同一条。
  **本条同时补上"轮询"那半截**：原稿说锁状态由 `/api/sessionState` 轮询读出，但 S4 只接了事件驱动的
  几个时机（连接、广播、任务终态、本地编辑回执），而 Agent 取锁落在两次广播之间，所以
  `main.ts:1622 start()` 的 15 秒 tick 现在在 `agent_owned` 期一并重读会话状态；阶段回到 `setup` 即停，
  不新增 WS 帧、不动协议、不进配置常量。**判据**：`session_state.test.ts` 的 `lightingFreeze` 4 支
  （无读数⇒不冻、无租约⇒不冻、`agent_owned` 无租约⇒不冻、两条点名任务的冻结、`quarantine` 单独存在⇒不冻），
  TS 437→**441** 支，`tsc` 零错。**页面观感**（点预设弹出这句话、预设真的收回）仍归 S6 smoke。
- **[x]** `applyWorld()`（`main.ts:1523`）里重置下拉框那行保留，语义变为"新现场带来新的默认光照"
  （同时 `renderer.setLightingOverride(null)`）。

### 2.4 ② 类：资产面板增删与切场景——保留并挂阶段闸门

- **[x]** `main.ts:467-489 chooseLibraryItem`：`kind === "scene"` 分支与 `model`/`robot` 分支
  **都保留**，但在函数入口加阶段判定；`agent_owned` 时不执行任何改动，直接走弹窗。
  实现口径：`const blocked = phaseBlockMessage(state.sessionPhase); if (blocked) { showPhaseBlock(blocked); return; }`
  放在取到 `item` 之后、两个分支之前，所以两条入口共用同一次判定。
- **[x]** 新增统一弹窗 `showPhaseBlock(reason, pending)`：正文第一行固定一句
  "现场已由 Agent 接管。"（**Q122**，2026-10-26 就地收敛：原句"本会话已交由 Agent 接管：这个操作会改动现场，
  要先有一个 setup 会话。"里"会改动现场／要先有一个 setup 会话"是同一件事的第二种说法，删）
  **点击必须有响应**——置灰不等于吞掉点击（这是 R2 与上一稿最大的区别）。
  实现口径：文案常量在 `local_edits.ts:PHASE_BLOCK_MESSAGE`，`#phase-block` 覆盖层挂在
  `.canvas-wrap` 里（`z-index:12`，压在 `#toast` 之上），带一个 `data-action="dismiss-phase-block"`
  关闭按钮；它**不套 `data-feature`**——闸门是正确性而不是可关掉的装饰。已按 8099 真页面验过：
  `agent_owned` 下点 Load Scene 不弹文件框、弹出该文案，点 X 收起。
  **入口本身的"置灰"视觉态顺延到 S4**（§4 第 1 层），且那里有一条硬约束：不能用 `disabled` 属性，
  点击委托 `main.ts:1091` 的 `if (target.disabled) return;` 会把点击吞掉，正是 R2 要消灭的形态。
- **[x]** **弹窗从"告知"改成"询问"（S6-b，2026-10-22 就地改口，覆盖上一稿那句"切场景请新建会话（Agent 面板 → 新建会话）"）**：
  用户口径是"当前处理方法是完全禁止一切操作，改为弹窗提示用户是否要开一个新会话，如果是，开一个新会话并在新会话中加载场景（注：启动新会话不应当影响 AgentBrain 的处理，AgentBrain 服务有可能还在后台执行旧会话的计算）"。
  落点（行号核对到 2026-10-26，**Q122** 就地收敛文案）：`#phase-block`（`main.ts:179`）在关闭按钮之外多了一枚
  `data-action="phase-new-session"`，正文两行——第一行仍是 `local_edits.ts:PHASE_BLOCK_MESSAGE`，
  第二行来自 `local_edits.ts:phaseOfferMessage(pending)`，它按被拦下的那次点击**只问要做什么**：
  `load-scene` 说"新建会话并重新打开场景文件选择框？"，预设场景说"新建会话并重新加载预设场景「名字」？"，
  模型／机械臂说"新建会话并重新添加「名字」？"。两枚按钮（`新建会话`／`取消`）由 `.phase-block` 的
  **单列 grid** 排在正文下方（上一稿是"正文一列／按钮一列"并排）。
  被拦的意图留在 `state.pendingPhaseEdit`（类型 `local_edits.ts:PendingPhaseEdit`），
  `main.ts:showPhaseBlock(reason, pending)` 收下、`main.ts:dismissPhaseBlock()` 点取消即丢弃
  （不会有一次过期意图在下一轮被误重做）、`main.ts:acceptPhaseBlock()` 走**同一道门**
  （`main.ts:createSession()`，`camera_id` 由 Brain／Gateway 从候选清单里自动选 —— 上一稿写的"候选清单＋用户
  点选的 `camera_id`"从未这么实现过，**Q123** 一并把这句改回与代码一致）→成功才收起弹窗并重做那一步。
  **四条口径写死在这里**：① **不打断 Agent**——开新会话只是 `POST /sessions`，Gateway 从不替 Brain 取消任务，
  旧会话的计算照旧跑完（用户特别注意的那点）；这条**从常态文案里撤下**、判据照旧（文案改短不等于行为改变，
  真被拦时由②把原话送回屏幕）；② **拒答原样可见**——租约还在或状态待确认时这道门自己会拦
  （`session_state.ts:newSessionView` 的 `blocked`），网关那侧同一条判据回 `409 task_busy`
  （§8.3：新建会话不是解锁证据）；`createSession` 因此不再自己弹 toast、改回 `{ok, reason}`，
  失败时**理由写回弹窗正文**（五秒就消失的 toast 正是一条"按钮没反应"的错觉，与 §4.6 选弹窗同一个理由）；
  原句"Agent 面板那枚按钮仍走 toast（它身下有状态条）"随 `#session-strip` 一起删除（**Q123**），
  两句都不粉饰成"已为你开好"；
  ③ 新按钮**不引入 `disabled`**，与 R2 同一条理由；
  ④ 重开文件框那次点击**骑在同一次用户激活上**：`POST /sessions` 慢过浏览器的瞬态激活窗口时程序化点击会被挡，
  此时用户再点一次 Load Scene——阶段已经回到 `setup`，那一次直接生效。这条无法在单测里钉，归 S6 smoke。
  判据：`local_edits.test.ts` 的 `phaseOfferMessage` **四支**照其在（Load Scene 分支、预设场景点名、
  模型／机械臂两支共用"重新添加「名字」"），但后两支的断言随句子改写：`Gateway 从不替它取消`（①已撤下常态文案）
  与`照原话被拒绝`（②改由 `blocked` 原话承担）两句**从断言里去掉**，改钉"问句以`新建会话并`开头"与
  "预设场景点名"这两件仍然成立的事；原 `phaseBlockMessage` 两支保留，其中"出路在文案里"那支的断言随新句子从
  `setup 会话` 改成 `Agent 接管`（改的是断言不是判据：句子变了，"文案里有说法"这件事照旧钉住）。`tsc` 零错。
- **[x]** `main.ts:1189-1212` `Load Scene` 分支同闸门；`world.ts` 的
  `addLocalObject`（`:511-513`）／`removeLocalObject`（`:516-518`）**保留**，
  并把两者改为"成功后推进 `scene_sub_version` 并 `reportWorld()`"（今天它们不推进、不上报，
  见 `:505-510` 那段注释——这正是 Q114 要改的口径：**本地改动必须让 Gateway 与 Brain 看得见**）。
  实现口径：推进在 `World` 里（`bumpSubVersion()`，失败的写不推进），上报在调用方——
  `chooseLibraryItem` 从 `world.addObject(...)` 改到 `world.addLocalObject(...)`（返回裸 id，
  不再是 `AddObjectResult`），后随 `reportWorld()`；`addObject` 从此只服务 Agent 写回。
  这正是 §1.3 那条正控制的落点：面板加一个物体 ⇒ 摘要变、`scene_version` 不变、子版本 +1。
- **[x]** `world.ts`：`World` 新增 `scene_sub_version: number`（读自文档，缺省 0），
  `addLocalObject`／`removeLocalObject` 成功后 `+= 1`；`toJSON()` 在 `> 0` 时写出该键，为 0 时省略；
  `scene_version` 的任何推进（Agent 写回）把子版本归 0。
  实现口径：字段在 `WorldMeta`（必填）＋ `WorldFile.scene_sub_version?: UintId`（可选，两份
  `types.ts` 同步）；读经 `readWorldMeta`（非 uint ⇒ 0），`get subVersion()`；归零写在
  `bumpVersion()` 里，`applyWorld()` 整份换 meta ⇒ 带进来的子版本随新现场走。
- **[x]** `main.ts:reportWorld()`（`:1602-1615`）本身不变——② 类改动一定带 `browser.state_sync` 上报；
  **S6-b 起调用方多了一条**：`adoptWorld`（`:1337`）在采纳别人写的那份现场之后也报一次，让候选清单配上新四元组
  （判 `idempotent`，不动版本；见 §9 那条补记）。上报后 Gateway 的回执（水位、四元组）写回状态栏。**回执那半截顺延到 S4**（§4 的 `#status-revision`）。
- **[x]** 层级面板：本地删除按钮今天不存在（核对结论），若 §4.6 补上则同挂闸门。
  核对：`main.ts` 里没有调用 `removeLocalObject` 的入口，`removeLocalObject` 目前只有单测读者；
  方法照 §2.4 的要求保留并推进子版本，等 §4.6 的按钮出现时同挂闸门。
- **[x]** 阶段来源：三条来源在 **S4-b** 全部接线（S0 交付的是闸门本身与弹窗，那时 `state.sessionPhase`
  恒为 `setup`、fail-open）——`/api/sessionState`（`main.ts:1279 refreshSessionState()`，连接、
  广播、任务终态、本地编辑回执与 15 秒 tick 都走它）、`session.phase_changed`（`main.ts:1197`）、
  `X-Session-Phase` 响应头（`main.ts:1285`，只作交叉核对、`reread` 把它送回权威路由，永不覆写）。
  合并判据在 `session_state.ts:36 mergeSessionPhase()`，三条来源的排序与"不一致以 `/api/sessionState`
  为准"都在它的 12 支单测里；§4 第 1 层的置灰（S4-c）与 §2.3 的持锁冻结（S5-d）读的同一份 `state.session`。

### 2.5 三分类之后剩下的东西（防止删多）

- `local_edits.ts` **保留文件**：`sceneExportName`（`:97-101`）+ 阶段闸门要用的判定函数。
- 导出（`exportScene`，`main.ts:278-285`）、`Load Traj`／播放、截图（Q108）、Auxiliary layers（Q105/Q106）、
  相机预览、轨迹路点与幽灵、`/updateTraj`、灯光预设、资产面板加物、层级本地删除 **全部不动**
  （除 §2.3／§2.4 列出的改造点）。
- **播放仍不算编辑**：`showFrame` 只动渲染层与驱动层，不改世界文档，`toJSON()` 里出现的
  是文件／写回声明的位姿（§8.5、Q33），**也不推进子版本**。采集期靠 `playback_state=stopped` + 租约保证现场静止。

## 3. 摘要与四元组（Gateway 单点，R5/R6）

**[x]** 新文件 `digest.py`（**S2-a 就地改口：Python，原稿的 `editor/src/digest.ts` 作废**，理由见 §0 R5）：
RFC 8785 规范化（键按 UTF-16 码元升序、数组保序、`1.0`→`1`、拒绝 NaN/Inf、**不做 Unicode 归一化**）
+ `hashlib` SHA-256 → 64 位小写 hex；`pack.py` 的 `RELEASE_FILES` 收 `digest.py`，发布包里 gateway 才取得到。
落点：`digest.py:canonical_json/scene_digest` + `tests/test_digest.py`（15 例）。规范形与 hex 用
**ECMAScript 侧生成的基准**校验（`JSON.parse` + 码元序 + `String(number)` + SHA-256）：五份真实 JSON
与 22,679 个随机 double 逐字节对过 —— 浏览器与 Brain 都讲 ECMAScript 的数字形，这条是它的直接后果；
基准是常量而不是第二处实现。**唯一一处实现的三个消费者按阶段就位**：`get_world` 的 `X-Scene-Digest`
跟 §6 那组响应头一起装（§5.1 要求"整型头、摘要与正文同源"，拆两次装就会配错对），
`/api/agent/jobs` 组装 `sceneDigest` 与 `/commitWorld` 的 `expected` 比对跟 §5/§6 的持久绑定与
`sessionID` 一起装（S3）。本卡先把**浏览器 `getWorld` 回执**扩到位，S3 一到摘要立刻有人消费。
**[x]** 输入是**浏览器送来的那份文档对象**，不是任何再加工结果：Gateway 对 `scene` 只做
"是不是 JSON 对象 + 尺寸"校验（规格 §5.15.1），**永不解析语义、永不补默认值、永不删未知字段**
（所以 `scene_sub_version` 缺失时**不要补成 0 再算摘要**）。
落点：`digest.py` 只吃一个 `object`，不碰任何模型；`tests/test_digest.py` 的"缺键不补 0"、
"未知字段照算"、"改名即换摘要"三条把它钉住，另加一条 `canonical_json` 的幂等（`digest(parse(canonical(x))) == digest(x)`）。
**[x]** `editor/src/scene_requests.ts:70-71`：`getWorld` 回执从 `{world}` 扩为
`{world, scene_id, scene_version, scene_sub_version, playback_state}`，头由 Gateway 统一算（流程文档 §5.1）。
落点：`contracts/types.ts`/`types.ts` 的 `GetWorldResult` + `PlaybackState`（**本卡定稿的三值词表**：
`playing` = rAF 循环在跑；`paused` = 挂着一条 clip ⇒ 司机停在**采样帧**（第 0 帧也是采样、不是声明）；
`stopped` = 没有 clip，`setTrajectory(null)` 走 `driver.reset()` 回到文件声明的位姿）。
采集要求 `playback_state=stopped`（流程文档 §9），所以 **S5 的租约复位必须走"卸 clip"那条路，只 `pausePlayback()` 不算停**。
回执里 `scene_sub_version` 是**视图层的 0**，文档层没这个键就继续没有（§3.5），两条测试分别断言。
**[~]** 测试用**对方交付的成对夹具**逐条比对（`AgentBrain修改点.md` §A8.B3）：非空真实场景 +
相等／不等用例，含"仅 `now_joint_positions_rad` 变"与"仅 `scene_sub_version` 变"各一例。
我方一侧的先合并：`tests/test_digest.py` 已带这两组不等用例与 ECMAScript 基准 hex，
夹具到位后在同一个文件追加成对比对即可；**"夹具不到齐不合并"这条对 TS `digest.test.ts` 的约束随那个名字一起作废**。

## 4. 会话阶段：三层防线（R2/R3）

**[x]** `main.ts:116-117` `BROWSER_ID` 持久化：`localStorage["webeditor.browserID"]`，缺失才生成并写回。
落点（S4-a）：判定挪进可测的 `local_edits.ts`——`BROWSER_ID_KEY:41`、`persistentBrowserID:54-58`
（读回不为空即用、**不重写**；空串按"没有 id"处理，因为网关要的是非空字符串），
`main.ts:128` 只剩一行 `persistentBrowserID(localStorage, () => crypto.randomUUID())`。
判据：`local_edits.test.ts` 的 `persistentBrowserID` 4 例（首次生成并写回、读回不再调 generate、
空串重新生成、不动别的键）。
**[x]** （§7.1 的另一半，随本条一起落）`clientRequestID` 的**复用**：
`submissionForSend:70-72` 只在"同一句还没结束"时沿用旧 id，换一句话就 generate；
`submissionAfterSend:84-87` 在**受理**与 `request_id_reused` 两种答复后结束这一次提交——前者
"下一条同样的话是新任务"，后者那个 id 已经花在别的内容上、再发只会再撞一次，换新的是唯一出路；
其余一切失败（`task_busy`／`session_context_required`／`501`／`502`／连不上）都没立起提交，
于是 id 留着等重试。`main.ts:1137` 用 `state.agentPending`（`:94`）串起来。
判据：`submissionForSend / submissionAfterSend` 8 例，含"同句复用是**同一个对象**"、
"6 种失败码一律留着 id"、"throws 出来的无码错误也算未结束"。
**[x]** 前端持有 `session_phase: setup | agent_owned`，来源为 `/api/sessionState`（新，刷新后用它找回）、
`/events` 的 `session.phase_changed` 广播、`/getWorld` 头 `X-Session-Phase`；三者不一致以 `/api/sessionState` 为准。
落点（S4-b）：契约先行——`types.ts:263` `SessionPhase`、`:271` `SessionStateView`（字段表与网关
`gateway_state.py:406 session_view()` 逐键对齐，`lease` 用 `:289 SceneLeaseView`＝`public_lease()`
**去 `leaseToken`** 的那份、`:298 SceneQuarantineView`、`:311 SessionPhaseEvent` 并入 `:328 GatewayFrame`
闭合枚举，`contracts/types.ts` 同步拷贝）；`local_edits.ts` 那份重复的 `SessionPhase` 声明删掉、改吃契约。
判定抽成纯模块 `editor/src/session_state.ts`：`parseSessionPhase:22`（词表外的任何值＝**这条读数什么也没说**，
不是 `setup`）、`mergeSessionPhase:33` 按来源定权重——`session-state` 与 `broadcast` 覆盖持有值，
`world-header` **只在还没持有时填空、绝不覆盖**，与持有值冲突时回 `reread: true` 令调用方去问权威那条
（§6.3"不一致时以 `sessionState` 为准"；权威源永不置 `reread`，所以这条路天然收敛不成环）。
接线：`main.ts:59` 初值由 `"setup"` 改 **`null`（还没读过）**、`:62` 存整份 `SessionStateView` 给 §8 的锁状态展示；
`:1098 applyPhase` 是唯一写点、`:1105 refreshSessionState` 读 `/api/sessionState`（失败只留 console.warn，
第 2 层照样拒写）；三个来源各接一处——`:1044` WS 帧分支（broadcast）、`:1001` 每次 socket open（含首连与重连，
关闭期间错过的边只能问盘）、`:1326` `start()`（刷新找回）；`:1121` `scene.applied` 自带的 `session_phase`
按 broadcast 吃下（早于 `adoptingWorld` 重入闸，被合并掉的广播也不丢阶段），`:1135-1138` 的 `/getWorld`
改走新的 `fetchJSON:242`（`json<T>` 只回 body、把 Headers 丢了；现在 `:249` 是它的一行委托）后读头。
判据：`session_state.test.ts` 8 例——两词收、其余全算沉默（含 `"Setup"`／`"agent-owned"`／空串／对象）、
`session-state` 与 `broadcast` 双向覆盖、`world-header` 两个方向都不覆盖且都置 `reread`、`held=null` 时填空不置位、
一致时不置位、**三种来源的沉默读数既不改值也不触发重取**、以及"sessionState 读数永不 `reread`"的收敛断言。
**[~]** `agent_owned` 期**三层**防线，缺一不可：
1. **[x]**（S4-c，S6-b 改口）**UI**：② 类入口禁用**且点击弹窗问"要不要开一个新会话"**（§2.4）——`Load Scene`、资产面板
   场景预设与加物、层级本地删除；`Load Traj`／播放／导出／截图／灯光预设**不**禁用（R1）。
   弹窗给的是出路不是句号：接受即 `POST /sessions`（回 `setup`，不打断 Agent 的旧任务）并重做刚才那一步。
   落点：**一条选择器把"该置灰的"与"会答弹窗的"钉成同一份**——`local_edits.ts:86 CLASS_TWO_ENTRIES`
   = `[data-action="load-scene"]`（闸门 `main.ts:1486`）+ `[data-asset-kind]`（闸门 `main.ts:539 chooseLibraryItem`），
   两者同属 ② 类记账模块，日后加入口只改这一行。`main.ts:1308 renderPhaseLock()` 只挂
   `#app.agent-owned` 视觉类 + 逐入口 `aria-disabled`，CSS 见 `styles.css:52` 那一行（含两个 `:hover`
   覆写，让置灰的卡片不再亮回来）；**处理器照常跑到弹窗**（S0-4 的 `#phase-block` `main.ts:158`，
   S6-b 起里面是两段正文＋两枚按钮）。
   **禁用态不用 `disabled` 属性**：点击委托第一行 `main.ts:1468` 就是 `if (target.disabled) return;`，
   真 `disabled` 会把点击吞掉——正是 R2 要消灭的失败模式。重绘两个时机：阶段**真的移动**才重绘
   （`applyPhase:1289`，`scene.applied` 每次写都带阶段，值没动就不去重打整列卡片），以及面板重建
   （`renderContext:568` 末尾——资产卡片是刚生成的 HTML，晚到的阶段读数不会自己长进去）。
   （以上 `main.ts`／`styles.css` 行号核对到 2026-10-22，S6-b 之后；这一组数字每落一条 FE 项就要重核一次。）
   **施工单原文的第三个入口"层级本地删除"在本仓库没有 UI 落点**：`World.removeLocalObject`
   （`world.ts:540`）无任何界面调用者，`removeObject` 只是 Agent→浏览器的**写动作**
   （`scene_requests.ts:75`，属第 2 层管的 CAS 路径，不是 ② 类本地编辑），故第 1 层只有两个入口，
   该缺失随本条记录、不造一个按钮凑数。
   判据：`CLASS_TWO_ENTRIES` 2 例（两条 ② 入口在内；11 个 R1／相邻控件一律不在内，含
   `[data-asset-kind-nav]` 这个属性名只差一截的近邻——CSS 精确匹配属性名，但这条断言让它不可被"顺手改成前缀匹配"）。
   观感与"点击仍弹窗"的联动只在真浏览器里可证，归 S6 smoke 的 `agent_owned` 一遍（`§12.8b` 那条口径）；
   **本轮不启服务，未做浏览器实测**。
2. **[x]**（S3-a/S3-c）**Gateway（权威）**：持久阶段 + 四元组 CAS；`agent_owned` 期收到 `scene_sub_version` 推进的
   `state_sync` ⇒ 拒绝并回 `manual_edit_forbidden`，现场保持不覆盖（§5、§6；判定见
   `gateway_state.py:84 adoption_verdict`，落点在 §6 那条 `[x]`）；
3. **[ ]**（归对方）**Brain（防御）**：同一判据再校验一次（对方 §A4）——联调日核。
**[x]** 阶段持久化在 `var/gateway_state.json`，刷新页面不丢；`POST /sessions` 显式新建／切换才回 `setup`。
落点：阶段存在 `gateway_state.py`（`set_phase` `:277`），写它的只有 `main()` 的 `--state-path` 与一次性栈。
转换边只有两条，且都在"Gateway 受理"的那一刻：`jobs`（`local_gateway.py:960`）里 `/startTask` 被 Brain
接受 ⇒ `agent_owned`（幂等，只写一次），`create_session` 显式新建会话 ⇒ `setup`。
`_announce_phase`（`:927`）**只在阶段真的移动时**广播一条 `session.phase_changed`
（载荷 `{browser_id, session_phase}`）：恢复同一 `sessionID`、重复 `/startTask` 都不发帧。
**仍归 S4**：前端三条读数（WS 广播／`/api/sessionState`／`X-Session-Phase` 头）的接线与置灰联动本轮未动，
本轮交付的是"三个读数有同一个源头"，S4 只负责让浏览器去读。（S4-a 已落这条的**身份**半边：
`browserID` 进 localStorage、`clientRequestID` 的复用；**S4-b 已落读数接线**——见 §4 的读数条，含 `scene.applied`
自带的第四份 `session_phase`；**S4-c 已落置灰联动**——见上面第 1 层。§4 至此我方全部到位，
剩下的阶段可见性只在 §8 的展示面（绑定浏览器／租约持有者／`quarantined` 提示），**S4-d 已落、"新建会话"入口
由 S5-c 补齐，§8 那条已打勾**（该展示面已于 2026-10-26 由 **Q123** 整条撤下，判据与拦答照旧，见 §8 那条的更正）。
第 3 层（Brain 再校验）仍是 `[ ]`，联调日核。）

## 5. Gateway：身份、持久状态、会话、申领

**[x]** 新增 `gateway_state.py`（或 `local_gateway.py` 内）落盘 `var/gateway_state.json`：
绑定 `browserID`、`sessionID`、`session_phase`、`camera_id` 候选清单、**对象 ID 高水位**、
租约表、事务回执索引。一次性验证栈（`tools/serve_verification.py`）必须注入**临时目录**，
绝不写真实 `var/`。
落点：新模块 `gateway_state.py`（键表 `STATE_KEYS` `:39`，`camera_id` 存候选清单、`lease`／`quarantine`
先占位给 S3-e、`scene_identity` 存四元组）。写盘走 `tempfile.mkstemp` + `os.replace`（`:178`），
读盘对坏文件的做法是**挪开再继续**（`gateway_state.json.corrupt-<epoch>` + `LOGGER.error`），
不是崩溃也不是静默清零；`session_phase` 不认识的值 ⇒ 回落 `setup` 并告警。
`var/` 进 `.gitignore`。**就地定稿（重要）**：`GatewayState(path=None)` 是**易失**的——不读盘、`save()`
空转。理由是当时有 23 处测试在构造 `StandaloneGateway()`，若默认落盘它们会共享同一个
`var/gateway_state.json`，绑定会从一个测试漏到下一个。因此只有**两个真实入口**点名文件：
`main()` 的 `--state-path`（默认 `ROOT/var/gateway_state.json`，`:1310`）与
`tools/serve_verification.py`（注入临时根目录，绝不写真实 `var/`）。
**认领／刷新规则**（`_register_browser` `local_gateway.py:962-996`）：第一个 `browser.hello` 认领绑定，
同一个 tab 刷新（同一个 `browser_id` 重连）**保留**绑定，第二个 tab 只作 viewer；
被绑定的 tab 断开后绑定才被接管——这条与 §4 的"刷新不丢阶段"是同一件事的两面，因为阶段就存在
这个文件里，不在内存里。三条都有单测钉死。
**[x]** `GET /sessions` / `POST /sessions`：创建／恢复／显式切换绑定，合并 Gateway 侧绑定视图后转发
Brain 同名端点；忙碌或 `uncertain` 时拒绝切换。
落点：`sessions`（`local_gateway.py:636`）按方法分派——GET 回 `GatewayState.session_record`
（`gateway_state.py:326`，绑定视图不含现场，那是 `/getWorld` 的活），POST 走 `create_session`（`:642-715`）。
**三道闸全在转发之前**，顺序即代码顺序：参数闸（`browserID` 缺失／空 ⇒ `400 MISSING_PARAM`、非字符串 ⇒
`400 INVALID_PARAM`；`camera_id` 不是 `0..2**32-1` 的整数 ⇒ `400 web_uint32_required`）→ 绑定闸
（另有 tab 持有绑定且在线 ⇒ `409 browser_new_session_required`；存活会话里改相机 ⇒ 同一码）→ 锁闸
（`lease`／`quarantine` 未清 ⇒ `409 task_busy`，消息点名是哪把锁、并写明只有 `POST /resolveUncertain` 能清）。
**就地定稿**：拒绝切换复用既有 `task_busy`，不为它开第六个错误码——§10 的码表已经闭合，"忙碌"在这套协议里
只有一个词；新会话**必须**带 `camera_id`（缺 ⇒ `400 MISSING_PARAM`），因为 §9 要求采集前相机已定。
**合并的样子**：Brain 回 ≥400 且是 JSON 对象 ⇒ **原文透传**且本地零改动（不绑、不开会话、不改阶段）；
2xx 但 `sessionID` 缺失／非字符串 ⇒ `502 UPSTREAM_ERROR`（一个没有 id 的会话无法被刷新后的浏览器找回）；
成功则 `bind_browser` ＋（id 变了才）`start_session`，同 id 恢复只走 `bind_camera`，随后 `_announce_phase`。
回话是 Brain 原载荷＋Gateway 盖回的四个字段（`sessionID`／`session_phase`／`browserID`／`camera_id`），
**不裁剪**——Brain 自己加的字段原样留给浏览器。**恢复同一 `sessionID` 绝不重置阶段**（§8.3 的同一口径：
换会话才是解锁，重复进入不是）。GET 与 `/brainCapabilities` **都不转发**：绑定与阶段是 Gateway 的权威读数。
**[x]** `GET /api/sessionState?browser_id=`：回 `{session_phase, lease, quarantine, scene_id, scene_version,
scene_sub_version, sceneDigest}`，供刷新后的浏览器决定 ② 类入口是否置灰。
落点：`session_state`（`local_gateway.py:597-604`）→ `GatewayState.session_view`（`gateway_state.py:313`）。
**就地定稿**：读数里额外给 `browser_id` 与 `next_object_id` 两个字段——§5 的字段表是"至少要回这些"，
而刷新后的浏览器第一件事就是知道自己是不是那个被绑定的 tab，第二个想知道的是下一个 id 从哪儿开始；
两个都不给的话它只能再发一次请求去猜。`?browser_id=` 只是**查询者**的名字，不改变返回的绑定。
**[x]** `GET /brainCapabilities?browser_id=`：`protocol: "brain-web/2"`，capabilities **八项**
（流程文档 §5.2，含 `sub_versioned_scene_identity` 与 `setup_only_local_edits`）。
落点：`brain_capabilities`（`local_gateway.py:717`）＋ 常量 `BRAIN_WEB2_PROTOCOL`／
`BRAIN_WEB2_CAPABILITIES`（`:92-96`）。八项是一个**元组常量**而不是散在响应里的字面量，§5.2 的表因此
能一眼对回代码；`?browser_id=` 只是查询者署名，回话里的 `browserID` 是**当前绑定**——与
`/api/sessionState` 同源，两个读数不可能对"谁持有现场"给出不同答案。该端点不转发。
**[x]** `GET /allocateObjectID {browserID, count}`：原子推进持久水位并返回号段；耗尽返回
`409 object_id_space_exhausted`；上限 **`2**32 - 1`**。申领方只有 Brain；
Gateway 转发 `/addObject` 且 body 未带 id 时**自己先领一个**再把确定的 id 交给浏览器（R7）。
落点：`allocate_object_id`（`local_gateway.py:606-634`）＋ `GatewayState.allocate_object_ids`
（`gateway_state.py:286`）——号段在**回话之前**写盘，所以申领方领完就崩也拿不回这批 id，
这正是"释放的 id 永不复用"的意思。四道闸按此顺序：`MISSING_PARAM` → `503 NO_BROWSER_CONNECTED`
→ `409 browser_new_session_required`（来申领的不是绑定 tab）→ `INVALID_PARAM`（`count` 为 0 或超上限）。
**两条就地定稿**：① 上限 `2**32 - 1` 是**高水位本身**，最后一个能交到调用方手上的 id 是 `2**32 - 2`
——发完它水位停在 `2**32 - 1`，再申领一次即 409，`uint32` 的边界因此不需要额外算术；
② 新增 `MAX_ALLOCATE_COUNT = 1024`（`gateway_state.py:37`）——原稿只防了跨 `2**32` 的耗尽，
没防"还没有现场就能用一个 `count=4e9` 把网关撑爆"，这条不需要任何前置状态就能打死进程，必须堵。
**[x]** **C 档**：`/addObject` 未带 id 时 Gateway 先领一个（R7，Q112）。本轮与"连接即上报"配对落地——
单独做网关侧会倒退：浏览器连上时若不上报现场，网关水位对此刻已加载的场景一无所知、从 0 开始发号，
撞上已有 id 只会把今天能成功的调用变成 `409 ID_ALREADY_EXISTS`。
落点：`add_object`（`local_gateway.py:1249-1261`）——body 无 `object_id_in_scene` 且绑定 tab 在线时
`allocate_object_ids(1)[0]` 注入，无绑定 tab 则**不申领**（照 §5.5 的四道闸，`503` 先于发号，
未绑定的调用方刷不出空号段）；browser 仍判重，撞上回 `ID_ALREADY_EXISTS`，该 id 就此作废（§6）。
前端配对：`connectWebSocket` 的 `onopen`（`editor/src/main.ts:979-984`）在 `browser.hello` 之后
`if (state.world.objectCount > 0) reportWorld()`——只在真持有场景时上报，空场景报了只会被 §1.2 拒成
`browser_new_session_required`、徒留一条 warning。价值场景是"网关重启而浏览器仍挂着场景"的重连：
重报把水位重新喂给网关，`X-Next-Object-ID` 不再落后。判据：`test_caller_apis`
`test_add_object_preclaims_the_next_id_when_the_caller_omits_it`（fresh 水位 0 ⇒ 注入 id 0、水位升到 1）
与 `test_add_object_passes_a_caller_id_through_unchanged`（带 id 不重打，水位从回报的 id 抬升）。
**[x]** `apply_state_sync`（`local_gateway.py:1264-1297`）：除缓存文档外，把水位抬到
`max(持久, 文档最大 id + 1)`；`agent_owned` 期遇到子版本推进 ⇒ 拒绝（§4 第 2 层）。
落点：判定抽成纯函数 `adoption_verdict`（`gateway_state.py:104`），§1.2 的五条规则一次说清
`adopt`／`idempotent`／`manual_edit_forbidden`／`browser_scene_version_conflict`／
`browser_new_session_required`，路由层只做"采纳或拒绝"。**拒绝的样子**：不缓存、不抬水位、
不广播 `scene.applied`，只留一条 `LOGGER.warning`；§14 的帧表是闭合的，本轮不为此新增帧类型，
浏览器按 §4 的口径去 `/api/sessionState` 核对——**不一致时以 `sessionState` 为准并重取现场**。
`/api/sessionState` 里 `agent_owned` 期被拒的读数就是"面板置灰"的依据。
采纳时广播的 `scene.applied`（`_scene_applied_frame` `:1299-1313`）已带完整四元组＋`session_phase`；
`_write` 那条广播的四元组补齐与 `scene_digest` 归 S3-c（见 §6）。
**[x]**（2026-10-22 联调第 1 条 P1，就地回改规则文本）**换场景的那次上报不再被当成非法**：原稿第 4 条把
"`scene_id` 变化"整条判成 `browser_new_session_required` 并**丢弃这份上报**，于是浏览器载入
OLS 场景 3 之后 `GET /getWorld` 已经答 scene 3/v3，`/api/sessionState` 与水位却仍留在 scene 1/v3；
Brain 拿实时四元组去 `/acquireSceneLease` 就吃到 `409 browser_scene_version_conflict`。
更糟的是这条拒绝顺手把 §9 的候选清单清空了——而"新建会话"正是第 4 条给用户的唯一出路，
出路自己把通往它的路拦死了（三条各自都对，接起来死锁）。
改法（就地改 §1.2 第 4 条的**主语**：那枚码说的是会话该重开，不是上报该丢）：
`adoption_verdict` 加第四枚入参 `scene_held`（`gateway_state.py:104`），
换场景只在 `setup` **且无租约／无隔离**时采纳（`adopt`，水位＋摘要＋`camera_candidates` 一起跟走），
`agent_owned`（§4 第 2 层）或现场被占着（§9 的锁禁止换场景）仍旧回 `browser_new_session_required`；
`scene_held` 由 `GatewayState.scene_held`（`gateway_state.py:264`）给出，两个调用点各传一次
（上报 `local_gateway.py:1276`、实时读 `:1357`，后者原本就只抬水位不挪存量，现在同一把尺子）。
**没做的一件事**：换场景不去改持久会话的 `camera_id`——相机绑定归 Brain（§9 第 3 项），
gateway 猜一份"这个 camera_id 在新现场里还算不算数"就是第二份真值；面板因此照常列出新现场的候选，
用户点"新建会话"重开一次才把绑定交回去。
**判据**：`tests/test_gateway_state.py` 49→**71 支里 +5**：`AdoptionVerdictTests` 三支纯函数
（`setup` 换场景⇒`adopt`、`setup`＋`scene_held`⇒拒、`agent_owned`⇒拒，外加"同现场的推进不受 `scene_held` 影响"一支负控制）
＋路由面三支（换场景后 `scene.applied`/`sessionState.scene_id`/`camera_candidates=[4,5]` 三处一起跟着走、
租约期拒、`agent_owned` 期拒），原 `test_a_reported_scene_id_change_is_not_adopted` 就地翻成正控制。
Python 全量 448→**453** 支，仍只有 `test_default_config_file_matches_the_built_in_defaults` 一支红
（§6 末注等裁决的常量差）。真浏览器那条"载入另一场景 ⇒ 新建会话可点 ⇒ Brain 申领不再 409"归 S6 smoke。

**判据**：`tests/test_gateway_state.py` 49 支，四组——`StateFileTests`（缺文件／坏 JSON 挪开／非对象／
未知阶段回落）、`DocumentReadingTests`（`object_ids_in_document` 只认 `object_id_in_scene`、布尔不算、
`scene_identity` 缺子版本按 0）、`AdoptionVerdictTests`（§1.2 五条各一支）、`PersistentGatewayTests`
＋`CaptureForwardingTests`（认领／刷新保留／第二 tab 只作 viewer／断开后接管、`sessionState` 字段表、
三处抬水位、`agent_owned` 期 `manual_edit_forbidden` 含"警告落日志"与"不给 watcher 广播"、
号段块与持久化、400/503/409/耗尽/巨量 count、采集载荷带 `browser_id`＋`lease_token` 且不问第二 tab）；
`ReleaseBundleTests` 钉 `pack.RELEASE_FILES` 含 `gateway_state.py`。

**判据（S3-b）**：`tests/test_gateway_sessions.py` 25 支，两组——`SessionRouteTests`（23 支：GET 读绑定与两把锁、
POST 成功时转发载荷点名 `browserID`＋`camera_id`、回话四字段是 Gateway 的而 Brain 原字段**不裁剪**、
Brain 自己的 4xx 原文透传、2xx 无 `sessionID` ⇒ `502 UPSTREAM_ERROR`、上游不可达与未配 URL
（`502`／`501`）两种情况都**不留半条会话**、三道闸各一支（缺 `browserID`／非字符串、新会话缺 `camera_id`、
`camera_id` 非 `uint32`、存活会话抢相机、绑定 tab 在线、租约、隔离）、阶段边三合一：新建 ⇒ `setup` 且发一条
`session.phase_changed`、`/startTask` 被受理 ⇒ `agent_owned` 且再发一条、第二个任务与恢复同会话**都不发帧**、
未被受理的任务不占用现场）、`HealthBindingReadoutTests`（2 支：两个 tab 在线时 `/health` 的 `browser` 子项
仍只点名持有会话的那一个；从未 `browser.hello` 的连接不算数）。夹具 `FakeBrainService` 只实现 `/sessions`
与 `/startTask`。

## 6. Gateway：写入定向、原子提交、租约

**[x]** `GET /captureScene`／`GET /get2DAABB` 的转发载荷补 `browser_id` 与 `lease_token`，
并**定向**绑定浏览器（流程文档 §9.1 的 V0–V5 序列与四道闸）。
落点：`capture_scene`（`:1350-1374`）、`get_2d_aabb`（`:1376-1391`）——`browser_id` 取查询参数，
缺省回落持久绑定；两者都把 `self.state.lease.leaseToken` 一起交给浏览器，好让"这一帧是在租约下拍的"
可以在响应里被证明。**行为变化**：省略 `?browser_id=` 的多标签页调用方今天只会问到自己绑定的那一个 tab，
不再广播——这是 §9.1 的要求（一张图必须能说出它是谁拍的），也意味着 `test_capture.py`／`test_bbox.py`
里"两个 tab 都收到请求"的旧断言已按新口径改写。
**[x]** `dispatch`：去掉"无 `browser_id` 就广播"的分支，写入一律定向绑定浏览器；
未绑定 ⇒ `503 NO_BROWSER_CONNECTED`。`_browser_targets` 对写操作只接受单目标。
落点：广播分支已删，`_browser_targets`（`local_gateway.py:1114-1137`）是唯一取目标处——绑定为空 ⇒ 503、
点名非绑定 tab ⇒ 503（文案点明"查看者从不收 `browser.request`（§5.2）"）、绑定 tab 掉线 ⇒ 503；
`dispatch_with_source`（`:1144-1185`，`:1149` 处 `targets = self._browser_targets(browser_id)`），再无一刀广播。
**行为变化（原稿"第二 tab 赢一次 `getWorld` 就回滚了刚加载的场景"这条坑）**：删掉竞争后坑 32 结构上不复存在，
前端四元组比较成了新的去重手段。判据：`test_ws_protocol`／`test_traj_api`／`test_caller_apis`／
`test_capture`／`test_bbox` 里"两个 tab 都收到请求"的旧断言全部改写为"非绑定 tab 收 503 或干脆什么都不收"。
**[x]** `_write`：广播载荷补四元组与阶段。
落点：`_write`（`:1393-1405`）→ `_identity_after_write`（`:1407-1437`）→ `_scene_applied_frame`
（`:1037-1052`）。广播帧的键是 `scene_digest`（蛇形，WS 帧非 HTTP body，规格 §5.4），
`source_browser_id` 已删（§5.4：一个绑定下它只会点名绑定 tab，换网关就漂移）。**同一支还手尾**：
`WriteOutcome` 的第三个字段 `browser_id` 与 `self._responders` 记账一并删除——§5.4 回源取消后无人读它，
而 `_responders` 只在 `dispatch_with_source`（写入）处被 pop，capture／getWorld／traj 的应答只写不读、
每条留一份残留，是个随请求增长的漏；`_apply_browser_response` 的 `browser_id` 形参随之去掉。
**就地定稿**：写入广播的 `scene_digest` 恒为 `null`——网关没读过落地文档，§5.1/§3 禁止把存量摘要配到一个非它来的版本上；
写后再 `getWorld` 回读会把最多 100 MB 搬运两遍去宣布一个"每个读者本就要重取"的消息（Q111：帧是"去重取"，
不是"这是现场"）。写入省下的回读由一次水位抬补上（§6 事件①：Agent 刚造的对象无论有没有人读都存在，
不进持久水位就是 `/allocateObjectID` 会重发的 id）。判据：`test_caller_apis`
`test_write_announcement_never_reuses_a_stored_digest`（先 `state_sync` 存一个真摘要，再写 ⇒ 帧摘要仍 `null`）。
**[x]** `get_world`：补齐流程文档 §5.1 全部响应头（`X-Scene-Digest`、
**`X-Scene-Sub-Version`**、`X-Session-Phase`、`X-Next-Object-ID`）。
落点：`get_world`（`:1439-1469`）＋`_world_response`（`:1092-1112`）＋`_adopt_live_world`（`:1065-1090`）。
所有头都描述**这一次读数**：live 分支的头来自文档本身重算的摘要，浏览器回报的扁平四元组只作交叉核对，
任一不符 ⇒ `502 GATEWAY_FRAMING_ERROR`；live 读数额外带 `X-Browser-ID`／`X-Playback-State`，
`X-Next-Object-ID` 取 `state.next_object_id`。`X-World-Cache-Age` **只给缓存读数**（live 无年龄可报，
带年龄会诱发比较两种不存在的"新鲜度"）。
**S3 就地改口（原稿"删除 `X-World-Source: cache` 分支"作废）**：缓存兜底**保留**，但必须同时给
`X-World-Cache-Age: <秒>`，且**年龄 > 1 秒的缓存读数不得进任何 CAS／写回判定**（`/startTask` 前的
实时取现场、`commitWorld` 的 `expected`、采集时序 V0／V4 一律 live）；浏览器不回仍走
`504 BROWSER_TIMEOUT`（预算 `browser_request_timeout_seconds`＝10 秒，不是采集的 15 秒），
不得把缓存冒充 live。理由：删兜底会让"无浏览器在线"的调用方从"能看见上次现场"退化成"什么都看不见"，
而协议要防的是**拿旧读数当证据**，不是防读数本身——口径已同步改进流程文档 §5 表行与 §5.1。
**判据（S3-c）**：受影响五支模块全绿——`test_caller_apis` 22、`test_ws_protocol` 24、`test_traj_api` 19、
`test_capture` 15、`test_bbox` 16。新增六支：live 全头集且 `X-Scene-Digest` 等本次正文摘要、无龄头
（`test_live_get_world_emits_the_full_header_set_from_one_read`）；缓存读数带龄头＋来源 `X-Browser-ID`、
无 `X-Playback-State`（`test_cache_get_world_labels_its_age_and_origin_and_has_no_playback`）；回报扁平字段
与自身文档冲突 ⇒ `502 GATEWAY_FRAMING_ERROR`（`test_get_world_refuses_a_reply_that_disagrees_with_its_own_document`）；
写入广播不复用存量摘要（`test_write_announcement_never_reuses_a_stored_digest`）；`/addObject` 无 id 预领
＋带 id 原样透传（`test_add_object_preclaims_…`／`test_add_object_passes_a_caller_id_through_unchanged`）。
按 §5.2/§5.8 新口径改写的旧扇出断言：`test_capture`／`test_bbox` 的"点名一个 viewer 就定向到它"
改成"点名非绑定 tab ⇒ 503、两个 socket 都不收请求"；`test_caller_apis`／`test_traj_api` 的"两个 tab 都收到写入"
改成"只有绑定 tab 收到 `browser.request`，另一个只收（或不收）`scene.applied` 通知"。TS 侧：`SceneApplied`
契约去 `source_browser_id`＋`scene_digest` 蛇形键（`cmp types.ts contracts/types.ts` 通过），
`adoptWorld` 改四元组比较去重，tsc／vitest 371／`npm run build` 全绿。

**[x]** 新增 `POST /commitWorld`：`transactionID` 幂等 +
`expected{scene_id, scene_version, scene_sub_version, sceneDigest}` CAS + 整份 candidate 交绑定浏览器
**先全量校验后一次落地** + 目标必须恰好 `v+1` + candidate 子版本必须为 `0`（否则
`gateway_scene_revision_invalid`）+ **不额外自增**；
回执 `{transactionID, browserID, status, scene_id, scene_version, scene_sub_version, sceneDigest, rejected_objects}`
落 `var/gateway_transactions/`。
落点：`local_gateway.py:commit_world`（四道闸按"越便宜的越靠前"排：形状与子版本闸 → 回执闸 → CAS 闸 → 目标闸，
`transactionID` 的白名单形状先于任何磁盘读，`expected` 用 `_expected_identity` 逐键必填而不是缺省成
`None`——缺 `sceneDigest` 读成 `None` 会与一份真存过 `None` 的现场相等，CAS 就白做了）；
载荷 `{"world", "strict": True, "expected_version"}` 经 `dispatch_with_source` 定向到 `browserID`；
落地分支才 `record_scene` + `_cache_world` + 广播（回执先落盘再广播，否则 Brain 在广播还在飞的窗口里查会读到
`unknown`）。回执三态由 `TransactionReceipts`（`gateway_state.py`，一事务一文件、`mkstemp`＋`os.replace`
原子写）承接；`_persist_receipt` 把 `OSError` 映射成 `500 INTERNAL_ERROR`（§9.2 第 ② 条：不得静默降级成
"只存内存"）。**回执缺失与拒绝分开**：闸内 4xx、`NO_BROWSER_CONNECTED`／`BROWSER_TIMEOUT`（`UNANSWERED_CODES`）
与 `GATEWAY_FRAMING_ERROR` 三支一律**不写回执**——写 `rejected` 就是替"我们不知道它落没落"下一个结论；
浏览器自己答出来的拒绝（`ok:false` 或 `rejected_objects` 非空）才落 `rejected`，且带**未改动的**四元组作为
"零落地"的证据。
**就地定稿**：① 目标不是恰好 `v+1` 这一判由 **Gateway** 产 `400 gateway_target_version_invalid`，而规格 §5.13
尾段原本把它与 `web_uint32_required` 一起列成"Brain 直接回、gateway 原样透传"——名字与含义一字不改，
只是生产者从一侧变两侧（candidate 一旦下发就可能落地，闸门必须在派发之前；Brain 侧同判据留作第二层）。
§5.13 尾段与流程文档 §10 那行同步就地改写，§12.17② 的"版本冲突拒收"补上码名。
② 磁盘回执多带一个内部键 `candidate_digest`，响应里由 `_receipt_view` 剥掉：`rejected` 回执的四元组是
**未改动的现场**而不是被拒的那份文档，没有 candidate 自己的摘要就没法判"同事务异内容 ⇒ 冲突"，
而 §5.9 那行的响应形状不许新增字段。
判据：`tests/test_commit_world.py` 22 例——落地一支同时钉下发形状（`strict is True`、`expected_version`）、
响应体、持久四元组、水位抬到 `10`、`world_cache`、回执文件存在、广播带**真**摘要；子版本闸、CAS 闸、
目标闸（三个 subTest 覆盖 `v+2`／同版本／另一场景）各自断言"400/409 且未下发"；必填表 9 行；
`transactionID` 形状表 7 行并断言工作区只剩 `gateway_state.json`；无绑定 tab ⇒ 503 且**连目录都不建**；
超时 ⇒ 504 且查询读回 `unknown`；应答与 candidate 不符 ⇒ 502 且同样 `unknown`；浏览器拒绝 ⇒ 回执带原四元组、
不广播、`/queryWorldCommit` 与 POST 响应逐字段相等；重发同内容 ⇒ 回原回执且**不再有第二个 `browser.request`**；
重发异内容 ⇒ 409；被拒事务不得重放成提交；`unknown` 是 200 且只有三个键；回执跨 Gateway 重启；
坏文件 ⇒ 500 而不是 `unknown`；两条路由注册在通配静态之前（坑 2）。
`tests/test_gateway_state.py` 另加 10 例：`is_transaction_id`／`is_scene_digest` 的形状表、内存态往返、
一事务一文件、目录里不留临时件、缺文件读成 `None` 且不建目录、非 JSON／非对象／表外 `status`／
读不出 ⇒ `TransactionReceiptError`、写不进磁盘 ⇒ `OSError` 而非静默降级。
**[x]** 新增 `GET /queryWorldCommit`：读持久回执，浏览器刷新后仍可核对。
落点：`local_gateway.py:query_world_commit`——只读磁盘，**不需要浏览器在场**（这正是它存在的理由：
事务的答案在 Gateway 手上，不在那个可能已经刷新的 tab 里）；缺参 400 `MISSING_PARAM`、形状非法 400
`INVALID_PARAM`，两者都不碰磁盘；查不到 ⇒ `200 {"transactionID", "status":"unknown", "reason":…}`，
不是 404、不带四元组（§8.1：回执缺失是关于**我们磁盘**的事实，不是关于现场的陈述，且 `unknown` 必须保锁、
不得按定时器老化成 `rejected`）。路由与 `/commitWorld` 一起注册在 `/updateTraj` 与通配静态之前。
判据：并入上述两支测试（跨重启那例走的就是这条路由；`test_the_transaction_pair_is_registered_before_the_static_catch_all`
按名字比整份路由表）。全套 `python -X utf8 -m unittest discover -s tests` **354** 例，
仅 `test_default_config_file_matches_the_built_in_defaults` 一支红（`config/constant_config.json` 的
`traj_waypoint_stride_frames: 5` vs 内置 `10`，与本轮无关，等用户裁决，不动它）。
**[x]** 浏览器侧**先验证再落地**：`world.ts` 现在 `replaceObjects` 直接把结果写进 `this`
（`:424-447`、`applyWorld` `:459-468`），改成 probe 全绿才 `replaceState`，保证任何拒绝零落地；
`rejected_objects[]` 的 `index` 用**提交数组下标**（规格 §3.6、Q59）。
**就地定稿**：原稿"改成 probe 全绿才 `replaceState`"若无条件生效，会连带改掉 `/updateWorld` 的
§3.6/Q59 逐对象容错——规格 §5.9 那行返回的正是 `ok:true` + 新版本 + `rejected_objects`，前提就是
好的子集已经落地。所以全有或全无只由 candidate 自带的 `strict` 标记触发：`applyWorld`
（`:472-489`）在 `strict && rejected.length > 0` 时**不 `replaceState`、不动 `meta`、不抬水位**，
把**改动前**的 `scene_id`／`scene_version`／`object_count` 连同整份 `rejected_objects` 回给 Gateway；
普通 `/updateWorld` 与 Load Scene 不带标记，行为一字不变。标记走
`browser.request{action:"updateWorld", payload:{world, strict:true}}`（`UpdateWorldPayload` 新增可选
`strict`）——它是**请求载荷**字段，不是 §1.2 第 6 条禁止的场景文档顶层字段。浏览器**不再为
candidate 补发 `browser.state_sync`**（D8 原稿那半句作废）：Gateway 手上就是它刚发下去的那份文档，
摘要由 `digest.py` 就地算，再传一遍只是把 Q111 省掉的百 MB 往返原样加回来。
落点：`editor/src/world.ts:applyWorld`、`editor/src/scene_requests.ts:case "updateWorld"`、
`contracts/types.ts`＋根 `types.ts:UpdateWorldPayload`。同步口径：流程文档 §8.1（`strict` 三条＋
不广播拒绝回执＋不重报）、D8 行、规格 §5 新路由表 `/commitWorld` 行。
判据：`world.test.ts` 新增 3 例（拒绝即零落地并回改动前读数、被拒 candidate 不得花掉一个 id、
全绿 candidate 一次落地）＋`scene_requests.test.ts` 1 例（同一份文档带／不带 `strict` 的两种结果）；
tsc 0 错、vitest **375**、`cmp types.ts contracts/types.ts` 无输出。
**[x]** 新增 `POST /acquireSceneLease`／`releaseSceneLease`：校验绑定 + **四元组**现场指纹，
停/拒轨迹播放、冻结切场景、**灯光预设复位并冻结**（§2.3），返回 `leaseToken`；后续请求验 `X-Brain-Lease`；
`keepLocked=true` ⇒ `quarantined`；**不实现超时自动解锁**。
落点：`local_gateway.py` `_lease_fingerprint:1495-1530`／`acquire_scene_lease:1532-1577`／
`release_scene_lease:1579-1615`，租约闸 `_require_brain_lease:1908-1931`（调用点
`capture_scene:1948`／`get_2d_aabb:1971`，排在参数闸**之后**，见流程文档 §9.1 的闸序）；
状态侧 `gateway_state.py` `acquire_lease:292`／`release_lease:303`／`resolve_lock:326`／
`public_lease:392-404`；路由注册 `:2113-2114`，紧跟 `/queryWorldCommit`、在 `/updateTraj` 与静态
兜底之前（坑 2）。**范围切分**：Gateway 这一侧只做记账（谁持锁、over 哪份四元组、票面归属），
"停播／冻切场景／灯光复位并冻结"三个动作是浏览器的，落在本单 §2.3 那条**延后到 S4** 的项里——
本协议只有 `scene.applied`／`agent.job_updated`／`session.phase_changed` 三种广播帧，
租约状态只能靠 `/api/sessionState` 轮询到达前端，所以 `public_lease()` 把 `leaseToken` 抹掉后
浏览器仍能从 `captureScene`／`get2DAABB` 的响应帧里拿到它要行动的那枚票。
判据：新增 `tests/test_scene_lease.py`（47 例：acquire 的幂等／`task_busy` 两分支／quarantine 挡 acquire／
指纹不符／未绑定 503，release 的五种 400 与 `keepLocked` 两种去向，租约闸的"没头 409 且不派发"／
"旧票 409"／"参数坏先报参数"，加 `create_app` 的路由序断言）＋`tests/test_gateway_state.py` 6 例
（票面在两个读数视图里都被遮、`keepLocked` 才留 quarantine 记录、结论两种都落盘、退会话后留阶段、
重启后租约仍在、旧状态文件读作 `last_resolution: None` 且水位 `12` 不动）。
**[x]** 新增 `POST /resolveUncertain`：按流程文档 §8.3 定稿字段实现六步顺序，
结论与证据写进持久状态；任何一步不满足返回 `kept_locked`。
落点：`local_gateway.py` `resolve_uncertain:1665-1761`（六步的读序即写序：①无锁在场 ②回执读不出／
浏览器不是活动那个 ③`late_write_excluded=false` ④live 读失败或无 `world`——四步都在
`_finish_resolution:1811-1827` 落盘后回 **200**）、`_uncertain_evidence:1626-1653`＋`_digest_or_none:1618`
（证据形状）、`_resolution_verdict:1763-1809`（④ 的判定：账本优先于证词、现读优先于两者；
矛盾⇒`quarantined`，不足⇒`kept_locked`，吻合⇒`resolved`）、`_resolution_answer:1655-1663`
（四元组始终随答，`sessionID` 只在 `resolved`+`new_session` 为 `null`）；路由注册 `:2115`。
**第 ④ 步只作证据**：现读只 `raise_water_mark`、绝不 `record_scene`，本路由因此不是改变现场的第二条路。
判据：`tests/test_scene_lease.py` 的 `ResolveUncertainTests` 22 例（含 14 行形状表、六步逐步拒、
三种结局各自的触发集、`resolved` 同清 lease 与 quarantine、`new_session` 退会话不改阶段、
live 读只抬水位不采纳）；tsc 0 错、vitest 375、`cmp types.ts contracts/types.ts` 无输出、
`python -X utf8 -m unittest discover -s tests` **408 例**仅
`test_default_config_file_matches_the_built_in_defaults` 一支红（等用户裁决，与本单无关）。

## 7. Gateway：Agent 转发与结果

**[x]** `AgentRelay.start_task`（原 `local_gateway.py:289-300`）载荷从 `{command, scene}` 扩为
`{command, scene, sceneDigest, scene_id, scene_version, scene_sub_version, sessionID, clientRequestID, browserID}`。
**[x]** `jobs`（原 `:392-421`）：`clientRequestID` 由前端生成并复用，Gateway 做幂等缓存；
提交前向绑定浏览器**实时**取当时现场（`browser.request{action:"getWorld"}`）并校验头自洽，
**不得**用 `state_sync` 缓存顶替（会把用户最后一次面板加物吞掉，流程文档 §14.6 第 1 条）。
**[x]** 新增产物白名单代理 `GET /tasks/{taskID}/artifacts/{name}`（四个名字白名单，禁任意路径）。

落点：`TaskContext:317-339`（七个线名只在 `start_task_fields` 拼一次；`scene_id`／`scene_version`／
`session_id` 三处可空是协议本允许的，不是这里的兜底）、`LiveWorld:342-349`、
`AgentRelay.start_task:463-`（上游九字段＝`{command, scene}`＋`**context.start_task_fields()`）、
`AgentRelay.artifact:525-`（管道：状态码／字节／`Content-Type` 原样带回，不经 `request()` 的 JSON 读法）、
`jobs:891-979`（闸门序：`501 AGENT_UNAVAILABLE` → `command`/`scene` 形状 → 幂等 → `task_busy` →
`session_context_required` → 才发 C2 实读）、`_client_request_id:1439-`、
`_read_live_world:1279-`（`/getWorld` 与提交共用同一次读；四元组取自文档自身 §5.1，不取浏览器
随手并上的平铺字段）、`agent_requests:623`（与 `jobs_state` 同命的进程内幂等表，§13 O10）、
路由注册 `:2340`（在静态 catch-all **之前**，坑 2）。
判据：`tests/test_gateway_agent.py` 57 例——`test_start_task_relays_the_command_and_the_scene` 逐字
对上上游九字段、`test_the_live_read_is_what_travels_not_the_body_scene`（body 里那份 `scene` 与实读
那份故意不同，出门的是实读）、`test_a_live_read_that_reports_two_versions_of_one_moment_starts_no_task`
（平铺字段与文档不一致 ⇒ 502，任务不产生）、`task_busy`／`request_id_reused`／`session_context_required`
各一支、产物流 5 支（字节原样、二进制 `Content-Type`、白名单外含 `%2e%2e%2f` 与 `scene_patch%2f..%2fsecret`
零上游调用、非 uint32 `taskID`、Brain 的 404 保持为 Brain 的 404）、
`test_the_artifact_route_is_registered_before_the_static_catch_all`；
`tests/test_gateway_sessions.py` 的两任务一支改成 `task_busy` → `terminal` → 放行；
`tests/test_agent_health.py` 的 `submit()` 带上实读。tsc 0 错、vitest 375、
`cmp types.ts contracts/types.ts` 无输出、inventory 重跑（新路由入表，`tests/test_caller_demo.py` 29 例绿）、
`python -X utf8 -m unittest discover -s tests` **458 例全绿**（本轮联调回归）。
**前端那一半本轮只落了"生成"**：`AgentTaskSubmission.clientRequestID` 进契约（types.ts 与
contracts 两份逐字节同），`main.ts:1137` 每次提交带一个新 `crypto.randomUUID()`——不补这一行，
HEAD 的浏览器会对每一发提交都吃 `409 session_context_required`。**"复用"由 S4-a 补上**（同一条
命令的重试沿用同一个 id，见上面 §4 那条 `submissionForSend`/`submissionAfterSend`），那与
localStorage 的 `browserID` 确实是同一次改动。

### 7.1 状态词表放开（R10，Q116）——本节主体是**删除**

**[x]** `local_gateway.py:65-76`：**删 `AGENT_STATES`（`:67-73`）、`AGENT_TERMINAL_STATES`（`:74`）、
`AGENT_UNKNOWN_STATE`（`:76`）三个常量**，`agent_status_for`（`:126-138`）整个函数删除。
`AGENT_POLL_ERROR_LIMIT`（`:79`）**保留不动**——那是传输上限，不是状态词。
落点：三个常量与 `agent_status_for` 已删；`AGENT_POLL_ERROR_LIMIT = 5` 原样留在 `:64`。
新增 `AGENT_OUTCOMES`（五态）与 `AGENT_ARTIFACT_NAMES`（四白名单），加上六个取值助手
`agent_state_text`／`agent_terminal`／`agent_state_detail`／`agent_outcome`／`agent_reply`／
`agent_artifact_names`——判据集中在助手里，字节级只用 `is True`。
**[x]** `AgentRelay.task_state`（`:302-309`）返回值从 `(status, state_text)` 改为
`(status, state_text, terminal, state_detail)`：
```python
# 现在（:309）：只取字符串
return status, str(payload.get("state") or "")
# 改成：字符串原样保留，终态只看上游布尔
return status, str(payload.get("state") or ""), payload.get("terminal") is True, \
       payload.get("state_detail") if isinstance(payload.get("state_detail"), str) else None
```
`payload.get("terminal") is True` 这一句就是全部判据：**缺失、`null`、`"true"`、`1` 一律算非终态**
（上游没明说结束就不许替它说），并 `LOGGER.warning` 一次。
落点：`task_state` 现返回四元组（`:342-349`），判据集中在 `agent_terminal`（`:122-135`）：
非布尔一律 `False` 并 warn 一次（`"Agent reply carries no boolean \`terminal\` (%r); keeping the task running"`）。
**[x]** `start_task`（`:291-300`）**建 job 时不接受 `terminal:true`**：收到就忽略、记 warning，
终态一律以第一次轮询为准（流程文档 §7.4 第 8 条）。
**就地修订（本轮落地时定夺）**：原文写"同样返回 `terminal`"作废——规则 8 的"忽略 + warn"在
`start_task` 内部就走完了，往返回元组里再塞一个恒为 `False` 的字段是死重量。
现签名 `start_task(...) -> tuple[int, str, str | None]`，即 `(task_id, agent_state, state_detail)`，
收到 `terminal:true` 时 warn `"Agent /startTask replied terminal:true for task %s; the first poll decides (§7.4)"`。
**[x]** `poll_agent_tasks_once`（`:435-463`）：跳过条件 `job["status"] in AGENT_TERMINAL_STATES`
改为 `job["terminal"] is True`；`:454-461` 那段"token 比较 + settle"改为"字符串与布尔都相同 ⇒ 不广播，
否则 `_settle_agent_job(...)`"。**404 分支（`:447-449`）与 `_note_agent_poll_error` 的 5 次上限
（`:465-472`）逻辑不动**，只把落法改成 `terminal=True` + `outcome="failed"` + `failure_reason` 存上游文本。
落点：`poll_agent_tasks_once`（`:498-511`）只剩"跳过 `terminal is True` + 循环 + `try/except BrowserError`"，
单轮逻辑抽到 `_poll_one_agent_task`（`:513-545`）。抽函数不是美化：终态那两次读（result／reason）一旦
抛 `BrowserError`，原写法会整轮中断、把后面的 job 一起饿死；现在一次传输失败只记给当前 job
（`_note_agent_poll_error`，`:547-556`；上限落法 `agent_state="" / terminal=True / outcome="failed" /
message=failure_reason=f"{error.code}: {error.message}"`）。非终态的比较同时看 `state` 与 `state_detail`，
只变 detail 也广播。404 分支不读 result／reason，直接以 `terminal=True + outcome="failed" +
failure_reason=上游文本` 落定。
**[x]** `AGENT_JOB_FIELDS`（`:82-83`）：`status` **删除**，新增 `terminal`（布尔）与 `state_detail`
（可空）；`agent_state`／`message`／`failure_reason` 保持。`_agent_job_view` 同步。
`message` 的取值从"`agent_state or status`"（`:413`）改成"`agent_state or failure_reason or ''`"。
落点：`:72-75`，同时预下 `outcome`／`reply`／`plan`／`metrics`／`artifacts`（下一条）。
`_agent_job_view`（`:494`）按字段表投影，`poll_errors` 依旧不外泄；建 job 时
`"message": agent_state`——那时既没有 `failure_reason` 也没有空串可替，等价于新规则的初值。
**[x]** `types.ts:179-195` 与 `contracts/types.ts` 同一处（**坑 6**，两份必须逐字节相同）：`AgentJob`
删 `status: string`，加 `terminal: boolean` 与 `state_detail: string | null`；
那段注释里"`status` is the token the panel's CSS classes and terminal check speak"与
"the five Chinese states of `docs/gateway与Agent服务通信流程.md`"两句**改写**为
"`agent_state` 是上游自定义字符串，是我方唯一显示来源；`terminal` 是唯一终态判据（流程文档 §7.4）"。
落点：两份已 `cmp` 逐字节相同；另新增导出 `AgentOutcome`（五态字面量联合），
`outcome: AgentOutcome | null`、`reply/plan/metrics: … | null`、`artifacts: string[]`。
**[x]** `jobs` 路由（`:394-421`）的 `_settle_agent_job`（`:473-480`）签名去掉 `status` 参数。
落点：`_settle_agent_job`（`:558`）现为 `(*, agent_state, terminal, message, state_detail=None,
outcome=None, reply=None, plan=None, metrics=None, artifacts=None, failure_reason=None)`，
无一是位置参数——写广播字段时用关键字，少一个漏一个都编译不过去。
**[x]** `editor/src/logic.ts:agentStatusLabel`：**删掉五个 token 的映射分支**（`logic.test.ts:202` 那条
"五个 token 出中文"的断言随之作废，只留"原样回显／空值回显空"两支），函数退化为
"原样回显 `agent_state`，空值回显空"；CSS 类不再由状态名派生，改由 `terminal`＋`outcome`
给 `is-pending`／`is-done`／`is-failed`（`editor/src/main.ts` 的渲染分支同批改）。
落点：`AGENT_STATUS_LABELS` 表整个删除，函数体一度剩一行 `return state ?? ""`；样式改在
`styles.css` 里以 `.is-done`（绿）／`.is-failed`（红）两条规则承接，`.is-pending` 沿用基色琥珀。
**S4-d 就地改口（原"退化成回显函数、留两支单测"作废）**：回显函数本身就是 `escapeHtml(agent_state)`
的一步，留着它只会让人以为那一层还认得某些状态名 —— `agentStatusLabel` 连同 `logic.test.ts` 的
`describe("Agent task status")`（4 支）**整体删除**，"原样回显一个词表外的新词"改由 `result_view.test.ts:72`
钉（`jobRow(...).state`），终态判据两支并入 `:115` 起的 `jobWorking / jobBadgeClass` describe。
§12.10 末尾与 §12.18④ 里"`logic.test.ts` 保留原样回显三支"那句同步作废：`logic.ts` 已无此函数，
可测的回显只剩 `result_view`。
**[x]** `editor/src/main.ts:1119-1121` 的 `terminalAgentStatuses = new Set(["success","failed"])`
**删除**——这是前端自己维护的第二份终态表，正是 Q116 要消灭的东西；`main.ts:527` 的
`working` 判定改用 `!job.terminal`。
落点：类名派生抽成两个提手函数 `agentMessageWorking`／`agentMessageClass`（消息行与操作行共用，
不再各写一遍终态判断）。
**[x]** `main.ts:27` 的 `AgentMessage`：`status?: string` 换成 `agent_state?: string` +
`terminal?: boolean`，`updateAgentJob`（`:1118`）改拷贝 `job.agent_state`／`job.terminal`。
落点：字段按 camelCase 落本地消息（`agentState`／`terminal`／`outcome`），`updateAgentJob`
同时拷 `job.outcome`；提交失败的补行直接写 `{ terminal: true, outcome: "failed" }`。
**一条必要的例外**：消息列表会写进 `localStorage`（`:527` 末尾那行），刷新后读回来的历史行没有
`terminal` 字段——显示层对**本地历史**按 `terminal ?? true` 处理（否则旧任务永远转圈），
对本次 job 视图仍按 `terminal === true` 才算结束。这条只影响 spinner，不参与任何协议判定。
**[x]** 新增 `/queryTaskResult` 转发与 `outcome` 五态落地：`terminal==true` 之后取一次 result +
一次 failureReason，把 `outcome/reply/plan/metrics.model_calls/artifacts/commitReceipts` 落 job 视图并广播。
落点：`AgentRelay.task_result`（`:351-366`）——**只有 HTTP 200 算结果**（Brain 在未完成的
任务上答 202，那是"还没有结果"的陈述，当结果读就会给出一个"没有产出"的终态），
非 200 或响应体不是对象都只 warn 后答 `None`，job 仍按状态文本落定、只是没有 `outcome`
（结果读不到 ≠ 任务没结束）；`BrowserError` 不吞，
交给 `poll_agent_tasks_once` 记成一次传输失败。终态那一轮先 result 后 reason，两者各一次，
然后**一帧**广播。~~**`commitReceipts` 一项本轮不下**：`commitWorld` 与回执要到 §7/S3 才存在，
现在无处可抄；已把 `outcome/reply/plan/metrics/artifacts` 五项落全，S3 给 job 视图加
`commit_receipts` 时回来勾这条的尾巴。~~
**尾巴已收（S3-g）**：`commit_receipts` 落进 job 视图，口径是**账本优先于证词**——Brain 的
`commitReceipts` 只提供"该查哪些事务"的名字（`agent_transaction_ids:220-` 只取 `transactionID`，
§8.1 文件名白名单不过的直接丢掉），每一项由 `job_commit_receipts:1539-` 从**本方磁盘**读回并按
`GET /queryWorldCommit` 的同一形状（`_receipt_view`，剥掉内部 `candidate_digest`）发布；
盘上无此回执 ⇒ `{transactionID, status:"unknown", reason: NO_RECEIPT_REASON}`，回执读不出
（半截文件、非对象、状态不在 `committed/rejected`）⇒ 同样只落 `unknown` 并记下原因，
**绝不**因为 Brain 说了 `committed` 就替它补一份回执，也**绝不**在这条只用于展示的读上抛 500——
把 `unknown` 变成结论是 `resolveUncertain` 一家的活（§8.3），一次轮询不该被一个坏文件打死。
同一轮对账本只读盘、不再问 Brain。
**就地定稿（我方是决策方）**：`/queryTaskResult` 响应体形状此前没有约定，本轮按
`{outcome, reply, plan:{commands}, metrics:{model_calls}, artifacts:[{name,…}], commitReceipts:[…]}`
钉死，其中 `artifacts` 容忍裸字符串元素，gateway 只把四个白名单名字（`scene_config`／
`scene_patch`／`commands`／`query_result`）以 `string[]` 发布——白名单外的名字（含任何服务端路径）
在这里就丢掉，不让每个查看者再筛一遍。已同步进流程文档 §7.3 与 Brain 施工单。
**判据**：`grep -n "已创建\|已进入队列\|进行中\|任务完成\|任务失败\|AGENT_STATES"` 在
`local_gateway.py`、`editor/src/logic.ts`、`editor/src/main.ts` 里**必须为空**（`tests/` 里的替身除外）。
已跑，三处为空；`tests/test_gateway_agent.py::test_no_source_file_names_the_old_state_vocabulary`
把这条判据本身钉成了测试（替身里的中文词由 `state()` 造，不再有源码依据）。

### 7.2 服务健康转发（R9，Q115）

**[x]** 新增 `GET /health`：向 `agent_service_url + /health` 请求一次，**超时 3 秒**，响应体原样透传；
不可达／超时／非 JSON ⇒ `502 {"status":"unreachable","detail":"…"}`（不得返回 200 冒充健康）。
落点：`AgentRelay.service_health`（`:437-467`）返回 `HealthProbe(status, body, payload, detail)`——
`body` 是上游的**字节**，透传路由直接把这段字节连同上游的状态码交出去（`agent_health` `:556-558` → `_health_view` `:579-590`），
所以"不裁剪、不改写、不重排"是靠不碰它来保证的；`payload` 同一份东西解出来给 `/api/health` 用。
`detail` 只在**根本没有读数**时才有值：`agent_service_url` 为空、连不上、3 秒不回、回的不是 JSON——
四种都落 502，一种都不落 200。`HEALTH_PROBE_TIMEOUT_SECONDS = 3.0` 是内置值，不进常量配置
（规约 §9.2：做成配置项只会多一处能配错的地方）。两条新路由都注册在静态兜底**之前**（坑 2）。
**[x]** 新增 `GET /api/agent/health`：浏览器用的同一条透传，缓存 **≤5 秒**，命中缓存时响应头带
`X-Agent-Health-Age: <秒>`；Brain 字段**一个都不裁剪、不改名**（裁剪权在 Brain）。
落点：`agent_health_cached`（`:560-577`），缓存就是 `self.agent_health_probe` + 一个单调时钟。
**失败读数同样进缓存**——这条原文没写，本轮定下来："unreachable"也是关于那个服务的一条读数，
而一个正在死的服务恰恰扛不住"每个标签页每次轮询都探它一次"；相应地命中缓存的 502 **也带**
`X-Agent-Health-Age`（`_health_view` `:579-590`：两条分支共用同一次头部写入），因为"不可达，4 秒前读的"和
"不可达，刚刚读的"对运维是两个问题。
**[x]** `health`（`GET /api/health`，`:518-539`）从 `{status,mode,uptimeSeconds}` 扩为
`{…, agent_service:{status,detail}, browser:{connected,browser_id}, assets:{reachable}}`：
`agent_service.status` ＝ Brain 分项里 `service/qwen/browser/assets` 全好 ⇒ `ready`，任一不好 ⇒ `degraded`，
取不到 ⇒ `unreachable`；`browser.connected` 读现有 WS 客户端表；`assets.reachable` 用
`GET {library_server_url}/list?type=model`（`editor/src/library.ts:227-230` 的既有路由，不新造）。
三分项**并发探、互不阻塞**，任一分项超时不影响整体 200（这条路由的主语是"Gateway 活着"）。
落点：`health`（`:518-539`）一次 `asyncio.gather` 出 `agent_service` 与 `assets` 两条真探活，
`browser` 读内存表；分类函数 `agent_service_health`（`:274-291`）是纯函数，`AGENT_HEALTH_GOOD`
（`:84`）把四个好值写成一张表。**这里保留了一次按值的比较，和 §7.1 放开词表不矛盾**：
`state` 是 Brain 自由发挥的运行状态名，而这四个是健康契约里**已经定稿的取值**（流程文档 §5.3），
新值出现的意思就是"这一项不好"，`degraded` 正是该给的读数。`degraded` 的 `detail` 一次性把
所有不好的项点名（`qwen=unavailable, browser=not_bound`），不是只报第一个。
**S3-b 就地改口（原"只在恰好一个浏览器在场时给名字、两个及以上给 `null`"作废）**：`browser_id` 改读
`state.browser_id`，并且**只有它还在线时才报**（`bound in self.browsers`，否则 `null`）——绑定是持久状态里
那一条，而"连着但没会话"和"没连着"对状态条是两件事。改读绑定的理由：§6 落地的正是这条读数要的东西，
按 tab 数猜的那版只是为了不在没有权威时假装权威；现在权威有了，猜测就成了 bug。多 tab 下读数指向
持有会话的那个 tab，`connected` 仍为 `true`。测试 `test_the_browser_sub_item_names_the_browser_that_holds_the_session`
（`tests/test_agent_health.py`）钉这三段。
**[x]** 硬边界：健康读数**只进服务状态条，不进 job 视图**，也不得触发任何 `failed` 落库
（`/health` 与轮询是两条独立通路，流程文档 §14.6 第 5 条）。
落点：三条测试钉住（`tests/test_agent_health.py::HealthCannotTouchJobsTests`）——job 视图字段表里
没有 health 这一项；探活失败前后 `jobs_state` 逐字段相等；反复"探活失败 + 一次正常轮询"的循环里
`poll_errors` 只按轮询的账累计，任务不到 5 次上限**不判死**。
`/api/health` 与轮询走的是两条不相交的代码路径，本轮没有为它们加任何共享状态。
**判据**：`tests/test_agent_health.py` 27 支（透传与四种 502、上游状态码透传、缓存龄与过期、
三分项分类、只停 Qwen ⇒ `degraded` 不是 `unreachable`、并发不叠加、超时不拖累另一项、
健康不改 job）；`tools/browser_smoke.cjs` 的服务状态条三档断言归 §10/S4 与 S6。

## 8. 前端结果展示

**[x]** Agent 面板按 `outcome` 分支渲染：回答／澄清问题／技能顺序（`plan.commands`）／Qwen 调用数
（`metrics.model_calls`）／产物下载；`plan_ready` 固定文案**"计划已生成，尚未执行"**；
任何终态都不得出现"执行成功"；`clarification_required` 不得显示成失败。
落点：一句一行，全部由 `editor/src/result_view.ts` 决定 —— 文案常量 `:22 PLAN_READY_COPY`、
`:24 SCENE_UPDATED_COPY`，分支在 `:41 outcomeSentence`（`answered`／`clarification_required` 出 `reply`，
`failed` 按 `message → failure_reason → agent_state` 的原样顺序引用上游文本，无 outcome 的进行中行只说
`message`），装配在 `:90 jobRow`。技能顺序 `plan.commands` 允许字符串或对象，对象按 `JSON.stringify` 原样引用
（`:76 commandText`）—— 猜键名（`step`? `skill`? `name`?）会把 Agent 真正点名的那一步丢掉；调用数 `:94` 认
`metrics.model_calls` 的数字，**`0` 是答案不是缺席**，非数字（`"三"`）当没读；产物 `:95` 只发 gateway 白名单
那条代理路径 `/tasks/{task_id}/artifacts/{name}`，`debug/` 与服务器路径不在此列。渲染层（`main.ts:1241 agentMessageMarkup`）只做格式化：流式计划缺席时用 `row.commands` 补上计划区，`:1251` 把落地的新四元组与调用数
并成一行 `.message-detail`（样式 `styles.css:27`），产物是 `.message-artifacts` 的下载链。四元组只从**本方账本**
取（`:62 landedIdentity`，只认 `status==="committed"` 的最后一张回执），Brain 在 `commitReceipts` 里点的名不是结论。
行文本改由 `main.ts:1222 updateAgentJob` 存进 `row`，`row.text` 为空时保留旧文本而不是把已显示的句子刷成空。
**判据**：`editor/src/result_view.test.ts`（19 支）—— 五态各一句、`plan_ready` 逐字钉死"计划已生成，尚未执行"、
一条 `for` 断言五个终态的正文与身份行**都不含"成功"**、澄清态徽章是 `is-done` 不是 `is-failed`、`model_calls`
的 `0` 与非数字、字符串与对象混排的 commands、`unknown`/`rejected` 回执不产出新现场、产物链接按 task 走代理。
本轮不启服务，浏览器里的实际排版归 §10/S6。

**[x]** ~~阶段与锁状态可见：当前绑定浏览器、`session_phase`、租约持有者、`quarantined` 待确认提示 +
"新建会话"入口。~~ **本条展示面已于 2026-10-26 由 `Q123` 整条撤下**：用户口径「删除 Agent 面板中上部的租约描述、
阶段、浏览器 ID、相机自动选择等内容，直接把这个子面板删除，这里的信息和按钮全都删掉」，于是
`#session-strip` 容器、`main.ts:renderSessionStrip` 与它的五处调用、`.session-new`／`recover-session`
两枚按钮的 action、`state.sessionNotice`、`session_state.ts:readSessionState`／`SessionReadout`、
`styles.css` 的 `.session-strip*` 规则一起删除（`.agent-stall` 那条与它同在一行，保留）。
**删的是展示面不是判据**：阶段仍驱动 ② 类入口的置灰与弹窗（§4.6／R2）、`lease` 仍冻结灯光预设
（`lightingFreeze`，本段下面那条）、`quarantine` 仍拦 `POST /sessions`（`newSessionView` 的 `blocked`）；
**`lease` 那一支由 Q128（2026-10-27）就地改口**：两枚显式「新建会话」都带 `allowActiveTask`，任务还在跑时
这道门不再替网关拒绝（租约不释放、任务不取消，只是留在旧会话自己的上下文里），只有不带该参数的自动开会话
仍按 `任务 #N 进行中，新建会话失败` 拦。这些话不再预先陈述，只在用户真去动这一步时由 §4.6 的弹窗原话说出来。
**两项连带后果，按用户的"全都删掉"执行**：① 面板上的"新建会话"入口消失，同一道门还剩弹窗那枚
`data-action="phase-new-session"` 与头部的 `data-action="new-conversation"`（后者本来就兼作
`createSession(true, true)`）；② `POST /api/agent/recover` 在浏览器侧**不再有入口**，quarantine 期间用户
只看到"上一个任务的结果还没确认，新建会话失败"那句拦答（**Q127**，2026-10-27 就地改：原句里的
`POST /resolveUncertain` 是内部名字，不进界面），恢复这条路由只剩调用方／运维直接调。下面这段按记录保留，
写的是这条删除之前落过什么。
落点（行号核对到 2026-10-21，S5-c 给 `main.ts` 加了 55 行，本段旧写的 `:526`／`:150`／`:1089`／`:1238` 各往后推）：
读数已落 ——`editor/src/session_state.ts:62 readSessionState` 把 `/api/sessionState` 的四项
翻成句子 —— 阶段一句、绑定浏览器**分"本标签页／另一个标签页在持有／尚未绑定"三种**、租约是
`任务 #N 由本标签页持有` 或 `任务 #N · 持有者 X`、`quarantine` 一句"现场状态待确认（任务 #N）：上一次任务的
写入还没有结论"。分成两说是 §8.2 的口径：quarantine 是一个还没被回答的问题，不是失败的结论。
`main.ts:552 renderSessionStrip` 渲染 `#session-strip`（模板 `:161`、样式 `styles.css:30`，quarantined 时挂
`.quarantined` 换琥珀底）；刷新时机三条：`refreshSessionState` 拿到读数后（`:1248`）、`session.phase_changed`
帧（`:1166-1168` —— 帧里只有阶段，其余三项必须回权威路由取）、任务翻终态时（`:1315 ended → refreshSessionState`，
租约正是这一刻释放）。读不到就整条不显示：一条空转的旧读数比空白更像结论。
**"新建会话"入口已落（S5-c，§9 第 3 项到位之后）**：同一条 strip 里 `#session-camera` 列候选、
`.session-new` 是门，`main.ts:592 createSession` 发 `POST /sessions{browserID, camera_id}`；
`camera_id` 必填这道闸（缺 ⇒ `400 MISSING_PARAM`，"Gateway 从不猜相机"）从此有了一条合法取到
`camera_id` 的路径 —— 上一版写的"先放一个只能拿 400 的按钮比不放更坏"就是它当时的理由。
头部现有的 `data-action="new-conversation"` 仍是 §5.15 明确**不动**的聊天会话本地 CRUD，
与协议会话是两件事，本条没有把它当这个入口。
**判据**：~~`editor/src/session_state.test.ts`（23 支，其中 8 支钉这段）—— 未读时 `null`、三种绑定说法、两种阶段
说法、本 tab 与他人租约的两种措辞、quarantine 有无 `task_id` 各一句且**不含"失败"**、非绑定 tab 读同一条租约
说的是"持有者"而不是"由本标签页持有"；另外 8 支是 §9 的 `newSessionView`（候选/封锁理由，含两条负控制），
真浏览器里点这一枚按钮的断言归 §10/S6。~~ **Q123 之后的判据**：钉这段的 `readSessionState` 那 6 支随函数一起
删除；`newSessionView`（8 支，含两条负控制）、`lightingFreeze`（4 支）、`mergeSessionPhase`／
`parseSessionPhase` 各支**一支不减**——它们钉的是这道门与这把锁，与 strip 是否可见无关。页面上不再存在
`#session-strip`、`.session-new`、`.session-camera-auto` 任何节点，这一条归 §10/S6 的真浏览器断言。

**[x]** 任务行文案改成"上游原文"：徽章正文是 `job.agent_state` 本身，CSS 类由 `terminal`＋`outcome` 给，
不再由状态名派生。
落点（净删除）：`editor/src/logic.ts` 的 `agentStatusLabel` 连同它的五 token 分支整体删掉，`main.ts` 的导入与
两处调用点（消息行、Operations 抽屉 `:551`）一并去掉 —— 现在显示的就是 `escapeHtml(message.agent_state ?? "")`。
类名走 `result_view.ts:114 jobBadgeClass`：`:109 jobWorking` 只认 `terminal === false`（`localStorage` 里读回来的
旧行没有这个字段，按已结束处理，否则上周的任务永远转圈 —— §4.9 那条只影响 spinner，不参与任何协议判定），
终态再按 `outcome === "failed"` 出 `is-failed`，其余 `is-done`。**流式计划的隐藏条件同构**：`main.ts:1256` 用
`jobWorking(message)`，不是状态名。
**判据**：原来钉 `agentStatusLabel` 的"正在渲染／已创建"回显断言搬到 `result_view.test.ts:72`（`jobRow(...).state`
原样保留词表外的新词），`logic.test.ts` 少 4 支、`logic.ts` 是净删除；徽章在 `result_view.test.ts:115` 起的
describe 里，其中一条断言 `terminal === false` 时哪怕 `outcome` 已是 `failed` 也仍给 `is-pending` —— 探针与
结论各管各的。

**[x]** **服务状态条（R9 的落点）**：`#agent-state` 那个 Connected／Offline 徽章今天**不是健康检查**——
`state.agentOnline` 只在 `refreshAgentJobs()` 成功或提交成功时置 `true`、提交失败时置 `false`，也就是说它今天
表达的是"我发过任务且没报错"。改成读 `GET /api/health` 的 `agent_service.status`，显示"Agent 服务：就绪／降级／
不可达（附分项原文，如 `qwen=unavailable`）"，启动即查 + 每 15 秒轮询，第二 tab 同样轮询，**不新增 WS 帧**。
硬口径：`unreachable` **只**变状态条，不得把正在跑的 job 染红或改它的文案，最多在 job 旁附一句"任务状态暂无法
更新（连续轮询失败 N 次）"。
**那句里的 N 就地改定（本轮修订，原"N 来自 §7.1 保留的 `AGENT_POLL_ERROR_LIMIT`"作废）**：N 是**本 tab 自己
连续失败的读数次数**，不是服务端那个计数 —— `poll_errors` 是 §5.15 明确不外泄的内部账（`local_gateway.py:1007`
的 docstring 与规格 §5.15 同一条），gateway 从来没把它发出来过。把 5 抄进前端只会多造一个会漂移的副本；判死
任务的那 5 次上限仍在 gateway 一侧，一行没动。
落点：措辞在 `result_view.ts:129 agentHealthLabel`（三档 + `detail` 原样附在括号里；**`null` 是"状态未知"，与
gateway 说"不可达"是两件事** —— 前者是这条 tab 没问到，后者是问到了且答案是服务不在）、`:143 pollStallNotice`
（`0` 次不显示任何话）。`main.ts:511 renderAgentStatus` 一个函数管这两处：徽章文本 + `title`（面板只有 285 px，
降级原文会被省略号截断，全文在 tooltip）+ 类名 `ready/degraded/unreachable/unknown`，连续读失败计数写进
`#agent-stall`（模板 `:150`、样式 `styles.css:30`）。取数 `main.ts:1269 refreshAgentHealth` 只在 `catch` 里把
读数置回 `null`（不改任何 job）；`:1444 start()` 启动即查一次 + `setInterval` 每 15 秒 —— 15000 是内置值，§12.18⑤ 明确写了不做成常量。
状态字段 `main.ts:93 agentService`、`:97 jobPollErrors`；`agentOnline` 与它的三处写入（`refreshAgentJobs` 成功、
`submitAgent` 成功与失败分支）全部删除。类型 `types.ts:264 ServiceHealthView`、`:275 GatewayHealthView` 按
`local_gateway.py:648 health()` 的三分项逐字段对齐（`assets` 只有 `reachable`，别写出不存在的字段）。
**判据**：`result_view.test.ts` 的 `agentHealthLabel (§8)`／`pollStallNotice (§8)` 两组 —— 三档各一句且降级／
不可达附原文、`null ≠ unreachable`、三档文案**都不含"任务"**、`0` 次返回 `null`、那句里不含"服务"（把探针与
读数分开的正是这一条）。硬口径由构造保证：`jobBadgeClass` 只读 `terminal`/`outcome`，`renderAgentStatus` 只写
徽章与横幅，两者无共享状态；`tests/test_agent_health.py::HealthCannotTouchJobsTests` 已在 gateway 侧钉住"探活
不改 job"。`tools/browser_smoke.cjs` 今天对 `#agent-state` **没有任何断言**（它只查 `#status-primary`），三档
实链断言归 §10/S6。

**[x]** `#status-revision` 从 `v{scene_version}` 改为 `v{scene_version}` + **子版本非 0 时追加 `.{scene_sub_version}`**：
诊断时一眼能看出"这是用户加的物体还是 Agent 写的"。
落点：`editor/src/local_edits.ts:31 sceneRevisionLabel`（放这里是因为它是 §4.8.2 的读数面，与导出名
`sceneExportName` 同族），调用点 `main.ts:504 updateStatus`。`0` 不追加 —— 与 `result_view.ts:67` 那句相反：
那里的 `v13.0` 是在说"这张回执落地时没有本地编辑垫在上面"，是账本读数；这里是给用户看自己的手动改动。
**判据**：`local_edits.test.ts` 的 `sceneRevisionLabel` 两支（`v13`／`v13.2`／0 不带点；另一支钉"Agent 写的 `v14` 与本地编辑的 `v13.4` 两种形状不可能撞车"）。全量 `npx vitest run` 424 支、`npx tsc -p editor/tsconfig.json --noEmit` 零错、`cmp types.ts contracts/types.ts` 逐字节相同。

## 9. 相机与采集校验

**[x]** `capture_scene`／`get_2d_aabb`：强制绑定浏览器，逐项校验 `browserID/scene_id/scene_version/scene_sub_version/sceneDigest/playback_state/width/height`
与本次现场一致才转发；播放中／旧缓存／错浏览器／尺寸不符／PNG magic 不正 ⇒ 拒绝。
落点（行号核对到 2026-10-21，S5-c 之后；原文的 `:802-820`／`:822-835` 早在 S3 的扩写里就作废旧了，
S5-a 当时写的 `:2205`／`:2273` 那批又被 S5-c 的候选清单整体推后 47 行）：身份闸
`local_gateway.py:2252-2281 _require_frame_identity` ＋ `:2283-2289 _require_frame_size`，比对基准
`:2238-2250 _frame_basis`（V1 存下的租约指纹，无指纹或无租约时回落 `GatewayState.scene_identity`，两者都没有 ⇒
`400` 且消息点名出路），调用点 `capture_scene:2320-2321`／`get_2d_aabb:2359-2360`；字节面
`:298-305 decode_frame_bytes` ＋ `:308-322 png_frame_size`（magic／IHDR／IEND／IHDR 自报尺寸），`rgbd` 深度字节数
在 `capture_scene:2332-2339`。绑定闸与出帧闸一行没动，出帧闸只是把"尺寸不符"接到它后面。
**两码分工就地定稿**：身份不符 ⇒ `400 camera_frame_scene_mismatch`（这一枚自 S5 起 **gateway 也产**，
规格 §5.13 末注由"两枚例外"改口为三枚，§5.9 那两行的"Brain 侧判据"随之改口）；字节与自己的头不符 ⇒
`502 GATEWAY_FRAMING_ERROR` —— 那不是"现场不对"，是这一帧根本没成形，与"应答里没有图"同族，不新造码。
浏览器报的是 Gateway 算过的那枚摘要：`editor/src/main.ts` 的 `pairedSceneDigest()` 只在四元组与正在渲染的
world 逐项配对时才回它，否则回 `null` ⇒ 被拒；**宁可拒也不报旧摘要**（§9.1"旧值"那一支）。
**判据**：新增 `tests/test_capture_identity.py`（15 例：四元组逐项改旧值、六项逐个缺键、`browserID` 指错 tab、
三种播放态、无基准、**租约指纹优先于在场读数**（正反两支）、量框同一道闸的两支、五种不完整 png、IHDR 自报
16×6 vs 声称 8×6、深度三种坏法＋一支正确 rgbd 正控制）；`tests/frame_identity.py` 是三条文件共用的替身应答
（`png_bytes` 只写头与尾、CRC 归零，因为 Gateway 从不解码像素）。`test_capture.py`／`test_bbox.py`／
`test_gateway_state.py::CaptureForwardingTests`／`test_scene_lease.py::CollectionLeaseGateTests` 四处的假浏览器
从此答得出身份（`record_scene` 一个基准 + 六项齐备），Python 全量 428→**443** 支，仅 `test_default_config_file_matches_the_built_in_defaults` 一支红（§6 末注那条等用户裁决的常量差，与本轮无关）。
**样例脚本不动**：`examples/gateway_client_demo.py` 只当调用方、从不发 `browser.hello`（`EventsListener:485-493`
的注释就是这条），它没有"替身浏览器"要补；规格 §5.9 末也写明本期不扩样例。
**[~]** 采集期持锁；采集前后 `getWorld` 摘要必须相同（同一现场）；灯光预设已被锁冻结，所以像素也可重复（§2.3 最后一条）。
gateway 这一侧到 S3-e 就齐了：租约闸（`_require_brain_lease:2213-2236`）＋ 采集帧里带 `lease_token`
（`capture_scene:2315-2316`／`get_2d_aabb:2354-2355` 的 body）＋ `state_sync` 采纳时 `record_scene` 抬水位；
浏览器这半截：灯光**复位**在 S0-3（`applyWorld()` 换现场回到默认光照），灯光**冻结**在 S5-d
（`session_state.ts:92 lightingFreeze()` 只认 `lease`，`main.ts:355 renderLightingFreeze()` 收预设 + `aria-disabled`，
`main.ts:1568` 的 15 秒 tick 在 `agent_owned` 期重读权威路由才看得见锁——见 §2.3 最后一条）。
**V0/V4 两次 live read 的摘要对账是 Brain 的动作**（流程文档 §9.1），我方只能保证读得到、拒得起；
"真实截图与框同现场"那条实链断言归 S6 smoke，不在本条打勾。
**[x]** 相机候选清单来自现场带 `Camera` 的物体，**不做名字猜测、不取第一个**；
选定与持久化归 Brain（`POST /sessions` 的 `camera_id`）。浏览器侧 `World.cameraObjectIds()` 已在 S5-b 落地
（按组件判定、文档序返回），Gateway 的缓存与 `camera_candidates` 读数面、FE 的"新建会话"入口在 S5-c。
落点（行号核对到 2026-10-21）：形状判定 `local_gateway.py:325-342 camera_candidate_ids`（非 uint32 丢弃、
保序、去重，**没有清单就清空**），采集与配对 `:1286-1287`（`apply_state_sync` 采纳那一刻把清单和
`incoming` 四元组一起留下），读数 `:1325-1334 _camera_candidate_view`（配不上 `state.scene_identity` ⇒ `[]`），
两条读路由各挂一枚键：`session_state:790-800`／`sessions:832-838` 的 `"camera_candidates"`；
进程内存、不落盘（清单是对文档的**上报**，不是状态文件里的事实）。
FE 侧 `editor/src/session_state.ts:107 newSessionView()` 是这道门的唯一判据（候选＋blocked），
`main.ts:616 renderSessionStrip()` 画 `#session-camera` 与 `.session-new`，`main.ts:661 createSession()`
发 `POST /sessions{browserID, camera_id}`，成功只回一句 `state.sessionNotice` 并重取权威读数；
禁用态仍旧是 `aria-disabled` 不是 `disabled`（R2 那条委托第一行）；**候选之外不做任何默认**——
`camera_id` 只从 `<select>` 的当前值读，且必须仍是清单里的一枚，否则一句"请先选一台相机"。
**不做 `camera_id ∈ candidates` 的闸**（就地定稿）：Gateway 不读文档，第二份关于相机的真值只会与文档分叉；
选错由现场自己以 `CAMERA_NOT_FOUND` ⇒ `404` 回答。**判据**：`tests/test_gateway_sessions.py`
25→**30** 支（两条读路由同源、形状〔乱序＋脏值＋去重〕、不再上报即清空、**换现场即清空**、
候选清单不是闸）；`editor/src/session_state.test.ts` `newSessionView` 8 支，其中两支是负控制——
`agent_owned` **不**封门（它正是这道门要离开的阶段）、别的 tab 绑着也**不**封门（关掉的绑定 tab 必须能交接，
封了就锁死这条出路，网关自己会用 `browser_new_session_required` 回答活着的另一种情形）。
Python 全量 443→**448**，TS 429→**437**，`tsc` 零错，`cmp types.ts contracts/types.ts` 逐字节相同。
规格 §5.9 的 `GET /sessions`／`GET /api/sessionState` 两行随之补上 `camera_candidates`
（后者顺手记下它是那一行十个键里唯一不来自磁盘文件的那一个），流程文档 §9 那条"只提供候选清单"补了交付口径。
**联调第 2 条 P1 补记（2026-10-22，就地改 §1.2 第 4 条的主语）**：`camera_candidates=[]` 的根因不在清单，
在"换场景的那份上报被整条拒掉"——载入另一个 `scene_id` 时 Gateway 既不留水位也不留清单，
于是配对着落不到任何存量现场上，而"新建会话"这道门恰恰需要它。改法与判据见 §5 那条 `apply_state_sync`
的补记；**"换现场即清空"这条判据本身不动**——它说的是"手上那份清单不属于存量现场"，
采纳换场景之后配得上的是**新**现场的那一份，不是旧的。TS 侧 `newSessionView` 八支一字未改。
**清单的另一半（S6-b，2026-10-22）：Agent 写入落地后没人再报一次**。上面那条把"换场景"救活了，但
**同一份现场被 Agent 改掉之后清单照样会空**——清单只有 `browser.state_sync` 这一个来源，而写入是
Gateway 自己 `record_scene`（`local_gateway.py:1765`）的，浏览器那边一次上报都没有，配对（`:1289`）
自然落不到新四元组上。修的是浏览器：**采纳别人写的那份现场之后补一次上报**，落点两条
（`main.ts:1337 adoptWorld`）——① 重读并落地那份文档的成功路径末尾；② 广播的四元组**已经**是本 tab 手上这份
（绑定 tab 自己执行了这次写入，所以它一定走这条早退分支）且这条广播**带 `source_request_id`**，即它来自
一次调用方写入而不是一次 `state_sync` 回执。第 ② 条的门就是不成环的凭据：`apply_state_sync` 广播时
`source_request_id=None`（`:1296`），所以"我报的"回声不会再触发一次"我报"，而调用方那次写必须补报。
**幂等性是这条路的安全带**：重报的是同一份文档 ⇒ `adoption_verdict` 先撞上 `incoming == stored` 那支
（`gateway_state.py:130`）回 `idempotent`，`scene_version`／子版本一步不动、`agent_owned` 期也不会被读成
`manual_edit_forbidden`（§1.2 第 2 条的推进判据在它之后）。这一条正是 §2.4 那枚"开一个新会话"按钮能按下去的
前提：门要靠清单打开，而清单要在接管期重新配对，只能靠这次重报。
**判据**：`tests/test_gateway_sessions.py` 在"换现场即清空"之后加一支
`test_a_re_report_of_the_stored_scene_repairs_the_list_after_a_commit_lands`——先 `set_phase("agent_owned")`
＋`record_scene` 把存量推到新版本（commit 落地的原样），清单为空，再重报**同一份**文档：两条读路由的清单都回来、
`scene_version` 仍是 4、`scene_sub_version` 仍是 0。Python 全量 453→**454**（照旧只剩那支已知红的
`test_default_config_file_matches_the_built_in_defaults`）。多 tab 时每台查看者各补一份整文档上报，
同一份内容、判 `idempotent`，代价是一次上传换一个不空的清单。TS 侧 `adoptWorld` 是 DOM/WS 接线，
无可抽的纯判据，观感归 S6 smoke。
**[x]** segmentation 明确拒绝。落点：`capture_scene:2304-2305` 的类型枚举闸（`rgb`|`rgbd` 之外一律 `400 INVALID_PARAM`），
Gateway 从不为它生成占位数据。**判据**：`test_capture.py::test_a_type_outside_the_enum_is_400` 的枚举里加了
`"segmentation"` 一项，连同 `depth`／`RGB`／空串四支都在应答发出之前就被拒（`assert_no_frame` 钉住"没派发到浏览器"）。

## 10. 测试与验收

| 层 | 内容 |
|---|---|
| 单测（TS） | **删**：`world.test.ts` 的 `setLocalTransform`/`setLocalJointPosition` 组、`local_edits.test.ts` 的 `poseEditBlock`/`jointEditEnabled` 组、`renderer.test.ts` 的 pose gizmo describe（`:1457` 起，连同 `gizmoStub`（`:66`）与 `adapterFixture` 的 gizmo 部件（`:105-178`））、`logic.test.ts:202` 的"五个 token 出中文"一支（R10，`:209-211` 的"认不出就原样回显"三支**保留**并成为唯一行为）。**改**：`lighting.test.ts` 从"改文档"改为"渲染层覆盖"断言（`toJSON()` 逐字节不变）；`world.test.ts` 的 `addLocalObject`/`removeLocalObject` 组**保留**并新增子版本推进断言；`scene_requests.test.ts` 阶段拒切场景 + 拒子版本推进；TS 侧**没有** `digest.test.ts` —— 摘要测试随 R5 的改口落到 Python（下行）。**负控制**：① 类方法在 `World` 公共接口里不存在（类型层面断言，防半路复活）；③ 类切预设后 `digest` 与 `toJSON()` 全等；② 类两次加物 ⇒ 子版本 +2、`scene_version` 不变、摘要变；**`types.ts` 的 `AgentJob` 里 `status` 键不存在**（R10 的类型层断言，收尾仍要 `cmp types.ts contracts/types.ts`） |
| 单测（Py） | **`test_digest.py`（新，S2-a 已合并 15 例）**：规范形五条口径 + ECMAScript 基准 hex + 不动点 + 缺键不补 0 + 未知字段照算 + ① 类那两组不等 + 单点检查（`RELEASE_FILES` 收 `digest.py`、`editor/src` 里不许出现 `crypto.subtle`）；新增 **`test_gateway_sessions.py`（31 例，S3-b 已绿：三道闸、Brain 4xx 透传＋本地零改动、回话四字段盖回、`session.phase_changed` 只在阶段移动时一条、`/health` 的 `browser` 子项点名绑定 tab；S5-c 再加五例候选清单：两条读路由同源、形状与保序去重、不再上报即清空、换现场即清空、清单不是闸；S6-b 再加一例"commit 落地后重报同一份文档把清单配回来、`scene_version`／子版本分毫不动"）**；S5-a 补 **`test_capture_identity.py`**（15 例，身份闸与 PNG/深度字节面，见 §9）＋三条文件共用的替身应答 `tests/frame_identity.py`；`test_commit_world.py`／`test_scene_lease.py` 已在 S3-d／S3-e 补齐；**`test_allocate_object_id.py`＋`test_state_sync_subversion.py` 两支在 S3-a 合并为 `test_gateway_state.py`（49 例，2026-10-22 联调回改后 **71** 例，已绿：号段申领、上报抬水位、`agent_owned` 期拒绝推进，外加"换场景在 `setup` 采纳／在锁下被拒"那三支）**——两块读的是同一个持久文件，拆开只会各造一份状态桩／**`test_agent_health.py`**（R9：转发 3 秒超时 ⇒ `502 unreachable`、Brain 200 时字段**逐键原样**、缓存 ≤5 秒且带 `X-Agent-Health-Age`、`/api/health` 三分项并发探且任一超时不改 job 状态）；**`test_gateway_agent.py` 整支改写**（R10）：`:20` 的 `AGENT_STATES` 导入、`:159/267/298/320/341/388` 的 `(status, agent_state)` 断言、`:411-419` 的"映射表就是契约"两支全部改成 `terminal`／`agent_state` 原文断言，并新增"替身回未见过的字符串 + `terminal:false` ⇒ 继续轮询、`agent_state` 原样"与"`terminal` 缺失 ⇒ 非终态 + warn"两支；`test_gateway_routes.py` 扩状态头并把 `allow_details_joint_angle_edit` 从"配置==内置默认"字段表里删掉；持久状态一律用 tmp 目录（S3-a 的口径是 `GatewayState(None)` **默认易失**，只有 `main()` 与 `serve_verification.py` 点名文件，所以测试不必逐个传路径也不会串台） |
| 无浏览器探针 | `tools/audit_parity.py` 扩 brain-web/2 路由面（**S3-b 后现 62 项，62/62 通过**；**S3-c 未加探针**——本工具的口径是"绝不伪造 `browser.response`"（仅 `state_sync` 例外），而 §5.1 的 live 头集必须有浏览器应答才可观测，那部分归单测（`test_ws_protocol` 的三支新 getWorld 头测）；探针侧只覆盖无应答即可判定的两态：`?browser_id=nobody` ⇒ `503`（§5.2/§5.8 定向即拒绝）、`state_sync` 后无浏览器 ⇒ `X-World-Source: cache`）：`CALLER_SURFACE` 加 `GET /api/sessionState`／`GET /sessions`／`GET /brainCapabilities`＝200 与 `GET /allocateObjectID?browserID=browser-1`＝`503 NO_BROWSER_CONNECTED`，`PARAMETER_GATES` 加 `/allocateObjectID` 缺参 ⇒ `MISSING_PARAM` 与 `/sessions` 四道参数闸（缺 `browserID`、缺 `camera_id`、`browserID` 非字符串 ⇒ `INVALID_PARAM`、`camera_id` 为字符串 ⇒ `web_uint32_required`）；**`POST /sessions` 刻意不进 `CALLER_SURFACE`**——它会真转发到 8500，探针结果取决于本机此刻有没有 Brain 在听，一个看环境的断言不算判据；新端点继续随阶段追加 + **`/health`／`/api/agent/health`**）；`tools/parity_inventory.py` 重新生成清单（S3-a 23→25，S3-b 后 `local_routes` **28**），`runtimeConfig` 探针去掉被删的常量键 |
| 真浏览器 | `tools/browser_smoke.cjs` **删**§12.8b 的 gizmo／关节两段（`:24` 的文件头说明同步改）；**实装删完之后 smoke 里还有七处仍点 ① 类入口，S6 必须一并清**（核对到 2026-10-20 的行号）：`:659`/`:661`（`[data-joint]` 读数断言）、`:761-829`（含 `:804` 的 `check` 标签）、`:827`（§12.8c 填夹爪关节）、`:889`（`[data-joint]` 计数断言）、`:1112`（§12.13 点 translate）、`:1429-1433`（§12.14 轮流点三种工具）、`:1662-1667`（§12.15 填 `door_joint`）——关节那几处改成只读断言（有 `(now)` 读数、输入节点数为 0），gizmo 那几处整段删除；**扩**：持久 browserID（刷新不变）、首任务后切场景被拒、**首任务后点面板加物弹出文案**（R2 的落点）、**弹窗是问句不是句号（S6-b）**——同一遍里断言 `#phase-block` 有两枚按钮（`.phase-accept`＋`dismiss-phase-block`，都不带 `disabled`）、正文第二段点名要重做什么，点 `.phase-accept` 真的发出 `POST /sessions`（回到 `setup` 后文件框／预设场景那一步跟着重做；状态待确认时理由留在弹窗里而不是五秒的 toast（**Q128**：任务还在跑、租约还在时这道门**放行**，弹窗那枚「新建会话」带 `allowActiveTask`））、**Agent 写入落地后 `/api/sessionState` 的 `camera_candidates` 不再是空表**（S6-b 那次补报的观感，配合 `#session-strip` 的"新建会话"由灰转亮）、`agent_owned` 期 `Load Traj`／播放／导出／截图／灯光预设仍可用（R1 的负控制，**取锁之前**点它）、
灯光预设切换后 `getWorld` 摘要不变、**持锁期点灯光预设：弹出"任务 #N 进行中，光照和阴影暂时改不了…"、`#lighting-preset` 带 `aria-disabled`
且 `renderer` 的预设灯数为 0（S5-d 的冻结），释放后同一个点法回到可用**、commit CAS、租约停播、真实截图与框同现场、**页面上找不到 `[data-tool=translate|rotate|scale]` 与 `[data-joint]` 节点**（① 类负控制，注意 `#lighting-preset` 这次**必须存在**）、**服务状态条三档**（就绪／降级／不可达，停 Brain 后状态条变红而 job 文案不变，R9）、**替身喂 `"规划中"` 时面板原样显示这四个字且不转成"进行中"**（R10） |
| 一次性栈 | `tools/serve_verification.py`（8098）继续带 `X-Standalone-Verification`，状态目录用临时目录，**不得**作为 brain-web/2 联调对象 |
| 发布包 | `pack.py` 重打，`config/constant_config.json` 与 `dist/` 随包，验证 8099 起包可用（对方回函 §一 的"尚未收到新包核验"由此关闭）；`distribute/1.0.0-rel/` 是上一版快照，**不回改** |

回归链每阶段跑：`tsc --noEmit`、`vitest`、`unittest discover`、`audit_parity.py`、`parity_inventory.py`、
`cmp types.ts contracts/types.ts`、`npm run build`；收尾停服务并 `netstat` 确认。

## 11. 施工顺序（我方）

```
S0  §2 三分类入口施工（① 删除 + ② 阶段闸门与弹窗 + ③ 灯光改渲染层）
S1  §1 规约改写（§3.1/§3.9/§4.6/§4.8/§4.8.1-4/§5.4/§5.5/§5.8/§5.9/§5.13/§5.15 两处/§9.2/§10.4/§12.8b
    + Q109-Q116 + §12.17/§12.18 + 场景文件描述.md 三处）
S2  §3 digest（Gateway 单点，Python `digest.py`）+ §7.1 状态词表放开 + §7.2 健康转发
S3  §5/§6 持久状态、会话、定向写入、commitWorld、租约、申领 + §7 的转发载荷
    （**S2-a 就地改序**：`startTask` 的 `sceneDigest`／`sessionID`／实时取现场三件事都要先有持久绑定与会话，
    §6 的 `X-Scene-Digest` 又必须与那组响应头同批装 —— 摘要先在 S2 落地并被单测钉住，消费者全在 S3）
S4  §4 三层防线 + §8 结果展示（含服务状态条）
S5  §9 相机校验
S6  §10 浏览器 smoke 扩项 + 发布包（8099 新包）
```

S0 排第一的理由：四元组、`X-Scene-Sub-Version`、"锁冻结灯光预设"这些口径只有在入口真的按三分类
落位后才测得出来；尤其 ③ 类必须先改造成"不动文档"，否则 §3 的摘要等式一上页面就假阳。

**§7.1 排在 S2 而不是单独一步**：它改的是 `AgentJob` 的字段与 job 视图，和 S2 的转发载荷扩字段
是同一批类型改动（`types.ts` 与 `contracts/types.ts` 只能逐字节同步一次）。
§7.2 的健康转发与协议无耦合，可以插在任何一步之后，但**必须早于 S4**——S4 的服务状态条要读它。

每阶段独立成卡：文档 → 代码 → 测试 → 任务卡 → 本地提交，**不 push**；收尾停掉本轮起的服务。
