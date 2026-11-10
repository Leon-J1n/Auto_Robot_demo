# FEAT-2026-10-16-D —— 播放条刻度：统一颜色、撤 hover 文字、点击定位

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-16 |
| 落地 | 本卡提交 |
| 依赖 | M4-4,M6-3,FEAT-2026-10-16-C |
| 决议号 | Q90、Q91（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-16d 新卡（第 ⑰ 张）：播放条刻度统一颜色 + 撤 hover 文字 + 点击定位（Q90），右侧读数留固定占位宽度（Q91）。用户原话：「不再区分他们的颜色，同时鼠标悬浮在其上时也不再显示文字信息。新增功能，当点击某个刻度时，把进度条拉到该刻度处。此外，我发现当右侧进度数字变化时，可能导致播放条长度变化，留出适当的空间让数字变化而不影响进度条长度」；决议在规格 §13.23，就地修订 §7.3 两条与 §9.3 一句。Q90：`renderPlaybackTicks()` 去 `title`、写 `data-seek-frame`，删 `.timeline-tick.attach` 配色（统一 `#4f7ca8`）与 `STEP_NAMES`/`stepLabel()`（阶段名是生成器约定不是 §8.2 字段），外扩热区从 `:hover::after` 改成常驻 `::after`（坑 58a），加 `cursor:pointer`；点击走 `pausePlayback(); showFrame(frame)`（与帧步进同路），不新增 feature 键 —— 刻度在 `.timeline-track[data-feature="playback.seek"]` 内，宿主关断即不可达，`features.json` 仍 78 键；DOM 保留 `step`/`attach` 类名作 §8.6 的断言痕迹。Q91：`#timeline-time{min-width:16ch}`/`#timeline-frame{min-width:12ch}`（flex 下没有宽度约束的读数会挤 `flex:1` 的轨道；`ch` 随字号缩放且读数本就 `tabular-nums`，`/`、空格窄于 `1ch` ⇒ 只多不少；用 `min-width` 不用 `width` 以免长 clip 切字）。验证：smoke §12.4 三条断言从 `title` 改读 `data-seek-frame` 并加"hover 全空 + 颜色 Set size 1"，`node tools/browser_smoke.cjs` 11 项全绿（`{ticks:5, widthBeforeSeek:280, widthAfterSeek:280}`，点第一根 Attach ⇒ `340 / 719`）；真机一次性栈（gateway 8099 + vite 5188 + OLS 8000，探针 `scene5probe\tick90\bar_probe.cjs`）载入九段 clip ⇒ 11 根刻度单色 `rgb(79,124,168)`、无 hover 文字、`cursor:pointer`，轨道宽度在帧 0/5/839 均为 601 px，点 630/750/390 三根均正确跳帧且停在暂停态，`pageerror` 空，截图 `bar-hover-attach.png`。新坑 58（热区不能挂 `:hover`；step 与 Attach 同帧重叠 ⇒ 双色本来就在 630/750 两帧撒谎）。不做：不给刻度加 `role`/`aria-label` 与键盘可达性、不动 §5.9 接口与 `types.ts`。回归链：build ✓ / vitest 329 / python 158 / parity 52-52 / types cmp ✓。

## 任务卡原文

### FEAT-2026-10-16d —— 播放条刻度：统一颜色、撤 hover 文字、点击定位（Q90）+ 读数占位宽度（Q91）（新卡；依赖 FEAT-2026-10-16c、M4-4、M6-3）

- **起因（用户原话）**：「不再区分他们的颜色，同时鼠标悬浮在其上时也不再显示文字信息。新增功能，当点击某个刻度时，把进度条拉到该刻度处。此外，我发现当右侧进度数字变化时，可能导致播放条长度变化，留出适当的空间让数字变化而不影响进度条长度」。决议 **Q90/Q91** 记在规格 **§13.23**，并**就地修订 §7.3**（原"阶段标记…鼠标悬停显示阶段名"与"挂载事件标记…以不同刻度显示"两条合并重写）与 **§9.3**（补"点击刻度落在 `playback.seek` 宿主之下，不新增键"一句）。
- **Q90 三处代码**：`renderPlaybackTicks()` 不再写 `title`、改写 `data-seek-frame`，两类刻度共用一个 `mark()` 工厂；`STEP_NAMES`/`stepLabel()` 整个删掉（阶段名是**生成器约定**不是 §8.2 的字段，前端替它起名本就越界，随 hover 文字一起走）；`styles.css` 删 `.timeline-tick.attach` 的 `#a8874f` 配色（统一 `#4f7ca8`）、加 `cursor:pointer`、把外扩热区从 `.timeline-tick:hover::after` 改成**无条件**的 `.timeline-tick::after`（**坑 58a**：热区靠 `:hover` 才存在 ⇒ 必须先命中 2rem 本体才拿得到，作为 tooltip 容错等于没有）。DOM 仍留 `step`/`attach` 类名，它是 §8.6 Attach 表在页面上唯一的可断言痕迹（smoke 就靠它）。
- **点击走哪条路**：click 处理器的 `closest()` 选择器加 `[data-seek-frame]`，分支写成 `pausePlayback(); showFrame(Number(frame))` —— 与 `prev`/`next` 帧步进同一条路径（点刻度是对 clip 的手动读取，不该让 rAF 时钟继续跑）；目标帧取自 `data-seek-frame` 而非滑块，所以 **坑 17** 那个"先取值再 pause"的顺序陷阱在这里天然不成立。**不新增 feature 键**：刻度本就画在 `.timeline-track[data-feature="playback.seek"]` 里，宿主关断 ⇒ 整条轨道连同刻度 `display:none` ⇒ 点击不可达，第二道门禁是死代码；`features.json` 仍 **78 键**。
- **Q91 为什么用 `ch`**：`.timeline` 是 flex，两个读数 `<span>` 此前只有 `nowrap`+`tabular-nums` 没有宽度约束，帧号 `0 / 839` → `839 / 839` 变宽时挤的是 `flex:1` 的 `.timeline-track`。补 `#timeline-time{min-width:16ch}`、`#timeline-frame{min-width:12ch}`：`ch` 就是当前字体"0"的 advance，而读数本来就是等宽数字，所以按字号自动缩放；`/`、`.`、空格都窄于数字 ⇒ 按最长可能串取整**只多不少**。**用 `min-width` 不用 `width`**：超长 clip（6 位帧号）下 `width` 会切字，`min-width` 只在短读数时补齐。
- **验证（smoke，§12.4 改断言）**：原来那三行是靠 `.timeline-tick` 的 `title` 前缀断言两条 Attach 的帧号，撤 tooltip 后改读 `data-seek-frame`，并加两条新断言 —— 全部刻度 `title` 为空、`backgroundColor` 集合大小为 1。`node tools/browser_smoke.cjs` **11 项 check 全绿**，`§12.4` 记 `{ticks:5, widthBeforeSeek:280, widthAfterSeek:280}`，其中"点第一根 Attach 刻度 ⇒ `#timeline-frame` 读 `340 / 719`"就是点击定位的断言。
- **验证（真机，一次性栈 gateway 8099 + vite 5188 + OLS 8000，探针在仓库外 `scene5probe\tick90\bar_probe.cjs`）**：载入 `examples/scene3_9_segments.traj` ⇒ **11 根刻度**（9 step + 2 attach）、`distinct colours: ["rgb(79, 124, 168)"]`、`any hover text: false`、`cursor: ["pointer"]`；`.timeline-track` 宽度在帧 **0 / 5 / 839** 三处都是 **601 px**（跨一位到三位数）；点 `630`/`750`/`390` 三根分别把播放头带到 `630 / 839`、`750 / 839`、`390 / 839`，宽度仍 601、播放按钮 title 一直是 `Play`（点击确实停在暂停态）；`page errors: []`；截图 `bar-hover-attach.png` 里鼠标悬在第一根 Attach 刻度上、条上方空无一物。
- **明确不做**：不新增 feature 键、不给刻度加 `role`/`aria-label`（撤了 hover 文字也没给键盘可达性，见下面第 3 条待确认）、不改 §5.9 任何接口、不动 `types.ts`/`contracts`。回归链：`npm run build` ✓、vitest **329**、python **158**、`audit_parity.py` **52/52**、`cmp types.ts contracts/types.ts` ✓。
