# SSIK 集成

`src/ssik` 保留上游 SSIK 源码，`adapter.py` 将它接入本项目的统一 IK
服务。插件通过 `solver.json` 注册，默认顺序为 EAIK → SSIK → Jacobian。

SSIK 只接受当前项目的 6R 或 7R 串联转动链参数。运行时需要安装该目录
`pyproject.toml` 定义的 `ssik` 包，或在项目虚拟环境中已有 `ssik` 安装。

显式调用示例：

```python
result = kin.solve_one(target_pose, backend="ssik")
```
