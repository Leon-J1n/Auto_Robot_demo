# 2026-11-19 Control 联调修改简介

基于已合并的上游 main@5e90185，保留 WebEditor 轨迹导出和实测场景输出修复。

- MuJoCo 初始化读取已保存当前关节；依赖关节按 equality 恢复，不以物理步进隐式改变初始现场，显式保存值优先。
- 最终轨迹追加与最终 Scene 相同的实测快照，减少末帧与状态回写不一致。
- 被动机构关节按对象命名空间匹配，禁止用跨对象裸关节名兜底。
- 修复 MuJoCo 3.12 与 NumPy 2.2 的枚举比较兼容问题。
- 支持显式 --collision-model proxy|precise，并在 summary 记录选择；precise 使用完整几何检查，不关闭碰撞，不改变安全阈值，代理校验失败不自动切换。
- 保留 output/summary.json、逐命令结果、最终 Scene 和 execution.traj，供桥接层按事务提交与回放。

验证：相关 Control 回归 127 通过、14 跳过；UR10 单臂模拟联调已取得单请求连续操作、跨编辑姿态保持及最终轨迹证据。Piper IK、双臂和全部抽屉重复操作未验收。控制失败保持真实错误，不伪造成功。

本机 mmap、运行请求、日志和外部 robotiq_reference_bundle 不提交；参考包能复现不代表不同 Linux 网格哈希已完成审核。
