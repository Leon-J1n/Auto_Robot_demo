# Collision 模块接口说明

## 模块职责

Collision 模块使用 supplied SimPlatform 的 FCL 后端进行规划阶段碰撞检测：

- 加载 JSON 场景；
- 加载机器人 URDF；
- 加载环境 URDF、Mesh 和基本几何体；
- 检查机器人自碰撞；
- 检查机器人与环境的碰撞和安全距离；
- 检查整条关节路径；
- 更新场景实体位姿和允许接触策略。

规划碰撞检测使用：

```python
Simulator(physics="none", collision="fcl")
```

## 主要入口

```python
from collision import SimPlatformFCLCollisionChecker

checker = SimPlatformFCLCollisionChecker(
    runtime=runtime,
    scene_file="assets/scenes/fcl_scene.json",
    robot_urdf="assets/models/robots/30/ur5e_arm.urdf",
)
```

默认场景为 [assets/scenes/fcl_scene.json](../assets/scenes/fcl_scene.json)。也可以通过环境变量覆盖：

```powershell
$env:FCL_SCENE_FILE = "C:\path\to\scene.json"
$env:FCL_ROBOT_URDF = "C:\path\to\robot.urdf"
$env:SIM_PLATFORM_LIBRARY = "C:\path\to\sim_platform_c.dll"
$env:SIMPLATFORM_PYTHON_ROOT = "C:\path\to\SimPlatform-main\python"
```

场景 JSON 的环境物体需要通过 `AssetRef`、`Collider` 或支持的 primitive 描述碰撞几何。只有 `MeshRenderer` 的对象不会自动成为碰撞体。

## 关节向量约定

Collision 模块对外接收项目的六维 UR5e 关节顺序：

```text
shoulder_pan_joint
shoulder_lift_joint
elbow_joint
wrist_1_joint
wrist_2_joint
wrist_3_joint
```

SimPlatform 可能返回不同顺序，并把固定关节 `attachment_site_fixed` 计入关节列表。适配器会自动完成顺序映射并为固定关节填零，上层接口始终使用六维向量。

## 单点检测

```python
result = checker.check(
    q,
    safety_distance=0.01,
)
```

输入：

```python
q.shape == (6,)
```

输出是 `CollisionResult`：

```python
result.in_collision       # bool
result.minimum_distance   # float
result.contacts           # tuple[dict]
```

接触记录通常包含：

```python
{
    "geom1": "wrist_3_link",
    "geom2": "Table",
    "distance": 0.0,
    "self_collision": False,
}
```

## 路径检测

```python
result = checker.check_path(
    waypoints,
    resolution=0.04,
    safety_distance=0.005,
)
```

输入形状：

```python
waypoints.shape == (N, 6)
```

模块会对相邻路径点按 `resolution` 插值，并逐个构型调用 FCL 检测。路径规划器 `DirectPlanner` 和 `CollisionFreePlanner` 都通过这个接口验证轨迹。

## 场景状态和接触策略

更新实体位姿：

```python
checker.set_entity_transform(
    "Apple",
    position=(0.1, 0.0, 0.82),
    quaternion_xyzw=(0.0, 0.0, 0.0, 1.0),
)
```

允许机器人接触指定目标：

```python
checker.set_allowed_target_names({"Apple"})
```

多工作线程使用独立 SimPlatform world 时，只复制策略，不复制实体句柄：

```python
worker_checker.copy_collision_policy_from(main_checker)
```

## 资源释放

```python
checker.close()
```

## 与 Kinematics 的边界

Collision 不负责 FK、Jacobian 或 IK，也不会调用 `KinematicsService`。它只接收 Kinematics 或路径规划器产生的关节角：

```python
ik_result = kin.solve_one(target, seed=q_current)
collision = checker.check(ik_result.solution)
```

典型规划流程是：

```text
目标位姿
  ↓
Kinematics.solve_one / solve_all
  ↓
候选关节角 q
  ↓
Collision.check(q)
  ↓
路径规划
  ↓
Collision.check_path(waypoints)
```

Kinematics 和 Collision 必须使用一致的 URDF、关节顺序、基座位姿和工具安装关系，否则两边计算的机器人构型可能不一致。

