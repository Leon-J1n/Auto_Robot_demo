# Auto Robot Demo

本项目用于机器人原子技能的开发、组合、规划、仿真执行和结果验证。当前统一演示入口是 `demo.run_demo`，主要运行在 MuJoCo 仿真环境中，并可通过对象库服务获取场景对象和交互位姿。

全局速度比例、加速度比例、轨迹采样帧率和首帧停顿在 `config/global.json` 中配置。
显式设置优先，否则使用全局默认值；配置格式和覆盖方式见 [全局配置说明](docs/global_config.md)。

## 根目录模块职责

| 目录 | 职责 |
| --- | --- |
| `assets/` | 项目资产库，保存物体模型、机器人模型、场景资源、资源索引和更新后的资产。 |
| `collision/` | 碰撞模型预处理、碰撞场景管理和碰撞查询，支持独立网格的凸分解以及 FCL/SimPlatform 后端。 |
| `control/` | 机器人和末端执行器控制逻辑，包括夹爪、连续轨迹、按压、推拉和接触状态控制。 |
| `demo/` | 任务级入口和编排层，负责命令解析、对象查询、交互位姿换算、任务阶段组织、运行报告和诊断工具。 |
| `execution/` | 执行运行时，负责机器人资源装配、MuJoCo 后端、夹爪模型、运动限制和执行状态管理。 |
| `hardware/` | 真实机器人、传感器和其他硬件适配接口的预留目录。 |
| `kinematics/` | 统一运动学接口、机器人模型适配、IK 插件注册、正逆运动学计算和解验证。具体算法库位于 `libraries/`。 |
| `libraries/` | 第三方或本地算法库，包括 IK、运动学链、Ruckig、SimPlatform 原生库和相关构建依赖。 |
| `planning/` | 运动规划模块，负责直接路径、避碰路径、交互接近/撤退候选、接触路径检查、轨迹优化和时间参数化。 |
| `sensors/` | 与具体后端无关的传感器接口和数据结构，例如统一的 RGB-D 相机帧。 |
| `services/` | 独立服务模块，目前主要包含对象库服务、场景索引和服务启动入口。 |
| `atomic_action/` | 原子技能实现，包括运动、抓取、释放、推拉、按压、定位和搜索等技能的接口、配置、校验和策略。 |
| `tests/` | 单元测试、集成测试、测试配置和测试夹具。运行生成的报告和临时输出不属于源码。 |
| `utils/` | 跨模块复用的通用工具，目前主要提供仿真运行时辅助功能。 |
| `assets/` | 当前仿真和对象服务使用的场景、机器人和物体资产。旧的 `world_model/` 场景目录已移除。 |
| `libraries/simplatform/` | SimPlatform 统一组件目录，包含源码、Python SDK、原生 DLL 和运行时依赖。 |

### 开发环境和工程元数据

| 目录 | 职责 |
| --- | --- |
| `.venv/` | Python 虚拟环境和项目依赖。 |
| `.idea/` | JetBrains 系列 IDE 的项目配置。 |
| `.git/` | Git 版本控制数据。 |
| `.cache/` | 运行时缓存。碰撞网格预处理结果位于 `.cache/collision/`，可在缓存失效时重新生成。 |

## 从场景文件和任务到执行结果的 Pipeline

### 1. 接收输入

项目接收两类主要输入。

- 场景文件。通常是 `assets/` 中的场景配置、机器人模型和对象资源。
- 任务命令文件。通常位于 `demo/task_commands/`，描述 `move`、`pick`、`put`、`push`、`pull`、`pour`、`grasp` 和 `release` 等任务。

默认会加载 `libraries/simplatform/bin/sim_platform_c.dll`，通常不需要传 `--library`。只有使用其他构建时才需要显式指定 DLL。

统一入口示例：

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo `
  --commands demo\task_commands\commands.001.json `
  --viewer
```

### 2. 加载场景和资源

`demo.run_demo` 只读场景配置和命令文件。`demo.scene.assembler.SceneAssembler` 在内存中复制场景，按场景中的 `MetadataRef` 向 OLS 查询机器人、夹爪和物体元数据，再从 `assets/` 的本地镜像定位对应 URDF/网格，生成本次运行专用的运行时场景。原始场景 JSON 和任务 JSON 不会被修改。

没有显式传入 `--object-library-url` 时，入口会自动启动项目内的 OLS；因此新场景只要使用有效的 OLS `MetadataRef`，就可以直接进入同一条 pipeline。`--start-object-library` 仍然兼容旧命令，但不再是必需参数。

需要对象查询时，`services/` 中的对象库服务读取场景对象、类别、链接和交互元数据。`demo/` 将服务返回的对象标识转换为当前运行时中的 MuJoCo body、几何体和对象状态。

### 3. 初始化碰撞模型

`collision/` 为场景中的碰撞几何建立查询场景。

- 已有的基础碰撞体和 URDF 碰撞定义直接使用。
- 独立 OBJ/STL 网格通过 `collision/mesh_preprocessing.py` 预处理。
- 非凸网格由 CoACD 分解为多个凸块。
- 预处理结果写入 `.cache/collision/`，后续运行在缓存有效时直接复用。

初始化完成后，FCL/SimPlatform 和 MuJoCo 使用对应的碰撞表示进行规划前检查、执行前复检和执行过程验证。

### 4. 解析任务并组织阶段

`demo/task_commands/` 中的命令由 `demo/` 解析，`atomic_skill/runner.py` 按任务类型组织执行阶段。

典型阶段包括：

- 目标解析和交互位姿查询
- 接近或预接触
- 抓取、建立接触或保持夹爪状态
- 主操作，例如移动、推、拉、倾倒或放置
- 抬升、撤退和释放
- 结果验证与报告记录

`atomic_skill/` 负责任务语义和阶段编排，生成标准化的技能请求，不直接承担通用规划器的执行职责。

### 5. 运动学求解和路径规划

任务请求进入技能和规划链路后，通常经过以下模块：

1. `atomic_action/` 校验请求、选择技能策略并生成执行所需的标准请求。
2. `kinematics/` 根据目标位姿或关节目标求解 IK，并验证解的连续性、关节限制和目标可达性。
3. `planning/` 生成直接路径或避碰路径，执行接近、接触、撤退等候选路径检查，并进行轨迹平滑和时间参数化。
4. `collision/` 检查目标状态、路径采样点和轨迹段是否碰撞。
5. 规划器返回关节路点、轨迹元数据、碰撞检查结果和失败诊断。

交互任务会根据任务特点生成多个阶段请求。比如 `pick` 通常包含接近、接触、抬升和撤退，`put` 通常包含接近、接触、释放和撤退。

### 6. 执行轨迹

`execution/` 接收已准备好的轨迹并驱动 MuJoCo 运行时，`control/` 负责夹爪、接触、推拉、按压和连续轨迹执行。执行期间会同步机器人、物体、夹爪和碰撞场景状态。

对于需要持续接触的操作，控制器还会监控接触保持、力限制、目标位移和停止条件。

### 7. 执行后验证和输出

执行过程中和执行结束后，系统会检查：

- 实际关节和末端位姿
- 实际轨迹的碰撞情况
- 目标是否到达
- 抓取、接触、释放和保持状态
- 规划缓存与碰撞查询缓存的命中情况

统一演示会生成任务结果、阶段状态、轨迹统计、碰撞缓存统计和错误诊断。默认运行输出目录为 `tests/output/`，该目录属于生成物，不作为源码模块使用。

## 相关文档

- [演示和运行说明](demo/readme.md)
- [技能接口说明](SKILL_INTERFACES.md)
- [运动学接口说明](KINEMATICS_INTERFACES.md)
- [规划接口说明](PLANNING_PIPELINE_INTERFACES.md)
- [MuJoCo 依赖说明](MUJOCO_DEPENDENCIES.md)
