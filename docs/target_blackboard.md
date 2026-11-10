# Target blackboard

`execution.blackboard.TargetBlackboard` is shared by the demo's target resolver
and scene synchronizer. One run owns one board; a new run starts empty. Saved
snapshots are diagnostic records, not automatically restored physical state.

## Data and authority

- OLS provides model metadata and object-local interaction poses. It does not
  provide the current world pose of a moved scene instance.
- Model metadata is fetched on a cache miss, keyed by OLS URL, user ID and model
  ID. Friction initialization shares this cache. Failed requests remain retryable.
- Measured backend frames maintain world object/link poses, mechanism joint
  positions, gripper state and simulation time. Older timed observations cannot
  overwrite newer observations. Commands continue to use measured robot state
  for planning and collision checking.
- Target IDs are scene instance IDs. Instances of the same model share metadata
  but retain distinct world poses and interaction anchors.

## Interaction points on articulated objects

The resolver first combines OLS object-local interaction poses with the measured
object pose. It remembers the selected interaction frame and the current link
poses. When the gripper confirms physical holding, its observed contact body is
used to express that selected frame relative to the contacted link. Later queries
use the link's measured transform, including after release. Thus the door handle
follows the door rather than the stationary cabinet root.

This anchor is query bookkeeping only: it never attaches bodies or writes joint
positions into the simulator. Unselected candidates are not guessed to belong to
the same link. An articulated interaction point with no observed contact-link
association still uses the object-local OLS definition.

## Integration

`SceneStateSynchronizer(blackboard=board)` updates the board on synchronization.
When omitted, the synchronizer creates its own run-scoped board. The shared
task runner registers every scene instance and wraps **every command** in
`sync.command_state(...)`: it synchronizes before dispatch and observes the
actual endpoint in `finally`, including partial failures and commands that
emit no frames. This applies to all commands files, with no filename allowlist.
New dispatcher branches must remain inside this common command context.

Object interaction resolution, explicit link-local motion, and pour source /
destination queries all use the shared board. TCP-relative moves and named
robot joint targets still use their own appropriate robot state; they do not
need OLS object resolution. Push/pull remain target-free tool motions, while
their physical effects are observed by the same board.

The default command recorder also covers skills without a custom recorder;
direct grasp/release commands record their physical frames as well.
The demo also observes recorded execution frames and reads final backend state
on exit, including partial execution failures. `ObjectLibraryGraspResolver`
accepts that same board and recomputes world grasp/control poses from its state.

The output directory contains `blackboard.json`; `report.json` exposes its path
as `blackboard_file`. The snapshot includes targets, poses, mechanisms, gripper
state, cache hit/miss counters and confirmed interaction anchors. Target
resolution reports include `blackboard_revision` and each candidate's
`pose_source` (`blackboard_object` or `blackboard_contact_link`).

Run tests with:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_target_blackboard tests.test_blackboard_command_coverage
```
