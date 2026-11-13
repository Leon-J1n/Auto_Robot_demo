/**
 * Placing a library asset into the live 现场 (spec v2.1 §4.6).
 *
 * An asset-panel click is a scene edit, not an add-at-the-origin: a 机械臂 takes the
 * pose its own `scene_one_arm` table gives relative to the work table, a 模型 takes a
 * free spot on the table top, and a 夹爪 or 相机 replaces the object of that role the
 * scene already has. Every rule here reads OLS metadata and plain arithmetic, so a
 * unit test needs neither a GPU nor a fetch.
 */
import type {
  CameraProps,
  ObjectRequest,
  Props,
  QuatXyzw,
  RobotAttachmentProps,
  SemanticProps,
  Vec3,
  WorldComponent,
} from "../../contracts/types";
import type { Aabb6, LibraryFileKind, ModelMetadata } from "./library";
import { chineseAssetName, resolveMetadataTarget } from "./library";
import { objVertexBounds } from "./logic";
import { parseUrdf, toolTipLinks, type UrdfRobot } from "./urdf";
import type { World, WorldObjectNode } from "./world";

/** §4.6: an empty 现场 gets its furniture from this OLS scene before anything is placed. */
export const BASELINE_SCENE_ID = 1;

/**
 * +90° about Z - the turn every shipped scene writes on an object standing on the
 * table, arm included. It is what makes a model's local +X point along world +Y.
 */
export const TABLE_YAW: QuatXyzw = [0, 0, Math.SQRT1_2, Math.SQRT1_2];

export function placementQuaternion(asset: AssetInfo): QuatXyzw {
  const geometric = dictionary(asset.metadata.raw.geometric_properties);
  const quaternion = asQuat(geometric?.placement_quaternion_xyzw);
  return quaternion?.some((value) => value !== 0) ? quaternion : TABLE_YAW;
}

const UNIT_SCALE: Vec3 = [1, 1, 1];
const IDENTITY_QUATERNION: QuatXyzw = [0, 0, 0, 1];
const ORIGIN: Vec3 = [0, 0, 0];

/** The intrinsics every shipped scene gives a camera; OLS metadata carries no camera model. */
const DEFAULT_CAMERA_PROPS: Props<CameraProps> = { type: "rgbd", fov: 60, near: 0.01, far: 10, resolution: [640, 480] };

/**
 * Where one asset belongs. `arm` and `gripper` are decided by the metadata's own
 * connect tables - an arm names links it can hand out, a gripper names the link it
 * hangs from - and `camera` by the tag OLS publishes, which is the only place a
 * camera says so (`d435i` and `d455` sit in the 机械臂 id space).
 */
export type AssetRole = "arm" | "gripper" | "camera" | "prop";

/** `scene_one_arm[0]`: the one table an arm is placed by, per the single-arm assumption. */
export interface ArmPlacement {
  percent: Vec3;
  hasPedestal: boolean;
  pedestalId: number | null;
  pedestalScale: Vec3;
}

/** The triple every shipped arm publishes; an arm with no `scene_one_arm` uses it. */
const DEFAULT_ARM_PERCENT: Vec3 = [0.2, 0.3, 0.5];

export interface Box {
  min: Vec3;
  max: Vec3;
}

export interface PlacementReport {
  /** what the user is told, in the same words as the panel's other toasts */
  notice: string;
  added: number[];
  updated: number[];
  removed: number[];
  /** the object to select afterwards: the one that was swapped in or newly stood up */
  selected: number | null;
}

export class PlacementError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PlacementError";
  }
}

/** The three OLS reads placement needs; `LibraryClient` satisfies it as it stands. */
export interface AssetSource {
  model(id: number): Promise<ModelMetadata>;
  robot(id: number): Promise<ModelMetadata>;
  fileText(id: number, path: string, isOnCos: boolean, kind?: LibraryFileKind): Promise<string>;
}

/**
 * The renderer seam placement borrows to measure the real geometry on the stage instead of
 * trusting OLS's declared AABB (§4.6.1; the 2026-11-05 穿模 report: URDF props 25/31 whose
 * metadata box is smaller than their driven reach). `sceneAABB` is the browser's own box -
 * the same measure the wireframe, the selection outline and `/get2DAABB` draw - read from
 * the live scene graph, so an articulated object is measured at its current joint state.
 * `settle` makes a freshly added object measurable: it pushes the world into the scene and
 * waits for that object's geometry to load. Absent in unit tests, which then fall back to
 * the metadata box exactly as before.
 */
export interface BoundsMeasure {
  /** the object's axis-aligned box in the world-file frame, or null when nothing is measured yet */
  sceneAABB(objectId: number): Box | null;
  /** resolve once the object and pending support/obstacle geometry are measurable */
  settle(objectId: number): Promise<void>;
}

export interface PlacementOptions {
  /** the sampling source, injected so a test can pin a spot (§4.6) */
  random?: () => number;
  /** how many candidate centres one click may try before it falls back to the sweep */
  attempts?: number;
  /** run the deterministic edge-aligned sweep once the samples all fail; `0` skips it and leaves random-only placement (Q144) */
  scanSteps?: number;
}

/** One asset as placement sees it: its metadata, its box and its connect tables. */
export interface AssetInfo {
  kind: LibraryFileKind;
  id: number;
  name: string;
  role: AssetRole;
  metadata: ModelMetadata;
  /** the AABB in the asset's own frame, or null when OLS publishes none */
  box: Box | null;
  /** Lowest point of the root's fixed base geometry, in the URDF root frame. */
  baseBottomZ?: number | null;
  /** `robot_driver.properties` minus its `robot_attachment`, or the URDF's own joint table */
  driver: Record<string, unknown> | null;
  childLink: string | null;
  connectJoints: string[];
  connectLinks: string[];
  mountPositions: Vec3[];
  mountQuaternions: QuatXyzw[];
  placement: ArmPlacement | null;
}

/** The work table the scene stands on, plus the surface everything else rests on. */
export interface TableSurface {
  id: number;
  name: string;
  box: Box;
}

/**
 * Read one asset: its metadata, then - only when the metadata does not already name
 * the joints and links - its URDF. The panel names the item it clicked; a furniture
 * read (the pedestal under an arm) has no panel entry, so OLS' own `object_name` stands in.
 *
 * A 模型 whose geometry is an OBJ also has its vertex bounds read, because the metadata AABB OLS
 * publishes for 9 of the 25 top-level OBJ models is authored in an axis frame that does not match
 * the mesh it ships (X and Y swapped for most, the up axis for two; the mug, model 23, is the
 * repro): placement measures that box to reserve a footprint, so a wrong one lets two props that
 * merely touch on paper overlap on screen (Q144). The mesh is the truth, so its bounds win; a model
 * with no readable vertices, and every non-OBJ asset, keeps the metadata box. That read is one
 * cached text fetch per distinct OBJ model, reused for the prop being placed and for the props it
 * must dodge.
 */
export async function readAsset(source: AssetSource, kind: LibraryFileKind, id: number, name?: string): Promise<AssetInfo> {
  const metadata = kind === "robot" ? await source.robot(id) : await source.model(id);
  const driver = dictionary(dictionary(metadata.raw.robot_driver)?.properties);
  const attachment = dictionary(dictionary(driver?.robot_attachment)?.properties);
  const asset: AssetInfo = {
    kind,
    id,
    name: chineseAssetName(metadata.raw) ?? name ?? (typeof metadata.semantic?.object_name === "string" && metadata.semantic.object_name
      ? metadata.semantic.object_name
      : `object_${id}`),
    role: "prop",
    metadata,
    box: metadata.aabb ? localBox(metadata.aabb) : null,
    driver: driver ? withoutAttachment(driver) : null,
    childLink: stringList(attachment?.child_link)[0] ?? null,
    connectJoints: stringList(driver?.available_connect_joints),
    connectLinks: stringList(driver?.available_connect_link),
    mountPositions: vecList(driver?.position),
    mountQuaternions: quatList(driver?.quaternion_xyzw),
    placement: readArmPlacement(metadata.raw.scene_one_arm),
  };
  const hasMetadataDriver = asset.driver !== null;
  if (isArticulated(metadata)) {
    const needsBaseMeasure = asset.kind === "robot" && asset.placement?.hasPedestal === true
      && asset.box !== null && Math.abs(asset.box.min[2]) > 1e-8;
    let robot: UrdfRobot | null = null;
    if (!hasMetadataDriver || needsBaseMeasure) {
      try {
        robot = await readUrdf(source, metadata);
      } catch (error) {
        if (!hasMetadataDriver) throw error;
      }
    }
    if (robot && needsBaseMeasure) {
      try {
        asset.baseBottomZ = await fixedBaseBottom(source, metadata, robot);
      } catch {
        asset.baseBottomZ = null;
      }
    }
    if (!hasMetadataDriver && robot) asset.driver = driverFromUrdf(robot);
    if (asset.kind === "robot" && !hasMetadataDriver && robot) {
      if (!asset.childLink) asset.childLink = robot.roots[0] ?? null;
      if (asset.connectLinks.length === 0) {
        const tips = toolTipLinks(robot);
        asset.connectLinks = tips;
        asset.connectJoints = tips.map((link) => robot.joints.find((joint) => joint.child === link)?.name ?? "");
      }
    }
  }
  if (asset.kind === "model" && isObj(metadata)) {
    const bounds = objVertexBounds(await source.fileText(metadata.modelId, metadata.path, metadata.isOnCos, metadata.kind));
    if (bounds) asset.box = bounds;
  }
  asset.role = classify(asset);
  return asset;
}

/**
 * Which role one asset has. The id space decides first: only a 机械臂 can be an arm,
 * a gripper or a camera, so a URDF-bearing 模型 (a drawer, a cabinet) stays a prop
 * however many joints it has. `model` and `robot` are separate id spaces (§6.11).
 */
function classify(asset: AssetInfo): AssetRole {
  if (asset.kind !== "robot") return "prop";
  if (asset.connectLinks.length > 0 || asset.connectJoints.some((joint) => joint)) return "arm";
  if (hasCameraTag(asset.metadata)) return "camera";
  if (asset.childLink) return "gripper";
  return "prop";
}

/** Both spellings: the live index tags its two cameras in Chinese (`D435i相机,深度相机,…`). */
function hasCameraTag(metadata: ModelMetadata): boolean {
  const tag = dictionary(metadata.raw.semantic_properties)?.tag;
  if (typeof tag !== "string") return false;
  return tag.toLowerCase().includes("camera") || tag.includes("相机");
}

/** `robot_attachment` is lifted out: that is what the `RobotAttachment` component carries. */
function withoutAttachment(driver: Record<string, unknown>): Record<string, unknown> {
  const { robot_attachment, ...rest } = driver;
  return rest;
}

function readArmPlacement(value: unknown): ArmPlacement | null {
  const entry = Array.isArray(value) ? dictionary(value[0]) : null;
  if (!entry) return null;
  const percent = asVec3(entry.robot_pose_percent_compared_to_table_length_center) ?? [...DEFAULT_ARM_PERCENT] as Vec3;
  const scale = asVec3(entry.pedestal_scale) ?? [...UNIT_SCALE] as Vec3;
  const pedestalId = typeof entry.pedestal_id === "number" ? entry.pedestal_id : null;
  return { percent, hasPedestal: entry.has_pedestal === true, pedestalId, pedestalScale: scale };
}

function isArticulated(metadata: ModelMetadata): boolean {
  return metadata.path.toLowerCase().endsWith(".urdf");
}

/** Whether the geometry OLS points at is an OBJ -- the one format placement can bound by reading its vertices. */
function isObj(metadata: ModelMetadata): boolean {
  return metadata.path.toLowerCase().endsWith(".obj");
}

async function readUrdf(source: AssetSource, metadata: ModelMetadata): Promise<UrdfRobot> {
  const text = await source.fileText(metadata.modelId, metadata.path, metadata.isOnCos, metadata.kind);
  return parseUrdf(text);
}

/** The joint table of a URDF that OLS gave no `robot_driver` for (§4.7's reading of the tree). */
function driverFromUrdf(robot: UrdfRobot): Record<string, unknown> {
  const sequence = robot.joints.filter((joint) => joint.type !== "fixed" && !joint.mimic).map((joint) => joint.name);
  const limits: Record<string, [number, number]> = {};
  for (const joint of robot.joints) {
    if (joint.type !== "fixed" && joint.limit) limits[joint.name] = joint.limit;
  }
  // A URDF names no park pose and no connect table, so nothing here claims to be an arm.
  return { role: "passive", joint_sequence: sequence, joint_limits: limits };
}

// ---------------------------------------------------------------------------
// Geometry: an asset's box, the scene's table, and where one goes
// ---------------------------------------------------------------------------

/** The six floats are read as three unordered pairs; both members of a pair may be positive. */
export function localBox(aabb: Aabb6): Box {
  const [x0, x1, y0, y1, z0, z1] = aabb;
  return {
    min: [Math.min(x0, x1), Math.min(y0, y1), Math.min(z0, z1)],
    max: [Math.max(x0, x1), Math.max(y0, y1), Math.max(z0, z1)],
  };
}

/** The box one AABB occupies in the scene frame: the eight corners, rotated and scaled (§4.6). */
export function orientedBox(box: Box, position: Vec3, quaternion: QuatXyzw, scale: Vec3): Box {
  const min: Vec3 = [Infinity, Infinity, Infinity];
  const max: Vec3 = [-Infinity, -Infinity, -Infinity];
  for (const corner of corners(box)) {
    const point = rotate(quaternion, [corner[0] * scale[0], corner[1] * scale[1], corner[2] * scale[2]]);
    for (let axis = 0; axis < 3; axis += 1) {
      min[axis] = Math.min(min[axis]!, position[axis]! + point[axis]!);
      max[axis] = Math.max(max[axis]!, position[axis]! + point[axis]!);
    }
  }
  return { min, max };
}

function corners(box: Box): Vec3[] {
  const out: Vec3[] = [];
  for (const x of [box.min[0], box.max[0]]) {
    for (const y of [box.min[1], box.max[1]]) {
      for (const z of [box.min[2], box.max[2]]) out.push([x, y, z]);
    }
  }
  return out;
}

/** `v + 2·w·(q×v) + 2·q×(q×v)` - the quaternion turn, without importing a scene graph. */
export function rotate(quaternion: QuatXyzw, vector: Vec3): Vec3 {
  const [x, y, z, w] = quaternion;
  const cross = [y * vector[2]! - z * vector[1]!, z * vector[0]! - x * vector[2]!, x * vector[1]! - y * vector[0]!] as Vec3;
  const twice = [
    2 * (w * cross[0]! + y * cross[2]! - z * cross[1]!),
    2 * (w * cross[1]! + z * cross[0]! - x * cross[2]!),
    2 * (w * cross[2]! + x * cross[1]! - y * cross[0]!),
  ] as Vec3;
  return [vector[0]! + twice[0]!, vector[1]! + twice[1]!, vector[2]! + twice[2]!] as Vec3;
}

function size(box: Box): Vec3 {
  return [box.max[0]! - box.min[0]!, box.max[1]! - box.min[1]!, box.max[2]! - box.min[2]!] as Vec3;
}

function center(box: Box): Vec3 {
  return [
    (box.min[0]! + box.max[0]!) / 2,
    (box.min[1]! + box.max[1]!) / 2,
    (box.min[2]! + box.max[2]!) / 2,
  ] as Vec3;
}

/** Two boxes that only touch do not collide; one inside the other does. */
export function overlaps(a: Box, b: Box): boolean {
  return a.min[0]! < b.max[0]! && a.max[0]! > b.min[0]!
    && a.min[1]! < b.max[1]! && a.max[1]! > b.min[1]!
    && a.min[2]! < b.max[2]! && a.max[2]! > b.min[2]!;
}

/**
 * Where the arm's origin goes, from its own `scene_one_arm` triple. OLS ships the
 * definition with the key: 桌子的长边 is the box edge running along X+ on the Y−/Z+
 * side, its 中点 is the centre of that edge, and every percentage is of the
 * corresponding edge length. That middle point is what the three offsets are added to -
 * all three of them, so Y moves towards the table's interior - and the sum is the arm's
 * 原点位置: its own geometry does not move it. `side` mirrors only X, for the second arm.
 */
export function armOrigin(table: Box, percent: Vec3, side = 1): Vec3 {
  const middle = center(table);
  const edge = size(table);
  return [
    middle[0]! + side * percent[0]! * edge[0]!,
    table.min[1]! + percent[1]! * edge[1]!,
    table.max[2]! + percent[2]! * edge[2]!,
  ] as Vec3;
}

/**
 * Measure the base that carries the robot, not a wrist or arm link that happens to
 * hang below it in the parked pose. Only root links and descendants joined by fixed
 * joints belong to this support footprint; movable joints define the articulated arm.
 */
async function fixedBaseBottom(source: AssetSource, metadata: ModelMetadata, robot: UrdfRobot): Promise<number | null> {
  const children = new Map<string, typeof robot.joints>();
  for (const joint of robot.joints) {
    const joints = children.get(joint.parent) ?? [];
    joints.push(joint);
    children.set(joint.parent, joints);
  }
  let bottom = Number.POSITIVE_INFINITY;
  let incomplete = false;
  const visit = async (linkName: string, position: Vec3, rotation: QuatXyzw, ancestors: Set<string>): Promise<void> => {
    if (ancestors.has(linkName)) return;
    const nextAncestors = new Set(ancestors).add(linkName);
    const link = robot.links.find((item) => item.name === linkName);
    for (const visual of link?.visuals ?? []) {
      let bounds: Box | null = null;
      let scale: Vec3 = UNIT_SCALE;
      const geometry = visual.geometry;
      if (geometry.kind === "mesh") {
        if (!geometry.filename.toLowerCase().endsWith(".obj")) {
          incomplete = true;
          continue;
        }
        const text = await source.fileText(metadata.modelId, geometry.filename, metadata.isOnCos, metadata.kind);
        bounds = objVertexBounds(text);
        if (!bounds) incomplete = true;
        scale = geometry.scale;
      } else if (geometry.kind === "box") {
        bounds = { min: [-geometry.size[0] / 2, -geometry.size[1] / 2, -geometry.size[2] / 2],
          max: [geometry.size[0] / 2, geometry.size[1] / 2, geometry.size[2] / 2] };
      } else if (geometry.kind === "cylinder") {
        bounds = { min: [-geometry.radius, -geometry.radius, -geometry.length / 2],
          max: [geometry.radius, geometry.radius, geometry.length / 2] };
      } else {
        bounds = { min: [-geometry.radius, -geometry.radius, -geometry.radius],
          max: [geometry.radius, geometry.radius, geometry.radius] };
      }
      if (!bounds) continue;
      const visualPosition = add(position, rotate(rotation, visual.origin.translation));
      const visualRotation = multiplyQuaternion(rotation, visual.origin.quaternion);
      bottom = Math.min(bottom, orientedBox(bounds, visualPosition, visualRotation, scale).min[2]);
    }
    for (const joint of children.get(linkName) ?? []) {
      if (joint.type !== "fixed") continue;
      const childPosition = add(position, rotate(rotation, joint.origin.translation));
      await visit(joint.child, childPosition, multiplyQuaternion(rotation, joint.origin.quaternion), nextAncestors);
    }
  };
  for (const root of robot.roots) await visit(root, ORIGIN, IDENTITY_QUATERNION, new Set());
  return Number.isFinite(bottom) && !incomplete ? bottom : null;
}

function add(left: Vec3, right: Vec3): Vec3 {
  return [left[0] + right[0], left[1] + right[1], left[2] + right[2]];
}

function multiplyQuaternion(left: QuatXyzw, right: QuatXyzw): QuatXyzw {
  const [x1, y1, z1, w1] = left;
  const [x2, y2, z2, w2] = right;
  return [
    w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
    w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
    w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
  ];
}

/**
 * The pedestal under one arm. Its scale is the one `scene_one_arm` declares, written
 * through unchanged (`[1, 1, 1]` when the key is absent), and the box top is put against
 * the arm's box bottom. Whether that leaves the pedestal's foot above the floor is the
 * declared number's business, not something to correct here.
 */
export function pedestalPose(armAt: Vec3, armBox: Box | null, pedestalBox: Box | null, scale: Vec3,
                             baseBottomZ: number | null = null, quaternion: QuatXyzw = TABLE_YAW): { position: Vec3; scale: Vec3 } {
  const top = baseBottomZ !== null && Number.isFinite(baseBottomZ)
    ? armAt[2]! + baseBottomZ
    : (armBox ? orientedBox(armBox, armAt, quaternion, UNIT_SCALE).min[2]! : armAt[2]!);
  if (!pedestalBox) return { position: [armAt[0]!, armAt[1]!, top] as Vec3, scale };
  return {
    position: [armAt[0]!, armAt[1]!, top - pedestalBox.max[2]! * scale[2]!] as Vec3,
    scale,
  };
}

/**
 * A free spot for one prop on the table top. The sampled point is where the prop's
 * footprint centre goes, so an off-centre mesh still lands centred; its bottom goes
 * on the surface, the way every shipped scene restates a tabletop object's Z.
 *
 * Random samples come first, which is what scatters props naturally. When every one of
 * them runs into something, a deterministic edge-aligned sweep (below) tries every flush
 * centre before the click gives up, so 放不下 means no spot fits rather than "the dice
 * were unlucky" or "the sweep was too coarse" (Q144): a table with one free gap - however
 * narrow - is a table the prop lands in.
 */
export function spotOnTable(
  table: Box,
  footprint: Box,
  occupied: Box[],
  options: PlacementOptions = {},
): { position: Vec3; box: Box } | null {
  const random = options.random ?? Math.random;
  const attempts = options.attempts ?? 64;
  const half = size(footprint);
  const low: Vec3 = [table.min[0]! + half[0]! / 2, table.min[1]! + half[1]! / 2, 0];
  const high: Vec3 = [table.max[0]! - half[0]! / 2, table.max[1]! - half[1]! / 2, 0];
  if (low[0]! > high[0]! || low[1]! > high[1]!) return null;
  const middle = center(footprint);
  // One candidate centre: the box it would occupy, or null when that box hits something.
  const at = (px: number, py: number): { position: Vec3; box: Box } | null => {
    const box: Box = {
      min: [px - half[0]! / 2, py - half[1]! / 2, table.max[2]!] as Vec3,
      max: [px + half[0]! / 2, py + half[1]! / 2, table.max[2]! + half[2]!] as Vec3,
    };
    if (occupied.some((other) => overlaps(box, other))) return null;
    return { position: [px - middle[0]!, py - middle[1]!, table.max[2]! - footprint.min[2]!] as Vec3, box };
  };
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const spot = at(low[0]! + random() * (high[0]! - low[0]!), low[1]! + random() * (high[1]! - low[1]!));
    if (spot) return spot;
  }
  // `scanSteps` is now the on/off switch for the deterministic pass, not its resolution: a test
  // (or a caller) that wants random-only passes 0, and the sweep is complete without a knob.
  if ((options.scanSteps ?? 32) > 0) {
    for (const [px, py] of centreFirstEdges(low, high, occupied, half)) {
      const spot = at(px, py);
      if (spot) return spot;
    }
  }
  return null;
}

/**
 * Candidate centres for the deterministic pass, ordered from the middle of the allowed region
 * outward. A box that fits anywhere on the top fits with one edge flush against an obstacle or
 * the table margin: slide any free centre straight toward the near edge and the first position
 * it reaches is a flush one, and that still clears every box (`overlaps` is strict, so touching
 * costs nothing). Those flush centres are `obstacle.min − half` / `obstacle.max + half` plus the
 * two margins and the middle - a finite, fixed set, so the pass is deterministic and, unlike a
 * fixed grid, complete: it cannot miss a gap narrower than a grid cell would have been. That is
 * the 2026-11-05 follow-up to Q144 - props refused while the top still showed room, because the
 * old `(steps+1)²` grid left a whole cell's width between its samples.
 */
function centreFirstEdges(low: Vec3, high: Vec3, occupied: Box[], half: Vec3): Array<[number, number]> {
  // `half` is the footprint's full extent; a flush centre is one true half-extent off an edge.
  const xs = edgeCentres(low[0]!, high[0]!, occupied, 0, half[0]! / 2);
  const ys = edgeCentres(low[1]!, high[1]!, occupied, 1, half[1]! / 2);
  const centre: [number, number] = [(low[0]! + high[0]!) / 2, (low[1]! + high[1]!) / 2];
  const pairs: Array<[number, number]> = [];
  for (const x of xs) for (const y of ys) pairs.push([x, y]);
  const fromCentre = (pair: [number, number]): number => (pair[0]! - centre[0]!) ** 2 + (pair[1]! - centre[1]!) ** 2;
  return pairs.sort((a, b) => fromCentre(a) - fromCentre(b));
}

/**
 * Every footprint centre that puts one edge flush against an obstacle edge or a table margin,
 * kept inside `[min, max]`. The margins and the middle are always offered so the pass still runs
 * when the region carries no obstacle on this axis.
 */
function edgeCentres(min: number, max: number, occupied: Box[], axis: 0 | 1, half: number): number[] {
  const values = new Set<number>([min, (min + max) / 2, max]);
  for (const other of occupied) {
    for (const centre of [other.min[axis]! - half, other.max[axis]! + half]) {
      if (centre >= min && centre <= max) values.add(centre);
    }
  }
  return [...values];
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

/**
 * Replace the components this placement owns and keep every other one exactly as the
 * scene has it (§3.8): an arm swap must not drop that object's `LocalAxes`, and a
 * camera swap must not rewrite the intrinsics that were authored for that viewpoint.
 */
export function rewriteComponents(node: WorldObjectNode | null, patches: WorldComponent[]): WorldComponent[] {
  const kept = (node?.rawComponents ?? []).map((component) => {
    const replacement = patches.find((patch) => patch.component_type === component?.component_type);
    return replacement ?? component;
  });
  const types = new Set((node?.rawComponents ?? []).map((component) => component?.component_type));
  return [...kept.filter(Boolean), ...patches.filter((patch) => !types.has(patch.component_type))];
}

/**
 * Each component gets its own copy of the numbers it is built from: `rawComponents`
 * keeps whatever array it is handed (§3.8), so two objects must never share one.
 */
function transform(position: Vec3, quaternion: QuatXyzw, scale: Vec3, parent: number | null): WorldComponent {
  return {
    component_type: "Transform",
    properties: {
      position: [...position] as Vec3,
      quaternion_xyzw: [...quaternion] as QuatXyzw,
      scale: [...scale] as Vec3,
      parent,
    },
  };
}

function metadataRef(asset: AssetInfo): WorldComponent {
  return { component_type: "MetadataRef", properties: { path: `$LIBRARY_SERVER/${asset.id}`, type: asset.kind } };
}

function semantic(asset: AssetInfo, category: string, support: number | null): WorldComponent<Props<SemanticProps>> {
  return { component_type: "Semantic", properties: { semantic_name: asset.name, category, support } };
}

function meshRenderer(): WorldComponent {
  return { component_type: "MeshRenderer", properties: {} };
}

/** §4.10 (Q165): the asset's `physical_properties` verbatim minus `scale`; keys it lacks keep the instance's own. */
function physicalProperties(asset: AssetInfo, node?: WorldObjectNode | null): WorldComponent {
  const current = node?.rawComponents.find((component) => component?.component_type === "PhysicalProperties");
  const properties: Record<string, unknown> = { ...dictionary(current?.properties) };
  const declared = dictionary(asset.metadata.raw.physical_properties);
  if (declared) {
    const copy = structuredClone(declared);
    delete copy.scale;
    Object.assign(properties, copy);
  }
  return { component_type: "PhysicalProperties", properties };
}

/** The driver component, or nothing at all for a model with no joints to drive. */
function robotDriver(asset: AssetInfo): WorldComponent | null {
  return asset.driver ? { component_type: "RobotDriver", properties: { ...asset.driver } } : null;
}

/** The tables that say how one arm hands out its links. */
export interface AssetMounts {
  connectJoints: string[];
  connectLinks: string[];
  mountPositions: Vec3[];
  mountQuaternions: QuatXyzw[];
}

/**
 * One mount: the arm's own connect tables name the link and the pose on it, and the
 * gripper's `robot_attachment.properties.child_link` names the link of its own tree
 * that the pose hangs from. With no metadata to read, that link is the URDF's root
 * and the pose is the link's own origin (§4.6).
 */
export function mountOn(arm: AssetMounts, childLink: string | null, armObjectId: number, index = 0): WorldComponent<Props<RobotAttachmentProps>> {
  const link = arm.connectLinks[index] ?? null;
  return {
    component_type: "RobotAttachment",
    properties: {
      parent_object_id: armObjectId,
      parent_joint: arm.connectJoints[index] || undefined,
      parent_link: link ?? undefined,
      child_link: childLink ?? undefined,
      connection_type: "fixed",
      transform: {
        position: [...(arm.mountPositions[index] ?? ORIGIN)] as Vec3,
        quaternion_xyzw: [...(arm.mountQuaternions[index] ?? IDENTITY_QUATERNION)] as QuatXyzw,
        reference_frame: link ? "parent_link" : "parent_object",
      },
    },
  };
}

/** The mount tables an arm object already in the scene carries, read back out of its driver. */
function mountsOfNode(node: WorldObjectNode): AssetMounts {
  const driver = node.robotDriver as Record<string, unknown> | undefined;
  return {
    connectJoints: stringList(driver?.available_connect_joints),
    connectLinks: stringList(driver?.available_connect_link),
    mountPositions: vecList(driver?.position),
    mountQuaternions: quatList(driver?.quaternion_xyzw),
  };
}

/** The full component list of a freshly added arm: no `MeshRenderer`, the way every template writes it. */
export function armComponents(asset: AssetInfo, position: Vec3): WorldComponent[] {
  const driver = robotDriver(asset);
  return [
    transform(position, placementQuaternion(asset), UNIT_SCALE, null),
    metadataRef(asset),
    ...(driver ? [driver] : []),
    semantic(asset, "robot", null),
    physicalProperties(asset),
  ];
}

/** A gripper the scene has none of: it hangs from the arm, so its own Transform is only a parent and a scale. */
export function gripperComponents(asset: AssetInfo, mount: WorldComponent<Props<RobotAttachmentProps>>): WorldComponent[] {
  const driver = robotDriver(asset);
  return [
    { component_type: "Transform", properties: { scale: [...UNIT_SCALE] as Vec3, parent: mount.properties.parent_object_id ?? null } },
    metadataRef(asset),
    ...(driver ? [driver] : []),
    mount,
    semantic(asset, "end_effector", null),
    physicalProperties(asset),
  ];
}

export function propComponents(asset: AssetInfo, position: Vec3, support: number, camera = false): WorldComponent[] {
  const driver = robotDriver(asset);
  return [
    transform(position, placementQuaternion(asset), UNIT_SCALE, null),
    metadataRef(asset),
    meshRenderer(),
    ...(driver ? [driver] : []),
    ...(camera ? [{ component_type: "Camera", properties: { ...DEFAULT_CAMERA_PROPS } } as WorldComponent] : []),
    semantic(asset, camera ? "sensor" : "object", support),
    physicalProperties(asset),
  ];
}

export function pedestalComponents(asset: AssetInfo, position: Vec3, scale: Vec3): WorldComponent[] {
  return [
    transform(position, IDENTITY_QUATERNION, scale, null),
    metadataRef(asset),
    meshRenderer(),
    semantic(asset, "support", null),
    physicalProperties(asset),
  ];
}

// ---------------------------------------------------------------------------
// Scene queries
// ---------------------------------------------------------------------------

/** The object the scene stands its furniture on: `work_table` first, `surface` after. */
export function findTableObject(world: World): WorldObjectNode | null {
  return world.objects.find((node) => node.semantic?.semantic_name === "work_table")
    ?? world.objects.find((node) => node.semantic?.category === "surface")
    ?? null;
}

/**
 * Every object the scene drives as an arm: the ones that hand out connect joints and
 * hang from nothing. A 夹爪 also names a base joint it can hand out (`robotiq_2f85_base_joint`
 * in every shipped template), so the attachment is what tells the two roles apart.
 */
export function findArmObjects(world: World): WorldObjectNode[] {
  return world.objects.filter((node) => node.attachment === undefined
    && (node.robotDriver?.available_connect_joints ?? []).some((joint) => typeof joint === "string" && joint));
}

/** The end effectors an arm (or several) already carries. */
export function findGripperObjects(world: World): WorldObjectNode[] {
  const armIds = new Set(findArmObjects(world).map((node) => node.id));
  return world.objects.filter((node) => node.attachment !== undefined && armIds.has(node.attachment.parent_object_id as number));
}

export function findCameraObjects(world: World): WorldObjectNode[] {
  return world.objects.filter((node) => node.camera !== undefined);
}

/** The pedestal furniture of the arm the scene has: `support` is what every template names it. */
function findPedestalObjects(world: World): WorldObjectNode[] {
  return world.objects.filter((node) => node.semantic?.category === "support");
}

/**
 * The arm and its kinematic assembly - the gripper it hands its links to and the sensor it wears
 * on the wrist - the things a new prop is NOT measured against (§4.6.1, **Q137**). The closure
 * walks both edges a shipped scene uses to hang something on the arm (`RobotAttachment.parent_object_id`
 * and `Transform.parent`), but only along members that live in the **robot** id space: those move
 * with the arm every frame, so treating them as static obstacles would eat most of the table. A
 * `type:model` prop the hand happens to be carrying - the wrist-mounted microwave (model 30), a
 * gripped pen - is a thing standing on the stage: it keeps its own box and stays in the collision
 * set, otherwise a freshly placed prop lands right inside it (the 2026-11-05 穿模 report; the old
 * closure followed every edge regardless of kind and swallowed wrist-mounted props). The pedestal
 * (`category=support`) and the standalone overhead camera are never reached here - they are not
 * under the arm - so they stay in the collision set exactly as before.
 */
export function armFamilyIds(world: World): Set<number> {
  // Only a member of the robot id space moves with the arm and is therefore exempt from collision.
  const ridesTheArm = (id: number): boolean => world.get(id)?.metadataRef?.type === "robot";
  const family = new Set<number>();
  const pending = findArmObjects(world).map((node) => node.id);
  while (pending.length > 0) {
    const id = pending.pop()!;
    if (family.has(id)) continue;
    family.add(id);
    for (const childId of world.childrenOf(id)) {
      if (ridesTheArm(childId)) pending.push(childId);
    }
    for (const node of world.objects) {
      if (node.attachment?.parent_object_id === id && node.metadataRef?.type === "robot") pending.push(node.id);
    }
  }
  return family;
}

/** The other things on the stage - what a new prop must not run into (§4.6.1; the arm family is not on this list). */
async function occupiedBoxes(
  world: World,
  source: AssetSource,
  tableId: number,
  cache: Map<string, AssetInfo>,
  measure?: BoundsMeasure,
  excludeId?: number,
): Promise<Box[]> {
  const skip = armFamilyIds(world);
  const boxes: Box[] = [];
  for (const node of world.objects) {
    if (node.id === tableId || node.id === excludeId || node.metadataRef === undefined || skip.has(node.id)) continue;
    // §4.6.1: an object already on the stage is measured from the browser's own box, which
    // already carries its transform, rotation and - for an articulated one - its current joint
    // state. Only one the renderer has not built (or a test with no seam) falls back to OLS.
    if (measure) await measure.settle(node.id);
    const measured = measure?.sceneAABB(node.id) ?? null;
    if (measured) {
      boxes.push(measured);
      continue;
    }
    let asset: AssetInfo | null;
    try {
      asset = await readMetadataAsset(source, node.metadataRef.path, node.metadataRef.type, node.name, cache);
    } catch {
      continue;
    }
    if (!asset?.box) continue;
    boxes.push(orientedBox(asset.box, node.transform.position, node.transform.quaternion, node.transform.scale));
  }
  return boxes;
}

/** The asset a `MetadataRef` in the scene points at, without the classification a click needs. */
async function readMetadataAsset(
  source: AssetSource,
  path: string,
  type: string | undefined,
  name: string,
  cache: Map<string, AssetInfo>,
): Promise<AssetInfo | null> {
  const target = resolveMetadataTarget(path, type);
  const key = `${target.kind}:${target.id}`;
  const cached = cache.get(key);
  if (cached) return cached;
  const asset = await readAsset(source, target.kind, target.id, name);
  cache.set(key, asset);
  return asset;
}

// ---------------------------------------------------------------------------
// The click
// ---------------------------------------------------------------------------

/**
 * One asset-panel click, applied to `world` (spec v2.1 §4.6). The caller owns the
 * scene, so this writes through the browser's local edit paths - `scene_sub_version`
 * advances and `scene_version` does not (Q114 ②) - and reports what it did.
 */
export async function placeAsset(
  world: World,
  source: AssetSource,
  kind: LibraryFileKind,
  id: number,
  name: string,
  options: PlacementOptions = {},
  measure?: BoundsMeasure,
): Promise<PlacementReport> {
  const cache = new Map<string, AssetInfo>();
  const asset = await readAsset(source, kind, id, name);
  const report: PlacementReport = { notice: "", added: [], updated: [], removed: [], selected: null };
  // Replacements preserve their existing support and never need the table's asset.
  if (asset.role === "gripper") {
    await placeGrippers(world, source, asset, cache, report);
    return report;
  }
  if (asset.role === "camera") {
    const cameras = findCameraObjects(world);
    if (cameras.length > 0) {
      for (const node of cameras) {
        write(world, node.id, { object_name_in_scene: node.name, components: rewriteComponents(node, [metadataRef(asset), physicalProperties(asset, node)]) }, report);
      }
      report.selected = cameras[0]!.id;
      report.notice = cameras.length === 1
        ? `已把相机换成「${asset.name}」`
        : `已把 ${cameras.length} 个相机换成「${asset.name}」`;
      return report;
    }
  }
  const tableNode = findTableObject(world);
  if (!tableNode) throw new PlacementError(`场景里没有工作台，放不了「${name}」`);
  const tableRef = tableNode.metadataRef;
  if (!tableRef) throw new PlacementError(`工作台「${tableNode.name}」没有元数据，量不出桌面`);
  if (measure) await measure.settle(tableNode.id);
  // §4.6.1: measure the table from the browser's own box when the stage can supply it; only a
  // table the renderer has not built yet - or a unit test with no seam - falls back to OLS's AABB.
  const measuredTable = measure?.sceneAABB(tableNode.id) ?? null;
  let tableBox = measuredTable;
  if (!tableBox) {
    const tableAsset = await readMetadataAsset(source, tableRef.path, tableRef.type, tableNode.name, cache);
    if (!tableAsset?.box) throw new PlacementError(`工作台「${tableNode.name}」没有可读的包围盒`);
    tableBox = orientedBox(tableAsset.box, tableNode.transform.position, tableNode.transform.quaternion, tableNode.transform.scale);
  }
  const table: TableSurface = {
    id: tableNode.id,
    name: tableNode.name,
    box: tableBox,
  };

  switch (asset.role) {
    case "arm":
      await placeArms(world, source, asset, table, report);
      break;
    case "camera":
      await placeProp(world, source, asset, table, cache, options, report, true, measure);
      break;
    default:
      await placeProp(world, source, asset, table, cache, options, report, false, measure);
  }
  return report;
}

/**
 * Swap in the arm. Every arm object the scene has keeps its identity - the gripper
 * that hangs from it names it by id - and only its asset, driver and pose change; a
 * second arm mirrors the first across the long-edge midpoint, which is how the
 * shipped dual-arm templates sit.
 */
async function placeArms(
  world: World,
  source: AssetSource,
  asset: AssetInfo,
  table: TableSurface,
  report: PlacementReport,
): Promise<void> {
  const armIds = findArmObjects(world).map((node) => node.id);
  const placement = asset.placement;
  const percent = placement?.percent ?? ([...DEFAULT_ARM_PERCENT] as Vec3);
  // Every read happens before the first write: a swap that failed halfway through
  // would otherwise leave the scene holding an arm with nothing standing under it.
  const pedestal = placement?.hasPedestal && placement.pedestalId !== null
    ? await readAsset(source, "robot", placement.pedestalId)
    : null;
  const first = armOrigin(table.box, percent, 1);
  // A library arm replaces the assembly with one instance, including integrated dual arms.
  for (const id of armIds.splice(1)) {
    report.removed.push(...world.subtreeOf(id));
    world.removeLocalSubtree(id);
  }
  armIds.forEach((id, index) => {
    const node = world.get(id);
    if (!node) return;
    const position = armOrigin(table.box, percent, index % 2 === 0 ? 1 : -1);
    const patches: WorldComponent[] = [
      transform(position, placementQuaternion(asset), UNIT_SCALE, node.transform.parent),
      metadataRef(asset),
      semantic(asset, "robot", null),
      physicalProperties(asset, node),
    ];
    const driver = robotDriver(asset);
    if (driver) patches.push(driver);
    write(world, id, { object_name_in_scene: asset.name, components: rewriteComponents(node, patches) }, report);
    remountGrippers(world, asset, id, report);
  });
  if (armIds.length === 0) {
    const added = world.addLocalObject({ object_name_in_scene: asset.name, components: armComponents(asset, first) });
    report.added.push(added);
    report.notice = `已把机械臂「${asset.name}」摆到桌边`;
  } else {
    report.notice = armIds.length === 1
      ? `已把机械臂换成「${asset.name}」`
      : `已把 ${armIds.length} 台机械臂换成「${asset.name}」`;
  }
  // The thing the click was about is what the panel selects afterwards, pedestal or not.
  report.selected = armIds[0] ?? report.added[0] ?? null;
  placePedestal(world, asset, pedestal, first, report);
}

/** Re-hang every end effector on the arm it already hangs from, against the new arm's connect tables. */
function remountGrippers(world: World, arm: AssetInfo, armId: number, report: PlacementReport): void {
  const attached = world.objects
    .filter((node) => node.attachment?.parent_object_id === armId)
    .map((node) => node.id);
  const slots = Math.max(1, arm.connectJoints.length, arm.connectLinks.length);
  for (const [ordinal, id] of attached.entries()) {
    const node = world.get(id);
    if (!node?.attachment) continue;
    if (ordinal >= slots) {
      report.removed.push(...world.subtreeOf(id));
      world.removeLocalSubtree(id);
      continue;
    }
    const index = ordinal;
    // The child link belongs to the gripper's own tree, so the one it wrote stays.
    const mount = mountOn(arm, node.attachment.child_link ?? null, armId, index);
    const patches: WorldComponent[] = [
      { component_type: "Transform", properties: { scale: node.transform.scale, parent: armId } },
      mount,
    ];
    write(world, id, { object_name_in_scene: node.name, components: rewriteComponents(node, patches) }, report);
  }
}

/**
 * The furniture the arm stands on: rewritten in place when the scene already has one,
 * added when it does not, and dropped when the new arm needs none - the shipped piper
 * templates have no pedestal at all.
 */
function placePedestal(world: World, arm: AssetInfo, pedestal: AssetInfo | null, armAt: Vec3, report: PlacementReport): void {
  const existing = findPedestalObjects(world).map((node) => node.id);
  const placement = arm.placement;
  if (!placement?.hasPedestal || !pedestal) {
    for (const id of existing) {
      world.removeLocalSubtree(id);
      report.removed.push(id);
    }
    return;
  }
  const pose = pedestalPose(armAt, arm.box, pedestal.box, placement.pedestalScale, arm.baseBottomZ ?? null, placementQuaternion(arm));
  const patches: WorldComponent[] = [
    transform(pose.position, IDENTITY_QUATERNION, pose.scale, null),
    metadataRef(pedestal),
    semantic(pedestal, "support", null),
    physicalProperties(pedestal),
  ];
  if (existing.length === 0) {
    report.added.push(world.addLocalObject({ object_name_in_scene: pedestal.name, components: rewriteComponents(null, patches) }));
    return;
  }
  for (const id of existing) {
    const node = world.get(id);
    if (!node) continue;
    write(world, id, {
      object_name_in_scene: pedestal.name,
      components: rewriteComponents(node, [
        transform(pose.position, IDENTITY_QUATERNION, pose.scale, node.transform.parent),
        metadataRef(pedestal),
        semantic(pedestal, "support", null),
        physicalProperties(pedestal, node),
      ]),
    }, report);
  }
}

/**
 * The mount tables of the arm a gripper hangs from. A scene file names only the joint
 * (`available_connect_joints` - the shipped templates carry no link list), so when it
 * leaves the link out the arm's own metadata answers for it; what the scene does
 * carry stands first, because that is what the Agent wrote.
 */
async function mountsOfArm(node: WorldObjectNode, source: AssetSource, cache: Map<string, AssetInfo>): Promise<AssetMounts> {
  const scene = mountsOfNode(node);
  const ref = node.metadataRef;
  if (scene.connectLinks.length > 0 || ref === undefined) return scene;
  let asset: AssetInfo | null;
  try {
    asset = await readMetadataAsset(source, ref.path, ref.type, node.name, cache);
  } catch {
    return scene;
  }
  if (!asset || asset.connectLinks.length === 0) return scene;
  return {
    connectJoints: asset.connectJoints,
    connectLinks: asset.connectLinks,
    mountPositions: asset.mountPositions,
    mountQuaternions: asset.mountQuaternions,
  };
}

async function placeGrippers(
  world: World,
  source: AssetSource,
  asset: AssetInfo,
  cache: Map<string, AssetInfo>,
  report: PlacementReport,
): Promise<void> {
  const arms = findArmObjects(world);
  if (arms.length === 0) throw new PlacementError(`场景里没有机械臂，「${asset.name}」挂不上去`);
  const attached = findGripperObjects(world).map((node) => node.id);
  if (attached.length === 0) {
    const arm = arms[0]!;
    const mount = mountOn(await mountsOfArm(arm, source, cache), asset.childLink, arm.id);
    report.selected = world.addLocalObject({ object_name_in_scene: asset.name, components: gripperComponents(asset, mount) });
    report.added.push(report.selected);
    report.notice = `已把夹爪「${asset.name}」挂到机械臂上`;
    return;
  }
  for (const id of attached) {
    const node = world.get(id);
    if (!node?.attachment) continue;
    const patches: WorldComponent[] = [metadataRef(asset), physicalProperties(asset, node)];
    const driver = robotDriver(asset);
    if (driver) patches.push(driver);
    const parent = world.get(node.attachment.parent_object_id as number);
    if (parent) {
      const mounts = await mountsOfArm(parent, source, cache);
      const index = Math.max(0, mounts.connectJoints.indexOf(node.attachment.parent_joint ?? ""));
      patches.push(mountOn(mounts, asset.childLink ?? node.attachment.child_link ?? null, parent.id, index));
    }
    write(world, id, { object_name_in_scene: asset.name, components: rewriteComponents(node, patches) }, report);
  }
  report.selected = attached[0]!;
  report.notice = attached.length === 1
    ? `已把夹爪换成「${asset.name}」`
    : `已把 ${attached.length} 个夹爪换成「${asset.name}」`;
}

async function placeProp(
  world: World,
  source: AssetSource,
  asset: AssetInfo,
  table: TableSurface,
  cache: Map<string, AssetInfo>,
  options: PlacementOptions,
  report: PlacementReport,
  camera = false,
  measure?: BoundsMeasure,
): Promise<void> {
  // §4.6.1 (2026-11-05): stand the prop up at the world origin so the renderer can build it,
  // measure the footprint from the browser's own box - the one it would draw the wireframe from,
  // at the object's current joint state - then move it to a free spot. `world.placeLocal` makes
  // the whole measure-then-place one sub-version step, so a refused click leaves the scene exactly
  // as it was instead of churning the four-tuple.
  const standing: ObjectRequest = {
    object_name_in_scene: asset.name,
    components: propComponents(asset, ORIGIN, table.id, camera),
  };
  const id = await world.placeLocal(
    standing,
    // Build the object on the stage before measuring. With no seam (a unit test) there is
    // nothing to wait for and nothing to build.
    measure ? async (provisional) => { await measure.settle(provisional); } : async () => undefined,
    async (provisional) => {
      // The browser box wins; only an object the renderer has not measured yet falls back to the
      // metadata AABB - OLS's box, or the OBJ vertices Q144 measures - exactly as before.
      const measured = measure?.sceneAABB(provisional) ?? null;
      const footprint = measured ?? (asset.box ? orientedBox(asset.box, ORIGIN, placementQuaternion(asset), UNIT_SCALE) : null);
      if (!footprint) throw new PlacementError(`「${asset.name}」的元数据里没有包围盒，放不上桌`);
      const occupied = await occupiedBoxes(world, source, table.id, cache, measure, provisional);
      const spot = spotOnTable(table.box, footprint, occupied, options);
      if (!spot) throw new PlacementError(`桌面上放不下「${asset.name}」，请先把别的模型挪开`);
      return {
        object_name_in_scene: asset.name,
        components: propComponents(asset, spot.position, table.id, camera),
      };
    },
  );
  report.selected = id;
  report.added.push(id);
  report.notice = camera ? `场景里本来没有相机，已把「${asset.name}」摆到桌面上` : `已把「${asset.name}」摆到桌面上`;
}

/** One browser-local rewrite of an object that is already on the stage (§4.8, Q114 ②). */
function write(world: World, id: number, object: ObjectRequest, report: PlacementReport): void {
  world.updateLocalItem(id, object);
  report.updated.push(id);
}

// ---------------------------------------------------------------------------
// Reading untyped OLS JSON
// ---------------------------------------------------------------------------

function dictionary(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && !!item) : [];
}

function asVec3(value: unknown): Vec3 | null {
  if (!Array.isArray(value) || value.length !== 3) return null;
  return value.every((item) => typeof item === "number" && Number.isFinite(item)) ? [...value] as Vec3 : null;
}

function asQuat(value: unknown): QuatXyzw | null {
  if (!Array.isArray(value) || value.length !== 4) return null;
  return value.every((item) => typeof item === "number" && Number.isFinite(item)) ? [...value] as QuatXyzw : null;
}

function vecList(value: unknown): Vec3[] {
  return Array.isArray(value) ? value.map(asVec3).filter((item): item is Vec3 => item !== null) : [];
}

function quatList(value: unknown): QuatXyzw[] {
  return Array.isArray(value) ? value.map(asQuat).filter((item): item is QuatXyzw => item !== null) : [];
}
