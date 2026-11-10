# 2026-11-06 UR10 支架与显示盒

## 原因

用户报告机械臂切换到 UR10 后，机械臂与底座悬空；UR10 的显示包围盒比肉眼观察到的模型大，且包围盒下沿高于底座上沿。

本机 OLS HTTP 服务未运行，但本地资产目录可读。UR10 metadata 的整机盒 `min.z=-0.03468528`；离线按 URDF 声明关节姿态重建视觉 OBJ 后，根部固定底座几何最低点为 `z=0`，旧支架定位会低约 3.47 cm。整机单盒是弯曲姿态下各 link 几何的世界轴对齐并集，会把 link 间空域也包括进来。

## 做了什么

- Q150：URDF 支架承重面从根 link 沿固定关节量 `<visual>` 底面，遇 movable joint 停；不支持的网格格式或量不到几何时回退到整机盒。非 URDF 摆放不变。
- Q150：`urdf_bounding_box_per_link` 默认改为 `true`，URDF 屏幕线框每 link 一只，调用方仍取 `worldLinkBoxes()` 并集。通过常量 `false` 可恢复整机单盒。
- 更新需求规格 §4.6.1、§4.8.6、§9.1、§9.2、§12.43；不改 OLS 资产、不改协议、不改 `/get2DAABB` 量测。

## 验证

| 项 | 值 |
|---|---|
| `npx vitest run --config editor/vitest.config.ts editor/src/placement.test.ts` | 67 例通过 |
| `npx vitest run --config editor/vitest.config.ts` | 673 例通过 |
| `npx tsc -p editor/tsconfig.json --noEmit` | 通过 |
| `npx vite build --config editor/vite.config.ts` | 通过；JS 899.33 kB，触发 650 kB chunk 提示 |
| `python -X utf8 -u -m unittest discover -s tests -p test_gateway_routes.py` | 13 例通过 |
| UR10 离线资产探针 | 根部固定底座 `min.z=0`；整机加载盒 `min.z=0`，metadata `min.z=-0.03468528` |
| 负控制 | 移除固定关节边界检查后，目标用例按预期失败；恢复后验证通过 |
| 浏览器真机 | 未运行；OLS HTTP 服务未启动 |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-06 |
| 落地 | Q150 本地提交 |
| 验证 | 定向及全量测试、类型检查、构建、gateway routes 均通过；未启动 OLS 或浏览器栈 |
