# RRT 耗时因素与调参入口

本清单对应当前 `Direct → RRT-Connect → shortcut → TrajOpt → 最终复核` 实现。
默认值指代码默认；当前全局 RRT 与复核分辨率均已改为 0.02rad。
第 13 步历史实测值来自
`requests/commands.005/output/commands/013_13/planning_timing.1280.json`。

## 当前基线与本次修改

| 阶段 | 第 13 步耗时 |
| --- | ---: |
| RRT 搜索 | 2.903s |
| shortcut | 0.508s |
| TrajOpt | 0.585s |
| 端点检查 | 0.044s |
| 最终路径碰撞复核 | 0.081s |
| 包含候选处理等开销的 planning 总计 | 4.252s |

本次只将 RRT 的整个同步规划调用放入代理 `query_scope()`：端点、搜索、后处理、
最终碰撞复核使用同一个状态签名，进入和退出各检查一次；退出发现状态或规则变化则拒绝结果。
未调整几何、距离阈值、采样分辨率、搜索参数或后处理预算。没有代理的检查器保持原路径。
诊断新增 `planning_stages_s.proxy_state_guards` 和 `planning_proxy_cache`。

小型 MuJoCo 代理测试，固定起终点、随机种子，关闭后处理，先预热代理；每组 12 次，
每次清空路径缓存：原无 scope 调用中位数 4.686ms、签名计算 12 次；新调用 2.351ms、2 次。
这不是 Tray 场景的回放结果，不能据此推算 4.252s 的下降比例。

## 入口限制

- **全局配置 G**：`config/global.json`；目前碰撞规划仅接受
  `planning.collision_free.rrt_resolution_rad`、`planning.collision_validation.resolution_rad`。
  严格校验会拒绝其他键，不能直接在这里添加 shortcut 或 TrajOpt 参数。
- **请求 R**：规范化 Move 请求；demo 通常合并场景 `move_defaults` 和动作请求。
  部分 approach/interaction 逻辑还会再设置规划预算。
- **规划器构造配置 C**：`CollisionFreePlanner(config=...)` 或运动 action 的构造 `config`。
  下表的 C 参数相对于 `config['planning']['collision_free']`。
  当前 demo 构造运动 action 时没有传入这份配置，所以使用默认值。
  将 C 参数写入请求 `planning.collision_free` **不会被这个规划器读取**；需要接线后才能通过场景调整。
- **场景 S**：场景根节点 `planning_collision_proxy`。
- **固定值 H**：当前写在 Python 中，调整需要改代码或先暴露为配置。

## 碰撞查询：通常是搜索阶段的主要成本

| 因素 | 当前值 / 入口 | 对耗时与行为的影响 |
| --- | --- | --- |
| 场景一致性校验频率 | 本次改为一次 RRT 调用前后各一次 | 避免每条边重复计算全模型签名；几何查询仍逐条执行 |
| 扩展边采样分辨率 | G `planning.collision_free.rrt_resolution_rad = 0.02` rad；C 同名键可覆盖 | 小值增加边内采样，大值可能漏掉采样间碰撞，也会改变搜出的路径；shortcut 同样读取它 |
| 最终复核分辨率 | G/R `planning.collision_validation.resolution_rad = 0.02` rad | 越细复核越慢；不直接改变 RRT 扩展分辨率 |
| TrajOpt 碰撞分辨率 | C `trajopt.collision_resolution`，默认全局最终复核值 0.02 | 控制优化局部路径的采样成本；它不读取请求中的最终复核覆盖值 |
| 安全距离 | R `constraints.safety_distance`；该步 0.003m | 影响碰撞判定和可行空间，过大可能制造窄通道或无解；不是纯性能参数 |
| 代理启用与否 | S `enabled = true` | 使用代理球/盒体；切换检查器会同时改变成本及几何近似 |
| 球覆盖网格尺寸 | S `sphere_cell_size = 0.035`m | 影响生成球数和覆盖形状；粗网格通常减少球数但更保守，可能堵住通道；既有夹爪球模板不会按此键整体重采样 |
| 代理 batch 大小 | S `batch_size = 32`，支持 1～256 | 大批次减少调用次数，但边短时填不满，遇碰撞时可能多算后续样本；需实测 |
| 球数、环境盒数、有效碰撞对数 | 场景几何、机器人/夹爪/持物模板和规则 | 自碰撞与环境距离计算随有效对数增长；不能通过任意忽略碰撞对提速 |
| 容器分解和代理形状 | S `container_names = ['Tray', 'Basket']`、几何构建代码 | 容器壁数量和空腔表达影响成本与可行性；合并成实心盒可能使任务无解 |
| 宽阶段过滤效果 | 代理内部按几何分组筛选，H | 物体分布、姿态和距离决定进入精细距离计算的对数 |
| 场景/夹爪/持物/阶段规则变化 | 运行状态 | 触发代理状态刷新或结构编译；冷启动与热调用成本不同 |
| 路径缓存命中 | 代理 `_paths` 最多 32 条；shortcut 局部边缓存；检查器可选 planning_cache | 完全相同的点、方向、分辨率和策略才可命中；随机边通常复用少。日志 `collision_state_cache: null` 不等于所有缓存都关闭 |

## RRT 搜索与候选目标

| 因素 | 当前值 / 入口 | 对耗时与行为的影响 |
| --- | --- | --- |
| 起点、目标关节构型 | 当前状态、IK 解、放置/接近位姿 | 关节距离、障碍拓扑和窄通道通常比预算本身更影响找到首解的速度 |
| 关节采样范围 | 机器人 `joint_limits` | 范围大时随机样本更分散；缩范围会限制可达空间，不能只为提速修改真实限位 |
| 扩展步长 | H 0.16，关节向量 L2 范数，单位 rad | 大步通常减少树节点，但每条边需要更多碰撞样本，复杂通道通过率可能下降；过小则节点与查询增多 |
| 目标采样概率 | H 0.15 | 高值偏向目标，低值偏向探索；阻挡布局不同，最优值不同 |
| Connect 连续扩展上限 | H 每次 80 步 | 大值可能减少外层循环但延长单次连接；当前内部没有逐步检查搜索 deadline |
| 随机种子 | H 17，每次 plan 重新初始化 | 决定探索顺序和首解时间；固定种子便于对照，多种子评估才能看成功率/尾延迟 |
| 最近邻实现与树规模 | H 遍历全部树节点计算距离 | 节点多时 Python 循环与线性扫描增长；可考虑数组化或空间索引，需维护插入成本 |
| 规划时间预算 | R `planning.planning_time`，默认 10s，该步 10s | 是搜索停止条件，不是固定等待时间；调低主要截断慢搜索，可能导致失败，不会让同一路首解自动更早出现 |
| 尝试次数 | R `planning.max_attempts`，默认 3；候选分支绕障实际强制 1 | 普通关节 RRT 各尝试共享一个 deadline，第一次失败通常已耗尽预算，因此加大该值往往无收益 |
| 请求总超时 | R `constraints.timeout`，常规默认 30s，该步约 60s | 上层跨候选/接近距离控制总预算；单次边检查和部分后处理不是可抢占硬中断 |
| IK 候选数、排序和剪枝 | path_request 的候选、kinematics 与 action 逻辑 | 候选增加会增加直接探测/绕障尝试；本次 4 个候选、1 次绕障、3 个被下界剪枝 |
| 首解后的候选搜索预算 | action 构造 C `post_solution_search_time = 0.15`s | 控制找到可行方案后再比较候选的时间；该步没有继续绕障重试 |
| 姿态偏好 | `config/arm_posture.json` 的 `enabled = false` | 启用后额外评估采样/路径的机械臂高度偏好，可能改变探索顺序和优化接受结果 |
| 偏好采样概率 | 同文件 `rrt_preferred_sample_probability = 0.6` | 只在姿态偏好启用时有效；部分随机采样会比较 3 个构型，增加 FK/评分成本 |
| approach 距离/位姿/高度候选 | skill 与对象目标解析配置 | 目标变化会改变 RRT 难度，失败重试还会增加总耗时；目标解析计时与该步 planning 计时分开记录 |

## 后处理

| 因素 | 当前值 / 入口 | 对耗时与行为的影响 |
| --- | --- | --- |
| shortcut 时间预算 | C `shortcut_time = 0.5`s | 直接控制路径简化开销；设 0 可以跳过其检查循环；可能保留更长、更弯的路径 |
| shortcut 次数与分配 | H 内点随机尝试最多 120 次，前半预算用于顶点捷径 | 与原始路径节点数、捷径碰撞率共同决定实际工作量 |
| TrajOpt 开关 | C `optimize_path = true` 和 `trajopt.enabled = true` | 关闭会跳过普通关节路径 TrajOpt；普通关节路径的 shortcut 仍由自己的预算独立运行 |
| TrajOpt 时间/迭代上限 | C `trajopt.max_time = 0.5`s、`max_iterations = 40` | 限制优化工作量；检查只在阶段边界进行，单次碰撞查询和最终复核可超出预算 |
| TrajOpt 信赖域 | C `trajopt.trust_region = 0.12`、`minimum_trust_region = 0.0001` | 影响每次改动大小、碰撞拒绝率和提前结束；不是越大越快 |
| 收敛与目标权重 | C `trajopt.convergence_tolerance = 1e-5`、`smoothness_weight = 0.25` | 影响提前退出、成本过滤和局部修改的接受率 |
| TrajOpt 回溯尺度 | H 1、0.5、0.25、0.125 | 一个点可能触发多次碰撞查询 |
| 优化控制点密度 | H `_densify(..., 0.16)` | 更密会增加优化变量和待检查局部段 |
| 最终路点密度 | H `_densify(..., 0.04)` | 控制输出点数、最终复核和后续样条处理成本；与采样分辨率是不同参数 |
| 优化结果被拒后的回退复核 | 路径、碰撞与姿态约束 | 优化不安全时复核原 RRT 路径，增加一次开销；该步没有发生此回退 |

## 固定末端姿态分支（该步未使用）

此分支在笛卡尔位置空间搜索，每条边都需连续 IK，成本结构与普通关节 RRT 不同。

| 因素 | 当前值 / 入口 |
| --- | --- |
| 笛卡尔边采样 | R `strategy_params.cartesian_step`，默认 0.02m |
| IK 方法和连续性阈值 | R `strategy_params.ik_method` 默认 `auto`、`max_joint_step` 默认 0.4rad |
| 扩展长度与目标概率 | H 0.08m、0.20 |
| 搜索空间边界 | H 按起终点加 0.20～0.50m margin |
| 迭代次数 | H `max_attempts × 900`，另受搜索 deadline 限制 |
| 捷径预算 | C `fixed_orientation_shortcut_time`，默认 0.5s |
| 平滑开关、预算和步幅 | C `optimize_path`、`smooth_result` 默认 true；`fixed_orientation_smoothing_time` 默认 0.5s、`fixed_orientation_smoothing_step` 默认 0.005m |
| 重建路径 IK | 搜索完成后按整条链重新求解，节点越多工作越多 |

## 执行环境与计时口径

- 当前 RRT 搜索和 NumPy TrajOpt 是 CPU 路径；更换 GPU 或 cuRobo CUDA 参数不会直接加速它。
- CPU 单核性能、系统电源模式、后台任务、内存分配/垃圾回收、NumPy/MuJoCo 版本会造成波动。
- `velocity_scale`、`acceleration_scale`、轨迹 FPS 主要影响时间参数化/执行，不直接改变普通 RRT 首解搜索。
- spline 插值器、`spline_subdivisions`、时间参数化后的碰撞复核影响后续
  `trajectory_preparation`，不能把这部分与 `rrt_search` 混为一谈。
- 对比应固定场景、持物状态、起终点、种子、碰撞规则与缓存预热条件；同时看成功率、路径长度、
  执行时间和多轮耗时分布。缩短规划时间却显著增加执行行程，未必减少总任务时间。

建议顺序：先测本次 scope 修改 → 再测 shortcut/TrajOpt 预算 → 最后测扩展步长和采样概率。
保持碰撞模型与安全约束不变，能更清楚地比较纯实现与搜索策略的收益。
