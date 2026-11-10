# FIX-2026-10-27-A —— FIX-2026-10-27-A

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-27 |
| 落地 | 本卡提交 |
| 依赖 | — |

## 原因与做了什么（PLAN_STATE 备注）

维护卡（非 §2 任务卡）：Windows 的目录 fsync 把已成功的落盘报成失败，并顺着 `/events` 把浏览器的 WS 打断（新坑 105） —— 用户口径「Agent 一直显示 browser xxxx is not connected，检查这是为什么并修复」。判据先于猜测：磁盘 `var/gateway_state.json` 的 `browser_id` 与 `/api/health` 的 `browser.connected:false` 同时成立，加上 `var/stack/gateway.err.log` 里 31 条 traceback 全在 `gateway_state.py:save()` ⇒ 不是绑定丢了，是登记/写盘被打断。改动面只有 `gateway_state.py`：新增 `_fsync_directory()`（`os.name != "posix"` 直接返回），两处原子写（状态文件 `save()` 与 §8.1 receipt）各换成调用它；POSIX 的持久化语义一字未动。测试：`tests/test_gateway_state.py` 新增 `DirectoryFsyncTests` 4 例 —— 钉 `nt` 跑两条写入路径、钉 `posix` 断言 open/fsync/close 各一次；四例都不依赖跑在哪个平台。陌生 `browserID` 仍回 503 的负控制由既有两例钉住（`test_a_browser_that_is_not_connected_gets_the_honest_answer`、`test_a_browser_that_never_helloed_is_not_connected`），它们随整套 python 一起由红转绿。回归链：python 483 例 FAILED(72 失败 / 80 错误) → 487 例 OK（那条"预存在的红"四轮来第一次清零，它同时是产品缺陷）、`tsc --noEmit` 干净、vitest 492 例 / 17 文件逐位不变（本卡不动 TS）、`npm run build` 通过、`parity_inventory` 重跑后 `docs/api-inventory.json` 无变化（没动 `local_gateway.py` 的行号）、`audit_parity` 62/62 · gaps []。真机：一次性栈 8098（`--library-url` 复用用户已在跑的 OLS 8000，他的 8099/8500 全程未动）+ 纯 aiohttp WS 客户端，`connected` 在 hello 后为 `true`、一帧 `browser.state_sync` 后仍 `true`、`closed:false`、`ping` 通、`/allocateObjectID` 绑定 id 200 / 陌生 id 503。生效条件：修复在 gateway 进程里，用户那台 8099 需要重启 gateway 才吃到（本卡不碰他的进程与状态文件）。收尾：8098 停止并核对端口已空，探针与日志删除；`docs/parity-probes.json` 按既有惯例丢弃、未提交。

## 任务卡原文

（本卡在原 `Web前端实施计划.md` §4 无独立卡体，决策与验收记录散见于当时的规格决议；原文已整体归档至 `docs/works/历史档案/`。）
