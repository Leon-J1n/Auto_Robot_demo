# 2026-11-06 臂盒逐 link 量：把"怎么量"换到顶点上，再给形状一枚常量

## 原因

用户第二次报同一件事：

> 机械臂的包围盒依然有问题，肉眼看起来包围盒依然大于机械臂本体（上一步你已经将包围盒缩小至机械臂本体而不包括其他链接物体）。我们应该动态计算每个 link 的包围盒，然后将这些 link 的包围盒合并起来得到机械臂本体的大包围盒。此修复应当对所有 urdf 类物体都一致。另在 config/constant_config.json 中添加一个 flag，用于描述 urdf 类的物体包围盒是每个 link 单独显示还是合在一起显示，默认为合在一起显示

Q148 掐对了**量谁**（臂的盒不再含腕上的夹爪），屏幕上的盒仍肉眼偏大 ⇒ 病根换到**怎么量**。量出来才发现是两条叠在一起：

**一、算式：角点口径的松不是余底，是关节转出来的。** 旧口径取每份网格**局部 AABB 的八个角点**乘世界矩阵再并。零件是在它自己 link 的轴里建模的，关节一转就把它转出那些轴之外，那八个角指的全是空处。这条**不可能靠把角点算得更聪明修好**：凸包角点在旋转下的像不构成旋转后凸包的角点，所以只有逐顶点是更紧的那一口。

用一支临时探针（`editor/src/zz_link_box_probe.test.ts`，跑完即删，摘要落 `var/lbp_summary.txt`）在**真资产、真加载路径**上重建出厂场景 2（`ur5e_table_apple`，20 份网格 / **358,618** 个顶点），按场景文件声明的关节位姿并排打印两种口径：

| 量 | x | y | z | 体积 |
|---|---|---|---|---|
| 现场显示盒（角点并，修复前） | 0.2854 | **0.9979** | **0.4354** | **0.12398 m³** |
| 逐顶点并盒（修复后） | 0.2854 | 0.9910 | 0.4031 | 0.11398 m³ |
| 面的越界 | 两面一字不差 | +Y **+0.69 cm** | −Z **+1.02**、+Z **+2.21 cm** | **+0.0100 m³ ≈ 8.8%** |

逐 link 拆开更能指出病在谁身上 —— 松多少取决于**关节把这条 link 转出它的建模轴多远**：

| link | 角点并 (x,y,z) | 逐顶点 (x,y,z) |
|---|---|---|
| `base` | 0.151, 0.0991, 0.151 | **逐字相同** |
| `shoulder_link` | 0.1345, 0.1354, 0.1202 | **逐字相同** |
| `upper_arm_link` | 0.1496, **0.5322**, **0.3767** | 0.1496, 0.4882, 0.3326（y −4.4、z −4.4 cm） |
| `forearm_link` | 0.1099, **0.4867**, **0.3267** | 0.1099, 0.4459, 0.277（y −4.1、z −5.0 cm） |
| `wrist_1/2/3_link` | — | 各收 0.7～1.0 cm |

`base` 与 `shoulder_link` 的零件本来就没被转出轴，所以两态相同 —— 这不是巧合，正是"越界是旋转的产物"这条判断的对照组。

**二、粒度：URDF 树是嵌套的，"逐 link 递归"第一版量出来的是 N 个整台臂。** 场景图是 `link → 关节节点 → 子 link → 关节节点 → …` 一路套下来的，从 `base_link` 走下去必然路过全部子孙。第一轮实现照此写完，`renderer.test.ts` 立刻红在一条可手算的断言上：夹具那条 link 的盒长应为圆柱长 **0.4**，实得 **1.35** = 整台臂。**红在生产代码，不在测试** —— 于是逐 link 的递归必须**止于其它 link 的根**（与 Q148 那条"止于别的物体 id"是同一形状的判项，管的却是另一件事：一条划**物界**，一条划**粒度**）。

## 做了什么

**逐 link、逐顶点**（`editor/src/renderer.ts`）：

- `worldLinkBoxes(objectId)`（renderer.ts:2030）：对每条 link 单独走一趟 `expandGeometry`，传入"其它 link 的根"这个 `skipRoots` 集合 ⇒ 遇到别人的根就停，不进去；空盒的 link（`<visual>` 无网格）不进结果（§4.5 那条推论，不是原点一只零号盒）。非 URDF 物体回 `[]`，这是它说"我没有 link，两态都画我这一只盒"的方式。
- `exactWorldBox`（renderer.ts:2132）：逐顶点乘 `matrixWorld` 扩张，**跳过非有限顶点**（Q141/Q143 的 NaN 口径一字未动），按（网格、`matrixWorld`、几何）三元组拿 `WeakMap` 缓存 —— 关节一动这枚键就变，缓存自然作废，不需要谁去通知它。
- `expandObject`（renderer.ts:2051）：**`worldAabb()` 字面上就是 `worldLinkBoxes()` 的并集**，再并上"不落在任何 link 里"的那部分几何（走物体节点自己的子树，但 `skipRoots` 为全部 link 根）。这一句是本轮唯一让显示常量**改不了量测**的理由，见下。
- 显示：`setLinkBoxesEnabled`、`linkHelpers`（以 `<objectId>/<link>` 为键）、`syncBoundingBoxes` 在 `perLink` 态委托 `syncLinkBoundingBoxes` —— 画每 link 一只盒时**把该物体的并盒从屏幕上摘掉**（一只盒套在它自己的盒里不是"看得细"，是噪声），回 `false` 时逐 link 助手全部撤下、并盒回来。诊断行仍是那一条 `Viewport boxes`，加 `perLink` 与 `drawnLinks` 两个字段（不是第二个出口）。

**形状常量 `urdf_bounding_box_per_link`**（默认 `false`）：`config/constant_config.json`（紧邻 `shadows_enabled`）+1 键 ⇒ `local_gateway.py` 的 `DEFAULT_CONSTANT_CONFIG` 同步 +1 ⇒ 两份逐字节相同的 `types.ts`／`contracts/types.ts` 的 `RuntimeConfig` 各 +1 可选字段（坑 6）⇒ `logic.ts:434/445` 的 `DEFAULT_URDF_LINK_BOXES` 与 `urdfLinkBoxesEnabled(runtime)`（**fail-open**：只有真正的布尔才采用，缺字段／`null`／字符串／`0`／`1`／`NaN` 一律 `false`，同 §9.1 全表）⇒ 唯一调用者 `main.ts:645 renderBoxes()`。它**坐在 `viewport.boundingBoxes` 下面而不是旁边**：那一键与勾选框仍决定画不画，本键只决定画成什么形状，所以整层的远程出口仍是 Q119 那一个（§9.3 的收口没被稀释）。**`docs/场景文件描述.md` 本轮不动** —— 逐 link 的 cut 读的是渲染器自己的 `robotTrees` 链接表，不是场景文件的约定，不为它开新口径。

**规格就地修订**（判据 **§12.42**）：§4.8.6「一物一盒」那条把两态说清 + 新增「URDF 的量测是逐 link、逐顶点的」与「形状开关」两条 bullet、诊断行与落点各改一行；§9.1 键表 + 一条 fail-open 说明；§9.2 JSON 样例 +1 行；§12 工具栏那条从「四条口径」改成「五条」，新增「形状另有一枚常量」。

**单测 9 条**（`renderer.test.ts` +7 ＝该文件 136 例、`logic.test.ts` +2 ＝90 例，全套 **661/20**）：夹具是一台**能手算**的 URDF —— 截面 24 面（15° 一档）的圆柱绕自身轴转 **45°**（45° 恰是那组面的对称），于是顶点量得 `2R`、角点量得 `2√2·R` 是两个钉得死的数；用方块不行（它自己就是凸的，转了也紧），用球不行（只能靠细分运气）。七条分别钉：a 转过的零件按顶点量（三轴 `2R/LENGTH/2R`）且同一份零件按角点量两轴各松 >0.05、第三轴逐字相同；b **父 link 的盒不含子 link 的中心、子 link 的盒含着它**（嵌套 cut 的 durable 判据，写成几何关系不写成常数，同坑 120 ③）；c `worldLinkBoxes` 的并 `equals` `worldAabb()`；d 无几何的 link 什么都不画；e 形状切换的对账（`drawnLinks` 恰为三条 `<objectId>/<link>`、并盒被摘掉、每只 link 助手与 `worldLinkBoxes` 逐字相同、回 `false` 那只盒等于存下来的并、纯 model 两态都是一只盒）；f 常量接到画它的那一层（按坑 25 那条老路钉 `main.ts:renderBoxes()` 的源码结构，两枚 `enabled` 判据都在其中）；g 关节一转缓存立刻失效（`after.equals(before) === false`）。`logic.ts` 那两条钉默认值与 7 种畸形值全回 `false`。Python 侧 `tests/test_gateway_routes.py` 钉**出厂文件里这个键就是 `False`**。挂载仍走真实加载路（`loadModel` → `built.traverse` 盖章 → `mountOnLinks`）。

**负控制 3 支，各红且只红一支**：α 摘掉 `skipRoots` ⇒ 只红 a，`size.y` 实得 **1.35 = 整台臂**（即第一轮那个缺陷本身）；β per-link 改回角点口径 ⇒ 只红 a，`size.x` 实得 **0.28284 = 2√2·R**；γ 把 `worldAabb()` 的 URDF 分支改成另走一趟子树角点量测（"两条各算一遍"）⇒ **只红 c、其余 135 条全绿**。改回后 `renderer.ts` 与备份 `md5sum -c` 通过、逐字节一致。

**明确"不做"**：
- **非 URDF 物体一行没改** —— 纯 model／道具／`Transform.parent` 挂上来的仍走那条角点路、一只盒。用户那句"此修复应当对所有 urdf 类物体都一致"划的正是这个边界（一致 = 所有 URDF 同一口径，不是把成本摊到全场）。
- 不动三道既有判项：编辑层排除（§4.4/Q50）、NaN 口径（Q141/Q143）、物界判项（Q148）。
- 不改 OLS 资产里 `<visual>` 原点偏保守的事（资产侧的事，前端本轮只保证不再放大它）。
- 不给每 link 盒加拾取或交互；**不加第二枚勾选框**（那会把"整层的远程出口只有一个键"稀释成两个）。
- 不碰协议面、不新增路由 ⇒ `audit_parity.py` 探针不增不减。

## 验证

| 项 | 值 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | rc 0，**0 错** |
| `npx vitest run --config editor/vitest.config.ts` | **661 例 / 20 文件全绿**（renderer 136、logic 90；比 Q148 的 652 多 9） |
| `npx vite build --config editor/vite.config.ts` | rc 0，`dist/assets/index-BgUOnx_S.js` **897.22 kB**（gzip 246.52，+2.48 kB），CSS 21.85 kB 不变 |
| `python -m unittest discover -s tests -p test_gateway_routes.py` | rc 0，**13 例 OK**（含"出厂文件该键 `is False`"，同一次跑还证明 `config/constant_config.json` 与 gateway 内置默认逐键相同） |
| 成本 | 逐顶点对 URDF 冷走一次 **6.9 ms**／台（`exactWalkMsPerPass = 6.8993`），250 ms 的刷新周期装得下；缓存命中后不再扫顶点 |
| 负控制 | 3 支各红 1 条（α 1.35、β 0.28284、γ 只红不变量那条且 135 条绿），还原后 `md5sum -c` 通过 |
| 真机走查 | **未做**：`tools/browser_smoke.cjs` 自 S5-a 起停在 §12.4 的身份闸（坑 90）。本轮补上了它缺的那一半 —— e 那条把"屏幕上画哪几只盒"做成 `boxReport()` 的可断言读数，取证不再只能靠眼睛 |
| `tools/audit_parity.py` | **未跑**（有意）：它读用户正在跑的 8099，而那台跑的仍是改前的代码，重跑只会把快照写回旧数；`docs/parity-probes.json` 按上次裁决整份不入本提交 |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-06 |
| 落地 | 本次本地提交（master，紧接 `062933c`） |
| 改动 | `editor/src/renderer.ts`（`worldLinkBoxes`／`exactWorldBox`＋缓存／`expandObject`／`expandGeometry` 的 `exact`＋`skipRoots`／`setLinkBoxesEnabled`／`syncLinkBoundingBoxes`／`boxReport`）、`editor/src/logic.ts`（`DEFAULT_URDF_LINK_BOXES`＋`urdfLinkBoxesEnabled`）、`editor/src/main.ts`（`renderBoxes()` 接两枚判据）、`config/constant_config.json`＋`local_gateway.py`＋两份 `types.ts`（常量面 +1）、`editor/src/renderer.test.ts`（夹具字段 +3、Q119 三条期望改为新 `boxReport` 形状、+7 例）、`editor/src/logic.test.ts`（+2 例）、`tests/test_gateway_routes.py`（+1 断言）、`docs/Web前端需求规格v3.md`（§4.8.6／§9.1／§9.2／§12 工具栏就地修订 + §12.42）、`docs/works/坑清单.md`（新增坑 121）、本卡与 `INDEX.md` 顶行 |
| 未提交 | 临时探针 `editor/src/zz_link_box_probe.test.ts`、`var/probe149.mjs` 及其 `var/` 输出已删除（`var/` 本就 gitignore）；`docs/parity-probes.json` 是 `audit_parity.py` 早前刷出的**上一卡（Q147）漂移**，仍留在工作区不入本提交，待用户裁决那条过时的 `/getWorld` 缓存探针 |
