# 2026-11-05 首连不再重读权威场景（闪现）

## 原因

同事报：在资产面板点开一个预设场景，场景**刚加载完就闪回/变空**，过一会儿（下一次重连）自己又回来了。用户口径：**「只修复闪现」**——只做最小改动，不碰采纳路径。

排查结论：`main.ts:connectWebSocket()` 的 `onopen` 在**每一次**打开（含首次）都跑 `void loadAuthoritativeWorld().then(() => reportWorld())`，而 `start()` 在建 socket **之前**已经 `await loadAuthoritativeWorld()` 读过一次（`main.ts:2443`）。于是首次打开的那一次是一次**多余的第二次读**：

- `loadAuthoritativeWorld()` 的落地动作是**整份覆盖** `state.world` + 一次 `renderer.syncScene()`；
- 一次 HTTP 读取的**答案允许晚于它出发的那一刻**回来，而点开预设场景后的资产加载正好跑在这个窗口里；
- 旧答案（Gateway 此刻仍握着的那份）落在刚摆好的新现场之上 ⇒ 屏幕上**闪回／变空**；下一次重连再读一次，场景又回来 ⇒ 看起来像"闪"。

同一轮里排查到的另一条候选（`adoptWorld` 的四元组不匹配分支去重读 `/getWorld`）**本轮不动**：那是 §5.8 既有规则，且它读回的是绑定浏览器的 **live** 答案；它之所以会端出旧现场，根子是同日上一卡（[2026-11-05-换场景无条件采纳](../2026-11-05-换场景无条件采纳/README.md)）修的"持久身份停在旧现场"，那条已经修完。Gateway 侧的 `world_cache` 回退分支同样**不在本轮范围**。

## 做了什么

- `main.ts`：在 `socket` 声明旁加模块级 `firstSocketOpenDone`（页面生命内只被置位一次）。`onopen` 里：
  - **首次打开** ⇒ 只 `reportWorld()`，**不再读**。`start()` 已经把权威现场读进来了，这里推的就是那一份。
  - **重连** ⇒ 保持**先 `loadAuthoritativeWorld()` 再 `reportWorld()`**。这一页聋过、Gateway 也可能换了进程，先读后报正是补听的意义——否则一次重启到协议空场景，会被上一进程的陈旧浏览器内存覆盖掉。
- **两条上报通路一条都没削**：§5.5/Q112 的"连接即重报，让重启过的 Gateway 不丢水位"两态都还在；空现场照样上报（那是一次合法的换场景，也照样负责清掉上一场景的相机候选）。`refreshSessionState()`／`refreshAgentJobs()` 不动，它们的补听判据本来就用现成的 `state.websocket === "reconnecting"`。
- 规格 §5.8 **就地新增一条**：「重读权威现场只发生在两处：启动一次、重连一次」，并写明为什么第一次不能再读、以及判据落在哪个测试文件。历史与改判关系写在本卡，不写进规格。
- **不做**：不改 `adoptWorld`、不改 gateway、不加 feature 键、不动路由清单 ⇒ `audit_parity.py` 探针**不增不减**。

## 验证

- **新增 `editor/src/socket_open.test.ts`（3 例）**：`main.ts` 一 import 就接 DOM，没有可导入面，所以按本仓库既有做法（`error_codes.test.ts`）**钉源码结构**——整个 `onopen` 体里 `loadAuthoritativeWorld(` 只出现一次且必须在 `firstSocketOpenDone` 为真那一支；首次打开那一支只有 `reportWorld()`；`reportWorld()` 两支各一次；`start()` 里那次 `await` 排在 `connectWebSocket()` 之前。
- **负控制（跑过，红→绿）**：把工作树临时改回"无条件先读再报"，前 2 例立刻红（第 3 例仍绿，说明它钉的是上报次序而不是分支）；改回后全绿，`main.ts` 与改动前逐字节一致。
- `npx tsc --noEmit` 0 错；`npx vitest run` **19 文件 637 例全绿**（本卡 +1 文件 +3 例）；`npx vite build` 成功（chunk>650kB 的告警是既有状态）。
- **未做真机走查**：内建浏览器／`tools/browser_smoke.cjs` 仍卡在 §12.4 的身份闸（见记忆卡「Smoke blocked by identity gate」），现象本身又是"要看时间窗"的竞态，人工复现口径写在下面，留给联调：点开预设场景 → 看是否还闪回；同时在 Network 里确认页面生命周期内 `/getWorld` 只有**启动那一次**（此后每一次重连才各一次）。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本地提交（master，未 push） |
| 验证 | `socket_open.test.ts` 3 例 + 负控制（改回旧行为即红）+ tsc 0 错 + vitest 637 例全绿 + vite build 成功；真机走查未做（身份闸阻塞） |
