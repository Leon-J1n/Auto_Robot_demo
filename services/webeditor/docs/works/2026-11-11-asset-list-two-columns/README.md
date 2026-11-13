# 2026-11-11 资产列表改双栏

## 原因

用户原话：「把资产列表（原本是单栏）更改成双栏。」

Q154 当时把面板项做成"一行一个条目、左侧 52rem 见方缩略图"，并在规格里写死"不做卡片网格"。本轮按用户要求改判那句：双栏 + 缩略图在上、名称在下，图变大成为扫视的主体。双栏需要宽度，而 Q157 刚好在资产面板展开时把 Agent 那一栏借了过来 —— 本轮把这份宽度用掉。

## 做了什么

- **就地修订规格**：
  - §4.6 那条面板项 bullet：删掉「缩略图 `52rem × 52rem`、行最小高度 `64rem`」，改写为双栏栅格 + 缩略图占满卡片内宽且 `aspect-ratio:1/1` + 名称仍 `13rem`；并写明展开期间 `.workspace` 带 `assets-open` 类把 `--context-track` 放宽到 `minmax(300rem,360rem)`，**只在 `min-width:1051px` 生效**。决议号 **Q158**（改判 **Q154** 的"不做卡片网格"）。
  - §12.7 那句「每一行一个条目（缩略图 + 名称 + `id · category`）」同步改成双栏卡片口径（`id · category` 早在 Q154 就已撤下，这里顺手对齐）。
- `editor/src/styles.css`：
  - `.asset-rows` 从 `flex` 竖排改成 `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6rem;align-content:start`；`.asset-rows>.empty-line { grid-column:1/-1 }` 让空态/告警跨两栏，不被挤进半幅。
  - `.asset-card` 改 `flex-direction:column`（图上文下），撤掉 `min-height:64rem`（高度由方图决定）。
  - `.thumb-frame` 从 `flex:0 0 52rem;width:52rem;height:52rem` 改成 `width:100%;aspect-ratio:1/1`；占位 `::before` 与 `.asset-thumb` 一字未动，取不到图仍降级为占位格子。
  - 新增 `@media(min-width:1051px){ .workspace.assets-open { --context-track:minmax(300rem,360rem); } }`。放在媒体查询里是因为窄屏三档（≤1279 / ≤1050 / ≤767）各有自己的栏宽，放宽只适用于宽屏那档。
- `editor/src/main.ts:renderContext()` 加一句 `workspace.classList.toggle("assets-open", panelVisible && state.panel === "assets")`。**用类而不是内联值**：面板收起时那句 `--context-track:0px` 是内联的，会盖住类规则；展开时内联被 `removeProperty` 移除，类规则才生效 —— 两者不打架。
- `editor/src/asset_rows.test.ts`：Q154 那支改判（断言方图新写法、断言 `width:52rem;height:52rem` **0 命中**、字号仍 13rem），新增 Q158 一支钉住栅格两列、卡片竖排、空态跨栏、`min-height:64rem` 0 命中、放宽规则带 `min-width:1051px` 前缀，以及 `renderContext` 里那句类开关。
- `tools/browser_smoke.cjs`：形状断言补三条真机读数 —— 计算样式 `gridTemplateColumns` 恰 **2** 段、卡片 `flexDirection === "column"`、`.workspace` 带 `assets-open`。原来那句 `gridCells === 0` 保留但改注释：`.asset-cards`（复数）是早已撤下的旧宿主，与本轮的 `.asset-rows` 栅格不是一回事。

### 不做

- 不改分类栏宽度（`58rem`）：真机上「桌面操作」仍省略成「桌面…」。这是 Q156 之前就有的样子，改它属于另一件事。
- 不改 `#panel-count`（仍是三类总数 58）。
- 不动 `assetRow()` 的 DOM 结构与寻址属性：`data-asset-kind` / `data-asset-id` 一字未改，点击行为因此不变（真机已验证第二栏的卡片照样载入场景）。
- 不新增功能键、常量、路由或协议字段；Gateway 一个字未改。

## 验证

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 错误 |
| `npx vitest run --config editor/vitest.config.ts` | **24 文件 / 722 例全绿**（本轮 +1，另改判 Q154 那支） |
| `npx vite build --config editor/vite.config.ts` | `index-BJ0BR_3v.css` 21.99 kB（gzip 5.19，+0.15 / +0.05）；`index-D9mmArig.js` 907.55 kB（gzip 250.68，+0.06 / +0.01） |
| `node --check tools/browser_smoke.cjs` | OK |

**负控制**（三次，各自只碰判据唯一所在的那一行）：

| 摘掉的东西 | 结果 |
|---|---|
| `repeat(2,…)` 改 `repeat(1,…)` | 恰好 Q158 那支红（1 红 / 2 绿） |
| `renderContext()` 里那句 `classList.toggle("assets-open", …)` | 恰好 Q158 那支红（1 红 / 2 绿） |
| `.thumb-frame` 去掉 `aspect-ratio:1/1` | 恰好改判后的 Q154 那支红（1 红 / 2 绿） |

三次都从 `var/nc4/` 的备份还原，`md5sum -c` 全部 `OK`，复跑 722 例全绿。

**真机取证**（一次性栈 8098 + 自带 OLS 8096）：

| 断言 | 实测（1440×1000） |
|---|---|
| 栅格列数与列宽 | `160.375px 160.375px`，两列等宽 |
| 卡片排布 | 第 1、2 张同一行（y 均 72），第 2 张在右（x 308 vs 141），第 3 张回到第一列下一行（x 141、y 266） |
| 缩略图 | 145×145（正方形，边长差 ≤1px），底边在名称顶边之上；比原来的 52rem 大得多 |
| 面板宽度 | 405px（`assets-open` 生效），Agent 仍 `hidden`（Q157 借栏照旧） |
| 横向溢出 | `.asset-list` 无横向溢出 |
| 预制模型 | 同样两列、无溢出 |
| 点击寻址 | 第二栏的场景卡片（id 2）照样载入 → `Connected · #2 ur5_table_apple · v3 · 9 objects` |
| 1000×800 窄屏 | 仍两列（`77.5px 77.5px`），面板宽度回到 230px（放宽规则不参与），无横向溢出 |
| 尺寸来回 | 视口改回 1440 后面板宽度回到 405px |

**一条先于本轮的噪声，已 A/B 归因**：从面板载入场景后 `/events` 会以 `Invalid frame header` 断开并在 console 留一条 error。把 `styles.css` 与 `main.ts` **换回 HEAD 的内容重新构建**后同一条探针复现同一串错误（`var/probe_ws_attrib.cjs`），因此与本轮无关；探针按 URL 排除它，而不是把 `errors` 断言整体放宽。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-11 |
| 落地 | 本地提交（主题 `feat(assets)`，Q158） |
| 验证 | tsc 0 错；vitest 722/722；三次负控制各 1 红后按校验和还原；真机宽/窄两档 + 点击寻址全过 |