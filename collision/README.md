# Collision 模块

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
│   ├── mesh.py
│   └── robot_urdf.py          # FCL URDF 的视觉几何碰撞回退
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

scene = load_scene("requests/commands.001/ur5_table_empty.json")
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
- `robot_urdf.py`：为 FCL 保留已有 `<collision>`；仅对没有碰撞定义的 link 从 `<visual>` 生成碰撞体。基础几何直接复用，网格经 `mesh.py` 凸分解并缓存，生成的 FCL 专用 URDF 不覆盖 OLS 资产。有惯性或活动关节、但缺少 collision 和 visual 的 link 会报错；无惯性的空固定坐标框架 link 允许保持无几何。

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

FCL 加载机器人 URDF 时应用视觉几何回退；该回退只作用于 FCL 模型。MuJoCo 仍按源 URDF 编译碰撞几何，资产没有 `<collision>` 的 link 不会因 FCL 回退而自动获得物理接触几何。

场景资源解析时，自动允许机械臂主体与安装基底的碰撞，适用于所有选定的机械臂型号。基底由 `Semantic.semantic_name=robot_pedestal` 或 `Semantic.category=pedestal` 标识；兼容旧场景对象名 `RobotPedestal`。规则合并到 `RobotModelRef.allowed_arm_environment_names`，由 FCL、MuJoCo 几何复核和 cuRobo 共用；不改变场景位姿，不豁免夹爪、持物或其他环境对象。

离散采样可能漏掉采样点之间的短暂碰撞；需要更高可靠性时，应缩小采样间隔或使用后端的连续碰撞检测能力。

## 6. 验证与限制

当前实现以 SimPlatform FCL 为主，MuJoCo 和 Bullet 属于可替换后端。不同后端的网格预处理、凸分解和接触算法可能产生差异，因此跨后端只保证接口和坐标约定一致，不保证每个网格接触结果逐点相同。

## 7. 场景碰撞检查

规划、执行前验证和运行时碰撞检查均使用场景及机器人资产解析出的原始碰撞几何。
机械臂、夹爪和环境物体保留各自精细碰撞模型；MuJoCo 的实时夹爪、动态物体和持物检查
读取当前构型。`--show-collision-geometry` 可查看仿真中的物理碰撞几何。

