# FEAT-2026-10-16-E —— UI 优化第一轮：撤掉整条顶栏

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-16 |
| 落地 | 本卡提交 |
| 依赖 | M6-2,M6-3,FEAT-2026-10-16-D |
| 决议号 | Q92、Q93（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-16e 新卡（第 ⑱ 张）：UI 优化第一轮 —— 撤掉整条顶栏（Q92）+ 资产面板改两级竖向层级栏（Q93）。用户原话：「取消顶栏，原本处于顶栏的 Load Scene 和 Load Traj 按钮移动到画布上方的工具栏…资产面板左侧应该是两个竖向的层级栏…列表区域每行只放一个物体或场景。取消刷新按钮和搜索栏」；决议在规格 §13.24，就地修订 §4.6/§7.1/§9.3（§7.1 改名「顶栏（已撤销）与视口工具栏」，§4.6 的过滤框+刷新按钮要求划掉）。Q92 四个去向：两个载入按钮进 `.viewport-toolbar` 最左（`data-action`/feature 键/`#scene-file`/`#traj-file` 一字不改）、连接指示归 `#status-primary`、品牌区随栏消失、Agent 面板本就在右栏；连带删 `--command-track`、`.command-bar`/`.brand*`/`.connection*` 与 `applyFeatureVisibility()` 那行 track，并把 `browser_smoke.cjs` 两处 `#connection-label` 等待改锚 `#status-primary`。Q93：`renderAssets()` 重写为一级 `.asset-kinds`（`data-asset-kind-nav`）+ 二级 `.asset-categories`（`data-asset-category`，显示 `categoryLabel()`）+ 每行一个 `.asset-card`；`state.library` 的 `filter`/`collapsed` 换成 `kind`/`category`，删 `filterAssets()`、加 `categoryLabel()`。键 84 → 79（只删不增），CSV 重生成 80 行、`features_unreferenced` 空。自查出的缺陷已修：载入按钮进栏后资产面板打开时 `#selection-label` 挤成 `W…`，改用 Range 量自然宽度 + `container-type:inline-size`/`@container(max-width:780px)` + `select{min-width:0}`（坑 59/60）。验证：`npm run build` ✓、vitest 330、python 158、`audit_parity.py` 52/52、`browser_smoke.cjs` 11/11（§4.6 报 `rails:{kinds:3,categories:1,rows:7,gridCells:0,withdrawn:0,topBar:0}`）、真机一次性栈 8099/5188/OLS 8000 截图取证

## 任务卡原文

### FEAT-2026-10-16e —— UI 优化第一轮：撤掉整条顶栏（Q92）+ 资产面板改两级竖向层级栏（Q93）（新卡；依赖 FEAT-2026-10-16d、M6-2、M6-3）

- **起因（用户原话）**：「下一步我们将进行UI优化，首先取消顶栏，原本处于顶栏的 Load Scene 和 Load Traj 按钮移动到画布上方的工具栏。然后对资产面板进行更新，更新后资产面板左侧应该是两个竖向的层级栏，第一层是场景/模型/机械臂，第二层是相应的子类，然后列表区域每行只放一个物体或场景。取消刷新按钮和搜索栏」。四件事一轮做完，决议 **Q92/Q93** 记在规格 **§13.24**，并**就地修订 §4.6、§7.1、§9.3**（§7.1 标题改为「顶栏（已撤销）与视口工具栏」，§4.6 里"必须支持名称过滤输入…与刷新按钮"整条划掉）。
- **Q92 的四个去向**：`main.ts` 模板里整段 `header.command-bar` 删除，`.workbench` 三行 grid 改两行。① 两个载入按钮进 `.viewport-toolbar` 最左，`data-action`、`viewport.loadScene`/`viewport.loadTraj`、隐藏输入 `#scene-file`/`#traj-file` 与两条处理逻辑一字不改；② 连接指示归底部 `#status-primary`（`updateStatus()` 里覆写 `#connection-label` 的两行删除 —— **撤控件不撤信息**，两处写的是同一件事）；③ 品牌区 `chrome.brand` 纯装饰，随栏消失；④ Agent 面板本来就在右栏 `aside`，位置没动过。**连带删**：`--command-track` 与 `@media(max-width:767px)` 里的 `grid-template-rows` 副本、`.command-bar`/`.brand*`/`.connection*` 规则、`applyFeatureVisibility()` 里那行 `track(workbench, "--command-track", …)`。
- **Q92 唯一的消费者风险已经处理**：`tools/browser_smoke.cjs` 有**两处**拿 `#connection-label` 当"WS 已连上"的锚点，顶栏一删这两处必挂。改锚 `#status-primary` 并以 `Connected` 起头匹配（该文本由 `updateStatus()` 拼装，与旧 `#connection-label` 同源，判据强度不降）。
- **Q93 面板结构**：`renderAssets()` 重写为 `asset-browser` 三块横向 —— `.asset-kinds`（一级：预设场景/预制模型/机械臂，写 `data-asset-kind-nav` + `aria-current`）、`.asset-categories`（二级：当前大类的子类，走 `/listCategory` 顺序、`未分类` 最后，写 `data-asset-category`，显示 `categoryLabel()` 去掉首段前缀、`title` 保留完整点号名）、`.asset-list`（每行一个条目 `.asset-card`：缩略图 + 名称 + `id · category`）。**状态从"折叠树"改成"两个选中项"**：`state.library` 的 `filter: string` 与 `collapsed: Set<string>` 换成 `kind: LibraryKind` + `category: string | null`，点击处理器两个分支各写一个字段后 `renderContext()`。新增 `assets.ts:categoryLabel()`（取首个 `.` 之后），**删除 `filterAssets()`**（连同它的两条单测）。
- **Q93 撤掉的两个控件为什么可以撤**：① 刷新按钮是 `start()` 里那句 `void refreshLibrary()` 的重复入口，页面装载即读 OLS，而 **坑 23** 修好的会话缓存问题已不存在；② 搜索框过滤的是"三组叠在一页滚动"的旧骨架，两级侧栏本身就是按 `category` 的定向浏览，`/listCategory` 给出的类别就是过滤器。§4.6 正文按"**撤按钮不撤能力**"就地改写，历史留在本卡。
- **键数 84 → 79（只删不增）**：`commandBar`、`commandBar.connection`、`chrome.brand`、`navigation.assets.refresh`、`panels.assets.search` 五键删除；`features.json` 与 `docs/feature-inventory.csv`（80 行 = 表头 + 79）由 `python tools/parity_inventory.py` 重生成，`features_unreferenced` 为空；`parity_inventory.py` 的分组前缀里那条已经没人读的 `commandBar`/`chrome` 一并删掉。**Q93 的两级侧栏不新增键**（一级用现存的 `navigation.assets.{presets,prefabs,robots}`，二级是数据不是开关）。
- **验证（真机，一次性栈 gateway 8099 + vite 5188 + OLS 8000，探针在仓库外 `scene5probe\ui92_probe.cjs`、`ui92b_label.cjs`，截图 `ui92\`、`ui92b\`）**：`toolbar 1440x900 width 995 overflow False`；`panel 1440x900 kinds ['预设场景7','预制模型34','机械臂14'] kindsClipped [] cats ['自定义7'] rows 7 rowH [52] hScroll False`；`railClicks`：切到模型一级后二级是 `['抓取物21','放置物13']`、行数 21，再点第二子类得 `active:'放置物13'`、首行 `basket4 · 模型.放置物`，加入世界后 `objects 9`。
- **验证（本卡自查出来的一个真缺陷，已修）**：两个载入按钮进栏后，**打开资产面板时** `#selection-label` 被挤成 `W…`（1440 窗口下工具栏只有 673 px）。第一次探针漏判，因为它在**面板关着**的页面上量、且用 `scrollWidth > clientWidth` 当截断判据 —— 被 `text-overflow:ellipsis` 裁掉的元素这两个值相等，永远读不出截断（**坑 59**）。改成用 `document.createRange()` 量文本自然宽度、并在资产面板打开的状态下量，1440/1280 复现 `truncated:true`。修法两条：`.viewport-shell` 声明 `container-type:inline-size` + `@container (max-width:780px)` 让载入按钮在**这一栏**窄时只留图标（`@media` 管的是窗口宽度，不是栏宽，**坑 60**），`.viewport-toolbar select` 补 `min-width:0` 让它先于文字让位。修后实测 1440/1280 `truncated:false`、`spills:false`；1100 窗口仍差 3 px（该宽度下资产面板已是浮层，按可接受处理）。
- **验证（回归链，全绿）**：`npm run build` 干净、`npm test` **330 例**、`python -X utf8 -m unittest discover -s tests` **158 例**、`python -X utf8 tools/audit_parity.py` **52/52**、`node tools/browser_smoke.cjs` **11/11**（§4.6 那条现在报 `rails:{kinds:3,categories:1,rows:7,gridCells:0,withdrawn:0,topBar:0}` —— 后三项分别钉住"行不是卡片网格""搜索框与刷新按钮已消失""顶栏已消失"）。
- **明确不做**：不改 `types.ts`/`contracts`、不动 §5.9 任何接口（顶栏从来不发请求，parity 判据不增不减）；OLS 侧零改动（`/listCategory` 早就发的是带层级的完整 `category`，前端只是不再自己拼折叠树）；§8 禁令文件一字不改；不给二级侧栏加键盘可达性/`role=tree`（与 **Q90** 同一口径，本轮没提就先不做）。
- **一轮收尾**：一次性栈起的 gateway 8099 与 vite 5188 逐个 `taskkill`（**坑 55**），`netstat` 确认无监听；**用户自己的 5173 / 8080 / OLS 8000 一律不停**，但要提醒：8080 那个 gateway 是本轮改动之前起的，`features.json` 只在启动时读一次（**坑 22**），要让它反映 79 键得自行重启；只本地提交、**不 push**；`examples/gateway_client_demo.py` 里用户自己的 `SPIN_TICK_HZ = 30.0` 改动与两份用户自有未跟踪文档不进本提交。
