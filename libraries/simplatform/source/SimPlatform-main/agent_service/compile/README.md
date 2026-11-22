# compile

Deterministic only. `SkillPlan` + scene plan + interaction metadata + profile
always produce identical commands, and nothing here calls a model.

## scene_fingerprint

The source stack hashed the generated `scene.xml` file. We no longer write one
scene file per task: Route A composes objects onto a base `.simstate`, and Route
B restores a saved scene as-is. Hashing the base file alone would give every
Route A task the same fingerprint.

`scene_fingerprint` is therefore a SHA-256 over a canonical JSON serialization
(sorted keys, no incidental whitespace, UTF-8) of:

- `base_scene_digest` — SHA-256 of the base `.simstate` contents
- `placements` — ordered object placements: id, body name, model, world
  position and footprint rounded to 6 decimals, orientation, movability
- `robot`, `route`, and the tabletop spec when one is created

Output paths, task directories and binding labels are excluded, so recompiling
the same plan elsewhere yields the same fingerprint. The field name and the
`^[0-9a-f]{64}$` shape are unchanged, so the protocol validators still pass.

A missing base scene hashes a path-derived placeholder rather than a constant,
so it can never collide with a real scene's digest.
