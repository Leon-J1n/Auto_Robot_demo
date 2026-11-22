---
name: operate-sim-platform
description: Operate and validate RobotSimPlatform simulation scenes through its Python SDKs, Live Python Control, GUI editor host, or native APIs. Use when asked to create, load, inspect, edit, or control a simulation scene; manipulate robots, joints, grippers, sensors, primitives, transforms, physics, collisions, aliases, or saved state; connect to or launch a headless/GUI simulation Session; or implement missing scene-control capabilities needed to complete such operations.
---

# Operate RobotSimPlatform

Use project documentation as the source of truth and this skill as the workflow router. Keep scene operations portable, observable, and reproducible.

## Resolve `SIM_PLATFORM_ROOT`

Treat `SIM_PLATFORM_ROOT` as a symbolic repository root, never as a fixed drive or machine path.

Resolve it in this order:

1. Use an existing `SIM_PLATFORM_ROOT` environment value only if it points to a directory containing `CMakeLists.txt`, `pyproject.toml`, and `docs/API_PYTHON_QUICK_REFERENCE.md`.
2. Otherwise run `git rev-parse --show-toplevel` from the current workspace and validate the same markers.
3. Otherwise inspect the available workspace roots for those markers.
4. Stop and ask for the repository location only if no validated root can be found.

Use `SIM_PLATFORM_ROOT/...` in reasoning, instructions, and durable project artifacts. In executable code, derive the root from `Path(__file__)`, a CLI argument, or a validated environment value. Never write a machine-specific absolute repository path into the skill or new reusable code. Treat user-provided external asset paths as inputs and expose them as CLI arguments when producing reusable scripts.

## Route Documentation

Read `SIM_PLATFORM_ROOT/docs/README.md` first when the task spans unfamiliar subsystems. Then read only the sources needed for the operation:

| Need | Read |
|---|---|
| Python scene, robot, sensor, state, GUI, or Live APIs | `SIM_PLATFORM_ROOT/docs/API_PYTHON_QUICK_REFERENCE.md` |
| Existing Session, launch/connect behavior, aliases, object proxies, or transport | `SIM_PLATFORM_ROOT/docs/LIVE_PYTHON_CONTROL_DESIGN.md` |
| Build, dependencies, binary locations, tests, or environment failures | `SIM_PLATFORM_ROOT/docs/HANDOFF_GUIDE.md` |
| Find the implementation layer or add a missing capability | `SIM_PLATFORM_ROOT/docs/IMPLEMENTATION_STRUCTURE.md` |
| Native C++ or stable C ABI operations | `SIM_PLATFORM_ROOT/docs/API_CPP_QUICK_REFERENCE.md` |
| Collision backend work | The matching backend document under `SIM_PLATFORM_ROOT/docs/` |

Search headings or API names before opening large sections. Do not read every document by default.

Consult the closest existing example before writing code:

- Live headless/GUI scene construction: `SIM_PLATFORM_ROOT/examples/python/test_live_python/jaka_wsg50_live.py`
- Existing Live Session joint commands: `SIM_PLATFORM_ROOT/examples/python/test_live_python/set_jaka_joint_angles_live.py`
- GUI joint target and convergence logic: `SIM_PLATFORM_ROOT/examples/python/set_jaka_joint_angles.py`
- Robot/gripper/sensor hierarchy: `SIM_PLATFORM_ROOT/examples/python/create_jaka_wsg50_d435_scene.py`
- Physics and primitives: `SIM_PLATFORM_ROOT/examples/python/kick_jaka_ball.py`
- Collision benchmarks: `SIM_PLATFORM_ROOT/examples/python/benchmark_collision_grid_*.py`

## Choose The Control Surface

Choose one ownership model and keep its handles within that model:

| Situation | Use |
|---|---|
| Modify a running GUI/headless instance or preserve state across Python processes | `sim_platform.live` proxies |
| Launch a new remotely controllable instance | `LaunchSpec` with `launch()` or atomic `connect_or_launch()` |
| Connect only when an instance already exists | `connect()`, `connect_named()`, or `list_instances()`; never call a launch API |
| Run an isolated, in-process headless batch | `sim_platform.sdk` `Simulator`/`World` |
| Use GUI-only editor capabilities not exposed by Live | `EditorSession`, while stating that this is not a Live validation |
| Modify core, host, backend, or C ABI behavior | Native C++/C API plus the corresponding Python wrapper when applicable |

Never mix `World` entities, `EditorSession` integer handles, and Live `ObjectRef` proxies.

## Execute Scene Operations

1. Inspect repository status, requested assets, active instances, and the nearest examples. Preserve unrelated user changes.
2. Determine whether the request means connect-only, always launch, or connect-or-launch. Do not infer launch permission from a connect-only request.
3. Resolve scene objects by explicit aliases in Live workflows. Use `ensure_robot(path, alias=...)` for idempotent creation and `require_robot(alias)` for existing-only control.
4. Build targets from `robot.joint_names`; never assume a fixed joint is absent or rely on numeric joint order. Convert degrees to radians at the API boundary.
5. Load an articulated gripper with `ensure_robot`/`load_robot` when its joints must move. Attach its root to the scoped robot link with `preserve_world=False`, then set the intended local transform.
6. Use `create_primitive()` for visible/collidable primitives. A basic entity alone is not a Cube or other renderable primitive.
7. Start or resume simulation only when motion is required and the request permits it. Use bounded polling with a tolerance and deadline when claiming a joint reached its target.
8. For repeating commands, use a clear interval and a stoppable loop. Keep the controller process alive when the user needs ongoing GUI acceptance.
9. Put reusable task scripts under `SIM_PLATFORM_ROOT/examples/python/` in a task-specific directory. Derive project paths portably and expose external models as arguments.

For a new multi-object scene workflow, validate headless first, then launch the GUI using the same Live scene-building logic when visual acceptance is requested. For an operation on an existing Session, work in that Session and do not create a replacement scene.

## Respect Live Semantics

- Python variables do not persist across processes; server aliases and object IDs do.
- `set_parent(..., preserve_world=False)` preserves the local transform and is appropriate for attachments. `True` preserves the world transform and is appropriate for interactive reparenting.
- Use `pid:<PID>` only for local discovery. Use a connectable TCP endpoint for transport.
- Do not connect to `0.0.0.0`; it is a bind address. Do not expose unauthenticated, unencrypted Live endpoints to untrusted networks.
- Allow long enough request timeouts for URDF and collision-mesh import, especially in GUI hosts.
- Treat capabilities and actual source implementations as authoritative when documentation and runtime behavior differ.

If a required Live API is missing, do not silently switch to a legacy SDK and claim Live completion. Implement the smallest common capability in:

- `SIM_PLATFORM_ROOT/python/sim_platform/live.py`
- `SIM_PLATFORM_ROOT/python/sim_platform/server.py` for headless
- `SIM_PLATFORM_ROOT/src/editor.cpp` for GUI when the GUI host must support it
- focused tests under `SIM_PLATFORM_ROOT/python/tests/`

Keep old RPC response fields when adding Live `object` references if existing `EditorSession` callers depend on them.

## Verify Proportionally

Run the smallest matrix that proves the requested behavior:

1. Compile changed Python files with `python -m py_compile`.
2. Run focused Python tests, commonly `python -m pytest python/tests/test_live.py python/tests/test_server.py -q`.
3. Rebuild `sim_editor` when `src/editor.cpp` changes, using the environment-specific commands from `SIM_PLATFORM_ROOT/docs/HANDOFF_GUIDE.md`.
4. Execute the real headless workflow and report observed state: loaded assets, hierarchy/aliases, joint targets versus actuals, loop count/interval, simulation status, and exit behavior.
5. Launch GUI only after headless success when GUI acceptance is required. Confirm both editor and controller remain alive and inspect logs for ongoing control.
6. Run `git diff --check` and review the final changed-file set. Do not include generated caches, binaries, state dumps, or logs unless explicitly requested.

Do not claim visual or physical success from RPC return values alone. Verify state or motion through joint state, transforms, hierarchy, simulation time/status, logs, or direct GUI inspection as appropriate.

## Report Results

State which control surface and instance were used, what changed in the scene, concrete verification evidence, reusable script locations under `SIM_PLATFORM_ROOT`, and any running GUI/controller processes left for acceptance. Report skipped verification and residual limitations explicitly.
