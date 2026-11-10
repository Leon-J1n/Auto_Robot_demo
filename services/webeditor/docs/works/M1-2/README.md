# M1-2 —— OLS 客户端与模型资源解析

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M0-1,M0-3 |

## 原因与做了什么（PLAN_STATE 备注）

`editor/src/library.ts` + 23 例 `library.test.ts`（fetch mock，全绿；`npm test` 82 例、`tsc --noEmit`、`npm run build` 通过）：`resolveMetadataRef` 三形态归约 model_id 且拒绝未替换占位符、`normalizeModelPath` 处理 `package://`/`file:` 并拒绝绝对路径/NUL/逃出模型目录、`parseModelMetadata` 保留 `raw` 且 422、`fileUrl` 按 `is_on_cos` 分流（COS 裸 GET vs `/file?id=&path=`）、`bundle` 中 `reference` 逐项拉取（非贴图失败 → `424 DEPENDENCY_FETCH_FAILED`，贴图 403/404 → 降级并告警，§6.6）、URL 去重缓存（§6.7）、`library_via_proxy` 只转发 `list`、`scene`、`model`、`file` 白名单（§6.9；该代理形式与配置键已由 Q68 退役），全程只用简单 GET。差异说明：卡片写的 `fetchMetadata/fetchFile` 命名为 `model()/fileBytes()/fileText()`；`resolveMetadataRef(path)` 不需要 `cfg` 参数（path 永不作 URL 使用）

## 任务卡原文

#### M1-2 OLS 客户端与模型资源解析
- **规格**：§6 全部
- **涉及**：新建 `editor/src/library.ts`；参考删除对象 `local_assets.py:_normalize_obj_bundle`、`urdf_dependencies`、`fetch_remote`（**这些 Python 能力作废，TS 侧需等价实现**）；旧 TS 侧 `logic.ts:resolveAssetDependency`、`renderer.ts:assetResourceBase` 可复用/迁移
- **改动要点**：
  - `resolveMetadataRef(path, cfg)`：正则 `^\$LIBRARY_SERVER/(\d+)$`，同时接受绝对 URL 与 `?id=` 形式（§6.1）；**严禁**把 path 当 URL 直接 GET。
  - `fetchMetadata(modelId)` → `GET {base}/model?id=`；`fetchFile(modelId, relPath)` → 依 `is_on_cos` 分流到 `{cos_base_url}/{path}` 或 `GET {base}/file?id=&path=`（§6.3）。
  - `reference[]` **逐个遍历**；任一失败 → `424 DEPENDENCY_FETCH_FAILED` 语义（局部失败，物体跳过并告警）。
  - 会话内 URL 去重缓存（§6.7）；贴图 403/404 → 降级无贴图（§6.6）。
  - 只用简单 GET，**不加自定义请求头**（§6.9，避免 preflight）。
- **DoD**：`library.test.ts` 用 `tests/fixtures/ols_stub.py` 的 URL 形态或 fetch mock 覆盖：`is_on_cos` 两分支、`reference` 部分失败、未替换占位符、`../` 拒绝、扩展名 403 降级。
- **验证**：`npx vitest run editor/src/library.test.ts`。
- *（2026-10-09 OLS 接口更新后的修订，见规格 §13.4：`fetchFile` 的 URL 实际形态是 `/file?type=<model|robot>&id=&path=`（TS 侧为 `fileUrl(id,path,isOnCos,kind)`，`kind` 默认 `"model"` 所以世界解析路径不受影响）；新增 `robot(id)`、`listCategories(kind)`、`thumbnailUrl(kind,id)`；`list(kind)` 的 `kind` 三值且 item 带可选 `category`；同源代理的白名单（`list`、`listCategory`、`scene`、`model`、`robot`、`file`、`thumbnail` 七条；该代理形式后来按 **Q68** 退役）。`resolveMetadataRef` 的结论不变 —— 数字恒为 `model_id`。）*
