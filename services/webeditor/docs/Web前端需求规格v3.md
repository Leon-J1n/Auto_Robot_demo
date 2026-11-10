# Web 前端需求规格 v3

> 本文件由 v2.1 提炼（2026-11-05），只保留现行口径；历史决议记录（v2 §13）与逐轮修订史见 `docs/works/历史档案/`；任务进度不在本文件，见 `docs/works/INDEX.md`。
> 版本基调沿用 v2 的"必须/不得"书写风格。关键词：**必须（MUST）** = 本期实现且验收卡这条；**应（SHOULD）** = 默认实现，可在待确认项中推翻；**可（MAY）** = 可选增强。
>
> 权威参考文件：
> - 世界描述文件：`docs/ur5e_table_empty.json`（真实场景数据）＋ `docs/场景文件描述.md`（逐组件的格式说明）。`tests/fixtures/scene_example.json` 是 M1~M7 各卡的回归输入（**禁改**）。
> - ObjectLibraryServer 契约：`docs/OLS接口.md`
> - 轨迹二进制格式：`docs/轨迹文件布局描述.md` + 样例 `docs/robot_grasp.traj`
> - Agent 通信流程：`docs/gateway与Agent新通信流程.md`（**brain-web/2**）

---

## 1. 总体架构变更

1.1 场景的**解析、构建、渲染、轨迹播放**全部在浏览器端（TypeScript + Three.js）完成。
1.2 gateway（Python）降级为：静态资源服务 + 调用方接口层 + 与浏览器的事件通道 + OLS 配置下发。**不解析、不校验、不计算场景语义**。
1.3 gateway **不再提供实时场景编辑**。场景写入路径只有：调用方接口（`updateWorld`/`updateItem`/`addObject`/`updateTraj`）与用户手动 Load Scene。
1.4 gateway **必须持久化最近一次生效的世界 json 与轨迹字节流**（原样存储，不解释语义；落盘位置见 §9.2 的 `state_dir`，默认 `var/gateway_state.json`）。这是"不维护场景"的唯一例外：**只存不算**。用途被限定为三件事 —— ② 类上报的 id 水位追认（§5.5 第 ③ 条）、新接入的只读标签页补状态（§5.2）、诊断与事务回执对账（§5.9 的 `/queryWorldCommit`）。
 - **它不是任何读取路由的回退来源**（**Q111**）：`GET /getWorld` 的 `X-World-Source` 只有 `live` 一个取值，绑定的浏览器不在线就是 `503 NO_BROWSER_CONNECTED`，gateway **绝不**拿手里那份缓存冒充现场。
1.5 Agent 模块按 `docs/gateway与Agent新通信流程.md`（**brain-web/2**）改造为**任务制**（链路细则 §5.15，**Q86/Q87/Q109/Q111/Q115/Q116**）：浏览器发「新一轮命令 + 当前场景 json」，gateway 实时取一次现场后转发 `POST {agent_service_url}/startTask` 取回任务 id，此后**每 5 秒**轮询 `GET /queryTaskState`，**终态的唯一判据是响应里的 `terminal` 布尔**（运行状态词表归 Brain，我方不内置枚举，**Q116**），终态后各取一次 `GET /queryTaskFailureReason` 与 `GET /queryTaskResult`，状态变化经 `/events` 推给浏览器；Agent 在「进行中」期间反过来调用 §5.9 的调用方接口（含 `commitWorld` 那族）。任务受理那一刻会话进入 `agent_owned`，② 类入口随之冻结（§4.6）。
 - 原需求第 17 条的预设场景字段**继续排期在后续，本期不做**；会话（conversation）的路由与语义不动（§5.15 末条）。

### 职责划分（目标态）

| 能力 | 归属 |
|------|------|
| URDF 解析展开 | 浏览器 `UrdfLoader`（TS） |
| 世界变换计算 | 浏览器（Three.js 场景图） |
| OBJ/STL/DAE/FBX 几何加载 | 浏览器 |
| 轨迹解析 | 浏览器（TRAJ v2 二进制） |
| 相机帧 | 浏览器离屏渲染 + 经 gateway 回传调用方 |
| 资产来源 | ObjectLibraryServer（OLS）/ 云存储（COS） |
| Agent 任务状态获取 | gateway 每 5 s 轮询 Agent 服务并经 `/events` 推送状态变化（§5.15、**Q86**） |

---

## 2. 术语与标识约定

2.1 **类型规则（全局统一）**：`id` 一律为 **uint32**（非负整数，禁止负数、禁止小数、禁止 `null`）；`name` 一律为字符串。
2.2 **唯一性**：同一类型内 `id` 不重复；`name` **可能重复**，因此任何解析逻辑**不得用 name 作为查找键**（`name` 仅用于展示）。
2.3 `object_id_in_scene`：物体在**当前场景内**的唯一 id（uint）。
2.4 `model_id` / `robot_id` / `scene_id`：OLS 索引里的全局 id（uint），**三者是互不相通的编号空间**（`model 2` 与 `robot 2` 是两个不同资产，见 §6.11、`docs/场景文件描述.md` §4.2），且都与 `object_id_in_scene` 无关，不得混用。
2.5 `0xFFFFFFFF`：轨迹文件中的"场景根"哨兵值，在本规格中一律映射为 `Transform.components.properties.parent = null`（**该映射规则取代轨迹文档 §3.1/§7-7 的 worldId 说法**）。
2.6 角度制式：`quaternion_xyzw`、`position/scale`（非角度）、关节角、`joint_limits` 一律**弧度**；`Camera.fov` 为**度**（**Q3**）。
2.7 长度单位：米。
2.8 四元数分量顺序：一律 **xyzw**（`docs/ur5e_table_empty.json` 与轨迹格式一致），禁止按 wxyz 解释文件。落地形态：`world.ts` 只做"长度为 4 的有限数组"校验并按原序保存，渲染层 `node.quaternion.set(...quaternion)` 直接交给 Three.js（其分量顺序同为 xyzw），因此**不存在也不需要**任何重排适配。
2.9 **`browserID` 是浏览器 profile 的持久身份**（Q153）：浏览器首次启动页面时读取当前 origin 的 `localStorage["webeditor.browserID"]`；缺失或空值时生成 UUID v4 并写回。优先使用 `crypto.randomUUID()`，在普通 HTTP 的 LAN 页面不可用时改用 `crypto.getRandomValues()` 生成同等强度的 UUID。不同设备、不同浏览器或不同浏览器 profile 的 origin 存储彼此隔离；同一 profile 的刷新、重启和多个 tab 复用同一个 browserID。它是 gateway 的路由分区键，不是鉴权凭据，用户复制／伪造 localStorage 不在本期安全边界内。
2.10 **Gateway 的状态隔离**（Q153）：所有可变现场、会话阶段、对象 id 水位、租约／隔离、任务、幂等表、对话、相机候选、事务回执和 pending request 都按 `browserID` 分区；同一 profile 的第二个 tab 仍沿用只读观察者规则。浏览器 HTTP 请求用 `X-WebEditor-Browser-ID` 选 profile；原有 query/body 中的 `browser_id`／`browserID` 仍是路由的业务输入与断言，不覆盖该头，也不改变 AgentBrain 报文。AgentBrain 的持久状态按 profile 保存，gateway 内部按 `(browserID, sessionID)` 关联，不能只用 `sessionID` 或全局 active state。

---

## 3. 世界描述文件格式（结构以本节为准；逐组件的取值口径见 `docs/场景文件描述.md` 与 `docs/ur5e_table_empty.json`）

3.1 顶层字段：`scene_schema_version`(uint)、`scene_version`(uint)、**`scene_sub_version`**(uint，**可选**、缺省 0)、`scene_id`(uint)、`scene_name`(string)、`objects`(array)。
除 `scene_sub_version` 之外不新增任何顶层字段，`scene_schema_version` 仍为 `1`（**Q109**）。
`scene_sub_version` 是"四元组身份"（§5.4）的第四项，规则只有两条：**只有 `setup` 阶段的浏览器本地内容改动推进它**（§4.6 资产面板加物、层级面板本地删除，每次成功 +1），
**§3.9 那四条 `scene_version` 推进路径每推进一次就把它归 0**（Agent 写回带回的现场一律从 0 重新数）。
缺失、非 uint、为 0 都合法且等价：按 0 读，`toJSON()`／导出／`/getWorld` 只在 **>0** 时写出这个键 —— 没人编辑过的现场因此逐字节回到它被读到的样子，摘要不会为一次不存在的改动移动。
3.2 `objects[]` 每项：`object_id_in_scene`(uint，必填唯一)、`object_name_in_scene`(string，**可缺省**、可重复；缺失或不是字符串时层级面板回退 `object_<id>`，见 §4.7)、`components`(array；不是数组时按空数组处理并告警，物体仍会创建)。
3.3 `components[]` 每项：`component_type`(string，区分大小写)、`properties`(object)。
3.4 解析端要求：**未知 `component_type` 必须忽略并在控制台告警**，不得使整场景加载失败；**同一 `component_type` 重复出现时只保留第一次出现的那个**（开放项 O5 的 v1 口径），其余同样告警忽略。`properties` 内未声明的字段**不参与本期的类型化读取，但不等于丢弃**——必须按 §3.8 随原始字典整体保留在内存中。字段命名以文件为准（存在 `object_id_in_scene` 蛇形与 `forceOverrideColor` 驼峰混用），解析端按字面匹配。
3.5 `id` 允许不连续，**不得假设从 0 起或连续**：真实样例 `docs/ur5e_table_empty.json` 恰好是 0~6 连续，而 `tests/fixtures/scene_example.json` 缺 4，§5.5 的 id 分配（已释放 id 不复用）也会留下空洞。三种形态都合法。
3.6 校验规则（浏览器执行，失败项按 §5.6 报错并跳过该物体，其余物体继续加载）：
 - `object_id_in_scene` 缺失、非 uint、或重复 → 该物体拒绝。
 - `Transform.parent` 必须指向已存在的 `object_id_in_scene` 或为 `null`；必须无环。
 - `parent` 为尚未出现的 id 时（前向引用）：**应支持两遍解析**（先建表后连父子），不得因顺序而失败。
 - `rejected_objects[]` 的 `index` **一律是提交数组 `objects[]` 的下标**，与 `object_id_in_scene` 无关：条目本身畸形时没有 id 可用，只能靠 `index` 定位；因此"父物体在层级阶段被丢弃"的那批也**不得**改用"存活节点列表的下标"（**Q59**）。

### 组件定义

| component_type | properties | 语义 |
|---|---|---|
| `Transform` | `position[3]`、`quaternion_xyzw[4]`、`scale[3]`、`parent` (uint\|null) | 物体相对**父物体**的局部位姿。缺字段回退并告警：`position`=[0,0,0]、`quaternion_xyzw`=[0,0,0,1]、`scale`=[1,1,1]（**任一分量 ≤ 0 时整体退回 [1,1,1]**）、`parent`=null（`0xFFFFFFFF` 亦映射为 null，§2.5）；完全没有 `Transform` 的物体按单位位姿处理 |
| `MetadataRef` | `path`、`type`(`model`\|`robot`) | 指向 OLS 里的一份资产，是几何的唯一来源。**`type` 决定向哪个 id 空间取数**：`model` → `GET {OLS}/model?id=`，`robot` → `GET {OLS}/robot?id=`。`type` 缺失时由 `path` 的形式判定（裸数字 `$LIBRARY_SERVER/<n>` 恒为 `model`，`$LIBRARY_SERVER/robot?id=<n>` 为 `robot`，`scene?id=` 一律拒绝）；`type` 为其它值（如 `scene`、`urdf`）→ 告警后丢弃该 `type`，仍按 `path` 形式判定，物体照常加载。示例 `$LIBRARY_SERVER/29`（桌面）；**真实库 model id 从 1 起，没有 0**（§6.11、**Q51**） |
| `MeshRenderer` | `forceOverrideColor`(`#RRGGBB` 或 `#RRGGBBAA`) | 有则用该纯色覆盖材质**并丢弃贴图**；末两位 `AA` 写入材质 `opacity`（小于 1 时同时置 `transparent`），只写 6 位保持不透明；无则使用模型自带材质。判据是 `#` + 6 位或 8 位十六进制（大小写不限），故 `FF0000`、`#FF00`、`red` 都算非法 → 告警并保留原材质（§6.8） |
| `Semantic` | `semantic_name`、`category`(`object`\|`surface`…)、`support` | 语义标签，用于展示与层级面板搜索；`support` 语义尚未定义，本期**不参与任何计算，但必须按 §3.8 原样保留在内存中** |
| `Light` | `type`(`Ambient`\|`Directional`\|`Point`\|`Spot`)、`intensity`、`color` | 光源。`type` 未知或不是字符串按 Ambient 回退并告警；`intensity` 非有限数值按 `1`；`color` 缺失或不可解析按白色。**本期不读方向/射程类附加字段**：`Directional` 与 `Spot` 沿该物体局部 **-Z** 发光，朝向完全由 `Transform` 决定 |
| `Camera` | `type`(`rgb`\|`rgbd`)、`fov`(度，垂直)、`near`、`far`、`resolution`[宽,高] | 相机实体，`/captureScene` 与 `/get2DAABB` 的目标。缺省值 `fov 60`/`near 0.1`/`far 1000`/`resolution [640,480]`，且 `far ≥ near × 10`；归一化只发生在渲染层读取时，**不回写世界数据**（**Q12**） |
| `RobotDriver` | `joint_sequence`(string[])、`joint_limits`({joint: [min,max]})、`initial_joint_positions_rad`({joint: 弧度})、`now_joint_positions_rad`({joint: 弧度})、`available_connect_joints`(string[])、**`role`**(`"arm"`\|`"passive"`，**缺省 `"arm"`**) | 机器人；关节角顺序 = `joint_sequence` 顺序，单位弧度。**位姿有两份、按关节合并**：`initial_joint_positions_rad` 打底，`now_joint_positions_rad` **只覆盖它点名的那几条关节**，其余关节仍取 `initial`；两份都没点名的关节保持在当前角度不动。`now` 缺席、空对象、值为 `null` 或非对象（数组也算）时完全不参与。合并结果在 URDF 树建好时**逐个关节落地**（载入场景看到的就是该停放位姿），轨迹播放后被每帧覆盖。渲染层的重建签名与落地读的都是**合并后的那一份**，调用方用 `/updateItem` 重写任一份字典里相关的关节即可刷新机械臂（或其他带 `RobotDriver` 的物体）的关节位置（**Q75**）；播放中收到写入会先退出播放态、再按文件声明的位姿重新停放关节（**Q76**）。`available_connect_joints` 本期只随原始属性保留。关节名必须与 URDF `<joint name>` 一致；URDF 的 `<mimic>` 由驱动层派生，跟随关节的正常值来自源关节而非自身指令，两份位姿共用同一套限幅与派生（§6.4、§8.4、**Q69**）。**`role` 只决定"这台 URDF 要不要按机械臂画装饰"**（**Q107**）：`"passive"` = 普通非臂 URDF 模型（微波炉、抽屉柜这类 `type: "model"` 资产），**不画轨迹路点（§7.3 第 1 条）也不画段末幽灵（§7.3 第 7 条）**；其余一切与机械臂完全一致 —— 仍按 `joint_sequence` 被 clip 逐帧驱动、仍收 `initial`/`now` 的合并位姿、仍能在 Details 里逐关节**只读查看**、仍可作为 `RobotAttachment` 的挂载父级或子级。**字段缺失、值不是 `"passive"` 的其它字符串、非字符串值一律按 `"arm"`**。`"arm"` 与 `"passive"` 之外的取值**不告警**（宽容口径，同 §4.2 `MetadataRef.type` 一族） |
| `RobotAttachment` | `parent_object_id`(uint)、`parent_joint`、`parent_link`、`child_link`、`connection_type`、`transform`{`position`、`quaternion_xyzw`、`reference_frame`} | 装配关系（如夹爪固定在机械臂腕部）。存在时 `transform.position` / `transform.quaternion_xyzw` **覆盖** `Transform` 里的同名字段（不论后者是否存在），`scale` 与 `parent` 仍按 §4.1 的 `Transform` 生效。三种情况退回"把 `Transform` 当普通变换"并告警：`parent_object_id` 不是合法 uint32、`parent_object_id` 与 `Transform.parent` 不一致、`transform` 既无 `position` 也无 `quaternion_xyzw`。`reference_frame` 决定**挂在哪里**：`parent_link`（缺省，字段缺失或空串按它处理）时 `transform` 的数值按该 link 的局部坐标系解释，物体节点改挂到 link 的 THREE 节点下，于是机械臂的 FK 与轨迹播放都会带着它走；`parent_object` 时挂父物体节点、数值按父物体坐标系解释；未知取值告警后按 `parent_object` 处理；link 在该机器人的 URDF 里不存在同样告警回退到父物体节点。`parent_joint`/`child_link`/`connection_type` 原样保留、不参与计算（取值口径与实测依据见 `docs/场景文件描述.md` §4.8；**Q73** 修订 **Q66**） |
| `LocalAxes` | `length`(number，米，缺省 `0.1`)、`colors`(`{x,y,z}`，每项一个 CSS 色值，缺省 `#FF0000`/`#00FF00`/`#0000FF`)、`show_labels`(boolean，缺省 `false`)、`offset`(`[x,y,z]`，米，缺省 `[0,0,0]`) | **物体带此组件即在视口画出它自己本地坐标系的三根轴**（+X/+Y/+Z 等长，从该物体的局部原点出发）（**Q130**，逐字段取值见 `docs/场景文件描述.md` §4.9）。**几何口径**：轴挂在**物体自己的 THREE 节点**之下（与相机视锥线、灯光占位球同族），于是父链、URDF 正运动学、轨迹播放与 `RobotAttachment` 的挂载自然带着它走，没有任何一处每帧重算；**长度随父链 `Transform.scale` 一起缩放、不做补偿**；世界仍是 Z-up，四元数为单位阵的物体**蓝轴朝天**。**`offset` 把整个帧从物体局部原点移开**：三根轴从偏移后的原点出发，方向与长度不变；偏移随父链 `Transform.scale` 一起缩放，与 `length` 同族。**它是编辑层**：识别用 `userData.localAxes` 一面身份标志：`renderer.ts:isEditorOnly()` 认这一族，`hideOverlays()` 与 `expandGeometry()` 两条通路覆盖到它（前者把编辑层从采集里按掉、后者跳过编辑层，轴不会把自己量进 AABB）；拾取那一路在 `selectable()` 的逐父链判断里用同一个标志项（`pick()` 的 `intersectObjects(…, true)` 是**递归**的，与视锥线"从来不是拾取目标（**Q71**）"靠 `node.userData.cameraMarker` 是同一个判断项）。于是 `/captureScene`（§5.10）、视口截图（**Q108**）与录制的默认态（**Q121**）里都没有它，`worldAabb()`、`/get2DAABB`（§5.11）、`frame()` 与选中描边都不因它改变，它也不会成为选中项。**判据与 §4.8.6 第 4 条同一条**：新层靠既有判据被排除，而不是给判据开新口子。**显隐**：**不跟选中态走**（与 **Q105/Q106** 管视锥线与占位球的口径相反 —— 勾上就是全场带组件的物体一起亮），**开关有两处**（**Q132**，见 §4.8.8）：`features.json` 的 `viewport.localAxesToggle`（§9.3）管工具栏那枚「显示坐标轴」勾选框（**Q133**）在不在，**勾选框管画不画**，判据 `logic.ts:localAxesEnabled(features, checked)` = 键 && 勾选，**默认不勾**；键关 ⇒ 宿主消失**且这一层不画**。分工写死：**文件声明哪些物体有帧，勾选框决定这一页看不看**，**没有**任何按物体的界面入口，撤掉单个物体的轴仍然只有删组件那一条路。**缺省与回退**：`length` 不是有限正数 → 告警并按 `0.1`；`colors` 判据同 `Light.color`（`#` + 3/6/8 位十六进制，大小写不限，8 位末两位写进材质 `opacity` 并在小于 1 时置 `transparent`，同 `forceOverrideColor`），整体不是对象、缺键或色值非法时**只有那一轴**回默认色并告警；`show_labels` **只有显式 `true` 生效**，其它值（缺失、`false`、字符串、数字）按不画且**不告警**；`offset` 不是 3 元素数组、或任一分量不是有限数 → 告警并按 `[0,0,0]`，**键缺失不告警**（§3：字段缺失按默认值回退、不是错误）。无 `MetadataRef` 的物体照样画：只带 `Transform` + `LocalAxes` 的物体是"纯坐标系标记"，**不违反 §3.7**。`properties` 添进该物体的**重建签名**，`/updateItem` 改了它即在该物体上落地、不需重载现场。**本期不读**的候选附加字段 `link`（改画某条 URDF link 的帧）与 `axes`（只画其中几根）按 §3.8 随字典原样保留，实装它们不改本行字段名 |

3.7 无 `MetadataRef` 的物体（如示例中的 Light、Camera）：只创建空节点/对应组件可视化，**不得生成占位立方体**。
3.8 **原始属性必须入内存**（**Q6**）：解析时必须为每个物体/组件保留一份**未经裁剪的原始 `properties` 字典**（含 `Semantic.support` 等本期不参与计算、以及 §3.4 中未识别的字段），挂到内存模型上（如 `object.rawComponents`），并通过只读访问接口可查。目的：后续功能（语义查询、抓取属性、外部读取）可直接取用，不必改解析器。**要求**：字段不得被丢弃、不得改名、不得做数值换算。落地形态：`objects[]` 的 `rawComponents` 原样保留整份组件列表（含未知 `component_type`），已识别组件的类型化视图（`meshRenderer`/`semantic`/`light`/`camera`/`robotDriver`/`attachment`）存的也是整个 `properties` 字典而非逐字段挑选，因此 `GET /getWorld` 回读能得到提交进去的原始数值。`docs/ur5e_table_empty.json` 是真实数据，`tests/fixtures/scene_example.json` 是占位示例，两种输入下解析端都须按 §3.6 对缺字段回退，**不得假设字段一定存在**。
3.9 `scene_version`：**由浏览器维护的单调递增计数**，接口可带 `expected_version` 做乐观并发（§5.7）。**这四条路径是 `scene_version` 的唯一推进者；浏览器侧的内容改动（§4.6 资产面板、§4.8 的 ② 类）一律只推进 `scene_sub_version`，不碰这里**（**Q109/Q114**）。反过来也一样：这里每 +1，`scene_sub_version` 归 0（§3.1）。四条路径的取值口径（实现见 `world.ts`，手动 Load 与采纳见 `main.ts`）：
- `/updateItem`、`/addObject`、`/removeObject`：成功后在当前版本上 **+1**，并随广播发出（§5.4）。
- `/updateWorld`：生效版本 = **当前版本 + 1**，与提交 json 里写的 `scene_version` 无关（**Q109**：多余的自增不保留）。写 json 里的数字既不能把时钟往回拨，也不能让它一次跳两格；`/commitWorld`（§5.9、§6）沿用同一条"恰好 +1"的规则，并额外要求 candidate 的 `scene_sub_version` 为 `0`。
- **用户手动 Load Scene**：走同一条解析管线但**不经过 gateway**，生效版本就是文件里写的那个（不本地自增），所以回读与文件一致。
- **第二个浏览器采纳他方世界**（§5.4 `adoptWorld`）：直接采纳权威视图 `/getWorld` 已自增后的版本，不重复 +1。

---

## 4. 功能需求

### 4.1 加载场景（两种方式，行为必须一致）
- **a) 用户手动**：顶部工具栏保留 **Load Scene** 按钮 → 选择本地 `.json` 文件 → 浏览器本地解析（§3）→ 按 §6 拉取模型 → 渲染。**不经过 gateway**。
- **b) 调用方**：`POST /updateWorld` → gateway **定向**发给绑定浏览器（§5.2，不广播）→ 浏览器清空原场景并加载。
- 两种方式共用同一条应用管线；b) 与 a) 成功后 gateway 都持久化该 json（§1.4）。
- 加载后 UI 反馈：层级树刷新、状态栏显示 `scene_id/scene_name/scene_version/object 数`；版本读数在 `scene_sub_version` 非 0 时追加 `.{scene_sub_version}`（§3.1、**Q114**）—— Agent 写回必然把子版本归 0（§3.9），诊断时一眼分清。

### 4.2 场景更新（调用方唯一写入路径）
- `POST /updateItem`：**除 `object_id_in_scene` 外可改该物体的一切信息**，包括 `parent`、位姿、增删组件。
  语义 = **全量替换**（`object_name_in_scene` + 整个 `components` 数组替换），不做字段级 merge，避免深合并歧义。删除某组件 = 不传该组件。
- `POST /addObject`：向当前世界新增一个物体。
- `POST /removeObject`：删除指定物体。**默认级联删除整棵子树**；带 `detach_children=true` 时只删自身，其直接子物体的 `parent` 改挂到被删物体的原 `parent`（无则为 `null`）。级联/摘除的 id 列表在响应中回传（§5.9）。
  删除**只有调用方接口，没有 UI 入口**（§4.7 已取消用户编辑能力）。
- 上述三类写入 + `updateWorld` 都必须实时广播给所有已连接浏览器（§5.4），使多端视图一致。

### 4.3 轨迹加载与播放
- 两种来源：用户手动 Load Traj（文件选择）与 `POST /updateTraj`。**两条来源写进同一份列表**（**Q100**，§7.3 第一条）：手动 Load Traj **把自己整批追加进去并选中这批的第一条**（下拉框里这几条各用自己的文件名标出），一次 `POST /updateTraj` **整批替换**列表并回到第 0 条。**手动通路可以多选**（**Q104**）：`#traj-file` 带 `multiple`，一次选中的 N 个文件**按选择顺序**追加成 N 条，**全有或全无** —— 任一条解码失败就整批拒绝、内存里已有的列表一字不动、选中项也不变（与 §5.3 调用方那一批同一个口径，**Q49/Q99**）；只选一个文件时与单选完全一样。
- 成功后浏览器**进入播放模式**，页面底部出现进度条播放条（§7.3）。
- 播放能力复用现有 timeline 系统（`editor/src/timeline.ts` + `main.ts` 播放循环 + `logic.ts` 的 `timelineDueIndex/timelineDelayMs`），但需按 §8 适配 TRAJ v2。

### 4.4 拍照
- `GET /captureScene`：由指定相机离屏渲染，**结果原样返回给发起调用的那个 HTTP 客户端**（协议 §5）。
- 深度图**必须米制**。
- **相机光学系（Q70/Q74）**：`Transform.quaternion_xyzw` 描述的是相机自身的坐标系，**局部 +Z = 光轴前方、+Y = 画面上方、+X = 画面左方**（仍是右手系：`x̂ = ŷ × ẑ`）。世界仍是 Z-up，所以**单位四元数的相机朝天**，俯拍桌面必须显式给安装姿态（如绕 X 轴 180°）。视锥 gizmo、`/captureScene`、`/get2DAABB` 三者共用这一份约定，任何一处单独"补 90°"都会破坏 §12.5 的像素互校。
- 渲染内容排除一切编辑器辅助图层：包围盒 overlay、选中描边、collider 线框、加载标记、云台网格、相机视锥线（**Q71**：相机标记只剩视锥线，不再画圆锥）、物体本地坐标轴（`LocalAxes`，**Q130**；它与视锥线同一族身份，判据见 §3 组件定义那一行与 §4.8.6 第 4 条）。
- **视锥线与灯光占位球的显隐（Q105/Q106）**：两个 **`features.json` 布尔键** —— `viewport.cameraFrustumAlways`、`viewport.lightMarkerAlways`，**默认都是 `false`**。`false` = 跟着选中态走（只有选中该相机/灯光时才显示它的视锥/占位球）；`true` = 无视选中、整场常驻。落点：`renderer.ts:applyOverlayVisibility()` 在每次 `select()` 末尾统一遍历 `nodes`，把 `userData.cameraMarker`（相机视锥线）与 `userData.lightMarker`（灯光占位球）的 `visible` 设为 `该键 || 这个物体就是当前选中项`。**灯光标签 Sprite 不归这两个键管**：它只在选中时出现，是选中态的附庸。**关断是 `visible=false` 不是删除**：节点仍在那棵子树里，`/getWorld`、层级树行数、`#panel-count`、`/get2DAABB`、拾取射线全都不变。**对外通道一条都不动**：视锥线与占位球本来就在上面那条排除清单里，`isEditorOnly()` 认它们、`hideOverlays()` 挡它们，所以**键为 `true` 时 `/captureScene` 与 Details 的相机预览仍然看不见它们**（判据见 §12.14 的两侧逐字节相同）。这两个键**没有 DOM 宿主**（管的是 scene graph 而不是控件），与 `playback.ghostPose`（**Q96**）同族，`parity_inventory.py` 判 `literal` 的依据是 `main.ts:applyFeatureVisibility()` 里那一次 `enabled()` 调用。**"这颗球此刻亮不亮"在浏览器里没有任何像素口径**，所以 `applyOverlayVisibility()` 每次算完显隐写一行 `console.debug("Viewport markers …")`（`markers` 由 `renderer.markerStates()` 从 scene graph 回读，关着的也列进去），与 §12.13 那行 `Trajectory waypoints` 同族 —— 判据与取证都读它（§12.14）。
- **地面网格与世界原点坐标轴的显隐（Q131/Q135）**：`features.json` 两个键控制渲染窗口中是否显示世界原点坐标轴、是否显示网格。**这两个键管的是屏幕上有没有，不是采集里有没有**：`THREE.GridHelper(80, 80, …)`（透明度 0.72）与 `THREE.AxesHelper(1.5)` 是构造时 `scene.add` 的两件家具，`type.endsWith("Helper")` 让 `isEditorOnly()` 认出它们，所以 §4.4 上面那条排除清单、`/captureScene`（§5.10）、`worldAabb()`/`/get2DAABB`/`frame()`、视口截图（**Q108**）与录制默认态（**Q121**）都不含它们。键关掉 ⇒ 屏幕上按掉；键打开 ⇒ 屏幕上回来；**别的什么都不会变**。落点是 `renderer.ts` 的 `setGridEnabled()`/`setWorldAxesEnabled()` 各写自己那一个 `visible`，加上构造时按同名布尔起一次 —— 这两个 helper 挂在 `scene` 之下、不在 `markerNodes()` 的行走范围，`applyOverlayVisibility()` 碰不到它们，`select()` 不会替一个关掉的键把层点亮（**Q105/Q106** 那族"选中才亮"的规则在这里不适用）。**默认都 `true`**。**没有 DOM 宿主**，与 `playback.ghostPose`（**Q96**）、`viewport.cameraFrustumAlways`/`viewport.lightMarkerAlways`（**Q105/Q106**）同族，`parity_inventory.py` 判 `literal` 的依据是 `main.ts:applyFeatureVisibility()` 里那两次 `enabled()`。**这一族的"此刻亮不亮"在浏览器里没有像素口径**，所以两个布尔一起写进 `applyOverlayVisibility()` 那行 `console.debug("Viewport markers …")`（由 `logOverlayState()` 单点输出，两个 setter 也走它），§12.14/§12.20 同一条教训。**三根轴站在哪个系里（Q135）**：`THREE.AxesHelper` 画的是它自己局部系的 +X/+Y/+Z，它挂在 `scene` 上、不在 `simWorld` 里面；口径是**同一个常量管两处**：`renderer.ts` 把 `simWorld` 那一句 `-Math.PI / 2` 提成 `export const SIM_WORLD_TILT`，`axesHelper.rotation.x` 赋的就是它（`simWorld.rotation.x` 也是）—— 换算后文件 +X→渲染 +X、+Y→渲染 −Z、**+Z→渲染 +Y（朝天）**，于是**红=文件 +X、绿=文件 +Y、蓝=文件 +Z**。地面网格**不需要**这一转：它自己的 XZ 平面正是文件的 `z=0` 地板。**颜色一字未改**：three 内置的每轴顶点色渐变（X 红 `1,0,0`→`1,0.6,0`、Y 绿 `0,1,0`→`0.6,1,0`、Z 蓝 `0,0,1`→`0,0.6,1`，`vertexColors` + `toneMapped:false`，经 `outputColorSpace=SRGBColorSpace` 输出）随几何一起被转，改的从来不是"哪根什么色"而是"哪根朝天"。**不做**：不给网格做间距/尺寸/颜色常量、不给坐标轴做长度常量（`80, 80` 与 `1.5 m` 是画法不是配置项）、不给它们配工具栏勾选框（要的是配置里的两个键）、不把它们挪进 `worldAabb()` 或任何对外通道。**判据见 §12.27。**
- **视口截图按钮（Q108）**：截取当前渲染窗口中的内容（不含工具指示器、灯光占位模型等），送入剪贴板。**截的是"当前视口相机所见"，与 `/captureScene` 是两件事**：后者是对外通道、走 HTTP、按某个物体的 `Camera` 组件的光学系离屏渲染（Q70/Q74）、结果交给发起调用的客户端；本条是**纯本地动作**，用编辑器自己的 orbit 相机与画布尺寸，产物**只进剪贴板**，不发请求、不落盘、不下载。**排除判据一字不改、直接复用同一条通路**：`renderer.ts:hideOverlays()` 按 `isEditorOnly()` 遍历 `scene` 把可见的编辑层临时按下去 ⇒ 选中描边（`Box3Helper`）、段末幽灵（**Q96/Q98** 的 `userData.ghost`）、轨迹路点（**Q101** 的 `userData.waypoint`）、相机视锥线、灯光占位球与类型标签（**Q105/Q106**）、地面网格与坐标轴（`GridHelper`/`AxesHelper`，靠 `type.endsWith("Helper")` 兜住）**一律不进截图**。**两个常驻 flag 在截图这条路上不起作用**：`viewport.cameraFrustumAlways`/`viewport.lightMarkerAlways` 管的只是**屏幕**（§12.14 ⑤），所以**四种 flag 组合 + 同一相机姿态 + 同一选中态截出来的 PNG 必须逐字节相同**。**取像素的方式是离屏读回，不是 `canvas.toDataURL()` 直读 WebGL 画布**（那要求 `preserveDrawingBuffer: true`，本渲染器没开）：`renderer.captureViewport(canvas)` 把当前视口相机渲进一张与 **drawing buffer 同尺寸**（CSS 尺寸 × `pixelRatio`，上限 2）的 `WebGLRenderTarget`，`readRenderTargetPixels` 读回，再按 §5.10 那套**翻行**（GPU 第 0 行是画面底行）写进调用方给的 2D 画布 —— 与 `renderSensorFrame()` 是同一条通路，只是相机换成 `this.camera`、尺寸换成画布自身。**那张目标的 `texture.colorSpace` 必须显式设成 `SRGBColorSpace`**：线性目标读回来的字节是线性值，画面会整片偏暗（真机 A/B 同一帧：改成 `LinearSRGBColorSpace` 后桌面木质的棕色参照像素从 **2976 个掉到 0 个**、最亮通道从 **146 掉到 74**，而"亮像素"计数只从 4997 掉到 3351，担不起判据；见 §12.16 ② 与坑 82）；§5.10 的 `/captureScene` 正是按 mode 设这一行的（rgb 设 sRGB、depth 设 `NoColorSpace`）。剪贴板由 `main.ts` 负责：`snapshotCanvas.toBlob()` ⇒ `navigator.clipboard.write([new ClipboardItem({ "image/png": blob })])`，成功提示报出像素尺寸，失败（非安全上下文、无 `ClipboardItem`、用户撤销剪贴板权限）走 `showError` 那条既有 toast 通道。**feature 键 `viewport.snapshot`**，宿主就是工具栏那个按钮（有真实 DOM ⇒ `parity_inventory.py` 判 `literal`）；关掉它 = 按钮不见，没有别的连带。**零副作用**：截图不写世界、不动播放头、不改选中态、不产生 `browser.state_sync`，`scene_version` / 层级树行数 / `#panel-count` / `/getWorld` 一字不动，§5.9 的接口面与 `audit_parity.py` 的探针**不增不减**。判据见 §12.16。

### 4.5 包围盒（与 `/get2DAABB` 是**同一个功能**，无独立入口，已合并）
- **嵌套物体的点选按世界节点归属（Q152）**：机械臂、夹爪、相机可以为运动而挂在同一棵 THREE 子树里，但点选必须沿真实注册的世界对象节点回溯；模型子网格继承的 `userData.objectId` 不能把相机合并进机械臂，且构建阶段不得覆盖已有对象身份标志。
- 浏览器按物体 id 计算世界坐标 **3D AABB** → 投影到指定相机 → 返回**像素 2D 框**，由调用方自行绘制。
- 超出视锥/部分出画：**与图像边界求交裁剪**（clip），返回 `clipped=true`。
- 完整可见性（是否被其他物体遮挡）：v1 **不做深度缓冲遮挡剔除**，仅做几何裁剪（**Q4**）。
- **相机的包围盒永远不再包括视锥，只用它自己的 Mesh 算（Q105）**：`renderer.worldAabb()` 按 §4.4 的排除清单递归**自身渲染几何**（**Q50**）；**屏幕上的选中描边读同一个函数**：`THREE.Box3Helper(worldAabb(objectId), 0xfacc15)`，于是"相机的盒 = 自身的 Mesh"在 `/get2DAABB`、`frame()`、屏幕描边**三个表面上一次成立**。**推论（写死在此）**：一个物体的渲染内容**全是**编辑层时（只有 `Light` 或只有 `Camera` 组件、没有网格），`worldAabb()` 回 `null` ⇒ **没有选中描边**；此时屏幕上的选中反馈就是那颗占位球或那段视锥线本身（按 Q105/Q106，选中才出现）。描边随物体移动每帧刷一次盒子（播放帧与调用方写入的关节位姿都会挪它），仍挂在 `scene` 而不是某个节点之下（§4.8.3 那条 Q50 教训不变），仍被 `isEditorOnly()` 按身份排除在截图与 AABB 之外。
- **一个坏零件不许毒死它的父级（Q141 立、Q143 改判后的现行口径）**：`worldAabb()` 那棵递归走的是**场景图的子树**，而 `RobotAttachment` 把夹爪挂在臂的 link 节点之下（`worldParentOf`）—— **Q148 起那棵递归止于另一个物体 id 的节点**，所以并进父级盒的只剩**同一个物体自己的零件**（夹爪的几何从此只进夹爪那个盒，见 §4.8.6）；**跨物体那条毒路被物界判项掐断，同物体这条仍靠 `isFiniteBox`**，两条防线管的是两个尺度。病根掐在加载那一侧（§6.5：读整份文件、把声明排到第一条面语句之前再交给 `OBJLoader`），出厂库那 6 份"面引用了尚未声明的顶点"的 OBJ 从此解析出**完整**网格、不再产生一个 NaN 顶点；**并集里 `isFiniteBox` 那个判项原样保留**（`expandGeometry` 里）—— 盒是并集，一个 NaN 角就把它整个变成 NaN，而这一层管的是**规范化没看过**的坏零件（从别的路径挂上去的、加载之后被改坏的、以后出现的新格式），**跳过那一份的量测、不跳过它下面挂着的零件**（NaN 网格的子节点是它们自己的几何）。于是"渲染内容全是坏零件"的物体回 `null`（进 §4.8.6 的 `skipped`），而不是回一个 NaN 的盒；`frame()` 因此永远写不进 NaN 的相机，**整台视口不会因为一个坏零件而全线脱靶**。（判据 §12.37）

### 4.6 资产面板（左栏，数据全部来自 OLS）
- **预设场景**：`GET {OLS}/list?type=scene`；点击 → 浏览器**清空原场景** → `GET {OLS}/scene?id=<scene_id>` → 走 §4.1 同一条加载管线 → 成功后同样上报并收到四元组 `scene.applied` 广播，gateway 持久化该 json（浏览器回传该 json 给 gateway，见 §5.8）。**切换改变 `scene_id` ⇒ 新会话**（§5.15、流程文档 §1.2 第 4 条）。
- **预制模型**：`GET {OLS}/list?type=model`；点击 → **把它摆到桌面上**（**Q134**，口径见 §4.6.1）：现场为空时先走预设场景的管线载入 **`GET {OLS}/scene?id=1`**（基线场景 —— 没有工作台就没有桌面可放），再在桌面顶面**随机取一个不与现场任何其他物体包围盒相交的落点**（**Q137**：机械臂那一整套不算"其他物体"，见 §4.6.1 的碰撞集），写入 `Transform`（`position` = 落点、`quaternion_xyzw` = 出厂场景写给每一个桌面物体的那个 +90°、`scale=[1,1,1]`、`parent=null`）+ `MetadataRef`（指向该 `model_id`）+ `MeshRenderer` + `Semantic`（`support` = 工作台那个物体的 id）；id 由浏览器按 §5.5 的 `setup` 期本地分配规则取（**以 `X-Next-Object-ID` 为种子**，不得只从文档现有 id 推），重复点击=重复添加，取不到空位则**拒绝并提示**，不得把它丢在原点。
- **机械臂**：`GET {OLS}/list?type=robot`，元数据走 `GET {OLS}/robot?id=<robot_id>`、文件走 `/file?type=robot&…`（**独立 id 空间**，见 §6.11、**Q51**），写入世界的 `MetadataRef.path` 是 `$LIBRARY_SERVER/<robot_id>` 且 `properties.type` 写 `robot`（§6.1）。**这一大类里点中的是哪一型，不由界面猜、由数据判**（判据与落点全在 §4.6.1）：是**机械臂** ⇒ 替换现场里的每一台臂（现场没有臂则新增一台，现场为空则先载基线场景）；是**夹爪** ⇒ 替换挂在臂上的那些末端执行器（现场没有臂可挂就拒绝）；是**相机** ⇒ 替换现场每一台相机，**保留作者写的视点位姿与内参**，只换元数据（现场本来没有相机 ⇒ 按上一条摆上桌）；其余（含整个 `type=model` 大类）⇒ 道具。
- **上面三条里改现场的那两条挂阶段闸门**（**Q114 ②**；弹窗文案 **Q122/Q127**，界面上不出现"租约／接管／现场"这类内部词）：点预设场景（整场替换）与点模型／机械臂（按 §4.6.1 摆放或替换资产，**Q134**）都只在 `setup` 阶段执行；会话进入 `agent_owned` 后它们**不改动任何东西**，点击弹出弹窗**并当场问要不要开一个新会话** —— 正文只剩最短的两行（**Q122**）：第一行说这一步为什么被拦下（`agent_owned` ⇒ "任务进行中，场景暂时不能改。"；会话里已有 Agent 任务 ⇒ "这个会话里有任务正在进行。"），第二行按被拦下的那次点击问要做什么（"新建会话并重新打开场景文件选择框？"／"新建会话并重新加载预设场景「名字」？"／"新建会话并重新添加「名字」？"），**两个选项排在正文下方**（`新建会话` = `data-action="phase-new-session"`，`取消` = `data-action="dismiss-phase-block"`；`.phase-block` 是单列 grid）（弹窗，不是 toast）。**接受即 `POST /sessions`**（走 §9 第 3 项同一道门：`camera_id` 由 Brain／Gateway 从候选清单里自动选，浏览器不让用户点选相机，**Q123**），成功回到 `setup` 后**重做刚才那一步**（`Load Scene` 那一步是重新打开文件选择框；这一次重开骑在用户那次点击的激活窗口上，网关慢过窗口时浏览器会挡下程序化点击，再点一次即直接生效）；点 X 收起即丢弃这次待办。**开新会话绝不打断 Agent**：Gateway 从不替 Brain 取消任务，旧会话的计算照旧跑完；**弹窗与面板头部这两枚「新建会话」都带 `allowActiveTask`**（**Q128**）：任务还持有租约**不是浏览器预先拒绝的理由**，网关随之把租约搬进那个会话的上下文（`local_gateway.py` 的 `allow_active_task` 分支）。这道门仍拦的只有**状态待确认**（`newSessionView` 的 `blocked`，§8.3：新建会话不是解锁证据）与不带 `allowActiveTask` 的那次（发送命令前的自动开会话）；`409 task_busy` 由网关回，两句都只把当前状态念给用户。**判据是"点击必须有响应"**（R2）：入口的禁用态**不得**用 `disabled` 属性 —— 点击委托第一句就是 `if (target.disabled) return;`，真 `disabled` 恰好把响应吞掉；用视觉类 + `aria-disabled`，处理器照常跑到弹窗。**浏览不受影响**：`/list`、`/listCategory`、缩略图全是只读，两个阶段都照常。加物成功走的是 §4.8 的 ② 类通路：`scene_version` 不动、**`scene_sub_version` +1**（§3.1）、随后 `browser.state_sync` 整份上报（§5.8）（**Q134** 起这条通路还包含"就地改写那一个物体"，判据同一个：`World.updateLocalItem` 与 `World.removeLocalSubtree` 同样只动子版本，§4.6.1）。层级面板的本地删除（§4.7）同挂这一道闸门。
- 面板项显示 `id + name + category`（`/list` 带 `category`，如 `模型.抓取物`、`机械臂.六轴`），类别清单来自 **`GET {OLS}/listCategory?type=<type>`**（路由拼写以 `OLS接口.md` 与真实服务为准，**Q20**、O7）。**面板的骨架是"两级竖向层级栏 + 单列列表"**（**Q93**）：第一层竖向三大类 = 场景 / 模型 / 机械臂（即 `LIBRARY_KINDS` 的 `scene|model|robot`），第二层竖向列出**当前大类**的子类（`/listCategory` 的顺序，未归类项落 `未分类` 并排最后），右侧列表区**每行只放一个物体或场景**（缩略图 + 名称 + `id · category`），不是多列卡片网格，也不"三个大类叠成一页滚动"。
- **缩略图（Q5）**：`GET {OLS}/thumbnail?type=<scene|model|robot>&id=<n>` 返回图片字节，面板用 `<img src>` 直接引用即可（简单 GET、无自定义头，不触发 preflight，§6.9 约束不变）；取不到图（403/404）时显示占位格子并告警一次，**不得**因此隐藏该条目。
- **面板没有名称过滤输入框，也没有手动刷新入口**（**Q93** 撤销；`assets.ts:filterAssets` 已删除）。清单是 `start()` 里 `void refreshLibrary()` 自动拉的（三条 `/list` + 三条 `/listCategory`），进入页面仍然自动读取，代价是"不重进页面就拿不到 OLS 的新增"。读取失败仍只在面板内告警，**不得**改动当前世界（§12.7）。
- 移除所有上传相关能力（`Load Asset`/拖拽上传/依赖选择弹窗/删除资产）。

#### 4.6.1 摆放与替换的口径（**Q134**）
- **快速连续库操作（Q151）**：同一页面从资产面板发起的场景加载、机械臂/夹爪/相机替换与桌面摆放按提交顺序逐个执行。一个摆放在等待模型加载、桌面量测或 Gateway 上报时，后续操作不得另换 `state.world` 或与它并发改写；前一项失败后，队列继续执行后续项。

- **生效窗口**：本节的每一次写都骑在 §4.6 那条阶段闸门上 —— 只在**当前会话里用户第一次给 Agent 发消息之前**（`setup` 期）成立；进入 `agent_owned` 后点击照旧被弹窗拦下，**什么都不改**。判据是 `logic.ts:sessionPhase` 那一条（§4.8.2）。
- **元数据只走两条读**：`GET {OLS}/model?id=<n>` 与 `GET {OLS}/robot?id=<n>`（**两个互不相交的 id 空间**，§6.11）。两者的 `geometric_properties.AABB` 是**六个浮点、成对给出、每对不分大小**（出厂值里两个都可能为正，例：工作台 `[…, 0.6, 0]`、底座 `[…, 0.25, -0.25]`），所以唯一的读法是把第 `2k`/`2k+1` 个当**同一条轴的一对**取 min/max；不是六个有限数（缺项、字符串、只给两项）一律按**没有包围盒**处理并拒绝上桌。线上确实带 AABB，因为 `server.py` 把 `<name>_files.json` 那份 sidecar 并进了这两条响应（§6.3）。
- **角色由数据判，不由大类判**：① `robot_driver.available_connect_joints`／`available_connect_link` **非空** ⇒ **机械臂**；② 否则 `semantic_properties.tag` 含 `camera` ⇒ **相机**（这是 OLS 里唯一说出"这是一台相机"的地方，它不给相机任何专用组件）；③ 否则 `robot_driver.robot_attachment.properties.child_link` 有值 ⇒ **夹爪**；④ 其余（含整个 `type=model` 大类）⇒ **道具**。实测这两侧在元数据里是干净互斥的（十一台臂全是 `cj≥1` 且 `child_link=[]`，`robotiq_2f85`／`robotiq_2f140`／`xarm_vacuum_gripper`／`franka_hand` 四枚爪全是 `cj=0` 且 `child_link` 恰有一枚）。**真正会把夹爪判成臂的是"空表当缺失"**：一旦把 `cj=[]` 读成缺失就去拉那枚爪的 URDF，四连杆的树会现造出一张非空连接表，① 于是先命中 —— ① 排在 ③ 前面就是把这条因果写死。**另有一处必须分清的两个"driver"**：**场景文件里**那枚爪的 `RobotDriver.available_connect_joints` 写的是 `["robotiq_2f85_base_joint"]`（出厂七份场景逐字相同，**非空**），而**元数据 `/robot?id=2` 里同一字段是空表**。⇒ 现场查询（`findArmObjects`）只能靠"`RobotAttachment` 缺席"把爪从臂里挑出去，元数据判角色（`classify`）只能靠连接表非空，两者**不能互换判据**。
- **`RobotDriver` 的写法（用户口径原文）**「如果元数据中有 robot_driver 信息就直接用，否则从 urdf 里读」：元数据里有 `robot_driver` ⇒ **照它写**（只把 `robot_attachment` 摘出去，那是另一条组件、落在物体自己身上）；**没有才去读 URDF**，从树里造出 `joint_sequence`/`joint_limits`/`available_connect_*`。**夹爪那种"有 driver、但连接表明写为空"不算缺失**。道具（`type=model`）无论 URDF 里有多少关节都不参与这个判定，§6.11 的两个 id 空间不许跨界。
- **机械臂摆在哪**：读该臂元数据的 **`scene_one_arm[0]`**，且**只读这一条** —— 所有机械臂均假定为单臂。其中的 `robot_pose_percent_compared_to_table_length_center = [p0,p1,p2]` 读作**以工作台包围盒的长边中点为基准点、三个偏移量逐轴相加**（用户口径：表示机械臂**原点位置**相对于桌子**长边中点**的比例，以桌子相应的边长为基准，XYZ 均是如此；offset 三个分量都**加**在中点上；OLS 注释：桌子的长边指其包围盒上沿着 X+,Y−,Z+ 方向的那条边）：基准点是 `(cx, box.min.y, box.max.z)`，`offset = [p0·Lx, p1·Ly, p2·Lz]`，于是**落点 = `[cx + p0·Lx, box.min.y + p1·Ly, box.max.z + p2·Lz]`** —— X 沿长边方向、Y 从 Y− 那一侧**回到桌内**、Z 从桌顶往上；三个边长与中点都取工作台**在世界里**那条轴的跨度／位置（工作台在场景里是转过、搬过的，不能拿它自己的盒算）。**算出的这一点就是写进 `Transform.position` 的那一个数** —— 键名说的是「机械臂**原点位置**」，所以该臂自己的包围盒不把它抬高也不把它压低。**例**：OLS 预制场景 3 的工作台站在 `[0,0,0]`、盒 `1.5 × 0.75 × 0.6` ⇒ 长边中点 `(0, -0.375, 0.6)`，`[0.2, 0.3, 0.5]` ⇒ `offset = [0.3, 0.225, 0.3]` ⇒ **`[0.3, -0.15, 0.9]`**（双臂镜像那一台只在 X 取负 ⇒ `[-0.3, -0.15, 0.9]`；本仓库夹具把同一张桌子搬到 `x=0.525` ⇒ `[0.825, -0.15, 0.9]`）。**不做合理性检查**（用户口径「替换机械臂时无需检查放置的合理性」）：臂这一路**只算不判**，**不报碰撞、不换位置、不拒绝**；随机落点与碰撞避免**只管道具**（`type=model` 那一类），臂与底座走的是声明值算出来的那一个点。元数据里**没有** `scene_one_arm` 时用 `[0.2, 0.3, 0.5]`（出厂**每一条** `scene_one_arm` 都是这三个数）。
- **底座（`has_pedestal`）**：为真 ⇒ 再读 `pedestal_id`（**在 robot 那一侧的 id 空间里**，出厂值 4 = `robot_pedestal`，`/robot?id=4`），按「底座上沿贴机械臂承重底面」摆它：x/y 与臂原点相同，`position.z = 臂承重底 − 底座盒 max.z × z 缩放`。URDF 机械臂的承重底面从根 link 沿固定关节递归量其 `<visual>` 几何的最低 z；不跨越可动关节，因此悬垂手臂/手腕不会把支架压低。能量出该值时用它，否则回退整机盒 `min.z`；非 URDF 资产保持原口径。**缩放照声明写**：`pedestal_scale` 原样写进 `Transform.scale`，缺该键才用 `[1,1,1]`，**不放大、不缩小、不去够地面**。换新臂而它 `has_pedestal` 为假 ⇒ 删除现有支架。
- **随机落点（先随机、随机全落空再确定性扫遍桌面，Q144）**：范围是桌面顶面 `z = 工作台盒 max.z` 上的内接矩形 `[min + 半格, max − 半格]`，**半格按该物体自己的朝向盒**算 —— 桌面物体在场景里统一带 `TABLE_YAW = [0,0,√2/2,√2/2]`（+90° 绕 Z，出厂七个场景每一个桌面物体都是这个四元数），所以 footprint 是先按这个转角之后再量的。**这个盒对一份 OBJ 模型来说是它网格真实顶点量出来的那一个、不是元数据 AABB**（出厂 **25** 份顶层 OBJ 模型里 **9** 份的元数据 AABB 与它自己发货的网格不在同一个轴系上 —— 多数是 X/Y 互换、两份是 up 轴错位，`model 23` 那只杯子是用户复现穿模的那一份）：摆放要量这个盒来占位（`placement.ts:readAsset` 对 OBJ 模型多读一次顶点、`logic.ts:objVertexBounds` 量它；那一次读是**每个不同 OBJ 模型一份带缓存的文本请求**，被摆放的道具与它要避开的道具共用同一份缓存）。**两段式取点**：**第一段**最多 **64** 次**随机**采样（`attempts`，缺省 64）；随机全落空才进**第二段** —— 一个**完备的确定性扫描**。**判据**：一个盒子若能在桌面上放到某处，就一定能平移到**某条边贴着某个障碍或贴着桌沿**的那一点而仍不碰任何东西（往近边平移，第一次接触处即贴边处；`overlaps` 取严格相交，贴边不算碰撞）。所以第二段只量这些**贴边候选中心**：对每个障碍取 `obstacle.min − 半格` 与 `obstacle.max + 半格`（夹进内接矩形 `[min+半格, max−半格]`），并上两条桌沿与矩形中心，组成一个**有限且固定**的集合，**从中心向外**逐点试，第一个放得下的就落它（`placement.ts:edgeCentres` + `centreFirstEdges`，排序固定 ⇒ 可被单测钉住）。**这套取代了旧的 `(steps+1)²` 定格点扫（`centreFirstGrid` 删除）**：定格的格点间距本身就是能找到的最窄缝隙的下限，一条比格子更窄、或恰好落在两格之间的可用缝会被整格跨过——于是"桌面肉眼看还有位置"却报"放不下"，且成功只发生在随机相位偶尔撞进那条缝的时候（即"前几次失败、突然又成功"的非确定性来源）。改贴边之后**同一桌面每次点击必得同一结论**：缝隙只要在 AABB 模型里存在就被找到。`scanSteps` 从"每轴格数"降为**这一段的开关**（缺省仍 >0 ⇒ 会扫；显式 `0` 退回纯随机、第二段一次不走）。第一段随机照旧，保留"随机摆放"的自然散布。**只有两段都取不到才拒绝并提示"桌面上放不下"**（用户口径：「先尝试随机摆放，如果失败则在桌面上尽可能寻找能放下的位置，如果实在找不到才提示放不下」）。候选盒与**碰撞集**做**严格** AABB 相交判定（只贴边不算碰撞，正好坐在桌顶的物体不会把自己挡在桌外）。**碰撞集 = 现场里除工作台、除机械臂那一整套之外每一个读得出包围盒的物体**（**Q137**）：臂自己、挂在它手上的夹爪（`RobotAttachment.parent_object_id`）、挂在夹爪上的腕部相机（`Transform.parent`）三者一起出局 —— 闭包沿这两条边走，但**只沿 robot id 空间的成员走**（判据 `placement.ts:armFamilyIds(world)` 以 `metadataRef.type === "robot"` 过滤——臂每帧都在动，把动的东西当静态障碍会把桌面大半吃掉，这条只对真正属于臂的那几件成立）。**挂在这条链上的 `type:model` 道具——被手夹着的钢笔、挂在腕上的微波炉（model 30）这类——不是臂系成员，它留在碰撞集里**（**2026-11-05 穿模修复**：旧闭包不分 id 空间一路走到底，把手上的 `type:model` 道具也当臂系剔出碰撞集，于是新摆的物体直接压进微波炉／钢笔；它虽随臂移动，却在此刻占着桌面上一块真实空间，摆放必须避开）。底座（`category=support`）与吊在空中的那台相机**留在集里** —— 它们是现场的家具，不是臂的一部分。落点 z = `桌顶 − 盒底`，于是物体**正好坐在桌面上**：出厂 apple 的 `z = 0.637373`（= 0.6 + 0.037373）就是这么来的，`Semantic.support` 写工作台的物体 id、`Transform.parent` 仍是 `null`（出厂每一份场景的抓取物都这么写，两者不是同一件事）。
- **`RobotAttachment` 的两侧**（用户口径原文）「`RobotAttachment` 的 `child_link` 来自 `robot_driver.robot_attachment` 中的信息，`parent_link` 等信息来自对应的机械臂的 `robot_driver`」：`child_link` 取**夹爪**元数据的 `robot_driver.robot_attachment.properties.child_link`；`parent_joint`／`parent_link`／挂载位姿取**那台臂**的 `robot_driver`，其中 `available_connect_joints[i]`、`available_connect_link[i]`、`position[i]`、`quaternion_xyzw[i]` **按下标成一组**，取第 0 组（夹爪已挂在第 k 枚 joint 上时沿用第 k 组）。`transform.reference_frame` 有 link 时写 `parent_link`、没有时写 `parent_object`。**数据两头都没有时按"URDF 是一棵树"**：根节点可当 `child_link`（例 `robotiq_2f85_mount`），叶子节点当 `available_connect_link`（例 `wrist_3_link`）—— 这条只在元数据**没给 driver** 那一路成立，不给它开第二次 URDF 读。**现场文件里臂的 driver 只有 joint、没有 link**（出厂七个场景一律如此），所以换夹爪时 `available_connect_link` 必须由**臂自己的元数据**补上，补不出才退回现场那份；否则换完夹爪那条 attachment 会丢掉 `parent_link`。
- **换还是加**：换一律**就地改写那个物体**（`World.updateLocalItem`），因为挂在它身上的东西按 id 指它 —— 臂换了 id，夹爪的 `parent_object_id` 与腕部相机的父级就指向一个不存在的物体。点臂 ⇒ 现场里**每一台**臂都换成它（出厂双臂模板里那两台是同一个型号），第二台沿**长边方向（X）镜像**到长边中点的另一侧、y/z 两轴不动；点夹爪 ⇒ 替换所有挂在臂上的 `end_effector`；点相机 ⇒ 替换现场**每一台**相机并**只换 `MetadataRef`**（作者写的 `Transform` 与 `Camera` 内参一个字不动 —— 视点是这幅画面作者定的，不是资产定的）。现场里没有对应角色时才是新增：没有臂 ⇒ 新增一台臂（先载基线场景），没有夹爪 ⇒ 挂到第一台臂上（一台臂都没有则拒绝），没有相机 ⇒ 按道具那条摆上桌。
- **一切写都是 §4.8 的 ② 类**：`scene_version` 不动、**`scene_sub_version` +1**、随后整份 `browser.state_sync` 上报（§5.8）。新增走 `World.addLocalObject`、就地改写走 `World.updateLocalItem`、连带删除走 `World.removeLocalSubtree` —— 三条都是本地下限，没有任何一条碰 `scene_version`（§1.2、**Q114** ②）。组件表按 §3.8 的"同类型替换、其它原样保留"写：换臂不得丢掉那台物体已有的 `LocalAxes`，换相机不得重写为那个视点写的内参。判据见 §12.30。
- **碰撞盒改由浏览器量，不信元数据 AABB（2026-11-05 穿模修复，就地改上一条「随机落点」的盒来源）**：上面那句「摆放要量这个盒来占位」与「`readAsset` 对 OBJ 多读一次顶点、其余读元数据 AABB」的盒来源口径 **作废** —— 摆上去的道具会压进 URDF 邻居（id 25/31 这类出厂臂声明的盒比它当前关节位姿下的真实轮廓小），所以自 2026-11-05 起，**候选盒与碰撞集里的每一件都改量 `renderer.sceneAABB(objectId)`**：它与屏幕上选中描边（§4.5）、包围盒线框（**Q119**）、`/get2DAABB`（**§5.11**）是**同一条 `worldAabb`**，`renderer.worldAabb` 递归**当前场景图** ⇒ URDF 带**当前关节状态**（用户硬要求：可动的物体量盒要看现在关节摆成什么）、OBJ 带**真实网格顶点**、桌面物体那份 `TABLE_YAW` 转角也已烘进渲染出来的网格；`renderer.ts:sceneAABB` 再用 `toSceneBox` 把它转回世界文件轴系，与 `orientedBox` 产出的障碍盒同系可比。摆放走**先立起来再量**的两段：`World.placeLocal`（`world.ts`）在原点 `insertObject` → 交给 `BoundsMeasure.settle` 推一次 `syncScene` 并等这份几何加载落地（`renderer.readyObject`）→ 量 `sceneAABB` → 选到空位 `writeItem` 就地改写落点；整段只 `bumpSubVersion` **一次**（§4.8 ②，**Q114** ②），放不下的那次点击 staged 物体被删、计数**一字不动**，所以一次成功摆放 = +1、一次拒绝 = +0，不会像 `addLocalObject`+`updateLocalItem` 那样数两次。**失败的那一次还要在渲染层收尾（2026-11-05 孤立项修复）**：`BoundsMeasure.settle` 为了能量盒，把这件暂存物体连同它的原点位置一起 `syncScene` 进了**可见**场景图；`placeLocal` 抛错时只把它从 `World` 里删掉，**不会**自动回头清渲染节点——于是被拒的物体会**滞留在世界原点成一个孤立项**（直到下一次摆放或刷新才被顺带 reconcile 掉）。所以 `main.ts:chooseLibraryItem` 的 `catch` 分支在弹窗报错**之前**先补一次 `renderer.syncScene(state.world)`（外加 `updateStatus()`）把那个孤立节点收回：`syncScene` 按世界文档的 id 调和节点，文档里已不存在的暂存物就被 dispose。**回退**：渲染器还没把这件几何建出来、或建了却没有可量几何（含单测里没有 `BoundsMeasure` 的整条旧路）才落回元数据 —— OBJ 走 `logic.ts:objVertexBounds`（**Q144**：出厂 25 份顶层 OBJ 里 9 份元数据 AABB 与自己发货的网格不在同一轴系，`model 23` 那只杯子是复现穿模的那一份）、非 OBJ 读 `geometric_properties.AABB` 成对取值（§6.3）；两条都读不出盒才按**没有包围盒**拒绝上桌（**Q134**）。工作台那条桌顶盒同样优先 `sceneAABB(tableNodeId)`，量不到才回退元数据。**接线**：`main.ts:chooseLibraryItem` 的 `kind==="model"` 那一路把 `{ sceneAABB: id => renderer.sceneAABB(id), settle: async id => { renderer.syncScene(world); await renderer.readyObject(id); } }` 作新参数传进 `placeAsset`；`placement.ts` 的 `occupiedBoxes` 与 `placeProp` 各多一个可选 `BoundsMeasure`（无参时逐字走旧路径 ⇒ 既有摆放单测零改动）。不新增 gateway 调用、不改任何请求形状 ⇒ §5.9 路由清单与 `audit_parity.py` 探针**不增不减**。

### 4.7 导航栏与编辑限制
- 左栏保留：**场景层级显示**（`navigation.scene`）+ **资产面板**（`navigation.assets`）+ **Details**（`navigation.details`，改为只读）。删除：**Settings**（`navigation.settings`）。
- Details 保留：实体信息只读展示、相机 RGB/深度预览（`viewport.cameraPreview.*` 全部保留，供人工验证 §4.4）。
  删除的可编辑控件：name 编辑、transform 输入、`panels.details.apply`、robot targets/send、物理开关等。
  **Details 面板没有任何可编辑控件**（**Q114** 撤掉 §4.8.4 的关节角度输入，**Q81**）：机器人区块回到纯只读读数，显示的是 §3.6/**Q75** 那份 `initial`/`now` 的**逐关节合并位姿**，`(now)` 那条标记保留 —— 撤掉的是"人能往里写"，不是"看得见"。
- 视口：**保留点选（拾取）与轨道相机操作**；删除拖拽重排父级、删除实体、资产投放、可编辑 Details 表单。`move/rotate/scale` gizmo 已由 **Q114** 撤销并删除（§10.4）：页面上不存在 `data-tool="translate|rotate|scale"` 三个模式键，`World` 上也不存在任何本地位姿写入方法（§4.8.3）。位姿的改法只剩调用方那四条写入路径（§3.9）。
- **拾取只落在水面上**（**Q140**）：一次点选的命中必须来自一张**表面**（`Mesh`，含 §4.4 那些灯标记的 `Mesh`）或一个 `Sprite`（灯标签）；**线（`Line`/`LineSegments`）与点（`Points`）几何永远不携带选中** —— 与 §4.4 的视锥线（**Q71**）、本地坐标轴（**Q130**）共用同一个 `renderer.ts:selectable()` 判据（验收见 §12.35）。理由：three 测线的命中用 `Raycaster.params.Line.threshold`，默认值 **1** 是**一米**的余量；而 `OBJLoader` **不能把面与线装进同一份几何**，一份带 `l` 语句的 OBJ 会整份解析成一副 `LineSegments` 而不是 `Mesh`，那副看不见的线框因此接管整张桌面（OLS 资产 22 的 `doll.obj` 实测：一次点击 **3403** 次线段命中、63 格网点 **26** 次落到它身上、场景里 19 件道具一次都没被点中过）。渲染器同轮把 `Line.threshold` 与 `Points.threshold` 置 `0` —— 那一行省的是无用的段测试，**规则本身在 `selectable()`**：以后再出现只会出线的资产，它依然抢不走点击。**代价**：一副纯线框资产因此无法用鼠标选中（树里点行仍然可选中它，那条路走的是 DOM 不是射线），要能点它得把面补回资产，而不是放开这条判据。

### 4.8 浏览器现场改动与导出（**Q114** 三分类）

分类的判据只有一条：**这个入口改不改浏览器持有的那份现场文档，以及改了之后 Gateway 看不看得见**。

- **① 类：撤销并删除** —— 视口 gizmo（§4.8.3，**Q79/Q80**）与 Details 的关节角度输入（§4.8.4，**Q81**）。撤销的理由：**它们改现场却没有任何人知道** —— 既不推进版本也不上报，于是下一次 Agent 写回静默覆盖用户手上的结果。撤掉之后 `World` 上**不存在任何本地位姿写入方法**（类型层面的负控制，§12.8b）。
- **② 类：保留，但挂阶段闸门并且一定上报** —— 资产面板加物与切场景（§4.6）、层级面板的本地删除。`setup` 阶段照常可用；进入 `agent_owned` 后点击弹窗（判据在 §4.6）。成功后**`scene_sub_version` +1**（§3.1）并走 `browser.state_sync` 整份上报（§5.8）。"浏览器是场景权威"（§1.4）不等于"浏览器可以偷偷改"。
- **③ 类：保留，但只到渲染层为止** —— 光照预设（§4.8.1）、阴影渲染（§4.8.5，**Q117**）、物体包围盒线框（§4.8.6，**Q119**）、视口录制（§4.8.7，**Q121**）。**永不**写 `World`，因此不进 `toJSON()`、不进导出、不进 `/getWorld`、不进摘要。
- **既不算编辑、也不受闸门约束的本地动作**：导出（§4.8.2）、`Load Traj` 与播放（§4.3、§8.5）、视口截图（§4.4、**Q108**）、视口录制（§4.8.7、**Q121**）、辅助图层显隐（§4.4、**Q105/Q106**，**Q119** 把物体包围盒线框也归进这一族）、相机预览、轨迹路点与幽灵、选中与轨道相机。它们不改现场，所以 **Agent 接管期间照常可用**（R1）—— 被冻结的只有"改现场"那一族。

两条共同口径：

- **实现落在纯函数模块**：`editor/src/local_edits.ts` 留 `sceneExportName` 与阶段闸门的判定，② 类的落地在 `world.ts:addLocalObject`/`removeLocalObject`，③ 类在 `renderer.ts:setLightingOverride` 与 `renderer.ts:setShadowsEnabled`（后者要的投影算式落在 `logic.ts:shadowExtentsOf`/`shadowRangeOf`，与 `projectBox` 同族：只吃数组、不碰 `THREE`），`main.ts` 只负责按钮、下载、弹窗与 `renderer.syncScene`。"入口做了什么"仍能被单测钉住，不必拖动一个 3D 控件。
- **② 类走子版本 + 上报，③ 类零落地**；两类的判据分别写在 §4.6 与 §4.8.1 里，都是可从页面外读到的量。

4.8.1 **光照预设 = 渲染层覆盖**（视口工具栏一个 `<select id="lighting-preset">`，**Q77/Q114**）

预设不替换世界里的光照物体，只决定 `three` 场景里实际建哪些灯。

- 预设表是前端常量（`editor/src/lighting.ts:LIGHTING_PRESETS`，初版 5 项：`neutral` 照抄真实 scene 1 的两盏灯、`studio` 低环境光 + 主/补两盏 Directional、`dramatic` 很低的 Ambient + 一盏强侧向、`overcast` 高 Ambient + 很弱 Directional、`lamp` 低 Ambient + 一盏暖色 `Point`），**每项仍只写 §4.5 已实现的三个字段**加一个位姿；`range`/`decay`/`Spot` 锥角解析端不读（`docs/场景文件描述.md` §4.5），写了也不会生效，所以预设里不出现。
- 产出形态：`presetLightSources(preset)` 产出"渲染层要建哪几盏灯"（`{light, position, quaternion}`）。不再有物体被创建。`beamQuaternion`（Spot 沿局部 −Z）保留。
- **覆盖态下渲染器不建文档里那些 `Light` 节点、改建预设的灯**：文档灯的 `THREE.Light` 打 `userData.docLight` 并按覆盖态置 `visible`（渲染器的灯表跟 `visible` 走），预设灯挂在 `simWorld` 下一个**不属于 `nodes`** 的组里；`syncScene()` 每次重建后重新应用覆盖（重建路径不止一条）。marker、灯标签、选中框照旧，因为文档确实还带着那盏灯。
- **判据（本小节的验收，也是它唯一的防回归手段）**：切换预设之后 `world.toJSON()` 与切换前**全等**、`scene_version` 与 `scene_sub_version` 都不动、摘要不变，`/getWorld` 与导出读到的仍是文件自带的那两盏灯。**覆盖不许触碰 `World`**。
- 空选项 "Scene lights" 就是 `null` = 跟随场景（默认态），是合法的"关掉覆盖"。提示文案（**Q124/Q133**）：选中预设时报 `光照已切换：<预设名>`（`<预设名>` 取 `lighting.ts` 里的 `label`，括注已删），报回空选项时报 `光照已跟随场景`。机制说明（"仅渲染层，不改现场"）不念给用户；判据（`world.toJSON()` 全等、四个版本字段不动）一个字不改。
- **持锁期内冻结为「跟随场景」**：`acquireSceneLease` 成功 ⇒ 浏览器把覆盖复位为 `null` 并禁用下拉框，`releaseSceneLease` 后解冻（§7.2、§9）。换现场（`applyWorld`）同样复位，语义是"新现场带来新的默认光照"。**界面那一句（Q127）**只说当前状态：`任务 #N 进行中，光照和阴影暂时改不了，任务结束后自动恢复` —— 同一句也盖住阴影勾选框（两处冻的是同一族画面效果）。

4.8.2 **导出场景**（视口工具栏一个 `data-action="export-scene"` 按钮，**Q78/Q114**）

- 内容 = 浏览器当前那份世界的 `World.toJSON()`，**逐字节保真**：未知组件、未知字段、`MeshRenderer` 的空字典都按 §3.8 原样带出去，**`scene_sub_version` 在 >0 时一起写出**（§3.1），因此导出与 `/getWorld` 读到的是同一份现场、同一个四元组。
- 走浏览器下载（`Blob` + `<a download>`），文件名 `{scene_name}_v{scene_version}.json`（`scene_name` 清洗掉路径非法字符，缺失回退 `scene`）。**文件名规则不变（R4），子版本不进文件名**。**不发任何请求、不写 gateway、不 `reportWorld`** —— 导出是一次读。
- 导出的文件可以直接喂给 `/updateWorld` 或 Load Scene，格式一致（§3）。

4.8.3 平移 / 旋转 / 缩放 gizmo：**已撤销并删除**（**Q79/Q80/Q114** ① 类）

- 落点：three 的 `TransformControls` 与三个 `data-tool` 模式键删除、`World.setLocalTransform` 删除、`renderer.ts` 的 gizmo 部件连同"拖动期间关轨道相机 / `mouseUp` 才落一次值"那套时序一起退出；`isEditorOnly()` 里"按身份比较 gizmo 根对象"那条例外随之消失（选中描边仍按 **Q105** 读 `worldAabb()`）。
- **`RobotAttachment.transform` 的覆盖语义一字不改**（`docs/场景文件描述.md` §4.8）：那是文件格式，不是编辑功能。变的是**没有任何入口会去改写它** —— "拖动挂载物体时同步写回 `attachment.transform`"（**Q80**）随 gizmo 一起作废。

4.8.4 Details 面板的关节角度输入：**已撤销并删除**（**Q81/Q114** ① 类）

- 面板回到纯只读读数（§4.7）：显示 §3.6/**Q75** 那份 `initial`/`now` 的逐关节合并位姿，`(now)` 标记保留。
- 落点：`World.setLocalJointPosition` 删除、`local_edits.ts` 的 `jointEditorRows`/`poseEditBlock` 删除、常量 `allow_details_joint_angle_edit` 从配置文件、gateway 内置默认、`/api/runtimeConfig` 下发链与 §9.2 的表**整条撤**。
- **`now_joint_positions_rad` 照旧存在、照旧参与合并**（§3.6/**Q75**、§8.4）：写它的人只剩调用方（`/updateItem`）与 Agent。撤的是入口，不是数据。

4.8.5 **阴影渲染 = 渲染层的投影**（视口工具栏一个 `<input type="checkbox" id="shadows-toggle">`，界面上写作「投射阴影」，弹窗写 `投射阴影已开启`，**Q117/Q133**）

它落在 ③ 类，**一条协议都不走**；八条口径：

- **不写进文件格式**：`Light` 仍然只有 §4.5 那三个字段（`type`/`intensity`/`color`），`MeshRenderer` 仍然只有 `forceOverrideColor` —— **场上没有 `castShadow`/`receiveShadow` 可写**。"谁投影、谁接收"是**渲染器自己的规则**。判据与 §4.8.1 同源：切换之后 `world.toJSON()` 与切换前**全等**，`scene_version`、`scene_sub_version`、摘要、`/getWorld`、导出全部不动。
- **哪些灯投影**：一盏灯**在场上实际亮着**（`visible` —— 覆盖态下文档那些灯不投、预设那几盏投）、类型是 `Directional`/`Spot`/`Point`、且 `intensity > 0`，三个条件同时成立才投影。`Ambient` 永不投影。`Point` 用的是六面立方贴图，一盏灯就是六次场景绘制，所以它的贴图只给 `SHADOW_CUBE_MAP_SIZE`（512），`Directional`/`Spot` 给 `SHADOW_MAP_SIZE`（1024）。
- **哪些几何参与**：**只有从资产库取回的那份几何**。标志打在 `parseAsset` 出来的**模板**上（每个 Mesh 同时 `castShadow` 与 `receiveShadow`），`cloneAsset` 逐层复制这两个标志，于是每台实例、每个 URDF link 跟着都有。编辑层一律不参与：段末幽灵在 `ghostAsset()` 里被明确清成不投影、不接收，路点球、灯光占位球与标签、相机视锥线、地面网格与坐标轴从头到尾没打开过这两个标志。
- **不加"承接阴影的地面"**：场上没有 Mesh，就没有影子可落。凭空补一块不可见的接影平面会同时改变 `/captureScene`（§5.10）与视口截图（**Q108**）的像素，而那张图是 Agent 判断现场的证据。
- **投影框怎么算**：直射光的正交框逐轴**贴合世界 AABB 在该灯视空间里的投影**（`logic.ts:shadowExtentsOf`），聚光与点光只取该灯到 AABB 八个角的最近与最远（`logic.ts:shadowRangeOf`）。写死的 ±5 m 框会把 1024² 的贴图摊到桌面级现场（约 1 m 见方）的 100 倍面积上，影子糊到看不见；框收得比现场还紧又会在臂伸出画面时把影子裁断。重算时机是每 `SHADOW_FIT_INTERVAL_MS`（250 ms）一次，外加"现场重建 / 灯表变化 / 资产落地"三个事件点 —— **不逐帧**（`worldAabb()` 要遍历全部几何的包围盒）。
- **开关分两处，勾选框只管其中一处的外观**（**Q118**）：`viewport.shadows`（§9.3）**只决定勾选框在不在**。为 `true` ⇒ 勾选框出现、默认勾上，**投不投影由勾选框的状态说了算**；为 `false` ⇒ 勾选框消失，此时**由 `config/constant_config.json` 的 `shadows_enabled`（§9.2）决定是否投射阴影** —— 构建方因此能在不给页面留控件的前提下把阴影打开或关掉。三种来源（flag、勾选态、常量）由 `main.ts:renderShadows()` 一并交给 `logic.ts:shadowsEnabled(features, runtime, checked, frozen)` 这**一个纯函数**，渲染器仍然只收到一个布尔。`agent_owned` 阶段**照常可切**（R1，它不改现场）；**持锁（采集）期内勾选框冻结为开** —— 采集像素要可重复。**常量那一路没有冻结**：`viewport.shadows` 为 `false` 时页面上根本没有可点的控件，而 `shadows_enabled` 是启动时读一次的构建方决定，本身就可重复。**切换必须两头都可逆**：three 把"这个材质要不要采深度图"烘进编译后的程序（`shadowMapEnabled` 是程序缓存键的一部分），所以翻过 `shadowMap.enabled` 之后还要遍历场景把每台 Mesh 的材质 `needsUpdate` 置真，否则关掉时画面变亮、再打开却再也暗不回来。
- **它改变了什么画面**：阴影是光照的一部分，所以 `/captureScene` 的 RGB、Details 的相机预览与视口截图都会带上影子（三条都走同一个 `renderer.render`）。深度不受影响：`meters`/`preview` 两条 pass 走 `scene.overrideMaterial`，投影框与影子都不进深度；`/get2DAABB`（§5.11）与 `worldAabb()` 同样不受影响 —— **阴影不新增几何**。
- **落点与接口面**：`renderer.ts`（`shadowMap` 初始化、`createLightSource` 打标志、`fitShadowRigs`、`setShadowsEnabled`、`ghostAsset` 清标志）、`logic.ts`（两个纯算式 + `shadowsEnabled()` 开关判据）、`main.ts`（勾选框、把 flag/勾选态/常量/租约四者交给那条判据、租约冻结）。不新增 gateway 调用、不改任何请求形状 ⇒ §5.9 路由清单与 `audit_parity.py` 的探针**不增不减**；配置面：`features.json`（+1 键）与 `docs/feature-inventory.csv`（+1 行），**Q118** 之后再加一条链：`config/constant_config.json` + gateway 内置默认 + 两份 `types.ts` 的 `RuntimeConfig`（**坑 6**）各 +1 键。

4.8.6 **物体包围盒线框 = 渲染层的一层辅助线**（视口工具栏一个 `<input type="checkbox" id="bbox-toggle">`，界面上写作「显示包围盒」，弹窗写 `包围盒已显示`，**Q119/Q133**）

它落在 ③ 类，**一条协议都不走**；十条口径：

- **盒就是那条已经存在的量测，不新造口径**：线框读的是 `renderer.worldAabb(objectId)` —— 与 §4.5 的选中描边、`/get2DAABB`（§5.11）、`frame()` 同一条算式（**Q105** 收成一处的第四个表面）。"屏幕上的盒"与"调用方拿到的盒"不可能各说一套：`Box3Helper` 拿到的那个 `Box3` 就是 `/get2DAABB` 投影的那个 `Box3`。**Q143 之后**那 6 份坏资产不再产生 NaN 顶点（§6.5 整份文件先规范化再交给 `OBJLoader`），盒是从**完整**网格量出来的那一个，屏幕上的盒与 `/get2DAABB` 报的盒仍逐字相等（判据 §12.37 b、§12.36 c）；`isFiniteBox` 那个判项仍挡着规范化没看过的坏零件，只有当那一份几何**全部**是坏零件时才回 `null`、进下面那条 `skipped`。
- **一物一盒，按世界文档的物体 id 数**：盒挂在 `scene` 之下而不是某个节点之下（§4.5 那条 Q50 教训不变），集合就是 `nodes` 的键。一台 URDF 机器人在**调用方**那里永远是**一个**盒（整台机器的几何并起来）；**屏幕上**它可以拆成每 link 一个盒，由 §9.1 的 `urdf_bounding_box_per_link` 决定（见下面那条"形状开关"，**Q149**），两种形状量的是同一批几何、并起来逐字相同。**盒只量这个物体自己的渲染几何（Q148）**：`RobotAttachment` 与 `Transform.parent` 都把子物体挂进父物体的**场景图子树**里（挂在腕上的微波炉、挂在微波炉上的相机），所以那棵递归走到**另一个物体 id 的节点**就停 —— 判项读 `userData.objectId` 那枚**既有**身份标志（`expandGeometry` 的物界判项；`docs/场景文件描述.md` §4.8 那条"补挂必须排在 `built.traverse(...)` 盖章之后"正是它的前提），没有为新层开新口子。改判前它把子物体并进父级的盒：出厂 apple 现场（臂 + 腕上夹爪 + 夹爪上腕部相机）实量父盒在 +X 面比**臂自己**的真实轮廓多出 **6.3 cm**（其中 **5.25 cm** 框的是夹爪、不是臂），整盒体积多 **16%** —— 用户报的「机械臂的显示包围盒明显大于其实际包围盒」是这一条。**两个盒仍会重叠**（腕部那一截互相穿过），但**包含不是预期、重叠才是**：文档里它们是两个物体，`/get2DAABB` 也按两个物体各量各的；整份世界的盒不受影响（每个物体节点都从自己那棵子树走一遍，子物体不会因为父级不量它而漏量）。
- **URDF 的量测是逐 link、逐顶点的（Q149）**：Q148 掐断了越界，屏幕上的盒仍肉眼比本体大，病根换在**算式**上。`worldLinkBoxes(objectId)` 对每条 link 单独走一趟，递归**止于其它 link 的根**（URDF 树是嵌套的：子 link 挂在关节节点下、关节节点挂在父 link 下，从 `base_link` 走下去量到的就是整台机器，"每 link 一个盒"会画成 N 个同形大盒），量的又是**顶点扫出的世界盒**（`exactWorldBox`：逐顶点乘 `matrixWorld` 再扩张）而不是局部盒那八个角点。角点口径的病根写清楚：**零件是在它自己 link 的轴里建模的，关节一转就把它转出那些轴之外**，于是那八个角指向的是空处 —— 一台 URDF 的角点并盒因此**不可能比顶点并盒更紧**。出厂 apple 现场实量（UR5e，20 份网格 / 358,618 个顶点，按场景文件声明的关节位姿）：角点并盒 `[0.285, 0.998, 0.435]` 对顶点并盒 `[0.285, 0.991, 0.403]` —— **x 那两个面一字不差**（那两个方向的零件没被转出轴），松的三面是 **−Z 1.02 cm、+Y 0.69 cm、+Z 2.21 cm**，整盒体积多 **0.010 m³ ≈ 8.8%**。逐 link 看更能说明病在谁身上：`upper_arm_link` 的盒 y 松 **4.4 cm**、z 松 **4.4 cm**，`forearm_link` 松 **4.1 / 5.0 cm**，两根腕 0.7～1.0 cm，而 `base`、`shoulder_link` 两态**逐字相同** —— 越紧的 link 正是关节把它转得越狠的那一条。代价：顶点量测按（网格、`matrixWorld`、几何）缓存，一台臂冷走一次 **6.9 ms**，250 ms 的刷新周期因此够用；关节一动这枚缓存立刻失效（单测钉的就是它）。**非 URDF 物体（纯 model、道具、`Transform.parent` 挂上来的东西）走原那条角点路，一行没改** —— 它们本来就只有一棵子树、一个盒。**NaN 口径不变**（Q141/Q143）：非有限顶点不进这个盒，一个坏零件代价的是它自己的量测而不是整盒，`skipped` 那条照旧。

- **形状开关 `urdf_bounding_box_per_link`（§9.1，**Q149/Q150**，默认 `true`）**：`false` ⇒ 一台 URDF 一只盒；`true` ⇒ 每台 URDF 画**每 link 一只盒**（线框集合以 `<objectId>/<link>` 为键），同时把它自己那只并盒从屏幕上摘掉 —— 一个盒套在它自己的盒里不是"看得更细"，是噪声。逐 link 是默认形状，因为跨多关节的整机轴对齐盒会把 link 之间的空域也框住。**它只说怎么画，不说量出来多大**：`worldAabb()` 就是 `worldLinkBoxes()` 的并集，于是 `/get2DAABB`、`sceneAABB`、`frame()` 与选中描边在这一键开与关时逐字不变。**fail-open 同 §9.1 全表**：键缺失、`null`、字符串、数字一律按 `true`，判据收在 `logic.ts:urdfLinkBoxesEnabled(runtime)` 一处。link 集合为空时回该物体自己的并盒；没有几何的 link 什么都不画。
- **没有渲染几何的物体不画盒**：纯 `Light` 或纯 `Camera` 的物体在这条算式下回 `null`（§4.5 那条推论），**跳过而不是画一个零号盒**；跳过的 id 与画出的 id 都写进诊断行（本节末条），否则"少画了一个"与"根本没开"在屏幕上同形。
- **它是编辑层，因此天生不进采集**：`THREE.Box3Helper` 的 `type` 以 `Helper` 结尾 ⇒ §4.4 那条 `isEditorOnly()` 判据**一个字都不改**就把它算成编辑层，于是 `hideOverlays()` 在 `/captureScene` 的两条 pass（§5.10）与视口截图（**Q108**）里都会临时把它按掉；反向也成立 —— `expandGeometry()` 跳过编辑层 ⇒ 线框自己不会把 `worldAabb()` 撑大，Frame Scene 与 `/get2DAABB` 不会因为开了线框而变；`pick()` 只对 `nodes` 射线 ⇒ 线框点不中，也不会被选中。
- **刷新时机抄阴影那一条**：`SHADOW_FIT_INTERVAL_MS`（250 ms）一次，外加"现场重建"这个事件点，**不逐帧**。播放帧与调用方写入的关节位姿靠这个周期跟着走，与选中描边每帧刷一次不同：盒子内只有一个物体，代价差一个数量级。**勾选关闭时一次都不走**，这一层的默认开销是零。
- **开关只有一处，且没有常量兜底**：`viewport.boundingBoxes`（§9.3）管宿主在不在，勾选框管画不画；键关掉 ⇒ 勾选框消失**且线框不画**。**这里不复制 Q118 那条 `shadows_enabled`**：这一条的默认外观本来就是"不画" —— 没有勾选框的画面与勾选框没勾上的画面**逐像素相同**，没有任何一份构建方的决定会被偷偷改掉。
- **不冻结租约，而且判据是采集字节相等**：包围盒线框是编辑层，已从两条采集里被按掉 —— 采集结果与它无关，**开与关同一次 `/captureScene` 回同一份字节**（§12.20 ②）。它与 `viewport.cameraFrustumAlways`/`viewport.lightMarkerAlways` 同族（**Q105/Q106**）：都是"屏幕上多一层，采集里少一层"。
- **画面上的样子**：`BOX_WIRE_COLOR`（`0x4ade80`，青绿）—— 与选中描边的 `0xfacc15`（黄）、相机视锥线的 `0x38bdf8`（蓝）都不同。诊断行是 `Viewport boxes {"enabled":…,"drawn":[id…],"skipped":[id…],"perLink":…,"drawnLinks":["<objectId>/<link>"…]}`（`skipped` 也报；后两项 **Q149/Q150** 加，`perLink` 就是 `urdf_bounding_box_per_link` 到浏览器之后落地的那个布尔，`drawnLinks` 是屏幕上此刻真正画着的那批 link 盒），**每次被要求渲染都答一次、内容与上一行相同则去重**（跟随 250 ms 周期刷新时一条不变的行每秒打四次会淹掉控制台；页面加载后若一声不吭，取证分不清"开局是关"与"根本没接上"—— `renderer.test.ts` 与 §12.20 ③ 读的都是开局那一条 `{"enabled":false,"drawn":[],"skipped":[],"perLink":true,"drawnLinks":[]}`）。
- **落点与接口面**：`renderer.ts`（`setBoundingBoxesEnabled`、`setLinkBoxesEnabled`、`syncBoundingBoxes`/`syncLinkBoundingBoxes`、`worldLinkBoxes`/`exactWorldBox`、`boxReport`、`syncScene` 的收尾一次、`dispose` 清层）、`logic.ts`（`boundingBoxesEnabled()` fail-closed、`urdfLinkBoxesEnabled()` + `DEFAULT_URDF_LINK_BOXES` fail-open）、`main.ts`（勾选框、`renderBoxes()`、`toggleBoxes()`）。**不写 `World`、不进 `toJSON()`、不进导出、不进 `/getWorld`、不进摘要**；不新增 gateway 调用、不改任何请求形状 ⇒ 路由清单与 `audit_parity.py` 的探针**不增不减**；配置面只多 `features.json` 一个键（83 → **84**）与 `docs/feature-inventory.csv` 一行。**Q149 加的是常量面不是键面**：`config/constant_config.json` +1 键（`urdf_bounding_box_per_link`）、gateway 内置默认 +1、两份 `types.ts` 的 `RuntimeConfig` 各 +1（**坑 6**，两份逐字节相同），`features.json` 与 `feature-inventory.csv` 一字不动（仍 87 键）。

4.8.7 **视口录制 = 从渲染循环取帧的一条本地带子**（视口工具栏一个 `<button data-action="record" data-feature="viewport.record">` + 一个 `<input type="checkbox" id="record-overlays">`，勾选框写作「录制辅助图层」，**Q121/Q133**）

用户口径：录制一段时间内渲染窗口中的内容，以视口中的自由相机为准（录制中可移动/旋转相机）；常规查看模式与轨迹播放模式都能录，且可跨模式连录；一个按钮点击开始、再点击结束并自动下载 mp4。它落在 ③ 类与"既不算编辑、也不受闸门约束"那一族的**交点**上，**一条协议都不走**；十条口径：

- **帧源 = 这条带子自己那一趟 §4.4 视口 pass，不是屏幕**：每交付一帧调用 `recordFrameTo(proxy)`，它转调 `captureViewport(proxy, excludeOverlays)` —— 与**截图按钮同一条通路**（`WebGLRenderTarget` samples:4 → sRGB readback → 行翻转 → `putImageData` 进一块谁都不看的 2D 代理画布，尺寸取视口 drawing buffer，`pixelRatio` 上限 2）。**文件与截图按钮永远不会对"这块视口长什么样"各执一词**。**代价写清楚**：一段带子期间每交付帧多一趟渲染（上限 30 次/秒）—— 这是"屏幕上有、带子里没有"的价格，也是本卡与 **Q108**「不为一件偶发的事向每一帧收费」唯一的口径分歧（录像不是偶发，它正在干的那件事就是每帧）。
- **哪一幅进带子由渲染循环决定，不是由浏览器的采样钟决定**：投递点在 `render()` 里 `this.renderer.render(...)` **之后**，节流走本模块常量 `RECORD_INTERVAL_MS`（1000/30 ≈ 33 ms）—— **同一个循环、同一套门控、不新增一条定时器**；没装取帧点时那一趟一次都不走 ⇒ 默认开销为零。流是 `proxy.captureStream(0)` + 每次交付一次 `track.requestFrame()`。选手动投递的理由：**它让"一帧交付 = 文件一帧"一一对应** —— 定时模式下采样时刻归浏览器，可能落在本卡那一趟 pass 写完之前，`recordFrameTo().frames` 不再是"文件里有多少帧"的真话，勾选框中途改的口径也没法在下一帧立刻生效。投递读的是那块 2D 代理画布，与 WebGL 默认缓冲无关 ⇒ 仍然**不需要** `preserveDrawingBuffer`。
- **相机就是用户手上那台自由相机，录制不干预它**：轨道相机、WASD/QE 飞行（`updateFlight()`）与 `frame()` 照旧可点；录制**不**冻结相机、**不**停播放、**不**改选中、**不**动 `viewport.orbit`。播放本身并不搬视口相机（`showFrame()` → `updateGhostPose()` 只写关节与幽灵），所以"以视口中的自由相机为准"落成的实现是**什么都不必做**。
- **一个 `MediaRecorder` 录到底，跨模式不断**：整段录制只有一个 recorder 与一个 track。这块画布与整套 renderer 全页只有一份（`main.ts` 构造一次，`renderer.dispose()` 只在 `beforeunload` 被调），而 `Load Traj`、进入/退出播放、`Load Scene` 都不重建 canvas / `WebGLRenderer` / controls / 相机 ⇒ **不需要任何"切换之后重新接上"的逻辑**。判据是带子的时长**覆盖了**那次模式切换。
- **辅助图层默认不进带子，一个勾选框把它要回来，屏幕上一次都不动**：`recordFrameTo()` 把勾选框那个布尔直接交给 §4.4 的 `viewportPass(..., excludeOverlays)` —— 排除态下那一趟 pass 里 `hideOverlays()` 按住这一族（物体包围盒线框 **Q119**、选中描边 **Q105**、轨迹路点 **Q101**、段末幽灵 **Q96**、相机视锥线与灯光标记 **Q105/Q106**），而**屏幕上这些层照常显示，整段录制没有任何一次可见性写入**；勾上「录制辅助图层」则原样收进带子。判据（§12.22 ③ C 实测）：默认态下带子里 Q119 绿像素 **0** 个、同一时刻屏幕上 **1,429** 个；勾上后带子里 **787–839** 个、屏幕照旧在，文件从 38 kB 涨到 1.84 MB（线框与描边是最贵的那一层）。
- **容器只认 MP4，判据是字节与可解码，不是 `isTypeSupported`**：候选按序 `video/mp4;codecs=avc1.640028` → `avc1.4d001f` → `video/mp4`，取第一个本机答"支持"的。**`isTypeSupported` 不是证据**：它答的是"这种档位本机编得出来"而不是"你这条流会拿到哪一档"；且"支持"与"文件"之间确实有一段会失败的路（软件 GL 那台配置下带子交付了帧仍写回 **0 字节**）。所以收尾判据是字节：`blob.size === 0` ⇒ 告警、**不下载**。**一个 MP4 候选都没有** ⇒ 告警、**不降级成 WebM**、不起 recorder、不产生文件。**边界写明**：候选表保证的是**容器**不是**编码** —— 只认裸 `video/mp4` 时 Chrome 交回的可能是 **MP4 容器里的 vp9**（3 kB、可解码、画面真实，§12.22 ③ D3），那仍是用户要的 `.mp4`，但别把"表里写着 avc1"读成"文件里一定是 avc1"。
- **时长、帧率、码率是代码常量，不做旋钮**：`RECORD_INTERVAL_MS`（≈30 fps）、`RECORD_MAX_MS`（300 000 ms = 5 分钟，到点**自动结束并照常下载**，告警说明原因）、`RECORD_BIT_RATE`（8 Mbps）、`RECORD_TIMESLICE_MS`（1000 ms 一片，`ondataavailable` 逐片收，万一没走到 `onstop` 也不至于全丢）。
- **零协议面，也不受闸门约束**：`captureStream` / `requestFrame` / `MediaRecorder` / `<a download>` 全在浏览器本地 ⇒ 不写 `World`、不进 `toJSON()`/导出/`/getWorld`/摘要，`scene_version` 与 `scene_sub_version` 一字不动，一条 gateway 调用都不发（路由清单与 `audit_parity.py` 的探针**不增不减**）。**持锁与 `agent_owned` 期照常可录**（R1）：录像是给人看的带子，不是 Agent 判断现场的证据，不需要 §4.8.5 那种"冻结为开"。**它与采集唯一的交点被负控制钉住**：同一次 `/captureScene` 在"正在录制"与"空闲"两态回**同一份字节**，`/get2DAABB` 去掉 `request_id`（**坑 87**）两态逐字段相同。
- **诚实边界四条**：**①** 页面切到后台 ⇒ `requestAnimationFrame` 停 ⇒ 带子不再增长（**坑 16** 同族，不做补偿，也没有"恢复上次录制"）；**②** 录制中改窗口尺寸会经 `ResizeObserver` → `setSize()` 换帧尺寸，`MediaRecorder` 在那一刻的行为**未测**、本卡不判，但 `recorder.onerror` 走与正常停止同一条落盘路径（已收到的分片不丢）；**③** 文件名 `viewport-YYYYMMDD-HHMMSS.mp4` 取结束时刻，**不含现场名**（一段带子可以跨两个现场）；**④** **帧数是这条链上唯一诚实的量** —— 软件 GL 下投不满 30 fps 是正常现象，告警与诊断行报的都是**实际交付的帧数**，不报"应有帧数"。
- **落点与接口面**：`renderer.ts`（`RECORD_INTERVAL_MS`、`setRecordFrameTap()`、`recordFrameTo()`、`captureViewport(canvas, excludeOverlays)` 的第二个入参、`recordReport()`、渲染循环里的投递点、`dispose()` 摘取帧点）、`logic.ts`（`RECORD_MIME_CANDIDATES`、`pickRecordMime()`、`recordVideoName()` —— 三者只吃参数、不碰浏览器 API，`isTypeSupported` 以回调注入）、`main.ts`（按钮与勾选框、`toggleRecording()`/`startRecording()`/`stopRecording()`/`finishClip()`、`URL.createObjectURL` 与上一段 url 的**替换式**释放）。`features.json` +1 键（84 → **85**，`viewport.record`）与 `docs/feature-inventory.csv` +1 行；**不动** `config/constant_config.json`、两份 `types.ts`（这一条的默认外观是"什么都没发生"，勾选框就能把内容要回来，不需要 Q118 那条常量链）。

4.8.8 **本地坐标轴勾选框 = 文件声明的那一层，由工具栏决定看不看**（视口工具栏一个 `<label class="toolbar-check" data-feature="viewport.localAxesToggle" title="Draw the local frame each object declares"><input type="checkbox" id="local-axes-toggle"><b>显示坐标轴</b></label>`，**Q132/Q133**，弹窗写 `坐标轴已显示`）

三个裁定：**键改名** `viewport.localAxes` → **`viewport.localAxesToggle`**、**键关掉 ⇒ 宿主消失且这一层也不画**（与 §4.8.6 包围盒同形）、**勾选框默认不勾**。九条口径：

- **谁声明"哪些物体有帧"仍然是文件**：这枚勾管的是**全场看不看**，不改 §4.9 那条集合算式 —— `components` 里带 `LocalAxes` 的那些物体，勾上就是**全部**亮出各自的三根轴，不勾就是**全部**收起。**不提供任何按物体的界面入口**：撤掉单个物体的轴仍然只有删组件那一条路。
- **开关只有两处，且没有常量兜底**：`viewport.localAxesToggle`（§9.3）管宿主在不在，勾选框管画不画，判据收在 `logic.ts:localAxesEnabled(features, checked)` = **键 && 勾选** 一处，`main.ts:renderLocalAxes()` 是唯一调用者，`renderer.ts` 只收到最终那个布尔、从不直接读配置。**不复制 Q118 那条常量**：这一层在勾上之前不画任何东西，"没有控件"与"没勾上"是同一张画面。
- **默认不勾**（与阴影勾选框相反、与包围盒相同）：页面重载回到不勾。**不跟选中态走**，勾上即全场一起亮。
- **它是编辑层，因此天生不进采集与量测**：§4.4 那条排除清单与 `userData.localAxes` 这面身份标志**一个字都不改** —— `hideOverlays()` 在 `/captureScene` 的两条 pass 与视口截图里本来就按掉它，`expandGeometry()` 本来就跳过它，`pick()` 只对 `nodes` 射线 ⇒ **勾选框开与关，同一次采集回同一份字节**，`worldAabb()`、`/get2DAABB`、`frame()`、`shadowReport()` 全不因它改变（§12.20 ②）。
- **不冻结租约**：与 §4.8.6 同一条理由 —— 采集字节与这枚勾无关。
- **状态是浏览器本地视图态**：勾与否存在 `state.localAxes`，**不进 wire、不回传**；`world.toJSON()`、`scene_version`/`scene_sub_version`、层级树行数、`#panel-count`、`/getWorld` 一字不动 ⇒ 场景文件里**没有**对应字段。
- **取证出口**：`Viewport markers` 那行 `console.debug`（`renderer.ts:logOverlayState()`）里的 `localAxesEnabled` 布尔，加上 `markers` 里逐物体的 `kind:"localAxes"` 条目，是唯一能读回这一层的地方。**"键关"、"没勾上"与"这份场景本来就没写这个组件"三件事在像素上同形**，只有这一行能把它们分开（§12.14/§12.20/§12.26 ③ D）。
- **改键名这一处的代价写在键旁边**：`features.json` 的键面由 `viewport.localAxes` 换成 `viewport.localAxesToggle`，**总键数不变（仍 87）**，`docs/feature-inventory.csv` 那一行随之改名。gateway 的 `features.json` 是**启动时快照**（**坑 22**）⇒ **不重启 gateway，旧快照里没有新键、`featureEnabled` fail-closed 读成 `false`，症状是"工具栏没有这枚勾选框且轴不画"** —— 不是报错。
- **落点与接口面**：`main.ts`（勾选框 markup、`state.localAxes = false`、`renderLocalAxes()`、`toggleLocalAxes()`、`change` 委托那一支、`applyFeatureVisibility()` 里由"推键值给渲染器"改成"调 `renderLocalAxes()`"）、`logic.ts`（`localAxesEnabled()`，fail-closed）、`renderer.ts`（**只动两处事实**：字段 `localAxesEnabled` 的初值为 `false` 以对齐"默认不画"，字段与 setter 的注释指向本节 —— `setLocalAxesEnabled()`、`addLocalAxes()`、`isEditorOnly()`／`selectable()`／`markerNodes()`／`markerStates()` 与重建签名**一字不动**）。不新增 gateway 调用、不改任何请求形状 ⇒ §5.9 路由清单与 `audit_parity.py` 的探针**不增不减**；**不动** `config/constant_config.json`、两份 `types.ts`。判据见 §12.28。

4.8.9 **相机画面悬浮窗 = 全场相机的实时缩略，由工具栏决定开不开**（视口工具栏一个 `<label class="toolbar-check" data-feature="viewport.cameraWall" title="Show what every camera in the scene sees"><input type="checkbox" id="camera-wall-toggle"><b>相机画面</b></label>`，画布之上一个 `<aside id="camera-wall">`，**Q138/Q139/Q146**；界面名按 §7.1 的 Q133 口径写四个字的 `相机画面`，弹窗写 `相机画面已显示`）

用户口径：添加一个悬浮窗显示所有相机的拍照内容；画面略小、悬浮窗可拖动、相机名称在图像上方居中、不显示缩略图大小、有 RGB/深度切换按钮、深度图用伪彩色；相机数目多时出滚动条。十三条口径：

- **一格一台相机，集合就是文件声明的那些**：`placement.ts:findCameraObjects(world)` —— 每个带 `Camera` 组件的物体。**不按选中态筛**、**也不因为挂在臂上就排除**：腕部相机（`Transform.parent` 吊在夹爪上那台）恰恰是最想看的一台，它随运动学动，画面也就跟着动。
- **内容是这一窗自己的两枚按钮选的：RGB 或深度，一屏一次**：每格走 `renderer.renderSensorFrame(id, mode, canvas, limits)`，`mode` 由 `state.cameraWallMode`（`"rgb"`／`"depth"`，**Q139**）给，与 Details 预览、`/captureScene` 同一条离屏 pass（§4.4），画的是**那台传感器的视线**，不是本页 orbit 相机在看什么。标题栏那两枚按钮 `<button data-camera-wall-mode="rgb">RGB</button>` / `<button data-camera-wall-mode="depth" data-feature="viewport.cameraPreview.modes.depth">深度</button>` 是**这一窗唯一的模式入口**，默认 `rgb`。**不 mint 第四枚宿主键**：深度态的门仍是 §9.3 那枚既有的 `viewport.cameraPreview.modes.depth`，判据 `logic.ts:cameraWallModeFor(features, requested)` 在键关时把 `depth` 折回 `rgb`（fail-closed），**它只改写请求、不改写偏好** —— 远端把键关掉再打开，用户选的仍是深度。Details 那两枚 tab（`data-preview`）与这里的两枚（`data-camera-wall-mode`）**属性不同名**，否则全局那条 `[data-preview]` 委托会让一处的点击把另一处也点亮。
- **深度那一格是伪彩色，不是灰度**（**Q139**）：`renderSensorFrame(…, "depth", …)` 写进画布的仍是 §4.4 那张 `sensorDepth` pass 的**灰度**图（近黑远白、`NoColorSpace`、背景 `0xffffff`），窗在画完之后加一步 `main.ts:tintDepthFrame(canvas)` —— `getImageData` → 一个 `Uint32Array` 视图 → 每格取**最低那个字节**（小端 `ImageData` 里 `0xAABBGGRR` 的 R，灰度三通道同值）作下标，查 `logic.ts:depthPalette()` 那 **256 项**表（jet 族：近蓝 `0xffff0000` → 青 → 绿 → 黄 → 远红 `0xff0000ff`，`depthColor(t)` 在五个锚点之间线性插值）→ `putImageData` 写回。**是后处理而不是给渲染器加第三种模式**：`renderer.ts` 一字未动，Details 那格预览的灰度口径不会被这扇窗顺带改掉 —— 伪彩是**这扇窗**的读法，不是深度图的定义；灰度字节仍然完整存在，映射是纯函数、单测可查。**代价写明**：这条色带**表达不了"没有测量"** —— 一条打空的射线在 pass 里被清到 `far`，字节是 `255`，于是它和"真有一个东西在 `far` 上"在窗里同为最远那一端的那个红（`NaN` 只在 `renderSensorDepth()` 那条 float32 米制、`/captureScene` 走的通路里可表达）。**`willReadFrequently` 必须在第一次画之前认领**：`getContext("2d", { willReadFrequently: true })` 的属性由**首次调用**定下，所以 `renderCameraWall()` 在建格子的同一支里就为每块 `<canvas>` 认领上下文，`tintDepthFrame()` 之后只 `getImageData`，不再 `getContext`。
- **尺寸与节奏是这条窗自己的决定**：`logic.ts:cameraWallPace(caps)` 把每格压在 `256×192` 以内（**Q139** 从 `320×240` 收下来，窗宽同轮从 `min(38%,300rem)` 收到 `min(34%,240rem)`）、把节拍压在 12 Hz 以内（这一半没动：缩略是排版问题，节拍是负载问题），**再与 §9.1 `camera_preview` 的三个上限取小**（那是天花板，不是目标）。`renderSensorFrame()` 在限内**保持传感器自己的长宽比**（§5.10 那套 ratio 写法），所以一格 1642×150 的相机不会被拉成方形。
- **它是 DOM，不是场景图 —— 所以"不进采集"是结构性的**：`<aside>` 在 `.canvas-wrap` 里、`#viewport` 那块画布**之上**，渲染器从来不知道它存在。于是 `/captureScene`（§5.10）、视口截图（**Q108**）、录制默认态（**Q121**）、`worldAabb()`/`/get2DAABB`/`frame()`、`world.toJSON()` 五处**没有一个新判据**就看不见它。代价是它也不进录制，即便勾上「录制辅助图层」（那个勾选框管的是场景图里的那几层）。
- **开关两处，与包围盒（Q119）、坐标轴（Q132）同形**：`viewport.cameraWall`（§9.3）管宿主在不在，勾选框管开不开，判据 `logic.ts:cameraWallEnabled(features, checked)` = 键 && 勾选，`main.ts:renderCameraWall()` 是唯一调用者。**不补 Q118 那条常量**。**默认不勾**。
- **窗右上角那枚 ✕ 不是第二个开关**：它清 `state.cameraWall` 然后走同一条 `toggleCameraWall()`，`renderCameraWall()` 再把勾选框写回未勾 —— 与 `renderShadows()` 把 `box.checked` 对齐 `state.shadows` 是同一种单向镜像，真值只有一份。**没有第三个开关**：不做缩放、不做"哪台进窗"的勾选列表。
- **标题栏是拖动手柄**（**Q139**）：`main.ts:dragCameraWall(event)` 挂在 `app` 的 `pointerdown` 上，`target.closest(".camera-wall-head")` 命中才接管，**按在标题栏里的按钮上不算拖动**（`target.closest("button")` 直接 return，✕ 与两枚模式按钮因此还是按钮）。按住即 `setPointerCapture`，`pointermove` 里把 `right` 交给 `auto`、`left`/`top` 夹在 `[0, wrap.clientWidth − wall.offsetWidth]` 与 `[0, wrap.clientHeight − wall.offsetHeight]` 之间 —— **可拖出视口以外的位置不存在**，窗不会跑到屏幕上看不见的那一角，也不会挡住工具栏。**拖到的位置不持久化**：它是视图态，不进 wire、不回传，关窗再开仍在（同一页里 `style` 没被清），刷新页面则回默认位。
- **窗高有上限、格子那一行会滚**（**Q146**）：`.camera-wall` 一直带 `max-height:calc(100% - 20rem)`（夹在它浮着的那块 `.canvas-wrap` 里），`.camera-wall-cells` 一直带 `overflow:auto` + `min-height:0`；`.camera-wall` 必须带 `grid-template-rows:auto minmax(0,1fr)` —— 标题栏那一行 `auto`、格子那一行 `minmax(0,1fr)`。`minmax(0,1fr)` 里那个 `0` 抹掉了 `auto` 行"最小也要 max-content"的下限，`1fr` 让这一行只吃**窗被 `max-height` 夹住之后余下的空间**，格子于是真的比内容矮、那条既有的 `overflow:auto` 才滚得起来（没有这一处，相机一多格子行会从窗盒底边溢出、被 `.canvas-wrap` 的 `overflow:hidden` 裁掉，而 `.camera-wall-cells` 自己 `scrollHeight === clientHeight`，滚动条永远不出现）。**窗矮于上限时逐像素不变**：内容不到 `max-height` 时 `1fr` 行等于内容高，空态与一两台相机的窗高逐字节相同（§12.40 ③ 量过 N=0/1/2/3）。**顺带修好拖动的一条既有小错**：夹持上界用的是 `wall.offsetHeight`，内容进盒之后它才是用户看得见的那扇窗的高。**这是纯 CSS 一处改动**：`main.ts`/`renderer.ts`/`logic.ts`/两份 `types.ts`/`features.json`/gateway 全部一字未动，`renderCameraWall()` 建格子的 markup 不变 ⇒ 单测面不增不减（§12.40 ①）、接口面不增不减（§12.40 ②）。
- **格子的 DOM 只在相机集合变化时重写**：每拍只往既有的 `<canvas>` 里画像素、往既有的 `<code>` 里写一行读数。这是 **Q136** 那个 `panelMarkupChanged()` 守卫的第二次上场（同一个判据，另一块 DOM）—— 无条件重写字符串会把它刚画进去的那块画布扔掉，整面窗每拍闪一次。`#camera-wall-count` 是独立元素的 `textContent`，与 `#panel-count` 同理不参与那次比较。**一格里只有三样，顺序是名称 → 画面 → 状态**（**Q139**）：`<b class="camera-wall-name">`、`<canvas data-camera-id>`、`<code class="camera-wall-stale" data-camera-status>`，名称 `text-align:center` 居在**它自己那格画面的正上方**。名称用文件里的 `object_name_in_scene`，与层级树、Details 同一份来源。
- **与 Details 那一格各走各的时钟**：Details 预览看**选中的那台**（`panels.details.camera.preview`，可到 640×480、可到 `max_hz`，可切深度），这一窗看**全部那几台**（缩略为主，按标题栏那两枚按钮走 RGB 或深度）。两把 `setInterval` 互不知晓，同时开就各画各的。**不合并成一把**：两者的节奏上限不同（12 Hz 是窗的、`max_hz` 是配给单台的）。
- **状态是浏览器本地视图态**：勾与否存在 `state.cameraWall`，这一族还有 `state.cameraWallMode`（`"rgb"`/`"depth"`，默认 `rgb`）与拖出来的 `style.left`/`style.top`（写在元素上，不进 state）。三者**都不进 wire、不回传**；`world.toJSON()`、`scene_version`/`scene_sub_version`、层级树行数、`#panel-count`、`/getWorld` 一字不动，场景文件里**没有**对应字段。**持锁期不冻结**（与 §4.8.6/§4.8.8 同）。
- **取证出口在 DOM 自身，不在 `console.debug`**：`#camera-wall-count` 是格数；每格 `data-camera-status` **在活着的时候是空的**（**Q139**：不显示缩略图大小，`.camera-wall-stale:empty { display:none }` 让成功的那一行整条不占高度），只有失败才写字：RGB 态写 `无画面`、深度态写 `无深度`（都是 `renderSensorFrame` 回 `false`，§5.11 那条既有含义：那不是相机、id 已不在世界里、画布取不到 2D 上下文；深度态多一种 —— 那台相机不是 `rgbd`，§4.4「深度图只对 `rgbd` 存在」）。三态里**"键关"与"没勾"在 DOM 上同形**（整块 `hidden`），**"场景里没有相机"不同形**（窗开着、里面一句空态）。**模式也是一处 DOM 取证**：`[data-camera-wall-mode]` 里带 `.active` 的那一枚就是**当前真的在画的那一态** —— `renderCameraWall()` 镜像的是 `cameraWallModeFor()` 折过之后的值，不是 `state.cameraWallMode` 那个偏好，所以键关时亮着的仍是 RGB（窗不假装它在画深度）。**Q139 不新增键**（深度借 §9.3 既有的 `viewport.cameraPreview.modes.depth`），这一轮**没有**"要重启 gateway"这条生效条件（**坑 22** 只在 Q138 那轮成立）；`features.json` 仍是 88 键，`docs/feature-inventory.csv` 只有 `viewport.cameraPreview.modes.depth` 那一行的**依据**从 `interpolated` 变成 `literal`，键本身、默认值、行数都不变。

---

## 5. Gateway ↔ 浏览器同步调用协议（本期新增，核心）

同步 GET（`captureScene`/`get2DAABB`）与写入 POST 都需要浏览器执行后把结果回传调用方。统一走现有 `/events` WebSocket，把它升级为**双向请求/响应通道**。

### 5.1 通道与消息信封
- 通道：`GET /events`（aiohttp WebSocket，已有）。浏览器连上后**必须**发送 hello：
  `{"type":"browser.hello","browser_id":"<uuid>","role":"viewer"}`；gateway 维护在线浏览器表。
  - 浏览器发往 gateway 的 HTTP 请求必须带 `X-WebEditor-Browser-ID: <browserID>`；该头选择状态 profile。已有 `browser_id` query／`browserID` body 继续由对应路由按既有规则校验（例如目标断言、会话绑定），不能反向改变 profile。兼容旧调用方时，仅在当前 gateway 只有一个 profile 且没有显式身份来源时允许使用匿名 bootstrap 状态；多 profile 时缺少该头及既有身份字段必须拒绝，不能猜测目标。
  - **`browser_id` 是持久的身份，不是每次连接的随机数**（§5.8）：`role` 由 gateway 判定而非由客户端声明 —— 与落盘绑定一致的那个连接是**绑定浏览器**（唯一收件人），其余连接一律按**只读观察者**处理（收广播、不接收 `browser.request`，§5.2）。
- gateway → 浏览器（请求）：
  `{"type":"browser.request","request_id":"<uuid>","action":"updateWorld|updateItem|addObject|removeObject|updateTraj|captureScene|get2DAABB|getWorld","payload":{...},"expects_binary":false}`
- 浏览器 → gateway（响应）：
  `{"type":"browser.response","request_id":"<uuid>","ok":true,"payload":{...}}`
  `{"type":"browser.response","request_id":"<uuid>","ok":false,"error":{"code":"TRAJ_INVALID_MAGIC","message":"..."}}`

### 5.2 请求—响应匹配
- 每个调用方 HTTP 请求生成一个 `request_id`；gateway 挂起该 HTTP 请求直到收到**该 `request_id` 的** `browser.response` 或超时。
- 无浏览器连接 → `503 NO_BROWSER_CONNECTED`（不排队等待）。
- 超时 → `504 BROWSER_TIMEOUT`。默认控制类 10 s、渲染类 15 s（`constant_config.json` 可配）。
- 多个浏览器同时在线：**广播写入已废止**（**Q111**）。每个 `browser_id` 都有自己的权威现场和 gateway 状态；绑定浏览器由该 id 认领，所有 `browser.request` —— 读取类（`captureScene`/`get2DAABB`/`getWorld`）与写入类（`updateWorld`/`updateItem`/`addObject`/`removeObject`/`updateTraj`）—— 只发给同一 browserID 的绑定 socket。`scene.applied`、`session.phase_changed`、`agent.job_updated` 和错误回执不得跨 browserID 广播。
  - 绑定浏览器离线／从未 hello → `503 NO_BROWSER_CONNECTED`，**不排队、不换节点**。
  - 可选参数 `browser_id` 的语义是"**断言目标**"而非"选目标"：与当前绑定不符 → `503 NO_BROWSER_CONNECTED`（绝不静默换成另一个节点的世界）。
  - "第一个响应者胜出"只对**同一个 action 的重复响应**还有意义（超时后晚到的 `browser.response` 一律丢弃并 warn）。
  - 第二标签页：允许连 `/events`，但它是**只读观察者**（D6）—— 不认领绑定、不接收 `browser.request`、不应用任何写入，只收 §5.4 的广播刷新显示。

### 5.3 二进制载荷传输约定（`updateTraj`）
- **一次请求可以携带多条轨迹**（**Q99**）。"多条"＝ **N 份各自独立的 v2 TRAJ 文件**：§8 的 v2 布局一字未动，也**不使用**轨迹 v3（`docs/轨迹文件布局v3(草案).md` 与本项目无关）。
- **调用方侧的线格式**：`Content-Type: multipart/form-data`，**每条轨迹一个 part，字段名固定 `trajs`**（同名重复 N 次，顺序即列表顺序），part 体 = 一份完整 `.traj` 的原始字节。可选查询参数 **`count`**：缺省＝实际 part 数；给了就必须是 ≥1 的整数**且等于 part 数**，否则 400（见 §5.13）。整条请求体仍受 §11.1 的 `max_payload_bytes`（默认 100 MB）**总量**约束 —— 那是 multipart 之后的**整个 body**（含边界分隔符），不是每条的上限。
- **禁止 base64 塞进 JSON**：一条就最大 100 MB，base64 要让它胀 1/3，而通道本来就支持原始字节帧。
- **浏览器侧的通道**：还是"一个文本帧 + 紧随的**一个**二进制帧"。gateway 把 N 个 part **按顺序首尾拼接**成一个二进制帧下发，并在文本帧里给出切分依据：
  1. gateway 发 `browser.request`（text 帧，`payload = {"count": N, "byte_lengths": [n1…nN], "byte_length": n1+…+nN}`，`expects_binary = true`；`byte_length` 由 gateway 写声明值，**Q34**）；
  2. 紧接一个 WS **binary 帧**，内容为拼接后的原始字节；
  3. 浏览器把"收到的第一个 binary 帧"绑定到最近一个 `expects_binary=true` 的未完成请求；绑定后清除标记。
- **校验分层，谁看见什么谁判**：
  - `count` 与实际 part 数是否相符 ⇒ **gateway**（它是唯一同时看见 `?count=` 和 multipart part 数的一方）；
  - 声明与到达是否相符 ⇒ **浏览器**（**Q34**）：`buffer.byteLength` 必须等于 `payload.byte_length`，且必须等于 `sum(byte_lengths)`，不符 → `502 GATEWAY_FRAMING_ERROR`；
  - 轨迹内容 ⇒ 浏览器逐条解码，**切分只靠 `byte_lengths` 做算术偏移，绝不去读某条的头部推它的长度** —— 一条坏文件恰恰推不出可信长度，靠声明长度切才能把"是第几条坏了"报准。
- **失败口径：全有或全无**。任何一条解码失败 ⇒ 整个请求带着该条自己的码失败（`TRAJ_INVALID_MAGIC`/`TRAJ_UNSUPPORTED_VERSION`/`TRAJ_TRUNCATED`/`TRAJ_POSE_DIM_UNSUPPORTED`），消息里点名下标（`trajs[2]: …`），并且**浏览器内存里已有的那份列表一字不动**（与 **Q49** 同源）。
- 该约定要求浏览器端 `WebSocket.binaryType = "arraybuffer"`。

### 5.4 状态广播
- **改动世界**的四类写入（`updateWorld`/`updateItem`/`addObject`/`removeObject`）成功后，gateway 向**所有在线连接**（绑定浏览器 + §5.2 的只读标签页）广播（不含 request/response 语义，纯事件）；`updateTraj` 不改动世界，因此**不广播**（**Q33**）：
  `{"type":"scene.applied","scene_id":<uint>,"scene_version":<uint>,"scene_sub_version":<uint>,"scene_digest":"<64 位小写 hex>","session_phase":"setup|agent_owned","session_id":"<uuid|null>","action":"<action>","source_request_id":"<uuid|null>","ack_report_seq":<uint|null>}`
- **语义是"通知刷新"**（**Q111**，与 §5.2 的定向写入配套）。写入只在绑定浏览器发生，广播的读者是"其余连接"：它们**不得**把广播里这份载荷当世界，只按四元组决定重取（`GET /getWorld?browser_id=<绑定>`，§5.8）还是只更新状态栏。绑定浏览器自己就是应用者，按 `source_request_id` 去重忽略，**不得重复应用**。
- **自己报的那一条按 `ack_report_seq` 认（2026-11-05 定稿，Q147）**：`browser.state_sync` 的 payload 带一个本页面自增的 `report_seq`（§5.8），Gateway 采纳后把它**逐字**回进这一帧的 `ack_report_seq`；凡不是由浏览器上报落地的（调用方四类写入、`/commitWorld`、`session_switch`）这一键为 `null`。`session_id` 是落地那一刻 Gateway 手里的持久会话号，没有会话时 `null`。读者的判据按顺序四支：
  ① `session_id` 与本 tab 已读到的会话号**都不为空且不相等** ⇒ 期间换过会话，先重问一次 `/api/sessionState`（本 tab 的读数可能只是慢了一步），重问后仍不相等 ⇒ 忽略这一帧；
  ② `ack_report_seq` 非空且 **< 本 tab 已发出的最大 `report_seq`** ⇒ 上一次切换的迟到回声，忽略；
  ③ `ack_report_seq` 非空且 **= 本 tab 最大 `report_seq`** ⇒ 这就是我自己那次的确认：不动 `state.world`、不重读 `/getWorld`、**不再上报**，只刷新阶段与状态栏；
  ④ `ack_report_seq` 为 `null`，或 **> 本 tab 最大 `report_seq`**（同一个 `browser_id` 的另一个 tab 报的号）⇒ 不是我这次的回声，按 Agent／调用方的写入对待——**直接相信并应用**（Agent 在任务期不得切场景，§4.6、§8）。
  为什么需要它：`apply_state_sync` 在广播之前要等一次 Brain 的建会话 HTTP（`_ensure_automatic_session`），用户快速连切 1→2→3 时 2 的回声可能晚于 3 落地；旧判据"四元组不符就重读 `/getWorld`"于是把 Gateway 那份 2 读回来覆盖屏幕上正确的 3，再被下一条"采纳后重报"固化成两边都是 2 —— 同事报的"点另一个场景先出目标场景、过一会儿自己回到原场景"就是这条路径。**对 Agent 侧无感**：这两个键只活在 WS 通知帧与 `browser.state_sync` 里，`/getWorld`、`/commitWorld`、`browser.request`／`browser.response` 的形状一字未动，Gateway 也从不比较或解释 `report_seq`（它只回声），判据全在浏览器一侧。
- 四元组是这条帧的身份（§3.1）：`scene_sub_version` 让只读标签页一眼分清"这是用户 `setup` 期加的物体"还是"Agent 写回的"（Agent 写回必把它归 0，§3.9）；`scene_digest` 由 gateway **单点**算出（JCS + SHA-256，对传输层那份 JSON 原文计算，流程文档 §3；浏览器不自算，我方 TS 之外不许有第二份实现）。
- 不带 `source_browser_id`：§5.2 定了唯一权威之后它恒等于绑定浏览器。回源目标只有一个，就是绑定浏览器，它不在线时按 §5.8 直接 `503`，不存在"退回另一台"的分支。
- **帧类型 `session.phase_changed`**（**Q111**）：会话阶段翻转时 gateway 广播
  `{"type":"session.phase_changed","payload":{"browser_id":"<uuid>","session_phase":"setup|agent_owned"}}`
  浏览器据此在同一次渲染里把 §4.6 的 ② 类入口置灰（**点击必须弹窗，不得静默吞掉**，R2）。置灰是**观感不是行为**：用视觉类 + `aria-disabled`，**不用 `disabled` 属性**。阶段有三个读数点 —— 这一帧、`GET /api/sessionState`（§5.9）、`GET /getWorld` 的 `X-Session-Phase` 头（§5.8）—— **三者同源，不一致时以 `/api/sessionState` 为准并重取现场**；权重按"谁为回答阶段而存在"排：`/api/sessionState` 是唯一可覆盖持有值的读数，两条 WS 推送（`session.phase_changed` 与 `scene.applied` 自带的 `session_phase`）按活边应用，`X-Session-Phase` 只是盖在世界答案上的邮戳——不覆盖持有值，冲突时浏览器去做的是重问 `/api/sessionState`。词表外的值（含头缺失）算"这条读数什么也没说"，**不等于 `setup`**；浏览器初值是"还没读过"，此时 ② 类入口照常可用，拒写由第 2 层负责。
- 旧的 `{"type":"scene.changed","revision":…,"payload":{"snapshot":…}}` 信封**作废**（snapshot 结构已废）。

### 5.5 id 分配权威
- **唯一权威是 gateway 磁盘上的持久高水位**（`var/gateway_state.json`，落在 §9.2 的 `state_dir`）。水位必须能在浏览器刷新甚至重启之后继续对账（会话阶段、租约、事务回执），**不得**存在浏览器内存里（否则"新增→删除→刷新复用旧 id"这条路重新打开）。
- **推进水位的三类事件**：① Agent 写回里含的新对象；② gateway 转发 `/addObject` 时的申领；③ gateway 采纳浏览器上报的现场（`browser.state_sync`，§5.8）时取 `max(已存水位, 文档里所有 object_id_in_scene + 1)` —— 第 ③ 条是 ② 类编辑能被持久水位追认的唯一途径，缺了它，gateway 之后发的号段会撞上用户 `setup` 期本地分配过的那串 id。
- **`GET /allocateObjectID {browserID, count}`** → `{"object_ids":[<uint>,…]}`：一次号段、原子推进。**申领方只有 Brain**（它组 commit candidate 需要 N 个新 id 时一次领 N 个，离线增物本期不支持）。gateway 转发 `/addObject` 而 body 未带 `object_id_in_scene` 时**自己先领一个**，再把确定的 id 交给绑定浏览器（**Q112**）；浏览器因此**不再是分配点**，只回答"这个 id 能不能落地"。
- `setup` 期浏览器的 ② 类本地加物**仍会分配 id**，但必须以 `GET /getWorld` 的 `X-Next-Object-ID`（§5.8）为**种子**向上单调，**不得只从文档现有 id 推**。进入 `agent_owned` 后 ② 类入口禁用（§4.6、**Q111**），浏览器再无任何分配点，刷新也不解禁。
- 调用方显式传了 `object_id_in_scene` 且该 id 已存在 → `409 ID_ALREADY_EXISTS`。
- `removeObject` 释放的 id **不复用**：水位只升不降、删除不回退、刷新不丢。避免调用方手上的旧 id 指向新物体。
- **上限 `2**32 - 1`**：水位到达上限时申领明确拒绝 → `409 object_id_space_exhausted`（§5.13）。`X-Next-Object-ID` 也必须在 `<= 2**32 - 1` 之内。

### 5.6 错误与提示
- gateway 对调用方的错误响应体统一为：
  `{"error":{"code":"<STRING_CODE>","message":"<人类可读>","request_id":"<uuid|null>"}}`，HTTP 状态码见 §5.9。
- 浏览器侧的用户可见错误：`showError`（视口通知，已有）+ 状态栏红点。轨迹/物体缺失类错误**必须提示后继续执行**，不得中断整条轨迹或整个场景。

### 5.7 并发与版本
- 每个 gateway 挂起请求上限默认 8，超出 → `429 TOO_MANY_REQUESTS`。
- 写入类接口支持可选 `expected_version`(uint)。浏览器当前 `scene_version` 不等于它 → 返回 `409 VERSION_CONFLICT`，不应用变更。不传则不做校验。
- 同一浏览器标识的多个 `browser.state_sync`（可能来自重连前后的不同 socket）必须串行经过当前身份/版本判定、自动会话准备与 `record_scene` 落盘；较早报告不得因异步等待晚恢复而覆盖较新的场景。

### 5.8 浏览器主动上报与 `GET /getWorld`
- 用户走 §4.1a 本地文件加载或 §4.6 资产面板加载后，浏览器必须把生效的世界 json 通过
  `{"type":"browser.state_sync","payload":{"world":<json>,"report_seq":<uint>}}` 发给 gateway，gateway 原样持久化（§1.4）并广播四元组 `scene.applied`（§5.4）。
- **每一次上报都编号**（2026-11-05 定稿，**Q147**，配套 §5.4 的 `ack_report_seq`）：`report_seq` 是本页面自增的整数（从 1 起），**不落 `localStorage`、gateway 也不持久化、不比较**——它只在这一页"我报了 ⇒ 我等回执"的生命周期里有意义，gateway 收到什么就原样回声什么。socket 未 OPEN 时的重试算新的一次上报，所以每次真正 `send` 才 +1；换页后从 1 重新数不会与旧号段相撞，因为旧号段从来不在这台机器上存在过。判据：`editor/src/adopt_ack.test.ts` 钉住 `main.ts` 的 `reportWorld()` 里带 `report_seq`、计数器不写 `localStorage`，以及 `adoptWorld()` 在动 `state.world` 之前先过 §5.4 那四支。
  ② 类改动（§4.8）每一次成功之后都重放这一条 —— 看不见的那份现场会被 Agent 覆盖，这是它必须上报的全部理由。
- **换场景的那一条无条件采纳**（流程文档 §1.2 第 4 条，2026-11-05 修订）：`scene_id` 一变就是一次合法上报，水位、摘要、`camera_candidates` 跟着新现场走，**不看阶段、也不看租约／隔离在不在，两份场景的 `scene_version`／`scene_sub_version` 更不参与比较**（那是各场景自己的计数，跨场景比没有意义）。原先那条"只有 `setup` 且租约／隔离都不在才采纳，否则回 `browser_new_session_required` 整条丢弃"的规则是死锁之源：Gateway 重启后从盘上读回一份**永不释放**的租约／隔离（持有 token 的 Brain 会话早已不在），于是每次换场景都被拒，持久身份与缓存永远停在浏览器已经离开的那份现场——`/getWorld` 继续把这份旧现场交给 API（任务的碰撞算在屏幕上根本看不见的场景上），`camera_candidates` 因配对失配读空，浏览器下一次权威重读又被拽回旧场景闪一下（同事联调两个 bug 同一个根）。改判之后 Agent 的锁仍在它真正该生效的地方：写入由**租约指纹**把关（一次迟到的旧写仍按 `browser_scene_version_conflict` 拒），会话由 `_ensure_automatic_session` 在 `scene_held`／`agent_owned` 期**拒绝新开**——采纳身份绝不偷偷起会话。判据：`gateway_state.py:adoption_verdict(phase, stored, incoming)`（`scene_held` 形参已删），`tests/test_gateway_state.py` 的 `AdoptionVerdictTests`（换场景在 `setup`／`agent_owned`／更低版本三态一律 ⇒ 采纳；同场景那一支仍按词典序判）加路由面两支（租约期与 `agent_owned` 期换场景**都落地**、身份与缓存跟着走，且**不会**在持锁现场自动建会话）。
- **重读权威现场只发生在两处：启动一次、重连一次**（2026-11-05 修订，删掉的是"每次 `/events` 打开都先重读再上报"这条从未写进规格的默认）：`start()` 在**建 socket 之前** `await loadAuthoritativeWorld()`，这一页此后每一次 `onopen` 都只做两件事——第一次打开**只上报**手里那份（§5.5/Q112 的水位重报照常发生），重连才先 `loadAuthoritativeWorld()` 再 `reportWorld()`（这一页聋过、Gateway 也可能换了进程，先读后报正是补听的意义）。为什么第一次不能再读一次：一次 `GET /getWorld` 的答案**允许晚于它出发的那一刻落地**，而落地的动作是整份覆盖 `state.world` 加一次 `syncScene`；`start()` 那一次读发生在页面还不能被操作时，覆盖不了任何东西，`onopen` 那一次却正好和用户点开预设场景后的资产加载并跑，晚到的旧答案把刚摆好的现场抹掉——同事报的"切完场景闪回／变空"就是这么来的（同一现场被下一次重连再抹回来，所以看起来像闪）。**上报一次都不能少**：空现场也要报，那是一次合法的换场景（上一条）与相机候选的清理。**判据**：`editor/src/socket_open.test.ts` 钉住 `main.ts:connectWebSocket()` 的 `onopen` 体——整个体里 `loadAuthoritativeWorld(` 只出现一次且必须在 `firstSocketOpenDone` 为真的那一支里，第一次打开那一支只有 `reportWorld()`，`reportWorld()` 总共两支各一次，且 `start()` 里那次 `await` 排在 `connectWebSocket()` 之前。
- **别人写的那份也要重报一次**（流程文档 §9 的候选清单条）：浏览器**采纳**一份不是自己报上来的现场之后（`scene.applied` ⇒ 重读 `/getWorld` 落地，或广播的四元组已经等于本 tab 手上这份且那条广播带 `source_request_id`——即它来自一次调用方写入而非 `state_sync` 的回声），必须再走一次 §5.8 的上报。理由：`camera_candidates` 是浏览器读文档读出来的（§4.6、§9.2），Gateway 的 commit 自己 `record_scene` 推水位、浏览器却一次都没报，配对就落不到新四元组上，"新建会话"这道门在接管期会点不动。**代价为零**：重报的是同一份文档，四元组与存量相等 ⇒ `adoption_verdict` 判 `idempotent`，`scene_version`／`scene_sub_version` 一步不动，`agent_owned` 期也不会被读成 `manual_edit_forbidden`；回声那条用 `source_request_id` 挡掉，于是"我报⇒回执⇒我再报"不可能成环（2026-11-05 起这一支更早收口：自己那次上报的 `ack_report_seq` 在 §5.4 的第 ③ 支就已经判了"确认、不再上报"，走不到这里）。
- **上报载荷仍是整份世界文档**，不得降成"元数据 + 计数"：gateway 需要文档里的**全部 `object_id_in_scene`** 来抬持久水位（§5.5 第 ③ 条），需要 `scene_sub_version` 来判阶段合法性（**Q111**：`agent_owned` 期收到子版本推进 ⇒ 拒绝并回 `manual_edit_forbidden`，现场保持不覆盖）。
- **`/events` 断线属于连接生命周期，不是场景写入失败**：浏览器重连只允许一个在途定时器，旧 socket 的关闭事件不得再次创建连接；Vite 代理对客户端主动断开的 `ECONNABORTED`、`ECONNRESET`、`EPIPE` 只作静默断线，不把它们记录成服务端错误。
- **`browser_id` 必须持久化**（`localStorage["webeditor.browserID"]`，缺失才生成并写回）：刷新、换 tab、重启浏览器都用同一个 id 认领同一份绑定。Gateway 状态文件以 `browser_id` 为顶层分区键；旧的单 browser 状态在 `--keep-state` 恢复时只允许迁移到文件中原来记录的 browser_id，不得复制给其他浏览器。
- gateway **不再 bump 版本**：采纳上报只是把收到的那份存下来，`scene_version` 的推进权仍按 §3.9 只属于四类写入。
- `GET /getWorld`（§5.1 的 `getWorld` action）：**只向绑定浏览器实时取**，响应头 `X-World-Source` 只剩 `live` 一个取值，**`cache` 分支整条删除**（**Q111**）。浏览器不在线 → `503 NO_BROWSER_CONNECTED`，调用方拿不到任何"看起来像现场"的东西。
- **响应头全部必填**（流程文档 §5.1）：`X-World-Source: live`、`X-Browser-ID`、`X-Playback-State`、`X-Scene-ID`、`X-Scene-Version`、**`X-Scene-Sub-Version`**、`X-Scene-Digest`（gateway 单点算）、`X-Next-Object-ID`（持久高水位，§5.5）、`X-Session-Phase`（§4.6 的三个读数点之一）。整型头、摘要与响应正文必须来自**同一次现场读取**，不得用上次的摘要配这次的正文；头里的 `X-Scene-Sub-Version` 与正文的 `scene_sub_version` 不一致 ⇒ 该次读取整体作废。
- 可选 query `browser_id` 按 §5.2 是**断言**而非选择器：与绑定不符 → `503 NO_BROWSER_CONNECTED`，绝不静默换成另一个节点的世界。

### 5.9 接口清单（本期最终态）

新增调用方接口（**根路径，无 `/api` 前缀**，路径风格与原始需求表一致；query 参数一律 snake_case。`/api/sessionState` 与 `/api/agent/health` 两条带前缀，因为它们**只服务浏览器**，与 `/api/health`／`/api/runtimeConfig` 同族）：

| 方法 | 路径 | 参数 | 成功响应 | 专属错误码 |
|---|---|---|---|---|
| GET | `/captureScene` | `id`(uint，必填)、`type`(`rgb`\|`rgbd`，必填)、可选 `browser_id`（§5.2 的**断言**，不是选择器） | §5.10 | 400 `CAMERA_TYPE_MISMATCH`、404 `CAMERA_NOT_FOUND`、502 `UPSTREAM_RENDER_FAILED`、**400 `camera_frame_scene_mismatch`**（§5.13 末注的第 3 条例外：由 gateway 的身份闸产出——响应自报的 `browserID`／四元组／播放态与本次现场逐项不符或缺项即拒，流程文档 §9.1）、**502 `GATEWAY_FRAMING_ERROR`**（`width`／`height` 非 ≥1 uint32，png 不完整，或 IHDR 自报尺寸与 `width`×`height` 不符，或 `rgbd` 深度字节数 ≠ `width*height*4`；§5.15 采集期） |
| GET | `/get2DAABB` | `id`(uint，必填)、`camera_id`(uint，必填)、可选 `browser_id`（同上） | §5.11 | 400 `EMPTY_AABB`、404 `OBJECT_NOT_FOUND`/`CAMERA_NOT_FOUND`、504 `BROWSER_TIMEOUT`、**400 `camera_frame_scene_mismatch`**／**502 `GATEWAY_FRAMING_ERROR`**（与上一行同一道闸、同一口径） |
| POST | `/updateWorld` | body：世界 json（`application/json`，≤100 MB）、可选 query `expected_version` | `{"ok":true,"scene_id":<uint>,"scene_version":<uint>,"object_count":<uint>,"rejected_objects":[{"object_id_in_scene":<uint>,"reason":"..."}]}` | 400 `INVALID_WORLD_SCHEMA`、409 `VERSION_CONFLICT` |
| POST | `/updateItem` | `id`(uint，query) + body：完整物体描述（json）、可选 `expected_version` | 同 `updateWorld` 的 `{"ok":true,...}` | 400 `BODY_ID_MISMATCH`/`INVALID_PARENT_CYCLE`/`MISSING_TRANSFORM_COMPONENT`、404 `OBJECT_NOT_FOUND`、409 `VERSION_CONFLICT` |
| POST | `/addObject` | body：物体描述（`object_id_in_scene` **可省略** —— 省略时由 **gateway 按 §5.5 申领**后再下发，浏览器不自己编号）、可选 `expected_version` | `{"ok":true,"object_id_in_scene":<uint>,"scene_version":<uint>}` | 409 `ID_ALREADY_EXISTS`/`VERSION_CONFLICT`、400 `MISSING_TRANSFORM_COMPONENT`/`INVALID_PARENT_CYCLE`、409 `object_id_space_exhausted`（§5.5） |
| POST | `/removeObject` | `id`(uint，query，必填)、可选 `detach_children`(bool，默认 false)、可选 `expected_version` | `{"ok":true,"removed_object_ids":[<uint>,...],"reparented_object_ids":[<uint>,...],"scene_version":<uint>}` | 400 `INVALID_PARAM`、404 `OBJECT_NOT_FOUND`、409 `VERSION_CONFLICT`（详见 §5.14） |
| POST | `/updateTraj` | body：**multipart/form-data，字段名 `trajs` 的 N 个 part**，每 part 一份完整 v2 TRAJ 原始字节（**Q99**，见 §5.3）。可选查询参数 **`count`**（缺省＝part 数；给了必须等于 part 数）。**不接受 `expected_version`**（轨迹不改动世界，无版本可预期，传了即忽略），也**不触发 §5.4 广播**（**Q33**） | §5.12 | 400 `INVALID_PARAM`（不是 multipart／multipart 结构解析不出（含 0 个 part）／part 字段名不是 `trajs`／某个 part 是空的／`count` 非正整数／`count` ≠ part 数）、400 `TRAJ_INVALID_MAGIC`/`TRAJ_UNSUPPORTED_VERSION`/`TRAJ_TRUNCATED`/`TRAJ_POSE_DIM_UNSUPPORTED`（逐条解码，§5.3 的全有或全无）、413 `PAYLOAD_TOO_LARGE`（整个请求体 >100 MB）、502 `GATEWAY_FRAMING_ERROR`（§5.3） |
| GET | `/getWorld` | 可选 `browser_id`（§5.2 的断言） | 当前生效世界 json + **§5.8 那组全部必填响应头**（`X-World-Source` 只有 `live` 一个取值，**Q111**） | 只向绑定浏览器实时取；浏览器不在场或超时 ⇒ `503 NO_BROWSER_CONNECTED`／`504 BROWSER_TIMEOUT`，**没有缓存回退分支**（§1.4、§5.8）；`browser_id` 与绑定不符即"向一台查看者要现场"，查看者从不收 `browser.request`（§5.2），故视同无绑定处理（`503`） |

**brain-web/2 新增路由**（**Q109/Q111/Q112**；调用方是 Brain 服务，经 gateway，端口 8099。字段定稿见流程文档 §5–§8，本表只给形状与错误码）：

| 方法 | 路径 | 参数 | 成功响应 | 专属错误码 |
|---|---|---|---|---|
| POST | `/commitWorld` | body：`{transactionID, browserID, expected{scene_id, scene_version, scene_sub_version, sceneDigest}, scene}`；candidate 的 `scene_sub_version` **必须为 0**（§3.9）；下发形态是 `browser.request{action:"updateWorld", payload:{world, strict:true}}` —— `strict` 让浏览器"先全量校验后一次落地"，任何拒绝零改动（普通 `/updateWorld` 不带它，§3.6 逐对象容错不变） | `{transactionID, browserID, status:"committed"\|"rejected", scene_id, scene_version, scene_sub_version, sceneDigest, rejected_objects:[…]}`，回执**落盘** `var/gateway_transactions/`（§9.2） | 400 `INVALID_WORLD_SCHEMA`/`gateway_scene_revision_invalid`/**`gateway_target_version_invalid`**（目标不是恰好 `expected.scene_version + 1`；这一判由 Gateway 在派发**之前**产出，Brain 侧同名判据留作第二层）、400 `MISSING_PARAM`/`INVALID_PARAM`/`web_uint32_required`（`transactionID`／`browserID`／`expected` 三个键的形状闸）、409 `browser_scene_version_conflict`（`expected` 四元组与当前现场不符；同一 `transactionID` 带**不同**内容重发也是这一码）、503 `NO_BROWSER_CONNECTED`、504 `BROWSER_TIMEOUT`、502 `GATEWAY_FRAMING_ERROR`（应答里既无 `rejected_objects` 列表、或报回的 `scene_id`/`scene_version` 与 candidate 不符 ⇒ 什么都不记）、500 `INTERNAL_ERROR`（回执写不进去，§9.2 第 ② 条）。**传输失败／超时／framing 失败都不是拒绝**：这三种一律**不落回执**，调用方只能改查 `/queryWorldCommit`，**绝不重发**（流程文档 §8.1） |
| GET | `/queryWorldCommit` | `transactionID`（只允许 1–128 个 `[A-Za-z0-9._-]`；缺 ⇒ 400 `MISSING_PARAM`，带路径分隔符／空格／超长的形状 ⇒ 400 `INVALID_PARAM`，两者都不碰磁盘） | 那份持久回执（形状同上，`transactionID`/`browserID` 原样回），**浏览器刷新后仍读得到** | 查不到 ⇒ `200` + `{"transactionID","status":"unknown","reason":…}`（**不是 404**：回执缺失是事实，且 `unknown` 必须保持锁 —— 它不得按定时器变成 `rejected`；回执里也**不带四元组**）。**唯一例外是"文件在但读不出"**：那份回执存在却不是合法 JSON／不是对象／`status` 不在 `committed\|rejected` 里 ⇒ `500 INTERNAL_ERROR` 并点名 transactionID |
| POST | `/acquireSceneLease` | body：`{taskID, browserID, scene_id, scene_version, scene_sub_version, sceneDigest}`——`taskID` 是**非负整数**（不是 Brain 那套字符串），`scene_id` **必填但可为 `null`**（键缺失与显式 `null` 是两件事：前者是调用方没答，后者是"这个现场没有 `scene_id`"），两个计数器是 uint32，`sceneDigest` 是 64 位小写 hex | `{browserID, leaseToken}`；副作用三件：停/拒轨迹播放、冻结切场景、**灯光预设复位并冻结**（§4.8.1）。**同一 `{taskID, browserID, 四元组}` 重发是幂等的**：回同一枚 `leaseToken`，不发第二张票 | 400 `INVALID_PARAM`（**本路由的所有形状失败都是这一支码**，`message` 点名是哪个字段）、409 `task_busy`（在场租约属于另一个 task，**或 quarantine 仍在场**——quarantine 期不能靠重新 acquire 绕开 §8.3）、409 `browser_scene_version_conflict`（指纹与当前现场不符，含我方还没有现场读数）、503 `NO_BROWSER_CONNECTED`（点名非绑定浏览器，或绑定浏览器已断开）。锁**不因超时自动解除** |
| POST | `/releaseSceneLease` | body：`{browserID, leaseToken, keepLocked}`（`keepLocked` **缺省 `false`**） | `{browserID, state:"released"\|"quarantined"}`（`keepLocked=true` ⇒ 后者，并把 `{browser_id, session_id, task_id, retired_lease_token, scene_identity, quarantined_at}` 记进 quarantine；旧 token 从此只剩"已退役"这一个身份，§8.3 第 ③ 步就靠它拒迟到写入） | 400 `INVALID_PARAM`（缺字段、`keepLocked` 不是布尔、**没有租约在场**、`browserID` 不是持有者、`leaseToken` 与当前持有者不符——这五种不造新码，`message` 里点名是哪一个） |
| POST | `/resolveUncertain` | body：`{browserID, sessionID, transactionID, evidence{receipt_status, live_digest, expected_digest, late_write_excluded}, resolution:"resume"\|"new_session"}`——`transactionID` **键必填**、值可为 `null`，且 `null` **只能**配 `receipt_status:"missing"`；两个 digest 可为 `null`（没读过 live 就是没读过） | `{state:"resolved"\|"kept_locked"\|"quarantined", sessionID, scene_id, scene_version, scene_sub_version, sceneDigest, reason}`——**四元组始终随答**，`sessionID` 只在 `resolved` + `new_session` 那一支为 `null`（阶段本身仍由 `POST /sessions` 才退回 `setup`）。`resolved` 同时清 lease 与 quarantine；第 ④ 步的 live 读**只作证据**：抬 id 水位、不采纳文档，本路由不得成为改变现场的第二条路 | 六步（流程文档 §8.3）任何一步不满足 ⇒ `200` + `kept_locked`（**这是结论，不是错误**，且照样落盘）；`quarantined` 是给"证词与账本相互矛盾"的（账本状态与 `evidence.receipt_status` 不符、`evidence.live_digest` 与我方现读不符、回执四元组与现场不符、声称有回执而我方无回执）；400 `INVALID_PARAM` |
| GET | `/brainCapabilities` | `browser_id` | `{protocol:"brain-web/2", browserID, capabilities:[8 项]}`（名单与逐项承诺见流程文档 §5.2） | 400 `INVALID_PARAM`、503 `NO_BROWSER_CONNECTED` |
| GET | `/sessions` | `browser_id` | `{sessionID, session_phase, browserID, camera_id, lease, quarantine, camera_candidates}`（`camera_candidates` 是绑定浏览器上报的候选相机 id 清单，只列不选；没有上报、或上报的是已被另一份现场取代的旧清单 ⇒ `[]`，流程文档 §9 item 3） | 503 `NO_BROWSER_CONNECTED` |
| POST | `/sessions` | body：`{browserID, sessionID?, camera_id}` | `{sessionID, session_phase, browserID, camera_id}`；**这是唯一把阶段退回 `setup` 的入口** | 400 `INVALID_PARAM`、409 `browser_new_session_required`（`scene_id` 已变）、409 `task_busy`／`uncertain` 期拒绝切换（透传 Brain 的码）。忙碌判定与 `camera_id` 首绑后不得改（换相机＝新会话，§5.15） |
| GET | `/allocateObjectID` | `browserID`、`count`(uint，≥1) | `{"object_ids":[<uint>,…]}`，长度＝`count`，水位**原子**推进 | 400 `INVALID_PARAM`、409 `object_id_space_exhausted`（上限 `2**32-1`，§5.5）、503 `NO_BROWSER_CONNECTED` |
| GET | `/tasks/{taskID}/artifacts/{name}` | `name` **只接受白名单四项** `scene_config`\|`scene_patch`\|`commands`\|`query_result` | 对应产物的字节，**连 Brain 的状态码与 `Content-Type` 一起原样透传**（gateway 只判 `taskID` 是 uint32、`name` 逐字命中白名单，两关不过 ⇒ `400 INVALID_PARAM`，根本不发出上游请求；那条 404 是 Brain 对自己存储的证词，**不**由本进程的 job 表决定，§13 O10；错误体同样是 Brain 自己的字节——管道不改写） | 400 `INVALID_PARAM`（白名单外、含任何路径分隔符）、404 `TASK_NOT_FOUND`（上游答的）、502 `UPSTREAM_ERROR`、501 `AGENT_UNAVAILABLE`、504 `AGENT_TIMEOUT` |

浏览器侧两条（前端 → gateway，**R9／Q115**）：

| 方法 | 路径 | 参数 | 成功响应 | 专属错误码 |
|---|---|---|---|---|
| GET | `/api/sessionState` | `browser_id` | `{session_phase, lease, quarantine, browser_id, next_object_id, scene_id, scene_version, scene_sub_version, sceneDigest, camera_candidates}` —— 刷新后找回阶段的**权威**读数（§5.4 三读数点里以它为准）。`next_object_id` 是本地分配器的种子（§5.5 只认这一个种子）。**`lease` 里的 `leaseToken` 一律抹掉**（`quarantine` 同理只留 `retired_lease_token`）——这条是每个 tab 都在轮询的读数，回出票面就等于把"冒充持有者过 §9.1 租约闸"的能力发给每个旁观者；持有者仍在 `captureScene`／`get2DAABB` 的响应帧里读到它要行动的那枚票。第十个键 `camera_candidates` 是这一行的**唯一例外**：前九个都是磁盘文件的读数，它却是浏览器对文档的**上报**（`browser.state_sync` 的 `camera_object_ids`，流程文档 §9 item 3），因此只活在进程内存里，且上报那份清单的现场一旦被四元组不同的另一份取代就回 `[]` —— Gateway 不读文档、也永不端出一份旧清单 | 503 `NO_BROWSER_CONNECTED` |
| GET | `/api/agent/health` | 无参 | Brain `GET /health` 的响应**原样透传**（一个字段都不裁、不改名），缓存 ≤5 秒，命中缓存时带响应头 `X-Agent-Health-Age: <秒>` | 502 `{"status":"unreachable","detail":"…"}`（**不得返回 200 冒充健康**） |

另有一条**调用方侧**的 `GET /health`（根路径，与上表同族）：向 `{agent_service_url}/health` 请求一次，**3 秒超时**，响应体原样透传，不可达／超时／非 JSON ⇒ 同上的 502 `unreachable`（流程文档 §5.3）。它与 `/api/agent/health` 是同一条转发的两个入口，`/api/agent/health` 多一层 5 秒缓存是因为它有 15 秒一次的浏览器轮询读者。

四写入路由（`updateWorld`/`updateItem`/`addObject`/`removeObject`）的成功响应**统一带两个字段**：`scene_sub_version`（Agent 写回后恒为 `0`，§3.9）与 `scene_digest`。理由是调用方（Brain）的下一步 `expected` 四元组与 `commitWorld` 必须能直接复用上一次写入的结果，`scene_version` 单独一个数不足以定位现场。这不改既有四个键的含义。

保留：`GET /api/health`（本期扩三个分项，见 §5.15 开头的"服务健康"）、`GET /events`（升级双向）、`GET /api/runtimeConfig`（新，见 §9）、静态资源兜底路由、**Agent 会话路由原样保留**；**Agent 任务路由为真转发**（`POST /api/agent/jobs` 转发 `POST /startTask`、`GET /api/agent/jobs[/{jobId}]` 读 gateway 的任务表，细则 §5.15、**Q86**），其中 `POST /api/agent/jobs/{jobId}/cancel` **删除**（§10.1）。
删除：见 §10。
**样例的覆盖面**：`examples/gateway_client_demo.py` 演示的是**上面第一张表**那些面向通用调用方的接口；brain-web/2 新增那两张表的读者是 Brain 服务（正确性由 §12.17 的联合验收覆盖，Python 替身在 `tests/` 里钉），本期**不扩**样例脚本。
**调用方样例**：`examples/gateway_client_demo.py` 按本表**第一张**逐条演示（11 个接口 + §5.13 的错误族），样例世界是内嵌的 OLS scene 6，**不读世界文件**（`/updateTraj` 那一步读的是与本脚本同目录、一起分发的两份样例轨迹，**Q103**）；依赖口径见 **Q84**。同一次运行里还有一步**连续写入的动画演示** —— 用 `/updateItem` 逐拍把 `shoulder_pan_joint` 以 1 rad/s 推到 2π（与 `/updateTraj` 的"一次交付一段动作"互为对照），路由分工、时基、限位与取证判据见 **Q85**。

### 5.10 `/captureScene` 响应体
统一 JSON + base64（避免 multipart 的客户端兼容问题）：
```json
{
  "request_id": "…", "id": 2, "type": "rgbd",
  "width": 640, "height": 480, "camera_type": "rgbd",
  "image_encoding": "png", "image_base64": "…",
  "depth_encoding": "float32le_meters", "depth_base64": "…",
  "depth_unit": "meter", "depth_invalid": "NaN",
  "captured_at": "2026-10-08T21:30:00Z", "scene_version": 7, "frame_index": 340
}
```
- `type=rgb` 时不含 `depth_*` 字段。
- `type=rgbd` 时 `depth_base64` = `width*height*4` 字节、**小端 float32**、行主序、**首行为图像顶行**，值为相机光轴 +Z 方向的米制距离；未命中/超出 `far` 的像素为 `NaN`（float32 quiet NaN）。
  明确否决 16-bit PNG 深度（毫米精度不够、且无法表达 NaN）。
- `frame_index`：播放模式下为当前帧号；非播放模式为 `null`。
- 请求 `type` 必须等于该实体 `Camera.type`，否则 `400 CAMERA_TYPE_MISMATCH`（不隐式改相机模式）。
- 渲染尺寸/内参取该实体 `Camera` 组件（`fov` 垂直角度换算、`resolution`、`near/far`）；不接受调用方覆盖（v1）。

### 5.11 `/get2DAABB` 响应体
```json
{
  "request_id": "…", "id": 3, "camera_id": 2,
  "width": 640, "height": 480, "coordinate_origin": "top_left",
  "visible": true, "clipped": true,
  "box": { "x_min": 120.4, "y_min": 88.0, "x_max": 301.2, "y_max": 260.7 },
  "aabb_world": { "min": [0.35, -0.04, 0.0], "max": [0.43, 0.04, 0.08] },
  "frame_index": null
}
```
- 算法：世界 AABB（`renderer.worldAabb()` 递归物体自身的渲染几何，**递归止于另一个物体 id 的节点**（**Q148**：挂在腕上的夹爪与相机只进它们自己的盒，不进臂的 `aabb_world`，判据见 §4.8.6 第 2 条），**排除 §4.4 的编辑层**：相机 marker 与视锥线、灯 marker 与标签、`*Helper`、选择框；含父链与当前播放帧位姿，**Q50**）→ 8 角点投影到相机裁剪空间 → 丢弃 `w<=0` 角点 → 剩余角点取像素矩形 → 与 `[0,width]×[0,height]` 求交裁剪。**同一条排除清单也是屏幕上选中描边的形状**（`Box3Helper(worldAabb(id))`，**Q105**）—— 这条路由 `worldAabb()` 单点提供，`clipped`/`aabb_world` 与用户看见的盒子不可能分叉。
- 像素坐标，原点在**左上角**，x 向右、y 向下；保证 `x_min<=x_max`、`y_min<=y_max`。
- 相机视锥内一点都没有（全在身后或全出画）：`HTTP 200` + `"visible": false, "box": null, "clipped": true`（**不是 404**，物体确实存在）。
- AABB 基于**当前帧真实几何**；无 `MetadataRef` 的物体用其可视节点/包围体，若完全无可渲染几何 → `400 EMPTY_AABB`。**Q50**：相机与灯的可视节点本身就是编辑层，排除后量不出东西，故 `/get2DAABB?id=<相机或灯>` 回 `400 EMPTY_AABB`（`frame()` 对它们同样是 no-op）。

### 5.12 `/updateTraj` 响应体
形状由 **Q99** 定：响应侧是 `trajs` 数组，**无顶层平铺字段**。每一条的字段与旧的 8 个**逐字相同**，只是不再带 `ok`；`index` 是本条在请求 part 顺序里的下标（0 起），调用方据此把自己手上的文件对上回读。
```json
{
  "ok": true, "count": 2,
  "trajs": [
    {
      "index": 0, "fps": 60, "total_frames": 720, "duration_seconds": 12.0,
      "object_count": 3, "attach_count": 2,
      "ignored_objects": [{ "object_id": 9, "reason": "OBJECT_NOT_IN_SCENE" }],
      "warnings": ["ATTACH_PARENT_MISSING@frame=530"]
    },
    { "index": 1, "fps": 30, "total_frames": 240, "duration_seconds": 8.0,
      "object_count": 2, "attach_count": 0, "ignored_objects": [], "warnings": [] }
  ]
}
```
- `count` 恒等于 `trajs.length`，也恒等于请求里的 part 数（§5.3 的 gateway 校验）。
- **`ignored_objects`/`warnings` 是逐条判定的**：同一个世界对第 1 条认识的 id，对第 2 条可能不认识；两组数字都只描述"这一条与当前场景的关系"，不跨条合并（**Q35** 的"文件事实 vs 生效情况"分工在每条内部照旧成立）。
- **响应里没有"当前选中哪条"**：浏览器播放哪一条是 UI 状态（§7.1 的轨迹选择器），不是 wire 状态；`/getWorld` 也永远读不到它（轨迹不是世界，**Q33**/§5.4）。调用方要复现自己的顺序就按 part 顺序，第 0 条即载入后默认在播的那条。

### 5.13 通用错误码表（gateway 与浏览器共用同一 `code` 命名空间）

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `MISSING_PARAM` / `INVALID_PARAM` | 必填参数缺失；`id` 非 uint、负数、非整数；`type` 不在枚举 |
| 400 | `INVALID_WORLD_SCHEMA` | 世界 json 顶层缺 `objects`/类型错/`scene_schema_version` 不支持。**缺 `scene_sub_version` 不属于此类**（缺省即 `0`，§3.1） |
| 400 | `BODY_ID_MISMATCH` | `updateItem` 的 `id` 与 body 内 `object_id_in_scene` 不一致 |
| 400 | `MISSING_TRANSFORM_COMPONENT` | `addObject`/`updateItem` 描述非法（无 `Transform` 且未允许） |
| 400 | `INVALID_PARENT_CYCLE` | `parent` 造成环或指向不存在的 id（两遍解析后仍失败） |
| 400 | `CAMERA_TYPE_MISMATCH` | 请求 `type` ≠ 实体 `Camera.type` |
| 400 | `EMPTY_AABB` | 目标物体无可渲染几何 |
| 400 | `gateway_scene_revision_invalid` | `/commitWorld` 的 candidate 里 `scene_sub_version != 0`（§3.9、§5.9、流程文档 §8.1）。gateway 在派给浏览器**之前**就能判，是 gateway 自产的码 |
| 400 | `TRAJ_INVALID_MAGIC` / `TRAJ_UNSUPPORTED_VERSION` / `TRAJ_TRUNCATED` / `TRAJ_POSE_DIM_UNSUPPORTED` | 头部校验失败（magic 必须数值比对 `0x5452414A`；`version` 必须为 2；`data_offset+frame_count*pose_dim*4` 不得超过文件长度；`pose_dim` 必须为 7）。**多条轨迹时按 §5.3 的全有或全无执行**，消息里点名是第几条（`trajs[<i>]: …`），已有列表不动 |
| 404 | `OBJECT_NOT_FOUND` / `CAMERA_NOT_FOUND` | `id`/`camera_id` 不在当前场景 |
| 404 | `MODEL_NOT_FOUND` / `SCENE_NOT_FOUND` | OLS 返回 404 时由 gateway/浏览器透传语义 |
| 404 | `TASK_NOT_FOUND` | `GET /api/agent/jobs/{jobId}` 的 id 不在 gateway 的任务表（§5.15） |
| 409 | `ID_ALREADY_EXISTS` | `addObject` 指定了已存在的 `object_id_in_scene` |
| 409 | `VERSION_CONFLICT` | `expected_version` 不匹配 |
| 409 | `scene_switch_forbidden_after_first_task` | `agent_owned` 期试图换现场：`Load Scene`／资产面板预设场景，以及把另一个 `scene_id` 送进 `/updateWorld` 的那次写入（§4.6、§4.8 的 ② 类闸门）。浏览器侧在页面上就被闸门挡下，所以这个名字出现在**视口通知与诊断**里，而**不是** `browser.response` —— 那一次尝试根本没有发到线上 |
| 409 | `manual_edit_forbidden` | `agent_owned` 期出现 ② 类内容改动：gateway 收到 `scene_sub_version` 推进的 `browser.state_sync`，或同版本却带了新对象的上报（§5.8、§4 三层防线的第 2 层）。现场保持不覆盖，回错后 gateway 持有的那份仍是上一份 |
| 409 | `object_id_space_exhausted` | `/allocateObjectID` 时持久高水位已达 `2**32-1`，号段发不出来（§5.5） |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体 > 100 MB |
| 422 | `MODEL_METADATA_INVALID` | OLS metadata 缺 `path` 或字段类型错 |
| 424 | `DEPENDENCY_FETCH_FAILED` | `reference`/mesh 子文件拉取失败（列出失败项） |
| 429 | `TOO_MANY_REQUESTS` | 挂起请求超过上限 |
| 500 | `INTERNAL_ERROR` | 未预期异常 |
| 501 | `AGENT_UNAVAILABLE` | `agent_service_url` 为空时提交 Agent 任务（§5.15） |
| 502 | `UPSTREAM_ERROR` / `UPSTREAM_RENDER_FAILED` / `GATEWAY_FRAMING_ERROR` | OLS/COS/Agent 服务 5xx 或网络不可达 / 浏览器渲染异常 / §5.3 长度不符 |
| 503 | `NO_BROWSER_CONNECTED` / `SCENE_NOT_LOADED` | 无已连接浏览器；无世界时收到 item/traj 类请求 |
| 504 | `BROWSER_TIMEOUT` | §5.2 超时 |
| 504 | `AGENT_TIMEOUT` | gateway 调用 Agent 服务的 HTTP 请求超过 `agent_request_timeout_seconds`（§5.15） |

OLS 自身的错误码沿用 `OLS接口.md`（400/403/404/500 + `{"error":..., "message":...}`），**不要求 OLS 改成 gateway 的信封格式**；gateway/浏览器把 OLS 错误映射为上表 code 并保留原始 message。

**两套命名共存，不是笔误**：上表里 `SCREAMING_SNAKE` 是 gateway／浏览器码，`lower_snake` 是 brain-web/2 新加的四枚 —— 它们的名字**逐字继承 Brain 侧码表**（流程文档 §10），同一件事在两边必须叫同一个名字，反向对账测试才不会变成两份真值。**大小写不做任何转换**：谁产的就谁原样回，gateway 的透传路径（`/commitWorld`／`/sessions` 那些）也不把它们改成大写。
**Brain 的其余既有码不进这张表**：`browser_scene_version_conflict`、`browser_new_session_required`、`browser_asset_replacement_requires_new_id`、`scene_object_id_reused`、`request_id_reused`、`camera_frame_scene_mismatch`、`gateway_object_allocator_invalid`、`gateway_protocol_upgrade_required`、`gateway_new_scene_version_invalid`、`gateway_target_version_invalid`、`web_uint32_required`、`web_platform_profile_unsupported`、`task_busy`、`session_context_required` 都是 **Brain 直接回给调用方、gateway 原样透传**的答复，浏览器从不产它们。**三枚例外也由 gateway 自己产出**：`gateway_target_version_invalid` 与 `web_uint32_required`（`/commitWorld` 的"目标恰好 `v+1`"和"`expected` 三个计数器是 uint32"两道闸在把 candidate 交给网络之前就能判，而 candidate 一旦下发就可能落地，所以闸门必须留在派发之前）；`camera_frame_scene_mismatch`（只在 `/captureScene`／`get2DAABB` 这两条同步读上——身份六项（`browserID`／四元组／`playback_state`）是响应**带回来的**，比对用的是 gateway 自己那次 live read 存下的指纹，流程文档 §9.1 的身份闸；浏览器一侧仍然照旧产 `CAMERA_NOT_FOUND`／`CAMERA_TYPE_MISMATCH`／`UPSTREAM_RENDER_FAILED`）。Brain 侧的同判据留作第二层，两侧对同一件事用同一个名字，不构成第二份真值。而 **`browser_scene_revision_conflict` 明确撤回、不得实现**：四元组"词典序不增"用既有的 `browser_scene_version_conflict` 就够，再造一枚同义码会让反向对账测试出现两个真值。

**表就是契约（Q57）**：`local_gateway.py:BROWSER_ERROR_STATUS` 必须与上表**浏览器上报的那些行逐行相等**，`tests/test_error_mapping.py` 用一条相等断言钉住，并把每个码真的走一遍 `/updateItem` 往返（校验状态、`code`、原始 message 与 `error.request_id`）。表里没有的码一律 502，缺 `code` 的错误信封按 `UPSTREAM_RENDER_FAILED` 502，`error` 不是对象时同样落到 502 而不是抛异常。**gateway 自己 raised 的 413/429/503/504 不进这张表**——它们发生在"浏览器还没答"之前，由 `tests/test_caller_apis.py`/`test_traj_api.py`/`test_ws_protocol.py` 各自覆盖。Agent 那三行（`501 AGENT_UNAVAILABLE`、`404 TASK_NOT_FOUND`、`504 AGENT_TIMEOUT`）同理：它们是 **gateway 或 Agent 服务**的答复，浏览器从不报这些码，**不进** `BROWSER_ERROR_STATUS`（`tests/test_gateway_agent.py` 钉住这三条的往返，**Q86**）。**brain-web/2 新增的四枚（`gateway_scene_revision_invalid`、`scene_switch_forbidden_after_first_task`、`manual_edit_forbidden`、`object_id_space_exhausted`）落在这同一族里**：它们在表里（因为调用方会收到），但**没有一枚由浏览器上报** —— 前两枚在 gateway 派单之前就能判，`manual_edit_forbidden` 判的是浏览器自己送来的 `state_sync`（那是一次**上行**，走 §5.8 的采纳路径而不是 `browser.response`），`scene_switch_forbidden_after_first_task` 在页面上就被闸门挡下。`tests/test_error_mapping.py` 的相等断言**同步扩**：第四份显式名单，断言这四枚 ∈ 规格表、∉ `BROWSER_ERROR_STATUS`，并逐条钉住它们的状态码（400/409/409/409）。
**浏览器答回来的任何判定必须原样回给调用方**（**Q58** 经 **Q111** 升格为通则并加强）：4xx／`rejected_objects[]`／`ok:false` 的 `error.code` 都不得被改写；gateway 持久化那份 json（§1.4）**不得**参与任何读取回退、也不得用来"补齐"浏览器没答的部分（没有缓存兜底，503/504 就是终答）。

**表的另一头也要钉住（Q60）**：**浏览器代码里能抛出的码必须要么在表里，要么被显式声明为不参与 §5.13 的渲染期码**。`editor/src/error_codes.test.ts` 扫 `editor/src/*.ts`（排除 `*.test.ts`）里的 `new WorldError|LibraryError|TrajError("CODE"`，与 `local_gateway.py:BROWSER_ERROR_STATUS` 对账，三张名单都写成相等断言而不是包含关系：**`RENDER_ONLY`**（当前只有 `INVALID_URDF`：`renderer.ts:loadModel` 把它收成一条诊断，永远不进 `browser.response`）、**`RESERVED_BY_SPEC`**（`SCENE_NOT_FOUND`/`SCENE_NOT_LOADED`：表里有、本构建没有处理器会答）、以及"抛出的码 ⊆ 表 ∪ RENDER_ONLY"。反向的对账（表 = §5.13）由 `tests/test_error_mapping.py` 负责，两边合起来才是整条链。

### 5.14 `/removeObject` 细则
- 级联范围：沿 `Transform.parent` 反向索引收集**整棵子树**并全部删除；`detach_children=true` 时只删目标物体，其直接子物体的 `parent` 改为目标物体的原 `parent`（`null` 则挂场景根），孙节点层级不变。
- 删除结果允许为**空场景**（0 物体），这是合法状态：此后 `captureScene`/`get2DAABB`/`updateItem` 一律 `404 OBJECT_NOT_FOUND`，`updateTraj` 因所有 `object_id` 都不存在而全部忽略并回报（§8.2）。
- 播放模式中的删除：被删物体若正被轨迹驱动，**其余物体的轨迹数据不受影响**，被删物体按 §8.2 规则忽略（首次发生时提示一次）；若删除的是当前 `updateTraj` 加载的机器人物体，仅跳过该物体。**Q76**：删除本身是写入，所以这一步同时会退出播放态（§7.3）。
- 幂等性：重复删除同一 id → 第二次返回 `404 OBJECT_NOT_FOUND`，不改 `scene_version`、不广播。
- 目标 id 命中"场景中唯一相机"时正常删除并在 `removed_object_ids` 中回传（不做保护性拒绝），调用方自行负责后续仍可 `addObject` 新相机。

### 5.15 Agent 任务链路（按 `docs/gateway与Agent新通信流程.md` §7/§14，**Q86/Q87** 基线 + **Q109/Q111/Q115/Q116**）

gateway 仍是**唯一**与 Agent 服务（Brain）通信的一方，浏览器永不直连它。一次任务的完整链路：

**服务健康（Q115，R9）**
- 浏览器**启动即查** `GET /api/health`，此后**每 15 秒**轮询；这一条路由为 `{status,mode,uptimeSeconds, agent_service:{status,detail}, browser:{connected,browser_id}, assets:{reachable}}`（§5.9）。
- `agent_service.status ∈ {ready, degraded, unreachable}`：Brain 的 `/health` 分项全好 ⇒ `ready`，任一不好 ⇒ `degraded`，取不到 ⇒ `unreachable`。gateway 侧的两条转发见 §5.9 的 `GET /health`（3 秒超时）与 `GET /api/agent/health`（缓存 ≤5 秒、`X-Agent-Health-Age`），三个分项**并发探、互不阻塞**，任一分项超时不影响整体 200 —— 这条路由的主语是"Gateway 活着"。`assets.reachable` 复用既有的 `GET {library_server_url}/list?type=model`（§6.11），不新造探活路由。
- **硬边界**：健康读数**只进服务状态条，绝不进 job 视图**。一次探活失败**不得**把在跑的任务判成失败 —— job 的状态只由下面第 4 步的 `terminal`、404、以及"连续 5 次轮询失败"上限三者决定（流程文档 §14.6 第 5 条）。
- 第二标签页同样轮询；**不新增 WS 帧**（阶段有 `session.phase_changed`，健康没有，§5.4）。

1. 浏览器把**新一轮命令 + 当前场景**发给 gateway：`POST /api/agent/jobs`，body `{"command": "<用户输入>", "scene": <世界 json>, "clientRequestID": "<uuid>"}`。`scene` 就是 `World.toJSON()` 的那一份（§4.8.2 导出用的同一个函数），因此 **② 类改动（面板增删）一定在里面**（它同时推进 `scene_sub_version` 并上报，§4.8/§5.8），**③ 类改动（灯光预设）一定不在里面**（渲染层覆盖，不进文档，§4.8.1）；位姿不能被本地编辑（① 类已删除）。gateway 对 `scene` 只做"是不是 JSON 对象"与尺寸校验（§11.1/§11.2），**永不解析其语义、永不补默认值、永不删未知字段**。
2. **受理前实时取现场**：gateway 用 `browser.request{action:"getWorld"}` 向**绑定浏览器**读当时那份，并校验响应头自洽（`X-Scene-Sub-Version` 与正文同源、`X-Scene-Digest` 与正文相配，§5.8）。**不得**用第 1 步 body 里那份或 `browser.state_sync` 的持久副本顶替（流程文档 §14.6 第 1 条）。同一 `clientRequestID` 且载荷相同 ⇒ 返回原任务（幂等）；同 id 不同内容 ⇒ `request_id_reused`。
   **S3-f 落地口径**：幂等键是 `(sessionID, clientRequestID)`，比较的载荷是**第 1 步 body 里的 `{command, scene}`**（用 `digest.py` 那份规范化算摘要）——实时读取允许在两次重试之间移动，所以它不进幂等判据。幂等表与 job 表同生命周期，都在 gateway 进程内存里（§13 O10），Gateway 重启把两者一起清空。"响应头自洽"里 `X-Scene-Digest` 是**构造成立**的：浏览器不报摘要（§5.8 的 `getWorld` 载荷只有 `world`／三个版本字段／`playback_state`），摘要由 gateway 从正文算出；真正会被校验的是 `scene_id`／`scene_version`／`scene_sub_version` 三个平铺字段与正文的同源（§5.1）。
3. gateway 转发 `POST {agent_service_url}/startTask`，body `{command, scene, sceneDigest, scene_id, scene_version, scene_sub_version, sessionID, clientRequestID, browserID}`（四元组 + 摘要 + 绑定，来自第 2 步那**同一次**读取；**`scene` 本身也是那一次读到的文档**，第 1 步 body 里那份只作形状／尺寸校验与幂等比较，永远不会被当成转发内容），响应取 `taskID`（int）、`state`、`terminal`（**必须为 `false`**；上游给 `true` 一律忽略并 warn，终态以第一次轮询为准，§7.4 落点）。**S3-f 落地口径**：`sessionID` 在 Brain 尚未经 `POST /sessions` 开会话时如实传 `null`，gateway 不替它造一个；`browserID` 就是绑定浏览器，取自持久状态而非请求体。**"每次发送只发新一轮的命令和场景"不变**：一份命令对应一个新任务，gateway 不拼接历史。Brain 接受任务那一刻会话进入 `agent_owned`，gateway 同批广播 `session.phase_changed`（§5.4）把 ② 类入口关掉 —— **不需要用户刷新**。
4. gateway 起一个**每 `agent_poll_interval_seconds`（默认 5 秒）跑一轮**的 asyncio 轮询任务，只对 **`terminal == false`** 的 job 逐个 `GET {agent_service_url}/queryTaskState?taskID=<int>` → `{taskID, state, terminal}`。轮询用 `await asyncio.sleep()`，**不得**阻塞事件循环（坑 10）。
   **运行状态词表归 Brain（Q116，R10）**：`state` 是 Brain 自定义的**任意非空字符串**，Brain 增加、改名、合并状态都不需要我方改代码。我方**不得内置状态枚举、不得翻译、不得按状态名判断结束**，唯一终态判据是同一响应里的 **`terminal` 布尔**（缺失、`null`、`"true"`、`1` 一律算非终态 + warn）。**字段来源表**（不存在"Agent `state` → `status` → 面板文案 → CSS 类"的映射表）：

   | 字段 | 来源 | 用途 |
   |---|---|---|
   | `agent_state` | `/queryTaskState` 的 `state` **原文** | 面板**唯一**显示来源，原样回显，不翻译、不映射 |
   | `terminal` | 同一响应的布尔 | **唯一**停轮询依据；`/startTask` 响应里的它必须为 `false` |
   | `state_detail` | 上游可选文本，无则 `null` | 进度附注，原样透传 |
   | `outcome` | 终态后 `GET /queryTaskResult`，**固定五值**（下表） | 结果分类；CSS 类与结果区块都按它 |
   | `failure_reason` | `terminal && outcome=="failed"` 时取 `/queryTaskFailureReason` | 失败原因；非失败终态回 `null` 且上游必须 200 |

   - CSS 类不由状态名派生：`terminal==false` ⇒ `is-pending`，终态按 `outcome` 给 `is-done`／`is-failed`。前端**不得再维护第二份终态表**（不存在 `terminalAgentStatuses` 那样的 `Set`）。
   - **运行状态 ≠ 结果分类**：`state` 是 Brain 的自由词表（进程此刻在做什么），`outcome` 是 brain-web/2 的固定五值（这次任务对用户意味着什么）。
   - 唯一的历史例外：Agent 面板的**本地消息历史**存在 `localStorage` 里，刷新后读回来的旧行没有 `terminal` 字段 ⇒ 显示层对**本地历史**按 `terminal ?? true` 处理（否则旧任务永远转圈）。这条只影响 spinner，不参与任何协议判定。
5. **执行期持锁**：Brain `POST /acquireSceneLease{taskID, browserID, 四元组}` → gateway 校验绑定与现场指纹 → 停/拒轨迹播放、冻结切场景、**灯光预设复位并冻结**（§4.8.1、§2.3 施工项）→ 回 `{browserID, leaseToken}`；此后它的每个请求带 `X-Brain-Lease`。锁的语义就是这三件事，**② 类编辑不由锁管而由阶段管**（§4.6）；锁**不因超时自动解除**，断线／未知／超时一律保持 `quarantined`（流程文档 §7.2、§8.2）。
6. Agent 在「进行中」期间反过来调用 §5.9 的调用方接口（`updateWorld`/`updateItem`/`addObject`/`removeObject`/`updateTraj`，外加 brain-web/2 的 `commitWorld`/`queryWorldCommit`/`allocateObjectID`/`captureScene`/`get2DAABB`）—— 走的是既有通道；按 **Q76**，这些写入照常退出浏览器的播放态。整份替换现场一律走 `POST /commitWorld`（CAS + 全有或全无，§5.9、流程文档 §8.1），**不额外自增版本**：目标恰好 `previous.scene_version + 1`，candidate 的 `scene_sub_version` 必须为 `0`。
7. `terminal` 翻 `true` 时 gateway 各取一次 `/queryTaskFailureReason` 与 `/queryTaskResult`，把 `outcome/reply/plan/metrics.model_calls/artifacts/commitReceipts` 落 job 视图，**一帧**经 `/events` 广播 `{"type":"agent.job_updated","payload":<AgentJob>}`（不分两次推送）；浏览器**不自己轮询**任务（**Q87**）。
8. 结果展示按 `outcome` 五态分支（流程文档 §7.3）：

   | `outcome` | 面板显示 | 明确禁止 |
   |---|---|---|
   | `scene_updated` | 场景已更新 + 新的 `(scene_id, scene_version+1, 0, sceneDigest)` | "机械臂执行成功" |
   | `answered` | 回答文本（`reply`） | — |
   | `clarification_required` | 澄清问题，等用户补充 | 当成失败、当成已执行 |
   | `plan_ready` | **计划已生成，尚未执行** + 技能顺序（`plan.commands`）+ 产物下载 | 任何"完成执行"字样 |
   | `failed` | 失败原因（附原始结构化错误） | 静默显示成"成功" |

   技能顺序取 `plan.commands`，Qwen 调用数取 `metrics.model_calls`（必须是真实计数）；产物只列白名单四项 `scene_config`／`scene_patch`／`commands`／`query_result`，经 §5.9 那条代理路由取，`debug/` 与服务器路径**不展示、不下载**。

- `AgentJob` 载荷：`{id, task_id, command, agent_state, terminal, state_detail, outcome, reply, plan, metrics, artifacts, commit_receipts, message, failure_reason, created_at, updated_at, conversation_id}`（`poll_errors` 是内部计数，不外泄）。`commit_receipts` 是 §8.1 回执的**本方账本读数**（每项即 `GET /queryWorldCommit` 的那个形状，磁盘无回执 ⇒ `status:"unknown"`），Brain 在 `commitReceipts` 里点的名只是"该查哪些事务"，不是结论。**不存在 `status` 这个 token 字段**；`id` = `str(taskID)`；`message` 取 `agent_state or failure_reason or ''`，gateway 自己**不撰写任何中文文案**。**载荷里绝不含 `scene`** —— 那份世界可能上百 MB，gateway 只在转发给 Brain 的那一刻持有它。
- `GET /api/agent/jobs` 返回 `{"jobs":[<AgentJob>,...]}`（按提交顺序）；可选查询参数 `conversationId` 只返回该历史行的任务。REST 与 WS job 均带 `conversation_id`（无关联时为 null），前端仅应用与当前历史行匹配的更新，异步返回后再次核对归属。`GET /api/agent/jobs/{jobId}` 返回单个 `AgentJob`，id 不认识 → `404 TASK_NOT_FOUND`。
- 提交携带捕获时的 `conversationId` 与 `sessionID`；保存用户消息期间会话或场景变化则不再发送任务。Gateway 在实时场景读取前后检查 session，已切换则返回 `409 conversation_session_mismatch`。消息追加只更新指定历史行，不隐式激活它。
- 轮询期错误（**两条上限不因 Q116 改动**）：Brain 答 404（它重启后不再认识这个 `taskID`）→ 该 job 置终态、`outcome="failed"`、`agent_state` 写上游 `error` 原文；连接被拒／超时／响应非 JSON → **保持非终态**并累计连续失败次数，达到 **5 次**（≈25 s）才落 `failed` 并停止轮询 —— **Q115 的探活结果也不许提前判死它**。
- 提交期错误：`agent_service_url` 为空 → `501 AGENT_UNAVAILABLE`（部署可以显式关掉这条链路）；`command` 缺失/空/非字符串 → `400 INVALID_PARAM`；`scene` 缺失或不是对象 → `400 MISSING_PARAM`；缺上下文字段 → `409 session_context_required`（**两个生产者**：`clientRequestID` 缺失或不是 1–128 个 `[A-Za-z0-9._-]`；没有绑定浏览器，于是第 2 步无处可读——注意这与实时读取自己报的 `503 NO_BROWSER_CONNECTED` 是两件事）；同一 `clientRequestID` 撞上不同载荷 → `409 request_id_reused`；上一任务未到终态 → `409 task_busy`。**这后三条全部在第 2 步实时读取与第 3 步转发之前判定**，一次拒绝不许留下半个 job、也不许多读一次现场。转发失败（不可达、非 2xx、缺 `taskID`）→ `502 UPSTREAM_ERROR`，**不落任何 job**。
- 不存在 `POST /api/agent/jobs/{jobId}/cancel`（已删除，§10.1）：Agent 服务没有取消接口。
- 会话与阶段：`POST /sessions` 显式新建／切换是**唯一**把阶段退回 `setup` 的途径，刷新页面不解禁（§4.6、**Q111**）；`browser_new_session_required` 现在**只由 `/sessions` 那道路由**在换相机／会话时抛（§5.9 路由表），`browser.state_sync` 的换场景**不再**回它——`scene_id` 一变就无条件采纳（§5.8，2026-11-05 修订）。会话（conversation）的本地 CRUD 与 `agent.debug` 帧**不变**：前者仍在 gateway 内存里，后者由新的样例 Agent 服务**不产生**任何 debug 帧，因此 Agent Debug 面板保持为空是符合预期的，不是缺陷。

---

## 6. 模型数据获取规则（原需求答复 9）

6.1 解析 `MetadataRef.properties.path`（**Q1**，真实示例：`"$LIBRARY_SERVER/29"` = 桌面）：
 - 匹配 `^\$LIBRARY_SERVER/(\d+)$` → 提取数字作为 **OLS `model_id`**（语法上 0 合法，仅禁止负数；但**当前实装库的 model id 从 1 起、没有 0**，`tests/fixtures/scene_example.json` 里的 `$LIBRARY_SERVER/0` 是 stub 时代编号，只对那份夹具的 stub 库成立，**Q67**）。
 - 同时接受已替换的绝对 URL 形式 `^https?://<host:port>/(\d+)$` 与显式查询形式 `.../model?id=<n>`，一律归约为 `model_id`。
 - **禁止把 `path` 直接当可 GET 的 URL 使用**：`{library_server_url}/0` 并不是 OLS 的合法路由（OLS 只有 `/model?id=`、`/modelByName?name=`、`/robot?id=`、`/file`），必须走 §6.2。
 - `path` 为空、`<id>` 未替换成数字、或含 `../`/绝对路径/空字节 → 该物体**报错跳过**（`400 INVALID_PARAM`，提示一次），其余物体继续加载。
 - 查找键**只用 model_id，不用 name**（§2.2：name 可能重复）。
 - **`MetadataRef.properties.type` 参与选 id 空间**（`docs/场景文件描述.md` §4.2）：`type` 为 `"model"` 或 `"robot"` 时**优先**决定向 OLS 请求的路由，`path` 里的数字在该空间内解释 ⇒ `$LIBRARY_SERVER/2` + `type:"robot"` 取的是 robot 2，不是 model 2。`type` 缺失、为 `null`/空串时退回"**裸数字恒为 `model` id**"规则，因此老世界文件与资产面板写入的 `$LIBRARY_SERVER/robot?id=<robot_id>`（同时接受已替换的绝对 URL `http://<host:port>/robot?id=<n>`）行为不变（§4.6、**Q51**；机械臂**可以**被世界文件引用）。`type` 是其它值（`"scene"`、`"urdf"`、非字符串）→ **只告警不中断**（§3.4）：报告 `MetadataRef type … is neither "model" nor "robot", the path form decides`，该 `type` 丢弃后由 `path` 的形式决定，物体照常加载。
 - 解析结果为 `{kind, id}`：`^\$LIBRARY_SERVER/(\d+)$` → `model`（或 `MetadataRef.type` 指定的那一个）；`^\$LIBRARY_SERVER/robot\?id=(\d+)$` → `robot`；绝对 URL 由查询里的 `id` 与路径末段（`model`/`robot`）共同判定。**`$LIBRARY_SERVER/scene?id=` 一律拒绝**（`400 INVALID_PARAM`）：场景是世界文件，不是物体目录，`/file?type=scene` 在 OLS 侧本来就是 400（§6.3）。
6.2 取 metadata：`GET {library_server_url}/model?id=<model_id>`；机械臂同构但走 `GET {library_server_url}/robot?id=<robot_id>`（结构见 `OLS接口.md`）。真实 OLS 在这一条路由里已经把主元数据 `<name>.json` 与文件清单 `<name>_files.json` 合并成一个 JSON 返回（`server.py:_serve_asset_json(with_files=True)`），前端拿到的是单份文档；两者解析成同一份元数据结构，只是后续 `/file` 的 `type` 不同。
6.3 取模型文件，按 `is_on_cos` 分支：
 - `is_on_cos == true`：**直连云存储**。最终 URL = `{cos_base_url}/{path}`（`path` 已是相对路径，不做拼接校验以外处理）；`reference[i]` 同理逐个拉取。
 - `is_on_cos == false`：走 OLS 文件接口，**query 形式且必须带 `type`**（**Q2**）：`GET {library_server_url}/file?type=<model|robot>&id=<item_id>&path=<relative/path>`，对 `path` 与 `reference` 每一项各请求一次。
   `OLS接口.md` 未定义 `/file/<id>/<path>` 路由，实现时**不得**臆造该形式；`type` 缺失或为 `scene` → OLS 返回 `400`，`type` 只能是 `model`/`robot`（场景目录没有可下载的散文件）。
 - `reference` 是列表，**必须逐个遍历拉取**，任一失败 → 该物体报错（`424`），其余物体继续。
6.4 URDF：`path = "xxx.urdf"`，`reference = ["meshes/1.dae", …]`。路径拼写规则不变（同一 model 目录内相对路径）。
 拉取后在**浏览器端**解析 URDF，且必须处理内部 `package://` 引用：
 - `package://<package_name>/<rest>` → 解析为**同一模型目录下的 `<rest>`**，按 §6.3 规则取文件（对齐 `OLS接口.md` §额外说明-1 的服务端改写规则）。
 - 相对路径（如 `../` 或绝对路径）→ **必须拒绝**，报 `424` 并告警，不得逃出该模型目录。
 - 递归发现：URDF 引用的 mesh 若又引用贴图（DAE/STL 内引用），按同样规则在同目录内解析。
 - `<mimic joint="…" multiplier="…" offset="…">`（**Q69**）：被模仿的那条关节是**派生**的，值 = `multiplier * 源关节值 + offset`，`multiplier` 默认 1、`offset` 默认 0（URDF 规范自己的默认值）。解析阶段必须校验源关节存在、可动（`revolute`/`continuous`/`prismatic`）且模仿链不成环，否则整份 URDF 按 §6.8 以 `INVALID_URDF` 报一条诊断——静默跳过等于把矛盾藏起来。
6.5 OBJ/MTL：`MTLLoader` 加载 `*.mtl` 后引用的贴图同样按 §6.3 拉取；贴图缺失不阻断几何，降级为 `MeshStandardMaterial` 并告警。**反向规则（Q72）**：网格没有纹理坐标（OBJ 的面写成 `f v//vn`，因而 `geometry.attributes.uv` 不存在）时**不使用贴图** —— 着色器照样按 `uv=(0,0)` 采样，纯色占位图会把漫反射乘成 `Kd × 占位色 = Kd²`，比只按 `Kd` 上色更暗；此时置空 `material.map`，让 MTL 的 `Kd` 单独生效（`renderer.ts:dropUnusableTextures`，只作用于 OBJ 分支，带 UV 的资产与 dae/glb/fbx 一概不动）。`forceOverrideColor` 按 §6.8 丢贴图是另一条既定规则，两者不要混为一谈。
 - **面可以引用文件里还没出现的顶点：交给 `OBJLoader` 之前先把整份文件规范化**（**Q141 立、Q143 改判后的现行口径**："加载不失败、量测不中毒、坏面认账"）：`OBJLoader` **按文件顺序**读，`f` 里那些索引到尚未声明的 `v`/`vn` 的位置解析成 `undefined`，落到 `BufferGeometry` 上就是 **NaN 顶点**。出厂库 **210 份 OBJ 里有 6 份**这样，全部是 robotiq 夹爪的零件（2F-85 的 `pad.obj`／`silicone_pad.obj` 各 **900/1052** 个顶点与 **900/1200** 条法线写在 **1200** 个面之后，⇒ **2700** 个坏顶点引用 = 900 面 × 3；2F-140 的 visual 与 collision 那两对 finger 同族，面／坏引用 **6080/13680**、**2712/6102**、**928/2088**、**576/1296**）⇒ 这是**一整个夹爪族的导出习惯**，按资产 id 白名单排雷被否。**口径**：**OBJ 的正索引本来就是绝对的、从文件头数起**，所以把全部 `v`/`vn`/`vt` 声明整体排到第一条面语句之前**不改变任何一个索引指向谁** —— `logic.ts:normalizeObjSource`（纯字符串算法，落在 `logic.ts` 以便直接单测）读整份源、`renderer.ts:parseAsset` 的 OBJ 分支喂给 `loader.parse()` 的就是规范化后的那一份。于是那 6 份资产解析出**完整**网格（一份 pad **1200** 个面全部到位，而不是只能找到顶点的那 **300** 个），NaN 顶点从源头消失，§4.5 的描边、§4.8.6 的线框、`/get2DAABB`（§5.11）与 `frame()` 读的都是干净的量测。**同一遍里还要规范化负数索引**（用户口径「在第一遍的时候直接做规范化，这样即使后面有新的资产使用了负数索引也无所谓」）：`-1` 的意思是"此处已声明的最后一个顶点"（`OBJLoader` 自己按 `index + len/3` 解），挪动声明就会悄悄改掉它指向谁，所以同一遍把每个负数索引写成它已经意味着的那个**绝对**索引（`declared + value + 1`）；出厂库 **0** 份用负数索引、**0** 处 `\<换行>` 续行，这一手是给以后的资产买的保险。规范化之后**没有任何一份面依赖声明写在哪儿**。**触发判据必须精确**：判"**有面索引了此处尚未声明的顶点**"⇒ **恰好 6 份**；判"任何 `v`/`vn`/`vt` 出现在第一条面之后"⇒ **28 份**，多出来的 **22** 份是合法的分块资产（d455、d435i、ur3 visual ×7、ur10 visual ×5、dobot_cr5 Link1–5、nero link2/4/5），它们的索引都是绝对的、就地解析完全正确 ⇒ **不需要改动的文件原样返回、逐字节相同**（210 份里 **204** 份如此），因此不需要逐资产回归。**保留** §4.5 那条并集判项（`expandGeometry` 里的 `isFiniteBox`）：它管的是规范化没看过的坏零件，与"从哪份源解析"正交。**不修资产**（`ObjectLibraryServer` 不在本仓、且带 `model_index.db`，换名要重索引），不升 three，不加键或常量。判据 §12.37。
6.6 扩展名白名单：`OLS接口.md` 的 `allowed_extensions` 当前只有 `.obj .mtl .png .jpg .jpeg .urdf .stl .dae .gltf .glb .bin`，
 **不含 `.tga .dds .gif .bmp .exr`**，DAE/glTF 常见贴图会被判 403。白名单补充由 OLS 侧后续处理（**Q7**），**本期前端仍必须**：遇到 403/404 的贴图引用降级为无贴图渲染（几何照常显示）并告警一次，不得让整个物体失败。
6.7 缓存：同一 `model_id` 的 metadata 与文件在浏览器会话内必须做内存缓存（按 URL 去重），避免多实例重复下载。
 renderer 的模型/机器人/模板三张缓存的键必须是 **`<kind>:<id>`**（`model:1` 与 `robot:1` 是两个不同物体，**Q53**）；**只缓存成功的读取**，失败的 promise 必须从缓存里删除，否则 OLS 重启后同一个物体永远读不回来（**Q54**）。
6.8 渲染：`MeshRenderer.forceOverrideColor` 存在时覆盖材质颜色；几何必须用真实模型，**禁止立方体占位**。加载中在层级树显示 spinner，不阻塞其他物体。
6.9 **跨域（CORS）责任归属（Q5）**：CORS 由浏览器强制、由**被访问方服务器授权**。
 - 前端**不能**靠"加请求头"绕过：`Origin` 由浏览器自动附带，且 `Origin`/`Access-Control-*` 属于禁止脚本设置的头部；能改的只有请求方式与自定义头。
 - 因此这是 **ObjectLibraryServer（与云存储）侧的范畴**：OLS 必须在响应中返回 `Access-Control-Allow-Origin`（值为前端 origin，如 `http://127.0.0.1:5173`，或 `*`）。
 - 本期前端**不得**给 OLS/COS 请求添加自定义头（如 `Authorization`、`X-Request-Id`），因为自定义头会触发 preflight `OPTIONS`，而 `OLS接口.md` 未定义 `OPTIONS` 处理。只用简单 GET。
 - **没有代理形式**（**Q68**）：`RuntimeConfig` 与 `constant_config.json` 里没有 `library_via_proxy`，gateway 不提供 `{gateway}/api/library/<ols相对路径>` 转发。浏览器只有一条路径：**直连 `library_server_url`**，所以「OLS 必须返回 `Access-Control-Allow-Origin`」是**硬前置**，并已由 OLS 侧实现（O2：`server.py` 按 config `cors_allow_origin` 发头，`tools/browser_smoke.cjs` 开跑前断这条头存在）。
 - gateway(8099) 与 OLS(8000) 端口不同即为不同 origin，**默认仍需要 OLS 加 CORS 头**。
6.10 `is_on_cos == true` 分支的鉴权方式待定（O3）：本期实现按 `{cos_base_url}/{path}` 裸 GET 处理；若 COS 需要签名/临时凭证，必须由调用方或 OLS 下发，不能把密钥放进前端或 `constant_config.json` 明文提交。
6.11 **类别、机械臂大类与缩略图（`docs/OLS接口.md`，Q20~Q23）**：
 - 三个 id 空间：`scene` / `model` / `robot` 各自独立编号（真实服务是每类型一个 SQLite 索引 `data/<type>_index.db`，`server.py`；客户端可见的形状不变：`/list` 出 `{id,name,category}`，`/listCategory` 出 `category` 数组）；URL 里的 `id` 是字符串，服务端解析为 int，**负数不允许**（`400`）。客户端一律按 int 处理，不做 `name` 查找（§2.2）。
 - `/list?type=<三者之一>` 的每个 item 是 `{id, name, category}`，`category` 为 `大类.小类` 点分字符串（例：`模型.抓取物`、`模型.容器`、`机械臂.六轴`、`机械臂.七轴`、`场景.桌面抓取`）；索引里没有该 id 的类别时为 `null`，前端按"未分类"处理，不得报错。
 - `/listCategory?type=<三者之一>` → `{"type":"model","category":["模型.抓取物","模型.容器"]}`，用于资产面板的分组/筛选。**路由名以真实 OLS（`server.py`）与 `OLS接口.md` 为准**（**O7** 最终决议）。
 - `/robot?id=<robot_id>` → `data/robots/<robot_id>/<name>.json` 与 `<name>_files.json` 合并后的 JSON，结构与模型元数据一致（§6.2）；机械臂的文件走 `/file?type=robot&…`。世界文件里指向它有两条等价写法：`$LIBRARY_SERVER/robot?id=<robot_id>`（靠形式），或 `$LIBRARY_SERVER/<n>` + `MetadataRef.properties.type == "robot"`（靠字段，§6.1）。裸数字且没有 `type` 时仍恒为 `model` id（§6.1、**Q51/Q66**）。
 - 资产面板给物体起名用 `/list` 返回的 `name`（三类同一套 `id + name + category` 卡片，`assets.ts:objectForItem`），因此 `model` 与 `robot` 同名（如都叫 `ur5e`）时在层级树里靠 `#id` 区分，不靠名字。
 - `/thumbnail?type=<scene|model|robot>&id=<n>` → `<name>_files.json` 清单里 `thumbnail` 字段登记的图片字节；该路径**相对资产目录 `data/<type>s/<id>/`**（例：`data/models/1/apple_files.json` 的 `"thumbnail": "apple_thumbnail.png"` → `data/models/1/apple_thumbnail.png`，O8）。前端只把它当 `<img>` 的 `src`，不参与几何加载，缓存/失败策略见 §4.6。
 - 七条路由（`list`、`listCategory`、`scene`、`model`、`robot`、`file`、`thumbnail`）全部由浏览器直接请求 `library_server_url`，中间没有 gateway 转发层（§6.9、**Q68**）。

## 7. UI 需求

### 7.1 顶栏（已撤销）与视口工具栏

**整条顶部命令栏 `header.command-bar` 已撤销**（**Q92**），DOM 里不再有这一层，`.workbench` 由"命令栏 / 工作区 / 状态栏"三行改为"工作区 / 状态栏"两行。它承载过的四样东西的去向：

- **Load Scene**（本地 json）、**Load Traj**（本地 `.traj`）→ 移入 `.viewport-toolbar`（画布正上方那条），排在最左；`data-action="load-scene|load-traj"`、feature 键 `viewport.loadScene`/`viewport.loadTraj`、隐藏文件输入 `#scene-file`/`#traj-file` 与两条处理逻辑一字不改。**Q104**：`#traj-file` 带 `multiple`（`#scene-file` 不加——世界只有一份，轨迹才有一批），`loadTrajFiles()` 一次整批追加（§4.3）。
- **连接指示**（`#connection-dot` + `#connection-label`）→ 归底部状态栏的 `#status-primary`（`statusBar.primary`），写的仍是同一件事（`Connected/Disconnected · <worldStatus>`），`updateStatus()` 里那两行覆写删除。**撤控件不撤信息**是本条判据。
- **品牌区**（`chrome.brand`）→ 纯装饰，随顶栏消失，无任何下游读取。
- **Agent 面板** → 位置从来不在顶栏（右栏 `aside.agent-panel`，`agent.*`）。任务制口径不变：发送"命令 + 当前场景 json"，只显示 gateway 经 `/events` 推来的五态与失败原因（链路 §5.15、面板口径 §13.21 **Q87**）。
- `commandBar` 与其全部剩余子键随整栏撤销一起消失（§9.3）；此前已撤的 `commandBar.create.*`、`commandBar.assetUpload`、`commandBar.history.*`、`commandBar.simulation.*`（速率语义迁入播放条倍速档位，**Q88** 后播放条不保留自由数值输入框）、`commandBar.state.*`、`/api/presentation` 模式切换按钮均不在场。

**文字口径（**Q133**，本节所有界面文字按这四条写）**：

- **看得见的名字用中文短语**：按钮与勾选框的 `<b>` 只说"这一下做什么"，不带"所有／含／本地"这类限定词（机制交给 `title` 与正文）。七个名字：`加载场景`、`加载轨迹`、`显示轨迹`、`录制辅助图层`、`投射阴影`、`显示包围盒`、`显示坐标轴`。**弹窗（`#toast`）复述同一动作时用同一个名字**：`投射阴影已开启`、`包围盒已显示`、`坐标轴已显示`、`加载场景失败：…`、`加载轨迹失败：…`。
- **悬浮提示一律英文，且每一个点得动的控件都要有 `title`**（含 `#traj-select`、路点勾选框、`#lighting-preset`、录制辅助图层勾选框）。`#selection-label` 是选中态回显、不是控件，不配 `title`。新写的 `title` 里**不许出现** `editor layers`、`render layer`、`not reported`、`never reported`、`browser local`（§12.23 ②、§12.29 ②）。
- **光照预设的名字不带括注**：下拉选项与切换弹窗都用 `Neutral / Studio / Dramatic / Overcast / Lamp` 五个短名，`editor/src/lighting.ts` 的 `label` 不含括号；toast 形如 `光照已切换：Studio`。
- **改的都是字，不是行为**：键名、`data-action`、`id`、`aria-label` 与判据验的那件事一字不动；正文里的 `Load Scene`／`Load Traj` 继续指代这两个入口（同时是 `data-action` 的值与 §4.3/§5.8 的通路名）。**范围到此为止**：左栏（层级／资产／Details）、播放条、状态栏、Agent 面板一个字都不动。带日期的历史记录（§9.3 各条、§13.x、任务卡）里引的当天原话不回改。

**视口工具栏（`.viewport-toolbar`）**，从左到右依次为：

- **载入** `<button data-action="load-scene" title="Load Scene (local json)"><b>加载场景</b></button>` + `<button data-action="load-traj" title="Load Traj (local .traj)"><b>加载轨迹</b></button>`（**Q92** 迁入、**Q133** 定字）。**Load Traj 一次可选多个文件**（**Q104**）：N 个文件 ⇒ 按选择顺序整批追加、选中这批第一条、播放头 0；其中任何一条不是合法 TRAJ ⇒ 整批不生效（§4.3 全有或全无，toast 点名坏的那份文件）。
  - **窄容器下这两个按钮只留图标**：工具栏宽度是 `.viewport-shell` 这一栏的宽度、不是窗口宽度（打开资产面板时 1440 窗口下实测 673 px），收字走 `@container (max-width:780px)`，`.viewport-shell` 声明 `container-type:inline-size`。判据是 `#selection-label` 不能被挤成省略号——它是这条栏上唯一的选中态回显。本条收字规则适用于这条栏上全部按钮与勾选框。
- **轨迹选择器** `<select id="traj-select" data-feature="playback.trajSelector" title="Trajectory to play">`（**Q100**、**Q133**）。四条口径：
  - 选项来自浏览器内存里的 `state.trajs`，顺序与列表一致。文本 = `轨迹 <i+1> · <total_frames> 帧 · <fps> fps`，本地 Load Traj 进来的那条前缀换成文件名（文件名不进 wire，§5.12）。**默认选中第一条**。
  - **只有 1 条时整条 `<select>` 隐藏**（沿用既有 `hidden` class 写法）；列表清空（写入退出播放 §5.4/**Q76**、清除轨迹）时同样隐藏；`playback.trajSelector` 关掉时一直隐藏——那时永远播第 0 条，是运维口子而不是坏状态。
  - **切换 = 整批换、回到 0 帧**：走与"载入一批新轨迹"完全相同的路径（`setTrajectory(list, index)`），停时钟、播放头归 0、刻度/时长/帧读数/幽灵全部按新活动那条重算。**不做"保住当前帧号"**（两条轨迹 fps 与物体集合不同，同一帧号无对应语义；§7.6 的采样继承同理）。
  - **不发出任何请求**：选哪条纯是浏览器本地状态，`scene_version` 不变、`/getWorld` 回读不变、gateway 与调用方都不知道。`parity_inventory.py` 路由清单与 `audit_parity.py` 探针不因这个下拉框增删。
- **轨迹路点勾选框** `<label class="toolbar-check" data-feature="playback.trajWaypoints" title="Show the waypoints of every loaded trajectory"><input type="checkbox" id="waypoint-all-trajs"><b>显示轨迹</b></label>`（**Q101**、**Q133**）。四条口径：
  - **它不是"路点总开关"**：不勾选时活动那条的路点照画（进入播放模式就画，是默认形态），勾选只是把其余几条也摆出来。勾选框只有两态、没有"全关"态；整体关掉走 `playback.trajWaypoints`（§9.3）。
  - **一个键管两块**：`playback.trajWaypoints` 关断时勾选框连同它管理的球一起消失（宿主是这个 `<label>`），不新增第二个键。
  - **状态是浏览器本地视图态**：存 `state.waypoints.showAll`，不进 wire、不回传，`scene_version`、`/getWorld`、`/captureScene` 都不因它变化；页面重载回到未勾选。
  - 窄容器下与载入按钮同一条收字规则；`#selection-label` 仍是不能被挤成省略号的那个。
- 视口操作：focus（`viewport.focusSelection`）、frame（`viewport.frameScene`）与 `#selection-label`。
- **截图** `<button data-action="snapshot" data-feature="viewport.snapshot" title="Copy viewport screenshot to clipboard">`（**Q108**，排在 frame 之后、`#selection-label` 之前）。五条口径：
  - 截的是编辑器自己的 orbit 相机此刻看到的画布，尺寸 = 画布 drawing buffer（CSS 尺寸 × `pixelRatio`，上限 2），与屏幕一致；**不是** §4.4 那条按 `Camera` 组件离屏渲染的 `/captureScene`。
  - **排除判据复用 §4.4 现成通路**（`hideOverlays()` 按 `isEditorOnly()` 临时按下可见编辑层）：选中描边、段末幽灵、轨迹路点、相机视锥线、灯光占位球与标签、地面网格与坐标轴都不进截图；`viewport.cameraFrustumAlways`/`viewport.lightMarkerAlways` 两个常驻 flag 只影响屏幕，**对截图无效果**（四种组合截出逐字节相同，§12.16 ③）。
  - **产物只进剪贴板**（PNG）：成功提示报像素尺寸；`ClipboardItem` 不存在、页面不在安全上下文、用户撤销剪贴板权限 ⇒ 走 `showError` 既有 toast 通道，**不落盘、不下载、不做"复制到文件"降级**。
  - **零副作用**：不写世界、不动播放头、不改选中态、不发一条请求（`scene_version`、层级树行数、`#panel-count`、`/getWorld` 一字不动），路由清单与探针不增不减，变的只有 `features.json`（+1 键）与 `docs/feature-inventory.csv`（+1 行）。
  - 关掉 `viewport.snapshot` = 按钮不见，无别的连带（键只管宿主，同 `viewport.exportScene`）。截图有**像素出口**（剪贴板 PNG 可读回数），判据是像素级的（§12.16），不需要诊断行。
- **录制** `<button class="button" data-action="record" data-feature="viewport.record" title="Record the viewport to an MP4 file">`（**Q121**；`title` 里那半句 "(browser local, never reported)" 已按 **Q124** 撤下）。四条口径（全文见 §4.8.7）：
  - **一个按钮两态**：未录 = 开始（图标 `record`），录制中 = 结束（图标 `square`、`aria-pressed="true"`、`title` 跟着换）。不新增第二个键、不做"暂停/继续"第三态。
  - **录的是自由相机正在看的现场，不是屏幕合成好的那张图**：每交付一帧由 `renderer.recordFrameTo()` 走一遍 §4.4 的视口 pass（与截图同一条 `captureViewport()`），画进一块代理画布——相机、机器人位姿、灯光与屏幕同源，编辑层按勾选框决定进不进。录制不冻结轨道相机、不停播放、不改选中；录制期间移动/旋转相机照旧生效。常规查看与轨迹播放之间来回切**不打断**带子（画布与 renderer 全页只有一份）。
  - **辅助图层默认排除，勾选框「录制辅助图层」把它要回来**（`<label>` 的 `title` 为英文 `Include the auxiliary layers in the recording`，**Q133**）：默认走 §4.4/**Q108** 的 `hideOverlays()` 口径（包围盒线框 **Q119**、选中描边、路点、幽灵、视锥线、灯光标记都不进带子）。**排除只发生在离屏那一趟：屏幕上这些层一路都在，录制从头到尾不改任何一层的可见性**；代价是每交付帧多一趟渲染（上限 30 次/秒，由 `RECORD_INTERVAL_MS` 封顶；§4.8.7 第 1、5 条）。勾选框与按钮**同挂 `viewport.record` 一个键**，两个宿主一起被 `applyFeatureVisibility()` 的 `[data-feature]` 循环收走。
  - **产物只落用户磁盘，两态失败都不产文件**：停止 ⇒ `MediaRecorder` 收尾 ⇒ `viewport-YYYYMMDD-HHMMSS.mp4` 经 `Blob` + `<a download>` 自动下载（5 分钟到点自动结束并照常下载）。这台浏览器编不出任何 MP4、或写回 **0 字节** ⇒ **只告警、不降级 WebM、不下载**。**零协议面**：不写 `World`、不动 `scene_version`、一条请求都不发（点击开始与停止处非 GET 与 `browser.state_sync` 增量 **0 / 0**，**坑 81**），路由清单与探针不增不减。窄容器下同一条收字规则。
- **光照预设** `<select id="lighting-preset" data-feature="viewport.lightingPreset" title="Lighting preset">`：第一项 `Scene lights` = **关闭覆盖**（跟随文件自带光照，也是载入新现场后的默认态），其后是 §4.8.1 的 5 个预设，选项文本就是五个短名 `Neutral`/`Studio`/`Dramatic`/`Overcast`/`Lamp`，**不带括注**（**Q133**；切换弹窗 `光照已切换：Studio` 报的是 `lighting.ts` 里同一个 `label`）。`change` 只切换**渲染层覆盖**（**Q114** 改语义），不写 `World`、不动 `scene_version`/`scene_sub_version`、不回传。
- **阴影勾选框** `<label class="toolbar-check" data-feature="viewport.shadows" title="Projected shadows"><input type="checkbox" id="shadows-toggle" checked><b>投射阴影</b></label>`（**Q117**，第 1 条口径由 **Q118** 改判）。三条口径：
  - **`viewport.shadows` 只管宿主在不在，不管投影本身**：`true` ⇒ 勾选框出现，投不投影**由勾选框状态决定**；`false` ⇒ 勾选框消失，投不投影**由 `config/constant_config.json` 的 `shadows_enabled` 决定**（§9.2）。判据收在 `logic.ts:shadowsEnabled()` 一处，`main.ts:renderShadows()` 是唯一调用者，渲染器只收最终那个布尔（与 **Q105**/**Q106** 分工一致：`renderer` 从不直接读配置）。
  - **状态是浏览器本地视图态**：存 `state.shadows`，不进 wire、不回传，`scene_version`、`scene_sub_version`、`/getWorld`、导出、摘要一字不动（§4.8.5 判据）；页面重载回到勾上。与预设的差别：预设改场上建哪几盏灯，勾选框只改这些灯投不投影——叠加时以预设的灯表为准（覆盖态下文档灯不亮也就不投）。
  - **持锁期内把可见的那条路冻结为「开」**：与 §4.8.1 的"冻结为跟随场景"同一时机（`renderLightingFreeze()` 顺带处理），租约期内 `aria-disabled`、点击只说明原因不改状态，释放后回到用户勾上的那份。**`viewport.shadows` 为 `false` 时没有这一条**（**Q118**）：那条路上没有控件可冻，`shadows_enabled` 是每个 tab 都读到同一值的启动配置，本身满足"可重复"。
- **物体包围盒勾选框** `<label class="toolbar-check" data-feature="viewport.boundingBoxes" title="Draw one bounding box per object"><input type="checkbox" id="bbox-toggle"><b>显示包围盒</b></label>`（**Q119**）。五条口径（全文见 §4.8.6）：
  - **勾选框是唯一开关**：`viewport.boundingBoxes` 管宿主在不在（`true` ⇒ 出现，`false` ⇒ 消失且线框不画），勾上与否管画不画——判据收在 `logic.ts:boundingBoxesEnabled(features, checked)` 一处，`main.ts:renderBoxes()` 是唯一调用者，`renderer.ts` 只收最终布尔。**没有常量兜底**（§4.8.6 第 6 条）：默认外观就是"不画"，收走控件不会误伤任何人的决定。
  - **默认不勾**（与阴影相反）：这一层是诊断辅助不是现场外观；页面重载回到不勾。盒的量测与选中描边、`/get2DAABB`、`frame()` 是**同一条算式**（§4.5 判据的第四个表面），一物一盒、按世界文档的物体 id 数，纯灯/纯相机这类没有渲染几何的物体跳过。URDF 那一只盒是**逐 link、逐顶点**量出来再并起来的（**Q149**），不是各零件局部盒角点的并 —— 关节把零件转出它自己的轴之后，那八个角指着空处。
  - **形状另有一枚常量**（**Q149/Q150**，§9.2）：`config/constant_config.json` 的 `urdf_bounding_box_per_link` 只决定 URDF 那只盒**并成一只**还是**切成每 link 一只**（默认 `true` = 每 link 一只），既不决定画不画（上面那条键与这枚勾选框的事），也不决定盒多大（`worldAabb()` 恒为 `worldLinkBoxes()` 的并集，`/get2DAABB`、摆放的 `sceneAABB`、`frame()`、描边在两态逐字相同）。判据 `logic.ts:urdfLinkBoxesEnabled(runtime)`，`main.ts:renderBoxes()` 是唯一调用者，fail-open 到 `true`。**不加第二枚勾选框**：这一层已经有开关，多一枚勾选框只是把 §9.3 那条"整层的远程出口只有一个键"稀释成两个。
  - **状态是浏览器本地视图态**：存 `state.boxes`，不进 wire、不回传，`scene_version`、`scene_sub_version`、`/getWorld`、导出、摘要一字不动；`parity_inventory.py` 判 `literal`（宿主就是这个 `<label>`）。
  - **持锁期不冻结**（与阴影相反）：线框属 §4.4 编辑层，`hideOverlays()` 本来就在 `/captureScene` 与视口截图两条采集通路里把它按掉——采集字节与勾选框无关。判据不是"看起来没变"而是**同一次采集在线框开/关两态回同一份字节**（§12.20 ②）。
- **本地坐标轴勾选框** `<label class="toolbar-check" data-feature="viewport.localAxesToggle" title="Draw the local frame each object declares"><input type="checkbox" id="local-axes-toggle"><b>显示坐标轴</b></label>`（**Q132**，全文见 §4.8.8；管的是 §4.9 `LocalAxes` 组件的轴，与 `viewport.worldAxes`（世界原点三根）不是一族）。四条：
  - **①** 勾上 ⇒ 场景里**每一个**带 `LocalAxes` 的物体同时亮出自己的三根轴，不勾 ⇒ 全部收起；"哪些物体有帧"由 `components` 决定，这枚勾**不提供按物体的入口**。
  - **② 默认不勾**（与包围盒同、与阴影反）：页面重载回到不勾——§4.9 那句"文件里写了就全程要看见它"改成"写了就等这一页想看的时候看"。
  - **③** 键名 `viewport.localAxesToggle`（由 `viewport.localAxes` 改名，**Q132**；总键数不变，`docs/feature-inventory.csv` 那一行随之改名，`parity_inventory.py` 仍判 `literal`，依据是 `logic.ts` 里那一次引名字符串）。
  - **④** 键关 ⇒ 宿主消失**且这一层不画**（`logic.ts:localAxesEnabled(features, checked)` = 键 && 勾选，`main.ts:renderLocalAxes()` 是唯一调用者），与 §4.8.6 第 6 条同形、**不补常量**。持锁期不冻结（采集字节与它无关）；窄容器同一条收字规则。**改键面要重启 gateway**（**坑 22**）：不重启的症状是"工具栏没有这枚勾选框、轴也不画"，不是报错。
- **相机画面勾选框** `<label class="toolbar-check" data-feature="viewport.cameraWall" title="Show what every camera in the scene sees"><input type="checkbox" id="camera-wall-toggle"><b>相机画面</b></label>`（**Q138**，全文见 §4.8.9）。三条：
  - **①** 勾上 ⇒ 视口右上角浮出一扇窗，**每台 `Camera` 物体一格**实时缩略（集合就是 §4.8.9 的 `findCameraObjects`，吊在臂上的腕部相机也算），不勾 ⇒ 整窗消失；窗右上角 ✕ 与勾选框是同一条开关的两头。**Q139** 起：窗**按住标题栏可拖**（夹在视口内）、每格**名称居中在画面正上方**、标题栏另有 `RGB`／`深度` 两枚按钮切这一窗的显示模式（深度借既有的 `viewport.cameraPreview.modes.depth`，**不新增键**；伪彩色见 §4.8.9）。
  - **② 默认不勾**（与包围盒、坐标轴同）：一窗实时图是"想看的时候看"，不是现场外观。
  - **③** 键 `viewport.cameraWall` 进 `features.json`（§9.3），`docs/feature-inventory.csv` +1 行、`editor_reach` 判 `literal`；**Q139 键面不变**，只把 `viewport.cameraPreview.modes.depth` 那一行的依据由 `interpolated` 改成 `literal`。这一层是 `.canvas-wrap` 里的 DOM、不进场景图，`/captureScene`、视口截图（**Q108**）、`world.toJSON()` 与一切量测**结构上**看不见它（§4.8.9），不需要新的排除判据。**新增键要重启 gateway**（**坑 22**，只对 Q138 那轮成立）：症状是"工具栏没有这枚勾选框、窗也开不了"；**Q139 不新增键 ⇒ 不需要重启**。布局修订（相机多了会滚，**Q146**）是纯 CSS，键面与接口面都不动、不需要重启，判据见 §12.40。
- **导出场景** `<button data-action="export-scene" data-feature="viewport.exportScene" title="Export scene JSON">`：浏览器下载 §4.8.2 的 JSON。
- **位姿 gizmo 模式已撤销并删除**（**Q114**，§4.8.3 作废）：工具栏不再有 `data-tool="translate|rotate|scale"` 三个模式键，`viewport.tools.translate/rotate/scale` 三个子键随功能删除（§9.3）；`.segmented` 里只剩 `data-tool="select"`，父键 `viewport.tools` 的宿主就是这一组。场上只剩拾取这一种行为，`state.tool` 下线。

### 7.2 左侧栏

- `scene`（层级）、`assets`（资产面板，§4.6）、`details`（只读 + 相机预览）。`settings` 删除。
- 层级树：树形展示 `Transform.parent` 关系，显示 `object_name_in_scene` + `object_id_in_scene`（名称缺失或非字符串时回退 `object_<id>`，§3.2），支持点击选中（拾取与树选双向同步）、按名称搜索（保留 `panels.scene.search`）。
- 层级树内禁止拖拽改父级、禁止删除按钮。
- **三视图共用同一块 `#context-content`，整块内容由一个字符串重写**（**Q136**）：`renderContext()` 每次把 `renderAssets()`／`renderHierarchy()`／`renderDetails()` 的那一个字符串落进容器；`innerHTML` 一落，里面每一行都是新造节点——列表滚动位置回顶部、缩略图重发请求、层级搜索框被抹掉。**判据**：写之前先问"这一段与屏幕上正在显示的是不是同一个字符串"（`logic.ts:panelMarkupChanged(previous, next)`，判据是**逐字相同**；`previous === null` 是首帧，总是写），相同就**一个字节都不写**。于是"点一行改的是现场、不是这份清单"那类点击不再滚回顶部：`assetRow()` 的行里没有选中态（被选中的是现场里的物体），摆放成功后重渲染得到同一字符串 ⇒ 不写 ⇒ 列表停在原位。**该刷新的照旧刷新**：`treeRows()` 把 `selected` 写进行类 class、Details 把位姿读数写在正文里，这两处字符串会变，选中高亮与实时读数不被守卫挡掉。`panels.context` 关掉时渲染出空串，同一守卫照旧成立。判据见 **§12.31**。

### 7.3 轨迹播放条（底部，替换原 timeline 条）

复用现有 DOM/逻辑骨架（`main.ts` 的 `#timeline`、`#timeline-range`、`#timeline-time`、`[data-timeline="play"]`；`logic.ts` 的 `timelineDueIndex`/`timelineDelayMs`），改为本地帧源，并补齐：

- **一次可以来一批轨迹，本节其余一切只读"活动那一条"**（**Q100**，配合 §5.3 请求侧 `trajs`）：浏览器内存是 `state.trajs: Traj[]` + `state.trajIndex`，**活动轨迹 = `trajs[trajIndex]`**，载入后默认 `0`。帧源 `TrajFrameProvider`、`#timeline-range.max`、刻度、读数、段末幽灵、`driver` 的关节驱动全部从它派生——判据是"**非活动的那几条对渲染与对外通道零影响**"：不建幽灵、不驱动关节、不进 `/captureScene`、不改 `scene_version`。下拉框切一条 = `setTrajectory(整批, 新下标)`，与"新批次到达"同一路径（§7.1）；`setTrajectory(null)`（写入退出播放 **Q76**、手动 Load Scene）清的是**整批**。**唯一有意的例外（Q101）**：勾选「显示所有轨迹路点」后，非活动那几条也贡献**编辑层**路点球——让路的只有画面：仍不建播放态、不驱动关节、不发请求、不改 `scene_version`、不进 `/captureScene`。
- 进度条拖动定位帧、播放/暂停。**播放条上没有"停止并退出播放"按钮，也没有连续 RTF 数值输入**（**Q88** 撤销）：退出播放态只走写入共存规则（**Q76**），手动 Load Scene / Load Traj 同理。
- 倍速：0.25/0.5/1/2/4 **五个档位的 `<select>`**（沿用"仿真时间/墙钟时间"定义），档位之外不接受用户自填速率数值。
- 帧步进 `←/→`（1 帧 = 1/fps 秒）、时间显示 `当前 s / 总 s` 与 `frame/total_frames`。**两个读数各留固定占位宽度**（**Q91**）：数字位数变化（如 `0 / 839` → `839 / 839`）不得改变进度条本身的宽度。
- 循环开关；末帧行为：非循环停在末帧，不自动跳回默认位姿。
- 刻度标记：段表 `step_count` 的每一段起点、Attach 表的每一条事件帧，都在进度条上画一根刻度。**两类刻度同色、鼠标悬停不显示任何文字**（**Q90**）；DOM 里仍用 `step`/`attach` 两个类名区分来源，仅供样式与测试挂钩。**点击任一刻度 ⇒ 播放头跳到该刻度那一帧**（与帧步进同一路径：先停时钟再定位），该能力归 `playback.seek`，不新增 feature 键。
- **段末幽灵位姿**（**Q96**/**Q97**/**Q98**）：轨迹在场时，用**半透明**（不透明度取 §9.2 的 `ghost_pose_opacity`，**默认 50%**）额外画出**播放头所在那一段末帧**的姿态，作为"这一段会把它们带到哪里"的预告；被画对象是 **clip 里在世界中带 `RobotDriver` 且 `role` 不是 `"passive"` 的每一个物体**。七条口径：
  1. **幽灵是同一台 URDF 的第二个实例**：几何与真机共享、材质克隆并设 `transparent=true`、`opacity=<ghost_pose_opacity>`、`depthWrite=false`。它**不进** `nodes`、**不进** `robotTrees`，层级树、Details 只读区块、`/getWorld` 回读都看不见它。
  2. **"段"就是刻度那条里的段**：幽灵与刻度用同一个 `TrajStep` 对象（`traj.ts:stepsFrom` 把同号且相连的段表条目合成一段）：当前帧落在哪个 `TrajStep` 里，就画那个 `TrajStep.endFrame` 的采样。段间空隙属于所在段；当前帧**在首段之前**没有段可预告，**隐藏**；**在末段之后**仍指最后那一段（与 §8.5 同源）。采样一律经 `poseAtFrame(机器人对象, step.endFrame)`，"当前是哪一段"的解析必须与 `poseAtFrame` 的循环同构（同按 `start_frame` 升序）。**多台幽灵共用同一个"当前段"**：`stepAtFrame` 的输入是跨物体并出来的 `Traj.steps`，臂与夹爪的幽灵永远指向同一帧号。
  3. **基座跟随，且幽灵跟着幽灵**：每台幽灵挂在其真机节点的父节点下、复用其局部 `position/quaternion/scale`（`/updateItem` 挪基座或 Attach 换挂载点后不留原地）；关节角完全由那一帧采样驱动。若某台幽灵的真机父节点本身落在另一台有幽灵的机器人树里（夹爪的真机父节点是臂的 `wrist_3_link`），必须改挂到**那台幽灵树里的同名 link**（按 `UrdfTree.links` 名字对上）；解析不到时退回真机父节点（宁可挂错位置也不让幽灵消失）（**Q98**）。
  4. **驱动走同一套 FK**：幽灵关节由真机写路径同一个函数摆出——`drivers.ts:applyFrameJointPositions(objectId, driver, pose, handles, warn)`（`joint_sequence` 顺序、`joint_limits` 裁剪、`<mimic>` 联动），限位与联动不存在两套实现。**只有幽灵这份 `warn` 传空实现**（越界提示真机已报过，不重复刷屏）。clip 里的苹果、盒子与 `Attach` 事件都不作用于幽灵。
  5. **接线**：`showFrame()` 里每帧重算（拖动、点刻度、帧步进、播放同一路径），`setTrajectory()` 负责建与拆；建立是异步的（经 OLS 取 URDF），clip 一到就 `void` 启动，模型未回来期间不画、不报错。
  6. **不进任何对外通道**：幽灵属编辑层，与包围盒同一判据（§4.4）——排除在 `/captureScene` 之外、不参与鼠标拾取；`playback.ghostPose` 关断、轨迹清除（含 Q76 写入退播）与场景重载时一并 dispose，不留残影。
  7. **对象集合 = clip 里带 `RobotDriver` 且 `role` 不是 `"passive"` 的每个物体**（**Q98**/**Q107**）：遍历 `Traj.objects`，凡 `World.robotDriver(objectId)` 存在且非 passive 的都画一台，**不要求该物体自己有段**（无段时 `poseAtFrame` 回落默认位姿，幽灵与真机重合等于没画）。`RobotDriver` 存在但世界里没有该物体（§8.2）仍不画、不报错。passive 的 URDF 家具（微波炉、抽屉柜这类 `type:"model"`）被 `role:"passive"` 从幽灵与路点两个装饰集合剔出，其余全部保留（§4.7）；判据入口是 `World.armDriver(objectId)`——只在两处装饰候选上使用，**不是**新的驱动门（§8.3、`/updateItem` 刷新、Q75 合并、Q76 停放、`RobotAttachment` 挂载全都仍读 `robotDriver()`）。
- **轨迹路点球**（**Q101**/**Q102**）：轨迹在场时，把**叶子机械臂末端**在 clip 里经过的位置画成一串小球。七条口径：
  1. **画谁 = 连接链上的叶子机械臂**：候选是这条 clip 携带的、且在世界里带 `RobotDriver` 且 `role` 不是 `"passive"` 的物体（与幽灵第 7 条同一套遍历、同一个 `World.armDriver()` 判据），再剔除"链上还有别的 `RobotDriver` 挂在它下面"的祖先——父机械臂不画，只画子机械臂。判据读**世界文件声明的挂载关系**（`WorldObjectNode.attachmentMount.objectId`，没有 `RobotAttachment` 时退回 `Transform.parent`），从每台驱动器物体沿父链上走，途中再遇另一台驱动器物体即为祖先；链更长（A←B←C）时只剩 C；链里只有一台驱动器物体时它自己就是叶子。**passive 的过滤发生在求叶子之前**（`main.ts:leafArmTargets()` 的 `.filter()` 第一步）：腕上挂 passive 家具不会把夹爪或臂降级成"非叶子"（`logic.ts:leafArmIds()` 一字不改），passive 自己永远拿不到路点。
  2. **点取在哪 = 该臂 URDF 里最深叶 link 的世界位置均值**（**Q102**）：在已解析的 URDF 图（`UrdfRobot.links`/`UrdfRobot.joints` 的 parent→child，含 fixed joint）里取"不是任何关节的 parent"的 link 集合，只留**距根最深**那一层，把这些 link 节点的世界位置求均值。真实资产已逐台验证：每台臂只有一个最深叶 link（均值即它自己）；每台夹爪的最深叶恰是左右对称的两块指垫，两点均值必然落在对称面上——即 **Q89** 算夹持中心的同一对称论证，读作"抓取中心"，不需要把坐标写死。`<gazebo>`/`<mimic>` 扩展标签不参与判定（父链只看 `<joint>` 的 parent/child）。
  3. **与 §8.6 的挂载点不是一条路，且不改它**：Attach 的重挂父仍走 `urdf.ts:endEffectorNode()`（`joint_sequence` 最后一条关节的 child link），因为已发布 clip 的抓取段位姿是按那一帧点写的（**Q89**）；路点用"最深叶 link 均值"因为要的是**夹取点**不是**挂点**。两条路各有唯一消费者，`endEffectorNode()` 一字不动，路点另起纯函数 `urdf.ts:toolTipLinks()`。
  4. **采样密度与大小走 §9.2 两个常量**：`traj_waypoint_stride_frames`（每 N 帧取一个，默认 **10**）与 `traj_waypoint_radius`（球半径，世界米，默认 **0.02**）。取帧是 `0, N, 2N, …` 直到 `total_frames-1`（**末帧必取**；末帧已落在等差数列上时不重复补），720 帧默认 **73** 个球、840 帧 **85** 个。回落口径 fail-open：`logic.ts:waypointSettings(runtime)` 只在收到**有限的正整数/正浮点**时采用，缺字段、`null`、字符串、`0`、负数、`NaN` 一律回默认（半径 0 把实例矩阵缩成奇异阵、stride 0 卡死采样循环，都不照收）。**读的是构建时刻**：改 `config/constant_config.json` 要重启 gateway 才生效（**坑 22**）。
  5. **什么时候算、什么时候重算**：路点是"这一条 clip 末端经过哪里"的**一次性扫描**，不逐帧现画。扫描 = 对该条 clip 建一个 `TrajFrameProvider`，按帧序列逐个 `frame(f)`（同一套 §8.4 FK 与 §8.6 重挂父），每次读叶子臂最深叶 link 世界位置，扫完把播放头**放回原处**（`showFrame(state.playIndex)`）。**触发时机两处**：① **批次落定**（载入 `updateTraj`、`setTrajectory()` 切活动条）时把这一批每一条都扫一遍、全量缓存——此后勾选框两态、点选轨迹只改可见性，不再扫；② **某条臂的 URDF 晚于 clip 到达**时重扫一次（`renderer.ts:loadModel()` 展开完关节树报 `onRobotTree(objectId)`，`main.ts:scheduleRobotLanding()` 把同一 tick 落地的多台并成一次「`showFrame(state.playIndex)` + `recomputeWaypoints()`」）。②是①的必要补集：没有它，冷启动先推轨迹后加载机器人会永久画不出球（**坑 67**）。②的前半句（重放当前帧）是**主判据**、路点重扫搭车：关掉 `playback.trajWaypoints` 也要重放（否则臂停在声明位而不是播放帧）。**「世界改动之后再算」不是独立时机**，被既有规则吃掉：任何写入按 **Q76** 一律 `setTrajectory(null)`；gizmo 与关节输入已被 **Q114** 撤销；剩下的关节写入通路只有 `/updateItem`，同样走 Q76 退播——"路点在场"与"浏览器里搬动基座/挂载点"结构上不会同时成立，缓存不需要额外失效钩子。扫描同步、纯内存、**不发一个 HTTP/WS 请求**，`scene_version` 与 `/getWorld` 不受影响（**Q33**）。
  6. **颜色与两态**：颜色是**代码内置固定调色板按轨迹下标循环**（`logic.ts:WAYPOINT_COLORS`，6 色），不进常量配置。**未勾选 = 只画活动那条**（颜色仍是调色板第 `trajIndex` 个，勾上/取消时同一条轨迹颜色不变）；**勾选 = 画列表里全部**，一条 clip 一层、同一层内所有球同色。小球**一层一个 `InstancedMesh`**（一份几何 + 一份材质，`count = 该条采样数`），`traj_waypoint_radius` 改了几何就换、随层重建。**渲染侧四个入口**（`renderer.ts`）：`setWaypointPaths(paths, radius)` 按 `(轨迹, 叶子臂)` 逐层建/复用/重建并写实例矩阵；`setWaypointVisibility(keys)` 只做两态显隐（不重扫）；`clearWaypoints()` 逐层 dispose；`waypointLayers()` 回读当前层身份、计数与各球坐标（从实例矩阵反读，只在安装/勾选/清空时调用，不在帧循环里）——§12.13 诊断行的数据来源。
  7. **编辑层，不进任何对外通道**：路点层挂在 `simWorld` 之下（世界 Z-up 同一坐标系），但**不进** `nodes`/`robotTrees`——层级树、`#panel-count`、Details、拾取射线、`worldAabb()`（`/get2DAABB`、`frame()`）与 `World.toJSON()`/`/getWorld` 全部看不见它；`hideOverlays()` 把它连同幽灵挡在 `/captureScene` 与相机预览之外。`playback.trajWaypoints` 关断、`setTrajectory(null)` 与批次清空时逐层 dispose，不留残影。
- 播放状态必须与 §5.4 广播共存（**Q76**）：播放中收到任何写入（`updateWorld`、`updateItem`、`addObject`、`removeObject`，含手动 Load Scene）→ **停止播放并清除轨迹**，回到正常场景展示，并按各自场景文件声明的位姿**重新停放机械臂关节**（§3.6、**Q75**）；收到新 `updateTraj` → 替换当前轨迹并从 0 帧暂停，此后物体与关节重归轨迹管（§8.5）。

### 7.4 Agent 面板的对话气泡（**Q120**，2026-10-24 新增）

命令与 Agent 回复采用聊天气泡形式：命令靠右、Agent 回复靠左，可明确区分。八条口径：

- **"谁说的"早就写在 DOM 里，只是把它画出来**：`main.ts:agentMessageMarkup()` 每条消息输出 `<article class="message user|agent">`，角色名（`You` / `Agent`）与时间在 `<header>`、任务徽标在 `<footer>`。改动**只有 `editor/src/styles.css` 一处**：`agentMessageMarkup()`、`result_view.ts`、`logic.ts` 与 `main.ts` 的显示逻辑一行不动。
- **左右两半是判据，不是装饰**：`.agent-messages` 为 `display:flex;flex-direction:column`，气泡用 `align-self` 分站两侧（`.message.agent` = `flex-start`、`.message.user` = `flex-end`），`max-width:86%` 给对侧留 14%。判据是**几何**的（§12.21 ②）：用户那条中心 X 落在容器中线右侧、Agent 那条落在左侧，且两者 `align-self` 计算值就是那两个字。不拿像素比对、也不拿"看着在右边"当结论。
- **选 `align-self` 而不是 `margin-left:auto`**：一条属性说一件事；空态 `.agent-empty` 不写 `align-self`，靠 flex 容器默认 `align-items`（计算值 `normal`，flex 里按 `stretch` 用）吃满整栏、保持居中。
- **颜色家族只表示"谁在说话"，绝不表示"结果如何"**：用户侧蓝族（底 `#16283c`、框 `#315d8d`，与 `--blue` 同族），Agent 侧绿族（底 `#151c24`、框取 `--green` 那一支）。任务状态**只**由 `terminal` + `outcome` 决定徽标那格的 `is-pending`/`is-done`/`is-failed`（§5.15 第 3 条、**Q116**），气泡底色不参与——失败的 Agent 回复照旧靠左、底色不变、徽标是红的。
- **形状与文案各补一重线索**：圆角不对称、尖角朝向自己那一侧的底角（Agent `2rem 10rem 10rem 10rem`、用户 `10rem 10rem 2rem 10rem`）；用户那条的 `<header>` 走 `row-reverse`（角色名靠栏内侧、时间靠外）。**边、形、名**三重任一重都能分边（灰度截图与色觉障碍不再是唯一线索），没有一重单独当判据。
- **气泡不许把内容挤坏**：`.message` 带 `overflow-wrap:anywhere`，判据是**每条气泡 `scrollWidth <= clientWidth`**（§12.21 ③）——覆盖 `.message-plan` 技能序、`.message-detail`、`.message-artifacts` 下载 chip、`<footer>` 徽标加 8 位任务号（与 `result_view.ts:DIGEST_IN_ROW = 8` 是同一约束的两次落地）。
- **顺序与滚动照旧**：消息按 `state.messages` 自上而下，`updateAgent()` 末尾 `scrollTop = scrollHeight` 不动；只把每条推到左右某一侧，**不做"按角色分两栏"、不并排**。本地历史（`localStorage`，§5.15 末注 `terminal ?? true`）读回的旧行本来就带 `user`/`agent` 类，旧记录在新样式下自动成为气泡，无迁移、无分裂。
- **对外通道零变化**：不发新请求、不改请求形状、`agent.*` 键一个不增不减、不写 `World`、不进 `toJSON()`/`/getWorld`/摘要。落点只有 `styles.css`；`parity_inventory.py` 与 `audit_parity.py` 清单不增不减，`docs/feature-inventory.csv` 不加行。

### 7.5 Agent 面板的标题行与空态（**Q125**，2026-10-27 新增）

五条口径：

- **标题行只留 `<h2>Agent</h2>`**：`<header class="agent-header">` 里包着 `<small>ASSISTANT</small>` + `<h2>Scene Agent</h2>` 的 `<div>` 整个撤掉；右侧那组（新建会话／历史／`#agent-state`）不动，`agent.header` 键管的仍是这个 `<header>`。**补一条样式** `.agent-header h2 { margin: 0 }`（全站 `h2` 默认 `margin: 2rem 0 0` 会把一行标题推到中线以下）。
- **空态换的是这句话，不是结构**：`.agent-empty` 的 `<i data-lucide="bot">` + `<strong>` + `<p>` 三件套形状不变，两行文字为 `Agent` / `向Agent发送命令`。空态仍只在 `state.messages.length === 0` 时出现，仍由 `agent.messages.emptyState` 门控。
- **`Text` 撤下后语音状态走 toast**：`#speech-status` 删除，"正在聆听"改走 `showAgentToast("正在聆听…")`（面板内 `role="status"` 那条，`#agent-toast`），不为瞬时状态留常驻 DOM。`agent.input.speechStatus` 键成为 `unreferenced`（§9.3 末条，占位策略见 §5.9，键不删）。
- **Operations 子面板整块撤下**：`<button class="operations" data-action="toggle-operations">`、`<div id="operations-content">`、`#operation-count` 三处宿主与 `toggle-operations` 分支一起删。**删的是界面，不是数据**：gateway 推来的 operations 帧仍按 §5.15 原样处理。`updateAgent()` 里对 `#operation-count`／`#operations-content` 的写入必须**跟着模板一起删**（留着会每次渲染 `querySelector` 回 `null` 抛 `TypeError`，面板表现为"整块不动了"）。`agent.operations` 键同样留成 `unreferenced`。
- **判据是计数为零**：`.viewport-badge`／`.operations`／`.operations-content`／`#operations-content`／`#operation-count`／`#speech-status` 六处宿主在页面上**计数为 0**、样式表对应规则 0 条；`.agent-header` 只有一行、标题文本恰为 `Agent`；空态恰含 1 个图标、文本 `Agent 向Agent发送命令`；页面文本里 `Scene Agent`、`ASSISTANT`、`Commands execute through` 三串命中 0。取证见 §12.23 ①。

### 7.6 Agent 输入框的回车 = 点发送（**Q129**，2026-10-27 新增）

光标聚焦在 Agent 面板输入框时，按回车等同于点击发送。七条口径：

- **"等同于"在实现上是字面的**：回车分支只做 `form.requestSubmit()`，与 `<button type="submit" class="send" data-feature="agent.input.send">` 派发**同一个 submit 事件**、落同一个处理函数（`enabled("agent.input.send")` → `submitAgent(input.value)` → `input.value = ""`）。**不复制第二套发送逻辑**：空文本两条路都答 `Agent command cannot be empty`，会话创建中都被 `sessionCreationInFlight` 拦回，请求 body 逐字段相同（只有 `clientRequestID` 的 uuid 每次新造）。
- **`Shift+Enter` 仍然是换行**：输入框是 `<textarea rows="3">`，多行能力不撤；`shiftKey` 为真时不接管、浏览器默认插入换行。
- **输入法确认候选词的回车不算发送**：判据是 `isComposing`（§12.25 ② 用合成的 `isComposing: true` keydown 复现）。
- **按住不放不连发**：`repeat` 为真时不接管。
- **`agent.input.send` 关掉时回车回到浏览器默认**：该键门的是发送按钮本身；回车是这枚按钮的别名，按钮不存在时别名也不存在——键路直接 return 且**不** `preventDefault` ⇒ 换行照旧。
- **`Ctrl/Meta/Alt+Enter` 不接管**：只有不带修饰键（`Shift` 除外）的回车是发送；修饰组合留给浏览器与操作系统。**不新增 feature 键**（`agent.input.send` 仍是唯一读者）。
- **判据分两层，且每一刻都有回应**：纯函数层把上述五条收进 `logic.ts:enterSendsAgentMessage(features, key)`，`main.ts` 只把事件字段抄给它、照回答决定接管与否（与 §4.8.5 `shadowsEnabled`、§4.8.6 `boundingBoxesEnabled` 同构）。页面层读真浏览器里那条气泡与输入框的值。**R2「点击必须有响应」在此的形态**：接管时走 submit 链（成功出气泡、失败出告警），不接管时走浏览器默认（换行）——两种回答都不是沉默。对外通道零变化。

---

## 8. TRAJ v2 播放适配（对照 `docs/轨迹文件布局描述.md`）

8.1 解析必须实现 §7 全部 8 条要点：全小端 `DataView`、magic 用数值 `0x5452414A` 比对、`pose_dim` 为唯一帧步长、`data_offset` 是绝对偏移、段可不连续且 `start_frame` 不必从 0 起、`step_count` 是步骤号非段号、`0xFFFFFFFF`→场景根（本规格 §2.5）、只重算偏移即向后兼容。

8.2 id 映射：TRAJ `object_id` ↔ 当前场景 `object_id_in_scene`。**该 id 不在场景中 → `showError` 提示 + 忽略该物体 + 继续执行**，并在 `ignored_objects` 中回报（§5.12）。

8.3 机器人判定：**物体带 `RobotDriver` 组件即视为机器人**（原 `scene.json.scene.robot.id` 概念不存在）。**这条判定与 `role` 无关**（**Q107**）：`role: "passive"` 的 URDF 家具在这一节里仍然是机器人——前 6 个分量照样按自己的 `joint_sequence` 赋关节角、按 `joint_limits` 裁剪、第 7 分量照样只记录；`role` 只把它从"轨迹路点与段末幽灵"两个装饰集合里剔出去（§7.3 第 1/7 条），**派发路径 `drivers.ts:apply()` 一个分支都不加**。
 - `pose_dim=7` 且是机器人 → 前 6 个分量按 `RobotDriver.joint_sequence` 顺序赋关节角（弧度），第 7 个为夹爪开合度（0.0 张开 / 闭合值来自 metadata `grasp_properties` 或 URDF 关节极限）。
 - 关节角必须按 `joint_limits` 裁剪（超界裁剪并告警一次，不逐帧刷屏）。
 - `pose_dim=7` 且非机器人 → `x,y,z` + `qx,qy,qz,qw`（**相对父物体的局部位姿**，与 §2.8 一致）。

8.4 **浏览器端前向运动学（必须实现）**：
 - TS `UrdfLoader`：解析 URDF 的 `links/joints/origin/axis/mesh/scale`，构建可 articulate 的 Three.js 节点树（joint 节点 = 可旋转/平移的 `Object3D`，visual 挂在 link 下）。
 - 每帧按 `joint_sequence` 写入关节值并刷新世界变换（`updateMatrixWorld`），由 §5.4/播放循环驱动。
 - 写入一个关节就写入它的全部模仿者（§6.4、**Q69**）：`joint_sequence` 只需命名被驱动的那一条，另一侧手指按 `multiplier * 源 + offset` 跟着动；源被 `joint_limits` 裁剪时，派生值取**裁剪之后**的源值。
 - 第 7 个分量的夹爪映射：**v1 只记录不驱动几何**（**O1**），不得猜测关节名；`<mimic>` 已落地，O1 只要给出关节名、写进 `joint_sequence` 就能带动整侧手指。
 - 驱动模块只依赖节点/关节句柄视图（`TrajView`），不依赖渲染器本身；裁剪告警去重与序列错位的处理见 §13.8 **Q39**。

8.5 空隙帧与未覆盖帧：沿用上一段末帧姿态；首段之前的帧使用默认位姿（Header/Index 默认位姿）。三种情形的精确取值与段表在解码期的升序归一化见 §13.8 **Q37**。

8.6 Attach：每次跳帧按 `frame <= f` 重放全部挂载事件以还原层级（顺序按帧递增）；`parent_id` 指向的物体不存在 → 忽略该事件并记 `warnings`。`parent_id` 是机械臂时，挂载目标节点由 `joint_sequence` 推导（§13.8 **Q38**）。重放的基准层级、哨兵父级与 `TrajView.root()` 见 §13.9 **Q41**。**父链成环同样忽略**（**Q101**）：目标节点已经是该物体的后代、或就是该物体自己 → 忽略该事件、子留在原父节点、播放继续。**这不是接口变更**：wire 侧 §5.12 的 `attach_count` 仍数文件声明的事件条数、`warnings` 仍按"父级在不在场"静态算出；补的只是运行时 `warnOnce`（`object N cannot be hung from M, which already hangs from it`，与 §8.3 同一套一次一关节的告警口径）。必须补的理由：世界与 clip 的 id 空间对不上时会出现这种事件（如「child 2 → parent 3」配上"2=臂、3=挂在它腕上的夹爪"），闭出的环会让每次世界矩阵更新递归到栈溢出。

8.7 帧计算必须走 `requestAnimationFrame` + 已有的"定时器补偿落后帧"策略，不得每帧发起网络请求（原 `TimelineLoader` 的 HTTP 取帧路径删除，改为 `TrajFrameProvider` 本地取帧，接口签名保持 `frame(index)`）。本地帧源的幂等实现见 §13.9 **Q40**，时钟/循环/共存/RTF 的实现位置见 **Q42**。

8.8 大文件性能：100 MB 轨迹必须用 `ArrayBuffer` + `Float32Array` 零拷贝窗口（`new Float32Array(buf, offset, n)`），禁止逐帧 `slice`；`DataView` 读取集中在解码阶段，播放阶段只读已解出的 `Float32Array`。

---

## 9. 配置

9.1 新建 `config/constant_config.json`，gateway 启动时读取（缺失则用内置默认值并 warn；JSON 语法错误则启动失败并打印错误，与 OLS 行为一致），并通过 `GET /api/runtimeConfig` 下发给浏览器（浏览器需要 `library_server_url` 等做 URL 模板替换）。命令行参数优先级高于配置文件（与 OLS 一致）：`--host/--port` 已有，另有 `--config`（默认 `config/constant_config.json`）。**禁止**把 OLS 地址硬编码进 `editor/src`；**禁止**用构建期环境变量承载运行时常量（现有 `WEB_GATEWAY_URL` 仅用于 Vite dev proxy，保持不变）。

9.2 结构（v1，出厂文件必须与 `local_gateway.py` 的内置默认逐键相同，`tests/test_gateway_routes.py` 钉住）：

```json
{
  "gateway": { "host": "0.0.0.0", "port": 8099, "state_dir": "var" },
  "library_server_url": "http://127.0.0.1:8000",
  "cos_base_url": "",
  "max_payload_bytes": 104857600,
  "browser_request_timeout_seconds": 10,
  "capture_timeout_seconds": 15,
  "max_pending_requests": 8,
  "camera_preview": { "max_width": 640, "max_height": 480, "max_hz": 30 },
  "ghost_pose_opacity": 0.5,
  "traj_waypoint_radius": 0.02,
  "traj_waypoint_stride_frames": 10,
  "shadows_enabled": true,
  "urdf_bounding_box_per_link": true,
  "viewport_background_color": "#000000",
  "agent_service_url": "http://127.0.0.1:8500",
  "agent_poll_interval_seconds": 5,
  "agent_request_timeout_seconds": 10
}
```

- 没有 `library_via_proxy`：gateway 的 `{gateway}/api/library/*` 代理形式已退役（§6.9、**Q68**），浏览器直连 `library_server_url`。
- **浏览器要读的纯前端数值键**进 `RuntimeConfig` 类型（两份 `types.ts` 同步，**坑 6**）：`ghost_pose_opacity`、`traj_waypoint_radius`、`traj_waypoint_stride_frames`、`shadows_enabled`、`viewport_background_color`、`urdf_bounding_box_per_link`。**本表数值/布尔键一律 fail-open 到默认值**：只在收到形状正确的值时采用，否则（缺字段、`null`、字符串、越界、`NaN` 等）回默认——这里没有"老 gateway 就该关掉能力"的安全含义，读不懂的值最合理的解释是"没人配过"。各键的精确判据：
  - `ghost_pose_opacity`（**Q97**）：段末幽灵不透明度（§7.3），**0~1 浮点，默认 `0.5`**；`logic.ts:ghostOpacity(runtime)` 只在收到**有限、落在 [0,1] 内**的数时采用，否则回 `0.5`。`0` 是合法值（等于把幽灵调到看不见，与 `playback.ghostPose:false` 殊途同归，留作运维手段）。幽灵是纯前端画法、无服务端消费者，故是常量而非 feature 键；关掉用 `playback.ghostPose`，调浓淡用它，两件事不混。
  - `traj_waypoint_radius` / `traj_waypoint_stride_frames`（**Q101**）：路点球**半径**（世界米，默认 `0.02`）与**采样步长**（每 N 帧一个，默认 `10`）。`logic.ts:waypointSettings(runtime)` 只在半径是**有限正浮点**、步长是**有限正整数**时采用，否则（含 `0`、负数、小数步长）回默认——半径 0 把实例矩阵缩成奇异阵、步长 0 让采样循环永不前进，必须回落不照收。**没有上限**：步长 1 就是逐帧（720 帧 ⇒ 720 球），一层一个 `InstancedMesh`，代价只在内存（每球 16 字节实例矩阵）。整体关掉用 `playback.trajWaypoints`。
  - `shadows_enabled`（**Q118**）：`viewport.shadows` 为 `false`（勾选框被收走）时决定投不投影的**构建方决定**（§4.8.5），布尔，默认 `true`。`logic.ts:shadowsEnabled()` 只在收到**真正的布尔**时采用，缺字段、`null`、字符串、`0`/`1` 一律回 `true`。它没有 DOM 宿主（宿主恰是被 `viewport.shadows` 关掉的那一个），故是常量而非 feature 键；阴影是 ③ 类渲染层、一条协议都不走，关掉它的动机是省显存与帧率而不是正确性，因此随数值键 fail-open。
  - `viewport_background_color`（**Q145**）：视口清屏背景色，CSS 十六进制串（`#RGB` 或 `#RRGGBB`），默认 `"#000000"`。`logic.ts:viewportBackground(runtime)` 只在收到**去掉首尾空白后匹配 `#RGB`/`#RRGGBB`**（大小写不敏感）的字符串时采用，否则（非字符串、`"black"`、纯数字、长度不对的 `#12345`/`#1234567`、非法字符 `#gggggg`）一律回 `#000000`。gateway 只把串原样下发、从不渲染；`RuntimeConfig` 新增可选字段 `viewport_background_color?: string`。**只改 `scene.background`（清屏色），不动距离雾 `scene.fog`**（仍是构造时的黑色，近 30/远 120）——背景与雾刻意不合并。传感器通道各自覆盖背景并在结束时还原（§4.4）；视口截图沿用静止背景（`viewportPass` 不换背景），因此本键只影响屏幕与视口截图，不影响深度/米制通道。
  - `urdf_bounding_box_per_link`（**Q149/Q150**）：URDF 物体的包围盒线框**画成什么形状**（§4.8.6），布尔，默认 `true` = 每 link 一只盒。`logic.ts:urdfLinkBoxesEnabled(runtime)` 只在收到**真正的布尔**时采用，缺字段、`null`、字符串、`0`/`1` 一律回 `true`。它不是这一层的开关：`viewport.boundingBoxes` 与那枚勾选框仍决定画不画；本键只切显示形状，整机 `worldAabb()` 及所有调用方不变。gateway 从不渲染，只把布尔原样下发；`RuntimeConfig` 新增可选字段 `urdf_bounding_box_per_link?: boolean`。
  - **改以上任何值都要重启 gateway**（常量启动时读一次，**坑 22**）。
- `agent_service_url` / `agent_poll_interval_seconds` / `agent_request_timeout_seconds`（**Q86**）：Agent 任务三个运行时常量，口径来自 `docs/gateway与Agent服务通信流程.md`（轮询周期 5 s）与 `docs/服务占用端口清单.md`（AgentService = 8500）。**只有 gateway 消费这三项**：浏览器从不直连 Agent 服务（§5.15），随 `GET /api/runtimeConfig` 下发只为运维从同一端点读到当前生效值，`RuntimeConfig` 类型里**不**为它们加字段。`agent_service_url` 写成空串 = 显式关闭链路，提交任务直接 `501 AGENT_UNAVAILABLE`。**Agent 健康转发（§5.15"服务健康"，R9）不新增常量**：`/health` 上游请求 3 秒超时、`/api/agent/health` 5 秒缓存上限、浏览器侧 15 秒轮询周期都是随代码走的**内置值**——它们是这条链路的正确性参数而不是可调策略。
- `gateway.state_dir`（**Q111**）：gateway **持久状态目录**，内置默认 `var`，解析出 `var/gateway_state.json`（§5.2 绑定浏览器、§5.5 对象 id 高水位、§5.15 会话阶段与租约）与 `var/gateway_transactions/`（§5.9 `/commitWorld` 回执）。四条口径：① **只有 gateway 写它**，浏览器与 Agent 都不碰磁盘；② 目录缺失时启动即创建；写失败必须 warn 并让相应路由回 `500 INTERNAL_ERROR`，**不得**静默降级成"只存内存"；③ **一次性验证栈（`tools/serve_verification.py`，8098）必须注入临时目录**，绝不写真实 `var/`（它天生带 `X-Standalone-Verification: 1`，§11.2.1）；④ **真实进程启动即清空上一轮**（2026-11-05）：`main()` 在建 app 之前调 `gateway_state.py:clear_persisted_state(state_path)` 删掉 `gateway_state.json`（含 `_read` 挪出的 `.corrupt-*` 副本与崩溃残留的 `.tmp`）与 `gateway_transactions/`，随后以干净默认起（水位回 0、无绑定、无租约／隔离、无场景身份）；清理**只按这份状态文件自己的名字前缀匹配**，同目录里无关文件一概不碰。**清空发生在入口而绝不在 `GatewayState`／`StandaloneGateway` 构造器**——上一轮跑完，状态照旧留在盘上任人事后读取排查，下一轮 `main()` 才抹干净；构造器仍原样重载，恢复语义与排障窗口都靠它。`--keep-state` 显式跳过清空（带上一轮的绑定／租约继续跑，或经活的只读路由读上一份现场）。**副作用一并写在账上**：这条把"重启读回一份永不释放的租约／隔离"那类陈旧锁直接清掉，正是 §5.8／流程 §1.2 第 4 条换场景无条件采纳所依赖的干净起点。`serve_verification.py` 走临时目录、每次本就是空的，不受此条影响。**它是目录而不是文件名**：事务回执按 transactionID 一份一个文件。

9.3 `features.json` 同步调整。**现行键面：88 键**（`docs/feature-inventory.csv` 89 行）；全部键 fail-closed（缺键按 `false`）；`features.json` 与常量都是 gateway 启动时快照，**改值与改键面都要重启 gateway**（**坑 22**）。

**删除清单**（本期已移除的控制项，键不得复活）：
`commandBar` 整族（`commandBar`、`commandBar.connection`、`commandBar.assetUpload`、`commandBar.create.*` 全部 9 项、`commandBar.history.*`、`commandBar.simulation.*`、`commandBar.state.*`；整条顶栏撤销后 `commandBar.*` 为空，**Q92**）、`chrome.brand`（从来只有这一个 `chrome.*` 键，删掉不留断链父级）、`navigation.settings`、`navigation.assets.refresh`（**Q93** 随刷新按钮删除，清单由 `start()` 自动拉）、`panels.settings.*`（全部）、`panels.scene.dragReparent`、`panels.scene.deleteEntity`、`panels.assets.search`（**Q93** 连同 `assets.ts:filterAssets` 删除）、`panels.assets.upload`、`panels.assets.create`、`panels.assets.delete`、`panels.assets.groups.builtin/project/uploaded`、`panels.details.identity.name`、`panels.details.transform.*`（三个子键）、`panels.details.visual.*`、`panels.details.physics.*`、`panels.details.camera.output/frameId/updateRate/width/height/verticalFov/nearPlane/farPlane`（只读后不需输入项）、`panels.details.robot.targets`、`panels.details.robot.send`、`panels.details.robot.jointNames`、`panels.details.robot.positions`、`panels.details.apply`、`assetDependencies.*`（全部 7 项）、`viewport.assetDrop`、`viewport.tools.move/rotate/scale`、`viewport.tools.translate/rotate/scale`（**Q114** 撤 gizmo；父键 `viewport.tools` 保留，宿主是 `<div class="segmented" data-feature="viewport.tools">`，组里仍有 `data-tool="select"`）、`viewport.overlays.collider`、`viewport.overlays.ifps`、`viewport.cameraPreview.header/close/image/status/metadata`（旧浮动预览卡；只留 `viewport.cameraPreview`/`.modes`/`.modes.rgb`/`.modes.depth` 四键，它们是 RGB/Depth 切换的父链，必须保留）、`viewport.rendererBadge`（**Q125** 随角标宿主删除）、`presentation.syncAndAnimate`、`presentation.timeline`、`statusBar.backends`。死键判据统一为"代码里既无 `enabled()`/`when()`/模板串引用，也无 `data-feature`/`data-requires-feature` 宿主"。

**保留/新增清单与接线口径**：
- `panels.details.transform`（只读 Transform 区块）与 `panels.details.transform.notice`（"播放中的位姿来自 TRAJ 采样"提示）**必须留在配置里**：父链任一为 false 即整体隐藏（`logic.ts:featureEnabled`），删了会让 Details 选中相机以外物体时整块消失。同理保留 `panels.details.identity`、`panels.details.robot`（只读关节序列/限位）、`panels.details.camera.preview`、`panels.scene.search`、`panels.assets.list`。
- `navigation.assets.presets`/`prefabs`/`robots`：两级层级栏第一层的三个入口（`LIBRARY_KINDS` 三大类）。**Q93 的两级侧栏不新增第二层键**：子类完全由 `/listCategory` 数据决定，选中的大类/子类存 `state.library`，是视图状态而非配置项。
- 播放条 7 键：`playback.bar` 在 `#timeline` 容器；`playback.play` 同时控制 play/prev/next 三个按钮（"能不能播"是一件事；stop 按钮已删，**Q88**）；`playback.seek` 控制 `.timeline-track`+`#timeline-time`+`#timeline-frame`；`playback.rate` 控制倍速 `<select>`（RTF 数值输入已删，**Q88**，键保留）；`playback.loop` 控制循环按钮；`playback.stepMarkers`/`playback.attachMarkers` 在 `renderPlaybackTicks()` 内分别控制 `step_count` 刻度与 Attach 刻度（§7.3）。**点击刻度定位播放头（Q90）不另设键**：刻度画在 `.timeline-track[data-feature="playback.seek"]` 里，宿主关断时整条轨道连同刻度消失，复用 `playback.seek`。
- `playback.ghostPose`（**Q96**）：与 `stepMarkers`/`attachMarkers` 同族——不是页面控件，宿主是 3D 场景；`parity_inventory.py` 判 `literal`（生产者只有 `main.ts` 里那一次 `enabled()`，在 `updateGhostPose()`，由 `showFrame()` 每帧调用）。不新增 gateway 调用（幽灵用的 URDF 就是真机已取过的那份，`renderer.ts` 的 `modelFiles` 缓存）。
- `playback.trajSelector`（**Q100**）：宿主 `<select id="traj-select">`，判 `literal`。关掉 = 藏下拉框、永远播第 0 条，**不影响 §5.3 请求能不能带多条**（批次是线格式的事，选哪条是 UI 的事）。路由清单不增不减，但 §5.3 改了 `/updateTraj` 的媒体类型与载荷，`audit_parity.py` 的三条 octet-stream 探针随之改判（JSON 体 / 空体 / 无浏览器时中继），CSV +1 行、`docs/parity-probes.json` 重生成。
- `playback.trajWaypoints`（**Q101**）：宿主是工具栏那个 `<label>`，判 `literal`，但**同时**管 3D 那层：`enabled()` 在扫描与画球处再判一次，关掉 = 勾选框不见、球也不再出现（与纯 scene graph 的 `playback.ghostPose` 不同）。密疏与大小走 §9.2 两个数值常量。路点是纯内存扫描，不新增 gateway 调用、不改 `/updateTraj` 请求形状，路由清单与探针不增不减。
- `viewport.loadScene`/`viewport.loadTraj`/`viewport.lightingPreset`/`viewport.exportScene`/`viewport.snapshot`/`viewport.record`/`viewport.shadows`/`viewport.boundingBoxes`/`viewport.localAxesToggle`/`viewport.cameraWall`：都有真实 DOM 宿主（按钮或 `<label>`），判 `literal`。`viewport.snapshot` 关掉 = 按钮消失、剪贴板里还是上一次那张，**没有"截不到"这条错误路径**。`viewport.record:false` ⇒ 按钮与勾选框**两处宿主一起消失**，且 `toggleRecording()` 第一句再判一次——用 `evaluate` 直接点看不见的按钮**也不起机**（点击在 `[data-feature]` 委托闸门就被丢掉，处理函数里的判断是第二道防线；§12.22 ③ D1）。「录制含辅助图层」做成勾选框而非常量，因为它是**每一段带子**的取舍，不是跨 tab 必须一致的构建方配置。**`viewport.localAxesToggle` 由 `viewport.localAxes` 改名（Q132）**，判 `literal` 的依据是 `logic.ts` 里那一次引名字符串；改名不增不减键。**只在 `data-feature` 属性里写、源码里没有带引号键名的，判不出 `literal`**（`parity_inventory.py` 要的是 `"key"` 字符串）。
- `viewport.cameraFrustumAlways`（**Q105**）、`viewport.lightMarkerAlways`（**Q106**）：**默认 `false`**——这份配置里仅有的两个 `false`（用户要"平时别看辅助图层，选中才看"，§4.4）。落在 `viewport.*` 族，**不复活** `viewport.overlays.*` 父族。宿主形态与 `playback.ghostPose` 同族：管 scene graph、没有 `data-feature` 元素，判 `literal` 的依据是 `main.ts:applyFeatureVisibility()` 里那一次 `enabled()`；`renderer` 侧只收 `setCameraFrustumAlways()`/`setLightMarkerAlways()` 两个布尔 setter——**renderer 从不直接读 `features.json`**（`main.ts` 是唯一消费者，renderer 收推送；Q96 以来一贯分工）。
- `viewport.worldAxes`、`viewport.grid`（**Q131**）：**默认都 `true`**（保持既有画面）。各管自己那一件的 `visible`，两键互不相干；管 scene graph 不管控件，没有 `data-feature` 元素、工具栏不配勾选框，判 `literal` 的依据是 `main.ts` 里那两次 `enabled()`；`renderer.ts` 只收 `setGridEnabled()`/`setWorldAxesEnabled()` 两个布尔。**它们管不到任何对外通道**：两件都是 `Helper`，§4.4 排除清单早把它们按在采集之外，键开键关下 `/captureScene`、视口截图、`worldAabb()`、`/get2DAABB`、`frame()` 一个字节都不变。**这两枚键的坑 22 与别处不同**：fail-closed + 启动快照 ⇒ 不重启 gateway，新键在旧进程里读成 `false`，**屏幕上原本一直看得见的网格与坐标轴会消失**——这是本表少数"缺席会误伤既有画面"的键；仍**不补常量**（撤掉的只是屏幕参考线，不是采集结果，症状一眼对上"重启没做"）。取证出口：`logOverlayState()` 的 `console.debug("Viewport markers …")` 行里的 `gridEnabled`/`worldAxesEnabled` 两个布尔。
- `viewport.cameraWall`（**Q138**）：**默认 `true`** = 工具栏有「相机画面」勾选框。宿主键，与 `viewport.boundingBoxes`/`viewport.localAxesToggle` 同形：键管 `<label>` 在不在，勾选框管窗开不开，判据 `logic.ts:cameraWallEnabled(features, checked)` = **键 && 勾选**，`main.ts:renderCameraWall()` 是唯一调用者；键关 ⇒ 宿主消失且这一层不存在。默认外观就是"没有窗"，**不补常量**。判 `literal`（依据 `logic.ts` 引名字符串）。**与 §4.8.5–4.8.8 四层的形式差别**：那四层的最终布尔送渲染器，这一层的布尔只决定一块 DOM 存不存在——`renderer.ts` 一字未动（`renderSensorFrame()`/`sensorSetup()` 原样复用），"渲染器不读配置"的分工不被绕开。取证出口是这层 DOM 自身（`#camera-wall-count` 与每格 `data-camera-status`），**不配 `console.debug`**（只有"键关"与"没勾"两态同形，且都是整块 `hidden`；"场景里没有相机"是窗开着、里面一句空态）。**Q139 不动这一族任何键**：窗里 `RGB`／`深度` 两枚按钮把深度态挂在既有 `viewport.cameraPreview.modes.depth` 上（`data-feature` 与 `cameraWallModeFor()` 两道都读它），CSV 只把 `modes.depth` 那行的 `editor_reach` 依据由 `interpolated` 改成 `literal`——**Q139 没有"要重启 gateway"这条生效条件**。
- `agent*` 全部保留不动。**Agent 任务制改造（§5.15，Q86/Q87）不增不减任何 feature 键**；`agent.debug` 的键与帧处理照旧留着（新样例 Agent 服务不产生 debug 帧，那块面板为空是预期状态）。**占位策略**：`agent.operations` 与 `agent.input.speechStatus` 的宿主已删（**Q125**）但**键留着**——`parity_inventory.py:status()` 对 `agent.*` 一族写死 `PLACEHOLDER_KEPT`（§5.9 把 Agent 面板键面当保留清单），两行状态为 `unreferenced`。
- feature 键**不进** `RuntimeConfig`（键面由 `/api/runtimeConfig` 的 `features` 字典整体带出，不需要镜像类型，与 **坑 6** 无关）；feature 键改动不动两份 `types.ts`。
- 派生文件惯例：`docs/parity-probes.json` 只差时间戳与 uptime（features 计数未变）时**不提交**；计数真变了才提交那一格。重生成带出的 `viewport.grid`/`viewport.worldAxes` 两格若是用户本地运行时值，`git checkout --` 回退、不入库。

---

## 10. 删除清单（原需求答复 二.1/二.2）

除 **Agent 模块的会话与调试部分**外，所有未被新需求使用的功能一律删除（Agent 不整块豁免：任务链路按 §5.15 改造，被砍的一条是 10.1 的 `jobs/{jobId}/cancel`，**Q86**）。

10.1 gateway 路由删除：`POST /api/commands`（含全部 13 个 method，`editor.undo`/`editor.redo`/`simulation.*`/`state.save`/`state.restore` 一并消失）、`GET /api/bootstrap`（由 `GET /api/runtimeConfig` + `GET /getWorld` 取代）、`GET /api/assets`、`POST /api/assets/upload`、`POST /api/assets/import-remote`、`GET /api/assets/remote-proxy`、`DELETE /api/assets/{id}`、`GET /api/assets/{id}/file[/{path}]`、`GET /api/sensors/{entity_id}/frame`（501 占位）、`POST /api/state/load-simstate`、`GET /api/diagnostics`、`GET /api/metrics`、`GET /api/presentation`、`POST /api/presentation/mode`、`POST /api/agent-control/{...}`（`captures*` 由 `/captureScene` 取代）、`GET /api/timelines/{id}/frames/{i}`、`GET /api/timelines/{id}/data`、**`POST /api/agent/jobs/{jobId}/cancel`**（§5.15：Agent 服务没有取消接口，恒 501 的路由只会让人误判成"暂时没连上"）。

10.2 gateway 状态/字段删除：`self.entities`、`saved_states`、`undo_stack`、`redo_stack`、`simulation_time/simulation_status`、`presentation/timeline` manifest、`snapshot()` 全结构。

10.3 Python 模块：`local_assets.py` 的上传资产体系删除（`data/uploads/` 不再使用）；`local_urdf.py` 的 `load_urdf()` 与 `update_world_transforms()` 删除（职责迁入 TS）。

10.4 前端删除：`editor/src/timeline.ts` 的 `TimelineLoader` HTTP 取帧与 `reconstructTimelineFrame`（JSON patch）、`contracts/types.ts` 中 `SceneSnapshot/EntityView/TimelineManifest/CompactTimelineFrame` 等旧结构（改为世界文件对应的 `World/ObjectDef/Component` 类型）、上传依赖选择弹窗、gizmo 工具切换（**Q114** 终态：`translate/rotate/scale` 三种工具连同工具栏三把键（DOM 值即这三个字面量）、`PoseGizmo`/`createTransformGizmo` 接线、`World.setLocalTransform` 一并移除，§4.8.3 整节作废；选中描边（`Box3Helper`，Q105）与 `worldAabb()` **保留**，它们不是编辑工具；不存在"恢复但不回传"的中间形态——连改动入口都没有）、Settings 面板、可编辑 Details 表单（**Q114**：关节角度输入整条链路删除——`editJointAngle`、`joint-edit` 渲染分支、`data-joint` change 分支、`World.setLocalJointPosition`、`local_edits.ts` 的 `jointEditEnabled`/`jointEditorRows`，以及常量 `allow_details_joint_angle_edit` 的全链（`config/constant_config.json`、`local_gateway.py` 内置默认、两份 `types.ts`、README），§4.8.4 整节作废、**Q81** 废止；面板回到纯只读读数，仍显示 §4.7 那份 `initial` 打底、`now` 覆盖的合并位姿与 `(now)` 标记）、`editor/src/main.ts:trackAgentJob`（浏览器侧每 1 s 拉任务状态的循环，轮询职责整体搬到 gateway，**Q87**）与 `refreshAgentDebug`（零调用点死代码）。

10.5 契约/文档/测试需同步（**验收项**）：`contracts/types.ts` 与根 `types.ts`（两份当前内容相同）、`docs/api-inventory.json`（用 `tools/parity_inventory.py` 重生成）、`docs/feature-inventory.csv`、`docs/parity-probes.json`、`README.md`（Models/cameras/Verification 段全部按新架构改写）。

10.6 测试处理：`tests/test_local_assets.py` 断言的是已删除行为（`/api/commands`、upload、`/frame` 501、`scene.changed` 信封等），必须重写为新的接口表 + WS 协议测试；`/api/agent/jobs` 的"恒 501"断言改为"`agent_service_url` 为空才 501"，任务转发的真实行为由 `tests/test_gateway_agent.py` 用假 Agent 服务覆盖，会话 CRUD 断言不动；`tools/browser_smoke.cjs` 改写为：Load Scene → 资产面板加载预设场景 → `POST /updateTraj` 播放 → `GET /captureScene` 像素校验 → `GET /get2DAABB` 数值校验；`tools/audit_parity.py` 探针目标同步更新。`editor/src/*.test.ts` 中被删模块的用例删除，`UrdfLoader`/`TrajFrameProvider`/投影函数补新单测。

---

## 11. 非功能要求

11.1 请求体上限 **100 MB**（`/updateTraj`、`/updateWorld`、用户本地文件同限），超出 `413`。**这条上限必须由 `create_app()` 透传给 aiohttp**（`web.Application(client_max_size=…)`）：aiohttp 默认 **1 MiB** 且在进入路由之前就挡——不抬的话调用方拿到的是没有 §5.6 错误体的裸 `413 Internal Server Error` 页面，`max_payload_bytes` 配成 100 MB 也永远轮不到生效。**Q99** 之后一次请求可装 N 份轨迹，跨线概率大增；`tests/test_traj_api.py` 用一份 1 MiB+4 KiB 的两条批次锁住这条口径。

11.2 无鉴权（内网调用方），但**必须**：§5.9 新增的全部接口（`/updateWorld`、`/updateItem`、`/addObject`、`/removeObject`、`/updateTraj`、`/captureScene`、`/get2DAABB`、`/getWorld`）只监听 `constant_config.json` 指定地址；默认 `0.0.0.0`，仅适用于可信局域网并由主机防火墙限制来源；`Content-Type` 严格校验（`/updateTraj` 必须为 `multipart/form-data`，**Q99**）；浏览器上报的 json 在 gateway 侧仅做尺寸与 JSON 合法性校验（防放大）。gateway 不得把调用方传入的任意字符串拼接成 OLS 路径；若启用 §6.9 代理模式，代理路由必须白名单限制，禁止转发任意 URL。

11.2.1 **端口分配**：gateway 默认监听端口 **`8099`**（`config/constant_config.json`、`local_gateway.py` 内置默认、`start-dev.ps1 -GatewayPort`、`editor/vite.config.ts` 的 `WEB_GATEWAY_URL` 兜底四处必须一致，`GET /api/runtimeConfig` 里的 `gateway.port` 就是浏览器看到的同一个数）。**一次性验证栈**（`tools/serve_verification.py` + `tools/browser_smoke.cjs`）用 **`8098`**，以免真实 gateway 与验证 gateway 抢端口——验证栈带 `X-Standalone-Verification` 头，撞端口会让冒烟认错对象。**`8080` 只作备用**；`docs/服务占用端口清单.md` 与本节是唯一分配口径。

11.2.2 **多 profile 持久化**（Q153）：`var/gateway_state.json` 保留首个／原有绑定 profile；后续 profile 写入 `var/gateway_profiles/<sha256(browserID)>.json`，事务回执写入 `var/gateway_transactions/<sha256(browserID)>/`。profile 文件名只用 SHA-256，原始 id 仅存于 JSON 内容，避免 id 进入路径。`--keep-state` 按 browserID 恢复各自状态；普通启动按现有清理流程清空主状态、profile 文件与所有 profile 回执。任务与对话仅驻进程内存，gateway 重启后按既有规则不恢复。

11.3 命名规范：路径用 lowerCamelCase 且与 §5.9 完全一致（`/get2DAABB`，非 `get2Daabb`）；query 参数一律 snake_case（`id`、`camera_id`、`browser_id`、`expected_version`）；世界文件字段按 `docs/ur5e_table_empty.json` 字面不改名（蛇形/驼峰混用一律保留）；错误码全大写下划线。

11.4 可观测：gateway 对每个 `request_id` 记录一行结构化日志（action、耗时、目标浏览器、结果码），便于排查 504。目标浏览器**恒为绑定的那一个**（§5.2），这一列直接记 `browser_id`；`browser_id` 与请求里的 `?browser_id=` 不符而被拒时，记**绑定值**与断言值两个——这是排查"用户开了第二个 tab"的唯一线索。

11.5 性能指标：640×480 `rgbd` 抓取端到端（调用方→浏览器→调用方）**应** ≤ 250 ms；720 帧 UR5e 轨迹在 60 fps 下播放不掉帧低于 45 fps；预设场景（≤50 物体）加载应 ≤ 3 s（不含首次模型下载）。

11.6 发布包（**Q94**）：`python pack.py <版本号>` 产出**可运行包**，不是源码包。
  - **版本号格式**：`大版本.中版本.小版本[-附加字符串]`，正则 `^\d+\.\d+\.\d+(?:-[0-9A-Za-z.~_-]+)?$`，例 `1.0.0`、`1.0.0-rel`、`1.2.1-dev.3`。版本号同时是目录名，含 `\ / : * ? " < > |` 或等于 `.`/`..` 的一律拒绝。
  - **产物位置**：`distribute/<版本号>/WebEditor-<版本号>/`（解包内容）+ 同级 `WebEditor-<版本号>.zip`（zip 内根就是那一层目录）。`--dist-dir` 给相对路径时按**仓库根**解析。同一版本号重复打包：**先把旧目录改名成 `WebEditor-<版本号>.old` 再删**，zip 就地覆盖，**不加时间戳另起目录**。改名是安全阀：Windows 下正在被运行的 gateway 当工作目录的包删不掉，`shutil.rmtree` 会先掏空内容、最后才在锁上失败；目录改名是原子的，锁着就整包失败、原样保留，脚本据此退出并提示"先停掉在用该目录的 gateway，或换一个版本号"。
  - **包内清单**：`dist/`（§11.7 的 SPA 产物，运行期唯一前端来源）、`local_gateway.py`、`features.json`、`config/constant_config.json`、`docs/`、`examples/`、`README.md`、`requirements.txt`、`launch_gateway.bat`，以及打包时写出的 `VERSION.txt`（`AppVersion <版本号>` + 打包时刻；gateway 启动时把首行打进日志，源码树里没有该文件则打印 `unversioned checkout`）。**不含** `node_modules/`、`editor/`、`tools/`、`tests/`、`contracts/`、根 `types.ts`（开发面，运行期读不到）。
  - **构建由脚本负责**：默认先跑 `npm run build`（`tsc -p editor/tsconfig.json` + `vite build`），构建失败即退出且不动 `distribute/`；`--no-build` 复用现有 `dist/`。两种构建缺失（`dist/` 不存在，或目录在但没产物）都在**删旧包之前**由同一个清单校验拦下退出，绝不出现"旧包已删、新包没成"。
  - 目标机只需 **Python 3.9+ 与 `pip install -r requirements.txt`**（aiohttp），不需要 Node。OLS 与 Agent 服务是外部依赖，包内不含，地址由 `library_server_url` / `agent_service_url` 指。

11.7 gateway 服务产物（**Q95**）：`GET /` 与 `GET /{route:.*}` catch-all（`static()`）按"**真实文件优先、SPA 兜底**"解析 `dist/`：请求路径命中 `dist/` 下真实文件（`/assets/index-<hash>.js`、`/vite.svg` 之类）就原样返回，否则返回 `dist/index.html`。三条硬约束：① **不得越界**——拼接后解析出的绝对路径必须仍在 `dist/` 之内，`/../config/constant_config.json` 这类一律落到 SPA 兜底，配置文件与 `features.json` 不能借静态路由被读到；② **注册顺序不变**，`static()` 仍排在 §5.9 全部路由之后（坑 2）；③ 缺 `dist/index.html`（没构建就起 gateway）返回 **404** 并在响应体里点名要跑 `npm run build`，**不回退** `editor/index.html`（那是 vite 开发入口，回退只会把"没构建"伪装成"页面坏了"）。开发流程不受影响：日常仍是 vite 5173 代理到 gateway。

---

## 12. 验收标准

**通用口径**（各小节不再重复）：
- **接口面"不增不减"** = §5.9 路由清单、`tools/audit_parity.py` 探针（现行 **62/62 · gaps []**）、`tools/parity_inventory.py`（`missing_caller_routes []`、`still_registered []`、`features_local 88`、`features_unreferenced` 恰为 `agent.input.speechStatus`／`agent.operations` 两条、`caller_routes 11 / deleted_routes 19 / browser_actions 8`）均无增删；`docs/feature-inventory.csv` 现行 **89 行**。
- **回归基线** = `npx tsc -p editor/tsconfig.json --noEmit` 干净、`npm run build` 通过、`npx vitest run`（现行基线 **619 例 / 18 文件**；纯样式/纯 CSS 轮次"例数逐位不变"本身就是判据）、`python -X utf8 -m unittest discover -s tests`。
- **测量通坑**：**坑 22**（features/常量是启动快照，改键面/改值要重启 gateway；未重启按 fail-closed 读 `false`）、**坑 68**（`page.route('**/api/runtimeConfig')` 只拦页面自己发的请求，补丁生效与否只从页面侧证据读，`page.request.get` 回读的是网关真实值）、**坑 81**（帧计数在点击当场算增量，页面重载自身会发一帧 `browser.state_sync`）、**坑 87**（`/get2DAABB` 响应里 `request_id` 每次是新 uuid，整份 JSON 哈希永不相等，比对必须剥掉它）、**坑 6**（两份 `types.ts` 逐字节相同）、**坑 90**（`tools/browser_smoke.cjs` 自 S5-a 起被 §9.1 身份闸挡在 §12.4，新增判据的真机证据由一次性作用域探针/隔离页给出，探针脚本跑完即删）。
- **像素测量口径**（§12.19 起沿用）：PNG 字节差不能当判据（一个像素抖动就重排 deflate）；判据用 `#viewport` 元素截图在页内解码后的**整帧均值亮度与亮像素数**，且必须先 `addStyleTag` 藏掉 `#toast`（**每次 `page.goto` 之后都要重藏**）并把轨道相机推近；可逆性判据是"来回切两次、再开与初开逐字相同"。
- 派生文件（CSV/probes）只差时间戳、uptime 或用户本地运行时值时按惯例不提交（§9.3）。

12.1 加载一份世界文件后：页面出现该文件的物体，几何来自 OLS 真实模型而非立方体；层级树父子关系与 `parent` 一致；`/getWorld` 回读与提交内容一致，且 `scene_version` 按 §3.9 的四条口径前进。取证分两处：**真机端到端**（`tools/browser_smoke.cjs` §12.1 幕）走手动 Load Scene 读 `docs/ur5e_table_empty.json`——即 OLS 侧 scene 1（8 个物体，数组顺序 `0、7、1、2、3、4、5、6`），带 `MetadataRef.type` 与 link 帧的 `RobotAttachment`（**Q73**），状态栏断言 `#1 ur5e_table_empty · v3 · 8 objects`，模型字节确实来自 `/model`+`/file`；**`POST /updateWorld` 的字节级往返、`rejected_objects` 与"无占位立方体"**由 `world.test.ts`/`scene_requests.test.ts` 用 `tests/fixtures/scene_example.json`（已删除的 `docs/scene_example.json` 的字节副本，**数值禁改**）钉住（**Q62**/**Q66**）。

12.2 `POST /updateItem` 修改 `object_id_in_scene=5` 的 `Transform.parent` 为 2 → 所有已连接浏览器同步移动；body 不带原 `Semantic` 组件 → 该组件消失（全量替换语义）。

12.3 `POST /addObject` 不带 id → 响应回传新 id，= 当前最大 id+1；带已存在 id → `409 ID_ALREADY_EXISTS`。

12.3b `POST /removeObject`：先 `addObject` 造一条 `Table(root) → Cup(parent=Table)` 链，`removeObject?id=<Table id>` → `removed_object_ids` 含两者（级联，§5.14）；加 `detach_children=true` 重放同场景 → 只删 Table，Cup 的 `Transform.parent` 变 `null` 且 `reparented_object_ids` 含 Cup id；再次删除同一 id → `404 OBJECT_NOT_FOUND` 且 `scene_version` 不变；删除后 `/getWorld` 回读中该物体不存在，其他浏览器同步消失。

12.4 `POST /updateTraj`（`docs/robot_grasp.traj`，12088 B，**单条也要按 §5.3 的 multipart 发**：一个 `trajs` part）→ `{"ok":true,"count":1,"trajs":[{"index":0,"fps":60,"total_frames":720,"duration_seconds":12,"object_count":3,"attach_count":2,…}]}`；拖动进度条到 340 帧 → 苹果成为机械臂子节点并跟随；到 530 帧 → 苹果改挂盒子；机械臂 6 关节按段表 smoothstep 运动（证明 §8.4 FK 生效）；夹爪开合按 §8.4 记录；引用不存在的 id → 弹一次错误提示且播放不中断。

12.4b **多条轨迹与选择器**（**Q99**/**Q100**）：同一次 `POST /updateTraj` 带 **2 个 `trajs` part**（`docs/robot_grasp.traj` + `examples/scene3_9_segments.traj`）→ 响应 `count: 2`、两条各带自己的 `index/fps/total_frames/object_count/ignored_objects/warnings`（两条对同一世界的 `ignored_objects` 判定**互不污染**）；`#traj-select` 出现 **2 个选项**且默认选中第 1 个，播放条读数与刻度来自**第 0 条**（`0 / 719`、**5** 根刻度：3 段起点 + 2 attach）；切到第 2 条 → 播放**立即暂停、播放头回 0**、读数与刻度换成 840 帧那条（`0 / 839`、**11** 根刻度）；切回第 1 条 → 再回 `0 / 719` 与 5 根刻度（**不保住当前帧号**）；段末幽灵读同一个活动轨迹 getter，与刻度在同一次 `setTrajectory()` 里重建（像素证据 §12.12）；只带 1 条时 `#traj-select` 隐藏；`playback.trajSelector` 关掉时同样隐藏且仍播第 0 条；第 2 个 part 故意写坏 magic → 整批 400 `TRAJ_INVALID_MAGIC` 且消息点名 `trajs[1]`，**下拉框与播放条保持原样**（§5.3 全有或全无）；`?count=3` 而只发 2 个 part → 400 `INVALID_PARAM` 且**没有向浏览器下发任何帧**；这一路之后 `GET /getWorld` 仍回原 `scene_version`、层级树行数不变（轨迹不是世界，**Q33**）。

12.4c **本地一次选多个轨迹文件**（**Q104**）：`#traj-file` 带 `multiple` ⇒ 一次递两份文件（`examples/scene3_wrist_first.traj` 840 帧 + `examples/scene3_alternate_joints.traj` 610 帧）→ 下拉框在**已有列表上整批追加两行**、标签是各自**文件名**与帧数、**选中这批第一条**、播放条 `0 / 839`；切第二行 → 播放暂停、播放头回 0、读数 `0 / 609`、刻度按 610 帧那条重算；再递「一份坏文件 + 一份好文件」⇒ **整批不生效**（§4.3 全有或全无，toast 点名坏的那份文件名），列表行数、选中项、播放头读数一字不动；只递一份好文件与单选一模一样；三份里两份合法 ⇒ 也整批拒绝，**不做"跳过坏的那条"**；全程 `scene_version` 不变、浏览器一帧 `browser.state_sync` 都不发（**Q33**）。两份样例轨迹由 `python -X utf8 examples/make_scene3_variants.py` 生成并逐字段回读（**Q103**），与 `examples/scene3_9_segments.traj` 三份并存互不覆盖。

12.5 `GET /captureScene?id=2&type=rgbd` → JSON 含 png base64 与 `width*height*4` 字节的 float32 深度；对深度第 i 行第 j 列取值 ≈ 该像素场景命中点到相机光轴的米制距离（与 `/get2DAABB` 的 `aabb_world` 中心距离同数量级、误差 <5%）；无浏览器连接时 `503`，断开后 15 s 内未响应 `504`。

12.6 `GET /get2DAABB?id=3&camera_id=2` → `box` 为像素矩形、`x_min<x_max`、原点左上；把物体移到画面边缘 → `clipped=true` 且框被图像边界截断；移到相机身后 → `visible:false, box:null` + `HTTP 200`。

12.7 资产面板（**Q93** 形态，§4.6）：三组列表分别来自 `GET {OLS}/list?type=scene|model|robot`，item 带 `category`（§6.11）；`GET {OLS}/listCategory?type=<kind>` 决定**第二级竖向侧栏**顺序（`未分类` 排最后）；面板不再有折叠树、名称过滤输入框与刷新按钮。每一行一个条目（缩略图 + 名称 + `id · category`），缩略图走 `GET {OLS}/thumbnail?type=<kind>&id=<id>`，403/404 时降级为占位图且不影响该行可点。点击某场景 → 原场景被清空并加载新场景，`scene.applied` 广播到其他标签页；预制模型/机械臂点击 → 物体加到原点并按 `type` 走 `/file?type=model|robot` 渲染真实模型。机械臂引用形式 `$LIBRARY_SERVER/robot?id=<n>` 由面板写入，世界文件自带同样写法也照常渲染，**裸数字在没有 `MetadataRef.type` 时仍是 `model` id**（§6.1）。OLS 不可用时面板出告警、当前世界不受影响；OLS 恢复后**重新载入页面**即读到三组列表（会话缓存修复（坑 23）仍在 `library.ts` 里生效）。

12.8 UI：左栏只剩 层级 / 资产 / Details(只读)；Details 仍可看到该相机实时 RGB 与深度预览；视口可点选、可轨道相机；**平移/旋转/缩放工具已删除**（§4.8.3 作废，判据见 §12.8b ① 类）；顶部无 undo/redo、无 simulation 按钮、无保存点；工具栏 `[data-tool]` 只剩 `select`。

12.8b 入口三分类（§4.8 的 **Q114**；取代旧"四类本地编辑全部不回传"口径——**不回传**这个说法整体作废：② 类改动**必须**上报，只有 ③ 类一步协议都不走）：

**现场** = OLS 场景 1 `docs/ur5e_table_empty.json`（与 §12.14 同一份，8 个物体），三分类各一段，② 与 ③ 的判据**成对**：一条必须动摘要、一条必须不动。

**① 类：入口在页面上不存在**（是删干净，不是禁用）——`editor/src/main.ts` 工具栏里 `[data-tool]` 的取值集合只剩 `select`，查不到 `translate`/`rotate`/`scale`（DOM 值即这三个字面量）；选中物体只有描边（`Box3Helper`，Q105）没有坐标轴，按住拖动只转轨道相机、任何 `Transform` 一字不动；Details 的 Robot Driver 区块是纯只读读数（`initial` 打底、`now` 覆盖的合并位姿 + `(now)` 标记），整页 `[data-joint]` 节点数为 **0**；常量 `allow_details_joint_angle_edit` 在 `GET /api/runtimeConfig` 响应里**不存在**（不是 `false`，是键整体没了）。**负控制**：用 `page.route('**/api/runtimeConfig')` 硬顶回一个多余的 `allow_details_joint_angle_edit: true` ⇒ 仍不渲染任何输入框——证明前端确实不再读它，而不是回退到默认分支。

**② 类：阶段闸门，`setup` 可用、`agent_owned` 置灰 + 点击弹窗**——
- `setup` 期：资产面板点一个 model ⇒ 物体进层级树、**`scene_sub_version` +1**、浏览器发出**恰好一条** `browser.state_sync`、**`scene_version` 一字不动**，gateway 侧摘要跟着变（正控制）。读数成对：`X-Scene-Sub-Version` 头与 `/getWorld` 正文里那个键必须同源（不同源则该次读取整体作废，§5.1）；`#status-revision` 在子版本非 0 时追加 `.{sub}`（§4.1）。
- `agent_owned` 期：同一入口点下去 ⇒ 文件选择框不弹、层级树行数不变、子版本不变、**零** `state_sync` 帧；`#phase-block` 覆盖层出现、正文第一行是 `local_edits.ts:PHASE_BLOCK_MESSAGE` 的"任务进行中，场景暂时不能改。"，第二行按被拦下的那次点击问要做什么（`phaseOfferMessage`，形如"新建会话并重新加载预设场景「名字」？"，是**问句**不是句号），**两枚按钮排在正文下方一行**（**Q122** 单列）：`data-action="dismiss-phase-block"`（`取消`）收起、`data-action="phase-new-session"`（`新建会话`）当场 `POST /sessions` 并在回到 `setup` 后重做那一步（**Q122**：问句只留"要做什么"，解释性长句全部删除）。**点击必须有响应（R2）反过来当判据**：闸门生效方式**不得是 `disabled` 属性**——入口一旦挂 `disabled`，点击委托第一行 `if (target.disabled) return;` 会吞掉点击、覆盖层不出现，本条即红；禁用态只能是视觉类 + `aria-disabled`（"新建会话"按钮同一规则）。
- `Load Scene`（本地文件）与资产面板场景预设走同一道闸门（拒绝码 `scene_switch_forbidden_after_first_task`）。
- **解除只有一条路**：`POST /sessions` 显式新建/切换会话回 `setup`；刷新页面不解禁，Gateway 用持久 `browserID` 找回阶段后 ② 类入口仍是禁用态（§4、§5.8）。

**③ 类：任何阶段可用（持锁期除外），但绝不出浏览器**——
- 切灯光预设 ⇒ `#viewport` 截图**必须不同**，而 `/getWorld` 正文、`X-Scene-Digest`、`scene_version`、`scene_sub_version`、层级树行数、`#panel-count` **全部一字不动**，且一帧 `state_sync` 都不发（负控制，与 ② 的正控制必须成对绿）。下拉里"跟随场景"读作 `null` = 覆盖关闭，`applyWorld()` 换现场时复位。
- 其余 ③ 类入口：Auxiliary layers（Q105/Q106 两个 flag）、`Load Traj` 与播放（**Q33**，播放态不写文档、**也不推进子版本**）、轨迹路点与段末幽灵（Q96–Q102）、视口截图（Q108）、轨道查看与缩放、选中与 Details 只读读数、相机预览、导出下载。
- **导出**（§4.8.2，**R4**）：文件名 `{scene_name}_v{scene_version}.json`，**不含子版本**；内容与 `/getWorld` 逐字节一致，`scene_sub_version > 0` 时正文**写出该键**、为 `0` 时**省略该键**（§3.1）；点导出不发任何 HTTP/WS 请求。

**持锁期例外**：`acquireSceneLease` 成功 ⇒ 灯光预设下拉复位为「跟随场景」并冻结，`releaseSceneLease` 解冻——锁管三件事：不许播放、不许换现场、不许换灯光。**租约不是阶段**：租约期必然落在 `agent_owned` 内，② 类闸门由阶段决定、不由租约决定，**不得实现成"释放租约后 ② 类入口解禁"**。

单测落点：`world.test.ts` 钉 `addLocalObject`/`removeLocalObject` 推进子版本而 `bumpVersion()` 归零、`toJSON()` 的键写出/省略两态；`local_edits.test.ts` 钉 `phaseBlockMessage()` 两阶段返回值；灯光"不进文档"由 `renderer.test.ts` 覆盖态不建文档灯 + `world.test.ts` `toJSON()` 不含预设灯共同钉。

12.9 命令与测试：`python -X utf8 -m unittest discover -s tests`、`npm test`、`npm run build` 全绿；改写后的 `tools/browser_smoke.cjs` 跑通 12.1/12.4/12.5/12.6；`grep` 确认 §10.1 路由已从代码中消失。

12.10 Agent 任务链路（§5.15；**Q115**/**Q116** 现行口径，旧"`status` 为 `created`／`success`"与"五态依次出现"断言已作废）：以一次性栈跑通（假 Agent 服务 8500 + gateway + 浏览器；`state_dir` **必须注入临时目录**，§9.2，绝不写真实 `var/`）。面板发一句命令后：
- ① gateway `POST /api/agent/jobs` 返回的 `id` 等于 Brain 的 `taskID`，**`terminal` 为 `false`**，`agent_state` 是上游 `state` 的**原文**。
- ② 载荷里的 `scene` 与浏览器 `World.toJSON()` 同形，且 `/startTask` 的 `sceneDigest`／`scene_id`／`scene_version`／`scene_sub_version` 与提交前那一次 `getWorld` **同源**（§5.15 第 2 步，用缓存顶替会在这里露馅）。
- ③ 面板不发一条 `/api/agent/jobs/{id}` 请求（轮询在 gateway），状态文字只随 `/events` 的 `agent.job_updated` 变化。
- ④ **词表判据（R10，本条核心）**：给替身喂一个我方从没见过的字符串 `"规划中"` ⇒ 面板**原样显示这四个字**、继续轮询、**不判终态**；替身改 `terminal:true` ⇒ **立刻**停轮询并出 `outcome` 区块；替身省略 `terminal` ⇒ 按非终态处理并在 gateway 留一条 warn。全程 `local_gateway.py`／`logic.ts`／`main.ts` 里**不存在状态名常量**（`grep` 五个中文态与 `AGENT_STATES` 必须为空，`tests/` 替身除外）。
- ⑤ 状态行 CSS 类由 `terminal`＋`outcome` 给出 `is-pending`／`is-done`／`is-failed`；`failed` 时气泡正文是 Brain `/queryTaskFailureReason` 返回那句原因**原文**；任何终态都不得出现"执行成功"字样（§5.15 第 8 步五态显示表）。
- ⑥ 关掉 Brain 再提交 → `502 UPSTREAM_ERROR`、面板给出提示且 `jobs` 列表不增长；`agent_service_url` 空串 → `501 AGENT_UNAVAILABLE`；**任务正在跑时把 Brain 停掉** ⇒ 服务状态条转"不可达"，而该 job 的文案与颜色**一律不变**，只有"连续 5 次轮询失败"上限才允许它落 `failed`（R9 硬边界，§12.18）。
- ⑦ Brain 在「进行中」期间反过来调 `/updateItem`，绑定浏览器按 §5.4/§7.3 照常应用写入并退出播放态。
- 单测：`tests/test_gateway_agent.py`（假 Agent 服务直接驱动轮询函数；`terminal`／`agent_state` 原文断言 + "未见过的状态 + `terminal:false` ⇒ 继续轮询" + "`terminal` 缺失 ⇒ 非终态 + warn"）与 `editor/src/result_view.test.ts`（"原样回显词表外新词"与终态判据；`agentStatusLabel` 已随回显层删除，`logic.test.ts` 相关 describe 不存在）。

12.11 发布包（§11.6/§11.7）：`python pack.py 1.0.0-rel` 在 `distribute/1.0.0-rel/` 下产出 `WebEditor-1.0.0-rel/` 与同名 zip；解包后 `python local_gateway.py --port <p>` 起来，浏览器**直接开 gateway 端口**即得完整编辑器——`GET /` 回 SPA、`/assets/*` 回真实 JS/CSS、`GET /api/runtimeConfig` 与 `WS /events` 同源可用、状态栏出现 `Connected`，全程不需要 Node。非法版本号（`1.0`、`v1.0.0`、`1.0.0/`）退出码非 0 且不建任何目录；`/../config/constant_config.json` 这类路径**不得**从静态路由读出内容（§11.7 ①）。

12.12 段末幽灵（§7.3，**Q96**/**Q97**/**Q98**）：**现场 A**（单台 `RobotDriver`，验"段末"与"不对外"两族判据）= 与 clip 同一 id 空间的世界（`tools/browser_smoke.cjs:clipWorld()`：`1=机械臂 / 2=苹果 / 3=盒子 / 4=传感器 / 6=探针`）+ `docs/robot_grasp.traj`（机械臂 9 条段表条目并成 3 段：120–339、340–529、530–689；`total_frames=720`）。**配对必须是那一份**：`tests/fixtures/scene_example.json` 与 clip 的 id 空间是"数字相同、语义不同"（**Q62**），拿它当幽灵现场会去驱动那台 rgbd 相机。**现场 B**（两台 `RobotDriver`，验 Q98 集合与跨幽灵挂载）= OLS 场景 3 `ur5e_table_sorting` + `examples/scene3_9_segments.traj`。九条判据（①~⑦ 现场 A，⑧ 现场 B，⑨ 任一场）：
- ① 播放头落在**第一段内**（如 200 帧，含 190–219 空隙）时，除真机臂外多出一个**半透明**手臂实例（浓淡由 `ghost_pose_opacity` 定，默认 50%），姿态等于 **339 帧**的采样（播放头拉到 339 时真机与幽灵重合）。
- ② 落在**第二段内**（如 400 帧）时幽灵换成 529 帧姿态；场地上仍是**每台 `RobotDriver` 各一个**幽灵，不是三段各留一个。
- ③ 落在**首段之前**（0–119，含刚载入的 0 帧）没有幽灵。
- ④ 落在**末段之后**（690–719）幽灵仍是 689 帧那一台，与 §8.5 真机"保持最后一次采样"一致。
- ⑤ 幽灵不参与拾取（点击幽灵所在像素选中的是背后物体或空处）；`/captureScene` 截图里看不见它；层级树与 `#panel-count` 不因它变化；Details 选中真机臂仍只有那一台。
- ⑥ `playback.ghostPose:false`（改 `features.json` 并重启，或拦截 `/api/runtimeConfig`——对前端等价，**坑 22**）⇒ 幽灵不再出现，其余播放条控件与 5 根刻度不受影响。
- ⑦ 清除轨迹（Q76 任意写入或重新 Load Traj 前的清空）后场景里查不到任何 `userData.ghost` 节点。
- ⑧（**Q98**，现场 B：`2=UR5eArm` 九段、`3=Robotiq2F85Gripper` 两条开合段）臂与夹爪**各画一台**幽灵，夹爪那台挂在**幽灵臂**的 `wrist_3_link` 上（真机的腕上不得多出第二只夹爪——两台幽灵必须是一体的姿势），两者共用**同一个**全局段末帧。
- ⑨（**Q97**）`ghost_pose_opacity` 改 `0.9` 与 `0.05` 后画面浓淡跟着变；改 `0` 时与"flag 关掉"**逐字节相同**；字段缺失、非数、越界一律按 `0.5`（判据在 `logic.ts:ghostOpacity()`）。
- 单测：`traj.test.ts` 的"当前段"解析（段内/空隙/首段前/末段后）、`logic.test.ts` 的 `ghostOpacity()`、`renderer.test.ts`/`drivers.test.ts` 的幽灵实例、透明度、多台与跨幽灵挂载与驱动。浏览器侧：**同一帧**开/关两侧 `/captureScene` 的 rgb 与 depth **逐字节相同**（§4.4 编辑层判据），`#viewport` 截图**必须不同**，`/getWorld` 的 id 列表不变（幽灵不是一个物体）。

12.13 轨迹路点（§7.3 的 Q101/Q102 + §7.1 勾选框）：**现场两个世界**——(a) §12.4 那份 `clipWorld()` fixture（`1=Arm` 是唯一 `RobotDriver`）配 `docs/robot_grasp.traj`（720 帧）；(b) OLS 场景 3 `ur5e_table_sorting`（`2=UR5eArm` 为父、`3=Robotiq2F85Gripper` 经 `RobotAttachment` 挂 `wrist_3_link`，两台都带 `RobotDriver`）配 `examples/scene3_9_segments.traj`（840 帧，携带 `2=臂 / 3=夹爪 / 7=苹果`，Attach 表 630 帧把苹果挂上夹爪、750 帧放回世界根）。**场景 3 里不混用 `docs/robot_grasp.traj`**：它的 Attach 表是按 §12.4 的 id 空间写的，搬到场景 3 会**成环**（§8.6 父链环检测），所以 ④ 的批次是同一条 clip 推两次（`?count=2`），两条各 85 球、颜色不同。候选集合按 §7.3 第 1 条读。八条判据：
- ① clip 在场即出现小球（**进入播放模式**就是触发条件；"即"读作**最终一定出现**、不读作同一 tick——臂的 URDF 晚于 clip 落地时小球随 §7.3 第 5 条②的自愈一起铺上，取证读**稳定后**那行诊断）。
- ② **只有叶子臂有球**：场景 3 装一条 clip ⇒ 只有一层球、`objectId=3`（夹爪），臂那层不存在；把 `3` 的 `RobotAttachment` **连同 `Transform.parent`（文件里就是 2）一起**去掉 ⇒ 两台驱动器物体互不相连，变成**两层**各自一串（只删 `RobotAttachment` 不够，`leafArmTargets()` 会退回 `Transform.parent`）。判据是层的数量与所属 objectId，不是像素。
- ③ **密度与大小来自 §9.2**：默认 `traj_waypoint_stride_frames=10` 下 840 帧那条 **85** 球、720 帧那条 **73** 球；计数是 `floor((total-1)/N) + 1`，**末帧不在等差数列上时再 +1**（`total % N == 1` 才正好落在末帧、不加；`logic.ts:waypointFrames()`）。把 stride 顶成 `20`、radius 顶成 `0.05`（`page.route`，**坑 22/68**：只从页面侧证据读——诊断行里的 `count` 与 `radius`）⇒ 场景 3 那条变 **43** 球、诊断行 `radius` 变 `0.05`（浏览器没有表面能读实例几何的包围球，半径看诊断行；几何重建由 `renderer.test.ts` 的 dispose 计数钉）。
- ④ **勾选框两态**：未勾选 ⇒ 只有活动那条那层 `visible=true`；勾选 ⇒ 两层都在且**颜色不同**（调色板按轨迹下标取，同层同色）；来回切两次不发一个 HTTP/WS 请求（`framesent` 增量 0），`#status-revision` 的 `scene_version`、层级树行数、`#panel-count` 一字不动。
- ⑤ **编辑层**：同一帧开/关 `playback.trajWaypoints` 两侧 `/captureScene` 的 rgb 与 depth **逐字节相同**（两遍必须先过**同一道与开关无关的舞台闸门**：父/子物体的 `aabb_world` 不再相同、且连续两次读数一致——URDF 未展开时两者量出同一个盒子，拿"等到了小球"当闸门会让未勾选那路永远等不到，**坑 67**）；比对连**四件物体的 `aabb_world`** 一起比（球不撑宽世界盒，臂必须停在播放帧而不是声明位）；`#viewport` 截图**必须不同**；`/getWorld` id 列表不变；**同一批像素点击**两遍选中同一个东西（小球对拾取射线不可见；判据是"点同一像素得同一结果"，不是"点在某颗球上"）；`/get2DAABB` 的 `aabb_world` 不因它变宽。
- ⑥ **路点跟着世界走**，可执行判据是**先搬后装**：场上没有 clip 时用 `/updateItem` 把机械臂基座平移距离 `d`，再载入 clip ⇒ 每个采样帧号上球的世界坐标都比"未搬时那次载入"的同一帧号多出同一个 `d`（比 `console.debug("Trajectory waypoints …")` 行里的层与点，不是像素），且装 clip 本身不动 `scene_version`。clip 在场时页面上**根本没有位姿入口**（① 类已删，§4.8.3 作废、`poseEditBlock()` 不存在）——"路点在场与世界被搬动不能同时成立"由**入口不存在**保证。
- ⑦ **取点确实是 URDF 最深叶 link 的均值**（Q102）：场景 3 装一条 clip，拿夹爪那层在帧号 700（苹果被挂在夹爪上，630–749 段内）的点 `P_pad`；把 `3` 的 **`RobotDriver` 组件**摘掉（挂载关系留着）重扫 ⇒ 只剩臂那层，同一帧号上是臂自己的最深叶 link（robot 1 的 `attachment_site`，腕法兰）`P_flange`；同帧用 `/get2DAABB?id=7` 读苹果世界包围盒中心 `P_apple`。判据：`|P_pad − P_apple|` 明显小于 `|P_flange − P_apple|`（三个数写进报告），即球落在**夹取点**一侧而不是**挂点**上。"两指垫均值落在对称面上"由 `urdf.test.ts`（真实夹爪最深叶恰为 `robotiq_2f85_right/left_silicone_pad`）与 `renderer.test.ts` 钉住。
- ⑧ 清除轨迹后诊断行 `layers` 变空（查不到任何 `userData.waypoint` 节点）；`playback.trajWaypoints:false` 时勾选框与球同时不见、其余播放条控件与刻度不受影响。
- 单测：`urdf.test.ts`（最深叶 link 集合：单叶臂/对称双叶夹爪/全 fixed 链/名字重复的 link）、`logic.test.ts`（`waypointSettings()` 回落、`waypointColor()` 循环、`waypointFrames()` 计数与末帧必取/整除不重复、叶子臂判定含 A←B←C 长链）、`renderer.test.ts`（建层、换半径重建并 dispose 旧几何、两态可见性、`isEditorOnly` 与 `hideOverlays`）。
- **数层数与球数靠一行诊断**：`main.ts:logWaypointLayers()` 每次画层后 `console.debug("Trajectory waypoints " + JSON.stringify({active, showAll, layers}))`，`layers` 是 `renderer.waypointLayers()` 回读的 `{key,trajectoryIndex,objectId,count,radius,color,visible,points}` 数组（`points` 是各球在**世界文件坐标系**里的 `[x,y,z]`，从实例矩阵反读；⑥/⑦ 比的就是它）；smoke 侧要在 `page.on("console")` 里**额外收 `debug` 级别**。

12.14 相机视锥与灯光占位球的显隐 flag、相机包围盒只用自身 Mesh（§4.4/§4.5 的 **Q105**/**Q106**）：**现场 = OLS 场景 1** `docs/ur5e_table_empty.json`——唯一一份两族都齐且相机自带网格的现场：`0=AmbientLight`、`7=TopDirectionalLight` 只有 `Light` 组件（无网格），`4=WristCameraD435i`、`5=OverheadCameraD435i` 各带 `MetadataRef + MeshRenderer + Camera`。§12.4 那份 `clipWorld()` 测不了"纯辅助图层物体没有选中框"一支（`4=Sensor` 只有 `Camera`、`far=50`，它的 `/get2DAABB` 是 **400 `EMPTY_AABB`**）。七条判据：
- ① **默认态**（两键都 `false`、无选中）：两台相机的视锥线与两盏灯的占位球都不在画面上。读法：`renderer.ts:applyOverlayVisibility()` 每次显隐算完后写一行 `console.debug("Viewport markers " + JSON.stringify({cameraFrustumAlways, lightMarkerAlways, selected, markers}))`，`markers` 是 `renderer.markerStates()` 从 scene graph 回读的 `{objectId,kind,visible}` 数组（`kind` 取 `cameraFrustum`/`lightSphere`/`lightLabel`，**关着的也列出来**）——"都不在画面上"读作这一行里 6 项 `visible` 全为 `false`，不是读截图。
- ② **选中才出现**：点层级树 `4` ⇒ 只有那台相机的视锥线出现、`5` 的不动也不见、灯球仍不见；改点 `0` ⇒ 那盏灯的占位球与类型标签出现、视锥线收回；再点 `1`（桌面）⇒ 两族一起消失。任何时刻至多"每台被选中的相机一条视锥 + 每盏被选中的灯一颗球"——可见性跟着**同一个选中态**（`renderer.ts:select()` 末尾一次遍历，不是每族各一个钩子）。
- ③ **两个键各自为政**：`page.route` 顶回 `viewport.cameraFrustumAlways:true`（**坑 22/68**）⇒ 无选中时两台相机视锥线都在场、选中桌面也不消失；灯球同理；**四种组合各测一遍**，一个键管不着另一族图层。
- ④ **相机的盒不含视锥**：选中 `4` 时描边量的必须是 D435i 那台**网格**的盒——数值判据拿 `/get2DAABB?id=4&camera_id=5` 回读的 `aabb_world`（与描边同由 `worldAabb()` 提供），边长应是**厘米级**，而其视锥按文件 `far` 会伸到**几十米**外；点 `0`（只有 `Light`、无网格）⇒ **没有描边**（`worldAabb()` 回 `null`），选中反馈就是那颗占位球本身（§4.5 那句"推论"的现场）。
- ⑤ **编辑层**：两键都顶 `true` 与都 `false` 两侧、**同一帧** `/captureScene?id=5` 的 rgb 与 depth **逐字节相同**，而 `#viewport` 截图**必须不同**；`/getWorld` id 列表与 `scene_version` 不变、层级树行数与 `#panel-count` 一字不动。
- ⑥ **看不见的辅助图层不能当点击目标**：`#viewport` 上 3×4 个网格像素各点一遍、读 `#selection-label` 尾部 `#<id>`。两盏灯没有网格，占位球是它们在 3D 里唯一可点身体——判据是"两键都 `false` 那一遍里**任何一次点击都不许答出 `#0` 或 `#7`**"（three 的 `Raycaster` **不看 `object.visible`**、只查 `layers`，不是"隐藏了自然点不到"）。两键都顶 `true` 再点同一批像素：除答出 `#0`/`#7` 的点（球亮着时点中那盏灯，Q71 既有可达性）外，**其余每一格与关断那遍逐格相同**（`spheresHit` 记进报告，为 0 只说明本版相机姿态下没蒙到球）。"看得见时点得着"由 `renderer.test.ts` 的 `selectable()` 钉。
- ⑦ **不牵连别的控件**：两键任意组合下 orbit、拾取、focus/frame 照常（`renderer.setInteractions()` 通道不动），关掉一整族图层不会让那族物体的 Details 读数或相机预览变空。
- 单测由 `renderer.test.ts` 钉住：两态可见性、`select()` 后只有被选物体的 marker 亮、两键各自为政、世界重建后 marker 直接建成当前态、`markerStates()` 列出全部六项、选中框读 `worldAabb()` 而非 `Box3.expandByObject` 且尺寸是 Mesh 的分米级不是 `far` 的几十米、纯编辑层物体没有选中框、移动 Mesh 后描边跟着重量、隐藏 marker 与亮着的视锥线都不进 `pick()`、`Box3Helper` 的 dispose 路径。

12.15 `RobotDriver.role`：普通 URDF 模型被轨迹驱动，但不被当成机械臂（§4.7/§7.3/§8.3 的 **Q107**）：**现场 = 本地场景** `docs/ur5e_passive_rig.json`（OLS 场景 1 的 `2=UR5eArm`（`role` 缺省）+ 一台挂腕部的 **`type:"model"` URDF 家具**（OLS model 30 微波炉，`microwave.urdf`、一条 `door_joint` 是 `revolute`），带 `RobotDriver{role:"passive", joint_sequence:["door_joint"]}` 与 `RobotAttachment{parent_object_id:2, parent_link:"wrist_3_link"}`；配套 clip `examples/passive_rig.traj` 由 `examples/make_passive_rig_traj.py` 产出，**同一条 clip 同时携带臂的六关节与那台模型的 `door_joint`**）。六条判据：
- ① **驱动一字不改**（§8.3 对 passive 仍成立）：把 passive 摆到两个不同播放帧（门开 0° 与 1.2 rad），`/get2DAABB?id=<passive>&camera_id=<over>` 的 `aabb_world` **必须随之改变**；`/updateItem` 写 `now_joint_positions_rad` 后照样刷新（**Q75**）、播放中收到写入照样退播（**Q76**）、Details 照样逐关节**只读显示**合并位姿（关节一律不给编辑，passive 与臂同权）。
- ② **不画轨迹路点**：`Trajectory waypoints` 诊断的 `layers` 里**只有臂那一条**（`objectId` 集合恰为叶子臂，不含 passive）。**负控制**：同一条 clip 换一份只删 `role` 字段的场景再载入 ⇒ 那一层**立刻从臂换到它**（不是"多出一层"：passive 一旦被当候选就是链上最深叶子，`leafArmIds()` 把臂降级成非叶子，路点只归最深那台）。这条"换人"是"过滤真的在生效"的唯一证明——否则"没有层"与"根本没进候选"在浏览器里无法区分。
- ③ **不画段末幽灵**：`Ghosts` 诊断行（`renderer.ghostStates()` 回读当前 ghosts 的物体 id 集合，只在集合变化时写一行）里 passive **不出现**（幽灵集合不求精叶子，删 `role` 的负控制是字面上的**多出一台**：`2` → `2,4`）；同一份 clip 同一帧的 `#viewport` 截图两态必须**不同**，而 `/getWorld` id 列表与 `scene_version` 一字不动。
- ④ **混用两个方向都成立**：(a) 臂末端连 URDF 模型 ⇒ passive 挂 `wrist_3_link`、播放时**跟着手腕走**（腕部 link 世界位置与 passive 的 `aabb_world` 中心同步变化，两帧之间 `/get2DAABB?id=<passive>` 不同即可）；(b) 机械臂固定在 URDF 模型上 ⇒ 把臂的 `RobotAttachment.parent_object_id` 指向 passive，臂挂进它的 link、照样被自己的六关节驱动（`parent_link` 解析只看 `robotTrees`，**不看父级有没有 driver、更不看 `role`**，`renderer.ts:worldParentOf()` 一字不改）。
- ⑤ **对外通道零变化**：`/getWorld` 与导出文件里 `role` **原样回读**（`rawComponents` 保留，§3.6）；层级树行数、`#panel-count`、拾取射线、`worldAabb()`（`/get2DAABB`、`frame()`、选中描边）**全部不因 `role` 改变**——passive 与 arm 的差别只有路点与幽灵两族装饰。
- ⑥ **缺省即今天**（向后兼容硬判据）：`role` 缺失、值为 `"arm"`、值为无法识别的字符串三种写法**行为完全相同**；OLS 7 份场景、`docs/ur5e_table_empty.json`、`tests/fixtures/scene_example.json` 与全部既有 clip 一字不改照常——由 §12.12（幽灵两台）与 §12.13（路点 `73/85/43`）两条既有判据照常绿承担，**不许**动那两处数值。
- 取证落点：`main.ts` 两处候选过滤（`leafArmTargets()`/`ghostTargets()`）与 `world.ts:armDriver()`；passive 判定只在**求叶子之前**发生。单测由 `world.test.ts` 钉（三种写法都算臂、`"passive"` 时 `armDriver()` 回 `undefined` 而 `robotDriver()` 照回属性对象、`role` 原样进 `rawComponents` 与 `toJSON()`）。

12.16 视口截图按钮（§4.4 末条 + §7.1 按钮的 **Q108**）：**现场 = OLS 场景 1**，要的是**编辑层在屏幕上大面积在场**：选中一台相机（描边 + 视锥线）并把 `viewport.cameraFrustumAlways` 顶成 `true`。辅助图层在此第一次有**像素出口**，判据是像素级的：剪贴板 PNG 用 `navigator.clipboard.read()` 读回、`createImageBitmap()` 解码、画进 2D 画布 `getImageData()` 数颜色，与同帧 `#viewport` 截图比。七条判据：
- ① **按钮在场、一点即得**：点 `[data-action="snapshot"]` ⇒ 剪贴板有一种 `image/png`，宽高**等于 `#viewport` 画布的 drawing buffer 尺寸**（`canvas.width/height`，不是 CSS 尺寸；截图按位图分辨率读），toast 报同样的 `宽×高`。Chromium 要 `context.grantPermissions(['clipboard-read','clipboard-write'])`（只给 write 时 `clipboard.read()` 回 `NotAllowedError`——测试夹具权限，不是产品行为）。
- ② **屏幕上有的编辑层不进截图（核心）**：同一帧两张图各数一次"描边黄 `0xfacc15`"与"视锥蓝 `0x38bdf8`"的近似像素（每通道 ±12）：选中 `4` 且 `cameraFrustumAlways:true` 时 `#viewport` 那张两族都 `> 0`、**剪贴板那张必须恰为 `0`**（`hideOverlays()` 挡的是这两族加路点/幽灵/网格/坐标轴）。**必须配下界**（否则整张截黑也能过）：下界**不拿"亮像素"跟屏幕比**（屏幕亮像素大头正是要排除的网格/坐标轴/视锥，拿 `lit` 做同数量级断言等于把功能判成 bug），改数**内容参照族**——桌面木质"棕色"像素（`r>40 && r<200 && g>15 && 15<=r-g<=90 && b<g`，没有任何编辑层画这个颜色族）：剪贴板那张 `brown > 0` 且不低于屏幕那张的一半；同时保留 `lit > 0` 兜纯色异常。这条下界**同时是 `SRGBColorSpace` 的回归探针**：colorSpace 改成线性 ⇒ `brown` 归 `0`、最亮通道从 `146` 掉到 `74`（离屏目标没做输出编码的真 bug 由它抓到，§4.4、坑 82）。
- ③ **两个常驻 flag 对截图无效果**：`cameraFrustumAlways` × `lightMarkerAlways` 四种组合、同一相机姿态同一选中态 ⇒ 四张 PNG **逐字节相同**，而 `#viewport` 截图四组合**互不相同**（证明 flag 真的在画）。
- ④ **零副作用**：点击前后 `/getWorld` id 列表、`scene_version`、层级树行数、`#panel-count`、`#timeline-frame`、`#selection-label` 一字不动；非 GET 与 `browser.state_sync` 帧增量 **0 / 0**（截图不是接口调用）。
- ⑤ **截的是视口相机、不污染 sensor 通路**：`frame()` 挪一次 orbit 相机再截 ⇒ 新 PNG 与上一张**不同**；同一段里前后两次 `/captureScene?id=<某相机>` 的 rgb **逐字节相同**（`captureViewport()` 若把 `setRenderTarget`/编辑层可见性漏在 `finally` 外，这条先红）。
- ⑥ **键关掉 = 按钮不见**：`viewport.snapshot:false`（`page.route` 顶回，**坑 22/68**）⇒ `[data-action="snapshot"]` 的 `hidden === true`。**"不见"的实现就是全应用那一条关断机制**（`applyFeatureVisibility()` 设 `hidden`，**元素不离开 DOM**），判据只读这一条属性、**不读 `.count()`**；工具栏其余控件 `hidden === false`、照常工作。
- ⑦ **不做的事**：不落盘、不下载、剪贴板里不放非 PNG、不改 §4.8.2 导出行为、不加"包含辅助图层"第二模式；剪贴板不可用时**不做**"下载一份"降级，只报错。
- 单测由 `renderer.test.ts` 钉 `captureViewport()`：渲染那一刻描边/幽灵/路点/网格/坐标轴/亮着的 marker 全被按下并在 `finally` 里逐个恢复（含 `render` 抛错那一路）、目标尺寸取 drawing buffer、翻行方向与 §5.10 一致（GPU 第 0 行是画面底行）、`getRenderTarget()` 旧值被还原、尺寸为 0 时直接回 `false` 且不动 scene graph。

12.17 brain-web/2 联合验收（流程文档 §1/§3/§4/§6/§8，**Q109**/**Q111**/**Q112**）：判据分两半——我方独立可测的（① 规范形、③④⑤⑥ 我方一侧）用替身与单测；必须双方在场的（① 跨实现相等、② 宽容解析回读）联调当天跑，**基准夹具由我方出**（`tests/fixtures/` 的非空真实场景 + 相等/不等各一组），对方按同一批对齐。
- ① **digest 同源**：同一份场景文档，浏览器 `World.toJSON()` 交给 Gateway 算出的 hex 必须与 Brain 的 JCS+SHA-256 **逐字符相同**（64 位小写 hex）。规范形由 `tests/test_digest.py` 钉住（Gateway 侧 Python `digest.py`，**R5**）：键按 UTF-16 码元升序、数组保序、`1` 与 `1.0` 同一规范形式、拒绝 NaN/Inf、**不做 Unicode 归一化**、**无容差**（摘要只有相等/不相等两态）。四组必测：全等 ⇒ 同 hex；仅 `now_joint_positions_rad` 不同 ⇒ 不同 hex；仅 `scene_sub_version` 不同 ⇒ 不同 hex；同一份重复算两次 ⇒ 同 hex。**输入是浏览器送来的那份对象**：Gateway 对 `scene` 只做 JSON 对象与尺寸校验（§5.15.1），摘要路径上**不得**补默认值、不得删未知字段、**不得在 `scene_sub_version` 缺失时补成 `0` 再算**。跨实现相等的那一半等对方夹具（ECMAScript 侧基准 hex 已合并：`JSON.parse` + 码元排序 + `String(number)` + SHA-256 的外部基准，不是第二处实现）。
- ② **四元组推进**：`setup` 期面板加物 ⇒ `scene_sub_version +1`、`scene_version` 不动、摘要变（§12.8b 正控制）；Agent 写回 ⇒ `scene_version` **恰好** `previous+1` 且子版本**归 0**；candidate 子版本非 0 ⇒ `400 gateway_scene_revision_invalid`；目标版本不是 `previous+1` ⇒ `400 gateway_target_version_invalid` 拒收（在**下发 candidate 之前**判，派发出去就可能落地），**不落半份现场**。`Load Scene` 取文件里写的值、不自增（§3.9）；文件不带 `scene_sub_version` 时读作 0 且**导出也不写该键**（§3.1、**R4**）。
- ③ **阶段与三条读数点同源**：第一次 `/startTask` 被受理那一刻，WS `session.phase_changed`、`GET /api/sessionState?browser_id=`、Brain `GET /getWorld` 的 `X-Session-Phase` 三处必须给同一阶段；不一致时以 `/api/sessionState` 为准并**重取现场**。`agent_owned` 期 Gateway 收到子版本推进的 `state_sync` ⇒ `409 manual_edit_forbidden`，且持久化现场**不被覆盖**——拒绝就是零落地。
- ④ **CAS 完整判据**：Brain 的 `expected{scene_id, scene_version, scene_sub_version, sceneDigest}` 四元组任一不符即拒；词典序不增而摘要变 ⇒ `browser_scene_version_conflict`（**唯一**例外是 `setup` 期子版本推进）；词典序不增且摘要也没变 ⇒ **幂等接受**，同一份文档重复上报不得报错；`transactionID` 重复 ⇒ 回**原回执**且不重复应用。
- ⑤ **申领 id**：Brain 增物前 `GET /allocateObjectID{browserID,count}` 拿连续号段、Gateway 原子推进持久水位；`/addObject` body 未带 id 时 Gateway **自己先领一个**再把确定 id 交给浏览器（R7）；采纳 `browser.state_sync` 时把水位抬到 `max(持久水位, 文档里所有 object_id + 1)`；上限 `2**32-1`，耗尽 ⇒ `409 object_id_space_exhausted`。可执行顺序：`setup` 期浏览器以 `X-Next-Object-ID` 为种子本地分配一串 id → 上报 → 重启 Gateway → Brain 申领一段 ⇒ 两段**不相交**（种子向上单调 + 采纳时抬水位，关闭"新增→删除→刷新复用旧 id"反例）。
- ⑥ **持久化与回执**：`var/gateway_state.json` 落盘后**在同一份路径上重开 `GatewayState`／`StandaloneGateway`**（这是恢复语义的读法，也是本判据用的读法），绑定 `browserID`、`session_phase`、id 高水位、租约表、事务回执索引**都在**；`GET /queryWorldCommit` 对已落盘事务回**原回执**、对未见过的 `transactionID` 回 `200 {"status":"unknown"}`（不是 404——"还没听说过"是结论不是错误，与 §5.13 的 `400 EMPTY_AABB`+`visible:false` 同一先例）；`POST /resolveUncertain` 的 `kept_locked` 同理回 200。**注意与 §9.2 第④条的分工**：那是**真实进程 `main()` 启动即清空上一轮**，所以经 CLI 重启一台 gateway 不再"全都在"——要跨进程重启带上一轮的绑定／租约，得显式 `--keep-state`；本判据靠直接重开状态层（不经 `main()`）证明**构造器只读不清**，两条不打架、各测各的面。一次性验证栈（8098）**必须注入临时目录**，跑完真实 `var/` 里不得出现该文件。**不扩样例脚本**（§5.9 末段仍成立）。

12.18 服务健康与运行状态词表（§5.15 的 **Q115**/**Q116**）：
- ① **`/health` 透传**：Brain 在场时 `GET /health`（Gateway 8099）把对方 JSON **原样**透出（不裁剪、不改写、不重排），超时上限 **3 秒**；对方进程停掉 ⇒ **502** `{"status":"unreachable","detail":"…"}`，**不得回 200**——"服务没起"与"服务起了但依赖坏了"必须是两个能分开的读数。`GET /api/agent/health` 是浏览器用的同一条透传，缓存上限 **5 秒**，命中缓存时响应头 `X-Agent-Health-Age` 给出秒数。
- ② **`/api/health` 三分项**（原有 `{status,mode,uptimeSeconds}`）：`agent_service:{status,detail}`（`status ∈ {ready,degraded,unreachable}`）、`browser:{connected,browser_id}`、`assets:{reachable}`；任一非 `ready`／非 `bound`／非 `ok` ⇒ `degraded`；三分项**不阻塞彼此**，任一分项超时单独记 `unreachable` 而整体仍回 200（这条路由的主语是"Gateway 自己活着"）；OLS 探活**复用** `GET {library_server_url}/list?type=model`，不新造探活路径。**核心判据（Q115 的存在理由）**：只停 Qwen、Brain 仍在 ⇒ 对方 `/health` **仍回 200** 且分项写 `qwen:"unavailable"`（或 `model_not_found`），我方 `/api/health` 报 **`degraded`** 而不是 `unreachable`。
- ③ **状态词表是透传**（Q116）：未见过的字符串（`"规划中"`）⇒ 面板**原样显示**、继续轮询、**不判终态**；`terminal:true` ⇒ **立刻**停轮询并出 `outcome` 区块；`terminal` 缺失或不是布尔 ⇒ 非终态 + gateway 留一条 warn。反面判据：`grep` 五个中文态字面量与 `AGENT_STATES` 在 `local_gateway.py`／`editor/src/logic.ts`／`editor/src/main.ts` 里**必须为空**（`tests/` 替身除外）；`types.ts` 的 `AgentJob` 里**不得有 `status` 键**（收尾仍 `cmp types.ts contracts/types.ts`，坑 6）。
- ④ **健康不进 job**（R9 硬边界）：任务在跑时停掉 Brain ⇒ 服务状态条转"不可达"，该 job 的 `agent_state` 文案与 CSS 类（`is-pending`）**一字不变**；只有"连续 5 次轮询失败"上限（≈25 秒）才允许落 `is-failed`。"一次探活失败就把在跑任务判成失败"是本条唯一要防的走样。
- ⑤ **浏览器侧请求面**：启动即一次 `GET /api/health`、此后每 **15 秒**一次（15 s 与 3 s／5 s 都是内置值，**不做成常量**，§9.2）；面板**一次都不发** `/api/agent/jobs/{id}`；浏览器 15 秒撞 Gateway 5 秒缓存 ⇒ 缓存必须命中（`X-Agent-Health-Age` 非 0），不得打穿对方。
- 单测：`tests/test_agent_health.py`（原样透传与 502 视图、三分项并发探且任一超时不改 job 状态、缓存龄）；`tests/test_gateway_agent.py` 按 `terminal`／`agent_state` 原文断言；回显断言在 `editor/src/result_view.test.ts`。真浏览器：服务状态条三档（就绪／降级／不可达；停 Brain 后状态条变红而 job 文案不变）与 `"规划中"` 原样显示。

12.19 阴影渲染（§4.8.5 的 **Q117**/**Q118**）：判据三层。
- ① **单测（无 GPU）**：`logic.test.ts` 钉两个纯算式——`shadowExtentsOf` 逐轴贴合 AABB 在该灯视空间里的投影、`margin` 只放大不外移、退化盒子仍产出一圈可渲染范围、恒有 `near < far` 且 `near >= 0.01`；`shadowRangeOf` 钉"灯在盒子内部/外部/紧贴"三种位姿的近远。`renderer.test.ts` 钉规则——`Directional`/`Spot`/`Point` 三族各建一盏并断言 `castShadow` 与贴图尺寸（前两族 `SHADOW_MAP_SIZE`、`Point` 用 `SHADOW_CUBE_MAP_SIZE`），`Ambient` 与 `intensity: 0` 永不投影，覆盖态下**文档灯不投、预设灯投**，资产几何每台实例同时 `castShadow` 与 `receiveShadow`，段末幽灵两个标志都 `false`，`setShadowsEnabled(false)` ⇒ `shadowMap.enabled` 为 `false`。`shadowsEnabled()` 的**四格表**（**Q118**）：flag 开×勾选框开/关、flag 关×常量 `true`/`false`，外加读不懂的常量值回 `true`。
- ② **负控制（核心）**：勾选框来回切两次后 `world.toJSON()` 与初始**全等**、`scene_version`/`scene_sub_version` 不动、`worldAabb()` 与 `/get2DAABB` 读数不变、`/captureScene` 深度数组长度与 `NaN` 分布不变（**阴影不进深度**）；一条 gateway 调用都不发。
- ③ **真浏览器**（OLS 场景 1）：载入后从渲染器诊断行读到"投影灯数 = 1（`TopDirectionalLight`）、投影几何数 > 0、接影几何数 > 0"，取消勾选 ⇒ 同一行 `enabled:false`。**开关三条来源各证一次**（Q118）：`viewport.shadows:true` ⇒ 勾选框在不在只随键、投不投影随勾选框；`viewport.shadows:false` ⇒ 勾选框消失、投不投影由 `shadows_enabled` 决定（`true`/`false` 各跑一遍，诊断行 `enabled` 跟着常量走，且页面里没有任何控件可点）。像素判据按 §12 通用口径（藏 `#toast`、推近相机、均值亮度 + 亮像素数）；勾选框**来回切两次**并断言"再开"与"初开"逐字相同——§4.8.5 可逆性的唯一现场证据。

12.20 物体包围盒线框（§4.8.6 的 **Q119**）：判据三层，重心在**"这一层是编辑层"能不能被证明**。
- ① **单测**：`logic.test.ts` 钉 `boundingBoxesEnabled()` 三格——只有"键开+勾上"回 `true`；键关时无论 `state.boxes` 存什么一律 `false`；**缺键按 `false`**（fail-closed，也钉住"没有常量兜底"）。`renderer.test.ts` 钉规则——默认关（`boxReport().enabled` 为 `false`、`scene` 里零个 `Box3Helper`，且"按状态问一次"会打出 `Viewport boxes {"enabled":false,"drawn":[],"skipped":[]}` 那一行——页面加载得有个回答）；打开后**一物一盒**且盒与 `worldAabb(objectId)` **逐字段相等**（命门：屏幕上的盒与 `/get2DAABB` 报的盒必须同源）；纯灯/纯相机进 `skipped` 不进 `drawn`（不画零号盒）；`drawn` 按 id 升序；`syncScene` 撤掉物体后盒一起消失；`setBoundingBoxesEnabled(false)` 清空整层并 `dispose()`，之后不留盒子。
- ② **负控制（核心，三层各证一次"不进采集、不进文档"）**：线框来回切两次后 `world.toJSON()` 全等、`scene_version`/`scene_sub_version` 不动；`worldAabb()`（整体）与 `frame()` 入参**不变**（线框是编辑层，不被自己撑大）；`shadowReport()` 的 `castMeshes`/`receiveMeshes` 计数不变（`LineSegments` 不是 `Mesh`）；`isEditorOnly()` 对每个盒回 `true`，`hideOverlays()` 把可见的盒全部按掉、`sensorPass`/`viewportPass` 复原后回到原状（§4.4 通路一个字没改）；一条 gateway 调用都不发。真机层把这条推到底：**同一次 `/captureScene` 在线框开/关两种页面状态下取回同一份字节**（§4.8.6 第 7 条"不冻结租约"的判据就是它，不是"看起来没变"）。
- ③ **真浏览器**（OLS 场景 1，一次性作用域探针）：`Viewport boxes` 行的 `drawn` 恰是**有几何的那几个 id**、`skipped` 恰是灯与相机，一个不多一个不少；勾选框**来回切两次**，`#viewport` 元素截图的均值亮度与亮像素数在"初开"与"再开"之间**逐字相同**、与"关"不同（测量口径同 §12.19）；`viewport.boundingBoxes:false` ⇒ 宿主 `hidden`、加载那一行是 `{"enabled":false,"drawn":[],"skipped":[]}`，**且 `evaluate` 直接点看不见的 input 也不画**（点击落在控件上、`checked` 回 `true`，但 `Viewport boxes` 一行都不新增、整帧亮度与"键开着但没勾上"态**逐字相同**——键关掉只剩"收走控件"，画不画仍由渲染层说）；持租约（`agent_owned` + `lease`）⇒ **勾选框仍可点、`enabled` 跟着点走**（与阴影的 `aria-disabled` 相对），同时 `/captureScene` 字节两态相同——冻结不适用但可重复性仍在；POST 与 `browser.state_sync` 增量**在点击当场算**为 **0 / 0**（**坑 81**），`scene_version`、层级树行数、`#panel-count` 一字不动。
- **三条测量口径**：① swiftshader 上一次 `rgbd` 采集实测 **13.6 s**，压在 `capture_timeout_seconds: 15` 线上，而本节判据要连着取好几次——所以逐字节比对那几趟把 `shadows_enabled` 路由成 `false`（与被测键无关，减的是阴影贴图开销），线框开着采集也在 13.6 s 回过 200（**坑 88**：先量一条"两次采集字节相等"的判据花多少秒，网关 15 s 预算不等人）；② `/get2DAABB` 响应剥去 `request_id` 再比（**坑 87**），`box` 与 `aabb_world` 两态逐字段相同；③ `addStyleTag` 属于文档，`page.goto` 之后就没了——**每一趟**重载都要在第一次读像素前重新藏 `#toast`。

12.21 Agent 对话气泡（§7.4 的 **Q120**）：判据重心在**"分边"被数字证明而不是被眼睛证明**。
- ① **单测**：不动任何 TS，全量 vitest **例数与基线逐位不变**就是判据（`result_view.test.ts` 早把 `is-pending`/`is-done`/`is-failed` 与行文本钉死；它若红了说明改动越界动了显示逻辑）。
- ② **真机 · 几何**（`page.route` 喂一份多条消息的会话 + 一次真实 `#agent-input` 提交）：`#agent-messages` 计算样式 `display:flex` + `flex-direction:column`；`.message.user` 的 `align-self` 为 `flex-end`、`.message.agent` 为 `flex-start`；两条气泡**中心 X 分居容器中线两侧**；宽度不到半栏的那一对还要用户**左缘**在 Agent **右缘**之右（把"靠右"落成不等式）——半栏限制是判据前提：两条都撑到 `max-width:86%` 时左右缘必然交叠，只有中心分边仍成立；两族 `background-color` 不同、圆角互为镜像（用户 `border-bottom-right-radius` 明显小于其余三角，Agent 反之），用户侧 `<header>` 的 `flex-direction` 是 `row-reverse`。
- ③ **真机 · 内容不被挤坏**：一条同时带技能序、"现场 #… · v… · 摘要 …"、下载 chip 与徽标 + 8 位任务号的 Agent 气泡，在最窄栏（`--agent-track` 下界 `280rem`）里**每一族都 `scrollWidth <= clientWidth`**、横向溢出为零；空态 `.agent-empty` 仍占满整栏并居中（`align-self` 计算值 `auto`、容器 `align-items` `normal`，合起来按 `stretch` 用——探针钉**宽度等于内容宽**而不是那个字）。
- ④ **协议面**：非 GET 与 `browser.state_sync` 增量 **0 / 0**，`scene_version`、层级树行数、`#panel-count` 一字不动；键面/CSV/探针不增不减。
- **测量口径**：(i) `page.route` 造会话不是偷懒——验证栈上没有 Agent 服务时 `POST /api/agent/jobs` 回 `501`，真实那路只有"一条用户命令 + 一条失败回执"，**装不满**气泡三族内容，所以两路都跑：真实路证"应用自己的写入路径画出来的也是气泡"，mock 路证"内容最满那条不溢出"。(ii) `#agent-messages` 是滚动容器，`getBoundingClientRect()` 对滚出视口的条目**照样给真 X**（几何判据与滚动无关），但**截图**那半必须先把 `scrollTop` 定到想要位置。

12.22 视口录制（§4.8.7 的 **Q121**）：判据重心在**"交出来的是不是一段真 MP4"与"跨模式那一段有没有断"**。
- ① **单测（无 GPU）**：`logic.test.ts` 钉 `pickRecordMime()`——只支持裸 `video/mp4` ⇒ 取它；High 与 Main 都在 ⇒ **High 在前**；全不接受 ⇒ `null`；候选表**内容**：没有 Baseline 两个字符串（裸 `video/mp4` 交回的就是 `Constrained Baseline`，写进去等于冗余；五个 H.264 字符串都能写出可解码 h264，不含 Baseline 是因为冗余不是因为它坏），也没有 `webm`（不降级是用户裁决）。`recordVideoName()` 的补零与"不含现场名"；`viewport.record` 缺键按 `false`。`renderer.test.ts` 钉规则——默认没装取帧点时渲染循环一次都不投递，但 `recordReport()` **被问就答**（`{"recording":false,…}` 那一行——沉默与"根本没接上"在取证里同形）；装上后按 `RECORD_INTERVAL_MS` 门控（喂 t=0/10/20/34/45 ⇒ 投 2 次且 `frames` 同步自增）；**一帧就是自己那一趟 pass**：`recordFrameTo(canvas)` 转调 `captureViewport(canvas, excludeOverlays)`，GPU 收到 `render(scene, camera)`、离屏画布收到 `putImageData`，且**排除态下屏幕上每一层始终 `visible`**（排除发生在离屏那趟，不在屏幕上；断言是"整段一次可见性写入都没有"）；勾上「录制辅助图层」时那趟里每层都在；`dispose()` 后 `recording` 为 `false` 且再无投递。
- ② **负控制**：录制整轮跑完 `world.toJSON()` 全等（两版本号不动）；`isEditorOnly()`/`hideOverlays()`/`expandGeometry()`/`pick()` 四条判据**一个字都没改**（录制不加图层，只是再画一遍）；同一次 `/captureScene` 在**空闲 / 开线框 / 录制进行中 / 关线框**四态回**同一份字节**；`/get2DAABB`（剥 `request_id`，**坑 87**）录制中与空闲**逐字段相同**；整轮非 GET 与 `browser.state_sync` 增量 **0 / 0**（当场算，**坑 81**），层级树行数、`#panel-count` 一字不动。
- ③ **真浏览器**（OLS 场景 1，一次性作用域探针，`--use-gl=angle --use-angle=d3d11`；`page.on("download")` 接文件后交 ffprobe/ffmpeg 与 PIL 判）：
  - **A｜常规模式 + 中途动相机**：录 2.5 s、中途拖轨道相机 ⇒ `viewport-YYYYMMDD-HHMMSS.mp4`、h264/**High**、`duration` **有限**、**逐帧 YMAX 有多种取值**（画面确实在动 = 相机被录进去）、抽帧亮像素可数。
  - **B｜跨模式一段到底（命门）**：录到一半 `Load Traj` 并起播、播放中再录一段才停 ⇒ **一个**文件覆盖两段模式、抽帧无黑帧、`Viewport record` 行的帧数/fps 与实际相符。
  - **C｜辅助图层两态**：默认态**带子里线框绿像素 0 个、同一时刻屏幕截图 > 0**（"屏幕上有、带子里没有"）；勾上后带子里有、文件显著变大；两态各自的 `excludesOverlays` 报的是真话（`true`/`false`）。
  - **D｜三格闸门与编解码**：**D1** `viewport.record:false` ⇒ 两个宿主 `hidden=true`，直接派发 click **不起机**（无 `recording:true` 行、无下载；点击在 `[data-feature]` 委托闸门就被丢掉，`toggleRecording()` 里的 `enabled()` 是第二道防线——D1 读的是"没有 `recording:true` 行、没有下载"两个可见事实，不是 toast，闸门在第一处所以话也不会说出来）；**D2** `addInitScript` 把 `isTypeSupported` 换成只认 `avc1.42001f` ⇒ 告警 `No MP4 encoder … does not fall back to WebM`、无 recorder、无文件；**D3** 只认裸 `video/mp4` ⇒ 照样落盘，容器是 MP4、编码是 **vp9**——**候选表保证的是容器不是编码**。
- **测量口径**：(i) **整帧平均亮度不是"黑帧判据"**（黑背景小桌子现场，正常带子 `scale=1:1` 平均只有 4.2/255）；诚实判据是 ffprobe 的 codec/profile/有限 `duration` + **逐帧 `signalstats` YMAX 有没有变化** + **全分辨率抽帧数绿像素与亮像素** + 文件字节 + 目视。(ii) `isTypeSupported` 不是证据；它真正会骗人的地方是"许的档位"与"给的档位"不是一回事（要 `avc1.42E01E` 之外的档、muxer 交回 `Constrained Baseline`）。(iii) 用 `captureStream(0)` + `requestFrame()`，理由是**让"一帧交付 = 文件一帧"一一对应**（采样钟归浏览器时 `recordReport().frames` 就不是文件帧数的真话）。(iv) `Load Traj` 与播放是合法本地动作、不产生非 GET，流量判据可整轮计数；一旦某步会发请求就必须分段。(v) 软件 GL 配置下带子会交付帧却**写回 0 字节**，取证一律带 `--use-gl=angle --use-angle=d3d11`；投不满 30 fps 是环境不是缺陷，报**实际交付帧数**（§4.8.7 第 9 条 ④）。(vi) C 必须排在 B **之前**——载入带子之后 `/captureScene` 会报 `camera_frame_scene_mismatch`（现场在 `paused` 而不是 `stopped`），字节稳定性那半就没法测。

12.23 UI 优化第三轮：撤下三处界面物 + 文案只说结果 + 新建会话不再报假失败（§7.5 与 §4.8 的 **Q124**/**Q125**/**Q126**）：三件事不同族，判据分开立，重心在 ③（唯一真 bug）。
- ① **结构判据（Q125，纯 DOM 计数）**：`.viewport-badge`、`.operations`、`.operations-content`、`#operations-content`、`#operation-count`、`#speech-status` 六处宿主**计数为 0**，`styles.css` 对应规则 0 条；`.agent-header` **只有一行**（`h2` 的 `top`/`bottom` 都落在 header 内框、header 里不再有第二个文本节点），标题文本恰为 `Agent` 且水平居中；空态恰含 **1 个图标** + `strong` = `Agent` + `p` = `向Agent发送命令`。**负控制两支**：`agent.messages` 有内容时空态不再出现（`agent.messages.emptyState` 门控不动）；`sendSitsRightOfForm` 仍成立——撤掉 `#speech-status` 后 mic/send 那组 `<p>` 靠 `.agent-panel form>div { justify-content: flex-end }` 贴右，不靠那个 `<span>` 撑位。**空态垂直居中不写成断言**（拿 `.agent-empty` 的框比 `#agent-messages` 的框恒不成立——内容区有 13 px 内边距；居中在这里没有数字口径，由截图目视确认——找不到诚实量测的判据就别立它）。
- ② **文案判据（Q124，读 `#toast`／`#agent-toast` 文本）**：六个动作各点一次、各读一句——截图 `截图已复制：<宽>×<高> 像素`（宽高与 `#viewport` drawing buffer 一致，判据钉"有 `截图已复制：` 加一组 `数字×数字 像素`"而不是整句）、光照 `光照已切换：Studio`、回空选项 `光照已跟随场景`、阴影 `投射阴影已开启`／`投射阴影已关闭`、线框 `包围盒已显示`／`包围盒已隐藏`（控制名以 **Q133** 现行为准，**判据的形状一字不动**：钉"点这一下出现这一族话"，不钉某批字符）。**另一半是否定式**：`仅渲染层`、`不进采集`、`不改现场`、`Scene Agent`、`ASSISTANT`、`Commands execute through` 六串在页面可见文本里命中 **0**；英文旧话术 `render layer`、`never the scene`、`not reported`、`never reported`、`browser local`、`editor layers` 六串在**所有 `title` 属性**里命中 0——自 **Q133** 起这是活判据：每一条新写的 `title` 都必须过这道闸。改的是话不是判据：§4.8 的"③ 类本地动作"分类与 §12.16/§12.19/§12.20 的负控制一字不改，机制从界面文案搬回文档正文。
- ③ **时序判据（Q126，唯一真 bug）**：点 `[data-action="new-conversation"]` ⇒ `#toast` **为空**、`#agent-toast` 给 `已新建对话并切换到空场景`，且这次点击的 `POST /sessions` 与 `POST /api/agent/conversations` 都成功。修复口径两条：**`refreshSessionState()` 的并发改成串行**（新读回链排在上一次之后；"答案不得覆盖更新的答案"的丢弃规则保留当保险，落地次序由调度决定）；**撤下"信念 id 必须等于这次 POST 返回的 id"判据**——对刚由这次 POST 建立的绑定，POST 才是权威，能取消这次迁移的只有**更新的用户选择**（`conversationSelectionGeneration`）。**请求数不增**（`readCurrentSessionState` 本来就先 `fetch` 再判版本戳，串行化只是把"丢弃"换成"排队"）。**测量口径**：(i) dev 模式的 vite 代理表 `callerRoutes` 必须含 `/sessions`（缺了它开发模式下点新建会话直接 404，这条竞态在 dev 里测不到；那是缺配置不是产品行为）；(ii) `#toast` 为空**不能单独**当"没报错"的证据——`showError` 有自动收起，读数要与 `#agent-toast` 的成功回执、`POST /sessions` 的 200 一起取。
- ④ **接口面**：路由不增不减；`viewport.rendererBadge` 键随宿主删除（§9.3），`agent.operations` 与 `agent.input.speechStatus` 两键按占位策略留着、状态 `unreferenced`（`parity_inventory.py` 的 `features_unreferenced` 恰是这两条）。
- ⑤ **回归**：tsc/build/vitest 基线不变——不动任何 TS 判据；`main.ts` 的时序与文案没有单测面（它若红了说明改动越界）。真机证据由一次性作用域探针给出（**坑 90**）；`var/stack/ui-round-*.png` 界面截图是给人核对版式用的，**不是判据**。

12.24 UI 优化第四轮：界面只说当前状态 + 任务进行中「新建会话」不再被浏览器替它拒绝（§4.6/§4.8.1 的 **Q127**/**Q128**）：
- ① **文案判据（Q127）**：界面五句落成 `任务 #N 进行中，新建会话失败`（门未带 `allowActiveTask` 时）、`上一个任务的结果还没确认，新建会话失败`（quarantine）、`还没读到会话状态，新建会话失败`（权威路由还没答）、`任务 #N 进行中，光照和阴影暂时改不了，任务结束后自动恢复`（灯光／阴影同一条，§4.8.1）、弹窗第一行 `任务进行中，场景暂时不能改。` 与 `这个会话里有任务正在进行。`。**否定式**：`租约`、`接管`、`现场`、`§`、`GET /api/sessionState`、`POST /resolveUncertain` 六串在浏览器自己写出的每一句界面文本里命中 **0**（`session_state.ts`／`local_edits.ts` 两个纯函数的整句断言钉死）；Gateway 与 Brain 的原话不在此列（§4 第 2 层不替对方改口径，`task_busy` 仍照原文念）。
- ② **门的判据（Q128，唯一真 bug）**：`newSessionView(view({ lease }), nameOf)` 默认仍拒（发送命令前那次自动开会话走这句），但 `newSessionView(view({ lease }), nameOf, true).blocked` 必须为 `null`——第三参数就是调用方已在 `POST /sessions` 里声明的 `allowActiveTask`，两枚显式入口（`acceptPhaseBlock`、`createConversation`）都传 `true`。浏览器**不得**在发出请求之前替网关拒绝（否则网关那道 `held == "lease" and not allow_active_task` 永远走不到，`allowActiveTask` 成死参数，「新建会话」在任务运行期必红）。修复后：租约仍**不被解除**（不取消任务、不释放锁，只是留在旧会话自己的上下文里；"新建会话不是解锁证据"一字未动），quarantine 在浏览器与网关两侧照旧拦，`lease.commit_unconfirmed` 由网关拦（`SceneLeaseView` 不含这个字段，浏览器无从镜像）。
- ③ **接口面**：路由不增不减、`POST /sessions` 请求形状不变（`allowActiveTask` 本来就在契约里）。
- ④ **回归**：tsc/build 干净，vitest 按新增断言计（`session_state.test.ts` 的 `allowActiveTask` 正/负控制 + 整句文案断言）。

12.25 Agent 输入框的回车发送（§7.6 的 **Q129**）：判据是"它有没有走按钮那条路"。
- ① **纯函数判据（`logic.ts:enterSendsAgentMessage`）**：一个正控制 + 六个否定式——键开着、`{ key: "Enter" }` 且六个标志全假 ⇒ `true`；`shiftKey`、`ctrlKey`、`metaKey`、`altKey`、`isComposing`、`repeat` 任一翻真 ⇒ `false`；`key: "a"` ⇒ `false`；键关掉 ⇒ `false`（这时回车不 `preventDefault`，换行照旧）。
- ② **页面判据（真浏览器）**：**回车那一路** ⇒ `#agent-messages` 末尾多出一条 `.message.user` 且正文等于所键入文本、`#agent-input` 被清空、观察到一次 `POST /api/agent/jobs`——与点 `.send` 按钮读到的四项**逐位相同**（"等同于"的可读形态）。**`Shift+Enter`** ⇒ 气泡数不变、`#agent-input` 值多一个 `\n`、请求数增 0。**合成 `isComposing: true` 的 Enter** ⇒ 同 `Shift+Enter` 的四项读数。**`agent.input.send: false`** ⇒ 按钮 `hidden`、回车后只多 `\n` 且请求数增 0——键回到浏览器默认，而不是被吃掉。
- ③ **接口面**：路由不增不减、`POST /api/agent/jobs` body 形状不变（回车与点击共用 `submitAgent`）；键面/CSV 不动（`agent.input.send` 的读者从 1 个 DOM 宿主变成"宿主 + 一条键"，状态仍 `literal`）。


12.26 物体本地坐标轴（§3 组件定义的 `LocalAxes`，**Q130**）：判据重心是**"这一层是编辑层"与"它是文件里的数据"两头同时成立**。
- ① **单测（无 GPU）**：`world.test.ts` 钉解析——`LocalAxes` 进 `KNOWN_COMPONENT_TYPES` 后不再报 unknown；`node.localAxes` 存整份 `properties`；`length` 缺失/`-1`/`"0.2"`/`NaN` 都落 `0.1`（后三种各回一条告警）；`colors` 只写 `x` 时另两轴按默认色、非法色只影响那一轴；`show_labels` 只有显式 `true` 才真且其它值不告警；`offset` 缺失、非 3 元素或分量非有限数时告警并按 `[0,0,0]`；同类组件只保留第一个（§3.4）。数值解析是纯函数 `localAxesRig()`（与 `cameraRig()` 同族，回 `{length, colors, showLabels, offset, warnings}`、不碰 Three.js）——解析端拿 `warnings`、渲染端拿数值，同一份回退不可能两头各算一套。`renderer.test.ts` 钉画面规则——物体节点下恰有一个 `userData.localAxes === true` 子节点；三轴顶点距离 = `length`、顶点色 = 解析后三色（默认 `#FF0000`/`#00FF00`/`#0000FF`）；`show_labels` 假时无标签 Sprite、真时恰有 `X`/`Y`/`Z` 三枚；`offset` 平移整个 Group 而轴与标签仍从 Group 原点出发；`setLocalAxesEnabled(false)` 整层不可见、`dispose()` 释放几何与材质；重建签名含该项 ⇒ "只改 `LocalAxes`"也再走一次 `populate()`，改别的物体的不动本物体。开关判据自 **Q132** 起收进纯函数 `localAxesEnabled(features, checked)`（与 `boundingBoxesEnabled()` 同形），渲染器只收那一个布尔。**实测（2026-10-28）**：新测试 19 条，vitest **523** 通过、tsc 干净。
- ② **负控制（两头都要钉）**：**不进采集与量测**——轴开/关两态下 `worldAabb()`（整体与单物体）、`/get2DAABB`、`frame()` 入参、`shadowReport()` 逐字段相同；同一次 `/captureScene` 与视口截图两态逐字节相同；拾取射线两态命中同一物体。身份标志只有两处：`isEditorOnly()` 与 `selectable()` 各认一次 `userData.localAxes`（`hideOverlays()`/`expandGeometry()`/`pick()` 原文一字不改）。**确实是文件里的数据**（与 §4.8 本地三类的关键差别，反向也要钉）：`/updateItem` 写入后 `scene_version` +1（§3.9）、`/getWorld` 与导出都有这一项；键与勾选框来回切，`world.toJSON()`、两版本号、层级树行数、`#panel-count` 一字不动。阴影名单只数 `isMesh`，轴是 `Line`/`Sprite` 天生不在名单上。
- ③ **真浏览器（本轮未跑，坑 90）**：A 带组件（Q132 起还要勾上勾选框）⇒ 红/绿/蓝三族像素计数都 > 0 且蓝轴（+Z）重心在物体原点上方（Z-up 的可数字化形态），撤下组件回 0；B 给带 `RobotAttachment` 的物体加轴，轨迹两帧不同位姿 ⇒ 轴屏幕位置必须移动（挂在物体节点之下的证据，不需要每帧重算）；C 只带 `Transform`+`LocalAxes` 的纯标记物体画得出轴而 `worldAabb()` 仍回 `null`；D 键 `false` 或未勾 ⇒ 计数回 0 且 `Viewport markers` 行 `localAxesEnabled:false`、`markers` 无 `kind:"localAxes"` 条目——"键关""没勾上""场景本来没写"三态像素同形，只有这一行能分开（§12.14/§12.20 教训）。
- ④ **接口面与回归**：零新增 gateway 调用；`features.json` 84→85 键（同日 Q132 改名 `viewport.localAxesToggle`、计数不变）、CSV +1 行；两份 `types.ts` 各加 `LocalAxesProps` 且 `cmp` 逐字节相同（坑 6）；用户维护输入件（`ur5e_table_empty.json`、`scene_example.json`）不代加组件。实测：tsc 干净、vitest 523/523、build、audit_parity **62/62**、parity_inventory `features_local 85` / 探针不增不减。

12.27 视口的两件家具：地面网格与世界原点坐标轴（§4.4 的 **Q131**；⑤ 为 Q135 就地修订）：两件东西自 M1-4 就在画，判据重心是**这两个键归谁管、管到哪一层为止**。
- ① **单测（`renderer.test.ts`，Q135 后 6 条）**：A 各自管各自的且关掉再打开回原样；B 选中态不得越权——两层关掉后 `select()` 不动它们（它们挂 `scene`、不在 `markerNodes()` 行走范围内，除 setter 无人写 `visible`）；C 关断是从屏幕上按掉不是从场景摘走（`scene.children` 仍含两者）；D 本来就是编辑层——`isEditorOnly()` 回 `true`、`hideOverlays()` 列进按掉的一层，且键先关的那层不会被采集恢复成可见（坑 75）；E `gridEnabled`/`worldAxesEnabled` 两布尔随 setter 变。**实测（2026-10-28）**：vitest **528**。
- ② **负控制**：两键只管屏幕——Helper 早在 §4.4 排除清单，`/captureScene` 与截图两态本就不含，"开关前后量到的东西一样"是既有排除的推论；也不是世界数据（与 §12.26 正相反的对照）：两键不进场任何解析/写入/导出，切四次一切外部通道一字不动，场景文件里没有对应字段。**唯一看不见的新行为是缺席的键**：`featureEnabled` fail-closed 而 gateway `features.json` 是启动快照（坑 22）⇒ 不重启，这两枚新键读成 `false`，每个人都在看的网格与坐标轴会消失 ⇒ 不补常量，把"改值要重启"写死（§9.3）。**不做**：尺寸/间距/颜色旋钮、给这两枚键配工具栏勾选框。
- ③ **真浏览器（未跑）**：A 默认态两族像素计数 > 0、关任一键该族回 0 而另一族不动；B `Viewport markers` 读数与屏幕一致；C 两态采集逐字节相同；D 腕部相机两态 `/get2DAABB`（剥 `request_id`，坑 87）逐字段相同。只有单测与代码路径证据。
- ④ **接口面**：`features.json` 85→87 键（`viewport.worldAxes`、`viewport.grid`，**默认 `true`** = 保持既有画面）、CSV +2 行；**两份 `types.ts` 本轮一字不动**（feature 键不进 `RuntimeConfig`，与坑 6 无关）；`logic.ts` 不新增判据函数——单键直读，`main.ts` 推布尔、`renderer.ts` 对配置一无所知（Q96/Q105/Q106/Q130 分工）。
- ⑤ **Q135（2026-11-02）三根轴改站进仿真系（就地修订）**：`AxesHelper` 原画渲染系（Y-up）的 +X/+Y/+Z，而文件都在 `simWorld` 的 −π/2 之后（仿真系 Z-up）⇒ 朝天那根是绿的、蓝轴指向文件的 −Y，与 §4.8.6 的 `LocalAxes` 同屏互指不同方向。改法一行：字面量提成 `export const SIM_WORLD_TILT`，`axesHelper.rotation.x` 与 `simWorld.rotation.x` 同一常量；网格明确不转。单测新增 F 条：两 `rotation.x` 相等（同源判据比断言具体角度抗漂移）＋四元数乘单位轴验换算 `+X→(1,0,0)`、`+Y→(0,0,-1)`、`+Z→(0,1,0)`；夹具限制在册（`adapterFixture()` 手搭初值，构造器覆盖面外）。负控制一例不减；不新增键、87 键不变、探针不增不减。**实测（2026-11-02）**：vitest **569**、build 885.09 kB。决议 §13.48，卡片 FEAT-2026-11-02-A。

12.28 本地坐标轴勾选框与那枚改名的键（§4.8.8 的 **Q132**）：判据重心是**"谁声明、谁开关、谁写 `visible`"三件事分家**——声明集合的是场景文件（一字未改），开关是工具栏勾选框，宿主在不在是 `viewport.localAxesToggle`，写 `visible` 的仍只有 `applyOverlayVisibility()` 那一趟遍历。
- ① **单测（`logic.test.ts` 3 条）**：a 只有"键开且勾上"回 `true`；b 键 `false` 与键缺席在勾上也回 `false`（宿主消失与没勾上同一张画面 ⇒ 这就是不补常量的形状）；c 旧名 `viewport.localAxes: true` 既不亮宿主也不开这一层——改名方向钉死。`renderer.test.ts` 那一族（§12.26 的 8 支 + Q131 的 5 支）一例不减一例不增：渲染器契约没变，只把类字段初值 `true`→`false` 对齐"默认不画"。**实测**：vitest **531**。
- ② **负控制**：这枚勾只管屏幕——勾与不勾两态下 `world.toJSON()`、两版本号、层级树行数、`#panel-count`、`/getWorld` 一字不动；`/captureScene` 与视口截图逐字节相同（四处判据一个字没改 ⇒ §12.20 ② 那条判据的第三次复用）。反向也钉：勾上之后轴仍不是拾取目标、仍不进 `worldAabb()`。
- ③ **真浏览器（未跑）**：A 宿主随键在/不在、强点看不见的 input 也不画；B 勾上 ⇒ 三族像素 > 0 且 `markers` 里 `kind:"localAxes"` 条目数等于场景带该组件的物体数（"显示所有"的可数形态），取消勾选回 0 而网格/世界轴两族不动；C 采集两态逐字节相同；D 旧快照（只有旧名）⇒ 无勾选框、不画轴、读数为 `false`（① 第三支的真机形态）。
- ④ **接口面**：配置面是**改名**（总键数 87 不变），CSV 该行随之改名（四个判定值一字未动）、probes 计数不变 ⇒ 不提交；两份 `types.ts` 一字不动。**生效条件：重启 gateway**（动的是键面，坑 22）。实测：vitest 531/17 文件、audit_parity 62/62、parity_inventory `features_local: 87`。卡片 FEAT-2026-10-28-C。

12.29 工具栏的界面文字（§7.1 文字口径的 **Q133**）：本轮只改字、不改行为，判据测的是"屏幕上出现的是哪几个字、悬浮时出现的是哪一种语言"。
- ① **单测（`lighting.test.ts` 2 支）**：`studio` 的 `label` 恰为 `Studio`；**每个预设的 `label` 都不含 `(`/`)`/`（`/`）`**——把"删括号"从一次改动变成一条规则。其余 TS 判据一例不减（`main.ts` 模板与话术没有单测面，它若红了说明改动越界）。**实测**：vitest **532**。
- ② **静态判据（读源码）**：`.viewport-toolbar` 七个 `<b>` 依次 `加载场景`、`加载轨迹`、`显示轨迹`、`录制辅助图层`、`投射阴影`、`显示包围盒`、`显示坐标轴`；**每个点得动的控件都带 `title` 且每句 `title` 纯 ASCII**；`#selection-label` 是回显不是控件、不配 `title`；§12.23 ② 那六串英文旧话术在所有 `title` 里仍命中 0。弹窗一族跟着改名：`投射阴影已开启/已关闭`、`包围盒已显示/已隐藏`、`坐标轴已显示/已隐藏`、`录制辅助图层已开启/已关闭：…`、`加载场景失败：…`、`加载轨迹失败：…`、`加载轨迹：N 个对象不在当前世界中，已忽略`、`光照已切换：<短名>`。
- ③ **真浏览器（本轮跑了，一次性手工探针）**：一次性网关 8098（`--state-path` 指临时文件）+ 现成 `dist/` + 一次静态服务 5199；未起 OLS、用户 8000/8080/8099/8500 全程未碰；收尾两端口杀掉、`netstat` 复核、临时文件删除。A 七个 `<b>` 与 ② 表逐字相同；B 14 个可点对象的 `title` 全部非空且匹配 `/^[\x20-\x7E]+$/`、`#selection-label` 仍无 `title`、六串旧话术命中 `[]`；C 六句弹窗逐字读回 + 六个预设名无括号。**口径就地修正**：路点勾选框按 Q101 设计只重画路点、不答话 ⇒ 这一格读回六句不是七句。窄容器对照组未测：收字由 CSS 两条规则决定、字变短只会让 §7.1 判据更宽松。
- ④ **保留英文的那些句子**：录制的过程提示与截图失败三句告警不动（§12.16/§12.22 的取证原话钉的就是那几句英文）；顺带修两处**因坑 90 从未跑到因此没人发现的旧冒烟断言**（`tools/browser_smoke.cjs` 匹配 Q124 前旧话术的两行）改成今天真的会出现在 `#toast` 里的句子。要整体中文化请另开一轮。
- ⑤ **接口面**：一个键都不增删（87 键）、一条请求都不发 ⇒ 探针与 CSV 一字不动（改的是 `<b>`/`title`/话术文本，`data-feature`/`data-action`/`id` 全在原位 ⇒ `editor_reach` 判定不受影响）；**不需重启 gateway**，看新文字要重新构建 `dist/`。实测：tsc 干净、vitest 532/17、build、audit_parity 62/62。

12.30 资产面板的摆放与替换（§4.6.1 的 **Q134**）：判据重心是**"落点是算出来的，不是写死的"**——每个数字都要能由 OLS 元数据 + 场景里工作台实际占的那个盒复原。
- ① **单测（`placement.test.ts` 34 条 + `world.test.ts` 2 条）**：夹具是逐字抄下来的真实 OLS 文档（工作台 = model 29、臂 = robot 1、底座 = robot 4、夹爪 = robot 2、相机 = robot 3/11、无 driver 的臂 = robot 16、无 `scene_one_arm` 的臂 = robot 13）与 `docs/ur5e_table_empty.json`。钉住：六个 AABB 浮点当**三对无序**读、非六个有限数按缺席处理；`armOrigin` 把 `[0.2,0.3,0.5]` 落在**长边中点**上加 `offset [0.3,0.225,0.3]`（表在原点 ⇒ `[±0.3,-0.15,0.9]`）且镜像那台只在 X 取负、该点**就是**写进 `Transform.position` 的原点——用例专挑 baxter（robot 15，出厂唯一盒底压到原点以下 0.92 m 的臂）钉"臂自己的盒既不抬也不压"；`pedestalPose` 盒顶贴臂盒底、z 缩放照 `pedestal_scale` 声明原样写（缺该键才 `[1,1,1]`，不放大去够地面）；`spotOnTable` 盒坐桌顶按采样点居中、撞车换样本、占满回 `null`；角色四条判据中"夹爪的空连接表不算缺失"那支同时钉"这次点击一次文件读都没花"（`source.reads` 空数组——把"不要去读 URDF"变成可测事实的唯一处）；换臂保留 `object_id_in_scene` 与其它组件、`RobotAttachment` 逐字段等于模板（ur5e 换 ur5e 是恒等回写）；换夹爪补回 `parent_link`、换相机只换 `MetadataRef`、现场无臂时点夹爪拒绝；`rewriteComponents` 同类型替换 + 原样保留 + 追加缺失。`updateLocalItem` 就地改写 +1 子版本（`scene_version` 冻住），`removeLocalSubtree` 连带删下（与 `removeLocalObject` 的"摘走子级"正相反），校验支不推进计数器。
- ② **静态判据**：`main.ts:chooseLibraryItem` 一次点击只有三个出口（空现场先取基线场景 `BASELINE_SCENE_ID = 1` → `placeAsset` → `syncScene`/写共存/`reportWorld`/选中）；调用方四条写入路径一个字节没改，本节的写全走 §4.8 的 ② 类，无一碰 `bumpVersion`；闸门仍是单选择器 `CLASS_TWO_ENTRIES`，**没有新增第二道判据**。
- ③ **真浏览器（未跑，坑 90）**：A 空页面点模型 ⇒ 基线 8 物体 + 第 9 个 `z ≈ 0.637`；B 连点两次 ⇒ 两落点不同且盒不相交；C 点 robot 8 ⇒ `scene_version` 不变、子版本 +1、臂 id 仍是 2、夹爪仍挂着；D 点 robot 2 ⇒ 提示换爪且 `parent_link` 非空。只有单测与代码路径证据。
- ④ **接口面与回归**：新增的都是只读 OLS 调用、gateway 一条新路由不加；87 键不变；`types.ts` 一字不动（`Aabb6` 是 `library.ts` 本地类型，不进契约面）。实测：tsc 干净、vitest **568/18 文件**（本轮前 532/17：`placement.test.ts` +35、`world.test.ts` +2、`library.test.ts` +1、`assets.test.ts` −2——撤下的是"面板点击写原点"的 `objectForItem`）。不重启 gateway；看新行为重建 `dist/`。
- ⑤ **数字口径**：a 出厂七场景的工作台永远站在 `[0,0,0]` 单位姿态 ⇒ 判据只钉**朝向盒**（`localBox()` 打父链位姿后的世界盒）而不是声明值——本仓库 `ur5e_table_empty.json` 把桌子搬到 `x=0.525`，同一条式子的第二个入参给 `[0.825,-0.15,0.9]`。b 基准点由 OLS 随键声明的定义决定（`//robot_pose_percent_compared_to_table_length_center`：长边沿 X+,Y−,Z+、**中点是这条边的中点**）⇒ 式 `[cx+p0·Lx, box.min.y+p1·Ly, box.max.z+p2·Lz]` 三个 offset 都加在中点上；与出厂手写坐标只有 x 复原得出（诚实记录：piper/cobotmagic 的 `[±0.3,-0.33,0.6]` 与 ur5e 的 `[0,-0.5,0.57]` 中 y、z 是作者手调，不由本规则复原；本轮被用户就地更正两次，历史在任务卡）。b'' 替换机械臂**只算不判**——现阶段百分比是占位数，臂/底座一路不查碰撞；随机采样与碰撞避免只作用于道具。b' 出厂十一台臂里四台盒底压在自己原点以下（最深 baxter −0.9242）⇒"该点即原点"与"盒底落在该点"不是同一条。c 底座缩放声明原样：`robot 4` 盒跨 `z∈[-0.25,0.25]`、十一台全声明 `[1,1,1]` ⇒ 盒跨 `[0.4,0.9]`，顶沿精确贴臂盒底 0.9、底沿悬空 0.4；曾加的 `max(声明, 臂盒底/盒高)` 放大被用户第三条口径推翻——悬空还是沉地都是声明值的后果，不由前端补；"上边缘重合"那句只约束顶沿。d 十一台臂全部发布 `[0.2,0.3,0.5]`+`has_pedestal:true`+`pedestal_id:4`+`pedestal_scale:[1,1,1]` ⇒ 桩数字是抄的不是编的。道具事实：静止桌顶（apple `z = 0.637373` 逐值断到这一位）、不挂桌下（`parent: null`，靠 `Semantic.support`）。

12.31 左栏重渲染不撤走列表的滚动位置（§7.2 的 **Q136**）：用户口径「点击资产列表中的东西后不再让列表回到顶部」。判据重心是**"同一个字符串不写第二次"**，不是"写完再把 `scrollTop` 找回来"。
- ① **单测（`logic.test.ts` 3 条）**：a 逐字相同的两段（含两个空串）回 `false`——这就是"不写"的形状；b 行数变了、同一行内容变了都回 `true`（层级高亮与 Details 读数必须继续重写）；c `previous === null` 两支都回 `true`（"屏幕上还没有东西"与"屏幕上是一段空"不是同一个事实）。
- ② **静态判据**：`#context-content` 的 `innerHTML` 只有一个赋值点、外套 `panelMarkupChanged(contextMarkup, markup)` 守卫，且缓存与写入**同时**更新；模块级缓存不随 `state.panel` 切换失效（有意：切换时字符串本来不同，守卫自然放行）。**为什么资产这一支成立而层级两支不成立**：`assetRow()` 的 markup 里没有选中态（被选中的是现场物体不是这一行）⇒ 摆放后重渲染得到逐字相同的串；`treeRows()` 把 `selected` 写进 class、`renderDetails()` 把读数写进正文，字符串会变、守卫不拦。
- ③ **真浏览器（未跑，坑 90）**："点一个模型，列表停在第 20 行、缩略图不重载"只有单测与代码路径证据。跑通后的最小探针：`.asset-list` 的 `scrollTop` 非零 ⇒ 点一行道具卡 ⇒ 同一 `scrollTop` 逐值不变且 `/thumbnail` 请求数不增。
- ④ **负控制与接口面**：只减一次 DOM 写——选中态、`#panel-count`、层级搜索、`reportWorld()` 与四元组全部照旧；反向对照：撤掉守卫 ⇒ ①a 立刻红。键面接口面不动（87 键、零请求、CSV/探针不增不减、`types.ts` 不动，坑 6）⇒ 不需重启 gateway。**顺带修好两件同源副作用**：层级搜索框的字不再被无关重渲染抹掉、缩略图不再每轮重发请求。实测：tsc 干净、vitest **572/18**（本轮前 569）、build 885.16 kB（CSS 哈希未变）。决议 §13.49，卡片 FEAT-2026-11-04-A。

12.32 摆放道具的碰撞集里没有机械臂那一整套（§4.6.1 的 **Q137**）：用户口径「放置模型时不再考虑机械臂的包围盒」。判据重心是**"出局的是整个臂族、不是臂那一个盒"与"留下来的仍然留下"两头同时成立**。
- ① **单测（`placement.test.ts` +6，全套 **578/18**）**：a `armFamilyIds(出厂夹具)` 恰为 `[2,3,4]`（臂、挂它手上的夹爪、挂夹爪上的腕部相机，两条边各认一次）；b `1/5/6` 不在族里（工作台、空中相机、底座）；c 无臂场景回空集；d 点一次道具后 `metaReads` 逐字等于 `["model:1","model:29","robot:3","robot:4"]`——臂与夹爪的元数据一次都没要（出局的不只是盒，连读都没有）；e 负控制：把臂的朝向盒塞回 occupied ⇒ `null`、空集 ⇒ 取到。卡 ㊶ 旧判据"绕开压在臂上方的臂"同轮改写两支：正中一次采样成功（`[0.525032792,-0.00021716,0.637373223]`）与"先占正中再点一次退到近角"（`[-0.187601202,-0.337889444,0.637373223]`——那个数字就是旧判据原来钉的那一个，换了归因：挡路的成了一件道具）。
- ② **静态判据**：臂族闭包只沿两条边走到底（`Transform.parent` 与 `RobotAttachment.parent_object_id`），起点 `findArmObjects(world)`；`occupiedBoxes()` 只多一个 `skip.has(node.id)`，采样/严格相交/64 次上限/`桌顶−盒底` 的 z 一字未动 ⇒ 改的是**谁进集合**，不是怎么判。底座留在集里是正面证据：同属 `has_pedestal` 声明，但它是现场家具，用户点名的只有臂。
- ③ **真浏览器（未跑）**：载入基线场景连点三次同一 model ⇒ 三次都成功、三落点两两不重叠、臂与爪原地不动。只有单测证据。
- ④ **负控制与接口面**：e 支同时钉"几何没变"与"改变归因于 `skip`"。其余一族一例不减；臂的**替换**一路一字未动（它从来就只算不判，§12.30 ⑤ b''）。零新键（87 键）、零请求、`types.ts` 不动 ⇒ 不增不减、不需重启。实测：tsc 干净、vitest **578/18**、build 885.43 kB（CSS 哈希未变）、audit_parity 62/62、`features_local: 87`。决议 §13.50，卡片 FEAT-2026-11-04-B。

12.33 相机画面悬浮窗（§4.8.9 的 **Q138**）：用户口径「添加一个悬浮窗，显示所有相机的拍照内容」。判据重心是**"窗里每一格都是那台传感器自己的视线"与"这一层天生不在采集通路上，因为它不在场景图里"**。
- ① **单测（`logic.test.ts` +6，全套 **584/18**）**：a 键开且勾上才 `true`；b 键关或缺席 ⇒ `false`（fail-closed；"没有控件"与"没勾上"同一张画面 ⇒ 不补常量）；c `{viewport.cameraPreview: true}` ⇒ `false`——旧快照里 Details 预览那枚老键不被读成"这扇窗该开"（坑 22 一族，与 §12.28"不回答旧名"同形）；d 出厂 `camera_preview`（640×480@30）下取窗自己的 `{320,240,1000/12}`（Q139 把 `{320,240}` 改写 `{256,192}`，`1000/12` 不动——缩的是排版不是负载）；e 更紧的运行时逐项取运行时那份——§9.1 那三个数是天花板不是目标；f 两端不越界：`max_hz:1000` 仍 12 Hz，`max_hz:0` 落 **1 Hz**（`1000/0` 是 `Infinity`，`setInterval` 把非有限值读成"尽可能快"——这支钉的就是最容易写错的分母）。
- ② **静态判据**：g 窗的集合就是既有 `findCameraObjects(world)`，没添第二条筛选——不按选中态、不因挂在臂上漏掉腕部相机；h Q138 版写死 `"rgb"`（Q139 建起深度入口，见 §12.34）；i `renderCameraWall()` 是 `cameraWallEnabled()`/`cameraWallPace()` 唯一调用者，渲染只经既有 `renderSensorFrame()`——`renderer.ts` 与 `placement.ts` 一字未动；j 窗是 `.canvas-wrap` 里画布之上的 DOM ⇒ "不进采集与量测"不需要新判据（与 §4.8.5–4.8.8 各层的排除机制**形状不同**，见 §4.8.9"它是 DOM，不是场景图"）。
- ③ **真浏览器（未跑，坑 90）**：勾上 ⇒ 窗可见、`#camera-wall-count` 等于场景相机数、每格 canvas 非空；播放轨迹两帧位姿 ⇒ 腕部那格像素哈希随帧变化（"实时"与"挂在臂上的没漏"双证）；取消勾选或键关 ⇒ `hidden` 且强点也不长出来；无相机场景 ⇒ 窗开着、count `0`、一句 `场景里没有相机。`（与上一支不同形 ⇒ 这层不必配日志行）；开关两态 `/captureScene` 与视口截图逐字节相同。
- ④ **负控制与接口面**：只加一块 DOM、一枚键、一条样式——Details 预览、拾取选中、左栏三视图、上报、录制截图全照旧。反向对照：撤 `&& checked` ⇒ a 红；撤 `Math.max(1,…)` ⇒ f 红。**那枚 ✕ 不构成第三个开关**（同一条布尔）。**键面 +1**：`features.json` 87→**88**、CSV +1 行（`viewport.cameraWall,…,literal,…,WIRED`）；probes 提交 `features: 87→88` 那一格（计数真的变了，与前两轮"只差时间戳不提交"不矛盾）；**生效条件 = 重启 8099**（坑 22；症状是"没有勾选框、窗开不了"，不是报错）。实测：vitest **584/18**、build 887.64 kB、CSS 换哈希 21.54 kB、audit_parity 62/62、`features_local: 88`。决议 §13.51，卡片 FEAT-2026-11-04-C。

12.34 相机画面悬浮窗的第二轮（§4.8.9 的 **Q139**）：六个动作、零个新能力（缩小/可拖动/名称上居中/不显示尺寸/RGB-深度切换/深度伪彩）。判据重心是**"深度态借的是既有那枚键，不是第四枚"与"伪彩是这扇窗的读法，不是深度图的定义"**。
- ① **单测（`logic.test.ts` +6，全套 **590/18**）**：a 键开时 `cameraWallModeFor(features,"depth")==="depth"`、`"rgb"` 永远原样返回；b 键关或缺席 ⇒ `depth` 折回 `rgb`（fail-closed；未重启的旧快照里窗只显示 RGB——温和面症状，不报错也不撤掉任何人一直在看的东西）；c 折回是**派生不是改写偏好**——`state.cameraWallMode` 仍是用户点的那枚，键再开时深度态自己回来；d `depthColor()` 五锚点逐字对得上（0 蓝、0.25 青、0.5 绿、0.75 黄、1 红）；e 锚点间线性插值、两端 clamp；f `depthPalette()` 恰 **256** 项、项是小端 `0xAABBGGRR`、第 i 项 = `depthColor(i/255)` 的字节打包（表与函数不会各说各话：表错一格，画面一格偏色）。
- ② **静态判据**：g 深度按钮的 `data-feature` 写 `viewport.cameraPreview.modes.depth`，`applyFeatureVisibility()` 免费门控，`cameraWallModeFor()` 是第二道（强点看不见的按钮也拿不到深度）；RGB 那枚不挂 `data-feature`；h 两枚按钮用 `data-camera-wall-mode` **不是** `data-preview`——Details 的委托选择器是全局 `[data-preview]`，同名会互相点亮；i `renderer.ts` 一字未动——伪彩是 `tintDepthFrame()` 画完后的**后处理**，Details 灰度口径一字不改（"渲染器不读配置"分工仍成立）；j `willReadFrequently` 在建格子那支里认领（上下文属性首次调用定死，晚一步永远认领不到）；k 拖动只在 `.camera-wall-head` 起手且 `closest("button")` 直接 return——✕ 与两枚模式按钮仍是按钮。
- ③ **真浏览器（本轮跑了）**：一次性栈（真 Chrome 1600×1000 + 一次性 gateway 临时常量/临时状态 + 一次性只读 OLS；用户 8099/8000 全程未动），场景走界面自己的「加载场景」。实测：窗宽解析 288.75px（`min(34%,240rem)`）；每格 canvas 缓冲 **256×192**；名称 `text-align:center` 居自己那格正上方；成功态 `data-camera-status` 空串 + `:empty` 隐藏（"不再显示尺寸"的落地）；RGB 两格 **86/256** 种颜色、深度两格 48/23 种，近蓝→中青绿→远红，**且红色的像素数与 RGB 里纯黑色数逐格相同（15399/35622）** ⇒ 打空射线被清到 `far` 再映成最远端红——"伪彩表达不了没有测量"的代价是测出来的。负控制：腕部那台改 `rgb` ⇒ 深度格写 `无深度`、另一格仍空。拖动走 CDP 可信鼠标事件（合成 PointerEvent 的 pointerId 无效）：拖 `(-240,+144)` 后 `left/top` 精确；甩 `±4000` 夹在 `wrap−wall` 那对上界逐字相等；按 ✕ 拖不动窗也不关窗。视图态活过开合、刷新才回默认。
- ④ **负控制与接口面**：**键面与接口面不动**（88 键、CSV 89 行、probes 计数不变 ⇒ 不提交；唯一一格是 `modes.depth` 的 `editor_reach` `interpolated→literal`）⇒ **不需要重启 gateway**（与 Q138 那轮唯一的生效条件差别）。反向对照三条：撤深度键半句判据 ⇒ b 红；撤 `| 0xff000000` alpha ⇒ f 红；改回 `320×240` ⇒ §12.33 d 红。行为一族不减。实测：vitest **590/18**、build 890.01 kB、CSS 换哈希 21.81 kB、audit_parity 62/62、`features_local: 88`。决议 §13.52，卡片 FEAT-2026-11-04-D。

12.35 拾取只落在水面上，线框资产抢不走邻居的点击（§4.7 的 **Q140**）：用户报"模型 22 的命中判定异常的大"并留了开放问题（"或许不是资产问题而是代码问题"）——**答案是两个都是**。根因两条独立的腿：**资产** `doll.obj` 多一条 1.5 mm 的 `l 952 960`（导出残留）；**拾取配置** three 测线命中用 `Raycaster.params.Line.threshold` 默认 **1**（一米余量），而 `OBJLoader` 不能把面与线装进同一份几何 ⇒ 那份资产整份解析成 `LineSegments`，一米内处处算命中。本轮只修得了腿二，判据重心是**"规则写在 `selectable()` 里，不写在阈值里"**。
- ① **单测（`renderer.test.ts` +3，全套 **593/18**）**：a 只有面的 OBJ 解析出恰一个 `Mesh` 零条线且 `selectable()` 认它；b 同一份补一条 `l 1 2` ⇒ `Mesh` 归 0、`LineSegments` 1、几何顶点反而更多——这支钉的是**加载器的行为**，以后任何人升级 three 只要此事不再成立就会红；c `selectable()` 对 `LineSegments`/`Line`/`Points` 回 `false`、对 `Mesh` 与 `Sprite` 回 `true`（不减的一面与出局的一面钉在同一支：灯标记本体是 `Mesh`，撤线框不等于撤标记的可点性）。夹具限制在册（构造器里两行阈值归零不在单测覆盖面，由 ② 的无头数字负责）。
- ② **静态判据（读代码 + 无头 three 复核）**：d 全库扫描——OLS 34 份 OBJ **只有资产 22 一份**带线语句 ⇒ 不是普遍现象，是"一份坏资产 × 一米阈值"的合成症状，不需要全场白名单；e 无头 A/B：带 `l` 只有一个 `LineSegments`（position 6806）零个 `Mesh`，删那行 ⇒ `Mesh` position 6804；f 无头射线（对导出场景 mug#13）：默认阈值一次点击 **3403** 个线段命中、横偏到 90 cm 仍命中；阈值 `0` 或 `0.01` ⇒ 命中 0；g 改动只有 `renderer.ts`（+14）与测试；h 没有误伤的另一半：灯标签是 `Sprite` 且只在选中时可见、Helper 与航点不挂 `nodes` ⇒ 本来就不在拾取集合（不补判据，见 §13.53"不在集合里"与"不许携带选中"两层各自独立）。
- ③ **真浏览器（本轮跑了，硬数字在修之前）**：一次性栈（gateway 8098 + 只读 OLS 8123 + headless Chrome），载入用户导出的那份场景。修复前 63 个垂直向下网点：`(none) 36 / doll#18 26 / RobotPedestal#6 1`，**19 件道具一件都没被点中过**。修复后逐点复测不可比（两个原因如实记下：相机阻尼运动未静；**取证窗口内 OLS 侧自己把资产 22 换成了 `toy_panda`**——734 面 0 线，线框案例已不存在）。修复后判据以人工实测为准（用户口径「现在物体能够精确的被选中了」）。腿一的账记在案：一副纯线框资产再次出现时在鼠标下**选不中**——要能选中就得把面补回资产。
- ④ **负控制与接口面**：删 `selectable()` 首行判项 ⇒ c 立刻红。零新键（88）、零新路由、`types.ts` 不动 ⇒ 不需重启。行为一族不减。实测：vitest **593/18**、build 890.13 kB（CSS 哈希不变）、audit_parity 62/62、`features_local: 88`。决议 §13.53，卡片 FEAT-2026-11-04-E。

12.36 一个坏零件不许把整台视口的量测与相机一起带走（§6.5 的 **Q141**；**同轮被 §12.37/Q143 改判两处，本节按历史保留**）：用户报"机械臂有时不显示包围盒、点击也无法选中"。根因不在臂上：出厂库 **210 份 OBJ 里 6 份**（全是 robotiq 夹爪零件）把一部分顶点写在引用它们的面之后 ⇒ `OBJLoader` 按文件顺序读 ⇒ 那些面解析成 **NaN 顶点**；臂被打中是因为 `RobotAttachment` 把夹爪挂在臂的 link 节点之下，`worldAabb()` 递归把 NaN 盒并进臂的盒。判据重心是**"坏的是那个被缓存的量测，不是那份网格"**（three 容忍 NaN，坑 113）。
- ① **单测（`renderer.test.ts` +4，全套 **597/18**）**：夹具 7 行 OBJ（第二面索引还没出现的顶点——`pad.obj` 犯了 900 次那个错的缩影）。a 直接喂 `OBJLoader`：`boundingBox.max.x` 非有限而射线仍命中完整面 1 次、丢顶点面 0 次（坑 113 的真实机制，不是"NaN 就不可点"的直觉）；b～c 原钉"渲染器加载路修复量测 + `Box3Helper` 与 `worldAabb` 逐字相等（Q119 命门）"——**随 Q143 删除**（病根改在加载侧掐掉，NaN 不再产生）；**d 保留**：没被修复看过的坏零件不许进父级的盒——臂盒有限 `[-0.5,3.25]`（判项跳的是那一份量测不是那棵子树），`frame(1)` 后相机与 `orbit.target` 全有限。
- ② **静态判据**：e 扫全部 210 份 OBJ 逐份精确计数 ⇒ 6 份 robotiq（`robots/2` 的 pad/silicone_pad 各 900 面 × 3 = **2700** 坏引用；`robots/10` 那两对 finger）⇒ 是一整个夹爪族的导出习惯，不能按资产 id 白名单排雷；f 改动只有 `renderer.ts`/测试；g 干净资产不被触碰——修复只在"有限顶点数 < 总数"时改写，其余 204 份行为一个字节没变。
- ③ **真浏览器（本轮跑了，用用户自己那台栈）**：`restart_stack.sh --build --keep` 起四服务、8099 服务本轮 `dist/`。读数按坑 112 口径（世界几何投影成 CSS 像素再派发事件，不照截图像素点——截图是 1.4 倍）：`worldAabb(2)` 有限；勾选框 ⇒ `boxReport().drawn` 含 2 与 3；`Frame Scene`/`Focus Selection` 不再把视口拍成一片空；35 个 link 投影点 28 个选中臂；`#toast` 那条告警逐字报出文件名且**只报一次**（`reportOnce`）。取证用的临时全局钩子同轮删除、`git status` 复核后才提交；它也纠正了本轮一次误判（照截图像素点出 `No selection` 曾让我以为修复无效——**坑 112**）。
- ④ **负控制与接口面**：撤修复调用 ⇒ b/c 红；撤 `expandGeometry()` 判项 ⇒ d 红（臂盒被坏零件带走，相机 NaN）。零新键、零新路由 ⇒ 不需重启。**代价写在正文**：那 900 个始终没拿到顶点的面既不画也不可点，要回来得重导出资产（`ObjectLibraryServer` 不在本仓）。实测：vitest **597/18**、build 891.10 kB、audit_parity 62/62、`features_local: 88`。决议 §13.54，卡片 FEAT-2026-11-04-F。

12.37 面可以引用文件后面还没出现的顶点：整份 OBJ 先规范化再交给 loader（§6.5 的 **Q143**；**同轮改判 §12.36 两处**）：用户口径「在第一遍的时候直接做规范化，这样即使后面有新的资产使用了负数索引也无所谓」。**Q141 治量测，Q143 治源**：OBJ 正索引本来就绝对、从文件头数起，把全部声明排到第一条面之前不改变任何索引指向 ⇒ 那 6 份资产解析出**完整**网格、NaN 从源头不产生、告警由构造消失。
- ① **单测（`logic.test.ts` +6、`renderer.test.ts` 四支改写；全套 597 → **607/18**，中间并行会话 Q142 占 601）**：`normalizeObjSource` 六支——①声明整体挪到面之前（逐字比对输出）；②不需要改的文件**逐字节原样返回**（204/210 份就是这个形状）；③负数索引写成它已意味着的绝对索引且声明一起挪；④就地断掉的引用（`f -5 -1 -1` 越过头）**保持断掉**，不造顶点；⑤`vt`/`vn` 各按自己的槽计数、正索引一个都不改写；⑥续行先接上、缩进先 `trimStart`（loader 看到什么就规范化什么）。`renderer.test.ts`：第一支保留（直接喂 loader 交出 NaN——坑 113 的钉子、也是"损伤在资产不在渲染器"的证词）；第二支改判——走渲染器加载路 ⇒ position 计数 = 6（两面 × 3）、分量全有限、**两条射线各命中 1 次**（新增射线打的正是以前丢顶点那个面）、`errors` 恰 0 条；第三支仍钉 Q119；第四支一字未动。
- ② **真实资产库（本轮最重的一层，因为没有真机层）**：210 份 OBJ 逐份喂进**真实的** `normalizeObjSource`（`vite-node` 直接 import，不是副本）再交真实的 `OBJLoader`：204 份逐字节相同、6 份被规范化；那 6 份非有限分量 8100/8100/41040/18306/6264/3888 → **全部 0**（每个恰是 §12.36 ② 坏引用数 × 3 分量 ⇒ 两轮账对得上）；**顶点数一个没变、其余 204 份的盒一个没变**。触发判据的宽窄实测差 22 份：按"面引用了尚未声明的顶点"⇒ 6 份；按"任何声明出现在第一条面之后"⇒ 28 份——多出的 22 份是合法分块资产（绝对索引、就地解析完全正确）⇒ 松判据会白白重写 22 份好资产（**坑 115**）。库里事实：0 份负索引、0 处续行、0 处缩进 ⇒ ① 的③/⑥ 是给以后的资产买的保险。
- ③ **真浏览器（本轮不起栈，按用户裁定「不起栈，只交离线证据」）**：本轮改的是一个纯字符串函数 + 它在 `parseAsset` 的一次调用；函数输出对真实 210 份逐份验过（②），验证走真实的 `OBJLoader`→`BufferGeometry`→`worldAabb()`/`Box3Helper`，真机能新增的信息量只在 GPU 侧而 NaN→有限在几何数据上已判完。留给下一轮的账：那 6 份 robotiq 零件应画得更完整（一份 pad 从 300 面回到 1200 面）、`#toast` 不再有那条告警——这两条是本轮唯一没有像素证据的判据。
- ④ **负控制与接口面**：三支负控制实跑，跑法"原地改→只跑那一个文件→从备份逐字节还原→`md5sum -c`"（worktree 起不来，坑 116）：A 调用改回 `loader.parse(source)` ⇒ 2 支红；B 删负数改写 ⇒ 1 支红；C 放宽触发判据 ⇒ 1 支红。零新键（88）、零新路由、`types.ts` 不动 ⇒ 不需重启。**OBJ 之外四种格式与 URDF 那条路一行没碰**——规范化只挂在 `parseAsset` 的 OBJ 分支。实测：vitest **607/18**、build 891.47 kB、audit_parity 62/62、`features_local: 88`。决议 §13.55，卡片 FEAT-2026-11-04-H。

12.38 摆放：随机放不下时确定性扫遍桌面 + OBJ 按真实顶点定碰撞盒（§4.6.1 的 **Q144**）：两个 bug。**bug 1 治"放弃得太早"**：原先 64 次随机全落空就报"放不下"而桌面明明有空位 ⇒ 两段式：随机 64 次全空才按 `scanSteps`（缺省 **32**）把内接矩形切 `(steps+1)²` 个格点**从中心向外**确定性扫（`centreFirstGrid`，排序固定 ⇒ 落的是离中心最近的能放点、可被单测钉住），两段都空才拒绝。**bug 2 治"碰撞盒不是画出来的那个盒"**：`model 23` 杯子元数据 AABB 与发货网格不在同一轴系（X/Y 互换）⇒ 占位盒相对网格转 90°，两只"纸上只贴边"的杯子屏幕上叠约 3.4 cm ⇒ OBJ 模型的 footprint 改用它**网格真实顶点**量出的盒（`objVertexBounds`；`readAsset` 对 OBJ 多读一次带缓存文本），元数据错了也以网格为准。
- ① **单测（+8，全套 **615/18**）**：a `objVertexBounds` 四支——只取 `v` 行极值/`vt` `vn` 不当位置/与语句顺序无关（不需先过 `normalizeObjSource`）/非有限跳过、无可用顶点回 `null`。b OBJ override 三支——`readAsset(source,"model",23)` 的盒等于网格盒**不等于**元数据盒、`source.reads` 恰 `["model:23"]`（那次文本读真的发生了）；URDF 模型仍走元数据盒；完整 `placeAsset` 连摆两只 ⇒ 两盒 `!overlaps`（接线支）。c bug 1 两支——旧支加 `scanSteps:0` 钉第一段；新支"随机全落空但桌面有一处空位 ⇒ 第二段扫到"。负控制：删第二段整块 ⇒ sweeps 支红；注掉 override ⇒ b 一/三支红。
- ② **真实数据（bug 2 根因对着出厂库逐份验过，本轮最重的一层）**：`data/models` 25 份顶层 OBJ 逐份比元数据 AABB 与网格盒 ⇒ **9 份对不上**（4 basket、8 gum_bottle、9 cream、14 mug、18 coffee_table、21 test_tube、22 toy_panda、23 mug、24 mug），多数 X/Y 互换、`18`/`21` 是 up 轴错位；`model 23` 元数据与网格 Z 逐字相同、X/Y 互换，正是用户复现穿模那一份。bug 2 不是猜的，是对着用户拥有的那份出厂数据量出来的（一次性脚本收尾删除）。
- ③ **真浏览器（未跑，坑 90）**：留给用户下次起栈看两条——该落空位的不再报"放不下"；连摆两只 `model 23` 不再穿模。
- ④ **接口面与代价**：零新键（88）、零新路由、`types.ts` 相同 ⇒ 不需重启。**代价写在正文**：摆放对每个不同 OBJ 模型多读一次带缓存文本，与本轮之前"道具摆放不发文件请求"的设计相反——接受它，因为元数据 AABB 对 9/25 是错的、网格是唯一可复核的真相；同型号只读一次。实测：vitest **615/18**、build 892.37 kB、audit_parity 62/62、`features_local: 88`。决议 §13.57，卡片 FEAT-2026-11-04-I。

12.39 视口背景色可配置（§4.4 的 **Q145**）：`config/constant_config.json` 新增 `viewport_background_color`（默认 `#000000`，`#RRGGBB`/`#RGB`）。纯前端数值常量，走 `shadows_enabled` 那条既有常量链：配置文件 + gateway 内置默认（逐字相同）→ `RuntimeConfig` 两份 `types.ts` → `logic.ts:viewportBackground()` fail-open 解析 → `renderer.ts:setBackgroundColor()` → `main.ts` 接线。
- ① **单测（+4，全套 **619/18**）**：a 解析两支——合法值（两种长度/大小写/首尾空白）原样通过；缺字段/`null`/非十六进制串/长度不对/含空格/非法类型 ⇒ 一律回默认黑。b 渲染两支——`#123456`/`#0f0` 各落 hex（两种长度都解析）；**只改清屏色不动距离雾**（`scene.fog` 原封不动——背景与雾是两件事，刻意不合并）。负控制：注掉正则 ⇒ a 第二支红（`"black"` 被放行）。
- ② **两处源必须逐字一致（本轮最重的一层）**：`config/constant_config.json` 与 `local_gateway.py:DEFAULT_CONSTANT_CONFIG` 同步新增；实测 `load_constant_config()` 与内置默认逐键相同、`test_gateway_routes.py` 的"出厂文件 == 内置默认"与 `runtimeConfig` 两条断言均过。只改一处会让那条测试红。
- ③ **真浏览器（未跑，坑 90）**：留给用户——改非黑值并**重启 gateway**（坑 22，常量启动读一次）后视口与截图变那个色；深度/米制通道不受影响（传感器通道各自覆盖背景并还原）。
- ④ **接口面与作用域**：零新 feature 键（88）、零新路由；`RuntimeConfig` 新增可选字段 ⇒ 两份 `types.ts` 要动（坑 6 适用）；**非默认值需重启 gateway**，但解析 fail-open 到黑，不重启照常渲染只是拿不到新颜色。**作用域一条写在正文**：本键只改 `scene.background`，不动 `scene.fog`——把雾一起改会连带影响远景衰减观感、超出这条键的承诺范围。实测：tsc 干净、vitest **619/18**、build、audit_parity/parity_inventory 见卡 FEAT-2026-11-04-J。决议 §13.58。

12.40 相机悬浮窗：相机多了会滚而不是被裁掉（§4.8.9 的 **Q146**）：纯 CSS 布局缺陷——`.camera-wall` 的 `max-height` 只夹窗自己的盒，grid 隐式 `auto` 行仍按 max-content 取高 ⇒ 相机一多，格子那行从窗盒底边溢出、被 `.canvas-wrap` 的 `overflow:hidden` 裁掉，而 cells 自己 `scrollHeight === clientHeight`，既有 `overflow:auto` 永远不触发。修法一行：`grid-template-rows:auto minmax(0,1fr)`。
- ① **单测（面不增不减）**：不动任何 `.ts`，vitest 逐位仍 **619/18**——那个"不变"本身就是判据（Q120 同形状）。
- ② **接口面（不增不减）**：零新键（88）、零新路由、`types.ts` 相同 ⇒ 不需重启；audit_parity 62/62、`features_local: 88`。派生 CSV/probes 只带出用户本地运行时值与既有漂移（probes `keys:16→17` 是 Q145 那条常量自快照以来的既有漂移、不是本轮）⇒ 整份不提交。
- ③ **真浏览器（本轮跑了，量的是真实样式表的前后两份）**：不走 smoke（坑 90）；隔离页 `<link>` 真实 `styles.css`、按真实 DOM 链复刻、钉 1280×800、每格真 `<canvas 256×192>`，同一 harness 跑两遍只差所 link 的样式表（改前 = `git show HEAD:`）。改前：N=0～3 窗随内容长不被裁，N≥4 窗盒夹死 647 而 cells 行长到内容高、`scrollHeight===clientHeight` ⇒ 超长、被裁、没滚动条（复现用户那条 bug）。改后：N=0～3 窗高与改前逐档差 < 1px 子像素（紧凑态无实质变化），N≥4 cells 行被 `minmax(0,1fr)` 收到 607、`scrollHeight > clientHeight` ⇒ 滚动条出、内容不再被 `.canvas-wrap` 裁。**判据是几何不是观感**（滚动条出现 == `scrollHeight > clientHeight`；窗不超长 == 窗与格底边都 ≤ wrap 底边）。遗留一条：真栈上挂满相机的窗与拖动夹持上界，待用户下次自己起栈扫一眼。决议 §13.59，卡片 FEAT-2026-11-04-K。

12.41 臂的盒止于臂：一物一盒是量测的口径，不只是画几个框（§4.8.6 的 **Q148**）：用户报「机械臂的显示包围盒明显大于其实际包围盒」。根因不在算式而在**量的范围**：`RobotAttachment` 与 `Transform.parent` 都把子物体挂进父物体的**场景图子树**，而 `worldAabb(objectId)` 递归整棵子树 ⇒ 臂的盒把它的夹爪、夹爪上的腕部相机一起并进来看作"臂"。实量（出厂 apple 现场结构：UR5e + Robotiq 2F-85 + D435i，按场景声明的关节位姿、真实 OBJ 顶点）：父盒 **+X 面比臂自己的真实轮廓空出 6.27 cm**（其中 **5.25 cm** 框住的是挂在腕上的夹爪与那台腕相机、1.02 cm 是角点量测的余底）、**-X 面 2.21 cm**、整盒体积 **+16%**。修法一处：`expandGeometry` 的递归走到 `child.userData.objectId` 与本物体不同的节点就**停** —— 用的是 `loadModel` 早就给每件内容盖上的那枚身份标志（`docs/场景文件描述.md` §4.8 那条"补挂必须排在 traverse 之后"因此从"别把 id 盖错"升成"量测的物界"），**没有为新口径开新口子**。
- ① **单测（`renderer.test.ts` +1 describe +3 例，全套 **652/20**）**：夹具是两台真会走的 URDF（臂两条 link 各带一只盒、相隔 0.5 m；手一条 link 一只盒在挂载系 +Y 0.4 m 处），挂载经**真实加载路**（`loadModel` → `built.traverse` 盖章 → `mountOnLinks`），不是手搭的父子关系。三条钉的是三件事：**a** 夹爪确实在臂的子树里（`nodes.get(2).parent === wrist_3_link`，这条不成立则后两条免费）＋臂的两条 link 仍在**同一个**盒里（§4.8.6 第 2 条前半句不许被改坏）＋手自己的盒中心**不在**臂盒里、**在**整份世界盒里；**b** 把手放大 10 倍 ⇒ 子盒尺寸 ×10 而父盒 `equals` 逐字不变（同一个场景图事件、两盒相反的答案，旧的走法两条都做不到）；**c** 三层链（臂 → 手 → `Transform.parent` 挂的 model 道具）里道具既不出现在臂盒也不出现在手盒，但**它自己的盒与整份世界的盒都还握着它**（父级不量 ≠ 没人量）。
- ② **负控制**：只摘掉物界判项那一条 `continue`（其余一字不动）⇒ 三条新例**同时红**、既有 126 例照旧绿；改回后 `renderer.ts` 与改前逐字节一致。
- ③ **接口面（不增不减）**：零新 feature 键（88）、零新路由、两份 `types.ts` 一字不动、不动 gateway 与协议 ⇒ `audit_parity.py` 的探针**不增不减**。本轮唯一对外可见的口径变化是 `aabb_world`：带挂载的物体（臂／手／腕上道具）的盒**变小到它自己那一份**，没有子物体的物体逐字不变 —— 这正是用户报的那件事。
- ④ **作用域三条**：**保留** per-part 角点量测（实测仍比真实轮廓每面空 1～2 cm、体积约 6%）—— §4.6.1 的碰撞集要的正是"宁大勿小"（**Q144** 立这条的理由），换成逐顶点会把 250 ms 那趟 `worldAabb` 乘以顶点数，两条都不在本轮承诺里；**摆放碰撞集不受影响** —— `armFamilyIds` 早在 **Q137** 就把臂族整体移出障碍、腕挂的 `type:model` 道具按**它自己的盒**留在集里，这条改动既没放宽也没收紧它；**两道既有判项一字未动** —— 编辑层排除（§4.4/Q50）与 `isFiniteBox`（§4.5/Q141）各自管自己的尺度。实测：tsc 0 错、vitest **652/20**、build **894.74 kB**（+0.08 kB）、CSS 21.85 kB 不变；`audit_parity.py` **61/62**，唯一缺口 `the cached world serves callers with no browser` 是 **Q147** 删掉 `/getWorld` 缓存回退后该探针仍按旧口径写着的既有漂移（本轮未碰 gateway），非本轮引入。真机走查未做（坑 90：`browser_smoke.cjs` 仍卡在 §12.4 身份闸）。决议 Q148（§13 登记册已冻结在历史档案，与 Q147 同样就地编号），卡片 2026-11-06-臂盒止于臂。

12.42 盒止于臂以后还差一步：**怎么量**——逐 link、逐顶点，外加一枚只管形状的量（§4.8.6 的 **Q149**）：用户第二次报同一件事——「包围盒依然大于机械臂本体」。Q148 掐对了**量谁**，屏幕上仍偏大，病根换到**怎么量**，且这次是两条叠在一起：① 旧口径取每份网格**局部 AABB 的八个角点**乘世界矩阵再并 —— 零件是在它自己 link 的轴里建模的，关节一转就把它转出那些轴之外，那八个角指的全是空处，这条**永远不可能靠把角点算得更聪明修好**（凸包角点在旋转下的像不构成旋转后凸包的角点）；② 改成逐 link 以后立刻踩到第二件事：**URDF 的场景图是嵌套的**（子 link 挂在关节节点下、关节节点挂在父 link 下），从 `base_link` 走下去路过全部子孙，于是"每 link 一只盒"会画成 35 只同形的整台臂套在一起 —— **第一轮跑红在生产代码上，不在测试上**。修法两处：`worldLinkBoxes(objectId)` 对每条 link 单独走一趟、递归**止于其它 link 的根**，量的又是**逐顶点扫出的世界盒**（`exactWorldBox`，按（网格、`matrixWorld`、几何）`WeakMap` 缓存）；`worldAabb()` **字面上**就是它的并集（∪ 不落在任何 link 里的那部分几何），所以新那枚显示常量改不了量测。出厂 apple 现场实量（UR5e，20 份网格 / **358,618** 个顶点，按场景声明的关节位姿）：角点并盒 `[0.2854, 0.9979, 0.4354]` 对顶点并盒 `[0.2854, 0.9910, 0.4031]` —— **x 那两个面一字不差**，松的是 **−Z 1.02 cm、+Y 0.69 cm、+Z 2.21 cm**，体积 **0.12398 → 0.11398 m³（−0.0100，≈8.8%）**；逐 link 拆开更能指出病在谁身上：`upper_arm_link` 两轴各松 **4.4 cm**、`forearm_link` **4.1 / 5.0 cm**、两根腕 0.7～1.0 cm，而 `base` 与 `shoulder_link` 两态**逐字相同** —— 关节把哪条 link 转得越狠，那一只盒就越空。
- ① **单测（`renderer.test.ts` +1 describe +7 例＝136 例、`logic.test.ts` +2 例＝90 例，全套 **661/20**，比 Q148 的 652 多 9）**：夹具是可**手算**的一根圆柱 —— 截面 24 面（15° 一档）、绕自身轴转 **45°**（45° 恰是那组面的对称），于是顶点量得 `2R`、角点量得 `2√2·R` 是两个能钉死的数（用方块不行：它转了也紧；用球不行：只能靠细分运气）。七条钉七件事：**a** 转过的零件按顶点量（`size` 三轴 `2R/LENGTH/2R`，同一份零件按角点量两轴各松 `>0.05`、第三轴逐字相同 —— 那个"没转的轴"正是判据的对照组）；**b** 一条 link 就是一条 link：**父 link 的盒不含子 link 的中心、子 link 的盒含着它**（嵌套 cut 的 durable 判据，写成几何关系不写成常数，同**坑 120 ③**）；**c** `worldLinkBoxes` 的并 `equals` `worldAabb()` —— 屏幕分开画的与调用方一起报的是同一批数；**d** 没有几何的 link（`visuals: []`）什么都不画，不是原点一只零号盒；**e** 形状切换的对账：`perLink:true` ⇒ `drawnLinks` 恰为那三条 `<objectId>/<link>`、每台 URDF 的**并盒从屏幕上摘掉**（`helpers(1) === false`）、每只 link 盒的 `box` 与 `worldLinkBoxes` 逐字相同，切回 `false` ⇒ 那只盒又出现且等于存下来的并；纯 model（物体 9）在两态都是一只盒；**f** 常量接到画它的那一层（按**坑 25** 的路子钉 `main.ts:renderBoxes()` 的源码结构，两枚判据都在其中，负控制见下）；**g** 关节一转缓存立刻作废（`after.equals(before) === false`，且新盒含腕部 holder 的世界位置、旧盒不含 —— 否则的表现是"臂动了盒不动"）。挂载仍走**真实加载路**（`loadModel` → `built.traverse` 盖章 → `mountOnLinks`），`logic.ts` 那两条钉 `urdfLinkBoxesEnabled`：默认 `false`，缺字段／`null`／`"true"`／`"false"`／`0`／`1`／`NaN` 一律 `false`（fail-open，同 §9.1 全表）。Python 侧 `tests/test_gateway_routes.py` 钉**出厂文件里这个键就是 `False`**（13 例 OK，同一次跑还证明 `config/constant_config.json` 与 gateway 内置默认逐键相同）。
- ② **负控制（三支，各红且只红一支）**：**α** 摘掉逐 link 递归的 `skipRoots` ⇒ 只红 a 那条，`size.y` 实得 **1.35 = 整台臂**（这就是第一轮撞到的缺陷本身）；**β** 把 per-link 改回角点口径（`exact=false`）⇒ 只红同一条，`size.x` 实得 **0.28284 = 2√2·R**；**γ** 把 `worldAabb()` 的 URDF 分支改成"另走一趟子树角点量测"（即两条各算一遍的口径）⇒ **只红 c**、其余 **135 条全绿** —— 不变量的钉子精确落在"同源那一句"上，改坏别处不会替它红。改回后 `renderer.ts` 与备份 `md5` 逐字节一致。
- ③ **接口面（常量面 +1，键面不动）**：`config/constant_config.json` **16 → 17** 键（`urdf_bounding_box_per_link`，紧邻 `shadows_enabled`）、gateway `DEFAULT_CONSTANT_CONFIG` 同步 +1、两份逐字节相同的 `types.ts`／`contracts/types.ts` 的 `RuntimeConfig` 各 +1 可选字段（**坑 6**）；零新 feature 键（`features.json`／`feature-inventory.csv` 一字未动，仍 87 键）、零新路由、零协议变化、gateway 从不渲染只原样下发 ⇒ `audit_parity.py` 的探针**不增不减**。诊断出口仍是 `Viewport boxes` 那一条 `console.debug`，只加 `perLink`／`drawnLinks` 两个字段，不是第二个出口。`docs/parity-probes.json` 按上次裁决**整份不提交**（HEAD 16 对工作树 17 的漂移自 **Q145** 挂在那里，本轮也未重跑 `audit_parity.py` —— 它读的是用户正在跑的 8099，而那台跑的仍是改前的代码，重跑只会把快照写回旧数）。实测：tsc 0 错、vitest **661/20**、build **897.22 kB**（gzip 246.52，+2.48 kB）、CSS 21.85 kB 不变、`test_gateway_routes.py` 13 例 OK。真机走查未做（坑 90：`browser_smoke.cjs` 仍卡在 §12.4 身份闸）；本轮新增的 e 那条把"屏幕上画哪几只盒"做成可断言的读数（`boxReport()`），第一次不需要靠眼睛。
- ④ **作用域与成本**：**非 URDF 物体一行没改**（纯 model／道具／`Transform.parent` 挂上来的仍走那条角点路、一只盒）——用户那句"此修复应当对所有 urdf 类物体都一致"划的正是这个边界；**三道既有判项一字未动** —— 编辑层排除（§4.4/Q50）、NaN 口径（§4.5/Q141 与 §4.8.6/Q143：非有限顶点不进盒、`skipped` 照报）、物界判项（**Q148** 的 `objectId` 停）各自管各自的尺度，逐 link 的 cut 与逐物体的 cut 是同一形状、两件事；**四个消费者在两态逐字相同** —— `/get2DAABB`（§5.11）、摆放碰撞的 `sceneAABB`（§4.6.1）、`frame()`、选中描边，所以默认 `false` 不只是"少画点东西"，它是**与出厂画面逐像素相同**的那一个形状；**成本**：逐顶点只对 URDF 做，一台臂冷走一次 **6.9 ms**，250 ms 的刷新周期装得下。**不做**：不改 OLS 资产里 `<visual>` 的原点偏保守（那是资产侧的事，本轮只保证前端不再把它放大）、不给每 link 盒加拾取或交互（这一层是诊断辅助）、不加第二枚勾选框（§9.3 那条"整层的远程出口只有一个键"不许被稀释）。决议 Q149（与 Q147／Q148 同样就地编号，§13 登记册已冻结），卡片 2026-11-06-臂盒逐link量。

12.43 UR10 切换后支架与显示盒偏离承重底座（**Q150**）：UR10 的整机 metadata AABB 最低点是 `z=-0.03468528`，但按其声明关节姿态重建真实 URDF/OBJ 后，根部固定底座几何最低点为 `z=0`。旧 `pedestalPose()` 用整机盒底作承重基准，会让支架低 **3.47 cm**；修复从根 link 沿固定关节量 base visual 几何，遇 movable joint 即停止，能量出时以此定位支架，其他格式/坏资产回退旧值。整机并盒本身确实是当前弯曲姿态所有 link 的世界轴对齐并集，空域是并盒的固有部分；因此 `urdf_bounding_box_per_link`（Q149）默认改为 `true`，屏幕分 link 画、调用方仍取 `worldLinkBoxes()` 并集。UR10 实资产重建盒 `[−0.3727,−0.0920,0]` 到 `[0.2399,0.2631,1.2847]`；默认显示现在每 link 一盒，避免一只大盒包住关节间空域。
- ① **单测**：`placement.test.ts` 用空根 link、固定底座视觉体、低垂可动 link 证明支架量的是固定底座而不是手臂；未提交的本机 UR10 探针确认根部 min z=0、metadata min z=-0.03468528。负控制是把固定关节条件改成遍历所有关节时，夹具中的可动 link 会把基准压低 0.1 m，断言失败。
- ② **显示形状**：`logic.test.ts` 钉默认逐 link、显式 `false` 回整机盒和畸形值 fail-open；`renderer.test.ts` 已有两形状一致性判据，`worldAabb()`/`/get2DAABB` 并集不变。gateway 配置默认与 `config/constant_config.json` 保持一致。
- ③ **边界**：只读 OLS 的本地 UR10 资产目录；OLS HTTP 服务当时未运行。没有修改 OLS 文件。浏览器真机未跑。提交前验证记录见本任务卡。
12.44 快速连续添加导致物体消失（**Q151**）：库面板异步场景操作串行；Gateway 对同连接 state_sync 的身份检查、自动会话准备及 `record_scene` 串行，防止旧报告晚落盘覆盖新现场。
