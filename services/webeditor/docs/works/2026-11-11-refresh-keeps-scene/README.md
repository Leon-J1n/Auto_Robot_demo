# 2026-11-11 刷新浏览器必须回到刷新前的现场

## 原因

用户原话（两条）：

> 发现了一个回归问题，在最近的某些修改中有什么东西影响了前端的场景展示。按照本月稍早一些的要求，刷新浏览器应当显示刷新前的场景，而不是回归空场景。

> 在我上一次的执行中，我只启动了 OLS 和前端，每次刷新都回到空场景是因为我没启动 AgentBrain 吗？我认为这应当和 AgentBrain 无关因为这是一个纯的前端特性。如果 Gateway 只在和 AgentBrain 建立完整会话后才保存场景，修复这个问题。

**复现（一次性栈：OLS 8096 + gateway 8098，不起 AgentBrain，与用户的部署一致）**：

```
BEFORE reload: Connected · #2 ur5_table_apple · v3 · 9 objects
  net 503 /getWorld
AFTER reload:  Connected · #0 · v0 · 0 objects
warning: [startup] could not read authoritative world: browser <id> is not connected
```

刷新后再问 gateway：`scene_id: 0, scene_version: 0` —— **盘上那份也没了**。

**因果链（四步，全在既有规格里各自成立，合起来是回归）**：

1. §5.4「重读权威现场只发生在两处：启动一次、重连一次」要求 `start()` 在**建 socket 之前** `await loadAuthoritativeWorld()`。所以这一读问出口时，**本页面的 socket 必然还没接上**。
2. **Q147**（`64d7eaa`，2026-11-06，规格 §5.8/§1.4 的 Q111 口径）把 `/getWorld` 的 `world_cache` 回退**整条删除**，只留 `live`。于是第 1 步那一读只剩 `503 NO_BROWSER_CONNECTED`。
3. `loadAuthoritativeWorld()` 的 `catch` 只 warn，`state.world` 保持内存里那份**空世界**（这个 catch 本身是对的：读失败不许猜一个场景）。
4. 第一次 `onopen` 按规格「空现场也要报」把它报上去；§5.8 第一条「换 `scene_id` 无条件采纳」照章采纳 ⇒ `record_scene` 把持久身份改成 `#0 v0`。**刷新前的场景连 gateway 手里那份都被覆盖了。**

**为什么"没起 AgentBrain"看起来像原因**：`get_world` 还有另一条出口 —— `_session_context(self.state.session_id).world_snapshot`。那份快照只在 `_sync_active_conversation_binding()` 里写，而它要求**有 Brain 建成的会话 + 有活动历史行**。所以起了 Brain 时刷新碰巧能走通，没起 Brain 就必然回空。**用户的判断是对的：现场保存不该以 Agent 会话为前提。** 场景是浏览器报上来的，gateway 采纳它的时候就该留住它，与有没有 Agent 无关。

## 做了什么

**决议 Q160，就地收窄 Q111 那句"没有缓存回退分支"。**

- **规格就地修订**（`docs/Web前端需求规格v3.md`）：
  - §1.4 那条「它不是任何读取路由的回退来源」→「**面向调用方**的回退来源」，并写明唯一例外是绑定浏览器本人回来认领。
  - §5.8 `/getWorld` 那条拆成两条：第一条改成「**优先**只向绑定浏览器实时取」+「**面向调用方的** `cache` 回退整条删除」；**新增一条 Q160**，写全上面那条因果链、写明与 AgentBrain 无关、写明四个条件与「只读不采纳」。
  - §5.8 响应头那条：`X-World-Source: live`（Q160 快照分支为 `snapshot`）。
  - §5.9 路由表 `/getWorld` 行同步。
  - §5.13 那句「不得参与任何读取回退」加「面向调用方」，并说明 Q160 的读者是浏览器本人。
  - §5.4「启动一次、重连一次」那条补一句：**"空现场"必须是读出来的空，不是读失败留下的空**。
- **`local_gateway.py`（唯一改的代码文件，51 增 12 删）**：
  - `_world_response(...)` 多一个 `source: str = "live"` 形参。
  - `get_world()` 把实时取包进 `try`，失败时问一次新函数 `_reclaimed_world(request, browser_id, error)`；它答 `None` 就**原样重抛**（错误码、message、状态一个字不改）。
  - 新增 `_reclaimed_world()` —— **四个条件同时成立**才答快照：
    | # | 条件 | 缺了它会怎样 |
    |---|---|---|
    | ① | `error.status == 503 and error.code == "NO_BROWSER_CONNECTED"` | `504 BROWSER_TIMEOUT`（浏览器在线只是没答）会被一份旧文档顶替 |
    | ② | `browser_id == self.state.browser_id` | `?browser_id=别人` 这条断言失败的路会拿到现场 |
    | ③ | `request.headers["X-WebEditor-Browser-ID"] == browser_id` | **调用方**（Brain、脚本）会拿到"看起来像现场"的东西 —— Q111 正是禁这个 |
    | ④ | `scene_identity(world_cache) == self.state.scene_identity` | Q147 的真实危险：缓存与盘上身份描述两个场景，把没人认过的一对交出去 |
  - 快照是**只读补读**：不 `record_scene`、不动水位、不广播，`X-Playback-State` 恒 `stopped`。
  - `world_cache` 本来就挂在 `BrowserProfile` 上（Q153 的 profile 分区），所以**天然按 `browser_id` 隔离**，不需要新增任何持久字段。
- **`tests/test_get_world_snapshot.py`（新增，11 例）**：4 支认领（正文逐字、头与正文同源、只读不采纳不广播、空场景仍认领为空）+ 7 支拒绝（无身份头、身份头是别人、`?browser_id` 指别人、在线不答 ⇒ 504、在线 ⇒ 仍走 live 且答的是新那份、缓存与持久身份失配、gateway 从没见过世界）。

**前端一个字节没改** —— `loadAuthoritativeWorld()` 与 `withBrowserIDHeader()` 早就是对的，缺的是 gateway 那一侧的答。

### 不做

- **不改 gateway 重启的行为**：`world_cache` 是进程内存（§1.4），`af9c90c` 的「真实进程启动即清空上一轮持久态」照旧。本轮修的是**浏览器刷新**，重启后仍是 `503` + 空场景，那是另一条口径。
- **不把世界写进 `gateway_state.json`**：`record_scene` 只存四元组与水位，本轮不动它 —— 用户的场景里 gateway 一直活着，进程内那份就够，落盘会让每次本地编辑都重写一份几十 KB 的状态文件。
- **不动 `_session_context(...).world_snapshot` 那条出口**：它服务的是后台会话（§5.2），与本轮无关，原样保留。
- **不改 `loadAuthoritativeWorld()` 的 catch**：读失败不猜场景是对的，本轮让它读得到，而不是让它失败时装作有。
- **不修 `test_gateway_routes.py` 那支既有红**（见下），不改 `config/constant_config.json`。
- 不新增路由、功能键、常量或协议字段；`types.ts` / `contracts/types.ts` 未动；`audit_parity.py` 的请求面不增不减。

## 验证

| 命令 | 结果 |
|---|---|
| `python -X utf8 -u -m unittest discover -s tests -p test_get_world_snapshot.py` | **11 例全绿** |
| 同上 `-p test_caller_apis.py` | 22 例全绿（含 Q111 那两支「无缓存回退」原判据） |
| 同上 `-p test_error_mapping.py` | 8 例全绿 |
| 同上 `-p test_gateway_sessions.py` | 59 例全绿 |
| 同上 `-p test_commit_world.py` | 25 例全绿 |
| 同上 `-p test_capture_identity.py` | 15 例全绿 |
| 同上 `-p test_traj_api.py` | 19 例全绿 |
| 同上 `-p test_gateway_agent.py` | 69 例全绿 |
| 同上 `-p test_bbox.py` | 16 例全绿 |
| 同上 `-p test_capture.py` | 15 例全绿 |
| 同上 `-p test_gateway_routes.py` | 14 例 **1 红**：`test_default_config_file_matches_the_built_in_defaults`（`config/constant_config.json` 与 `DEFAULT_CONSTANT_CONFIG` 的一个布尔不一致）。**已 A/B 归因**：`git show HEAD:local_gateway.py` 覆盖后复跑，同一支同样红 ⇒ 先于本轮；还原后 `md5sum -c` OK |
| 同上 `-p test_ws_protocol.py` | **卡死**（90 s 超时，输出 0 字节）。**已 A/B 归因**：HEAD 侧同样 90 s 超时、同样 0 字节 ⇒ 先于本轮，与 `test_gateway_state` 同族（坑：Windows 上必须逐模块跑） |

合计 **262 例既有 + 11 例新增**，除上面那支既有红与那支既有卡死外全绿。

`python tools/audit_parity.py` 同样**卡死**（120 s 超时、输出 0 字节），HEAD 侧逐字相同 ⇒ 与 `test_ws_protocol` 同族的既有问题，先于本轮。**请求面不增不减**：本轮没有新增/删除任何路由、查询参数或请求体字段，只给 `/getWorld` 的 `X-World-Source` 增加了一个取值 `snapshot`，且只在浏览器自证时出现。

**顺带把审计派生件 `docs/api-inventory.json` 补齐并入库**（用户要求）：`python tools/parity_inventory.py` 重跑，退出 0（§5.9 路由全部注册、§10.1 路由全部不存在），37 条本地路由 / 8 个 browser action / 88 个功能键。与上一份入库快照的差异只有两类 —— 绝大多数是 `line` 行号漂移，唯一的实质新增是 **`GET /speech`**（`bdc9af4` 语音那一轮加了路由但没重跑派生件，本轮补上）。**`docs/feature-inventory.csv` 不入库**：它这次的两处变化（`panels.assets.metadata` 转 `DECLARED_UNWIRED`、`viewport.worldAxes` 默认值翻成 `False`）是别的会话在改的东西，不属于本轮。

**负控制（四次，每次只摘一处，`var/nc160/` 备份 + `md5sum -c` 还原）**：

| 摘掉的东西 | 期望 | 实测 |
|---|---|---|
| ③ 身份头那两行 | 只红「无身份头」「身份头是别人」 | **恰好 2 红 / 9 绿** |
| ① 改成 `error.status not in (503, 504)` | 只红「在线不答 ⇒ 504」 | **恰好 1 红 / 10 绿** |
| ④ 身份比对那两行 | 只红「缓存与持久身份失配」 | **恰好 1 红 / 10 绿** |
| 整个 `_reclaimed_world` 调用（= 修复前行为） | 只红四支认领用例 | **恰好 4 红（3 fail + 1 error）/ 7 绿** |

四次还原后 `local_gateway.py: OK`，复跑 11 例全绿。

**真机取证（一次性栈 8098 + 自带 OLS 8096，全程不起 AgentBrain；用户的 8000/8080/8099/8500 未动）**：

| 步骤 | 实测 |
|---|---|
| 全新页面 | `#0 · v0 · 0 objects`（`/getWorld` 503，符合预期：还没人报过） |
| 面板点 `ur5_table_apple` | `#2 ur5_table_apple · v3 · 9 objects` |
| **刷新第 1 次** | `#2 ur5_table_apple · v3 · 9 objects`，`/getWorld` **200 `X-World-Source: snapshot`** |
| **刷新第 2 次** | 同上，仍是 `snapshot` |
| 改点 `ur5_table_cabinet` 后刷新 | `#4 ur5_table_cabinet · v3 · 11 objects`，`snapshot` |
| 另开一个浏览器 context（另一个 `browserID`） | `#0 · v0 · 0 objects` —— profile 隔离未被破坏 |
| 刷新后 console | **0 条 error / warning**（修复前是 503 + `[startup] could not read authoritative world`） |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-11 |
| 落地 | 本地提交（主题 `fix(gateway)`，Q160） |
| 验证 | 新增 11 例全绿 + 十个既有模块 262 例（1 支既有红、1 支既有卡死，均已 A/B 归因）；四次负控制各自精确命中后按校验和还原；真机连刷两次 + 换场景后刷新 + 另 profile 全过 |