# 2026-11-05 场景切换 ACK 回声（连点闪回不再钉死）

## 原因

同事报：切场景时**有时先出现目标场景、然后又回到原场景**。这与上一卡（[2026-11-05-首连不再重读权威场景](../2026-11-05-首连不再重读权威场景/README.md)）修的"首连多余重读"是**另一条路径**——那一卡收尾时明确写着 `adoptWorld` 分支与 gateway 缓存回退**本轮不动**，本卡就是动这两处。

用户口径（原话要点）：浏览器开新会话时已经和 gateway 绑定了浏览器 id 与会话 id，所以广播给所有人没有必要，只广播给对应浏览器即可；浏览器收到一个 `scene.applied` **先查会话 id**，不一致说明这期间用户切了会话，忽略；一致才刷场景。参考 TCP 的 ACK：假定浏览器一段时间内连点 1、2、3，此时浏览器场景是 3，3 加载完却收到 gateway 对 2 的回应，因为 2<3 就忽略、等 `ack=3`；收到 3 的回应后最终确认为 3。又因 AgentBrain 侧有代码走几乎相同的路径来应用场景、而 Agent 在任务中不允许切场景，所以**当浏览器收到一个 ack 大于自己请求数的场景应用时，应当直接相信并应用**。此修复是 gateway 与浏览器之间的通信问题，**对 Agent 侧无感**。

批准落地时定的三条边界：**ack 不持久化**、**`/getWorld` 不再允许 cache**、广播仍走既有 fan-out（把上报方那一份变成**显式 ACK**，不改寻址）。

### 排查到的根因链（四步，缺一步都不会钉死）

1. `local_gateway.py:apply_state_sync()` 在广播之前要 `await self._ensure_automatic_session(...)`（Brain 的 `POST /sessions`）。Brain 忙或不在场时这一步会等到超时 ⇒ **这一帧的回声可以晚到用户已经切去下一个场景之后**。
2. 旧的 `main.ts:adoptWorld()` 只有两条路：四元组相同 ⇒ 重报；不同 ⇒ 重读 `/getWorld` 并**整份覆盖** `state.world`。晚到的那一帧四元组是**旧场景** ⇒ 走重读 ⇒ 旧场景盖回屏幕上刚加载好的新场景（同事看到的"先出现目标场景、然后回到原场景"）。
3. 那次重读的答案本身也可能是旧的：`get_world()` 的 `world_cache` 回退分支（503/504 时端缓存）与每会话 `world_snapshot` 都可能仍停在旧场景，`X-World-Source: cache` 就是它的自白。
4. 覆盖之后 `reportWorld()` 把**旧场景推回 gateway**；而同日上一轮把跨场景上报改成了**无条件采纳**（[2026-11-05-换场景无条件采纳](../2026-11-05-换场景无条件采纳/README.md)）⇒ gateway 也认了旧场景 ⇒ **两边一起钉死**，不再像以前那样"下一次重连自己回来"。

外加一条把自愈也堵死的：旧 `adoptWorld()` 首行 `if (adoptingWorld) return;`，重读期间到达的**新**回声被整帧丢掉 ⇒ 新的那次确认永远不来。

## 做了什么

### 协议（**Q147**）

- `browser.state_sync.payload` 加 **`report_seq`**：本页面自增的整数，从 1 起，**不落 `localStorage`**；gateway 不比较、不持久化、不解释，收到什么就回声什么。socket 未 OPEN 时的重试算**新的一次**上报，所以计数器只在真的 `send` 之后 +1。
- `scene.applied` 加两键：**`session_id`**（落地那一刻 gateway 手里的持久会话号，没有会话时 `null`）与 **`ack_report_seq`**（逐字回声上报方的 `report_seq`；凡不是由浏览器上报落地的——调用方四类写入、`/commitWorld`、`session_switch`——一律 `null`）。
- `GET /getWorld` **删掉 cache 回退**：无绑定浏览器 ⇒ `503 NO_BROWSER_CONNECTED`，超时 ⇒ `504 BROWSER_TIMEOUT`，浏览器自己拒绝 ⇒ 原样透传它的码。`X-World-Source` 只剩 **`live`** 一个取值，`X-World-Cache-Age` 不再存在。`world_cache` 字段仍照旧记录（水位与诊断），只是**不再当一次读数**。

### 浏览器端

- `editor/src/session_identity.ts:appliedNoticeVerdict()`：纯判据四支，顺序即优先级——`other_session`（会话号两边都有且不等）／`late_echo`（`ack_report_seq <` 本页最大 `report_seq`）／`confirm`（`==`）／`apply`（`null` 或 `>`）。
- `main.ts:adoptWorld()` 改成**先判后动**：`other_session` 先 `refreshSessionState()` 再判一次（`session_switch` 恢复历史那一帧的新会话号可能比本页轮询先到），仍不一致才丢；`late_echo` 直接丢；`confirm` **只**刷会话与状态栏——不动 `state.world`、不重读、不重报；`apply` 才进原来的落地体（提出为 `main.ts:applyAppliedNotice()`，体内一字未改）。
- 落地期间到达的帧不再丢：`queuedAdoption` 只留**最新**一帧、循环取用 ⇒ 连点 1→2→3 时最后那一帧一定被应用（旧写法把它 `return` 掉了）。
- `reportWorld()` 的计数器 `worldReportSeq` 是**页面生命**的：持久化会让刷新后的页面拿着一个 gateway 从没见过的号，把自己第一条回声读成"别人的写入"。

### gateway 端（`local_gateway.py`）

- `apply_state_sync()` 取 `report_seq`（只认非 `bool` 的 `int`，其余一律 `None`），广播时回声。
- `_scene_applied_frame(..., ack_report_seq=None)` 带上 `state.session_id`；三个非上报落地的调用点（`session_switch`、调用方写入、`/commitWorld`）保持默认 `None`。
- `_cache_world(document)` 去掉 `browser_id` 与时间戳记账，`_cache_age_seconds()` 删；`_world_response()` 去掉 `source` 形参、头写死 `live`；`get_world()` 的 try/except 回退分支删（两处每会话 `world_snapshot` 分支保留）。

### 文档就地修订

- 规格 §5.4：帧行加 `session_id`/`ack_report_seq` 两键 + 新增一条「**自己报的那一条按 `ack_report_seq` 认**（2026-11-05 定稿，Q147）」，含四行判据表、"为什么需要它"（`_ensure_automatic_session` 的等待窗口）与「**对 Agent 侧无感**」两句。
- 规格 §5.8：payload 行加 `report_seq` + 新增一条「**每一次上报都编号**」；「别人写的那份也要重报一次」那条补一句收口说明（自己那次上报的回执在 §5.4 第 ③ 支就已经判了"确认、不再上报"，走不到这里）。
- 流程文档：§14 帧表 B→G 行、Brain→GW `/getWorld` 行、§14.2 B2 行；§5.1 段落改写；`X-World-Source: live  # 唯一取值` 注释。
- **改判关系两条写在正文**：① 流程文档 §5.1 上一稿写着"缓存 ≤1 秒内可作诊断回退"——本轮**收回**。规格 §1.4/§5.8/§5.9 本来就写着 `cache` 分支删除，那一口子从来不该开；跨场景无条件采纳之后，一次缓存读数足以把"晚到的回声"升级成"两边都同意的旧场景"。② 上一卡正文那句"Gateway 侧的 `world_cache` 回退分支同样不在本轮范围"，本轮把它做掉了。

### 不做（边界）

- `local_gateway.py:adoption_verdict()`（gateway 的采纳判据）**一字未动**——换场景无条件采纳照旧，本轮只改"读者怎么认这一帧"。
- 广播仍是 **fan-out**，没有改成按 `browser_id` 定向。用户口径提过"只广播给对应浏览器即可"，但 `BROWSER_ID` 是 `localStorage` 里的持久号（`editor/src/local_edits.ts:BROWSER_ID_KEY`），**同一 profile 的两个 tab 共用它**，定向并不能把它们分开；真正要区分的是"这一帧是谁报的"，由 ack 号解决。
- §5.8「别人写的那份也要重报一次」照旧保留；`firstSocketOpenDone`（上一卡）不动。
- Brain / Agent 侧代码与 REST 报文**一字未动**：没有新路由、没有新报文字段进 Agent 视野，gateway 只是回声一个不透明整数。

## 验证

- **新增 `editor/src/adopt_ack.test.ts`（7 例）**：`main.ts` 一 import 就接 DOM、没有可导入面，按坑 25 那条老路钉源码结构——计数器只在越过重试分支之后 +1；`socket.send(` 那一片里有 `report_seq: worldReportSeq`；`worldReportSeq` 与 `localStorage` 之间**任何方向**都没有联系；`adoptWorld` 在 `applyAppliedNotice` 之前先过 `appliedNoticeVerdict(localReportReading(), broadcast)`，`other_session` 支里有 `await refreshSessionState()`、`late_echo` 支是 `return`；`confirm` 支里既没有 `reportWorld(` 也没有 `fetchJSON(`／`state.world =`，但有 `refreshSessionState()`；队列那两句（`queuedAdoption = broadcast` 与 `notice = queuedAdoption`）都在；`applyAppliedNotice` 仍持有 `await fetchJSON<unknown>("/getWorld")` 与 `if (broadcast.source_request_id !== null) reportWorld();`。
- **`editor/src/session_identity.test.ts` 新增 5 例**纯判据：`other_session` 优先（含"既晚到又是别的会话"）、单边 `null` 不当过滤、`late_echo`（ack 1/2 vs 本页 3）、`confirm`（含 1/1）、`apply`（`null` 与 ack 4 > 3）。
- `npx tsc -p editor/tsconfig.json --noEmit` **0 错**；`npx vitest run --config editor/vitest.config.ts` **20 文件 649 例全绿**（本轮前 19/637）；`npx vite build --config editor/vite.config.ts` 成功（**894.66 kB** JS / 21.85 kB CSS；chunk >650kB 的告警是既有状态）。
- **Python**：`tests/test_ws_protocol.py` **26 例**（新增 3 例回声判据：带号上报原样回声、不带号回声 `null`、非整数号不回声；两例"暖缓存不再答 `/getWorld`"改成断言 `503`/`504` 且**没有** `X-World-Source`/`X-World-Cache-Age`、而 `world_cache` 仍被记录）；`tests/test_commit_world.py` **25 例**；`tests/test_caller_apis.py`、`tests/test_error_mapping.py`、`tests/test_gateway_sessions.py` 全绿（`_cache_world()` 形参变了，13 处调用点跟着改）。
- **全量 `python -X utf8 -m unittest discover -s tests`（本机跑不完，但不是本轮的账）**：全套 **493** 例，它停在 `tests/test_gateway_state.py:PersistentGatewayTests.test_a_scene_switch_in_setup_moves_the_stored_scene_and_its_candidates`（第 667 行，那一例开第二个浏览器当 watcher、等一帧 `scene.applied`）——**既不出结果也不报错**，只是不回来。**已证明与本轮无关**：把 `local_gateway.py` 备份后换成 `git show HEAD:local_gateway.py`，单跑该例一样卡死（`timeout 100` ⇒ RC 124），换回备份后 `md5sum` 与改动前一致。绕开它把其余跑完，**492 例全绿、0 失败 0 错误**：卡住之前的 12 个模块 313 例（`-v` 全量跑到 `test_gateway_state` 为止，`... FAIL`/`... ERROR` 计数为 0）＋ `test_gateway_state` 79 例里除卡住那 1 例的 78 例（按类 `-k` 分批 + `PersistentGatewayTests` 逐例单跑）＋ `test_pack` 10、`test_scene_lease` 37、`test_traj_api` 19、`test_traj_examples` 9、`test_ws_protocol` 26。**跑法两条**：`discover -s tests -p <模块>.py` 是好用的（旧的"只有裸 `discover -s tests` 才行"那句只对 `python -m unittest tests.test_x` 成立）；套 `timeout` 时必须加 `python -u`，否则 `-v` 的进度留在 stdout 缓冲里、进程被杀就一个字也看不到。
- **顺带一条实测更正**：`gateway_state.py` 的目录 fsync 早已加 Windows 守卫（`_fsync_directory`），`DirectoryFsyncTests` 4 例全绿——记忆里"全套在 Windows 上 72 失败 / 80 错误"那条基线**不再成立**，今天唯一的拦路症状是上面那一例卡死。
- **真机走查未做**，两条理由写清：① 晚到窗口要靠 Brain 的 `POST /sessions` 真的慢下来才出现，本地没有 Brain ⇒ `_ensure_automatic_session` 快速失败 ⇒ 复现不出这一竞态；② `tools/browser_smoke.cjs` 仍卡在 §12.4 的身份闸（记忆卡「Smoke blocked by identity gate」）。留给联调的人工口径：Load Scene 连点两个本地场景文件（1→2→3），看屏幕是否停在 3、DevTools Network 里 `/getWorld` 的 `X-World-Source` 是否只有 `live`、WS 帧里 `scene.applied.ack_report_seq` 是否随每次上报递增。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本地提交（master，未 push） |
| 验证 | `adopt_ack.test.ts` 7 例 + `session_identity.test.ts` 新增 5 例 + tsc 0 错 + vitest 20 文件 649 例全绿 + vite build 成功；Python 全套 493 例里 **492 绿 / 0 失败**，唯一 1 例卡死是 `test_gateway_state` 那条例先在 Windows 上就不回来的（已用 `git show HEAD:local_gateway.py` A/B 证明与本轮无关）；真机走查未做（本地没有 Brain ⇒ 竞态复现不出，smoke 卡 §12.4 身份闸） |
