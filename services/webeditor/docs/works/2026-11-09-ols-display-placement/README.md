# 2026-11-09 OLS 中文名称与放置旋转

## 原因
OLS 新增 object_name_Chinese 和 placement_quaternion_xyzw，资产面板需要采用中文名称，摆放需要采用资产声明的旋转。

## 做了什么
- 就地修订需求规格 §4.6。library.ts 为三类列表补读同类型详情，使用 semantic_properties.object_name_Chinese；无中文名或详情失败保留原名、分类及条目。
- placement.ts 复用现有四元数读取与默认 TABLE_YAW，采用 geometric_properties.placement_quaternion_xyzw，覆盖道具、自由相机、机械臂新增与替换，以及无浏览器量测时的占地计算。机械臂底座高度的回退计算同步采用该旋转。
- 无有效四元数时兼容旧默认值。挂载姿态、场景原有物体姿态不变；不改 OLS、Gateway、共享协议，不增加依赖。

## 验证
- 全部 Vitest：23 文件、695 用例通过；定向 library/placement 共 107 用例通过。
- npm run build：类型检查与生产构建通过，JS 906.25 kB（gzip 250.21 kB），相对 HEAD 的 905.82 / 250.05 kB 增加 0.43 / 0.16 kB；存在构建体积提醒。
- 负控制：移除中文名选择，恰好 3 项新增用例失败；强制返回旧旋转，恰好 1 项新增用例失败。逐次恢复并核对 SHA-256。
- 本轮未启动或停止用户服务；未进行真实浏览器操作。
- audit_parity.py 启动后持续无输出，已中止，未取得通过结论；未提交派生 parity-probes.json。

## 进度
| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-09 |
| 落地 | 本任务唯一的本地提交 |
| 验证 | 全量前端测试、类型检查、生产构建、负控制 |
