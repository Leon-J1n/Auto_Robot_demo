# 2026-11-12 切换相机被当普通道具摆上桌（Q161）

## 原因

用户口径：「我发现现在的代码在切换相机时会把相机当一个普通物体摆放到桌面上。检查一下原来的代码是如何分辨相机和普通物体的。然后修复这个问题。」

原来的分辨法只有一处，**Q134**（`3104e94`）写进 `placement.ts:classify` 的第 ② 条：`semantic_properties.tag` 含英文 `camera` ⇒ 相机。`hasCameraTag` 自那以后一字未改。

但线上数据不说英文。核对 `ObjectLibraryServer/data/robot_index.db` 全 14 条与各自详情：

| id | name | category | tag |
|---|---|---|---|
| 3 | d435i | 机器人.摄像机 | `D435i相机,深度相机,双目,RealSense` |
| 11 | d455 | 机器人.摄像机 | `D455相机,深度相机,双目,RealSense` |

**14 条里没有一条 tag 含英文 `camera`**；含 `相机` 的恰是这两条，其余 12 条（臂／爪／底座／背景）一条不含。于是 `classify` 对每一台线上相机都落到第 ④ 条「道具」，`placeAsset` 跳过「就地换相机」分支、走 `default: placeProp(..., camera=false, ...)`：桌面上多出一个**没有 `Camera` 组件**的普通物体，现场原有的相机一台没换。

为什么单测一直绿：`placement.test.ts` 的两枚相机夹具写的是**编出来的英文 tag**（`"d435i,camera"`、`"d455 camera"`）。判据与夹具互相印证，谁都没碰线上数据 —— 与坑 132 同族：「数据里的词」以真实索引为准，散文与夹具都只当参考。

## 做了什么

1. **规格就地改** `docs/Web前端需求规格v3.md` §4.6.1「角色由数据判」第 ② 条：含 `camera` **或 `相机`**，并写明线上 14 条的实测互斥与「只认英文那半句自 Q134 起对线上每一台相机都判成道具」的因果（**Q161**）。
2. **代码一行判据** `editor/src/placement.ts:hasCameraTag`：`tag.toLowerCase().includes("camera") || tag.includes("相机")`，外加一行说明线上 tag 是中文的注释。`classify` 的次序、`placeAsset` 的换/加分支、`findCameraObjects` 全部未动。
3. **夹具换成线上逐字 tag**：`placement.test.ts` 的 robot 3 / robot 11 两枚 `semantic_properties.tag` 改为上表那两串中文。既有 5 条相机用例（角色判定 1 条、碰撞集 2 条、就地换 1 条、无相机时摆上桌 1 条）随之成为这条缺陷的回归钉。
4. **新增两条用例 + 一个 `taggedSource` 助手**（改写某一枚 robot 的 tag，其余照旧）：英文写法 `d435i,camera` 仍判相机；中文 tag 的底座 `机器人底座,支架,圆柱` 仍判道具 —— 钉住「认的是 `相机` 这个词，不是中文这套字」。
5. 本卡、`docs/works/INDEX.md` 顶行、`docs/works/坑清单.md` 第 134 条。

## 不做

- **不改用索引大类 `机器人.摄像机` 判角色**。规格「角色由数据判，不由大类判」仍成立；`readAsset` 只拿元数据，接大类要改它的签名与面板接线，超出本轮缺陷。
- 不动 `findCameraObjects`（现场侧按 `Camera` 组件判）—— §4.6.1 已写死现场查询与元数据判角色是两条不能互换的判据。
- 不动「换相机只换 `MetadataRef`、作者写的 `Transform` 与内参一字不动」的口径。
- 不修 `/events` 的 `Invalid frame header`（坑 131 已在 Q158 归因于一次性栈，与本轮无关）。
- 不修「一次换两台相机 `scene_sub_version` +2」的计数口径（既有行为，不是本轮缺陷）。

## 验证

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 错 |
| `npx vitest run --config editor/vitest.config.ts` | **729 / 24 文件全绿**（`placement.test.ts` 71 条，+2） |
| `npx vite build --config editor/vite.config.ts` | `index-BWdzTIk2.js` **907.77 kB**（gzip 250.75，较上轮 +0.02 / +0.01）；`index-BJ0BR_3v.css` 21.99 kB 不变 |

**负控制**：把 `hasCameraTag` 的 `|| tag.includes("相机")` 删掉重跑 `placement.test.ts` ⇒ **恰 5 红**，全是相机路：

1. `readAsset … > calls a camera by the tag OLS publishes, the only place one says so`（`expected 'prop' to be 'camera'`）
2. `placement collision … > replaces robot 11 (camera) without reading unrelated table metadata or geometry`
3. `placement collision … > replaces robot 11 (camera) in a scene without a table`
4. `switching the 夹爪 and the 相机 > replaces both cameras by reference and keeps the viewpoints the scene authored`（`expected [] to deeply equal [ 4, 5 ]`）
5. `switching the 夹爪 and the 相机 > stands a camera on the table when the scene has none to replace`（toast 变成道具那句「已把「d455」摆到桌面上」——正是用户报的症状）

两条新用例（英文写法、中文底座）保持绿：它们不依赖中文那半句。恢复后 `md5sum -c` 校验 `placement.ts` 与快照一致。

**真浏览器**（一次性栈：gateway 8098 + 自启 OLS 8096 + `dist/`，跑完已停）：载入预设场景 2（`#2 ur5_table_apple · v3 · 9 objects`）→ 导航栏「机器人」→ 类别「摄像机」（面板列出 `3:D435i相机`、`11:D455相机`）→ 点 d455：

- toast = **已把 2 个相机换成「D455相机」**；计数**仍 9 objects**（没有新增桌面物体）；
- `/api/sessionState`：`camera_candidates` 仍 `[4,5]`（就地换、id 不变），`sceneDigest` 由 `685ee0a8…` 变 `ab99b266…`，`scene_sub_version` 1→3；
- 再点 d435i：toast = 已把 2 个相机换成「D435i相机」，计数仍 9；
- 刷新后仍 `#2 ur5_table_apple · v3 · 9 objects`、digest 不变（Q160 的刷新保现场不受影响）；
- 截图里腕部与头顶两台相机在原视点、桌面上只有苹果（截图存 `var/`，不提交）。

console 噪声按坑 131 的纪律**按 URL 排除**：`/events` 的 `Invalid frame header`（Q158 已归因）与一次性网关还没有现场时首帧 `/getWorld` 的 503；其余无 error。

**接口面**：纯浏览器判据改动，不新增路由、不改请求形状、不碰 gateway ⇒ §5.9 路由清单与 parity 探针不增不减；`types.ts` / `contracts/types.ts` 未动。

## 进度

已完成；一个本地提交。