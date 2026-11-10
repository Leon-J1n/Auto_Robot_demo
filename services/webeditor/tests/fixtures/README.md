# Browser geometry fixtures

`transformed.dae` and `phong_cube.fbx` are copied unchanged from the original
RobotSimPlatform `tests/data` fixtures for real Three.js loader regression checks.
The standalone runtime does not access that repository. The DAE has two mesh
nodes with nested transforms; the FBX is an actual binary model.

# Contract fixtures

`robot_grasp.traj` is a verbatim copy of the user-maintained file under `docs/`.
`scene_example.json` is the copy that outlived its `docs/` twin: the reference
scene the documentation uses now is `docs/ur5e_table_empty.json`, which
`editor/src/world.test.ts` reads in place, so what the doc shows is what the
parser is tested against. These files are the immutable inputs for the world
parser, the TRAJ decoder and the renderer tests; never edit them here or in `docs/`.

# Object library

There is no stand-in here any more. `ols_stub.py` and `tests/test_ols_stub.py`
were retired when ObjectLibraryServer itself was written (`F:\ObjectLibraryServer`,
`python server.py --port 8000`); `tools/serve_verification.py` and
`start-dev.ps1` start or reuse that service instead. The library failures the
stub used to provide on purpose - `424 DEPENDENCY_FETCH_FAILED` for a missing
dependency, a texture outside `allowed_extensions` refused with `403` - are
covered by `library.test.ts` against a fake `fetch`, so they need no server to
reproduce.

`ols_data/` is the sample library the stub generated; it is gitignored, and
nothing reads it now.
