# 运行参数配置

服务每次执行对应命令时重新读取配置，保存后下次命令生效，无需重启。
JSON 不支持注释；配置缺失、格式错误或数值无效会返回 `INVALID_CONFIG`，不会静默回退。

## move 持物限速

编辑 `motion/move/carry_config.json`：

| 字段 | 当前默认值 | 单位 |
| --- | --- | --- |
| `velocity_limit_rad_s` | 1.0 | rad/s |
| `acceleration_limit_rad_s2` | 0.5 | rad/s² |
| `jerk_limit_rad_s3` | 2.0 | rad/s³ |

三个值均须为有限正数，作用于每个机械臂关节。仅在系统确认正在持物时生效。
这些是缩放后的最终上限；命令或普通 move 配置中更低的限制仍保留。
速度上限同时约束轨迹规划与运行时伺服，命令结束后恢复原运行时设置。
例如可先尝试 `0.5`、`0.2`、`0.5`，再根据实际抓取稳定性调整。
普通空载参数仍在 `motion/move/config.yaml`。

## grasp 接触后闭合步数

编辑 `manipulation/grasp/contact_config.json` 中的 `post_contact_step`。
当前默认保留 20 步，允许 0–10000 的整数。双侧手指接触后，继续按既有速度闭合指定仿真步数，受闭合行程限制。
时间 = 步数 × 仿真 dt；dt 为 0.002 s 时，10 步 = 0.02 s，20 步 = 0.04 s，50 步 = 0.1 s。
该字段映射至控制器的 `post_contact_steps`；命令显式提供的 `post_contact_steps` 优先于文件默认值。
