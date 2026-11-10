/**
 * Browser-side world model (spec v2.1 §3, §5.5, §5.7).
 * The browser is the only authority on the scene, so validation, normalisation,
 * id allocation and the scene_version counter all live here.
 */
import type {
  AddObjectResult,
  CameraProps,
  LightProps,
  LocalAxesProps,
  MeshRendererProps,
  ObjectRequest,
  QuatXyzw,
  RejectedObject,
  RemoveObjectResult,
  RobotAttachmentProps,
  RobotDriverProps,
  SemanticProps,
  TransformProps,
  Vec3,
  WorldApplyResult,
  WorldComponent,
  WorldFile,
  WorldObject,
} from "../../contracts/types";
import { localAxesRig } from "./logic";

/** Traj/scene root sentinel 0xFFFFFFFF maps to a null parent (§2.5). */
export const SCENE_ROOT_SENTINEL = 0xffffffff;
export const SUPPORTED_SCHEMA_VERSION = 1;

const IDENTITY_POSITION: Vec3 = [0, 0, 0];
const IDENTITY_QUATERNION: QuatXyzw = [0, 0, 0, 1];
const IDENTITY_SCALE: Vec3 = [1, 1, 1];

export class WorldError extends Error {
  constructor(readonly code: string, message: string, readonly status: number = 400) {
    super(message);
    this.name = "WorldError";
  }
}

/** uint32 ids only: no negatives, no fractions, no strings (§2.1). */
export function isUintId(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 0xffffffff;
}

function readVec3(value: unknown, fallback: Vec3, label: string, warn: (message: string) => void): Vec3 {
  if (Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === "number" && Number.isFinite(item))) {
    return [value[0] as number, value[1] as number, value[2] as number];
  }
  if (value !== undefined) warn(`${label} is malformed, using ${fallback.join(",")}`);
  return [...fallback] as Vec3;
}

function readQuaternion(value: unknown, warn: (message: string) => void): QuatXyzw {
  if (Array.isArray(value) && value.length === 4 && value.every((item) => typeof item === "number" && Number.isFinite(item))) {
    return [value[0] as number, value[1] as number, value[2] as number, value[3] as number];
  }
  if (value !== undefined) warn("quaternion_xyzw is malformed, using identity");
  return [...IDENTITY_QUATERNION] as QuatXyzw;
}

function readScale(value: unknown, warn: (message: string) => void): Vec3 {
  const scale = readVec3(value, IDENTITY_SCALE, "scale", warn);
  if (scale.some((component) => component <= 0)) {
    warn("scale must be greater than zero, using 1,1,1");
    return [...IDENTITY_SCALE] as Vec3;
  }
  return scale;
}

function readParent(value: unknown, warn: (message: string) => void): number | null {
  if (value === null || value === undefined) return null;
  if (value === SCENE_ROOT_SENTINEL) return null;
  if (!isUintId(value)) {
    warn(`parent ${JSON.stringify(value)} is not a uint32, treated as scene root`);
    return null;
  }
  return value;
}

export interface NormalizedTransform {
  position: Vec3;
  quaternion: QuatXyzw;
  scale: Vec3;
  parent: number | null;
}

/**
 * Where an accepted `RobotAttachment` hangs its object. `transform.reference_frame`
 * names the frame the mount pose is written in, and the exporter writes
 * "parent_link": a link of the parent robot's URDF, so the pose is read in that
 * link's own coordinate system (§ 场景文件描述 §4.8).
 */
export interface AttachmentMount {
  /** the parent object the mount was accepted against; equals `Transform.parent` */
  objectId: number;
  /** the URDF link of that robot to hang from, or null for the parent object node itself */
  link: string | null;
}

export interface WorldObjectNode {
  id: number;
  name: string;
  transform: NormalizedTransform;
  metadataRef?: { path: string; type?: string };
  meshRenderer?: MeshRendererProps;
  semantic?: SemanticProps;
  light?: LightProps;
  camera?: CameraProps;
  robotDriver?: RobotDriverProps;
  /** the local-frame axes overlay of §4.9 (Q130); its own defaults and warnings live in `logic.ts` */
  localAxes?: LocalAxesProps;
  /** the mount pose, when it is consistent with `Transform.parent` (§ 场景文件描述 §4.8) */
  attachment?: RobotAttachmentProps;
  /** how to read that pose, once the attachment has been accepted */
  attachmentMount?: AttachmentMount;
  /** component list exactly as received, including unknown types and unknown fields (§3.8) */
  rawComponents: WorldComponent[];
}

const KNOWN_COMPONENT_TYPES = new Set([
  "Transform", "MetadataRef", "MeshRenderer", "Semantic", "Light", "Camera", "RobotDriver", "RobotAttachment", "LocalAxes",
]);

interface Hierarchy {
  byId: Map<number, WorldObjectNode>;
  childrenOf: Map<number, number[]>;
  rejections: RejectedObject[];
}

/** §3.8 keeps the source dictionary, so typed accessors read the fields they know. */
function asProps<T>(value: unknown): T | undefined {
  return value && typeof value === "object" ? (value as T) : undefined;
}

function normalizeObject(raw: unknown, index: number): { node: WorldObjectNode; warnings: string[] } | { rejection: RejectedObject } {
  if (!raw || typeof raw !== "object") return { rejection: { object_id_in_scene: null, index, reason: "OBJECT_NOT_AN_OBJECT" } };
  const entry = raw as Record<string, unknown>;
  if (!isUintId(entry.object_id_in_scene)) {
    return { rejection: { object_id_in_scene: null, index, reason: `INVALID_OBJECT_ID:${JSON.stringify(entry.object_id_in_scene)}` } };
  }
  const id = entry.object_id_in_scene;
  const warnings: string[] = [];
  const warn = (message: string) => {
    warnings.push(message);
    console.warn(`[world] object ${id}: ${message}`);
  };
  const rawComponents = Array.isArray(entry.components) ? (entry.components as WorldComponent[]) : [];
  if (!Array.isArray(entry.components)) warn("components is not an array, treated as empty");

  const node: WorldObjectNode = {
    id,
    name: typeof entry.object_name_in_scene === "string" ? entry.object_name_in_scene : `object_${id}`,
    transform: {
      position: [...IDENTITY_POSITION] as Vec3,
      quaternion: [...IDENTITY_QUATERNION] as QuatXyzw,
      scale: [...IDENTITY_SCALE] as Vec3,
      parent: null,
    },
    rawComponents,
  };
  const seen = new Set<string>();
  for (const component of rawComponents) {
    const type = component && typeof component.component_type === "string" ? component.component_type : "";
    const properties = asProps<Record<string, unknown>>(component?.properties) ?? {};
    if (!KNOWN_COMPONENT_TYPES.has(type)) {
      // §3.4: an unknown component must never abort the load.
      warn(`unknown component_type "${type || "(missing)"}", ignored`);
      continue;
    }
    if (seen.has(type)) {
      // O5: repeated components of one type fall back to the first occurrence.
      warn(`duplicate ${type} component, keeping the first one`);
      continue;
    }
    seen.add(type);
    switch (type) {
      case "Transform":
        node.transform = {
          position: readVec3(properties.position, IDENTITY_POSITION, "position", warn),
          quaternion: readQuaternion(properties.quaternion_xyzw, warn),
          scale: readScale(properties.scale, warn),
          parent: readParent(properties.parent, warn),
        };
        break;
      case "MetadataRef": {
        const path = typeof properties.path === "string" ? properties.path : "";
        if (!path) warn("MetadataRef without path, the object renders without a model");
        else {
          const type = properties.type;
          if (type === undefined) node.metadataRef = { path };
          else if (type === "model" || type === "robot") node.metadataRef = { path, type };
          else {
            warn(`MetadataRef type ${JSON.stringify(type)} is neither "model" nor "robot", the path form decides`);
            node.metadataRef = { path };
          }
        }
        break;
      }
      case "MeshRenderer":
        node.meshRenderer = asProps<MeshRendererProps>(properties);
        break;
      case "Semantic":
        node.semantic = asProps<SemanticProps>(properties);
        break;
      case "Light":
        node.light = asProps<LightProps>(properties);
        break;
      case "Camera":
        node.camera = asProps<CameraProps>(properties);
        break;
      case "RobotDriver":
        node.robotDriver = asProps<RobotDriverProps>(properties);
        break;
      case "RobotAttachment":
        node.attachment = asProps<RobotAttachmentProps>(properties);
        break;
      case "LocalAxes":
        node.localAxes = asProps<LocalAxesProps>(properties);
        break;
      default:
        break;
    }
  }
  if (!seen.has("Transform")) warn("no Transform component, using identity pose");
  // §4.9: parse time and draw time ask `localAxesRig` the same question, so the file that
  // says `length: "long"` warns once here and still gets the 0.1 m axes it fell back to.
  for (const warning of localAxesRig(node.localAxes).warnings) warn(warning);
  applyAttachment(node, warn);
  return { node, warnings };
}

/**
 * `RobotAttachment.transform` carries the mount pose and replaces `Transform.position`
 * /`quaternion_xyzw`; `scale` and `parent` stay with the Transform (§ 场景文件描述 §4.8).
 * A `parent_object_id` that disagrees with `Transform.parent` means the two components
 * describe different assemblies, so the object falls back to a plain Transform.
 *
 * The pose is written in the frame `transform.reference_frame` names, so the accepted
 * mount is recorded as an `AttachmentMount` and the view hangs the object from that
 * link's node; the pose then reads as an offset in the link's own coordinate system
 * and follows it through the arm's kinematics.
 */
function applyAttachment(node: WorldObjectNode, warn: (message: string) => void): void {
  const attachment = node.attachment;
  if (!attachment) return;
  const declared = attachment.parent_object_id;
  if (!isUintId(declared)) {
    warn(`RobotAttachment parent_object_id ${JSON.stringify(declared)} is not a uint32, using the plain Transform`);
    return;
  }
  if (declared !== node.transform.parent) {
    warn(`RobotAttachment parent_object_id ${declared} disagrees with Transform.parent ${JSON.stringify(node.transform.parent)}, using the plain Transform`);
    return;
  }
  const transform = asProps<Record<string, unknown>>(attachment.transform) ?? {};
  if (transform.position === undefined && transform.quaternion_xyzw === undefined) {
    warn("RobotAttachment without a transform position or quaternion, using the plain Transform");
    return;
  }
  node.transform = {
    ...node.transform,
    position: readVec3(transform.position, node.transform.position, "attachment position", warn),
    quaternion: transform.quaternion_xyzw === undefined
      ? node.transform.quaternion
      : readQuaternion(transform.quaternion_xyzw, warn),
  };
  node.attachmentMount = { objectId: declared, link: mountLink(attachment, transform.reference_frame, warn) };
}

/**
 * The link an accepted mount points at. "parent_link" is the frame the exporter
 * writes and the only one that needs a link; anything else is read against the
 * parent object node, which is what an object's own `Transform` is written in.
 */
function mountLink(attachment: RobotAttachmentProps, frame: unknown, warn: (message: string) => void): string | null {
  const name = typeof frame === "string" && frame ? frame : "parent_link";
  if (name !== "parent_link") {
    if (name !== "parent_object") warn(`RobotAttachment reference_frame "${name}" is not a known frame, mounting on the parent object`);
    return null;
  }
  if (typeof attachment.parent_link === "string" && attachment.parent_link) return attachment.parent_link;
  warn("RobotAttachment framed on parent_link names no parent_link, mounting on the parent object");
  return null;
}

/**
 * Two-pass hierarchy build (§3.6): index every id first, then wire parents so a
 * forward reference works regardless of array order. Objects that end up with an
 * unknown parent or sit on a cycle are rejected together with their descendants.
 */
function buildHierarchy(nodes: WorldObjectNode[], positions: Map<WorldObjectNode, number>): Hierarchy {
  const byId = new Map<number, WorldObjectNode>();
  const childrenOf = new Map<number, number[]>();
  const rejections: RejectedObject[] = [];
  const droppedReasons = new Map<number, string>();
  // Dropping a node removes it from byId, so "unknown parent" also covers "parent was dropped".
  const drop = (node: WorldObjectNode, reason: string) => {
    if (byId.get(node.id) === node) byId.delete(node.id);
    droppedReasons.set(node.id, reason);
    rejections.push({ object_id_in_scene: node.id, index: positions.get(node) ?? -1, reason });
  };

  for (const node of nodes) {
    if (byId.has(node.id)) drop(node, "DUPLICATE_OBJECT_ID");
    else byId.set(node.id, node);
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of nodes) {
      if (byId.get(node.id) !== node) continue;
      const parent = node.transform.parent;
      if (parent === null) continue;
      if (!byId.has(parent)) {
        drop(node, droppedReasons.has(parent) ? `PARENT_REJECTED:${parent}` : `PARENT_NOT_FOUND:${parent}`);
        changed = true;
      }
    }
    const before = byId.size;
    dropCycles(byId, drop);
    if (byId.size !== before) changed = true;
  }
  for (const node of byId.values()) {
    if (node.transform.parent !== null) childrenOf.set(node.transform.parent, [...(childrenOf.get(node.transform.parent) ?? []), node.id]);
  }
  return { byId, childrenOf, rejections };
}

function dropCycles(byId: Map<number, WorldObjectNode>, drop: (node: WorldObjectNode, reason: string) => void): void {
  const state = new Map<number, "walking" | "clear">();
  const onCycle = new Set<number>();
  const walk = (id: number, trail: number[]): void => {
    if (state.get(id) === "clear") return;
    if (state.get(id) === "walking") {
      trail.slice(trail.indexOf(id)).forEach((member) => onCycle.add(member));
      return;
    }
    state.set(id, "walking");
    const parent = byId.get(id)?.transform.parent;
    if (parent !== undefined && parent !== null) walk(parent, [...trail, id]);
    state.set(id, "clear");
  };
  for (const id of byId.keys()) walk(id, []);
  for (const id of onCycle) {
    const node = byId.get(id);
    if (node) drop(node, "INVALID_PARENT_CYCLE");
  }
}

export interface WorldMeta {
  scene_schema_version: number;
  scene_version: number;
  /**
   * §1.2 (Q109/Q114): the fourth of the four-tuple. Only a `setup`-phase browser edit
   * of the scene's *content* (class ②) advances it; `scene_version` advancing resets it,
   * and a file that does not carry the key reads as 0 - which is also what `toJSON()`
   * writes back, so a never-edited scene round-trips byte-for-byte.
   */
  scene_sub_version: number;
  scene_id: number;
  scene_name: string;
}

function readWorldMeta(value: unknown): WorldMeta {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new WorldError("INVALID_WORLD_SCHEMA", "world document must be a JSON object");
  const document = value as Record<string, unknown>;
  if (!isUintId(document.scene_schema_version) || document.scene_schema_version > SUPPORTED_SCHEMA_VERSION) {
    throw new WorldError("INVALID_WORLD_SCHEMA", `scene_schema_version ${JSON.stringify(document.scene_schema_version)} is not supported`);
  }
  if (!Array.isArray(document.objects)) throw new WorldError("INVALID_WORLD_SCHEMA", "objects must be an array");
  return {
    scene_schema_version: document.scene_schema_version,
    scene_version: isUintId(document.scene_version) ? document.scene_version : 0,
    scene_sub_version: isUintId(document.scene_sub_version) ? document.scene_sub_version : 0,
    scene_id: isUintId(document.scene_id) ? document.scene_id : 0,
    scene_name: typeof document.scene_name === "string" ? document.scene_name : "",
  };
}

function toWorldObject(node: WorldObjectNode, parentOverride?: number | null): WorldObject {
  const components: WorldComponent[] = node.rawComponents.map((component) => {
    if (parentOverride === undefined || !component || component.component_type !== "Transform") return component;
    const properties = asProps<Record<string, unknown>>(component.properties) ?? {};
    return { ...component, properties: { ...properties, parent: parentOverride } };
  });
  if (parentOverride !== undefined && !components.some((component) => component?.component_type === "Transform")) {
    components.push({ component_type: "Transform", properties: {
      position: node.transform.position, quaternion_xyzw: node.transform.quaternion, scale: node.transform.scale, parent: parentOverride,
    } satisfies TransformProps & Record<string, unknown> });
  }
  return { object_id_in_scene: node.id, object_name_in_scene: node.name, components };
}

export class World {
  meta: WorldMeta;
  objects: WorldObjectNode[] = [];
  /** monotonic watermark so released ids are never reused (§5.5) */
  private watermark = -1;
  private byId = new Map<number, WorldObjectNode>();
  private children = new Map<number, number[]>();
  private explicitSubVersion: boolean;

  constructor(meta: WorldMeta, explicitSubVersion = false) {
    this.meta = meta;
    this.explicitSubVersion = explicitSubVersion;
  }

  get version(): number {
    return this.meta.scene_version;
  }

  /** §1.2: how many class-② edits this browser has made since the last `scene_version`. */
  get subVersion(): number {
    return this.meta.scene_sub_version;
  }

  get sceneId(): number {
    return this.meta.scene_id;
  }

  get objectCount(): number {
    return this.objects.length;
  }

  get(id: number): WorldObjectNode | undefined {
    return this.byId.get(id);
  }

  childrenOf(id: number): number[] {
    return this.children.get(id) ?? [];
  }

  roots(): WorldObjectNode[] {
    return this.objects.filter((node) => node.transform.parent === null);
  }

  camera(id: number): CameraProps | undefined {
    return this.byId.get(id)?.camera;
  }

  /**
   * §9 item 3: every object that carries a `Camera` component, in document order.
   *
   * The component is the whole test -- no name matching, no "the wrist one", and no picking of the
   * first hit, because UR5e's empty template really does have two (ID4 腕部 and ID5 顶视) and a
   * guess between them is a wrong camera, not a wrong id.
   */
  cameraObjectIds(): number[] {
    return this.objects.filter((node) => node.camera !== undefined).map((node) => node.id);
  }

  robotDriver(id: number): RobotDriverProps | undefined {
    return this.byId.get(id)?.robotDriver;
  }

  /**
   * §4.7 (Q107): the driver of an object the editor may draw arm decorations for. Everything
   * else - driving, mounting, joint editing, `/updateItem` refresh, Q75 merge, Q76 re-park -
   * keeps reading `robotDriver()`, because a passive URDF model is a robot by §8.3 as well.
   */
  armDriver(id: number): RobotDriverProps | undefined {
    const driver = this.byId.get(id)?.robotDriver;
    return driver?.role === "passive" ? undefined : driver;
  }

  /** Replace every object from raw `objects[]` entries and report per-object rejections (§3.6). */
  replaceObjects(rawObjects: readonly unknown[]): RejectedObject[] {
    const nodes: WorldObjectNode[] = [];
    const rejections: RejectedObject[] = [];
    // One index space for both passes: `index` always means "the entry of the submitted
    // objects[]", so a malformed entry earlier in the array cannot shift the report of a
    // hierarchy rejection onto a different object.
    const positions = new Map<WorldObjectNode, number>();
    rawObjects.forEach((raw, index) => {
      const parsed = normalizeObject(raw, index);
      if ("rejection" in parsed) rejections.push(parsed.rejection);
      else {
        nodes.push(parsed.node);
        positions.set(parsed.node, index);
      }
    });
    const hierarchy = buildHierarchy(nodes, positions);
    const kept = new Set(hierarchy.byId.values());
    this.objects = nodes.filter((node) => kept.has(node));
    this.byId = hierarchy.byId;
    this.children = hierarchy.childrenOf;
    rejections.push(...hierarchy.rejections);
    this.watermark = Math.max(this.watermark, ...this.objects.map((node) => node.id), -1);
    return rejections;
  }

  /**
   * Full-scene apply for /updateWorld and Load Scene (§4.1, §3.9).
   *
   * `strict` is the `commitWorld` candidate: a rejected entry means the whole candidate lands
   * nowhere (流程文档 §8.1), so the probe is thrown away and the current version, id and object
   * set are reported back untouched. Without it the good subset applies and the rejections ride
   * along in `rejected_objects` (§3.6).
   */
  applyWorld(value: unknown, expectedVersion?: number, strict = false): WorldApplyResult {
    this.requireVersion(expectedVersion);
    const meta = readWorldMeta(value);
    const probe = new World(meta, Object.prototype.hasOwnProperty.call(value, "scene_sub_version"));
    const rejected = probe.replaceObjects((value as { objects: unknown[] }).objects);
    if (strict && rejected.length > 0) return this.applyResult(rejected);
    // An Agent bootstrap may replace the protocol's empty scene (id 0) with a newly
    // generated scene identity and an already assigned baseline version.  That identity change
    // is not a same-scene edit, so do not increment the candidate once more; doing so makes the
    // browser answer version 2 for a candidate version 1 and the Gateway must quarantine an
    // otherwise successful commit.  A same-scene candidate already carries the version it lands
    // as (§8.1 asks for exactly current + 1), so the answer is that version, or one past our own
    // when a viewer is behind and has to be pulled forward.
    const identityChanged = this.sceneId === 0 && meta.scene_id !== 0;
    this.meta = { ...meta, scene_version: identityChanged ? meta.scene_version
      : strict ? meta.scene_version : this.version + 1 };
    this.explicitSubVersion = probe.explicitSubVersion;
    this.replaceState(probe);
    return this.applyResult(rejected);
  }

  /** /updateItem: replace everything except the id (§4.2). */
  updateItem(id: number, object: ObjectRequest, expectedVersion?: number): WorldApplyResult {
    this.requireVersion(expectedVersion);
    this.writeItem(id, object);
    this.bumpVersion();
    return this.applyResult([]);
  }

  /** /addObject: id optional, allocated from the watermark when absent (§5.5). */
  addObject(object: ObjectRequest, expectedVersion?: number): AddObjectResult {
    this.requireVersion(expectedVersion);
    const id = this.insertObject(object);
    this.bumpVersion();
    return { ok: true, object_id_in_scene: id, scene_version: this.version };
  }

  /** /removeObject: cascade by default, detach_children re-parents (§5.14). */
  removeObject(id: number, detachChildren = false, expectedVersion?: number): RemoveObjectResult {
    this.requireVersion(expectedVersion);
    const result = this.deleteObject(id, detachChildren);
    this.bumpVersion();
    return { ...result, scene_version: this.version };
  }

  /**
   * §4.8 (Q114 ②): the browser's own content edit. It never touches `scene_version` -
   * that counter belongs to Gateway and the Agent writes - but it is no longer invisible:
   * every successful add or remove advances `scene_sub_version`, and the caller reports
   * the world right after, because a 现场 the Gateway cannot see is a 现场 the Agent
   * will overwrite.
   */
  addLocalObject(object: ObjectRequest): number {
    const id = this.insertObject(object);
    this.bumpSubVersion();
    return id;
  }

  /** §4.8.1: dropping a light never orphans its children, so the local path always detaches. */
  removeLocalObject(id: number): void {
    this.deleteObject(id, true);
    this.bumpSubVersion();
  }

  /**
   * §4.6: the local twin of `/updateItem`. Replacing an object's components is what a
   * panel swap of an arm, a gripper or a camera is, and it must not move `scene_version`
   * - that counter belongs to Gateway and the Agent writes (Q114 ②).
   */
  updateLocalItem(id: number, object: ObjectRequest): void {
    this.writeItem(id, object);
    this.bumpSubVersion();
  }

  /**
   * §4.6: drop one object and everything hanging from it, local version. `removeLocalObject`
   * detaches children because a dropped light must not delete the room; this one cascades
   * because what the panel drops is furniture an arm no longer stands on.
   */
  removeLocalSubtree(id: number): void {
    this.deleteObject(id, false);
    this.bumpSubVersion();
  }

  /**
   * §4.6.1 (2026-11-05 穿模修复): place one object the browser has to measure against the live
   * stage before it knows where the thing fits. `insertObject` stands it up, `settle` lets the
   * renderer build it so a footprint can be read, `finalize` returns the object rewritten to its
   * real spot - and it throws to refuse the click. The staging insert and the rewrite are
   * plumbing inside a single user edit, so only ONE `scene_sub_version` bump is recorded, and a
   * refusal (thrown or otherwise) deletes the staging object and leaves the counter exactly where
   * it was. That is the difference from `addLocalObject` + `updateLocalItem`, which would advance
   * it twice for one placement and once for a click that lands nothing.
   */
  async placeLocal(
    initial: ObjectRequest,
    settle: (objectId: number) => Promise<void>,
    finalize: (objectId: number) => Promise<ObjectRequest>,
  ): Promise<number> {
    const id = this.insertObject(initial);
    try {
      await settle(id);
      const placed = await finalize(id);
      this.writeItem(id, placed);
      this.bumpSubVersion();
    } catch (error) {
      this.deleteObject(id, false);
      throw error;
    }
    return id;
  }

  /**
   * §4.8 (Q114 ②): switching to a preset scene is a class-② edit, so the four-tuple that
   * goes out on `browser.state_sync` must strictly advance. A fresh scene document carries
   * its own (usually absent) sub-version; rebase it one step above the world the browser is
   * leaving so a switch between presets that share a `scene_id` still lands, instead of
   * collapsing to a non-increasing four-tuple the Gateway silently refuses - which is what
   * made a refresh show the pre-switch scene. Marking the sub-version explicit forces
   * `toJSON()` to write the key even when the file omitted it.
   */
  rebaseSubVersionAfterSwitch(previousSubVersion: number): void {
    this.meta = { ...this.meta, scene_sub_version: previousSubVersion + 1 };
    this.explicitSubVersion = true;
  }

  subtreeOf(id: number): number[] {
    const collected: number[] = [];
    const pending = [id];
    while (pending.length) {
      const current = pending.shift() as number;
      collected.push(current);
      pending.push(...this.childrenOf(current));
    }
    return collected;
  }

  /**
   * Preserve absent versus explicitly supplied zero: both mean sub-version zero, but
   * they are different full-document JCS digests. Agent candidates must round-trip intact.
   */
  toJSON(): WorldFile {
    const { scene_sub_version, ...meta } = this.meta;
    return {
      ...meta,
      ...(this.explicitSubVersion || scene_sub_version > 0 ? { scene_sub_version } : {}),
      objects: this.objects.map((node) => toWorldObject(node)),
    };
  }

  /** Shared body of `/addObject` and the local path: validate, allocate the id, rebuild (§5.5). */
  private insertObject(object: ObjectRequest): number {
    const components = Array.isArray(object.components) ? object.components : [];
    if (!components.some((component) => component?.component_type === "Transform")) {
      throw new WorldError("MISSING_TRANSFORM_COMPONENT", "addObject requires a Transform component");
    }
    const declared = object.object_id_in_scene;
    let id: number;
    if (declared === undefined) {
      id = Math.max(this.watermark, ...this.objects.map((node) => node.id), -1) + 1;
    } else {
      if (!isUintId(declared)) throw new WorldError("MISSING_PARAM", "object_id_in_scene must be a uint32");
      if (this.byId.get(declared)) throw new WorldError("ID_ALREADY_EXISTS", `object ${declared} already exists`, 409);
      id = declared;
    }
    this.rebuild([...this.objects.map((node) => toWorldObject(node)), { ...object, object_id_in_scene: id }], id);
    this.watermark = Math.max(this.watermark, id);
    return id;
  }

  /** Shared body of `/updateItem` and the local path: validate the replacement and rebuild (§4.2). */
  private writeItem(id: number, object: ObjectRequest): void {
    if (!isUintId(id)) throw new WorldError("MISSING_PARAM", "id must be a uint32");
    if (!this.byId.get(id)) throw new WorldError("OBJECT_NOT_FOUND", `object ${id} is not in the scene`, 404);
    if (object.object_id_in_scene !== undefined && object.object_id_in_scene !== id) {
      throw new WorldError("BODY_ID_MISMATCH", `body id ${object.object_id_in_scene} does not match ${id}`);
    }
    const components = Array.isArray(object.components) ? object.components : [];
    if (!components.some((component) => component?.component_type === "Transform")) {
      throw new WorldError("MISSING_TRANSFORM_COMPONENT", "updateItem requires a Transform component");
    }
    const replacement: unknown = { ...object, object_id_in_scene: id };
    const entries = this.objects.map((node) => (node.id === id ? replacement : toWorldObject(node)));
    this.rebuild(entries, id);
  }

  /** Shared body of `/removeObject` and the local path: cascade or detach, then rebuild (§5.14). */
  private deleteObject(id: number, detachChildren: boolean): RemoveObjectResult {
    if (!isUintId(id)) throw new WorldError("INVALID_PARAM", "id must be a uint32");
    const target = this.byId.get(id);
    if (!target) throw new WorldError("OBJECT_NOT_FOUND", `object ${id} is not in the scene`, 404);
    const removed = detachChildren ? [id] : this.subtreeOf(id);
    const removedSet = new Set(removed);
    const reparented: number[] = [];
    const entries = this.objects.filter((node) => !removedSet.has(node.id)).map((node) => {
      if (detachChildren && node.transform.parent === id) {
        reparented.push(node.id);
        return toWorldObject(node, target.transform.parent);
      }
      return toWorldObject(node);
    });
    const probe = new World(this.meta);
    const rejected = probe.replaceObjects(entries);
    if (rejected.length) {
      throw new WorldError("INVALID_PARENT_CYCLE", `removing ${id} would leave ${rejected.length} object(s) without a valid parent`);
    }
    this.replaceState(probe);
    return { ok: true, removed_object_ids: removed, reparented_object_ids: reparented, scene_version: this.version };
  }

  /** Swap in a validated entry list; if any object we already had disappears the change is refused (§5.13). */
  private rebuild(entries: readonly unknown[], mustSurvive: number): void {
    const probe = new World(this.meta);
    const rejected = probe.replaceObjects(entries);
    const missing = rejected.find((rejection) => rejection.object_id_in_scene === mustSurvive);
    if (missing) throw new WorldError("INVALID_PARENT_CYCLE", `${missing.reason} for object ${mustSurvive}`);
    const lost = this.objects.filter((node) => !probe.byId.get(node.id));
    if (lost.length) {
      throw new WorldError("INVALID_PARENT_CYCLE", `hierarchy would drop object ${lost[0]?.id}`);
    }
    this.replaceState(probe);
  }

  private replaceState(probe: World): void {
    this.objects = probe.objects;
    this.byId = probe.byId;
    this.children = probe.children;
    this.watermark = Math.max(this.watermark, probe.watermark, ...probe.objects.map((node) => node.id), -1);
  }

  private requireVersion(expectedVersion?: number): void {
    if (expectedVersion !== undefined && expectedVersion !== this.version) {
      throw new WorldError("VERSION_CONFLICT", `expected_version ${expectedVersion} != current ${this.version}`, 409);
    }
  }

  private bumpVersion(): void {
    // §1.2: an Agent write rebases the sub-version - a commit candidate has to carry 0.
    this.meta = { ...this.meta, scene_version: this.meta.scene_version + 1, scene_sub_version: 0 };
  }

  /** §1.2 (Q114 ②): one successful browser content edit, counted against the current `scene_version`. */
  private bumpSubVersion(): void {
    this.meta = { ...this.meta, scene_sub_version: this.meta.scene_sub_version + 1 };
  }

  private applyResult(rejected: RejectedObject[]): WorldApplyResult {
    return { ok: true, scene_id: this.meta.scene_id, scene_version: this.version, object_count: this.objectCount, rejected_objects: rejected };
  }
}

/** Validate and build the scene model from a submitted or loaded world document. */
export function parseWorld(json: unknown): World {
  const meta = readWorldMeta(json);
  const world = new World(meta, Object.prototype.hasOwnProperty.call(json, "scene_sub_version"));
  const rejected = world.replaceObjects((json as { objects: unknown[] }).objects);
  for (const rejection of rejected) {
    console.warn(`[world] object #${rejection.index} skipped: ${rejection.reason}`);
  }
  return world;
}
