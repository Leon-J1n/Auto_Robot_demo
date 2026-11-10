# M1-5 —— Load Scene

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M1-1,M1-4 |

## 原因与做了什么（PLAN_STATE 备注）

`main.ts`：Load Scene 按钮（`data-feature="viewport.loadScene"`，该键已补进 `features.json`，否则 `applyFeatureVisibility` 会把按钮藏掉）→ `#scene-file`（`accept="application/json,.json"`）→ `loadSceneFile()`：先按 §11.1 拒 >100 MB，再 `JSON.parse` + `parseWorld`，任一步失败只 `showError`、不碰 `state.world`；成功后 `renderer.syncScene` + `reportWorld()` 发 `{"type":"browser.state_sync","payload":{"world":…}}`（§5.8；socket 实例存进模块级 `let socket`，gateway 今天只 `async for _ in socket: pass` 排空不处理，真正落地在 M2-1，坑 1 的 `max_msg_size` 也在那张卡）；状态栏 `worldStatus()` 按 §4.1 显示 `#scene_id scene_name · v{scene_version} · {n} objects`，且 `/api/bootstrap` 不可用的早退路径同样显示。删除：`prepareAssetEntry`/`prepareDependencyBundle`/`uploadAsset`/`importRemoteAsset`/`loadSimstate`/`assetDependencyReferences`/`urdf*`/`obj*`/`selectedPath`/`pathBasename`/`dependencyModal*`/`requestDependencyFolder`/`confirmDependencies`/`pendingAssetEntry`/`UploadFile` + `#asset-upload`/`#simstate-upload`/`#asset-dependencies` 三个 input + `#dependency-modal` 整块 DOM + `panels.assets.upload`/`importRemote` 按钮与禁用选择器 + `logic.ts:resolveAssetDependency` 及其单测（能力归 `library.ts:normalizeModelPath`）。`features.json` 的旧键（`panels.assets.upload`、`assetDependencies*`、`commandBar.assetUpload`）保留到 M6-3 统一增删。验证：`tsc --noEmit` 0 错、100 例 / 6 文件（比上卡少 1 例＝删掉的依赖解析用例）、`npm run build` 通过、python 10 OK；浏览器手测（gateway 8080 + `ols_stub.py --port 8000` + vite 5173，经 `#scene-file` 注入真实 File）：加载 `tests/fixtures/scene_example.json` → 状态栏 `#1 Sample Scene · v1 · 5 objects`、15 个 OLS 请求（`/model?id=` 与 `/file?id=&path=`，`package://` 已改写）、`state_sync` 帧 2192 B 发出且 gateway 无异常断线、画面渲染出 apple/ur5e/table/相机视锥（唯一 warn 是 `table.obj` 引用了不在其 `reference` 里的 `material.mtl`，按 §6.6 降级）；非法 JSON 与伪造 100 MB+ 两次均只弹 `Load Scene 失败：…`、场景仍是 5 objects、未再发 `state_sync`。两点环境说明：本地 gitignored 的 `ols_data/model/ur5e/ur5e.urdf` 是 M1-3 改生成器前的旧产物（含未声明的 `world` 父 link，严格解析按 §3.6 拒掉该物体并弹错），本次用 `--rebuild` 刷新；自动化浏览器视口仅 531px，`@media(max-width:1050px){.command-bar>.button{display:none}}` 会隐藏按钮（既有响应式样式，非开关问题），已实测 `hidden=false` 且 `btn.click()` 触发 `#scene-file` 的 click

## 任务卡原文

#### M1-5 Load Scene（用户手动路径）
- **规格**：§4.1a、§5.8
- **涉及**：`main.ts` 的 `commandBar` 模板区(约 75-79)、`app.addEventListener("click")`(654)、`showError`(465)
- **改动要点**：新增 **Load Scene** 按钮 → `<input type="file" accept="application/json">` → `FileReader` → `parseWorld` → 应用；**≤100 MB** 前置校验（§11.1）；成功后 `state.websocket.send(JSON.stringify({type:"browser.state_sync",payload:{world}}))`（§5.8）；**删除**旧上传/依赖选择弹窗/`loadSimstate`。
- **DoD**：浏览器手测：选择 fixture → 场景正确显示；>100 MB 或非法 json → `showError` 且不破坏当前场景。
- **验证**：`node tools/browser_smoke.cjs`（M7 重写后回归）。
