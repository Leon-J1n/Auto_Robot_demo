# PiPER 测试：沿用 commands.002 的场景布局

`commands.005.json` 引用 `piper_table_sorting.json`（scene_id=3，scene_version=8）。场景内容以 `requests/commands.002/ur3_table_sorting.json` 为准。

已逐对象比较，只有以下三项不同：

- 场景对象 2：UR3 替换为 OLS 资产 8 的 PiPER，使用资产关节名称、限位、初始关节值和物理属性。基座位置为 `[0, -0.5, 0.65]`，绕世界 Z 轴朝向 85°。
- 场景对象 3：夹爪仅修改 `RobotAttachment`，连接到 `joint6` / `link6`，绕法兰局部 Z 轴 -90°；夹爪其他组件保持源场景一致。
- 场景对象 6：底座位置调整为 `[0, -0.5, 0.4]`，尺寸保持不变。

其余 12 个对象（含桌子、物品、容器、相机和灯光）的全部属性与源场景逐项一致，没有改变摆放、朝向、尺寸或资产引用。源 UR3 场景文件没有修改。场景身份字段更新为本任务版本，原 14 步命令保持不变。

```powershell
.\.venv\Scripts\python.exe -B -m demo.run_demo --commands requests/commands.005/commands.005.json --start-object-library --viewer
```

场景装配和 MuJoCo 加载已通过，验证中间文件位于 `tmp/commands005_ur3_layout_piper/runtime/`。此版本尚未执行完整任务；此前修改过物体布局的试跑结果不适用于本版本。

`check_layout.py` 会检查网格间距和固定姿态 IK 样本；本版本保留源场景物体位置，检查失败时应调整机器人基座或抓取策略，不应挪动物体来满足检查。
