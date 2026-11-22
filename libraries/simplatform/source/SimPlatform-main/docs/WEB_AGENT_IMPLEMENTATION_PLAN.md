# Web Agent 服务实施计划

> 对应设计：`docs/WEB_AGENT_SERVICE_DESIGN.md`
>
> 计划状态：Local/dev implementation complete; production identity, retention and quota policy remain deployment decisions.
> Agent 服务实现：`F:\RobotSimPlatform\agent_service`（本仓库内，不再依赖外部 agent 仓库）

## 1. 实施原则

- 先固定合同和唯一状态所有权，再接模型与 UI。
- 每阶段都可独立测试、回滚，不把 Agent、Live snapshot、时间线一次性合并。
- Gateway 侧只保留与实现无关的传输契约；新增模块优先采用组合而非修改旧类。
- 模型运行器是 OpenAI 兼容的 Qwen HTTP 客户端，不依赖任何 CLI 子进程；配置缺失只标记 `MODEL_UNAVAILABLE`，不阻断服务启动。
- Gateway 以结构化 `controlContext` 传递受信控制信息；`agent_service` 的 planning/compile/runtime 三层都不直接导入或使用 MuJoCo。
- 不引入新的生产依赖，除非现有 FastAPI/aiohttp/标准库不能满足且经过单独评审。
- 所有 API/schema 变更同时更新契约测试。

## 2. 阶段与交付物

### P0：合同与能力 Spike

目标：消除两个最高风险假设。

工作：

1. 在部署目标环境验证 Qwen 端点的结构化输出（`json_schema` 及其回退链）、超时、取消和鉴权失效行为。
2. 用 20-50 个实体测量 native/C API/Python/Live `scene_snapshot` 候选 payload 的大小、序列化时间和 20 Hz 采样开销。
3. 确定 canonical `SceneSnapshot` schema、C ABI 两阶段 buffer 合同，以及与现有 Web `SceneSnapshot` 的字段映射。
4. 固化 Agent Service OpenAPI、Gateway WebSocket event schema、timeline manifest schema。

完成门槛：

- 有可重复的模型 runner fixture，而不是依赖真实模型跑单测。
- 明确统一 native snapshot builder、C API、Python SDK、Live transport 的实现位置和性能预算。
- schema 评审通过，错误码和版本字段齐全。

### P1：Agent 对话服务骨架

仓库：`F:\RobotSimPlatform`

预计文件：

```text
agent_service/{api,schemas,store,service,runner}.py
agent_service/cli.py
agent_service/tests/test_service.py
```

工作：

1. 新建 conversations/messages/turns/events 表和 repository。
2. 实现 conversation/turn REST API、幂等 request ID、每会话串行锁、取消和重启 interrupted 状态。
3. 用桩 runner 完成所有 API 和状态机测试。
4. 新增 `python -m agent_service.api` 服务入口与 `python -m agent_service.cli` 本地入口。

完成门槛：

- `agent_service` 全部单测通过。
- 新增 API 契约测试通过。
- 服务重启、重复提交、并发提交、取消覆盖。

### P2：Qwen Runner、确定性编译与 Live 运行时

仓库：`F:\RobotSimPlatform`

工作：

1. 实现 OpenAI 兼容的 Qwen runner（`agent_service/planning/qwen_client.py`），结构化输出按 `json_schema` -> `json_object` -> 带校验纯文本回退，并保留原始响应仅入受限诊断日志。
2. 启动时做一次配置自检：缺少 base URL 或模型名标记 `MODEL_UNAVAILABLE`，`/health` 仍返回 ok，turn 提交返回明确 blocker。
3. 添加 turn 最终输出 JSON Schema，拒绝非结构化/超限结果。
4. `planning/` 是唯一允许调用模型的层；`compile/` 与 `runtime/` 必须确定性，对相同 SkillPlan + 场景产出相同命令。
5. `runtime/` 只通过已发布 Live Python 能力操作 `controlContext` 指定的会话：FK/Jacobian、DLS IK、接触查询、RRT-Connect、夹爪与轨迹。
6. 验证 blocker 行为：实例不存在、API 缺失、资产缺失时不得 launch 替代实例或用私有 `_request()` 绕过。
7. 增加取消、超时和日志脱敏。
8. 新增 `python -m agent_service.cli chat|repl`，要求显式 `--connect`，复用同一个 ConversationService。

完成门槛：

- 桩 provider 集成测试覆盖成功、失败、非法 JSON、超时和取消。
- 模型未配置时 `/health` 仍 ok 且 turn 返回 `MODEL_UNAVAILABLE` 有测试。
- 一次真实 smoke test 完成两轮上下文对话。
- 本地 CLI 完成 connect-only smoke test。
- Web 可见事件中不含 tool call、命令或内部 trace。

### P3：平台统一 SceneSnapshot 与权威 Live Session

仓库：`F:\RobotSimPlatform`

预计涉及：

```text
include/sim/c_api.h
include/sim/scene_snapshot.hpp（或现有 core 公共模型的合适位置）
src/c_api.cpp
src/scene_snapshot.cpp
python/sim_platform/live.py
python/sim_platform/sdk.py
python/sim_platform/server.py 或对应 host transport
schemas/live-control.schema.json
web/gateway/scene backend adapter
tests/test_core.cpp
python/tests/test_live.py
python/tests/test_sdk.py
web/tests/test_gateway.py
docs/API_PYTHON_QUICK_REFERENCE.md
```

工作：

1. 定义版本化公共 `SceneSnapshot` schema 与唯一 native snapshot builder，和现有可恢复 `WorldSnapshot`/`State` 明确分层。
2. 在 C API 增加 UTF-8 JSON 两阶段读取函数，规定 NUL、容量不足、world 变化、错误码和线程安全语义；不改现有 `sim_save_state` ABI。
3. Python SDK 增加 `SceneSnapshot` 强类型模型和 `Simulator.scene_snapshot(world)`，只通过新 C API 获取并校验数据。
4. Live RPC 增加 `scene.snapshot` capability，`LiveSession.scene_snapshot()` 复用同一个 Python 模型。
5. 快照包含稳定对象 ID、generation/revision/time、层级、transform、visual/collider/rigid body、robot joints、camera/sensor 与资源引用。
6. 增加 native -> C JSON -> Python SDK -> Live 的 canonical golden contract test，确保同一静止 world 内容一致。
7. Gateway 改为绑定明确 Live Session；启动时校验 Agent 和 Gateway 的 session ID 一致。
8. 提供 revision 事件或有界 polling，完成断线重连和 generation 变化处理。
9. 保留现有进程内 `SceneService` 作为测试 backend，不让生产路径同时维护两个权威 world。

完成门槛：

- Agent 修改 Live Session 后，Gateway 拉取的实体、关节和 time 一致。
- C API、Python SDK、Live Session 在同一测试场景上的 schema/version/canonical payload 一致。
- `sim_save_state`/`sim_restore_state`、Python `State` 和 `.simstate` 回归测试全部通过，ABI 符号保持兼容。
- C API 公开头、Python SDK、Live schema、快速参考文档和跨进程测试同步更新。
- 20 Hz 目标下无无界积压；最终 revision 一定可达。

### P4：Gateway Agent Client、Prompt 策略与场景写租约

仓库：`F:\RobotSimPlatform`

工作：

1. 以有类型的异步 `AgentServiceClient` 承载传输，传递 conversation/turn/request/scene binding。
2. Gateway 只发送结构化 `controlContext`（scene binding、回调地址、per-job token、presentation mode），不拼装任何自然语言控制指令；用户原始文本只放在 `message`。
3. Agent Service 以结构化字段做鉴权和校验，不从用户文本推断会话绑定。
4. Gateway 持久化或可恢复映射：Web conversation -> Agent conversation -> scene binding。
5. 实现 scene write lease，同一 scene 的 Agent 与用户写操作串行。
6. 将 Agent 公开事件转为 `agent.turn_updated`，保持旧 `agent.job_updated` 的短期兼容适配。
7. Agent Service 不可用时健康检查降级，不影响最后场景预览和非 Agent 功能。

完成门槛：

- Gateway 契约测试覆盖重复 request、Agent 重连、取消、scene busy、generation mismatch。
- 测试浏览器不能覆盖 `controlContext`；Agent 收到原始 message 和结构化 binding，且 token 不出现在任何浏览器可见载荷中。
- 测试 Web 请求只能操作已绑定会话，本地 CLI 需显式 `--connect`。
- 浏览器网络响应与 WebSocket 中没有内部 trace。

### P5：Presentation Store 与双模式

仓库：`F:\RobotSimPlatform`

工作：

1. 实现 mode、静态投影、原子 publish、presentation revision。
2. `real-sync`：revision 变化自动拉取并合并推送。
3. `sync-and-animate`：冻结投影，Gateway 服务端拒绝所有场景写命令。
4. 为 Agent 提供受认证的 presentation control API：publish snapshot、capture start/finish/abort。
5. 实现场景 session/generation 校验和旧投影 detached 标记。

完成门槛：

- 未 publish 时权威世界变化不影响网页投影。
- 模式只读由服务端强制，而不是只禁用按钮。
- capture 失败不覆盖上次成功发布内容。

### P6：时间线存储与前端播放

仓库：`F:\RobotSimPlatform`

预计涉及：

```text
web/contracts/types.ts
web/contracts/openapi.yaml
web/gateway/presentation.py
web/gateway/server.py
web/editor/src/main.ts
web/editor/src/renderer.ts
web/editor/src/styles.css
web/editor/index.html
web/tests/
web/e2e/
```

工作：

1. 实现按时长和字节约束、帧数不限的 capture、完整快照帧、manifest、按需帧 HTTP API、内存中的原子发布和任务终态清理。
2. 前端新增模式 segmented control 和 read-only 状态。
3. 主视口下方实现播放/暂停、slider、首尾、时间显示和倍速菜单。
4. Renderer 支持快速应用时间线帧，播放不触发场景写 API。
5. 处理新 timeline 到达、对象消失、帧加载失败、拖拽取消和移动端布局。

完成门槛：

- 1 帧无 timeline，2 帧以上控件完整。
- Playwright 覆盖拖拽、播放、暂停、倍速、模式只读、WebSocket 重连。
- 桌面和移动端无重叠，长时间线不导致内存无界增长。

### P7：端到端、安全与运维

工作：

1. 端到端场景：两轮上下文、两种同步模式、静态发布、多帧发布、取消、Agent/Gateway/sim 重启。
2. Agent Service 默认只监听 loopback，Gateway control API 使用 turn-bound token；payload/帧数/时长/字节限制和日志脱敏已实现。生产身份认证、保留期和配额清理任务需要部署策略确认。
3. Gateway `/api/metrics` 提供 turn 终态、累计延迟、capture bytes/frames 和 WebSocket connection 聚合指标；scene sync lag、runner errors 细分和 reconnect 次数接入统一采集系统后补齐。
4. 编写启动配置、环境变量、端口、健康检查、升级/回滚手册。
5. 灰度开关：conversation agent、sync-and-animate、timeline 分别可关闭。

完成门槛：

- 设计书第 12 节全部验收。
- 旧 Web 编辑与 `agent_service` 回归套件通过。
- 故障注入无数据串话、无静默启动替代 sim、无未清理 runner/capture。

## 3. 测试矩阵

| 层次 | 必测内容 |
| --- | --- |
| Agent 单元 | store、状态机、幂等、并发锁、事件过滤、结构化输出解析 |
| Agent 集成 | 模型未配置 blocker、结构化输出回退链、确定性编译、公开 SDK blocker、取消/超时 |
| Platform C++ | native snapshot builder、组件覆盖、排序、revision/generation、自洽性 |
| Platform C API | 两阶段 buffer、UTF-8/NUL、容量不足、错误码、ABI/state 回归 |
| Platform Python SDK | 强类型解析、C API 映射、静止 world canonical 一致性 |
| Platform Live | RPC capability、跨进程一致性、断线、generation 变化 |
| Gateway 单元 | mode 权限、write lease、publish 原子性、timeline 上限 |
| Gateway 契约 | OpenAPI、WebSocket event、`controlContext` 防篡改与 token 不外泄、重连恢复、错误码 |
| 本地 CLI | connect-only、conversation 续接、无 Gateway 运行 |
| Frontend 单元 | frame selection、播放时钟、倍速、readonly command guard |
| E2E | 两种模式、单/多帧、两轮对话、断线、刷新、移动端 |
| 性能 | 20 Hz 快照、600 帧时间线、多个观察客户端、慢客户端背压 |

## 4. 建议合并顺序

1. PR-A（agent）：P1，纯对话服务 + 桩 runner。
2. PR-B（agent）：P2，Qwen runner + 确定性编译 + Live 运行时。
3. PR-C1（platform）：P3 前半，native schema + C API + Python SDK snapshot。
4. PR-C2（platform）：P3 后半，Live snapshot + Gateway authoritative-session adapter。
5. PR-D（platform）：P4，Gateway Agent Service client + write lease。
6. PR-E（platform）：P5，Presentation Store + 双模式。
7. PR-F（platform）：P6，timeline backend + frontend。
8. PR-G（两仓库）：P7，E2E、部署和安全收尾。

每个 PR 独立启用 feature flag；不要在 PR-D 前删除旧 Agent transport，待集成验收后再移除兼容路径。

## 5. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 上游模型响应格式变化 | runner 适配层可替换；Pydantic 校验 + 结构化输出回退链；解析失败即失败不猜测 |
| 用户文本试图覆盖控制信息 | 控制信息只走结构化 `controlContext`；服务端二次校验 session/generation；浏览器无法提供该字段 |
| 移植破坏既有仿真能力 | 新增独立包；不改 `sim_save_state`/`State` ABI；平台与 Gateway 回归套件必须通过 |
| 统一 snapshot 过大或过慢 | native 一次构建、字段裁剪、资产引用复用、推送限频、性能门槛；后续 delta/binary API |
| C/Python/Live schema 漂移 | 单一 schema/native builder、canonical golden test、版本兼容策略 |
| 观察快照与恢复状态混淆 | API 命名和类型分离；禁止 snapshot 用于 restore；保留 state 回归测试 |
| Agent 和用户并发改写 | Gateway scene write lease + revision 检查 |
| Agent 失败后世界已部分改变 | 不自动回滚；查询实际状态并由用户/新 turn 决定恢复 |
| sync 模式只在 UI 禁用导致绕过 | Gateway 服务端统一返回 `MODE_READ_ONLY` |
| 内部尝试泄露 | runner 事件白名单；公开/诊断存储分离；契约测试扫描敏感字段 |
| 时间线耗尽磁盘/内存 | 帧数、时长、字节三重上限；TTL/LRU；按需加载 |
| sim 实例漂移 | 所有请求携带 session ID + generation；不允许隐式 launch |

## 6. 首个开发迭代建议

第一迭代只做 P0 + P1：先得到完全不依赖真实模型的有上下文 API、持久化和状态机测试。第二迭代并行推进 Qwen Runner 与统一 native/C API/Python/Live snapshot；只有两者都通过各自契约后才联通 Gateway。这样能把改动集中在 `agent_service` 与一个 CLI 入口，并尽早暴露平台公共快照能力是否足够这一关键风险。
