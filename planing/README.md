# 轨迹优化与时间参数化

MoveSkill 已默认接入：规划 → 路径优化 → 时间参数化 → 碰撞复核 → 按时间戳执行。
`demo.run_demo` 和 `tests/test.py` 调用 MoveSkill 时自动使用，无需修改 commands JSON。
抓取、推拉等独立控制器的接触动作不在本模块改动范围内。

## 来源与实现

- `timing.py` 改编自 `ROBOTSIM/algorithms/timing/ruckig/parameterizer.py`，复用
  `Ruckig.calculate`、`Trajectory.at_time` 的离线计算与采样流程。
  源项目包装标注 0.15.3；本项目使用有 Python 3.10/Windows wheel 的 0.12.2。
- ROBOTSIM 的 `optimization/trajopt` 和 `optimization/curobo` 是需要注入后端的包装，
  不能直接在当前 MuJoCo 环境运行。因此 `trajectory.py` 新增本地的碰撞检查捷径优化
  和五次 Bezier 圆滑，没有把它们冒充为 TrajOpt/cuRobo。
- TOPP-RA、Tesseract TOTG 也已检查，本次没有接入：它们需要额外后端，并且不能
  单凭时间参数化就保证所需的 jerk 限制。

自由关节路径尝试圆滑，保留首尾位置，圆滑曲线在连接点具有连续的位置、速度和加速度。
通过曲线导数控制点的界限，计算满足每个关节速度、加速度、jerk 限制的路径参数限制，
再用一维 Ruckig 驱动该路径，避免各关节独立插值改变几何路径。
圆滑碰撞时逐步减小圆滑程度，必要时沿折线路径分段计时。

直线路径使用 `linear.py` 的自然三次样条串联原始等距 Cartesian IK 点，
速度和加速度跨点连续，整段只在首尾停下。每个采样点通过正运动学检查直线和固定姿态：
位置偏差不超过请求容差与 1 mm 中的较小值，姿态偏差不超过请求容差与 0.25° 中的较小值。
不允许以平滑为由放宽 hard 约束；超过容差或碰撞时回退原路径，原因写入
`postprocessing.linear_smoothing_fallback`。未提供 kinematics 的独立调用同样保留原路径。
圆弧、其他固定姿态路径和回退路径目前仍保留 IK 折线，折点处会减速至零。
本模块不承诺最短执行时间。
碰撞验证使用现有 MuJoCo 检查器，曲线采样间隔按关节变化限制为约 0.01 rad，
连接线继续以 0.02 rad 分辨率检查；这是离散检查，不是数学上的连续碰撞证明。

## 使用与参数

当前虚拟环境已安装 Ruckig；其他环境执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r planing/requirements.txt
```

可选地向 `MoveSkill(config={"trajectory": {...}})` 传入全局配置，或在单次 Move
请求的 `strategy_params.trajectory` 中覆盖：

```json
{
  "timing_method": "auto",
  "optimize": true,
  "smooth_linear": true,
  "sample_period": 0.01,
  "velocity_limits": [1, 1, 1, 1, 1, 1],
  "acceleration_limits": [2, 2, 2, 2, 2, 2],
  "jerk_limits": [10, 10, 10, 10, 10, 10],
  "jerk_scale": 1.0
}
```

这些速度、加速度、jerk 默认值分别使用 rad/s、rad/s²、rad/s³，是仿真配置，
并非从模型提取的机器人厂家额定限制。也可用一个数对所有关节设置相同限制。
已有 `constraints.velocity_scale`、`acceleration_scale` 会乘到对应限制上。
`optimize: false` 关闭几何圆滑，但保留受限时间参数化。
直线连续插值由独立开关 `smooth_linear` 控制，默认开启。
`auto` 优先 Ruckig，未安装时使用 NumPy 五次多项式回退；显式 `ruckig` 缺失时报错。
`quintic` 可用于测试无额外依赖的回退。算法运行失败不会默默忽略限制。

结果增加 `time_from_start`、`velocities`、`accelerations`、`jerks`，
`duration` 为实际规划时长。`postprocessing` 记录算法、点数、处理耗时、限制和采样峰值。
Ruckig jerk 诊断值由加速度差分估计；计算时的 jerk 限制由 Ruckig 和曲线导数界限约束。

执行器按时间戳插值，不再按路径长度重新匀速播放。
kinematic 模式的 `playback_speed` 仅改变显示速度；例如 2 倍播放的视觉速度也加倍，
不代表原规划的物理速度限制变大。actuator 模式按仿真时间发送参考位置，
仅终点等待收敛。控制器实际跟踪误差、硬件运动平滑性需要另行验证；
采样参考的位置在执行时采用线性插值，不保证样本之间的实际驱动加速度连续。

验证命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_trajectory_processing.py -v
```
