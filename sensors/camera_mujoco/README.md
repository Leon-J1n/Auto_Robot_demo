# MuJoCo camera

`MujocoCamera` is a pull-based RGB-D adapter for a loaded MuJoCo model and
data object. It does not advance simulation time.

```python
from sensors.camera_mujoco import MujocoCamera

camera = MujocoCamera(runtime.model, runtime.data, "front_camera", width=640, height=480)
frame = camera.get_current_frame()
rgb = frame.rgb       # uint8, shape (480, 640, 3), RGB order
depth = frame.depth   # float32, shape (480, 640), metres
camera.close()
```

The camera must exist in the MJCF model, for example:

```xml
<camera name="front_camera" mode="fixed" pos="1 -1 1" xyaxes="1 1 0 0 0 1"/>
```

MuJoCo's renderer returns metric distance along the camera optical axis;
pixels at the far clipping plane are marked `NaN` because no surface was seen.
Call `mujoco.mj_forward(model, data)` first when
writing positions or joints directly into `data`.

`SceneRobotRuntime` reads the two camera definitions from the matching scene
JSON and adds them to the MuJoCo model in memory. Call
`runtime.get_camera_frame("global")` or `runtime.get_camera_frame("wrist")`;
the latter camera follows the gripper. `scene_json_path` can be passed to the
runtime constructor when the JSON cannot be inferred from the MJCF model id.
No scene asset is rewritten.
