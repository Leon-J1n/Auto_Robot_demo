# EAIK 实验求解器

已在本项目 Windows/Python 3.10 虚拟环境验证 EAIK 1.2.2。当前 UR5e 的数据、转换细节与结果见 [EAIK_UR5E_CHECK.md](C:/Users/21474/Desktop/robot-agent/EAIK_UR5E_CHECK.md)。

- `model_data.py`：保留原参数检查接口；提取实现已集中到 `libraries/kinematic_chain/mujoco.py`。
- `check_model.py`：导出 JSON、验证参数 FK，以及可选的真实 EAIK FK/IK 检查。
- `adapter.py`：适配统一 IKBackend 协议；按需导入 EAIK。
- `solver.json`：注册 `eaik`，默认优先参与 `auto`。
- `urdf.py` / `urdf_cli.py`：独立 URDF 入口，指定基座和末端即可调用。
- `model_checks/`：各场景的实际输入与测试结果。

```python
result = kin.solve_one(pose, seed=q, backend="eaik")
result = kin.solve_one(pose, seed=q, backends=["eaik", "ikfast", "jacobian"])
```

工厂优先读取模型提供的 `chain_parameters`，兼容原 `eaik_parameters` 属性；只有旧参考模型才从 `robot.runtime` 提取 MuJoCo 串联转动链。也可直接构造 `EAIKBackend(robot, parameters)`，传入已导出的参数字典。RobotModel 的 FK/基座变换需与参数一致；不支持的几何结构由 EAIK 的 `hasKnownDecomposition()` 拒绝。

当前默认场景和 URDF 模型都提供通用 `chain_parameters`，EAIK 工厂优先使用它，不再重复提取 MuJoCo 几何。URDF 读取器已集中到 `libraries/kinematic_chain/urdf.py`。

参数 JSON 本身不依赖 MuJoCo；`parameter_forward(parameters, q)` 是仅依赖 NumPy 的校验函数。核心求解服务没有新增专用模型协议。

## URDF 入口

```python
from libraries.eaik import load_urdf
kin = load_urdf("robot.urdf", base_link="base_link", tip_link="tool0")
result = kin.solve_one(target_pose, seed=current_q)
```

Python、命令行、工具/基座变换和可运行示例见 [URDF_USAGE.md](C:/Users/21474/Desktop/robot-agent/libraries/eaik/URDF_USAGE.md)。此独立入口无需 MuJoCo，不改变当前仿真服务；尚未自动处理 Xacro。

## 在新环境安装

Windows 使用官方源码和 C++ 编译工具链；本次采用 MSVC x64。以下是新环境步骤（目录应尚不存在）：

```powershell
git clone --depth 1 --recurse-submodules --shallow-submodules https://github.com/OstermD/EAIK.git "$env:TEMP\robot-agent-eaik-source"
.\.venv\Scripts\python.exe -m pip --isolated install --no-cache-dir wheel packaging pybind11 pybind11-stubgen
.\.venv\Scripts\python.exe -m pip --isolated install --no-cache-dir --no-build-isolation "$env:TEMP\robot-agent-eaik-source"
```

源码可以额外固定到报告中的提交。`--isolated` 忽略当前 shell 的 `PIP_NO_INDEX` 等 pip 环境设置。无需为每种机器人再编译一份 EAIK 原生库，机器人结构由输入参数确定。

官方仓库与安装说明：[OstermD/EAIK](https://github.com/OstermD/EAIK)。
