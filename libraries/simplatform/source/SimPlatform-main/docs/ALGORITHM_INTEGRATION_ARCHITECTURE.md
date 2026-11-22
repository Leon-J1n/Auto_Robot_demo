# 算法库集成方案

本文规定 IK、路径规划等算法如何接入 RobotSimPlatform。核心原则是：算法首先是一个可以正常 `include` 或 `import` 的库；registry 只是可选的按名称查找入口。

## 1. 目标调用方式

### C / C++

```c
#include <sim_algorithms/ik.h>

int test_func(void) {
    SimIkInput input = {0};
    SimIkOutput output = {0};
    int status = solve_ik(&input, &output);
    if (status != SIM_ALGORITHM_OK) return status;
    other_func(&output);
    return 0;
}
```

### Python SDK

```python
from sim_algorithms.ik import solve_ik

def test_func():
    output = solve_ik(input1, input2)
    other_func(output)
```

### Live Python Control

```python
from sim_algorithms.ik import solve_ik
from sim_platform.live import connect

def test_func():
    with connect("pid:18432") as sim:
        robot = sim.require_robot("robot")
        positions, _, _ = robot.joint_state
        output = solve_ik(robot.joint_names, positions, target_pose)
        robot.command_joint_positions(output)
```

无窗口 Python、GUI Python 和 Live Python 都 import 同一个 `sim_algorithms` 包。Live 中算法在客户端 Python 进程执行，不把源码发送到仿真服务端。

## 2. 两层 API

`sim_algorithms` 只有两层：

1. **直接 API**：`solve_ik()`、`plan_path()` 等普通函数。这是业务代码的默认入口。
2. **轻量 registry**：名称到函数的映射。CLI、Agent 或可配置工具确实需要动态选择算法时才使用。

registry 不统一所有算法的输入输出，不包装 result，不创建 handle，也不管理仿真生命周期。每个算法保留最适合自己的强类型签名和返回值。

## 3. 目录结构

建议新增：

```text
include/sim_algorithms/
  common.h                 # 导出宏、公共状态码和 last_error
  ik.h                     # solve_ik C API
  path_planning.h          # plan_path C API
  registry.h               # C 函数表
src/sim_algorithms/
  ik.cpp
  path_planning.cpp
  registry.cpp
  python_bridge.cpp        # 仅 Python 实现需要从 C 调用时使用
python/sim_algorithms/
  __init__.py
  ik.py
  path_planning.py
  registry.py
tests/algorithms/
```

构建产物：

- `sim_algorithms_c.dll` / `libsim_algorithms_c.so`：C ABI 和 C registry；
- `sim_algorithms` Python 包：Python 直接 API 和 Python registry。

算法库和仿真平台库分开。调用方可同时链接 `sim_algorithms_c` 与 `sim_platform_c`，但算法函数不进入 `sim_platform_c.dll`。

## 4. C Direct API

每个算法头文件定义自己的输入输出。不要为了 registry 把所有算法改成一个通用 `AlgorithmInput/AlgorithmResult`。

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

约束：

- 只使用 C ABI 类型；不能暴露 STL、C++ 异常或内部类。
- 输入输出结构以 `struct_size` 支持向后兼容。
- 调用方提供输出 buffer，避免跨 DLL 分配和释放。
- `0` 成功，非零值来自 `SimAlgorithmStatus`。
- 错误详情由线程局部 `sim_algorithms_last_error()` 返回。
- 异常必须在 C facade 内转换为状态码。

## 5. C Registry

用户期望的函数表形式是正确方向，但 IK 和路径规划的函数签名不同，不能安全地都强转成同一个函数指针。使用一个很小的 `kind + union`：

```c
/* include/sim_algorithms/registry.h */
#include <sim_algorithms/ik.h>
#include <sim_algorithms/path_planning.h>

typedef int (*SimSolveIkFn)(const SimIkInput*, SimIkOutput*);
typedef int (*SimPlanPathFn)(const SimPathInput*, SimPathOutput*);

typedef enum SimAlgorithmKind {
    SIM_ALGORITHM_IK,
    SIM_ALGORITHM_PATH_PLANNING
} SimAlgorithmKind;

typedef union SimAlgorithmFunction {
    SimSolveIkFn solve_ik;
    SimPlanPathFn plan_path;
} SimAlgorithmFunction;

typedef struct SimAlgorithmEntry {
    const char* name;
    SimAlgorithmKind kind;
    SimAlgorithmFunction function;
} SimAlgorithmEntry;

SIM_ALGORITHMS_API size_t sim_algorithms_count(void);
SIM_ALGORITHMS_API const SimAlgorithmEntry* sim_algorithms_at(size_t index);
SIM_ALGORITHMS_API const SimAlgorithmEntry* sim_algorithms_find(const char* name);
```

内置表就是简单静态数组：

```c
static const SimAlgorithmEntry FUNCTIONS[] = {
    {"solve_ik", SIM_ALGORITHM_IK, {.solve_ik = solve_ik}},
    {"plan_path", SIM_ALGORITHM_PATH_PLANNING, {.plan_path = plan_path}},
};
```

动态调用方必须检查 `kind`：

```c
const SimAlgorithmEntry* entry = sim_algorithms_find("solve_ik");
if (entry == NULL || entry->kind != SIM_ALGORITHM_IK) return 1;
int status = entry->function.solve_ik(&input, &output);
```

普通业务代码仍直接调用 `solve_ik()`。registry 不需要通用 request、result、序列化协议或 descriptor。

### 注册规则

- 名称使用稳定的 Python 函数名，例如 `solve_ik`。
- 名称必须全局唯一；重复名称在构建测试中失败。
- 新增一种函数签名时，给 enum 和 union 增加一个明确字段。
- 不把函数指针存入 `void*`，也不调用不兼容类型的函数指针。
- 第一阶段只注册随 `sim_algorithms_c` 构建的函数，不做运行时 DLL 插件加载。

## 6. Python Direct API

Python 函数使用自然签名和自然返回值。不同算法不必共享 `AlgorithmResult`：

```python
# python/sim_algorithms/ik.py
def solve_ik(
    joint_names: tuple[str, ...],
    seed_positions,
    target_pose,
    *,
    timeout_ms: int = 100,
):
    """Return joint positions in the same order as joint_names."""
    ...
```

返回值可以是 NumPy 数组、算法自己的 dataclass 或 tuple，只要该函数的文档和类型标注明确。不要为了 registry 改变直接 API。

若底层是 C++，`ik.py` 负责 NumPy 校验并通过 `ctypes`/扩展模块调用 `solve_ik` C ABI。若底层是 Python，`ik.py` 可直接包含实现或调用内部 provider。业务代码永远只 import 公共模块。

## 7. Python Registry

Python registry 使用一个普通字典：

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

内置函数在包初始化时显式注册：

```python
# python/sim_algorithms/__init__.py
from .ik import solve_ik
from .path_planning import plan_path
from .registry import register, registry

register("solve_ik", solve_ik)
register("plan_path", plan_path)

__all__ = ["solve_ik", "plan_path", "registry"]
```

调用方式：

```python
from sim_algorithms import registry
from sim_algorithms.registry import call, get, names

output = registry["solve_ik"](input1, input2)
output = get("solve_ik")(input1, input2)
output = call("solve_ik", input1, input2)
print(names())
```

这套 registry 有意不提供：

- descriptor、handle 或统一 result；
- 自动目录扫描或动态 import；
- entry point 和第三方包发现；
- 自动参数转换；
- 生命周期、异步任务或取消管理。

项目内扩展包在自己的初始化函数中显式调用 `register(name, function)`。是否加载该扩展由应用启动代码决定，行为可预测且容易排错。

## 8. C++ 与 Python 实现如何覆盖所有入口

算法实现语言不改变公共调用方式。

### C/C++ 实现

```text
C/C++ caller -> solve_ik C ABI -> native implementation
Python caller -> sim_algorithms.ik -> C ABI -> native implementation
Live Python  -> sim_algorithms.ik -> C ABI -> native implementation
```

这是最简单、性能最稳定的全入口路径。

### Python 实现

```text
Python caller -> sim_algorithms.ik -> Python implementation
Live Python  -> sim_algorithms.ik -> Python implementation
C/C++ caller -> solve_ik C facade -> local Python bridge -> Python implementation
```

为了让纯 Python 算法也能从 C 调用，需要一个受控的本机 Python bridge。它不是 registry 的职责：每个 C facade 仍负责把自己的强类型输入转换为 Python 参数，再把 Python 返回值写回自己的输出结构。

bridge 可以是内嵌 CPython 或独立 worker。推荐独立 worker，避免把 Python 版本、GIL 和崩溃传播进仿真进程。第一版协议只需要：固定模块/函数白名单、请求超时、错误文本和进程退出恢复；不要提供任意源码执行。

在 bridge 尚未实现时，Python-only 算法只能承诺 Python/GUI/Live 可用。如果某个算法从第一天就要求 C 可用，则必须同时提供 native 实现或完成该算法的 C bridge。

## 9. 与仿真平台的关系

算法函数只接收普通值，不接收 `World`、`Robot`、`EditorRobot` 或 `RobotProxy`。各入口负责读取平台状态和提交算法结果：

| 入口 | 读取状态 | 调用算法 | 应用结果 |
|---|---|---|---|
| C API | `sim_robot_get_joint_state` | `solve_ik` | `sim_robot_command_joint_positions` |
| Python SDK | `robot.joint_state` | `solve_ik` | `robot.command_joint_positions` |
| GUI Python | `EditorRobot.joint_state` | `solve_ik` | `EditorRobot.command_joint_positions` |
| Live Python | `RobotProxy.joint_state` | `solve_ik` | `RobotProxy.command_joint_positions` |

碰撞检查、推进仿真和保存状态仍由 `sim_platform` 完成。算法库不隐式创建 World、不推进 step，也不选择后端。

## 10. 统一约定

- 长度用米，角度用弧度，时间用秒。
- 坐标系为右手系，X 前、Y 左、Z 上。
- 四元数顺序为 `w,x,y,z`。
- 关节数组必须与显式 `joint_names` 一一对应；不能硬编码自由度。
- C 和 Python 的同名函数必须具有相同语义，但可使用各语言自然的数据类型。
- C registry 和 Python registry 的名称集合必须由测试保持一致。
- 算法版本记录在对应模块/头文件文档中，不引入全局 descriptor 系统。

## 11. 实施顺序

1. 创建 `sim_algorithms_c` 和 Python `sim_algorithms` 包骨架。
2. 以 `solve_ik` 完成一个 C++ 实现的 vertical slice。
3. 加入 C 静态函数表和 Python 字典 registry。
4. 在 C API、Python SDK、GUI Python、Live Python 中分别完成调用测试。
5. 接入 `plan_path`，验证 registry 能容纳第二种函数签名。
6. 第一个 Python-only 算法出现时，再实现最小 Python worker bridge。
