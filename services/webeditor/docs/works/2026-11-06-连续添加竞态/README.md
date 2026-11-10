# 2026-11-06 连续添加竞态

## 原因

用户快速连续向桌面添加物体时，先前已放下的物体偶尔消失，并观察到 Vite 对 `/events` WebSocket 代理出现 `ECONNABORTED` / `ECONNRESET`。

前端资产选择允许并发。空 World 上的两次快速摆放可各自等待并应用一份 baseline，先开始的摆放随后继续改写已脱离 `state.world` 的旧对象；另一方面，重连前后不同 socket 的 `browser.state_sync` 可在旧身份检查之后并发等待自动会话，令旧快照晚于新快照落盘。

## 做了什么

- Q151：库面板场景加载、摆放及替换通过可恢复的异步队列按点击顺序运行。
- Q151：Gateway 对 state_sync 的状态判定、自动会话准备与场景落盘串行化，避免异步间隔覆盖新场景。
- 更新需求规格 §4.6.1、§5.7、§12.44；未增加路由或协议字段。

## 验证

| 项 | 值 |
|---|---|
| 前端串行队列定向测试 | 2 例通过 |
| Gateway state_sync 交错回归与负控制 | 5 例通过；去掉锁后预期断言失败 |
| 全量 Vitest | 675 例通过，21 个测试文件 |
| TypeScript | `npx tsc -p editor/tsconfig.json --noEmit` 通过 |
| Vite 构建 | 通过；JS 899.49 kB，触发 650 kB chunk 提示 |
| Gateway routes | 11 条 GatewayRouteTests 通过；包含默认配置的完整 13 例有 1 例因工作区 `config/constant_config.json` 当前为 `false`、内置默认仍为 `true` 而失败，保留该既有改动 |
| diff 检查 | `git diff --check` 通过 |
| Vite 浏览器真机 | 尚未执行 |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-06 |
| 落地 | Q151 本地提交 |
| 验证 | 回归、类型、构建及 gateway 定向检查完成；未运行浏览器真机 |
