# Agent Service 与 Web Gateway 接口清单

本文按运行服务整理当前源码中可调用的接口。路径和字段以实现为准；OpenAPI 文件为补充说明（`web/contracts/openapi.yaml`）。

## 1. Web Gateway（`web/gateway/server.py`，默认 `127.0.0.1:8080`）

### 1.0 `POST /api/commands` 参数与结果速查

所有命令都包在 `{requestId,sceneId,baseRevision,method,params}` 中；`baseRevision` 必须等于当前场景 revision。成功响应统一 `{requestId,data,revision}`。

| `method` | `params` 主要字段 | `data` 成功结果 |
|---|---|---|
| `scene.create_primitive` | `kind`=`cube/plane/sphere/capsule/cylinder`，`name?`、`size?`、`parent?`、`translation?` | `{entity}` |
| `scene.create_primitives` | `count` 1..10000、`kind?`、`spacing?`、`size?`、`group?` | `{entities: number[], count, root}` |
| `scene.create_camera` | `name?`、`parent?`、`frameId?`、`updateRateHz?`、`width?`、`height?`、`verticalFovDegrees?`、`nearPlane?`、`farPlane?`、`output?` | `{entity}` |
| `scene.create_light` | `type?`、`name?`、`parent?`、`color?`、`intensity?`、`range?`、`decay?`、`innerConeRadians?`、`outerConeRadians?`、`angularDiameterRadians?`、`enabled?`、`translation?`、`rotationWxyz?` | `{entity}` |
| `scene.load_asset` | `assetId`、`parent?`、`translation?` | `{entity}`；URDF 作为 robot 加载，普通模型作为 asset。|
| `scene.update_entity` | `entity`；可选 `name`、`transform{translation,rotationWxyz,scale}`、`visual{visible,minLod,maxLod}`、`rigidBody{enabled,dynamic,gravity,mass,friction,restitution}`、`ifpsSphereCount`、`camera{frameId,updateRateHz,width,height,verticalFovDegrees,nearPlane,farPlane,output}`、`light{type,color,intensity,range,decay,innerConeRadians,outerConeRadians,angularDiameterRadians,enabled}` | `{entity}` |
| `scene.set_parent` | `entity`、`parent`（0 表示无父级） | `{entity,parent}`；robot joint 不能脱离 articulation。|
| `scene.delete_entity` | `entity` | `{deleted:number[]}`；robot joint 不能单独删除。|
| `robot.command_joint_positions` | `robot`、`targets[]`（长度必须等于关节数） | `{robot,targets}` |
| `backend.apply` | `physics?`、`collision?`、`renderer?`、`browserRenderer?`（必须 `three`）、`fixedTimeStep?`、`realTimeFactor?` | `{settings}`；切换失败 `BACKEND_CONFIGURATION_FAILED`。|
| `editor.undo` / `editor.redo` | `{}` | `{}`；无历史分别为 `UNDO_EMPTY`/`REDO_EMPTY`。|
| `simulation.start` / `simulation.pause` / `simulation.reset` | `{}` | `{}`，并更新状态。|
| `simulation.step` | `count?`（至少 1） | `{count}`。|
| `state.save` | `name?`（默认 `default`） | `{name}`。|
| `state.restore` | `name?` | `{name}`；不存在 `STATE_NOT_FOUND`。|

命令错误还包括 `METHOD_NOT_FOUND`、`ENTITY_NOT_FOUND`、`COMPONENT_MISSING`、`INVALID_ARGUMENT`、`INVALID_ASSET`、`ASSET_IMPORT_FAILED`、`ROBOT_LINK_LOCKED`；可重试的场景繁忙/版本冲突使用 HTTP 409，否则通常 400。

**最小示例：**

```http
POST /api/commands
Content-Type: application/json

{"requestId":"r-1","sceneId":"main","baseRevision":7,
 "method":"scene.create_primitive",
 "params":{"kind":"cube","name":"Box","size":[0.2,0.2,0.2],"translation":[0,0.1,0]}}
```

```json
{"requestId":"r-1","data":{"entity":42},"revision":8}
```

### 1.1 场景、仿真和资源

| 方法与路径 | 请求 | 响应/语义 |
|---|---|---|
| `GET /api/health` | 无 | `{ok, service, version, agentConnected}`。|
| `GET /api/bootstrap` | 无 | 当前 Gateway 对外的场景快照（由 presentation 同步状态生成；在动画模式下可能是 presentation 当前帧，不保证始终等于物理世界权威状态）：`sceneId`、`revision`、`status`、`simulationTime`、`entities`、`settings`、`features`。|
| `POST /api/commands` | JSON：`requestId?`、`sceneId`、`baseRevision`、`method`、`params` | 执行编辑器/仿真命令，成功 `{requestId,data,revision}`；场景被 Agent 占用或版本冲突返回 409（`SCENE_BUSY`/`REVISION_CONFLICT`），校验错误 400。`method` 包括 `scene.create_primitive(s)`、`scene.create_camera`、`scene.create_light`、`scene.load_asset`、`scene.update_entity`、`scene.set_parent`、`scene.delete_entity`、`robot.command_joint_positions`、`backend.apply`、`editor.undo/redo`、`simulation.start/pause/step/reset`、`state.save/restore`。|
| `GET /api/assets` | 无 | `{assets:[AssetSummary...]}`，包含内置、项目和上传资产。|
| `GET /api/assets/{asset_id}/file[/{path}]` | 路径参数 | 返回资产文件流；未知资产 404。|
| `POST /api/assets/upload` | `multipart/form-data`：单文件 `file`，或 bundle 的 `entry`、重复 `path` 与 `file/files` | 201：`{asset: AssetSummary}`。|
| `POST /api/assets/import-remote` | JSON `{url}` | 拉取远程资源并注册；201 返回资产摘要。|
| `DELETE /api/assets/{asset_id}` | 路径参数 | `{deleted: asset_id}`；活动场景引用时拒绝。|
| `POST /api/state/load-simstate` | multipart 文件字段 `file` | 恢复 `.simstate`，返回 `{revision}` 并广播 `scene.changed`。|
| `GET /api/sensors/{entity_id}/frame?mode=rgb|depth` | 相机实体 ID；默认 `rgb` | `image/png`；深度为灰度图。相机不可用 503 `SENSOR_UNAVAILABLE`。|

### 1.2 浏览器事件

`GET /events` 升级 WebSocket。连接后立即收到 `{type:"connected",revision}`；客户端文本 `ping` 得到 `pong`。服务端广播 `scene.changed`、`simulation.tick`、`agent.turn_updated`、`agent.job_updated`、`presentation.snapshot_published`、`presentation.timeline_published` 等事件，事件负载为 JSON 对象。

### 1.3 Agent 作业与会话（Gateway 代理面）

| 方法与路径 | 请求/查询 | 响应 |
|---|---|---|
| `POST /api/agent/jobs` | JSON：`text`（必填）、`requestId?`、`sceneId?` | 202，返回作业对象（`id/sceneId/text/status/createdAt/message`，完成后追加 `conversationId/turnId/revision`，可有 `phase/debugEvents/errorCode`）。作业会异步调用 Agent Service；`agent.conversation` 关闭时 404。|
| `GET /api/agent/jobs` | 无 | `{jobs:[...]}`。|
| `GET /api/agent/jobs/{job_id}` | 路径参数 | 作业状态；不存在 404。|
| `POST /api/agent/jobs/{job_id}/cancel` | 无 | 202 取消；终态作业原样返回。|
| `GET /api/agent/conversations` | `agent.history` 特性开启 | `{activeConversationId, conversations:[summary]}`。|
| `POST /api/agent/conversations` | 无 | 重置/绑定当前场景并创建会话，201 返回会话详情、快照和 presentation。已有 Agent 作业或场景写锁期间 409。|
| `GET /api/agent/conversations/{conversation_id}` | 路径参数 | 持久化消息、turns、debugEvents、presentation；无 checkpoint 404。|
| `DELETE /api/agent/conversations/{conversation_id}` | 路径参数 | `{deleted: conversation_id}`；活动会话或 Agent 执行中 409。|
| `POST /api/agent/conversations/{conversation_id}/activate` | 路径参数 | 恢复 checkpoint 并激活，返回会话详情；缺失 404，Agent 作业或场景写锁期间 409。|
| `GET /api/diagnostics` | 无 | `{logs:[...]}`，有界操作日志。|
| `GET /api/metrics` | 无 | 进程内计数器对象（Agent 成功/失败/延迟、WebSocket、capture 字节数等）。|

### 1.4 Presentation 与 Agent 控制回调

控制回调必须同时带 `X-Agent-Control-Token` 和本次 turn 的 `X-Agent-Job-Id`；缺失/错误为 403。时间线帧必须绑定当前 `sceneId/worldId/sceneGeneration`，`simulationTime` 有限且严格递增。

| 方法与路径 | 请求 | 响应/限制 |
|---|---|---|
| `GET /api/presentation` | 无 | 当前 `{mode,presentationRevision,snapshot,timeline,detached}`。|
| `POST /api/presentation/mode` | `{mode:"real-sync"|"sync-and-animate",basePresentationRevision}` | 更新模式；版本冲突 409。动画模式受 `presentation.syncAndAnimate` 特性控制。|
| `POST /api/agent-control/presentation/snapshot` | 空 JSON | 从权威场景发布静态快照。|
| `POST /api/agent-control/presentation/timeline` | `{frames:[SceneSnapshot...], baseSnapshot?}` | 原子发布完整时间线；至少 2 帧、严格递增时间、受 `SIM_WEB_TIMELINE_MAX_BYTES` 限制。|
| `POST /api/agent-control/assets/resolve` | `{asset}` | `{assetId,path}`；不唯一/无文件 404 `ASSET_NOT_FOUND`。|
| `POST /api/agent-control/captures` | 空 JSON | 201 `{captureId,sourceTurnId}`；已有 capture 409 `CAPTURE_BUSY`。|
| `POST /api/agent-control/captures/{capture_id}/frames` | `{snapshot: SceneSnapshot}` | 追加帧，返回 `{captureId,frameCount}`；大小超限 409。|
| `POST /api/agent-control/captures/{capture_id}/finish` | `{publish?:boolean}`（默认 true） | 发布时间线或 `{captureId,published:false}`。|
| `POST /api/agent-control/captures/{capture_id}/abort` | 空 JSON | `{captureId,aborted:true}`。|
| `GET /api/timelines/{timeline_id}/frames/{index}` | 路径参数 | 展开后的 SceneSnapshot；不存在 404；支持压缩。|
| `GET /api/timelines/{timeline_id}/data` | 路径参数 | `{snapshot,frames}` 压缩数据；不存在 404。|

## 2. Agent Service（`agent_service/api.py`，默认 `127.0.0.1:8765`）

所有错误统一包含 `{"detail": message, "error":{"code", "message"}}`。请求体为严格 Pydantic 模型（未知字段拒绝），单请求最大 16 MiB。

| 方法与路径 | 请求字段 | 响应 |
|---|---|---|
| `GET /health` | 无 | `{status:"ok", model:{...}}`；服务在线但模型未配置仍返回 200。|
| `POST /v1/conversations` | `target`（默认 `platform`）、`sceneBinding?`、`presentationMode?` | 201 `ConversationView`。`sceneBinding`：`sceneId,simSessionId,sceneGeneration,connect?`；模式为 `real-sync` 或 `sync-and-animate`。|
| `GET /v1/conversations?target=` | 可选 target | 会话摘要数组。|
| `GET /v1/conversations/{conversation_id}` | 无 | `ConversationView`（会话、消息和 turns）。|
| `DELETE /v1/conversations/{conversation_id}` | 无 | `{deleted:true}`。|
| `POST /v1/conversations/{conversation_id}/turns` | `requestId`、`message`、`controlContext?`、`presentationMode?`、`sceneBinding?`、`debug?` | 202 `TurnView`（初始 queued/running）。`controlContext` 可含 gatewayBaseUrl、controlToken、jobId、sceneId、simSessionId、sceneGeneration、liveEndpoint、presentationMode、robotAssets。|
| `GET /v1/conversations/{conversation_id}/turns` | 无 | `TurnView[]`。|
| `GET /v1/conversations/{conversation_id}/turns/{turn_id}` | 无 | 单个 `TurnView`。|
| `POST /v1/conversations/{conversation_id}/turns/{turn_id}/cancel` | 无 | 取消后的 `TurnView`。|
| `GET /v1/conversations/{conversation_id}/events?after=&limit=` | `after` 默认 0，`limit` 默认 200、上限 1000 | `EventView[]`：`eventId,conversationId,turnId?,type,payload,createdAt`。|

`TurnView` 状态：`queued/running/succeeded/failed/cancelled/interrupted`；字段含 `turnId,conversationId,requestId,status,message,errorCode?,createdAt,finishedAt?,presentationRevision?,affectedObjects[]`。`MessageView` 角色只有 `user/assistant`。可见阶段标签由 `agent_service.schemas.PHASE_LABELS` 白名单限定：scene、understand、plan、compile、execute、publish。

**Agent Service 请求示例：**

```http
POST /v1/conversations
Content-Type: application/json

{"target":"platform","presentationMode":"real-sync",
 "sceneBinding":{"sceneId":"main","simSessionId":"sim-1","sceneGeneration":3,
                  "connect":"tcp://127.0.0.1:9100"}}
```

```json
{"conversationId":"c-123","target":"platform","status":"active",
 "createdAt":"2026-09-24T10:00:00Z","sceneBinding":{"sceneId":"main","simSessionId":"sim-1","sceneGeneration":3,"connect":"tcp://127.0.0.1:9100"},"presentationMode":"real-sync","messages":[],"turns":[]}
```

```http
POST /v1/conversations/c-123/turns
Content-Type: application/json

{"requestId":"req-9","message":"创建一个方块",
 "controlContext":{"gatewayBaseUrl":"http://127.0.0.1:8080","jobId":"job-1","sceneId":"main","simSessionId":"sim-1","sceneGeneration":3},"debug":false}
```

响应为 `202 TurnView`。服务只接受 Gateway 注入的 `controlContext`，客户端不应把控制 token 拼入用户消息；错误响应中的 `detail` 便于旧客户端兼容，`error.code` 用于程序分支。

### 2.1 Skill 平台运行时与碰撞检查

`agent_service` 中的 `move` 等 skill 需要访问仿真平台时，应使用 Gateway 注入的 `controlContext.liveEndpoint` 连接权威 Live 仿真实例。调用链为：

```text
skill -> PlatformCollisionChecker -> SceneRobotRuntime/LiveSceneRuntime
      -> sim_platform.live.RobotProxy -> Live RPC -> 原生 collision backend
```

`LiveSceneRuntime.connect()` 是仅连接入口；在线 skill 不应自行启动替代仿真实例。`MoveSkill` 已通过 `robot_runtime` 接收运行时，并把 `PlatformCollisionChecker` 注入各运动策略和规划器。扩展 skill 时应复用注入的 checker，不要在策略中重新连接平台。

#### 碰撞检查接口

| 接口 | 用途 |
|---|---|
| `checker.check(joints, safety_distance=0.0, allowed_geom_ids=None)` | 检查一个 6 维 UR5e 关节配置；返回 `CollisionResult`。 |
| `checker.check_path(waypoints, resolution=0.05, safety_distance=0.0, allowed_geom_ids=None)` | 对相邻路点按关节角分辨率插值并检查路径，遇到首个阻塞配置即返回。 |
| `checker.planning_scope()` | 规划上下文，启用本次规划的查询缓存；退出时清理规划状态。 |
| `checker.clear_cache()` / `checker.refresh_baseline()` | 场景移动后清缓存；场景组成变化后重建基线并清缓存。 |

`CollisionResult` 包含 `in_collision`、`minimum_distance` 和 `contacts`。接触详情包含 `geom1`、`geom2`、`distance`、`self_collision`。`check_path()` 是离散采样检查；关节限位由请求校验和规划器负责，checker 底层接触查询使用 `limits="ignore"`，所以“无碰撞”不等于“关节合法”。

典型调用如下：

```python
with self.collision_checker.planning_scope():
    result = self.collision_checker.check(
        q_goal,
        safety_distance=0.01,
    )
    if result.in_collision:
        raise ValueError(result.contacts)

    result = self.collision_checker.check_path(
        waypoints,
        resolution=0.05,
        safety_distance=0.01,
    )
    if result.in_collision:
        raise ValueError(result.contacts)
```

#### 底层 RobotProxy 查询

必要时可使用公开的 `sim_platform.live.RobotProxy` 方法：

| 方法 | 用途与限制 |
|---|---|
| `configuration_collides(positions)` | 单个完整配置的原始布尔查询。 |
| `configurations_collide(positions_batch)` | 多个完整配置的批量原始布尔查询；不自动插值，也不应用 skill 的接触豁免。 |
| `configuration_contacts(positions, distance_threshold=0.0, limits=...)` | 查询具体接触 pair、距离和碰撞标志；checker 用它实现安全距离和过滤。 |

原始结果不能直接按 contact 数量判定 skill 是否可执行：机器人相邻 link 的基础自接触可能持续存在。`PlatformCollisionChecker` 会过滤起始姿态已有的机器人自身接触、夹持物与夹爪的允许接触，并应用 `safety_distance`。`allowed_geom_ids` 只应表示有意的目标物体接触，不能跳过机械臂对障碍物的检查。

规划阶段应在候选配置上查询私有 world clone，不要为了探测候选点而修改真实世界。若 skill 直接修改关节或场景对象后再查询，必须先同步后端，并清理 checker 缓存：

```python
runtime.sync_backends()  # 同步 physics 和 collision
self.collision_checker.clear_cache()
```

场景组成变化后还应调用 `refresh_baseline()`。执行阶段应使用既有运动命令和仿真步进管线，并在环境可能变化时重新检查最终状态。collision backend 不可用、RPC 失败或能力不支持时必须传播错误，不能按“无碰撞”继续执行。

skill 不应直接调用私有 `_request()`、C ABI 或 MuJoCo，不应使用 `connect_or_launch()` 创建替代世界，也不应通过 Web Gateway `/api/commands` 做内部碰撞查询。在线碰撞查询走 Live 平台接口；Move 的规划逻辑走已有 checker 和 planner。

## 3. Agent Service Gateway 控制客户端

`agent_service/gateway_client.py::GatewayClient` 使用上述双 Header 调用：`resolve_asset`、`publish_snapshot`、`publish_timeline`、`capture_start/frame/finish/abort`。时间线支持 `baseSnapshot + compact frames`；轨迹点由 `TrajectorySegment` 转为仅 visual 的球标记，不写入物理碰撞。

### 3.1 本地 CLI

`python -m agent_service.cli chat "指令" --connect <live-endpoint> [--conversation ID] [--database PATH] [--debug] [--log-level LEVEL]` 执行一次 turn；`repl` 子命令进入连续输入模式。`--connect` 必填，CLI 只连接已有 Live endpoint，不启动替代仿真实例；退出码 0 表示 succeeded，1 表示 turn 终态失败，2 表示会话参数错误。CLI 使用同一 `ConversationService`，但没有 Gateway presentation 发布和双 Header 控制回调，适合本地验证/排障。

## 4. JSON-RPC/消息 Schema

### 4.1 `schemas/agent-command.schema.json`

对象仅允许 `id`（string/integer/null）、`method`（必填）和 `params`（对象，默认 `{}`）。方法枚举：`scene.query`、`scene.create_entity`、`scene.set_transform`、`scene.load_asset`、`scene.load_robot`、`simulation.step`、`simulation.reset`、`robot.command_joint_positions`、`state.save`、`state.restore`、`shared_memory.allocate/describe/release`、`capabilities.query`。

### 4.2 `schemas/live-control.schema.json`

同样要求 `id`、`method`，禁止额外字段。方法覆盖 session（`hello/ping/shutdown`）、capabilities、scene（query/snapshot/create_entity/load_asset/load_robot/ensure_robot/resolve_object/get_transform/update_transform/set_transform/set_parent/destroy_object）、robot（get_link/command_joint_positions）、simulation（start/pause/status/step/reset）、workspace（get/resolve/bind/unbind）、state（save/restore）。

### 4.3 `agent_service/protocol/commands.py` 命令文档

`CommandDocument` v1 字段：`schema_version:"1.0"`、绝对路径 `robot/scene/registry`、64 位小写十六进制 `scene_fingerprint`、`runtime`、`request_defaults`、至少一项 `commands`。每个 `SkillCommand` 要求 `command_id`、技能名 `locate|move|grasp|release|press|pull|push`、`parameters.target` 非空；runtime 可配置 `execution_mode`（kinematic/actuator）、`playback_fps`、`playback_speed`、`minimum_playback_duration`、`end_effector_site`。

`agent_service/protocol/interactions.py`、`legacy_loader.py` 和 `protocol/README.md` 是规划层的内部协议契约：空间源类型统一归一为 `entity`（旧 `body/site/geom/joint` 仅保留 `legacy_type`），坐标系统一为 `platform_world`（兼容输入 `mujoco_world`）。这些不是 HTTP 路由，但会影响 Agent 生成的 registry/skill 参数；协议错误码扩展包含 `TARGET_DEPTH_NOT_REACHED`、`TARGET_NOT_ACTUATED`、`LOCAL_COLLISION_DETECTED`、`SENSOR_DATA_UNAVAILABLE`、`PUSH_PULL_UNSUPPORTED`、`IK_FAILED`、`MODEL_UNAVAILABLE` 等。

## 5. 运行配置与特性开关

`web/config/features.json` 是 version 1 的布尔特性表，关键接口开关为 `agent.history`、`agent.debug`、`presentation.syncAndAnimate`、`presentation.timeline`、资产上传/远程导入和各编辑器命令开关。Gateway 通过 `SIM_WEB_TIMELINE_MAX_BYTES` 控制时间线请求上限；Agent Service 通过 `AGENT_SERVICE_DB` 指定会话数据库（默认 `.sim-cache/agent-service/conversations.db`）。

## 6. 源码证据与边界

- Web 路由注册：`web/gateway/server.py:1275-1311`；处理器实现同文件 `:422-1270`。
- Agent Service 路由与错误中间件：`agent_service/api.py:39-145`。
- Agent 请求/响应模型：`agent_service/schemas.py:14-110`。
- Gateway Agent 回调客户端：`agent_service/gateway_client.py`。
- 正式 OpenAPI：`web/contracts/openapi.yaml`（未逐项描述 import-remote、state/load-simstate、timeline data 等实现扩展）。
- 两个 JSON Schema 只定义方法名与顶层 envelope；具体 `params` 由各服务实现校验，不能从 schema 推断完整业务字段。
