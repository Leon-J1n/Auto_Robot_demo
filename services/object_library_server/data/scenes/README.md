# 场景 JSON 规范

这些场景严格按 `ur5e_work_table_scene.json` 模板编写。平台读取 `MetadataRef.path` 的 OLS 形式 `$LIBRARY_SERVER/<id>`；本地查看器用 `MetadataRef.type` 区分 `robots/<id>` 与 `models/<id>`，再通过对应的 `*_index.json` 固定解析到本地模型。

## 组件规则

1. `AmbientLight` 必须放在 `objects` 第一个，便于平台先建立光照。
2. 普通对象使用 `Transform.position` 和 `Transform.quaternion_xyzw`。`parent` 是场景对象 ID。
3. 有 `RobotAttachment` 时，连接位姿只由 `RobotAttachment.transform` 决定。此时 `Transform.position` 和 `Transform.quaternion_xyzw` 必须省略，`Transform.scale` 保留，`Transform.parent` 与 `Attachment.parent_object_id` 保持一致。
4. `RobotAttachment.transform` 中的四元数顺序是 `quaternion_xyzw`，`reference_frame` 写明连接参考坐标系。夹爪、末端相机等固定连接件都使用此规则。
5. 机械臂的 `RobotDriver` 写出 `joint_sequence`、`joint_limits`、`initial_joint_positions_rad` 和 `available_connect_joints`。查看器按初始关节角渲染。
6. 关节名称和限制必须逐项核对当前 OLS 对应模型的 URDF。驱动序列不含固定关节、底座或 mimic 跟随关节；夹爪只列独立驱动关节，不把连杆被动关节当作开合控制。
7. `MeshRenderer.properties` 不设置 `forceOverrideColor` 或其他 `forceOverride` 参数，使用模型自身材质；无其他设置时保留空对象 `{}`。
8. 场景光照显式写入 JSON：环境光 `Ambient` 强度 0.5，顶部 `Directional` 强度 0.7。光源的局部 -Z 为照射方向；查看器不再额外添加隐藏灯光。
9. `RobotAttachment.transform` 是真正相对 `parent_link` 的变换，不是相对整个机械臂根节点。查看器按初始关节角做 URDF 正向运动学，再应用连接位姿。`child_link` 必须真实存在；D435i 的资源入口现为 `d435i.urdf`，根链接为 `camera_link`，原有网格原点不变。
10. 历史位姿曾按机器人根坐标系预览。此次已换算为末端链接坐标系并验证初始化世界位姿不变；以后不能把根坐标系的位置直接写成 `reference_frame: parent_link`。
11. 顶部全景摄像机保持垂直向下，相对于原始朝向绕世界 Z 轴旋转 +90°；当前 `quaternion_xyzw` 为 `[0.7071067811865476, 0.7071067811865476, 0, 0]`。以后从场景 1 复制该朝向，避免重复累加旋转。

## 资产材质统一规则

- OBJ/MTL 是外观的权威来源。纯色路线只用 MTL 的 `Kd`；`Ka` 与 `Kd` 相同，分别供环境光与漫反射使用，不是两套不同配色。URDF 为兼容保留同值的 `color rgba`，不得再写不同颜色。
- 纹理路线要求存在真实图片、有效 `vt`，且每个贴图面的 UV 索引有效。颜色来自 `map_Kd` 图片，MTL 的 `Ka/Kd` 为中性白色，URDF 不再额外覆盖颜色。
- 2×2/4×4 的单一颜色图转成纯色材质，不保留图片贴图引用。迁移时若 URDF 有明确的部件配色，以它恢复颜色，再写入 MTL；没有时用原 MTL 或纯色图的颜色。14 号臂杆保留原始灰色，夹爪恢复黑灰配色，不虚构原资产没有的贴图。
- 纯色网格移除冗余 `vt` 及面上的 UV 引用。所有面必须有有效法线索引；补法线保留原平滑组，未指定平滑时按面法线处理，保留已有有效法线。零面积面不形成有效表面，已清理。
- `*_files.json.reference` 从入口文件递归收集 URDF → OBJ → MTL → 图片的真实依赖，不能按文件名猜测哪些图片是纹理或预览。
- 摄像机入口从 OBJ 补为 URDF 后，OLS 资产 ID 不变；上传时同步更新资源说明文件及依赖，不能只替换场景 JSON。

检查程序：`python world_model/robotsim/assets/final_models/test/validate_assets.py`。结果保存在 `test/asset_validation.json`；修复前备份和修复记录也保存在 `test`，不属于 OLS 场景/模型资源。

## 文件布局

每个场景放在自己的数字 ID 文件夹中，JSON、1280×1280 预览图和 128×128 缩略图同名：

```text
scenes/<scene_id>/<scene_name>.json
scenes/<scene_id>/<scene_name>.png
scenes/<scene_id>/<scene_name>_thumbnail.png
scenes/<scene_id>/<scene_name>_file.json
```

`<scene_name>_file.json` 只包含 `is_on_cos`、`path` 和 `thumbnail`，路径相对于当前场景文件夹，不列高清图。查看器查找场景 JSON 时必须排除这些资源说明文件。

`scene_index.json` 只记录场景 ID、场景名称和任务类别；场景 JSON 内部仍完整保留平台模板字段。场景名应简短描述内容；任务类别使用 `抓取`、`开柜`、`拉抽屉`、`插拔`、`空白`、`按压`、`旋转扭动`、`推拉` 等固定值。

场景 6 `ur5e_table_twist`：按钮 33、螺丝刀 13、瓶子 32、杯子 14；场景 7 `ur5e_table_push_pull`：微波炉 31、抽屉 26。两者继承场景 1 的机器人、夹爪、相机、底座和光照；新增对象按实际网格底面放置在桌面，保留模型 URDF 内的操作关节。

## 本地查看

在仓库根目录运行：

```powershell
python world_model/robotsim/assets/final_models/scenes/view_scene.py 1
```

加 `--screenshot <文件.png>` 可输出渲染图。修改 JSON 后重新运行即可查看；查看器会自动把 `$LIBRARY_SERVER/<id>` 映射到 `final_models/robots` 或 `final_models/models`。

