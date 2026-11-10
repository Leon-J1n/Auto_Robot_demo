/**
 * Three.js view of the browser-owned world (spec v2.1 §3, §4.1, §6.8).
 *
 * The scene graph mirrors `Transform.parent`, poses are read as xyzw (§2.8), and
 * every piece of geometry comes from the object library - an object without a
 * `MetadataRef` renders as an empty node plus its component visualisation, never
 * as a placeholder cube (§3.7).
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { ColladaLoader } from "three/addons/loaders/ColladaLoader.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import type { CameraProps, LightProps, LocalAxesProps, RobotDriverProps, Vec3 } from "../../contracts/types";
import type { World, WorldObjectNode } from "./world";
import { cameraFrustumSegments, cameraRig, localAxesRig, normalizeObjSource, projectBox, shadowExtentsOf, shadowRangeOf, type CameraRig, type ProjectedBox } from "./logic";
import { LibraryClient, LibraryError, normalizeModelPath, resolveMetadataTarget } from "./library";
import type { LibraryFileKind, MetadataTarget } from "./library";
import { applyDeclaredJointPositions, declaredJointPositions, type TrajView } from "./drivers";
import { presetLightSources } from "./lighting";
import type { LightingPreset, PresetLightSource } from "./lighting";
import { buildUrdfTree, parseUrdf, toolTipLinks, type Rgba, type UrdfJointHandle, type UrdfRobot, type UrdfTree } from "./urdf";

export interface RendererCallbacks {
  onSelect(objectId: number | null): void;
  onPerformance(fps: number, renderMs: number): void;
  onError(message: string): void;
  /**
   * One object's URDF has landed, joints and all. The subtree arrives at the angles the world
   * file declares, so a clip that reached the stage while the asset was still fetching has to
   * re-apply its frame - and can only now measure a tool centre point to sweep waypoints with
   * (§8.5, §7.3 **Q101**).
   */
  onRobotTree(objectId: number): void;
}

/** Downscale bound for a sensor pass; `NATIVE_SENSOR` asks for the sensor's own resolution. */
export interface SensorLimits {
  maxWidth: number;
  maxHeight: number;
}

export const NATIVE_SENSOR: SensorLimits = { maxWidth: Number.POSITIVE_INFINITY, maxHeight: Number.POSITIVE_INFINITY };

/**
 * §4.8.5 (**Q117**): what a shadow costs. A `Directional` and a `Spot` render one square depth
 * map; a `Point` renders six faces, so that family gets the smaller map - its price per frame is
 * six scene passes, not the texel count.
 */
export const SHADOW_MAP_SIZE = 1024;
export const SHADOW_CUBE_MAP_SIZE = 512;
/** Metres a shadow volume reaches past the world box, so a caster's own silhouette is not its rim. */
export const SHADOW_MARGIN = 0.25;
/** The fit walks every geometry bounding box, so it runs on events and at most this often. */
export const SHADOW_FIT_INTERVAL_MS = 250;
/** Stripes on the caster itself are depth-map acne; a bias in world units moves it off. */
export const SHADOW_NORMAL_BIAS = 0.02;
/**
 * §4.8.6 (**Q119**): the wireframe that draws one box per world object. Green, because yellow is
 * the selection outline (§4.5) and sky blue is the camera frustum (§4.4) - three families on one
 * stage have to be told apart at a glance.
 */
export const BOX_WIRE_COLOR = 0x4ade80;

/**
 * §4.8.7 (**Q121**): how often a running recording gets a frame. The stream is `captureStream(0)`,
 * so the file is made of exactly the frames this loop pushes. A timed stream would sample the proxy
 * canvas on its own clock, and a sample taken before this pass wrote it makes `recordFrames` stop
 * being the truth about the file - timed capture does work on this Chromium, and the older note here
 * ("delivers no frames at all") was read off the 0-byte takes the software GL stack produced. The cap
 * is also what keeps the take's extra viewport pass from costing more than 30 renders a second.
 */
export const RECORD_INTERVAL_MS = 1000 / 30;

/** What the recording layer is, as one line the page and the probes read (§4.8.7). */
export interface RecordReport {
  recording: boolean;
  excludesOverlays: boolean;
  frames: number;
  intervalMs: number;
}

/** One metric depth map in §5.10 order: row major, top row first. */
export interface SensorDepthFrame {
  width: number;
  height: number;
  meters: Float32Array;
}

/** One projected AABB (§5.11): the world box plus its pixel rectangle. */
export interface SensorBoxFrame extends ProjectedBox {
  width: number;
  height: number;
  aabb: { min: Vec3; max: Vec3 };
}

/** One projected light's share of the rig, and how much geometry takes part (§4.8.5, §12.19). */
export interface ShadowReport {
  enabled: boolean;
  casters: Array<{ type: string; mapSize: number; near: number; far: number }>;
  castMeshes: number;
  receiveMeshes: number;
}

/**
 * The wireframe layer as a line the page can read (§4.8.6, §12.20). Which ids got a box, and which
 * got none because §4.5's measure has nothing to measure - an object whose render content is only
 * editor layers. Without `skipped` a box nobody drew and an object that never had one look the
 * same on screen.
 */
export interface BoxReport {
  enabled: boolean;
  drawn: number[];
  skipped: number[];
  /** §4.8.6 (**Q149**): whether URDF objects show one box per link instead of one per object. */
  perLink: boolean;
  /** The link boxes on screen in that mode, as `<objectId>/<link>`; empty while boxes show per object. */
  drawnLinks: string[];
}

/**
 * The one conversion between the world file and the render frame: -90° about X, because
 * the file is Z-up while Three.js is Y-up, so three (x, y, z) is file (x, -z, y). Every
 * node of the document gets it by hanging under `simWorld`; the axes at the world origin
 * hang on `scene` beside that group, so they carry the same tilt themselves - without it
 * the arm that points up on screen is the green one, and the world frame then disagrees
 * with every `LocalAxes` rig in the scene (场景文件描述 §4.9: a body with an identity
 * quaternion has its blue +Z axis 朝天). §4.4, **Q135**.
 */
export const SIM_WORLD_TILT = -Math.PI / 2;

/** §5.11 reports `aabb_world` in the frame the caller writes its world files in. */
function toSceneBox(min: THREE.Vector3, max: THREE.Vector3): { min: Vec3; max: Vec3 } {
  return { min: [min.x, -max.z, min.y], max: [max.x, -min.z, max.y] };
}

/** §4.9 (Q130): the three directions of an object's own frame, in that object's coordinates. */
const LOCAL_AXES: Array<{ key: "x" | "y" | "z"; label: string; unit: Vec3 }> = [
  { key: "x", label: "X", unit: [1, 0, 0] },
  { key: "y", label: "Y", unit: [0, 1, 0] },
  { key: "z", label: "Z", unit: [0, 0, 1] },
];

const DEPTH_VERTEX = `varying float viewDepth; void main() {
  vec4 positionView = modelViewMatrix * vec4(position, 1.0);
  viewDepth = -positionView.z; gl_Position = projectionMatrix * positionView;
}`;

const CSS_COLOR = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const BLOB_MIME: Record<string, string> = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
  bmp: "image/bmp", tga: "image/x-tga", dds: "image/vnd-ms.dds", exr: "image/x-exr",
  ktx: "image/ktx", ktx2: "image/ktx2", bin: "application/octet-stream",
};

function suffixOf(path: string): string {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

function lightColor(raw: unknown): THREE.Color {
  const color = new THREE.Color(0xffffff);
  if (typeof raw === "string" && CSS_COLOR.test(raw.trim())) color.set(raw.trim());
  return color;
}

/**
 * The files of one model directory (§6.3). A reference found inside a file stays
 * inside that directory: it is served from the bundle through a `blob:` URL or
 * re-resolved against the library routes, never against an arbitrary URL and
 * never with a custom header (§6.4, §6.9).
 */
export class ModelFiles {
  private readonly blobUrls = new Map<string, string>();

  constructor(
    readonly modelId: number,
    /** which id space this directory came from; `/file` needs it as `type` (§6.11) */
    readonly kind: LibraryFileKind,
    readonly entry: string,
    readonly isOnCos: boolean,
    private readonly client: LibraryClient,
    private readonly files: Map<string, ArrayBuffer>,
  ) {}

  directoryOf(path: string): string {
    const index = path.lastIndexOf("/");
    return index < 0 ? "" : path.slice(0, index + 1);
  }

  has(path: string): boolean {
    return this.files.has(path);
  }

  bytes(path: string): ArrayBuffer | undefined {
    return this.files.get(path);
  }

  text(path: string): string {
    const bytes = this.files.get(path);
    if (!bytes) throw new LibraryError("DEPENDENCY_FETCH_FAILED", `${path} is not part of model ${this.modelId}`, 424, [path]);
    return new TextDecoder().decode(bytes);
  }

  /** Loader manager that maps every relative reference this model makes. */
  manager(): THREE.LoadingManager {
    const manager = new THREE.LoadingManager();
    manager.resolveURL = (url: string) => this.resolve(url);
    return manager;
  }

  resolve(uri: string): string {
    const clean = uri.replace(/^file:/i, "").split("#")[0]!.split("?")[0]!;
    if (/^(blob:|data:|https?:)/i.test(clean)) return uri;
    let path: string;
    try {
      path = normalizeModelPath(clean);
    } catch {
      // An escaping reference is refused; the loader fails on it and the caller
      // reports the missing dependency (§6.4).
      return uri;
    }
    const bytes = this.files.get(path);
    if (bytes) return this.blobUrl(path, bytes);
    return this.client.fileUrl(this.modelId, path, this.isOnCos, this.kind);
  }

  private blobUrl(path: string, bytes: ArrayBuffer): string {
    const existing = this.blobUrls.get(path);
    if (existing) return existing;
    // The session owns the model, so the object URL lives as long as its cache (§6.7).
    const created = URL.createObjectURL(new Blob([bytes], { type: BLOB_MIME[suffixOf(path)] ?? "application/octet-stream" }));
    this.blobUrls.set(path, created);
    return created;
  }
}

/** Deep copy that owns its geometry and materials, so one view cannot break another. */
function cloneAsset(template: THREE.Object3D): THREE.Object3D {
  const clone = template.clone(true);
  clone.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh) return;
    mesh.geometry = mesh.geometry.clone();
    mesh.material = Array.isArray(mesh.material)
      ? mesh.material.map((material) => material.clone())
      : mesh.material.clone();
  });
  return clone;
}

/**
 * §4.8.5 (**Q117**): library geometry is the only thing that takes part in a shadow. The two flags
 * go on the shared template, so `cloneAsset` carries them onto every instance, every URDF link and
 * every mounted tool without each assembly path having to remember to ask.
 */
function markShadowRoles(object: THREE.Object3D): THREE.Object3D {
  object.traverse((child) => {
    if (!(child as THREE.Mesh).isMesh) return;
    child.castShadow = true;
    child.receiveShadow = true;
  });
  return object;
}

/** A box with a NaN or infinite corner cannot be unioned into anything; it only poisons it. */
function isFiniteBox(box: THREE.Box3): boolean {
  return [box.min.x, box.min.y, box.min.z, box.max.x, box.max.y, box.max.z].every(Number.isFinite);
}

/** STL carries no material, so the colour the URDF declares for its link is used. */
function tintAsset(object: THREE.Object3D, [r, g, b, a]: Rgba): void {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh) return;
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const material of materials) {
      const standard = material as THREE.MeshStandardMaterial;
      // Same convention as the rest of the renderer: rgb in the working colour space.
      if (standard.color) standard.color.setRGB(r, g, b);
      standard.opacity = a;
      standard.transparent = a < 1;
    }
  });
}

/**
 * A mesh without UV cannot sample a texture: the shader reads uv (0, 0), so the whole
 * surface is multiplied by one texel. The URDF→OBJ chain leaves `vt` out of the robot
 * meshes while their MTL still declares `map_Kd`, so the declared diffuse has to stand
 * alone there (§6.5).
 */
function dropUnusableTextures(object: THREE.Object3D): void {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh || mesh.geometry?.attributes.uv) return;
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const material of materials) {
      const standard = material as THREE.MeshStandardMaterial;
      if (!standard.map) continue;
      standard.map = null;
      standard.needsUpdate = true;
    }
  });
}

/** §6.8: `forceOverrideColor` replaces the material colour and drops its texture. */
const HEX_COLOR = /^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/;

function overrideAssetColor(object: THREE.Object3D, raw: string): boolean {
  const value = raw.trim();
  const hex = HEX_COLOR.exec(value);
  if (!hex) return false;
  const color = new THREE.Color();
  color.set(`#${hex[1]}`);
  // Three.js parses `#RRGGBB` but not an 8-digit hex, so the alpha is taken here.
  const opacity = hex[2] ? Number.parseInt(hex[2], 16) / 255 : null;
  object.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh) return;
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const material of materials) {
      const standard = material as THREE.MeshStandardMaterial;
      if (standard.color) standard.color.copy(color);
      if (opacity !== null) {
        standard.opacity = opacity;
        standard.transparent = opacity < 1;
      }
      standard.map = null;
      standard.needsUpdate = true;
    }
  });
  return true;
}

/**
 * Is `node` `ancestor` itself or one of its descendants? Scene nodes re-parent on every
 * Attach (§8.6), so the only reliable answer is to walk up from the node.
 */
function isWithin(node: THREE.Object3D, ancestor: THREE.Object3D): boolean {
  for (let current: THREE.Object3D | null = node; current !== null; current = current.parent) {
    if (current === ancestor) return true;
  }
  return false;
}

/**
 * Turn an instance into the translucent ghost of §7.3 (**Q96**, opacity from **Q97**).
 * The stamp is what makes it an editor layer (`isEditorOnly`), and `depthWrite` has to
 * go: translucent faces of one arm would occlude each other and the ghost would read as
 * broken geometry instead of a pose.
 */
function ghostAsset(object: THREE.Object3D, opacity: number): void {
  object.traverse((child) => {
    child.userData.ghost = true;
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh) return;
    // §4.8.5 (**Q117**): the clone is a copy of a caster, so its inherited flags go with it -
    // a preview of a pose must not drop a solid shadow the pose itself never cast.
    mesh.castShadow = false;
    mesh.receiveShadow = false;
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const material of materials) {
      const standard = material as THREE.MeshStandardMaterial;
      standard.opacity = opacity;
      standard.transparent = true;
      standard.depthWrite = false;
    }
  });
}

/**
 * One trajectory's waypoint layer (§7.3, Q101). The geometry and material are held
 * alongside because every sphere of a layer shares them: radius and colour describe the
 * layer, not a point, and changing either means replacing both.
 */
interface WaypointLayer {
  mesh: THREE.InstancedMesh;
  geometry: THREE.SphereGeometry;
  material: THREE.MeshBasicMaterial;
  /** which clip these points came from, for the colour and the checkbox's two states */
  trajectoryIndex: number;
  /** the leaf arm whose end effector these points trace */
  objectId: number;
  /** how many instances the shared buffer holds; a longer path needs a new mesh */
  capacity: number;
  radius: number;
  color: number;
}

/**
 * The sampled path of one leaf arm in one clip (§7.3, Q101). `key` names the layer, because
 * a clip can carry two disconnected arms and each of them gets its own string of spheres.
 */
export interface WaypointPath {
  key: string;
  trajectoryIndex: number;
  objectId: number;
  color: number;
  points: readonly Vec3[];
}

export class ThreeRendererAdapter {
  private readonly scene = new THREE.Scene();
  private readonly simWorld = new THREE.Group();
  private readonly camera = new THREE.PerspectiveCamera(52, 1, 0.01, 2000);
  private readonly renderer: THREE.WebGLRenderer;
  private readonly orbit: OrbitControls;
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointer = new THREE.Vector2();
  private readonly nodes = new Map<number, THREE.Object3D>();
  private readonly objects = new Map<number, WorldObjectNode>();
  private readonly signatures = new Map<number, string>();
  // `model` and `robot` ids are separate spaces (§6.11), so every cache is keyed by
  // `<kind>:<id>`; keying by the number alone would serve robot 0 for model 0.
  private readonly models = new Map<string, Promise<ModelFiles>>();
  private readonly templates = new Map<string, Promise<THREE.Object3D>>();
  private readonly robots = new Map<string, Promise<UrdfRobot>>();
  // The per-instance joint nodes a trajectory drives (§8.4), keyed by object.
  private readonly robotTrees = new Map<number, { robot: UrdfRobot; tree: UrdfTree }>();
  // The segment-end ghosts (§7.3, Q96/Q98): one second URDF instance per driven robot, so
  // an arm and the gripper mounted on it preview their own end-of-step poses. Each is
  // deliberately absent from `nodes` and `robotTrees` so nothing outside the viewport sees it.
  private readonly ghosts = new Map<number, UrdfTree>();
  private readonly ghostLoading = new Map<number, Promise<boolean>>();
  // The in-flight `loadModel` per object id, so `readyObject` can await an object's
  // geometry before measuring it (§4.6.1 placement bounds); a settled or dropped
  // entry means the geometry is either on the node or was abandoned as stale.
  private readonly loading = new Map<number, Promise<void>>();
  // Bumped whenever a ghost goes away, so an in-flight build of a ghost nobody wants
  // any more disposes of itself instead of landing on the stage.
  private readonly ghostGeneration = new Map<number, number>();
  // The waypoint spheres of each clip in the batch (§7.3, Q101): one InstancedMesh per
  // (clip, leaf arm) pair, hung under `simWorld` and deliberately absent from `nodes`.
  private readonly waypoints = new Map<string, WaypointLayer>();
  /** scratch, because `toolTipPosition` runs once per sampled frame of every clip */
  private readonly scratchVector = new THREE.Vector3();
  private readonly waypointPoint = new THREE.Vector3();
  private readonly simMatrix = new THREE.Matrix4();
  /** scratch, for the world position of one light's target during a shadow fit (§4.8.5) */
  private readonly shadowTarget = new THREE.Vector3();
  /** scratch, for reading a sphere's coordinates back out of its instance matrix */
  private readonly layerMatrix = new THREE.Matrix4();
  private readonly clock = new THREE.Clock();
  private readonly pressed = new Set<string>();
  private readonly reported = new Set<string>();
  private library: LibraryClient | null = null;
  private selectionBox: THREE.Box3Helper | null = null;
  private selectedId: number | null = null;
  /** §4.4 (Q105/Q106): whether each marker family shows with no selection behind it. */
  private cameraFrustumAlways = false;
  private lightMarkerAlways = false;
  /**
   * §4.9 with §4.8.8 (**Q132**): whether the local-axes layer shows at all. Still not a pin over
   * selection-only markers like the two flags above - these axes are declared per object, so when the
   * layer is on it is on for every object that declares one. It starts closed now because the layer's
   * default look is *no axes*: the toolbar checkbox opens it, and a page that never reached that
   * checkbox draws none.
   */
  private localAxesEnabled = false;
  /**
   * §4.4 (**Q131**): the two pieces of viewport furniture - the ground grid and the axes drawn at
   * the world origin. Both are `Helper` objects, so `isEditorOnly()` already keeps them out of every
   * capture and every extent; these two keys decide only whether the user sees them. They hang on
   * `scene` rather than on an object node, so `applyOverlayVisibility()` never touches them and each
   * setter below is the single writer of that `visible`.
   */
  private readonly gridHelper: THREE.GridHelper;
  private readonly axesHelper: THREE.AxesHelper;
  private gridEnabled = true;
  private worldAxesEnabled = true;
  /**
   * §4.8.1 (Q114 ③): the preset the stage is lit with instead of the file's lights.
   * `null` is "Scene lights" - the default, and the only state the world document knows.
   */
  private lightingOverride: LightingPreset | null = null;
  private readonly overrideLights = new THREE.Group();
  /**
   * §4.8.5 (**Q117**): whether the lights on the stage drop shadows, and when their volumes were
   * last fitted to the world box. The fit walks every geometry bounding box, so it runs on events
   * plus a clock - not every frame (§4.4 keeps that walk for the pick ray and the outline).
   */
  private shadowsEnabled = true;
  private shadowRigDirty = true;
  private lastShadowFit = 0;
  /**
   * §4.8.6 (**Q119**): one `Box3Helper` per world object, hung off `scene` exactly like the
   * selection outline, and refreshed on the same clock as the shadow fit for the same reason -
   * `worldAabb()` walks every geometry box, so neither walk belongs in the render loop. Nothing is
   * walked at all while the layer is off, which is how a checkbox nobody clicked costs zero.
   */
  private boxesEnabled = false;
  private readonly boxHelpers = new Map<number, THREE.Box3Helper>();
  /**
   * §4.8.6 (**Q149**): `urdf_bounding_box_per_link`. With it on, a URDF object gets one wireframe
   * per link instead of one for the whole object, and the per-object helpers go away - the two
   * modes never draw at the same time, because a link box and its own union on screen is a box
   * inside a box. Plain models have no links, so they keep the single box in either mode.
   */
  private linkBoxesEnabled = false;
  private readonly linkHelpers = new Map<string, THREE.Box3Helper>();
  /** The ids the last walk could not measure (§4.5's corollary), kept so a report reads honestly. */
  private boxSkipped: number[] = [];
  /**
   * The exact world box of one mesh, kept until that mesh's world matrix or its geometry changes
   * (**Q149**). A URDF link's silhouette is measured by transforming its vertices, which is the
   * only way that does not over-state a rotated part; the walk is otherwise the same, so a static
   * stage pays for the vertices once and the 250 ms clock afterwards reads the cache.
   */
  private readonly exactBoxes = new WeakMap<THREE.Object3D, {
    matrix: THREE.Matrix4;
    geometry: THREE.BufferGeometry;
    box: THREE.Box3;
  }>();
  private lastBoxSync = 0;
  private lastBoxReport = "";
  /**
   * §4.8.7 (**Q121**): the recording's frame tap, installed by the page when a take starts and
   * cleared when it stops. A delivered frame is its own §4.4 pass, so the editor layer is decided
   * frame by frame and the screen never changes: nothing is hidden "for the take", and a checkbox
   * clicked mid-take is honored by the next frame rather than at the next stop-and-restart.
   */
  private recordTap: (() => void) | null = null;
  private recordExcludesOverlays = false;
  private lastRecordFrame = 0;
  private recordFrames = 0;
  private lastRecordReport = "";
  private selectionInteractionEnabled = true;
  /** the `viewport.orbit` flag, remembered so selection can be turned off without losing it */
  private orbitEnabled = true;
  private animation = 0;
  private frames = 0;
  private frameWindow = performance.now();
  private disposed = false;
  private sensorTarget: THREE.WebGLRenderTarget | null = null;
  private sensorFloatTarget: THREE.WebGLRenderTarget | null = null;
  private sensorPixels: Uint8Array | null = null;
  private sensorFloatPixels: Float32Array | null = null;
  private sensorImage: ImageData | null = null;
  // The viewport screenshot's own copies of the same three (§4.4, Q108): sharing the sensor
  // ones would make every window resize recreate the sensor's `ImageData` cache.
  private viewportTarget: THREE.WebGLRenderTarget | null = null;
  private viewportPixels: Uint8Array | null = null;
  private viewportImage: ImageData | null = null;
  private readonly sensorCamera = new THREE.PerspectiveCamera();
  private readonly sensorDepth = new THREE.ShaderMaterial({
    uniforms: { nearPlane: { value: 0.01 }, farPlane: { value: 100 } },
    vertexShader: DEPTH_VERTEX,
    fragmentShader: `uniform float nearPlane; uniform float farPlane; varying float viewDepth;
      void main() { float d = clamp((viewDepth - nearPlane) / (farPlane - nearPlane), 0.0, 1.0);
      gl_FragColor = vec4(vec3(d), 1.0); }`,
    side: THREE.DoubleSide,
  });
  /** Metres along the optical axis, straight into a float32 attachment (§5.10). */
  private readonly sensorMeters = new THREE.ShaderMaterial({
    vertexShader: DEPTH_VERTEX,
    fragmentShader: `varying float viewDepth; void main() { gl_FragColor = vec4(viewDepth); }`,
    side: THREE.DoubleSide,
  });

  constructor(private readonly canvas: HTMLCanvasElement, private readonly callbacks: RendererCallbacks) {
    this.scene.background = new THREE.Color(0x000000);
    // The world is Z-up while Three.js is Y-up. The conversion happens on the
    // outermost group, so every node keeps the local pose the world file describes.
    // The two pieces of viewport furniture hang outside this group (§4.4): the grid needs
    // no conversion, the world-origin axes do - see `axesHelper.rotation.x` below.
    this.simWorld.rotation.x = SIM_WORLD_TILT;
    this.scene.add(this.simWorld);
    // §4.7 (**Q140**): three's line/point thresholds are world metres, and 1 is a metre of slack
    // around every segment. `selectable()` already refuses a line as a selection; zeroing these
    // stops the raycaster building the phantom region in the first place -- a wireframe asset
    // measured 3403 hits per click before this line, 0 after.
    this.raycaster.params.Line.threshold = 0;
    this.raycaster.params.Points.threshold = 0;
    // The preset lights hang under the same conversion, in a group that is not part of
    // the scene document, so `syncScene` never prunes them and `World` never sees them.
    this.simWorld.add(this.overrideLights);
    this.scene.fog = new THREE.Fog(0x000000, 30, 120);
    this.camera.position.set(7, 5, 8);
    this.renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: false, powerPreference: "high-performance",
    });
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    // §4.8.5 (Q117): one square depth map per projecting light, filtered, drawn by the same pass
    // that draws the frame. `setShadowsEnabled` is what turns it off; nothing here is a document
    // field, because §4.5 reads three `Light` fields and a shadow is not one of them.
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.orbit = new OrbitControls(this.camera, canvas);
    this.orbit.enableDamping = true;
    this.orbit.target.set(0, 0.8, 0);
    this.gridHelper = new THREE.GridHelper(80, 80, 0x405064, 0x252e38);
    this.gridHelper.material.opacity = 0.72;
    this.gridHelper.material.transparent = true;
    this.gridHelper.visible = this.gridEnabled;
    this.axesHelper = new THREE.AxesHelper(1.5);
    // The grid needs no conversion - its own XZ plane is already the file's z=0 floor - but the
    // arms do: outside `simWorld` they would stand in the render frame, and the arm pointing up
    // would be the green one. The same tilt makes blue the file's +Z, 朝天, like every `LocalAxes`.
    this.axesHelper.rotation.x = SIM_WORLD_TILT;
    this.axesHelper.visible = this.worldAxesEnabled;
    this.scene.add(this.gridHelper, this.axesHelper);
    canvas.addEventListener("pointerdown", this.pick);
    canvas.addEventListener("webglcontextlost", this.contextLost);
    window.addEventListener("keydown", this.keyDown);
    window.addEventListener("keyup", this.keyUp);
    this.resize();
    this.render();
  }

  /** The library client exists once `/api/runtimeConfig` has been read (§9.1). */
  setLibrary(library: LibraryClient): void {
    this.library = library;
  }

  /**
   * What a trajectory is allowed to touch (§8.4): the node of an object, the
   * joint handles of its URDF, and a matrix refresh - never the world itself.
   * Every query reads the live maps, so a view handed out before a `syncScene`
   * cannot pin a disposed node tree.
   */
  get trajView(): TrajView {
    return {
      object: (objectId) => this.nodes.get(objectId) ?? null,
      joints: (objectId) => this.robotTrees.get(objectId)?.tree.joints ?? null,
      robot: (objectId) => this.robotTrees.get(objectId) ?? null,
      root: () => this.simWorld,
      worldParent: (objectId) => {
        const object = this.objects.get(objectId);
        return object ? this.worldParentOf(object) : null;
      },
      refresh: () => this.scene.updateMatrixWorld(true),
    };
  }

  syncScene(world: World): void {
    const incoming = new Set(world.objects.map((object) => object.id));
    this.retainGhosts(incoming);
    for (const [id, node] of [...this.nodes]) {
      if (incoming.has(id)) continue;
      node.removeFromParent();
      this.disposeObject(node);
      this.nodes.delete(id);
      this.objects.delete(id);
      this.signatures.delete(id);
      this.robotTrees.delete(id);
    }
    // Two passes: `objects[]` may list a child before its parent (§3.6).
    for (const object of world.objects) {
      if (this.nodes.has(object.id)) continue;
      const node = new THREE.Group();
      node.name = object.name;
      node.userData.objectId = object.id;
      this.nodes.set(object.id, node);
    }
    for (const object of world.objects) {
      this.objects.set(object.id, object);
      const node = this.nodes.get(object.id)!;
      const parent = this.worldParentOf(object);
      if (node.parent !== parent) parent.add(node);
      node.position.set(...object.transform.position);
      // quaternion_xyzw (§2.8): Three.js reads x, y, z, w in that order.
      node.quaternion.set(...object.transform.quaternion);
      node.scale.set(...object.transform.scale);
      const signature = JSON.stringify([object.metadataRef ?? null, object.meshRenderer ?? null,
        object.light ?? null, object.camera ?? null, declaredJointPositions(object.robotDriver), object.localAxes ?? null]);
      if (this.signatures.get(object.id) !== signature) {
        this.signatures.set(object.id, signature);
        this.populate(object.id, node, object, signature);
      }
    }
    // A rebuild makes fresh, visible copies of the file's lights, so the override - if
    // any - has to be re-applied to the objects that were just created (§4.8.1, Q114 ③).
    this.applyLightingOverride();
    // §4.8.6 (Q119): a rebuild can add or drop whole objects, so the wireframe reconciles now
    // rather than leaving a box of a deleted object on screen until the next refit tick.
    if (this.boxesEnabled) this.syncBoundingBoxes();
    this.select(this.selectedId !== null && incoming.has(this.selectedId) ? this.selectedId : null);
  }

  /**
   * Put every robot on the stage back at the pose its own world file declares
   * (§ 场景文件描述 §4.7). `syncScene` writes the transforms a world file owns but never
   * touches joint angles - those are the trajectory driver's only output - so when
   * playback ends the arms would otherwise stay frozen at their last driven pose.
   */
  reparkRobots(): void {
    for (const [objectId, entry] of this.robotTrees) {
      const driver = this.objects.get(objectId)?.robotDriver;
      if (!driver) continue;
      applyDeclaredJointPositions(driver, entry.tree.joints, objectId, (message) => {
        this.reportOnce(`pose:${objectId}:${message}`, message);
      });
    }
    this.scene.updateMatrixWorld(true);
  }

  /**
   * The node an object hangs from according to its own world file: the URDF link an
   * accepted `RobotAttachment` is framed on (§ 场景文件描述 §4.8), otherwise the node
   * `Transform.parent` names (§3.6). A link whose robot has not finished loading
   * resolves to the parent object node for now, and `mountOnLinks` moves the object
   * once the tree exists.
   */
  private worldParentOf(object: WorldObjectNode): THREE.Object3D {
    const fallback = object.transform.parent === null
      ? this.simWorld
      : this.nodes.get(object.transform.parent) ?? this.simWorld;
    const mount = object.attachmentMount;
    if (!mount || mount.link === null) return fallback;
    const built = this.robotTrees.get(mount.objectId);
    if (!built) return fallback;
    const link = built.tree.links.get(mount.link);
    if (!link) {
      this.reportOnce(`link:${mount.objectId}:${mount.link}`,
        `attachment of object ${object.id} names link "${mount.link}", which robot ${mount.objectId} does not have; mounted on the parent object`);
      return fallback;
    }
    return link;
  }

  /**
   * Re-resolve the children mounted on one robot's links. Called when its tree
   * arrives, and before an old tree is disposed so a mounted object is never
   * traversed away with the link node it happened to hang from.
   */
  private mountOnLinks(objectId: number): void {
    for (const object of this.objects.values()) {
      if (object.attachmentMount?.objectId !== objectId) continue;
      const node = this.nodes.get(object.id);
      if (!node) continue;
      const parent = this.worldParentOf(object);
      if (node.parent !== parent) parent.add(node);
    }
  }

  private populate(objectId: number, node: THREE.Object3D, object: WorldObjectNode, signature: string): void {
    // Any rebuild drops the previous articulated instance; the new one registers
    // itself when it resolves (§8.4).
    this.robotTrees.delete(objectId);
    this.shadowRigDirty = true;
    this.mountOnLinks(objectId);
    for (const child of [...node.children]) {
      if (!child.userData.renderContent) continue;
      node.remove(child);
      this.disposeObject(child);
    }
    if (object.light) this.addLight(node, object.light);
    if (object.camera) this.addCamera(node, object.camera);
    if (object.localAxes) this.addLocalAxes(node, object.localAxes);
    // §3.7: without a MetadataRef, and without a light, camera or axes component, the
    // object stays an empty node.
    if (object.metadataRef) {
      const settled = this.loadModel(objectId, node, object, signature);
      this.loading.set(objectId, settled);
      void settled.finally(() => {
        // Only clear the entry this call owns; a later rebuild has already replaced it.
        if (this.loading.get(objectId) === settled) this.loading.delete(objectId);
      });
    }
  }

  /**
   * The one place a `Light` component becomes a Three.js light, shared by the document's
   * own lights (§4.5) and a preset's (§4.8.1, Q114 ③) so both read the same defaults.
   */
  private createLightSource(light: LightProps): THREE.Light {
    const type = typeof light.type === "string" ? light.type : "Ambient";
    const color = lightColor(light.color);
    const intensity = typeof light.intensity === "number" && Number.isFinite(light.intensity) ? light.intensity : 1;
    let source: THREE.Light;
    if (type === "Directional") source = new THREE.DirectionalLight(color, intensity);
    else if (type === "Point") source = new THREE.PointLight(color, intensity);
    else if (type === "Spot") source = new THREE.SpotLight(color, intensity);
    else {
      if (type !== "Ambient") {
        // O4: the enumeration is not final, so an unknown type degrades to Ambient.
        console.warn(`[renderer] unknown Light.type "${type}", using Ambient`);
      }
      source = new THREE.AmbientLight(color, intensity);
    }
    // `DirectionalLight` and `SpotLight` are constructed a metre up their own Y axis
    // (`Object3D.DEFAULT_UP`), a convenience for a hand-written scene and a lie about this
    // document: §4.5 says a light's pose *is* its object's Transform. The offset was invisible
    // while nothing projected; a shadow volume fitted around an apex the file does not declare
    // casts in the wrong place, so the constructor's gift goes back where it came from.
    source.position.set(0, 0, 0);
    this.prepareShadow(source, intensity);
    return source;
  }

  /**
   * §4.8.5 (**Q117**): which lights the renderer lets project. Only a light with a direction has a
   * shadow to cast - an Ambient one would blacken the whole volume it covers - and a light with no
   * intensity is already dark, so its depth map would be the one thing it costs. Whether the light
   * is actually *on* the stage is not decided here: three builds its light list from the visible
   * objects, so a doc light a preset dimmed stops casting on its own (§4.8.1).
   */
  private prepareShadow(source: THREE.Light, intensity: number): void {
    const projecting = source instanceof THREE.DirectionalLight
      || source instanceof THREE.SpotLight
      || source instanceof THREE.PointLight;
    if (!projecting || intensity <= 0) return;
    source.castShadow = true;
    const size = source instanceof THREE.PointLight ? SHADOW_CUBE_MAP_SIZE : SHADOW_MAP_SIZE;
    source.shadow.mapSize.set(size, size);
    source.shadow.normalBias = SHADOW_NORMAL_BIAS;
  }

  private addLight(node: THREE.Object3D, light: LightProps): void {
    const type = typeof light.type === "string" ? light.type : "Ambient";
    const color = lightColor(light.color);
    const source = this.createLightSource(light);
    // Under a preset the file's own lights stop illuminating the stage, but they stay
    // in the document and keep their markers, because the document did not change.
    source.userData.docLight = true;
    source.visible = this.lightingOverride === null;
    source.userData.renderContent = true;
    node.add(source);
    if (source instanceof THREE.DirectionalLight || source instanceof THREE.SpotLight) {
      // A light emits along the local -Z of its object in this Z-up world.
      const target = new THREE.Object3D();
      target.position.set(0, 0, -1);
      target.userData.renderContent = true;
      node.add(target);
      source.target = target;
    }
    const marker = new THREE.Mesh(new THREE.SphereGeometry(0.12, 12, 8),
      new THREE.MeshBasicMaterial({ color, wireframe: true }));
    marker.userData.objectId = node.userData.objectId;
    marker.userData.renderContent = true;
    marker.userData.lightMarker = true;
    // §4.4 (Q106): the sphere shows when its light is selected, unless the flag pins it on.
    marker.visible = this.lightMarkerAlways || node.userData.objectId === this.selectedId;
    node.add(marker);
    const label = this.lightLabel(type, color);
    label.position.set(0, 0, 0.25);
    label.userData.objectId = node.userData.objectId;
    label.userData.renderContent = true;
    label.userData.lightLabel = true;
    label.visible = node.userData.objectId === this.selectedId;
    node.add(label);
  }

  /** §4.5: the light's type spelled out at its own position, tinted with the light's colour. */
  private lightLabel(type: string, color: THREE.Color): THREE.Sprite {
    const sprite = this.textSprite(type, color);
    sprite.scale.set(1.8, 0.28, 1);
    return sprite;
  }

  /**
   * The one canvas-to-sprite text path in the editor (§4.5): `LocalAxes` tip letters (§4.9)
   * are drawn with it, which is why it sets no scale and claims no position.
   */
  private textSprite(text: string, color: THREE.Color): THREE.Sprite {
    const canvas = document.createElement("canvas");
    canvas.width = 256; canvas.height = 40;
    const context = canvas.getContext("2d")!;
    context.font = "bold 22px sans-serif";
    context.fillStyle = `#${color.getHexString()}`;
    context.fillText(text, 4, 27);
    const texture = new THREE.CanvasTexture(canvas);
    return new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }));
  }

  /**
   * §4.8.1 (Q114 ③): light the stage with a preset without touching the world it shows.
   * The file's lights go invisible - the renderer's light list follows `visible` - and
   * the preset's take their place in a group that hangs outside `nodes`. `World` is not
   * read or written here, which is the whole point: `toJSON()`, an export, `/getWorld`
   * and the scene digest keep reporting the lights the file declares. `null` is
   * "Scene lights", the default.
   */
  setLightingOverride(preset: LightingPreset | null): void {
    this.lightingOverride = preset;
    this.applyLightingOverride();
  }

  /** How many preset lights are on the stage right now - zero while following the scene. */
  get lightingOverrideCount(): number {
    return this.overrideLights.children.length;
  }

  private applyLightingOverride(): void {
    const hadOverride = this.overrideLights.children.length > 0;
    for (const holder of [...this.overrideLights.children]) {
      this.overrideLights.remove(holder);
      this.disposeObject(holder);
    }
    for (const entry of this.lightingOverride ? presetLightSources(this.lightingOverride) : []) {
      this.addOverrideLight(entry);
    }
    // `syncScene` re-applies the override on every pass whether or not anything moved
    // (§4.8.1), so the caster list only goes stale when a preset light actually came or went.
    if (hadOverride || this.overrideLights.children.length > 0) this.shadowRigDirty = true;
    for (const node of this.nodes.values()) {
      for (const child of node.children) {
        if (child.userData.docLight) child.visible = this.lightingOverride === null;
      }
    }
  }

  private addOverrideLight(entry: PresetLightSource): void {
    const holder = new THREE.Group();
    holder.position.set(...entry.position);
    holder.quaternion.set(...entry.quaternion);
    const source = this.createLightSource(entry.light);
    holder.add(source);
    if (source instanceof THREE.DirectionalLight || source instanceof THREE.SpotLight) {
      const target = new THREE.Object3D();
      target.position.set(0, 0, -1);
      holder.add(target);
      source.target = target;
    }
    this.overrideLights.add(holder);
  }

  /**
   * §4.4 (**Q145**): the colour the viewport clears to, a CSS hex string already validated by
   * `logic.ts:viewportBackground()` (`#RGB` or `#RRGGBB`). `THREE.Color` parses both shapes, so
   * this only hands the string over -- the renderer never reads `config/constant_config.json`.
   * The resting background is what the screen shows and what a viewport screenshot carries
   * (`viewportPass` swaps nothing); the sensor passes still override it per §4.4 and restore it.
   */
  setBackgroundColor(color: string): void {
    this.scene.background = new THREE.Color(color);
  }

  /**
   * §4.8.5 (**Q117**): the toolbar's 投射阴影 checkbox, and the lease freeze that answers its click
   * while a task holds the 现场. Only the renderer's shadow stage moves - the lights keep their
   * `castShadow` and three stops drawing the depth maps - so switching back costs no rebuild and,
   * as always for a class-③ entry, no write to the world document.
   */
  setShadowsEnabled(enabled: boolean): void {
    if (this.shadowsEnabled === enabled) return;
    this.shadowsEnabled = enabled;
    this.renderer.shadowMap.enabled = enabled;
    // three bakes the shadow lookups into the compiled program: a material compiled while the switch
    // was off never samples a depth map again, so the stage brightened on the way out and stayed
    // bright on the way back. Recompiling is what makes the toggle reversible (§12.19 ③).
    this.rebuildPrograms();
    this.shadowRigDirty = true;
    this.reportShadows();
  }

  private rebuildPrograms(): void {
    this.scene.traverse((object) => {
      const material = (object as THREE.Mesh).material;
      if (!material) return;
      for (const one of Array.isArray(material) ? material : [material]) one.needsUpdate = true;
    });
  }

  /** Whether the stage projects right now: what the freeze and the smoke read back. */
  get shadowsOn(): boolean {
    return this.shadowsEnabled;
  }

  /**
   * The shadow rig as a line the page can read (§12.19). Like **Q105**'s markers and **Q107**'s
   * ghosts it lives in a scene graph the viewport cannot count, so this is the only surface the
   * smoke has: which lights project, how far each volume reaches, and how much geometry takes part.
   */
  shadowReport(): ShadowReport {
    const casters = this.projectingLights().map((light) => {
      const camera = light.shadow.camera as THREE.OrthographicCamera;
      return { type: light.type, mapSize: light.shadow.mapSize.width, near: camera.near, far: camera.far };
    });
    let castMeshes = 0;
    let receiveMeshes = 0;
    this.scene.traverse((child) => {
      if (!(child as THREE.Mesh).isMesh || !child.visible) return;
      if (child.castShadow) castMeshes += 1;
      if (child.receiveShadow) receiveMeshes += 1;
    });
    return { enabled: this.shadowsEnabled && this.renderer.shadowMap.enabled, casters, castMeshes, receiveMeshes };
  }

  /** Say what the rig is, on every event that could have changed it (§12.19). */
  private reportShadows(): void {
    console.debug(`Viewport shadows ${JSON.stringify(this.shadowReport())}`);
  }

  /**
   * The lights that project *and* are on the stage. `visible` is the whole test, because three
   * builds its light list out of the visible objects: a doc light a preset dimmed is already out of
   * the shadow pass without anything here having to know about the override (§4.8.1).
   */
  private projectingLights(): Array<THREE.DirectionalLight | THREE.SpotLight | THREE.PointLight> {
    const found: Array<THREE.DirectionalLight | THREE.SpotLight | THREE.PointLight> = [];
    this.scene.traverse((child) => {
      if (child instanceof THREE.DirectionalLight || child instanceof THREE.SpotLight || child instanceof THREE.PointLight) {
        if (child.castShadow && child.visible) found.push(child);
      }
    });
    return found;
  }

  /**
   * Refit every shadow volume to the world box, in the frame the light will render it from
   * (§4.8.5). A directional light gets a box hugging the box it sees; a `Spot` and a `Point` get the
   * nearest and farthest corner of the same walk. Deliberately not every frame - the walk is
   * `worldAabb()`, and §4.4 keeps that out of the render loop for exactly that reason.
   */
  private fitShadowRigs(): void {
    this.shadowRigDirty = false;
    this.lastShadowFit = performance.now();
    // The line is how a smoke run reads the shadow stage, and "nothing is projected" is a state worth
    // seeing: a renderer with the feature off and one that never fitted a rig would otherwise print
    // exactly the same silence.
    if (!this.shadowsEnabled) {
      this.reportShadows();
      return;
    }
    this.scene.updateMatrixWorld(true);
    const box = this.worldAabb();
    if (!box) return;
    const corners = [
      box.min.x, box.min.y, box.min.z, box.max.x, box.min.y, box.min.z, box.min.x, box.max.y, box.min.z, box.max.x, box.max.y, box.min.z,
      box.min.x, box.min.y, box.max.z, box.max.x, box.min.y, box.max.z, box.min.x, box.max.y, box.max.z, box.max.x, box.max.y, box.max.z,
    ];
    for (const light of this.projectingLights()) {
      const camera = light.shadow.camera as THREE.OrthographicCamera;
      const position = light.getWorldPosition(this.scratchVector);
      const range = shadowRangeOf(corners, [position.x, position.y, position.z], SHADOW_MARGIN);
      if (!range) continue;
      camera.near = range.near;
      camera.far = range.far;
      if (light instanceof THREE.DirectionalLight) {
        // three re-aims this camera at the light's target on every shadow pass, so the fit reads the
        // orientation three will actually use - position, look, one matrix update - not the light's own.
        camera.position.copy(position);
        camera.lookAt(light.target.getWorldPosition(this.shadowTarget));
        camera.updateMatrixWorld();
        const extents = shadowExtentsOf(corners, camera.matrixWorldInverse.elements, SHADOW_MARGIN);
        if (!extents) continue;
        camera.left = extents.left;
        camera.right = extents.right;
        camera.top = extents.top;
        camera.bottom = extents.bottom;
        camera.near = extents.near;
        camera.far = extents.far;
      }
      // `LightShadow.updateMatrices` only re-projects a spot or point camera, so an orthographic fit
      // that nobody re-projects would keep rendering last frame's box.
      camera.updateProjectionMatrix();
    }
    this.reportShadows();
  }

  /**
   * §4.8.6 (**Q119**): the toolbar's 显示包围盒 checkbox. One boolean in, one layer out.
   * The boxes are drawn from `worldAabb(objectId)` - the same measure the selection outline (§4.5),
   * `/get2DAABB` and `frame()` read - so the box on the screen cannot disagree with the box a
   * caller gets. Helpers hang off `scene`, which is what keeps them out of every capture without
   * §4.4's rule having to learn about this layer: a `Box3Helper` is already editor-only by type.
   */
  setBoundingBoxesEnabled(enabled: boolean): void {
    if (this.boxesEnabled !== enabled) {
      this.boxesEnabled = enabled;
      // The click answers on the spot instead of waiting for the refit clock.
      if (enabled) this.syncBoundingBoxes();
      else this.clearBoundingBoxes();
    }
    // Reported even when nothing changed: a page load asks which state it starts in, and the only
    // other answer is silence - which a smoke run cannot tell apart from a layer that never works.
    this.reportBoxes();
  }

  /**
   * §4.8.6 (**Q149**): `urdf_bounding_box_per_link`, read from the constant config by the page.
   * It only says *how a URDF object's box is drawn* - one wireframe per link instead of one for
   * the whole object - and never *how big* it is: `worldAabb()` still answers with the union of
   * those same link boxes, so `/get2DAABB`, the selection outline and `frame()` keep seeing one
   * box per object whichever way this is set. A plain model has no links and keeps its one box.
   */
  setLinkBoxesEnabled(enabled: boolean): void {
    if (this.linkBoxesEnabled !== enabled) {
      this.linkBoxesEnabled = enabled;
      if (this.boxesEnabled) this.syncBoundingBoxes();
    }
    this.reportBoxes();
  }

  /** Which objects got a box and which could not have one (§4.8.6): the line the page reads. */
  boxReport(): BoxReport {
    const drawn = [...this.boxHelpers.keys()].sort((left, right) => left - right);
    return {
      enabled: this.boxesEnabled,
      drawn,
      skipped: this.boxSkipped,
      perLink: this.linkBoxesEnabled,
      drawnLinks: [...this.linkHelpers.keys()].sort(),
    };
  }

  /**
   * Say what the layer is, but only when the answer changed. The walk below runs four times a
   * second while the layer is on, and `fitShadowRigs` can afford its unconditional line because a
   * rig only refits when something moved; here the same cadence would print a steady four lines a
   * second and bury both the console and the line a smoke run reads.
   */
  private reportBoxes(): void {
    const line = JSON.stringify(this.boxReport());
    if (line === this.lastBoxReport) return;
    this.lastBoxReport = line;
    console.debug(`Viewport boxes ${line}`);
  }

  /**
   * One box per object that has geometry, and no box for the objects that went away (§4.8.6). A
   * world reload that left a wireframe floating where an old object used to be is the one thing
   * this layer must not do, so the reconciliation runs here and once per `syncScene`, not lazily.
   */
  private syncBoundingBoxes(): void {
    this.lastBoxSync = performance.now();
    this.scene.updateMatrixWorld(true);
    if (this.linkBoxesEnabled) return this.syncLinkBoundingBoxes();
    for (const [key, helper] of [...this.linkHelpers]) this.dropLinkBox(key, helper);
    for (const [id, helper] of [...this.boxHelpers]) {
      if (this.nodes.has(id)) continue;
      this.dropBox(id, helper);
    }
    const skipped: number[] = [];
    for (const id of this.nodes.keys()) {
      const box = this.worldAabb(id);
      // §4.5's corollary, on screen: nothing to measure means no box, not a box at the origin.
      if (!box) {
        this.dropBox(id);
        skipped.push(id);
        continue;
      }
      const helper = this.boxHelpers.get(id);
      if (helper) helper.box.copy(box);
      else {
        const created = new THREE.Box3Helper(box, BOX_WIRE_COLOR);
        this.scene.add(created);
        this.boxHelpers.set(id, created);
      }
    }
    this.boxSkipped = skipped.sort((left, right) => left - right);
    this.reportBoxes();
  }

  /**
   * §4.8.6 (**Q149**): the same layer with the URDF objects cut open - one wireframe per link, and
   * the object's own union box taken off the screen so a box does not sit inside its own box. A
   * link with nothing to measure gets nothing drawn, which is what an empty `<link>` or a `<visual>`
   * whose mesh the library never returned looks like; the object keeps its union box only when it
   * has no links at all, i.e. when it is not a URDF object.
   */
  private syncLinkBoundingBoxes(): void {
    for (const [id, helper] of [...this.boxHelpers]) {
      // An object that grew links since the last walk no longer gets the union box drawn.
      if (!this.nodes.has(id) || this.robotTrees.has(id)) this.dropBox(id, helper);
    }
    const skipped: number[] = [];
    const drawnLinks = new Set<string>();
    for (const id of this.nodes.keys()) {
      const links = this.worldLinkBoxes(id);
      if (!links.length) {
        const box = this.worldAabb(id);
        if (!box) {
          this.dropBox(id);
          skipped.push(id);
          continue;
        }
        const helper = this.boxHelpers.get(id);
        if (helper) helper.box.copy(box);
        else {
          const created = new THREE.Box3Helper(box, BOX_WIRE_COLOR);
          this.scene.add(created);
          this.boxHelpers.set(id, created);
        }
        continue;
      }
      this.dropBox(id);
      for (const { link, box } of links) {
        const key = `${id}/${link}`;
        drawnLinks.add(key);
        const helper = this.linkHelpers.get(key);
        if (helper) helper.box.copy(box);
        else {
          const created = new THREE.Box3Helper(box, BOX_WIRE_COLOR);
          this.scene.add(created);
          this.linkHelpers.set(key, created);
        }
      }
    }
    for (const [key, helper] of [...this.linkHelpers]) {
      if (drawnLinks.has(key)) continue;
      this.dropLinkBox(key, helper);
    }
    this.boxSkipped = skipped.sort((left, right) => left - right);
    this.reportBoxes();
  }

  private dropBox(objectId: number, helper = this.boxHelpers.get(objectId)): void {
    if (!helper) return;
    this.boxHelpers.delete(objectId);
    helper.removeFromParent();
    helper.dispose();
  }

  /** §4.8.6 (**Q149**): one link wireframe, keyed `<objectId>/<link>`. */
  private dropLinkBox(key: string, helper = this.linkHelpers.get(key)): void {
    if (!helper) return;
    this.linkHelpers.delete(key);
    helper.removeFromParent();
    helper.dispose();
  }

  private clearBoundingBoxes(): void {
    for (const [id, helper] of [...this.boxHelpers]) this.dropBox(id, helper);
    for (const [key, helper] of [...this.linkHelpers]) this.dropLinkBox(key, helper);
    this.boxSkipped = [];
  }

  /**
   * §4.8.7 (**Q121**): install or clear the frame tap of a recording. The page hands over the one
   * function that pushes a frame into the stream, and says whether the editor layers stay out of the
   * take; the cadence and the counter are this module's business. Called with `null` it still answers
   * the question a page load asks - what state is this layer in - because silence reads the same as
   * "never wired" (§4.8.6's rule).
   */
  setRecordFrameTap(tap: (() => void) | null, excludeOverlays: boolean): void {
    if (tap && this.recordTap === null) this.recordFrames = 0;
    this.recordTap = tap;
    this.recordExcludesOverlays = excludeOverlays;
    // A take's first frame is due on the very next draw, not one interval after the click, so the
    // clock starts out "nothing delivered yet" rather than "just delivered" - and clearing it back to
    // zero is what makes the gate testable with fed timestamps (§12.22 ①).
    this.lastRecordFrame = tap ? Number.NEGATIVE_INFINITY : 0;
    this.reportRecording();
  }

  /** What the recording layer is doing, and how many frames it really delivered (§4.8.7). */
  recordReport(): RecordReport {
    return {
      recording: this.recordTap !== null,
      excludesOverlays: this.recordExcludesOverlays,
      frames: this.recordFrames,
      intervalMs: Math.round(RECORD_INTERVAL_MS),
    };
  }

  /**
   * §4.8.7 (**Q121**): one frame of a running take - the §4.4 viewport pass, into the canvas the page
   * is streaming, with or without the editor layers as the take was asked for.
   *
   * It is a second render rather than a copy of the frame that was just drawn because the two are not
   * the same picture: what the screen composites includes every editor layer the take is supposed to
   * leave out, and hiding them on the screen to fix that would make the viewport flicker for the whole
   * take. This is also the ruling the feature was asked for ("按视口截图那套"), so the file and the
   * screenshot button can never disagree about what the viewport looks like.
   */
  recordFrameTo(target: HTMLCanvasElement): boolean {
    return this.captureViewport(target, this.recordExcludesOverlays);
  }

  /**
   * Asked, it answers - but the delivered frame count stays out of the dedup key, because a running
   * take changes that number thirty times a second and the line a probe reads would be buried under
   * it. `reportBoxes` learned the same lesson on a four-times-a-second clock.
   */
  private reportRecording(): void {
    const report = this.recordReport();
    const key = `${report.recording}:${report.excludesOverlays}`;
    if (key === this.lastRecordReport) return;
    this.lastRecordReport = key;
    console.debug(`Viewport record ${JSON.stringify(report)}`);
  }

  /**
   * The frame this render-loop iteration owes the running take, or `null` when nothing is due. It is
   * a separate callable because the cadence is the part §12.22 measures - a test feeds timestamps and
   * counts what comes back - and the suite has neither `requestAnimationFrame` nor fake timers.
   */
  private dueRecordFrame(now: number): (() => void) | null {
    const tap = this.recordTap;
    if (!tap || now - this.lastRecordFrame < RECORD_INTERVAL_MS) return null;
    this.lastRecordFrame = now;
    this.recordFrames += 1;
    return tap;
  }

  private addCamera(node: THREE.Object3D, camera: CameraProps): void {
    const rig = cameraRig(camera);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(cameraFrustumSegments(rig), 3));
    const frustum = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0x38bdf8 }));
    frustum.userData.objectId = node.userData.objectId;
    frustum.userData.renderContent = true;
    frustum.userData.cameraMarker = true;
    // §4.4 (Q105): the frustum lines show when this camera is selected, unless pinned on.
    frustum.visible = this.cameraFrustumAlways || node.userData.objectId === this.selectedId;
    node.add(frustum);
  }

  /**
   * The three axes of the object's own frame (§4.9, **Q130**). One child group carries the whole
   * overlay, so the parent chain, the URDF kinematics of §8.3 and the mount pose of §4.8 push it
   * around for free and nothing is recomputed per frame. The single `userData.localAxes` stamp is
   * the identity the existing judgments read -- editor layer for captures and extents, never a
   * pick target -- which is why none of them grew a branch.
   */
  private addLocalAxes(node: THREE.Object3D, props: LocalAxesProps): void {
    const rig = localAxesRig(props);
    const group = new THREE.Group();
    group.userData.objectId = node.userData.objectId;
    group.userData.renderContent = true;
    group.userData.localAxes = true;
    // §4.9: offset shifts the whole frame away from the object's local origin.
    group.position.set(rig.offset[0], rig.offset[1], rig.offset[2]);
    for (const axis of LOCAL_AXES) {
      const style = rig.colors[axis.key];
      const color = new THREE.Color(style.color);
      const tip = new THREE.Vector3(axis.unit[0] * rig.length, axis.unit[1] * rig.length, axis.unit[2] * rig.length);
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), tip]),
        new THREE.LineBasicMaterial({ color, opacity: style.opacity, transparent: style.opacity < 1 }));
      line.userData.objectId = node.userData.objectId;
      group.add(line);
      if (rig.showLabels) {
        const label = this.textSprite(axis.label, color);
        // The light label is a fixed width because its marker is a fixed-size sphere; a tip label
        // travels with `length`, or a 0.1 m frame would carry 1.8 m of text.
        label.scale.set(rig.length, rig.length * (40 / 256), 1);
        label.position.copy(tip);
        group.add(label);
      }
    }
    // §4.9: declared per object and always on -- the flag, not the selection, is the only switch.
    group.visible = this.localAxesEnabled;
    node.add(group);
  }

  /**
   * Fetch the referenced model and mount its geometry. A failure is reported once
   * for that object and leaves the rest of the world loading (§6.1, §6.3).
   */
  private async loadModel(objectId: number, node: THREE.Object3D, object: WorldObjectNode,
                          signature: string): Promise<void> {
    const report = (message: string) => this.reportOnce(`object:${objectId}`, message);
    if (!this.library) {
      report("the object library is not configured, models cannot be loaded");
      return;
    }
    let target: MetadataTarget;
    try {
      target = resolveMetadataTarget(object.metadataRef?.path, object.metadataRef?.type);
    } catch (error) {
      report(`object ${objectId}: ${(error as Error).message}`);
      return;
    }
    let files: ModelFiles;
    try {
      files = await this.modelFiles(target);
    } catch (error) {
      report(`${target.kind} ${target.id} could not be loaded: ${(error as Error).message}`);
      return;
    }
    if (this.stale(objectId, node, signature)) return;
    try {
      const articulated = suffixOf(files.entry) === "urdf";
      const built = articulated
        ? await this.buildRobot(objectId, files, object.robotDriver)
        : cloneAsset(await this.asset(files, files.entry));
      if (this.stale(objectId, node, signature)) {
        this.disposeObject(built);
        return;
      }
      const override = object.meshRenderer?.forceOverrideColor;
      if (typeof override === "string" && override.trim() && !overrideAssetColor(built, override.trim())) {
        report(`forceOverrideColor "${override}" is not a #RRGGBB or #RRGGBBAA colour`);
      }
      built.userData.renderContent = true;
      built.traverse((child) => {
        if (child.userData.objectId == null) child.userData.objectId = objectId;
      });
      node.add(built);
      // A model that was not on the stage is new shadow material: the volumes fitted before
      // it landed are too small for it (§4.8.5).
      this.shadowRigDirty = true;
      // The parent's link nodes only exist from here on, so what the world file
      // mounts on them moves in (§ 场景文件描述 §4.8).
      this.mountOnLinks(objectId);
      // §8.5, §7.3 (Q101): this object's joints exist only from here, so a clip that reached
      // the stage before this asset did re-applies its frame and can sweep waypoints now.
      if (articulated) this.callbacks.onRobotTree(objectId);
      if (this.selectedId === objectId) this.select(objectId);
    } catch (error) {
      report(`${files.kind} ${files.modelId} entry ${files.entry} failed: ${(error as Error).message}`);
    }
  }

  private stale(objectId: number, node: THREE.Object3D, signature: string): boolean {
    return this.disposed || this.nodes.get(objectId) !== node || this.signatures.get(objectId) !== signature;
  }

  /** One directory of one id space (§6.11): `model` 0 and `robot` 0 are different models. */
  private modelFiles(target: MetadataTarget): Promise<ModelFiles> {
    const key = `${target.kind}:${target.id}`;
    const cached = this.models.get(key);
    if (cached) return cached;
    const loading = (async () => {
      const client = this.library;
      if (!client) throw new LibraryError("INVALID_PARAM", "the object library is not configured");
      const metadata = target.kind === "robot" ? await client.robot(target.id) : await client.model(target.id);
      const bundle = await client.bundle(metadata);
      return new ModelFiles(target.id, target.kind, bundle.entry, metadata.isOnCos, client, bundle.files);
    })();
    const tracked = loading.catch((error) => {
      this.models.delete(key);
      throw error;
    });
    this.models.set(key, tracked);
    return tracked;
  }

  private robot(files: ModelFiles): Promise<UrdfRobot> {
    const key = `${files.kind}:${files.modelId}`;
    const cached = this.robots.get(key);
    if (cached) return cached;
    const tracked = Promise.resolve()
      .then(() => parseUrdf(files.text(files.entry)))
      .catch((error) => {
        this.robots.delete(key);
        throw error;
      });
    this.robots.set(key, tracked);
    return tracked;
  }

  /**
   * A URDF is expanded per instance because each robot needs its own joint nodes
   * to move later (§8.4); its meshes stay shared templates (§6.7). A mesh that
   * cannot be resolved is left out instead of replaced by a cube (§6.8).
   */
  private async expandRobot(files: ModelFiles): Promise<{ robot: UrdfRobot; tree: UrdfTree }> {
    const robot = await this.robot(files);
    const templates = new Map<string, THREE.Object3D>();
    const missing: string[] = [];
    await Promise.all(robot.meshFiles.map(async (filename) => {
      if (!files.has(filename)) {
        missing.push(filename);
        return;
      }
      try {
        templates.set(filename, cloneAsset(await this.asset(files, filename)));
      } catch (error) {
        missing.push(`${filename} (${(error as Error).message})`);
      }
    }));
    if (missing.length) {
      this.reportOnce(`urdf:${files.kind}:${files.modelId}`, `${files.kind} ${files.modelId} is missing ${missing.join(", ")}`);
    }
    const tree = buildUrdfTree(robot, (filename, rgba) => {
      const template = templates.get(filename);
      if (!template) return null;
      const clone = cloneAsset(template);
      if (template.userData.tintable) tintAsset(clone, rgba);
      return clone;
    });
    return { robot, tree };
  }

  /**
   * The live instance of one object: expand its URDF, register its joint nodes, and
   * park it at the pose the world file declares (§ 场景文件描述 §4.7).
   */
  private async buildRobot(objectId: number, files: ModelFiles, driver?: RobotDriverProps): Promise<THREE.Object3D> {
    const built = await this.expandRobot(files);
    this.robotTrees.set(objectId, built);
    if (driver) {
      applyDeclaredJointPositions(driver, built.tree.joints, objectId, (message) => {
        this.reportOnce(`pose:${objectId}:${message}`, message);
      });
    }
    return built.tree.root;
  }

  /**
   * Put one robot's segment-end ghost on the stage (§7.3, Q96/Q98): a second instance of
   * the same URDF at `opacity`, hanging where the live one hangs. A ghost is not
   * registered in `nodes` or `robotTrees`, so the hierarchy tree, Details, the pick ray
   * and the world AABB all miss it, and only `hideOverlays` has to know it exists.
   */
  async buildGhost(objectId: number, opacity: number): Promise<boolean> {
    if (this.disposed) return false;
    if (this.ghosts.has(objectId)) return true;
    const loading = this.ghostLoading.get(objectId);
    if (loading) return loading;
    const generation = this.ghostGenerationOf(objectId);
    const promise = this.createGhost(objectId, opacity, generation).then((built) => {
      // Only the build that owns the entry clears it: a cancelled build must not drop the
      // entry a later one for the same object installed meanwhile.
      if (this.ghostLoading.get(objectId) === promise) this.ghostLoading.delete(objectId);
      return built;
    });
    this.ghostLoading.set(objectId, promise);
    return promise;
  }

  /**
   * The ghost's joint handles, re-hung where its live counterpart hangs (§8.6), or null
   * while no ghost for that object exists. The base is copied every frame because an
   * `updateItem` or an Attach can move the robot the ghost mirrors.
   */
  syncGhost(objectId: number): ReadonlyMap<string, UrdfJointHandle> | null {
    const ghost = this.ghosts.get(objectId);
    const node = this.nodes.get(objectId);
    if (!ghost || !node?.parent) return null;
    this.rehangGhost(objectId, node);
    return ghost.joints;
  }

  /** Drop the ghosts of objects nobody previews any more (§7.3, Q98), in-flight builds included. */
  retainGhosts(keep: ReadonlySet<number>): void {
    for (const objectId of this.ghostIds()) {
      if (!keep.has(objectId)) this.removeGhost(objectId);
    }
  }

  /** Take every ghost away, including one whose asset is still on its way. */
  clearGhost(): void {
    for (const objectId of this.ghostIds()) this.removeGhost(objectId);
  }

  private ghostIds(): number[] {
    return [...new Set([...this.ghosts.keys(), ...this.ghostLoading.keys()])];
  }

  /**
   * §12.15 (Q107): which robots are mirrored right now, read back from the scene graph. A
   * ghost is neither an object (`/getWorld` never lists it) nor something the viewport can
   * count, so this is the only page-side truth about the set - the same reason `markerStates()`
   * and `waypointLayers()` exist (**Q105**, **Q101**). `ready` is false while its asset is
   * still on its way.
   */
  ghostStates(): Array<{ objectId: number; ready: boolean }> {
    return this.ghostIds()
      .map((objectId) => ({ objectId, ready: this.ghosts.has(objectId) }))
      .sort((left, right) => left.objectId - right.objectId);
  }

  private removeGhost(objectId: number): void {
    this.ghostGeneration.set(objectId, this.ghostGenerationOf(objectId) + 1);
    this.ghostLoading.delete(objectId);
    const ghost = this.ghosts.get(objectId);
    this.ghosts.delete(objectId);
    if (!ghost) return;
    ghost.root.removeFromParent();
    this.disposeObject(ghost.root);
  }

  /** An object nobody has ever ghosted is on generation 0, the number `buildGhost` starts from. */
  private ghostGenerationOf(objectId: number): number {
    return this.ghostGeneration.get(objectId) ?? 0;
  }

  /**
   * Where a ghost hangs (§7.3, Q98): its own live parent, unless that parent is a link
   * inside another robot that also has a ghost. The gripper's live parent is the arm's
   * wrist link, so a ghost gripper left there would sit at the playhead's pose while its
   * ghost arm reaches the end of the step -- the pair has to read as one pose, not two.
   */
  private ghostParent(objectId: number, liveParent: THREE.Object3D): THREE.Object3D {
    for (const [ownerId, ghost] of this.ghosts) {
      if (ownerId === objectId) continue;
      const owner = this.nodes.get(ownerId);
      if (!owner || !isWithin(liveParent, owner)) continue;
      const built = this.robotTrees.get(ownerId);
      const link = built && [...built.tree.links].find(([, node]) => node === liveParent)?.[0];
      return (link === undefined ? undefined : ghost.links.get(link)) ?? liveParent;
    }
    return liveParent;
  }

  private async createGhost(objectId: number, opacity: number, generation: number): Promise<boolean> {
    const node = this.nodes.get(objectId);
    const object = this.objects.get(objectId);
    if (!node || !object || !this.library) return false;
    let target: MetadataTarget;
    try {
      target = resolveMetadataTarget(object.metadataRef?.path, object.metadataRef?.type);
    } catch (error) {
      this.reportOnce(`ghost:${objectId}`, `${(error as Error).message}`);
      return false;
    }
    let files: ModelFiles;
    try {
      files = await this.modelFiles(target);
    } catch (error) {
      this.reportOnce(`ghost:${objectId}`, `${target.kind} ${target.id} could not be loaded: ${(error as Error).message}`);
      return false;
    }
    const stale = () => this.disposed || this.ghostGenerationOf(objectId) !== generation;
    if (stale()) return false;
    if (suffixOf(files.entry) !== "urdf") {
      this.reportOnce(`ghost:${objectId}`, `object ${objectId} is driven but its ${files.kind} ${files.modelId} entry ${files.entry} is not a URDF, so it has no ghost to draw`);
      return false;
    }
    let built: UrdfTree;
    try {
      built = (await this.expandRobot(files)).tree;
    } catch (error) {
      this.reportOnce(`ghost:${objectId}`, `the ghost of object ${objectId} could not be built: ${(error as Error).message}`);
      return false;
    }
    if (stale()) {
      this.disposeObject(built.root);
      return false;
    }
    ghostAsset(built.root, opacity);
    this.ghosts.set(objectId, built);
    this.rehangGhost(objectId, node);
    return true;
  }

  /** Hang one ghost where its live counterpart hangs now (§7.3, Q96/Q98). */
  private rehangGhost(objectId: number, node: THREE.Object3D): void {
    const ghost = this.ghosts.get(objectId);
    const parent = node.parent;
    if (!ghost || !parent) return;
    const target = this.ghostParent(objectId, parent);
    if (ghost.root.parent !== target) target.add(ghost.root);
    ghost.root.position.copy(node.position);
    ghost.root.quaternion.copy(node.quaternion);
    ghost.root.scale.copy(node.scale);
  }

  /**
   * Where one robot's tool centre point is right now, in the world file's coordinates
   * (§7.3, Q102): the mean of its deepest leaf links. Reading it from the expanded tree is
   * what makes the answer follow the arm's base, so a sweep taken after the base moved
   * lays the whole path in the moved arm's frame.
   */
  toolTipPosition(objectId: number): Vec3 | null {
    const built = this.robotTrees.get(objectId);
    if (!built) return null;
    const total = this.waypointPoint.set(0, 0, 0);
    let counted = 0;
    for (const name of toolTipLinks(built.robot)) {
      const node = built.tree.links.get(name);
      if (!node) continue;
      total.add(node.getWorldPosition(this.scratchVector));
      counted += 1;
    }
    if (!counted) return null;
    total.divideScalar(counted);
    // `simWorld` carries the Z-up conversion, so the sphere layer needs the same point
    // with that one rotation taken back off.
    this.simWorld.updateWorldMatrix(true, false);
    total.applyMatrix4(this.simMatrix.copy(this.simWorld.matrixWorld).invert());
    return [total.x, total.y, total.z];
  }

  /**
   * Put the batch's waypoint spheres on the stage (§7.3, Q101): one `InstancedMesh` per
   * (clip, leaf arm) pair, so a dense path is a single draw call however many frames it
   * samples. Layers `paths` no longer mentions are dropped, and a layer whose radius, colour
   * or instance capacity changed is rebuilt - every sphere of a layer shares that one
   * geometry and material. Points are world-file coordinates; the layers hang under
   * `simWorld`, which owns the axis conversion.
   */
  setWaypointPaths(paths: readonly WaypointPath[], radius: number): void {
    if (this.disposed) return;
    const wanted = new Set(paths.map((path) => path.key));
    for (const key of [...this.waypoints.keys()]) if (!wanted.has(key)) this.removeWaypointLayer(key);
    const matrix = new THREE.Matrix4();
    for (const path of paths) {
      if (path.points.length === 0) continue;
      const existing = this.waypoints.get(path.key);
      const reusable = !!existing && existing.radius === radius && existing.color === path.color
        && existing.capacity >= path.points.length;
      // Rebuilt means replaced: the old mesh has to leave the stage as well as the map,
      // or a radius change would draw two strings of spheres on top of each other.
      if (existing && !reusable) this.removeWaypointLayer(path.key);
      const layer = reusable && existing ? existing : this.createWaypointLayer(path, radius);
      layer.trajectoryIndex = path.trajectoryIndex;
      layer.objectId = path.objectId;
      for (let index = 0; index < path.points.length; index += 1) {
        const [x, y, z] = path.points[index]!;
        matrix.setPosition(x, y, z);
        layer.mesh.setMatrixAt(index, matrix);
      }
      layer.mesh.count = path.points.length;
      layer.mesh.instanceMatrix.needsUpdate = true;
    }
  }

  /** Which layers are on show: the active clip, or every clip once the checkbox says so (§7.1, Q101). */
  setWaypointVisibility(visible: ReadonlySet<string>): void {
    for (const [key, layer] of this.waypoints) layer.mesh.visible = visible.has(key);
  }

  /** Take every waypoint away, as the end of playback does (§7.3, Q101). */
  clearWaypoints(): void {
    for (const key of [...this.waypoints.keys()]) this.removeWaypointLayer(key);
  }

  /**
   * What the layers currently are, for the diagnostics line the evidence reads (§12.13).
   * Each layer carries its sampled points back off the instance matrices, because ⑥ and ⑦
   * compare coordinates rather than counting spheres. This is called at install, on the
   * checkbox and at clear - never per frame - so the readback is not a hot path.
   */
  waypointLayers(): Array<{ key: string; trajectoryIndex: number; objectId: number; count: number; radius: number; color: number; visible: boolean; points: Vec3[] }> {
    return [...this.waypoints].map(([key, layer]) => {
      const points: Vec3[] = [];
      for (let index = 0; index < layer.mesh.count; index += 1) {
        layer.mesh.getMatrixAt(index, this.layerMatrix);
        // Column-major: the translation lives in the last three entries.
        const { elements: e } = this.layerMatrix;
        points.push([e[12]!, e[13]!, e[14]!]);
      }
      return {
        key,
        trajectoryIndex: layer.trajectoryIndex,
        objectId: layer.objectId,
        count: layer.mesh.count,
        radius: layer.radius,
        color: layer.color,
        visible: layer.mesh.visible,
        points,
      };
    });
  }

  private createWaypointLayer(path: WaypointPath, radius: number): WaypointLayer {
    const geometry = new THREE.SphereGeometry(radius, 12, 8);
    const material = new THREE.MeshBasicMaterial({ color: path.color });
    const mesh = new THREE.InstancedMesh(geometry, material, path.points.length);
    mesh.userData.waypoint = true;
    this.simWorld.add(mesh);
    const layer: WaypointLayer = {
      mesh, geometry, material,
      trajectoryIndex: path.trajectoryIndex,
      objectId: path.objectId,
      capacity: path.points.length,
      radius,
      color: path.color,
    };
    this.waypoints.set(path.key, layer);
    return layer;
  }

  private removeWaypointLayer(key: string): void {
    const layer = this.waypoints.get(key);
    this.waypoints.delete(key);
    if (!layer) return;
    layer.mesh.removeFromParent();
    this.disposeObject(layer.mesh);
  }

  /** Parse, and cache per model, one geometry file of a model directory (§6.4). */
  private asset(files: ModelFiles, path: string): Promise<THREE.Object3D> {
    const key = `${files.kind}:${files.modelId}/${path}`;
    const cached = this.templates.get(key);
    if (cached) return cached;
    const tracked = this.parseAsset(files, path)
      .then((object) => markShadowRoles(object))
      .catch((error) => {
      this.templates.delete(key);
      throw error;
    });
    this.templates.set(key, tracked);
    return tracked;
  }

  private async parseAsset(files: ModelFiles, path: string): Promise<THREE.Object3D> {
    const suffix = suffixOf(path);
    const directory = files.directoryOf(path);
    const manager = files.manager();
    const bytes = files.bytes(path);
    if (!bytes) throw new LibraryError("DEPENDENCY_FETCH_FAILED", `${path} is not part of model ${files.modelId}`, 424, [path]);
    if (suffix === "stl") {
      const geometry = new STLLoader(manager).parse(bytes);
      if (!geometry.hasAttribute("normal")) geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ roughness: 0.7, metalness: 0.08 }));
      mesh.userData.tintable = true;
      return mesh;
    }
    if (suffix === "obj") {
      const source = new TextDecoder().decode(bytes);
      const loader = new OBJLoader(manager);
      const materialName = source.match(/^\s*mtllib\s+(.+)\s*$/im)?.[1]?.trim();
      if (materialName) {
        try {
          const materials = new MTLLoader(manager).parse(files.text(normalizeModelPath(materialName, path)), directory);
          materials.preload();
          loader.setMaterials(materials);
        } catch (error) {
          // §6.5: a missing MTL degrades the material, the geometry still renders.
          console.warn(`[renderer] ${path} has no usable MTL ${materialName}: ${(error as Error).message}`);
        }
      }
      const parsed = loader.parse(normalizeObjSource(source));
      dropUnusableTextures(parsed);
      return parsed;
    }
    if (suffix === "dae") {
      const source = new TextDecoder().decode(bytes);
      const upAxis = new DOMParser().parseFromString(source, "application/xml")
        .querySelector("asset > up_axis")?.textContent?.trim();
      const scene = new ColladaLoader(manager).parse(source, directory).scene;
      // ColladaLoader pre-rotates a Z_UP asset by -pi/2 to normalise it to Y-up.
      // simWorld already applies that rotation to the whole world, so undo it here
      // or the conversion lands twice (plan §5 pit 4).
      if (upAxis === "Z_UP") scene.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));
      return scene;
    }
    if (suffix === "fbx") {
      const root = new FBXLoader(manager).parse(bytes, directory);
      // FBXLoader only scales the root when the file carries unit metadata. The
      // C++ asset pipeline defaults to centimetres and normalises to metres, so
      // apply the same 0.01 when that metadata is absent.
      if (Math.abs(root.scale.x - 1) < 1e-5 && Math.abs(root.scale.y - 1) < 1e-5 && Math.abs(root.scale.z - 1) < 1e-5) {
        root.scale.set(0.01, 0.01, 0.01);
      }
      return root;
    }
    if (suffix === "glb" || suffix === "gltf") {
      const gltf = await new GLTFLoader(manager).parseAsync(bytes, directory);
      return gltf.scene;
    }
    throw new LibraryError("INVALID_PARAM", `unsupported browser mesh format: ${suffix || "unknown"}`);
  }

  private reportOnce(key: string, message: string): void {
    if (this.reported.has(key)) return;
    this.reported.add(key);
    this.callbacks.onError(message);
  }

  select(objectId: number | null): void {
    this.selectedId = objectId;
    this.applyOverlayVisibility();
    this.syncSelectionBox();
  }

  /**
   * §4.5 (Q105): the selection outline is the same mesh-only box `/get2DAABB` and
   * `frame()` measure, never the bounding box of everything hanging under the node --
   * `Box3.expandByObject` ignores `visible` and would frame a camera by its frustum lines,
   * which reach the `far` plane. Called every frame, so the box follows playback and
   * shows up once an object's model lands.
   */
  private syncSelectionBox(): void {
    const box = this.selectedId === null ? null : this.worldAabb(this.selectedId);
    if (!box) {
      this.disposeSelectionBox();
      return;
    }
    if (this.selectionBox) this.selectionBox.box.copy(box);
    else {
      this.selectionBox = new THREE.Box3Helper(box, 0xfacc15);
      this.scene.add(this.selectionBox);
    }
  }

  private disposeSelectionBox(): void {
    if (!this.selectionBox) return;
    this.scene.remove(this.selectionBox);
    // `Box3Helper` types its material as `Material | Material[]`, so it disposes itself.
    this.selectionBox.dispose();
    this.selectionBox = null;
  }

  /**
   * Put every editor marker (§4.4) in the state the flags and the selection ask for.
   * One pass because all families follow the same rule: the light's type label has
   * been selection-only since Q71, Q105/Q106 bring the frustum lines and the
   * placeholder spheres onto it, each with a flag that can pin its family on regardless,
   * and Q130 adds the local-axes layer, whose flag only ever closes it.
   */
  private applyOverlayVisibility(): void {
    for (const child of this.markerNodes()) {
      if (child.userData.cameraMarker) {
        child.visible = this.cameraFrustumAlways || child.userData.objectId === this.selectedId;
      } else if (child.userData.lightMarker) {
        child.visible = this.lightMarkerAlways || child.userData.objectId === this.selectedId;
      } else if (child.userData.lightLabel) {
        child.visible = child.userData.objectId === this.selectedId;
      } else if (child.userData.localAxes) {
        // §4.9 (Q130): the object declared these axes, so the only state that hides them is the flag.
        child.visible = this.localAxesEnabled;
      }
    }
    this.logOverlayState();
  }

  /**
   * §12.14: every editor layer is invisible to `/getWorld` and to a pixel readout alike, so this
   * line is the only surface that can say which of them are on show. `setGridEnabled`/
   * `setWorldAxesEnabled` write no marker family, but they report through here too - a flag that
   * cannot be read back is a flag that cannot be evidenced (§4.4, **Q131**).
   */
  private logOverlayState(): void {
    console.debug(`Viewport markers ${JSON.stringify({
      cameraFrustumAlways: this.cameraFrustumAlways,
      lightMarkerAlways: this.lightMarkerAlways,
      localAxesEnabled: this.localAxesEnabled,
      gridEnabled: this.gridEnabled,
      worldAxesEnabled: this.worldAxesEnabled,
      selected: this.selectedId,
      markers: this.markerStates(),
    })}`);
  }

  /**
   * The markers §4.4 recognizes. Each is hung on the object's own node by `addLight`/
   * `addCamera`/`addLocalAxes`, so this walks `children` rather than `traverse`: a nested object - scene 1's
   * wrist camera `#4` sits under the gripper `#3` under the arm `#2` - would otherwise be
   * found once per ancestor, and the diagnostic below would report the same marker thrice.
   */
  private markerNodes(): THREE.Object3D[] {
    const found: THREE.Object3D[] = [];
    for (const node of this.nodes.values()) {
      for (const child of node.children) {
        if (child.userData.cameraMarker || child.userData.lightMarker || child.userData.lightLabel
          || child.userData.localAxes) found.push(child);
      }
    }
    return found;
  }

  /** §4.4 (Q105/Q106): each marker in the scene graph and whether it is showing right now. */
  markerStates(): Array<{ objectId: number; kind: string; visible: boolean }> {
    const states = this.markerNodes().map((child) => ({
      objectId: child.userData.objectId as number,
      kind: child.userData.cameraMarker ? "cameraFrustum"
        : child.userData.lightMarker ? "lightSphere"
          : child.userData.localAxes ? "localAxes" : "lightLabel",
      visible: child.visible,
    }));
    return states.sort((left, right) => left.objectId - right.objectId || left.kind.localeCompare(right.kind));
  }

  /** §4.4 (Q105/Q106): pin the camera frustum lines on, or leave them to the selection. */
  setCameraFrustumAlways(always: boolean): void {
    this.cameraFrustumAlways = always;
    this.applyOverlayVisibility();
  }

  /** §4.4 (Q105/Q106): pin the light placeholder spheres on, or leave them to the selection. */
  setLightMarkerAlways(always: boolean): void {
    this.lightMarkerAlways = always;
    this.applyOverlayVisibility();
  }

  /**
   * §4.8.8 (Q132): open or close the local-axes layer for the whole viewport. The objects keep their
   * components; with the flag back on every one of them shows its axes again.
   */
  setLocalAxesEnabled(enabled: boolean): void {
    this.localAxesEnabled = enabled;
    this.applyOverlayVisibility();
  }

  /** §4.4 (Q131): whether the ground grid is drawn. The helper stays in the scene either way. */
  setGridEnabled(enabled: boolean): void {
    this.gridEnabled = enabled;
    this.gridHelper.visible = enabled;
    this.logOverlayState();
  }

  /** §4.4 (Q131): whether the three axes at the world origin are drawn. */
  setWorldAxesEnabled(enabled: boolean): void {
    this.worldAxesEnabled = enabled;
    this.axesHelper.visible = enabled;
    this.logOverlayState();
  }

  setInteractions(orbit: boolean, selection: boolean): void {
    this.orbitEnabled = orbit;
    this.orbit.enabled = orbit;
    this.selectionInteractionEnabled = selection;
  }

  /**
   * World-space AABB of one object, or of the whole world when `objectId` is absent.
   *
   * "One object" is the world document's object: the walk stops at a child node that carries a
   * different `objectId`, so an arm measures the arm and the gripper mounted on its wrist measures
   * the gripper (§4.8.6 一物一盒, **Q148**). Whole-world is unaffected - every object node is
   * walked from its own root, so nothing mounted under another object goes unmeasured.
   *
   * A URDF object is measured **link by link, and each link exactly** (**Q149**): a part rotated
   * out of the mesh's own axes is bounded by its vertices, not by the eight corners of its local
   * box, so `base` is `base` whichever way the wrist is turned. The union of `worldLinkBoxes()`
   * is this box - the display layer may draw the links apart or this union together, and both
   * readings are the same geometry (§4.8.6).
   *
   * The walk skips the visualization layers §4.4 keeps out of a capture. Counting them
   * was not conservative, it was wrong: a camera's frustum lines reach its `far` plane,
   * so the box of that camera's parent -- and therefore Frame Scene -- blew up to
   * thousands of metres and left nothing but the background on screen. Since Q105 the
   * selection outline reads this too, which is what keeps that rule off the screen as
   * well as off `/get2DAABB`.
   */
  worldAabb(objectId?: number): THREE.Box3 | null {
    const box = new THREE.Box3();
    if (objectId !== undefined) {
      const node = this.nodes.get(objectId);
      if (!node) return null;
      this.expandObject(box, objectId, node);
    } else {
      for (const node of this.nodes.values()) this.expandObject(box, node.userData.objectId as number, node);
    }
    return box.isEmpty() ? null : box;
  }

  /**
   * §4.8.6 (**Q149**): one box per URDF link of one object, in the link's current pose - what the
   * 每 link 一个盒 display draws. Empty for anything that is not a URDF object, which is how a
   * plain model says "I have no links, draw my one box either way". A link with no geometry is
   * left out rather than reported as a box at the origin (§4.5's corollary).
   *
   * A URDF tree nests: the child link hangs off its joint node, which hangs off its parent link, so
   * a walk from one link reaches every link below it. Each box therefore stops at the *other* link
   * roots - otherwise the base link's box is the whole arm's, and the display the flag turns on
   * draws the same wireframe once per link at once.
   */
  worldLinkBoxes(objectId: number): Array<{ link: string; box: THREE.Box3 }> {
    const links = this.robotTrees.get(objectId)?.tree.links;
    if (!links) return [];
    const roots = new Set(links.values());
    const out: Array<{ link: string; box: THREE.Box3 }> = [];
    for (const [link, node] of links) {
      // Measurement may run immediately after loading or a joint edit, before a render.
      // Link traversal skips its ancestors, so refresh their transforms explicitly.
      node.updateWorldMatrix(true, false);
      const others = new Set(roots);
      others.delete(node);
      const box = new THREE.Box3();
      this.expandGeometry(box, node, objectId, true, others);
      if (!box.isEmpty()) out.push({ link, box });
    }
    return out;
  }

  /**
   * One object's measure: the union of `worldLinkBoxes()` when its asset is a URDF, the subtree walk
   * otherwise (**Q149**). The subtree walk still runs over a URDF object, but with the link roots
   * skipped, so what hangs off the object node itself - before the URDF expands, or on a path that
   * never goes through a link - is not silently dropped from the box.
   */
  private expandObject(box: THREE.Box3, ownerId: number, node: THREE.Object3D): void {
    node.updateWorldMatrix(true, false);
    const links = this.robotTrees.get(ownerId)?.tree.links;
    if (!links) {
      this.expandGeometry(box, node, ownerId, false);
      return;
    }
    for (const link of this.worldLinkBoxes(ownerId)) box.union(link.box);
    this.expandGeometry(box, node, ownerId, false, new Set(links.values()));
  }

  /**
   * The object's own axis-aligned box in the frame the world files are written in - the
   * same trusted measure `syncBoundingBoxes` draws and `/get2DAABB` reports (§5.11), and
   * already rotated and joint-driven to whatever the scene graph currently holds. Placement
   * collision reads this instead of OLS's declared AABB, which is authored in a frame that
   * does not always match the shipped mesh (Q144, and the URDF props 25/31 that punched
   * through their neighbours because their metadata box was too small for the real geometry).
   */
  sceneAABB(objectId: number): { min: Vec3; max: Vec3 } | null {
    const box = this.worldAabb(objectId);
    return box ? toSceneBox(box.min, box.max) : null;
  }

  /**
   * Resolve once `objectId`'s geometry has finished loading (or was abandoned as stale), so
   * a just-placed object can be measured by `sceneAABB`. An object with no load in flight
   * resolves immediately; one the library never returns resolves via the load's own catch.
   */
  async readyObject(objectId: number): Promise<void> {
    const pending = this.loading.get(objectId);
    if (pending) await pending.catch(() => undefined);
  }

  private expandGeometry(
    box: THREE.Box3,
    object: THREE.Object3D,
    ownerId: number,
    exact = false,
    skipRoots?: Set<THREE.Object3D>,
  ): void {
    if (this.isEditorOnly(object)) return;
    // §4.8.6 (**Q149**): the link roots were measured on their own; this pass covers only what the
    // link walk does not reach, so it must not add their looser corner boxes back on top.
    if (skipRoots?.has(object)) return;
    object.updateWorldMatrix(false, false);
    const geometry = (object as THREE.Mesh).geometry as THREE.BufferGeometry | undefined;
    if (geometry?.attributes.position) {
      if (exact) {
        box.union(this.exactWorldBox(object as THREE.Mesh, geometry));
      } else {
        if (geometry.boundingBox === null) geometry.computeBoundingBox();
        // §4.5 (**Q141**): this walk runs through every part mounted under the object, and a box is a
        // union -- one NaN corner would take the whole box, then `frame()` would take the camera with
        // it. Skip that part's own measure, not the parts hanging under it: a NaN mesh's children are
        // their own geometry.
        if (isFiniteBox(geometry.boundingBox!)) {
          box.union(geometry.boundingBox!.clone().applyMatrix4(object.matrixWorld));
        }
      }
    }
    for (const child of object.children) {
      // §4.8.6 (**Q148**): 一物一盒 stops the walk at another object's node. `RobotAttachment` and
      // `Transform.parent` both hang a child *inside* this subtree - the gripper off the wrist link,
      // a prop off the hand - and its geometry belongs to the child's own box, which the layer draws
      // beside it. Measuring it here made the parent's wireframe wrap geometry the document says is
      // not the parent's, so a UR5e arm reported a box reaching 6 cm past its own wrist flange.
      const childOwner = child.userData.objectId as number | undefined;
      if (childOwner !== undefined && childOwner !== ownerId) continue;
      this.expandGeometry(box, child, ownerId, exact, skipRoots);
    }
  }

  /**
   * The box a mesh's own vertices sweep in the world, cached until that mesh moves or its geometry
   * is swapped (**Q149**). This is the measure a corner box cannot be: a URDF part is authored in
   * its link's axes and the joints turn it out of them, and the eight corners of a local box rotated
   * 60° stand further out than the metal - the shipped UR5e showed 2.2 cm of that on its upper arm
   * and 1.0 cm on its forearm at the pose the scene file declares. §4.5's NaN rule applies at the
   * vertex scale here: a point that is not finite is left out of the box, so one damaged part costs
   * its own measure and not the box of the object.
   */
  private exactWorldBox(mesh: THREE.Mesh, geometry: THREE.BufferGeometry): THREE.Box3 {
    const cached = this.exactBoxes.get(mesh);
    if (cached && cached.geometry === geometry && cached.matrix.equals(mesh.matrixWorld)) return cached.box;
    const box = new THREE.Box3();
    const attribute = geometry.getAttribute("position") as THREE.BufferAttribute;
    const point = new THREE.Vector3();
    for (let index = 0; index < attribute.count; index += 1) {
      point.fromBufferAttribute(attribute, index).applyMatrix4(mesh.matrixWorld);
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y) || !Number.isFinite(point.z)) continue;
      box.expandByPoint(point);
    }
    this.exactBoxes.set(mesh, { matrix: mesh.matrixWorld.clone(), geometry, box });
    return box;
  }

  /** §4.4: markers, labels, helpers and the selection box are drawn for the editor, not the scene. */
  private isEditorOnly(object: THREE.Object3D): boolean {
    return object === this.selectionBox ||
      object.userData.ghost === true || object.userData.waypoint === true ||
      object.type.endsWith("Helper") ||
      Boolean(object.userData.cameraMarker || object.userData.lightMarker || object.userData.lightLabel
        || object.userData.localAxes);
  }

  frame(objectId?: number): void {
    const box = this.worldAabb(objectId);
    if (!box) return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const direction = new THREE.Vector3(1, 0.75, 1).normalize();
    const verticalFov = THREE.MathUtils.degToRad(this.camera.fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * this.camera.aspect);
    const limitingFov = Math.min(verticalFov, horizontalFov);
    const distance = sphere.radius / Math.max(Math.sin(limitingFov / 2), 0.01) * 1.2;
    this.orbit.target.copy(sphere.center);
    this.camera.position.copy(sphere.center).addScaledVector(direction, Math.max(distance, this.camera.near * 4));
    this.orbit.update();
  }

  resize = (): void => {
    const width = Math.max(this.canvas.clientWidth, 1);
    const height = Math.max(this.canvas.clientHeight, 1);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  };

  private pick = (event: PointerEvent): void => {
    if (!this.selectionInteractionEnabled) return;
    const bounds = this.canvas.getBoundingClientRect();
    this.pointer.set(((event.clientX - bounds.left) / bounds.width) * 2 - 1, -((event.clientY - bounds.top) / bounds.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects([...this.nodes.values()], true);
    const hit = hits.find((entry) => this.selectable(entry.object))?.object ?? null;
    this.callbacks.onSelect(this.objectIdForHit(hit));
  };

  /** Resolve the owning world node from the actual scene ancestry, preserving nested object identity. */
  private objectIdForHit(object: THREE.Object3D | null): number | null {
    for (let current = object; current !== null; current = current.parent) {
      const objectId = current.userData.objectId;
      if (typeof objectId === "number" && this.nodes.get(objectId) === current) return objectId;
    }
    return null;
  }

  /**
   * Whether a raycast hit may carry a selection. Three's raycaster never looks at
   * `object.visible` (only at `layers`), so the markers §4.4 hides by default would
   * otherwise stay clickable -- clicking empty space and selecting a light nobody can
   * see. Frustum lines were never a target (Q71) and stay out on the same walk, and so do
   * the local axes of §4.9, which are on show and must not become a selection (Q130).
   *
   * A selection also lands on a surface, never on line or point geometry (**Q140**). Three tests a
   * `Line` against `Raycaster.params.Line.threshold`, whose default **1** is one *metre* of slack
   * around every segment, and `OBJLoader` cannot keep faces and lines in one geometry: an OBJ with a
   * stray `l` statement parses to a single `LineSegments` instead of a `Mesh`, and that wireframe then
   * answers clicks from a metre away -- every neighbour on the table selects it (asset 22 `doll`: 26 of
   * 63 clicks over the exported scene, and no other prop reachable at all).
   */
  private selectable(object: THREE.Object3D): boolean {
    if (object instanceof THREE.Line || object instanceof THREE.Points) return false;
    for (let node: THREE.Object3D | null = object; node !== null; node = node.parent) {
      if (!node.visible || node.userData.cameraMarker || node.userData.localAxes) return false;
    }
    return true;
  }

  private keyDown = (event: KeyboardEvent): void => {
    if ((event.target as HTMLElement)?.matches("input, textarea, select")) return;
    this.pressed.add(event.code);
  };
  private keyUp = (event: KeyboardEvent): void => { this.pressed.delete(event.code); };

  private updateFlight(delta: number): void {
    const speed = delta * 5;
    const forward = new THREE.Vector3();
    this.camera.getWorldDirection(forward);
    const right = new THREE.Vector3().crossVectors(forward, this.camera.up).normalize();
    const movement = new THREE.Vector3();
    if (this.pressed.has("KeyW")) movement.add(forward);
    if (this.pressed.has("KeyS")) movement.sub(forward);
    if (this.pressed.has("KeyD")) movement.add(right);
    if (this.pressed.has("KeyA")) movement.sub(right);
    if (this.pressed.has("KeyE")) movement.y += 1;
    if (this.pressed.has("KeyQ")) movement.y -= 1;
    if (movement.lengthSq()) {
      movement.normalize().multiplyScalar(speed);
      this.camera.position.add(movement);
      this.orbit.target.add(movement);
    }
  }

  /**
   * Offscreen render of one `Camera` object, used by the inspector preview (§4.4).
   * A depth map only exists for `rgbd` cameras.
   */
  renderSensorFrame(objectId: number, mode: "rgb" | "depth", canvas: HTMLCanvasElement,
                    limits: SensorLimits = { maxWidth: 640, maxHeight: 480 }): boolean {
    const setup = this.sensorSetup(objectId, limits);
    if (!setup) return false;
    const { rig, width, height } = setup;
    if (mode === "depth" && !rig.depth) return false;
    const target = this.colorTarget(width, height);
    target.texture.colorSpace = mode === "rgb" ? THREE.SRGBColorSpace : THREE.NoColorSpace;
    const pixels = this.colorPixels(width, height);
    this.sensorPass(mode === "rgb" ? "rgb" : "preview", rig, target, pixels);
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) return false;
    const frame = this.sensorImage ??= context.createImageData(width, height);
    for (let row = 0; row < height; row += 1) {
      frame.data.set(pixels.subarray((height - row - 1) * width * 4, (height - row) * width * 4), row * width * 4);
    }
    context.putImageData(frame, 0, 0);
    return true;
  }

  /**
   * One metric depth map for `/captureScene` (§5.10): `width*height` float32 metres
   * along the optical axis, row major with the top row first, `NaN` where the ray
   * hit nothing. Rendered at the sensor's own resolution - callers cannot override it.
   *
   * The `NaN` on a miss is why this pass is not the preview's packed depth: an 8-bit
   * target cannot say "no measurement", and a metre value at `far` = 1000 would
   * quantise to steps of metres.
   */
  renderSensorDepth(objectId: number): SensorDepthFrame | null {
    const setup = this.sensorSetup(objectId, NATIVE_SENSOR);
    if (!setup || !setup.rig.depth) return null;
    const { rig, width, height } = setup;
    const target = this.metersTarget(width, height);
    if (!target) return null;
    const raw = this.metersPixels(width, height);
    this.sensorPass("meters", rig, target, raw);
    const meters = new Float32Array(width * height);
    for (let row = 0; row < height; row += 1) {
      // `readPixels` starts at the bottom left, §5.10 wants the top row first.
      const source = (height - row - 1) * width * 4;
      for (let column = 0; column < width; column += 1) {
        const value = raw[source + column * 4]!;
        // A miss clears to 0, and `> far` cannot survive the frustum; both are no measurement.
        meters[row * width + column] = Number.isFinite(value) && value > 0 && value <= rig.farPlane ? value : Number.NaN;
      }
    }
    return { width, height, meters };
  }

  /**
   * §4.4 (**Q108**): the viewport's current frame without a single editor layer, for the
   * toolbar's screenshot button and for a recording frame (**Q121**). The pixels come back through
   * an offscreen target rather than `canvas.toDataURL()` because that route needs
   * `preserveDrawingBuffer: true`, which would charge every animation frame for the sake of an
   * occasional click - this is the same readback `/captureScene` uses (§5.10), only with the orbit
   * camera and the canvas's own drawing-buffer size.
   *
   * `excludeOverlays` is the one thing a recording asks for and a screenshot never does: with the
   * 「录制辅助图层」 box ticked the take is the screen, selection outline and bounding boxes and all.
   */
  captureViewport(canvas: HTMLCanvasElement, excludeOverlays = true): boolean {
    const size = this.renderer.getDrawingBufferSize(new THREE.Vector2());
    const width = Math.floor(size.x);
    const height = Math.floor(size.y);
    if (this.disposed || width < 1 || height < 1) return false;
    const target = this.makeViewportTarget(width, height);
    if (!target) return false;
    const pixels = this.viewportPixelBuffer(width, height);
    const context = canvas.getContext("2d");
    if (!context) return false;
    // The scene matrices go stale between animation frames while an Attach re-hangs a node,
    // and the orbit camera only updates in the render loop, so neither is guaranteed fresh
    // when a click gets here. Both refreshes touch nothing observable (§4.4: zero side effects).
    this.orbit.update();
    this.scene.updateMatrixWorld(true);
    this.viewportPass(target, pixels, excludeOverlays);
    // Assigning a canvas's size clears it even when the number is the same, and a take lands here
    // thirty times a second, so the resize only happens when the buffer really changed shape.
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const frame = this.viewportImage ??= context.createImageData(width, height);
    for (let row = 0; row < height; row += 1) {
      frame.data.set(pixels.subarray((height - row - 1) * width * 4, (height - row) * width * 4), row * width * 4);
    }
    context.putImageData(frame, 0, 0);
    return true;
  }

  /**
   * One object's world AABB and its pixel rectangle as seen by one camera (§5.11).
   * Rendered at the sensor's own resolution, so `width`/`height` match what
   * `/captureScene` would report for the same camera.
   *
   * `null` means "nothing measurable": no node for either id, or an empty node - §3.7
   * gives an object without a `MetadataRef` no placeholder geometry, and §5.11 asks for
   * exactly that visible node when there is one. A lost GL context lands here too;
   * §5.11 gives this route one 4xx and `capture.ts` names both ids in the message.
   */
  projectObjectBox(objectId: number, cameraId: number): SensorBoxFrame | null {
    // The setup refreshes the world matrices, so the box reads the current frame (§8.5).
    const setup = this.sensorSetup(cameraId, NATIVE_SENSOR);
    if (!setup) return null;
    const box = this.worldAabb(objectId);
    if (!box) return null;
    const { min, max } = box;
    const corners = [
      min.x, min.y, min.z, max.x, min.y, min.z, min.x, max.y, min.z, max.x, max.y, min.z,
      min.x, min.y, max.z, max.x, min.y, max.z, min.x, max.y, max.z, max.x, max.y, max.z,
    ];
    this.sensorCamera.updateMatrixWorld();
    const viewProjection = new THREE.Matrix4()
      .multiplyMatrices(this.sensorCamera.projectionMatrix, this.sensorCamera.matrixWorldInverse);
    return {
      width: setup.width,
      height: setup.height,
      aabb: toSceneBox(min, max),
      ...projectBox(corners, viewProjection.elements, setup.width, setup.height),
    };
  }

  /** Validate the object, size the pass and aim the optical frame at the world pose (§4.4). */
  private sensorSetup(objectId: number, limits: SensorLimits): { rig: CameraRig; width: number; height: number } | null {
    const object = this.objects.get(objectId);
    const node = this.nodes.get(objectId);
    if (!object?.camera || !node || this.disposed || this.renderer.getContext().isContextLost()) return null;
    const rig = cameraRig(object.camera);
    const ratio = Math.min(1, limits.maxWidth / rig.width, limits.maxHeight / rig.height);
    const width = Math.max(1, Math.round(rig.width * ratio));
    const height = Math.max(1, Math.round(rig.height * ratio));
    this.scene.updateMatrixWorld(true);
    node.getWorldPosition(this.sensorCamera.position);
    const rotation = node.getWorldQuaternion(new THREE.Quaternion());
    // Optical +Z forward, +Y up, +X left -> Three.js -Z forward, +Y up.
    this.sensorCamera.up.copy(new THREE.Vector3(0, 1, 0).applyQuaternion(rotation));
    this.sensorCamera.lookAt(this.sensorCamera.position.clone().add(new THREE.Vector3(0, 0, 1).applyQuaternion(rotation)));
    this.sensorCamera.fov = rig.verticalFovDegrees;
    this.sensorCamera.aspect = rig.width / rig.height;
    this.sensorCamera.near = rig.nearPlane;
    this.sensorCamera.far = rig.farPlane;
    this.sensorCamera.updateProjectionMatrix();
    return { rig, width, height };
  }

  /** One offscreen pass: hide editor layers, swap scene state, render, read back, restore (§4.4). */
  private sensorPass(mode: "rgb" | "preview" | "meters", rig: CameraRig, target: THREE.WebGLRenderTarget,
                     pixels: Uint8Array | Float32Array): void {
    const hidden = this.hideOverlays();
    const previousTarget = this.renderer.getRenderTarget();
    const override = this.scene.overrideMaterial;
    const background = this.scene.background;
    const fog = this.scene.fog;
    try {
      this.scene.fog = null;
      if (mode === "preview") {
        this.sensorDepth.uniforms.nearPlane!.value = rig.nearPlane;
        this.sensorDepth.uniforms.farPlane!.value = rig.farPlane;
        this.scene.overrideMaterial = this.sensorDepth;
        this.scene.background = new THREE.Color(0xffffff);
      } else if (mode === "meters") {
        this.scene.overrideMaterial = this.sensorMeters;
        // An unlit background clears the float target to 0, which reads back as `NaN`.
        this.scene.background = null;
      }
      this.renderer.setRenderTarget(target);
      this.renderer.render(this.scene, this.sensorCamera);
      this.renderer.readRenderTargetPixels(target, 0, 0, target.width, target.height, pixels);
    } finally {
      this.renderer.setRenderTarget(previousTarget);
      this.scene.overrideMaterial = override;
      this.scene.background = background;
      this.scene.fog = fog;
      hidden.forEach((overlay) => { overlay.visible = true; });
    }
  }

  /**
   * §4.4 (**Q108**): one clean pass of the viewport's own camera. It differs from
   * `sensorPass` in exactly two ways - the camera and the fact that nothing is swapped:
   * a viewport screenshot has to look like the screen, so the background, the fog and the
   * materials all stay as the render loop leaves them. Only the editor layers are held back,
   * by the same `isEditorOnly()` rule that keeps them out of `/captureScene` - and **Q121**'s
   * 「录制辅助图层」 box is what turns that holding-back off for one frame at a time.
   */
  private viewportPass(target: THREE.WebGLRenderTarget, pixels: Uint8Array, excludeOverlays: boolean): void {
    const hidden = excludeOverlays ? this.hideOverlays() : [];
    const previousTarget = this.renderer.getRenderTarget();
    try {
      this.renderer.setRenderTarget(target);
      this.renderer.render(this.scene, this.camera);
      this.renderer.readRenderTargetPixels(target, 0, 0, target.width, target.height, pixels);
    } finally {
      this.renderer.setRenderTarget(previousTarget);
      hidden.forEach((overlay) => { overlay.visible = true; });
    }
  }

  /**
   * The screenshot's render target, at the canvas's own resolution. Multisampled because
   * the on-screen canvas is (`antialias: true`), and three resolves the samples on readback;
   * a plain target would hand back a visibly jagged copy of a smooth viewport.
   */
  private makeViewportTarget(width: number, height: number): THREE.WebGLRenderTarget | null {
    if (!this.renderer.getContext().isContextLost()) {
      if (!this.viewportTarget || this.viewportTarget.width !== width || this.viewportTarget.height !== height) {
        this.viewportTarget?.dispose();
        this.viewportTarget = new THREE.WebGLRenderTarget(width, height, { samples: 4 });
        // Three skips the sRGB encode when a target is linear, so an unset color space reads
        // back as a dark picture the screen never shows. `/captureScene` sets it per mode.
        this.viewportTarget.texture.colorSpace = THREE.SRGBColorSpace;
        this.viewportImage = null;
      }
      return this.viewportTarget;
    }
    this.callbacks.onError("The WebGL context is lost, so the viewport cannot be captured.");
    return null;
  }

  private viewportPixelBuffer(width: number, height: number): Uint8Array {
    if (!this.viewportPixels || this.viewportPixels.length !== width * height * 4) {
      this.viewportPixels = new Uint8Array(width * height * 4);
      this.viewportImage = null;
    }
    return this.viewportPixels;
  }

  private colorTarget(width: number, height: number): THREE.WebGLRenderTarget {
    if (!this.sensorTarget || this.sensorTarget.width !== width || this.sensorTarget.height !== height) {
      this.sensorTarget?.dispose();
      this.sensorTarget = new THREE.WebGLRenderTarget(width, height);
      this.sensorImage = null;
    }
    return this.sensorTarget;
  }

  private metersTarget(width: number, height: number): THREE.WebGLRenderTarget | null {
    if (!this.sensorFloatTarget || this.sensorFloatTarget.width !== width || this.sensorFloatTarget.height !== height) {
      this.sensorFloatTarget?.dispose();
      this.sensorFloatTarget = null;
      // Metres into a colour attachment need a float-renderable target (WebGL2 +
      // EXT_color_buffer_float). Without it the framebuffer is incomplete and every
      // pixel reads back as a miss, so refuse rather than hand out a blank depth map.
      if (!this.renderer.getContext().getExtension("EXT_color_buffer_float")) {
        this.callbacks.onError("The GPU cannot render a float32 depth map (EXT_color_buffer_float missing).");
        return null;
      }
      this.sensorFloatTarget = new THREE.WebGLRenderTarget(width, height, { type: THREE.FloatType });
    }
    return this.sensorFloatTarget;
  }

  private colorPixels(width: number, height: number): Uint8Array {
    if (!this.sensorPixels || this.sensorPixels.length !== width * height * 4) {
      this.sensorPixels = new Uint8Array(width * height * 4);
      this.sensorImage = null;
    }
    return this.sensorPixels;
  }

  private metersPixels(width: number, height: number): Float32Array {
    if (!this.sensorFloatPixels || this.sensorFloatPixels.length !== width * height * 4) {
      this.sensorFloatPixels = new Float32Array(width * height * 4);
    }
    return this.sensorFloatPixels;
  }

  /** Editor-only layers must never reach a capture (§4.4): boxes, helpers, markers, labels. */
  private hideOverlays(): THREE.Object3D[] {
    const hidden: THREE.Object3D[] = [];
    this.scene.traverse((child) => {
      if (!child.visible) return;
      if (this.isEditorOnly(child)) {
        hidden.push(child);
        child.visible = false;
      }
    });
    return hidden;
  }

  private render = (): void => {
    if (this.disposed) return;
    const start = performance.now();
    const delta = Math.min(this.clock.getDelta(), 0.1);
    this.updateFlight(delta);
    this.orbit.update();
    this.syncSelectionBox();
    // §4.8.5 (Q117): an event marks the volumes stale and this clock catches the poses a driven
    // robot changes; `worldAabb()` is not something to walk every frame (§4.4 says so already).
    if (this.shadowRigDirty || start - this.lastShadowFit >= SHADOW_FIT_INTERVAL_MS) this.fitShadowRigs();
    // §4.8.6 (Q119): the wireframe shares this clock and the reason for it - both walk every
    // geometry box - and it is not walked at all while the checkbox is off.
    if (this.boxesEnabled && start - this.lastBoxSync >= SHADOW_FIT_INTERVAL_MS) this.syncBoundingBoxes();
    // §4.8.7 (Q121): the frame this iteration owes is decided before the draw and handed over after
    // it, so the take's own pass sees the camera, the robot pose and the light rig of this frame.
    const recordFrame = this.dueRecordFrame(start);
    this.renderer.render(this.scene, this.camera);
    this.frames += 1;
    const now = performance.now();
    if (now - this.frameWindow >= 500) {
      this.callbacks.onPerformance(this.frames * 1000 / (now - this.frameWindow), now - start);
      this.frames = 0;
      this.frameWindow = now;
    }
    recordFrame?.();
    this.animation = requestAnimationFrame(this.render);
  };

  private contextLost = (event: Event): void => {
    event.preventDefault();
    this.callbacks.onError("WebGL context lost. Waiting for browser recovery.");
  };

  private disposeObject(object: THREE.Object3D): void {
    object.traverse((child) => {
      const mesh = child as THREE.Mesh;
      mesh.geometry?.dispose();
      const materials = Array.isArray(mesh.material) ? mesh.material : mesh.material ? [mesh.material] : [];
      for (const material of materials) material.dispose();
      // §4.8.5 (Q117): a projecting light owns a depth map and removing the light does not free
      // it, so a world reload that swaps the rig would leave one render target behind per light.
      (child as THREE.Light).shadow?.dispose();
    });
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.animation);
    this.canvas.removeEventListener("pointerdown", this.pick);
    this.canvas.removeEventListener("webglcontextlost", this.contextLost);
    window.removeEventListener("keydown", this.keyDown);
    window.removeEventListener("keyup", this.keyUp);
    this.orbit.dispose();
    for (const node of this.nodes.values()) this.disposeObject(node);
    this.nodes.clear();
    this.objects.clear();
    this.signatures.clear();
    this.models.clear();
    this.robots.clear();
    this.robotTrees.clear();
    this.clearWaypoints();
    this.clearBoundingBoxes();
    // §4.8.7 (Q121): the page is going away with it, so the tap is dropped rather than restored -
    // a disposed context must not be handed one more frame, and nobody is left to look at the screen.
    this.recordTap = null;
    this.reported.clear();
    for (const loading of this.templates.values()) {
      void loading.then((asset) => this.disposeObject(asset)).catch(() => undefined);
    }
    this.templates.clear();
    this.sensorTarget?.dispose();
    this.sensorTarget = null;
    this.viewportTarget?.dispose();
    this.viewportTarget = null;
    this.viewportPixels = null;
    this.viewportImage = null;
    this.sensorFloatTarget?.dispose();
    this.sensorFloatTarget = null;
    this.sensorPixels = null;
    this.sensorFloatPixels = null;
    this.sensorImage = null;
    this.sensorDepth.dispose();
    this.sensorMeters.dispose();
    this.renderer.dispose();
  }
}
