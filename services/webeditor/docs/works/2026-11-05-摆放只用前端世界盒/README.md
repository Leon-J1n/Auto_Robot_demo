# 2026-11-05 摆放只用前端自己的世界盒

## 原因

用户原话：摆放物体时新摆的物体有可能与原本存在的物体穿模，此现象在 id 为 25 和 31 的物体上尤为明显（可能因为这两个物体比较大）；昨天修了 OBJ 物体的包围盒，怀疑 URDF 类物体的包围盒也有问题。**要求前端不再信任 OLS 的 AABB 包围盒，只使用自己的包围盒**（前端本来就要显示包围盒，这是已有可信功能）。中断时补一条硬约束：**「urdf 是可动的，获取包围盒时要根据当前关节的状态生成包围盒」**。

现象根因：`placement.ts` 的碰撞盒来源是 OLS 元数据 `geometric_properties.AABB`（OBJ 模型例外，昨天 **Q144** 改用它网格真实顶点 `objVertexBounds`）。URDF 道具（抽屉柜、微波炉这类 `type:model` 且带关节）出厂声明的 AABB 是按**静止/rest 姿态**、且未必与它当前关节位姿下的真实轮廓一致——25/31 声明的盒比实际伸出来的轮廓小，于是 `spotOnTable` 判"放得下"、摆上去屏幕上就压进邻居。而前端**已经**在为选中描边（§4.5）、包围盒线框（**Q119**）、`/get2DAABB`（**§5.11**）算一条可信的世界 AABB：`renderer.worldAabb(id)` 递归**当前场景图**，天然带着该物体此刻的关节状态与 `TABLE_YAW` 转角。摆放该读的就是这个盒。

## 做了什么

**口径（用户已确认的两项取向）**：改造范围 = A「全用世界系 `worldAabb`」——障碍与候选都用世界盒；拿不到盒时 = 「回退到 OLS AABB」。

- `editor/src/renderer.ts`：
  - 新增 `private readonly loading = new Map<number, Promise<void>>()`，在 `populate` 里对每个带 `MetadataRef` 的物体记下 `loadModel` 的 Promise（`finally` 只在条目仍归本次所有时删除，避免被后续重建覆盖）。
  - 新增 `sceneAABB(objectId): { min; max } | null` = `worldAabb(objectId)` 经 `toSceneBox` 转回世界文件轴系（与 `orientedBox` 产出的障碍盒同系可比；Y-up→场景系的映射与 `/get2DAABB` 那一条一致）。这是"前端显示哪个盒、摆放就量哪个盒"的单点。
  - 新增 `async readyObject(objectId)`：等待 `loading` 里该物体的加载落地（无在途则立即 resolve；失败走那次 `catch`）。
- `editor/src/placement.ts`：
  - 新增 `BoundsMeasure` 接口（`sceneAABB(id) → Box|null`、`settle(id) → Promise<void>`）——摆放读渲染器的接缝；**可选参数**，无接缝时逐字走旧路径 ⇒ 既有摆放单测零改动。
  - `placeAsset` 多一个可选 `measure`：桌顶盒优先 `measure.sceneAABB(tableNodeId)`，量不到才回退 `readMetadataAsset + orientedBox`（两条都没有才抛「工作台…没有可读的包围盒」）；`measure` 一路转发到 `placeCameras`/`placeProp`。
  - `occupiedBoxes` 多 `measure?`、`excludeId?`：现场里每一件障碍先 `measure.sceneAABB(node.id)`，量到就用它（含其当前关节位姿），量不到才回退元数据；`excludeId` 排除正在摆放的那个候选自身。
  - `placeProp` 改**两段式**：先在原点立起来 → `settle` → 量 `sceneAABB` 得 footprint（回退元数据/OBJ 顶点）→ `occupiedBoxes`（排自身）→ `spotOnTable` → 就地改写落点；放不下或无盒则删掉那个临时物体并抛原错误。
- `editor/src/world.ts`：新增 `async placeLocal(initial, settle, finalize)`。**关键**：一次摆放 = `insertObject`（staged，不推子版本）+ `finalize` 后 `writeItem`（改写落点，不推子版本）+ 收尾 `bumpSubVersion` **一次**；`settle`/`finalize` 抛错（放不下 / 无盒）则删掉 staged 物体且子版本**一字不动**。这是为了满足 §4.8 ②「一次编辑 `scene_sub_version +1`」——若沿用 `addLocalObject` + `updateLocalItem` 会把一次摆放数成 +2、一次失败的点击还会 +2 净空改，四元组被无谓推进。
- `editor/src/main.ts:chooseLibraryItem`：构造 `{ sceneAABB: id => renderer.sceneAABB(id), settle: async id => { renderer.syncScene(state.world); await renderer.readyObject(id); } }` 作 `placeAsset` 的新参数传入（`{}` 为 options、`measure` 为其后）。

**明确的边界**：
- 机械臂与换臂路径不扫碰撞（沿用 §4.6.1「臂这一路只算不判」），夹爪/换相机沿用 `updateLocalItem`/`removeLocalSubtree`，未改其行为。
- `agent_owned`/持租约/隔离期点击仍被 §4.6 阶段闸门弹窗拦下、什么都不改——本轮只动 `setup` 期的盒来源。
- 回退仍是可信来源之一：几何没加载、或纯灯/纯相机这类无可量几何的物体，落回元数据 AABB/OBJ 顶点，读不出盒才按「没有包围盒」拒绝上桌（Q134）。
- 不新增 gateway 调用、不改任何请求形状 ⇒ §5.9 路由清单与 `audit_parity.py` 探针不增不减。

**规格就地修订**（保留 Q 溯源）：`docs/Web前端需求规格v3.md` §4.6.1 在「随机落点」那条之后追加一条 override，把旧句「摆放要量这个盒来占位（OBJ 读顶点、其余读元数据 AABB）」标注**作废**，改述为「候选盒与碰撞集都量 `renderer.sceneAABB`（含当前关节状态），量不到才回退元数据」，并写明 `placeLocal` 单次推进子版本、失败零推进。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-05 |
| 落地 | 本地提交（master，未 push） |
| 验证 | `npx tsc -p editor/tsconfig.json --noEmit` 干净；`npx vitest run --config editor/vitest.config.ts` 18 文件 629 例全绿。新增：`placement.test.ts` 一段 `BoundsMeasure` 接缝 6 例（候选经 settle 量自身盒 / 大盒放不下回退并净空撤销 / 障碍用 `sceneAABB` 而非其元数据小盒 / 接缝全 null 时逐字回退元数据、`metaReads` 不变 / 无盒仍拒 / OLS 无盒但前端量得出则能摆 / 桌顶盒用接缝且不再读 `model:29`）；`world.test.ts`「records a measured placement as one sub-version step and rolls a refused one back」钉住成功 +1、拒绝 +0；`renderer.test.ts`「hands back the object's own box in the world-file axes」钉住 `sceneAABB` = `worldAabb` 的 Y-up→场景轴转回。既有 615/620 摆放宽路径零改动（接缝可选）。 |
