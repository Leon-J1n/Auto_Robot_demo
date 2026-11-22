# Python 服务与 SDK 接口

本文按 Python 源码当前实现整理，接口入口主要位于 `python/sim_platform` 和 `python/sim_algorithms`。远程控制协议为 TCP 上的 JSON Lines（JSONL），不是 HTTP。参数及返回字段以实现为准；功能声明列表不能代替具体方法的实现。

## 安装、依赖与启动

项目以 Python 3.10+、NumPy 和 `bqlog` 为基础依赖。仓库根目录的 `pyproject.toml` 使用 `python/` 作为包目录，安装开发版本：

```powershell
python -m pip install -e .
```

`sim_platform.sdk` 在导入时加载本机 C API 动态库。可以用 `SIM_PLATFORM_LIBRARY` 指向 `sim_platform_c.dll`、`libsim_platform_c.so` 或 `libsim_platform_c.dylib`；未指定时会搜索包目录及若干构建目录。SDK 要求 C API 版本至少为 9，找不到库或版本过旧时导入会抛 `RuntimeError`。`sim_algorithms._native` 的原生算法封装通过 `SIM_ALGORITHMS_LIBRARY` 指定 `sim_algorithms_c` 动态库。

Agent RPC 服务入口为 `simulator-server` 或 `python -m sim_platform.server`，默认监听 `127.0.0.1:8765`：

```text
simulator-server --host 127.0.0.1 --port 8765 --physics none --collision fcl
```

`--physics` 支持 `none|mujoco|bullet3`，`--collision` 支持 `fcl|bullet3|coal|ifps-cpu|ifps-gpu`；另有 `--cache-directory`、`--instance-name`、`--registry-dir` 和可重复指定的 `--advertise`。端口可指定 0 让系统分配临时端口。服务写实例描述文件供按 PID/名称发现，退出时尝试移除自己的描述文件。

`simulator-python` 提供 `launch` 和 `run`：`launch` 可启动新实例并输出 JSON 连接信息；`run SCRIPT --connect ENDPOINT` 连接已有实例，或 `run SCRIPT --launch-if-missing --name NAME` 按名称启动/连接。运行脚本命名空间注入 `sim`、`load_robot`、`ensure_robot` 和 `require_robot`。本地 Editor 自动化则使用 `EditorSession.launch(...)`，依赖可运行的编辑器进程及其本地控制端口。

## 本地 C API Python SDK (`sim_platform.sdk`)

SDK 是 ctypes 对 C API 的封装，不走 Agent RPC。大多数原生非零状态由 `_check` 转为 `RuntimeError`，消息取自 `sim_last_error()`；Python 层参数检查会先抛 `ValueError`。所有句柄都只在所属 Simulator/World 生命周期及场景代次内有效。

### 数据类型

| 类型 | 字段/用途 |
|---|---|
| `Transform` | `translation=(0,0,0)`、`rotation_wxyz=(1,0,0,0)`、`scale=(1,1,1)`，均为不可变元组。 |
| `JointInfo` | `name/type/axis/lower_limit/upper_limit/entity/parent_link/child_link`；`movable` 对 `type != "fixed"` 为真。 |
| `ContactPair` | `robot_collider/other/distance/colliding`；距离是表面间隔，碰撞时为 0，不代表穿透深度。 |
| `KinematicsBatch` | `transforms[configuration][link]`；可选 `jacobians`，形状为 `(configuration_count, 6, joint_count)`。 |
| `State` | 原生快照句柄 `handle`。 |
| `SceneSnapshot` | `schema_version/scene_id/world_id/scene_generation/revision/simulation_time/status/entities`；`from_dict` 只接受 `schemaVersion == "1.0"` 和对象数组 `entities`。 |
| `Event` | `type/world/entity/simulation_time/detail`。 |
| `EditorEntityInfo` | 编辑器实体的父句柄、标记、名称、本地/世界变换及 visual/collider/rigid_body/sensor/camera/light 元数据。 |

### `Simulator` 与 `World`

`Simulator(cache_directory=None, physics="none", collision="fcl", plugins=None)` 创建运行时；上下文管理器退出时销毁原生运行时。物理选项来自原生后端支持；碰撞后端可选 FCL、Bullet3、Coal、IFPS CPU/GPU 等，具体构建能力取决于 native build/plugins。`api_version()` 返回已加载库版本。

| 调用 | 返回与副作用 |
|---|---|
| `Simulator.create_world(fixed_time_step=0.002, seed=0)` | 新建 World 句柄。 |
| `step(world, count=1)` / `step_unpaced(world, count=1)` | 推进指定 tick；前者受节拍约束，后者尽可能快。返回 `None`。 |
| `step_profiled(world, count=1)` | 推进并返回耗时毫秒数。 |
| `start(world)` / `pause(world)` | 启停连续仿真，返回 `None`。 |
| `status(world)` / `time(world)` | 返回状态字符串或仿真时间秒数。 |
| `poll_event(world)` | 返回 `Event` 或无事件时 `None`。 |
| `physics_contacts(world)` | 返回接触记录字典元组。 |
| `scene_snapshot(world)` / `scene_transforms(world)` / `scene_metadata(world)` | 返回 `SceneSnapshot`、变换 JSON 字典、`(world_id, revision)`。 |
| `save_state(world)` / `restore_state(world, state)` | 在内存中保存/恢复；恢复会改变场景状态。 |
| `save_state_file(world, path)` / `restore_state_file(world, path)` | 从文件保存/恢复；路径编码为 UTF-8。恢复后先前实体句柄可能失效。 |
| `reset(world)` | 重置仿真世界。 |
| `sync_collision(world)` / `sync_physics(world)` | 将后端缓存与当前场景/物理状态同步。直接改关节后做碰撞查询时应先同步。 |
| `command_joint_positions_batch(world, robots, positions)` | 对机器人批量下发关节目标；宽度/数量须与机器人及关节数匹配，否则底层失败为 `RuntimeError`。 |

`World` 的场景/碰撞/传感器方法：`load_robot(path) -> Robot`、`load_asset(path) -> Entity`、`create_entity(name, parent=None) -> Entity`、`create_primitive(name, shape, size, *, parent=None) -> Entity`、`create_light(name="Point Light", type="point", *, parent=None, color=(1,1,1), intensity=1, range=10, decay=2, inner_cone_radians=.35, outer_cone_radians=.7, angular_diameter_radians=0, enabled=True) -> Entity`、`entity(handle) -> Entity`、`find_entity(object_id) -> Entity`、`editor_entities() -> tuple[EditorEntityInfo,...]`、`check_collision(first, second) -> bool`、`distance(first, second) -> float`、`scene_collisions(entities) -> (碰撞布尔矩阵, 最大距离)`、`set_transforms(entities, transforms) -> None`、create_camera(parent, *, frame_id="camera", update_rate_hz=30, width=640, height=480, vertical_fov_radians=1.0471975512, near_plane=.01, far_plane=1000) -> Camera`。Primitive shape 和尺寸要求由 C API 校验；实体/父对象必须属于对应 World。

### `Entity`、`Robot` 与传感器

`Entity` 提供 `handle`、只读 `object_id`、可读写 `transform`，以及 `destroy()`、`set_parent(parent, preserve_world=True)`、`set_name(name)`、`set_visual(visible=True,min_lod=0,max_lod=7)`、`set_light(...)`、`set_rigid_body(enabled=True,dynamic=False,gravity=True,mass=1,friction=(1,.005,.0001),restitution=0)`、`apply_wrench(force, torque=(0,0,0))`。变换写入会修改场景；名称去空白后不得为空；LOD 必须满足 `0 <= min_lod <= max_lod <= 7`；摩擦是三个非负数，质量为正，恢复系数在 `[0,1]`。IFPS 的 `ifps_sphere_count` 属性允许整数 0 到 1,000,000，0 选择后端默认值。

`Robot(Entity)` 另有：`joint_count`、`joint_names`、`links -> ((name, Entity),...)`、`get_link(name) -> Entity`、`joint_state -> (positions, velocities, efforts)`、`joint_infos`、`joint_limits`；`command_joint_positions(targets, *, kp=None, kd=None, effort_limits=None)` 下发位置控制；`command_joint_efforts(targets)` 下发力矩；`set_joint_effort_limits(limits)` 设置力矩上限；`set_joint_positions(positions, *, limits="enforce")` 直接设置关节状态，limits 可为 `enforce|clamp|ignore`；`set_joint_continuous(joint_name, enabled=True)` 改连续关节配置；`command_joint_velocities(targets)` 下发速度目标；`configuration_collides(positions) -> bool`、`configurations_collide(positions) -> ndarray` 查询一组配置；`kinematics(positions, links, *, jacobian_link=None) -> KinematicsBatch` 计算指定链接位姿，可选一个链接的雅可比；`configuration_contacts(positions, *, max_distance=0.0, include_non_colliding=False) -> tuple[ContactPair,...]` 查询机器人配置的接触对。关节向量长度必须对应机器人 DOF；错误长度/无效限位策略会抛 `ValueError` 或原生 `RuntimeError`。

`Camera.configure(frame_id="camera", update_rate_hz=30, width=640, height=480, vertical_fov_radians=..., near_plane=.01, far_plane=1000, rgbd_output=True)` 改配置；`dimensions -> (width,height)`、`intrinsics -> ndarray`、`rgb -> ndarray`、`depth -> ndarray` 读取相机数据。读取/配置失败由 C API 错误转为 `RuntimeError`。

## 远程 Live SDK (`sim_platform.live`)

`connect(endpoint, *, workspace="default", timeout=10.0, registry_directory=None) -> LiveSession` 接受 `Endpoint`、正整数 PID、`pid:<pid>`、`host:port`、`tcp://host:port` 或解析器允许的 TLS URI。当前发现逻辑从实例描述符选择 TCP endpoint；不能据此认为 TLS 传输已实现。`connect_named(name, ...)` 按名称发现；`list_instances(registry_directory=None)` 返回存活实例描述字典元组。`launch(spec, *, workspace="default")` 启动实例并等待连接，`connect_or_launch(name, spec, *, workspace="default")` 先连接同名实例、不存在时启动。连接/发现/协议错误为 `LiveControlError(code, message)`，其 `.code` 保存稳定错误码。`LiveSession.close()` 关闭连接；上下文管理器可自动关闭，`shutdown()` 请求服务退出。

`LaunchSpec` 字段：`kind="headless"`、`instance_name=None`、`listen=()`、`advertise=()`、`detached=True`、`executable=None`、`physics="none"`、`collision="fcl"`、`cache_directory=None`、`registry_directory=None`、`startup_timeout=15.0`、`environment={}`。服务端 CLI 只接受 headless 仿真参数；editor 启动由 launcher 路径支持时使用。启动过程错误包括超时、子进程退出和无法发现 endpoint。

`LiveSession` 把以下操作映射到 Agent RPC，成功时返回 Python 代理、标量/数组或字典：`load_robot(path, alias=None) -> RobotProxy`、`load_asset(path, alias=None) -> EntityProxy`、`create_entity(name="entity", parent=None, alias=None)`、`create_primitive(kind, name=None, size=None, alias=None)`、`create_camera(parent=None, ...)`、`create_light(type="point", ...)`、`ensure_robot(path, alias)`、`require_robot(alias)`、`find_entity(object_id)`、`command_joint_positions_batch(robots, positions)`；仿真控制 `start/pause/step(count=1)/step_unpaced(count=1)/step_profiled(count=1)/reset/status/poll_event/physics_contacts`；同步 `sync_physics/sync_collision`；状态 `save_state/restore_state/save_state_file/restore_state_file`；后端 `backend_status/apply_backends(physics=None,collision=None)`；场景 `scene_snapshot/scene_transforms/set_transforms`；碰撞 `check_collision/collision_distance/scene_collisions`。`step` 返回新仿真时间，`step_profiled` 返回 `(time, elapsed_ms)`，`status` 返回 `(status,time)`。参数/后端不支持或对象过期时抛 `LiveControlError`。

`EntityProxy` 暴露 `object_id`、`handle`、`transform`，及 `set_position/set_rotation/set_scale/set_name/set_visual`、`ifps_sphere_count` 属性、`apply_wrench`、`set_rigid_body`、`set_parent(parent,preserve_world=False)`、`destroy()`。`RobotProxy` 扩展 `joint_names/joint_count/joint_state/joint_limits`、`get_link/list_links`、`command_joint_positions(targets,...)`、`set_joint_positions(positions,...)`、`execute_joint_segment(start,target,...)`、`set_joint_continuous`、`command_joint_velocities`、配置碰撞、运动学及配置接触查询。`CameraProxy` 有 `configure/dimensions/intrinsics/rgb/depth`，图像通过共享内存传输并转为 NumPy 数组；`LidarProxy.get_points()` 返回点和距离数组。代理引用采用 `ObjectRef`（`object_id/world_id/scene_generation/runtime_handle/kind/name`）；恢复状态或重建场景后旧引用可能因 generation/handle 不匹配而失效。当前 `HeadlessService._invoke` 没有 `scene.create_light` 分支，尽管 `LiveSession.create_light` 会发出该请求；对 headless Agent 服务调用它会得到 `method_not_found`。

`ObjectRegistry` 是按 workspace 隔离的别名操作门面：`get(alias) -> proxy | None`，`require(alias, kind=None) -> proxy`（不存在或种类不符抛 `LiveControlError`），`bind(alias, proxy) -> None`，`unbind(alias) -> bool`。`ensure_robot` 在别名绑定路径相同机器人时复用，否则发生冲突。

## Editor Python 适配器 (`sim_platform.editor`)

`EditorSession.launch(editor, *, startup_timeout=15.0)` 启动指定编辑器可执行文件并轮询其本地控制端口；路径不存在抛 `FileNotFoundError`，进程启动失败抛 `RuntimeError`，超时抛 `TimeoutError`。它返回面向编辑器专用 RPC 的会话。`EditorRpcError` 表示远端拒绝或传输/协议失败。会话提供 `is_open`、`close()` 和上下文管理器。

编辑器场景方法：`load_robot(path) -> EditorRobot`、`load_asset(path) -> int`、`import_remote_asset(url) -> int`、`scene_hierarchy() -> tuple[dict,...]`、`set_sensor_enabled(entity, enabled=True)`、`set_parent(entity,parent)`、`create_primitive(kind) -> int`、`draw_trajectories(...) -> int`、`clear_trajectories(name=None) -> int`、`mirror_pose(...)`、`create_camera(...) -> int`、`save_state(path) -> Path`、`load_state(path) -> int`、`set_backends(physics=None,collision=None)`、`check_collision(first,second) -> bool`、`create_collision_grid_benchmark(...) -> dict`、`profile_step(entities,render=True) -> dict`、`set_transform(entity,...)`、`set_name(entity,name)`、`get_transform(entity) -> dict`、`set_rigid_body(entity,...)`、`start()`、`pause()`；`simulation_time` 和 `performance` 分别提供仿真时间与性能二元组。此适配器方法对应 editor RPC，不等同于 headless `server.py` 的 Agent RPC，能力以编辑器端处理器为准。

`EditorRobot` 包装编辑器机器人句柄：`joint_count`、`get_link(name) -> int`、`joint_state -> (positions, velocities, efforts)`、`command_joint_velocities(targets)`、`command_joint_positions(targets, ...)`、`set_joint_continuous(joint_name, enabled=True)`、`set_joint_positions(positions)`。RPC 失败抛 `EditorRpcError`；参数数组由 Python 侧转换成 JSON 数组。

## Agent RPC 传输与错误约定

TCP 每行一个 UTF-8 JSON 对象，每个请求一行响应。请求通用形状为 `{"id": 1, "method": "session.hello", "params": {...}}`；响应成功为 `{"id": 1, "ok": true, "result": {...}}`，失败为 `{"id": 1, "ok": false, "error": {"code": "...", "message": "..."}}`。ID 原样回显。首个会话请求应为 `session.hello`，提供 `protocol_version: 1` 和可选 `workspace`（默认 `default`，1 至 128 字符）；不匹配返回 `protocol_mismatch`。`LiveSession` 自动握手、串行分配请求 ID 并刷新代理引用。传入的 method 非字符串或 params 非对象返回 `invalid_request`；未实现方法返回 `method_not_found`；`AgentRpcError` 维持其错误码；未预期 Python/C API 异常统一为 `simulation_error`。服务会记录请求/响应，调用方不应将敏感数据放入参数。

### 已实现 RPC 方法

以下方法来自 `HeadlessService._invoke`，是服务端实现清单，参数中列出的 `object` 通常是 `ObjectRef`，机器人/实体引用也有少量兼容 raw handle 路径。字段未列出时服务端通常使用源码中所示默认值。

| 分类 / 方法 | 参数与结果 / 副作用 |
|---|---|
| 会话 `session.hello` | `protocol_version`, `workspace`；回协议、进程/实例信息、revision/generation、endpoints、功能标签。完成工作区握手。 |
| `session.ping` | 无；返回 wall-clock `time` 与 `instance_id`。 |
| `session.shutdown` | 无；远程触发关闭线程，只有服务配置了 shutdown callback 才可用，否则 `unsupported_operation`。 |
| 服务发现 `capabilities.query` | 无；返回协议、实例、共享内存信息和方法名列表。该静态列表是声明信息，未必覆盖 `_invoke` 中所有分支（例如部分 workspace 辅助方法），也不保证每个声明方法都在特定构建后端可用。 |
| 场景读 `scene.query` | 无；返回实体句柄、translation、kind 及可用 ObjectRef。 |
| `scene.snapshot` / `scene.transforms` | 无；返回快照包装对象或变换字典。 |
| 场景建 `scene.create_entity` | `name="entity"`, `parent`, `alias`；新实体和 ObjectRef，可选父实体并绑定 workspace alias。 |
| `scene.create_primitive` | `kind`（cube/box/sphere/plane/capsule/cylinder）、`name`, `size` 三元组、`ifps_sphere_count`, `alias`；创建带 visual/collider 的实体。尺寸必须为正。 |
| `scene.load_asset` / `scene.load_robot` | `path`, 可选 `alias`；分别载入实体资源或机器人，后者返回机器人句柄、关节数及 ObjectRef。路径无效/资源加载失败转错误响应。 |
| `scene.ensure_robot` | `path`, 必需非空 `alias`；若 workspace 别名已指向同路径机器人则 `{created:false}`，否则加载并绑定，冲突报 `alias_conflict`。 |
| `scene.set_transform` | `object`/entity 引用，`translation[3]`、`rotation_wxyz[4]`、`scale[3]`；写入完整变换。 |
| `scene.get_transform` | `object`；返回三个变换向量。 |
| `scene.update_transform` | `object` 及可选变换字段；未提供字段保留当前值，返回新 revision。 |
| `scene.set_transforms` | `items` 对象数组，每项含 `object` 和可选变换字段；批量写入，返回 count。 |
| `scene.set_rigid_body` | `object`, `enabled=true`, `dynamic=false`, `gravity=true`, `mass=1`, `friction=[1,.005,.0001]`, `restitution=0`；质量正数、摩擦非负、restitution `[0,1]`，返回所用 physics 后端。 |
| `scene.set_name` / `scene.set_visual` | `object`,`name`；或 `object`,`visible=true`,`min_lod=0`,`max_lod=7`；修改实体并增加场景 revision。 |
| `scene.get_ifps_sphere_count` / `scene.set_ifps_sphere_count` | `object`；setter 另需 `count`，返回 count。 |
| `scene.apply_wrench` | `object`,`force[3]`,`torque[3]`，默认零；对刚体施力/力矩。 |
| `scene.set_parent` | `object`,`parent`（ObjectRef 或 null）,`preserve_world=false`；重设层级，返回 revision。 |
| `scene.find_object` / `scene.resolve_object` | 前者 `object_id`，后者 `object`；返回经过校验的当前 ObjectRef。不存在、跨 world、陈旧 generation/handle 分别产生对象错误。 |
| `scene.destroy_object` | `object`；销毁实体并清理对象注册，返回 object_id、destroyed、revision。 |
| 传感器 `scene.create_camera` | `parent` 可空，`frame_id="camera"`,`update_rate_hz=30`,`width=640`,`height=480`,`vertical_fov_radians=1.0471975512`,`near_plane=.01`,`far_plane=1000`；创建并返回 ObjectRef。 |
| `scene.create_light` | `HeadlessService._invoke` 当前未实现；Live SDK 虽有 `create_light` 方法，但服务端会返回 `method_not_found`。 |
|  |
| `camera.configure` | `object` 和配置字段（frame_id、更新率、尺寸、FOV、裁剪面、`rgbd_output`）；修改相机。 |
| `camera.describe` | `object`；返回相机尺寸和内参描述。 |
| `camera.frame` | `object`,`mode`（默认 `rgb`；值为 `rgb` 时 RGB，其余值读取 depth）；返回 dtype、shape 及 base64 编码的原始数组字节。 |
|  |
| 机器人 `robot.get_link` | `robot`,`name`；精确链接名查找，返回链接 ObjectRef。 |
| `robot.joint_state` / `robot.joint_limits` | `robot`；返回关节位置/速度/力矩，或 limits 数据。 |
| `robot.command_joint_positions` | `robot`,`targets`，可选位置控制调参、effort limits；返回机器人句柄及采用的调参。 |
| `robot.set_joint_positions` | `robot`,`positions`,`limits="enforce"`；直接改关节状态。 |
| `robot.command_joint_velocities` | `robot`,`targets`；下发速度控制。 |
| `robot.set_joint_continuous` | `robot`,`joint`,`enabled=true`；设置连续关节标志。 |
| `robot.execute_joint_segment` | `robot`,`start`,`target` 及时间/控制参数；按段执行关节轨迹，结果含执行状态/时间信息。 |
| `robot.command_joint_positions_batch` | 机器人列表与批量 positions；一次提交多个机器人目标。 |
| `robot.list_links` | `robot`；返回名称和链接对象引用列表。 |
| `robot.configuration_collides` / `robot.configurations_collide` | `robot`,`positions`（单配置或二维配置数组）；返回单个或批量碰撞布尔结果。 |
| `robot.kinematics` | `robot`,`positions`,`links`，可选 Jacobian 链接；返回分批变换及可选雅可比数组/共享内存数据。 |
| `robot.configuration_contacts` | `robot`,`positions`,`max_distance`,`include_non_colliding`；返回 ContactPair 字段列表。 |
| 碰撞 `collision.check` / `collision.distance` | `first`,`second` 对象引用；返回碰撞布尔值或距离。 |
| `collision.scene` | `objects` 对象引用列表；返回成对碰撞结果及距离数据。 |
| 仿真 `simulation.step` | `count=1`,`paced=true`；count 必须正数，paced false 走 unpaced；返回新 time 并更新场景 revision。 |
| `simulation.step_profiled` | `count=1`（正数）；返回 time 和 elapsed_ms。 |
| `simulation.start` / `simulation.pause` / `simulation.status` | 无；分别启停或返回状态与时间。 |
| `simulation.reset` | 无；重置世界并刷新场景/注册状态。 |
| `simulation.poll_event` / `simulation.physics_contacts` | 无；返回一项事件（可为空）或接触数组。 |
| `simulation.sync_physics` / `simulation.sync_collision` | 无；更新指定后端缓存。 |
| 后端 `backend.status` | 无；返回当前 physics/collision 后端。 |
| `backend.apply` | `physics` 和/或 `collision`；切换运行时后端，依赖构建时插件；失败保留错误响应，成功刷新场景 generation/对象注册。 |
| 工作区别名 `workspace.resolve` / `workspace.get` | `alias`；查当前 hello workspace 中的别名，返回对象引用或空值。 |
| `workspace.bind` | `alias`,`object`；绑定别名到实体，受重名/对象种类校验。 |
| `workspace.unbind` | `alias`；移除绑定，返回是否移除。 |
| 状态 `state.save` / `state.restore` | save 无参返回进程内 state handle；restore `state` 句柄恢复，返回时间、generation 和当前对象映射。 |
| `state.save_file` / `state.restore_file` | `path`；写入/读取状态文件。恢复文件会提升 generation、重建对象注册并使旧引用失效。 |
| 共享内存 `shared_memory.allocate` | `size`，允许 1 至 1 GiB；创建服务端命名共享内存，返回 `name/size/offset=0`。 |
| `shared_memory.describe` | `name`；只描述当前服务持有的 buffer。 |
| `shared_memory.release` | `name`；关闭并 unlink buffer。无效名称返回 `invalid_handle`。 |

错误码还包括 `invalid_argument`、`invalid_handle`、`object_not_found`、`stale_object`、`alias_conflict`、`unsupported_operation`、`protocol_mismatch` 和统一的 `simulation_error`。每个失败仍返回匹配请求 ID 的 JSONL 响应，连接本身通常保持可用。

## Python 算法包 (`sim_algorithms`)

`registry.register(name, function)` 注册可调用对象；空名或重复名抛 `ValueError`。`get(name)` 返回函数，不存在抛 `KeyError`；`call(name,*args,**kwargs)` 原样转发参数并返回算法结果；`names()` 返回排序后的名称元组。`sim_algorithms.__init__` 导入 Python 与 native 示例模块并提供注册入口；注册行为以包当前 `__init__.py` 为准。

包导入时自动注册五个示例名称：`testHelloworld`、`testPrintInt`、`testPrintPy`、`testCReg`、`testPyReg`。示例 Python 函数 `testPrintPy(value: str) -> None` 要求字符串，否则 `TypeError`，通过 `sim_platform` 日志记录内容；`testPyReg() -> None` 记录注册探针。Native 封装 `testHelloworld() -> None`、`testPrintInt(num: int) -> None`、`testCReg() -> None` 调用 `sim_algorithms_c`，非零状态由 native last-error 转为 `RuntimeError`。对应库缺失时模块导入失败（`FileNotFoundError`）。私有 `_bridge` 子进程入口支持 `testPrintPy b64:<base64 UTF-8>` 与 `testPyReg` 两种调用；参数不支持返回退出码 2 并记录诊断。此 bridge 命令格式不是公共 HTTP/RPC 协议。

## 简短用法

```python
from sim_platform import LaunchSpec, connect_or_launch

spec = LaunchSpec(physics="none", collision="fcl")
with connect_or_launch("demo", spec) as sim:
    robot = sim.ensure_robot("assets/robot.urdf", alias="arm")
    print(robot.joint_names)
    sim.step(10)
    print(sim.status())
```

若只使用本地 C API SDK，应设置正确 native library 后使用 `with Simulator(...) as sim:`，再 `world = sim.create_world()`。远程和本地 SDK 的对象类型、句柄及错误路径不同，不要混用 `Entity` 与 `EntityProxy`。
