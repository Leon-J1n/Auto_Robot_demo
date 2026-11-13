# 2026-11-11 资产面板打开时折叠 Agent 面板

## 原因

用户原话：「当资产面板打开时，暂时折叠 Agent 面板，但资产面板关闭时再恢复显示。」

Q156 之后导航栏上只剩三枚资产大类按钮，资产面板成了左栏唯一的内容，而右侧 Agent 那一栏在挑资产时用不上却让视口窄一截。折叠是**借栏**，不是关断能力。

## 做了什么

- **就地修订规格 §7.5**，新增一条口径（决议号 **Q157**）：资产面板确实展开时 `.agent-panel` 置 `hidden`、`.workspace` 的 `--agent-track` 置 `0px`；收起或切到 `scene`/`details` 时按同一判据恢复。顺带把该节抬头的「五条口径」改成「七条口径」（原文早已列了六条，本轮再加一条）。
- `editor/src/logic.ts:agentPanelVisible({agentEnabled, contextEnabled, panel, panelOpen})`：唯一判据。`contextEnabled` 必须入参 —— `panels.context` 关断时资产面板本就不显示，此时若照样折叠 Agent，工作区会剩两栏空白。
- `editor/src/main.ts:applyAgentVisibility()`：唯一写者，同时写宿主 `hidden` 与栅格 `--agent-track`（可见时**移除**内联值让样式表默认值生效，不是写一个数）。两处调用：
  - `applyFeatureVisibility()`（`start()` 里那一次），位置在 `[data-feature]` 通用循环**之后** —— `.agent-panel` 自带 `data-feature="agent"`，通用循环会先按 `agent` 键写一遍 `hidden`，谁在后面谁说话。
  - `renderContext()`（每次开合/切大类）。只改前者的症状是「加载时对、点一下就不对」，且不抛错。
- 视口尺寸不用额外通知：`main.ts:232` 的 `ResizeObserver` 早就在观察画布父节点，栅格一变自动 `renderer.resize()`。
- 测试：
  - `editor/src/logic.test.ts` 新增 4 支（`the Agent column is lent to the asset panel`）：非资产视图/面板收起时归还、资产展开时借走、`agent.conversation` 关断时始终不显示、`panels.context` 关断时不借。
  - `editor/src/agent_details.test.ts` 新增 3 支源码级接线用例：写者本身问了共享判据且写了宿主与栅格两处；两个渲染路径都调用它；**全文除这个写者之外没有第二处**写 `.agent-panel.hidden` 或 `--agent-track`。

### 不做

- 不新增功能键：`agent.conversation` 仍是 Agent 面板唯一的远程出口，折叠只是布局。
- 不动会话、WebSocket、语音、toast：折叠期间照常运行，恢复后消息原样可见（真机上 `.agent-empty` 节点在折叠态仍在 DOM 里，计数 1）。
- 不改 `--agent-track` 的样式表默认值，也不改三档媒体查询里的 Agent 布局。
- 不动资产面板自身的宽度（那是下一轮双栏的事）。

## 验证

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 错误 |
| `npx vitest run --config editor/vitest.config.ts` | **24 文件 / 721 例全绿**（本轮 +7：logic 4、agent_details 3） |
| `npx vite build --config editor/vite.config.ts` | `index-T2n8xECn.css` 21.84 kB（gzip 5.14，与上轮同）；`index-BUhf-SB0.js` 907.49 kB（gzip 250.67，+0.31 kB / +0.10 kB） |

**负控制**（两次，各自只碰判据唯一所在的那一行）：

| 摘掉的东西 | 结果 |
|---|---|
| `agentPanelVisible` 里 `view.contextEnabled &&` | 恰好「does not take the column when the context panel is switched off」1 支红（1 红 / 99 绿） |
| `renderContext()` 里那句 `applyAgentVisibility();` | 恰好「is called by the startup feature pass and by every context render」1 支红（1 红 / 11 绿） |

两次都从 `var/nc3/` 的备份还原，`md5sum -c` 全部 `OK`，复跑 721 例全绿。

**真机取证**（一次性栈：`tools/serve_verification.py --port 8098 --library-port 8096`，1440×1000）：

| 状态 | `.agent-panel.hidden` | Agent 宽 | `--agent-track` | 栅格四栏（px） | 视口宽 |
|---|---|---|---|---|---|
| 静止（未开面板） | false | 376 | （空） | 69.75 / 0 / 994.5 / 375.75 | 995 |
| 开预设场景 | **true** | 0 | `0px` | 69.75 / 321.75 / 1048.5 / **0** | 1049 |
| 切到预制模型 | true | 0 | `0px` | — | — |
| 再按预制模型（收起） | **false** | 376 | （空） | 69.75 / 0 / 994.5 / 375.75 | 995 |
| 开/收机械臂再走一遍 | true → false | — | — | — | — |

视口宽度的判据是**算术**而不是「变宽了」：`995 + 376 - 322 = 1049`，让出来的 Agent 栏要与刚打开的左栏分账。`pageerror` / console error 0 条（`/getWorld` 的 503 是一次性网关还没有现场，按 URL 排除）。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-11 |
| 落地 | 本地提交（主题 `feat(agent)`，Q157） |
| 验证 | tsc 0 错；vitest 721/721；两次负控制各 1 红后按校验和还原；真机借栏/还栏两个方向各走两遍 |