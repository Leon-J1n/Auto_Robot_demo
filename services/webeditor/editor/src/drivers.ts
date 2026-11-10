/**
 * Turning a trajectory into movement (spec v2.1 §8.3, §8.4).
 *
 * One frame of TRAJ is seven floats. For an object carrying `RobotDriver` they
 * are six joint angles plus the gripper; for anything else they are a local
 * pose relative to its parent. The driver only writes into nodes the view
 * hands it, so the whole module runs in a unit test without a GPU.
 */
import type { Object3D } from "three";
import type { RobotDriverProps } from "../../contracts/types";
import { endEffectorNode, setJointValue, type UrdfJointHandle, type UrdfRobot, type UrdfTree } from "./urdf";
import { poseAtFrame, type Traj } from "./traj";
import type { World } from "./world";

/** A frame carries six joints and one gripper value (§8.3). */
export const JOINT_VALUES_PER_FRAME = 6;

/** The moving view of the scene - the renderer implements it as `trajView`. */
export interface TrajView {
  /** the node of one object, or null when it is not in the scene */
  object(objectId: number): Object3D | null;
  /** the joint handles of one object's URDF, or null when it has none */
  joints(objectId: number): ReadonlyMap<string, UrdfJointHandle> | null;
  /** the parsed URDF and its node tree, for end-effector resolution */
  robot(objectId: number): { robot: UrdfRobot; tree: UrdfTree } | null;
  /** what a parent-less object hangs from: the scene root of §2.5 and §8.6 */
  root(): Object3D;
  /** where the object's own world file hangs it: a § 场景文件描述 §4.8 link, else its parent node */
  worldParent(objectId: number): Object3D | null;
  /** refresh world matrices after a frame (§8.4) */
  refresh(): void;
}

export interface DriveReport {
  /** object ids that moved */
  driven: number[];
  /** object ids the trajectory names but the scene no longer has (§8.2) */
  missing: number[];
}

/**
 * Drives one trajectory. A limit violation is reported once per joint, not once
 * per frame (§8.3), and the gripper value is only recorded: no world file names
 * the gripper joint, so moving geometry from it would be a guess (O1).
 */
export class TrajDriver {
  private readonly warned = new Set<string>();
  private readonly gripper = new Map<number, number>();

  constructor(
    private readonly view: TrajView,
    private readonly world: () => World,
    private readonly onWarn: (message: string) => void,
  ) {}

  /** The last recorded open/close value of one robot (0.0 open, O1: nothing else). */
  gripperOf(objectId: number): number | undefined {
    return this.gripper.get(objectId);
  }

  /** Forget the once-per-joint warnings, e.g. when a new trajectory replaces this one. */
  reset(): void {
    this.warned.clear();
    this.gripper.clear();
  }

  apply(traj: Traj, frame: number): DriveReport {
    const driven: number[] = [];
    const missing: number[] = [];
    for (const object of traj.objects) {
      const node = this.view.object(object.objectId);
      if (!node) {
        missing.push(object.objectId);
        continue;
      }
      const pose = poseAtFrame(object, frame);
      const driver = this.world().robotDriver(object.objectId);
      if (driver) this.driveRobot(object.objectId, driver, pose);
      else this.drivePose(node, pose);
      driven.push(object.objectId);
    }
    this.view.refresh();
    return { driven, missing };
  }

  /**
   * The node an object gets attached to when the trajectory re-parents it onto a
   * robot (§8.6): its tool centre point, or the object node itself when the
   * parent turns out not to be an articulated robot.
   */
  attachTarget(objectId: number): Object3D | null {
    const node = this.view.object(objectId);
    const driver = this.world().robotDriver(objectId);
    const built = this.view.robot(objectId);
    if (!driver || !built) return node;
    return endEffectorNode(built.robot, built.tree, jointSequence(driver)) ?? node;
  }

  /**
   * Hang the child where the Attach table says (§8.6). A null parent is the
   * scene-root sentinel, and a parent the scene no longer has leaves the child
   * exactly where it was - §8.6 reports that event, it does not move anything.
   * A parent that already hangs from the child is refused the same way: an
   * Attach table that re-parents an arm onto its own gripper would otherwise
   * close a cycle the moment it is honoured, and every later world-matrix walk
   * down that subtree never returns.
   */
  attach(childId: number, parentId: number | null): boolean {
    const node = this.view.object(childId);
    if (!node) return false;
    const parent = parentId === null ? this.view.root() : this.attachTarget(parentId);
    if (!parent) return false;
    if (parent === node || hangsFrom(node, parent)) {
      this.warnOnce(`attach:${childId}:${parentId}`,
        `object ${childId} cannot be hung from ${parentId}, which already hangs from it`);
      return false;
    }
    if (node.parent !== parent) parent.add(node);
    return true;
  }

  /**
   * Put the object back where its own world file hangs it (§7.2). The view resolves
   * that: a gripper the file mounts on a wrist link returns to that link node, an
   * apple on a table returns to the table.
   */
  restoreParent(objectId: number): void {
    const node = this.view.object(objectId);
    if (!node || this.world().get(objectId) === undefined) return;
    const parent = this.view.worldParent(objectId) ?? this.view.root();
    if (node.parent !== parent) parent.add(node);
  }

  private driveRobot(objectId: number, driver: RobotDriverProps, pose: ArrayLike<number>): void {
    const handles = this.view.joints(objectId);
    if (!handles) {
      this.warnOnce(`tree:${objectId}`, `robot ${objectId} has no URDF in the scene, its joint values cannot be driven`);
      return;
    }
    applyFrameJointPositions(objectId, driver, pose, handles, (key, message) => this.warnOnce(key, message));
    const gripper = pose[JOINT_VALUES_PER_FRAME];
    if (typeof gripper === "number") this.gripper.set(objectId, gripper);
  }

  private drivePose(node: Object3D, pose: ArrayLike<number>): void {
    node.position.set(number(pose, 0, 0), number(pose, 1, 0), number(pose, 2, 0));
    // §2.8: the frame quaternion is qx, qy, qz, qw - what Three.js reads.
    node.quaternion.set(number(pose, 3, 0), number(pose, 4, 0), number(pose, 5, 0), number(pose, 6, 1));
  }

  private warnOnce(key: string, message: string): void {
    if (this.warned.has(key)) return;
    this.warned.add(key);
    this.onWarn(message);
  }
}

/**
 * Whether `object` already hangs somewhere below `node` - the parent chain an
 * Attach event must not close into a ring (§8.6).
 */
function hangsFrom(node: Object3D, object: Object3D): boolean {
  for (let current = object.parent; current !== null; current = current.parent) {
    if (current === node) return true;
  }
  return false;
}

function jointSequence(driver: RobotDriverProps): string[] {
  return Array.isArray(driver.joint_sequence) ? driver.joint_sequence.filter((name) => typeof name === "string") : [];
}

/** One parked joint and the dictionary its value came from. */
export interface DeclaredJoint {
  name: string;
  value: number;
  source: "initial" | "now";
}

/**
 * The joints a scene parks the arm at (§ 场景文件描述 §4.7): `now_joint_positions_rad`
 * overrides only the names it carries and `initial_joint_positions_rad` fills the rest,
 * so a caller can move three joints without restating the other six. A joint neither
 * dictionary names is absent here, which is what keeps it at whatever angle it is at.
 * Each entry remembers its source because a wrong joint name has to be reported with
 * the dictionary that wrote it.
 */
export function declaredJointPositions(driver: RobotDriverProps | null | undefined): DeclaredJoint[] {
  if (!driver) return [];
  const declared: DeclaredJoint[] = [];
  const seen = new Map<string, DeclaredJoint>();
  for (const [source, pose] of [["initial", driver.initial_joint_positions_rad],
    ["now", driver.now_joint_positions_rad]] as const) {
    if (!isDictionary(pose)) continue;
    for (const [name, value] of Object.entries(pose)) {
      const entry = seen.get(name);
      if (entry) { entry.value = value; entry.source = source; continue; }
      const added: DeclaredJoint = { name, value, source };
      seen.set(name, added);
      declared.push(added);
    }
  }
  return declared;
}

function isDictionary(pose: unknown): pose is Record<string, number> {
  return !!pose && typeof pose === "object" && !Array.isArray(pose);
}

/**
 * Park a robot at the pose its own world file declares (§ 场景文件描述 §4.7), which is
 * what a scene shows before any trajectory takes over. Joint names the URDF does not
 * carry are reported the same way §8.3 reports them - once, and without stopping.
 */
export function applyDeclaredJointPositions(
  driver: RobotDriverProps,
  handles: ReadonlyMap<string, UrdfJointHandle>,
  objectId: number,
  onWarn: (message: string) => void,
): void {
  for (const { name, value, source } of declaredJointPositions(driver)) {
    const handle = handles.get(name);
    if (!handle) {
      onWarn(`robot ${objectId}: ${source} joint "${name}" is not in its URDF`);
      continue;
    }
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    writeJoint(handles, handle, value, driver, objectId, (_joint, message) => {
      onWarn(message);
    });
  }
}

/**
 * Write one trajectory frame's joint angles into a set of handles (§8.3): the
 * `joint_sequence` order, the `joint_limits` clamp, and the `<mimic>` followers.
 * Both readers of a frame go through this - the live arm through `TrajDriver`, the
 * segment-end ghost (§7.3, Q96) directly - so a ghost can never preview a pose the
 * arm could not reach; only what the two do with `warn` differs.
 */
export function applyFrameJointPositions(
  objectId: number,
  driver: RobotDriverProps,
  pose: ArrayLike<number>,
  handles: ReadonlyMap<string, UrdfJointHandle>,
  warn: (key: string, message: string) => void,
): void {
  const sequence = jointSequence(driver);
  if (sequence.length > JOINT_VALUES_PER_FRAME) {
    warn(`sequence:${objectId}`,
      `robot ${objectId} declares ${sequence.length} joints, a trajectory frame only carries ${JOINT_VALUES_PER_FRAME} values`);
  }
  for (let index = 0; index < Math.min(JOINT_VALUES_PER_FRAME, sequence.length); index += 1) {
    const name = sequence[index]!;
    const handle = handles.get(name);
    if (!handle) {
      warn(`joint:${objectId}:${name}`, `robot ${objectId}: joint "${name}" is not in its URDF`);
      continue;
    }
    const raw = pose[index];
    if (typeof raw !== "number") continue;
    writeJoint(handles, handle, raw, driver, objectId, (joint, message) => {
      warn(`limit:${objectId}:${joint}`, message);
    });
  }
}

/**
 * Write one joint, then every joint whose `<mimic>` chain reaches it (§6.4).
 * A follower holds `multiplier * source + offset` of the value the source
 * actually got, so a limit clamp on the source moves the follower to the
 * clamped position too, and the follower is never commanded directly.
 */
function writeJoint(
  handles: ReadonlyMap<string, UrdfJointHandle>,
  handle: UrdfJointHandle,
  value: number,
  driver: RobotDriverProps,
  objectId: number,
  warn: (joint: string, message: string) => void,
): void {
  const pending: Array<{ handle: UrdfJointHandle; value: number }> = [{ handle, value }];
  for (let index = 0; index < pending.length; index += 1) {
    const current = pending[index]!;
    const applied = clampToLimit(current.value, driver.joint_limits?.[current.handle.name],
      (message) => { warn(current.handle.name, message); }, current.handle.name, objectId);
    setJointValue(current.handle, applied);
    for (const follower of handles.values()) {
      const mimic = follower.mimic;
      if (mimic && mimic.joint === current.handle.name) {
        pending.push({ handle: follower, value: mimic.multiplier * applied + mimic.offset });
      }
    }
  }
}

function number(pose: ArrayLike<number>, index: number, fallback: number): number {
  const value = pose[index];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** §8.3: clamp what leaves the declared range, and say so once. */
function clampToLimit(value: number, limit: unknown, warn: (message: string) => void, name: string, objectId: number): number {
  if (!Array.isArray(limit) || limit.length !== 2) return value;
  const lower = limit[0];
  const upper = limit[1];
  if (typeof lower !== "number" || typeof upper !== "number" || !Number.isFinite(lower) || !Number.isFinite(upper)) {
    return value;
  }
  if (value >= lower && value <= upper) return value;
  warn(`joint "${name}" of robot ${objectId} is at ${value}, outside [${lower}, ${upper}]; it was clamped`);
  return value < lower ? lower : upper;
}
