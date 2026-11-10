# M0-1 —— 新建运行时配置

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | — |

## 原因与做了什么（PLAN_STATE 备注）

`config/constant_config.json` + `load_constant_config` + `--config`（命令行优先）+ `GET /api/runtimeConfig`（注册在通配 static 前）；坏 JSON 退出码 1、缺文件回退默认并 warn，均已实测

## 任务卡原文

#### M0-1 新建运行时配置
- **规格**：§9.1、§9.2
- **涉及**：`config/constant_config.json`(新建)、`local_gateway.py:main`、`local_gateway.py:create_app`、`editor/vite.config.ts`
- **改动要点**：
  - 按 §9.2 的 JSON 结构建文件（`gateway`/`library_server_url`/`cos_base_url`/`max_payload_bytes`/`browser_request_timeout_seconds`/`capture_timeout_seconds`/`max_pending_requests`/`camera_preview`）。
  - `local_gateway.py` 新增 `load_constant_config(path)`：文件缺失 → 内置默认值 + `logging.warning`；JSON 语法错 → **启动失败并打印错误**（与 OLS 行为一致）。
  - `main()` 的 argparse 增 `--config`，默认 `config/constant_config.json`；**命令行参数优先级高于文件**（§9.2）。
  - 新增 handler `runtime_config()` 注册为 `GET /api/runtimeConfig`（在 `create_app` 内**必须注册在通配 static 之前**，§5-坑2）。
- **DoD**：`python local_gateway.py --port 8080` 启动后 `curl -s localhost:8080/api/runtimeConfig` 返回该文件内容；故意写坏 JSON 时进程退出码非 0 且有错误信息。
- **验证**：`python -X utf8 -c "import local_gateway"` + 上述 curl。
