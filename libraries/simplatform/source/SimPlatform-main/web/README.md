# RobotSimPlatform WebEditor

This workspace contains the replacement Browser WebEditor and Web Gateway. It does not depend on the removed `WebEditor/` or `WebServer/` prototypes.

## Development

Build the native `sim_platform_c` target once, install dependencies when the lockfile changes,
then start both development services from the repository root:

```powershell
python -m pip install -e ".[web]"
cd web
corepack pnpm install --force
cd ..
.\Start-WebEditor.ps1
```

Stop the managed Vite and Gateway processes with:

```powershell
.\Stop-WebEditor.ps1
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` and `/events` to the Gateway.
`Start-WebEditor.ps1` starts Gateway and Vite, exporting `PYTHONPATH`, `SIM_PLATFORM_LIBRARY`, and
`SIM_PLUGIN_DIR` first. It starts Vite directly and never runs the production build.

The Agent Service is a separate process reached over HTTP on port 8765. Gateway probes
`GET /health` and treats `{"status": "ok"}` as connected; `/api/health` reports the result as
`agentConnected` and `/api/status` as `agent.connected`. Nothing else about the service is assumed,
so any implementation of the `/v1/conversations` surface in `web/gateway/agent.py` can serve Web
turns. While no service answers on that port, the Agent panel loads but turns fail with a transport
error; the rest of the WebEditor is unaffected.

Gateway hands each turn a JSON `controlContext` (gateway base URL, per-job control token, job id,
scene id, sim session id, scene generation, Live endpoint, presentation mode, robot assets) instead
of a prose prompt. The service uses those values to call back into `/api/agent-control/*` to publish
snapshots, timelines, and captures.

## Feature configuration

All visible WebEditor capabilities are controlled by `web/config/features.json`. Switches use
dot-separated paths and boolean values. A disabled parent disables all descendants, so these are
both valid:

`navigation` controls the narrow vertical rail at the far left of the workspace. Its child switches
`navigation.scene`, `navigation.details`, `navigation.settings`, and `navigation.assets` control the
Scene, Details, Settings, and Assets buttons in that rail. A button is also hidden when its matching
`panels.*` panel is disabled.

```json
{
  "version": 1,
  "features": {
    "panels.scene": false,
    "viewport.tools.move": false,
    "panels.details.physics.ifpsSphereCount": false,
    "agent.input.voiceInput": false
  }
}
```

The Web Agent rollout switches are `agent.conversation`, `agent.debug`,
`presentation.syncAndAnimate`, and `presentation.timeline`. They can be disabled independently.
`agent.debug` is disabled by default. When enabled, the right Agent panel displays the debug events
the Agent Service emits for a turn (`turn.debug`) for local troubleshooting. Treat that output as
developer-only data and restart Gateway after changing the switch.

The committed configuration contains the complete explicit default switch set.
Missing switches fail closed in the browser. Restart the Gateway after editing the file. To use a
different file, set `SIM_WEB_FEATURE_CONFIG` before running `Start-WebEditor.ps1`, or start the
Gateway with `--feature-config <path>`.

Feature switches control WebEditor visibility and interaction availability. They are not an API
authorization boundary.

Every future user-visible panel, command, field, mode, preview, or interaction must add a switch
to `web/config/features.json`, use that switch in the UI, and add coverage in the same change.

## Production

```powershell
cd web
corepack pnpm run build
cd ..
python -m web.gateway.server --port 8080
```

Start the Agent Service separately and point Gateway at it:

```powershell
python -m web.gateway.server --port 8080 --agent-host 127.0.0.1 --agent-port 8765
```

Health endpoints are `http://127.0.0.1:8765/health` and `http://127.0.0.1:8080/api/health`. Gateway
presentation and conversation bindings live under `.sim-cache/web/`; conversation and turn history
belong to the Agent Service. For rollback, stop every service, switch the repositories to the
previous local commits, and restart. Do not copy the persistence files between incompatible scene
instances; restored timelines are marked `detached` when their instance binding differs.

`GET http://127.0.0.1:8080/api/metrics` returns aggregate turn, capture, and WebSocket connection
counters. It never returns upstream session identifiers, tool calls, or runner trace.

Timelines have no frame-count limit. Timeline duration defaults to 60 seconds and payload size to
512 MiB of uncompressed JSON. Override the byte limit with `SIM_WEB_TIMELINE_MAX_BYTES`.
Gateway retains at most 1000 public Agent jobs by default; configure `SIM_WEB_AGENT_JOB_LIMIT`.
Gateway serializes Agent turns against the scene lock, so one turn drives the scene at a time
regardless of how much concurrency the Agent Service allows.

The production build is written to `web/dist`, which the Gateway serves when present.

## Lighting API

光源可从 Scene Hierarchy 选中并在 Details 面板编辑，也可通过 Gateway 命令创建/修改：

```json
{"command":"scene.create_light","params":{
  "type":"spot","name":"Spotlight","color":[1,1,1],
  "intensity":1.0,"range":10.0,"decay":2.0,
  "innerConeRadians":0.35,"outerConeRadians":0.7
}}
```

支持 `point`、`spot`、`directional`（平行/天空光）、`ambient`（环境光）四类。环境光对所有表面提供方向无关的基础照明。API 中的锥角和
`angularDiameterRadians` 使用弧度，UI 将聚光灯角度显示为度数。Spotlight 的局部 `-Z`
是发光正面；内外锥为半角，Gateway 拒绝不满足 `0 <= inner <= outer < pi/2` 的请求。
已有实体通过 `scene.update_entity` 的 `light` 对象修改；默认场景包含名为 `环境光` 的环境光源，避免未被方向光直接照亮的表面完全变黑。

## Verification

```powershell
cd web
corepack pnpm run test
corepack pnpm run build
corepack pnpm run e2e
```
