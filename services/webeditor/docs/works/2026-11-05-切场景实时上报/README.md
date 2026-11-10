# 2026-11-05 切场景实时上报

## 原因

用户原话：切换机械臂或切换预设场景后刷新浏览器，看到的依然是切换前的场景。怀疑切换是浏览器本地操作、没上报给 gateway，导致 gateway 拿不到最新现场。

排查后的真实现象：两条切换路径其实都已经调用 `reportWorld()`（发 `browser.state_sync`）——换臂走 `placeAsset → updateLocalItem/addLocalObject` 会推进子版本，预设场景走 `applyWorld` 整份替换。问题出在**预设场景切换不推进 `scene_sub_version`**：`applyWorld` 用解析出的新世界整体替换 `state.world`，新世界的子版本按文件取值（OLS 预设文件通常不带该键 ⇒ 读作 0）。当两个预设的 `scene_id`/`scene_version` 相同时（预设文件常不带独立 `scene_id`，缺省即 0），上报的四元组 `(scene_version, scene_sub_version)` 相对上一份**词典序不增**，gateway 判 `browser_scene_version_conflict` **静默拒收**（`gateway_state.py:adoption_verdict` 第 140 行、`local_gateway.py:apply_state_sync` 仅 `LOGGER.warning`、不广播 `scene.applied`）。拒收 ⇒ 不写 `world_cache` ⇒ 刷新时 `loadAuthoritativeWorld()` 的 `GET /getWorld` 读回旧场景。

规格 §4.8（Q114 ②）本就把「切场景」列为 ② 类、要求"成功后 `scene_sub_version +1` 并走 `browser.state_sync` 整份上报"（`docs/Web前端需求规格v3.md` 第 212 行）。**这是实现落后于既有规格，不是改口径**，故本轮不改规格正文。

## 做了什么

- `editor/src/world.ts`：新增 `World.rebaseSubVersionAfterSwitch(previousSubVersion)`——把新世界 `meta.scene_sub_version` 设为「切换前那一份 +1」，并置 `explicitSubVersion = true` 强制 `toJSON()` 写出该键。基准取「离开的那份」而非文件自带值，保证四元组严格递增。
- `editor/src/main.ts:chooseLibraryItem` 的 `kind === "scene"` 分支：`parseWorld` 后先 `world.rebaseSubVersionAfterSwitch(state.world.subVersion)` 再 `applyWorld(world)`（其内已 `reportWorld()`）。预设场景切换由此推进子版本并实时上报。
- 换臂路径（`placeAsset`）已 +1 且已上报，未改动。

明确的边界（沿用规格既有口径，未动）：
- **上报异步、失败静默**：`reportWorld()` 本就是非阻塞；socket 未开时最多 100ms×30 次重试后放弃，gateway 拒收只记 warning，均不弹窗——符合用户"失败也不弹窗报错"的要求。
- **`agent_owned` / 持租约 / 隔离期切换仍维持静默拒绝**（`adoption_verdict` 回 `browser_new_session_required` / `manual_edit_forbidden`），与三层防线一致。用户已确认此取向，不改为强制覆盖。**〔2026-11-05 当日稍后被取代：见 [换场景无条件采纳](../2026-11-05-换场景无条件采纳/README.md)。同事联调证明"重启恢复出永不释放的租约／隔离"这一场景下，换场景被静默拒正是死锁之源，`scene_id` 变化的那一支已改为无条件采纳；本卡针对的同 `scene_id` 子版本推进不受影响，仍然有效。〕**

判据：同 `scene_id` 的两个预设来回切，第二次上报子版本 = 第一次 +1、`scene_version` 不动 ⇒ gateway `(...,sub+1) > (...,sub)` 采纳 ⇒ 刷新读回新场景。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本地提交（master，未 push） |
| 验证 | `npx vitest run --config editor/vitest.config.ts` 18 文件 620 例全绿（新增 `world.test.ts`「rebases the sub-version one step above the previous scene when switching presets」）；`npx tsc -p editor/tsconfig.json --noEmit` 干净 |
