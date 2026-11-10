# FEAT-2026-11-04-E —— 拾取只落在水面上：线框资产不再抢走邻居的点击

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-04 |
| 落地 | 本卡提交 |
| 依赖 | M1-4,M6-3,FEAT-2026-10-28-A |
| 决议号 | Q71、Q105、Q106、Q130、Q140（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-11-04e 新卡（第 ㊼ 张）：拾取只落在水面上 —— 线框资产不再抢走邻居的点击（Q140，修复与验收同一轮）。用户报「模型22的命中判定异常的大…」并附导出场景；根因两条腿：资产 22 的 `doll.obj` 多一条 `l 952 960`（`OBJLoader` 因此整份解析成 `LineSegments`，零个 `Mesh`）× three 的 `Raycaster.params.Line.threshold` 默认 1 米余量 ⇒ 无头实测一次点击 3403 次线段命中、真浏览器 63 网点 26 次落到 doll、19 件道具一次都没点中过。规则加在 `renderer.ts:selectable()`（线／点永不携带选中，与 Q71 视锥线、Q130 本地坐标轴同族第四条），阈值归零只当性能；+3 单测（590 → 593）、零新键 ⇒ 不需要重启 gateway。资产那半由用户同轮换掉（22 ⇒ `toy_panda`，734 面 0 线），修复后的判据取用户手动实测「现在物体能够精确的被选中了」。见本文 §9 卡 ㊼ 与规格 §12.35／§13.53。

## UI 优化记录行

| 日期 | 任务 | 做了什么 | 状态 | 验证与证据 | 落地 |
|---|---|---|---|---|---|
| 2026-11-04 | 拾取只落在水面上：线框资产不再抢走邻居的点击（**Q140**，卡 `FEAT-2026-11-04-E`，新卡 ㊼） | 用户原话（一条 bug 报告）：「模型22的命中判定异常的大，导致点击与其相邻的其他物体时依然选中了doll。或许这也有可能不是资产的问题而是我们代码的问题」，并附一份导出场景。根因是**两条独立的腿合成一个症状**：OLS 资产 22 的 `doll.obj` 多一条 `l 952 960`（两顶点相距 1.5 mm，导出残留；全库 34 份 OBJ 只有这一份带线），而 `OBJLoader` **不能把面与线装进同一份几何** ⇒ 整份资产解析成一个 `LineSegments`、零个 `Mesh`；three 测线的命中用 `Raycaster.params.Line.threshold`，默认 **1** 是**一米**余量，那副看不见的线框因此接管整张桌面。**落地**：`renderer.ts:selectable()` 新增同族第四条（选中必须来自 `Mesh`／`Sprite`，`Line`/`LineSegments`/`Points` 永不携带选中，与 Q71 视锥线、Q130 本地坐标轴同一个函数），构造器把 `Line`／`Points` 阈值置 `0` —— **规则在判据里，阈值只省 3403 次无用的段测试**。 **完成**（用户手动实测出具判据） | 真浏览器（修复前，一次性栈 gateway 8098 + 只读 OLS 8123 + headless Chrome 9222，载入用户那份 `ur5e_table_empty_v3.json`，63 个网点接近垂直向下、真鼠标事件、读 `.tree-row.selected`）：**`{(none): 36, doll#18: 26, RobotPedestal#6: 1}`，19 件道具一次都没被点中过**。无头 three：一次点击 **3403** 次线段命中（首个命中 `LineSegments:"Dino_DinoMesh"`，横向偏 0.16 m→90 cm 都命中），阈值 `0`／`0.01` ⇒ **0 命中**；资产 A/B：带 `l` ⇒ 只有 `LineSegments position:6806`、零个 `Mesh`，删掉那行 ⇒ `Mesh position:6804`。单测 **590 → 593 / 18 文件**（新 `describe` 三支：只有面 ⇒ 1 `Mesh` 且可点；补一条 `l 1 2` ⇒ 0 `Mesh`、1 `LineSegments`、顶点数还变多；线／点回 `false` 而 `Mesh`／`Sprite` 回 `true`），负控制 = 撤那个判项 ⇒ 第三支立刻红。`tsc --noEmit` 干净、`npm run build` 过（`index-DZ1DT_PE.js` **890.13 kB**，CSS 哈希不变）、`audit_parity.py` **62/62**、`parity_inventory.py` **88 键**不增不减 ⇒ **不需要重启 gateway**（**坑 22** 不适用）。**修复后的逐点自动化复测不可比**：探针等 650 ms 时相机仍在阻尼运动（正中像素不一定落在目标上），且**取证窗口内用户自己把资产 22 换成 `toy_panda`（734 面、0 条线）** ⇒ 线框案例消失；判据取用户口径「经过我手动测试，现在物体能够精确的被选中了」。**代价写在 §4.7／§13.53**：一副纯线框资产从此无法用鼠标选中（树里点行仍可，那条路不走射线）。 | `editor/src/renderer.ts`（+14：`selectable()` 判项与注释、构造器两行阈值）、`editor/src/renderer.test.ts`（+67：`linesOf()`、`OBJLoader` 导入、新 `describe` 三支）；规格 §4.7（新增一条）、§12.35、§13.53；README 的编辑器图层那段；零新键（88）、零新路由、`types.ts` 不动；`logic.ts`／`main.ts`／`placement.ts`／`styles.css` 一字未动。先例 Q71／Q105/Q106／Q130（同一个 `selectable()` 的前三条）。卡 **FEAT-2026-11-04-E**（㊼）。 |

## 任务卡原文

### FEAT-2026-11-04-E —— 拾取只落在水面上：线框资产不再抢走邻居的点击（**Q140**；口径与实装同一张卡；依赖 Q71、Q105/Q106、Q130）

- **起因（用户原话，一条 bug 报告 + 一份导出场景）**：「我发现一个bug，在当前场景下，模型22的命中判定异常的大，导致点击与其相邻的其他物体时依然选中了doll。或许这也有可能不是资产的问题而是我们代码的问题。我在点击时从接近垂直向下的视角点击物体。为了方便复现，我可以提供导出的场景文件。」附件 `ur5e_table_empty_v3.json`：场景里的 object **18** 名字是 `doll`，用户口中的"模型22"是 **OLS 资产 id 22**（两套 id 空间，§6.1）。用户同时留下了那个关键开放问题：是资产的锅还是代码的锅 —— **答：两个都是，本轮修得了代码那一条**。
- **诊断（两条独立的腿，都要写在案上）**：**腿一（资产）** `data/models/22/doll.obj` 里多一条 `l 952 960`，那两个顶点相距 **1.5 mm** —— 导出残留，不是设计；全库 34 份 OBJ **只有这一份**带线语句。**腿二（代码）** `OBJLoader` 不能把面与线装进同一份几何，于是整份资产解析成**一个 `LineSegments`、零个 `Mesh`**（带 `l` ⇒ `position: 6806`；删掉那行 ⇒ `Mesh position: 6804`），而 three 测线的命中用 `Raycaster.params.Line.threshold`，默认 **1 = 一米**余量 —— 无头射线对着 mug#13 那一格打：**一次点击 3403 个线段命中**、横向偏 0.16 m 一路到 90 cm 都还命中，阈值改 `0` 或 `0.01` ⇒ **0 命中**。真浏览器（一次性栈 8098／8123／headless Chrome 9222，载入用户那份场景，63 个网点、接近垂直向下、真鼠标事件、读 `.tree-row.selected`）：**修复前 `{(none): 36, doll#18: 26, RobotPedestal#6: 1}`，19 件道具一次都没被点中过**。
- **口径与实装同一轮**：`docs/Web前端需求规格v2.md` **§4.7** 在"视口"那条之后新增一条"拾取只落在水面上"（含阈值那一米、`OBJLoader` 面线不能同存、代价与"树里点行走 DOM"那条区分），新增验收 **§12.35**（四层）与决议 **§13.53**；README 的编辑器图层那段就地补一句。历史留在本卡，正文不写"曾经如何"。
- **本轮最重要的一处判断：规则写在 `selectable()`，不写在阈值里**。既有判据已经在这一个私有函数里排掉视锥线（Q71）、本地坐标轴（Q130）与"看不见的标记"（Q105/Q106），本轮加的是同族第四条：`if (object instanceof THREE.Line || object instanceof THREE.Points) return false;`。**为什么不用"把阈值调小"当规则**：任何有限阈值都是替某一份资产挑的尺寸（`0.01` 在 17 cm 玩偶上是 1 cm，在 3 m 工件上是另一个故事），类型层面的判据与尺寸无关。**阈值仍然归零**，但注释写清它是**性能不是正确性**：构造器里 `params.Line.threshold = 0` / `params.Points.threshold = 0` 省掉那 3403 次无用的段测试，删掉它们也不会让线框重新获得选中。
- **实装形态**：`renderer.ts` 两处、共 **+14 行** —— 构造器（`this.scene.add(this.simWorld)` 之后）两行阈值，`selectable()` 首行一个判项 + 注释里那条一米余量与资产 22 的数字。`logic.ts`／`main.ts`／`placement.ts`／`styles.css` **一字未动**（`git diff --numstat` 复核），两份 `types.ts` 未动（**坑 6**）。**没有误伤的另一半**：灯标记是 `Mesh`（`userData.lightMarker`）、灯标签是 `Sprite`（只在那盏灯已选中时可见 ⇒ 抢不走未选中物体的点击）、`GridHelper`／`AxesHelper`／`Box3Helper`／航点 `InstancedMesh` 挂在 `scene`／`simWorld` 而不在 `nodes` 里 —— "不在集合里"与"不许携带选中"是两层，本轮只动后者，前者不补判据。
- **单测（590 → 593 例 / 18 文件，+3，全在 `renderer.test.ts` 一个新 `describe`）**：`describe("a wireframe asset does not steal its neighbours' clicks (§4.7, Q140)")` —— **a** 只有面的 OBJ 解析出恰一个 `Mesh`、零条线，`selectable()` 认它；**b** 同一份文件补一条 `l 1 2` ⇒ `Mesh` 归 0、`LineSegments` 为 1，且几何顶点数比有面那一版**还多**（面被拆成线段重画）—— 这一支钉的是**加载器的行为**，升级 three 后一旦不成立就会红；**c** `LineSegments`／`Line`／`Points` 回 `false`，`Mesh` 与 `Sprite` 回 `true` —— 出局的那一面与必须留下的那一面钉在同一支里。**夹具限制在册**：`adapterFixture()` 用 `Object.create(prototype)` 手搭、不跑构造函数 ⇒ 构造器那两行阈值不在单测覆盖面内（与卡 ㊷ 那条同源限制同族），由无头数字负责。**负控制**：撤掉 `selectable()` 首行那个判项 ⇒ **c** 立刻红。
- **验证（回归链，全绿）**：`npx tsc -p editor/tsconfig.json --noEmit` 干净、`npx vitest run --config editor/vitest.config.ts` **593 例 / 18 文件**、`npm run build` 成功（`dist/assets/index-DZ1DT_PE.js` **890.13 kB** / gzip 244.20 kB，比卡 ㊻ 那份 `index-DPOGBfDe.js` 890.01 kB 多约 120 字节；CSS **哈希不变** `index-CsK1Gru1.css` 21.81 kB —— 本轮不动样式，这正是预期的）、`python tools/audit_parity.py` **62/62 · gaps []**、`python tools/parity_inventory.py` → `features_local` **88**（不增不减）、路由与 browser actions 不增不减。派生文件**整份不动**：`docs/feature-inventory.csv` 与 `docs/parity-probes.json` 都不受本轮影响（零新键；重生成会顺手把 `viewport.grid`／`viewport.worldAxes` 写成用户本地的关值，按既有规矩**不入库**）。Python 侧不跑：一行网关代码没动、一条路由没变。
- **③ 真浏览器：硬数字在修之前，修复后的判据由用户手动实测出具**。修复前的 63 网点数字见上。**修复后的逐点复测本轮不可比，两个原因都在案**：其一，改用"选中行 → Focus Selection → 点视口正中"的探针（`var/stack/q140_focusmap.mjs`）之后相机仍在阻尼运动里（650 ms 不够），正中像素不一定落在目标上；其二，**取证窗口内 OLS 侧自己把资产 22 换成了 `toy_panda`**（734 面、0 条线，2026-11-04 14:33 落盘）⇒ 那个物体从此有真实表面、会按正常遮挡赢下正中像素，线框案例已不存在。用户结论：「经过我手动测试，现在物体能够精确的被选中了」。**另一个坑记在案**：headless 之外那次真 Chrome 窗口被 OS 遮挡时 `visibilityState: hidden`、rAF 掉到 0 FPS，截图停在旧帧上（看起来像道具被删了，实际场景树里 27 件物体都在）—— 取证必须用 `--headless=new`；探针导航到已加载场景的页面会被 `beforeunload` 对话框挂死，CDP 侧要 `Page.handleJavaScriptDialog` 自动接。
- **不做**：不给线框资产加"允许点选"的开关或键、不按资产 id 白名单排雷、不改资产文件（`ObjectLibraryServer` 不在本仓且带 `model_index.db`，本轮由用户替换）、不动 `intersectObjects` 的递归集合、不给 `GridHelper` 那几层补判据、不碰 §8 保护文件与 `distribute/**` 那七份快照。
- **一轮收尾**：本轮启动的一次性服务（gateway 8098、只读 OLS 8123、CDP Chrome 9222）在收尾时全部停掉，端口空；用户自己的 OLS／QwenMock／gateway／AgentBrain（8000／8080／8099／8500）全程未动（`--state-path` 与 `--user-data-dir` 都指临时位置）。`var/stack/doll*`、`var/stack/q140*`、`dist/q140_scene.json` 与 `var/stack/q140_profile/` 是本轮一次性产物，**不入库、收尾删除**；`kill_gateway.ps1` 同前几轮一样留在工作区不入库。**生效条件**：**不动键面** ⇒ 用户那台 8099 **不需要重启 gateway**，看新行为只需重新构建的 `dist/`（本轮已构建）。只本地提交、**不 push**。
