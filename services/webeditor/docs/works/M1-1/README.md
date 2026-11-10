# M1-1 —— 浏览器侧世界解析器

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M0-2,M0-3 |

## 原因与做了什么（PLAN_STATE 备注）

`editor/src/world.ts`：`parseWorld` 返回 `World`（DoD 的 8 类用例 + 自父环/悬垂父级/重复组件/空场景 共 24 例全绿），两遍解析 + 父级失效传播迭代到不动点；`rawComponents` 原样保留并可 `toJSON()` 回读等于提交内容（fixture `toEqual` 断言）；`applyWorld/updateItem/addObject/removeObject` 与 §5.5 水位、§5.7 版本、§5.14 级联/摘除语义已实现，供 M3-1 直接调用。差异说明：`aabbOf(id)` 不在 world.ts —— 世界 AABB 依赖真实几何，落在 `renderer.ts`（M5-2 `worldAabb`）

## 任务卡原文

#### M1-1 浏览器侧世界解析器
- **规格**：§3.4、§3.6、§3.8、§2.5、§2.8
- **涉及**：新建 `editor/src/world.ts`；`main.ts:state`、`main.ts:refreshSnapshot`
- **改动要点**：
  - `parseWorld(json: unknown): World` —— 校验 + 归一化：id 必须是 uint（拒绝负数/小数/字符串）、`object_id_in_scene` 重复拒绝、`parent` 前向引用需**两遍解析**、父链成环拒绝（`INVALID_PARENT_CYCLE`）、缺 `Transform` 按单位位姿回退、未知 `component_type` **忽略并 `console.warn`**（不得抛错中断）。
  - 内部内存模型：`WorldIndex { byId: Map<number, WorldObject>, childrenOf: Map<number, number[]>, rawComponents 保留 }`，提供 `aabbOf(id)`、`resolveSceneVersion()` 等只读访问。
  - `rawComponents` 必须原样保留（§3.8）：不裁剪、不改名、不换算。
  - `scene_version` 与 id 分配水位由浏览器维护（§3.9、§5.5）。
- **DoD**：`editor/src/world.test.ts` 覆盖：正常解析、重复 id、前向 parent、环、未知组件、非 uint id、空 objects、`0xFFFFFFFF` 映射为 `null` 根。
- **验证**：`npx vitest run editor/src/world.test.ts`。
