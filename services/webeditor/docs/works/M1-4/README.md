# M1-4 —— 渲染器接入新世界模型

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M1-1,M1-2,M1-3 |

## 原因与做了什么（PLAN_STATE 备注）

`renderer.ts` 整体重写为消费 `World`：`syncScene(world)` 两遍挂父子（子可先于父，§3.6），位姿单点写入且四元数按 xyzw（§2.8），Z-up 只在最外层 `simWorld` 转 `-π/2`；`Light`→四类 THREE 灯（未知 type 降级 Ambient+warn 即 O4，Directional/Spot 的 target 挂本地 `-Z`）、`Camera`→`LineSegments` 视锥（`cameraFrustumSegments(cameraRig(camera))`）、`MetadataRef`→`LibraryClient` 拉模型（`.urdf` 按实例展开、静态格式共享模板 + `cloneAsset`，坑 13）、`MeshRenderer.forceOverrideColor` 覆盖材质并丢贴图（§6.8）；无 `MetadataRef` 的物体是空节点、无占位立方体（§3.7），签名门控避免重复 populate。删除：`onTransform`/`setTool`/gizmo、`syncTimelineFrame`、collider/IFPS overlay、实例化批次、内置几何与加载方块、`assetResourceBase`；`main.ts` 同步删掉 gizmo 工具栏与 overlay 复选框（它们驱动的渲染能力已不存在）。中间态（Q14）：视口只吃 `state.world`，M1-5「Load Scene」/M2-3 `/getWorld` 接通前为空，旧 `/api/bootstrap` 快照不再绘制视口（面板、相机预览仍走 entity 路径，`logic.ts`/`legacy-types.ts` 的删除按卡在 M6）。新增 `logic.ts:cameraRig`（Q12 缺省归一化）、`urdf.ts:MeshFactory(filename, rgba)`（Q15）。验证：`renderer.test.ts` 22 例（DoD：5 个物体节点、`Light1.parent===Cube`、视锥、无占位方块；另锁 xyzw 分量顺序、MetadataRef→STL 挂载与 override 只作用本实例、DAE `Z_UP` 只补偿一次、FBX 单位、blob 引用解析、capture 排除图层与行翻转），全量 101 例 / 6 文件、`tsc --noEmit` 0 错、`npm run build` 通过、`python -X utf8 -m unittest tests.test_local_assets` 10 OK。已知在途不一致：`tools/browser_smoke.cjs` 仍在用 `adapter.loadAsset` 与旧上传/快照流程（本卡后即失效），按 M1-5 那句"（M7 重写后回归）"由 M7-2 重写，不要临时打补丁

## 任务卡原文

#### M1-4 渲染器接入新世界模型
- **规格**：§3、§4.1、§6.8
- **涉及**：`renderer.ts:syncScene`、`populateNode`、`applyTransforms`、`loadAsset`、`select`、`frame`、`RendererCallbacks`
- **改动要点**：
  - `syncScene(world: World)` 替换 `syncScene(snapshot)`：按 `Transform.parent` 建真实父子 `Object3D`；四元数按 **xyzw**；保留最外层 `-π/2` Z-up 旋转；`MetadataRef` → M1-2 拉模型；`MeshRenderer.forceOverrideColor` 覆盖材质；**无 `MetadataRef` 的物体不得生成占位立方体**（§3.7）。
  - `Light`/`Camera` 组件映射到 `THREE.*Light`/相机视锥（复用 `logic.ts:cameraFrustumSegments`）。
  - 删除 `onTransform` 回调与 `setTool("move"|"rotate"|"scale")` 相关分支（§4.7）。
- **DoD**：加载 `tests/fixtures/scene_example.json` 后 `renderer.test.ts` 断言 5 个物体节点存在、`Light1.parent===Cube`、相机视锥存在、无立方体占位。
- **验证**：`npx vitest run editor/src/renderer.test.ts`。
