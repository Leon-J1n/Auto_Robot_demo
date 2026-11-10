# 2026-11-05 gateway 启动清空持久态（下一轮以干净状态起）

## 需求（用户口径 · 原话）

> gateway 增加一项功能，当启动 gateway 时清理上一轮的记录、会话状态、保持的场景等信息，然后以一个干净的状态启动。为什么要在启动时清理？因为我们有可能要在程序退出后读取期间保存的一些信息来进行排查。所以在下一轮启动时直接清理并以干净的状态启动我认为是对当前程序影响最小的一种改法。

取向明确：**清空放在入口（启动时），不放在退出时**——跑完的那轮把状态留在盘上任人事后读取排查，下一轮 `main()` 才抹干净。这样既保证每轮干净起步，又保住"退出后仍可读"的排障窗口，对现有程序改动最小。

## 做了什么

一台 gateway 真正跨进程存活的只有两份磁盘产物：状态文件 `var/gateway_state.json`（绑定 `browserID`、`session_phase`、租约／隔离、场景身份、对象 id 高水位）和它旁边的回执目录 `var/gateway_transactions/`（一次 `/commitWorld` 一个文件）。会话（conversation）、job 表、`world_cache`、`camera_candidates` 本就都在进程内存里、重启自然清空，无需处理。

- `gateway_state.py`：新增 `TRANSACTIONS_DIRNAME`（回执目录名只在此处写死一次，写方／读方／清扫三者不再各敲一份字面量）、`transactions_dir_for(state_path)` 与 `clear_persisted_state(state_path)`（`gateway_state.py:161`／`:166`）。清扫**按这份状态文件自己的名字前缀 glob**：删状态文件、`_read` 挪出的 `.corrupt-*` 副本、崩溃残留的 `.tmp`，再整个删掉回执目录；同目录里的无关文件（日志、`ctab*`、探针产物）一概不碰。缺失即 no-op，返回被删名字供记日志。
- `local_gateway.py:main()`（`:3489`／`:3512`）：建 app 之前，默认调用 `clear_persisted_state(state_path)` 并记一行 `cleared previous round's state …`；`--keep-state`（`:3489`）显式跳过，日志改为 `carrying over the previous round`。receipts 目录构造点（`:688`）改走 `transactions_dir_for`。
- **构造器只读不清**：`GatewayState`／`StandaloneGateway` 原样重载持久态。清空严格钉在进程入口，恢复语义与排障窗口都依赖构造器仍会读——这一点有回归测试钉住（见下）。
- **与上一张卡的关系**：这条顺带从源头清掉"重启读回一份永不释放的租约／隔离"那类陈旧锁，正是 [换场景无条件采纳](../2026-11-05-换场景无条件采纳/README.md) 所依赖的干净起点；但无条件采纳那条照旧保留——它覆盖 `--keep-state` 带上一轮继续跑、以及一次性栈显式恢复出租约的情形，两者一预防一兜底。

## 验证（真实数据）

用真实 OLS 场景形状预置一份"带未释放租约 + 场景身份 + 一份回执 + 一个 `.corrupt` 副本"的临时状态目录，跑真 `main()` 两条：

| | `python local_gateway.py --state-path <临时>` | 再加 `--keep-state` |
|---|---|---|
| 启动日志 | `cleared previous round's state in …: gateway_state.json, gateway_transactions/` | `persistent state: … (--keep-state: carrying over the previous round)` |
| 状态文件 | 已删 | 保留 |
| 回执目录 | 已删 | 保留 |
| 之后 | 以默认干净起 | 带上一轮继续跑 |

一次性验证栈 `tools/serve_verification.py` 每次走 `tempfile.TemporaryDirectory()`、本就空，不受影响；测得 :8096／:8095 两个进程验完即停、端口释放、临时目录删净。

- `tests/test_gateway_state.py`：新增 `ClearPersistedStateTests` 四支——① 删状态文件／回执目录／`.corrupt-*`／`.tmp` 且**保留同目录无关文件**；② 清后新 `GatewayState` 回默认（`phase=setup`、`scene_identity=None`、`lease=None`、水位回 0）；③ 缺失路径 no-op；④ **回归**：`StandaloneGateway(state_path=已写文件)` 是**重载而非清空**（带租约时身份照读回、回执目录不被删）。
- 全绿：`test_gateway_state.py` 79 例（本轮 +4）、`test_gateway_sessions.py` 59、`test_scene_lease.py` 37、`test_gateway_agent.py` 69、`test_gateway_routes.py` 13。本轮纯 Python + 文档，未动 `editor/` 与契约面，vitest 基线不涉及。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本地提交（master，未 push） |
| 生效 | **需重启 gateway**（改的是入口逻辑）；`--keep-state` 为新的退出/续用开关 |
| 验证 | 见上：真 `main()` 两条对照（清空 vs 保留）+ `ClearPersistedStateTests` 四支 + 四个 Python 套件全绿 |
