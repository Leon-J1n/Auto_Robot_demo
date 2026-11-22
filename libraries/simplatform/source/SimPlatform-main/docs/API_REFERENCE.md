# RobotSimPlatform 接口文档

本文档按运行时服务边界整理仓库的公开接口。接口形状以对应头文件、路由注册和实现为准；可选后端未编译时，相应能力不可用。已有细节型文档仍保留，见文末索引。

## 1. 服务总览

| 服务/边界 | 接口形式 | 实现入口 | 默认地址/产物 |
|---|---|---|---|
| sim_core（含稳定 C ABI、插件和 CLI） | C++20 API、C ABI、命令行 | `include/sim/*.hpp`、`include/sim/c_api.h`、`src/` | `sim_core`、`sim_platform_c.dll`、`simulator` |
| Python SDK 与控制服务 | 同进程 ctypes、GUI TCP JSONL、Live TCP JSONL、算法绑定 | `python/sim_platform/`、`python/sim_algorithms/` | `pip install -e .`；Live 默认 OS 分配端口 |
| Web Gateway | HTTP JSON、WebSocket、静态前端 | `web/gateway/server.py` | `http://127.0.0.1:8080`，事件 `/events` |
| Agent Service | HTTP JSON 对话/turn API | `agent_service/api.py` | `http://127.0.0.1:8765` |

Web Gateway 与 Agent Service 是两个进程/服务。Gateway 调用 Agent Service；Agent turn 再通过 Gateway 提供的带令牌控制 API 和 Python Live 控制端点操作当前场景。

## 2. 公共约定

- JSON 属性名大小写敏感。HTTP JSON 使用 UTF-8；TCP JSONL 每行一个 JSON 对象，响应也以换行结束。
- 位置、长度、时间单位分别为米、秒；关节角为弧度，速度为弧度/秒；力为牛顿，力矩为牛顿米。四元数数组采用 `w,x,y,z`。
- 句柄只在创建它的 Simulator/World 生命周期内有效。Live object reference 还绑定实例 ID、World ID 和 scene generation，不应跨重建场景复用。
- C ABI 函数返回 `0` 表示成功，非 `0` 表示失败；失败后立即读取 `sim_last_error()`。C++ 接口通过 `sim::Error` 等异常报告失败。
- 选项能力取决于构建开关及插件：MuJoCo、Bullet3 Physics、Bullet3/Coal/IFPS Collision、FCL、Vulkan、Assimp、URDFDOM、Qt 均可能未启用。

## 3. sim_core 服务

### 3.1 原生 C++ API

公共头文件位于 `include/sim/`，目标名为 `sim::core`，命名空间为 `sim`。

| 头文件 | 主要 API | 能力 |
|---|---|---|
| `core.hpp` | `Simulator`、`World`、`Registry`、`Scheduler`、`EventBus`、命令队列、`Error` | World 生命周期、ECS、步进、事件、系统阶段调度 |
| `assets.hpp` | `ResourceManager`、`AssetIR`、`UrdfImporter`、`ObjImporter`、`AssimpImporter`、`AssetValidator` | 加载、缓存、验证和实例化资产 |
| `robot.hpp` | `Robot`、`InstallRobotControlSystem` | 关节状态/命令、FK、Jacobian |
| `physics.hpp` / `plugin.hpp` | `IPhysicsBackend`、`MuJoCoBackend`、`PluginManager` | Physics backend 生命周期、插件发现和创建 |
| `collision.hpp` | `FCLBackend`、`FCLQueryContext` | 碰撞、距离、机器人构型查询 |
| `render.hpp` | `VulkanBackend`、`RenderSnapshot` | 场景同步、离屏渲染和快照 |
| `sensors.hpp` | `SensorSystem`、Camera/LiDAR/JointState sensor | 传感器创建、更新和帧读取 |
| `state_io.hpp` | `SaveStateFile`、`LoadStateFile` | 版本化状态归档读写 |

`Simulator::create_world(dt, seed)` 创建 World。World 提供实体层级操作、`start/pause/step/reset`、内存 `snapshot/restore`、Registry、FrameTree、EventBus、CommandQueue 和 Scheduler。Scheduler 阶段依次为 `PreUpdate`、`CommandUpdate`、`ControllerUpdate`、`PrePhysics`、`PhysicsStep`、`PostPhysics`、`CollisionSync`、`SensorUpdate`、`RenderSync`、`Render`、`EventDispatch`、`PostUpdate`。

直接使用原生 C++ 时，调用方负责按需构造后端、`build(world)` 并调度其 step/sync；完整托管后端生命周期可使用 C ABI 或 Python SDK。更完整的类、字段和示例见 [C++ API 速查](API_CPP_QUICK_REFERENCE.md)。

### 3.2 稳定 C ABI

头文件 `include/sim/c_api.h`；共享库目标 `sim_c_api`，Windows 产物名 `sim_platform_c.dll`。`SimHandle` 是总实例句柄，`SimWorldHandle`、`SimEntityHandle` 和 `SimStateHandle` 分别代表 World、实体和内存检查点。

| 接口组 | 导出函数 |
|---|---|
| 版本、错误和实例 | `sim_api_version`、`sim_last_error`、`sim_create`、`sim_create_with_plugins`、`sim_destroy` |
| World、场景快照与元数据 | `sim_create_world`、`sim_editor_entity_count`、`sim_editor_entity_info`、`sim_scene_snapshot_json_size/json`、`sim_scene_transforms_json_size/json`、`sim_scene_metadata`、`sim_get_object_id`、`sim_find_entity_by_object_id` |
| 实体、资产和组件 | `sim_load_asset`、`sim_create_entity`、`sim_destroy_entity`、`sim_set_parent`、`sim_set_entity_name`、`sim_create_primitive`、`sim_set/get_transform`、`sim_set_transforms`、`sim_set_visual_properties`、`sim_set_rigid_body`、`sim_set/get_ifps_sphere_count` |
| 光照 | `sim_create_light`、`sim_set_light_properties`、`sim_get_light_properties`；属性通过 `SimLightProperties` 表示，角度为弧度 |
| 机器人与关节 | `sim_robot_joint_count/name/info`、`sim_robot_link_count/info`、`sim_robot_get_joint_state`、`sim_robot_set_joint_positions`、`sim_robot_set_joint_continuous`、`sim_robot_command_joint_positions`、`sim_robot_command_joint_positions_batch`、`sim_robot_command_joint_positions_tuned`、`sim_robot_command_joint_positions_with_effort_limits`、`sim_robot_command_joint_efforts`、`sim_robot_command_joint_velocities`、`sim_robot_set_joint_effort_limits` |
| 运动学和接触 | `sim_robot_kinematics_batch`、`sim_robot_configuration_contacts`、`sim_robot_configuration_collision`、`sim_robot_configurations_collision` |
| 碰撞和物理同步 | `sim_collision_sync`、`sim_physics_sync`、`sim_physics_contacts`、`sim_collision_check`、`sim_collision_distance`、`sim_scene_collision_batch`、`sim_apply_body_wrench` |
| 传感器 | `sim_create_camera`、`sim_set_camera_properties`、`sim_camera_get_dimensions/intrinsics/rgb/depth`、`sim_create_lidar`、`sim_lidar_point_count`、`sim_lidar_get_points` |
| 仿真和状态 | `sim_start`、`sim_pause`、`sim_get_status`、`sim_poll_event`、`sim_step`、`sim_step_unpaced`、`sim_step_profiled`、`sim_get_time`、`sim_reset`、`sim_save_state`、`sim_restore_state`、`sim_save_state_file`、`sim_restore_state_file` |

所有字符串参数/输出使用 UTF-8。JSON 缓冲区接口先调用 `_size` 获取含结尾 NUL 的容量，再调用序列化函数。批量关节配置按 configuration-major 排列；Jacobian 每个关节 6 个数，顺序为线速度 xyz、角速度 xyz。碰撞 contact API 通过 `required` 返回所需 pair 数，调用方应在缓冲区不足时重试。每个 `SimHandle` 的 ABI 调用由内部 mutex 串行化。

### 3.3 插件和 CLI

插件使用 `.simplugin.json` 描述符；`PluginManager` 支持发现、按 ID 创建 Physics backend。可选构建插件包括 Bullet3 Physics、Bullet3 Collision、Coal Collision、IFPS CPU/GPU。Physics 插件实例存活时不要卸载其动态库。

`simulator` CLI 命令：`run`、`server`、`bake <asset>`、`validate <asset>`、`plugins list`、`backends check`。`server` 会启动 `python -m sim_platform.server`，其余命令在当前进程执行。详细参数以 `simulator <command> --help` 和当前构建能力为准。

算法插件另有独立 `sim_algorithms_c` 库：`include/sim_algorithms/registry.h` 导出 `sim_algorithms_count()`、`sim_algorithms_at(index)`、`sim_algorithms_find(name)`。返回的 `SimAlgorithmEntry` 描述 no-args/int/UTF-8 string 函数指针；字符串和注册表生命周期由库管理。Python `sim_algorithms` 包在此之上提供 registry/bridge。

## 4. Python 服务与 SDK

Python 包位于 `python/sim_platform`。主要入口分三种：

| 入口 | 传输 | 适用场景 |
|---|---|---|
| `Simulator`、`World`、`Entity`、`Robot`、`Camera`、`LiDAR` (`sdk.py`) | ctypes 调用 C ABI | 当前进程内无窗口仿真 |
| `EditorSession`、`EditorRobot` (`editor.py`) | GUI 进程 TCP JSONL | 编辑器和可视化脚本 |
| `connect/launch/connect_or_launch`、`LiveSession` 代理 (`live.py`) | 跨进程 Live TCP JSONL | Agent 或自动化控制正在运行的实例 |

无窗口 SDK 覆盖场景导入、实体/Primitive/Light、Transform/刚体、机器人关节控制与状态、FK/Jacobian、碰撞检查、Camera/LiDAR、仿真 step/start/pause/reset、事件、状态保存恢复和场景快照。完整类方法见 [Python API 速查](API_PYTHON_QUICK_REFERENCE.md)。算法接口由 `python/sim_algorithms` 提供 registry、Python 算法包装和 Native bridge；细节见 [算法集成手册](ALGORITHM_INTEGRATION_GUIDE.md)。

### 4.1 Live RPC 协议

传输为每连接 TCP 字节流，UTF-8 JSONL，一问一答；请求 `{"id": ..., "method": "...", "params": {...}}`，成功响应 `{"id": ..., "ok": true, "result": {...}}`，错误响应 `{"id": ..., "ok": false, "error": {"code": "...", "message": "..."}}`。必须先发 `session.hello`，声明 `protocol_version` 和 workspace。Live Python 客户端当前协议版本必须匹配服务端。

`python/sim_platform/server.py` 当前 dispatcher 实现的方法如下（注册状态以实现为准；JSON Schema 是可供工具消费的静态白名单，可能落后于代码）：

| 类别 | 方法 |
|---|---|
| 会话 | `session.hello`、`session.ping`、`session.shutdown` |
| 场景读取/对象 | `scene.query`、`scene.snapshot`、`scene.transforms`、`scene.create_entity`、`scene.create_primitive`、`scene.load_asset`、`scene.load_robot`、`scene.ensure_robot`、`scene.create_camera`、`scene.create_lidar`、`scene.get_transform`、`scene.set_transform`、`scene.update_transform`、`scene.set_transforms`、`scene.set_parent`、`scene.destroy_object`、`scene.find_object`、`scene.set_name`、`scene.set_visual`、`scene.set_rigid_body`、`scene.get_ifps_sphere_count`、`scene.set_ifps_sphere_count`、`scene.apply_wrench`、`scene.resolve_object` |
| 后端配置 | `backend.apply` |
| 机器人 | `robot.get_link`、`robot.command_joint_positions`、`robot.set_joint_positions`、`robot.execute_joint_segment`、`robot.set_joint_continuous`、`robot.command_joint_velocities`、`robot.configuration_collides`、`robot.configurations_collide`、`robot.kinematics`、`robot.configuration_contacts` |
| 仿真 | `simulation.start`、`simulation.pause`、`simulation.status`、`simulation.step`、`simulation.step_profiled`、`simulation.poll_event`、`simulation.physics_contacts`、`simulation.sync_physics`、`simulation.sync_collision`、`simulation.reset` |
| 后端、碰撞和传感器 | `backend.status`、`backend.apply`；`collision.check`、`collision.distance`、`collision.scene`；`camera.configure`、`camera.describe`、`camera.frame`；`lidar.points` |
| Workspace alias | `workspace.get`、`workspace.resolve`、`workspace.bind`、`workspace.unbind` |
| 状态与能力 | `state.save`、`state.restore`、`state.save_file`、`state.restore_file`、`capabilities.query`；共享内存：`shared_memory.allocate`、`shared_memory.describe`、`shared_memory.release` |
| 机器人补充 | `robot.list_links`、`robot.joint_limits`、`robot.joint_state`、`robot.command_joint_positions_batch` |

方法参数和结果应以 `HeadlessService._invoke` 及 LiveSession 同名封装为准。通用参数约定：对象类方法的 `object` 接受 object reference、alias 或句柄；Transform 使用 `translation`(3)、`rotation_wxyz`(4)、`scale`(3)；数组方法使用 `positions`/`targets`，批量方法使用二维数组；状态接口可返回 opaque state reference；camera/lidar 方法返回尺寸、内参、RGB/depth 或点云数组。RPC 通用错误码包含 `invalid_request`、`protocol_mismatch`、`invalid_argument`、`unsupported_operation`、`connection_lost`、`invalid_response`、`simulation_error` 等；实体引用应优先使用带 instance/world/object/generation 的 object reference，而不是裸运行时 handle。会话以 workspace 隔离 alias 注册表，场景 generation 变化时引用可能失效。

请求 schema 文件 `schemas/live-control.schema.json` 与实现并非始终同步；例如其 method enum 不含实现后来加入的 `scene.transforms`、`backend.apply`、`scene.create_primitive` 等。因此 schema 不能替代服务端实现作为完整 API 清单。

## 5. Web Gateway 服务

实现：`web/gateway/server.py`，基于 aiohttp。默认监听 `127.0.0.1:8080`，Agent Service 默认 `127.0.0.1:8765`。大多数响应为 JSON；失败时使用 HTTP 状态码和 `{error:{code,message,...}}` 或 aiohttp HTTP 错误。端点明细：

| 方法和路径 | 用途/主要载荷 |
|---|---|
| `GET /api/health` | Gateway 与底层仿真健康状态 |
| `GET /api/bootstrap` | 前端初始完整快照、Agent 连接、feature 和 presentation 状态 |
| `POST /api/commands` | 场景命令入口；携带命令、`baseRevision` 等并返回更新快照/冲突 |
| `GET /api/assets` | 资产列表 |
| `POST /api/assets/upload` | 上传资产 multipart 数据 |
| `POST /api/assets/import-remote` | 导入远端资产 |
| `DELETE /api/assets/{asset_id}` | 删除资产 |
| `GET /api/assets/{asset_id}/file[/{path}]` | 读取已登记资产文件 |
| `POST /api/state/load-simstate` | 从 `.simstate` 文件载入仿真状态 |
| `GET /api/sensors/{entity_id}/frame?mode=rgb\|depth` | 获取 Camera 帧图像 |
| `POST /api/agent/jobs` | 提交自然语言任务；返回 job id/status |
| `GET /api/agent/jobs[/{job_id}]` | 列出或读取 job |
| `POST /api/agent/jobs/{job_id}/cancel` | 取消 Agent job |
| `GET /api/agent/conversations` | 列出对话，可按实现支持的 query 筛选 |
| `POST /api/agent/conversations` | 创建对话 |
| `GET /api/agent/conversations/{conversation_id}` | 读取对话 |
| `DELETE /api/agent/conversations/{conversation_id}` | 删除对话 |
| `POST /api/agent/conversations/{conversation_id}/activate` | 激活并恢复对话场景 |
| `GET /api/diagnostics` | 获取近期诊断日志 |
| `GET /api/metrics` | Agent turn、capture、WebSocket 计数指标 |
| `GET /api/presentation` | 当前展示模式和快照/时间线元数据 |
| `POST /api/presentation/mode` | 切换展示模式；请求带 `mode`、`basePresentationRevision` |
| `POST /api/agent-control/presentation/snapshot` | Agent 发布当前快照 |
| `POST /api/agent-control/presentation/timeline` | Agent 发布完整/压缩时间线 |
| `POST /api/agent-control/assets/resolve` | 由资产标识解析受管理资产路径 |
| `POST /api/agent-control/captures` | 开始帧采集 |
| `POST /api/agent-control/captures/{capture_id}/frames` | 追加一帧 snapshot |
| `POST /api/agent-control/captures/{capture_id}/finish` | 完成采集，可通过 `publish` 决定发布 |
| `POST /api/agent-control/captures/{capture_id}/abort` | 中止采集 |
| `GET /api/timelines/{timeline_id}/frames/{index}` | 获取单帧 |
| `GET /api/timelines/{timeline_id}/data` | 获取时间线数据 |
| `GET /events` (WebSocket) | 推送场景、仿真、对话/job、presentation 事件 |

Agent control 写接口要求当前活动 job 对应的 `X-Agent-Job-Id` 与 `X-Agent-Control-Token`。命令写入会校验场景 revision；模式切换校验 presentation revision，冲突返回 `409`。Agent 执行期间禁止切换模式/其他互斥场景写入。Presentation 时间线要求 schemaVersion `1.0`、相同 scene/world/generation 绑定、合法 entity/transform，simulationTime 有限且严格递增；默认最大时间线 512 MiB，可由 `SIM_WEB_TIMELINE_MAX_BYTES` 调整。Feature 开关来自 `SIM_WEB_FEATURE_CONFIG` 指定的配置。

`/events` 事件基本结构：`eventId`、`sceneId`、`revision`、`type`、`presentationRevision`、UTC `timestamp`、`actor`、`payload`。Web 接口受限路径和详细字段以 handler 实现及 OpenAPI 导出为准。

## 6. Agent Service

实现 `agent_service/api.py`，aiohttp HTTP JSON 服务，默认 `127.0.0.1:8765`；默认 SQLite 文件为当前目录 `.sim-cache/agent-service/conversations.db`，可用 `AGENT_SERVICE_DB` 或 `--database` 指定。JSON 请求体限制 16 MiB。请求/响应模型定义在 `agent_service/schemas.py`，额外字段在严格请求模型中会被拒绝。

| 方法和路径 | 请求 | 响应/语义 |
|---|---|---|
| `GET /health` | 无 | `{status:"ok", model:{...}}`；model 未配置时服务仍可健康 |
| `POST /v1/conversations` | `target?`、`sceneBinding?`、`presentationMode?` | `201 ConversationView` |
| `GET /v1/conversations?target=` | query 可选 | 对话摘要数组 |
| `GET /v1/conversations/{conversation_id}` | 无 | ConversationView，含消息与 turns |
| `DELETE /v1/conversations/{conversation_id}` | 无 | `{deleted:true}`；活动 turn 会取消 |
| `POST /v1/conversations/{conversation_id}/turns` | `requestId`、`message` 必须；`controlContext?`、`presentationMode?`、`sceneBinding?`、`debug?` | `202 TurnView`；同 conversation 的相同 requestId 幂等 |
| `GET /v1/conversations/{conversation_id}/turns` | 无 | TurnView 数组 |
| `GET /v1/conversations/{conversation_id}/turns/{turn_id}` | 无 | 单个 TurnView |
| `POST /v1/conversations/{conversation_id}/turns/{turn_id}/cancel` | 无 | 更新后的 TurnView，取消尽力而为 |
| `GET /v1/conversations/{conversation_id}/events?after=&limit=` | 游标可选，limit 默认 200、最大 1000 | EventView 数组 |

`presentationMode` 取 `real-sync` 或 `sync-and-animate`。`sceneBinding` 含 `sceneId`、`simSessionId`、`sceneGeneration` 和可选 Live `connect` endpoint。`controlContext` 是 Gateway 控制平面上下文，包括 Gateway URL、control token、job ID、scene binding、模式和 robot assets；它不能由用户自然语言拼装。Turn 状态为 `queued -> running -> succeeded|failed|cancelled|interrupted`；服务重启时未完成 turn 标记为 interrupted，不自动重放。每个 conversation 同时一个活动 turn，全局并发默认 4（`AGENT_SERVICE_MAX_CONCURRENT_TURNS`）。

成功响应字段见 `ConversationView`、`TurnView`、`MessageView`、`EventView`。错误响应为 `{detail, error:{code,message}}`，常见状态：400 `INVALID_REQUEST`、404 `CONVERSATION_NOT_FOUND`/`TURN_NOT_FOUND`、409 业务状态冲突。`GET events` 为轮询接口，不是 WebSocket；Web UI 实时事件由 Gateway `/events` 提供。

## 7. 协议 Schema 与实现边界

`schemas/agent-command.schema.json` 描述 Agent command 子集，不等同于 Python Live RPC 的完整 dispatcher。`schemas/live-control.schema.json` 也可能落后于服务代码；编写客户端时以本文件标出的具体服务源码为最终依据，遇到未知字段/方法先核对实现和版本，不要假设所有安装构建启用同一后端。

## 8. 深入参考

- [C++ API 速查](API_CPP_QUICK_REFERENCE.md)
- [Python API 速查](API_PYTHON_QUICK_REFERENCE.md)
- [运行中 Python 控制设计](LIVE_PYTHON_CONTROL_DESIGN.md)
- [网页 Agent 服务设计](WEB_AGENT_SERVICE_DESIGN.md)
- [算法库集成架构](ALGORITHM_INTEGRATION_ARCHITECTURE.md) 与 [算法集成手册](ALGORITHM_INTEGRATION_GUIDE.md)
