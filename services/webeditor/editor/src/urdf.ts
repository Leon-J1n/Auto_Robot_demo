/**
 * URDF expansion in the browser (spec v2.1 §6.4, §8.4).
 *
 * `parseUrdf` turns the XML into plain data, `buildUrdfTree` turns that data
 * into an articulated Three.js node tree at the zero joint pose. Every mesh
 * filename is model-relative: `package://<pkg>/` is rewritten away and a path
 * that would leave the model directory is refused (§6.4), so the loader can
 * never be steered outside the library entry it was given.
 */
import {
  BoxGeometry,
  CylinderGeometry,
  Euler,
  Mesh,
  MeshStandardMaterial,
  Object3D,
  Quaternion,
  SphereGeometry,
  Vector3,
} from "three";
import type { QuatXyzw, Vec3 } from "../../contracts/types";
import { LibraryError, normalizeModelPath } from "./library";

export type UrdfJointType = "revolute" | "continuous" | "prismatic" | "fixed" | "floating" | "planar";
const JOINT_TYPES: UrdfJointType[] = ["revolute", "continuous", "prismatic", "fixed", "floating", "planar"];

export interface UrdfOrigin {
  translation: Vec3;
  /** xyzw, the order every file in this project uses (§2.8) */
  quaternion: QuatXyzw;
}

export type UrdfGeometry =
  | { kind: "box"; size: Vec3 }
  | { kind: "cylinder"; radius: number; length: number }
  | { kind: "sphere"; radius: number }
  | { kind: "mesh"; filename: string; scale: Vec3 };

export type Rgba = [number, number, number, number];

export interface UrdfVisual {
  name: string;
  origin: UrdfOrigin;
  geometry: UrdfGeometry;
  rgba: Rgba;
}

export interface UrdfLink {
  name: string;
  visuals: UrdfVisual[];
}

/** `<mimic joint="…" multiplier="…" offset="…">`: the value of one joint derives from another. */
export interface UrdfMimic {
  /** name of the joint whose value is copied */
  joint: string;
  multiplier: number;
  offset: number;
}

export interface UrdfJoint {
  name: string;
  type: UrdfJointType;
  parent: string;
  child: string;
  origin: UrdfOrigin;
  axis: Vec3;
  /** `limit lower/upper` in radians or metres; movable joints without a limit are unbounded */
  limit: [number, number] | null;
  /** null unless the joint is driven by another one; such a joint is never commanded directly */
  mimic: UrdfMimic | null;
}

export interface UrdfRobot {
  name: string;
  links: UrdfLink[];
  joints: UrdfJoint[];
  /** links that are not any joint's child */
  roots: string[];
  /** every mesh file the robot needs, model-relative and de-duplicated */
  meshFiles: string[];
}

function fail(message: string): never {
  throw new LibraryError("INVALID_URDF", message, 400);
}

function finiteNumbers(value: string, expected: number, context: string): number[] {
  const parts = value.trim().length ? value.trim().split(/\s+/) : [];
  if (parts.length !== expected) fail(`${context} needs ${expected} numbers, got "${value}"`);
  const numbers = parts.map(Number);
  if (numbers.some((number) => !Number.isFinite(number))) fail(`${context} has non-numeric values: "${value}"`);
  return numbers;
}

function readVector(element: Element | null, attribute: string, fallback: number[], context: string): number[] {
  if (!element) return fallback;
  const raw = element.getAttribute(attribute);
  if (raw === null || !raw.trim()) return fallback;
  return finiteNumbers(raw, fallback.length, `${context} ${attribute}`);
}

export function rpyToQuaternion(rpy: Vec3): QuatXyzw {
  // URDF rpy is fixed-axis: R = Rz(yaw) * Ry(pitch) * Rx(roll), which is Three.js
  // Euler order "ZYX". "XYZ" is a different rotation and must not be used.
  const quaternion = new Quaternion().setFromEuler(new Euler(rpy[0], rpy[1], rpy[2], "ZYX"));
  return [quaternion.x, quaternion.y, quaternion.z, quaternion.w];
}

function readOrigin(parent: Element, context: string): UrdfOrigin {
  const element = child(parent, "origin");
  return {
    translation: vec3(readVector(element, "xyz", [0, 0, 0], `${context} origin`)),
    quaternion: rpyToQuaternion(vec3(readVector(element, "rpy", [0, 0, 0], `${context} origin`))),
  };
}

function vec3(values: number[]): Vec3 {
  return [values[0]!, values[1]!, values[2]!];
}

function readLimit(element: Element, joint: string): [number, number] {
  const lower = Number(element.getAttribute("lower") ?? "0");
  const upper = Number(element.getAttribute("upper") ?? "0");
  if (!Number.isFinite(lower) || !Number.isFinite(upper)) fail(`joint ${joint} limit is not numeric`);
  if (lower > upper) fail(`joint ${joint} limit lower ${lower} is above upper ${upper}`);
  return [lower, upper];
}

function numberAttribute(element: Element, attribute: string, fallback: number, context: string): number {
  const raw = element.getAttribute(attribute);
  if (raw === null || !raw.trim()) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) fail(`${context} is not numeric: "${raw}"`);
  return value;
}

/** Only these three hold a value, so only these can be driven or follow another joint. */
function isMovable(type: UrdfJointType): boolean {
  return type === "revolute" || type === "continuous" || type === "prismatic";
}

/** `multiplier` defaults to 1 and `offset` to 0, the URDF spec's own defaults (§6.4). */
function readMimic(element: Element | null, joint: string): UrdfMimic | null {
  if (!element) return null;
  const source = element.getAttribute("joint");
  if (!source) fail(`joint ${joint} has a <mimic> without a joint attribute`);
  return {
    joint: source,
    multiplier: numberAttribute(element, "multiplier", 1, `joint ${joint} mimic multiplier`),
    offset: numberAttribute(element, "offset", 0, `joint ${joint} mimic offset`),
  };
}

function childrenOf(parent: Element, tag: string): Element[] {
  return Array.from(parent.children).filter((node) => node.tagName === tag);
}

function child(parent: Element, tag: string): Element | null {
  return childrenOf(parent, tag)[0] ?? null;
}

function requiredName(element: Element, kind: string): string {
  const name = element.getAttribute("name");
  if (!name) fail(`${kind} element without a name attribute`);
  return name;
}

function readRgba(element: Element | null, materials: Map<string, Rgba>): Rgba {
  const fallback: Rgba = [0.8, 0.8, 0.8, 1];
  if (!element) return fallback;
  const inline = child(element, "color");
  if (inline) {
    const values = readVector(inline, "rgba", fallback, "material color");
    return [values[0]!, values[1]!, values[2]!, values[3]!];
  }
  return materials.get(element.getAttribute("name") ?? "") ?? fallback;
}

function readGeometry(geometry: Element | null, context: string): UrdfGeometry {
  if (!geometry || geometry.children.length !== 1) fail(`${context} needs exactly one geometry shape`);
  const shape = geometry.children[0]!;
  if (shape.tagName === "box") {
    const size = vec3(finiteNumbers(shape.getAttribute("size") ?? "", 3, `${context} box size`));
    requirePositive(size, `${context} box size`);
    return { kind: "box", size };
  }
  if (shape.tagName === "cylinder") {
    const radius = singlePositive(shape.getAttribute("radius"), `${context} cylinder radius`);
    const length = singlePositive(shape.getAttribute("length"), `${context} cylinder length`);
    return { kind: "cylinder", radius, length };
  }
  if (shape.tagName === "sphere") {
    return { kind: "sphere", radius: singlePositive(shape.getAttribute("radius"), `${context} sphere radius`) };
  }
  if (shape.tagName === "mesh") {
    const filename = (shape.getAttribute("filename") ?? "").trim();
    if (!filename) fail(`${context} mesh has no filename`);
    let relative: string;
    try {
      relative = normalizeModelPath(filename);
    } catch (error) {
      // §6.4: an escaping or absolute filename is a dependency failure, not a parse accident.
      throw new LibraryError("DEPENDENCY_FETCH_FAILED", `${context} mesh ${(error as Error).message}`, 424, [filename]);
    }
    const scale = shape.getAttribute("scale");
    if (scale !== null && scale.trim().length) {
      const values = finiteNumbers(scale, 3, `${context} mesh scale`);
      if (values.some((value) => value === 0)) fail(`${context} mesh scale must not be zero`);
      return { kind: "mesh", filename: relative, scale: [values[0]!, values[1]!, values[2]!] };
    }
    return { kind: "mesh", filename: relative, scale: [1, 1, 1] };
  }
  return fail(`${context} has unsupported geometry <${shape.tagName}>`);
}

function requirePositive(values: number[], context: string): void {
  if (values.some((value) => value <= 0)) fail(`${context} values must be greater than zero`);
}

function singlePositive(raw: string | null, context: string): number {
  if (raw === null || !raw.trim()) fail(`${context} is missing`);
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) fail(`${context} must be a positive number`);
  return value;
}

export function parseUrdf(source: string): UrdfRobot {
  const document = new DOMParser().parseFromString(source, "application/xml");
  const invalid = document.getElementsByTagName("parsererror");
  if (invalid.length) fail(`XML is not well formed: ${invalid[0]!.textContent?.trim() ?? "parse error"}`);
  if (document.documentElement?.tagName !== "robot") fail(`URDF root must be <robot>, got <${document.documentElement?.tagName ?? "empty"}>`);
  const root = document.documentElement;

  const materials = new Map<string, Rgba>();
  for (const material of childrenOf(root, "material")) {
    const color = child(material, "color");
    if (color) {
      const values = readVector(color, "rgba", [0.8, 0.8, 0.8, 1], `material ${material.getAttribute("name")} color`);
      materials.set(requiredName(material, "material"), [values[0]!, values[1]!, values[2]!, values[3]!]);
    }
  }

  const links: UrdfLink[] = [];
  const byName = new Map<string, UrdfLink>();
  for (const element of childrenOf(root, "link")) {
    const name = requiredName(element, "link");
    if (byName.has(name)) fail(`duplicate link name "${name}"`);
    const visuals: UrdfVisual[] = childrenOf(element, "visual").map((visual, index) => ({
      name: visual.getAttribute("name") ?? `${name}_visual_${index + 1}`,
      origin: readOrigin(visual, `link ${name} visual ${index + 1}`),
      geometry: readGeometry(child(visual, "geometry"), `link ${name} visual ${index + 1}`),
      rgba: readRgba(child(visual, "material"), materials),
    }));
    const link = { name, visuals };
    byName.set(name, link);
    links.push(link);
  }
  if (!links.length) fail("URDF must declare at least one link");

  const joints: UrdfJoint[] = [];
  const childLinks = new Set<string>();
  const jointNames = new Set<string>();
  for (const element of childrenOf(root, "joint")) {
    const name = requiredName(element, "joint");
    if (jointNames.has(name)) fail(`duplicate joint name "${name}"`);
    jointNames.add(name);
    const type = element.getAttribute("type") ?? "";
    if (!JOINT_TYPES.includes(type as UrdfJointType)) fail(`joint ${name} has unknown type "${type}"`);
    const parent = child(element, "parent")?.getAttribute("link");
    const childName = child(element, "child")?.getAttribute("link");
    if (!parent || !byName.has(parent)) fail(`joint ${name} parent link "${parent ?? ""}" is not defined`);
    if (!childName || !byName.has(childName)) fail(`joint ${name} child link "${childName ?? ""}" is not defined`);
    if (childLinks.has(childName)) fail(`link ${childName} has more than one parent joint`);
    childLinks.add(childName);
    const limit = child(element, "limit");
    const joint: UrdfJoint = {
      name,
      type: type as UrdfJointType,
      parent,
      child: childName,
      origin: readOrigin(element, `joint ${name}`),
      axis: vec3(readVector(child(element, "axis"), "xyz", [0, 0, 1], `joint ${name} axis`)),
      limit: limit === null ? null : readLimit(limit, name),
      mimic: readMimic(child(element, "mimic"), name),
    };
    joints.push(joint);
  }
  checkMimics(joints);

  const roots = links.filter((link) => !childLinks.has(link.name)).map((link) => link.name);
  detectCycle(joints);
  const meshFiles: string[] = [];
  for (const visual of links.flatMap((link) => link.visuals)) {
    if (visual.geometry.kind === "mesh" && !meshFiles.includes(visual.geometry.filename)) meshFiles.push(visual.geometry.filename);
  }
  return { name: root.getAttribute("name") ?? "robot", links, joints, roots, meshFiles };
}

/**
 * A `<mimic>` is a one-way derivation, so the source has to be a joint that can
 * hold a value and the chain has to end somewhere (§6.4). Both failures are
 * structural, like a missing parent link: refuse the document instead of
 * silently leaving the follower at its origin.
 */
function checkMimics(joints: UrdfJoint[]): void {
  const byName = new Map(joints.map((joint) => [joint.name, joint]));
  for (const joint of joints) {
    const mimic = joint.mimic;
    if (!mimic) continue;
    const source = byName.get(mimic.joint);
    if (!source) fail(`joint ${joint.name} mimics "${mimic.joint}", which is not defined`);
    if (!isMovable(source.type)) fail(`joint ${joint.name} mimics "${mimic.joint}", which cannot hold a value`);
  }
  const sourceOf = new Map(joints.filter((joint) => joint.mimic).map((joint) => [joint.name, joint.mimic!.joint]));
  for (const start of sourceOf.keys()) {
    const trail = new Set<string>([start]);
    let current = sourceOf.get(start) ?? null;
    while (current !== null) {
      if (trail.has(current)) fail(`joint ${start} has a cyclic <mimic> chain through "${current}"`);
      trail.add(current);
      current = sourceOf.get(current) ?? null;
    }
  }
}

function detectCycle(joints: UrdfJoint[]): void {
  const parentOf = new Map(joints.map((joint) => [joint.child, joint.parent]));
  for (const start of parentOf.keys()) {
    const trail = new Set<string>([start]);
    let current = parentOf.get(start) ?? null;
    while (current !== null) {
      if (trail.has(current)) fail(`URDF link "${current}" is its own ancestor`);
      trail.add(current);
      current = parentOf.get(current) ?? null;
    }
  }
}

export interface UrdfJointHandle {
  name: string;
  type: UrdfJointType;
  /** joint-frame axis, as written in the URDF (it may be unnormalised) */
  axis: Vec3;
  limit: [number, number] | null;
  /** the joint's own origin; a joint value is always applied on top of it */
  origin: UrdfOrigin;
  /** name of the link this joint moves */
  childLink: string;
  /** the joint whose value this one derives from, or null when it is driven directly */
  mimic: UrdfMimic | null;
  /** node carrying the joint origin; `setJointValue` turns or slides this node */
  node: Object3D;
}

export interface UrdfTree {
  root: Object3D;
  links: Map<string, Object3D>;
  joints: Map<string, UrdfJointHandle>;
}

/**
 * Called for every `<mesh>` visual with the link's resolved colour. A file that
 * carries no material of its own (STL) should take the colour from the URDF; a
 * file with embedded materials (DAE/OBJ+MTL/glTF) keeps its own (§6.8).
 */
export type MeshFactory = (filename: string, rgba: Rgba) => Object3D | null;

/**
 * Expand the robot at the zero joint pose. Every joint gets a node carrying its
 * origin, but only revolute/continuous/prismatic ones are exposed as handles,
 * because those are the nodes a joint value may turn or slide (§8.4). `meshes`
 * is called for every `<mesh>` visual and may return null while the asset is
 * still loading or has failed (§6.8: never a placeholder cube).
 */
export function buildUrdfTree(robot: UrdfRobot, meshes: MeshFactory = () => null): UrdfTree {
  const root = new Object3D();
  root.name = robot.name;
  const links = new Map<string, Object3D>();
  for (const link of robot.links) {
    const node = new Object3D();
    node.name = link.name;
    links.set(link.name, node);
    for (const visual of link.visuals) node.add(buildVisual(visual, meshes));
  }
  const joints = new Map<string, UrdfJointHandle>();
  for (const joint of robot.joints) {
    const node = new Object3D();
    node.name = joint.name;
    node.position.set(...joint.origin.translation);
    node.quaternion.set(...joint.origin.quaternion);
    links.get(joint.parent)!.add(node);
    node.add(links.get(joint.child)!);
    if (isMovable(joint.type)) {
      joints.set(joint.name, {
        name: joint.name,
        type: joint.type,
        axis: joint.axis,
        limit: joint.limit,
        origin: joint.origin,
        childLink: joint.child,
        mimic: joint.mimic,
        node,
      });
    }
  }
  for (const name of robot.roots) root.add(links.get(name)!);
  return { root, links, joints };
}

/**
 * Apply one joint value to the tree (§8.4): radians for revolute/continuous,
 * metres for prismatic. The joint origin is never overwritten - the value is
 * composed on top of it, so calling this with 0 restores the expanded pose.
 * A joint that moves about an axis of zero length cannot be driven and is left
 * at its origin rather than rotated by a made-up direction.
 */
export function setJointValue(handle: UrdfJointHandle, value: number): void {
  const { node, origin, type } = handle;
  const amount = Number.isFinite(value) ? value : 0;
  node.position.set(origin.translation[0], origin.translation[1], origin.translation[2]);
  node.quaternion.set(origin.quaternion[0], origin.quaternion[1], origin.quaternion[2], origin.quaternion[3]);
  const axis = new Vector3(handle.axis[0], handle.axis[1], handle.axis[2]);
  if (axis.lengthSq() < 1e-12) return;
  axis.normalize();
  if (type === "prismatic") {
    // URDF states the axis in the joint frame, so the slide direction has to be
    // turned by the origin rotation before it can offset the origin position.
    node.position.add(axis.applyQuaternion(node.quaternion).multiplyScalar(amount));
    return;
  }
  node.quaternion.multiply(new Quaternion().setFromAxisAngle(axis, amount));
}

/**
 * The node a grasped object hangs from once the trajectory re-attaches it to
 * this robot (§8.6): the child link of the last joint named in `jointSequence`.
 * The Attach table only names the arm *object*, and a world file has no gripper
 * field to read (O1), so the joint sequence - which the world does name - is the
 * only non-guessed route to the tool centre point. Without a resolvable
 * sequence the deepest link nothing else hangs from is the closest match.
 */
export function endEffectorNode(robot: UrdfRobot, tree: UrdfTree, jointSequence: readonly string[]): Object3D | null {
  for (let index = jointSequence.length - 1; index >= 0; index -= 1) {
    const handle = tree.joints.get(jointSequence[index]!);
    const link = handle ? tree.links.get(handle.childLink) ?? null : null;
    if (link) return link;
  }
  const parents = new Set(robot.joints.map((joint) => joint.parent));
  let deepest: { node: Object3D; depth: number } | null = null;
  for (const link of robot.links) {
    if (parents.has(link.name)) continue;
    const depth = depthOf(robot, link.name);
    if (!deepest || depth > deepest.depth) deepest = { node: tree.links.get(link.name)!, depth };
  }
  return deepest?.node ?? null;
}

function depthOf(robot: UrdfRobot, link: string): number {
  const parentOf = new Map(robot.joints.map((joint) => [joint.child, joint.parent]));
  let depth = 0;
  let current = parentOf.get(link) ?? null;
  while (current !== null) {
    depth += 1;
    current = parentOf.get(current) ?? null;
  }
  return depth;
}

/**
 * The links a waypoint should follow (§7.3, Q102): the leaf links - ones no joint hangs
 * anything from - at the greatest depth in the `<link>`/`<joint>` graph. An arm has a
 * single one (`tool0`, `attachment_site`), so the set is that point; a parallel gripper has
 * the two symmetric finger pads, and the mean of a symmetric pair lies on the symmetry
 * plane, which is where the grasp happens - the same argument Q89 used for the mount
 * offset, this time read off the model instead of authored into a file. Simulator and
 * coupling tags (`<gazebo>`, `<mimic>`) say nothing about structure, so only joint
 * parentage is consulted here.
 */
export function toolTipLinks(robot: UrdfRobot): string[] {
  const parents = new Set(robot.joints.map((joint) => joint.parent));
  const names: string[] = [];
  let deepest = -1;
  for (const link of robot.links) {
    if (parents.has(link.name) || names.includes(link.name)) continue;
    const depth = depthOf(robot, link.name);
    if (depth > deepest) {
      deepest = depth;
      names.length = 0;
      names.push(link.name);
    } else if (depth === deepest) {
      names.push(link.name);
    }
  }
  return names;
}

function buildVisual(visual: UrdfVisual, meshes: MeshFactory): Object3D {
  const holder = new Object3D();
  holder.name = visual.name;
  holder.position.set(...visual.origin.translation);
  holder.quaternion.set(...visual.origin.quaternion);
  const { geometry } = visual;
  if (geometry.kind === "mesh") {
    const mesh = meshes(geometry.filename, visual.rgba);
    if (mesh) {
      mesh.scale.set(...geometry.scale);
      holder.add(mesh);
    }
    return holder;
  }
  const shape = geometry.kind === "box"
    ? new BoxGeometry(...geometry.size)
    : geometry.kind === "sphere"
      ? new SphereGeometry(geometry.radius, 24, 16)
      : new CylinderGeometry(geometry.radius, geometry.radius, geometry.length, 24);
  // The scene graph is Z-up (the renderer only tilts the outermost root), while
  // Three.js builds cylinders along Y, so the geometry itself has to be turned.
  if (geometry.kind === "cylinder") shape.rotateX(Math.PI / 2);
  const material = new MeshStandardMaterial();
  // Same convention as the rest of the renderer: rgb in the working colour space (§4.1).
  material.color.setRGB(visual.rgba[0], visual.rgba[1], visual.rgba[2]);
  material.opacity = visual.rgba[3];
  material.transparent = visual.rgba[3] < 1;
  const mesh = new Mesh(shape, material);
  mesh.name = `${visual.name}_${geometry.kind}`;
  holder.add(mesh);
  return holder;
}
