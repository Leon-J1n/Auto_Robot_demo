# Collision 模块

机械臂默认从 URDF 的各 link `<visual>` 自动生成碰撞几何。资源加载时合并同一
link 的视觉部件并应用 `origin` / `scale`，简化输入面数，再生成 CoACD 凸分解；
不会跨关节合并，不修改源 URDF、视觉网格、关节或惯性。没有 visual 的 link
保留原有 collision；独立夹爪仍沿用自己的碰撞资产。

默认预算：每个 link 输入最多 3000 个三角面，输出最多 8 个凸块，每块最多
64 个顶点。开放表面会经过 CoACD 预处理，结果是近似几何；预算较紧时不能
保证凹陷、薄壁或细节与视觉网格完全一致。球代理保守覆盖的是生成的凸块，
不是原始视觉网格。

凸块预算由生成器统一收敛：CoACD 先按凹形阈值分解，超出块数预算时合并
最近的凸块；合并后的顶点数仍超预算时使用该块的外包盒。合并结果覆盖
合并前的凸块，但可能填充空隙，因此较紧的预算会增加保守程度。

派生 URDF 缓存于 `.cache/collision/robots/<hash>/robot.urdf`，凸块存于
`.cache/collision/<hash>/`。缓存包含内容校验和，网格、变换、缩放、参数或
生成库版本改变后重新生成。`manifest.json` 记录各 link 的来源和凸块数量。
FCL 和 MuJoCo 使用同一份派生 URDF；显示仍使用原视觉资产。

可在场景机械臂的 `RobotModelRef.collision_geometry` 中设置：

```json
{
  "mode": "auto",
  "max_input_faces": 3000,
  "max_convex_hulls": 8,
  "max_vertices": 64,
  "threshold": 0.05,
  "exclude_links": [],
  "package_paths": {}
}
```

`mode: "auto"`（默认）在 URDF 为所有视觉链提供 authored collision 时直接使用原始
collision；只有碰撞几何不完整时才生成 visual-derived collision。`mode: "authored"`
强制使用原 URDF collision，`mode: "visual"` 强制使用视觉网格生成碰撞。`exclude_links` 为明确不参与碰撞的
link 名单（例如专用装饰 link），名称拼错会报错。`package_paths` 将
`package://` 的包名映射到本地绝对目录；普通相对路径按源 URDF 目录解析。

可通过 `link_overrides` 仅提高局部复杂关节的预算，例如
`"link_overrides": {"link5": {"max_convex_hulls": 32, "max_vertices": 256,
"max_input_faces": 6000, "threshold": 0.03}}`。其余 link 保持默认值。
尚未装配 OLS 资产的场景也可声明只含 `collision_geometry` 的 `RobotModelRef`；
资源解析会补齐 URDF 和安装配置。

规划时先使用球代理/包围盒批量粗筛，仅对重叠候选调用源凸几何距离精检，
覆盖机械臂自碰撞、环境及携带物体。沿用原来的允许碰撞对和安全距离；
同一刚体内部、直接相邻 link 的过滤不会因切换碰撞资产而取消。

安装接触由运行时识别，无需给场景增加字段：已有语义或对象库引用表明是
静态机械臂安装底座、且在机械臂基座下方水平对齐时，仅豁免 URDF 根节点
经 fixed 关节连接的固定基座几何与该底座的接触。活动连杆、夹爪、携带物体
及普通桌面障碍物不享有此豁免；多个候选底座时不自动推断。相同规则应用
于 FCL、规划代理的源几何策略和 MuJoCo 物理接触过滤。

## 1. 职责与边界

`collision` 为规划阶段提供统一的碰撞检测能力。它接收规范化场景、机器人模型和关节轨迹，返回统一的碰撞结果；不负责任务解析、运动规划、轨迹执行或仿真运行时控制。

碰撞检查只在规划和规划结果复核阶段使用。轨迹进入 `execution` 后，由执行后端推进物理状态，不再调用碰撞规划器。

边界规则：

1. 任务和场景由 `requests/<request>/` 管理，碰撞模块只接收场景文件或 `CollisionScene`。
2. OLS `data` 负责机器人、夹爪和物体资产；碰撞模块负责解析和使用，不复制资产目录。
3. 场景格式保持唯一。格式升级必须先提供版本迁移，再交给 `scene_loader.py`。
4. 后端差异只能存在于 `backends/`，不能泄漏到 `demo`、`execution` 或规划器。
5. 采样分辨率、安全距离、允许碰撞对和缓存容量通过配置传入，不散落在调用方。

## 2. 目录结构与职责

```text
collision/
├── api.py                     # 稳定公共接口与后端工厂
├── scene/                     # 场景和机器人模型
│   ├── scene_loader.py
│   ├── scene_model.py
│   └── robot_model.py
├── assets/                    # 资产解析和网格处理
│   ├── asset_resolver.py
│   └── mesh.py
├── common/                    # 后端共享校验和缓存
│   ├── validation.py
│   └── cache.py
└── backends/                  # 具体碰撞算法适配器
    ├── simplatform_fcl.py
    ├── mujoco.py
    └── bullet.py
```

## 3. 数据流

```text
scene.json / planned trajectory
        ↓
scene_loader.py → CollisionScene
        ↓
api.py → selected backend
        ↓
check(configuration) / check_path(trajectory)
        ↓
统一 CollisionResult
```

## 4. 对外接口

调用方应优先使用稳定接口：

```python
from collision import CollisionChecker, load_scene

scene = load_scene("requests/commands.001/ur3_table_empty.json")
checker = CollisionChecker.create("simplatform_fcl", scene=scene)
result = checker.check(joints)
```

- `load_scene(path)`：读取唯一格式的场景 JSON 并构造 `CollisionScene`。
- `CollisionChecker.create(name, scene=...)`：按名称创建后端检查器。
- `check(joints)`：检查单个关节构型。
- `check_path(trajectory)`：检查关节轨迹及采样参数。
- 返回值必须使用统一的碰撞结果结构，不能暴露后端内部对象。

### 各层职责

### `scene/`

- `scene_loader.py`：读取场景 JSON，检查版本、文件和 `objects`。
- `scene_model.py`：定义后端无关的 `CollisionScene`。
- `robot_model.py`：定义 URDF、关节、base link 和 tip link 元数据。

这里不执行碰撞检测，也不加载具体仿真器。

### `assets/`

- `asset_resolver.py`：从 OLS `data` 目录解析机器人和模型文件。
- `mesh.py`：校验 OBJ/STL 网格，执行凸分解和内容寻址缓存。

### `common/`

- `validation.py`：校验关节向量、采样参数和碰撞配置。
- `cache.py`：提供有界缓存基础类。

### `backends/`

- `simplatform_fcl.py`：当前主用的 SimPlatform FCL 后端。
- `mujoco.py`：MuJoCo 距离和接触检查后端。
- `bullet.py`：Bullet 适配器预留入口；未安装依赖时明确抛出 `NotImplementedError`。

新增后端只需增加 `backends/<name>.py` 并在 `api.py` 注册，不修改场景解析和规划代码。

## 5. 配置、输入与输出

安全距离、采样分辨率、允许碰撞对和缓存容量由调用方配置。缓存键和失效策略由具体后端实现，但不能改变统一接口语义。

离散采样可能漏掉采样点之间的短暂碰撞；需要更高可靠性时，应缩小采样间隔或使用后端的连续碰撞检测能力。

## 6. 验证与限制

当前实现以 SimPlatform FCL 为主，MuJoCo 和 Bullet 属于可替换后端。不同后端的网格预处理、凸分解和接触算法可能产生差异，因此跨后端只保证接口和坐标约定一致，不保证每个网格接触结果逐点相同。

## 7. 统一简化规划代理（sorting 场景试用）

`collision/proxy.py` 维护独立于视觉／物理网格的规划碰撞模型。
所有场景解析时默认启用；场景可通过 `planning_collision_proxy` 覆盖参数，
显式指定 `enabled: false` 可恢复原检查链路。默认值位于
`execution/resources/resolver.py` 的 `DEFAULT_SCENE_SETTINGS`，已有场景无需逐个修改。

```json
"planning_collision_proxy": {
  "enabled": true,
  "sphere_cell_size": 0.035,
  "container_names": ["Tray", "Basket"],
  "batch_size": 32
}
```

`GraspCollisionChecker` 绑定实时后端后启用代理。Direct、IK 候选的普通
`check/check_path`、cuRobo 导出和最终路径复核均读取它；不在每次规划后再强制
运行原精细 FCL。原检查器仍用于启动一致性检查、部分专用抓取探针和离线对照。
仿真接触／动力学几何保持不变。`--show-collision-geometry` 仍显示物理几何，不是代理。

代理规则：

- 仅合并同一实际 body、且对所有计划几何具有完全相同豁免／距离规则的碎片；
  source IDs 保留在 `source_geoms`，不按旧编号直接复制规则。
- 机械臂圆柱用轴向覆盖球；普通持物用包络盒的网格覆盖球。环境使用有向盒体；
  指定的开口容器按底面及四侧分组，每个源凸块完整归入一个包络板，保留内部空间。
- Robotiq 的细小机构空隙不能直接用大盒填充。`proxies/robotiq_2f85.json` 保留了
  上一版已验证的 VOXEL 球拟合，并按形状／网格内容 SHA256 匹配。更换资产后不匹配
  会报错，需要重新制作和验证模板；不依赖本机 `.cache` 才能读取现有模板。
- 新增盒／球为外包络，但**保留的夹爪球拟合仍是近似**，不能声称完整覆盖精细网格。
  `sphere_cell_size` 是覆盖网格尺寸，不是允许误差；包络额外占据空间与请求间距分开。
  3 mm 请求阈值保持，不能据此承诺相对原网格恰好 3 mm。过窄通道可能被代理拒绝。
- CPU 路径检查先计算独立 MuJoCo FK，再按几何组包围球保守筛选，批量计算球—球、
  球—盒距离；GPU 直接使用同一批球心、半径和盒体，不重新拟合。
- 有限圆柱的轴向覆盖球会突出平端面。CPU 对圆柱—环境的代理碰撞候选，使用当前
  候选关节姿态下的原始 MuJoCo 凸几何距离复核，并应用源几何的冻结阈值／豁免规则。
  阈值比较容差为 1 nm，恰好达到安全距离视为通过。其他代理对仍沿用球／盒检查；
  cuRobo GPU 仍使用外包络，可能比 CPU 更保守。
- Direct 一次规划内共享场景快照，进入和退出时各做一次完整状态校验。路径缓存有界，
  场景、策略、夹爪构型、抓取／释放或代理设置变化即失效，校验期间状态漂移会拒绝结果。
- 代理的几何／碰撞对编译与逐帧状态分开缓存。仅 `qpos`、mocap 或场景同步序号变化时，
  重新计算当前机械臂、夹爪、持物、世界盒体及导出运动链变换，清空旧路径结果，
  不重复简化网格或冻结相同规则。最多保留四份模型／策略编译结果；临时豁免恢复时，
  只有完整模型、抓取、阈值及规则签名匹配才复用编译，碰撞结果仍按当前状态重新计算。
  新规则／新形状未命中缓存时仍完整构建。`planning_proxy_cache` 诊断记录本次 direct
  的 `builds`、`structure_hits`、`state_refreshes`，方便区分首次构建与状态刷新。
- 支撑／lift 规则保留。lift 开始时在代理上重新测量已有接触距离，之后不随运动重置，
  不把精细网格的初始间距直接当作代理间距。释放后持物恢复为世界障碍物。

`planning_proxy` 诊断记录代理来源、源／代理几何数量、球数、盒数和包络说明。
关闭场景中的 `enabled` 并重新加载，即恢复原检查链路。

离线模板导出工具：

```powershell
python collision/proxies/export_templates.py --snapshot raw_runtime.json --robot-yaml validated_robot.yml --output reviewed_templates.json
```

输出只供审核和验证后替换；工具不会执行运动。CPU 测试为
`python -B -m unittest planning.tests.test_collision_proxy -v`。

## 8. 可复现碰撞资产交付包

生产环境可以选择一个经过审核的、内容寻址的碰撞交付包。包由独立的生成环境产生，必须同时固定生成代码版本、依赖版本、生成参数、输入资产 SHA256 和审核状态。运行时只读取包内已经审核的 OBJ/URDF，不在本机重新做 CoACD/网格简化，也不会在包缺失资产时回退到 `.cache` 或现场生成。

设置：

```bash
export COLLISION_BUNDLE_ROOT=/绝对路径/collision_generation_delivery_20261122_v1
```

该目录应包含 `reference_bundle/bundle.json`、`reference_bundle/blobs/`、`reference/cache_evidence/` 和 `reference/local_fingerprint.json`。上线前先运行：

```bash
python tools/verify_collision_bundle.py "$COLLISION_BUNDLE_ROOT"
```

只有 `review_status=verified` 的包会被默认加载。交付包若仍标记为 `local_success_reference_unreviewed`，只能在隔离复现时显式使用：

```bash
export COLLISION_BUNDLE_ALLOW_UNREVIEWED=1
```

这个开关不会把候选包变成生产资产；审核完成后应移除该环境变量并重新运行校验。包的每个凸块和缓存证据都按 SHA256 校验，任何缺失或篡改都会 fail closed。

新资产的标准流程是：固定 Docker/依赖和生成器 commit，收集 OLS 源文件及哈希，生成 manifest 和凸分解结果，运行独立验证器，再由控制端审核几何、碰撞对和回归场景，最后把 `review_status` 改为 `verified`。不要直接覆盖已有 `.cache`。
