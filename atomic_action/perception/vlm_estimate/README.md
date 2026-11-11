# VLM point estimation

## 抓取估计命名

两套计算逻辑统一使用以下名称：

| 名称 | 抓取位置 | 抓取姿态 |
| --- | --- | --- |
| 平面抓取估计 | VLM 抓取像素结合 RGB-D 深度回投得到表面点，再搜索抓取深度 | 沿世界竖直向下接近；细长物体根据长轴的水平投影调整夹爪朝向 |
| 垂面抓取估计 | VLM 抓取像素结合 RGB-D 深度回投得到表面点，沿朝外法向固定抓浅 1.5 cm | 将抓取点转换到 target 局部坐标系，匹配其局部 AABB 的最近表面；将朝外法向转换回世界坐标，沿反法向接近，长轴暂取与表面平行的水平方向 |

“平面抓取估计”指已有的桌面向下抓取逻辑；“垂面抓取估计”指当前约定的侧面抓取方案。
垂面方案已接入 target 局部 AABB 法向估计，默认执行流程不再依赖面板点云拟合。
这里的名称不要求修改 `pick` 命令参数。

`VLMPointEstimator` accepts one synchronized RGB-D frame. RGB is `uint8` in
RGB channel order, depth is in meters, and the camera pose is in the MuJoCo
world frame. The VLM selects a normalized image point; the selected pixel's
depth and camera intrinsics/pose produce a world-frame 3-D point. Ordinary
grasp/place requests return a point estimate. Handle requests additionally
recover an orientation from local depth geometry; feasibility is checked by
the task resolver and motion planners.

## Drawer handle grasping

当前抽屉使用**垂面抓取估计**，`pick` 参数保持原样：

```json
[
  {"command_id":"1","skill_name":"approach","parameters":{"target":9}},
  {"command_id":"2","skill_name":"pick","parameters":{"target":9,"grasp_index":0,"hold_force":10.0}},
  {"command_id":"3","skill_name":"pull","parameters":{"frame":"grasp","direction":[0,0,-1],"distance":0.16}}
]
```

场景 `Semantic.semantic_name: drawer` 自动选择垂面方案。其他目标仍可通过已有的
`grasp_type: handle` 显式选择；`top_down` 使用平面抓取估计。
所有抓取模式统一使用 `vlm_pick.json`；垂面计算使用返回的抓取表面点和置信度，通过 RGB-D 得到世界坐标。
法向计算在 resolver 中进行：

1. 从对象库取得 target 的局部 AABB。六元素格式为
   `[x_max,x_min,y_max,y_min,z_max,z_min]`；三元素格式表示以原点为中心的尺寸。
2. 使用实测 target 世界位姿，将抓取点变换到 target 局部坐标系。
3. 计算点到各有限侧面的最近距离。世界法向 Z 分量绝对值大于 0.5 的面
   不作为垂面候选；最近两个侧面的距离差不超过 2 mm 时，返回
   `AABB_FACE_AMBIGUOUS`，避免在棱角处任意选择。
4. 将选中面的朝外法向变换到世界坐标。抓取 Z 轴取反法向，X 轴取
   `normalize(world_up × normal)`，Y 轴取 `Z × X`，保持把手长轴水平。

垂面抓取不搜索抓深。以 RGB-D 表面点为基准，沿朝外法向固定偏移 15 mm
作为夹爪闭合中心，即抓取 Z 方向的深度为 −15 mm。该位置只尝试两个相差
180° 的夹爪朝向，保留 IK、张开夹爪碰撞和接近路径检查；不可行时返回失败，
不改变抓取深度。缓存复用时不重复偏移。平面抓取继续使用原有深度搜索。
`grasp_shallow_offset_m` 记录 15 mm 保守距离，`depth_search_enabled` 为 false。

平面和垂面视觉抓取的 `approach` 运动成功后，自动获取当前完整腕部 RGB-D，
让 VLM 重新定位同一目标的可见抓取点，不投影原抓取点、不裁剪局部区域。
两种方案均保留已选夹爪朝向。
垂面新表面点仍沿原朝外法向偏移 1.5 cm，不进行抓深搜索。
平面从缓存闭合中心减去已选抓深，恢复原表面点用于偏移检查；在精定位后的
表面点上重新执行原有抓深搜索及桌面净空检查（包含原桌面底限兜底），不重复叠加旧抓深。
平面二次感知不要求长轴参考点，首次估计的朝向继续使用。
置信度须至少 0.5，三维坐标必须有效。精定位点与粗定位点的偏移记录在
`wrist_refinement.correction_world_m` 中，但不再用固定距离阈值否决精定位点。
遮挡或无法确认目标时，精定位仍可能失败；置信度不能保证 VLM 一定识别同一把手。
修正位姿必须通过基于当前关节状态的 IK、张开夹爪碰撞及接近路径检查，
通过后才替换缓存。若取得精定位点但运动可行性检查失败，`approach` 报错并停止
后续 `pick`；若采图或感知失败，保留粗定位缓存，报告记录
`wrist_refinement.status: fallback_to_coarse` 及失败原因。
此策略同时适用于平面和垂面抓取。无需新增命令参数。
报告保存 `coarse_target_resolution` 及 `wrist_refinement` 的相机、完整图像模式和偏移信息。
默认 VLM 客户端网络超时为 45 秒，关闭 SDK 自动重试；这不是整个精定位流程的总时限。
终端不输出精定位阶段日志；界面状态显示采图、VLM 定位、IK/碰撞/接近路径验证阶段；报告记录
`timing_s` 和 `elapsed_s`，失败详情包含 `substage`，用于区分网络等待与规划耗时。

`approach` 缓存精定位后的位姿，`pick` 复用并执行闭爪确认；`pull` 使用实测
`grasp_center` 坐标系执行，不再次感知。`frame: grasp` 的 `[0,0,-1]` 为拉出方向。
报告中的 `orientation_source: target_local_aabb`、`aabb_estimate` 和
`surface_normal_world` 记录法向来源、局部抓取点、各面距离和选中侧面。

AABB 是近似几何：它不能修正 VLM 选错物体或表面的错误，也不能表达曲面法向。
对象库 AABB 必须与 target 位姿使用同一模型局部坐标系。
旧的 `point_type="handle"` 点云拟合接口保留供独立对比测试；当前抽屉流程
调用 `point_type="vertical"`，不请求长轴或面板参考点。

## Grasp surface references and yaw

`vlm_pick.json` now requests `grasp_point`, `confidence`, `is_elongated`, and
`axis_points`. For elongated objects, `axis_points` contains two normalized
`{x, y}` references on the visible surface, on either side of the grasp point.
For curved objects these describe the local segment being grasped, rather
than the line between the object's most distant tips. For symmetric or
ambiguous objects the prompt requests `is_elongated: false, axis_points: null`.
Legacy top-level `{x, y, confidence}` responses remain supported with fixed yaw.
The place prompt and its point-only response are unchanged.

The estimator back-projects each reference using its own depth, camera
intrinsics and pose, then projects their difference onto the world XY plane.
It returns `axis_status`, a Chinese `axis_reason` when unavailable, and the
reference pixels/depths/world points. Valid axes also include `long_axis_world`
and `axis_length_m`. This calculation does not read model AABB or object pose.

Axis checks require exactly two references with finite normalized coordinates,
valid positive metric depth at each endpoint, and at least 5 mm of world-XY
separation. The estimator does not sample depth along their connecting line,
compare endpoint/intermediate heights with the grasp point, or require the line
to pass near the grasp pixel. This avoids rejecting curved objects merely
because their straight image chord crosses background. These are numerical
checks, not semantic segmentation or proof of object membership. Invalid axes trigger at most
one extra VLM request on the same RGB-D frame; if still unavailable, grasp yaw
falls back to the existing fixed orientation with the reason recorded.

`demo/interaction/vlm_grasp.py` uses a valid axis as grasp-frame X, keeps Z
vertically downward, and thus places jaw-opening Y across the axis for the
current Robotiq grasp-frame calibration. It offers two yaw variants separated
by 180 degrees to the existing IK/collision/joint-travel selector. Both denote
the same logical grasp (index 0); the selected variant is cached so approach
and pick reuse it. Cached poses continue to follow target motion. Fixed grasp
and place orientations both use Rz(+90 degrees) @ Rx(180 degrees), keeping
grasp-frame Z vertically downward. Model AABB remains in use for placement
calculations, but not for grasp-point acceptance, yaw or grasp-depth selection.
The resolver accepts the selected RGB-D grasp point without model-bound or
visible-surface checks and without a point-reselection retry. Basic normalized
coordinate and valid metric depth checks remain necessary for back-projection.
Axis-reference validation/retry is separate and remains in place.

## Placement estimate lifetime

Each `put` command starts a distinct placement cycle, identified by that put's
command ID. Its nearest bound `approach` and `retreat` share the cycle ID, as
inferred from the command sequence without extra command-file configuration.
The first placement-pose request in that cycle captures a fresh RGB-D frame,
runs VLM placement perception, and calculates release height for the current
held object. Repeated requests within that cycle reuse the estimate and follow
destination motion. A later put to the same destination must perceive again;
failed new perception cannot fall back to a prior cycle's cached placement.
Direct puts without an approach also start a new cycle. Grasp caching is
unchanged. `placement_cycle_id` in blackboard placement evidence identifies
which put owns the current cache. Calls without a command/cycle context do
not reuse a placement cache across requests.

`put` also accepts horizontal `relation` (`front`, `behind`, `left`, `right`)
and nonnegative `distance_m`. The put owns these parameters; command binding
passes them to its nearest matching approach, which perceives once for both.
Directions follow robot-base horizontal axes, as for object-relative move_to.
The resolver projects the destination's oriented model bounds onto that
direction, offsets outward from its boundary, then crops the live RGB-D to
a 2 cm radius around the requested position. The shared put prompt receives
the requested region as runtime context. Surface depth comes from that
external region; the existing world-Z height search still applies. Any random
recovery candidates remain inside the requested region. An unobserved region
returns `PLACEMENT_REGION_UNOBSERVED`; it never reuses an interior destination
point. Omitted relation (or `at`) keeps ordinary internal placement.
`above` and `below` instead use world Z and the destination's oriented top or
bottom model boundary. They produce an exact spatial release target at the
requested offset, including unsupported positions in free space. They do not
request VLM perception or run the 0-10 cm placement height search; ordinary
IK, collision, and motion planning checks still apply.

Placement perception errors, or failure of all 11 height candidates at the
VLM point, enable a bounded fallback in `demo/interaction/placement_fallback.py`.
It samples at most five distinct valid RGB-D pixels in the destination's
central ellipse with radii of 30% of the model size on each local XY axis, excluding the exact center and its
tiny neighborhood. Each point uses its measured depth and the same ascending
0–10 cm height search with IK/endpoint collision checks. It adds no closure
simulation or duplicate path preflight. The selected fallback is shared only
within the current approach/put cycle. `placement_center_fallback` records
the trigger, sampled points, and height attempts; total failure preserves
both primary and fallback rejection diagnostics internally. User-facing errors
report placement failure causes without describing the fallback. Grasp perception is unchanged.

## Grasp-depth search

The resolver caches the RGB-D surface anchor first, then searches offsets of
30, 20, 10, 0 and -10 mm (a 10 mm step) along the grasp approach axis.
Negative depth places the grasp center above the selected surface point;
-10 mm is 10 mm above it for the downward grasp orientation.
The top of named static table geometry (including its visible surface when
the collision proxy is thinner) below the target is used as
an absolute world-Z floor. After physical closure in the isolated trial, the
lowest point across all Robotiq collision links must be strictly above that
floor; touching or going below fails the candidate. The measurement uses
runtime geometry and transforms, not object metadata AABB. Missing table or
gripper geometry reports an explicit error. Table height, closed-tool minimum
height and clearance are retained in `table_clearance` diagnostics.
No object dimensions, AABB thickness or finger-length formula prunes this list.
For each depth, yaw variants and IK branches are checked before moving to a
shallower depth. Endpoint IK/collision checks are followed by a collision-checked
vertical contact path from a prepoint 100, 70, 40 or 30 mm above the goal, and
opening/closing confirmation on an isolated MuJoCo model/data copy. Confirmation
must concern the requested target, not a neighboring object. The live robot is
not moved during this search. The transit to the prepoint and actual execution
continue through the ordinary motion planners and contact monitoring.

The deepest candidate that passes preflight is cached with its yaw. Subsequent
commands reuse that pose and maintain it as the target moves; they do not
reselect depth. This preflight adds computation to the first pose request and
does not guarantee that the later physical grasp will succeed. Failures across
all depths raise `GRASP_DEPTH_SEARCH_FAILED` and retain per-depth diagnostics.
Blackboard estimate evidence records `depth_selected`, `grasp_depth_offset_m`,
`grasp_depth_candidates_m`, and `grasp_depth_attempts`.

The estimator has a default DashScope API key configured in `estimator.py`.
`DASHSCOPE_API_KEY` overrides it, and `QWEN_MODEL` optionally overrides the
default `qwen3.8-flash` model. Treat the source file as credential-bearing.

Chinese task prompts are loaded from `prompts/vlm_pick.json` and
`prompts/vlm_put.json`. Each file defines `target`, `description`, and
`respond`, with no additional fields. Target names are passed through unchanged.
All grasp modes share `vlm_pick.json`; ordinary and relative placements share
`vlm_put.json`. Runtime context supplies the requested region or prior point
projection without loading dedicated prompts. Edit these two JSON files to
change the VLM instructions.

```python
from atomic_action.perception.vlm_estimate import VLMPointEstimator

frame = runtime.get_camera_frame("global")
result = VLMPointEstimator().estimate_frame(
    frame, point_type="grasp", target="apple"
)
world_point = result["world_point"]
```

The same operation is available via `LocateAction` with
`localization.strategy: vlm_estimate`, `target.type: grasp | place`, and
`target.object` (or `target.category` / `strategy_params.target_description`).
The Locate result has `point_only: true` and `target_pose: null` because this
strategy does not estimate orientation or run the unfinished pose validator.
