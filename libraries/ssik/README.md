# SSIK 求解器

与 `libraries/eaik/` 同级，适配项目统一 `KinematicsService` 协议，支持串联 6R/7R 转动链。

## 已部署版本

- 上游：https://github.com/personalrobotics/ssik
- 版本：9.0.0；固定提交：`9562488fe2ce7c11c37e3e113f51e9b9fb97ae2e`。
- 本机环境：项目 `.venv`，Windows x64 / Python 3.10.9。
- 上游源码位于 `source/`，已安装到 `.venv`；源码工作副本被 Git 忽略，通过固定提交重建。
- Windows 当前使用上游 Python 求解路径，`native_available()` 为 `False`。不要求 EAIK 或机器人专用 DLL。

## 安装或重建

在项目根目录运行，安装固定提交及其依赖：

```powershell
.\.venv\Scripts\python.exe -m pip --isolated install -r libraries/ssik/requirements.txt
```

若也需要本地上游源码（目标目录应不存在）：

```powershell
git clone https://github.com/personalrobotics/ssik.git libraries/ssik/source
git -C libraries/ssik/source checkout 9562488fe2ce7c11c37e3e113f51e9b9fb97ae2e
.\.venv\Scripts\python.exe -m pip --isolated install ./libraries/ssik/source
```

上游 README 的 Python 3.11+ 描述尚未同步；此提交的 `pyproject.toml` 声明支持 Python >=3.10，已在本项目 3.10 环境实际安装和求解验证。

## 调用

已有统一运动学服务可直接指定后端：

```python
result = kin.solve_one(target_pose, seed=q, backend="ssik", timeout=5)
results = kin.solve_all(target_pose, seed=q, backend="ssik", timeout=5)
```

也可指定回退顺序：`backends=["eaik", "ssik", "jacobian"]`。
`solver.json` 的 `auto_priority` 为 `15`，默认自动顺序为 EAIK → SSIK → Jacobian。首次自动调用先准备解析后端并复核 FK，排除不支持当前几何或依赖不可用的后端；结果缓存在当前服务实例。EAIK 不支持的 6R/7R 链自动转用 SSIK。准备阶段与目标求解预算分开；首次调用可能额外耗时。

独立 URDF 入口复用项目的模型读取器（需要根目录依赖中的 Urchin）：

```python
from libraries.ssik import load_urdf

kin = load_urdf("robot.urdf", base_link="base_link", tip_link="tool0")
result = kin.solve_one(target_pose, seed=q, backend="ssik", timeout=5)
```

模型参数包括 `tcp_transform`、`world_from_base`、`initial_joints` 和 `continuous_limits`，透传到公共 `UrdfRobotModel`。continuous 关节必须指定有限搜索窗口。
`load_urdf` 会用当前位姿验证 SSIK；返回服务后可显式指定 `backend="ssik"`，或使用默认 `auto`。`kin.automatic_backend_info()` 返回当前几何适用的后端顺序和排除原因。显式选择仍绕过自动路由。

适配器读取公共 `chain_parameters`（兼容 `eaik_parameters`）；旧 MuJoCo 模型可在首次调用时提取链参数。世界基座变换和 TCP 零位旋转会转换到 SSIK 的 H/P 坐标约定，初始化时独立复核 FK。候选的限位、周期、连续性与目标精度由公共服务再次验证。

首次初始化和部分通用机器人几何的代数预处理可能较慢；上游求解调用不可中断，`timeout` 无法强制终止正在运行的 SSIK 调用。7R 返回冗余采样中的离散解，不保证枚举整条连续解流形。此模块仅计算运动学，碰撞和路径可行性由后续模块验证。

## 验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest libraries.ssik.test_integration -v
```

真实 SSIK 检查覆盖注册、自动优先 EAIK 与默认顺序，以及 10 个可达目标的 FK→IK 闭环（包含世界基座平移、旋转和非单位工具零位旋转）。

本机还验证了上游 `source/tests/fixtures/ur5e.urdf` 的独立入口，以及 `kuka_iiwa14.urdf` 的 7R 求解（`seven_r.srs`，返回解位置误差约 `3.23e-16 m`）。`pip check` 通过。

自动路由也已用项目真实资产 `services/object_library_server/data/robots/8/piper.urdf` 验证：EAIK 报不支持此几何，自动选择 SSIK，位置误差约 `8.44e-17 m`。通用 6R 首次符号推导可能耗时数分钟，SSIK 会把推导缓存到用户缓存目录供后续进程复用；自动路由在目标求解计时前完成这项准备。


竖直抓取等特殊目标姿态下，Windows Python 求解路径可能漏掉限位内的分支。适配器启用官方 `allow_refinement=True`，并在诊断中记录 `refinement_used` 数量；精修后候选仍需通过公共服务的限位与 FK 检查。`requests/commands.005/check_layout.py` 提供 PiPER 场景的 32 个高度样本回归检查。
