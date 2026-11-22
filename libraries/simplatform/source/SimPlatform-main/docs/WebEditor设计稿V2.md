# WebEditor 总体设计 V2

## 1. 设计定位

WebEditor 是 RobotSimPlatform 的浏览器工作台，用于加载和组织资产、观察和控制仿真场景，并通过文本或语音向 Coding Agent 发布高层语义命令。

WebEditor 与 Qt GUI 面向同一个 Sim Core，但服务于不同的使用场景：

- Qt GUI 适合本地高性能编辑、调试和 Vulkan 原生渲染；
- WebEditor 适合轻量访问、远程协作、Agent 交互和跨平台展示；
- Headless Server 在未启动任何 GUI 时仍可独立运行仿真；
- WebEditor 不直接访问 ECS、MuJoCo、FCL、Vulkan 或插件私有对象。

仓库中原有的 `WebEditor/` 和 `WebServer/` 早期验证实现已经清除。新 WebEditor 不继承其页面结构、代码组织、HTTP 路由、内存状态模型或技术选型。

设计目标：

- 在一个页面内完成“选择资产 → 放入场景 → 调整属性 → 运行仿真 → 通过 Agent 操作”的主要流程；
- WebEditor 与 Qt SimEditor 的基础功能保持语义一致，差异仅限平台相关实现和 Web 端增强能力；
- 以场景快照、命令和事件作为 Web 层与 Sim Core 的边界；
- 通过 `RendererAdapter` 隔离浏览器渲染实现，首期默认使用 Three.js；
- 通过 `AgentTransport` 隔离 Agent 的连接协议和任务状态；
- 让断连、超时、权限拒绝、数据过期和后端不可用等状态明确可见；
- 语音转写持续实时显示，追加在已有输入之后，发送前始终可编辑。

设计原则：

```text
Browser UI 只持有展示状态和未提交草稿
        │
        ▼
Web Gateway 负责鉴权、校验、协议转换和连接管理
        │
        ▼
Communication Layer / Public API 是唯一业务入口
        │
        ▼
Command System 修改场景，Event / Snapshot 驱动界面更新
```

------

# 2. 总体架构

WebEditor 建议采用以下分层结构：

```text
┌──────────────────────────────────────────────────────────────────┐
│                         Browser WebEditor                        │
│                                                                  │
│ App Shell │ Scene Hierarchy │ Inspector │ Viewport │ Asset Browser│
│ Simulation Control │ Sensor Viewer │ Agent Panel │ Diagnostics   │
└───────────────────────────────┬──────────────────────────────────┘
                                │ HTTPS / WebSocket
┌───────────────────────────────▼──────────────────────────────────┐
│                           Web Gateway                            │
│                                                                  │
│ Static Files │ Auth │ REST API │ Event Stream │ Upload │ Session │
│ Scene Gateway │ AgentTransport │ Sensor Stream Descriptor        │
└───────────────────────────────┬──────────────────────────────────┘
                                │ RPC / JSONL / Future Shared Stream
┌───────────────────────────────▼──────────────────────────────────┐
│             Public API / Communication Layer                    │
│                                                                  │
│ Scene Query │ Command Schema │ Simulation API │ Agent API         │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│                            Sim Core                              │
│                                                                  │
│ Command System │ Event System │ State Manager │ Runtime Scene     │
│ Physics / Collision / Render / Sensor Backends                   │
└──────────────────────────────────────────────────────────────────┘
```

控制命令和高频数据需要分离：

```text
低频控制：HTTP / RPC
场景增量：WebSocket Event
完整状态：Scene Snapshot
图像与点云：独立二进制数据流或媒体流
资产文件：HTTP Range / 静态资源服务
```

新实现首期即使用 WebSocket 推送场景事件和任务状态。HTTP 仅用于 Bootstrap、低频命令和资源传输，不采用持续轮询场景全量快照的临时方案。

------

# 3. 职责边界与状态所有权

## 3.1 Browser WebEditor

Browser WebEditor 负责：

- 页面布局、交互和可访问性；
- 当前选中实体、展开节点、面板尺寸、相机视角等展示状态；
- Inspector 中尚未提交的编辑值；
- Agent 输入草稿和语音临时转写片段；
- 场景快照的只读缓存和增量事件应用；
- 渲染资源的加载、缓存和释放；
- 网络状态、命令状态和错误信息展示。

Browser WebEditor 不负责：

- 分配权威 Entity ID；
- 判断 Backend 组合是否合法；
- 直接修改物理世界或 ECS；
- 将浏览器本地路径传递给 Sim Core；
- 将本地缓存当作权威场景状态；
- 在客户端保存 Agent 或平台密钥。

## 3.2 Web Gateway

Web Gateway 负责：

- 提供 WebEditor 静态资源；
- 用户身份、场景权限和会话管理；
- 校验请求参数并转换为稳定的 Communication Layer 命令；
- 管理场景快照、增量事件和重连游标；
- 代理 Agent 命令并维护任务状态；
- 处理上传、文件名清理、类型检查、配额和资源索引；
- 为图像、深度图和点云提供流描述，不在 JSON 中传输大型帧数据；
- 将内部错误映射为稳定、可诊断的 Web API 错误。

## 3.3 Sim Core

Sim Core 是以下状态的唯一权威来源：

- Entity、Component 和父子关系；
- 仿真运行状态、仿真时间和固定步长；
- Physics、Collision、Renderer 和 Sensor Backend 能力；
- Command 执行结果、Undo / Redo 历史和场景 Revision；
- State Save / Restore 和 Reset 后的最终状态。

Agent 和 WebEditor 对场景的所有修改都必须转换为 Command。Command 成功后由 Event / Snapshot 更新所有客户端，不允许 WebEditor 和 Agent 各自维护一套互相覆盖的场景状态。

## 3.4 状态同步规则

每个场景快照必须包含单调递增的 `revision`。客户端修改请求需要携带其基于的 `baseRevision`：

```text
WebEditor 编辑 Entity
        │ baseRevision = 41
        ▼
Web Gateway → Command System
        │
        ├── 成功：revision = 42，发布 entity.updated
        └── 冲突：返回 REVISION_CONFLICT 和最新 revision
```

同步规则：

- UI 只在收到成功响应或对应事件后确认持久化修改；
- 对 Transform 拖动可以做乐观预览，但失败时必须回滚到权威值；
- 事件必须包含 `sceneId`、`revision`、`eventId`、`type` 和 `payload`；
- 收到重复事件时按 `eventId` 去重；
- Revision 跳号时暂停应用增量并重新获取完整快照；
- 重连时优先从最后确认的事件游标续传，无法续传时重新 Bootstrap；
- Reset、Restore State 和切换场景会使选中对象失效，客户端必须按 Entity ID 重新校验。

------

# 4. 页面信息架构

## 4.1 界面参考输入

界面实现参考 `C:\Users\tianlan\Desktop\web-design` 中的以下材料：

- `主界面设计参考.png`：确定左侧功能导航、上下文面板、中央 Display、右侧对话和输入区的空间关系；
- `index.html`、`styles.css`、`app.js`：参考深色工作台、紧凑工具栏、状态栏和 Agent 消息层级；
- `语音模块参考.mp4`：参考聊天输入框内的流式听写、发送前编辑和对话上下文保持；
- `WebEditor设计稿V1.md`：保留资产分类、Renderer Adapter 和 Agent 接入方向。

这些材料只作为视觉和交互输入，不作为源码、依赖或验收对象。新实现不得直接复制其中的演示数据、占位交互和旧目录约定。

## 4.2 Qt SimEditor 对齐基线

“基本功能保持一致”是指用户可以在 WebEditor 和 Qt SimEditor 中完成相同的基础任务，操作结果都落到同一个 Sim Core。控件位置、快捷键和渲染技术可以不同。

| 功能域 | Qt SimEditor 已有基线 | WebEditor 第一阶段要求 |
| --- | --- | --- |
| 资产与创建 | 加载 URDF / OBJ / FBX；创建 Cube、Sphere、Plane、Capsule、Cylinder | 等价支持，并增加 Project / Uploaded Asset Browser |
| 场景层级 | 真实父子树、选中、展开保持、重挂父节点、保持世界姿态、Robot Link 约束 | 语义与约束一致 |
| Viewport | 实时场景、拾取、黄色描边、Move / Rotate / Scale Gizmo、Focus、Frame Scene | Three.js 实现相同基础操作与反馈 |
| 相机导航 | 轨道旋转、平移、滚轮缩放、WASDQE 飞行 | 桌面端全部提供，快捷键冲突时遵循输入焦点规则 |
| Inspector | Name、Position、Rotation、Scale、Visible、LOD、Parent、Components、删除 | 字段、单位、范围和只读约束一致 |
| 物理属性 | RigidBody 启用、Static / Dynamic、Gravity、Mass、三类 Friction、Restitution | 能力一致，修改通过 Command System |
| 碰撞可视化 | Collider Wireframe；静态、动态、Trigger 使用不同颜色 | 提供等价开关和图例 |
| IFPS 可视化 | Leaf Sphere 数量、Wireframe / Solid、Level 0～7 显示 | 在 Backend 声明 IFPS 能力时提供等价控制 |
| 相机 Sensor | 创建 RGB / RGBD；Frame ID、频率、分辨率、FOV、Near / Far；RGB / Depth 预览 | 属性和预览能力一致，数据经 Sensor Stream 传输 |
| Robot Control | 对选中 Robot 下发关节位置目标 | 提供关节列表、目标值校验和执行反馈 |
| 仿真与状态 | Start、Pause、Step、Reset、Real-Time Factor、Save State、Restore State | 命令和最终状态一致 |
| Backend | Physics、Collision、Renderer 能力选择、组合校验、应用结果 | 展示同一 Backend Manager 能力；浏览器 Renderer 单独配置 |
| Agent | 连接状态、Agent Operations 日志、场景变更同步 | 保留这些能力并增加对话、任务、确认和语音输入 |
| 日志与性能 | Log、错误、仿真时间、Render FPS / ms | 提供 Log / Diagnostics 和等价状态指标 |

Qt 的 Vulkan Render Backend 与浏览器的 Three.js Renderer Adapter 是两个不同层次：Backend 配置决定 Sim Core 的服务端渲染能力，Renderer Adapter 决定浏览器本地 Viewport 的绘制方式。界面必须分开显示，不能把 Three.js 当作平台 Render Backend。

## 4.3 页面结构

```text
WebEditor
├─ 顶部命令栏
│  ├─ 项目、场景与未保存状态
│  ├─ Load Asset / Add Primitive / Add Sensor
│  ├─ Start / Pause / Step / Reset / Save / Restore
│  └─ Real-Time Factor 与连接状态
├─ 左侧导航
│  ├─ Scene Hierarchy
│  ├─ Details
│  │  ├─ Inspector / Physics / IFPS / Camera
│  │  └─ Robot Control
│  ├─ Global Settings / Backends
│  └─ Asset Browser
├─ 中央工作区
│  ├─ Viewport Toolbar
│  ├─ 3D Viewport / Gizmo
│  ├─ Collider / IFPS Overlay
│  ├─ Camera Preview
│  └─ Status Bar / Log / Diagnostics
└─ 右侧 Agent 工作区
   ├─ 会话历史
   ├─ 任务执行状态
   ├─ Agent Operations
   ├─ 场景变化与确认
   └─ 文本 / 语音共享输入框
```

左侧窄导航决定相邻上下文面板的内容，不同时堆叠所有属性面板。Details 根据当前 Entity 的 Component 动态显示 Inspector、Physics、IFPS、Camera 或 Robot Control 分组。Log / Diagnostics 属于第一阶段基本功能，可从底部状态栏展开；Timeline、Record / Replay 编辑器属于后续能力。

------

# 5. 主界面布局

## 5.1 桌面端基准布局

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ WebEditor / Scene: demo  [Load][Add][保存/恢复][运行/暂停][Step][RTF: 1]   │
├──────┬───────────────┬──────────────────────────────┬──────────────────────┤
│导航  │ 上下文面板      │ 工具栏 + 主视口               │ Agent 对话            │
│      │               │                              │ [历史会话]            │
│层级  │ ▾ World       │ [移动][旋转][缩放][碰撞][聚焦] │ 用户：把球移到...     │
│细节  │   ▾ JAKA      │                              │ Agent：已执行...       │
│设置  │     ├ Link1   │       3D Scene / Gizmo       │ [任务详情/确认]        │
│资产  │     └ Gripper │                              │ [Agent Operations]    │
│      │               │                              │                      │
│      │ 当前为层级面板  │                  [相机预览]  │                      │
│      │ 切换细节后显示  │                  RGB/Depth   ├──────────────────────┤
│      │ 对应组件属性    │                              │ [文本/实时语音] [发送] │
├──────┴───────────────┴──────────────────────────────┴──────────────────────┤
│ Connected · Running · t=12.4s · 60 FPS / 16.7 ms · MuJoCo · FCL · Rev 42 │
└────────────────────────────────────────────────────────────────────────────┘
```

布局约束：

- 1440×900 及以上：四区同时显示，左侧编辑区和右侧 Agent 区可调整宽度；
- 1280×800：保留完整主视口，Camera Preview 默认折叠；
- 768～1279 px：左侧 Inspector 与右侧 Agent 使用可切换抽屉；
- 小于 768 px：只要求查看、基础仿真控制和 Agent 输入；完整编辑能力的验收以桌面端为准；
- 顶部命令栏、底部状态栏和 Agent 输入栏尺寸稳定，动态状态不得造成布局跳动；
- 所有可调整面板需要设置最小宽度，不能覆盖 Viewport 或输入栏。

## 5.2 视觉与控件规范

- 采用参考稿的深色专业工作台方向，以中性石墨灰为背景，蓝色表示选中，绿色表示成功，琥珀色表示警告，红色表示错误或删除；
- 不使用装饰性渐变、光斑、超大标题和营销式卡片，Viewport 是中央主体验；
- 顶部栏约 52 px、Viewport Toolbar 约 44 px、状态栏约 28 px，左侧导航约 64 px；上下文面板和 Agent Panel 支持拖动调整宽度；
- 面板通过边线和背景层级区分，不使用页面区块悬浮卡片；Camera Preview 是唯一允许覆盖 Viewport 的工具窗；
- Move、Rotate、Scale、Focus、Frame Scene、Collider、麦克风、发送等使用熟悉图标，图标按钮提供 Tooltip 和可读名称；
- 二元设置使用 Toggle / Checkbox，模式使用 Segmented Control 或 Tabs，Backend 和多选项使用 Select / Menu；
- 卡片和浮层圆角不超过 8 px，控件文本不得溢出或改变工具栏高度；
- 选中 Entity 使用黄色三维包围描边，Gizmo 使用 X 红、Y 绿、Z 蓝，与 Qt SimEditor 保持一致。

------

# 6. Scene Hierarchy

Scene Hierarchy 用于显示：

- World；
- 普通 Entity；
- Robot 与 Link；
- Sensor；
- Light；
- Camera；
- 父子关系和可见状态。

主要交互：

1. 点击节点选中实体，Viewport 高亮，Inspector 显示对应 Component；
2. 展开、折叠和搜索仅改变客户端展示状态；
3. 在层级树中拖动普通资产根或完整 Robot 根到目标实体，提交 Reparent Command；
4. Reparent 成功后保持世界姿态，由服务端计算或校验新的局部 Transform；
5. Robot Link 不允许独立拆离，World 不允许删除或重命名为无效值；
6. 右键菜单提供重命名、复制、删除、聚焦和显示/隐藏；
7. 删除父节点时明确展示将被删除的子树，并提供确认或可撤销结果；
8. Agent 修改场景后，受影响节点短暂标记，并允许定位到实体。

节点显示至少包含：

```text
类型图标  名称  可见状态  锁定状态  脏状态或远程变更提示
```

大场景需要采用虚拟滚动。搜索结果必须保留祖先路径，避免只显示孤立节点。

------

# 7. Property Inspector

Property Inspector 按 Entity 所拥有的 Component 分组显示：

```text
Identity
Transform
Visual / Material
Collider
RigidBody
Joint
Sensor
Controller
Backend Binding
```

第一阶段字段与 Qt SimEditor 对齐：

| 分组 | 字段或能力 |
| --- | --- |
| Identity | Name；只读 Entity ID、Kind、Parent、Components |
| Transform | 本地 Position XYZ、Rotation RPY°、Scale XYZ |
| Visual | Visible、Finest LOD 0～7、Coarsest LOD 0～7 |
| RigidBody | Enabled、Static / Dynamic、Gravity、Mass kg |
| Collider | Sliding / Torsional / Rolling Friction、Restitution |
| IFPS Visualization | Selected Shell、Leaf Spheres、Wireframe / Solid、Level 0～7 |
| Camera | RGB / RGBD、Frame ID、Update Rate、Width、Height、Vertical FOV、Near / Far Plane、RGB / Depth Preview |
| Robot / Joint | Robot 身份与关节列表；Robot Link 的受控字段只读；关节目标从 Robot Control 提交 |

只有 Entity 实际拥有或允许添加的 Component 才显示对应编辑项。Robot articulated system 管理的 RigidBody、Link 层级和部分 Transform 字段需要显示只读原因，不能仅禁用而不解释。

编辑规则：

- 数字字段必须限制合法范围并拒绝 `NaN`、`Infinity` 和空数组；
- Rotation 界面可使用角度，协议层必须明确单位，建议统一传输弧度或显式携带 `unit`；
- Transform 需要明确局部坐标与世界坐标模式，首期默认局部坐标；
- Scale 必须大于零；Robot Link 的 Scale 默认只读；
- `Finest LOD <= Coarsest LOD`，修改后显示实际生效范围；
- Mass 必须为正，Friction 不得为负，Restitution 限制在 `[0, 1]`；
- Camera 的 `Far Plane > Near Plane`，分辨率、更新频率和 FOV 使用与 Qt 相同的有效范围或由 Schema 下发范围；
- IFPS Leaf Spheres 必须为正；解析球体可固定为一个精确 Sphere；较大数量需要提示离线构建成本；
- Inspector 的一次提交对应一个 Command，连续 Gizmo 拖动可以合并为一个 Undo 记录；
- 字段失焦、Enter 或明确 Apply 时提交，提交中显示 pending 状态；
- 校验失败保留用户输入并在字段附近显示原因；
- Revision 冲突时展示最新值，不静默覆盖其他客户端的修改。

------

# 8. 3D Viewport 与 Renderer Adapter

## 8.1 Viewport 功能

Viewport 首期提供：

- 轨道相机旋转、平移和缩放；
- WASDQE 前后左右上下飞行相机，输入控件聚焦时不触发；
- Entity 拾取与选中高亮；
- 黄色三维包围描边，以及红、绿、蓝三轴 Move、Rotate、Scale Gizmo；
- Focus Selection 和 Frame Scene；
- Grid、世界坐标轴和基础灯光；
- Camera Frustum；
- Collider Wireframe，静态为绿色、动态为青色、Trigger 为品红色，并显示图例；
- IFPS Sphere Shell 的 Wireframe / Solid 与 Level 0～7 分层颜色；
- 将资产拖入视口并转换为世界坐标落点；
- 加载中、资源缺失、渲染器不可用和上下文丢失状态。

Viewport 只渲染场景的可视化副本。Gizmo 操作不能直接成为权威物理状态，提交路径仍然是 Command System。

## 8.2 Renderer Adapter

浏览器渲染器通过统一接口隔离：

```ts
interface RendererAdapter {
  mount(canvas: HTMLCanvasElement): Promise<void> | void;
  syncScene(snapshot: SceneSnapshot): Promise<void> | void;
  applyEvent?(event: SceneEvent): Promise<void> | void;
  select(entityId: number | null): void;
  setTool?(tool: "select" | "move" | "rotate" | "scale"): void;
  worldPoint?(pointer: { clientX: number; clientY: number }, planeZ: number): Vec3 | null;
  frame?(entityId?: number): void;
  resize?(width: number, height: number, dpr: number): void;
  render(): void;
  dispose(): void;
}
```

首期 Browser Renderer 默认实现为 Three.js。`WebGL` 是图形 API 名称，不应作为与 Three.js 并列的产品级 Renderer Adapter；如果保留选项，应明确为 Three.js WebGL Renderer。平台 Render Backend 仍由 Backend Manager 声明，可为 None、Vulkan 或未来实现，与 Browser Renderer 分开配置和报告。

Renderer 切换流程：

```text
检查能力 → 创建新 Adapter → 加载当前 Snapshot → 首帧成功
                                           │
                     成功：释放旧 Adapter ─┤
                     失败：保留旧 Adapter ─┘
```

渲染器切换不得修改 Scene、仿真时间、选中实体或 Undo 历史。

## 8.3 资源生命周期

- 纹理、Geometry 和 Material 按 Asset ID 与版本缓存；
- Entity 删除或场景切换后释放不再引用的 GPU 资源；
- 浏览器 `webglcontextlost` 时暂停渲染并显示恢复状态；
- 大型模型加载不能阻塞工具栏、Agent 输入或仿真状态更新；
- 模型解析失败时使用边界盒占位，并保留可读错误。

------

# 9. Asset Browser 与上传

资产来源统一分为：

```text
Built-in          内置 Primitive 和标准 Sensor
Project Assets    项目受控资产，只读
Uploaded          当前服务管理的上传资产
```

新实现由 Asset Service 和 Resource Manager 返回 Asset ID、来源、版本和能力。浏览器不扫描项目目录，不了解服务器文件路径，也不直接把上传目录当作资产索引。

资产元数据建议包含：

```ts
interface AssetSummary {
  id: string;
  name: string;
  type: "primitive" | "robot" | "mesh" | "sensor" | "scene" | "unknown";
  source: "builtin" | "project" | "uploaded";
  version?: string;
  thumbnailUrl?: string;
  importStatus: "ready" | "processing" | "failed";
  capabilities?: string[];
  error?: ApiError;
}
```

主要交互：

- 按来源、类型和名称过滤；
- 展示名称、类型、缩略图、来源和导入状态；
- 从工具栏或 Asset Browser 创建 Cube、Sphere、Plane、Capsule 和 Cylinder；
- 创建 RGB Camera 和 RGBD Camera，默认挂到当前选中 Entity 或 World root；
- 拖拽资产卡片到 Viewport 或 Scene Hierarchy 创建实例；
- 上传显示逐文件进度、大小、校验阶段、失败原因和重试入口；
- Uploaded Asset 支持删除和重新加载；
- 删除仍被场景引用的资产时必须拒绝或展示影响范围；
- 导入 Robot 时创建完整 Robot 根和 Link 结构，不将其降级为单个 Mesh Entity。

首期允许的格式以平台 Asset Importer 的能力声明为准，不应仅凭扩展名宣称支持。为了与 Qt SimEditor 保持一致，P0 必须包括 URDF、OBJ 和 FBX，并正确处理 URDF Visual、材质、Origin、Mesh Scale、`package://` 资源和 FBX 节点层级。STL、glTF、USD 是否开放由实际 Importer 能力决定。

资源加载结果需要展示 imported / cache hit、Link / Visual / Mesh / Texture 数量和错误详情。Windows 中文路径、大型索引 Mesh 和 Baking Cache 失效重建的行为必须与桌面端使用同一个 Resource Manager 语义。

上传安全要求：

- 限制单文件大小、总配额和文件数量；
- 清理文件名并使用服务端生成的 Asset ID；
- 校验真实内容类型，禁止路径穿越和任意覆盖；
- 解析工作在隔离目录或受限进程中完成；
- 资产文件接口只允许读取已登记资源；
- 不向浏览器返回服务器绝对路径。

------

# 10. Simulation Control

Simulation Control 提供：

- Start；
- Pause；
- Step；
- Reset；
- Fixed Time Step；
- Real-Time Factor；
- Save State；
- Restore State；
- 后续 Record 与 Replay。

建议状态机：

```text
loading ──成功──> paused <──Pause── running
   │                │  │             │
   └──失败──> error │  └──Start──────┘
                    ├──Step──> paused
                    └──Reset / Restore──> synchronizing──> paused
```

规则：

- `Step` 仅在 paused 状态可用；
- Start、Pause、Step 和 Reset 在请求未完成时禁用重复提交；
- Reset、Restore State 和切换 Backend 前需要确认未保存修改的处理方式；
- UI 显示服务端返回的运行状态、simulation time、fixed time step 和 real-time factor；
- 浏览器标签页降频不能改变服务端仿真步进；
- Physics / Collision 组合由 Backend Manager 校验，前端只展示能力和错误；
- 第一阶段不支持运行中热切换 Backend。

## 10.1 Robot Control

选中 Robot 根或其 Link 时，Details 中提供 Robot Control：

- Robot 名称、关节数量和关节名称；
- 当前位置、速度和 Effort 的只读状态；
- 关节位置目标输入，首期与 Qt SimEditor 的 Joint Target 能力一致；
- 目标数量、数值范围和连续关节校验；
- 提交、执行中、成功和失败反馈；
- 不选择 Robot 时明确提示需要选择 Robot Entity。

WebEditor 不在浏览器内执行控制器。所有目标通过 Robot Control Command 进入 Sim Core，仿真后的真实 Joint State 通过事件更新。

## 10.2 Backend Configuration

Global Settings 显示 Backend Manager 返回的实际能力：

```text
Physics:   None / MuJoCo / Bullet3 / capability-defined
Collision: None / FCL / Bullet3 / Coal / IFPS-CPU / IFPS-GPU / capability-defined
Renderer:  None / Vulkan / capability-defined
Browser Renderer: Three.js / future adapter
```

- Physics、Collision、平台 Renderer 与 Browser Renderer 使用独立字段；
- 应用配置前由服务端校验组合，失败时保留原有效配置；
- Active 状态、能力缺失和失败原因同时显示在 Settings、Status Bar 和 Log；
- Dynamic RigidBody 没有有效 Physics Backend 时提示并拒绝运行，不由浏览器擅自选择默认 Backend；
- 第一阶段只要求 paused 状态应用 Backend 配置。

------

# 11. Sensor Viewer

第一阶段支持从工具栏创建 RGB Camera 和 RGBD Camera。选中 Camera Entity 后，Details 中必须能够编辑与 Qt SimEditor 一致的属性：

- Output；
- Frame ID；
- Update Rate；
- Width 与 Height；
- Vertical FOV；
- Near Plane 与 Far Plane；
- RGB 或 Depth Preview；
- Camera 光轴约定和 Frustum 可视化。

Camera Preview 提供 RGB、Depth 和 RGBD 查看模式，并显示：

- Sensor 名称；
- 分辨率；
- 数据时间戳；
- 帧率；
- 仿真时间；
- `loading`、`streaming`、`no_data`、`stale`、`disconnected` 和 `error` 状态。

图像流不得嵌入 Scene Snapshot。建议由控制接口返回流描述：

```ts
interface SensorStreamDescriptor {
  sensorId: number;
  mode: "rgb" | "depth" | "rgbd";
  transport: "webrtc" | "websocket-binary" | "mjpeg" | "snapshot";
  url: string;
  width: number;
  height: number;
  encoding: string;
  expiresAt?: string;
}
```

Depth 显示需要固定或可调的量程和颜色映射，并明确无效深度值。RGBD 模式应定义并排、叠加或标签切换行为。无真实 Sensor Provider 时必须显示 `no_data`，不能使用静态示意图伪装实时画面。

------

# 12. Agent 交互

## 12.1 调用关系

Agent 不通过 WebEditor 直接操作场景：

```text
WebEditor
    │ Agent Command
    ▼
Web Gateway / AgentTransport
    │
    ▼
Communication Layer
    │
    ▼
Command System ──> Sim Core ──> Event / Snapshot ──> WebEditor
```

即使 WebEditor 关闭，Agent 仍可在 Headless 模式下运行。WebEditor 只是 Agent 会话、确认、结果和场景变化的可视化入口。

为保持 Qt SimEditor 基础能力，Agent Panel 还必须提供：

- Agent 连接状态和当前 Endpoint 的受控描述；
- Agent Operations 结构化日志；
- 按 Job、时间、状态和受影响 Entity 过滤；
- 查看原始 JSONL / Command 仅限有诊断权限的用户；
- Agent 场景变更自动同步到 Tree、Inspector 和 Viewport。

## 12.2 消息与任务状态

文本和语音共享同一个可编辑输入框。每条命令至少展示：

- 用户输入；
- 创建时间；
- 目标 Scene ID；
- Job ID；
- `queued`、`sending`、`running`、`awaiting_confirmation`、`success`、`failed`、`cancelled` 或 `timeout`；
- 结果摘要和稳定错误码；
- 受影响的 Entity ID；
- 需要时提供“定位到实体”“查看变更”“撤销”操作。

建议接口：

```ts
interface AgentTransport {
  send(command: AgentCommand): Promise<AgentJob>;
  subscribe(jobId: string, onEvent: (event: AgentJobEvent) => void): Unsubscribe;
  cancel?(jobId: string): Promise<void>;
  dispose(): void;
}

interface AgentCommand {
  requestId: string;
  sceneId: string;
  baseRevision: number;
  text: string;
  attachments?: string[];
}
```

Agent 命令通过 HTTP 或 RPC 创建 Job，后续状态通过统一 WebSocket Event Stream 推送。客户端生成 `requestId`，服务端以此实现幂等重试；刷新或重连后使用 Job ID 查询权威终态。

## 12.3 确认与权限

Agent 操作分为：

```text
只读查询              无需确认
可撤销场景编辑         可按用户策略自动执行
运行控制              可配置确认
删除、Reset、Restore   默认确认
文件写入、外部访问     必须显式授权
```

确认卡片必须展示拟执行动作、目标对象、关键参数和影响范围。拒绝确认后 Job 进入 `cancelled`，不得继续执行。Agent 输出文本不能绕过 Web Gateway 权限校验直接生成内部命令。

## 12.4 会话持久化

- 输入草稿按用户和场景保存，场景切换时隔离；
- 已确认消息由服务端持久化，`localStorage` 只可保存非权威的短期界面缓存；
- 刷新后恢复消息状态时以 Job 状态为准；
- 会话历史需要数量上限、清理入口和隐私提示；
- 附件只保存 Asset ID 或受控资源引用，不保存浏览器本地路径。

------

# 13. 语音输入

语音输入状态机：

```text
idle
  │ 点击麦克风
  ▼
requesting ──拒绝──> denied
  │ 授权
  ▼
listening <──> transcribing
  │ 停止
  ▼
editable ──发送──> sending
  │
  └──识别失败──> error ──保留文本──> editable
```

交互规则：

- 仅在第一次使用语音输入，或上一次权限状态为 `denied` 时申请麦克风权限；
- 已获得 `granted` 后再次点击不得由应用主动重复申请；浏览器仍可能按自身策略再次提示；
- 权限结果可以持久化到当前浏览器会话，但实际权限状态必须以浏览器 API 为准；
- 识别采用持续会话和流式增量结果，不能等整句话结束才显示；
- 开始识别时记录输入框已有内容为 `prefix`；
- 最终片段追加到 `final`，临时片段只替换 `interim` 尾部；
- 输入框展示值为 `prefix + final + interim`，任何识别事件都不能覆盖 `prefix`；
- 用户在识别中把光标移回前文或手动修正时，已编辑区立即成为新的稳定文本；后续临时结果只能更新当前语音尾部，不能恢复错误旧文本；
- 识别实现无法可靠维护编辑边界时，应先停止当前识别并以完整输入建立新 prefix，不能冒险覆盖用户修改；
- 停止录音后内容保持可编辑，不自动发送；
- 无浏览器 Speech API 时保留手动输入，并提示可接入 Web Gateway 流式语音服务；
- 识别语言可配置，首期默认 `zh-CN`；
- 置信度、识别延迟和错误只作为辅助状态，不阻塞手动修正。

权限或识别失败需要区分：

```text
permission_denied
no_microphone
no_speech
network_error
service_unavailable
recognition_timeout
unsupported_browser
```

语音数据默认不落盘。若未来接入服务端语音识别，上传和保留策略需要单独取得用户同意。

------

# 14. Web API 与数据契约

## 14.1 目标接口分组

新 Web API 按资源和职责分组。具体路径在 OpenAPI 与 JSON Schema 冻结时确定，设计稿不继承旧 WebServer 路由。

| 接口组 | 主要能力 | 传输方式 |
| --- | --- | --- |
| Session API | 登录、退出、当前用户、权限和服务版本 | HTTPS |
| Project API | 项目列表、打开项目、最近项目 | HTTPS |
| Scene API | Bootstrap、Snapshot、创建和切换场景 | HTTPS |
| Command API | Entity 编辑、仿真控制、Undo / Redo、状态保存与恢复 | HTTPS / RPC Gateway |
| Event API | 场景增量、仿真状态、Agent Job 和诊断事件 | WebSocket |
| Asset API | 查询、上传、导入、删除和缩略图 | HTTPS |
| Sensor API | Sensor 列表、Stream Descriptor 和流控制 | HTTPS + 独立媒体流 |
| Agent API | 创建、取消、确认和查询 Job | HTTPS + WebSocket Event |
| Diagnostics API | 健康状态、能力声明和受控日志查询 | HTTPS |

所有修改型接口必须支持：

- `requestId` 幂等标识；
- `sceneId` 和 `baseRevision`；
- 明确的用户与权限上下文；
- 稳定错误码；
- 审计字段；
- 成功后的新 Revision 或异步 Job ID。

## 14.2 正式响应格式

成功响应建议统一为：

```json
{
  "requestId": "req-01",
  "data": {},
  "revision": 42
}
```

错误响应建议统一为：

```json
{
  "requestId": "req-01",
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "Scene changed after this edit started",
    "details": {
      "expectedRevision": 41,
      "actualRevision": 42
    },
    "retryable": true
  }
}
```

HTTP 状态码用于表达传输层结果，`error.code` 用于界面行为和诊断。不得依赖解析英文 `message` 判断错误类型。

## 14.3 Scene Snapshot

```ts
interface SceneSnapshot {
  sceneId: string;
  revision: number;
  status: "loading" | "paused" | "running" | "error";
  simulationTime: number;
  entities: EntityView[];
  settings: SceneSettings;
  capabilities: CapabilitySet;
}

interface EntityView {
  id: number;
  name: string;
  parent: number | null;
  kind: string;
  translation: Vec3;
  rotation: Vec3;
  scale: Vec3;
  visible: boolean;
  components?: Record<string, unknown>;
}
```

正式契约需要与平台现有 Schema 统一，TypeScript 类型应由同一份 JSON Schema 或 OpenAPI 定义生成，避免前后端手工维护两套字段。

## 14.4 事件格式

```ts
interface SceneEvent<T = unknown> {
  eventId: string;
  sceneId: string;
  revision: number;
  type: string;
  timestamp: string;
  actor?: { type: "user" | "agent" | "system"; id?: string };
  payload: T;
}
```

首期事件类型至少包括：

```text
scene.loaded
scene.reset
entity.created
entity.updated
entity.deleted
selection.hint
simulation.status_changed
settings.updated
sensor.stream_changed
agent.job_updated
diagnostic.created
```

------

# 15. 错误处理与诊断

错误分层：

```text
输入错误        字段旁提示，保留草稿
业务拒绝        Toast + 详情，例如 Link 不可拆离
Revision 冲突   阻止覆盖，刷新受影响数据
连接中断        全局状态条 + 自动重连
资源加载失败    Viewport 占位 + 资产详情
后端错误        Diagnostic 面板 + requestId / traceId
致命初始化错误  阻止进入编辑态，提供重试
```

诊断信息至少包含：

- 时间；
- 来源模块；
- 稳定错误码；
- 用户可读说明；
- Scene ID 和 Entity ID；
- Request ID、Job ID 或 Trace ID；
- 是否可重试；
- 建议恢复动作。

不得向普通用户直接显示服务器堆栈、绝对路径、密钥或 Backend 私有对象信息。开发模式可以在受控 Diagnostics 中展示详细日志。

连接状态统一为：

```text
connecting → connected → reconnecting → disconnected
                         └──────────────> incompatible
```

重连使用指数退避和随机抖动。断连期间保留 Inspector 与 Agent 草稿，但禁止提交依赖权威状态的修改。重新连接后先校验服务版本和 Scene Revision。

------

# 16. 安全与权限

正式部署至少需要：

- HTTPS 和受保护的 WebSocket；
- 用户身份认证和场景级授权；
- 修改场景、运行仿真、上传资产和调用 Agent 的独立权限；
- CSRF 防护或同源 Token 策略；
- 明确的 CORS Allowlist，不能使用生产环境通配符；
- Content Security Policy 和禁止未受控脚本执行；
- 上传大小、请求频率、Agent 并发数和任务时长限制；
- Agent Command、确认、执行结果和关键场景修改审计；
- 凭据仅由服务端持有，浏览器不保存 Agent、RPC 或 Backend 密钥。

已经清除的旧 WebServer 不进入部署链路，也不承担兼容服务。新服务必须在默认配置下满足上述安全约束，开发模式的放宽项需要显式启用并限制在本机地址。

------

# 17. 可访问性、国际化与快捷键

可访问性要求：

- 所有按钮、表单、树节点和 Tab 可通过键盘访问；
- 图标按钮提供可读名称和 Tooltip；
- 焦点样式清晰，打开抽屉或确认框后正确管理焦点；
- 状态不能仅依赖颜色区分；
- Toast 和任务状态使用合适的 `aria-live`，避免高频事件持续打断读屏；
- 文本与关键控件满足 WCAG 2.1 AA 对比度；
- `prefers-reduced-motion` 时关闭非必要动画；
- Canvas 提供当前选中对象和场景状态的文本替代信息。

建议快捷键：

| 快捷键 | 行为 |
| --- | --- |
| `Q` | Select |
| `W` | Move |
| `E` | Rotate |
| `R` | Scale |
| `F` | Focus Selection |
| `Delete` | 请求删除选中 Entity |
| `Ctrl+Z` | Undo |
| `Ctrl+Shift+Z` | Redo |
| `Space` | Start / Pause，输入框聚焦时不触发 |
| `Ctrl+Enter` | 发送 Agent 命令 |
| `Escape` | 取消当前操作或关闭浮层 |

界面文本首期使用中文，代码、协议字段和日志保持英文稳定标识。所有用户可见文本应集中管理，为后续英文界面预留，不把翻译文本作为业务判断条件。

------

# 18. 性能与稳定性

性能目标：

- 首屏 UI 可交互时间不超过 2 秒，不包含大型资产下载；
- 1,000 个可见 Entity 的示例场景中，桌面端 Viewport 保持不低于 30 FPS；
- 普通文本命令从发送到显示 Web Gateway ACK 不超过 300 ms；
- 服务端能力满足时，语音首个临时转写片段不超过 1 秒；
- Inspector 普通编辑在收到事件后 500 ms 内同步到 Tree 和 Viewport；
- Scene Hierarchy 在 10,000 个节点时通过虚拟滚动保持可操作；
- 网络断开不会导致页面崩溃，重连后保留未提交草稿。

性能策略：

- 场景更新优先使用增量事件，快照用于初始化和纠偏；
- Transform 高频预览与服务端提交节流分离；
- Tree、Inspector 和 Agent Panel 避免因渲染帧循环整体重绘；
- 大模型解析使用 Worker 或异步任务；
- 浏览器 DPR 设置上限，后台标签页暂停非必要 Viewport 渲染；
- Sensor 数据和场景控制使用独立通道，避免大帧阻塞命令 ACK。

------

# 19. 建议的工程结构

新实现建议使用 `web/` 独立根目录，不重新占用已经清除的旧目录结构。以下目录名是职责示意，最终名称可在工程初始化时确定：

```text
web/
├─ editor/
│  ├─ src/
│  │  ├─ app/
│  │  ├─ scene/
│  │  ├─ inspector/
│  │  ├─ viewport/
│  │  ├─ assets/
│  │  ├─ simulation/
│  │  ├─ sensors/
│  │  ├─ agent/
│  │  ├─ transport/
│  │  └─ diagnostics/
│  ├─ tests/
│  └─ public/
├─ gateway/
│  ├─ api/
│  ├─ auth/
│  ├─ scene_gateway/
│  ├─ agent_transport/
│  ├─ asset_service/
│  ├─ stream_gateway/
│  └─ tests/
├─ contracts/
│  ├─ openapi/
│  ├─ schemas/
│  └─ generated/
└─ e2e/
```

`contracts/` 是 WebEditor、Web Gateway 和 Communication Layer 的共同边界，生成代码不可反向成为 Schema 的编辑源。拆分触发条件是出现独立状态、接口或测试边界，而不是单纯按页面元素拆文件。

正式技术栈需要在新工程初始化前单独确认，至少评估：

- 前端框架的长期维护、状态模型和测试生态；
- Three.js 与选定框架的集成方式；
- Gateway 使用现有 C++ Communication Layer、独立服务或轻量适配进程的成本；
- OpenAPI / JSON Schema 的代码生成链路；
- Windows 和 Linux 的统一构建、打包与部署；
- 与仓库现有 CMake、Python 和 CI 流程的边界。

不重新引入全局状态、手写 DOM 更新、内存 Demo Scene、目录扫描资产索引和轮询 Job 等旧模式。

------

# 20. 第一阶段开发内容

## 20.1 Browser WebEditor

第一阶段至少完成：

- App Shell 和响应式桌面布局；
- Scene Hierarchy 的展示、选中、搜索和约束拖放；
- Property Inspector 的 Identity、Transform、Visual / LOD、RigidBody / Collider、IFPS 和 Camera 编辑；
- Three.js Viewport、轨道与飞行相机、拾取、黄色描边、三轴 Gizmo、Focus 和 Frame Scene；
- Collider Wireframe、Camera Frustum 和 IFPS 分层可视化；
- Asset Browser、上传进度、失败反馈和拖入场景；
- URDF / OBJ / FBX 加载，以及五种基础 Primitive 和 RGB / RGBD Camera 创建；
- Start、Pause、Step、Reset、Real-Time Factor、Save State 和 Restore State；
- Robot 关节状态和关节位置目标；
- Physics、Collision、平台 Renderer 和 Browser Renderer 的独立配置与状态；
- RGB / Depth / RGBD 真实数据流和 Camera 参数编辑；
- Log / Diagnostics、性能指标和可恢复错误；
- Agent 会话、连接状态、Operations、任务、确认和场景定位；
- 文本与语音共享输入框；
- 连接、错误和诊断状态；
- 基础键盘操作和窄屏抽屉。

## 20.2 Web Gateway

第一阶段至少完成：

- 静态资源服务和版本握手；
- 身份与场景权限的最小实现；
- Scene Snapshot 和 Revision；
- Command API 和统一错误结构；
- WebSocket 场景事件；
- Agent Job 创建、状态推送、超时和取消；
- 上传校验、Asset ID 和导入任务状态；
- Sensor Stream Descriptor；
- 与 Communication Layer 的适配；
- 请求日志、审计字段和健康检查。

## 20.3 与 Sim Core 集成

第一阶段至少贯通：

```text
WebEditor → Web Gateway → Communication Layer → Command System
    ▲                                             │
    └──────────── Event / Snapshot ───────────────┘
```

必须验证：

- URDF / OBJ / FBX 与基础 Primitive / Camera 创建；
- 创建、修改、删除和保持世界姿态重挂 Entity；
- Robot Link 约束和关节位置目标；
- Inspector、RigidBody / Collider、IFPS 和 Camera 属性；
- Viewport 导航、选择、描边、Gizmo、Collider 和 IFPS Overlay；
- Start、Pause、Step、Reset 和 Real-Time Factor；
- Backend 能力、应用结果和组合错误；
- Save State 和 Restore State；
- RGB 与 Depth Sensor Frame；
- Log、性能指标和 Agent Operations；
- Agent Command 触发场景修改；
- WebEditor 自动显示 Agent 修改；
- Headless Server 在无 WebEditor 时独立工作。

------

# 21. 测试策略

## 21.1 前端单元测试

- Scene Event 去重和 Revision 跳号恢复；
- Tree 构建、搜索和选中同步；
- Inspector 校验和冲突处理；
- RigidBody、Collider、IFPS、Camera 和 Robot Control Schema 映射；
- RendererAdapter 生命周期和切换失败回滚；
- Agent Job 状态转换；
- 语音 `prefix + final + interim` 合并逻辑；
- 权限拒绝、无 Speech API 和识别错误；
- 草稿按用户和 Scene 隔离。

## 21.2 Web Gateway 单元测试

- 请求 Schema 与统一错误码；
- 路径穿越、非法文件名和上传限制；
- Scene Revision 冲突；
- Agent Job 超时、取消和幂等；
- Event 游标续传和快照回退；
- 权限矩阵；
- Sensor Stream Descriptor 过期；
- 静态资源与 API 不泄露绝对路径。

## 21.3 集成测试

- WebEditor → Web Gateway → Sim Core 场景查询；
- 创建 Entity → Event → Tree / Viewport / Inspector 同步；
- Gizmo 修改 → Command → Physics / Render 状态同步；
- Collider / IFPS Overlay 与服务端 Component 状态同步；
- 上传 URDF → Import → Asset Browser → 创建 Robot；
- OBJ / FBX → Resource Manager → Viewport，并验证中文路径和 cache hit 信息；
- 创建五种 Primitive、RGB Camera 和 RGBD Camera；
- Robot Joint Target → Controller → Joint State Event；
- Start → Step → Save → Step → Restore；
- Agent Command → Command System → WebEditor 场景更新；
- Backend 组合错误 → 稳定错误码 → UI 恢复；
- Sensor Provider → 浏览器 RGB / Depth 展示；
- WebSocket 断开 → 重连 → Revision 恢复。

## 21.4 浏览器端 E2E

至少覆盖：

1. 加载示例场景；
2. 在 Tree 中选中实体并验证 Viewport / Inspector 同步；
3. 加载 URDF / OBJ / FBX，创建 Primitive 和 Camera；
4. 修改 Transform、LOD、RigidBody / Collider、IFPS 和 Camera 属性；
5. 验证轨道 / 飞行相机、描边、Gizmo、Focus、Frame、Collider 和 IFPS Overlay；
6. 下发 Robot Joint Target；
7. 运行、暂停、单步、Reset、Save 和 Restore；
8. 切换合法 Backend，拒绝非法组合并检查 Log；
9. 查看 RGB / Depth Frame；
10. 发送文本命令并查看 Agent Operations 和场景结果；
11. 验证语音权限、流式转写、前文修正和停止后编辑；
12. 模拟 Agent 未连接、API 500、WebSocket 断连和资源加载失败；
13. 在 1440×900、1280×800、平板和移动宽度检查无重叠。

## 21.5 性能测试

- 1,000 个可见 Entity 的 Viewport 帧率；
- 10,000 个 Tree 节点的展开、搜索和选中；
- 高频 Transform Event；
- 大型 URDF / Mesh 加载；
- Sensor Stream 与命令并发；
- 长会话 Agent 消息列表；
- 断连重连和 Snapshot 恢复时间。

------

# 22. 第一阶段暂不包含的内容

为了控制范围，第一阶段暂不包含：

- 在浏览器中直接运行 Physics Backend；
- 浏览器直接访问 ECS 或 Backend 私有对象；
- 浏览器端 Vulkan 原生渲染；
- 3DGS 和光线追踪；
- 完整 Timeline、Record 和 Replay 编辑器；
- 多用户实时协同编辑和冲突合并 UI；
- Backend 运行中热切换；
- 通用可视化脚本插件市场；
- 在浏览器保存长期 Agent 密钥；
- 将上传目录作为生产级资产数据库；
- 将 Web Speech API 作为唯一语音方案；
- 在没有真实数据时模拟 Sensor、Agent 或 Sim Core 已连接。

这些能力应在正式 Communication Layer、Command / Event 闭环、资源导入和权限模型稳定后逐步加入。

------

# 23. 验收标准

## 23.1 Qt SimEditor 功能一致性门槛

验收使用同一版本 Sim Core、同一组 Backend、同一场景资产和等价命令分别操作 Qt SimEditor 与 WebEditor。要求权威场景结果一致、错误语义一致、界面反馈等价；不要求 Vulkan 与 Three.js 的像素结果完全一致。

| 对齐项 | 通过条件 |
| --- | --- |
| Asset Load | 两端均可加载同一 URDF、OBJ、FBX，并得到一致的 Entity / Link 层级和资源错误 |
| Primitive / Sensor Create | 两端均可创建 Cube、Sphere、Plane、Capsule、Cylinder、RGB Camera 和 RGBD Camera |
| Scene Hierarchy | 父子关系、保持世界姿态重挂、解除父子关系、Robot Link 禁止拆离的结果一致 |
| Selection / Viewport | 均支持拾取、黄色描边、Move / Rotate / Scale、Focus、Frame Scene 和相机导航 |
| Debug Overlay | 均支持 Collider Wireframe；IFPS 可用时支持 Sphere Shell、模式和 Level 选择 |
| Inspector | Identity、Transform、Visible、LOD、RigidBody、Collider、Camera 字段和约束一致 |
| Robot Control | 对同一 Robot 的关节位置目标校验、提交结果和 Joint State 更新一致 |
| Simulation | Start、Pause、Step、Reset、Real-Time Factor、Save / Restore 的最终状态一致 |
| Backend | 展示同一能力声明；相同组合得到一致的成功结果或错误码 |
| Sensor | 同一 Camera 配置、RGB / Depth 可用性和错误状态一致 |
| Agent / Log | Agent 连接、Operations、场景变更和关键错误在两端均可查看 |
| Status / Performance | 均显示 simulation time、运行状态、Backend 和 Render FPS / ms 或等价指标 |

任一对齐项缺失即视为第一阶段未完成。只有当测试环境中的 Sim Core 或 Backend 本身未声明该能力时，WebEditor 才可显示为不可用；不能用前端占位代替功能。

## 23.2 P0：必须通过

- [ ] 在 1440×900 与 1280×800 窗口中，导航、Tree、Viewport、Agent Panel 和输入栏均可访问且不互相遮挡；
- [ ] 加载示例场景后，Tree 正确展示父子关系，点击 Entity 后 Viewport 高亮与 Inspector 同步；
- [ ] Tree 拖放保持世界姿态，并遵循 Robot Link 不可拆离约束；
- [ ] Position、Rotation 和 Scale 修改经过 Command API，成功后在 500 ms 内同步到 Tree、Viewport 和 Inspector；
- [ ] LOD、RigidBody、Collider、IFPS 和 Camera 属性按 Capability 显示并可正确提交；
- [ ] 非法数字、非正 Scale、Robot Link 拆离和 World 删除被明确拒绝；
- [ ] Revision 冲突不会静默覆盖服务端状态，界面提供刷新或重新应用入口；
- [ ] Start、Pause、Step、Reset、Save State 和 Restore State 的状态及错误与服务端一致；
- [ ] 资产库区分 Built-in、Project 和 Uploaded，上传有进度与失败反馈，拖拽资产能创建场景实例；
- [ ] Robot Asset 创建完整层级，导入失败展示稳定错误码和可读原因；
- [ ] 五种 Primitive、RGB / RGBD Camera 和 Robot Joint Target 可实际执行；
- [ ] Physics、Collision、平台 Renderer 与 Browser Renderer 被明确区分，Backend 应用结果进入状态栏和 Log；
- [ ] 文本命令能够创建 Agent Job，并显示 queued、running、success、failed、cancelled 和 timeout 状态；
- [ ] Agent 未连接、空命令和未知命令均显示真实状态，不伪装执行成功；
- [ ] Agent 修改场景后，WebEditor 通过 Event / Snapshot 自动更新，并可定位到受影响 Entity；
- [ ] 语音只在首次使用或上次被拒绝后由应用申请权限，已授权状态再次使用不主动重复申请；
- [ ] 语音识别以持续流式方式显示，不等待整句结束；
- [ ] 输入框已有文本在语音开始后保持不变，临时片段更新不覆盖 prefix 和 final；
- [ ] 停止语音后内容仍可编辑且不自动发送；无权限、无 API 和识别失败均有可恢复提示；
- [ ] 默认使用 Three.js，渲染调用只经过 `RendererAdapter`；切换失败时保留原渲染器和场景状态；
- [ ] Camera Preview 区分 loading、streaming、no_data、stale、disconnected 和 error；
- [ ] RGB / Depth 预览使用真实 Sensor 数据；未连接 Provider 时不显示模拟画面；
- [ ] WebSocket 断开后 UI 不崩溃，重连后恢复状态并保留未提交草稿；
- [ ] Web Gateway 不向浏览器泄露服务器绝对路径、堆栈和凭据；
- [ ] WebEditor、Agent 和 Qt GUI 对同一场景的修改都经过 Command System。

## 23.3 P1：应通过

- [ ] Undo / Redo 能覆盖 Inspector、Gizmo、创建和删除操作；
- [ ] 对话历史按用户和 Scene 恢复，刷新后不丢失已确认消息与 Job 状态；
- [ ] 关键流程可完全使用键盘完成，状态不只依赖颜色表达；
- [ ] Scene Hierarchy 对大场景使用虚拟滚动；
- [ ] 关键逻辑具有单元测试，主流程具有浏览器 E2E；
- [ ] Headless Server 在未启动 WebEditor 时仍可接受 Agent 和仿真控制命令。

## 23.4 性能与稳定性

- [ ] 首屏 UI 可交互时间不超过 2 秒，不包含大型资产下载；
- [ ] 1,000 个可见 Entity 的示例场景中保持不低于 30 FPS；
- [ ] 普通文本命令从发送到显示 Web Gateway ACK 不超过 300 ms；
- [ ] 服务端能力满足时，语音首个文本片段不超过 1 秒；
- [ ] 高频场景事件不会阻塞 Agent 输入和 Simulation Control；
- [ ] 长时间运行后不存在持续增长的 Renderer、事件订阅或 Sensor Stream 资源泄漏。

------

# 24. 第一阶段完成标准

第一阶段可以定义以下端到端验收场景：

1. 用户登录并打开一个 Headless Server 中的示例场景；
2. WebEditor 获取 Scene Snapshot 并显示 Entity 层级；
3. 用户从 Project Assets 导入一个 URDF Robot；
4. 用户加载 OBJ / FBX，并创建五种 Primitive；
5. 用户在 Tree 中保持世界姿态重挂普通资产，并验证 Robot Link 约束；
6. 用户通过 Inspector 和 Gizmo 调整 Transform、LOD、RigidBody / Collider；
7. 用户打开 Collider Overlay，并在 IFPS 可用时检查 Sphere Shell 和 Level；
8. 用户创建 RGBD Camera，编辑参数并查看真实 RGB / Depth；
9. 用户选择 Robot、下发 Joint Target 并查看 Joint State；
10. 所有编辑转换为 Command，并通过 Event 同步回 WebEditor；
11. 用户配置合法的 Physics / Collision / Renderer Backend 组合并查看 Log；
12. 用户 Start、Pause、Step 和调整 Real-Time Factor；
13. 用户保存状态、继续运行并恢复状态；
14. 用户通过文本或语音输入一条 Agent 命令，并在识别中修正前文；
15. Agent Job 展示执行进度、Operations、结果和受影响 Entity；
16. Agent 对场景的修改自动出现在 Tree、Inspector 和 Viewport；
17. 断开 WebSocket 后界面进入 reconnecting，恢复连接后校验 Revision；
18. 对非法 Backend 组合、Robot Link 拆离和 Agent 未连接给出明确错误；
19. 使用相同场景执行 Qt / Web 一致性矩阵，所有对齐项通过；
20. 关闭 WebEditor 后，Headless Server 和 Agent 仍可继续运行。

满足以上内容后，WebEditor 即形成第一条完整闭环：

```text
Browser WebEditor
        ↓
Web Gateway / Auth / Protocol Adapter
        ↓
Communication Layer / Command System
        ↓
Runtime Scene / Simulation / Sensor
        ↓
Event / Snapshot / Stream
        ↓
Tree / Inspector / Viewport / Agent Panel
```

------

# 25. 交付边界

本文档位于 `docs/WebEditor设计稿V2.md`，用于约束新 WebEditor 和 Web Gateway 的设计与正式实现，不再视为 `web-design/` 下的临时设计文件。

仓库中的旧 `WebEditor/` 与 `WebServer/` 已经清除，不存在源码迁移或兼容阶段。新工程从正式契约和本文档起步，不恢复旧路由、旧页面、内存 Demo Scene 或旧上传目录。

新实现启动前，需要优先冻结以下跨模块契约：

1. Scene Snapshot 与 Scene Event Schema；
2. Command、Revision 和 Undo / Redo 语义；
3. Backend Capability Schema；
4. Agent Job、确认和审计 Schema；
5. Asset ID、导入任务和文件访问规则；
6. Sensor Stream Descriptor；
7. 身份、场景权限和部署拓扑。

上述契约应与 `仿真平台架构v2.md` 中的 Public API、Communication Layer、Command System、Event System、State Manager 和 Backend Manager 保持一致。新实现以正式契约和 Sim Core 状态所有权为准，旧实现行为不具有约束力。

------

# 26. Web 功能开关约束

Web Gateway 必须从 `web/config/features.json` 读取功能开关，并通过 Scene Snapshot 的
`features` 字段下发给 WebEditor。开关采用点分层级路径；任一父路径为 `false` 时，其全部
子项均视为关闭。未在配置中显式声明的功能默认关闭，配置值必须是 JSON boolean。

功能开关至少覆盖命令栏、导航、场景层级、Details 各 Component 分组及字段、Backend
Settings、资产库、Viewport 工具与交互、Camera Preview、Diagnostics、Agent 输入与语音、
状态栏和资产依赖确认流程。关闭开关必须同时移除或禁用对应交互，动态表单中被关闭的字段
在提交其他字段时必须保留原值。

后续新增任何用户可见功能时，必须在同一变更中完成以下内容：

1. 在 `web/config/features.json` 增加唯一的功能键；
2. 在 WebEditor 中接入可见性和交互保护；
3. 在 Bootstrap 契约及相关类型中保持功能配置可用；
4. 增加父开关、叶子开关或页面行为的自动化覆盖；
5. 更新相关用户或设计文档。

缺少上述任一项的用户可见功能不得视为完成。功能开关用于产品能力裁剪，不替代服务端身份、
权限和 API 授权控制。
