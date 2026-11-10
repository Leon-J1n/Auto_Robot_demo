import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";
import { ColladaLoader } from "three/addons/loaders/ColladaLoader.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import type { WorldComponent } from "../../contracts/types";
import type { LibraryFileKind } from "./library";
import { cameraFrustumSegments, cameraRig } from "./logic";
import { beamQuaternion, LIGHTING_PRESET_BY_ID } from "./lighting";
import { LibraryClient, LibraryError } from "./library";
import { ModelFiles, ThreeRendererAdapter, BOX_WIRE_COLOR, SHADOW_CUBE_MAP_SIZE, SHADOW_MARGIN, SHADOW_MAP_SIZE, SHADOW_NORMAL_BIAS, SIM_WORLD_TILT } from "./renderer";
import { setJointValue, type UrdfJointHandle, type UrdfRobot, type UrdfTree, type UrdfVisual } from "./urdf";
import { parseWorld, World } from "./world";
import type { WorldObjectNode } from "./world";

const fixtureUrl = new URL("../../tests/fixtures/scene_example.json", import.meta.url);
const sampleWorld = () => parseWorld(JSON.parse(readFileSync(fixtureUrl, "utf-8")));

/** `lightLabel` draws its text through a canvas and a texture is created as an `<img>`,
 *  so node tests have to provide both; neither ever finishes loading. */
function stubCanvasDocument(): void {
  const element = () => ({
    width: 0,
    height: 0,
    complete: false,
    style: {},
    getContext: () => ({ font: "", fillStyle: "", fillText: () => undefined }),
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    setAttribute: () => undefined,
  });
  vi.stubGlobal("document", { createElement: element, createElementNS: () => element() });
}

function worldFrom(objects: unknown[]): World {
  return parseWorld({ scene_schema_version: 1, scene_version: 1, scene_id: 7, scene_name: "unit", objects });
}

function transform(parent?: number | null, extra: Record<string, unknown> = {}): WorldComponent {
  return {
    component_type: "Transform",
    properties: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: parent ?? null, ...extra },
  };
}

function object(id: number, name: string, components: WorldComponent[]): Record<string, unknown> {
  return { object_id_in_scene: id, object_name_in_scene: name, components };
}

function bundleOf(modelId: number, entry: string, text: string): ModelFiles {
  const files = new Map<string, ArrayBuffer>([[entry, new TextEncoder().encode(text).buffer as ArrayBuffer]]);
  return modelFiles(modelId, "model", entry, files);
}

function modelFiles(modelId: number, kind: LibraryFileKind, entry: string, files: Map<string, ArrayBuffer>,
                    fileUrl = (id: number, path: string, cos: boolean, fileKind: LibraryFileKind = "model") =>
                      `${cos ? "cos" : "ols"}://${fileKind}/${id}/${path}`): ModelFiles {
  const client = Object.assign(Object.create(LibraryClient.prototype), { fileUrl });
  return new ModelFiles(modelId, kind, entry, false, client, files);
}

/**
 * The adapter is exercised without a GPU: the fields the tested methods read are
 * installed by hand, exactly as the real constructor would.
 */
function adapterFixture(errors: string[] = []) {
  stubCanvasDocument();
  const nodes = new Map<number, THREE.Object3D>();
  const objects = new Map<number, WorldObjectNode>();
  const robotTrees = new Map<number, { robot: UrdfRobot; tree: UrdfTree }>();
  const robots = new Map<string, Promise<unknown>>();
  const simWorld = new THREE.Group();
  simWorld.rotation.x = SIM_WORLD_TILT;
  const scene = new THREE.Scene();
  scene.add(simWorld);
  // The constructor hangs the preset group under `simWorld` (§4.8.1, Q114 ③); a fixture
  // that builds the fields by hand owes the same wiring.
  const overrideLights = new THREE.Group();
  simWorld.add(overrideLights);
  const landedRobots: number[] = [];
  // `captureViewport` refreshes the orbit camera before it renders (§4.4), and `frame()` has
  // always called `update()` too; the stub only has to survive the call. `dispose()` unhooks the
  // keyboard and the context, so it now needs a `dispose` of its own (§4.8.7, Q121).
  const orbit = { enabled: true, update: vi.fn(), dispose: vi.fn() };
  // §4.8.5 (Q117): the only WebGL surface a node test may touch is the shadow switch, and the
  // only frame it may draw is a stubbed one, so `renderer` is a record of what was asked for.
  const rendererStub = {
    shadowMap: { enabled: true, type: THREE.PCFSoftShadowMap },
    getDrawingBufferSize: (out: THREE.Vector2) => out.set(640, 360),
    getContext: () => ({ isContextLost: () => false }),
    dispose: vi.fn(),
  };
  // §4.8.7 (Q121): `dispose()` is part of what a recording has to prove, and it reaches the canvas
  // the constructor was handed - here only the listener unhook is called.
  const canvasStub = { removeEventListener: vi.fn() } as unknown as HTMLCanvasElement;
  // §4.4 (Q131): the constructor's two pieces of viewport furniture. The fixture keeps them out of
  // `scene` on purpose - these tests read what the setters write, and the capture pass already
  // proves a `Helper` never reaches a frame. The axes carry the same Z-up tilt the constructor gives
  // them (Q135), which is the one thing here a by-hand fixture can miss.
  const gridHelper = new THREE.GridHelper(8, 8);
  const axesHelper = new THREE.AxesHelper(1);
  axesHelper.rotation.x = SIM_WORLD_TILT;
  const adapter: ThreeRendererAdapter = Object.assign(Object.create(ThreeRendererAdapter.prototype), {
    scene, simWorld, nodes, objects, robotTrees,
    gridHelper, axesHelper,
    gridEnabled: true,
    worldAxesEnabled: true,
    canvas: canvasStub,
    signatures: new Map<number, string>(),
    models: new Map<string, Promise<ModelFiles>>(),
    templates: new Map<string, Promise<THREE.Object3D>>(),
    robots,
    reported: new Set<string>(),
    library: null,
    selectionBox: null,
    cameraFrustumAlways: false,
    lightMarkerAlways: false,
    // §4.9 (Q130): the same reason for the axes layer, and the same default the flag ships with.
    localAxesEnabled: true,
    lightingOverride: null,
    overrideLights,
    // §4.8.5 (Q117): `Object.create` runs no field initialiser, so the shadow stage's own
    // state is the fixture's to supply.
    shadowsEnabled: true,
    shadowRigDirty: true,
    lastShadowFit: 0,
    // §4.8.6 (Q119): same reason for the wireframe layer's own state.
    boxesEnabled: false,
    boxHelpers: new Map<number, THREE.Box3Helper>(),
    boxSkipped: [] as number[],
    // §4.8.6 (Q149): and for the per-link display mode the constant config drives.
    linkBoxesEnabled: false,
    linkHelpers: new Map<string, THREE.Box3Helper>(),
    exactBoxes: new WeakMap<object, { matrix: THREE.Matrix4; geometry: THREE.BufferGeometry; box: THREE.Box3 }>(),
    lastBoxSync: 0,
    lastBoxReport: "",
    // §4.8.7 (Q121): and again for the recording layer.
    recordTap: null as (() => void) | null,
    recordExcludesOverlays: false,
    lastRecordFrame: 0,
    recordFrames: 0,
    lastRecordReport: "",
    shadowTarget: new THREE.Vector3(),
    renderer: rendererStub,
    selectedId: null,
    ghosts: new Map<number, UrdfTree>(),
    ghostLoading: new Map<number, Promise<boolean>>(),
    ghostGeneration: new Map<number, number>(),
    // §4.6.1: the load-in-flight map `readyObject` awaits is the fixture's to supply too,
    // for the same reason as above (`Object.create` runs no field initialiser).
    loading: new Map<number, Promise<void>>(),
    waypoints: new Map<string, unknown>(),
    scratchVector: new THREE.Vector3(),
    waypointPoint: new THREE.Vector3(),
    simMatrix: new THREE.Matrix4(),
    layerMatrix: new THREE.Matrix4(),
    orbit,
    orbitEnabled: true,
    disposed: false,
    sensorTarget: null,
    sensorFloatTarget: null,
    sensorPixels: null,
    sensorFloatPixels: null,
    sensorImage: null,
    viewportTarget: null,
    viewportPixels: null,
    viewportImage: null,
    sensorCamera: new THREE.PerspectiveCamera(),
    sensorDepth: new THREE.ShaderMaterial({
      uniforms: { nearPlane: { value: 0.01 }, farPlane: { value: 100 } },
      vertexShader: "void main() { gl_Position = vec4(0.0); }",
      fragmentShader: "void main() { gl_FragColor = vec4(0.0); }",
    }),
    sensorMeters: new THREE.ShaderMaterial({
      vertexShader: "void main() { gl_Position = vec4(0.0); }",
      fragmentShader: "void main() { gl_FragColor = vec4(0.0); }",
    }),
    callbacks: {
      onSelect: () => undefined,
      onPerformance: () => undefined,
      onError: (message: string) => { errors.push(message); },
      onRobotTree: (objectId: number) => { landedRobots.push(objectId); },
    },
  });
  return { adapter, canvas: canvasStub, nodes, robotTrees, robots, scene, simWorld, overrideLights, errors, orbit, rendererStub, landedRobots, gridHelper, axesHelper };
}

function meshesOf(node: THREE.Object3D): THREE.Mesh[] {
  const found: THREE.Mesh[] = [];
  node.traverse((child) => { if ((child as THREE.Mesh).isMesh) found.push(child as THREE.Mesh); });
  return found;
}

function linesOf(node: THREE.Object3D): THREE.Line[] {
  const found: THREE.Line[] = [];
  node.traverse((child) => { if ((child as THREE.Line).isLine) found.push(child as THREE.Line); });
  return found;
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("world sync", () => {
  it("mounts the sample world with parent chains, component markers and no placeholder cubes", () => {
    const world = sampleWorld();
    const { adapter, nodes, simWorld } = adapterFixture();

    adapter.syncScene(world);

    expect(nodes.size).toBe(5);
    expect([...nodes.keys()].sort()).toEqual([0, 1, 2, 3, 5]);
    const cube = nodes.get(0)!;
    expect(cube.parent).toBe(simWorld);
    expect(cube.name).toBe("Cube");
    expect(nodes.get(1)!.parent).toBe(cube);
    expect(nodes.get(2)!.parent).toBe(cube);
    expect(nodes.get(3)!.parent).toBe(simWorld);
    expect(nodes.get(5)!.parent).toBe(simWorld);

    // §3.7: an object is only drawn from library geometry or a light/camera marker.
    for (const node of nodes.values()) {
      expect(meshesOf(node).filter((mesh) => mesh.geometry.type === "BoxGeometry")).toHaveLength(0);
    }
    // No model has been fetched, so these objects stay empty nodes inside the hierarchy.
    for (const id of [0, 3, 5]) {
      expect(nodes.get(id)!.children.filter((child) => child.userData.renderContent)).toHaveLength(0);
    }
    expect(nodes.get(1)!.children.filter((child) => child.userData.renderContent).length).toBeGreaterThan(0);

    expect([...nodes.get(1)!.children].some((child) => child instanceof THREE.AmbientLight)).toBe(true);
    const camera = nodes.get(2)!;
    const frustum = [...camera.children].find((child) => child instanceof THREE.LineSegments)!;
    expect(frustum.userData.cameraMarker).toBe(true);
    // Q71: the frustum lines are the camera's only marker, so selecting one shows one shape.
    expect([...camera.children].filter((child) => child.userData.cameraMarker)).toHaveLength(1);
    const expected = cameraFrustumSegments(cameraRig(world.get(2)!.camera!));
    expect((frustum.geometry as THREE.BufferGeometry).getAttribute("position").count).toBe(expected.length / 3);
  });

  it("measures the world box from geometry, not from the layers §4.4 keeps out of a capture", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(sampleWorld());
    // The fixture camera has `far` = 1000, so its frustum lines alone would frame a kilometre.
    expect(adapter.worldAabb(2)).toBeNull();

    const mesh = new THREE.Mesh(new THREE.BoxGeometry(2, 1, 1));
    mesh.position.set(1, 0, 0);
    nodes.get(0)!.add(mesh);
    const whole = adapter.worldAabb()!;
    expect(whole.min.x).toBeCloseTo(0);
    expect(whole.max.x).toBeCloseTo(2);
    // Nothing else in the world has geometry, so the project box is exactly that mesh.
    expect(whole.getCenter(new THREE.Vector3()).x).toBeCloseTo(1);
    expect(whole.max.length()).toBeLessThan(5);
  });

  it("reads Transform.quaternion_xyzw as xyzw (§2.8)", () => {
    const sine = Math.SQRT1_2;
    const { adapter, nodes } = adapterFixture();

    adapter.syncScene(worldFrom([object(1, "rotated", [transform(null, { quaternion_xyzw: [0, 0, sine, sine] })])]));

    const node = nodes.get(1)!;
    expect(node.quaternion.z).toBeCloseTo(sine);
    expect(node.quaternion.w).toBeCloseTo(sine);
    expect(node.rotation.x).toBeCloseTo(0);
    expect(node.rotation.y).toBeCloseTo(0);
    expect(node.rotation.z).toBeCloseTo(Math.PI / 2);
  });

  it("keeps an object without a MetadataRef as an empty node (§3.7)", () => {
    const { adapter, nodes } = adapterFixture();

    adapter.syncScene(worldFrom([
      object(1, "empty", [transform()]),
      object(2, "semantic-only", [transform(), { component_type: "Semantic", properties: { semantic_name: "floor", category: "surface" } }]),
    ]));

    expect([...nodes.get(1)!.children]).toHaveLength(0);
    expect([...nodes.get(2)!.children]).toHaveLength(0);
  });

  it("reports an unconfigured library once per object and still mounts the world", () => {
    const errors: string[] = [];
    const { adapter, nodes } = adapterFixture(errors);

    adapter.syncScene(sampleWorld());

    expect(nodes.size).toBe(5);
    expect(errors.filter((message) => message.includes("object library is not configured"))).toHaveLength(3);
  });

  it("keeps a failed URDF parse a render-side diagnostic, not a §5.13 answer (§6.8)", async () => {
    const errors: string[] = [];
    const bytes = new TextEncoder().encode("<robot name='r'></robot>").buffer as ArrayBuffer;
    const client = Object.assign(Object.create(LibraryClient.prototype), {
      robot: async (modelId: number) => ({ modelId, kind: "robot", path: "arm.urdf", isOnCos: false, reference: [], raw: {} }),
      bundle: async () => ({ entry: "arm.urdf", files: new Map([["arm.urdf", bytes]]), failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?type=robot&id=${id}&path=${path}`,
    });
    const { adapter, nodes, robots } = adapterFixture(errors);
    adapter.setLibrary(client);
    // `urdf.test.ts` proves parseUrdf raises exactly this code; this file runs without a
    // DOM, so the same rejection is installed at the point the renderer would hit it.
    robots.set("robot:0", Promise.reject(new LibraryError("INVALID_URDF", "URDF must declare at least one link", 400)));
    const metadata = { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/robot?id=0" } } as WorldComponent;

    adapter.syncScene(worldFrom([object(1, "arm", [transform(), metadata])]));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // `INVALID_URDF` is the code `error_codes.test.ts` keeps out of §5.13, and this is why:
    // it stops in `loadModel` as one warning, so a broken mesh directory can never turn an
    // applied addObject into a 502 for the caller.
    expect(errors).toEqual(["robot 0 entry arm.urdf failed: URDF must declare at least one link"]);
    expect(nodes.get(1)!.children).toHaveLength(0);
    expect(nodes.get(1)!.userData.renderContent).toBeUndefined();

    // A changed signature re-runs the load, so a second warning can only come from a
    // broken once-per-object rule (§6.8: one bad directory must not spam the panel).
    adapter.syncScene(worldFrom([object(1, "arm",
      [transform(), metadata, { component_type: "MeshRenderer", properties: { forceOverrideColor: "#FF0000" } }])]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(errors).toHaveLength(1);
  });

  it("leaves unchanged objects alone and repopulates when components change", () => {
    const { adapter, nodes } = adapterFixture();
    const first = worldFrom([object(1, "lamp", [transform(), { component_type: "Light", properties: { type: "Ambient", intensity: 0.5 } }])]);
    adapter.syncScene(first);
    const light = [...nodes.get(1)!.children][0];
    expect(light).toBeInstanceOf(THREE.AmbientLight);

    adapter.syncScene(first);
    expect([...nodes.get(1)!.children][0]).toBe(light);

    adapter.syncScene(worldFrom([object(1, "lamp", [transform(), { component_type: "Light", properties: { type: "Point", intensity: 0.5 } }])]));
    const replaced = [...nodes.get(1)!.children][0];
    expect(replaced).toBeInstanceOf(THREE.PointLight);
    expect(replaced).not.toBe(light);
  });

  it("degrades an unknown Light type to Ambient with a warning (O4)", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { adapter, nodes } = adapterFixture();

    adapter.syncScene(worldFrom([object(1, "odd", [transform(), { component_type: "Light", properties: { type: "Hollow" } }])]));

    expect([...nodes.get(1)!.children][0]).toBeInstanceOf(THREE.AmbientLight);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('unknown Light.type "Hollow"'));
  });

  /**
   * Q114's ③ class: a preset may only change how the stage looks. The world it renders is
   * the authority -- an export, `/getWorld` and the digest all read it -- so the test's
   * judgment is `toJSON()` staying byte-for-byte what an untouched scene writes.
   */
  describe("lighting override is a render layer (§4.8.1, Q114 ③)", () => {
    const lampWorld = () => worldFrom([object(1, "lamp", [transform(), { component_type: "Light", properties: { type: "Ambient", intensity: 0.5 } }])]);

    it("dims the file's lights and raises the preset's without touching the document", () => {
      const { adapter, nodes, overrideLights } = adapterFixture();
      const world = lampWorld();
      adapter.syncScene(world);
      const docLight = [...nodes.get(1)!.children].find((child) => child instanceof THREE.Light)!;
      expect(docLight.visible).toBe(true);

      adapter.setLightingOverride(LIGHTING_PRESET_BY_ID.get("studio")!);

      expect(docLight.visible).toBe(false);
      // Studio is ambient + key + fill; a directional carries its own target object.
      expect(overrideLights.children).toHaveLength(3);
      expect(overrideLights.children.map((holder) => holder.children.length)).toEqual([1, 2, 2]);
      expect(world.toJSON()).toEqual(lampWorld().toJSON());
      expect(world.version).toBe(1);

      adapter.setLightingOverride(null);
      expect(docLight.visible).toBe(true);
      expect(overrideLights.children).toHaveLength(0);
    });

    it("re-applies the override when a rebuild made fresh lights", () => {
      const { adapter, nodes, overrideLights } = adapterFixture();
      const preset = LIGHTING_PRESET_BY_ID.get("dramatic")!;
      adapter.syncScene(lampWorld());
      adapter.setLightingOverride(preset);

      // Same component, new signature: `populate` rebuilds the node's children.
      adapter.syncScene(worldFrom([object(1, "lamp", [transform(), { component_type: "Light", properties: { type: "Point", intensity: 0.5 } }])]));

      const rebuilt = [...nodes.get(1)!.children].find((child) => child instanceof THREE.Light)!;
      expect(rebuilt).toBeInstanceOf(THREE.PointLight);
      expect(rebuilt.visible).toBe(false);
      expect(overrideLights.children).toHaveLength(preset.lights.length);
    });

    it("places a preset light on the pose its table entry describes", () => {
      const { adapter, overrideLights } = adapterFixture();
      adapter.setLightingOverride(LIGHTING_PRESET_BY_ID.get("dramatic")!);

      const key = overrideLights.children[1]!;
      expect([...key.position.toArray()]).toEqual([3.2, 0.6, 1.4]);
      expect(key.quaternion.toArray()).toEqual(beamQuaternion([-0.9, -0.15, -0.35]));
      // An ambient entry has no beam and sits at the origin, unrotated.
      expect([...overrideLights.children[0]!.position.toArray()]).toEqual([0, 0, 0]);
      expect(overrideLights.children[0]!.quaternion.toArray()).toEqual([0, 0, 0, 1]);
    });

    it("keeps the markers of a light the document still declares", () => {
      const { adapter, nodes } = adapterFixture();
      adapter.syncScene(lampWorld());
      adapter.setLightingOverride(LIGHTING_PRESET_BY_ID.get("lamp")!);

      // The sphere and the type label belong to object 1, which is still in the scene.
      const markers = [...nodes.get(1)!.children].filter((child) => child.userData.lightMarker || child.userData.lightLabel);
      expect(markers).toHaveLength(2);
      expect(markers.every((marker) => marker.visible === false)).toBe(true);
    });
  });

  /**
   * §4.8.5 (**Q117**): the renderer, not the file, decides who projects - so the questions a
   * node test can ask are which lights get a depth map, what volume they render into, and
   * whether the document noticed. The last one is the whole judgment of class ③.
   */
  describe("shadow rendering is a render layer (§4.8.5, Q117)", () => {
    const lightOf = (nodes: Map<number, THREE.Object3D>, id: number): THREE.Light =>
      [...nodes.get(id)!.children].find((child) => child instanceof THREE.Light) as THREE.Light;
    /** The render loop's own refit, reached the way `syncSelectionBox` is reached above. */
    const fit = (adapter: ThreeRendererAdapter): void => {
      (adapter as unknown as { fitShadowRigs(): void }).fitShadowRigs();
    };
    const isDirty = (adapter: ThreeRendererAdapter): boolean =>
      (adapter as unknown as { shadowRigDirty: boolean }).shadowRigDirty;

    function projectorWorld(sunIntensity = 2): World {
      return worldFrom([
        object(1, "sun", [transform(), { component_type: "Light", properties: { type: "Directional", intensity: sunIntensity } }]),
        object(2, "cone", [transform(), { component_type: "Light", properties: { type: "Spot", intensity: 1 } }]),
        object(3, "bulb", [transform(), { component_type: "Light", properties: { type: "Point", intensity: 1 } }]),
        object(4, "haze", [transform(), { component_type: "Light", properties: { type: "Ambient", intensity: 1 } }]),
        object(5, "dark", [transform(), { component_type: "Light", properties: { type: "Directional", intensity: 0 } }]),
      ]);
    }

    /** A 2 m crate under a lamp and a cone 10 m above it, both beams straight down. */
    function crateWorld(): World {
      return worldFrom([
        object(1, "sun", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Directional", intensity: 2 } }]),
        object(2, "cone", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Spot", intensity: 1 } }]),
        object(3, "crate", [transform(null, { position: [0, 0, 4] })]),
      ]);
    }

    /** The crate's box in Three's Y-up stage: the file's `[0,0,4]` is straight up, so this is 4 m high. */
    const CRATE = new THREE.Box3(new THREE.Vector3(-1, 3, -1), new THREE.Vector3(1, 5, 1));

    function addCrate(nodes: Map<number, THREE.Object3D>): void {
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2));
      mesh.userData.renderContent = true;
      nodes.get(3)!.add(mesh);
    }

    function boxCorners(box: THREE.Box3): THREE.Vector3[] {
      const found: THREE.Vector3[] = [];
      for (const x of [box.min.x, box.max.x]) {
        for (const y of [box.min.y, box.max.y]) {
          for (const z of [box.min.z, box.max.z]) found.push(new THREE.Vector3(x, y, z));
        }
      }
      return found;
    }

    it("gives a light with a direction a depth map and leaves Ambient and a dark one out", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes } = adapterFixture();
      adapter.syncScene(projectorWorld());

      expect(lightOf(nodes, 1).castShadow).toBe(true);
      expect(lightOf(nodes, 2).castShadow).toBe(true);
      expect(lightOf(nodes, 3).castShadow).toBe(true);
      // An Ambient has no direction: its depth map would be a uniform sheet of black.
      expect(lightOf(nodes, 4).castShadow).toBe(false);
      // A light with no intensity darkens nothing, so its pass over the scene is pure cost.
      expect(lightOf(nodes, 5).castShadow).toBe(false);
      // A point light's cube is six draws a frame, so it gets the smaller map (§4.8.5).
      expect((lightOf(nodes, 1) as THREE.DirectionalLight).shadow.mapSize.width).toBe(SHADOW_MAP_SIZE);
      expect((lightOf(nodes, 2) as THREE.SpotLight).shadow.mapSize.width).toBe(SHADOW_MAP_SIZE);
      expect((lightOf(nodes, 3) as THREE.PointLight).shadow.mapSize.width).toBe(SHADOW_CUBE_MAP_SIZE);
      expect((lightOf(nodes, 1) as THREE.DirectionalLight).shadow.normalBias).toBe(SHADOW_NORMAL_BIAS);
      expect(adapter.shadowReport().casters.map((caster) => caster.type))
        .toEqual(["DirectionalLight", "SpotLight", "PointLight"]);
      debug.mockRestore();
    });

    it("leaves the document exactly as it found it", () => {
      const { adapter, rendererStub } = adapterFixture();
      const world = projectorWorld();
      adapter.syncScene(world);
      const before = world.toJSON();

      adapter.setLightingOverride(LIGHTING_PRESET_BY_ID.get("lamp")!);
      adapter.setShadowsEnabled(false);
      adapter.setShadowsEnabled(true);

      // A shadow is not a field §4.5 reads, so no switch of it may reach the file: an export,
      // `/getWorld` and the digest all read this object and must keep reading the same thing.
      expect(world.toJSON()).toEqual(before);
      expect(world.version).toBe(1);
      expect(rendererStub.shadowMap.enabled).toBe(true);
    });

    it("projects with the lights that are actually on the stage", () => {
      const { adapter, nodes } = adapterFixture();
      adapter.syncScene(projectorWorld());
      const sun = lightOf(nodes, 1);

      adapter.setLightingOverride(LIGHTING_PRESET_BY_ID.get("studio")!);

      // Three builds its light list from the visible objects, so a dimmed doc light leaves the
      // shadow pass on its own - nothing here has to know the override exists (§4.8.1).
      expect(sun.visible).toBe(false);
      expect(adapter.shadowReport().casters.map((caster) => caster.type))
        .toEqual(["DirectionalLight", "DirectionalLight"]);
    });

    it("stops the depth pass without rewriting the lights", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene, rendererStub } = adapterFixture();
      adapter.syncScene(projectorWorld());
      const sun = lightOf(nodes, 1);
      const material = new THREE.MeshStandardMaterial();
      scene.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), material));
      const compiled = material.version;

      adapter.setShadowsEnabled(false);
      expect(rendererStub.shadowMap.enabled).toBe(false);
      expect(adapter.shadowsOn).toBe(false);
      expect(adapter.shadowReport().enabled).toBe(false);
      // The flags stay put, so switching back is one render state, not a rebuild of the rig.
      expect(sun.castShadow).toBe(true);
      // three keeps the program it compiled, so a flip only reaches the pixels once every material
      // is rebuilt; without this the browser showed a stage that brightened and never darkened.
      expect(material.version).toBeGreaterThan(compiled);

      adapter.setShadowsEnabled(true);
      expect(rendererStub.shadowMap.enabled).toBe(true);
      expect(adapter.shadowReport().enabled).toBe(true);
      expect(material.version).toBeGreaterThan(compiled + 1);
      debug.mockRestore();
    });

    it("fits a directional volume to the 现场 it sees instead of a hardcoded box", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene } = adapterFixture();
      adapter.syncScene(crateWorld());
      addCrate(nodes);
      // `worldAabb` reads `matrixWorld`, which the render loop keeps current; a node test has
      // to ask for it once before the conversion that makes the stage Y-up shows up in the box.
      scene.updateMatrixWorld(true);

      // That rotation leaves a float or two of dust on the box, so the crate is measured
      // rather than compared bit for bit.
      const measured = adapter.worldAabb()!;
      expect(measured.min.distanceTo(CRATE.min)).toBeLessThan(1e-9);
      expect(measured.max.distanceTo(CRATE.max)).toBeLessThan(1e-9);
      fit(adapter);

      const camera = (lightOf(nodes, 1) as THREE.DirectionalLight).shadow.camera as THREE.OrthographicCamera;
      // 2 m of crate seen from 6 m up, plus the margin - not three's default ±5 and far 500,
      // which would spread 1024² texels over a volume a hundred times the size of the table.
      expect(camera.left).toBeCloseTo(-1 - SHADOW_MARGIN, 2);
      expect(camera.right).toBeCloseTo(1 + SHADOW_MARGIN, 2);
      expect(camera.bottom).toBeCloseTo(-1 - SHADOW_MARGIN, 2);
      expect(camera.top).toBeCloseTo(1 + SHADOW_MARGIN, 2);
      expect(camera.near).toBeCloseTo(5 - SHADOW_MARGIN, 2);
      expect(camera.far).toBeCloseTo(7 + SHADOW_MARGIN, 2);

      // Oracle: three's own frustum, not the arithmetic the fit just ran.
      const frustum = new THREE.Frustum().setFromProjectionMatrix(
        new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse));
      for (const corner of boxCorners(CRATE)) expect(frustum.containsPoint(corner)).toBe(true);
      // Tight, though: a metre outside the padded box is already out of the map.
      expect(frustum.containsPoint(new THREE.Vector3(2, 4, 0))).toBe(false);
      debug.mockRestore();
    });

    /**
     * §4.6.1 (2026-11-05): placement collision reads `sceneAABB`, not `worldAabb` - the same
     * trusted box the wireframe draws, but handed back in the world-file axes so it lines up with
     * the scene-file boxes `spotOnTable` fits into. A crate at file `[0,0,4]` stands 4 m up in the
     * Y-up stage; the scene-file box has to carry that height on the file's own Z, with Three's Y
     * mapped to the file's −Z depth. This pins that the conversion is the wrapper and nothing else.
     */
    it("hands back the object's own box in the world-file axes for placement to read", () => {
      const { adapter, nodes, scene } = adapterFixture();
      adapter.syncScene(crateWorld());
      addCrate(nodes);
      scene.updateMatrixWorld(true);

      const measured = adapter.sceneAABB(3)!;
      // toSceneBox maps Three's (x, y, z) to the file's (x, -z, y): the crate's ±1 in x stay ±1,
      // its 3..5 height in Three-Y becomes the file's 3..5 on Z, and its ±1 in Three-Z flips to ∓1
      // on file Y. Read back off CRATE so the assertion is the geometry, not a hand-computed number.
      expect(measured.min[0]).toBeCloseTo(CRATE.min.x, 6);
      expect(measured.max[0]).toBeCloseTo(CRATE.max.x, 6);
      expect(measured.min[1]).toBeCloseTo(-CRATE.max.z, 6);
      expect(measured.max[1]).toBeCloseTo(-CRATE.min.z, 6);
      expect(measured.min[2]).toBeCloseTo(CRATE.min.y, 6);
      expect(measured.max[2]).toBeCloseTo(CRATE.max.y, 6);
      // Nothing to bound, nothing to measure: the seam reports null and placement falls back.
      expect(adapter.sceneAABB(404)).toBeNull();
    });

    /**
     * The shadow pass is what found this one: a preset light aims along its object's -Z like every
     * other light in §4.5, so a `beam` that lands on +Z points the lamp backwards and its fitted
     * volume brackets the empty sky behind it - a depth map of nothing, and no shadow to see.
     */
    it("aims a preset's declared beam at the 现场 instead of away from it", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene, overrideLights } = adapterFixture();
      adapter.syncScene(crateWorld());
      addCrate(nodes);
      adapter.setLightingOverride({
        id: "overhead",
        label: "Overhead",
        lights: [{ light: { type: "Directional", intensity: 2, color: "#FFFFFF" }, position: [0, 0, 10], beam: [0, 0, -1] }],
      });
      scene.updateMatrixWorld(true);

      fit(adapter);

      const key = [...overrideLights.children[0]!.children].find((child) => child instanceof THREE.DirectionalLight) as THREE.DirectionalLight;
      const camera = key.shadow.camera as THREE.OrthographicCamera;
      const frustum = new THREE.Frustum().setFromProjectionMatrix(
        new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse));
      for (const corner of boxCorners(CRATE)) expect(frustum.containsPoint(corner)).toBe(true);
      debug.mockRestore();
    });

    it("gives a cone the reach to the crate it points at", () => {      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes } = adapterFixture();
      adapter.syncScene(crateWorld());
      addCrate(nodes);

      fit(adapter);

      // A spot and a point are measured by distance to the corners, not by beam projection:
      // their camera is perspective, so the volume only has to bracket what it can turn to.
      // `three` moves a fresh `SpotLight` a unit up on its own, so the oracle reads where the
      // lamp actually stands rather than where the file says its object is.
      const camera = (lightOf(nodes, 2) as THREE.SpotLight).shadow.camera as THREE.PerspectiveCamera;
      const origin = lightOf(nodes, 2).getWorldPosition(new THREE.Vector3());
      const distances = boxCorners(CRATE).map((corner) => corner.distanceTo(origin));
      expect(camera.near).toBeCloseTo(Math.min(...distances) - SHADOW_MARGIN, 3);
      expect(camera.far).toBeCloseTo(Math.max(...distances) + SHADOW_MARGIN, 3);
      debug.mockRestore();
    });

    it("refits on an event and on the clock, not on every frame", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes } = adapterFixture();
      adapter.syncScene(crateWorld());
      addCrate(nodes);

      expect(isDirty(adapter)).toBe(true);
      fit(adapter);
      expect(isDirty(adapter)).toBe(false);

      // The same world again: no node repopulated and no preset light came or went, so the
      // volumes are still the right size - `syncScene` re-applies the override every pass
      // (§4.8.1) and that must not read as an event.
      adapter.syncScene(crateWorld());
      expect(isDirty(adapter)).toBe(false);

      // A component that changed did repopulate, which is exactly when a fit is due.
      adapter.syncScene(worldFrom([
        object(1, "sun", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Directional", intensity: 3 } }]),
        object(2, "cone", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Spot", intensity: 1 } }]),
        object(3, "crate", [transform(null, { position: [0, 0, 4] })]),
      ]));
      expect(isDirty(adapter)).toBe(true);
      debug.mockRestore();
    });

    it("keeps every editor layer out of the depth pass", async () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes } = adapterFixture();
      adapter.setLibrary(stlClient());
      const metadata = { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/5" } } as WorldComponent;
      adapter.syncScene(worldFrom([
        object(1, "sun", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Directional", intensity: 2 } }]),
        object(2, "crate", [transform(null, { position: [0, 0, 4] }), metadata]),
      ]));
      await new Promise((resolve) => setTimeout(resolve, 0));
      adapter.select(2);

      fit(adapter);

      const report = adapter.shadowReport();
      // The light brought a placeholder sphere and a label, the selection brought a helper, and
      // all three are drawn for the editor: the crate's own mesh is the only thing in the map.
      expect(meshesOf(nodes.get(1)!)).toHaveLength(1);
      expect(report.castMeshes).toBe(1);
      expect(report.receiveMeshes).toBe(1);
      // And the box the volumes were fitted to ignores all of them the way §4.4 makes it.
      expect(report.casters).toHaveLength(1);
      debug.mockRestore();
    });

    /** The smallest asset the loader accepts, handed out by a client that never touches the network. */
    function stlClient(): LibraryClient {
      const asciiStl = `solid a\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n  endloop\n endfacet\nendsolid a\n`;
      const bytes = new TextEncoder().encode(asciiStl).buffer as ArrayBuffer;
      return Object.assign(Object.create(LibraryClient.prototype), {
        model: async (modelId: number) => ({ modelId, kind: "model", path: "arm.stl", isOnCos: false, reference: [], raw: {} }),
        bundle: async () => ({ entry: "arm.stl", files: new Map([["arm.stl", bytes]]), failures: [] }),
        fileUrl: (id: number, path: string) => `http://ols.test/file?id=${id}&path=${path}`,
      }) as LibraryClient;
    }

    it("marks the library template once and lets every instance inherit it", async () => {
      const { adapter, nodes } = adapterFixture();
      adapter.setLibrary(stlClient());
      const metadata = { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/5" } } as WorldComponent;

      adapter.syncScene(worldFrom([
        object(1, "first", [transform(), metadata]),
        object(2, "second", [transform(), metadata]),
      ]));
      await new Promise((resolve) => setTimeout(resolve, 0));

      // The flags go on the template, so `cloneAsset` carries them onto each instance and no
      // assembly path - URDF link, attachment, tool - has to remember to ask.
      for (const id of [1, 2]) {
        const meshes = meshesOf(nodes.get(id)!);
        expect(meshes).toHaveLength(1);
        for (const mesh of meshes) {
          expect(mesh.castShadow).toBe(true);
          expect(mesh.receiveShadow).toBe(true);
        }
      }
      expect(adapter.shadowReport().castMeshes).toBe(2);
    });
  });

  /**
   * §4.8.6 (**Q119**): the claim a wireframe layer has to prove is not "it draws lines" but "it
   * draws the box the rest of the editor already measures, and none of it reaches a capture". Both
   * halves are answerable without a GPU; the pixel half lives in §12.20 ③.
   */
  describe("bounding boxes are a render layer (§4.8.6, Q119)", () => {
    const helpers = (adapter: ThreeRendererAdapter): Map<number, THREE.Box3Helper> =>
      (adapter as unknown as { boxHelpers: Map<number, THREE.Box3Helper> }).boxHelpers;
    const editorOnly = (adapter: ThreeRendererAdapter, object: THREE.Object3D): boolean =>
      (adapter as unknown as { isEditorOnly(object: THREE.Object3D): boolean }).isEditorOnly(object);
    const hidden = (adapter: ThreeRendererAdapter): THREE.Object3D[] =>
      (adapter as unknown as { hideOverlays(): THREE.Object3D[] }).hideOverlays();
    /** The refit the render loop runs on its clock, reached the way `fitShadowRigs` is reached above. */
    const refit = (adapter: ThreeRendererAdapter): void => {
      (adapter as unknown as { syncBoundingBoxes(): void }).syncBoundingBoxes();
    };

    /** A lamp and two crates: the lamp has no renderable geometry, so §4.5 gives it no box. */
    function rigWorld(): World {
      return worldFrom([
        object(1, "sun", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Directional", intensity: 2 } }]),
        object(2, "box_a", [transform(null, { position: [0, 0, 4] })]),
        object(3, "box_b", [transform(null, { position: [5, 0, 4] })]),
      ]);
    }

    /** A 2 m cube of asset geometry under each crate, which is what `worldAabb()` walks. */
    function addCrates(nodes: Map<number, THREE.Object3D>): void {
      for (const id of [2, 3]) {
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2));
        mesh.userData.renderContent = true;
        nodes.get(id)!.add(mesh);
      }
    }

    it("waits for the checkbox before it draws anything", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene } = adapterFixture();
      adapter.syncScene(rigWorld());
      addCrates(nodes);

      // Off is a real state, not "the layer exists and nobody can see it": with nothing drawn the
      // walk does not run at all, so the report has no ids in either column.
      expect(adapter.boxReport()).toEqual({ enabled: false, drawn: [], skipped: [], perLink: false, drawnLinks: [] });
      expect(scene.children.filter((child) => child.type === "Box3Helper")).toHaveLength(0);
      // And it is a state the page says out loud. `main.ts:renderBoxes()` asks for the state a load
      // starts in; answering that with silence is indistinguishable from a layer nobody wired up.
      adapter.setBoundingBoxesEnabled(false);
      expect(debug.mock.calls.map((call) => String(call[0]))
        .filter((text) => text.startsWith("Viewport boxes")))
        .toEqual(['Viewport boxes {"enabled":false,"drawn":[],"skipped":[],"perLink":false,"drawnLinks":[]}']);
      debug.mockRestore();
    });

    it("draws one box per object from the measure the rest of the editor reads", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene } = adapterFixture();
      adapter.syncScene(rigWorld());
      addCrates(nodes);

      adapter.setBoundingBoxesEnabled(true);

      expect(adapter.boxReport()).toEqual({ enabled: true, drawn: [2, 3], skipped: [1], perLink: false, drawnLinks: [] });
      expect(scene.children.filter((child) => child.type === "Box3Helper")).toHaveLength(2);
      // The crux of this card: the box on the screen is the box `/get2DAABB` reports. A helper built
      // from `Box3.setFromObject(node)` would pass a "there are two boxes" check and disagree here.
      for (const id of [2, 3]) {
        expect(helpers(adapter).get(id)!.box.equals(adapter.worldAabb(id)!)).toBe(true);
      }
      // Three families on one stage have to be told apart at a glance (§4.8.6).
      const material = helpers(adapter).get(2)!.material as THREE.LineBasicMaterial;
      expect(material.color.getHex()).toBe(BOX_WIRE_COLOR);
      debug.mockRestore();
    });

    it("lifts the wireframe out of a capture without §4.4 having to learn about it", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes } = adapterFixture();
      adapter.syncScene(rigWorld());
      addCrates(nodes);
      adapter.setBoundingBoxesEnabled(true);
      const before = adapter.worldAabb()!;
      const report = adapter.shadowReport();

      // `Box3Helper` ends in "Helper", so the editor-layer rule already covered this layer before
      // the layer existed - the test is that neither predicate nor `hideOverlays()` had to change.
      const taken = hidden(adapter);
      expect(taken).toHaveLength(2);
      for (const helper of helpers(adapter).values()) {
        expect(editorOnly(adapter, helper)).toBe(true);
        expect(helper.visible).toBe(false);
      }
      // Self-exclusion runs the other way too: a box drawn from `worldAabb()` must not inflate the
      // box it was drawn from, or Frame Scene and `/get2DAABB` would move when nobody edited anything.
      expect(adapter.worldAabb()!.equals(before)).toBe(true);
      expect(adapter.shadowReport().castMeshes).toBe(report.castMeshes);
      debug.mockRestore();
    });

    it("follows an object that moved and forgets one that left", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene } = adapterFixture();
      adapter.syncScene(rigWorld());
      addCrates(nodes);
      adapter.setBoundingBoxesEnabled(true);
      const moved = new THREE.Vector3(9, 0, 0);
      nodes.get(3)!.position.add(moved);
      refit(adapter);
      expect(helpers(adapter).get(3)!.box.equals(adapter.worldAabb(3)!)).toBe(true);

      // A world reload that leaves a wireframe floating where a deleted object used to be is the
      // one thing this layer must not do, so `syncScene` reconciles instead of waiting for the clock.
      adapter.syncScene(worldFrom([object(1, "sun", [transform()]), object(2, "box_a", [transform()])]));
      expect(adapter.boxReport().drawn).toEqual([2]);
      expect(helpers(adapter).has(3)).toBe(false);
      expect(scene.children.filter((child) => child.type === "Box3Helper")).toHaveLength(1);
      debug.mockRestore();
    });

    it("keeps the document exactly as it found it", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene } = adapterFixture();
      const world = rigWorld();
      adapter.syncScene(world);
      addCrates(nodes);
      const before = world.toJSON();

      adapter.setBoundingBoxesEnabled(true);
      adapter.setBoundingBoxesEnabled(false);

      expect(world.toJSON()).toEqual(before);
      expect(world.version).toBe(1);
      expect(helpers(adapter).size).toBe(0);
      expect(scene.children.filter((child) => child.type === "Box3Helper")).toHaveLength(0);
      debug.mockRestore();
    });
  });

  /**
   * §4.8.7 (**Q121**): a recording is a *reader* of this canvas, so the two things the layer owes
   * evidence for are its own clock (who gets a frame, and how often) and the policy that keeps the
   * editor layers out of a take. Both are answerable without a GPU and without a browser API; that
   * the bytes decode to a moving picture is §12.22 ③'s job.
   */
  describe("recording takes frames from the render loop (§4.8.7, Q121)", () => {
    /** The gate the loop calls. It is private and separate on purpose: the suite has no rAF clock. */
    const due = (adapter: ThreeRendererAdapter, now: number): (() => void) | null =>
      (adapter as unknown as { dueRecordFrame(now: number): (() => void) | null }).dueRecordFrame(now);
    const outline = (adapter: ThreeRendererAdapter): THREE.Box3Helper | null =>
      (adapter as unknown as { selectionBox: THREE.Box3Helper | null }).selectionBox;
    const wireframes = (adapter: ThreeRendererAdapter): Map<number, THREE.Box3Helper> =>
      (adapter as unknown as { boxHelpers: Map<number, THREE.Box3Helper> }).boxHelpers;

    /** Two crates with geometry and one lamp: the layer being held out has to be present to be held. */
    function overlayStage(adapter: ThreeRendererAdapter, nodes: Map<number, THREE.Object3D>): World {
      const world = worldFrom([
        object(1, "sun", [transform(null, { position: [0, 0, 10] }), { component_type: "Light", properties: { type: "Directional", intensity: 2 } }]),
        object(2, "box_a", [transform(null, { position: [0, 0, 4] })]),
        object(3, "box_b", [transform(null, { position: [5, 0, 4] })]),
      ]);
      adapter.syncScene(world);
      for (const id of [2, 3]) {
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2));
        mesh.userData.renderContent = true;
        nodes.get(id)!.add(mesh);
      }
      return world;
    }

    it("answers what state it is in and delivers nothing until asked", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter } = adapterFixture();
      // No tap means no frames at all - the loop runs on the page's clock, and this is the same
      // gate with the clock handed over by hand.
      expect(due(adapter, 0)).toBeNull();
      expect(due(adapter, 100)).toBeNull();
      expect(due(adapter, 10_000)).toBeNull();
      expect(adapter.recordReport()).toEqual({ recording: false, excludesOverlays: false, frames: 0, intervalMs: 33 });
      // Being asked is what prints the line, exactly as in §4.8.6: silence and "nobody wired this
      // layer" are the same picture to a probe.
      const lines = () => debug.mock.calls.map((call) => String(call[0]))
        .filter((text) => text.startsWith("Viewport record"));
      expect(lines()).toEqual([]);
      adapter.setRecordFrameTap(null, true);
      expect(lines()).toEqual(['Viewport record {"recording":false,"excludesOverlays":true,"frames":0,"intervalMs":33}']);
      // Twice asked, once said: the dedupe key is the state, and a running take's frame count is
      // deliberately not part of it.
      adapter.setRecordFrameTap(null, true);
      expect(lines()).toHaveLength(1);
      debug.mockRestore();
    });

    it("hands over one frame per interval and none in between", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter } = adapterFixture();
      let delivered = 0;
      const tap = () => { delivered += 1; };
      adapter.setRecordFrameTap(tap, false);

      // §12.22 ①'s fed timestamps. The click's next draw is due at once - a take that opens 33 ms
      // late is a take missing the moment the operator clicked - and after that the gate is it.
      for (const now of [0, 10, 20, 34, 45]) {
        const frame = due(adapter, now);
        if (frame) {
          expect(frame).toBe(tap);
          frame();
        }
      }
      expect(delivered).toBe(2);
      expect(adapter.recordReport()).toEqual({ recording: true, excludesOverlays: false, frames: 2, intervalMs: 33 });

      // The counter belongs to the take, so a second one starts from zero and the first one's
      // number is what `finishClip` reads when it has to explain a 0-byte file.
      adapter.setRecordFrameTap(null, false);
      adapter.setRecordFrameTap(tap, false);
      expect(adapter.recordReport().frames).toBe(0);
      debug.mockRestore();
    });

    it("leaves every editor layer on the screen while a take runs", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      const { adapter, nodes, scene } = adapterFixture();
      const world = overlayStage(adapter, nodes);
      adapter.setBoundingBoxesEnabled(true);
      adapter.select(2);
      const before = world.toJSON();
      const measure = adapter.worldAabb()!;
      expect(wireframes(adapter).size).toBe(2);
      expect(outline(adapter)).not.toBeNull();

      let delivered = 0;
      adapter.setRecordFrameTap(() => { delivered += 1; }, true);

      // A take is its own pass, so the screen loses nothing to it: the 「录制辅助图层」 box decides
      // what goes into the file, never what the operator is looking at. Pressing the layers down for
      // the whole take would have made the viewport flicker every time a take started.
      for (const helper of wireframes(adapter).values()) expect(helper.visible).toBe(true);
      expect(outline(adapter)!.visible).toBe(true);
      const frame = due(adapter, 1_000);
      expect(frame).not.toBeNull();
      frame!();
      expect(delivered).toBe(1);
      for (const helper of wireframes(adapter).values()) expect(helper.visible).toBe(true);
      expect(outline(adapter)!.visible).toBe(true);

      adapter.setRecordFrameTap(null, true);
      expect(wireframes(adapter).size).toBe(2);

      // And the negative control: a take reads the canvas and writes nothing. §4.4's path did not
      // learn about this layer, so neither the document nor the measure moves.
      expect(world.toJSON()).toEqual(before);
      expect(world.version).toBe(1);
      expect(adapter.worldAabb()!.equals(measure)).toBe(true);
      expect(scene.children.filter((child) => child.type === "Box3Helper")).toHaveLength(3);
      debug.mockRestore();
    });

    it("drops the tap when the page goes away", () => {
      const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
      vi.stubGlobal("window", { addEventListener: vi.fn(), removeEventListener: vi.fn() });
      vi.stubGlobal("cancelAnimationFrame", vi.fn());
      const { adapter, nodes } = adapterFixture();
      overlayStage(adapter, nodes);
      let delivered = 0;
      adapter.setRecordFrameTap(() => { delivered += 1; }, true);

      adapter.dispose();

      // A disposed context must not be handed one more frame, and the report must not say otherwise.
      expect(adapter.recordReport().recording).toBe(false);
      expect(due(adapter, 1_000)).toBeNull();
      expect(due(adapter, 100_000)).toBeNull();
      expect(delivered).toBe(0);
      debug.mockRestore();
    });
  });

  it("mounts MetadataRef geometry and keeps forceOverrideColor per instance (§6.3, §6.8)", async () => {
    const asciiStl = `solid a\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n  endloop\n endfacet\nendsolid a\n`;
    const bytes = new TextEncoder().encode(asciiStl).buffer as ArrayBuffer;
    const client = Object.assign(Object.create(LibraryClient.prototype), {
      model: async (modelId: number) => ({ modelId, path: "arm.stl", isOnCos: false, reference: [], raw: {} }),
      bundle: async () => ({ entry: "arm.stl", files: new Map([["arm.stl", bytes]]), failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?id=${id}&path=${path}`,
    });
    const { adapter, nodes } = adapterFixture();
    adapter.setLibrary(client);
    const metadata = { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/5" } } as WorldComponent;
    const world = worldFrom([
      object(1, "tinted", [transform(), metadata, { component_type: "MeshRenderer", properties: { forceOverrideColor: "#FF8000" } }]),
      object(2, "plain", [transform(), metadata]),
    ]);

    adapter.syncScene(world);
    await new Promise((resolve) => setTimeout(resolve, 0));

    const tinted = meshesOf(nodes.get(1)!);
    const plain = meshesOf(nodes.get(2)!);
    expect(tinted).toHaveLength(1);
    expect(plain).toHaveLength(1);
    expect((tinted[0]!.material as THREE.MeshStandardMaterial).color.getHexString()).toBe("ff8000");
    expect((tinted[0]!.material as THREE.MeshStandardMaterial).map).toBe(null);
    // The per-model template is shared, so an override must not reach the other instance (坑 13).
    expect((plain[0]!.material as THREE.MeshStandardMaterial).color.getHexString()).toBe("ffffff");
    expect(tinted[0]!.geometry).not.toBe(plain[0]!.geometry);
  });

  /** One MTL with a `map_Kd`, loaded as an OBJ that either carries `vt` or does not. */
  async function objFixture(withUv: boolean): Promise<THREE.Mesh> {
    const mtl = "newmtl surface\nKd 0.486 0.675 0.8\nmap_Kd arm.png\n";
    const png = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
    const vertices = "v -0.5 -0.5 0\nv 0.5 -0.5 0\nv -0.5 0.5 0\n";
    const uvLines = withUv ? "vt 0 0\nvt 1 0\nvt 0 1\n" : "";
    const faces = withUv ? "f 1/1/1 2/2/1 3/3/1\n" : "f 1//1 2//1 3//1\n";
    const obj = `mtllib arm.mtl\n${vertices}${uvLines}vn 0 0 1\nusemtl surface\n${faces}`;
    const encode = (text: string): ArrayBuffer => new TextEncoder().encode(text).buffer as ArrayBuffer;
    const files = new Map<string, ArrayBuffer>([
      ["arm.obj", encode(obj)],
      ["arm.mtl", encode(mtl)],
      ["arm.png", png.buffer as ArrayBuffer],
    ]);
    const client = Object.assign(Object.create(LibraryClient.prototype), {
      model: async (modelId: number) => ({ modelId, kind: "model", path: "arm.obj", isOnCos: false, reference: ["arm.mtl", "arm.png"], raw: {} }),
      bundle: async () => ({ entry: "arm.obj", files, failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?id=${id}&path=${path}`,
    });
    const { adapter, nodes } = adapterFixture();
    adapter.setLibrary(client);
    adapter.syncScene(worldFrom([object(1, "arm", [transform(), { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/5" } }])]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    return meshesOf(nodes.get(1)!)[0]!;
  }

  it("renders an OBJ without UV from its MTL diffuse instead of a texture it cannot sample (§6.5)", async () => {
    const mesh = await objFixture(false);
    expect(mesh.geometry.hasAttribute("uv")).toBe(false);
    // `robots/1` writes a solid 4×4 per link whose colour equals Kd - `forearm_0.png` is
    // rgb(124,172,204) - so sampling it would square the link's own colour.
    expect((mesh.material as THREE.MeshStandardMaterial).map).toBe(null);
    expect((mesh.material as THREE.MeshStandardMaterial).color.getHexString()).toBe("7caccc");
  });

  it("keeps the MTL texture for an OBJ that does carry UV", async () => {
    const mesh = await objFixture(true);
    expect(mesh.geometry.hasAttribute("uv")).toBe(true);
    expect((mesh.material as THREE.MeshStandardMaterial).map).not.toBe(null);
  });

  it("reads #RRGGBBAA as a colour plus an opacity and refuses anything else (§4.3)", async () => {
    const asciiStl = `solid a\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n  endloop\n endfacet\nendsolid a\n`;
    const bytes = new TextEncoder().encode(asciiStl).buffer as ArrayBuffer;
    const client = Object.assign(Object.create(LibraryClient.prototype), {
      model: async (modelId: number) => ({ modelId, kind: "model", path: "arm.stl", isOnCos: false, reference: [], raw: {} }),
      bundle: async () => ({ entry: "arm.stl", files: new Map([["arm.stl", bytes]]), failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?id=${id}&path=${path}`,
    });
    const errors: string[] = [];
    const { adapter, nodes } = adapterFixture(errors);
    adapter.setLibrary(client);
    const metadata = { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/5" } } as WorldComponent;
    const tint = (color: string): WorldComponent => ({ component_type: "MeshRenderer", properties: { forceOverrideColor: color } });

    adapter.syncScene(worldFrom([
      object(1, "glass", [transform(), metadata, tint("#FF800080")]),
      object(2, "solid", [transform(), metadata, tint("#FF8000")]),
      object(3, "typo", [transform(), metadata, tint("ff8000")]),
    ]));
    await new Promise((resolve) => setTimeout(resolve, 0));

    const glass = meshesOf(nodes.get(1)!)[0]!.material as THREE.MeshStandardMaterial;
    expect(glass.color.getHexString()).toBe("ff8000");
    expect(glass.opacity).toBeCloseTo(0x80 / 255, 3);
    expect(glass.transparent).toBe(true);
    // Six digits stay fully opaque, so a scene that only ever wrote #RRGGBB is unchanged.
    const solid = meshesOf(nodes.get(2)!)[0]!.material as THREE.MeshStandardMaterial;
    expect(solid.opacity).toBe(1);
    expect(solid.transparent).toBe(false);
    // A string the file cannot mean is reported and leaves the model's own material alone.
    expect((meshesOf(nodes.get(3)!)[0]!.material as THREE.MeshStandardMaterial).color.getHexString()).toBe("ffffff");
    expect(errors).toEqual(['forceOverrideColor "ff8000" is not a #RRGGBB or #RRGGBBAA colour']);
  });

  it("asks OLS in the id space the MetadataRef type names (§4.2, §6.11)", async () => {
    const asciiStl = `solid a\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n  endloop\n endfacet\nendsolid a\n`;
    const bytes = new TextEncoder().encode(asciiStl).buffer as ArrayBuffer;
    const called: string[] = [];
    const client = Object.assign(Object.create(LibraryClient.prototype), {
      model: async (modelId: number) => {
        called.push(`model:${modelId}`);
        return { modelId, kind: "model", path: "arm.stl", isOnCos: false, reference: [], raw: {} };
      },
      robot: async (modelId: number) => {
        called.push(`robot:${modelId}`);
        return { modelId, kind: "robot", path: "arm.stl", isOnCos: false, reference: [], raw: {} };
      },
      bundle: async (metadata: { path: string }) => ({ entry: metadata.path, files: new Map([[metadata.path, bytes]]), failures: [] }),
      fileUrl: (id: number, path: string, _cos: boolean, kind: LibraryFileKind = "model") =>
        `http://ols.test/file?type=${kind}&id=${id}&path=${path}`,
    });
    const { adapter, nodes } = adapterFixture();
    adapter.setLibrary(client);
    const ref = (id: number, type?: string): WorldComponent =>
      ({ component_type: "MetadataRef", properties: { path: `$LIBRARY_SERVER/${id}`, ...(type ? { type } : {}) } } as WorldComponent);

    adapter.syncScene(worldFrom([
      // The real scene's wrist camera is robot 3: reading it as model 3 would show another asset.
      object(1, "wrist camera", [transform(), ref(3, "robot")]),
      object(2, "table", [transform(), ref(29, "model")]),
      object(3, "untyped", [transform(), ref(7)]),
    ]));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(called).toEqual(["robot:3", "model:29", "model:7"]);
    expect(meshesOf(nodes.get(1)!)).toHaveLength(1);
  });

  /** Two revolute joints: enough to show which pose the stage parks each joint at. */
  const armRobot = (): UrdfRobot => ({
    name: "arm",
    links: [{ name: "base", visuals: [] }, { name: "link1", visuals: [] }, { name: "link2", visuals: [] }],
    joints: [{ name: "joint1", type: "revolute", parent: "base", child: "link1",
      origin: { translation: [0, 0, 0.3], quaternion: [0, 0, 0, 1] }, axis: [0, 0, 1], limit: [-3.14, 3.14], mimic: null },
      { name: "joint2", type: "revolute", parent: "link1", child: "link2",
        origin: { translation: [0, 0, 0.6], quaternion: [0, 0, 0, 1] }, axis: [0, 0, 1], limit: [-3.14, 3.14], mimic: null }],
    roots: ["base"],
    meshFiles: [],
  });

  /** A robot bundle whose entry is URDF, so `buildRobot` runs. */
  const urdfRobotClient = (): LibraryClient => {
    const bytes = new TextEncoder().encode("<robot name='arm'/>").buffer as ArrayBuffer;
    return Object.assign(Object.create(LibraryClient.prototype), {
      robot: async (modelId: number) => ({ modelId, kind: "robot", path: "arm.urdf", isOnCos: false, reference: [], raw: {} }),
      bundle: async () => ({ entry: "arm.urdf", files: new Map([["arm.urdf", bytes]]), failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?type=robot&id=${id}&path=${path}`,
    }) as LibraryClient;
  };

  const armObject = (pose: Record<string, unknown>): WorldComponent[] => [
    transform(),
    { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/1", type: "robot" } } as WorldComponent,
    { component_type: "RobotDriver", properties: { joint_sequence: ["joint1", "joint2"], ...pose } } as WorldComponent,
  ];

  it("parks a URDF instance at the joint pose the world file declares (§4.7)", async () => {
    const errors: string[] = [];
    const { adapter, robotTrees, robots } = adapterFixture(errors);
    adapter.setLibrary(urdfRobotClient());
    // `parseUrdf` needs a DOM and `urdf.test.ts` already proves it; the robot it would
    // return is installed at the cache key the renderer reads.
    robots.set("robot:1", Promise.resolve(armRobot()));

    const angleOf = (): number => robotTrees.get(1)!.tree.joints.get("joint1")!.node.quaternion.angleTo(new THREE.Quaternion());

    adapter.syncScene(worldFrom([object(1, "arm", armObject({ initial_joint_positions_rad: { joint1: Math.PI / 2 } }))]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(angleOf()).toBeCloseTo(Math.PI / 2, 4);

    // Editing the parked pose is a change of appearance, so it re-runs the load instead of
    // leaving the old angle on the stage.
    adapter.syncScene(worldFrom([object(1, "arm", armObject({ initial_joint_positions_rad: { joint1: 0 } }))]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(angleOf()).toBe(0);
    expect(errors).toEqual([]);
  });

  it("parks each joint from the dictionary that names it, and rebuilds for either (Q75)", async () => {
    const errors: string[] = [];
    const { adapter, robotTrees, robots } = adapterFixture(errors);
    adapter.setLibrary(urdfRobotClient());
    robots.set("robot:1", Promise.resolve(armRobot()));

    const angleOf = (name: string): number => robotTrees.get(1)!.tree.joints.get(name)!.node.quaternion.angleTo(new THREE.Quaternion());
    const load = async (pose: Record<string, unknown>): Promise<void> => {
      adapter.syncScene(worldFrom([object(1, "arm", armObject(pose))]));
      await new Promise((resolve) => setTimeout(resolve, 0));
    };

    // `now` only owns joint1; joint2 still comes from `initial`.
    await load({ initial_joint_positions_rad: { joint1: Math.PI / 2, joint2: 0.1 }, now_joint_positions_rad: { joint1: 0 } });
    expect(angleOf("joint1")).toBe(0);
    expect(angleOf("joint2")).toBeCloseTo(0.1, 4);

    const initialTree = robotTrees.get(1)!.tree;
    await load({ initial_joint_positions_rad: { joint1: Math.PI / 2, joint2: 0.1 }, now_joint_positions_rad: { joint1: Math.PI / 4 } });
    expect(robotTrees.get(1)!.tree).not.toBe(initialTree);
    expect(angleOf("joint1")).toBeCloseTo(Math.PI / 4, 4);
    expect(angleOf("joint2")).toBeCloseTo(0.1, 4);

    // A joint `now` does not name is still read from `initial`, so editing it is a real
    // change of appearance and has to rebuild too.
    const heldTree = robotTrees.get(1)!.tree;
    await load({ initial_joint_positions_rad: { joint1: Math.PI / 2, joint2: 0.2 }, now_joint_positions_rad: { joint1: Math.PI / 4 } });
    expect(robotTrees.get(1)!.tree).not.toBe(heldTree);
    expect(angleOf("joint1")).toBeCloseTo(Math.PI / 4, 4);
    expect(angleOf("joint2")).toBeCloseTo(0.2, 4);
    expect(errors).toEqual([]);
  });

  it("puts the arms back at the declared pose when the stage leaves playback (Q76)", async () => {
    const errors: string[] = [];
    const { adapter, robotTrees, robots } = adapterFixture(errors);
    adapter.setLibrary(urdfRobotClient());
    robots.set("robot:1", Promise.resolve(armRobot()));
    adapter.syncScene(worldFrom([object(1, "arm", armObject({ initial_joint_positions_rad: { joint1: Math.PI / 2 }, now_joint_positions_rad: { joint2: 0.3 } }))]));
    await new Promise((resolve) => setTimeout(resolve, 0));

    const joint1 = robotTrees.get(1)!.tree.joints.get("joint1")!;
    const joint2 = robotTrees.get(1)!.tree.joints.get("joint2")!;
    setJointValue(joint1, 0);
    setJointValue(joint2, 0);
    expect(joint1.node.quaternion.angleTo(new THREE.Quaternion())).toBe(0);

    // A clip drives joints and nothing else does, so ending playback has to re-park them.
    adapter.reparkRobots();
    expect(joint1.node.quaternion.angleTo(new THREE.Quaternion())).toBeCloseTo(Math.PI / 2, 4);
    expect(joint2.node.quaternion.angleTo(new THREE.Quaternion())).toBeCloseTo(0.3, 4);
    expect(errors).toEqual([]);
  });

  it("aims Directional and Spot lights along the local -Z", () => {
    const { adapter, nodes } = adapterFixture();

    adapter.syncScene(worldFrom([
      object(1, "sun", [transform(), { component_type: "Light", properties: { type: "Directional" } }]),
      object(2, "spot", [transform(), { component_type: "Light", properties: { type: "Spot" } }]),
    ]));

    for (const id of [1, 2]) {
      const light = [...nodes.get(id)!.children].find(
        (child) => child instanceof THREE.DirectionalLight || child instanceof THREE.SpotLight,
      )! as THREE.SpotLight;
      expect(light.target.position.z).toBeCloseTo(-1);
      expect(nodes.get(id)!.children).toContain(light.target);
    }
  });

  it("hands a trajectory driver nodes and joint handles, and drops them with the object (§8.4)", () => {
    const { adapter, nodes, robotTrees } = adapterFixture();
    const world = worldFrom([object(1, "arm", [transform()]), object(2, "apple", [transform(1)])]);
    adapter.syncScene(world);

    const armRoot = nodes.get(1)!;
    const carriage = new THREE.Object3D();
    const joint1 = new THREE.Object3D();
    carriage.name = "carriage_link";
    joint1.name = "shoulder_pan_joint";
    armRoot.add(joint1);
    joint1.add(carriage);
    const tree: UrdfTree = {
      root: armRoot,
      links: new Map([["carriage", carriage]]),
      joints: new Map<string, UrdfJointHandle>([["shoulder_pan_joint", {
        name: "shoulder_pan_joint",
        type: "revolute",
        axis: [0, 0, 1],
        limit: null,
        origin: { translation: [0, 0, 0.2], quaternion: [0, 0, 0, 1] },
        childLink: "carriage",
        mimic: null,
        node: joint1,
      }]]),
    };
    const robot: UrdfRobot = { name: "arm", links: [], joints: [], roots: ["carriage"], meshFiles: [] };
    robotTrees.set(1, { robot, tree });

    const view = adapter.trajView;
    expect(view.object(1)).toBe(armRoot);
    expect(view.joints(1)?.get("shoulder_pan_joint")?.node).toBe(joint1);
    expect(view.robot(1)).toEqual({ robot, tree });
    // An ordinary object is a node with no joints; an unknown id has neither.
    expect(view.joints(2)).toBeNull();
    expect(view.robot(2)).toBeNull();
    expect(view.object(404)).toBeNull();
    // §7.2: the driver asks the view where the world file hangs an object.
    expect(view.worldParent(2)).toBe(armRoot);
    expect(view.worldParent(404)).toBeNull();

    // §8.4: the handle the view hands out wraps the live node, and `refresh` is
    // what makes a written joint visible to the links below it.
    setJointValue(view.joints(1)!.get("shoulder_pan_joint")!, Math.PI / 2);
    expect(joint1.position.z).toBeCloseTo(0.2, 5);
    expect(joint1.quaternion.z).toBeCloseTo(Math.SQRT1_2, 5);

    armRoot.position.set(1, 0, 0);
    expect(carriage.matrixWorld.elements[12]).toBeCloseTo(0, 5);
    view.refresh();
    expect(carriage.matrixWorld.elements[12]).toBeCloseTo(1, 5);

    // A removed object takes its URDF registration with it, so a stale joint
    // sequence cannot keep driving a robot that is no longer on the stage.
    adapter.syncScene(worldFrom([object(2, "apple", [transform()])]));
    expect(view.object(1)).toBeNull();
    expect(view.joints(1)).toBeNull();
    expect(view.robot(1)).toBeNull();
    expect(robotTrees.size).toBe(0);
  });
});

describe("picking nested world objects", () => {
  it("resolves the camera node from ancestry even when a child mesh carries the arm id", () => {
    const { adapter, nodes, simWorld } = adapterFixture();
    const arm = new THREE.Group();
    const camera = new THREE.Group();
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial());
    arm.userData.objectId = 2;
    camera.userData.objectId = 4;
    mesh.userData.objectId = 2;
    nodes.set(2, arm);
    nodes.set(4, camera);
    simWorld.add(arm);
    arm.add(camera);
    camera.add(mesh);

    expect((adapter as unknown as { objectIdForHit(object: THREE.Object3D | null): number | null })
      .objectIdForHit(mesh)).toBe(4);
  });
});

/** A camera and two lights carrying nothing but their markers, for §4.4's visibility rules. */
function markerWorld(far = 40): World {
  const light = (type: string): WorldComponent =>
    ({ component_type: "Light", properties: { type, intensity: 1, color: "#FFFFFF" } });
  return worldFrom([
    object(0, "Ambient", [transform(), light("Ambient")]),
    object(1, "Sun", [transform(null, { position: [0, 0, 3] }), light("Directional")]),
    object(2, "Cam", [transform(null, { position: [0, 0, -3] }),
      { component_type: "Camera", properties: { type: "rgbd", fov: 90, near: 0.1, far, resolution: [64, 32] } }]),
  ]);
}

/** The one child of a node that carries that marker flag; these tests read `visible` off it. */
function markerOf(nodes: Map<number, THREE.Object3D>, id: number, flag: string): THREE.Object3D {
  const found = [...nodes.get(id)!.children].find((child) => child.userData[flag]);
  if (!found) throw new Error(`object ${id} has no userData.${flag} child`);
  return found;
}

/** The selection outline, which §4.5 (Q105) says is a `Box3Helper` hung under the scene. */
function outline(scene: THREE.Scene): THREE.Box3Helper | null {
  const found = scene.children.find((child) => child.type === "Box3Helper");
  return (found as THREE.Box3Helper | undefined) ?? null;
}

describe("marker visibility and the selection outline (§4.4, §4.5, Q105, Q106)", () => {
  it("shows each marker only for the object the user selected, with both flags off", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(markerWorld());
    const frustum = markerOf(nodes, 2, "cameraMarker");
    const ambient = markerOf(nodes, 0, "lightMarker");
    const sun = markerOf(nodes, 1, "lightMarker");
    const label = markerOf(nodes, 0, "lightLabel");
    // A fresh world has no selection, and both keys default to false: the stage is clean.
    expect([frustum.visible, ambient.visible, sun.visible, label.visible]).toEqual([false, false, false, false]);

    adapter.select(2);
    expect(frustum.visible).toBe(true);
    expect([ambient.visible, sun.visible]).toEqual([false, false]);

    adapter.select(0);
    expect(frustum.visible).toBe(false);
    expect([ambient.visible, label.visible]).toEqual([true, true]);
    expect(sun.visible).toBe(false);

    adapter.select(null);
    expect([frustum.visible, ambient.visible, label.visible]).toEqual([false, false, false]);

    // The one surface §12.14 can read from the browser is the state list behind the
    // "Viewport markers" line, so it has to name every marker, not just the lit ones.
    expect(adapter.markerStates()).toEqual([
      { objectId: 0, kind: "lightLabel", visible: false },
      { objectId: 0, kind: "lightSphere", visible: false },
      { objectId: 1, kind: "lightLabel", visible: false },
      { objectId: 1, kind: "lightSphere", visible: false },
      { objectId: 2, kind: "cameraFrustum", visible: false },
    ]);
  });

  it("pins each family with its own flag and leaves the other family to the selection", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(markerWorld());
    const frustum = markerOf(nodes, 2, "cameraMarker");
    const ambient = markerOf(nodes, 0, "lightMarker");

    adapter.setCameraFrustumAlways(true);
    expect(frustum.visible).toBe(true);
    expect(ambient.visible).toBe(false);

    adapter.select(0);
    expect([frustum.visible, ambient.visible]).toEqual([true, true]);

    adapter.setCameraFrustumAlways(false);
    adapter.setLightMarkerAlways(true);
    adapter.select(null);
    expect(frustum.visible).toBe(false);
    expect(ambient.visible).toBe(true);
    expect(markerOf(nodes, 1, "lightMarker").visible).toBe(true);
  });

  it("rebuilds a marker straight into the state the flags and the selection ask for", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(markerWorld());
    adapter.select(2);
    expect(markerOf(nodes, 2, "cameraMarker").visible).toBe(true);

    // A changed component list repopulates that object's children from scratch (§4.4) and
    // the selection survives the sync, so the new frustum has to arrive already visible.
    adapter.syncScene(markerWorld(60));
    expect(markerOf(nodes, 2, "cameraMarker").visible).toBe(true);
    expect(markerOf(nodes, 0, "lightMarker").visible).toBe(false);
  });

  it("outlines a camera with its own mesh, not with the frustum that reaches its far plane", () => {
    const { adapter, nodes, scene } = adapterFixture();
    adapter.syncScene(markerWorld());
    nodes.get(2)!.add(new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.1, 0.1)));
    adapter.select(2);

    const box = adapter.worldAabb(2)!;
    const helper = outline(scene);
    expect(helper).not.toBeNull();
    expect(helper!.box.min.toArray()).toEqual(box.min.toArray());
    expect(helper!.box.max.toArray()).toEqual(box.max.toArray());
    // `far` is 40 m and the frustum lines reach it; the outline is the mesh's decimetres.
    const size = box.getSize(new THREE.Vector3());
    expect(Math.max(size.x, size.y, size.z)).toBeCloseTo(0.2);
  });

  it("draws no outline for an object whose render content is only editor layers", () => {
    const { adapter, nodes, scene } = adapterFixture();
    adapter.syncScene(markerWorld());
    adapter.select(2);
    expect(adapter.worldAabb(2)).toBeNull();
    expect(outline(scene)).toBeNull();
    // The frustum itself is what says "this camera is the selected one" (§4.5's corollary).
    expect(markerOf(nodes, 2, "cameraMarker").visible).toBe(true);
  });

  it("re-measures the outline it already has, so a moved object is still framed", () => {
    const { adapter, nodes, scene } = adapterFixture();
    adapter.syncScene(markerWorld());
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.1, 0.1));
    nodes.get(2)!.add(body);
    adapter.select(2);
    const helper = outline(scene)!;
    const before = helper.box.max.toArray();

    body.position.set(1, 0, 0);
    body.updateMatrixWorld(true);
    (adapter as unknown as { syncSelectionBox(): void }).syncSelectionBox();
    expect(outline(scene)).toBe(helper);
    expect(helper.box.max.toArray()).not.toEqual(before);
    expect(helper.box.max.toArray()).toEqual(adapter.worldAabb(2)!.max.toArray());
  });

  it("keeps a marker nobody can see out of the click target list", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(markerWorld());
    // Three's raycaster filters on `layers` alone, so `pick()` has to drop invisible hits
    // itself -- otherwise clicking empty space would select a light nobody can see.
    const selectable = (node: THREE.Object3D): boolean =>
      (adapter as unknown as { selectable(object: THREE.Object3D): boolean }).selectable(node);
    const sphere = markerOf(nodes, 0, "lightMarker");
    const frustum = markerOf(nodes, 2, "cameraMarker");
    expect([selectable(sphere), selectable(frustum)]).toEqual([false, false]);

    adapter.select(0);
    expect(selectable(sphere)).toBe(true);
    // A frustum that is on screen still is not a target: a line drawing, not a body (Q71).
    adapter.setCameraFrustumAlways(true);
    expect(frustum.visible).toBe(true);
    expect(selectable(frustum)).toBe(false);

    const body = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
    nodes.get(1)!.add(body);
    expect(selectable(body)).toBe(true);
    body.visible = false;
    expect(selectable(body)).toBe(false);
  });
});

/**
 * §4.7 (**Q140**): a click selects a body, never a wireframe. `OBJLoader` cannot keep faces and
 * `l` statements in one geometry, so a single stray line statement turns a whole asset into one
 * `LineSegments` -- and three tests a `Line` against `Raycaster.params.Line.threshold`, whose
 * default `1` is one *metre* of slack around every segment. OLS model 22 (`doll`) ships exactly
 * one such line, and in the exported scene it answered 26 of 63 clicks over the table, including
 * every click that was aimed at a neighbour.
 */
describe("a wireframe asset does not steal its neighbours' clicks (§4.7, Q140)", () => {
  const selectableOf = (adapter: ThreeRendererAdapter) => (node: THREE.Object3D): boolean =>
    (adapter as unknown as { selectable(object: THREE.Object3D): boolean }).selectable(node);
  const objFile = (statements: string) =>
    `o Dino_DinoMesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nvn 0 0 1\nvn 0 1 0\n${statements}`;
  const faces = "f 1//1 2//1 3//1\nf 1//1 3//1 4//1\n";

  it("ships the faces as a Mesh when the file carries no line statement", () => {
    const parsed = new OBJLoader().parse(objFile(faces));
    const bodies = meshesOf(parsed);
    expect(bodies).toHaveLength(1);
    expect(linesOf(parsed)).toHaveLength(0);
    const { adapter } = adapterFixture();
    expect(selectableOf(adapter)(bodies[0]!)).toBe(true);
  });

  it("turns the same file into one LineSegments as soon as a line statement joins it", () => {
    const solid = new OBJLoader().parse(objFile(faces));
    const wire = new OBJLoader().parse(objFile(`${faces}l 1 2\n`));
    expect(meshesOf(solid)).toHaveLength(1);
    expect(linesOf(solid)).toHaveLength(0);
    expect(meshesOf(wire)).toHaveLength(0);
    const segments = linesOf(wire);
    expect(segments).toHaveLength(1);
    // The faces did not vanish, they were reinterpreted as segment pairs -- which is exactly how a
    // 17 cm doll ends up with 3400 raycast candidates and a metre of slack around each one.
    const solidCount = meshesOf(solid)[0]!.geometry.getAttribute("position").count;
    const wireCount = segments[0]!.geometry.getAttribute("position").count;
    expect(wireCount).toBeGreaterThan(solidCount);
  });

  it("refuses the wireframe, the points and keeps every surface and label a target", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(sampleWorld());
    const selectable = selectableOf(adapter);
    const wire = linesOf(new OBJLoader().parse(objFile(`${faces}l 1 2\n`)))[0]!;
    const stage = nodes.get([...nodes.keys()].sort()[0]!)!;
    stage.add(wire);
    expect(selectable(wire)).toBe(false);
    expect(selectable(new THREE.Points(new THREE.BufferGeometry()))).toBe(false);
    expect(selectable(new THREE.LineSegments(wire.geometry, wire.material as THREE.Material))).toBe(false);

    // What the rule must not take away: a body, and the light label that answers for its own object.
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1));
    stage.add(body);
    expect(selectable(body)).toBe(true);
    const label = new THREE.Sprite();
    stage.add(label);
    expect(selectable(label)).toBe(true);
  });
});

/**
 * §6.5 (**Q143**): `robotiq_2f85_meshes/pad.obj` declares 900 of its 1052 vertices -- and 900 of
 * its 1200 normals -- *after* the 1200 faces that index them, and `OBJLoader` reads a file in
 * order, so those faces resolve to `undefined`. The position buffer that would land on the stage is
 * then three-quarters NaN, and `computeBoundingBox`/`computeBoundingSphere` cache that the first
 * time anything asks, so the NaN outlives the parse: `worldAabb` unions it, the object loses its
 * §4.5 outline and its §4.8.6 wireframe draws nothing, and `frame()` writes it into the camera --
 * after which no ray on the stage hits *anything*, which is the "sometimes" the user saw. The arm
 * inherits it without owning a single NaN vertex: `worldParentOf` mounts the gripper under the arm's
 * link node, and `worldAabb` of the arm walks that subtree. Six of the 210 OBJs in the shipped
 * library are written that way, all of them robotiq gripper parts.
 *
 * The renderer never hands such a file to the loader: `normalizeObjSource` reads it as a whole first
 * (§6.5, Q143), so what lands on the stage is the *complete* mesh -- every face, drawing and
 * clickable -- and there is nothing left to report. The last test here is the other half of the rule:
 * a NaN part that reaches the stage by some other path still must not take its parent's box, or the
 * camera, with it.
 */
describe("a mesh whose faces index vertices the file has not read yet (§6.5, §4.5, §4.8.6, Q143)", () => {
  /** One vertex after the face that needs it: the mistake `pad.obj` makes 900 times, in miniature. */
  const padObj = "o Pad\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvn 0 0 1\nf 1//1 2//1 3//1\nf 1//1 3//1 4//1\nv 0 0 1\n";
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  const helpers = (adapter: ThreeRendererAdapter): Map<number, THREE.Box3Helper> =>
    (adapter as unknown as { boxHelpers: Map<number, THREE.Box3Helper> }).boxHelpers;
  const templatesOf = (adapter: ThreeRendererAdapter): Map<string, Promise<THREE.Object3D>> =>
    (adapter as unknown as { templates: Map<string, Promise<THREE.Object3D>> }).templates;
  const isFiniteBox = (box: THREE.Box3): boolean =>
    [box.min.x, box.min.y, box.min.z, box.max.x, box.max.y, box.max.z].every((value) => Number.isFinite(value));
  /** Straight down onto the face that lies in `z = 0`. */
  const rayOnto = (x: number, y: number): THREE.Raycaster =>
    new THREE.Raycaster(new THREE.Vector3(x, y, 5), new THREE.Vector3(0, 0, -1));
  /** Along `-x` onto the face the late vertex belongs to, which lies in `x = 0`. */
  const rayAcross = (y: number, z: number): THREE.Raycaster =>
    new THREE.Raycaster(new THREE.Vector3(5, y, z), new THREE.Vector3(-1, 0, 0));

  function padClient(): LibraryClient {
    const bytes = new TextEncoder().encode(padObj).buffer as ArrayBuffer;
    return Object.assign(Object.create(LibraryClient.prototype), {
      model: async (modelId: number) => ({ modelId, kind: "model", path: "pad.obj", isOnCos: false, reference: [], raw: {} }),
      bundle: async () => ({ entry: "pad.obj", files: new Map([["pad.obj", bytes]]), failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?id=${id}&path=${path}`,
    }) as LibraryClient;
  }

  /** The pad on a stage of its own, mounted the way the gripper mounts it. */
  function padWorld(): World {
    return worldFrom([
      object(1, "pad", [transform(), { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/5" } } as WorldComponent]),
    ]);
  }

  it("loses the face that indexed a vertex the file had not read yet, when three reads it alone", () => {
    // What three produces from that file on its own: the damage is in the asset, not in the
    // renderer, and `computeBoundingBox` says so out loud on every geometry that has it. This is the
    // parse the renderer must not perform, and the reason `normalizeObjSource` exists.
    const noisy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const mesh = meshesOf(new OBJLoader().parse(padObj))[0]!;
    mesh.geometry.computeBoundingBox();
    expect(Number.isFinite(mesh.geometry.boundingBox!.max.x)).toBe(false);
    mesh.geometry.computeBoundingSphere();
    expect(Number.isFinite(mesh.geometry.boundingSphere!.radius)).toBe(false);
    // The face that was complete is a real surface; the one that indexed the late vertex is gone.
    expect(rayOnto(0.25, 0.25).intersectObject(mesh)).toHaveLength(1);
    expect(rayAcross(0.25, 0.25).intersectObject(mesh)).toHaveLength(0);
    noisy.mockRestore();
  });

  it("puts the whole mesh on the stage, and says nothing about it", async () => {
    const { adapter, nodes, errors } = adapterFixture();
    adapter.setLibrary(padClient());
    adapter.syncScene(padWorld());
    await settle();

    const mesh = meshesOf(await templatesOf(adapter).get("model:5/pad.obj")!)[0]!;
    const position = mesh.geometry.attributes.position!;
    // Two triangles, and not one of their eighteen components is a NaN: the face that used to index
    // vertex 4 has its vertex now.
    expect(position.count).toBe(6);
    expect(Array.from(position.array).every(Number.isFinite)).toBe(true);
    mesh.geometry.computeBoundingBox();
    expect(mesh.geometry.boundingBox!.min.toArray()).toEqual([0, 0, 0]);
    expect(mesh.geometry.boundingBox!.max.toArray()).toEqual([1, 1, 1]);
    // Both faces are surfaces, so both draw and both answer a click.
    expect(rayOnto(0.25, 0.25).intersectObject(mesh)).toHaveLength(1);
    expect(rayAcross(0.25, 0.25).intersectObject(mesh)).toHaveLength(1);
    // The instance on the stage inherits the extents: `cloneAsset` copies them with the geometry.
    expect(isFiniteBox(adapter.worldAabb(1)!)).toBe(true);
    expect(meshesOf(nodes.get(1)!)[0]!.geometry.attributes.position!.count).toBe(6);
    // Nothing to report: the file was read, not worked around. Q141's toast is gone with the repair.
    expect(errors).toHaveLength(0);
  });

  it("draws the wireframe of the object as a box instead of as nothing", async () => {
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const { adapter } = adapterFixture();
    adapter.setLibrary(padClient());
    adapter.syncScene(padWorld());
    await settle();
    adapter.setBoundingBoxesEnabled(true);

    expect(adapter.boxReport().drawn).toEqual([1]);
    const measured = adapter.worldAabb(1)!;
    expect(isFiniteBox(measured)).toBe(true);
    // Q119's crux, still standing: the box on the screen is the box `/get2DAABB` reports. Before the
    // normalization the helper existed and its box was NaN on all six corners, which draws nothing.
    expect(helpers(adapter).get(1)!.box.equals(measured)).toBe(true);
    debug.mockRestore();
  });

  it("keeps a NaN part out of the box of what hangs next to it and out of the camera", () => {
    // `computeBoundingBox` says out loud what it found; the guard is about the number not escaping.
    const noisy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(worldFrom([object(1, "arm", [transform()])]));
    nodes.get(1)!.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1)));
    // A part that never went through `normalizeObjSource` -- parsed by hand here, mounted by another
    // path there, or damaged after the load. The arm is measured by walking its children, so one such
    // part must not reach the union.
    const damagedPart = meshesOf(new OBJLoader().parse(padObj))[0]!;
    nodes.get(1)!.add(damagedPart);
    // What hangs *under* the damaged part is its own geometry: the guard skips that one measure, not
    // the subtree, so this child still reaches the union (a `return` there would shrink the box).
    const under = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5));
    under.position.x = 3;
    damagedPart.add(under);

    const box = adapter.worldAabb(1)!;
    expect(isFiniteBox(box)).toBe(true);
    // The cube plus the child: `simWorld`'s Z-up turn leaves an axis-aligned cube's x range untouched.
    expect([box.min.x, box.max.x]).toEqual([-0.5, 3.25]);

    // The consumer that made it a stage-wide bug: a NaN box travels through `frame()` into the
    // camera, and from then on every ray on the stage misses. `frame` reads two constructor
    // fields the by-hand fixture does not install, so the test hands it its own.
    const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 100);
    camera.position.set(2, 2, 2);
    const stage = adapter as unknown as { camera: THREE.PerspectiveCamera; orbit: { target: THREE.Vector3; update(): void } };
    stage.camera = camera;
    stage.orbit = { target: new THREE.Vector3(), update: () => undefined };
    adapter.frame(1);
    expect([camera.position.x, camera.position.y, camera.position.z].every(Number.isFinite)).toBe(true);
    expect(Number.isFinite(stage.orbit.target.x)).toBe(true);
    noisy.mockRestore();
  });
});

/**
 * §4.9 (**Q130**): the local-frame axes an object may declare. The four questions this layer
 * has to answer are the four the frustum lines already answered - does it draw, is it an editor
 * layer for the captures and the extents, is it out of the click list, and does the flag own it
 * alone - and the fixture answers all four without a GPU.
 */
describe("local axes overlay (场景文件描述 §4.9, Q130)", () => {
  const axesOf = (properties: Record<string, unknown>): WorldComponent =>
    ({ component_type: "LocalAxes", properties });
  /** Object 1 has nothing to draw; object 2 declares the frame. */
  const axesWorld = (properties: Record<string, unknown> = {}): World => worldFrom([
    object(1, "plain", [transform(null, { position: [1, 0, 0] })]),
    object(2, "frame", [transform(null, { position: [0, 0, 2] }), axesOf(properties)]),
  ]);
  const groupOf = (nodes: Map<number, THREE.Object3D>): THREE.Object3D => markerOf(nodes, 2, "localAxes");
  const linesIn = (group: THREE.Object3D): THREE.Line[] =>
    group.children.filter((child) => (child as THREE.Line).isLine) as THREE.Line[];
  const spritesIn = (group: THREE.Object3D): THREE.Sprite[] =>
    group.children.filter((child) => (child as THREE.Sprite).isSprite) as THREE.Sprite[];
  /** Where an axis ends: the second vertex of its two-point line, in the object's own frame.
   *  Rounded, because a buffer attribute stores float32. */
  const tipOf = (line: THREE.Line): number[] => {
    const position = line.geometry.getAttribute("position");
    return [position.getX(1), position.getY(1), position.getZ(1)].map((value) => Number(value.toFixed(6)));
  };
  const materialOf = (line: THREE.Line): THREE.LineBasicMaterial => line.material as THREE.LineBasicMaterial;
  const editorOnly = (adapter: ThreeRendererAdapter, object: THREE.Object3D): boolean =>
    (adapter as unknown as { isEditorOnly(object: THREE.Object3D): boolean }).isEditorOnly(object);
  const hidden = (adapter: ThreeRendererAdapter): THREE.Object3D[] =>
    (adapter as unknown as { hideOverlays(): THREE.Object3D[] }).hideOverlays();
  const selectable = (adapter: ThreeRendererAdapter, node: THREE.Object3D): boolean =>
    (adapter as unknown as { selectable(object: THREE.Object3D): boolean }).selectable(node);

  it("draws three equal axes along the object's own +X/+Y/+Z, in the file's metres", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld({ length: 0.4 }));
    const group = groupOf(nodes);
    expect(linesIn(group).map(tipOf)).toEqual([[0.4, 0, 0], [0, 0.4, 0], [0, 0, 0.4]]);
    expect(linesIn(group).map((line) => materialOf(line).color.getHexString())).toEqual(["ff0000", "00ff00", "0000ff"]);
    // The stamp that makes it the object's overlay: `populate()` sweeps children marked this way.
    expect(group.userData.renderContent).toBe(true);
    // §4.9 item 5: an object with no `MetadataRef` is a frame marker, not a placeholder cube.
    expect(nodes.get(2)!.children.some((child) => (child as THREE.Mesh).isMesh)).toBe(false);
    expect(nodes.get(1)!.children).toHaveLength(0);
  });

  it("recolours the axis the file named and leaves the other two alone", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld({ colors: { x: "#112233", y: "crimson", z: "#00ff0080" } }));
    const materials = linesIn(groupOf(nodes)).map(materialOf);
    expect(materials.map((material) => material.color.getHexString())).toEqual(["112233", "00ff00", "00ff00"]);
    // Three.js parses neither `#RGB` nor an 8-digit literal, so `logic.ts` resolves both (§4.3 family).
    expect(materials[2]!.opacity).toBeCloseTo(128 / 255, 6);
    expect(materials[2]!.transparent).toBe(true);
    expect(materials[0]!.transparent).toBe(false);
  });

  it("puts letters at the tips only for a literal true, sized with the axis", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld({ length: 0.4 }));
    expect(spritesIn(groupOf(nodes))).toHaveLength(0);

    adapter.syncScene(axesWorld({ length: 0.4, show_labels: true }));
    const group = groupOf(nodes);
    const labels = spritesIn(group);
    expect(labels.map((sprite) => sprite.position.toArray())).toEqual([[0.4, 0, 0], [0, 0.4, 0], [0, 0, 0.4]]);
    // A light label is a fixed width because its marker is a fixed-size sphere; these travel with
    // `length`, so a 0.4 m frame gets 0.4 m of text and the canvas aspect keeps the letters square.
    expect(labels[0]!.scale.x).toBeCloseTo(0.4);
    expect(labels[0]!.scale.y).toBeCloseTo(0.4 * 40 / 256, 6);
  });

  it("shifts the whole frame by offset, in the object's own metres", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld({ length: 0.4, offset: [0.1, -0.2, 0.3] }));
    const group = groupOf(nodes);
    // The group itself carries the offset; the axes inside it still start at the group's origin.
    expect(group.position.toArray()).toEqual([0.1, -0.2, 0.3]);
    expect(linesIn(group).map(tipOf)).toEqual([[0.4, 0, 0], [0, 0.4, 0], [0, 0, 0.4]]);
    // Labels travel with the tips, which are now offset from the object's origin.
    adapter.syncScene(axesWorld({ length: 0.4, offset: [0.1, -0.2, 0.3], show_labels: true }));
    const labels = spritesIn(groupOf(nodes));
    expect(labels.map((sprite) => sprite.position.toArray())).toEqual([[0.4, 0, 0], [0, 0.4, 0], [0, 0, 0.4]]);
  });

  it("keeps the layer out of the extent, the capture and the click list", () => {
    const withAxes = adapterFixture();
    const without = adapterFixture();
    withAxes.adapter.syncScene(axesWorld({ length: 4, show_labels: true }));
    without.adapter.syncScene(worldFrom([
      object(1, "plain", [transform(null, { position: [1, 0, 0] })]),
      object(2, "frame", [transform(null, { position: [0, 0, 2] })]),
    ]));
    // A 2 m cube of asset geometry on each frame object: this is what `worldAabb()` walks,
    // and the 4 m axes above it must not move it by a single centimetre.
    for (const fixture of [withAxes, without]) {
      const crate = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2));
      crate.userData.renderContent = true;
      fixture.nodes.get(2)!.add(crate);
      fixture.scene.updateMatrixWorld(true);
    }
    const measured = withAxes.adapter.worldAabb()!;
    expect(measured.equals(without.adapter.worldAabb()!)).toBe(true);
    expect(withAxes.adapter.worldAabb(2)!.equals(without.adapter.worldAabb(2)!)).toBe(true);

    const group = groupOf(withAxes.nodes);
    expect(editorOnly(withAxes.adapter, group)).toBe(true);
    // A line on show is still not a body to select (Q71's rule, the same walk). Asked before
    // the capture pass, because that pass is what puts the layer in the dark.
    const line = linesIn(group)[0]!;
    expect(group.visible).toBe(true);
    expect(selectable(withAxes.adapter, line)).toBe(false);
    expect(selectable(withAxes.adapter, spritesIn(group)[0]!)).toBe(false);
    expect(hidden(withAxes.adapter)).toContain(group);
  });

  it("answers to its own flag and to no selection at all", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld());
    const group = groupOf(nodes);
    expect([group.visible, adapter.markerStates()]).toEqual([true, [{ objectId: 2, kind: "localAxes", visible: true }]]);

    // Unlike the frustum lines and the placeholder spheres, nothing here is selection-only.
    adapter.select(1);
    adapter.select(2);
    adapter.select(null);
    expect(group.visible).toBe(true);

    adapter.setLocalAxesEnabled(false);
    expect(group.visible).toBe(false);
    expect(adapter.markerStates()).toEqual([{ objectId: 2, kind: "localAxes", visible: false }]);
    adapter.setLocalAxesEnabled(true);
    expect(group.visible).toBe(true);
  });

  it("redraws when a write replaces the block, and takes the axes away when it goes", () => {
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld({ length: 0.4 }));
    adapter.syncScene(axesWorld({ length: 0.8 }));
    // The signature carries the component, so one rebuild - not one rebuild per keystroke.
    expect([...nodes.get(2)!.children].filter((child) => child.userData.localAxes)).toHaveLength(1);
    expect(linesIn(groupOf(nodes)).map(tipOf)[2]).toEqual([0, 0, 0.8]);

    adapter.syncScene(worldFrom([object(2, "frame", [transform(null, { position: [0, 0, 2] })])]));
    expect([...nodes.get(2)!.children].some((child) => child.userData.localAxes)).toBe(false);
  });

  it("rebuilds only the object whose own block changed", () => {
    const { adapter, nodes } = adapterFixture();
    const both = (second: Record<string, unknown>): World => worldFrom([
      object(1, "plain", [transform(null, { position: [1, 0, 0] }), axesOf({ length: 0.2 })]),
      object(2, "frame", [transform(null, { position: [0, 0, 2] }), axesOf(second)]),
    ]);
    adapter.syncScene(both({ length: 0.4 }));
    const mine = groupOf(nodes);
    const neighbour = markerOf(nodes, 1, "localAxes");
    adapter.syncScene(both({ length: 0.6 }));
    expect(groupOf(nodes)).not.toBe(mine);
    expect(markerOf(nodes, 1, "localAxes")).toBe(neighbour);
  });

  it("hands its geometry and materials back when the adapter is disposed", () => {
    // The same two globals `dispose()` needs and a node test does not have (§4.8.7, Q121).
    vi.stubGlobal("window", { addEventListener: vi.fn(), removeEventListener: vi.fn() });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const { adapter, nodes } = adapterFixture();
    adapter.syncScene(axesWorld({ length: 0.4, show_labels: true }));
    const line = linesIn(groupOf(nodes))[0]!;
    const geometry = vi.spyOn(line.geometry, "dispose");
    const material = vi.spyOn(materialOf(line), "dispose");
    adapter.dispose();
    expect([geometry.mock.calls.length, material.mock.calls.length]).toEqual([1, 1]);
  });
});

/**
 * §4.4 (**Q131**): the ground grid and the axes at the world origin - the two pieces of furniture
 * the viewport has drawn since M1-4 and never let anyone turn off. Both are `Helper` objects, so the
 * questions this round has to answer are not about captures or extents (those already exclude them);
 * they are about who owns `visible`, and whether the flag can be read back.
 */
describe("viewport furniture: grid and world axes (规格 §4.4, Q131)", () => {
  const editorOnly = (adapter: ThreeRendererAdapter, object: THREE.Object3D): boolean =>
    (adapter as unknown as { isEditorOnly(object: THREE.Object3D): boolean }).isEditorOnly(object);
  const hidden = (adapter: ThreeRendererAdapter): THREE.Object3D[] =>
    (adapter as unknown as { hideOverlays(): THREE.Object3D[] }).hideOverlays();
  /** The last `Viewport markers` line written: the only surface that can say what is on show. */
  const markersOf = (calls: unknown[][]): Record<string, unknown> => {
    const line = calls.map(([text]) => String(text)).reverse().find((text) => text.startsWith("Viewport markers "));
    return JSON.parse((line ?? "").slice("Viewport markers ".length));
  };

  it("gives each key its own layer and nothing else", () => {
    const { adapter, gridHelper, axesHelper } = adapterFixture();
    adapter.setGridEnabled(false);
    expect([gridHelper.visible, axesHelper.visible]).toEqual([false, true]);
    adapter.setGridEnabled(true);
    expect(gridHelper.visible).toBe(true);

    adapter.setWorldAxesEnabled(false);
    expect([axesHelper.visible, gridHelper.visible]).toEqual([false, true]);
    adapter.setWorldAxesEnabled(true);
    expect(axesHelper.visible).toBe(true);
  });

  it("keeps the state the keys left while the selection does its own round", () => {
    // `applyOverlayVisibility()` owns every marker hung on an object node; these two live on `scene`
    // and it must not reach them, or a click would bring a closed layer back (§4.4, Q131).
    const { adapter, nodes, gridHelper, axesHelper } = adapterFixture();
    adapter.syncScene(worldFrom([
      object(1, "box", [transform(null, { position: [1, 0, 0] })]),
      object(2, "cam", [transform(null, { position: [0, 0, 2] }),
        { component_type: "Camera", properties: { type: "rgbd", fov: 90, near: 0.1, far: 10, resolution: [64, 32] } }]),
    ]));
    adapter.setGridEnabled(false);
    adapter.setWorldAxesEnabled(false);

    adapter.select(2);
    expect(markerOf(nodes, 2, "cameraMarker").visible).toBe(true);
    expect([gridHelper.visible, axesHelper.visible]).toEqual([false, false]);
    adapter.select(null);
    expect([gridHelper.visible, axesHelper.visible]).toEqual([false, false]);
  });

  it("takes them off the screen without taking them off the stage", () => {
    const { adapter, scene, gridHelper, axesHelper } = adapterFixture();
    scene.add(gridHelper, axesHelper);
    adapter.setGridEnabled(false);
    expect(gridHelper.visible).toBe(false);
    expect(scene.children).toContain(gridHelper);
    expect(scene.children).toContain(axesHelper);
  });

  it("stands the three arms in the 仿真系, the frame every node of the document is drawn in", () => {
    // §4.4 (Q135): the helper hangs on `scene`, next to `simWorld` rather than under it, so it has
    // to carry the Z-up conversion itself or the arm that points up on screen is the green one and
    // the world frame disagrees with 场景文件描述 §4.9 (单位姿态的物体蓝轴朝天). The fixture
    // installs the constructor's fields by hand, so this pins the constant and the pairing - the
    // same limit §12.27 ① already accepts for `visible` and the class-field defaults.
    const { simWorld, axesHelper } = adapterFixture();
    expect(axesHelper.rotation.x).toBe(simWorld.rotation.x);
    const tilt = new THREE.Quaternion().setFromEuler(new THREE.Euler(SIM_WORLD_TILT, 0, 0));
    const arm = (unit: [number, number, number]) => new THREE.Vector3(...unit).applyQuaternion(tilt);
    // three (x, y, z) is file (x, -z, y), so a file direction lands where `toSceneBox` says it does.
    expect(arm([1, 0, 0]).toArray().map((v) => Number(v.toFixed(6)))).toEqual([1, 0, 0]);
    expect(arm([0, 1, 0]).toArray().map((v) => Number(v.toFixed(6)))).toEqual([0, 0, -1]);
    expect(arm([0, 0, 1]).toArray().map((v) => Number(v.toFixed(6)))).toEqual([0, 1, 0]);
  });

  it("is already an editor layer, so no capture ever saw them and none starts to", () => {
    const { adapter, scene, gridHelper, axesHelper } = adapterFixture();
    scene.add(gridHelper, axesHelper);
    expect([editorOnly(adapter, gridHelper), editorOnly(adapter, axesHelper)]).toEqual([true, true]);

    const collected = hidden(adapter);
    expect(collected).toContain(gridHelper);
    expect(collected).toContain(axesHelper);
    expect([gridHelper.visible, axesHelper.visible]).toEqual([false, false]);

    // A layer the key closed first is not something the capture pass hands back as visible (坑 75).
    adapter.setGridEnabled(true);
    adapter.setWorldAxesEnabled(false);
    expect(hidden(adapter)).not.toContain(axesHelper);
    expect(axesHelper.visible).toBe(false);
  });

  it("says on the one readable line which of the two are on show", () => {
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const { adapter } = adapterFixture();
    adapter.setGridEnabled(false);
    expect(markersOf(debug.mock.calls)).toMatchObject({ gridEnabled: false, worldAxesEnabled: true });

    adapter.setWorldAxesEnabled(false);
    expect(markersOf(debug.mock.calls)).toMatchObject({ gridEnabled: false, worldAxesEnabled: false });

    adapter.setGridEnabled(true);
    adapter.setWorldAxesEnabled(true);
    expect(markersOf(debug.mock.calls)).toMatchObject({ gridEnabled: true, worldAxesEnabled: true });
    debug.mockRestore();
  });
});

describe("RobotAttachment mounting (场景文件描述 §4.8)", () => {
  const armRef = { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/1", type: "robot" } } as WorldComponent;
  /** An arm whose wrist link is the frame the gripper's mount pose is written in. */
  const wristRobot: UrdfRobot = {
    name: "arm",
    links: [{ name: "base_link", visuals: [] }, { name: "wrist_3_link", visuals: [] }],
    joints: [{ name: "shoulder_pan_joint", type: "revolute", parent: "base_link", child: "wrist_3_link",
      origin: { translation: [0, 0, 0.5], quaternion: [0, 0, 0, 1] }, axis: [0, 0, 1], limit: null, mimic: null }],
    roots: ["base_link"],
    meshFiles: [],
  };

  /**
   * The stage with the arm already fetchable: `parseUrdf` needs a DOM and
   * `urdf.test.ts` proves it, so the robot it would return is installed at the
   * cache key the renderer reads.
   */
  function armStage(errors: string[] = []) {
    const bytes = new TextEncoder().encode("<robot name='arm'/>").buffer as ArrayBuffer;
    const client = Object.assign(Object.create(LibraryClient.prototype), {
      robot: async (modelId: number) => ({ modelId, kind: "robot", path: "arm.urdf", isOnCos: false, reference: [], raw: {} }),
      bundle: async () => ({ entry: "arm.urdf", files: new Map([["arm.urdf", bytes]]), failures: [] }),
      fileUrl: (id: number, path: string) => `http://ols.test/file?type=robot&id=${id}&path=${path}`,
    });
    const fixture = adapterFixture(errors);
    fixture.robots.set("robot:1", Promise.resolve(wristRobot));
    fixture.adapter.setLibrary(client);
    return fixture;
  }

  /** The mount the real scene writes: 0.1 m along the wrist link's +Y. */
  function mount(link: string): WorldComponent {
    return {
      component_type: "RobotAttachment",
      properties: {
        parent_object_id: 1, parent_link: link, connection_type: "fixed",
        transform: { position: [0, 0.1, 0], quaternion_xyzw: [0, 0, 0, 1], reference_frame: "parent_link" },
      },
    };
  }

  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

  it("hangs the gripper from the wrist link the attachment names, once the arm has loaded", async () => {
    const errors: string[] = [];
    const { adapter, nodes, robotTrees } = armStage(errors);

    adapter.syncScene(worldFrom([
      object(1, "arm", [transform(null, { position: [0, 0, 0.5] }), armRef]),
      object(2, "gripper", [transform(1), mount("wrist_3_link")]),
    ]));
    // Before the URDF resolves there is no link to hang from, so the object node stands in.
    expect(nodes.get(2)!.parent).toBe(nodes.get(1));

    await settle();
    expect(errors).toEqual([]);
    const wrist = robotTrees.get(1)!.tree.links.get("wrist_3_link")!;
    expect(nodes.get(2)!.parent).toBe(wrist);
    // The mount pose is local to that link: the file's 0.1 m along +Y of the wrist.
    expect(nodes.get(2)!.position.toArray()).toEqual([0, 0.1, 0]);

    // Turning the arm carries the gripper with it, which is the whole point of the link frame.
    const atRest = nodes.get(2)!.getWorldPosition(new THREE.Vector3()).clone();
    setJointValue(robotTrees.get(1)!.tree.joints.get("shoulder_pan_joint")!, Math.PI / 2);
    adapter.trajView.refresh();
    expect(nodes.get(2)!.getWorldPosition(new THREE.Vector3()).distanceTo(atRest)).toBeGreaterThan(0.01);
  });

  it("mounts on the link during the sync when the robot tree is already on the stage", () => {
    const { adapter, nodes, robotTrees } = adapterFixture();
    const wrist = new THREE.Object3D();
    wrist.name = "wrist_3_link";
    const armRoot = new THREE.Object3D();
    armRoot.add(wrist);

    adapter.syncScene(worldFrom([object(1, "arm", [transform()])]));
    robotTrees.set(1, {
      robot: { name: "arm", links: [], joints: [], roots: ["base_link"], meshFiles: [] },
      tree: { root: armRoot, links: new Map([["wrist_3_link", wrist]]), joints: new Map() },
    });
    // The arm's signature is unchanged, so this sync keeps the tree and can mount at once.
    adapter.syncScene(worldFrom([object(1, "arm", [transform()]), object(2, "gripper", [transform(1), mount("wrist_3_link")])]));
    expect(nodes.get(2)!.parent).toBe(wrist);
  });

  it("reports a link the parent's URDF does not have and keeps the object on the parent node", async () => {
    const errors: string[] = [];
    const { adapter, nodes } = armStage(errors);

    adapter.syncScene(worldFrom([
      object(1, "arm", [transform(), armRef]),
      object(2, "gripper", [transform(1), mount("camera_mount")]),
    ]));
    await settle();

    expect(errors).toEqual(['attachment of object 2 names link "camera_mount", which robot 1 does not have; mounted on the parent object']);
    expect(nodes.get(2)!.parent).toBe(nodes.get(1));
  });

  it("does not stamp the mounted object with the arm's id", async () => {
    const { adapter, nodes } = armStage();

    adapter.syncScene(worldFrom([object(1, "arm", [transform(), armRef]), object(2, "gripper", [transform(1), mount("wrist_3_link")])]));
    await settle();

    // `loadModel` stamps its subtree; a node mounted into it afterwards keeps its own owner.
    expect(nodes.get(2)!.userData.objectId).toBe(2);
  });

  it("moves a mounted object off the arm's tree before that tree is rebuilt", async () => {
    const { adapter, nodes, robotTrees } = armStage();
    adapter.syncScene(worldFrom([object(1, "arm", [transform(), armRef]), object(2, "gripper", [transform(1), mount("wrist_3_link")])]));
    await settle();
    const oldWrist = robotTrees.get(1)!.tree.links.get("wrist_3_link")!;
    expect(nodes.get(2)!.parent).toBe(oldWrist);
    const geometry = new THREE.BoxGeometry(1, 1, 1);
    let disposed = 0;
    geometry.addEventListener("dispose", () => { disposed += 1; });
    nodes.get(2)!.add(new THREE.Mesh(geometry, new THREE.MeshStandardMaterial()));

    // A re-populate disposes the old tree; the gripper and its own geometry must survive it.
    adapter.syncScene(worldFrom([
      object(1, "arm", [transform(), armRef, { component_type: "MeshRenderer", properties: { forceOverrideColor: "#FF0000" } }]),
      object(2, "gripper", [transform(1), mount("wrist_3_link")]),
    ]));
    expect(nodes.get(2)!.parent).toBe(nodes.get(1));
    await settle();

    expect(disposed).toBe(0);
    const newWrist = robotTrees.get(1)!.tree.links.get("wrist_3_link")!;
    expect(newWrist).not.toBe(oldWrist);
    expect(nodes.get(2)!.parent).toBe(newWrist);
  });

  it("mounts on the parent object node when the mount is framed on the object", () => {
    const errors: string[] = [];
    const { adapter, nodes } = adapterFixture(errors);
    const objectFramed: WorldComponent = {
      component_type: "RobotAttachment",
      properties: {
        parent_object_id: 1, parent_link: "wrist_3_link",
        transform: { position: [0.26, 0.13, 0.69], reference_frame: "parent_object" },
      },
    };

    adapter.syncScene(worldFrom([object(1, "arm", [transform()]), object(2, "gripper", [transform(1), objectFramed])]));
    expect(nodes.get(2)!.parent).toBe(nodes.get(1));
    expect(nodes.get(2)!.position.toArray()).toEqual([0.26, 0.13, 0.69]);
    expect(errors).toEqual([]);
  });
});

/**
 * §7.3's two editor layers - the segment-end ghost (Q96-Q98) and the trajectory waypoints
 * (Q101, Q102) - are both drawn on a stage that has a robot mounted and its URDF expanded,
 * so the fixtures they need live here rather than inside one describe.
 */

/** An arm with a box on each moving link: enough geometry for a material to tint. */
const boxRobot: UrdfRobot = {
  name: "arm",
  links: [
    { name: "base", visuals: [] },
    { name: "link1", visuals: [{ name: "link1_box", origin: { translation: [0, 0, 0], quaternion: [0, 0, 0, 1] },
      geometry: { kind: "box", size: [0.1, 0.1, 0.2] }, rgba: [0.5, 0.5, 0.5, 1] }] },
    { name: "link2", visuals: [{ name: "link2_box", origin: { translation: [0, 0, 0], quaternion: [0, 0, 0, 1] },
      geometry: { kind: "box", size: [0.1, 0.1, 0.2] }, rgba: [0.5, 0.5, 0.5, 1] }] },
  ],
  // `joint1` turns about Y so its value really swings the wrist sideways in a test.
  joints: [
    { name: "joint1", type: "revolute", parent: "base", child: "link1",
      origin: { translation: [0, 0, 0.3], quaternion: [0, 0, 0, 1] }, axis: [0, 1, 0], limit: [-3.14, 3.14], mimic: null },
    { name: "joint2", type: "revolute", parent: "link1", child: "link2",
      origin: { translation: [0, 0, 0.6], quaternion: [0, 0, 0, 1] }, axis: [0, 1, 0], limit: [-3.14, 3.14], mimic: null },
  ],
  roots: ["base"],
  meshFiles: [],
};

/** A parallel gripper: two symmetric pads hung off one base (**Q102**'s shape in OLS). */
const padRobot: UrdfRobot = {
  name: "gripper",
  links: [{ name: "base", visuals: [] }, { name: "right_silicone_pad", visuals: [] }, { name: "left_silicone_pad", visuals: [] }],
  joints: [
    { name: "right_pad_joint", type: "fixed", parent: "base", child: "right_silicone_pad",
      origin: { translation: [0.04, 0, 0.1], quaternion: [0, 0, 0, 1] }, axis: [0, 1, 0], limit: null, mimic: null },
    { name: "left_pad_joint", type: "fixed", parent: "base", child: "left_silicone_pad",
      origin: { translation: [-0.04, 0, 0.1], quaternion: [0, 0, 0, 1] }, axis: [0, 1, 0], limit: null, mimic: null },
  ],
  roots: ["base"],
  meshFiles: [],
};

const driver = (joints: string[]): WorldComponent =>
  ({ component_type: "RobotDriver", properties: { joint_sequence: joints } }) as WorldComponent;

const robotObject = (id: number, name: string, parent: number | null, joints: string[]): Record<string, unknown> =>
  object(id, name, [
    transform(parent),
    { component_type: "MetadataRef", properties: { path: `$LIBRARY_SERVER/${id}`, type: "robot" } } as WorldComponent,
    driver(joints),
  ]);

const armWorld = (): World => worldFrom([robotObject(1, "arm", null, ["joint1", "joint2"])]);

/** Arm plus the gripper mounted on it: every real scene has two driven robots (**Q98**). */
const pairWorld = (): World => worldFrom([
  robotObject(1, "arm", null, ["joint1", "joint2"]),
  robotObject(2, "gripper", 1, ["joint1"]),
]);

/** The same directory, but the URDF only arrives when the test opens the gate. */
function gatedGhostClient(): { client: LibraryClient; release: () => void } {
  const bytes = new TextEncoder().encode("<robot name='arm'/>").buffer as ArrayBuffer;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const client = Object.assign(Object.create(LibraryClient.prototype), {
    robot: async (modelId: number) => ({ modelId, kind: "robot", path: "arm.urdf", isOnCos: false, reference: [], raw: {} }),
    bundle: async () => {
      await gate;
      return { entry: "arm.urdf", files: new Map([["arm.urdf", bytes]]), failures: [] };
    },
    fileUrl: (id: number, path: string) => `http://ols.test/file?type=robot&id=${id}&path=${path}`,
  }) as LibraryClient;
  return { client, release };
}

/** A robot directory whose entry is a URDF, or anything else when a test says otherwise. */
function ghostClient(entry = "arm.urdf"): LibraryClient {
  const bytes = new TextEncoder().encode("<robot name='arm'/>").buffer as ArrayBuffer;
  return Object.assign(Object.create(LibraryClient.prototype), {
    robot: async (modelId: number) => ({ modelId, kind: "robot", path: entry, isOnCos: false, reference: [], raw: {} }),
    bundle: async () => ({ entry, files: new Map([[entry, bytes]]), failures: [] }),
    fileUrl: (id: number, path: string) => `http://ols.test/file?type=robot&id=${id}&path=${path}`,
  }) as LibraryClient;
}

describe("segment-end ghost (§7.3, Q96, Q97, Q98)", () => {
  /** `ghosts` is deliberately private: the tests read it the way the adapter itself does. */
  const ghostTree = (adapter: ThreeRendererAdapter, objectId: number): UrdfTree | undefined =>
    (adapter as unknown as { ghosts: Map<number, UrdfTree> }).ghosts.get(objectId);

  /** The world mounted and its robot expanded, which is what a loaded clip is played on. */
  async function ghostFixture(world: World = armWorld(), ids: number[] = [1]) {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    fixture.adapter.setLibrary(ghostClient());
    for (const id of ids) fixture.robots.set(`robot:${id}`, Promise.resolve(boxRobot));
    fixture.adapter.syncScene(world);
    await new Promise((resolve) => setTimeout(resolve, 0));
    errors.length = 0;
    const ghostRoot = (objectId = 1) => ghostTree(fixture.adapter, objectId)?.root ?? null;
    return { ...fixture, errors, ghostRoot };
  }

  const twoRobotFixture = () => ghostFixture(pairWorld(), [1, 2]);

  const ghostNodes = (object: THREE.Object3D) => object.children.filter((child) => child.userData.ghost);

  it("paints a second arm at the opacity it is asked for that nothing outside the renderer can see", async () => {
    const { adapter, nodes, robotTrees, simWorld, errors, ghostRoot } = await ghostFixture();

    expect(await adapter.buildGhost(1, 0.5)).toBe(true);
    expect(errors).toEqual([]);

    const ghost = ghostRoot()!;
    const meshes = meshesOf(ghost);
    expect(meshes).toHaveLength(2);
    for (const mesh of meshes) {
      const material = mesh.material as THREE.MeshStandardMaterial;
      // **Q97**: the value is not baked into the renderer, the caller reads it from the
      // runtime config, so a test of the transport is a test of this argument.
      expect(material.opacity).toBe(0.5);
      expect(material.transparent).toBe(true);
      // One translucent arm over another would hide itself, so only the live one writes depth.
      expect(material.depthWrite).toBe(false);
      // §4.8.5 (Q117): and a pose preview must not drop a solid shadow either, so the ghost
      // is out of the depth pass exactly the way it is out of the world box.
      expect(mesh.castShadow).toBe(false);
      expect(mesh.receiveShadow).toBe(false);
    }
    for (const mesh of meshesOf(nodes.get(1)!)) {
      expect((mesh.material as THREE.MeshStandardMaterial).opacity).toBe(1);
      expect((mesh.material as THREE.MeshStandardMaterial).depthWrite).toBe(true);
    }

    // A ghost is not an object: the stage still has one node and one joint tree.
    expect(nodes.size).toBe(1);
    expect(robotTrees.size).toBe(1);
    expect(ghostNodes(simWorld)).toEqual([ghost]);
    // It hangs from the live arm's parent, at the live arm's base pose (§8.6).
    expect(ghost.parent).toBe(nodes.get(1)!.parent);
    expect(ghost.position.toArray()).toEqual(nodes.get(1)!.position.toArray());
  });

  it("follows the arm it mirrors and turns only its own joints", async () => {
    const { adapter, nodes, robotTrees, scene, ghostRoot } = await ghostFixture();
    await adapter.buildGhost(1, 0.5);

    const ghostJoints = adapter.syncGhost(1)!;
    const liveJoints = robotTrees.get(1)!.tree.joints;
    expect(ghostJoints.get("joint1")).not.toBe(liveJoints.get("joint1"));

    const node = nodes.get(1)!;
    node.position.set(1, 2, 3);
    node.quaternion.setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 3);
    adapter.syncGhost(1);
    expect(ghostRoot()!.position.toArray()).toEqual([1, 2, 3]);
    expect(ghostRoot()!.quaternion.angleTo(node.quaternion)).toBeCloseTo(0, 9);

    const ghostWrist = ghostJoints.get("joint2")!.node;
    const liveWrist = liveJoints.get("joint2")!.node;
    scene.updateMatrixWorld(true);
    const liveBefore = liveWrist.getWorldPosition(new THREE.Vector3()).toArray();
    const ghostBefore = ghostWrist.getWorldPosition(new THREE.Vector3()).toArray();

    setJointValue(ghostJoints.get("joint1")!, Math.PI / 2);
    scene.updateMatrixWorld(true);
    expect(ghostWrist.getWorldPosition(new THREE.Vector3()).toArray()).not.toEqual(ghostBefore);
    expect(liveWrist.getWorldPosition(new THREE.Vector3()).toArray()).toEqual(liveBefore);

    // An object that left the stage has nothing to mirror.
    expect(adapter.syncGhost(404)).toBeNull();
  });

  it("stays out of the box the whole world is measured by", async () => {
    const { adapter, scene, ghostRoot } = await ghostFixture();
    await adapter.buildGhost(1, 0.5);
    scene.updateMatrixWorld(true);
    const measured = adapter.worldAabb()!.clone();

    // Park the ghost far away: if the world were measured from the scene graph rather
    // than from `nodes`, this alone would blow the box up.
    ghostRoot()!.position.set(50, 0, 0);
    scene.updateMatrixWorld(true);
    expect(adapter.worldAabb()!.equals(measured)).toBe(true);
    expect(adapter.worldAabb(1)!.equals(measured)).toBe(true);
  });

  it("reuses the instance already on the stage instead of expanding a third arm", async () => {
    const { adapter, simWorld, ghostRoot } = await ghostFixture();
    const first = adapter.buildGhost(1, 0.5);
    const second = adapter.buildGhost(1, 0.5);
    expect(await first).toBe(true);
    expect(await second).toBe(true);
    expect(await adapter.buildGhost(1, 0.5)).toBe(true);
    expect(ghostNodes(simWorld)).toHaveLength(1);
    expect(adapter.syncGhost(1)).not.toBeNull();

    // An object the world does not have cannot be mirrored.
    expect(await adapter.buildGhost(404, 0.5)).toBe(false);
    expect(ghostRoot()).not.toBeNull();
  });

  it("mirrors every driven robot, and hangs a mounted one off the ghost arm (§7.3, Q98)", async () => {
    const { adapter, nodes, robotTrees, ghostRoot } = await twoRobotFixture();
    expect(await adapter.buildGhost(1, 0.5)).toBe(true);
    // The second driven robot adds a ghost instead of replacing the first.
    expect(await adapter.buildGhost(2, 0.5)).toBe(true);
    expect(ghostRoot(1)).not.toBeNull();
    expect(ghostRoot(2)).not.toBeNull();
    // Until an Attach moves it, the gripper's ghost shares the arm's scene node.
    expect(ghostRoot(2)!.parent).toBe(nodes.get(1));

    // An Attach re-hangs the live gripper on the arm's wrist link (§8.6, drivers.ts:attachTarget),
    // and the ghost has to follow the *ghost* wrist or the pair splits into two poses.
    const liveWrist = robotTrees.get(1)!.tree.links.get("link1")!;
    liveWrist.add(nodes.get(2)!);
    expect(adapter.syncGhost(2)).not.toBeNull();
    expect(ghostRoot(2)!.parent).toBe(ghostTree(adapter, 1)!.links.get("link1"));
    expect(ghostRoot(2)!.parent).not.toBe(liveWrist);

    // Removing the arm's ghost leaves the gripper on the live parent rather than lost.
    adapter.retainGhosts(new Set([2]));
    expect(ghostRoot(1)).toBeNull();
    expect(adapter.syncGhost(2)).not.toBeNull();
    expect(ghostRoot(2)!.parent).toBe(liveWrist);
  });

  it("keeps only the ghosts the caller asks for", async () => {
    const { adapter, scene, simWorld, ghostRoot } = await twoRobotFixture();
    await adapter.buildGhost(1, 0.5);
    await adapter.buildGhost(2, 0.5);

    adapter.retainGhosts(new Set([1]));
    expect(ghostRoot(2)).toBeNull();
    expect(ghostRoot(1)).not.toBeNull();
    expect(ghostNodes(simWorld)).toEqual([ghostRoot(1)]);

    adapter.retainGhosts(new Set());
    expect(ghostRoot(1)).toBeNull();
    expect(ghostNodes(scene)).toEqual([]);
  });

  /** §12.15 (Q107): the page-side readout - withdrawing one robot must show up as one id less. */
  it("reports which robots are mirrored, in id order", async () => {
    const { adapter } = await twoRobotFixture();
    expect(adapter.ghostStates()).toEqual([]);
    await adapter.buildGhost(2, 0.5);
    await adapter.buildGhost(1, 0.5);
    expect(adapter.ghostStates()).toEqual([{ objectId: 1, ready: true }, { objectId: 2, ready: true }]);
    adapter.retainGhosts(new Set([2]));
    expect(adapter.ghostStates()).toEqual([{ objectId: 2, ready: true }]);
    adapter.clearGhost();
    expect(adapter.ghostStates()).toEqual([]);
  });

  it("throws away a ghost nobody wants by the time its asset lands", async () => {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    const gated = gatedGhostClient();
    fixture.adapter.setLibrary(gated.client);
    fixture.robots.set("robot:1", Promise.resolve(boxRobot));
    fixture.adapter.syncScene(armWorld());
    await new Promise((resolve) => setTimeout(resolve, 0));

    const building = fixture.adapter.buildGhost(1, 0.5);
    // The clip is cleared while the URDF is still on its way: the ghost must not appear.
    fixture.adapter.clearGhost();
    gated.release();
    expect(await building).toBe(false);
    expect(ghostTree(fixture.adapter, 1)).toBeUndefined();
    expect(ghostNodes(fixture.simWorld)).toEqual([]);
  });

  it("takes the ghost away when the world no longer has the arm", async () => {
    const { adapter, scene, simWorld, ghostRoot } = await ghostFixture();
    await adapter.buildGhost(1, 0.5);
    const ghost = ghostRoot()!;

    adapter.syncScene(worldFrom([object(2, "apple", [transform()])]));
    expect(ghostRoot()).toBeNull();
    expect(ghost.parent).toBeNull();
    expect(ghostNodes(scene)).toEqual([]);
    expect(ghostNodes(simWorld)).toEqual([]);
    // Clearing is idempotent: `setTrajectory` calls it on every write that ends playback.
    expect(() => adapter.clearGhost()).not.toThrow();
  });

  it("says once that an asset without a URDF has no ghost to draw", async () => {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    fixture.adapter.setLibrary(ghostClient("arm.stl"));
    fixture.adapter.syncScene(armWorld());
    await new Promise((resolve) => setTimeout(resolve, 0));
    errors.length = 0;

    expect(await fixture.adapter.buildGhost(1, 0.5)).toBe(false);
    expect(await fixture.adapter.buildGhost(1, 0.5)).toBe(false);
    expect(errors.filter((message) => message.includes("is not a URDF"))).toHaveLength(1);
  });
});

describe("trajectory waypoint spheres (§7.3, Q101, Q102)", () => {
  /** `waypoints` is deliberately private: the evidence counts these layers, not pixels. */
  type Layer = { mesh: THREE.InstancedMesh; radius: number; color: number; capacity: number };
  const layerMap = (adapter: ThreeRendererAdapter) => (adapter as unknown as { waypoints: Map<string, Layer> }).waypoints;
  const waypointNodes = (object: THREE.Object3D) => object.children.filter((child) => child.userData.waypoint);

  /** The stage a clip would be swept on: world mounted, robot expanded. */
  async function stage(world: World = armWorld(), id = 1, built: UrdfRobot = boxRobot) {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    fixture.adapter.setLibrary(ghostClient());
    fixture.robots.set(`robot:${id}`, Promise.resolve(built));
    fixture.adapter.syncScene(world);
    await new Promise((resolve) => setTimeout(resolve, 0));
    errors.length = 0;
    return { ...fixture, errors };
  }

  const path = (key: string, trajectoryIndex: number, objectId: number, color: number, points: [number, number, number][] = [[0, 0, 0]]) =>
    ({ key, trajectoryIndex, objectId, color, points });

  it("takes the mean of the deepest leaf links in the world file's own axes (§7.3, Q102)", async () => {
    const { adapter } = await stage();
    // base -> joint1 (0 0 0.3) -> link1 -> joint2 (0 0 0.6): the one leaf of an arm.
    expect(adapter.toolTipPosition(1)!.map((value) => Number(value.toFixed(6)))).toEqual([0, 0, 0.9]);

    // Two symmetric pads: the mean sits on the symmetry plane, which is neither link.
    const gripper = await stage(worldFrom([robotObject(1, "gripper", null, [])]), 1, padRobot);
    const mean = gripper.adapter.toolTipPosition(1)!.map((value) => Number(value.toFixed(6)));
    expect(mean).toEqual([0, 0, 0.1]);

    // An object with no expanded tree has no tool centre point to report.
    expect(adapter.toolTipPosition(404)).toBeNull();
  });

  it("reports the robot when its URDF lands, not when the world mounts (§7.3, Q101)", async () => {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    const gated = gatedGhostClient();
    fixture.adapter.setLibrary(gated.client);
    fixture.robots.set("robot:1", Promise.resolve(boxRobot));
    fixture.adapter.syncScene(armWorld());
    await new Promise((resolve) => setTimeout(resolve, 0));
    // The clip that was installed at this point found no tool centre point to sample, which
    // is exactly the case the report exists for: the joints are only measurable from here.
    expect(fixture.landedRobots).toEqual([]);

    gated.release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fixture.landedRobots).toEqual([1]);
    expect(fixture.adapter.toolTipPosition(1)).not.toBeNull();
  });

  it("follows the arm it is drawn for, in the layer's own coordinates", async () => {
    const { adapter, nodes, simWorld } = await stage();
    const point = adapter.toolTipPosition(1)!;
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [point])], 0.02);

    // `simWorld` owns the Z-up conversion, so a layer's instance matrix is the file's point.
    const mesh = layerMap(adapter).get("0:1")!.mesh;
    const stored = new THREE.Matrix4();
    mesh.getMatrixAt(0, stored);
    const placed = new THREE.Vector3().setFromMatrixPosition(stored).toArray();
    // Instance matrices are Float32, so the file's metre is stored to 7 significant digits.
    placed.forEach((value, index) => expect(value).toBeCloseTo(point[index]!, 6));
    expect(mesh.parent).toBe(simWorld);
    expect(mesh.parent).not.toBe(nodes.get(1));
  });

  it("draws one instanced layer per clip and arm, and shows only the ones asked for", async () => {
    const { adapter, simWorld } = await stage(pairWorld(), 1);
    adapter.setWaypointPaths([
      path("0:3", 0, 3, 0xff5d5d, [[0, 0, 0.1], [0.1, 0, 0.2], [0.2, 0, 0.3]]),
      path("1:3", 1, 3, 0x4dd2ff, [[1, 1, 1]]),
    ], 0.02);

    const layers = adapter.waypointLayers();
    expect(layers.map(({ points, ...rest }) => rest)).toEqual([
      { key: "0:3", trajectoryIndex: 0, objectId: 3, count: 3, radius: 0.02, color: 0xff5d5d, visible: true },
      { key: "1:3", trajectoryIndex: 1, objectId: 3, count: 1, radius: 0.02, color: 0x4dd2ff, visible: true },
    ]);
    // The line carries each sphere's coordinates, because §12.13 ⑥/⑦ compare positions.
    // Instance matrices are Float32, so the file's metres come back close, not equal.
    expect(layers[0]!.points).toHaveLength(3);
    for (const [index, expected] of [[0, 0, 0.1], [0.1, 0, 0.2], [0.2, 0, 0.3]].entries()) {
      layers[0]!.points[index]!.forEach((value, axis) => expect(value).toBeCloseTo(expected[axis]!, 6));
    }
    expect(layers[1]!.points).toEqual([[1, 1, 1]]);
    expect(waypointNodes(simWorld)).toHaveLength(2);
    // One geometry and one material per layer: a dense path is still a single draw call.
    const first = layerMap(adapter).get("0:3")!;
    expect(first.mesh.count).toBe(3);
    expect((first.mesh.geometry as THREE.SphereGeometry).parameters.radius).toBe(0.02);
    expect((first.mesh.material as THREE.MeshBasicMaterial).color.getHex()).toBe(0xff5d5d);

    // The checkbox's two states (§7.1, Q101): only the active clip's layer is on show.
    adapter.setWaypointVisibility(new Set(["0:3"]));
    expect(adapter.waypointLayers().map((layer) => [layer.key, layer.visible])).toEqual([["0:3", true], ["1:3", false]]);
    adapter.setWaypointVisibility(new Set(["0:3", "1:3"]));
    expect(adapter.waypointLayers().map((layer) => layer.visible)).toEqual([true, true]);
    // A key nothing drew is not visible: the set is the whole say in the matter.
    adapter.setWaypointVisibility(new Set());
    expect(adapter.waypointLayers().map((layer) => layer.visible)).toEqual([false, false]);
  });

  it("stays out of the stage's own books and out of the box the world is measured by", async () => {
    const { adapter, nodes, robotTrees, scene, simWorld } = await stage();
    scene.updateMatrixWorld(true);
    const measured = adapter.worldAabb()!.clone();
    const measuredArm = adapter.worldAabb(1)!.clone();

    // Park a sphere 50 m away: if the world were measured from the scene graph rather
    // than from `nodes`, this alone would blow the box up (§12.13 ⑤).
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [[50, 0, 0], [50, 50, 50]])], 0.02);
    scene.updateMatrixWorld(true);
    expect(waypointNodes(simWorld)).toHaveLength(1);
    expect(adapter.worldAabb()!.equals(measured)).toBe(true);
    expect(adapter.worldAabb(1)!.equals(measuredArm)).toBe(true);
    // It is not an object: no tree, no hierarchy row, no id in the world's own list.
    expect(nodes.size).toBe(1);
    expect(robotTrees.size).toBe(1);
    expect(nodes.get(1)!.children.filter((child) => child.userData.waypoint)).toEqual([]);
  });

  it("reuses a layer that has not changed and rebuilds one that has", async () => {
    const { adapter } = await stage();
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [[0, 0, 0], [0, 0, 1]])], 0.02);
    const first = layerMap(adapter).get("0:1")!;
    const geometryDispose = vi.spyOn(first.mesh.geometry, "dispose");
    const materialDispose = vi.spyOn(first.mesh.material as THREE.Material, "dispose");

    // The same layer with fewer points to show: the capacity is still big enough, so the
    // mesh is the same object and nothing is thrown away.
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [[0, 0, 0]])], 0.02);
    expect(layerMap(adapter).get("0:1")).toBe(first);
    expect(adapter.waypointLayers()[0]).toMatchObject({ count: 1, radius: 0.02, color: 0xff5d5d });
    expect(geometryDispose).not.toHaveBeenCalled();
    expect(materialDispose).not.toHaveBeenCalled();

    // A new radius is a new geometry, and a new colour a new material: both dispose the old.
    adapter.setWaypointPaths([path("0:1", 0, 1, 0x4dd2ff, [[0, 0, 0], [0, 0, 1]])], 0.05);
    const second = layerMap(adapter).get("0:1")!;
    expect(second).not.toBe(first);
    expect(second.capacity).toBe(2);
    expect((second.mesh.geometry as THREE.SphereGeometry).parameters.radius).toBe(0.05);
    expect((second.mesh.material as THREE.MeshBasicMaterial).color.getHex()).toBe(0x4dd2ff);
    expect(geometryDispose).toHaveBeenCalledTimes(1);
    expect(materialDispose).toHaveBeenCalledTimes(1);
    expect(first.mesh.parent).toBeNull();

    // More points than the layer was built for is the third reason to rebuild: an
    // `InstancedMesh` cannot grow past the capacity it was constructed with.
    adapter.setWaypointPaths([path("0:1", 0, 1, 0x4dd2ff, [[0, 0, 0], [0, 0, 1], [0, 0, 2]])], 0.05);
    const third = layerMap(adapter).get("0:1")!;
    expect(third).not.toBe(second);
    expect(third.capacity).toBe(3);
    expect(adapter.waypointLayers()[0]).toMatchObject({ count: 3, radius: 0.05, color: 0x4dd2ff });
    expect(second.mesh.parent).toBeNull();
  });

  it("drops the layers a batch no longer mentions and survives being cleared twice", async () => {
    const { adapter, simWorld } = await stage();
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [[0, 0, 0]]), path("1:1", 1, 1, 0x4dd2ff, [[1, 1, 1]])], 0.02);
    const dropped = layerMap(adapter).get("1:1")!.mesh.geometry;
    const droppedDispose = vi.spyOn(dropped, "dispose");

    // A clip that goes away takes its string of spheres with it (§12.13 ⑧).
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [[0, 0, 0]])], 0.02);
    expect([...layerMap(adapter).keys()]).toEqual(["0:1"]);
    expect(droppedDispose).toHaveBeenCalledTimes(1);

    adapter.clearWaypoints();
    expect(layerMap(adapter).size).toBe(0);
    expect(waypointNodes(simWorld)).toEqual([]);
    expect(() => adapter.clearWaypoints()).not.toThrow();
    // Clearing is what the end of playback does, and an empty path list says the same thing.
    adapter.setWaypointPaths([], 0.02);
    expect(layerMap(adapter).size).toBe(0);
    // A layer with no points is not worth a draw call, so it is not built at all.
    adapter.setWaypointPaths([path("0:1", 0, 1, 0xff5d5d, [])], 0.02);
    expect(layerMap(adapter).size).toBe(0);
  });
});

describe("sensor capture", () => {
  const width = 64;
  const height = 32;

  function sensorFixture(type: "rgb" | "rgbd", resolution: [number, number] = [width, height]) {
    const errors: string[] = [];
    const { adapter, nodes, scene } = adapterFixture(errors);
    const sensorCamera = new THREE.PerspectiveCamera();
    adapter.syncScene(worldFrom([object(4, "cam", [
      transform(null, { position: [2, 3, 4] }),
      { component_type: "Camera", properties: { type, fov: 60, near: 0.2, far: 40, resolution } },
    ])]));
    Object.assign(adapter, { sensorCamera });
    // Q105: the frustum lines now only show for the selected camera, and selecting it here
    // is what makes the assertions below mean something -- a marker that is on screen and
    // still does not reach the capture is the §4.4 rule, an invisible one proves nothing.
    adapter.select(4);
    const original = new THREE.WebGLRenderTarget(1, 1);
    let floatCapable = true;
    const gpu = {
      getContext: () => ({
        isContextLost: () => false,
        getExtension: (name: string) => (name === "EXT_color_buffer_float" && floatCapable ? {} : null),
      }),
      getRenderTarget: () => original,
      setRenderTarget: vi.fn(),
      render: vi.fn(),
      readRenderTargetPixels: vi.fn((_target: unknown, _x: number, _y: number, w: number, h: number, buffer: Uint8Array | Float32Array) => {
        // GPU row 0 is the bottom row of the image, so the copy has to flip rows.
        if (buffer instanceof Float32Array) {
          for (let row = 0; row < h; row += 1) buffer.fill(row + 1, row * w * 4, (row + 1) * w * 4);
          // Bottom row of the image: a cleared pixel, one past `far`, one the shader lost.
          buffer[0] = 0;
          buffer[4] = 41;
          buffer[8] = Number.NaN;
        } else {
          for (let row = 0; row < h; row += 1) buffer.fill(row === 0 ? 10 : 20, row * w * 4, (row + 1) * w * 4);
        }
      }),
    };
    Object.assign(adapter, { renderer: gpu });
    const drawn: Uint8ClampedArray[] = [];
    const canvas = {
      width: 0,
      height: 0,
      getContext: () => ({
        createImageData: (w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4) }),
        putImageData: (frame: { data: Uint8ClampedArray }) => { drawn.push(frame.data); },
      }),
    } as unknown as HTMLCanvasElement;
    const setFloatCapable = (value: boolean) => { floatCapable = value; };
    return { adapter, gpu, canvas, drawn, scene, sensorCamera, node: nodes.get(4)!, original, setFloatCapable, errors };
  }

  it("uses the live optical pose, hides editor layers and flips GPU rows", () => {
    const f = sensorFixture("rgbd");
    const marker = [...f.node.children].find((child) => child.userData.cameraMarker)!;
    f.gpu.render.mockImplementation(() => {
      expect(marker.visible).toBe(false);
    });

    expect(f.adapter.renderSensorFrame(4, "rgb", f.canvas)).toBe(true);
    // Z-up world poses reach the optical frame: +Z forward, +Y up, +X left (§4.4).
    expect(f.sensorCamera.position.x).toBeCloseTo(2);
    expect(f.sensorCamera.position.y).toBeCloseTo(4);
    expect(f.sensorCamera.position.z).toBeCloseTo(-3);
    expect(f.sensorCamera.getWorldDirection(new THREE.Vector3()).y).toBeCloseTo(1);
    expect(f.sensorCamera.up.z).toBeCloseTo(-1);
    expect(f.sensorCamera.fov).toBe(60);
    expect(f.sensorCamera.aspect).toBeCloseTo(2);
    expect(f.sensorCamera.near).toBeCloseTo(0.2);
    expect(f.sensorCamera.far).toBeCloseTo(40);
    expect(marker.visible).toBe(true);
    expect(f.canvas.width).toBe(width);
    expect(f.drawn).toHaveLength(1);
    const frame = f.drawn[0]!;
    expect(frame[0]).toBe(20);
    expect(frame[(height - 1) * width * 4]).toBe(10);
  });

  it("refuses depth for an rgb camera and an unknown object", () => {
    const f = sensorFixture("rgb");
    expect(f.adapter.renderSensorFrame(4, "depth", f.canvas)).toBe(false);
    expect(f.gpu.render).not.toHaveBeenCalled();
    expect(f.adapter.renderSensorFrame(99, "rgb", f.canvas)).toBe(false);
  });

  it("restores scene state when the GPU fails", () => {
    const f = sensorFixture("rgbd");
    const background = new THREE.Color(0x112233);
    const fog = new THREE.Fog(0x123456, 2, 8);
    f.scene.background = background;
    f.scene.fog = fog;
    f.gpu.render.mockImplementation(() => { throw new Error("GPU failure"); });
    const marker = [...f.node.children].find((child) => child.userData.cameraMarker)!;

    expect(() => f.adapter.renderSensorFrame(4, "depth", f.canvas)).toThrow("GPU failure");
    expect(f.scene.background).toBe(background);
    expect(f.scene.fog).toBe(fog);
    expect(f.scene.overrideMaterial).toBe(null);
    expect(marker.visible).toBe(true);
    expect(f.gpu.setRenderTarget).toHaveBeenLastCalledWith(f.original);
  });

  it("honours the preview size limits", () => {
    const f = sensorFixture("rgbd");
    expect(f.adapter.renderSensorFrame(4, "rgb", f.canvas, { maxWidth: 16, maxHeight: 16 })).toBe(true);
    expect(f.canvas.width).toBe(16);
    expect(f.canvas.height).toBe(8);
  });

  it("renders metres into a float32 target, top row first, NaN for every miss (§5.10)", () => {
    const f = sensorFixture("rgbd");
    const marker = [...f.node.children].find((child) => child.userData.cameraMarker)!;
    f.gpu.render.mockImplementation(() => { expect(marker.visible).toBe(false); });

    const frame = f.adapter.renderSensorDepth(4);
    expect(frame).not.toBeNull();
    expect(frame!.width).toBe(width);
    expect(frame!.height).toBe(height);
    expect(frame!.meters.length).toBe(width * height);
    // The top image row is GPU row height-1, which the mock filled with `height`.
    expect(frame!.meters[2]).toBe(height);
    expect(frame!.meters[width]).toBe(height - 1);
    // The bottom image row carries the three ways a pixel has no measurement.
    const last = (height - 1) * width;
    expect(Number.isNaN(frame!.meters[last])).toBe(true);
    expect(Number.isNaN(frame!.meters[last + 1])).toBe(true);
    expect(Number.isNaN(frame!.meters[last + 2])).toBe(true);
    expect(marker.visible).toBe(true);
    // The pass itself; the last call is the restore to the viewport's target.
    const target = f.gpu.setRenderTarget.mock.calls[0]?.[0] as THREE.WebGLRenderTarget;
    expect(target.texture.type).toBe(THREE.FloatType);
    expect(target.width).toBe(width);
    expect(f.gpu.setRenderTarget).toHaveBeenLastCalledWith(f.original);
  });

  it("captures a depth map at the sensor's own resolution, not the preview cap", () => {
    const f = sensorFixture("rgbd", [1280, 720]);
    expect(f.adapter.renderSensorDepth(4)!.width).toBe(1280);
    // The inspector preview keeps its §9.1 cap, so the two paths must not share limits.
    expect(f.adapter.renderSensorFrame(4, "rgb", f.canvas)).toBe(true);
    expect(f.canvas.width).toBe(640);
    expect(f.canvas.height).toBe(360);
  });

  it("refuses a depth map for an rgb camera and for a GPU without float attachments", () => {
    expect(sensorFixture("rgb").adapter.renderSensorDepth(4)).toBeNull();
    expect(sensorFixture("rgbd").adapter.renderSensorDepth(99)).toBeNull();

    const f = sensorFixture("rgbd");
    f.setFloatCapable(false);
    expect(f.adapter.renderSensorDepth(4)).toBeNull();
    expect(f.gpu.render).not.toHaveBeenCalled();
    expect(f.errors.join(" ")).toContain("EXT_color_buffer_float");
  });
});

describe("viewport screenshot (§4.4, Q108)", () => {
  const width = 48;
  const height = 20;

  /**
   * The whole editor-layer family on screen at once (§12.16's unit half): both Q105/Q106 flags
   * pinned on and a selected point light, so the frustum lines, two placeholder spheres, the
   * type label and the selection outline are all lit; a ghost, a waypoint sphere and
   * the ground grid and axes join them by hand. The unselected light's label stays dark - labels
   * follow the selection alone - which is the other half of the rule: `hideOverlays` only ever
   * touches what is visible, and must not re-light what was already off.
   */
  function viewportFixture(options: { size?: [number, number]; lost?: boolean } = {}) {
    const errors: string[] = [];
    const { adapter, nodes, scene, orbit } = adapterFixture(errors);
    adapter.syncScene(worldFrom([
      object(2, "cam", [transform(null, { position: [0, 0, 3] }),
        { component_type: "Camera", properties: { type: "rgbd", fov: 60, near: 0.2, far: 40, resolution: [64, 32] } }]),
      object(4, "lamp", [transform(null, { position: [0, 0, 1] }),
        { component_type: "Light", properties: { type: "Point", intensity: 1, color: "#FFFFFF" } }]),
      object(5, "flood", [transform(null, { position: [1, 0, 1] }),
        { component_type: "Light", properties: { type: "Point", intensity: 1, color: "#FF0000" } }]),
    ]));
    // §4.5 (Q105): the outline only exists once the selected object has a mesh of its own.
    nodes.get(4)!.add(new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.1, 0.1)));
    adapter.setCameraFrustumAlways(true);
    adapter.setLightMarkerAlways(true);
    adapter.select(4);

    const ghost = new THREE.Group();
    ghost.userData.ghost = true;
    const waypoint = new THREE.Mesh(new THREE.SphereGeometry(0.01));
    waypoint.userData.waypoint = true;
    const grid = new THREE.GridHelper(10, 10);
    const axes = new THREE.AxesHelper(1);
    scene.add(ghost, waypoint, grid, axes);
    const layers: THREE.Object3D[] = [
      markerOf(nodes, 2, "cameraMarker"), markerOf(nodes, 4, "lightMarker"), markerOf(nodes, 4, "lightLabel"),
      markerOf(nodes, 5, "lightMarker"), outline(scene)!, ghost, waypoint, grid, axes,
    ];
    const dark = markerOf(nodes, 5, "lightLabel");
    expect(layers.every((layer) => layer.visible)).toBe(true);
    expect(dark.visible).toBe(false);

    const [bufferWidth, bufferHeight] = options.size ?? [width, height];
    const original = new THREE.WebGLRenderTarget(1, 1);
    const gpu = {
      getContext: () => ({ isContextLost: () => options.lost === true, getExtension: () => null }),
      // The real one reports `canvas.width`/`canvas.height` scaled by the pixel ratio, which
      // is deliberately not the CSS box (§4.4): the fixture only has to return it.
      getDrawingBufferSize: (out: THREE.Vector2) => out.set(bufferWidth, bufferHeight),
      getRenderTarget: () => original,
      setRenderTarget: vi.fn(),
      render: vi.fn(),
      readRenderTargetPixels: vi.fn(
        (_target: unknown, _x: number, _y: number, w: number, h: number, buffer: Uint8Array) => {
          // GPU row 0 is the bottom row of the image, so the copy has to flip rows (§5.10).
          for (let row = 0; row < h; row += 1) buffer.fill(row === 0 ? 10 : 20, row * w * 4, (row + 1) * w * 4);
        }),
    };
    const camera = new THREE.PerspectiveCamera();
    Object.assign(adapter, { renderer: gpu, camera });

    const drawn: Uint8ClampedArray[] = [];
    const canvas = {
      width: 0,
      height: 0,
      getContext: () => ({
        createImageData: (w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4) }),
        putImageData: (frame: { data: Uint8ClampedArray }) => { drawn.push(frame.data); },
      }),
    } as unknown as HTMLCanvasElement;
    return { adapter, gpu, canvas, drawn, scene, camera, original, orbit, errors, layers, dark };
  }

  it("holds every editor layer back for the pass and puts each one back after it", () => {
    const f = viewportFixture();
    f.gpu.render.mockImplementation(() => {
      // The screen is showing all of them, and not one of them may reach the pixels.
      expect(f.layers.map((layer) => layer.visible)).toEqual(f.layers.map(() => false));
    });

    expect(f.adapter.captureViewport(f.canvas)).toBe(true);
    // The viewport's own camera, not the sensor's: this is what the user is looking at.
    expect(f.gpu.render).toHaveBeenCalledWith(f.scene, f.camera);
    expect(f.orbit.update).toHaveBeenCalled();
    expect(f.layers.map((layer) => layer.visible)).toEqual(f.layers.map(() => true));
    // A marker that was already dark was never hidden, so it is never re-lit either.
    expect(f.dark.visible).toBe(false);
  });

  /**
   * §4.8.7 (**Q121**): a recording frame is this pass, not a second way of reading the canvas - so
   * the file and the screenshot button cannot disagree about what the viewport looks like, and the
   * only thing the take adds is the choice the 「录制辅助图层」 box makes.
   */
  it("gives a running take the same pass, with the layers or without them (§4.8.7, Q121)", () => {
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const f = viewportFixture();
    f.adapter.setRecordFrameTap(() => undefined, true);
    expect(f.adapter.recordFrameTo(f.canvas)).toBe(true);
    expect(f.gpu.render).toHaveBeenCalledWith(f.scene, f.camera);
    expect(f.drawn).toHaveLength(1);
    // Held out: the family disappears for the pass and is back the moment it ends, so the screen -
    // which is not what this canvas is - never loses a layer.
    expect(f.layers.every((layer) => layer.visible)).toBe(true);

    // Ticked in: the take is the screen, outline and wireframe and all, so not one of them is asked
    // to go dark. `render` is where the pass would have hidden them, so that is where the question
    // gets asked.
    f.gpu.render.mockClear();
    f.drawn.length = 0;
    f.adapter.setRecordFrameTap(() => undefined, false);
    expect(f.adapter.recordFrameTo(f.canvas)).toBe(true);
    let sawAll = false;
    f.gpu.render.mockImplementation(() => {
      sawAll = f.layers.every((layer) => layer.visible);
    });
    expect(f.adapter.recordFrameTo(f.canvas)).toBe(true);
    expect(sawAll).toBe(true);
    expect(f.drawn).toHaveLength(2);
    debug.mockRestore();
  });

  it("puts every layer back even when the GPU fails mid-pass", () => {
    const f = viewportFixture();
    f.gpu.render.mockImplementation(() => { throw new Error("GPU failure"); });

    expect(() => f.adapter.captureViewport(f.canvas)).toThrow("GPU failure");
    expect(f.layers.every((layer) => layer.visible)).toBe(true);
    expect(f.dark.visible).toBe(false);
    // The viewport's render target comes back, or the animation frame after this is black.
    expect(f.gpu.setRenderTarget).toHaveBeenLastCalledWith(f.original);
    expect(f.drawn).toHaveLength(0);
  });

  it("copies the drawing buffer, not the CSS box, and encodes it the way the screen does", () => {
    const f = viewportFixture({ size: [96, 40] });
    expect(f.adapter.captureViewport(f.canvas)).toBe(true);
    expect(f.canvas.width).toBe(96);
    expect(f.canvas.height).toBe(40);
    const target = f.gpu.setRenderTarget.mock.calls[0]?.[0] as THREE.WebGLRenderTarget;
    expect(target.width).toBe(96);
    expect(target.height).toBe(40);
    expect(target.samples).toBeGreaterThan(0);
    expect(target.texture.colorSpace).toBe(THREE.SRGBColorSpace);
    expect(f.gpu.readRenderTargetPixels).toHaveBeenCalledWith(target, 0, 0, 96, 40, expect.any(Uint8Array));
  });

  it("flips GPU rows the way §5.10 does", () => {
    const f = viewportFixture();
    expect(f.adapter.captureViewport(f.canvas)).toBe(true);
    expect(f.drawn).toHaveLength(1);
    const frame = f.drawn[0]!;
    // The mock paints GPU row 0 with 10: that is the bottom row of the picture.
    expect(frame[(height - 1) * width * 4]).toBe(10);
    expect(frame[0]).toBe(20);
  });

  it("keeps the target and the row buffer between clicks, and rebuilds them on a resize", () => {
    const f = viewportFixture();
    expect(f.adapter.captureViewport(f.canvas)).toBe(true);
    expect(f.adapter.captureViewport(f.canvas)).toBe(true);
    const first = f.gpu.setRenderTarget.mock.calls[0]?.[0];
    expect(f.gpu.setRenderTarget.mock.calls[2]?.[0]).toBe(first);

    // The window changed size, so both caches are the wrong shape now.
    f.gpu.getDrawingBufferSize = (out: THREE.Vector2) => out.set(width, height / 2);
    expect(f.adapter.captureViewport(f.canvas)).toBe(true);
    expect(f.gpu.setRenderTarget.mock.calls[4]?.[0]).not.toBe(first);
    expect(f.drawn[2]!.length).toBe(width * (height / 2) * 4);
  });

  it("refuses a frame with no pixels, a disposed adapter and a lost context", () => {
    const empty = viewportFixture({ size: [0, 0] });
    expect(empty.adapter.captureViewport(empty.canvas)).toBe(false);
    expect(empty.gpu.render).not.toHaveBeenCalled();
    expect(empty.layers.every((layer) => layer.visible)).toBe(true);

    const gone = viewportFixture();
    Object.assign(gone.adapter, { disposed: true });
    expect(gone.adapter.captureViewport(gone.canvas)).toBe(false);
    expect(gone.gpu.render).not.toHaveBeenCalled();

    const lost = viewportFixture({ lost: true });
    expect(lost.adapter.captureViewport(lost.canvas)).toBe(false);
    expect(lost.gpu.render).not.toHaveBeenCalled();
    expect(lost.drawn).toHaveLength(0);
    expect(lost.errors.join(" ")).toContain("context is lost");
  });
});

describe("aabb projection", () => {
  /** One 8 cm cube at a file position, measured by a 60° camera at the world origin. */
  function boxFixture(position: [number, number, number] = [0, 0, 2]) {
    const { adapter, nodes } = adapterFixture();
    Object.assign(adapter, { renderer: { getContext: () => ({ isContextLost: () => false }) } });
    adapter.syncScene(worldFrom([
      object(2, "cam", [
        transform(),
        { component_type: "Camera", properties: { type: "rgbd", fov: 60, near: 0.1, far: 100, resolution: [640, 480] } },
      ]),
      object(5, "apple", [transform(null, { position })]),
      object(6, "empty", [transform(null, { position: [0, 0, 1] })]),
    ]));
    nodes.get(5)!.add(new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.08, 0.08)));
    return { adapter, nodes };
  }

  it("projects a centred cube to a square rectangle at the image centre (§5.11)", () => {
    const result = boxFixture().adapter.projectObjectBox(5, 2)!;
    expect(result.width).toBe(640);
    expect(result.height).toBe(480);
    expect(result.visible).toBe(true);
    expect(result.clipped).toBe(false);
    // The near face at 1.96 m subtends the most, and a 4:3 image of square pixels
    // gives the same half span on both axes.
    const half = (0.04 / 1.96) / Math.tan(Math.PI / 6) * 240;
    expect(result.box!.x_min).toBeCloseTo(320 - half, 6);
    expect(result.box!.x_max).toBeCloseTo(320 + half, 6);
    expect(result.box!.y_min).toBeCloseTo(240 - half, 6);
    expect(result.box!.y_max).toBeCloseTo(240 + half, 6);
    // Metres in the frame the world file uses: the camera looks down its own +Z (§4.4).
    expect(result.aabb.min.map((value) => value.toFixed(2))).toEqual(["-0.04", "-0.04", "1.96"]);
    expect(result.aabb.max.map((value) => value.toFixed(2))).toEqual(["0.04", "0.04", "2.04"]);
  });

  it("keeps the file's axes: -X goes to the image right, +Y goes up it", () => {
    const result = boxFixture([-1.54, -0.5, 2]).adapter.projectObjectBox(5, 2)!;
    // The exact rectangle is `projectBox`'s job; what this pins is the axis mapping —
    // 1.54 m along the file's -X and 0.5 m along its -Y has to land right of and below
    // the image centre, because the optical frame is +X left and +Y up (§4.4).
    expect(result.visible).toBe(true);
    expect(result.box!.x_min).toBeGreaterThan(320);
    expect(result.box!.y_min).toBeGreaterThan(240);
    // Crossing the right edge is why it is clipped rather than lost.
    expect(result.clipped).toBe(true);
    expect(result.box!.x_max).toBe(640);
    expect(result.aabb.min.map((value) => value.toFixed(2))).toEqual(["-1.58", "-0.54", "1.96"]);
    expect(result.aabb.max.map((value) => value.toFixed(2))).toEqual(["-1.50", "-0.46", "2.04"]);
  });

  it("measures a box that is entirely behind the camera but keeps it invisible", () => {
    const result = boxFixture([0, 0, -2]).adapter.projectObjectBox(5, 2)!;
    // HTTP 200 with `visible: false`, not a 404: the object is really there (§5.11).
    expect(result.visible).toBe(false);
    expect(result.clipped).toBe(true);
    expect(result.box).toBeNull();
    expect(result.aabb.min.map((value) => value.toFixed(2))).toEqual(["-0.04", "-0.04", "-2.04"]);
  });

  it("follows the driven pose, so a trajectory moves the box", () => {
    const f = boxFixture([0, 0, 2]);
    const before = f.adapter.projectObjectBox(5, 2)!;
    f.nodes.get(5)!.position.set(0, 0, 4);
    const after = f.adapter.projectObjectBox(5, 2)!;
    expect(after.aabb.min.map((value) => value.toFixed(2))).toEqual(["-0.04", "-0.04", "3.96"]);
    expect(after.box!.x_max - after.box!.x_min).toBeLessThan(before.box!.x_max - before.box!.x_min);
  });

  it("has no box to report for an object with nothing to bound", () => {
    const f = boxFixture();
    // A bare Transform is an empty node: §3.7 gives it no placeholder geometry.
    expect(f.adapter.projectObjectBox(6, 2)).toBeNull();
    expect(f.adapter.projectObjectBox(99, 2)).toBeNull();
    expect(f.adapter.projectObjectBox(5, 99)).toBeNull();
    expect(f.adapter.projectObjectBox(5, 6)).toBeNull();
  });
});

describe("model file references", () => {
  function filesWith(paths: Record<string, string>, kind: LibraryFileKind = "model"): ModelFiles {
    const files = new Map(Object.entries(paths).map(([path, text]) => [path, new TextEncoder().encode(text).buffer as ArrayBuffer]));
    return modelFiles(3, kind, "meshes/robot.dae", files);
  }

  it("serves a bundled reference from the model directory through a blob URL (§6.4)", () => {
    const files = filesWith({ "meshes/arm.stl": "solid", "meshes/textures/albedo.png": "png" });
    expect(files.resolve("meshes/textures/albedo.png")).toMatch(/^blob:/);
    expect(files.resolve("file:meshes/arm.stl")).toBe(files.resolve("meshes/arm.stl"));
    expect(files.resolve("meshes/textures/albedo.png#view=1")).toBe(files.resolve("meshes/textures/albedo.png"));
  });

  it("resolves an unbundled reference through the library routes (§6.3)", () => {
    const files = filesWith({ "meshes/robot.dae": "<collada/>" });
    expect(files.resolve("extra/mesh.obj")).toBe("ols://model/3/extra/mesh.obj");
  });

  it("asks for the file route of the id space the directory came from (§6.11)", () => {
    const files = filesWith({ "meshes/robot.dae": "<collada/>" }, "robot");
    expect(files.resolve("extra/mesh.obj")).toBe("ols://robot/3/extra/mesh.obj");
  });

  it("refuses an escaping reference and leaves an absolute URL untouched", () => {
    const files = filesWith({});
    expect(files.resolve("../outside.obj")).toBe("../outside.obj");
    expect(files.resolve("http://elsewhere.test/x.png")).toBe("http://elsewhere.test/x.png");
  });

  it("keeps the directory of a nested entry for loader references", () => {
    const files = filesWith({ "meshes/sub/case.obj": "v 0 0 0" });
    expect(files.directoryOf("meshes/sub/case.obj")).toBe("meshes/sub/");
    expect(files.directoryOf("top.stl")).toBe("");
    expect(files.has("meshes/sub/case.obj")).toBe(true);
    expect(files.bytes("missing.obj")).toBeUndefined();
    expect(() => files.text("missing.obj")).toThrow("not part of model 3");
  });
});

describe("asset parsing", () => {
  function parseProbe() {
    const adapter = Object.assign(Object.create(ThreeRendererAdapter.prototype), {
      reported: new Set<string>(),
      callbacks: { onSelect: () => undefined, onPerformance: () => undefined, onError: () => undefined },
    }) as { parseAsset(files: ModelFiles, path: string): Promise<THREE.Object3D> };
    return adapter;
  }

  it.each(["Z_UP", "Y_UP", undefined])("undoes only Collada Z_UP normalization (%s)", async (upAxis) => {
    const adapter = parseProbe();
    const files = bundleOf(9, "meshes/arm.dae", "<collada/>");
    const scene = new THREE.Group();
    if (upAxis === "Z_UP") scene.rotation.x = -Math.PI / 2;
    vi.stubGlobal("DOMParser", class {
      parseFromString() { return { querySelector: () => (upAxis ? { textContent: upAxis } : null) }; }
    });
    const parse = vi.spyOn(ColladaLoader.prototype, "parse").mockImplementation(() => {
      const parsed = Object.assign({} as ReturnType<ColladaLoader["parse"]>, { scene });
      return parsed;
    });

    const result = await adapter.parseAsset(files, "meshes/arm.dae");

    // simWorld already carries the -pi/2 Z-up rotation, so it must not land twice (pit 4).
    expect(parse).toHaveBeenCalledWith("<collada/>", "meshes/");
    expect(result.quaternion.angleTo(new THREE.Quaternion())).toBeCloseTo(0);
  });

  it("applies the centimetre default only when an FBX declares no unit", async () => {
    const adapter = parseProbe();
    const root = new THREE.Group();
    const parse = vi.spyOn(FBXLoader.prototype, "parse").mockReturnValue(root);

    await expect(adapter.parseAsset(bundleOf(9, "arm.fbx", "fbx"), "arm.fbx")).resolves.toBe(root);
    expect(parse).toHaveBeenCalledWith(expect.any(ArrayBuffer), "");
    expect(root.scale.x).toBeCloseTo(0.01);
  });

  it("keeps an FBX that already declares metres", async () => {
    const adapter = parseProbe();
    const root = new THREE.Group();
    root.scale.set(1, 2, 1);
    vi.spyOn(FBXLoader.prototype, "parse").mockReturnValue(root);

    await adapter.parseAsset(bundleOf(9, "arm.fbx", "fbx"), "arm.fbx");
    expect(root.scale.y).toBeCloseTo(2);
  });

  it("refuses a mesh format the browser cannot parse", async () => {
    const adapter = parseProbe();
    await expect(adapter.parseAsset(bundleOf(9, "arm.usd", "usd"), "arm.usd")).rejects.toThrow("unsupported browser mesh format");
  });
});

describe("the viewport background colour (§4.4, Q145)", () => {
  it("clears to the colour logic.ts validated, #RRGGBB and #RGB alike", () => {
    const { adapter, scene } = adapterFixture();
    adapter.setBackgroundColor("#123456");
    expect((scene.background as THREE.Color).getHex()).toBe(0x123456);
    // THREE.Color parses the short shape too, so the renderer never has to expand it.
    adapter.setBackgroundColor("#0f0");
    expect((scene.background as THREE.Color).getHex()).toBe(0x00ff00);
  });

  it("touches only the clear colour, not the distance fog", () => {
    const { adapter, scene } = adapterFixture();
    const fog = new THREE.Fog(0x000000, 30, 120);
    scene.fog = fog;
    adapter.setBackgroundColor("#ffffff");
    expect((scene.background as THREE.Color).getHex()).toBe(0xffffff);
    // Q145 scopes the constant to the clear colour; the fog keeps its own hardcoded black.
    expect(scene.fog).toBe(fog);
  });
});

/**
 * §4.8.6 (**Q148**): "one box per object" is a rule about the measure, not only about how many
 * wireframes get drawn. `RobotAttachment` and `Transform.parent` both hang a child object *inside*
 * the parent's subtree, so a walk that measures "everything under this node" reports a box reaching
 * past the arm into the hand it is carrying - and the arm's wireframe on screen, `/get2DAABB`,
 * `frame()` and the selection outline all read that same measure.
 */
describe("one box per world object (规格 §4.8.6, Q148)", () => {
  const boxVisual = (name: string, translation: [number, number, number]): UrdfVisual => ({
    name,
    origin: { translation, quaternion: [0, 0, 0, 1] },
    geometry: { kind: "box", size: [0.1, 0.1, 0.1] },
    rgba: [0.5, 0.5, 0.5, 1],
  });

  /** An arm of two boxes half a metre apart: two links, one object, one box (§4.8.6 kept). */
  const linkArmRobot: UrdfRobot = {
    name: "arm",
    links: [
      { name: "base_link", visuals: [boxVisual("base_box", [0, 0, 0])] },
      { name: "wrist_3_link", visuals: [boxVisual("wrist_box", [0, 0, 0.5])] },
    ],
    joints: [{ name: "shoulder_pan_joint", type: "revolute", parent: "base_link", child: "wrist_3_link",
      origin: { translation: [0, 0, 0.5], quaternion: [0, 0, 0, 1] }, axis: [0, 0, 1],
      limit: [-3.14, 3.14], mimic: null }],
    roots: ["base_link"],
    meshFiles: [],
  };

  /** The hand: one box 0.4 m out along its mount frame, so it is unmissable in the parent's box. */
  const handRobot: UrdfRobot = {
    name: "gripper",
    links: [{ name: "robotiq_2f85_mount", visuals: [boxVisual("gripper_box", [0, 0.4, 0])] }],
    joints: [],
    roots: ["robotiq_2f85_mount"],
    meshFiles: [],
  };

  const mountOnWrist = (): WorldComponent => ({
    component_type: "RobotAttachment",
    properties: {
      parent_object_id: 1, parent_link: "wrist_3_link", connection_type: "fixed",
      transform: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], reference_frame: "parent_link" },
    },
  });

  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

  /** Arm and hand both fetchable, the world mounting the hand on the wrist link. */
  async function mounted(extra: unknown[] = []) {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    fixture.robots.set("robot:1", Promise.resolve(linkArmRobot));
    fixture.robots.set("robot:2", Promise.resolve(handRobot));
    fixture.adapter.setLibrary(ghostClient());
    fixture.adapter.syncScene(worldFrom([
      robotObject(1, "arm", null, ["shoulder_pan_joint"]),
      object(2, "gripper", [
        transform(1),
        { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/2", type: "robot" } } as WorldComponent,
        mountOnWrist(),
      ]),
      ...extra,
    ]));
    await settle();
    fixture.scene.updateMatrixWorld(true);
    errors.length = 0;
    const center = (id: number) => fixture.adapter.worldAabb(id)!.getCenter(new THREE.Vector3());
    return { ...fixture, errors, center };
  }

  it("keeps the hand the arm is carrying out of the arm's box", async () => {
    const { adapter, nodes, robotTrees, center } = await mounted();

    // The mount really did land inside the arm's subtree - without this the next two lines are free.
    expect(nodes.get(2)!.parent).toBe(robotTrees.get(1)!.tree.links.get("wrist_3_link"));

    const armBox = adapter.worldAabb(1)!;
    // One URDF is still one box: both of the arm's own links are inside it, half a metre apart.
    for (const link of ["base_link", "wrist_3_link"]) {
      expect(armBox.containsPoint(robotTrees.get(1)!.tree.links.get(link)!.getWorldPosition(new THREE.Vector3())))
        .toBe(true);
    }
    // And it stops there: the centre of the hand's own box is outside it.
    expect(armBox.containsPoint(center(2))).toBe(false);
    // Nothing is lost to the world: the whole-world measure walks every object node from its own
    // root, so the hand is covered even though the arm never measures it.
    expect(adapter.worldAabb()!.containsPoint(center(2))).toBe(true);
  });

  it("grows the child's box without growing its parent's", async () => {
    const { adapter, nodes, scene } = await mounted();
    const armBefore = adapter.worldAabb(1)!.clone();
    const handBefore = adapter.worldAabb(2)!.clone();

    // Ten times the hand is a measure the old walk could not miss.
    nodes.get(2)!.scale.setScalar(10);
    scene.updateMatrixWorld(true);

    expect(adapter.worldAabb(1)!.equals(armBefore)).toBe(true);
    const grown = adapter.worldAabb(2)!.getSize(new THREE.Vector3());
    const was = handBefore.getSize(new THREE.Vector3());
    expect(grown.x).toBeCloseTo(was.x * 10, 4);
  });

  it("keeps a prop parented to the hand out of both robots' boxes", async () => {
    // The §4.8.6 example: a wrist-mounted `model` prop, three levels deep in one subtree.
    const { adapter, nodes, center } = await mounted([
      object(3, "microwave_on_wrist", [transform(2, { position: [0, 0, 3] }),
        { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/30", type: "model" } } as WorldComponent]),
    ]);
    const prop = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.2, 0.2));
    prop.userData.renderContent = true;
    nodes.get(3)!.add(prop);
    adapter.trajView.refresh();

    expect(nodes.get(3)!.parent).toBe(nodes.get(2));
    const propCenter = center(3);
    expect(adapter.worldAabb(2)!.containsPoint(propCenter)).toBe(false);
    expect(adapter.worldAabb(1)!.containsPoint(propCenter)).toBe(false);
    // The prop's own box and the whole-world box both still hold it.
    expect(adapter.worldAabb(3)!.containsPoint(propCenter)).toBe(true);
    expect(adapter.worldAabb()!.containsPoint(propCenter)).toBe(true);
  });
});

/**
 * §4.8.6 (**Q149**): Q148 stopped the walk at another object's node, and the arm's wireframe still
 * stood off the metal, because the measure itself was the eight corners of each part's *local* box
 * carried through the joints. A URDF part is authored in its link's axes and a joint turns it out of
 * them, and the corners of a box are not the metal - on the shipped UR5e the pose the scene file
 * declares left 2.2 cm of that on the upper arm and 1.0 cm on the forearm. The measure is now taken
 * link by link and vertex by vertex, and `urdf_bounding_box_per_link` says only how the display
 * draws it: `worldAabb()` is still the union of exactly those link boxes, so `/get2DAABB`, the
 * selection outline, `frame()` and placement never see a different geometry than the screen does.
 */
describe("per-link exact bounding boxes (规格 §4.8.6, Q149)", () => {
  const R = 0.1;
  const LENGTH = 0.4;

  /**
   * 45° about the part's own axis. The facet step of a URDF cylinder is 15°, so the turned facet set
   * is the same set: the exact measure is provably `R` out, while the corners of its local box - a
   * square the metal never fills - are provably `R * √2`.
   */
  const turnedFacet = (() => {
    const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 4);
    return [q.x, q.y, q.z, q.w] as [number, number, number, number];
  })();

  const armRobot: UrdfRobot = {
    name: "tilt",
    links: [
      { name: "arm_link", visuals: [{ name: "bar", origin: { translation: [0, 0, 0], quaternion: turnedFacet },
        geometry: { kind: "cylinder", radius: R, length: LENGTH }, rgba: [0.5, 0.5, 0.5, 1] }] },
      { name: "tip_link", visuals: [{ name: "tip", origin: { translation: [0, 0, 0.3], quaternion: [0, 0, 0, 1] },
        geometry: { kind: "box", size: [0.1, 0.1, 0.1] }, rgba: [0.5, 0.5, 0.5, 1] }] },
      // An empty `<link>`: no geometry to measure, so nothing to draw - never a box at the origin.
      { name: "empty_link", visuals: [] },
    ],
    joints: [
      { name: "tip_joint", type: "revolute", parent: "arm_link", child: "tip_link",
      origin: { translation: [0, 0, 0.8], quaternion: [0, 0, 0, 1] }, axis: [0, 1, 0],
      limit: [-3.14, 3.14], mimic: null },
      { name: "empty_joint", type: "fixed", parent: "tip_link", child: "empty_link",
      origin: { translation: [0, 0, 0.2], quaternion: [0, 0, 0, 1] }, axis: [0, 0, 1], limit: null, mimic: null },
    ],
    roots: ["arm_link"],
    meshFiles: [],
  };

  /** The hand, mounted a full 0.4 m out so it cannot hide inside a 0.1 m link box. */
  const handRobot: UrdfRobot = {
    name: "gripper",
    links: [{ name: "hand_link", visuals: [{ name: "palm", origin: { translation: [0, 0.4, 0], quaternion: [0, 0, 0, 1] },
      geometry: { kind: "box", size: [0.1, 0.1, 0.1] }, rgba: [0.5, 0.5, 0.5, 1] }] }],
    joints: [],
    roots: ["hand_link"],
    meshFiles: [],
  };

  const helpers = (adapter: ThreeRendererAdapter): Map<number, THREE.Box3Helper> =>
    (adapter as unknown as { boxHelpers: Map<number, THREE.Box3Helper> }).boxHelpers;
  const linkHelpers = (adapter: ThreeRendererAdapter): Map<string, THREE.Box3Helper> =>
    (adapter as unknown as { linkHelpers: Map<string, THREE.Box3Helper> }).linkHelpers;

  /** The part's box measured the way Q149 replaced: its local corners, carried through the world matrix. */
  function cornerMeasure(linkNode: THREE.Object3D): THREE.Box3 {
    const mesh = (linkNode.children[0]!.children[0] as THREE.Mesh);
    mesh.geometry.computeBoundingBox();
    return mesh.geometry.boundingBox!.clone().applyMatrix4(mesh.matrixWorld);
  }

  async function stage(extra: Record<string, unknown>[] = []) {
    const errors: string[] = [];
    const fixture = adapterFixture(errors);
    fixture.robots.set("robot:1", Promise.resolve(armRobot));
    fixture.robots.set("robot:2", Promise.resolve(handRobot));
    fixture.adapter.setLibrary(ghostClient());
    fixture.adapter.syncScene(worldFrom([
      robotObject(1, "tilt", null, ["tip_joint"]),
      object(2, "gripper", [
        transform(1),
        { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/2", type: "robot" } } as WorldComponent,
        { component_type: "RobotAttachment", properties: {
          parent_object_id: 1, parent_link: "tip_link", connection_type: "fixed",
          transform: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], reference_frame: "parent_link" },
        } } as WorldComponent,
      ]),
      ...extra,
    ]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.scene.updateMatrixWorld(true);
    errors.length = 0;
    const center = (id: number) => fixture.adapter.worldAabb(id)!.getCenter(new THREE.Vector3());
    return { ...fixture, errors, center };
  }

  it("measures a freshly loaded URDF before its first render frame", async () => {
    const { adapter, robots, nodes, scene } = adapterFixture();
    robots.set("robot:1", Promise.resolve(armRobot));
    adapter.setLibrary(ghostClient());
    adapter.syncScene(worldFrom([robotObject(1, "tilt", null, ["tip_joint"])]));
    nodes.get(1)!.position.set(0.3, -0.2, 0.6);
    nodes.get(1)!.quaternion.setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 2);
    await adapter.readyObject(1);

    // Placement reads in the load continuation, before the render loop updates ancestors.
    const immediate = adapter.sceneAABB(1)!;
    expect(immediate).not.toBeNull();
    expect(immediate.min[2]).toBeCloseTo(0.4, 6);
    expect(immediate.max[2]).toBeCloseTo(1.75, 6);
    scene.updateMatrixWorld(true);
    expect(immediate).toEqual(adapter.sceneAABB(1));
  });

  it("refreshes joint ancestors when placement measures between render frames", async () => {
    const { adapter, robotTrees, nodes, scene } = await stage();
    const before = adapter.sceneAABB(1)!;
    nodes.get(1)!.position.set(0.4, -0.3, 0.6);
    setJointValue(robotTrees.get(1)!.tree.joints.get("tip_joint")!, Math.PI / 2);

    const immediate = adapter.sceneAABB(1)!;
    const immediateLinks = adapter.worldLinkBoxes(1);
    expect(immediate).not.toEqual(before);
    scene.updateMatrixWorld(true);
    expect(immediate).toEqual(adapter.sceneAABB(1));
    expect(immediateLinks).toEqual(adapter.worldLinkBoxes(1));
  });

  it("measures a turned part by the vertices it has, not the corners it has not", async () => {
    const { adapter, robotTrees } = await stage();
    const arm = adapter.worldLinkBoxes(1).find(({ link }) => link === "arm_link")!;
    const size = arm.box.getSize(new THREE.Vector3());
    // A facet ring turned by a whole number of its own steps is the same ring: the metal reaches
    // `R` out across its axis and `LENGTH` along it - and the tilt puts the URDF's z-up axis on
    // the world y, so that is the one axis the turn leaves alone.
    expect(size.x).toBeCloseTo(2 * R, 5);
    expect(size.z).toBeCloseTo(2 * R, 5);
    expect(size.y).toBeCloseTo(LENGTH, 5);
    // And that is the whole complaint: the same part measured from its local corners - the measure
    // Q148 left behind - is √2 looser across the facet, which is the wireframe standing off the arm.
    const loose = cornerMeasure(robotTrees.get(1)!.tree.links.get("arm_link")!).getSize(new THREE.Vector3());
    expect(loose.x).toBeGreaterThan(size.x + 0.05);
    expect(loose.z).toBeGreaterThan(size.z + 0.05);
    expect(loose.y).toBeCloseTo(size.y, 5);
    // And one link is one link: a URDF tree nests, so a walk from the base reaches everything below
    // it, and the display would draw the whole arm once per link. The metal below stays below.
    const tipCenter = robotTrees.get(1)!.tree.links.get("tip_link")!.children[0]!
      .getWorldPosition(new THREE.Vector3());
    expect(arm.box.containsPoint(tipCenter)).toBe(false);
    expect(adapter.worldLinkBoxes(1).find(({ link }) => link === "tip_link")!.box.containsPoint(tipCenter)).toBe(true);
  });

  it("answers with the union of the link boxes whichever way the display is set", async () => {
    const { adapter } = await stage();
    const links = adapter.worldLinkBoxes(1);
    expect(links.map(({ link }) => link)).toEqual(["arm_link", "tip_link"]);
    const union = new THREE.Box3();
    for (const { box } of links) union.union(box);
    // The one line that makes the display flag safe: what the page draws apart is what the API
    // reports together, byte for byte - not a second, looser measure of the same arm.
    expect(union.equals(adapter.worldAabb(1)!)).toBe(true);
  });

  it("gives a link with no geometry nothing to draw", async () => {
    const { adapter } = await stage();
    expect(adapter.worldLinkBoxes(1).some(({ link }) => link === "empty_link")).toBe(false);
    adapter.setBoundingBoxesEnabled(true);
    adapter.setLinkBoxesEnabled(true);
    expect(adapter.boxReport().drawnLinks).toEqual(["1/arm_link", "1/tip_link", "2/hand_link"]);
    expect(linkHelpers(adapter).has("1/empty_link")).toBe(false);
  });

  it("cuts the URDF open and leaves a plain model one box", async () => {
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const { adapter, nodes, scene } = await stage([object(9, "crate", [transform()])]);
    const crate = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2));
    crate.userData.renderContent = true;
    nodes.get(9)!.add(crate);
    scene.updateMatrixWorld(true);

    adapter.setBoundingBoxesEnabled(true);
    adapter.setLinkBoxesEnabled(true);

    // The URDF is drawn link by link, and its own union box comes off the screen - a box would
    // otherwise sit inside its own box. The model has no links, so it keeps the one it always had.
    expect(adapter.boxReport()).toEqual({
      enabled: true, drawn: [9], skipped: [], perLink: true,
      drawnLinks: ["1/arm_link", "1/tip_link", "2/hand_link"],
    });
    expect(helpers(adapter).has(1)).toBe(false);
    expect(scene.children.filter((child) => child.type === "Box3Helper")).toHaveLength(4);
    // Each wireframe is the link's box, read off the same measure the union is made of.
    for (const { link, box } of adapter.worldLinkBoxes(1)) {
      expect(linkHelpers(adapter).get(`1/${link}`)!.box.equals(box)).toBe(true);
    }

    // And the flag is a display switch: with it off the arm is one box again, the same box.
    const union = adapter.worldAabb(1)!.clone();
    adapter.setLinkBoxesEnabled(false);
    expect(adapter.boxReport()).toEqual({
      enabled: true, drawn: [1, 2, 9], skipped: [], perLink: false, drawnLinks: [],
    });
    expect(helpers(adapter).get(1)!.box.equals(union)).toBe(true);
    expect(linkHelpers(adapter).size).toBe(0);
    debug.mockRestore();
  });

  it("keeps the hand it carries out of the arm's link boxes too", async () => {
    const { adapter, nodes, robotTrees, center } = await stage();
    // The mount really did land inside the arm's subtree, in the very link the display draws apart.
    expect(nodes.get(2)!.parent).toBe(robotTrees.get(1)!.tree.links.get("tip_link"));
    for (const { box } of adapter.worldLinkBoxes(1)) {
      expect(box.containsPoint(center(2))).toBe(false);
    }
    expect(adapter.worldAabb(1)!.containsPoint(center(2))).toBe(false);
    // The hand is not lost: it is its own object, and its own link gets its own box.
    expect(adapter.worldLinkBoxes(2).map(({ link }) => link)).toEqual(["hand_link"]);
  });

  it("wires the constant to the layer that draws it", () => {
    // `main.ts` wires the DOM on import, so the only surface it has is the source: the box layer
    // reads the feature key and the checkbox, and the shape underneath them reads the constant.
    const mainSource = readFileSync(new URL("./main.ts", import.meta.url), "utf-8");
    const start = mainSource.indexOf("function renderBoxes(): void {");
    expect(start, "main.ts no longer has a renderBoxes()").toBeGreaterThanOrEqual(0);
    const body = mainSource.slice(start, mainSource.indexOf("\n}", start));
    expect(body).toContain("boundingBoxesEnabled(state.features, state.boxes)");
    expect(body).toContain("urdfLinkBoxesEnabled(state.runtime)");
  });

  it("refuses to answer with the box a link had before its joint turned", async () => {
    const { adapter, robotTrees, scene } = await stage();
    const before = adapter.worldAabb(1)!.clone();

    setJointValue(robotTrees.get(1)!.tree.joints.get("tip_joint")!, Math.PI / 2);
    scene.updateMatrixWorld(true);

    const after = adapter.worldAabb(1)!;
    expect(after.equals(before)).toBe(false);
    // The vertex measure is cached per mesh, so this is the assertion that says the cache keys on
    // the pose and not on the mesh: the metal swung 0.42 m, and a stale box would not hold it.
    const tip = robotTrees.get(1)!.tree.links.get("tip_link")!.children[0]!;
    expect(after.containsPoint(tip.getWorldPosition(new THREE.Vector3()))).toBe(true);
    expect(before.containsPoint(tip.getWorldPosition(new THREE.Vector3()))).toBe(false);
  });
});
