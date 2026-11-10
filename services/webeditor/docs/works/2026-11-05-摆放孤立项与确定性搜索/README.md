# 2026-11-05 摆放孤立项与确定性搜索

## 原因

上一卡（`2026-11-05-摆放只用前端世界盒`，commit `77884cb`）把碰撞盒换成浏览器自己的世界盒后，用户复测反馈摆放还剩两个问题：

1. **摆放失败时世界原点冒出一个目标物体**。它有概率在一定时间后自动消失，也可能在下一次摆放成功时消失，刷新页面也会消失。
2. **随机+确定性摆放仍然"前几次失败、突然又有一次成功"，而肉眼看桌面上还有不少能摆下的空间**。用户判断是查找算法的问题。

用户口径原文：「当摆放失败时世界原点会出现一个目标物体……前面有一次修改，我们把摆放改成了先随机尝试摆放+失败后确定性寻找可用位置，但是现在摆放依然存在前面几次尝试失败，但突然又有一次成功（但是从肉眼看桌面上还有不少有可能能摆下物体的空间）。这也许是你的查找算法的问题。修复这两个问题」。

## 做了什么

**问题 1 —— 失败路径没收尾渲染层**（`main.ts:chooseLibraryItem`）。
根因：`BoundsMeasure.settle` 为了量盒，会 `renderer.syncScene(state.world)` 把**暂存物体连同原点位置**推进**可见**场景图。摆放失败时 `World.placeLocal` 只把暂存物从 `World` 文档里 `deleteObject`，**不会**回头清渲染节点；而成功路径里那句 `renderer.syncScene` 被 `catch` 跳过 ⇒ 被拒物体滞留在世界原点成孤立项，直到下一次摆放/刷新的 `syncScene` 按文档 id reconcile 时被 dispose（这正好解释"下一句"现象）。
改法：在 `catch` 分支报错弹窗**之前**补 `renderer.syncScene(state.world)` + `updateStatus()`，把孤立节点当场收回。`world.test.ts` 既有 `placeLocal` 用例已钉住"失败 ⇒ 文档计数一字不动"，本处只是补上渲染层与之对齐。

**问题 2 —— 第二段定格点扫描不完备**（`placement.ts:spotOnTable`）。
根因：旧第二段按 `scanSteps`（缺省 32）把内接矩形切成 `(steps+1)²` 个格点、`centreFirstGrid` 从中心向外扫。**格点间距本身就是能被找到的最窄缝隙的下限**：一条比格子更窄、或恰好落在两格之间的可用缝会被整格跨过 ⇒ "桌面肉眼看还有位置"却报"放不下"。而"失败几次突然成功"正是这种漏检——同一桌面里缝隙对随机采样是"撞得到就成、撞不到就看那个也会漏的定格扫描"，成败取决于骰子。
改法：第二段换成**贴边候选中心**的**完备确定性扫描**（`edgeCentres` + `centreFirstEdges`，`centreFirstGrid` 删除）。判据：一个盒子若能放到某处，平移贴到某个障碍边或桌沿时仍不碰任何东西（`overlaps` 严格相交，贴边不算碰撞）；于是只需试 `obstacle.min − 半格`／`obstacle.max + 半格`（夹进 `[min+半格, max−半格]`）并上两条桌沿中点与矩形中心——一个**有限、固定**的集合，**从中心向外**逐点试。这样**缝隙只要在 AABB 模型里存在就必被找到**，同一桌面每次点击结论一致，不再依赖随机。`scanSteps` 从"每轴格数"降为该段**开关**（缺省 >0 仍扫，显式 `0` 退回纯随机）。第一段随机照旧保留散布。
修的一处细节：`spotOnTable` 里 `half = size(footprint)` 是**全长**而非半长，`at()` 用 `half/2` 作真实半宽；`edgeCentres` 的偏移同样必须用 `half/2`，否则贴边点会算到障碍内部、把缝隙判成放不下（首轮单测即抓到此 off-by-2）。

**明确"不做"**：
- 不改碰撞集来源（仍是 `renderer.sceneAABB`，上一卡的硬要求），不改回元数据 AABB。
- 不为"未来可能需要"给搜索加分辨率旋钮；`scanSteps` 只当 on/off。
- 不改任何协议面、不动 `scene_version`、不新增 gateway 调用 ⇒ §5.9 路由清单与 `audit_parity.py` 探针不增不减。

规格就地修订：`docs/Web前端需求规格v3.md` §4.6.1「随机落点」第二段口径改写为贴边完备扫描并标注 `centreFirstGrid` 已被取代；§4.6.1「一切写都是 ② 类」那条的 2026-11-05 孤立项修复补写"失败那一次要在渲染层收尾"。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本次本地提交（master，紧接 `77884cb`） |
| 改动 | `editor/src/main.ts`（catch 补 syncScene+updateStatus）、`editor/src/placement.ts`（`spotOnTable` 第二段改贴边、删 `centreFirstGrid`、加 `centreFirstEdges`/`edgeCentres`、改 `scanSteps` 语义与文档）、`editor/src/placement.test.ts`（+3 单测） |
| 验证 | `npx tsc -p editor/tsconfig.json --noEmit` 干净；`npx vitest run --config editor/vitest.config.ts` 全绿 **632/18**（placement 55，含新增"窄缝被旧格子跨过""每次点击都成功""缝隙太窄则拒绝"三支） |
