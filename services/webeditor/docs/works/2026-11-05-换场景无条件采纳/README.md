# 2026-11-05 换场景无条件采纳（同事联调 2 bug）

## 原因

同事报两个 bug，怀疑与今天早些时候的 `e2baa6b`（切场景实时上报）同因。判定：**不同因**。`e2baa6b` 修的是**同一 `scene_id`** 下四元组词典序不增被 `adoption_verdict` 判 `browser_scene_version_conflict` 静默拒收；而这两个 bug 走的是**不同 `scene_id`** 那一支——预设场景 `scene_id` 各不相同（`ur5e_table_empty`=1、`ur5e_table_apple`=2、`ur5e_table_push_pull`=7…），压根不碰子版本比较。两条分支、两个根因。

- **问题 1（gateway 停在旧现场）**：浏览器界面显示空场景 `ur5e_table_empty`（`scene_id=1`），gateway 却还握着任务 T01.V01 加过苹果的那份旧现场（`scene_id=2`）；API 发的命令按旧现场算碰撞 ⇒ 失败。
- **问题 2（切场景闪回）**：资产面板先载场景1、再切场景2，载入后立刻闪回场景1；F12 看 `getWorld` 永远返回场景1；时好时坏，有一次"过一会儿自己好了"。

排查到的**同一个真根因**在 `gateway_state.py:adoption_verdict` 的换场景分支：

```python
if incoming["scene_id"] != stored["scene_id"]:
    if phase != "setup" or scene_held:
        return "browser_new_session_required"   # 整条丢弃：不缓存、不广播、不动水位
    return "adopt"
```

`reset_after_gateway_restart()` 只在**没有**租约／隔离时才清掉持久身份。Gateway 重启后若从盘上读回一份**永不释放**的租约／隔离（持有 `leaseToken` 的 Brain 会话早已随旧进程消失，`/releaseSceneLease` 再也无人来调），`scene_held` 永远为真 ⇒ 每次换场景都被拒 ⇒ 持久 `scene_identity` 与 `world_cache` 永远停在浏览器已经离开的那份旧现场。于是：

- `/getWorld` 的会话快照分支只在 `context.scene_identity == state.scene_identity` 时命中（`local_gateway.py:3361`），身份停在旧现场 ⇒ API 拿到的就是旧现场（问题 1）；
- `camera_candidates` 与身份配对（`_camera_candidate_view`）失配读空；
- 浏览器切到新场景后 `reportWorld` 被拒、不广播，任何一次 `scene.applied` 回声或重连的 `loadAuthoritativeWorld` 触发 `adoptWorld` 的四元组不匹配分支 ⇒ 重读 `/getWorld` 拿回旧现场、`state.world` 被覆盖 ⇒ **闪回**（问题 2）。"过一会儿自己好了"= 那份陈旧租约／隔离最终被显式恢复流程清掉，`scene_held` 转假，切换重新被采纳。

## 用户口径（原话）

> 我希望切换到不同 scene_id 也能正常接收。当有新的场景进入时，可以忽略新场景和旧场景的 scene_version 和 scene_sub_version 的差异。

## 做了什么

- `gateway_state.py:adoption_verdict`：换场景分支改**无条件 `return "adopt"`**——`scene_id` 一变就采纳，两份场景各自的 `scene_version`／`scene_sub_version` 不参与比较（跨场景比没有意义）。删掉已无人使用的 `scene_held` 形参；同 `scene_id` 那一支（幂等／词典序递增采纳／`manual_edit_forbidden`／`browser_scene_version_conflict`）原样保留。
- `local_gateway.py`：两处调用点（`apply_state_sync`、`_adopt_live_world`）随之去掉 `scene_held` 实参；`apply_state_sync` 里那段只为换场景算的 `scene_held_for_switch` 一并删除。
- **Agent 的锁没动，仍在它该生效的地方**：写入由**租约指纹**把关（一次迟到的旧写照旧 `browser_scene_version_conflict`，`_acquire_scene_lease_locked` 的 `current != fingerprint` 不变）；会话由 `_ensure_automatic_session` 在 `scene_held`／`agent_owned` 期**拒绝新开**（`local_gateway.py:2037`）——采纳身份绝不偷偷起 Brain 会话。`browser_new_session_required` 这枚码现在只属于 `/sessions` 路由（换相机／会话），不再是 `browser.state_sync` 的答复。
- 浏览器侧**无需改动**：gateway 采纳并广播新场景后，`adoptWorld` 看到自己那份四元组已相等 ⇒ 不再重读旧缓存 ⇒ 闪回消失。`e2baa6b` 的 `rebaseSubVersionAfterSwitch` 仍保留（同 `scene_id` 的两个预设之间切换仍靠它推进子版本）。

## 验证（真实数据）

同时起两份 gateway、喂同一份"重启后握着苹果现场（`scene_id=2`）+ 未释放租约"的种子态，用真实 OLS 场景 id 发同一条 `browser.state_sync(scene_id=1)`：

| | 修复前（HEAD 代码，`:8097`） | 修复后（工作树代码，`:8099`） |
|---|---|---|
| 日志 | `browser.state_sync refused (browser_new_session_required): reported scene_id=1 … against persisted scene_id=2` | 无拒绝行 |
| 持久身份 | 停在 `scene_id=2`（旧现场） | 跟着浏览器实到的现场走 |
| `/getWorld` | 把旧现场交给 API | 返回浏览器当前现场 |

内置浏览器（连的是修复后 gateway）在持租约现场重连后，其上报被采纳、身份从 `scene_id=2` 移到浏览器实到的现场；桌面版资产抽屉在内建窄浏览器里渲染不全，端到端 UI 走查受此限制，故以上述真实 gateway 对照 + 单测为准。

- `tests/test_gateway_state.py`：`AdoptionVerdictTests` 改 4 支（换场景在 `setup`／`agent_owned`／更低版本三态一律采纳；同场景那一支仍按词典序判）；`PersistentGatewayTests` 两支路由面测试由"租约期／`agent_owned` 期换场景被拒、身份留在 7"改写为"换场景**都落地**、身份与缓存跟着走、且不在持锁现场自动建会话"。
- 全绿：`test_gateway_state.py` 75 例、`test_gateway_sessions.py` 59、`test_scene_lease.py` 37、`test_gateway_agent.py` 69；`vitest` 18 文件 634 例。

## 与上一张卡的关系

本卡**取代** [2026-11-05-切场景实时上报](../2026-11-05-切场景实时上报/README.md) 里"`agent_owned`／持租约／隔离期切换仍维持静默拒绝、不改为强制覆盖"那条边界——那是当时确认的取向，同事联调后证明它在"重启恢复出永不释放的锁"这一场景下正是死锁之源，本轮据用户明确口径改为无条件采纳。规格 §5.8、流程文档 §1.2 第 4 条已就地改写。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本地提交（master，未 push） |
| 验证 | 见上「验证（真实数据）」：两份 gateway 真实对照 + 4 个 Python 测试文件 + vitest 全绿 |
