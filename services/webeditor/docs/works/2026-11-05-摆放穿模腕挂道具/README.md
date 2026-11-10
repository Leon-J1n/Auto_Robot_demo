# 2026-11-05 摆放穿模：腕挂 model 道具须留在碰撞集

## 原因

上一卡（`2026-11-05-摆放孤立项与确定性搜索`，commit `a390d2e`）之后，用户复测：

> 发现了一个问题：修改后的摆放有可能会与场景中其他物体穿模，如当前场景中的钢笔和微波炉

即新摆的道具会压进现场里**挂在机械臂腕上的微波炉**（以及被手夹着的钢笔）。

排查：碰撞集由 `placement.ts:armFamilyIds` 把"随臂移动的那一整套"剔出去（**Q137** 的动机是臂每帧在动，别把动的东西当静态障碍吃掉桌面）。但那个闭包沿 `RobotAttachment.parent_object_id` 与 `Transform.parent` **两条边走到底、不分 id 空间**，于是把**挂在腕上的 `type:model` 道具**也一并当臂系剔出了碰撞集——`docs/ur5e_passive_rig.json` 里微波炉（id 4 / model 30）正是 `RobotAttachment.parent_object_id = 2` 挂在臂上。被剔出 ⇒ `occupiedBoxes` 不再量它 ⇒ 新摆放直接坐在它此刻占着的那块真实空间里 ⇒ 穿模。（Round-3 的贴边搜索只是让摆放更敢往缝里钻，于是更容易撞上这个"本该是障碍却被忽略"的物体；真正的根因是这条闭包。）

## 做了什么

**`armFamilyIds` 只沿 robot id 空间的成员闭包**（`editor/src/placement.ts:armFamilyIds`）。判据一句：`ridesTheArm(id) = world.get(id)?.metadataRef?.type === "robot"`。臂／夹爪／腕部相机在出厂场景里都是 `$LIBRARY_SERVER/<robot_id>`（`type:"robot"`，见 fixture：臂 id2=robot1、夹爪 id3=robot2、腕相机 id4=robot3），继续随臂出局；而**被手夹着的钢笔、挂在腕上的微波炉是 `type:"model"`**，不再被认作臂系成员，**留在碰撞集里**，摆放必须避开。

- 底座（`category=support`）、吊在空中的那台相机：本来就不在臂的子节点上（`parent:null`、无 attachment），从不被闭包触到，行为不变（与 **Q137** 一致）。
- 没有新增判据去猜角色：直接复用 §4.6.1 既有的"`type:model`＝道具、`type:robot`＝机械臂那侧"这条 id 空间口径。

**规格 §4.6.1 就地修订**：「随机落点」那条里「碰撞集 = …臂每帧都在动，闭包沿两条边走到底」改写为「闭包沿这两条边走、但只沿 `metadataRef.type === "robot"` 的成员走；挂在链上的 `type:model` 道具留在碰撞集里」，标注为 2026-11-05 穿模修复。

**单测**（`editor/src/placement.test.ts`，+2）：
- `wristCarriedModel(40)` 在真场景上挂一件 `type:model` 到臂 id2（parent 2 + `RobotAttachment`）。`armFamilyIds` 仍含 {2,3,4}、**不含 40**。
- `placeAsset` 用 `BoundsMeasure` seam 让 40 号那件盒盖住整张桌顶：修复后它是障碍 ⇒ 拒绝（`桌面上放不下`）；旧闭包把它当臂系忽略 ⇒ 会落进桌里（即用户看到的穿模）。

**明确"不做"**：
- 不给摆放加安全间距——"只贴边不算碰撞"是 **Q134** 既定口径，加 margin 会违反它；本卡修的是"该量的没量"，不是"贴太近"。
- 不改 `sceneAABB` 量测来源（仍 `renderer.sceneAABB`，含当前关节态，Round-2 硬要求）。
- 不动任何用户维护的输入件（含 `docs/ur5e_table_empty.json`；测试里挂道具用的是合成对象）。
- 不动协议面、不动 `scene_version`、不新增 gateway 调用 ⇒ 路由清单与 `audit_parity.py` 探针不增不减。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本次本地提交（master，紧接 `a390d2e`） |
| 改动 | `editor/src/placement.ts`（`armFamilyIds` 闭包加 robot 过滤 + 文档）、`editor/src/placement.test.ts`（+2 单测、+`wristCarriedModel` 辅助）、`docs/Web前端需求规格v3.md`（§4.6.1 碰撞集口径就地修订） |
| 验证 | `npx tsc -p editor/tsconfig.json --noEmit` 干净；`npx vitest run --config editor/vitest.config.ts` 全绿 **634/18**（placement 57，含新增"腕挂 model 不入臂系""摆放不与腕挂 model 穿模"两支）。**碰撞判定是纯逻辑，已在单测层复现挂载结构并验证；本轮未起浏览器/网关做端到端截图复测。** |
