# M0-2 —— 重写类型契约

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | — |

## 原因与做了什么（PLAN_STATE 备注）

新契约落 `contracts/types.ts`（WorldFile/WorldObject/WorldComponent + 7 类 props + `Props<T>` 交叉保留原字典 + WS 协议 + §5.9~§5.12 载荷/结果 + RuntimeConfig），根 `types.ts` 已 `cmp` 校验逐字节一致；旧类型整体迁到 `editor/src/legacy-types.ts`（过渡 shim，M1-4/M6 删除，未用 any/@ts-ignore），`npx tsc --noEmit`、`npm run build`、`npm test`（35 例）全绿

## 任务卡原文

#### M0-2 重写类型契约（世界文件模型）
- **规格**：§3、§2
- **涉及**：`contracts/types.ts`（整体重写）、根 `types.ts`（**必须与前者内容一致**）
- **改动要点**：新类型 = `WorldFile`(`scene_schema_version`/`scene_version`/`scene_id`/`scene_name`/`objects`)、`WorldObject`(`object_id_in_scene`/`object_name_in_scene`/`components`)、`WorldComponent`(`component_type`/`properties`) + 7 类组件的 properties 接口（`TransformProps`/`MetadataRefProps`/`MeshRendererProps`/`SemanticProps`/`LightProps`/`CameraProps`/`RobotDriverProps`）。删除 `SceneSnapshot`/`EntityView`/`TimelineManifest`/`CameraView`/`SceneEvent`(128) 等旧类型。
  所有组件 properties 用 `Record<string, unknown>` 交叉保留原始字典（§3.8 的 `rawComponents`）。
  新增 WS 协议类型：`BrowserHello`/`BrowserRequest`/`BrowserResponse`/`BrowserStateSync`/`SceneApplied`（动作枚举见 §5.9）。
- **DoD**：`npm run build` 通过（此时 `main.ts`/`renderer.ts` 会大面积报错 → 允许先用最小 shim 保持编译，或本卡与 M1-1 合并提交；**不允许**用 `any` 或 `@ts-ignore` 掩盖）。
- **验证**：`npx tsc -p editor/tsconfig.json --noEmit`。
