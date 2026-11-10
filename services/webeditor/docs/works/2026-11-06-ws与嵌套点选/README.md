# 2026-11-06 WebSocket 断线与嵌套点选

## 原因

最新运行仍出现 Vite `/events` 的 `ECONNABORTED` / `ECONNRESET`。同时相机挂在机械臂层级下时，模型解析与点选会把相机和机械臂视为同一对象。

## 做了什么

- Q152：Vite WebSocket 代理过滤客户端主动断开；浏览器重连定时器去重，并忽略旧 socket 的关闭回调。
- Q152：点选沿实际注册的世界对象节点回溯，模型解析不覆盖已有 `userData.objectId`。
- 修订需求规格 §4.5、§5.8；不改世界文件格式、HTTP 路由或资产文件。

## 验证

| 项 | 值 |
|---|---|
| 渲染器定向测试 | 139 例通过（含相机嵌套点选回归） |
| TypeScript | `npx tsc -p editor/tsconfig.json --noEmit` 通过 |
| Vite 构建 | `npx vite build --config editor/vite.config.ts` 通过；JS 899.73 kB，保留既有 chunk 提示 |
| diff 检查 | `git diff --check` 通过 |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-06 |
| 落地 | Q152 本地提交 |
| 验证 | 见上表 |
