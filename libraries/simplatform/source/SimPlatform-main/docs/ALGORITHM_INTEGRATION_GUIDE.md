# 算法集成手册

本文是新增 IK、路径规划等算法的操作手册。目标是让调用代码保持简单：C/C++ 直接 `#include`，Python 和 Live Control 直接 `import`。registry 只是可选函数表。

## 1. 开始前决定实现方式

| 原始实现 | 推荐接入方式 | 当前可覆盖入口 |
|---|---|---|
| C/C++ | 导出 C ABI，Python 包装该 ABI | C/C++、Python SDK、GUI Python、Live Python |
| Python | 公共 Python 函数 | Python SDK、GUI Python、Live Python |
| Python 且要求 C 调用 | 增加该函数的 C facade 和 Python worker bridge，或补 native 实现 | 全部入口 |

不要先实现 registry。先让直接调用工作，再把已经可用的函数加入两侧函数表。

## 2. 创建文件

以 IK 为例：

```text
include/sim_algorithms/ik.h
src/sim_algorithms/ik.cpp
python/sim_algorithms/ik.py
tests/algorithms/test_ik.cpp
python/tests/test_algorithms_ik.py
```

若这是第一个算法，还需要：

```text
include/sim_algorithms/common.h
include/sim_algorithms/registry.h
src/sim_algorithms/registry.cpp
python/sim_algorithms/__init__.py
python/sim_algorithms/registry.py
```

## 3. 先写函数契约

在编码前确定：

- 输入和输出的含义、形状、dtype；
- 关节名与数组顺序；
- 坐标系、单位、四元数顺序；
- 超时、不可达、部分解和非法输入如何报告；
- 输出内存由谁分配和释放；
- 是否线程安全、是否确定、是否需要 GPU。

平台约定：米、秒、弧度、右手系 X 前/Y 左/Z 上、四元数 `w,x,y,z`。所有关节数组都必须带有或引用明确的 `joint_names`。

## 4. 接入 C/C++ 算法

### 4.1 声明 C API

```c
/* include/sim_algorithms/ik.h */
#pragma once
#include <sim_algorithms/common.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct SimIkInput {
    uint32_t struct_size;
    const char* const* joint_names_utf8;
    const double* seed_positions;
    size_t joint_count;
    double target_translation[3];
    double target_rotation_wxyz[4];
    uint64_t timeout_ms;
} SimIkInput;

typedef struct SimIkOutput {
    uint32_t struct_size;
    double* positions;
    size_t position_capacity;
    size_t position_count;
    double residual;
} SimIkOutput;

SIM_ALGORITHMS_API int solve_ik(
    const SimIkInput* input,
    SimIkOutput* output);

#ifdef __cplusplus
}
#endif
```

### 4.2 实现 facade

实现必须：

1. 检查指针、`struct_size`、数组长度和有限数值。
2. 把 C 输入转换给内部 C++ 算法。
3. 检查 `position_capacity` 后写入调用方 buffer。
4. 捕获全部 C++ 异常。
5. 设置线程局部错误文本并返回 `SimAlgorithmStatus`。

不要返回 STL 对象，不要让异常穿过 ABI，不要在 DLL 中分配后要求调用方直接 `free()`。

### 4.3 验证直接 C 调用

```c
#include <sim_algorithms/ik.h>

double positions[16];
SimIkInput input = {sizeof(SimIkInput)};
SimIkOutput output = {sizeof(SimIkOutput)};
output.positions = positions;
output.position_capacity = 16;

int status = solve_ik(&input, &output);
if (status != SIM_ALGORITHM_OK) {
    fprintf(stderr, "%s\n", sim_algorithms_last_error());
}
```

CMake 调用方：

```cmake
target_link_libraries(my_app PRIVATE sim_algorithms_c sim_platform_c)
```

## 5. 接入 Python 算法或包装 native 算法

公共模块保持普通函数：

```python
# python/sim_algorithms/ik.py
def solve_ik(
    joint_names: tuple[str, ...],
    seed_positions,
    target_pose,
    *,
    timeout_ms: int = 100,
):
    """Return positions in the same order as joint_names."""
    ...
```

若实现是 C++：

1. 把 `seed_positions` 转为连续 `float64` 数组。
2. 校验 joint count、target pose 和 timeout。
3. 通过 `ctypes` 或扩展模块调用 C `solve_ik()`。
4. 把非零状态转换为 `AlgorithmError`。
5. 返回独立拥有内存的 NumPy 数组。

若实现是 Python，直接调用内部实现即可。不同算法可以返回不同类型，不需要继承统一 result 类。

验证直接 Python 调用：

```python
from sim_algorithms.ik import solve_ik

output = solve_ik(joint_names, seed_positions, target_pose)
```

## 6. 加入 C Registry

在 `registry.h` 中为该签名声明函数指针类型，并确保 union 有对应字段：

```c
typedef int (*SimSolveIkFn)(const SimIkInput*, SimIkOutput*);

typedef union SimAlgorithmFunction {
    SimSolveIkFn solve_ik;
    SimPlanPathFn plan_path;
} SimAlgorithmFunction;
```

在 `registry.cpp` 的静态表增加一行：

```c
static const SimAlgorithmEntry FUNCTIONS[] = {
    {"solve_ik", SIM_ALGORITHM_IK, {.solve_ik = solve_ik}},
    /* other functions */
};
```

检查项：

- `name` 与 Python 公共函数名一致；
- `kind` 与 union 字段匹配；
- 名称未重复；
- 没有 `reinterpret_cast` 或不兼容函数指针强转；
- 直接调用测试和 registry 调用测试都存在。

## 7. 加入 Python Registry

Python registry 是普通字典：

```python
# python/sim_algorithms/registry.py
from collections.abc import Callable
from typing import Any

registry: dict[str, Callable[..., Any]] = {}


def register(name: str, function: Callable[..., Any]) -> None:
    if not name:
        raise ValueError("algorithm name must not be empty")
    if name in registry:
        raise ValueError(f"algorithm already registered: {name}")
    registry[name] = function


def get(name: str) -> Callable[..., Any]:
    try:
        return registry[name]
    except KeyError:
        raise KeyError(f"algorithm not registered: {name}") from None


def call(name: str, *args: Any, **kwargs: Any) -> Any:
    return get(name)(*args, **kwargs)


def names() -> tuple[str, ...]:
    return tuple(sorted(registry))
```

在包初始化中显式注册：

```python
# python/sim_algorithms/__init__.py
from .ik import solve_ik
from .registry import register, registry

register("solve_ik", solve_ik)

__all__ = ["solve_ik", "registry"]
```

现在以下三种写法都可用：

```python
from sim_algorithms import registry
from sim_algorithms.registry import call, get

output = registry["solve_ik"](input1, input2)
output = get("solve_ik")(input1, input2)
output = call("solve_ik", input1, input2)
```

不要添加 descriptor、handle、统一 result、目录扫描或自动插件发现。项目内扩展在应用启动时显式调用 `register()`。

## 8. Python SDK 调用

```python
from sim_algorithms.ik import solve_ik
from sim_platform import Simulator

with Simulator(physics="mujoco", collision="fcl") as sim:
    world = sim.create_world(fixed_time_step=0.002, seed=42)
    robot = world.load_robot("robot.urdf")
    positions, _, _ = robot.joint_state

    target = solve_ik(robot.joint_names, positions, target_pose)
    if robot.configuration_collides(target):
        raise RuntimeError("IK result is in collision")

    robot.command_joint_positions(target)
    sim.step(world, 100)
```

## 9. GUI Python 调用

```python
from sim_algorithms.ik import solve_ik
from sim_platform import EditorSession

with EditorSession.launch(editor_path) as session:
    robot = session.load_robot("robot.urdf")
    positions, _, _ = robot.joint_state
    target = solve_ik(robot.joint_names, positions, target_pose)
    robot.command_joint_positions(target)
    session.start()
```

GUI 用于可视化验证，不用于算法性能基准。

## 10. Live Python Control 调用

```python
from sim_algorithms.ik import solve_ik
from sim_platform.live import connect

with connect("pid:18432", workspace="default") as sim:
    robot = sim.require_robot("robot")
    positions, _, _ = robot.joint_state
    target = solve_ik(robot.joint_names, positions, target_pose)
    robot.command_joint_positions(target)
    sim.step(1)
```

运行该脚本的客户端环境必须安装 `sim_algorithms`。算法在客户端运行，Live 服务端无需加载算法，也不接收 Python 源码。

## 11. C API 调用完整流程

C 应用组合两个独立库：

```text
sim_platform_c:   加载机器人、读取状态、下发命令、推进仿真
sim_algorithms_c: 求解 IK、规划路径等纯算法
```

流程：

1. `sim_create`、`sim_create_world`。
2. `sim_load_asset` 加载 URDF。
3. `sim_robot_joint_count/name` 建立名称与数组顺序。
4. `sim_robot_get_joint_state` 读取当前状态。
5. 直接调用 `solve_ik`。
6. 可选调用 `sim_robot_configuration_collision` 验证解。
7. `sim_robot_command_joint_positions` 下发结果。
8. `sim_step` 推进仿真。

每个函数都检查返回值。平台错误读取 `sim_last_error()`，算法错误读取 `sim_algorithms_last_error()`，不要混用。

## 12. Python-only 算法支持 C

Python-only 算法默认已经支持 Python SDK、GUI Python 和 Live Python。要支持纯 C，再做以下二选一：

### 推荐：补 native 实现

让 C `solve_ik()` 调 native 实现，Python 公共函数包装同一个 C ABI。这最适合实时或高频算法。

### 备选：实现 C facade + Python worker

C facade 保持强类型 `solve_ik(input, output)`，内部：

1. 启动或复用本机 Python worker。
2. 将该函数的输入转换为协议消息。
3. worker 调用 `sim_algorithms.ik.solve_ik`。
4. 将返回值写入 `SimIkOutput`。
5. 将 Python 异常转换为 C 状态码和错误文本。

worker 只允许固定模块和函数，不接受源码字符串。必须有启动超时、调用超时、进程退出恢复和资源清理。该路径不用于实时控制环。

## 13. 必须通过的测试

### 单算法测试

- 已知输入、不可达目标、非法维度、关节限位和 timeout。
- C 和 Python 对同一 fixture 结果一致。
- C 输出 buffer 太小时正确失败并报告所需长度。
- 单位、四元数顺序和关节名重排有单独测试。

### Registry 测试

- C/Python registry 名称集合一致。
- 重复名称注册失败。
- 不存在名称返回明确错误。
- C entry 的 kind 与 union 字段正确。
- registry 调用与直接调用结果一致。

### 平台入口测试

- C API -> 算法 -> C API 闭环。
- Python SDK -> 算法 -> Python SDK 闭环。
- GUI Python 和 Live Python 能 import 同一个模块并下发结果。
- 固定 seed/dt 下可重复；浮点容差写进测试。

### Python bridge 测试（仅 Python-only + C）

- worker 冷启动、复用、超时和异常映射。
- worker 崩溃后可恢复。
- 非白名单模块/函数被拒绝。
- 进程退出后无残留共享内存或子进程。

## 14. 提交前清单

- [ ] 直接 C/C++ 和 Python 调用已先通过。
- [ ] C ABI 没有 STL、异常或跨 DLL 错误释放。
- [ ] Python 公共函数不暴露内部 provider。
- [ ] C registry 使用 `kind + union`，没有不安全强转。
- [ ] Python registry 是简单显式字典，没有隐藏扫描。
- [ ] 两侧 registry 名称一致且不重复。
- [ ] C API、Python SDK、GUI Python、Live Python 覆盖情况真实。
- [ ] Python-only 算法未在缺少 bridge/native 实现时宣称支持 C。
- [ ] 单位、关节顺序、错误、依赖和许可证已记录。
