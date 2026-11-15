# 全局默认配置

统一入口自动加载项目根目录的 `config/global.json`，与启动命令时的工作目录无关。
修改后重新启动任务即可生效，不需要在每个 commands 文件中重复配置。

```json
{
  "motion": {
    "velocity_scale": 0.6,
    "acceleration_scale": 1.0
  },
  "trajectory": {
    "fps": 60,
    "pause_seconds": 1.0
  },
  "planning": {
    "collision_validation": {
      "resolution_rad": 0.04
    },
    "collision_free": {
      "rrt_resolution_rad": 0.03
    }
  },
  "gripper": {
    "open_speed": 0.8,
    "close_speed": 0.8,
    "grasp_force": 20.0,
    "hold_force": 10.0,
    "maximum_force": 40.0
  }
}
```

- `velocity_scale`：关节速度上限的比例，范围 `[0.01, 1.0]`。
- `acceleration_scale`：关节加速度基准的比例，范围 `[0.01, 1.0]`。
  基准取自机器人 URDF；URDF 未提供加速度时，继续使用项目已有的速度数值兜底规则。
- `fps`：按仿真时间采样及导出回放的统一帧率，必须为正整数，不能超过物理步频。
  它不会改变物理步长或机械臂速度。当前物理步长为 0.002 秒，即最多 500 Hz。
- `pause_seconds`：每段第一帧与第二个原始采样帧之间额外插入的停顿；设为 `0` 关闭。
  仅影响导出回放，不增加仿真执行时间。停顿换算为最近的整数帧，半帧向上取整；
  单帧段不额外停顿。setup 段如果有多帧，也保留相同的停顿规则。
- `planning.collision_validation.resolution_rad`：普通路径和最终轨迹复核的默认关节采样分辨率。
- `planning.collision_free.rrt_resolution_rad`：RRT-Connect 扩展边的默认碰撞采样分辨率，单位 rad。
- `gripper.open_speed` / `gripper.close_speed`：夹爪原子张开和闭合的默认速度；抓取或释放请求中的同名参数可以覆盖。
- `gripper.grasp_force`：默认闭合抓取力，单位 N。
- `gripper.hold_force`：默认保持夹持力，单位 N。
- `gripper.maximum_force`：默认允许的最大闭合接触力，单位 N。

速度、加速度比例限制的是运动上限，并不保证整个运动过程都达到上限。

## 显式覆盖

运动参数优先级从高到低：命令的 `parameters.request.constraints`、任务顶层
`defaults.motion`、场景运行配置/场景 `move_defaults.constraints`、全局配置。
现有显式值会保留，不会被全局文件强行覆盖。

在 commands JSON 的顶层可加入部分覆盖，不需要重复其它默认项：

```json
"defaults": {
  "motion": {"velocity_scale": 0.3},
  "trajectory": {"fps": 30, "pause_seconds": 0.5}
}
```

单条运动命令可在 parameters 中设置：

```json
"request": {
  "constraints": {"velocity_scale": 0.2, "acceleration_scale": 0.5}
}
```

帧率和停顿按整个任务配置：`defaults.trajectory` 优先于场景 `trajectory`，场景值
优先于全局配置。TRAJ 文件只有一个全局帧率，因此不提供单条 command 的帧率覆盖。
重新导出已有 frames.jsonl 时应显式传入其原始采样帧率；修改导出文件头不会重采样旧数据。

`report.json` 中的 `effective_defaults` 保存本次任务生效的默认值，单条命令覆盖后的
速度/加速度上限可在对应运动的 `trajectory.diagnostics.time_parameterization` 中查看。
