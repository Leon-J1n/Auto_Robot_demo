# protocol

Wire contracts ported from `robot_agent_protocol`. Pydantic only, no simulator
imports, no MuJoCo.

## Decision: spatial sources are platform entities

The source stack typed `spatial.source.type` with MJCF concepts —
`body`, `site`, `geom`, `joint`. RobotSimPlatform exposes only named scene
entities, so those four kinds have no distinct meaning here.

`InteractionRegistryDocument` therefore normalizes on load:

- `type` becomes `entity` (`ENTITY_SOURCE_TYPE`) for every object.
- A legacy kind is preserved as `legacy_type` for diagnostics only; no layer
  branches on it.
- An unknown `type` is rejected rather than silently passed through.

Downstream code resolves `source.name` against the bound scene's entity names.

## Decision: coordinate frame

`coordinate_frame` defaults to `platform_world`. Registries authored against the
stack say `mujoco_world`; that value is accepted and rewritten to
`platform_world` on load, so only one frame name reaches planning and runtime.

## Error codes

`ErrorCode` carries the stack's 17 members plus runtime/model codes the stack
raised as bare strings: `TARGET_DEPTH_NOT_REACHED`, `TARGET_NOT_ACTUATED`,
`LOCAL_COLLISION_DETECTED`, `SENSOR_DATA_UNAVAILABLE`, `PUSH_PULL_UNSUPPORTED`,
`IK_FAILED`, `MODEL_UNAVAILABLE`.
