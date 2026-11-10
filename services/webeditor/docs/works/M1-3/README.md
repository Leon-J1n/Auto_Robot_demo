# M1-3 —— `UrdfLoader`

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M1-2 |

## 原因与做了什么（PLAN_STATE 备注）

`editor/src/urdf.ts`：`parseUrdf`（DOMParser）+ `buildUrdfTree` 零位姿展开，joint 节点承载 origin 变换，`revolute/continuous/prismatic` 才登记 handle（fixed 只当变换节点，避免 M4-3 误驱动）；`rpy` 经 `Euler(r, p, y, "ZYX")` 输出 xyzw（§2.8）—— 顺序必须是 `"ZYX"`：URDF rpy 是固定轴 roll→pitch→yaw，即 `R = Rz(yaw)·Ry(pitch)·Rx(roll)`，逐样本数值比对与 `ZYX` 的最大误差 1.1e-16，而 `"XYZ"` 给出的是另一个旋转（已记为 §5 坑 12，测试里用 `[π/2, π/2, 0]` 复合作用例锁死）；`package://` 交给 `normalizeModelPath` 重写，逃逸/绝对/URL 文件名报 `424 DEPENDENCY_FETCH_FAILED`（§6.4）；cylinder 按 Z-up 旋转几何；`meshes` 工厂缺文件时不留占位立方体（§6.8）。14 例 `urdf.test.ts` 全绿（18 类畸形文档拒绝、沿链 origin 复合数值断言、mesh scale、多根森林）。为测试新增 devDependency `happy-dom`（仅测试环境，`lockfileVersion` 仍 9.0，构建产物不含）；`ols_stub.py` 的 urdf 夹具删掉未声明的 `world` 父 link（严格解析会拒绝），本地需 `--rebuild` 重生成 `ols_data/`

## 任务卡原文

#### M1-3 `UrdfLoader`（TS，静态展开）
- **规格**：§6.4、§1.1 职责表
- **涉及**：新建 `editor/src/urdf.ts`；对照删除 `local_urdf.py:load_urdf`
- **改动要点**：DOMParser 解析 `links/joints/origin(xyz,rpy)/axis/mesh(filename,scale)`；构建 `Object3D` 树（joint 节点为可动节点，visual 挂 link 下）；`package://<pkg>/<rest>` → **同一模型目录内 `<rest>`**（§6.4，与 OLS 服务端改写规则对齐）；相对路径/绝对路径/`../` 一律拒绝；递归发现 mesh 内部贴图引用。rpy → 四元数需按 xyzw 输出（§2.8）。
- **DoD**：`urdf.test.ts` 覆盖：多 link、连续关节 origin 复合、`package://` 重写、逃逸路径拒绝、mesh scale。本卡只需**零位姿展开正确**，可动性留给 M4-3。
- **验证**：`npx vitest run editor/src/urdf.test.ts`。
