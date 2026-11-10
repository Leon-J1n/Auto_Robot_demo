# VLM point estimation

`VLMPointEstimator` accepts one synchronized RGB-D frame. RGB is `uint8` in
RGB channel order, depth is in meters, and the camera pose is in the MuJoCo
world frame. The VLM selects a normalized image point; the selected pixel's
depth and camera intrinsics/pose produce a world-frame 3-D point. This is a
point estimate, not a gripper orientation or collision-checked grasp pose.

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

Placement perception errors, or failure of all 11 height candidates at the
VLM point, enable a bounded fallback in `demo/interaction/placement_fallback.py`.
It samples at most three distinct valid RGB-D pixels in the destination's
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
`respond`; `target_names` maps known English scene names to Chinese. Edit
these JSON files to change the VLM instructions.

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
