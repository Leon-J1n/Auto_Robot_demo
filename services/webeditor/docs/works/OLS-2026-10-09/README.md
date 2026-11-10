# OLS-2026-10-09 —— OLS-2026-10-09

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M0-3,M1-2 |

## 原因与做了什么（PLAN_STATE 备注）

维护卡（非 §2 任务卡）：上游 `docs/OLS接口.md` 更新后同步受影响面 —— 物体类别、机械臂大类/小类、缩略图接口。决议记在规格 §13.4（Q20~Q24），§13.1 的 Q2/Q5 加交叉指针（Q2 补 `type=`，Q5 的"不做缩略图"改为本期内做，CORS 结论不变）；规格正文 §4.6/§6.1/§6.2/§6.3 改写并新增 §6.11（三 id 空间、`/list`、`/listCategory`、`/robot`、`/thumbnail`、代理白名单七条），§12.7 验收补三组列表 + 类别分组 + 缩略图降级，§13.2 增 O7~O9 开放项（类别列表路由名、thumbnail 路径基准、robot 能否被世界引用）；计划文档 §1.4 路由清单重写、§5 坑 8 加 `type=` 约束、M1-2 与 M6-2 卡片加修订注。代码：`ols_stub.py` 升级为三类型库（scene 2 / model 5 / robot 3，模型含 `urdf+meshes/*.stl+metadata.json+thumbnail.png`，model id 0..4 固定以匹配 `scene_example.json`），`_typed_kind/_name_for_id` 用 `isdigit()` 挡住负数与非数字；`library.ts` 新增 `LibraryKind/LibraryFileKind/listCategories/thumbnailUrl/robot()`，`fileUrl(...,kind)` 默认 `"model"` 故世界解析路径与 `renderer.ts` 零改动，代理白名单扩为七条。测试：新建 `tests/test_ols_stub.py` 7 例（真起 HTTP 服务，覆盖三 id 空间列表键集合恰为 `{id,name,category}`、类别逐项/按类排序、`/file` 缺 type→400、`type=scene`→400、`../`/绝对/`.tga`→403、`/thumbnail` 三类型 200 `image/png`、CORS 头），`library.test.ts` 23→28 例（`/file?type=model&id=` 全部断言改写 + robot/thumbnail/白名单/listCategories/`bundle()` 走 `type=robot`）。验证见本卡提交前的三套命令输出

## 任务卡原文

（本卡在原 `Web前端实施计划.md` §4 无独立卡体，决策与验收记录散见于当时的规格决议；原文已整体归档至 `docs/works/历史档案/`。）
