# M7-3 —— E2E 与脚本

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-10 |
| 落地 | 本卡提交 |
| 依赖 | M7-1,M7-2 |

## 原因与做了什么（PLAN_STATE 备注）

DoD：`node tools/browser_smoke.cjs` 全绿（实测 `{"result":"passed","frontend":"http://127.0.0.1:5188","ols":"http://127.0.0.1:8010","checks":8}`，README 记录的 `PLAYWRIGHT_MODULE` 用法未变，脚本仍 `require(process.env.PLAYWRIGHT_MODULE \

## 任务卡原文

#### M7-3 E2E 与脚本
- **规格**：§12、§10.6
- **涉及**：`tools/browser_smoke.cjs`、`tools/audit_parity.py`、`tools/parity_inventory.py`、`tools/serve_verification.py`、`start-dev.ps1`
- **改动要点**：smoke 改流程 = Load Scene fixture → 资产面板加载预设场景 → `POST /updateTraj` → 拖到 340/530 帧像素校验 → `GET /captureScene` → `GET /get2DAABB`；`serve_verification.py` 起 OLS stub；`start-dev.ps1` 增 `-LibraryPort`；parity 工具目标路由换成新接口表。
- **DoD**：`node tools/browser_smoke.cjs` 通过（README 里记录的 `PLAYWRIGHT_MODULE` 用法不变）。
