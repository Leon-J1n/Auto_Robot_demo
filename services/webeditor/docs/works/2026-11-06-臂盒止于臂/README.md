# 2026-11-06 臂盒止于臂：一物一盒的边界是文档的物体 id

## 原因

用户报的下一条 bug：

> 修复下一个Bug，机械臂的显示包围盒明显大于其实际包围盒

排查先从数字入手。用一支临时探针（`editor/src/zz_arm_box_probe.test.ts`，跑完即删，输出落 `var/arm_box_probe.json`）在**真资产、真加载路径**上重建出厂场景 2（`ur5e_table_apple`：一台 UR5e，腕上挂 robotiq 夹爪，夹爪上再挂一台 d435i 腕相机），把 `worldAabb(臂)` 与"只取臂自己那 20 份网格逐顶点量出来的真盒"并排打印：

| 量 | x 宽 | y 宽 | z 宽 |
|---|---|---|---|
| 现场显示盒（修复前） | **0.6475** | 0.2853 | **0.8456** |
| 臂自己的真盒（逐顶点） | 0.5628 | 0.2854 | 0.8387 |
| 面的越界 | −X **+2.21 cm**、+X **+6.27 cm** | — | +Z **+0.69 cm** |

体积多 **0.021567 m³ ≈ +16%**，"+X 那一边明显多出来一截"正是用户肉眼看到的那一条。

再拆 +X 那 6.27 cm：臂自己的几何**按零件角点**并集是 x 宽 0.5951（比逐顶点松 3.23 cm，摊成 −X 2.21 cm 与 +X 1.02 cm），而挂在腕上的夹爪＋腕相机**再加 5.25 cm**（0.6476 − 0.5951）。夹爪自己的盒只有 0.003058 m³，臂是 0.143588 m³ —— 越界不是因为手大，是因为**手在臂外面**，而它被算进了臂的盒。

机制：`renderer.worldAabb(objectId)` 走的是一棵**场景图子树**（`expandGeometry` → `object.children`），而 `RobotAttachment` 与 `Transform.parent` 恰恰**把孩子挂在父物体的节点之下**（夹爪挂在 `wrist_3_link` 节点上，见 `场景文件描述.md` §4.8 的挂载两拍）⇒ 父物体的并集里混进了子物体的几何。同一个数被 `§4.8.6` 的线框、`§4.5` 的选中外框、`§5.11` 的 `aabb_world`、`frame()` 和 `sceneAABB()` 共用，所以四处一致地"大"，看不出哪一环坏了。

排除掉的假设（都量过，不是它们）：零件角点 vs 顶点的松（只占 ~6% 体积、每面 1–3 cm，量级不够）；编辑器层混入（`*Helper`／ghost／waypoint／`selectionBox` 早被 `isEditorOnly` 挡住）；URDF 的 `<collision>` 几何（本渲染器不量它）；机器人被装两遍（`armMeshes` 恰好 20，与单台一致）。

## 做了什么

**递归止于另一个物体的节点**（`editor/src/renderer.ts:1893`、`editor/src/renderer.ts:1928`）：`worldAabb(objectId)` 把"本次量测属于哪个物体"一路传进 `expandGeometry`，递归遇到 `child.userData.objectId` 已盖且**不等于**本物体 id ⇒ `continue`，不进去。

- 判项读的是**既有**身份标志 `userData.objectId`（`syncScene` 盖物体节点、`loadModel` 用 `built.traverse` 盖整份构建内容）——没有为新口径加新字段，否则每条后来的挂载路径都要记得填它。
- 全world 分支不受影响：`this.nodes` 里每个物体节点各自作为根走一遍，挂在别人底下的内容照样被它自己的根量到，一处不丢。
- 盖章顺序从此**承重**：`mountOnLinks()` 必须排在 `built.traverse(盖章)` 之后（`场景文件描述.md` §4.8 早就写了这条），倒过来父级会把孩子盖成"自己的一部分"，越界原样回来且不报错。本轮把"这枚 `objectId` 同时是 §4.8.6 的物界判项"写进那份文档。

**规格就地修订**（判据 **§12.41**）：
- §4.8.6「一物一盒，按世界文档的物体 id 数」：补"盒只量这个物体自己的渲染几何（Q148）"、越界数字、以及把旧那句"重叠是预期而不是 bug"改成"**包含不是预期、重叠才是**"。
- §4.5「一个坏零件不许毒死它的父级」：跨物体那条毒路现被物界判项掐断，同物体那条仍靠 `isFiniteBox` —— 两条防线管两个尺度，原样保留。
- §5.11 `/get2DAABB` 算法行：加"递归止于另一个物体 id 的节点"。

**单测 3 条**（`editor/src/renderer.test.ts`，走真加载路径：`buildUrdfTree` + `loadModel` + `mountOnLinks`，臂的两个 link、腕上的夹爪、手上的道具都是真挂上去的）：
1. **臂不再把手算进自己**：夹爪的父节点确为 `wrist_3_link`；臂两个 link 的世界位置都落在 `worldAabb(臂)` 内；夹爪中心**不在**臂盒里，但**在**全world 盒里。
2. **放大孩子不改父盒**：夹爪 `scale ×10` ⇒ `worldAabb(臂)` 逐位不变，夹爪自己的盒按 10 倍长。
3. **三级链各归其盒**：挂在手上的道具（`type:model`）不进夹爪盒、不进臂盒，只进它自己的盒与全world 盒。

判据写成**几何关系**（"父盒不含子盒中心""放大子物体不改父盒"），不写成常数——常数随资产变，出厂库一更新就是一条永远红的假失败。

**负控制**：摘掉那一句 `continue` ⇒ 本轮 3 条新用例全红、其余 **126** 条绿；还原后与暂存前逐字节相同。

**明确"不做"**：
- **不改零件级量测**：仍取每份几何 `boundingBox` 的角点并集，不逐顶点。那是 **Q144** 摆放碰撞故意留下的保守（宁可盒略大），且这趟递归每 250 ms 走一次全场景图，换成逐顶点就是把成本从"零件数"变成"顶点数"。本轮修的是"该停的地方没停"，不是"量得不够紧"。
- 不动编辑器层判据、不动 `isFiniteBox` 那道同物体防线。
- 不碰 gateway、不碰协议面、不新增根路径接口 ⇒ 路由清单（坑 3）与 `audit_parity.py` 探针不增不减。
- 不改 `tools/audit_parity.py` 里那条随 **Q147** 已过时的 `/getWorld` 缓存探针（见下面"验证"），那是上一卡的漂移，另卡处理。

## 验证

| 项 | 值 |
|---|---|
| `npx tsc --noEmit` | 0 错 |
| `npx vitest run --config editor/vitest.config.ts` | **652 例 / 20 文件全绿**（新增 3 例） |
| `npx vite build --config editor/vite.config.ts` | `dist/assets/index-9MRbu1jT.js` **894.74 kB**（+0.08 kB），CSS 21.85 kB 不变 |
| `python tools/audit_parity.py` | **61/62**。唯一缺口 `the cached world serves callers with no browser`：Q147 删掉 `/getWorld` 的 `world_cache` 回退后，该探针仍按旧口径写着（今天实测回 `503`／`source: null`），**本轮未碰 gateway**，非本轮引入（顺带刷新了 `runtimeConfig` 的键数 16→17，属真实契约现状，非失败） |
| 真机走查 | **未做**：冒烟 `tools/browser_smoke.cjs` 自 S5-a 起停在 §12.4 的身份闸（坑 90，`paused` vs `stopped`），量框与采集那几幕今天跑不到 |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-06 |
| 落地 | 本次本地提交（master，紧接 `64d7eaa`） |
| 改动 | `editor/src/renderer.ts`（`worldAabb`/`expandGeometry` 的物界判项 + 文档注释）、`editor/src/renderer.test.ts`（+1 处 `UrdfVisual` 导入、+1 个 describe 含 3 条用例与挂载 fixture）、`docs/Web前端需求规格v3.md`（§4.8.6／§4.5／§5.11 就地修订 + §12.41）、`docs/场景文件描述.md`（§4.8：`objectId` 兼作物界判项、盖章顺序承重）、`docs/works/坑清单.md`（新增坑 120、坑 67 补"前提已改"）、本卡与 `INDEX.md` 顶行 |
| 未提交 | 临时探针 `editor/src/zz_arm_box_probe.test.ts` 与其 `var/` 输出已删除；`docs/parity-probes.json` 是 `audit_parity.py` 重跑刷出的**上一卡（Q147）漂移**，留在工作区不入本提交 |
