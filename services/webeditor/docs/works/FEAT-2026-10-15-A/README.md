# FEAT-2026-10-15-A —— 浏览器本地三件套：光照预设 / 场景导出 / 位姿 gizmo

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-15 |
| 落地 | 本卡提交 |
| 依赖 | M1-1,M1-4,M6-1,M7-* |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-15 新卡（第 ⑧ 张）：浏览器本地三件套 —— 光照预设（Q77）/ 场景导出（Q78）/ 平移·旋转·缩放 gizmo（Q79，2026-10-09 §4.7/§10.4 的"删除拖拽 gizmo"按用户裁定就地修订，规格只描述当前行为，历史留在本卡与坑 37）。用户原话："切换光照预设…将原本场景的所有光照替换成选中的那个光照预设"、"一个按钮，用于导出场景（包含光照信息）"、"把物体的平移、缩放、旋转加回来，但所有操作都只在浏览器本地进行，编辑场景时不向gateway回传数据"。公共口径（Q78）：只写浏览器持有的那份世界 —— 不发 `browser.state_sync`、不 POST 写入路由，`scene_version` 原地不动，调用方随后的 `/updateItem`/`/updateWorld` 照旧通过 `expected_version` 并直接覆盖本地结果；可证据只有两条（版本号 + WS 帧计数），`/getWorld` 不能当证据（它由 gateway 向浏览器实时 dispatch，读到的就是编辑后的世界，见坑 37）；导出与 `/getWorld` 逐字节同一份文档，所以"包含光照"无需额外通道。落点：新模块 `editor/src/lighting.ts`（`LIGHTING_PRESETS` 五项 `neutral/studio/dramatic/overcast/lamp`、每条灯只写 `type/intensity/color` 三键、`beamQuaternion()` 把局部 −Z 旋到光束方向并单列 `x===0&&y===0` 退化支、`presetLightRequest()` 不写 id）+ `editor/src/local_edits.ts`（`applyLightingPreset` 删光 `node.light` 再逐条 `addLocalObject`、`poseEditBlock` 三种拒绝按 `RobotDriver` → `attachmentMount` → 场上轨迹的优先级、`sceneExportName` 的 `ILLEGAL_IN_FILENAME` 清理 + 去尾点 + 空名回退）；`world.ts` 新增本地写半区 `addLocalObject/removeLocalObject/setLocalTransform`（同一套校验与 watermark 只前进不复用，但一律不 bump 版本、不上报；`setLocalTransform` 是双写 —— `node.transform` + `rawComponents` 里那份 `Transform`，因为 `toWorldObject` 序列化后者，只改 typed 视图会让 `/getWorld` 与导出报旧位姿；保留未知键与 `parent`，物体没有 `Transform` 时补全三项，这是 §3 允许的可达路径）；`renderer.ts` 把 three 的 `TransformControls` 藏进六方法 `PoseGizmo`（提交在 `mouseUp` 不在每帧 `objectChange`，否则破 §8.5；编辑规则 `poseEditable` 由调用方注入，因为它要读 `state.traj`；`retargetGizmo()` 改幂等 —— 目标节点未变就不重发 `attach`，否则换模式会把 handle 拆了重装、测试看到重复序列；拖拽期间借相机、释放时交还用户开关而非无条件打开；`isEditorOnly()` 按身份比 helper（three 命名 `TransformControlsRoot`），`/captureScene`、`/get2DAABB`、`worldAabb()` 三条通道一起排除）；`main.ts` 接工具栏 `<select id="lighting-preset">` + `[data-tool=translate

## 任务卡原文

### FEAT-2026-10-15 —— 浏览器本地三件套：光照预设 / 场景导出 / 位姿 gizmo（新卡；依赖 M1-1、M1-4、M6-1、M7-*）

用户 2026-10-15 的要求原话：「添加三个新功能：1.切换光照预设，提供一个下拉菜单，里面存放若干预设的光照信息，在切换时将原本场景的所有光照替换成选中的那个光照预设。2.提供一个按钮，用于导出场景（包含光照信息）3.把物体的平移、缩放、旋转加回来，但所有操作都只在浏览器本地进行，编辑场景时不向gateway回传数据。」按长期口径（记忆：文档 → 实现 → 测试 → 本卡 → 本地提交）顺序完成。

**三条的公共口径（决议 Q78，规格 §4.8 开头四条）**：一、**只写浏览器持有的那份世界**，不发 `browser.state_sync`、不 POST 任何写入路由，所以 `scene_version` 原地不动 —— 调用方随后发来的 `/updateItem`/`/updateWorld` 的 `expected_version` 照样通过，并且**直接覆盖**本地结果（这是"本地"二字的代价，写进规格而不是藏在注释里）。二、可观测判据因此只有两条：版本号不变 + **WS 一帧 `browser.state_sync` 都没发出**。`/getWorld` **不能**当证据 —— 它是 gateway 向浏览器实时 dispatch 的（`local_gateway.py` 的 `_dispatch_to_browser`，`X-World-Source: live`），本地编辑后 `/getWorld` 读到的正是编辑后的世界，那恰恰是 §1.4 "浏览器是权威"的体现（本卡初版把这条写反了，纠正过程记为**坑 37**）。三、导出的文档与 `/getWorld` 的回答**逐字节相同**（同一份 `world.toJSON()`），所以"包含光照信息"不需要额外通道 —— 预设换掉的灯就在里面。四、三类操作都不碰 gateway 代码、不碰 `contracts/`、不碰 `docs/ur5e_table_empty.json`。
