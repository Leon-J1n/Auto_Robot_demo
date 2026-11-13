/*
 * §10.6 / §12.9: the end-to-end pass over the v2 caller surface.
 *
 * Run it against the disposable gateway, never against a gateway that owns real
 * data - tools/serve_verification.py stamps every response with
 * X-Standalone-Verification and this script refuses to continue without it:
 *
 *   python tools/serve_verification.py
 *   $env:WEB_GATEWAY_URL='http://127.0.0.1:8098'; npm run dev -- --host 127.0.0.1 --port 5188
 *   node tools/browser_smoke.cjs                     # PLAYWRIGHT_MODULE may point at an install
 *
 * playwright is deliberately not a dependency here (it drags a 118 MB browser into the
 * tree). After a clean node_modules: `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install
 * --no-save playwright` then `npx playwright install chromium` - the cached build must
 * match node_modules/playwright-core/browsers.json, not just exist in ms-playwright.
 *
 * Covered: §12.1 (Load Scene + the geometry OLS serves for the fixture's
 * MetadataRefs), §4.6 (a preset scene from the asset panel), §12.4 (POST
 * /updateTraj, the 340/530 frame changes), §12.4b (a multi-clip batch and the toolbar
 * trajectory dropdown, Q99/Q100), §12.5 (/captureScene rgb+depth) and
 * §12.6 (/get2DAABB normal, clipped, behind) and §12.7 (an /updateItem that ends
 * playback (Q76) and moves the arm joint by joint from now_joint_positions_rad (Q75))
 * and §12.8b (the four browser-local edits: a lighting preset swap, a scene export, the
 * pose gizmo and the Details joint editor, none of which sends a browser.state_sync frame)
 * and §12.8c (one commanded driver joint folding the four-bar gripper through <mimic>)
 * and §12.13 (the trajectory waypoint spheres of Q101/Q102, counted off the browser's own
 * diagnostics line, and the show-all-waypoints checkbox's two states)
 * and §12.14 (the camera frustum lines and light placeholder spheres of Q105/Q106, read off
 * the same kind of line, plus the mesh-only camera box and the four combinations of the keys)
 * and §12.15 (the passive URDF model of Q107: driven by its clip like any other robot, drawn
 * as neither a waypoint string nor a ghost, and mountable in both directions)
 * and §12.16 (the viewport screenshot button of Q108: the clipboard PNG counted pixel by pixel
 * against the same frame on screen, with no editor layer in it and no request behind it).
 *
 * Calibrated 2026-10-14 against the installed ObjectLibraryServer library:
 * `scene` 1..7 (`ur5e_table_empty` ... `ur5e_table_push_pull`), `model` 1..34
 * (apple = 1, basket = 12, sugar_box = 19, table = 29) and `robot` 1..14
 * (ur5e_arm = 1, robotiq_2f85 = 2, d435i = 3, robot_pedestal = 4). Load Scene
 * reads docs/ur5e_table_empty.json, the same document OLS serves as scene 1.
 * The centimetre figures below are what that geometry measures in the browser,
 * so a failure now means a bug in the frontend, not a stale fixture.
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');

const FRONTEND = process.env.FRONTEND_URL || 'http://127.0.0.1:5188';
const SCENE_FIXTURE = path.join(__dirname, '..', 'docs', 'ur5e_table_empty.json');
const TRAJ_FIXTURE = path.join(__dirname, '..', 'tests', 'fixtures', 'robot_grasp.traj');
// A second, deliberately different clip (840 frames / 9 steps) so §12.4b can see which
// one the dropdown picked without reading any browser internals.
const TRAJ3_FIXTURE = path.join(__dirname, '..', 'examples', 'scene3_9_segments.traj');
const CLIP_BYTES = readFileSync(TRAJ_FIXTURE);
const CLIP3_BYTES = readFileSync(TRAJ3_FIXTURE);
// The two variant clips of §13.30 (**Q103**), used by §12.4c to feed the multi-select
// picker: 840 and 610 frames, so which row is playing is readable off the playhead.
const WRIST_FIRST_FIXTURE = path.join(__dirname, '..', 'examples', 'scene3_wrist_first.traj');
const ALTERNATE_FIXTURE = path.join(__dirname, '..', 'examples', 'scene3_alternate_joints.traj');
// §12.15 (**Q107**): the local rig that hangs a `type: "model"` URDF (OLS model 30, a microwave
// with one revolute `door_joint`) on the UR5e's wrist, and the clip that drives both of them.
const PASSIVE_FIXTURE = path.join(__dirname, '..', 'docs', 'ur5e_passive_rig.json');
const PASSIVE_RIG_BYTES = readFileSync(path.join(__dirname, '..', 'examples', 'passive_rig.traj'));

const LIGHT = 0, ARM = 1, APPLE = 2, BOX = 3, CAMERA = 4, PROBE = 6;

/**
 * §12.4 drives objects 1/2/3 by id (arm, apple, box) and never writes them, so the
 * world the pixel checks measure has to name those ids the way the clip does. The
 * §12.1 fixture uses a different id space and is checked as a hierarchy instead.
 * PROBE is a fifth object no clip mentions: its pose stays exactly as declared, so
 * it is the only thing §12.5/§12.6 can be measured against while a clip is loaded.
 */
function clipWorld() {
  const transform = (position, parent = null) => ({
    component_type: 'Transform',
    properties: { position, quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent },
  });
  const model = (id) => [
    { component_type: 'MetadataRef', properties: { path: `$LIBRARY_SERVER/${id}`, type: 'model' } },
    { component_type: 'MeshRenderer', properties: { forceOverrideColor: '#FFFFFF' } },
  ];
  return {
    scene_schema_version: 1, scene_version: 1, scene_id: 77, scene_name: 'Smoke Scene',
    objects: [
      { object_id_in_scene: LIGHT, object_name_in_scene: 'Light', components: [
        transform([0, 0, 2]),
        { component_type: 'Light', properties: { type: 'Ambient', intensity: 0.8, color: '#FFFFFF' } }] },
      { object_id_in_scene: ARM, object_name_in_scene: 'Arm', components: [
        transform([0, 0, 0]),
        { component_type: 'MetadataRef', properties: { path: '$LIBRARY_SERVER/1', type: 'robot' } },
        { component_type: 'RobotDriver', properties: {
          joint_sequence: ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint',
            'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint'],
          joint_limits: { shoulder_pan_joint: [-3.14, 3.14], shoulder_lift_joint: [-3.14, 3.14],
            elbow_joint: [-3.14, 3.14], wrist_1_joint: [-3.14, 3.14], wrist_2_joint: [-3.14, 3.14],
            wrist_3_joint: [-3.14, 3.14] },
          // The pose the scene file declares (the real scene 1 values), so §12.7 can see
          // which joints a written `now` overrode and which ones stayed on `initial`.
          initial_joint_positions_rad: { shoulder_pan_joint: 0, shoulder_lift_joint: -2.094395,
            elbow_joint: 1.570796, wrist_1_joint: -1.5707963, wrist_2_joint: -1.5707963, wrist_3_joint: 0 } } }] },
      { object_id_in_scene: APPLE, object_name_in_scene: 'Apple', components: [transform([0, 0, 0]), ...model(1)] },
      { object_id_in_scene: BOX, object_name_in_scene: 'Box', components: [transform([0, 0, 0]), ...model(12)] },
      { object_id_in_scene: CAMERA, object_name_in_scene: 'Sensor', components: [
        transform([0, 0, -3]),
        { component_type: 'Camera', properties: { type: 'rgbd', fov: 90, near: 0.1, far: 50, resolution: [640, 480] } }] },
      // The apple is 0.0747 x 0.0747 x 0.0712 m (OLS geometric_properties for model 1).
      // An identity quaternion looks along the file's +Z (§4.4 optical frame), so the
      // sensor sits 3 m below the objects on its axis, and the probe is 0.3/0.5 m off
      // that axis: nothing else lies on that ray, so its front face is the depth §12.5 reads.
      { object_id_in_scene: PROBE, object_name_in_scene: 'Probe', components: [transform([0.3, 0.5, 0]), ...model(1)] },
    ],
  };
}

function pngSize(bytes) {
  assert.equal(bytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', 'the image must be a PNG');
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

function depthAt(bytes, width, column, row) {
  const offset = (row * width + column) * 4;
  return bytes.readFloatLE(offset);
}

(async () => {
  // SwiftShader is the only renderer a headless run has, and WebGL2 plus
  // EXT_color_buffer_float is what /captureScene's depth pass needs (§5.10).
  const browser = await chromium.launch({ headless: true,
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const report = { checks: [] };
  function check(name, detail) { report.checks.push({ name, detail }); console.log(`ok ${name} ${JSON.stringify(detail)}`); }
  try {
    const page = await browser.newPage({ baseURL: FRONTEND, viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(30000);
    const errors = [];
    const fetched = [];
    // §12.13 (Q101): a waypoint is an editor layer, so no pixel readout can count them. The
    // browser says what it drew on one console.debug line, and this is where it is collected.
    // §12.14 (Q105/Q106): the frustum lines and the light spheres are editor layers too, and
    // whether one shows is a `visible` flag no surface outside that line can report.
    // §12.15 (Q107): a ghost is the same kind of layer, so `Ghosts` is where the mirrored
    // set is read - which robot is withdrawn from it cannot be seen anywhere else.
    const waypointLogs = [];
    const markerLogs = [];
    const ghostLogs = [];
    const requests = [];
    // §12.8b: `browser.state_sync` is the one thing the browser sends on its own, so
    // counting it is the direct proof that a local edit was never reported (§5.8).
    const reports = [];
    page.on('pageerror', error => errors.push(String(error)));
    page.on('request', request => requests.push(request.url()));
    page.on('console', message => {
      const text = message.text();
      if (message.type() === 'error') errors.push(text);
      if (text.startsWith('Trajectory waypoints ')) waypointLogs.push(JSON.parse(text.slice('Trajectory waypoints '.length)));
      if (text.startsWith('Viewport markers ')) markerLogs.push(JSON.parse(text.slice('Viewport markers '.length)));
      if (text.startsWith('Ghosts ')) ghostLogs.push(JSON.parse(text.slice('Ghosts '.length)));
    });
    page.on('websocket', socket => socket.on('framesent', event => {
      const payload = String(event.payload || '');
      if (payload.includes('browser.state_sync')) reports.push(payload);
    }));
    page.on('response', response => {
      const url = response.url();
      if (url.includes('/file?') || url.includes('/metadata')) fetched.push({ url, status: response.status() });
    });

    const health = await page.request.get('/api/health');
    assert.equal(health.headers()['x-standalone-verification'], '1',
      'run tools/serve_verification.py first; refusing to edit a real scene');
    const runtime = await (await page.request.get('/api/runtimeConfig')).json();
    const ols = runtime.library_server_url;
    // OLS is the CORS authority (§6.9, §13.2 O2): the browser talks to it
    // directly, so a library without Access-Control-Allow-Origin is a real
    // deployment failure and this run says so up front.
    assert.equal((await fetch(`${ols}/list?type=model`)).headers.get('access-control-allow-origin'), '*',
      'OLS must send Access-Control-Allow-Origin or the asset panel cannot read it');

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    // Q92: the header's dot/label pair is gone, so the bottom status bar is the connection readout.
    await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
      null, { timeout: 15000 });

    async function world() {
      const response = await page.request.get('/getWorld');
      assert.equal(response.status(), 200, await response.text());
      return { document: await response.json(), source: response.headers()['x-world-source'] };
    }
    async function write(route, body, query = '') {
      const response = await page.request.post(`${route}${query}`, { data: body });
      const text = await response.text();
      assert.equal(response.status(), 200, `${route} -> ${response.status()} ${text.slice(0, 300)}`);
      return JSON.parse(text);
    }
    async function readJson(route) {
      const response = await page.request.get(route);
      const text = await response.text();
      return { status: response.status(), body: text.startsWith('{') ? JSON.parse(text) : text };
    }
    const ids = doc => doc.objects.map(object => object.object_id_in_scene).sort((a, b) => a - b);
    async function status() { return (await page.locator('#status-primary')).textContent(); }
    /** Geometry only exists once the OLS fetch behind it resolved (§6.4). */
    async function waitGeometry(objectId) {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const { status, body } = await readJson(`/get2DAABB?id=${objectId}&camera_id=${CAMERA}`);
        if (status === 200 && body.visible) return body;
        await page.waitForTimeout(200);
      }
      throw new Error(`object ${objectId} never reported renderable geometry`);
    }
    /** The same wait without the visibility half: 400 EMPTY_AABB means the mesh has not landed yet,
     *  while `visible:false` is already the answer §5.11 gives for an object off this camera's frame. */
    async function waitMesh(objectId) {
      let last = null;
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const { status, body } = await readJson(`/get2DAABB?id=${objectId}&camera_id=${CAMERA}`);
        if (status === 200) return body;
        last = { status, body };
        await page.waitForTimeout(200);
      }
      throw new Error(`object ${objectId} never answered /get2DAABB with a box, last ${JSON.stringify(last).slice(0, 300)}`);
    }
    async function seek(frame, last = 719) {
      await page.locator('#timeline-range').evaluate((element, index) => {
        element.value = String(index);
        element.dispatchEvent(new Event('input', { bubbles: true }));
      }, frame);
      await page.waitForFunction(({ index, total }) => document.querySelector('#timeline-frame')?.textContent === `${index} / ${total}`,
        { index: frame, total: last });
    }
    async function capture() {
      const { status, body } = await readJson(`/captureScene?id=${CAMERA}&type=rgbd`);
      assert.equal(status, 200, JSON.stringify(body).slice(0, 300));
      const image = Buffer.from(body.image_base64, 'base64');
      const depth = Buffer.from(body.depth_base64, 'base64');
      assert.deepEqual(pngSize(image), [body.width, body.height], 'PNG size must match the reported one');
      assert.equal(depth.length, body.width * body.height * 4, 'depth must be float32 per pixel');
      return { body, image, depth };
    }
    /**
     * §5.3 (**Q99**): one multipart part per clip, every part named `trajs`. The framing
     * is the caller's job, so it is built here by hand rather than through a form helper
     * that might rename or re-encode the bytes.
     */
    function trajForm(clips) {
      const boundary = 'smokeclipboundary';
      const head = Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="trajs"\r\n`
        + 'Content-Type: application/octet-stream\r\n\r\n');
      return {
        data: Buffer.concat([...clips.flatMap(clip => [head, clip, Buffer.from('\r\n')]),
                             Buffer.from(`--${boundary}--\r\n`)]),
        headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}` },
      };
    }
    /** POST /updateTraj with `clips` and read the reply, error envelope included. */
    async function pushTrajs(clips, query = '') {
      const response = await page.request.post(`/updateTraj${query}`, trajForm(clips));
      const text = await response.text();
      return { status: response.status(), body: text.startsWith('{') ? JSON.parse(text) : text };
    }
    const selectorVisible = () => page.locator('#traj-select').evaluate(element => !element.hidden);
    const selectorOptions = () => page.locator('#traj-select option').evaluateAll(
      nodes => nodes.map(node => node.textContent));
    /**
     * A local pick is read from disk and decoded before it can be laid in the dropdown, so the
     * rows it adds are not on screen the moment `setInputFiles` returns. Wait for the count the
     * pick promises, then let the batch settle - a row that lands late must not read as a
     * refused batch.
     */
    async function waitRows(total) {
      await page.waitForFunction(wanted => document.querySelectorAll('#traj-select option').length >= wanted,
        total, { timeout: 20000 });
      await page.waitForTimeout(200);
    }

    // §12.1 - the manual path: Load Scene reads SCENE_FIXTURE (docs/ur5e_table_empty.json).
    await page.setInputFiles('#scene-file', SCENE_FIXTURE);
    await page.waitForFunction(() => /#1 ur5e_table_empty · v3 · 8 objects/.test(document.querySelector('#status-primary')?.textContent || ''));
    await page.locator('[data-panel="scene"]').click();
    const rows = await page.locator('.tree-row').evaluateAll(nodes => nodes.map(node => ({
      id: Number(node.dataset.entity), depth: Number(node.style.getPropertyValue('--depth') || 0),
    })));
    assert.deepEqual(rows.map(row => row.id), [0, 7, 1, 2, 3, 4, 5, 6], 'the tree lists the fixture objects');
    assert.deepEqual(rows.map(row => row.depth), [0, 0, 0, 0, 1, 2, 0, 0],
      'and nests the gripper under the arm and the wrist camera under the gripper (§3.2)');
    const loaded = await world();
    assert.equal(loaded.source, 'live', 'a connected browser is the authority (§1.4)');
    assert.deepEqual(ids(loaded.document), [0, 1, 2, 3, 4, 5, 6, 7]);
    assert.equal(loaded.document.scene_name, 'ur5e_table_empty');
    // §4.8: the gripper is a child of the arm, the wrist camera a child of the gripper.
    const parentOf = id => {
      const object = loaded.document.objects.find(item => item.object_id_in_scene === id);
      return (object.components.find(item => item.component_type === 'Transform')?.properties || {}).parent;
    };
    assert.deepEqual([parentOf(3), parentOf(4)], [2, 3], 'the mount keeps the file parent (§4.8)');
    check('§12.1 Load Scene', { status: (await status()).trim(), world: ids(loaded.document) });

    // §4.6 - the asset panel reads three OLS id spaces and a preset scene replaces
    // the world through the same pipeline as Load Scene.
    await page.locator('.rail [data-asset-kind-nav="scene"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.asset-card').length > 0);
    // The panel must show the whole library, so the expected total is counted from
    // OLS rather than pinned: the library grows when the asset side adds scenes.
    // One request at a time - the stdlib server refuses a connection it has to queue
    // while the page is still pulling model files.
    const listed = {};
    async function libraryItems(kind) {
      if (listed[kind]) return listed[kind];
      for (let attempt = 0; attempt < 5; attempt += 1) {
        try {
          listed[kind] = (await (await fetch(`${ols}/list?type=${kind}`)).json()).items;
          return listed[kind];
        } catch {
          await page.waitForTimeout(1000);
        }
      }
      throw new Error(`OLS did not answer /list?type=${kind}`);
    }
    // §4.6 (Q156): the 大类 is a rail button now, and the panel keeps one category rail.
    // Reaching a row means picking its 大类 on the left rail and its category in the panel.
    // The rail button is a toggle keyed on (panel, 大类) - main.ts:panelTabPress - so it is
    // only pressed when that 大类 is not already the lit one, never blindly.
    async function showAssetKind(kind) {
      const tab = page.locator(`.rail [data-asset-kind-nav="${kind}"]`);
      if (!(await tab.evaluate(node => node.classList.contains('active')))) await tab.click();
      await page.locator('.asset-categories').waitFor({ state: 'visible' });
    }
    async function openAssetRow(kind, id) {
      const category = ((await libraryItems(kind)).find(item => item.id === id) || {}).category || '未分类';
      await showAssetKind(kind);
      await page.locator(`.asset-category[data-asset-category="${category}"]`).click();
      const row = page.locator(`[data-asset-kind="${kind}"][data-asset-id="${id}"]`);
      await row.waitFor();
      await row.click();
    }
    const libraryTotal = await ['scene', 'model', 'robot'].reduce(
      async (sum, kind) => (await sum) + (await libraryItems(kind)).length,
      Promise.resolve(0),
    );
    assert.equal((await page.locator('#panel-count').textContent()).trim(), String(libraryTotal),
      `${libraryTotal} = every scene + model + robot OLS lists`);
    // §4.6/§4.7 (Q92/Q93/Q156): the three 大类 are rail buttons, the panel keeps one category
    // rail, and no search box, refresh button or command bar is left in the DOM.
    assert.deepEqual(await page.locator('.rail [data-asset-kind-nav] span').allTextContents(),
      ['场景', '模型', '机器人'], 'the rail says OLS\'s own first level (§4.7 Q156/Q159)');
    const shape = await page.evaluate(() => ({
      kinds: document.querySelectorAll('.rail [data-asset-kind-nav]').length,
      categories: document.querySelectorAll('.asset-categories > button').length,
      rows: document.querySelectorAll('.asset-rows > .asset-card').length,
      gridCells: document.querySelectorAll('.asset-cards').length,
      // §4.6 (Q158): the list is a two-column grid, so the column count is a computed style, not a class.
      columns: getComputedStyle(document.querySelector('.asset-rows')).gridTemplateColumns.split(' ').length,
      cardStacked: getComputedStyle(document.querySelector('.asset-card')).flexDirection,
      wider: document.querySelector('.workspace').classList.contains('assets-open'),
      innerKindRail: document.querySelectorAll('.asset-kinds, .asset-kind').length,
      withdrawn: document.querySelectorAll('#asset-search, [data-action="refresh-assets"]').length,
      topBar: document.querySelectorAll('.command-bar, #connection-label, #connection-dot').length,
    }));
    assert.ok(shape.categories >= 1 && shape.rows >= 1, `the category rail and the list render: ${JSON.stringify(shape)}`);
    assert.equal(shape.innerKindRail, 0, 'Q156: the 大类 rail moved out of the panel');
    assert.equal(shape.gridCells, 0, 'the withdrawn .asset-cards host stays out of the DOM');
    assert.equal(shape.columns, 2, 'Q158: the list is two columns');
    assert.equal(shape.cardStacked, 'column', 'Q158: thumbnail over name');
    assert.equal(shape.wider, true, 'Q158: the open asset panel borrows the Agent width');
    assert.equal(shape.withdrawn, 0, 'Q93: the filter box and the refresh button are gone');
    assert.equal(shape.topBar, 0, 'Q92: the command bar is gone');
    await openAssetRow('scene', 2);
    await page.waitForFunction(() => /#2 ur5e_table_apple/.test(document.querySelector('#status-primary')?.textContent || ''));
    const appleScene = await world();
    assert.deepEqual(ids(appleScene.document), [0, 1, 2, 3, 4, 5, 6, 7, 8]);
    await openAssetRow('scene', 1);
    await page.waitForFunction(() => /ur5e_table_empty/.test(document.querySelector('#status-primary')?.textContent || ''));
    assert.deepEqual(ids((await world()).document), [0, 1, 2, 3, 4, 5, 6, 7]);
    check('§4.6 preset scenes from OLS', { rails: shape, appleScene: ids(appleScene.document), emptyScene: ids((await world()).document) });

    // Real geometry, not placeholders: every model the fixture names has to arrive
    // through /file?type=... with its dependencies (§6.1/§6.4).
    const ok = pattern => fetched.filter(item => pattern.test(item.url) && item.status === 200).length;
    assert.ok(ok(/file\?type=model&id=1&path=apple\.obj/) >= 1, 'apple.obj must be fetched');
    assert.ok(ok(/file\?type=model&id=1&path=apple\.mtl/) >= 1, 'and its material');
    assert.ok(ok(/file\?type=robot&id=1&path=ur5e_arm\.urdf/) >= 1, 'ur5e_arm.urdf must be fetched');
    assert.ok(ok(/file\?type=model&id=29&path=table\.urdf/) >= 1, 'the work table must be fetched');
    check('§12.1 real OLS geometry', { requests: fetched.filter(item => item.url.includes('/file?')).length });

    // §12.4 - the caller path: /updateWorld then /updateTraj with the real clip.
    const pushed = await write('/updateWorld', clipWorld());
    assert.equal(pushed.ok, true, JSON.stringify(pushed));
    assert.equal(pushed.object_count, 6);
    assert.deepEqual(pushed.rejected_objects, []);
    const armed = await world();
    assert.deepEqual(ids(armed.document), [0, 1, 2, 3, 4, 6]);
    assert.equal(armed.document.scene_id, 77);
    await waitGeometry(ARM);
    await waitGeometry(PROBE);
    const beforeClip = (await world()).document.scene_version;
    const posted = await pushTrajs([CLIP_BYTES]);
    assert.equal(posted.status, 200, JSON.stringify(posted.body));
    const clip = posted.body;
    assert.equal(clip.trajs[0].total_frames, 720, 'the browser read the frame count out of the header');
    assert.deepEqual(clip, { ok: true, count: 1, trajs: [{ index: 0, fps: 60, total_frames: 720,
      duration_seconds: 12, object_count: 3, attach_count: 2, ignored_objects: [], warnings: [] }] },
      '§5.12 (Q99): one summary per part, no flat fields left');
    assert.equal((await world()).document.scene_version, beforeClip, 'a clip is not a world change (Q33)');
    await page.waitForFunction(() => !document.querySelector('#timeline')?.classList.contains('hidden'));
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 719');
    assert.equal(await page.locator('#timeline-range').getAttribute('max'), '719');
    // §7.3 (Q90): the two kinds of mark are one colour and say nothing on hover -
    // their position is the message, and clicking one seeks there.
    const marks = await page.locator('.timeline-tick').evaluateAll(nodes => nodes.map(node => ({
      kind: node.classList.contains('attach') ? 'attach' : 'step',
      frame: node.dataset.seekFrame, hover: node.title,
      colour: getComputedStyle(node).backgroundColor,
    })));
    assert.deepEqual(marks.filter(mark => mark.kind === 'attach').map(mark => mark.frame), ['340', '530'],
      '§8.6 attach table (§7.3)');
    assert.ok(marks.every(mark => !mark.hover), 'Q90: a mark carries no hover text');
    assert.equal(new Set(marks.map(mark => mark.colour)).size, 1, 'Q90: step and attach marks share one colour');
    // Q91: `0 / 719` and `340 / 719` are different widths of text, and the track must not notice.
    const trackWidth = () => page.locator('.timeline-track').evaluate(element => element.offsetWidth);
    const widthBeforeSeek = await trackWidth();
    await page.locator('.timeline-tick.attach').first().click();
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '340 / 719',
      'Q90: clicking a mark pulls the playhead to that frame');
    assert.equal(await trackWidth(), widthBeforeSeek, 'Q91: digit count must not resize the progress bar');
    check('§12.4 updateTraj', { clip, ticks: marks.length, widthBeforeSeek, widthAfterSeek: await trackWidth() });

    // The apple rides the arm from 340 and the box from 530 (§12.4). Measured
    // through /get2DAABB, which follows the driven pose (§5.11).
    const center = box => box.aabb_world.min.map((minimum, axis) => (minimum + box.aabb_world.max[axis]) / 2);
    const round3 = values => values.map(value => Number(value.toFixed(3)));
    await seek(0);
    // `waitMesh`, not a bare read and not `waitGeometry`: the apple's mesh arrives from OLS after
    // the world is applied, and until it does `/get2DAABB` answers 400 EMPTY_AABB (§6.4) - a single
    // read here is a race (坑 89, measured 2026-10-23). Visibility is deliberately not part of the
    // wait: at frame 0 the wrist camera has the apple behind it, which §5.11 answers with
    // `visible:false` and a full `aabb_world`, and that is exactly the resting centre wanted here.
    const resting = center(await waitMesh(APPLE));
    await seek(340);
    const riding = await readJson(`/get2DAABB?id=${APPLE}&camera_id=${CAMERA}`);
    assert.equal(riding.status, 200, JSON.stringify(riding.body));
    assert.equal(riding.body.frame_index, 340);
    // Measured 2026-10-12 against robot 1 (ur5e_arm): the clip's 340th frame puts the
    // flange back and low, so the apple has to leave its frame-0 spot by over a metre.
    assert.deepEqual(round3(center(riding.body)), [-0.701, -0.038, 0.2], `apple at 340, got ${JSON.stringify(center(riding.body))}`);
    assert.ok(Math.hypot(...center(riding.body).map((value, axis) => value - resting[axis])) > 1,
      `the arm must carry the apple, ${JSON.stringify(resting)} -> ${JSON.stringify(center(riding.body))}`);
    const first = await capture();
    assert.equal(first.body.frame_index, 340, '§5.10 reports the playback frame');
    await seek(530);
    const second = await capture();
    assert.equal(second.body.frame_index, 530);
    assert.notEqual(second.image.toString('base64'), first.image.toString('base64'),
      'the arm and the re-attached apple must change the sensor pixels');
    assert.notEqual(second.depth.toString('base64'), first.depth.toString('base64'),
      'and the depth buffer with them');
    const placed = await readJson(`/get2DAABB?id=${APPLE}&camera_id=${CAMERA}`);
    const host = center((await readJson(`/get2DAABB?id=${BOX}&camera_id=${CAMERA}`)).body);
    assert.deepEqual(round3(center(placed.body)), [0.6, 0.3, 0.088],
      `at 530 the apple hangs off the box, got ${JSON.stringify(center(placed.body))}`);
    assert.ok(Math.hypot(...center(placed.body).map((value, axis) => value - host[axis])) < 0.3,
      `the apple must land on the basket at ${JSON.stringify(host)}, got ${JSON.stringify(center(placed.body))}`);
    check('§12.4 playback frames', { at340: center(riding.body), at530: center(placed.body),
      pixelsDiffer: first.image.toString('base64') !== second.image.toString('base64') });

    // §12.4b - several clips in one request (**Q99**) and the toolbar dropdown that
    // chooses which one plays (**Q100**). The two clips are deliberately different lengths
    // so "which one is playing" is readable straight off the timeline. Default is the
    // first entry; picking another goes down the same path as a new batch: pause, back to
    // frame 0, no request of its own and no scene_version move.
    const revisionBeforeBatch = (await world()).document.scene_version;
    const batch = await pushTrajs([CLIP_BYTES, CLIP3_BYTES], '?count=2');
    assert.equal(batch.status, 200, JSON.stringify(batch.body));
    assert.equal(batch.body.count, 2);
    assert.deepEqual(batch.body.trajs.map(entry => entry.index), [0, 1], '§5.12: one summary per part, in order');
    assert.deepEqual(batch.body.trajs.map(entry => entry.total_frames), [720, 840],
      '§5.12: each summary describes its own clip');
    assert.deepEqual(batch.body.trajs.map(entry => entry.duration_seconds), [12, 14]);
    assert.deepEqual(batch.body.trajs[0].ignored_objects, [], 'the first clip covers this world entirely');
    assert.deepEqual(batch.body.trajs[1].ignored_objects, [{ object_id: 7, reason: 'OBJECT_NOT_IN_SCENE' }],
      '§5.3: the second clip ignores its own ids, and only its own - the two judgements do not mix');
    assert.equal(await selectorVisible(), true, 'Q100: two clips make the dropdown appear');
    assert.deepEqual(await selectorOptions(), ['轨迹 1 · 720 帧 · 60 fps', '轨迹 2 · 840 帧 · 60 fps'],
      'Q100: one row per clip, in the caller part order');
    assert.equal(await page.locator('#traj-select').inputValue(), '0', 'Q100: the first clip is selected by default');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 719',
      'Q100: a new batch plays the first entry, from frame 0');
    const tickCount = () => page.locator('.timeline-tick').count();
    assert.equal(await tickCount(), 5, 'the marks come from the active clip (clip 0, §7.3)');
    await seek(200);
    await page.locator('#traj-select').selectOption('1');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 839',
      'Q100: switching clips pauses and returns to frame 0, exactly like a new batch');
    assert.equal(await tickCount(), 11, 'and the timeline rebuilds from the clip now active');
    await page.locator('#traj-select').selectOption('0');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 719',
      'Q100: switching back does not try to keep the frame number');
    assert.equal(await tickCount(), 5);

    // A batch that does not add up fails before the socket (`?count=` disagrees with the
    // part count, gateway-side), and a batch whose second clip is garbage fails whole:
    // the held list stays exactly as it was, with the failing index named (§5.3, Q99).
    const miscounted = await pushTrajs([CLIP_BYTES, CLIP3_BYTES], '?count=3');
    assert.equal(miscounted.status, 400, JSON.stringify(miscounted.body));
    assert.equal(miscounted.body.error.code, 'INVALID_PARAM');
    assert.match(miscounted.body.error.message, /count=3 but the body holds 2 trajs parts/);
    const halfGarbage = await pushTrajs([CLIP_BYTES, Buffer.alloc(32)], '?count=2');
    assert.equal(halfGarbage.status, 400, JSON.stringify(halfGarbage.body));
    assert.equal(halfGarbage.body.error.code, 'TRAJ_INVALID_MAGIC');
    assert.match(halfGarbage.body.error.message, /trajs\[1\]/, 'Q99: all-or-nothing names the clip that failed');
    assert.equal(await selectorVisible(), true, 'the refused batch leaves the held list untouched (Q49)');
    assert.deepEqual(await selectorOptions(), ['轨迹 1 · 720 帧 · 60 fps', '轨迹 2 · 840 帧 · 60 fps']);
    assert.equal(await page.locator('#traj-select').inputValue(), '0', 'and the selection with it');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 719',
      'so does the playhead - both refusals dispatched nothing');

    // Local Load Traj appends to the same list and selects what it just read; the label
    // is the file name, which is browser-only knowledge and never reaches the wire.
    await pushTrajs([CLIP_BYTES]);
    assert.equal(await selectorVisible(), false, 'Q100: one clip needs no dropdown, hidden rather than empty');
    await page.setInputFiles('#traj-file', TRAJ_FIXTURE);
    await waitRows(2);
    assert.equal(await selectorVisible(), true, 'Q100: the appended second clip brings the dropdown back');
    assert.deepEqual(await selectorOptions(), ['轨迹 1 · 720 帧 · 60 fps', 'robot_grasp.traj · 720 帧 · 60 fps'],
      '§7.1: the local file is labelled with its name');
    assert.equal(await page.locator('#traj-select').inputValue(), '1', 'Q100: a local load selects itself');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 719');
    await pushTrajs([CLIP_BYTES]);
    assert.equal((await world()).document.scene_version, revisionBeforeBatch,
      'Q33: none of this - batch, switch, refused batch, local append - is a world change');
    check('§12.4b multi-clip batch and the trajectory dropdown', {
      batch: batch.body, miscounted: miscounted.body.error.code, halfGarbage: halfGarbage.body.error.message });

    // §12.4c - the file picker takes several clips in one selection (**Q104**). Two files
    // are one batch: both append in picker order, the batch selects its own first file,
    // and a batch with a broken member applies nothing. One file stays the N=1 case.
    const revisionBeforeMulti = (await world()).document.scene_version;
    await page.setInputFiles('#traj-file', [WRIST_FIRST_FIXTURE, ALTERNATE_FIXTURE]);
    await waitRows(3);
    assert.equal(await selectorVisible(), true, 'Q104: two appended clips are two more rows');
    assert.deepEqual(await selectorOptions(), [
      '轨迹 1 · 720 帧 · 60 fps',
      'scene3_wrist_first.traj · 840 帧 · 60 fps',
      'scene3_alternate_joints.traj · 610 帧 · 60 fps',
    ], 'Q104: picker order, each row its own file name and frame count');
    assert.equal(await page.locator('#traj-select').inputValue(), '1',
      'Q104: the appended batch selects its own first file, the rule a caller push follows too');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 839');
    assert.equal(await tickCount(), 11, 'Q100: the timeline rebuilt for the clip that is now active');
    await page.locator('#traj-select').selectOption('2');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 609',
      'Q104: the 610-frame file really is the second row - frame 0 on its own last frame');

    const heldRows = await selectorOptions();
    await page.setInputFiles('#traj-file', [
      { name: 'broken.traj', mimeType: 'application/octet-stream', buffer: Buffer.alloc(64) },
      { name: 'scene3_wrist_first.traj', mimeType: 'application/octet-stream', buffer: readFileSync(WRIST_FIRST_FIXTURE) },
    ]);
    await page.waitForFunction(() => /broken\.traj/.test(document.querySelector('#toast')?.textContent || ''),
      null, { timeout: 10000 });
    assert.match((await page.locator('#toast').textContent()) || '', /broken\.traj/,
      'Q104: the refusal names the file the user picked, not the part index the decoder saw');
    assert.deepEqual(await selectorOptions(), heldRows,
      'Q49/Q104: all-or-nothing - the good file in a bad batch did not sneak in');
    assert.equal(await page.locator('#traj-select').inputValue(), '2', 'and the selection stayed');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 609', 'and the playhead did');

    await page.setInputFiles('#traj-file', ALTERNATE_FIXTURE);
    await waitRows(4);
    const finalRows = await selectorOptions();
    assert.equal(finalRows.length, 4, 'Q104: a single pick still appends one row');
    assert.equal(await page.locator('#traj-select').inputValue(), '3', 'and still selects itself');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 609');
    assert.equal((await world()).document.scene_version, revisionBeforeMulti,
      'Q33: a local multi-select is still not a world change');
    check('§12.4c one selection, several clips', {
      rows: finalRows,
      refused: (await page.locator('#toast').textContent()) || '',
      sceneVersion: (await world()).document.scene_version });

    // Hand §12.5 onward the state it was written against: a caller batch replaces the held
    // list, so the local 610-frame clip the picker left active is back to clip 0 / 719.
    await pushTrajs([CLIP_BYTES]);

    // §12.5 - depth in metres along the optical axis, cross-checked against
    // /get2DAABB's world box within 5%.
    await seek(0);
    const still = await capture();
    assert.equal(still.body.camera_type, 'rgbd');
    assert.equal(still.body.width, 640);
    assert.equal(still.body.height, 480);
    assert.equal(still.body.depth_encoding, 'float32le_meters');
    assert.equal(still.body.depth_unit, 'meter');
    const probe = await waitGeometry(PROBE);
    assert.equal(probe.width, 640);
    assert.equal(probe.coordinate_origin, 'top_left');
    assert.ok(probe.visible && !probe.clipped, JSON.stringify(probe));
    assert.ok(probe.box.x_min < probe.box.x_max && probe.box.y_min < probe.box.y_max, JSON.stringify(probe.box));
    const column = Math.round((probe.box.x_min + probe.box.x_max) / 2);
    const row = Math.round((probe.box.y_min + probe.box.y_max) / 2);
    const measured = depthAt(still.depth, still.body.width, column, row);
    const middle = center(probe);
    const fromSensor = Math.hypot(middle[0], middle[1], middle[2] + 3);
    assert.ok(measured > 0 && measured < 50, `probe depth must be a real distance, got ${measured}`);
    const error = Math.abs(measured - fromSensor) / fromSensor;
    assert.ok(error < 0.05, `depth ${measured} vs aabb centre ${fromSensor} is ${(error * 100).toFixed(2)}%`);
    let misses = 0;
    for (let index = 0; index < still.depth.length / 4; index += 1) if (Number.isNaN(still.depth.readFloatLE(index * 4))) misses += 1;
    assert.ok(misses > still.body.width * 10, 'background pixels must be NaN');
    check('§12.5 captureScene depth', { column, row, measured, centreDistance: fromSensor,
      errorPercent: Number((error * 100).toFixed(2)), nanPixels: misses });

    // §12.6 - clipped at the image edge, then behind the camera.
    const wide = clipWorld().objects.find(object => object.object_id_in_scene === PROBE);
    async function moveProbe(position, scale) {
      const object = { ...wide, components: [
        { component_type: 'Transform', properties: { position, quaternion_xyzw: [0, 0, 0, 1], scale, parent: null } },
        ...wide.components.slice(1) ] };
      return (await write(`/updateItem?id=${PROBE}`, object)).scene_version;
    }
    // 3.95 m along the file's +X: the horizontal half-extent at 3 m is 4 m
    // (90° vertical fov, 4:3), so the 10x apple straddles a border. The camera
    // has an identity quaternion, and in the §4.4 optical frame (+Z forward,
    // +Y up, +X left, Q74) the file's +X is the image's left half.
    await moveProbe([3.95, 0.5, 0], [10, 10, 10]);
    const clipped = await readJson(`/get2DAABB?id=${PROBE}&camera_id=${CAMERA}`);
    assert.equal(clipped.status, 200, JSON.stringify(clipped.body));
    assert.equal(clipped.body.visible, true, JSON.stringify(clipped.body));
    assert.equal(clipped.body.clipped, true, 'the rectangle must be cut by the image edge');
    const box = clipped.body.box;
    assert.equal(box.x_min, 0, `the file's +X must clip the left border, got ${JSON.stringify(box)}`);
    assert.ok(box.x_max < clipped.body.width / 2 && box.y_min > 0 && box.y_max < clipped.body.height,
      `only the left border may be cut, got ${JSON.stringify(box)}`);
    await moveProbe([0, 0, -6], [1, 1, 1]);
    const behind = await readJson(`/get2DAABB?id=${PROBE}&camera_id=${CAMERA}`);
    assert.equal(behind.status, 200, 'behind the camera is still 200 (§5.11)');
    assert.equal(behind.body.visible, false);
    assert.equal(behind.body.box, null);
    assert.ok(behind.body.aabb_world.min[2] < -5, JSON.stringify(behind.body.aabb_world));
    check('§12.6 get2DAABB states', { clipped: clipped.body.box, behind: behind.body.visible });

    // §12.7 - a caller write ends playback (Q76) and refreshes the arm joint by joint
    // from now_joint_positions_rad (Q75). §12.6's own /updateItem calls already ended
    // the playback §12.4 started, so the clip is posted again here.
    const armSpec = clipWorld().objects.find(object => object.object_id_in_scene === ARM);
    const withNow = (pose) => ({ ...armSpec, components: armSpec.components.map(component =>
      component.component_type === 'RobotDriver'
        ? { ...component, properties: { ...component.properties, now_joint_positions_rad: pose } }
        : component) });
    /** The full arm, not the gripper-only subtree a half-loaded robot measures (§5 坑 33). */
    async function settledArm() {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const { status, body } = await readJson(`/get2DAABB?id=${ARM}&camera_id=${CAMERA}`);
        if (status === 200 && body.aabb_world && body.aabb_world.max[2] - body.aabb_world.min[2] > 0.4) return center(body);
        await page.waitForTimeout(200);
      }
      throw new Error('the arm never settled');
    }
    async function postClip() {
      const { status, body } = await pushTrajs([CLIP_BYTES]);
      assert.equal(status, 200, JSON.stringify(body));
    }
    await postClip();
    const clipPose = await settledArm();
    const frameWhilePlaying = (await readJson(`/get2DAABB?id=${ARM}&camera_id=${CAMERA}`)).body.frame_index;
    assert.equal(frameWhilePlaying, 0, 'the clip owns the stage while it plays (§5.10)');

    await write(`/updateItem?id=${ARM}`, withNow({ shoulder_pan_joint: 1.2, shoulder_lift_joint: -1.2 }));
    const parked = await settledArm();
    assert.ok(Math.hypot(...parked.map((value, axis) => value - clipPose[axis])) > 0.05,
      `writing now_joint_positions_rad must move the arm, ${JSON.stringify(clipPose)} -> ${JSON.stringify(parked)}`);
    assert.ok(await page.locator('#timeline').evaluate(node => node.classList.contains('hidden')),
      'a caller write leaves playback mode and shows the plain scene (§7.3, Q76)');
    assert.equal((await readJson(`/get2DAABB?id=${ARM}&camera_id=${CAMERA}`)).body.frame_index, null,
      'and the box read stops reporting a playback frame');

    // The merge is per joint: the two named ones carry the written value, the other four
    // stay on the file's `initial` pose. Q81 turned that readout into the panel's own
    // inputs, so the written value is read back from the field that offers to change it.
    await page.locator('[data-panel="scene"]').click();
    await page.locator('.tree-row[data-entity="1"]').click();
    await page.locator('[data-panel="details"]').click();
    assert.equal(await page.locator('[data-joint="shoulder_pan_joint"]').inputValue(), '1.2',
      'the joint the caller wrote shows its value and is editable (Q81)');
    assert.equal(await page.locator('[data-joint="elbow_joint"]').inputValue(), '1.5708',
      'the joint it did not name is still on the file pose (Q75)');

    // A trajectory call after the write takes the objects and the joints back (Q76).
    await postClip();
    assert.deepEqual(round3(await settledArm()), round3(clipPose), 'a fresh clip re-drives the arm from frame 0');
    check('§12.7 updateItem ends playback and refreshes joints', {
      clipPose: round3(clipPose), parked: round3(parked), frameWhilePlaying });

    // §12.8b - the three browser-local edits (Q77-Q79). The fixture is loaded again first:
    // Load Scene clears any clip (Q76), and its own `scene_version` is the revision the
    // local edits must not move.
    await page.setInputFiles('#scene-file', SCENE_FIXTURE);
    await page.waitForFunction(() => /#1 ur5e_table_empty · v3 · 8 objects/.test(document.querySelector('#status-primary')?.textContent || ''));
    await page.waitForTimeout(500);   // let Load Scene's own report settle before counting
    await page.locator('[data-panel="scene"]').click();
    const revisionBefore = (await page.locator('#status-revision').textContent()).trim();
    const reportsBefore = reports.length;
    assert.equal(revisionBefore, 'v3', 'the loaded fixture fixes the revision the local edits are measured against');

    /** Two identical frames mean the viewport is quiescent, so the next pair can be compared. */
    async function stableShot() {
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const first = await page.locator('#viewport').screenshot();
        await page.waitForTimeout(500);
        const second = await page.locator('#viewport').screenshot();
        if (Buffer.compare(first, second) === 0) return second;
      }
      throw new Error('the viewport never stopped changing, so the handle cannot be measured against it');
    }
    const toastText = async () => (await page.locator('#toast').textContent()) || '';
    /** `showError` hides the notification after five seconds, so a silence check starts from an empty one. */
    async function toastCleared() {
      // The element stays in the document and only loses the `hidden` class, so wait on it being attached.
      await page.waitForSelector('#toast.hidden', { state: 'attached', timeout: 8000 });
    }

    // Q77: one choice replaces the scene's whole lighting rig, at the same revision.
    await page.selectOption('#lighting-preset', 'studio');
    assert.match(await toastText(), /光照已切换：Studio/);
    const litWorld = await world();
    const litNames = litWorld.document.objects.map(object => object.object_name_in_scene);
    assert.deepEqual(litNames.filter(name => name.startsWith('Preset')), ['PresetAmbient', 'PresetKey', 'PresetFill'],
      'the preset is the only lighting left on stage');
    assert.ok(!litNames.includes('AmbientLight') && !litNames.includes('TopDirectionalLight'),
      'the file rig goes away whole, not lamp by lamp (§4.8.1)');
    assert.equal(litWorld.document.scene_version, 3, 'a local edit does not move the revision (§4.8)');
    assert.equal((await page.locator('#panel-count').textContent()), '9', 'and the panel counts what the tree shows');
    // §5.5: the fixture ran 0..7, so the new lights start at 8 and never take a deleted id back.
    assert.deepEqual(ids(litWorld.document).filter(id => id > 7), [8, 9, 10], 'released light ids are not reused');

    // Q78: the download is the document the live `/getWorld` just answered with, and the
    // click makes no request of its own.
    const downloadPromise = page.waitForEvent('download');
    await page.locator('[data-action="export-scene"]').click();
    const download = await downloadPromise;
    assert.equal(download.suggestedFilename(), 'ur5e_table_empty_v3.json', 'the name carries the scene and the revision it was read at');
    const exported = JSON.parse(readFileSync(await download.path(), 'utf-8'));
    assert.deepEqual(exported, litWorld.document, 'the export and /getWorld are the same document (§12.8b ②)');
    assert.equal((await page.locator('#status-revision').textContent()).trim(), revisionBefore);

    // Q79: the handle is drawn for a plain object and taken away when the tool closes.
    await page.locator('.tree-row[data-entity="1"]').click();
    const plainView = await stableShot();
    await page.locator('[data-tool="translate"]').click();
    assert.notDeepEqual(await stableShot(), plainView, 'the translate handle must be on screen');
    await page.locator('[data-tool="rotate"]').click();
    assert.notDeepEqual(await stableShot(), plainView, 'and so is the rotate one');
    await page.locator('[data-tool="select"]').click();
    assert.deepEqual(await stableShot(), plainView, 'the closed tool leaves nothing behind');

    // Q80 (2026-10-15): the arm and the mounted gripper get a handle too. The first
    // version refused both -- "object 2 is driven by its joints" / "object 4 is mounted
    // on link6" -- which is exactly what the user reported, so what is being asserted
    // here is that the handle appears and nothing complains.
    await page.locator('[data-tool="select"]').click();
    await toastCleared();
    await page.locator('.tree-row[data-entity="2"]').click();
    const armPlain = await stableShot();
    await page.locator('[data-tool="translate"]').click();
    assert.notDeepEqual(await stableShot(), armPlain, 'the robot base takes the handle (Q80)');
    await page.locator('[data-tool="rotate"]').click();
    assert.notDeepEqual(await stableShot(), armPlain, 'and so does the rotate handle on it');
    await page.locator('[data-tool="select"]').click();

    // The gripper is the mounted case: its own Transform is the mount pose, so the
    // handle attaches there as well. That a drag also rewrites `RobotAttachment.transform`
    // is a data-layer fact and `world.test.ts` pins it; a pixel shot cannot tell the two
    // writes apart.
    await page.locator('.tree-row[data-entity="3"]').click();
    const gripperPlain = await stableShot();
    await page.locator('[data-tool="translate"]').click();
    assert.notDeepEqual(await stableShot(), gripperPlain, 'a mounted object is editable too (Q80)');
    await page.locator('[data-tool="select"]').click();
    assert.ok(await page.locator('#toast').evaluate((node) => node.classList.contains('hidden')),
      'neither of them said anything about it');

    // Q81: an angle typed into Details re-parks that one joint, on the same revision.
    await page.locator('.tree-row[data-entity="2"]').click();
    await page.locator('[data-panel="details"]').click();
    const panInput = page.locator('[data-joint="shoulder_pan_joint"]');
    await panInput.waitFor();
    const elbowBefore = await page.locator('[data-joint="elbow_joint"]').inputValue();
    const armBefore = await stableShot();
    await panInput.fill('0.9');
    await panInput.press('Enter');
    await page.waitForTimeout(400);
    assert.notDeepEqual(await stableShot(), armBefore, 'the arm must re-park on the typed angle');
    const jointWorld = await world();
    const driverProps = jointWorld.document.objects.find((object) => object.object_id_in_scene === 2)
      .components.find((component) => component.component_type === 'RobotDriver').properties;
    assert.deepEqual(driverProps.now_joint_positions_rad, { shoulder_pan_joint: 0.9 },
      'the write lands in the dictionary /updateItem writes, joint by joint (Q75)');
    assert.equal(jointWorld.document.scene_version, 3, 'and it stays a local edit (§4.8)');
    assert.equal(await page.locator('[data-joint="shoulder_pan_joint"]').inputValue(), '0.9');
    assert.equal(await page.locator('[data-joint="elbow_joint"]').inputValue(), elbowBefore,
      'the joint that was not named did not move');

    // A clip on stage owns every object, so even the table is off-limits until it goes.
    // The gizmo has to be open for the refusal to say anything (§4.8.3: a closed tool
    // has nothing to complain about), and the scene panel is the one holding the rows.
    await page.locator('[data-panel="scene"]').click();
    await toastCleared();
    await postClip();
    await page.locator('[data-tool="translate"]').click();
    await page.locator('.tree-row[data-entity="6"]').click();
    assert.match(await toastText(), /a trajectory owns the scene while a clip is loaded/, 'the clip is the owner (§8.5)');
    await page.locator('[data-tool="select"]').click();

    // Q81 meets Q76 here: the same local edit made under a clip ends playback.
    await page.locator('.tree-row[data-entity="2"]').click();
    await page.locator('[data-panel="details"]').click();
    await page.locator('[data-joint="shoulder_pan_joint"]').fill('0.4');
    await page.locator('[data-joint="shoulder_pan_joint"]').press('Enter');
    assert.ok(await page.locator('#timeline').evaluate(node => node.classList.contains('hidden')),
      'a local joint write leaves playback mode and shows the plain scene (Q76)');

    const afterLocal = await world();
    assert.equal(reports.length, reportsBefore,
      'none of the four edits reported the world: not one browser.state_sync frame left the browser (§5.8)');
    assert.equal((await page.locator('#status-revision').textContent()).trim(), revisionBefore);
    assert.deepEqual(ids(afterLocal.document), ids(litWorld.document),
      'the live read still answers with the locally edited world (§1.4), which is why the frame count is the proof');
    check('§12.8b local edits (preset, export, gizmo, joints)', {
      revision: revisionBefore, reportsSent: reports.length - reportsBefore,
      lights: litNames.filter(name => name.startsWith('Preset')), exported: download.suggestedFilename(),
      jointWrite: driverProps.now_joint_positions_rad });

    // §12.8c - the coupled gripper. OLS robot 2 is the original four-bar model, whose
    // follower/spring links now declare `<mimic>` against `robotiq_2f85_right_driver_joint`,
    // so the scene still commands exactly one joint. OLS serves the scene, and its
    // `joint_sequence` names that same driver joint.
    await openAssetRow('scene', 1);
    await page.waitForFunction(() => /ur5e_table_empty/.test(document.querySelector('#status-primary')?.textContent || ''));
    const objFetched = fetched.filter(item => /type=robot&id=2/.test(item.url) && /\.obj$/i.test(item.url) && item.status === 200);
    const mtlFetched = fetched.filter(item => /type=robot&id=2/.test(item.url) && /\.mtl$/i.test(item.url) && item.status === 200);
    assert.ok(objFetched.length >= 8, `every wavefront visual must come from the asset, got ${JSON.stringify(objFetched.map(item => item.url))}`);
    assert.equal(mtlFetched.length, objFetched.length, 'and each .obj arrives with the material its mtllib names');
    assert.deepEqual(fetched.filter(item => /type=robot&id=2/.test(item.url) && /\.(dae|stl)$/i.test(item.url)).map(item => item.url), [],
      'the manifest lists no Collada or STL geometry, so none may be requested');

    await page.locator('[data-panel="scene"]').click();
    await page.locator('.tree-row[data-entity="3"]').click();
    await page.locator('[data-panel="details"]').click();
    const fingerInput = page.locator('[data-joint="robotiq_2f85_right_driver_joint"]');
    await fingerInput.waitFor();
    assert.equal(await page.locator('[data-joint]').count(), 1,
      'one commanded row is the whole gripper (the other five movable joints are <mimic> followers)');

    const gripperWorldBefore = await world();
    const versionBefore = gripperWorldBefore.document.scene_version;
    const openBox = await readJson(`/get2DAABB?id=3&camera_id=${CAMERA}`);
    assert.equal(openBox.status, 200, JSON.stringify(openBox.body));
    const reportsBeforeGripper = reports.length;
    const gripperShot = await stableShot();
    await fingerInput.fill('0.8');
    await fingerInput.press('Enter');
    await page.waitForTimeout(600);
    assert.notDeepEqual(await stableShot(), gripperShot, 'the fingers must fold on screen');
    const closedBox = await readJson(`/get2DAABB?id=3&camera_id=${CAMERA}`);
    const span = aabb => aabb.max.map((top, axis) => top - aabb.min[axis]);
    const openSpan = span(openBox.body.aabb_world);
    const closedSpan = span(closedBox.body.aabb_world);
    const collapsed = Math.max(...openSpan.map((top, axis) => top - closedSpan[axis]));
    // Before the asset declared `<mimic>` the follower and spring links stayed at zero,
    // so the fingers only spun their own knuckle and the outer shape barely changed.
    // A real fold shows up as exactly one world axis losing the pad gap; the other two
    // grow, because the tilted links now occupy diagonal space. The world AABB reads ~14 mm
    // where the model frame measures the 87 mm pad closure, since the static knuckles
    // still bound the widest points.
    const shrunk = openSpan.map((top, axis) => top - closedSpan[axis]).filter(delta => delta > 1e-6);
    assert.equal(shrunk.length, 1, `the fingers close along one axis, got ${JSON.stringify({ openSpan, closedSpan })}`);
    assert.ok(shrunk[0] > 0.012, `the pad gap must lose more than 12 mm, got ${shrunk[0]}`);
    const gripperWorld = await world();
    const gripperDriver = gripperWorld.document.objects.find(object => object.object_id_in_scene === 3)
      .components.find(component => component.component_type === 'RobotDriver').properties;
    assert.deepEqual(gripperDriver.now_joint_positions_rad, { robotiq_2f85_right_driver_joint: 0.8 },
      'one joint written, nothing else in the dictionary');
    assert.equal(gripperWorld.document.scene_version, versionBefore, 'still a local edit (§4.8)');
    assert.equal(reports.length, reportsBeforeGripper, 'and still not one frame reported');
    assert.ok(await page.locator('#toast').evaluate(node => node.classList.contains('hidden')),
      'the asset names every joint it has, so nothing warns');
    check('§12.8c mimic-coupled gripper (OLS robot 2)', {
      objRequests: objFetched.length, mtlRequests: mtlFetched.length,
      spanOpen: openSpan.map(number => Number(number.toFixed(4))),
      spanClosed: closedSpan.map(number => Number(number.toFixed(4))), collapsed,
      now: gripperDriver.now_joint_positions_rad, revision: gripperWorld.document.scene_version });

    // The switch is a deployment decision, so its off state has to be on screen too: serve
    // the runtime config with the key off and reload. This runs after the frame count above
    // because a reload does report its world, and that report is not a local edit.
    await page.route('**/api/runtimeConfig', (route) => route.fulfill({
      status: 200,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ...runtime, allow_details_joint_angle_edit: false }),
    }));
    await page.reload({ waitUntil: 'domcontentloaded' });
    // Q92: the header's dot/label pair is gone, so the bottom status bar is the connection readout.
    await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
      null, { timeout: 15000 });
    await page.setInputFiles('#scene-file', SCENE_FIXTURE);
    await page.waitForFunction(() => /#1 ur5e_table_empty · v3 · 8 objects/.test(document.querySelector('#status-primary')?.textContent || ''));
    await page.waitForTimeout(500);
    await page.locator('[data-panel="scene"]').click();
    await page.locator('.tree-row[data-entity="2"]').click();
    await page.locator('[data-panel="details"]').click();
    assert.equal(await page.locator('[data-joint]').count(), 0,
      'flag off: the Robot Driver block renders no input at all (Q81)');
    assert.match(((await page.locator('#context-panel').textContent()) || ''), /Parked pose \(rad\)/,
      'and the pose is back to a readout');
    await page.unroute('**/api/runtimeConfig');

    // §7.3 (Q96, opacity from Q97): the segment-end ghost is more on the screen and
    // nothing more anywhere else. The same frame with the key on and off has to leave the
    // sensor byte for byte the same and change the viewport, and so does the constant.
    async function clip200() {
      const pushed = await write('/updateWorld', clipWorld());
      assert.equal(pushed.ok, true, JSON.stringify(pushed));
      await waitGeometry(ARM);
      await waitGeometry(PROBE);
      const loaded = await pushTrajs([CLIP_BYTES]);
      assert.equal(loaded.status, 200, 'the clip must load for the ghost to have a step to preview');
      await page.waitForFunction(() => !document.querySelector('#timeline')?.classList.contains('hidden'));
      // Frame 200 sits inside the first step run (120-339), so its ghost holds frame 339's pose.
      await seek(200);
      assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '200 / 719');
      await page.waitForTimeout(800);
      return capture();
    }

    /** Reload on a patched runtime config, then play the clip to frame 200 and take both views. */
    async function ghostPass(patch) {
      await page.route('**/api/runtimeConfig', (route) => route.fulfill({
        status: 200,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...runtime, ...patch }),
      }));
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
        null, { timeout: 15000 });
      const shot = await clip200();
      return { ...shot, view: await stableShot() };
    }

    const ghostOn = await clip200();
    const viewOn = await stableShot();
    const ghostOff = await ghostPass({ features: { ...runtime.features, 'playback.ghostPose': false } });
    await page.unroute('**/api/runtimeConfig');

    assert.equal(ghostOff.body.frame_index, ghostOn.body.frame_index, 'both sides read the same playback frame');
    assert.equal(ghostOff.image.toString('base64'), ghostOn.image.toString('base64'),
      'Q96: the translucent arm never reaches the sensor (§4.4)');
    assert.equal(ghostOff.depth.toString('base64'), ghostOn.depth.toString('base64'),
      'and it never reaches the depth buffer');
    assert.notDeepEqual(ghostOff.view, viewOn, 'Q96: with the key on, the arm of frame 339 shows translucent');
    const ghostWorld = await world();
    assert.deepEqual(ids(ghostWorld.document), [0, 1, 2, 3, 4, 6], 'Q96: a ghost is not an object (§5.5)');

    // **Q97**: two configured opacities are two different pictures, so the number really is
    // the one from `constant_config.json`, and neither of them is on the sensor. Fully
    // transparent has to be indistinguishable from no ghost at all.
    const dense = await ghostPass({ ghost_pose_opacity: 0.9 });
    const faint = await ghostPass({ ghost_pose_opacity: 0.05 });
    const invisible = await ghostPass({ ghost_pose_opacity: 0 });
    await page.unroute('**/api/runtimeConfig');
    const sensor = (shot) => shot.image.toString('base64');
    assert.equal(dense.image.toString('base64'), faint.image.toString('base64'),
      'Q97: no opacity reaches the sensor (§4.4)');
    assert.equal(invisible.image.toString('base64'), ghostOff.image.toString('base64'),
      'and neither does the fully transparent one');
    assert.notDeepEqual(dense.view, faint.view, 'Q97: ghost_pose_opacity paints the ghost, no hardcoded value');
    assert.deepEqual(invisible.view, ghostOff.view,
      'Q97: an opacity of 0 is the same picture as a ghost that was never built');

    check('§7.3 segment-end ghost (Q96, Q97)', { frame: ghostOn.body.frame_index,
      sensorIdentical: sensor(ghostOff) === sensor(ghostOn) && sensor(dense) === sensor(faint),
      viewportDiffers: Buffer.compare(viewOn, ghostOff.view) !== 0,
      opacityDiffers: Buffer.compare(dense.view, faint.view) !== 0,
      objects: ids(ghostWorld.document) });

    // §7.3 (Q101, Q102): the waypoint spheres. The evidence is the one console.debug line the
    // browser writes per paint, because a sphere is an editor layer - no pixel readout can
    // count them and no /getWorld entry may mention them.
    const ARM3 = 2, GRIPPER3 = 3, TABLE3 = 1, APPLE3 = 7, OVER3 = 5;
    /** A frame inside the 630-749 run, where the clip hangs the apple on the gripper. */
    const CARRY_FRAME = 700;
    const distance = (left, right) => Math.hypot(...left.map((value, axis) => value - right[axis]));
    const layerShape = line => line.layers.map(layer =>
      [layer.key, layer.objectId, layer.count, layer.radius, layer.color, layer.visible]);
    /**
     * The next settled "Trajectory waypoints" line. A URDF that lands after its clip measures
     * no tool centre point on the first pass and repaints when the asset arrives (§7.3, Q101),
     * so a caller that wants spheres says how many layers it wants and reads the line that has
     * them rather than the one that has nothing yet.
     */
    async function waypointLog(after, label, layersWanted = 1) {
      for (let attempt = 0; attempt < 300; attempt += 1) {
        const line = waypointLogs[waypointLogs.length - 1];
        if (waypointLogs.length > after && (line ? line.layers.length : 0) >= layersWanted) return line;
        await page.waitForTimeout(100);
      }
      throw new Error(`no "Trajectory waypoints" line with ${layersWanted} layer(s) after ${label} - `
        + 'the browser draws debug lines only if main.ts still logs them');
    }

    // ① and ③, first half: §12.4's own fixture world holds one driver object, so that arm is
    // its own leaf arm and 720 frames at the default stride are 73 spheres.
    await write('/updateWorld', clipWorld());
    await waitGeometry(ARM);
    await waitGeometry(PROBE);
    let mark = waypointLogs.length;
    await pushTrajs([CLIP_BYTES]);
    const solo = await waypointLog(mark, 'the single-driver clip');
    assert.deepEqual([solo.active, solo.showAll], [0, false], '①: the clip is on stage and the checkbox is not');
    assert.deepEqual(layerShape(solo), [['0:1', ARM, 73, 0.02, 0xff5d5d, true]],
      `①③: one leaf arm, 73 spheres at stride 10, got ${JSON.stringify(layerShape(solo))}`);
    assert.equal(solo.layers[0].points.length, 73, 'and the line carries one point per sphere (§12.13 ⑥)');
    check('§12.13 ①③ a 720-frame clip at the default stride', { layers: layerShape(solo) });

    async function waitRendered(objectId) {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const { status } = await readJson(`/get2DAABB?id=${objectId}&camera_id=${OVER3}`);
        if (status === 200) return;
        await page.waitForTimeout(200);
      }
      throw new Error(`object ${objectId} never reported measurable geometry`);
    }

    /**
     * Reload OLS scene 3 and wait for both robot trees to have expanded. A sweep taken before
     * the URDFs arrive finds no tool centre point and lays nothing at all, so the geometry is
     * the gate - installing a clip is not.
     */
    async function scene3() {
      await openAssetRow('scene', 3);
      await page.waitForFunction(() => /#3 ur5e_table_sorting/.test(document.querySelector('#status-primary')?.textContent || ''));
      await page.waitForTimeout(500);
      await waitRendered(ARM3);
      await waitRendered(GRIPPER3);
      await waitRendered(APPLE3);
      await page.locator('[data-panel="scene"]').click();
    }

    /** Post scene 3 back through /updateWorld with the named components taken off one object. */
    async function scene3Without(objectId, types) {
      const doc = (await world()).document;
      const object = doc.objects.find(item => item.object_id_in_scene === objectId);
      object.components = object.components.filter(item => !types.includes(item.component_type));
      // The file also hangs the gripper on the arm with Transform.parent, so unhinging the
      // mount means unhinging the parent too - otherwise the arm stays its ancestor (②).
      if (types.includes('RobotAttachment')) {
        object.components.find(item => item.component_type === 'Transform').properties.parent = null;
      }
      const pushed = await write('/updateWorld', doc);
      assert.equal(pushed.ok, true, JSON.stringify(pushed));
      assert.deepEqual(pushed.rejected_objects, [], `stripping ${types.join('/')} must still be a readable world`);
      await waitRendered(objectId);
      await waitRendered(ARM3);
    }

    // ②, ③ and ⑦, first half: in the real scene the gripper is the only leaf arm.
    await scene3();
    const scene3Doc = JSON.parse(JSON.stringify((await world()).document));
    mark = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES]);
    const mounted = await waypointLog(mark, 'scene 3 with one clip');
    assert.deepEqual(mounted.layers.map(layer => [layer.key, layer.objectId, layer.count]), [[`0:${GRIPPER3}`, GRIPPER3, 85]],
      `②③: only the gripper is a leaf arm, and 840 frames at stride 10 are 85 spheres, got ${JSON.stringify(layerShape(mounted))}`);
    const baseline = mounted.layers[0].points;
    await seek(CARRY_FRAME, 839);
    const apple = await readJson(`/get2DAABB?id=${APPLE3}&camera_id=${OVER3}`);
    assert.equal(apple.status, 200, JSON.stringify(apple.body));
    assert.equal(apple.body.frame_index, CARRY_FRAME, 'the box follows the playback frame (§5.11)');
    const appleCentre = center(apple.body);
    const padPoint = baseline[CARRY_FRAME / 10];

    // ④: a second clip is a second layer, and the checkbox only decides which of them show.
    mark = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES, CLIP3_BYTES], '?count=2');
    const pair = await waypointLog(mark, 'a batch of two clips', 2);
    assert.deepEqual(layerShape(pair), [
      [`0:${GRIPPER3}`, GRIPPER3, 85, 0.02, 0xff5d5d, true],
      [`1:${GRIPPER3}`, GRIPPER3, 85, 0.02, 0x4dd2ff, false],
    ], `④: one layer per clip, only the active one on show, got ${JSON.stringify(layerShape(pair))}`);
    const pairRevision = (await world()).document.scene_version;
    const pairRows = await page.locator('.tree-row').count();
    const pairCount = (await page.locator('#panel-count').textContent()).trim();
    const pairRequests = requests.length;
    const pairReports = reports.length;
    mark = waypointLogs.length;
    await page.locator('#waypoint-all-trajs').check();
    const both = await waypointLog(mark, 'the checkbox ticked', 2);
    mark = waypointLogs.length;
    await page.locator('#waypoint-all-trajs').uncheck();
    const only = await waypointLog(mark, 'the checkbox cleared', 2);
    assert.equal(both.showAll, true);
    assert.deepEqual(both.layers.map(layer => layer.visible), [true, true], '④: ticking shows every clip in the batch');
    assert.deepEqual(only.layers.map(layer => layer.visible), [true, false], '④: and clears back to the active one');
    assert.deepEqual(only.layers.map(layer => layer.color), [0xff5d5d, 0x4dd2ff],
      '④: a clip keeps its own colour across the two states');
    assert.equal(requests.length, pairRequests, '④: ticking the box sends no request');
    assert.equal(reports.length, pairReports, '④: no state_sync frame either');
    assert.equal((await world()).document.scene_version, pairRevision, '④: and it is not a world change');
    assert.deepEqual([await page.locator('.tree-row').count(), (await page.locator('#panel-count').textContent()).trim()],
      [pairRows, pairCount], '④: the tree and the panel count never noticed');
    check('§12.13 ②③④ leaf arms, the stride and the checkbox', {
      single: layerShape(mounted), batch: layerShape(pair), appleCentre: round3(appleCentre) });

    // ⑥: the sweep reads the world as it stands, so moving the base before the clip arrives
    // moves every sphere by the same vector. Q80 still refuses the drag after it arrives.
    await scene3();
    const movedArm = JSON.parse(JSON.stringify(scene3Doc.objects.find(item => item.object_id_in_scene === ARM3)));
    const SHIFT = [0, 0.3, 0];
    const base = movedArm.components.find(item => item.component_type === 'Transform').properties.position;
    movedArm.components.find(item => item.component_type === 'Transform').properties.position
      = base.map((value, axis) => value + SHIFT[axis]);
    await write(`/updateItem?id=${ARM3}`, movedArm);
    mark = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES]);
    const moved = await waypointLog(mark, 'the base moved by 0.3 m');
    assert.deepEqual(moved.layers.map(layer => layer.key), [`0:${GRIPPER3}`], '⑥: the same one leaf arm');
    const revisionAfterMove = (await world()).document.scene_version;
    moved.layers[0].points.forEach((point, index) => point.forEach((value, axis) => assert.ok(
      Math.abs(value - (baseline[index][axis] + SHIFT[axis])) < 1e-4,
      `⑥: sphere ${index} did not follow the base: ${JSON.stringify(point)} vs ${JSON.stringify(baseline[index])}`)));
    assert.equal((await world()).document.scene_version, revisionAfterMove, '⑥: installing the clip moved nothing');
    assert.equal((await page.locator('#timeline-frame').textContent()).trim(), '0 / 839',
      '⑥: and the sweep left the playhead at frame 0');
    await toastCleared();
    await page.locator('[data-tool="translate"]').click();
    await page.locator(`.tree-row[data-entity="${ARM3}"]`).click();
    assert.match(await toastText(), /a trajectory owns the scene while a clip is loaded/,
      '⑥: the drag this round gave up on is still refused (Q80)');
    await page.locator('[data-tool="select"]').click();

    // ②, second half: unhook the gripper and both driver objects are leaf arms.
    await scene3();
    await scene3Without(GRIPPER3, ['RobotAttachment']);
    mark = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES]);
    const split = await waypointLog(mark, 'the unmounted gripper', 2);
    assert.deepEqual(split.layers.map(layer => [layer.key, layer.count]), [['0:2', 85], ['0:3', 85]],
      `②: two disconnected driver objects draw two strings, got ${JSON.stringify(layerShape(split))}`);

    // ⑦, second half: take the gripper's driver away and the arm is the leaf arm again, so the
    // same frame number measures the flange - how far the pad mean sits from the mount point.
    await scene3();
    await scene3Without(GRIPPER3, ['RobotDriver']);
    mark = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES]);
    const flanged = await waypointLog(mark, 'the gripper without a driver');
    assert.deepEqual(flanged.layers.map(layer => [layer.key, layer.objectId]), [[`0:${ARM3}`, ARM3]],
      `⑦: with no driver below it the arm is the leaf arm, got ${JSON.stringify(layerShape(flanged))}`);
    const flangePoint = flanged.layers[0].points[CARRY_FRAME / 10];
    const graspGap = distance(padPoint, appleCentre);
    const flangeGap = distance(flangePoint, appleCentre);
    assert.ok(graspGap < flangeGap,
      `⑦: the deepest leaf links are the two finger pads, so their mean (${graspGap} m) must sit nearer the apple than the arm's flange (${flangeGap} m)`);
    check('§12.13 ⑥⑦ world moves and the grasp point', {
      shift: SHIFT, graspGap: Number(graspGap.toFixed(4)), flangeGap: Number(flangeGap.toFixed(4)),
      padPoint: round3(padPoint), flangePoint: round3(flangePoint), appleCentre: round3(appleCentre),
      unmounted: split.layers.map(layer => layer.key) });

    // ③, second half: the two numbers come from §9.2 and are read at build time, so they are
    // patched into the runtime config and the page is reloaded (坑 22).
    await page.route('**/api/runtimeConfig', (route) => route.fulfill({
      status: 200,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ...runtime, traj_waypoint_stride_frames: 20, traj_waypoint_radius: 0.05 }),
    }));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
      null, { timeout: 15000 });
    await scene3();
    mark = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES]);
    const sparse = await waypointLog(mark, 'stride 20, radius 0.05');
    assert.deepEqual(sparse.layers.map(layer => [layer.key, layer.count, layer.radius]), [[`0:${GRIPPER3}`, 43, 0.05]],
      `③: 840 frames at stride 20 are 43 spheres of radius 0.05, got ${JSON.stringify(layerShape(sparse))}`);
    await page.unroute('**/api/runtimeConfig');

    /**
     * Wait until the stage the clip describes is actually on screen. While a robot's URDF is
     * still on its way its gripper hangs on the arm's own node, so both objects measure the
     * same box; once they differ and stop changing, the subtree is expanded and the pose is
     * the frame's (§8.5: a subtree that lands late re-applies the frame, 坑 67). The gate is
     * deliberately blind to `playback.trajWaypoints`, so both ⑤ passes stand on it.
     */
    async function waitStageSettled() {
      let previous = null;
      for (let attempt = 0; attempt < 80; attempt += 1) {
        const boxes = {};
        for (const id of [ARM3, GRIPPER3]) {
          boxes[id] = (await readJson(`/get2DAABB?id=${id}&camera_id=${OVER3}`)).body.aabb_world;
        }
        const expanded = JSON.stringify(boxes[ARM3]) !== JSON.stringify(boxes[GRIPPER3]);
        if (expanded && previous && JSON.stringify(previous) === JSON.stringify(boxes)) return;
        previous = boxes;
        await page.waitForTimeout(250);
      }
      throw new Error('the two robots never measured different boxes, so no capture here measures an expanded stage');
    }

    /**
     * Load scene 3, play the clip to the carry frame and read back everything ⑤ compares.
     * `layersWanted` is 0 for the pass with `playback.trajWaypoints` off, where no layer
     * may be laid at all.
     */
    async function waypointSweep(layersWanted = 1) {
      await scene3();
      const sweepMark = waypointLogs.length;
      await pushTrajs([CLIP3_BYTES]);
      const line = await waypointLog(sweepMark, 'a sweep', layersWanted);
      await seek(CARRY_FRAME, 839);
      await waitStageSettled();
      const shot = await capture();
      const view = await stableShot();
      // The arm's own box is the pose readout: a URDF that lands after its clip used to
      // bring the subtree in at the declared angle and leave it there (§8.5, 坑 67). The
      // whole reply is kept except its echo `request_id`, because `frame_index` says which
      // frame the measurement was taken on and two sweeps never share a UUID.
      const boxes = {};
      for (const id of [TABLE3, ARM3, GRIPPER3, APPLE3]) {
        const { request_id, ...body } = (await readJson(`/get2DAABB?id=${id}&camera_id=${OVER3}`)).body;
        boxes[id] = body;
      }
      const picked = [];
      for (const [fx, fy] of [[0.5, 0.5], [0.35, 0.62]]) {
        const box = await page.locator('#viewport').boundingBox();
        await page.mouse.click(box.x + box.width * fx, box.y + box.height * fy);
        picked.push(((await page.locator('#selection-label').textContent()) || '').trim());
      }
      return { line, shot, view, boxes, picked };
    }

    const spheresOn = await waypointSweep();
    await page.route('**/api/runtimeConfig', (route) => route.fulfill({
      status: 200,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ...runtime, features: { ...runtime.features, 'playback.trajWaypoints': false } }),
    }));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
      null, { timeout: 15000 });
    const spheresOff = await waypointSweep(0);
    await page.unroute('**/api/runtimeConfig');

    assert.deepEqual(spheresOff.line.layers, [], '⑧: with the key off nothing is laid at all');
    assert.equal(await page.locator('label[data-feature="playback.trajWaypoints"]').evaluate(node => node.hidden), true,
      '⑧: and the checkbox leaves with it');
    assert.equal(spheresOff.shot.image.toString('base64'), spheresOn.shot.image.toString('base64'),
      '⑤: the spheres never reach the sensor (§4.4)');
    assert.equal(spheresOff.shot.depth.toString('base64'), spheresOn.shot.depth.toString('base64'),
      'and never the depth buffer');
    assert.notDeepEqual(spheresOff.view, spheresOn.view, '⑤: on the viewport they are unmistakable');
    assert.deepEqual(spheresOff.boxes, spheresOn.boxes,
      `⑤: they do not widen the world box and leave the pose alone (arm ${JSON.stringify(spheresOn.boxes[ARM3])} vs ${JSON.stringify(spheresOff.boxes[ARM3])})`);
    assert.deepEqual(spheresOff.picked, spheresOn.picked, '⑤: the same pixels pick the same thing, spheres or not');
    assert.deepEqual(ids((await world()).document), ids(scene3Doc), '⑤: a sphere is not an object (§5.5)');

    // ⑧: any caller write ends playback (Q76) and takes the layers with it. The layers have
    // to be on stage first, so the run goes back to the state where the key is on.
    await page.unroute('**/api/runtimeConfig');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
      null, { timeout: 15000 });
    await scene3();
    const beforeWrite = waypointLogs.length;
    await pushTrajs([CLIP3_BYTES]);
    assert.equal((await waypointLog(beforeWrite, 'the clip before the write')).layers.length, 1);
    const afterWrite = waypointLogs.length;
    await write(`/updateItem?id=${APPLE3}`, scene3Doc.objects.find(item => item.object_id_in_scene === APPLE3));
    const cleared = await waypointLog(afterWrite, 'the write that ends playback', 0);
    assert.deepEqual(cleared.layers, [], '⑧: an editor layer that was never an object leaves no trace');
    assert.ok(await page.locator('#timeline').evaluate(node => node.classList.contains('hidden')),
      '⑧: and playback really did end');
    check('§12.13 ⑤⑧ the spheres stay in the editor layer', {
      sensorIdentical: spheresOff.shot.image.toString('base64') === spheresOn.shot.image.toString('base64'),
      viewportDiffers: Buffer.compare(spheresOff.view, spheresOn.view) !== 0,
      armOnFrame: round3(center(spheresOn.boxes[ARM3])),
      picked: spheresOn.picked, sparse: layerShape(sparse), cleared: cleared.layers });

    // §4.4/§4.5 (Q105, Q106): the camera frustum lines and the light placeholder spheres.
    // Scene 1 is the only stage carrying both families *and* cameras with a mesh of their own:
    // `0`/`7` are lights with no mesh, `4`/`5` are D435i cameras with one and `far` of 10 m.
    const AMBIENT1 = 0, TABLE1 = 1, WRIST1 = 4, OVER1 = 5, SUN1 = 7;
    const FRUSTUM_KEY = 'viewport.cameraFrustumAlways', SPHERE_KEY = 'viewport.lightMarkerAlways';
    const fixtureObjects = JSON.parse(readFileSync(SCENE_FIXTURE, 'utf8')).objects;
    const farPlaneOf = id => fixtureObjects.find(item => item.object_id_in_scene === id)
      .components.find(item => item.component_type === 'Camera').properties.far;

    /** The newest "Viewport markers" line - the only page-side truth about a marker's `visible`. */
    async function markerLine(after, label) {
      for (let attempt = 0; attempt < 200; attempt += 1) {
        if (markerLogs.length > after) return markerLogs[markerLogs.length - 1];
        await page.waitForTimeout(50);
      }
      throw new Error(`no "Viewport markers" line after ${label} - renderer.ts must still log one per change`);
    }
    const litOf = line => line.markers.filter(marker => marker.visible)
      .map(marker => `${marker.objectId}:${marker.kind}`).sort();
    const pickedId = label => {
      const match = /#(\d+)$/.exec((label || '').trim());
      return match ? Number(match[1]) : null;
    };
    /** Capture from a real rgbd camera and keep both buffers for the byte comparison. */
    async function sensorShot(objectId) {
      const { status, body } = await readJson(`/captureScene?id=${objectId}&type=rgbd`);
      assert.equal(status, 200, JSON.stringify(body).slice(0, 200));
      return { frame: body.frame_index, image: Buffer.from(body.image_base64, 'base64'),
        depth: Buffer.from(body.depth_base64, 'base64') };
    }
    /**
     * Reload with the two keys patched, put scene 1 on stage, optionally select an object and
     * read back everything §12.14 compares. The keys are read at startup (坑 22) and the route
     * covers only the page's own requests (坑 68), so the evidence is the line the page prints.
     */
    async function markerPass(patch, select, label) {
      await page.unroute('**/api/runtimeConfig');
      await page.route('**/api/runtimeConfig', route => route.fulfill({
        status: 200,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...runtime, features: { ...runtime.features, ...patch } }),
      }));
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => (document.querySelector('#status-primary')?.textContent || '').startsWith('Connected'),
        null, { timeout: 15000 });
      const before = markerLogs.length;
      await page.setInputFiles('#scene-file', SCENE_FIXTURE);
      await page.waitForFunction(() => /#1 ur5e_table_empty · v3 · 8 objects/.test(document.querySelector('#status-primary')?.textContent || ''));
      await waitRendered(WRIST1);
      await page.locator('[data-panel="scene"]').click();
      if (select !== null) await page.locator(`.tree-row[data-entity="${select}"]`).click();
      await markerLine(before, `${label} (selected ${select})`);
      await page.waitForTimeout(400);
      const { document: doc } = await world();
      return { line: markerLogs[markerLogs.length - 1], view: await stableShot(), sensor: await sensorShot(OVER1),
        revision: doc.scene_version, ids: ids(doc),
        rows: await page.locator('.tree-row').count(),
        count: (await page.locator('#panel-count').textContent()).trim() };
    }
    /** Click a grid over the canvas and report what each pixel selected, in order. */
    async function clickGrid() {
      const box = await page.locator('#viewport').boundingBox();
      const picked = [];
      for (const fy of [0.3, 0.5, 0.7]) {
        for (const fx of [0.3, 0.45, 0.6, 0.75]) {
          await page.mouse.click(box.x + box.width * fx, box.y + box.height * fy);
          picked.push(((await page.locator('#selection-label').textContent()) || '').trim());
        }
      }
      return picked;
    }

    // ① and ③: the four combinations of the two keys, each measured with nothing selected.
    // One key never reaches the other family, and both off leaves the stage clean.
    const combos = [
      { lit: [] },
      { patch: { [FRUSTUM_KEY]: true }, lit: [`${WRIST1}:cameraFrustum`, `${OVER1}:cameraFrustum`] },
      { patch: { [SPHERE_KEY]: true }, lit: [`${AMBIENT1}:lightSphere`, `${SUN1}:lightSphere`] },
      { patch: { [FRUSTUM_KEY]: true, [SPHERE_KEY]: true },
        lit: [`${AMBIENT1}:lightSphere`, `${WRIST1}:cameraFrustum`, `${OVER1}:cameraFrustum`, `${SUN1}:lightSphere`] },
    ];
    const flags = {};
    for (const combo of combos) {
      const patch = combo.patch || {};
      const wanted = [patch[FRUSTUM_KEY] === true, patch[SPHERE_KEY] === true];
      const pass = await markerPass(patch, null, `the ${wanted.join('/')} flags`);
      flags[wanted.join('/')] = pass;
      assert.deepEqual([pass.line.cameraFrustumAlways, pass.line.lightMarkerAlways], wanted,
        `③: ${wanted.join('/')} must reach the renderer`);
      assert.equal(pass.line.selected, null, `③: ${wanted.join('/')} is measured with nothing selected`);
      assert.deepEqual(litOf(pass.line), combo.lit,
        `①③: ${wanted.join('/')} lights exactly its own family, got ${JSON.stringify(litOf(pass.line))}`);
    }
    const quiet = flags['false/false'];
    check('§12.14 ①③ the two keys, four combinations', Object.fromEntries(
      Object.entries(flags).map(([key, pass]) => [key, litOf(pass.line)])));

    // ②: with both keys off, a selection is the only thing that puts a marker on screen, and
    // always at most one frustum plus one sphere plus its label.
    for (const [objectId, lit] of [
      [WRIST1, [`${WRIST1}:cameraFrustum`]],
      [AMBIENT1, [`${AMBIENT1}:lightLabel`, `${AMBIENT1}:lightSphere`]],
      [TABLE1, []],
    ]) {
      const pass = await markerPass({}, objectId, `#${objectId} selected`);
      assert.equal(pass.line.selected, objectId, `②: the line follows the selection, too`);
      assert.deepEqual(litOf(pass.line), lit,
        `②: selecting #${objectId} lights only its own marker, got ${JSON.stringify(litOf(pass.line))}`);
    }

    // ④: the box a selection outline is drawn from is the camera's own mesh - centimetres, not
    // the 10 m frustum the old `BoxHelper(node)` framed - and a light with no mesh has no box.
    const cameraBox = (await readJson(`/get2DAABB?id=${WRIST1}&camera_id=${OVER1}`)).body.aabb_world;
    const spans = cameraBox.min.map((minimum, axis) => cameraBox.max[axis] - minimum);
    const meshSpan = Math.max(...spans);
    assert.ok(meshSpan > 0 && meshSpan < farPlaneOf(WRIST1) / 4,
      `④: the D435i box is ${meshSpan} m across while its frustum reaches ${farPlaneOf(WRIST1)} m`);
    const lightBox = await readJson(`/get2DAABB?id=${AMBIENT1}&camera_id=${OVER1}`);
    assert.deepEqual([lightBox.status, lightBox.body.error.code], [400, 'EMPTY_AABB'],
      '④: an object whose content is only editor layers has no box at all (§4.5)');

    // ⑤: none of it reaches the sensor. Four flag states, one frame, byte for byte the same
    // picture - while the viewport shows the difference, and the world never noticed.
    for (const [key, pass] of Object.entries(flags)) {
      assert.equal(pass.sensor.frame, quiet.sensor.frame, `⑤: ${key} reads the same frame`);
      assert.equal(pass.sensor.image.toString('base64'), quiet.sensor.image.toString('base64'),
        `⑤: the markers under ${key} never reach the sensor (§4.4)`);
      assert.equal(pass.sensor.depth.toString('base64'), quiet.sensor.depth.toString('base64'),
        `and never the depth buffer (${key})`);
      assert.deepEqual([pass.revision, pass.ids, pass.rows, pass.count],
        [quiet.revision, quiet.ids, quiet.rows, quiet.count], `⑤: ${key} is not a world change`);
    }
    assert.notDeepEqual(flags['true/false'].view, quiet.view, '⑤: on the viewport the frustum lines are unmistakable');
    assert.notDeepEqual(flags['false/true'].view, quiet.view, 'and so are the placeholder spheres');
    assert.notDeepEqual(flags['true/true'].view, flags['true/false'].view,
      '③: and each key paints only its own family there too');

    // ⑥: three's raycaster filters on `layers` alone, so an invisible sphere would stay
    // clickable unless `pick()` drops it. A light's only clickable body IS its sphere, so the
    // hidden pass must never answer with a light while the shown pass may.
    await markerPass({ [FRUSTUM_KEY]: true, [SPHERE_KEY]: true }, null, 'the grid with the markers shown');
    const shown = await clickGrid();
    await markerPass({}, null, 'the grid with the markers hidden');
    const hidden = await clickGrid();
    shown.forEach((label, index) => {
      const id = pickedId(label);
      if (id === AMBIENT1 || id === SUN1) return;
      assert.equal(hidden[index], label, `⑥: pixel ${index} selected the same thing with the markers hidden`);
    });
    const spheresHit = shown.filter(label => [AMBIENT1, SUN1].includes(pickedId(label))).length;
    assert.equal(hidden.some(label => [AMBIENT1, SUN1].includes(pickedId(label))), false,
      '⑥: a placeholder sphere nobody can see is not a click target (§4.4, Q106)');

    // ⑦: the two keys touch nothing else - the camera preview still runs and the three pose
    // handles still draw while both families are pinned on.
    const pinned = await markerPass({ [FRUSTUM_KEY]: true, [SPHERE_KEY]: true }, OVER1, 'the camera preview with both keys on');
    assert.ok(litOf(pinned.line).includes(`${OVER1}:cameraFrustum`),
      '②: a selected camera is on show even when its family is already pinned on');
    // The live preview only runs while the Details tab is the open panel (§7.2), so ⑦ has to
    // switch to it - the tree clicks above deliberately kept the Scene tab open.
    await page.locator('[data-panel="details"]').click();
    await page.waitForFunction(() => (document.querySelector('#detail-preview-status')?.textContent || '') === 'live',
      null, { timeout: 15000 });
    const handleless = await stableShot();
    for (const tool of ['translate', 'rotate', 'scale']) {
      await page.locator(`[data-tool="${tool}"]`).click();
      assert.notDeepEqual(await stableShot(), handleless, `⑦: the ${tool} handle still draws with both keys on`);
    }
    await page.locator('[data-tool="select"]').click();
    await page.unroute('**/api/runtimeConfig');
    check('§12.14 ②④⑤⑥⑦ selection, the box, the sensor and the clicks', {
      meshSpan: Number(meshSpan.toFixed(4)), far: farPlaneOf(WRIST1), lightBox: lightBox.body.error.code,
      sensorIdentical: Object.values(flags).every(pass => pass.sensor.image.toString('base64') === quiet.sensor.image.toString('base64')),
      viewportDiffers: Object.entries(flags).filter(([key]) => key !== 'false/false')
        .map(([key, pass]) => [key, Buffer.compare(pass.view, quiet.view) !== 0]),
      spheresHit, selected: Object.fromEntries(Object.entries(flags).map(([key, pass]) => [key, litOf(pass.line)])) });

    // §4.7/§7.3 (**Q107**): the passive URDF model. `docs/ur5e_passive_rig.json` hangs OLS model 30
    // (a microwave whose URDF carries one revolute `door_joint`, limited to [-1.5707963, 0]) on the
    // UR5e's wrist and gives it `RobotDriver{role:"passive"}`, and `examples/passive_rig.traj` drives
    // that door and two of the arm's joints on one 390-frame clock. Everything about the model is
    // ordinary - only the two arm decorations withdraw - so the evidence is those two diagnostics
    // lines, one box that has to keep moving, and one picture that has to change.
    const ARM11 = 2, GRIPPER11 = 3, PASSIVE11 = 4, OVER11 = 5;
    const PASSIVE_LAST = 389;
    // The probe pairs the generator lays out: 100 and 179 differ only in the door, 179 and 269 only
    // in the wrist that carries it, because §8.5 holds a gap at its previous sample.
    const DOOR_CLOSED = 100, DOOR_OPEN = 179, WRIST_MOVED = 269;
    // Any frame inside the arm's first segment (30-89): the ghost needs a step under the playhead.
    const GHOST_FRAME = 60;
    const STRIDE = runtime.traj_waypoint_stride_frames;
    const spheresAtStride = Math.floor(PASSIVE_LAST / STRIDE) + 1 + (PASSIVE_LAST % STRIDE ? 1 : 0);

    /** The newest `Ghosts` line once this exact set is on stage and every member is built. */
    async function ghostLog(after, label, wanted) {
      for (let attempt = 0; attempt < 200; attempt += 1) {
        const line = ghostLogs[ghostLogs.length - 1];
        if (ghostLogs.length > after && line
          && JSON.stringify(line.ghosts.map(ghost => ghost.objectId)) === JSON.stringify(wanted)
          && line.ghosts.every(ghost => ghost.ready)) return line;
        await page.waitForTimeout(100);
      }
      throw new Error(`no "Ghosts" line for ${JSON.stringify(wanted)} after ${label}, last was `
        + JSON.stringify(ghostLogs[ghostLogs.length - 1]) + ' - renderer.ts must still report the mirrored set');
    }
    const ghostIds = line => line.ghosts.map(ghost => `${ghost.objectId}:${ghost.ready}`);
    const layerIds = line => line.layers.map(layer => `${layer.key}/${layer.objectId}/${layer.count}`);
    async function passiveBox(objectId) {
      return (await readJson(`/get2DAABB?id=${objectId}&camera_id=${OVER11}`)).body;
    }
    /** One object's world box once it has stopped moving on the frame the playhead holds. */
    async function settledBox(objectId, frame, label) {
      await seek(frame, PASSIVE_LAST);
      let previous = null;
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const body = await passiveBox(objectId);
        assert.ok(body.aabb_world, `${label}: ${JSON.stringify(body).slice(0, 200)}`);
        assert.equal(body.frame_index, frame, `${label}: measured on the frame that was asked for`);
        if (previous && JSON.stringify(previous) === JSON.stringify(body.aabb_world)) return body.aabb_world;
        previous = body.aabb_world;
        await page.waitForTimeout(250);
      }
      throw new Error(`object ${objectId} never settled on frame ${frame} (${label})`);
    }

    const driverOf = (doc, id) => doc.objects.find(object => object.object_id_in_scene === id)
      .components.find(component => component.component_type === 'RobotDriver').properties;
    const transformOf = (doc, id) => doc.objects.find(object => object.object_id_in_scene === id)
      .components.find(component => component.component_type === 'Transform').properties;

    /**
     * Load the rig and put one edit of it on stage. Every pass starts from a fresh Load Scene, so
     * the world write below ends playback and empties both decoration sets - which is what makes
     * the next `Ghosts` line a statement about this pass and not a leftover of the last one.
     */
    async function passiveRig(label, edit) {
      // Load Scene reads the file on its own time (§4.1a), and every pass of this rig ends with the
      // same status line - `#11 ur5e_passive_rig · v1 · 5 objects` was already true before the new
      // load applied, so waiting on it can hand back the *previous* pass's world. The load then
      // reports itself once (§5.8), and that frame is the moment the file is on stage: without this
      // wait the edit below is written to the old world and the pending load overwrites it a beat
      // later, which reads as "`role` was ignored".
      const reported = reports.length;
      await page.setInputFiles('#scene-file', PASSIVE_FIXTURE);
      for (let attempt = 0; attempt < 100 && reports.length === reported; attempt += 1) {
        await page.waitForTimeout(50);
      }
      assert.ok(reports.length > reported, `${label}: Load Scene never reported the world it read (§5.8)`);
      await page.waitForFunction(() => /#11 ur5e_passive_rig · v1 · 5 objects/
        .test(document.querySelector('#status-primary')?.textContent || ''));
      const doc = (await world()).document;
      if (edit) {
        edit(doc);
        const pushed = await write('/updateWorld', doc);
        assert.deepEqual(pushed.rejected_objects, [], `${label}: the edited rig must still be a readable world`);
      }
      for (const id of [ARM11, GRIPPER11, PASSIVE11]) await waitRendered(id);
      await page.locator('[data-panel="scene"]').click();
      return (await world()).document;
    }

    /**
     * Put the clip on the world that is on stage and read back what it decorates.
     *
     * A ghost mirrors the end of the step the playhead sits in (Q96), so a playhead still parked
     * in the 30-frame pre-roll has no step to preview and an empty set is the honest answer - the
     * readout has to happen on frame 60, inside the arm's first segment. The waypoint layer does
     * not care where the playhead is, so the two lines are read after the same seek.
     */
    async function decoratedPass(label, wantedGhosts) {
      const before = (await world()).document;
      const mark = waypointLogs.length;
      const gmark = ghostLogs.length;
      const ack = await pushTrajs([PASSIVE_RIG_BYTES]);
      assert.equal(ack.status, 200, JSON.stringify(ack.body).slice(0, 300));
      const line = await waypointLog(mark, label);
      await seek(GHOST_FRAME, PASSIVE_LAST);
      const ghosts = await ghostLog(gmark, label, wantedGhosts);
      const after = (await world()).document;
      return { line, ghosts, versions: [before.scene_version, after.scene_version],
        idLists: [ids(before), ids(after)],
        rows: await page.locator('.tree-row').count(),
        count: (await page.locator('#panel-count').textContent()).trim() };
    }
    /** Details prints the role the world actually resolves, not the spelling the file used. */
    const roleReadout = () => page.evaluate(() => [...document.querySelectorAll('.readout')]
      .find(node => node.querySelector('span')?.textContent === 'Role')?.querySelector('code')?.textContent ?? null);

    // ①②③⑤: the rig as written - the arm keeps both decorations and the model keeps neither.
    const rig = await passiveRig('the rig as written', null);
    assert.equal(driverOf(rig, PASSIVE11).role, 'passive', '⑤: /getWorld answers with the field verbatim (§3.6)');
    assert.equal(driverOf(rig, ARM11).role, undefined, '⑥: the arm declares no role and reads as the default');
    const asDrawn = await decoratedPass('the rig with role "passive"', [ARM11]);
    assert.deepEqual(layerIds(asDrawn.line), [`0:${ARM11}/${ARM11}/${spheresAtStride}`],
      `②: only the arm is a waypoint string, got ${JSON.stringify(layerIds(asDrawn.line))}`);
    assert.deepEqual(ghostIds(asDrawn.ghosts), [`${ARM11}:true`], '③: and only the arm is mirrored');
    assert.deepEqual(asDrawn.versions, [1, 1], '③: neither a ghost nor a sphere is a world change');
    assert.deepEqual(asDrawn.idLists, [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5]], '⑤: and not an object either (§5.5)');

    // ①: the withdrawn robot is still driven joint by joint, because §8.3 reads `RobotDriver` and
    // §8.5 samples the clip - neither of them has learned the word `role`.
    const doorShut = await settledBox(PASSIVE11, DOOR_CLOSED, 'the door held at 0.0');
    const armShut = await settledBox(ARM11, DOOR_CLOSED, 'the arm at the end of its pan');
    const doorOpen = await settledBox(PASSIVE11, DOOR_OPEN, 'the door at -1.2');
    assert.notDeepEqual(doorOpen, doorShut,
      `①: door_joint is still dispatched, so the model's own box swings with it, got ${JSON.stringify({ doorShut, doorOpen })}`);
    // ④(a): no Attach table in this clip - the mount lives in the world file, so the only thing
    // that moved between 179 and 269 is the wrist the model hangs on.
    const carried = await settledBox(PASSIVE11, WRIST_MOVED, 'the door held while the wrist turns');
    assert.notDeepEqual(carried, doorOpen, '④: a passive mount follows its parent link like any other');
    await seek(DOOR_OPEN, PASSIVE_LAST);
    const viewArmed = await stableShot();

    // ② and ③, negative control: delete the field and both decorations come back. That is the
    // only distinction the browser can make between "drawn nothing for it" and "never got there".
    const bare = await passiveRig('the rig with the field deleted', doc => { delete driverOf(doc, PASSIVE11).role; });
    assert.equal(driverOf(bare, PASSIVE11).role, undefined, '⑥: the field really is gone');
    const unwithdrawn = await decoratedPass('the rig with no role', [ARM11, PASSIVE11]);
    assert.deepEqual(ghostIds(unwithdrawn.ghosts), [`${ARM11}:true`, `${PASSIVE11}:true`],
      '③: with the field gone the model is mirrored too');
    assert.deepEqual(layerIds(unwithdrawn.line), [`0:${PASSIVE11}/${PASSIVE11}/${spheresAtStride}`],
      `②: and it is now the deepest driven object, so the string moves onto it and off the arm, got ${JSON.stringify(layerIds(unwithdrawn.line))}`);
    assert.deepEqual(await settledBox(PASSIVE11, DOOR_OPEN, 'the door with nothing to withdraw'), doorOpen,
      '⑤: the box is the same box - the field changes nothing about the model itself');
    const viewBare = await stableShot();
    assert.notDeepEqual(viewBare, viewArmed,
      '③: but on the viewport the withdrawn decorations are unmistakable');
    assert.deepEqual([unwithdrawn.rows, unwithdrawn.count], [asDrawn.rows, asDrawn.count],
      '⑤: the tree and the panel count never noticed');

    // ⑥: an unreadable spelling is the same default - not a warning, and not a third behaviour.
    const misspelt = await passiveRig('the rig with role "door"', doc => { driverOf(doc, PASSIVE11).role = 'door'; });
    assert.equal(driverOf(misspelt, PASSIVE11).role, 'door', '⑤: the parser keeps what the file wrote');
    const typo = await decoratedPass('the rig with an unusable role', [ARM11, PASSIVE11]);
    assert.deepEqual(layerIds(typo.line), layerIds(unwithdrawn.line),
      '⑥: "door" behaves exactly like a missing field, waypoints and all');
    assert.deepEqual(ghostIds(typo.ghosts), ghostIds(unwithdrawn.ghosts), '⑥: and so it does for the ghosts');

    // ④(b): the other direction of the mix. The link lookup that resolves a mount consults the
    // parent's robot tree and nothing else (`renderer.ts:worldParentOf()`), so an arm can hang on
    // a withdrawn robot.
    const reversed = await passiveRig('the arm hung on the model', doc => {
      const model = doc.objects.find(object => object.object_id_in_scene === PASSIVE11);
      model.components = model.components.filter(component => component.component_type !== 'RobotAttachment');
      const parked = transformOf(doc, PASSIVE11);
      // The fixture hangs the model on the arm twice - by `RobotAttachment` and by
      // `Transform.parent`, which is the fallback the world reads when no mount is accepted
      // (§4.8). Only the attachment is being moved, so the stale parent has to go with it or
      // the new `parent: 4` on the arm closes a cycle and `/updateWorld` rejects all three rows.
      parked.parent = null;
      parked.position = [0.9, 0, 0.75];
      parked.scale = [1, 1, 1];
      const arm = doc.objects.find(object => object.object_id_in_scene === ARM11);
      const own = transformOf(doc, ARM11);
      own.position = [0, 0, 0];
      own.parent = PASSIVE11;
      arm.components.push({ component_type: 'RobotAttachment', properties: {
        parent_object_id: PASSIVE11, parent_joint: 'door_joint', parent_link: 'base_link',
        child_link: 'base_link', connection_type: 'fixed',
        transform: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], reference_frame: 'parent_link' },
      } });
    });
    assert.equal(reversed.objects.find(object => object.object_id_in_scene === ARM11)
      .components.find(component => component.component_type === 'RobotAttachment').properties.parent_object_id,
      PASSIVE11, '④: the world answers with the mount it was given');
    const onModel = await decoratedPass('the arm on a passive parent', [ARM11]);
    assert.deepEqual(layerIds(onModel.line), [`0:${ARM11}/${ARM11}/${spheresAtStride}`],
      '④: the arm keeps its own string when it hangs on a withdrawn robot');
    assert.deepEqual(ghostIds(onModel.ghosts), [`${ARM11}:true`], '④: and its own ghost');
    const baseHome = (await settledBox(ARM11, 0, 'the mounted arm at its home pose')).min;
    const basePanned = (await settledBox(ARM11, 89, 'the mounted arm after its pan')).min;
    assert.ok(Math.hypot(...baseHome.map((value, axis) => value - basePanned[axis])) > 0.05,
      `④: driven through its own six joints from there too, got ${JSON.stringify({ baseHome, basePanned })}`);
    const shifted = Math.hypot(...(await settledBox(ARM11, DOOR_CLOSED, 'the mounted arm at frame 100')).min
      .map((value, axis) => value - armShut.min[axis]));
    assert.ok(shifted > 0.2, `④: the mount resolved into the model's base_link, got ${shifted}`);

    // ①, the other half: a passive driver takes a caller write, the Q75 merge and the Q76 exit just
    // as an arm does, and Details re-parks its one joint on the same revision.
    await passiveRig('the rig, once more', null);
    await pushTrajs([PASSIVE_RIG_BYTES]);
    const doorBeforeWrite = await settledBox(PASSIVE11, DOOR_CLOSED, 'the door before the write');
    const microwave = (await world()).document.objects.find(object => object.object_id_in_scene === PASSIVE11);
    microwave.components = microwave.components.map(component => component.component_type === 'RobotDriver'
      ? { ...component, properties: { ...component.properties, now_joint_positions_rad: { door_joint: -0.7 } } }
      : component);
    await write(`/updateItem?id=${PASSIVE11}`, microwave);
    await page.waitForTimeout(400);
    assert.notDeepEqual((await passiveBox(PASSIVE11)).aabb_world, doorBeforeWrite,
      '①: the written angle re-parks the door, joint by joint (Q75)');
    assert.ok(await page.locator('#timeline').evaluate(node => node.classList.contains('hidden')),
      '①: and a write to a passive object ends playback like any other (Q76)');
    assert.equal((await passiveBox(PASSIVE11)).frame_index, null, '①: the box stops reporting a playback frame');
    assert.equal(driverOf((await world()).document, PASSIVE11).role, 'passive', '⑤: the merge leaves the field alone');

    await page.locator(`.tree-row[data-entity="${PASSIVE11}"]`).click();
    await page.locator('[data-panel="details"]').click();
    await page.locator(`[data-joint="door_joint"]`).waitFor();
    assert.equal(await roleReadout(), 'passive', '⑤: Details reports the role this object resolves to');
    const revisionBeforeEdit = (await world()).document.scene_version;
    const doorBeforeEdit = (await passiveBox(PASSIVE11)).aabb_world;
    await page.locator('[data-joint="door_joint"]').fill('-0.35');
    await page.locator('[data-joint="door_joint"]').press('Enter');
    await page.waitForTimeout(600);
    assert.notDeepEqual((await passiveBox(PASSIVE11)).aabb_world, doorBeforeEdit,
      '①: one angle typed into Details re-parks that one joint (§4.8.4, Q81)');
    const edited = driverOf((await world()).document, PASSIVE11);
    assert.deepEqual(edited.now_joint_positions_rad, { door_joint: -0.35 },
      '①: the write lands in the dictionary /updateItem already writes');
    assert.equal(edited.role, 'passive', '⑤: a local edit does not rewrite the component');
    assert.equal((await world()).document.scene_version, revisionBeforeEdit, '①: and it stays a local edit (§4.8)');
    check('§12.15 ①④ the door is driven and the mount works both ways', {
      stride: STRIDE, spheresAtStride, doorMoved: JSON.stringify(doorOpen) !== JSON.stringify(doorShut),
      carriedByWrist: JSON.stringify(carried) !== JSON.stringify(doorOpen), armShiftedByMount: Number(shifted.toFixed(4)),
      baseHome: round3(baseHome), basePanned: round3(basePanned), roleAfterWrites: edited.role });

    // The evidence for ②③⑥ is those two diagnostics lines: the model is missing from them only
    // while the field is present, and a deleted field and an unusable one read the same.
    check('§12.15 ②③⑤⑥ the withdrawn decorations', {
      withRole: { layers: layerIds(asDrawn.line), ghosts: ghostIds(asDrawn.ghosts) },
      withoutRole: { layers: layerIds(unwithdrawn.line), ghosts: ghostIds(unwithdrawn.ghosts) },
      misspeltRole: { layers: layerIds(typo.line), ghosts: ghostIds(typo.ghosts) },
      viewportDiffers: Buffer.compare(viewArmed, viewBare) !== 0,
      worldUntouched: { versions: asDrawn.versions, ids: asDrawn.idLists[1], rows: asDrawn.rows, count: asDrawn.count } });

    const negative = await readJson(`/get2DAABB?id=999&camera_id=${CAMERA}`);
    assert.deepEqual([negative.status, negative.body.error.code], [404, 'OBJECT_NOT_FOUND']);
    const mismatch = await readJson(`/captureScene?id=${PROBE}&type=rgbd`);
    assert.deepEqual([mismatch.status, mismatch.body.error.code], [404, 'CAMERA_NOT_FOUND']);
    check('§5.13 negative paths', { negative: mismatch.body.error.code });

    // §4.4/§7.1 (**Q108**): the viewport screenshot button. The three rounds of editor layers
    // before this one could only be read off a `console.debug` line; a clipboard PNG is pixels,
    // so this round counts colors. Scene 1 with a camera selected and `cameraFrustumAlways`
    // pinned on puts two of those layers' own constants on screen - the selection outline's
    // `0xfacc15` and the frustum lines' `0x38bdf8` - and neither of them may reach the clipboard.
    const SNAPSHOT_KEY = 'viewport.snapshot';
    const shotDownloads = [];
    const noteDownload = download => shotDownloads.push(download.suggestedFilename());
    page.on('download', noteDownload);
    // A clipboard read needs the read permission granted to the test context; the product only
    // ever asks for the write (§12.16 ①), so this grant is fixture plumbing, not behavior.
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.bringToFront();

    /**
     * Decode a PNG in the page and count the pixels §12.16 cares about: the two editor-layer
     * colors within +/-12 a channel, everything brighter than the black scene background, and
     * `brown` - the wood table, a real asset no editor layer paints, so a capture that is a
     * genuine frame has to carry a share of it. The literal `lit` count cannot be compared
     * against the screen: the always-on grid, the gizmo axes and the selection frustum pyramid
     * add tens of thousands of lit pixels the capture deliberately drops. The `brown` witness
     * is the positive lower bound (see 坑 82: every "this family must be zero" needs a
     * reference that is not itself in the zeroed family). An FNV hash of the whole RGBA buffer
     * closes it out, so two frames can be called identical without trusting the encoder bytes.
     */
    async function layerPixels(image) {
      return page.evaluate(async (base64) => {
        const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${base64}`)).blob());
        const target = document.createElement('canvas');
        target.width = bitmap.width; target.height = bitmap.height;
        const context = target.getContext('2d', { willReadFrequently: true });
        context.drawImage(bitmap, 0, 0);
        const { data } = context.getImageData(0, 0, target.width, target.height);
        const close = (offset, r, g, b) =>
          Math.abs(data[offset] - r) <= 12 && Math.abs(data[offset + 1] - g) <= 12 && Math.abs(data[offset + 2] - b) <= 12;
        let outline = 0, frustum = 0, lit = 0, brown = 0, hash = 0x811c9dc5;
        for (let offset = 0; offset < data.length; offset += 4) {
          const r = data[offset], g = data[offset + 1], b = data[offset + 2];
          if (close(offset, 0xfa, 0xcc, 0x15)) outline += 1;
          if (close(offset, 0x38, 0xbd, 0xf8)) frustum += 1;
          if (r > 8 || g > 8 || b > 8) lit += 1;
          if (r > 40 && r < 200 && g > 15 && r - g >= 15 && r - g <= 90 && b < g) brown += 1;
          for (let channel = 0; channel < 4; channel += 1) {
            hash = Math.imul(hash ^ data[offset + channel], 0x01000193) >>> 0;
          }
        }
        return { width: target.width, height: target.height, outline, frustum, lit, brown,
          pixels: data.length / 4, hash: hash.toString(16) };
      }, image.toString('base64'));
    }

    /** Click the button, wait for its own toast, and read the PNG back out of the clipboard. */
    async function clipboardShot() {
      // The toast lives five seconds, so waiting on its text alone would accept the previous
      // click's message and read the clipboard before this one had written it.
      await toastCleared();
      await page.bringToFront();
      await page.locator('[data-action="snapshot"]').click();
      await page.waitForFunction(() => /截图已复制：\d+×\d+ 像素/.test(document.querySelector('#toast')?.textContent || ''),
        null, { timeout: 15000 });
      const toast = ((await page.locator('#toast').textContent()) || '').trim();
      // `clipboard.read()` hands back item *references*, and Chromium invalidates them when the
      // clipboard generation moves again - which the app's own write is still doing for a moment
      // after its toast lands. `getType()` then throws `InvalidStateError: Clipboard data has
      // changed`, so the whole read is retried against the *current* clipboard (坑 84's shared
      // toast is what makes this read racy in the first place).
      let png = null;
      for (let attempt = 0; attempt < 4 && png === null; attempt += 1) {
        if (attempt > 0) await page.waitForTimeout(300);
        png = await page.evaluate(async () => {
          for (const item of await navigator.clipboard.read()) {
            if (!item.types.includes('image/png')) continue;
            const bytes = new Uint8Array(await item.getType('image/png').then(blob => blob.arrayBuffer()));
            let binary = '';
            for (let offset = 0; offset < bytes.length; offset += 32768) {
              binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + 32768));
            }
            return btoa(binary);
          }
          return null;
        }).catch(error => {
          if (!/Clipboard data has changed|InvalidStateError/.test(String(error))) throw error;
          return null;
        });
      }
      assert.ok(png, '①: the clipboard carries an image/png');
      return { toast, bytes: Buffer.from(png, 'base64') };
    }

    // ①②: put scene 1 on stage with the frustum lines pinned, then look for a selection that
    // really does paint both editor-layer colors on the screen - the whole point of the round
    // needs something unmistakably on screen and unmistakably absent from the clipboard.
    await markerPass({ [FRUSTUM_KEY]: true }, null, 'the screenshot frame');
    const probes = {};
    let shotObject = null;
    let onScreen = null;
    for (const objectId of [WRIST1, OVER1]) {
      await page.locator(`.tree-row[data-entity="${objectId}"]`).click();
      await page.waitForTimeout(400);
      const view = await stableShot();
      probes[objectId] = await layerPixels(view);
      if (probes[objectId].outline > 0 && probes[objectId].frustum > 0) {
        shotObject = objectId; onScreen = probes[objectId];
        break;
      }
    }
    assert.ok(shotObject !== null,
      `②: no camera selection puts both the outline and the frustum lines on screen: ${JSON.stringify(probes)}`);

    // ④: everything a click must not touch, read before and after the same click.
    const { document: shotWorld } = await world();
    const quietBefore = {
      ids: ids(shotWorld), revision: (await page.locator('#status-revision').textContent()).trim(),
      rows: await page.locator('.tree-row').count(),
      count: (await page.locator('#panel-count').textContent()).trim(),
      frame: (await page.locator('#timeline-frame').textContent()).trim(),
      label: (await page.locator('#selection-label').textContent()).trim(),
      requests: requests.length, reports: reports.length,
    };
    await page.waitForTimeout(500);
    const requestsAtClick = requests.length;
    const shot = await clipboardShot();
    const inClipboard = await layerPixels(shot.bytes);
    const drawingBuffer = await page.evaluate(() => {
      const node = document.querySelector('#viewport');
      return [node.width, node.height];
    });
    check('§12.16 ① the button, the PNG and the toast', {
      selected: shotObject, toast: shot.toast, drawingBuffer, png: pngSize(shot.bytes), clipboard: inClipboard });
    assert.deepEqual(pngSize(shot.bytes), drawingBuffer,
      '①: the PNG is the canvas drawing buffer, not the CSS box (§4.4)');
    assert.equal(shot.toast, `截图已复制：${drawingBuffer[0]}×${drawingBuffer[1]} 像素`,
      '①: the toast reports the same size');
    assert.deepEqual(shotDownloads, [], '①: nothing was downloaded or saved (§4.4)');

    // ②: the core criterion - the screen carries both layers, the clipboard carries neither,
    // and the clipboard still carries the world. The second half is the one worth reading
    // twice: it cannot compare against `lit`, because the screen's `lit` is dominated by the
    // always-on grid, the gizmo axes and the frustum pyramid, all of which the capture drops
    // on purpose. The wood table is the witness no editor layer paints.
    assert.ok(onScreen.outline > 0 && onScreen.frustum > 0, `②: the screen shows both layers (${JSON.stringify(onScreen)})`);
    assert.deepEqual([inClipboard.outline, inClipboard.frustum], [0, 0],
      `②: not one pixel of the outline or the frustum lines reaches the clipboard, got ${JSON.stringify(inClipboard)}`);
    assert.ok(onScreen.brown > 0 && inClipboard.brown > onScreen.brown / 2,
      `②: and the capture is a real frame, not a black one (${inClipboard.brown} of ${inClipboard.pixels} table pixels, screen ${onScreen.brown}; ${inClipboard.lit} lit)`);

    // ④ again, from the other side of the same click.
    const { document: afterWorld } = await world();
    assert.deepEqual({
      ids: ids(afterWorld), revision: (await page.locator('#status-revision').textContent()).trim(),
      rows: await page.locator('.tree-row').count(),
      count: (await page.locator('#panel-count').textContent()).trim(),
      frame: (await page.locator('#timeline-frame').textContent()).trim(),
      label: (await page.locator('#selection-label').textContent()).trim(),
    }, { ids: quietBefore.ids, revision: quietBefore.revision, rows: quietBefore.rows,
      count: quietBefore.count, frame: quietBefore.frame, label: quietBefore.label },
      '④: the click changed no world, no selection, no playhead and no panel');
    const pageRequests = requests.length - requestsAtClick;
    assert.equal(pageRequests, 0, '④: a screenshot is not an API call (§5.9)');
    assert.equal(reports.length, quietBefore.reports, '④: and it sends no browser.state_sync');
    // Measured here, not in the report below: ③ reloads the page four times and each Load
    // Scene sends its own `browser.state_sync`, so a delta read at the end would attribute
    // those four frames to the screenshot click.
    const clickSideEffects = { requests: pageRequests, reports: reports.length - quietBefore.reports };

    // ⑤: the viewport camera moved, so the clipboard picture has to change - while the sensor
    // pass, which `captureViewport()` also borrows a render target from, stays byte for byte.
    const sensorBefore = await sensorShot(shotObject);
    await page.locator('[data-action="frame"]').click();
    await page.waitForTimeout(800);
    const movedShot = await layerPixels((await clipboardShot()).bytes);
    const sensorAfter = await sensorShot(shotObject);
    assert.notEqual(movedShot.hash, inClipboard.hash, '⑤: the frame the clipboard holds followed the orbit camera');
    assert.equal(sensorAfter.image.toString('base64'), sensorBefore.image.toString('base64'),
      '⑤: and /captureScene never noticed the detour (§4.4, Q108)');

    // ③: the two resident keys paint the screen and nothing else. Four combinations, one
    // selection, one camera pose - four identical pictures, four different screens.
    const shotCombos = [
      { patch: {}, label: 'both keys off' },
      { patch: { [FRUSTUM_KEY]: true }, label: 'the frustum lines pinned' },
      { patch: { [SPHERE_KEY]: true }, label: 'the placeholder spheres pinned' },
      { patch: { [FRUSTUM_KEY]: true, [SPHERE_KEY]: true }, label: 'both pinned' },
    ];
    const shots = {};
    for (const combo of shotCombos) {
      const pass = await markerPass(combo.patch, shotObject, combo.label);
      const clip = await clipboardShot();
      shots[combo.label] = { view: pass.view, screen: await layerPixels(pass.view),
        clipboard: await layerPixels(clip.bytes), png: clip.bytes };
    }
    const quietCombo = shotCombos[0].label;
    for (const [label, pass] of Object.entries(shots)) {
      assert.equal(pass.clipboard.hash, shots[quietCombo].clipboard.hash, `③: ${label} captures the same pixels as ${quietCombo}`);
      assert.deepEqual([pass.clipboard.outline, pass.clipboard.frustum], [0, 0], `③: ${label} still holds no editor layer`);
      if (label === quietCombo) continue;
      assert.notEqual(Buffer.compare(pass.view, shots[quietCombo].view), 0, `③: ${label} is a different picture on the screen`);
    }
    check('§12.16 ②③④⑤ the clipboard without the layers', {
      selected: shotObject, screen: { outline: onScreen.outline, frustum: onScreen.frustum },
      clipboard: { outline: inClipboard.outline, frustum: inClipboard.frustum, lit: inClipboard.lit },
      tableWitness: { screen: onScreen.brown, clipboard: inClipboard.brown },
      sensorIdentical: sensorAfter.image.equals(sensorBefore.image), movedHash: movedShot.hash,
      comboHashes: Object.fromEntries(Object.entries(shots).map(([label, pass]) => [label, pass.clipboard.hash])),
      screensDiffer: Object.entries(shots).filter(([label]) => label !== quietCombo)
        .map(([label, pass]) => [label, Buffer.compare(pass.view, shots[quietCombo].view) !== 0]),
      bytesIdentical: Object.values(shots).every(pass => pass.png.equals(shots[quietCombo].png)),
      zeroSideEffects: clickSideEffects,
      downloads: shotDownloads.length });

    // ⑥: the key's whole job is its host - off hides that one button and nothing else. The
    // app-wide mechanism is `applyFeatureVisibility()` setting `hidden` on every `[data-feature]`
    // element, so the node stays in the DOM and a `.count()` criterion can never go green
    // (坑 68 reads the same property for the same reason); `evaluate` itself proves the node is there.
    await markerPass({ [SNAPSHOT_KEY]: false }, shotObject, 'the screenshot key off');
    const hiddenState = async selector => page.locator(selector).evaluate(node => node.hidden);
    const toolbar = {
      snapshot: await hiddenState('[data-action="snapshot"]'),
      frame: await hiddenState('[data-action="frame"]'),
      loadScene: await hiddenState('[data-action="load-scene"]'),
      export: await hiddenState('[data-action="export-scene"]'),
    };
    assert.deepEqual(toolbar, { snapshot: true, frame: false, loadScene: false, export: false },
      `⑥: ${SNAPSHOT_KEY}:false hides only its own button, got ${JSON.stringify(toolbar)}`);
    await page.locator('[data-action="frame"]').click();
    await page.waitForTimeout(400);
    check('§12.16 ⑥ the key and its button', toolbar);
    await page.unroute('**/api/runtimeConfig');
    page.off('download', noteDownload);

    assert.deepEqual(errors, [], 'the page must report no runtime or load errors');
    console.log(JSON.stringify({ result: 'passed', frontend: FRONTEND, ols, checks: report.checks.length }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
