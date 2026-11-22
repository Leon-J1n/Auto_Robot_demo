# RobotSimPlatform 文档索引

本文档集以当前源码为准，目标是让接手者先建立完整心智模型，只在真正修改功能时进入相关实现文件。

| 文档 | 适合何时阅读 |
|---|---|
| [仓库接口文档](API_REFERENCE.md) | 按 sim_core、Python、Web Gateway、Agent Service 查找服务接口、传输协议和请求约定时 |
| [Python 服务接口详表](API_PYTHON_SERVICES.md) | 需要 Python SDK、Live JSONL、Headless RPC、Editor 和算法包的完整方法清单时 |
| [Web/Agent 接口清单](API_INVENTORY_AGENT_WEB.md) | 需要逐路由 HTTP、WebSocket、Agent 控制回调和 schema 字段时 |
| [C++ API 速查](API_CPP_QUICK_REFERENCE.md) | 使用原生 C++、ECS、后端或稳定 C ABI 时 |
| [Python API 速查](API_PYTHON_QUICK_REFERENCE.md) | 编写无窗口仿真、GUI 自动化或 Agent 服务时 |
| [运行中 Python 控制设计](LIVE_PYTHON_CONTROL_DESIGN.md) | 设计或实现运行后接入、PID/TCP 连接、跨进程对象代理和 alias 注册表时 |
| [算法库集成方案](ALGORITHM_INTEGRATION_ARCHITECTURE.md) | 设计 `sim_algorithms` Direct API、C/Python registry 和 provider 时 |
| [算法集成手册](ALGORITHM_INTEGRATION_GUIDE.md) | 新同事从零接入 IK、路径规划并覆盖 C/Python/Live 调用时 |
| [关键实现结构](IMPLEMENTATION_STRUCTURE.md) | 判断一个功能位于哪一层、追踪控制流或准备修改实现时 |
| [项目接手指南](HANDOFF_GUIDE.md) | 新环境安装、构建、测试、运行、排障与目录导航时 |
| [IFPS 构建与维护](IFPS_COLLISION_BACKENDS.md) | 构建 IFPS-CPU/GPU、理解球壳/缓存原理或修改插件时 |
| [Bullet3 Collision Backend](BULLET3_COLLISION_BACKEND.md) | 构建 Bullet3 Collision 插件、理解 DBVT/GImpact BVH 或修改查询时 |
| [Coal Collision Backend](COAL_COLLISION_BACKEND.md) | 构建 Coal 插件、理解 Dynamic AABB Tree/OBBRSS 或修改查询时 |

补充资料：

- [第一阶段状态](../FIRST_STAGE_STATUS.md)：验收范围、已完成功能和历史验证证据。
- [架构设计 v2](../仿真平台架构v2.md)：原始目标架构。若与当前实现冲突，以源码和本文档集为准。
- [项目 README](../README.md)：项目概览和最短构建说明。

## 三条主要使用路径

```text
原生 C++          include/sim/*.hpp -> sim::core -> MuJoCo/FCL/Vulkan + Bullet3/Coal/IFPS 插件
无窗口 Python     python/sim_platform/sdk.py -> sim_platform_c.dll -> src/c_api.cpp
GUI Python        python/sim_platform/editor.py -> TCP JSONL -> src/editor.cpp
```

不要把无窗口 `Simulator` 和 GUI `EditorSession` 混用：它们拥有不同的进程、World 和实体句柄。
