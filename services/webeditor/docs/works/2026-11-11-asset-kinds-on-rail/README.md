# 2026-11-11 三大类提升到导航栏、资产面板只剩一层分类

## 原因

用户原话：「将场景、模型、机器人三个大分类从资产面板提升到 navigation 导航栏中，替换掉原来的 Asset 按钮。然后资产面板中原本的双层分类变为一层（由于大分类已经提升到导航栏，因此新的面板中的小分类是 OLS 数据中的第二级）。」

这两句在代码上是**同一处改动**：面板里的第一层竖向栏（`.asset-kinds`）就是那三个大类，把它搬到导航栏之后，面板内剩下的那条 `.asset-categories` 本来就已经是 OLS `category` 的第二级（`assets.ts:categoryLabel` 取第一个点之后的部分）。所以「提升大类」与「双层变一层」是一个提交，不是两个。

## 做了什么

- **就地修订规格**：
  - §4.6 那条面板形状 bullet 改判 **Q93** 的「两级竖向层级栏」，写成「大类在导航栏、面板内只剩一条分类栏，列 OLS `category` 的第二级，未分类排最后」。决议号 **Q156**。
  - §4.7 新增一条：三个导航按钮复用既有功能键 `navigation.assets.presets/.prefabs/.robots`，都带 `data-panel="assets"` + `data-asset-kind-nav="<kind>"`；按下 = 打开并切到该大类，再按当前大类 = 收起，开着时按另一个 = 只切换（分类重置为新大类的第一个小类）。三者都是 R1，不受阶段闸限制。
  - §7.2 第一条改写成「`active` / `aria-expanded` 要按**大类**判，不能只看 `data-panel`」。
- **功能键集合不增不减**：三个按钮用的就是原来面板内层那三个键，祖先 `navigation.assets`、`panels.assets` 原样保留，因此 `features.json` 不改、`docs/feature-inventory.csv` **不加行**。`navigation.assets` 从此只作为祖先键存在（自己没有 DOM 宿主），CSV 里它那一行的引用方式随之从 `literal` 变成 `interpolated`（只改这一行）；`tools/parity_inventory.py` RC=0。
  - 顺带说明：跑 `parity_inventory.py` 还会改 `docs/api-inventory.json`（全是行号漂移，来自别轮对 `local_gateway.py` 的改动）和 CSV 另外两行（`panels.assets.metadata` 在 HEAD 里就已无引用、`viewport.worldAxes` 的开关值早就变了）—— 这三处都是**先于本轮**的漂移，本轮按 hunk 只提交自己那一行，其余留在工作区不动。
- `editor/src/logic.ts` 新增两支纯判据：
  - `panelTabPress(current, panel, kind?)`：一次按压后的开合与大类。**「同一个 tab」= (panel, kind)**，否则从预设场景切到机械臂会被读成「再按一次同一个 tab」，把用户刚要看的面板关掉。
  - `panelTabActive(tab, current)`：同一判据的反向读法，供 `active` 类用。少了 kind 那一半，三个按钮会一起亮 —— 它们共用一个 `data-panel`。
- `editor/src/main.ts`：
  - 导航栏模板用 `LIBRARY_KINDS.map` 生成三个按钮（图标 `GROUP_ICONS`：`boxes` / `box` / `bot`），删掉原来的 `Assets` 按钮。
  - `renderAssets()` 去掉内层大类栏，只留 `.asset-categories` + `.asset-list`；新增 `currentKind()`，因为功能键可能把存着的那个大类关掉，此时落到第一个开着的大类。
  - `renderContext()` 的 `[data-panel]` 循环改用 `panelTabActive`；点击委托里 `data-panel` 那一支改用 `panelTabPress`，切换大类时把 `state.library.category` 置空。**删掉**原先独立的 `if (target.dataset.assetKindNav)` 分支（坑 106：宿主搬走了，它的查询点必须一起走）。
- `editor/src/styles.css`：删 `.asset-kinds` / `.asset-kind` 两族。这两族与仍在用的 `.asset-categories` / `.asset-category` **挤在同一行**，按坑 104 逐条只摘自己那半，留下的规则每条声明都在。
- `tools/browser_smoke.cjs`：§4.6 的入口改成点导航栏的 `[data-asset-kind-nav="scene"]`；`showAssetsPanel()` 换成 `showAssetKind(kind)`（已亮就不点，避免把面板点关）；分类点击落到 `.asset-category[data-asset-category=…]`；标签断言改读 `.rail [data-asset-kind-nav] span`；形状断言新增 `innerKindRail === 0`。

### 不做

- 不改 `#panel-count`：它仍报三类资产的总条数（本轮真机上是 58），不是当前大类的条数。改它会连带动 `browser_smoke.cjs` 的总数断言，超出本轮范围。
- 不动缩略图尺寸、名称字号、列表栏数（那是后两轮的事）。
- 不新增路由、常量、功能键、共享协议字段；Gateway 一个字未改。

## 验证

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 错误 |
| `npx vitest run --config editor/vitest.config.ts` | **24 文件 / 714 例全绿**（本轮 +7：logic 6、asset_rows 1） |
| `npx vite build --config editor/vite.config.ts` | `index-T2n8xECn.css` 21.84 kB（gzip 5.14）；`index-D-MvrdbN.js` 907.18 kB（gzip 250.57） |
| `python tools/parity_inventory.py` | RC=0，`features_local: 88`，未引用键仍是那三条既有的 |
| `node --check tools/browser_smoke.cjs` | OK |
| `grep -c '\.asset-kind' editor/src/styles.css` | 0 命中（坑 104 的静态侧） |

**负控制**（三次，各自只碰判据唯一所在的那一行）：

| 摘掉的东西 | 结果 |
|---|---|
| `panelTabPress` 里 `same` 的 `kind === current.kind` 那一半 | 恰好「switches the 大类 without closing the panel」1 支红 |
| `panelTabActive` 里 `tab.kind === current.kind` 那一半 | 恰好「lights exactly one of the three asset buttons」1 支红 |
| `styles.css` 里 `.asset-category.active` 改名成 `.lit`（模拟坑 104 的连坐：选中态那条规则被摘掉） | 恰好 asset_rows 的 Q156 那支红（1 红 / 1 绿） |

三次都从 `var/nc/` 的备份还原，`md5sum -c` 全部 `OK`，复跑全绿。

**真机取证**（一次性栈：`python tools/serve_verification.py --port 8098 --library-port 8096`，自带 OLS，服务 `dist/`；用户自己的 8000/8080/8099/8500 全程未碰）：

| 断言 | 实测 |
|---|---|
| 导航栏三个大类的文字 | `["预设场景","预制模型","机械臂"]`，`Assets` 按钮 0 个 |
| 打开预设场景后面板内的竖向栏数 | 1 条；`.asset-kinds` / `.asset-kind` 节点 **0** 个；卡片 7 张 |
| 场景卡片名（同时是 Q155 的真机证据） | 空场景 / 苹果夹取 / 收纳物品 / 开柜门 / 双臂夹取 / 倒水按压 / 拉抽屉 |
| 按预制模型 | 面板**保持打开**，只有它亮；分类栏是第二级：实验器材3 / 家具3 / 工具3 / 文具3 / 果蔬6 / 玩具5 |
| 按机械臂 | 同上，分类栏：单臂6 / 夹爪3 / 摄像机2 |
| 再按机械臂 | 面板收起（`#context-panel.collapsed`），三个按钮全不亮 |
| 再按机械臂 | 重新打开并记得大类 |
| `pageerror` / console error | 除 `/getWorld` 的 503（一次性网关还没有现场）外 0 条 |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-11 |
| 落地 | 本地提交（主题 `feat(assets)`，Q156） |
| 验证 | tsc 0 错；vitest 714/714；三次负控制各 1 红后按校验和还原；真机 8 条断言全过 |