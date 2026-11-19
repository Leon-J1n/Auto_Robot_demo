# Tray approach 连续轨迹复核失败排查

## 结论

已在完整 commands.005 无界面回放中复现两次失败、第三次成功，命令 13 的累计 planning 为 5.237s，
与用户报告的 5.308s 同类。16 个命令最终全部成功。

主要问题是**最终折线路径的离散采样漏检**：不仅样条路径，原折线路径自身也存在低于 3mm 安全距离的段内位置。
因此仅改用 PCHIP 不能消除此类重试。尚未单独隔离是原始 RRT、shortcut 还是 TrajOpt 首先引入了该段。

## 同一状态下的对比

诊断在准备轨迹报错时立即执行；对三种路径使用相同检查器、代理、持物状态、碰撞规则和 3mm 安全距离。
每组在同一个 query_scope 内检查，没有执行运动或更改碰撞阈值。

| 路径表示 | 采样分辨率 | 100mm 尝试 | 95mm 尝试 |
| --- | --- | --- | --- |
| 原输出折线 | 0.04rad | 通过，所查样本最小距离 3.068mm | 通过，所查样本最小距离 3.030mm |
| 原输出折线 | 0.01rad | 拒绝，首次命中距离 2.782mm | 拒绝，首次命中距离 2.881mm |
| 原输出折线 | 0.002rad | 拒绝 | 拒绝 |
| C2 cubic | 0.04 / 0.01 / 0.002rad | 全部拒绝 | 全部拒绝 |
| PCHIP | 0.04 / 0.01 / 0.002rad | 全部拒绝 | 全部拒绝 |

两次失败中所有原始路点单独检查都通过；违规位于路点之间。违规对均为持物 RubiksCube 与环境 Basket。
检查器会在发现违规后提前退出，因此拒绝结果中的 minimum_distance 不是整条路径的全局最小距离。

## 代码原因

1. `planning/planners/collision_free_planner.py` 将优化路径按最大关节分量步幅 0.04rad 加密，
   再以请求/全局配置中的 0.04rad 复核。大量短段只检查端点，段内间距不足仍可能通过。
2. `planning/planners/trajopt_optimizer.py` 的局部修改和最终输出默认也使用全局 0.04rad。
   离散检查通过并不保证整条边满足安全距离。
3. `planning/constraints/collision_sampling.py` 对连续样条每个区间至少细分两份，
   同时根据导数界增加采样。即使两处都写 0.04rad，实际查询的位置也不相同。
4. 连续轨迹检查因此检出了规划器未查询的位置。已有 cubic → PCHIP 回退仍然违规，
   上层 approach 随后重新规划 95mm、90mm 目标，累计耗时增加。

query_scope 减少的是重复状态签名计算，未取消边内碰撞查询；本次证据不支持“共享校验范围导致漏检”。
其加速还使固定 0.5s 后处理预算内能完成更多优化步骤，路径可能与原先不同，但不能据此断言是哪一步引入违规。

## 后续修正方向

- 让 RRT 扩展、shortcut、TrajOpt 接受检查和最终复核具有一致的采样策略；
  不能只把最终复核调细，否则只会更早拒绝，搜索/优化仍可能反复生成同类路径。
- 可先评估将上述阶段统一为 0.01rad，保留 3mm 距离和现有几何；
  这会增加单次查询成本，也仍是离散近似，并非连续碰撞保证。
- 更进一步可按机器人/持物的空间位移界和剩余净空自适应细分，集中检查窄间隙段。
- 优化结果失败时先尝试保留的优化前路径并做同样严格的连续轨迹验证，
  避免立即改变 approach 目标并重做整个规划。
- 诊断不能通过降低安全距离或跳过最终复核来修复。

本轮只完成诊断和复现工具，没有修改生产规划参数或轨迹生成算法。

## 复现与证据

```powershell
.venv\Scripts\python.exe demo/tools/diagnose_continuous_collision.py `
  --commands requests/commands.005/commands.005.json `
  --output tmp/rrt_collision_diagnosis_replay
```

回放需要已有 VLM 配置与网络、项目本地 OLS 和原生碰撞库。
工具将原始路点及各样条采样保存为 NPZ，并将同状态比较写入 JSON：

- `tmp/rrt_collision_diagnosis_replay/collision_diagnosis.json`
- `tmp/rrt_collision_diagnosis_replay/failure_1.npz`
- `tmp/rrt_collision_diagnosis_replay/failure_2.npz`
- `tmp/rrt_collision_diagnosis_replay/replay/report.29908.json`（后续回放的文件名 PID 会变化）
