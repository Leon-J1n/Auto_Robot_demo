# 运行中 Python 控制设计

状态：首版已实现并验证；本文同时记录当前边界与后续增强项。

当前实现基线：

- Windows、Linux 和 macOS 使用同一套标准库 TCP/PID descriptor 代码；Windows 已完成构建和端到端验证，Linux/macOS 需要在对应 CI/机器上补跑验收。
- 一个实例当前只创建一个 TCP listener。未显式指定时默认监听 `tcp://127.0.0.1:0`，由操作系统分配空闲端口。
- `pid:<PID>` 是本机发现格式，不是传输协议；客户端读取实例描述文件后连接其中公布的 TCP endpoint。
- Named Pipe、Unix Domain Socket、多 listener、token 和 TLS 尚未实现，作为后续安全与本机传输优化。

## 1. 背景与结论

目标是在仿真实例已经启动后，允许新的 Python 进程连接当前实例、加载资产、修改场景并继续控制之前创建的对象。使用体验应接近 Blender 的多次脚本执行，但第一阶段不把 CPython 嵌入 `sim_core`，也不把任意 Python 源码发送到仿真服务端执行。

第一阶段采用以下边界：

```text
新的 Python 进程
  simulator-python / sim_platform.live
              |
              | Live Control Protocol
              | PID discovery 或 TCP
              v
运行中的 sim_editor / headless host
              |
              | owner-thread command mailbox
              v
           sim_core World
```

Python 代码在客户端进程执行。场景对象由仿真实例持有，Python 中的 `Robot`、`Link`、`Entity` 等只是远程代理。这样可以实现运行中增量控制，同时避免 Python 崩溃影响仿真进程，也避免默认开放远程任意代码执行。

## 2. 设计目标

必须支持：

1. Windows、Linux 和 macOS。
2. 仿真实例启动后再连接，不要求由 Python 启动实例。
3. 使用 `pid:<PID>` 连接本机实例。
4. 使用 `tcp://<host>:<port>` 或 `<host>:<port>` 连接本机或远程实例。
5. TCP 服务可显式监听任意网卡，不硬编码为 loopback。
6. 本机 IPC 不硬编码为仅当前 Windows 用户访问，访问范围由部署配置决定。
7. 第二次执行通常是一个新的 Python 进程。
8. 第一次执行创建的机器人、夹爪和其他实体在第二次执行时仍可查找和操作。
9. 场景写操作与仿真 tick 串行化，不能从网络线程直接并发修改 `World`。
10. GUI 和 headless host 使用同一套协议语义和 Python 代理 API。
11. Agent 每次执行都可以从服务端显式解析对象，不依赖上一次 Python 进程的变量状态。
12. 没有可用实例时，可以显式启动 GUI 或 headless 实例，并选择固定端口或由操作系统分配端口。

第一阶段不包含：

- 在 `sim_core` 内嵌 CPython；
- 通过 PID 注入 DLL、共享库或 Python 解释器；
- 跨进程保留任意 Python 对象、模块状态或线程；
- 对正在运行的 Python 代码进行强制中断；
- 通过公共 TCP 端点提供 `python.exec`。

## 3. 用户工作流

### 3.1 启动可连接实例

没有已启动实例时，推荐由公共 launcher 创建空场景实例。动态端口最适合本机 Agent：

```powershell
simulator-python launch --kind headless --name assembly-demo --listen tcp://127.0.0.1:0 --detach
```

命令输出 JSON，其中 `pid_endpoint` 可交给后续 Python 进程，`endpoints` 包含实际分配的端口。要启动 GUI，将 `headless` 改为 `editor`。要固定端口并允许远程客户端连接：

```powershell
simulator-python launch --kind headless --name assembly-demo `
  --listen tcp://0.0.0.0:8765 `
  --advertise tcp://sim-host.example.com:8765 `
  --detach
```

TCP 监听地址由用户显式配置，可以是 `127.0.0.1`、局域网地址、`0.0.0.0` 或 IPv6 地址。`0.0.0.0` 只用于 bind，远程客户端需要使用主机实际 IP/DNS；`--advertise` 用来把该可连接地址写入实例描述文件。当前协议没有认证或 TLS，暴露到非可信网络前必须使用防火墙、隔离网络或受控代理。

### 3.2 第一次执行

```python
robot = ensure_robot(alias="robot", path=asset_path)
robot.set_position((1.0, 2.0, 0.0))
```

```powershell
simulator-python run first.py --connect pid:18432
```

`ensure_robot` 原子地查找 alias；不存在时加载机器人并绑定 alias，存在时返回已有对象。这样 Agent 重试同一段代码时不会重复加载机器人。

### 3.3 第二次执行，新 Python 进程

```python
robot = require_robot("robot")
gripper = ensure_robot(alias="gripper", path=gripper_path)
gripper.set_parent(robot.get_link("Link_6"), preserve_world=False)
gripper.set_rotation((1.0, 0.0, 0.0, 0.0))
```

```powershell
simulator-python run second.py --connect pid:18432
```

新进程通过服务端 alias registry 重新解析 `robot`。Python 变量只属于当前进程，不参与协议持久化。

Python SDK 采用同样的显式接口：

```python
from sim_platform.live import connect

with connect("tcp://192.168.1.20:8765", workspace="default") as sim:
    robot = sim.objects.require("robot", kind="robot")
    gripper = sim.ensure_robot(gripper_path, alias="gripper")
    gripper.set_parent(robot.get_link("Link_6"), preserve_world=False)
```

交互控制台以后可以提供自动恢复代理变量的便利模式，但该模式不属于 Agent 的稳定接口，也不是第一阶段验收条件。

## 4. 连接标识

### 4.1 规范格式

| 输入 | 含义 |
|---|---|
| `pid:18432` | 通过本机实例发现记录连接 PID 18432 |
| `tcp://127.0.0.1:8765` | TCP 明文连接 |
| `tcp://192.168.1.20:8765` | 远程 TCP 明文连接 |
| `tcp://[2001:db8::10]:8765` | IPv6 TCP 连接 |
| `host.example.com:8765` | `tcp://host.example.com:8765` 的简写 |
| `tls://host.example.com:8765` | 预留的 TLS 连接格式 |

只提供端口号，例如 `8765`，存在目标主机歧义，因此不作为规范持久格式。CLI 可以把它解释为 `tcp://127.0.0.1:8765`，但日志、配置和发现记录必须保存完整 URI。

### 4.2 PID 不是传输协议

PID 只用于识别本机进程，不能直接传输请求。`pid:<PID>` 的解析过程如下：

```text
pid:18432
    |
    | 读取 18432.json
    v
InstanceDescriptor
    instance_id + PID + endpoints
    |
    | 选择当前客户端支持且可连接的端点
    v
TCP endpoint
```

禁止使用 PID 做进程注入或直接读写目标进程内存。

## 5. 跨平台实例发现

每个启用控制能力的实例原子写入一个 JSON 描述文件：

```json
{
  "protocol_version": 1,
  "pid": 18432,
  "process_start_time": "2026-08-04T15:21:09.412Z",
  "instance_id": "019fce00-5db8-7de0-a101-6dd90ddc21a4",
  "instance_name": "assembly-demo",
  "endpoints": [
    "pipe://RobotSimPlatform/019fce00-5db8-7de0-a101-6dd90ddc21a4",
    "tcp://192.168.1.20:8765"
  ]
}
```

发现目录按以下顺序搜索：

1. `SIM_CONTROL_REGISTRY_DIR` 指定的目录；
2. 平台用户运行时目录；
3. 管理员配置的系统共享发现目录。

平台用户运行时目录使用系统 API 或约定：

| 平台 | 默认位置 |
|---|---|
| Windows | `%LOCALAPPDATA%\RobotSimPlatform\instances` |
| Linux | `$XDG_RUNTIME_DIR/robot-sim-platform/instances`，缺失时回退到用户临时目录 |
| macOS | `$TMPDIR/robot-sim-platform-$UID/instances` |

跨用户发现不依赖这些用户目录。部署方通过 `SIM_CONTROL_REGISTRY_DIR` 指向一个共享目录，并配置该目录的 ACL、owner/group 和写权限。服务端还应支持显式 `--registry-dir`。

当前实现解析记录时校验：

- PID 当前存在；
- `instance_id` 与连接握手返回值一致；
- 协议主版本兼容。

进程启动时间校验用于彻底规避 PID 复用，保留为后续增强项；当前通过存活 PID、随机 `instance_id` 和握手匹配避免连接到错误实例。

实例正常退出时删除记录。异常退出留下的记录由客户端校验后忽略，并可由后续实例或清理命令移除。

## 6. 本机与网络传输

### 6.1 本机 IPC

目标传输：

| 平台 | 实现 |
|---|---|
| Windows | Named Pipe；Qt host 使用 `QLocalServer`/`QLocalSocket` |
| Linux | Unix Domain Socket |
| macOS | Unix Domain Socket |

本机端点的访问策略可配置为 `owner`、`group` 或 `all`，并允许指定 Windows ACL、Unix group 或 socket mode。默认值可以保持保守，但协议和实现不能假设客户端一定属于当前 Windows 用户。

首版尚未实现 Named Pipe/Unix Domain Socket。本机 PID 发现和远程地址目前都解析到 TCP；因此访问控制由 bind 地址、主机防火墙和部署网络负责。

### 6.2 TCP

服务端接受显式监听 URI：

```text
--listen tcp://127.0.0.1:8765
--listen tcp://0.0.0.0:8765
--listen tcp://192.168.1.20:8765
--listen tcp://[::]:8765
```

首版一个实例只支持一个 TCP listener，并可通过 `advertise` 发布一个或多个指向该 listener 的可连接地址。PID 发现从描述文件中选择 TCP 端点。多 listener 以及本机 IPC 优先、TCP 回退属于后续增强。

第一阶段沿用 UTF-8、逐行 JSON 请求/响应，避免同时更换传输和协议。单条消息设置尺寸上限，二进制帧和大数组继续使用共享内存或后续的二进制流协议。

### 6.3 实例不存在时的启动策略

客户端提供三种明确模式，不能把它们混成一个有隐式副作用的 `connect()`：

| 模式 | 行为 |
|---|---|
| `connect(endpoint)` | 只连接；目标不存在时返回错误，绝不启动进程 |
| `launch(spec)` | 总是启动一个新实例，等待 ready 后返回连接 |
| `connect_or_launch(name, spec)` | 先按实例名查找；不存在时以 spec 启动 |

显式 `pid:<PID>` 只表示已有本机进程。PID 不存在时返回 `instance_not_found`，不能用另一个 PID 启动替代。显式远程 TCP 地址连接失败时也不能尝试在远程机器启动进程；远程启动属于后续部署编排系统的职责。

Python API：

```python
from sim_platform.live import LaunchSpec, connect_or_launch, launch

# 固定端口，允许远程客户端连接。
sim = launch(LaunchSpec(
    kind="headless",
    instance_name="assembly-demo",
    listen=("tcp://0.0.0.0:8765",),
    detached=True,
))

# 动态端口。操作系统选择空闲端口，sim.endpoints 返回实际值。
sim = launch(LaunchSpec(
    kind="editor",
    instance_name="interactive-demo",
    listen=("tcp://127.0.0.1:0",),
    detached=True,
))
print(sim.endpoints)

# 多个 Agent 并发调用时只允许一个调用者创建该命名实例。
sim = connect_or_launch(
    "assembly-demo",
    LaunchSpec(kind="headless", listen=("tcp://127.0.0.1:0",), detached=True),
)
```

CLI 对应形式：

```text
simulator-python launch --kind headless --name assembly-demo --listen tcp://0.0.0.0:8765 --detach

simulator-python run first.py --launch-if-missing --kind headless --name assembly-demo --listen tcp://0.0.0.0:0

simulator-python run second.py --connect pid:18432
simulator-python run second.py --connect tcp://192.168.1.20:8765
```

`kind` 第一阶段支持：

- `editor`：启动 `sim_editor`，包含 GUI 和 Live Control host；
- `headless`：启动持续运行的 SimulationRunner，不是当前执行一次 step 后退出的 `simulator run`；
- 可通过 `executable` 显式指定二进制路径，未指定时按安装目录和 PATH 查找。

### 6.4 端口选择与发布

端口规则：

- `tcp://host:8765` 请求固定端口 8765；
- `tcp://host:0` 请求操作系统原子分配一个空闲端口；
- 固定端口被占用时返回 `bind_failed`，不能静默换端口；
- 动态端口绑定完成后，ready 响应和 InstanceDescriptor 必须包含实际端口；
- 未提供 `listen` 时使用 `tcp://127.0.0.1:0`；
- 当前一个实例只接受一个 `--listen`，多个 listener 属于后续增强。

监听地址和对外发布地址是两个概念。`0.0.0.0`、`::` 可以用于 bind，但不是远程客户端可连接的地址。需要发布动态远程端口时显式提供 advertise URI：

```text
--listen tcp://0.0.0.0:0
--advertise tcp://sim-host.example.com:0
```

服务端将 advertise URI 中的 `0` 替换为实际绑定端口，再写入 InstanceDescriptor。若没有配置 advertise，描述文件可以保留本机可连接端点，但不能猜测应该向远程客户端公布哪块网卡或哪个 DNS 名称。

### 6.5 启动就绪、生命周期与并发

launcher 创建子进程后不解析 stdout。首版以 PID descriptor 出现且握手成功作为 ready 条件；描述文件只会在 bind 完成后原子写入，因此同时给出实际端口和实例身份：

```json
{
  "status": "ready",
  "pid": 18432,
  "instance_id": "019fce00-5db8-7de0-a101-6dd90ddc21a4",
  "endpoints": ["tcp://127.0.0.1:49152"]
}
```

若进程退出、端口绑定失败或超过 `startup_timeout`，launcher 返回结构化启动错误并清理未完成的发现记录。

生命周期策略：

- `detached=True`：实例独立于启动它的 Python 进程，适合 Agent 多次执行；
- `detached=False`：不创建独立进程组并保留子进程输出，测试通过显式 `session.shutdown()` 正常退出；
- detached 实例通过 GUI 关闭、`session.shutdown()` 或系统服务管理器结束；首版没有单独的 CLI `stop` 子命令；
- client 断线不能默认终止 detached 实例。

`connect_or_launch` 使用 `(registry_dir, instance_name)` 启动锁：先查找、获取锁、再次查找、再启动。这样多个 Agent 同时发现实例不存在时只创建一个实例。锁持有者启动失败后释放锁，其他调用者才可重试。仅靠“先查找再启动”会产生重复实例，禁止这样实现。

## 7. 握手与能力协商

连接建立后的第一个请求必须是 `session.hello`：

```json
{
  "id": 1,
  "method": "session.hello",
  "params": {
    "protocol_version": 1,
    "client": "sim-platform-python/0.1.0",
    "workspace": "default",
    "token": "optional-token"
  }
}
```

响应至少包含：

```json
{
  "id": 1,
  "ok": true,
  "result": {
    "protocol_version": 1,
    "instance_id": "019fce00-5db8-7de0-a101-6dd90ddc21a4",
    "process_start_time": "2026-08-04T15:21:09.412Z",
    "scene_revision": 42,
    "capabilities": [
      "scene.read",
      "scene.write",
      "workspace.aliases",
      "robot.links"
    ]
  }
}
```

客户端不能根据端口或进程名推断能力，所有可选能力通过握手协商。

## 8. 稳定对象身份

当前 `EntityId` 只适合进程内运行时访问。跨进程代理使用 `ObjectRef`：

```json
{
  "instance_id": "019fce00-5db8-7de0-a101-6dd90ddc21a4",
  "world_id": 1,
  "object_id": "019fce08-0e34-7ca2-b442-e64777435e51",
  "runtime_handle": 127,
  "kind": "robot",
  "scene_generation": 3
}
```

设计要求：

- `object_id` 是场景对象的持久 UUID，在对象创建或资产实例化时生成；
- `runtime_handle` 仅作为同一进程内的快速路径，不能单独持久化；
- `scene_generation` 在整场景替换、状态装载等会整体使代理失效的操作后递增；
- 服务端每次使用代理时校验 `instance_id`、`world_id`、`object_id` 和 generation；
- 对象删除后返回 `stale_object`，不能让旧 handle 意外指向新对象；
- `object_id` 进入 `.simstate`，状态保存和恢复后保持稳定。

机器人 Link 同样拥有 `ObjectRef`。`robot.get_link(name)` 在服务端按该机器人 `RobotComponent.links` 的范围查找，不能在整个场景中仅按名称模糊匹配。

## 9. 跨进程对象注册表

### 9.1 注册表保存什么

对象注册表由仿真实例维护，键为 `(instance_id, workspace_name, alias)`。第一阶段只保存：

- 显式 alias 到 `ObjectRef` 的映射；
- 注册表 revision；
- alias 创建者、创建时间等可审计元数据。

注册表不保存 Python 变量或任意 Python 值，也不保存文件句柄、模块、函数、生成器、线程、NumPy 内存视图或 pickle。

### 9.2 Agent 解析流程

每次 Agent 执行都按以下流程显式解析对象：

1. 连接并握手；
2. 使用 alias 或上一次工具结果中的 `object_id` 请求对象；
3. 服务端校验对象仍存在、类型匹配且 generation 有效；
4. 客户端创建新的远程代理；
5. Agent 执行操作并保存需要传给下一次任务的结构化结果。

核心接口：

```python
sim.objects.bind("robot", robot)
robot = sim.objects.require("robot", kind="robot")
sim.objects.unbind("robot")
```

`ensure_robot(path, alias)` 是 `require + load + bind` 的服务端原子操作。若 alias 已存在但对象类型或资产来源不匹配，返回 `alias_conflict`，不能静默复用错误对象。

如果两个客户端并发绑定同一 alias，后提交者收到 `workspace_conflict` 或 `alias_conflict`，重新读取后由调用者决定下一步。不能静默 last-write-wins。

### 9.3 为什么不持久化 Python 变量名

Agent 更适合显式重新解析，原因是：

- Agent 任务天然会重试、并发、迁移进程或发生上下文压缩；
- Python 变量可能被改名、覆盖或只存在于局部作用域；
- 显式 alias/object_id 能做类型校验、权限检查和审计；
- 重新解析能及时发现对象删除、场景替换和 generation 失效；
- 服务端原子 `ensure` 操作可以提供幂等性，自动扫描脚本 globals 做不到可靠幂等。

alias 是场景控制协议的一部分，例如 `robot`；Python 变量只是当前脚本的局部表达方式。两者可以同名，但不能混为同一种状态。

## 10. Python 代理 API

第一阶段最小 API：

```python
session = connect("pid:18432", workspace="default")

robot = session.ensure_robot(path, alias="robot")
robot.object_id
robot.name
robot.get_link(name)
robot.set_position((x, y, z))
robot.set_rotation((w, x, y, z))
robot.set_transform(...)

link = robot.get_link("Link_6")

gripper = session.ensure_robot(path, alias="gripper")
gripper.set_parent(link, preserve_world=False)
gripper.command_joint_positions([...])

session.objects.get(alias)
session.objects.require(alias, kind=None)
session.objects.bind(alias, proxy)
session.scene_revision
session.close()
```

`set_position()` 和 `set_rotation()` 是对一次 `scene.set_transform` 的读改写封装。为避免两个客户端互相覆盖 Transform，协议应提供字段级更新，而不是客户端先读取完整 Transform 再整体写回。

`set_parent(parent, preserve_world=False)` 的语义：

- `False`：保留 local transform，适合把夹爪按相对位姿挂到 Link；
- `True`：保留当前 world transform，适合编辑器交互式重挂载。

## 11. 线程与执行模型

目标模型是网络线程、Named Pipe 线程和 Python 客户端不直接访问 `World`，由 host 通过语言无关的 `ControlMailbox` 转交 owner thread：

```text
transport thread
    parse + validate request
    submit CommandEnvelope
             |
             v
simulation owner thread
    drain mailbox at a safe point
    mutate/query World
    complete response promise
```

约束：

- GUI host 在 Qt 主线程的每次 frame callback 中排空 mailbox；
- paused/stopped 状态也必须排空 mailbox，不能依赖 `World::step()`；
- headless host 必须有明确的 SimulationRunner owner thread；
- 单个实例按服务端 sequence 确定全局命令顺序；
- 一次 RPC 默认是原子的，一段 Python 脚本不是自动事务；
- 后续可以增加 `transaction.execute`，第一阶段不允许事务跨越长时间或仿真 tick；
- 慢客户端和网络写回不能阻塞 simulation owner thread。

`sim_core` 提供 mailbox 所需的线程安全任务抽象和稳定对象身份，但不包含 socket、Qt 或 Python 代码。传输适配器属于 host/application 层。

首版 GUI 的 Socket 回调本身运行在 Qt 主线程；headless host 则用同一把互斥锁串行化 RPC handler 和持续 SimulationRunner，因此两者都不会并发访问 `World`，暂停/停止状态也能继续处理请求。Core `ControlMailbox` 已在 `PreUpdate` 排空，供后续纯 C++ host 使用；Python headless RPC 严格转发到单一 owner thread 是后续增强项。

## 12. 协议方法

第一阶段新增或统一：

| 方法 | 用途 |
|---|---|
| `session.hello` | 握手、认证和能力协商 |
| `session.ping` | 存活检查和延迟测量 |
| `scene.get_revision` | 查询场景 revision/generation |
| `scene.load_robot` | 加载机器人并返回 ObjectRef |
| `scene.get_transform` | 查询局部和世界 Transform |
| `scene.update_transform` | 字段级修改位置、旋转或缩放 |
| `scene.set_parent` | 设置父对象并选择 preserve_world |
| `scene.resolve_object` | 用 object_id 解析当前 runtime handle |
| `robot.get_link` | 在指定机器人范围内按名称取 Link |
| `workspace.get` | 获取显式对象别名和 revision |
| `workspace.update` | compare-and-swap 更新对象别名 |
| `workspace.bind` | 显式绑定单个别名 |
| `workspace.unbind` | 删除别名 |
| `scene.ensure_robot` | 按 alias 原子查找或加载机器人，支持 Agent 幂等重试 |
| `capabilities.query` | 查询详细方法和限制 |

现有 GUI control RPC 和 Agent RPC 的重叠方法应迁移到公共 dispatch 层，避免两个入口继续产生不同语义。

## 13. 网络与权限模型

实现不能强制仅本机或仅当前 Windows 用户，但开放范围必须由部署者显式选择。

建议策略：

- 未提供 `--listen` 时，不自动开启公网 TCP；
- `--listen tcp://0.0.0.0:PORT` 是合法配置；
- TCP token 可通过 `--auth-token-file`、环境变量或上层认证代理配置；
- token 不写入发现记录和普通日志；
- `tls://` 作为正式 URI 预留，第一阶段可通过反向代理或 VPN 提供加密；
- 协议区分只读和读写 capability，认证结果决定授权范围；
- 记录连接来源、workspace、方法、耗时和结果码，不记录敏感 token；
- 设置消息尺寸、请求速率、并发请求数和超时上限。

由于第一阶段 Python 源码只在客户端执行，远程服务端暴露的是结构化场景 API，而不是任意代码执行接口。远程控制仍具有加载文件和修改场景的能力，生产部署必须配置认证和资源路径策略。

## 14. 错误模型

协议错误至少包含：

| code | 含义 |
|---|---|
| `protocol_mismatch` | 协议主版本不兼容 |
| `instance_not_found` | 指定 PID、名称或 endpoint 没有可连接实例 |
| `launch_failed` | 新实例进程未能启动或在 ready 前退出 |
| `startup_timeout` | 新实例未在期限内完成监听和初始化 |
| `bind_failed` | 固定监听地址或端口无法绑定 |
| `authentication_failed` | 认证失败 |
| `permission_denied` | 当前会话没有所需 capability |
| `instance_mismatch` | ObjectRef 属于另一个实例 |
| `stale_object` | 对象已删除或 generation 已失效 |
| `object_not_found` | object_id 或别名不存在 |
| `alias_conflict` | alias 已绑定到类型或资产来源不匹配的对象 |
| `ambiguous_link` | 机器人范围内存在重名 Link |
| `workspace_conflict` | workspace revision compare-and-swap 失败 |
| `scene_conflict` | 条件更新所依赖的 revision 已变化 |
| `queue_full` | owner-thread mailbox 已达到上限 |
| `request_timeout` | 请求未在截止时间内完成 |

客户端异常必须包含 endpoint、method 和服务端 message，但不能泄露 token。

## 15. 实施分期

### Phase 1：公共协议与连接（首版基线已交付）

- 实现 endpoint parser，支持 `pid:`、`tcp://`、IPv6 和 host:port 简写；
- 实现 `connect`、`launch`、`connect_or_launch` 和 descriptor/握手 ready 检测；
- 支持固定端口、动态端口 `:0`、listen/advertise 分离及启动锁；
- 实现 `session.hello` 和能力协商；
- 实现实例描述文件、PID 存活检查、instance ID 握手校验和陈旧记录忽略；
- Qt host 与 headless host 支持配置任意 TCP bind address。

### Phase 2：Core 并发安全入口与稳定对象（首版基线已交付）

- 增加 `ControlMailbox` 并在 `PreUpdate` 排空；
- GUI 主线程和 headless host 锁分别保证场景访问串行化，paused/stopped 时仍处理控制命令；
- 为 Entity 增加持久 object UUID，并纳入 state serialization；
- 增加 scene generation/revision；
- 增加 `robot.get_link` 和字段级 Transform 更新。

### Phase 3：Python Live SDK（首版已完成）

- 新增 `sim_platform.live.connect()`；
- 实现 `LiveSession`、`EntityProxy`、`RobotProxy` 和 `LinkProxy`；
- `EntityProxy.set_rigid_body()` 与 C API/Python SDK 同步支持 `enabled`、`dynamic`、
  `gravity`、`mass`、三参数 `friction` 和 `restitution`；Live capability 列表必须包含
  `scene.set_rigid_body`。
- Live 场景操作与 C ABI/Python SDK 对齐：资产/实体/基础几何体、名称和可视属性、
  IFPS 球数、单个及批量位姿、父子关系、刚体和外力、机器人位置/速度控制与配置碰撞、
  Camera/LiDAR 创建和读取、碰撞查询、仿真启停/步进/性能步进/事件、状态保存恢复和重置。
- `backend.status`/`backend.apply` 允许现有 Live Session 在保留场景的前提下切换
  `none`/`mujoco`/`bullet3` 物理后端及公开碰撞后端；切换后服务返回新 object refs，
  Python Live SDK 自动刷新已有 proxy，调用者不需要连接或启动替代实例。
- `EditorSession.launch()` 保留为原有便利入口，新增的 GUI Live 启动统一使用公共 `launch(LaunchSpec(kind="editor"))`；
- 新增 `simulator-python run script.py --connect ... [--workspace ...]`；
- 实现显式对象解析、`ensure_robot` 和 alias 冲突检测；
- 自动恢复 Python 变量仅作为后续可选的交互控制台能力。

### Phase 4：远程部署完善

- 提取 GUI control RPC 与 headless RPC 的公共 dispatcher 和结构化错误映射；
- Python headless host 将 RPC 严格路由到单一 simulation owner thread；
- Named Pipe/Unix Domain Socket 和可配置本机访问范围；
- 一个实例支持多个 listener；
- descriptor 增加并校验进程启动时间；
- token/capability 授权；
- 连接、消息和速率限制；
- TLS 原生支持或正式记录受支持的代理部署方式；
- 多客户端、断线重连和长时间运行压力测试。

## 16. 预计修改范围

实施时预计涉及：

```text
include/sim/                 ControlMailbox、ObjectId、revision API
src/core.cpp                 owner-thread command drain、对象解析
src/state_io.cpp             ObjectId 持久化
src/editor.cpp               Live dispatcher、实例 descriptor、可配置 TCP listener
python/sim_platform/server.py headless transport/dispatcher 适配
python/sim_platform/editor.py 迁移到公共 LiveSession
python/sim_platform/live.py   endpoint、session 和代理对象
python/sim_platform/cli.py    simulator-python、launch/connect-or-launch
schemas/                     Live Control 请求与响应 schema
tests/                       Core mailbox/ObjectId 测试
python/tests/                PID/TCP、重连、workspace 和代理测试
```

首版没有新增 Python 第三方依赖。Qt GUI 复用 Qt Network；Python 端使用标准库 socket、JSON、进程和文件 API。Named Pipe/Unix Domain Socket 实施时再评估标准库与成熟依赖，不在首版预先引入。

## 17. 验收标准

以下流程必须在 Windows、Linux 和 macOS 上通过：

1. `launch` 使用固定端口启动一个空场景实例并获得 PID；
2. `launch` 使用端口 `0` 启动实例并返回实际分配的 endpoint；
3. 两个 Agent 并发执行 `connect_or_launch` 时只产生一个命名实例；
4. 新 Python 进程通过 `pid:<PID>` 加载机器人并设置位置；
5. 第一个 Python 进程退出，detached 仿真实例继续运行；
6. 第二个 Python 进程连接同一 PID，通过 alias 重新解析 `robot` 代理；
7. 加载夹爪，取得指定 robot Link，设置父子关系和相对旋转；
8. 仿真持续运行且没有跨线程访问错误；
9. 通过 TCP 地址从另一台机器重复步骤 4 到 7；
10. 固定端口冲突时稳定返回 `bind_failed`，不自动更换端口；
11. 删除机器人后，旧代理稳定返回 `stale_object`；
12. 重启实例导致 PID 被复用时，旧发现记录和旧 ObjectRef 均被拒绝；
13. 两个客户端并发更新同一 workspace 时返回可检测的冲突。

目标示例最终应能以显式、幂等的方式分两次运行：

```python
# first.py
robot = ensure_robot(alias="robot", path=asset_path)
robot.set_position((1.0, 2.0, 0.0))
```

```python
# second.py, 新 Python 进程
robot = require_robot("robot")
gripper = ensure_robot(alias="gripper", path=gripper_path)
gripper.set_parent(robot.get_link("Link_6"), preserve_world=False)
gripper.set_rotation((1.0, 0.0, 0.0, 0.0))
```
