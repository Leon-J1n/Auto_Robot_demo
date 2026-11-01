# Libraries

第三方库源码和适配入口统一放在本目录。业务代码不得从原始源码目录直接导入。

- `eaik/`：基于 URDF 的 EAIK 逆解，当前 `auto` 默认优先使用。
- `ikfast/`：UR5e IKFast 解析逆解；暂不加入 `auto` 回退链，仍可显式调用。
- `trac_ik/`：TRAC-IK 原生后端预留；当前 Windows 运行时由 `kinematics` 中的 MuJoCo Jacobian 数值兼容实现提供同类求解能力。

迁移 Linux 后，应在对应目录编译官方后端，并保持 `kinematics.MujocoKinematics.solve()` 的公共接口不变。
