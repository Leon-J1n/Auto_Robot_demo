# FIX-2026-10-13-D —— 载入场景三问题排查

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-14 |
| 落地 | 本卡提交 |
| 依赖 | FIX-2026-10-13-A,M1-4 |

## 原因与做了什么（PLAN_STATE 备注）

FIX-2026-10-13 维护卡第 ④ 张（2026-10-14 追加）：无 UV 的网格不再被 MTL 贴图调制，机械臂按 `Kd` 上色（决议 Q72，规格 §6.5 补一句）。动因是用户的反证："这些资产在 Blender 里显示正常"——同一份 OBJ/MTL 不该在浏览器里更暗，说明第 ① 张的"前端已经在按 `Kd` 上色"查错了。机制（读 three 源码得出，不是从画面反推）：`OBJLoader.js:758` 只在面里有 `vt` 时才写 `uv` 属性，无属性的网格在着色器里按 `uv=(0,0)` 采样；`MTLLoader.js:365` 把 `map.colorSpace` 设成 `SRGBColorSpace`、`:388` 把 `Kd` 从 sRGB 转进工作空间，两边是同一个色值 ⇒ 漫反射 = `Kd × 占位色 = Kd²`，`Kd=0.033` 的深色杆件被压到 `0.0011`，这正是"纹理丢失/发黑"的观感。改动只在一处：`renderer.ts:dropUnusableTextures()`（`parseAsset()` 的 obj 分支解析完就调），`!geometry.attributes.uv && material.map` 则置空 `map` 并 `needsUpdate = true`；只动 obj 分支（dae/glb/fbx 与各资产自带 UV 的情况一概不碰，`models/1~28` 有 `vt` + 真实贴图，动了反而洗掉正常资产的贴图）。不做的三件事：不给网格合成 UV（资产侧的活）、不省掉占位图的下载、不碰 `forceOverrideColor`（§6.8 故意丢贴图，用户 2026-10-13 明确"是预期行为，不用管"）。测试：`renderer.test.ts` 新增 2 例（自建 `arm.obj`/`arm.mtl`，`Kd 0.486 0.675 0.8` + `map_Kd arm.png`）钉住"无 `vt` ⇒ `hasAttribute('uv')` 为 `false`、`map === null`、`color` 为 `7caccc`（=`rgb(124,172,204)`，即 `Kd` 本身）"与"有 `vt` ⇒ `map` 保留"；为这两例先修的是测试桩 —— `ImageLoader` 走 `document.createElementNS`（`three/src/loaders/ImageLoader.js:39`），旧 `stubCanvasDocument()` 只桩 `createElement`，异常被 `parseAsset` 的 MTL `try/catch` 吞掉后材质退回默认白，症状是"`map` 恒 `null`、颜色 `ffffff`"，看着像生产代码坏了。vitest 260→262 例 / 12 文件。真机（gateway 8099 + vite 5188 + 真实 OLS 8000，载入 `scene?id=1`）前后两张视口图按像素聚类，四组主色块全部单调变亮且与 `Kd²→Kd` 一致：`(3,3,3)→(31,31,31)` 4651 px、`(85,85,85)→(105,105,105)` 3960 px、`(24,55,81)→(60,86,103)` 2328 px、`(8,8,10)→(130,130,130)` 1322 px，共 15565 像素变亮（最大 +121）、整图均值 34.24→35.35，其余像素不动（桌子/苹果/篮筐有 `vt`）；A/B 摘掉这一行调用即回到 `before`。另清掉一条假缺陷（判据写进坑 32）：本卡一度让 smoke §12.1 稳定红（树 `[]`、状态栏回到 `#0 · v0 · 0 objects`，网关日志成排 `stale or unknown; dropped`），真因是上一轮取证遗留在内置浏览器里的 5188 标签页——同一验证网关上的第二个 viewer 用空世界抢答了 `/getWorld`（§5.2 首个回答者优先），把刚载入的场景回卷；导航离开后 smoke 立刻 8 项全绿，`local_gateway.py` 与前端都没有缺陷。验证：`tsc --noEmit` 0 错、vitest 262 例全绿、`npm run build` 通过（`index-BM_zm-mK.js` 824.34 kB，chunk>650 kB warn 为既有）、`python -X utf8 -m unittest discover -s tests` 102 例 OK、`audit_parity.py` 52 PASS / gaps []、`parity_inventory.py` 退出码 0、`cmp types.ts contracts/types.ts` 无输出、`node tools/browser_smoke.cjs` 8 项全绿。

> 注：本节卡体原文为多张卡共享。

## 任务卡原文

### FIX-2026-10-13 —— 载入场景三问题排查（维护卡，非 §2 任务卡；依赖 M5-*、M7-*）

用户 2026-10-13 报的三条现象，本卡逐条取证后**②③ 落在前端，① 两侧都有**：第 ① 条当天只查到资产侧（缺 UV），2026-10-14 由「同一份资产在 Blender 里显示正常」这条反证推翻，补查出前端另有一处真实缺陷，见 **FIX-D**。规格同步：§4.4 补光学系与排除图层两条、§6.5 补无 UV 不采样贴图一条、新增 **§13.19 Q70/Q71/Q72**；`docs/场景文件描述.md` §3 与 §4.6 两处正文改写；本文 §5 新增**坑 30/31/32**（坑 30 的原始结论已于 2026-10-14 就地更正，同日按"纯色路线"再补一段）并就地修正**坑 19/27** 的失效描述。本卡的第 ⑤ 条工作不在这里，见下一节 **FIX-2026-10-14 / FIX-E**（`RobotAttachment` 挂载语义）。
