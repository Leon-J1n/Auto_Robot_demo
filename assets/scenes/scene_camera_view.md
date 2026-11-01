# 场景照片统一视角

所有 `final_models/scenes/<id>/` 的主图和缩略图都使用
`capture_scene_photos.py` 生成。相机从机械臂所在方向朝向桌面，保持水平
方位 `azimuth = 12°`，从上向下 `elevation = -35°`，目标点为
`lookat = [0.48, 0.00, 0.45]`，距离为 `3.10 m`。这个视角让机械臂位于
画面前方中央，桌面完整可见，桌面物体不会被完全遮住，构图与参考照片一致。

背景使用纯白 skybox；主图为 `1280×1280`，缩略图为 `128×128`。相机参数集中
保存在 `scene_camera_settings.json`，以后可直接修改该文件后重新运行：

```powershell
python assets/scenes/capture_scene_photos.py
```

只重拍某些场景时，例如只重拍 2 和 4：

```powershell
python assets/scenes/capture_scene_photos.py --scene-id 2 4
```

输出文件会覆盖每个场景目录中的 `<scene_name>.png` 和
`<scene_name>_thumbnail.png`，场景 JSON 和模型文件不改变。



