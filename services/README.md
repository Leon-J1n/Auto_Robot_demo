# ObjectLibraryServer 交互位姿接入

`object_library_server/` 保存用户提供的 ObjectLibraryServer 1.0.4-rel 服务、数据库、模型资源与原有文档。项目通过其原生 HTTP `GET /model?id=<模型ID>&uid=0` 查询元数据；没有用本地 JSON 查询替代服务。接入层仅使用读取接口。

## 运行

在项目根目录执行以下命令，自动在本机空闲端口启动服务，任务结束后关闭本次启动的服务：

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo\skill_command\ols_grasp.commands.json `
  --start-object-library `
  --library "libraries\simplatform\bin\sim_platform_c.dll" `
  --output tests\output\ols_grasp `
  --viewer
```

也可先单独运行 `python -m services.run_object_library --port 8000`，再把上述 `--start-object-library` 换成 `--object-library-url http://127.0.0.1:8000`。外部启动的服务不会随任务关闭。省略这两个参数时，读取场景的 `object_library.base_url`，默认本机 8000 端口。

## ID 与数据流

测试场景为 `assets/scenes/ols_grasp_test.json`，在 `assets/catalog.json` 注册；保留原来的 move → home 测试场景。

```text
commands: move.parameters.target = 101
  → 场景 object_id_in_scene = 101 的 Apple 实例
  → MetadataRef.object_id = 1（库中的 apple 模型）
  → 后端当前物体世界位姿 + HTTP 查询模型 1 的 grasp_properties.poses
  → 世界系夹持中心位姿 → 控制 TCP 位姿
  → demo 搜索接近点并验证到目标的直线路径
  → demo 生成两条普通 Move 请求：到接近点 → 保持姿态直线到目标
  → 现有 EAIK / Jacobian、FCL、MoveSkill / Planning
  → 关节轨迹 → MuJoCo 实际执行 → 校验夹持中心到达误差
```

命令示例：

```json
{
  "command_id": "grasp-001",
  "skill_name": "move",
  "parameters": {"target": 101, "planning_method": "joint"}
}
```

`target` 是场景实例 ID，不能填写模型 ID 或物体名称代替它。同一模型可以有多个场景实例。实例的组件需包含 `MetadataRef`，例如：

```json
{
  "component_type": "MetadataRef",
  "properties": {"type": "model", "object_id": 1, "path": "$LIBRARY_SERVER/1"}
}
```

支持从 `path` 推导模型 ID；两种表示同时存在时必须一致。`AssetRef` 指向本地几何资源，其模型原点、朝向、单位必须与服务模型相同。苹果测试已核对两侧 Mesh 文件哈希一致；其他模型需自行保持对应关系，当前不从服务自动下载或替换本地 Mesh。

服务字段仍叫 `grasp_properties`，项目将其解释为可供移动到达的交互位姿，不表示已经执行抓取。示例命令现在是 `move 101 → move home`。`home` / `test_move` 等命名目标和 `end_effector` 相对运动继续使用原有逻辑，不查询服务。场景 `ols_grasp_test` 的版本仍为 1。

## 坐标约定与夹爪标定

按已确认约定，每条 `poses` 为 `[x,y,z,rx,ry,rz,ax,ay,az,angle]`：位置单位米；`rx/ry/rz` 是弧度制 XYZ 固定轴欧拉角，即 `Rz(rz) @ Ry(ry) @ Rx(rx)`；前六项定义模型局部坐标中的夹持中心位姿。后四项是旋转容差轴和角度，本次保留并校验，但暂不在容差范围内采样。

设 `T_A_B` 把 B 系坐标变换到 A 系：

```text
T_world_grasp   = T_world_object × T_object_grasp
T_world_control = T_world_grasp × inverse(T_control_grasp)
```

机器人 `RobotModelRef.grasp_frame` 指定夹持中心相对 URDF 连杆的标定：

```json
{
  "link": "robotiq_2f85_base",
  "position": [0, 0, 0.145],
  "quaternion_xyzw": [0, 0, 0, 1]
}
```

接入层沿 URDF 固定关节计算夹持中心相对控制 TCP 的变换，并考虑 `tcp_transform`。当前装配相对 `attachment_site` 为平移 `[0,0,0.1558]` 米和绕 Z 轴 `-π/2`；更换工具应更新标定，而不是直接把夹持中心坐标当成 `attachment_site` 的坐标。

## 调用接口

| 接口 | 职责 |
| --- | --- |
| `ObjectLibraryClient(base_url, uid=0, timeout=5).get_model(model_id)` | 查询原服务、验证响应模型 ID，失败抛出 `ObjectLibraryError` |
| `LocalObjectLibraryService()` / `close()` | 启停本次任务专用服务；`url` 为实际地址，也支持上下文管理器 |
| `scene_object_by_id(scene, target_id)` | 精确定位场景实例，拒绝缺失或歧义 ID |
| `model_id_for(obj)` | 从实例元数据引用解析库模型 ID |
| `control_from_grasp_frame(resources)` | 从共享 URDF 和工具标定计算 `T_control_grasp`，仅支持固定工具关节 |
| `ObjectLibraryGraspResolver(client).resolve(target_id, resources, state)` | 使用 `state["objects"]` 当前世界位姿，查询服务并转换全部抓取候选 |
| `resolver.select(resolved, kin, checker, seed, safety_distance=.003, grasp_index=None)` | 通过现有 IK 和 FCL 选择可达候选，返回关节目标、所选候选与拒绝原因 |
| `DemoInteractionMove(resolver, resources, kinematics, collision_checker, joint_limits)` | demo 层的对象移动编排器，不扩展技能接口 |
| `demo_move.prepare(request, state)` | 输入 demo 的对象引用及同步场景状态，返回普通运动请求列表与解析信息；查询失败抛出异常，由 demo 记录 |

客户端位于 `services/object_library.py`，位姿解析器位于 `demo/interaction/object_library.py`，接近编排器位于 `demo/task_skills/move.py`，命令执行位于 `demo/run_demo.py`。服务只提供模型交互数据，运动学与碰撞计算仍由项目现有模块完成。

对象 ID 仅属于 demo 命令层。`SkillCommandConverter` 通过注册表 `scene_object_ids` 识别对象，生成 demo 内部的对象引用；随后由编排器解析。底层 `MoveSkill.execute()` 仍只接收既有 `pose` / `joint` / `named_state` 请求，不接受整数对象 ID、不查询服务，也没有 `target_resolver` 参数。未经用户明确允许，不修改 `skills/` 中任何文件。

第一段保留 `planning_method` 指定的路径类型，关节模式使用选定接近点的关节目标，直线/圆弧模式使用接近点位姿；圆弧仍需补充原有参数。第二段固定为保持姿态的直线运动，使用硬路径约束，禁止退化成关节绕行。执行第二段时从实际到达状态重新规划与检查。为保证夹持中心准确到达服务位姿，对象 ID 命令暂不支持 `keep_end_effector_orientation=true` 或 `relation/distance_m/anchor` 偏移；后续撤离可以单独使用 `move end_effector`。

候选选择先检查 IK 和终点碰撞。两段式 demo 使用 `prefer_direct=False`，按关节欧氏位移选择可达目标，然后验证接近点与接近线，省去“直接到最终目标”的预检查。候选姿态可能与旧排序不同，后续返回 home 也可能需要 RRT 绕障，不能保证每个场景都选择全任务最快的候选。可用 `parameters.grasp_index` 固定从 0 开始的原始候选序号。`resolver.select()` 默认仍保留 `prefer_direct=True` 的旧行为，供诊断或其他调用者使用。未对目标苹果禁用碰撞。

选定交互位姿后，demo 沿交互坐标系 −Z 方向尝试 15、12、10、14、13、11 cm 接近点，检查接近点 IK / FCL 及接近点到交互位姿的连续 IK 采样路径。找到可行接近点时生成 `approach`、`contact` 两个子步骤。全部失败时保留原 demo 的单段回退行为，并在报告中明确记录 `approach.used=false`、拒绝原因和 `direct_fallback`；不会把回退伪装成已执行两段式。第一段到接近点的路径仍由底层技能规划和校验。

## 结果与边界

`report.json` 记录所选模型与抓取候选、世界位姿、关节轨迹和到达误差；`frames.jsonl` 记录执行采样。服务不可达、模型缺失、抓取数据无效、没有可行 IK 或路径等失败都会停止流程，并记录 `active_command_id`、`error.stage` 和错误原因。

目前夹爪保持固定张开。Move 成功表示**夹持中心已到交互位姿**，结果为 `status="completed"`。demo 报告记录 `target_resolution`、`interaction_position_error_m` 和 `interaction_orientation_error_rad`；`substeps` 保存每段的请求、规划与执行结果，帧记录增加 `substep` 区分接近、接触和 home。步骤顶层的 `trajectory` / `execution_result` 对应最后执行的子步骤，完整两段轨迹请读取 `substeps`。Move 不执行闭合夹爪、接触抓紧、抬起物体或附着关系。此前临时加入的 `grasp + mode=position_only` 分支已移除；该统一场景测试入口当前仅支持 locate/move。

测试苹果是静态物体；使用步骤开始时的实测位姿规划，未实现运动目标跟踪。执行中继续做已有的实际状态 FCL 检查。查询或候选选择失败时，由 demo 报告 `TARGET_RESOLUTION_FAILED`，`failed_stage="target_resolution"`；某个子步骤失败时停止，不继续接触或后续 home 命令。底层技能错误码不受此 demo 扩展影响。

测试命令（原生集成需要 DLL）：

```powershell
$env:SIM_PLATFORM_LIBRARY = "libraries\simplatform\bin\sim_platform_c.dll"
.\.venv\Scripts\python.exe -B -m unittest tests.test_object_library_grasp tests.test_unified_execution tests.test_interpolated_timing
.\.venv\Scripts\python.exe -B services/object_library_server/tests/test_api.py
```



