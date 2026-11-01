# UR5e IKFast 后端

当前 Windows x64 环境已编译 `ur5e_ikfast.dll`，使用仓库已有的
`source/ur5e/ur5e_ikfast61.cpp`，不是用数值 IK 冒充解析求解。
`native.cpp` 提供双精度 C 接口，Python 通过 ctypes 调用，可用于并行候选搜索。
编译源码中的许可证继续保留在 source 中。

## 默认行为

场景注册表、测试示例及运动策略现在默认 `ik_method: auto`：

- 优先 IKFast，并使用 MuJoCo FK 验证每个解和关节范围。
- 解析器不可用、无有效解时，回退已有 MuJoCo Jacobian 数值求解器。
- 推拉、按压跟随限制单帧关节变化为 0.3 rad；解析分支跳变时先尝试数值回退，
  回退仍超限则报错，不执行跳变。
- 已有解析候选时不再为凑够候选数量额外启动数值多初值搜索。
- 多个解析解按参考关节角规范化，关节运动会优先选择无碰撞目标分支。

显式 `ikfast` 禁用数值回退；显式 `jacobian` 使用原来的数值实现，`trac_ik` 保留为兼容别名。
结果中的 `solver`、`backend` 表明真正使用的算法；auto 数值回退还包含 `fallback_reason`。

## 坐标与模型

IKFast 的目标是其生成模型的工具端，不是世界坐标系的夹爪 pinch 点。
`libraries/ikfast/adapter.py` 通过 RobotModel 提取机器人基座世界变换，并推导工具端到
当前末端 site 的刚性变换。首次使用会在不同关节姿态下对比两个模型的 FK；
不匹配会拒绝使用该解析后端。每个返回解还会通过实际 MuJoCo 模型的 FK 复核。
只适用于与该生成源码运动学一致、末端 site 刚性连接在工具端的机器人。
插件由本目录 `solver.json` 注册；统一接口与新增库说明见 [运动学接口](../../kinematics/README.md)。

## 构建和依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r libraries/ikfast/requirements.txt
.\.venv\Scripts\python.exe -m libraries.ikfast.build
```

Windows 需要 Visual Studio 的 x64 C++ 工具链和 Windows SDK。
本机已完成构建；运行时不会自动编译。源码或 ABI 改动后应退出使用旧 DLL 的 Python
进程再重新构建。使用其他平台需重新编译相应动态库。

这份生成代码调用 LAPACK 解多项式的矩阵问题。包装使用 SciPy 随 wheel 提供的
**LP64** OpenBLAS 导出符号，不能替换成 NumPy 的 ILP64 库。
加载器先查找当前 Python 环境的 SciPy，再查找 Conda 基础环境的 scipy.libs。
本次验证使用现有 Anaconda 基础环境中的 SciPy 1.15.3 LP64 DLL；没有成功安装新的
SciPy 到 `.venv`。独立部署建议安装上面的 requirements，避免依赖基础环境。

原始归档中生成文件版本为 0x49、头文件为 0x48。本次只将版本检查明确扩展为
允许已使用的这两种 solution-list API，未伪改头文件版本；补充 MSVC `isinf` 兼容。
通过随机关节 FK/IK 闭环、并发调用、回退及场景流程测试验证此组合。

## 测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_kinematics_plugins.py -v
```

IKFast 尤其适合多分支候选搜索，但不保证每次调用都快于已有良好初值的数值法。
碰撞检测以及实际播放时长不由 IK 求解速度决定。
