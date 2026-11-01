# 按钮回弹设置

按钮滑动关节 `button_press_joint` 的正方向是向下，行程为 0～0.03 m。
原模型只有阻尼和 0.05 N 关节摩擦，没有弹簧；20 g 按钮头的重力约为
0.196 N，因此运行物理仿真时会自行下滑。

现有弹簧参数保存在 `button.urdf` 的 Gazebo 关节扩展中：

- `springStiffness = 120` N/m。
- `springReference = -0.004` m；在弹起位置提供约 0.48 N 向上的预紧力。
- `dynamics damping = 3` N·s/m，`friction = 0.25` N，略高于按钮头重力，增加静摩擦支撑。
- 约 4.2 N 的向下力可以按完 30 mm 行程；撤去外力后弹簧自然复位。

这是被动弹簧，没有锁定位置，也没有每帧把按钮传送回原位。任何足够大的
向下接触力都可以按压按钮，并不限于夹爪。装入场景时应固定按钮底座，
并启用物理步进；只调用 `mj_forward` 的运动学预览不会演算回弹。

## 加载方式

- 项目 `world_model/test_models/scene_builder_urdf.py` 已将 URDF 中的弹簧参数
  转换成 MuJoCo 的 `stiffness` / `springref`。
- Gazebo/SDFormat 的 URDF 转换器支持这些关节扩展。
- **MuJoCo 原生直接读取 URDF 不读取上述弹簧扩展。** 直接在 MuJoCo 中加载
  本模型时使用同目录的 `button_mujoco.xml`。该文件由同一 URDF 转换而来，
  含全部五个外观网格、碰撞体和被动弹簧，依然使用 `button_meshes`。
  修改 URDF 弹簧参数后，应同步更新该文件。

模型的主要入口仍为 `button.urdf`，`button_files.json` 将 MuJoCo 版本作为
参考文件列出。模型 ID、抓取数据和索引路径保持原值。

## 验证

在项目根目录使用 `auto_robot` 环境运行：

```powershell
python tests/test_button_spring.py
```

覆盖重力下静置 10 秒、2 N 外力按压后释放、实际接触压头下压后撤离、
连续 10 次全行程按压，以及项目生成场景中的弹簧回弹。
接触测试下压约 19.1 mm，2 N 外力下压约 14.3 mm；释放后均回到弹起端。
MuJoCo 默认的软关节限位使弹起端有小于 0.5 mm 的向上限位误差。

参考：[Gazebo 的 URDF 弹簧扩展](https://github.com/gazebosim/sdf_tutorials/blob/master/docs/urdf/sdf_extensions.md#gazebo-elements-for-joints)、
[MuJoCo 的 URDF 扩展范围](https://mujoco.readthedocs.io/en/stable/modeling.html#urdf-extensions)。
