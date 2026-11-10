# M0-3 —— 契约夹具

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | — |

## 原因与做了什么（PLAN_STATE 备注）

`tests/fixtures/scene_example.json`、`robot_grasp.traj` 与 docs 原件 `cmp` 一致；`ols_stub.py` 提供 `/list /scene /model /file` + CORS + OLS 路径规则，样例库含 `crate`（依赖 404）与 `tga_case`（贴图 403）两条降级路径夹具；生成的 `ols_data/` 已 gitignore

## 任务卡原文

#### M0-3 契约夹具
- **规格**：§12.1
- **涉及**：新建 `tests/fixtures/scene_example.json`（从 `docs/scene_example.json` **原样复制**，作为不可漂移的测试输入）、`tests/fixtures/robot_grasp.traj`（复制 `docs/robot_grasp.traj`）、新建 `tests/fixtures/ols_stub.py`（用 `http.server` 起一个假 OLS：实现 `/list`、`/scene`、`/model`、`/file` 四个路由 + **返回 `Access-Control-Allow-Origin: *`**）。
- **DoD**：`python -m http.server` 风格可独立启动 OLS stub；vitest 与 unittest 都能读到两个 fixture 文件。
- **说明**：新会话**不要**改 `docs/scene_example.json`（它是用户维护的权威样例）。
