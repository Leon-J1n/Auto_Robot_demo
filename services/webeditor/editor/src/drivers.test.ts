// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { Group, Object3D, Quaternion, Scene, Vector3 } from "three";
import type { WorldComponent } from "../../contracts/types";
import { applyDeclaredJointPositions, applyFrameJointPositions, declaredJointPositions, TrajDriver, type TrajView } from "./drivers";
import { buildUrdfTree, parseUrdf, setJointValue, type UrdfTree } from "./urdf";
import { decodeTraj, type Traj, type TrajObject, type TrajSegment } from "./traj";
import { parseWorld, type World } from "./world";

/**
 * Three joints on purpose: two revolute about +Z at different heights and one
 * prismatic whose axis is turned 90 deg by its own origin, so a wrong origin
 * composition cannot hide behind a symmetric arm.
 *
 * At the zero pose the tool sits 0.3 up, 0.5 + 0.4 along +X: (0.9, 0, 0.3).
 */
const ARM_URDF = `<?xml version="1.0"?>
<robot name="arm">
  <link name="base"><visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry><box size="0.1 0.1 0.1"/></geometry></visual></link>
  <link name="link1"><visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry><box size="0.1 0.1 0.1"/></geometry></visual></link>
  <link name="link2"><visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry><box size="0.1 0.1 0.1"/></geometry></visual></link>
  <link name="tool"><visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry><box size="0.05 0.05 0.05"/></geometry></visual></link>
  <joint name="joint1" type="revolute">
    <parent link="base"/><child link="link1"/>
    <origin xyz="0 0 0.3" rpy="0 0 0"/><axis xyz="0 0 1"/>
  </joint>
  <joint name="joint2" type="revolute">
    <parent link="link1"/><child link="link2"/>
    <origin xyz="0.5 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>
  </joint>
  <joint name="joint3" type="prismatic">
    <parent link="link2"/><child link="tool"/>
    <origin xyz="0.4 0 0" rpy="0 0 1.5707963267948966"/><axis xyz="1 0 0"/>
  </joint>
</robot>
`;

/**
 * The shape the real grippers have: one joint is commanded and the rest of the
 * fingers are derived from it by `<mimic>` (§6.4). `left_joint` mirrors
 * `right_joint` through a yawed origin, and `left_finger_joint` sits one more
 * level down, so a derivation that only goes one hop deep cannot pass.
 */
const GRIPPER_URDF = `<?xml version="1.0"?>
<robot name="gripper">
  <link name="base"/><link name="right"/><link name="left"/>
  <link name="right_finger"/><link name="left_finger"/>
  <joint name="right_joint" type="revolute">
    <parent link="base"/><child link="right"/>
    <origin xyz="0 0 0.1" rpy="0 0 0"/><axis xyz="0 0 1"/>
  </joint>
  <joint name="left_joint" type="revolute">
    <parent link="base"/><child link="left"/>
    <origin xyz="0 0 0.1" rpy="0 0 3.14159265"/><axis xyz="0 0 1"/>
    <mimic joint="right_joint"/>
  </joint>
  <joint name="right_finger_joint" type="revolute">
    <parent link="right"/><child link="right_finger"/>
    <origin xyz="0.4 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>
    <mimic joint="right_joint" multiplier="-1" offset="0.05"/>
  </joint>
  <joint name="left_finger_joint" type="revolute">
    <parent link="left"/><child link="left_finger"/>
    <origin xyz="0.4 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>
    <mimic joint="left_joint"/>
  </joint>
</robot>
`;

/** happy-dom replaces the global `URL`, so `new URL(..., import.meta.url)` stops being a `file:` URL. */
const trajFixture = resolve(dirname(fileURLToPath(import.meta.url)), "../../tests/fixtures/robot_grasp.traj");

function trajBytes(): ArrayBuffer {
  const raw = readFileSync(trajFixture);
  const copy = new ArrayBuffer(raw.byteLength);
  new Uint8Array(copy).set(raw);
  return copy;
}

function transform(parent: number | null = null): WorldComponent {
  return {
    component_type: "Transform",
    properties: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent },
  };
}

function driverComponent(jointSequence: string[], jointLimits: Record<string, [number, number]> = {}): WorldComponent {
  return { component_type: "RobotDriver", properties: { joint_sequence: jointSequence, joint_limits: jointLimits } };
}

/** The mount the real gripper files write: a pose framed on one link of the parent arm. */
function attachmentComponent(parentId: number, link: string): WorldComponent {
  return {
    component_type: "RobotAttachment",
    properties: {
      parent_object_id: parentId, parent_link: link, connection_type: "fixed",
      transform: { position: [0, 0, 0.1], quaternion_xyzw: [0, 0, 0, 1], reference_frame: "parent_link" },
    },
  };
}

function objectOf(id: number, name: string, components: WorldComponent[]): Record<string, unknown> {
  return { object_id_in_scene: id, object_name_in_scene: name, components };
}

function worldOf(objects: Record<string, unknown>[]): World {
  return parseWorld({ scene_schema_version: 1, scene_version: 1, scene_id: 3, scene_name: "drive", objects });
}

const robotWorld = (limits: Record<string, [number, number]> = {}): World =>
  worldOf([
    objectOf(1, "arm", [transform(), driverComponent(["joint1", "joint2", "joint3"], limits)]),
    objectOf(2, "apple", [transform(1)]),
  ]);

function segment(startFrame: number, poses: number[][]): TrajSegment {
  return { startFrame, frameCount: poses.length, dataOffset: 0, step: 1, poses: new Float32Array(poses.flat()) };
}

function objectPose(id: number, segments: TrajSegment[] = [], defaultPose = [0, 0, 0, 0, 0, 0, 1]): TrajObject {
  return { objectId: id, parentId: null, poseDim: 7, defaultPose, segments };
}

function trajOf(objects: TrajObject[], totalFrames = 720): Traj {
  return {
    version: 2, fps: 60, totalFrames, objectCount: objects.length, attachCount: 0,
    durationSeconds: totalFrames / 60, byteLength: 0, dataStart: 0,
    objects, attaches: [], steps: [],
  };
}

interface Harness {
  driver: TrajDriver;
  warnings: string[];
  nodes: Map<number, Object3D>;
  tree: UrdfTree;
  /** the tool link in world space - the end of the chain every joint moves */
  tool(): Vector3;
}

/**
 * The renderer's `trajView` shape, filled by a real URDF tree. The stage mirrors
 * the world file, so an object a trajectory names but the world dropped really
 * is missing rather than being silently present.
 */
function harness(world: World, robotObjectId = 1, source = ARM_URDF): Harness {
  const robot = parseUrdf(source);
  const tree = buildUrdfTree(robot);
  const scene = new Scene();
  const nodes = new Map<number, Object3D>();
  for (const object of world.objects) {
    const group = new Group();
    group.name = `object ${object.id}`;
    nodes.set(object.id, group);
    scene.add(group);
  }
  nodes.get(robotObjectId)?.add(tree.root);

  const warnings: string[] = [];
  const view: TrajView = {
    object: (objectId) => nodes.get(objectId) ?? null,
    joints: (objectId) => (objectId === robotObjectId ? tree.joints : null),
    robot: (objectId) => (objectId === robotObjectId ? { robot, tree } : null),
    root: () => scene,
    // The same rule the renderer applies: an accepted attachment hangs the object from
    // the parent robot's link, everything else from the parent object's node.
    worldParent: (objectId) => {
      const object = world.get(objectId);
      if (!object) return null;
      const fallback = object.transform.parent === null ? scene : nodes.get(object.transform.parent) ?? scene;
      const mount = object.attachmentMount;
      if (!mount || mount.objectId !== robotObjectId || mount.link === null) return fallback;
      return tree.links.get(mount.link) ?? fallback;
    },
    refresh: () => scene.updateMatrixWorld(true),
  };
  return {
    driver: new TrajDriver(view, () => world, (message) => { warnings.push(message); }),
    warnings,
    nodes,
    tree,
    tool: () => tree.links.get("tool")!.getWorldPosition(new Vector3()),
  };
}

/** Float32Array round-trips, so exact equality is the wrong check. */
function expectClose(actual: Vector3, [x, y, z]: [number, number, number]): void {
  expect(actual.x).toBeCloseTo(x, 4);
  expect(actual.y).toBeCloseTo(y, 4);
  expect(actual.z).toBeCloseTo(z, 4);
}

const identity = (): Quaternion => new Quaternion();

/** The turn a joint node carries on top of its own origin: origin * rotZ(t) -> t. */
function turnOf(tree: UrdfTree, name: string): number {
  const handle = tree.joints.get(name)!;
  const relative = handle.node.quaternion.clone()
    .premultiply(new Quaternion().fromArray(handle.origin.quaternion).invert());
  return 2 * Math.atan2(relative.z, relative.w);
}

/** One commanded driver, the fingers derived: what the real grippers look like. */
function gripperHarness(limits: Record<string, [number, number]> = {}): Harness {
  const world = worldOf([objectOf(1, "gripper", [transform(), driverComponent(["right_joint"], limits)])]);
  return harness(world, 1, GRIPPER_URDF);
}

describe("TrajDriver drives robots (§8.3, §8.4)", () => {
  it("sends the first six values to joint_sequence and keeps the seventh as the gripper", () => {
    const { driver, nodes, tree, warnings } = harness(robotWorld());
    // Values 4..6 would be a quaternion for an ordinary object; a robot must not read them.
    const traj = trajOf([objectPose(1, [segment(0, [[Math.PI / 2, 0, 0, 9, 9, 9, 0.72]])]), objectPose(2)]);

    expect(driver.apply(traj, 0).driven).toEqual([1, 2]);
    expect(warnings).toEqual([]);
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(Math.PI / 2, 4);
    expect(tree.joints.get("joint2")!.node.quaternion.angleTo(identity())).toBe(0);
    expect(nodes.get(1)!.position.toArray()).toEqual([0, 0, 0]);
    expect(nodes.get(1)!.quaternion.angleTo(identity())).toBe(0);
    expect(driver.gripperOf(1)).toBeCloseTo(0.72, 4);
  });

  it("composes each joint value on top of its own origin down the chain (§8.4)", () => {
    const { driver, tool } = harness(robotWorld());
    const traj = trajOf([objectPose(1, [segment(0, [
      [0, 0, 0, 0, 0, 0, 0],
      [Math.PI / 2, 0, 0, 0, 0, 0, 0],
      [0, Math.PI / 2, 0, 0, 0, 0, 0],
      [0, 0, 0.3, 0, 0, 0, 0],
    ])])]);

    driver.apply(traj, 0);
    expectClose(tool(), [0.9, 0, 0.3]);
    driver.apply(traj, 1);
    // 90 deg about Z swings the whole arm off +X onto +Y.
    expectClose(tool(), [0, 0.9, 0.3]);
    driver.apply(traj, 2);
    // joint2 turns inside joint1's frame, so only link2 and the tool move.
    expectClose(tool(), [0.5, 0.4, 0.3]);
    driver.apply(traj, 3);
    // The prismatic axis is +X in the joint frame, but its origin yaws 90 deg: 0.3 m along +Y.
    expectClose(tool(), [0.9, 0.3, 0.3]);
    driver.apply(traj, 0);
    expectClose(tool(), [0.9, 0, 0.3]);
  });

  it("clamps a value outside joint_limits and says so once per joint, not once per frame (§8.3)", () => {
    const { driver, warnings, tree, tool } = harness(robotWorld({ joint1: [-1, 1] }));
    const traj = trajOf([objectPose(1, [segment(0, [[2, 0, 0, 0, 0, 0, 0], [3, 0, 0, 0, 0, 0, 0]])])]);

    driver.apply(traj, 0);
    driver.apply(traj, 1);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('"joint1" of robot 1 is at 2');
    expect(warnings[0]).toContain("outside [-1, 1]");
    // What reached the node is the clamp bound, not the file's 2 or 3.
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(1, 4);
    expectClose(tool(), [0.9 * Math.cos(1), 0.9 * Math.sin(1), 0.3]);
    // A new trajectory is a new chance to complain about the same joint.
    driver.reset();
    driver.apply(traj, 0);
    expect(warnings).toHaveLength(2);
  });

  it("records the seventh value as data only: no world file names a gripper joint (O1)", () => {
    const { driver, tree, tool } = harness(robotWorld());
    const traj = trajOf([objectPose(1, [segment(0, [
      [0, 0, 0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0, 0, 0.72],
    ])])]);

    driver.apply(traj, 0);
    expect(driver.gripperOf(1)).toBe(0);
    const open = tool();
    driver.apply(traj, 1);
    expectClose(tool(), [open.x, open.y, open.z]);
    expect(tree.joints.get("joint3")!.node.position.toArray()).toEqual([0.4, 0, 0]);
    expect(driver.gripperOf(1)).toBeCloseTo(0.72, 4);
  });

  it("warns once for a joint the URDF does not have without shifting the other values", () => {
    const world = worldOf([objectOf(1, "arm", [transform(), driverComponent(["nope", "joint1", "joint2"], {})])]);
    const { driver, warnings, tree } = harness(world);
    const traj = trajOf([objectPose(1, [segment(0, [[0.4, 0.2, 0.1, 0, 0, 0, 0]])])]);

    driver.apply(traj, 0);
    driver.apply(traj, 0);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('"nope"');
    // The unreadable component keeps its slot: index 1 is still joint1, at 0.2.
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(0.2, 4);
    expect(tree.joints.get("joint2")!.node.quaternion.angleTo(identity())).toBeCloseTo(0.1, 4);
  });

  it("warns when a robot declares more joints than one trajectory frame carries", () => {
    const world = worldOf([
      objectOf(1, "arm", [transform(), driverComponent(["joint1", "joint2", "joint3", "joint1", "joint2", "joint3", "joint4"], {})]),
    ]);
    const { driver, warnings } = harness(world);
    const traj = trajOf([objectPose(1, [segment(0, [[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]])])]);

    driver.apply(traj, 0);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("declares 7 joints");
    expect(warnings[0]).toContain("only carries 6 values");
    // The seventh value stays the gripper even for a robot that overflows a frame.
    expect(driver.gripperOf(1)).toBeCloseTo(0.7, 4);
  });

  it("reports trajectory objects the scene no longer has (§8.2)", () => {
    const world = worldOf([objectOf(1, "arm", [transform(), driverComponent(["joint1"], {})])]);
    const { driver } = harness(world);

    const report = driver.apply(trajOf([objectPose(1), objectPose(9)]), 0);
    expect(report.driven).toEqual([1]);
    expect(report.missing).toEqual([9]);
  });

  it("says once that a RobotDriver whose URDF never arrived cannot be driven", () => {
    const world = robotWorld();
    const scene = new Scene();
    const node = new Group();
    scene.add(node);
    const warnings: string[] = [];
    const view: TrajView = {
      object: (objectId) => (objectId === 1 ? node : null),
      joints: () => null,
      robot: () => null,
      root: () => scene,
      worldParent: () => scene,
      refresh: () => scene.updateMatrixWorld(true),
    };
    const driver = new TrajDriver(view, () => world, (message) => { warnings.push(message); });

    const traj = trajOf([objectPose(1, [segment(0, [[1, 1, 1, 0, 0, 0, 0], [2, 1, 1, 0, 0, 0, 0]])])]);
    expect(driver.apply(traj, 0).driven).toEqual([1]);
    driver.apply(traj, 1);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("no URDF");
    // Neither a joint pose nor the fallback object pose is written.
    expect(node.position.toArray()).toEqual([0, 0, 0]);
    expect(driver.gripperOf(1)).toBeUndefined();
  });
});

/**
 * `<mimic>` (§6.4) is why Robotiq's fingers close together: the URDF commands one
 * driver joint and derives the rest, so a follower never takes a value of its own.
 */
describe("TrajDriver follows mimic joints (§6.4)", () => {
  it("derives every mimicking joint from the single joint the trajectory commands", () => {
    const { driver, tree, warnings } = gripperHarness();
    const traj = trajOf([objectPose(1, [segment(0, [[0.4, 0, 0, 0, 0, 0, 0]])])]);

    driver.apply(traj, 0);
    expect(warnings).toEqual([]);
    expect(turnOf(tree, "right_joint")).toBeCloseTo(0.4, 4);
    expect(turnOf(tree, "left_joint")).toBeCloseTo(0.4, 4);
    // multiplier -1 and offset 0.05, then the chain one level deeper.
    expect(turnOf(tree, "right_finger_joint")).toBeCloseTo(-0.35, 4);
    expect(turnOf(tree, "left_finger_joint")).toBeCloseTo(0.4, 4);
  });

  it("derives followers from the clamped source, not from the value the file asked for (§8.3)", () => {
    const { driver, tree, warnings } = gripperHarness({ right_joint: [-1, 1] });
    const traj = trajOf([objectPose(1, [segment(0, [[2, 0, 0, 0, 0, 0, 0]])])]);

    driver.apply(traj, 0);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('"right_joint" of robot 1 is at 2');
    expect(turnOf(tree, "right_joint")).toBeCloseTo(1, 4);
    expect(turnOf(tree, "left_joint")).toBeCloseTo(1, 4);
    expect(turnOf(tree, "right_finger_joint")).toBeCloseTo(-0.95, 4);
  });

  it("re-derives a follower the sequence also names: its slot is a copy, not a command", () => {
    const world = worldOf([objectOf(1, "gripper", [transform(), driverComponent(["right_finger_joint", "right_joint"], {})])]);
    const { driver, tree, warnings } = harness(world, 1, GRIPPER_URDF);
    const traj = trajOf([objectPose(1, [segment(0, [[0.9, 0.2, 0, 0, 0, 0, 0]])])]);

    driver.apply(traj, 0);
    expect(warnings).toEqual([]);
    expect(turnOf(tree, "right_joint")).toBeCloseTo(0.2, 4);
    expect(turnOf(tree, "right_finger_joint")).toBeCloseTo(-0.15, 4);
  });

  it("parks the whole gripper from one mimic source in initial_joint_positions_rad", () => {
    const tree = buildUrdfTree(parseUrdf(GRIPPER_URDF));
    tree.root.updateMatrixWorld(true);
    const warnings: string[] = [];
    applyDeclaredJointPositions({ initial_joint_positions_rad: { right_joint: 0.6 } }, tree.joints, 1, (m) => { warnings.push(m); });

    expect(warnings).toEqual([]);
    expect(turnOf(tree, "left_joint")).toBeCloseTo(0.6, 4);
    expect(turnOf(tree, "right_finger_joint")).toBeCloseTo(-0.55, 4);
    expect(turnOf(tree, "left_finger_joint")).toBeCloseTo(0.6, 4);
  });
});

describe("TrajDriver drives ordinary objects (§8.3)", () => {
  it("writes x, y, z and the quaternion as one local pose in xyzw order (§2.8)", () => {
    const { driver, nodes } = harness(worldOf([objectOf(2, "apple", [transform()])]), 99);
    const traj = trajOf([objectPose(2, [segment(0, [[0.35, 0.04, 0.1, 0, 0, 0.7071, 0.7071]])])]);

    driver.apply(traj, 0);
    expectClose(nodes.get(2)!.position, [0.35, 0.04, 0.1]);
    const q = nodes.get(2)!.quaternion;
    expect(q.x).toBeCloseTo(0, 4);
    expect(q.y).toBeCloseTo(0, 4);
    expect(q.z).toBeCloseTo(0.7071, 4);
    expect(q.w).toBeCloseTo(0.7071, 4);
  });

  it("treats an articulated-looking object with no RobotDriver as a pose, not as joints", () => {
    const { driver, tree, nodes } = harness(worldOf([objectOf(1, "arm", [transform()])]));
    const traj = trajOf([objectPose(1, [segment(0, [[1, 2, 3, 0, 0, Math.SQRT1_2, Math.SQRT1_2]])])]);

    driver.apply(traj, 0);
    expectClose(nodes.get(1)!.position, [1, 2, 3]);
    // The last four floats are a quaternion for a pose: 90 deg about Z, which
    // turns the whole expanded arm, while every joint value stays at its origin.
    expectClose(tree.links.get("tool")!.getWorldPosition(new Vector3()), [1, 2.9, 3.3]);
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBe(0);
  });

  it("rests on the Index default before the first segment and holds the last frame over a gap (§8.5)", () => {
    const { driver, nodes } = harness(worldOf([objectOf(2, "apple", [transform()])]), 99);
    const object = objectPose(2, [
      segment(100, [[1, 0, 0, 0, 0, 0, 1], [2, 0, 0, 0, 0, 0, 1]]),
      segment(400, [[3, 0, 0, 0, 0, 0, 1]]),
    ], [0.5, 0.5, 0.5, 0, 0, 0, 1]);
    const traj = trajOf([object]);

    for (const [frame, x] of [[0, 0.5], [99, 0.5], [100, 1], [101, 2], [399, 2], [400, 3], [719, 3]] as const) {
      driver.apply(traj, frame);
      expect(nodes.get(2)!.position.x).toBeCloseTo(x, 4);
    }
  });
});

describe("end-effector attachment (§8.6)", () => {
  it("attaches a grasped object to the last joint's child link so it rides the arm", () => {
    const { driver, nodes, tree } = harness(robotWorld());
    const target = driver.attachTarget(1);
    expect(target).toBe(tree.links.get("tool") ?? null);

    const traj = trajOf([
      objectPose(1, [segment(0, [
        [0, 0, 0, 0, 0, 0, 0],
        [Math.PI / 2, 0, 0, 0, 0, 0, 0.72],
      ])]),
      objectPose(2),
    ]);
    nodes.get(2)!.removeFromParent();
    target!.add(nodes.get(2)!);

    driver.apply(traj, 0);
    expectClose(nodes.get(2)!.getWorldPosition(new Vector3()), [0.9, 0, 0.3]);
    driver.apply(traj, 1);
    expectClose(nodes.get(2)!.getWorldPosition(new Vector3()), [0, 0.9, 0.3]);
  });

  it("attaches to an ordinary object node when the parent is not a robot", () => {
    const { driver, nodes } = harness(worldOf([objectOf(2, "apple", [transform()]), objectOf(3, "box", [transform()])]), 99);

    expect(driver.attachTarget(3)).toBe(nodes.get(3)!);
    expect(driver.attachTarget(9)).toBeNull();
  });

  it("moves a child onto the parent the Attach table names and back to the one its file names (§8.6, §7.2)", () => {
    const { driver, nodes, tree } = harness(robotWorld());
    const apple = nodes.get(2)!;

    expect(driver.attach(2, 1)).toBe(true);
    expect(apple.parent).toBe(tree.links.get("tool"));

    // A parent the scene no longer has is §8.6's report, not a move.
    expect(driver.attach(2, 9)).toBe(false);
    expect(apple.parent).toBe(tree.links.get("tool"));

    // The world file hangs the apple on object 1, so that is where a restore goes.
    driver.restoreParent(2);
    expect(apple.parent).toBe(nodes.get(1)!);
    expect(driver.attach(9, 1)).toBe(false);
    driver.restoreParent(9);
  });

  it("returns a file-mounted object to its wrist link, not to the arm's own node (§4.8)", () => {
    const world = worldOf([
      objectOf(1, "arm", [transform(), driverComponent(["joint1", "joint2", "joint3"])]),
      objectOf(2, "gripper", [transform(1), attachmentComponent(1, "link2")]),
    ]);
    const { driver, nodes, tree } = harness(world);

    expect(driver.attach(2, 1)).toBe(true);
    expect(nodes.get(2)!.parent).toBe(tree.links.get("tool"));
    // The world file frames the gripper on `link2`, so that is where it comes back to.
    driver.restoreParent(2);
    expect(nodes.get(2)!.parent).toBe(tree.links.get("link2"));
  });

  it("sends a child to the scene root for the parent-less sentinel (§2.5)", () => {
    const { driver, nodes } = harness(robotWorld());
    const apple = nodes.get(2)!;

    driver.attach(2, 1);
    expect(driver.attach(2, null)).toBe(true);
    expect(apple.parent).toBe(nodes.get(1)!.parent);
  });

  it("refuses an Attach that would hang the arm inside its own gripper (§8.6)", () => {
    const world = worldOf([
      objectOf(1, "arm", [transform(), driverComponent(["joint1", "joint2", "joint3"])]),
      objectOf(3, "gripper", [transform(1), driverComponent(["right_joint"])]),
    ]);
    const { driver, nodes, tree, warnings } = harness(world);
    const arm = nodes.get(1)!;
    const restParent = arm.parent;

    // The grasp event that starts it: the gripper goes to the arm's tool link.
    expect(driver.attach(3, 1)).toBe(true);
    expect(nodes.get(3)!.parent).toBe(tree.links.get("tool"));

    // A later frame of a file naming the arm as the child and its own gripper as
    // the parent would close the ring. Honouring it wedges every later
    // `updateMatrixWorld`, so it is reported and the arm stays put.
    expect(driver.attach(1, 3)).toBe(false);
    // A child hung from itself is the same ring, one step shorter.
    expect(driver.attach(1, 1)).toBe(false);
    expect(arm.parent).toBe(restParent);
    expect(warnings).toEqual([
      "object 1 cannot be hung from 3, which already hangs from it",
      "object 1 cannot be hung from 1, which already hangs from it",
    ]);

    // The refusal is the ring, not the parent: a free-standing gripper re-hangs the arm.
    nodes.get(3)!.removeFromParent();
    expect(driver.attach(1, 3)).toBe(true);
    expect(arm.parent).toBe(nodes.get(3)!);
  });

  it("drives the real grasp file and brings the apple from frame 0 to frame 340", () => {
    const traj = decodeTraj(trajBytes());
    const { driver, nodes, tree, tool, warnings } = harness(robotWorld());
    expect(traj.objects.map((object) => object.objectId)).toEqual([1, 2, 3]);

    const atRest = driver.apply(traj, 0);
    expect(atRest.driven).toEqual([1, 2]);
    // The file's third object is the place target, which this stage does not have.
    expect(atRest.missing).toEqual([3]);
    expect(warnings).toEqual([]);
    // The grasp starts with an open gripper and joint1 at its Index default, 2.75.
    expect(driver.gripperOf(1)).toBe(0);
    const jointRest = tree.joints.get("joint1")!.node.quaternion.clone();
    expect(jointRest.angleTo(identity())).toBeCloseTo(2.75, 3);

    nodes.get(2)!.removeFromParent();
    driver.attachTarget(1)!.add(nodes.get(2)!);
    driver.apply(traj, 0);
    const appleRest = nodes.get(2)!.getWorldPosition(new Vector3());
    // The apple keeps its own local pose: 0.35, 0, 0.04 from the tool link it hangs on.
    expect(appleRest.distanceTo(tool())).toBeCloseTo(Math.hypot(0.35, 0.04), 4);

    driver.apply(traj, 340);
    // Frame 340 of the grasp segment has joint1 at 0.39072, so the arm has turned 2.36 rad.
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(jointRest)).toBeCloseTo(2.75 - 0.39072, 3);
    const appleGrabbed = nodes.get(2)!.getWorldPosition(new Vector3());
    expect(appleGrabbed.distanceTo(appleRest)).toBeGreaterThan(0.05);
    // Rigid attachment: only the arm moved, the grasp offset did not.
    expect(appleGrabbed.distanceTo(tool())).toBeCloseTo(Math.hypot(0.35, 0.04), 4);
    expect(driver.gripperOf(1)).toBeCloseTo(0.72, 4);
  });
});

/** The parked pose is what a scene shows before any trajectory takes over, and `now` owns it. */
describe("applying the parked joint pose (§ 场景文件描述 §4.7)", () => {
  const freshTree = (): UrdfTree => {
    const tree = buildUrdfTree(parseUrdf(ARM_URDF));
    tree.root.updateMatrixWorld(true);
    return tree;
  };
  const toolOf = (tree: UrdfTree): Vector3 => tree.links.get("tool")!.getWorldPosition(new Vector3());

  it("writes the joints it names and leaves the others at their origins", () => {
    const tree = freshTree();
    const warnings: string[] = [];
    applyDeclaredJointPositions({ initial_joint_positions_rad: { joint1: Math.PI / 2 } }, tree.joints, 1, (m) => { warnings.push(m); });

    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(Math.PI / 2, 4);
    expect(tree.joints.get("joint2")!.node.quaternion.angleTo(identity())).toBe(0);
    expect(warnings).toEqual([]);
    // The whole chain rides the parked pose, which is the frame a RobotAttachment measures in (§4.8).
    expectClose(toolOf(tree), [0, 0.9, 0.3]);
  });

  it("clamps a parked value that leaves joint_limits and says so (§8.3)", () => {
    const tree = freshTree();
    const warnings: string[] = [];
    applyDeclaredJointPositions({ joint_limits: { joint1: [-1, 1] }, initial_joint_positions_rad: { joint1: 2 } }, tree.joints, 1, (m) => { warnings.push(m); });

    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(1, 4);
    expect(warnings).toEqual(['joint "joint1" of robot 1 is at 2, outside [-1, 1]; it was clamped']);
  });

  it("reports a name the URDF does not carry without stopping the joints it does", () => {
    const tree = freshTree();
    const warnings: string[] = [];
    // The gripper names of the real scene file are not URDF joints; loading has to survive that.
    applyDeclaredJointPositions({ initial_joint_positions_rad: { robotiq2f85_base: 0.4, joint2: 0.2 } }, tree.joints, 3, (m) => { warnings.push(m); });

    expect(warnings).toEqual(['robot 3: initial joint "robotiq2f85_base" is not in its URDF']);
    expect(tree.joints.get("joint2")!.node.quaternion.angleTo(identity())).toBeCloseTo(0.2, 4);
  });

  it("ignores a driver without parked positions and a value that is not a number", () => {
    const untouched = freshTree();
    applyDeclaredJointPositions({ joint_sequence: ["joint1"] }, untouched.joints, 1, () => undefined);
    expect(untouched.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBe(0);

    const tree = freshTree();
    const warnings: string[] = [];
    // A file that writes a string or a NaN in place of a radian is skipped, not applied.
    const ragged = { joint1: "0.5", joint2: Number.NaN } as unknown as Record<string, number>;
    applyDeclaredJointPositions({ initial_joint_positions_rad: ragged }, tree.joints, 1, (m) => { warnings.push(m); });
    expect(toolOf(tree).distanceTo(new Vector3(0.9, 0, 0.3))).toBeCloseTo(0, 4);
    expect(warnings).toEqual([]);
  });

  it("overrides only the joints now_joint_positions_rad names (Q75)", () => {
    expect(declaredJointPositions({
      initial_joint_positions_rad: { joint1: 1, joint2: 2 },
      now_joint_positions_rad: { joint2: 3 },
    })).toEqual([
      { name: "joint1", value: 1, source: "initial" },
      { name: "joint2", value: 3, source: "now" },
    ]);

    const tree = freshTree();
    applyDeclaredJointPositions(
      { initial_joint_positions_rad: { joint1: Math.PI / 2, joint2: 0.1 }, now_joint_positions_rad: { joint1: 0.3 } },
      tree.joints, 1, () => undefined,
    );
    const turn = (name: string): number => tree.joints.get(name)!.node.quaternion.angleTo(identity());
    expect(turn("joint1")).toBeCloseTo(0.3, 4);
    expect(turn("joint2")).toBeCloseTo(0.1, 4);
  });

  it("keeps the whole initial pose when now is empty or not a dictionary", () => {
    for (const now of [{}, undefined, null, "joint1", 42, []]) {
      const tree = freshTree();
      applyDeclaredJointPositions({ initial_joint_positions_rad: { joint1: Math.PI / 2 }, now_joint_positions_rad: now as unknown as Record<string, number> }, tree.joints, 1, () => undefined);
      expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(Math.PI / 2, 4);
    }
  });

  it("leaves a joint neither dictionary names exactly where it is", () => {
    const tree = freshTree();
    setJointValue(tree.joints.get("joint2")!, 0.7);
    applyDeclaredJointPositions({ initial_joint_positions_rad: { joint1: 0.2 }, now_joint_positions_rad: { joint1: 0.3 } }, tree.joints, 1, () => undefined);
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(0.3, 4);
    expect(tree.joints.get("joint2")!.node.quaternion.angleTo(identity())).toBeCloseTo(0.7, 4);
    expect(declaredJointPositions(undefined)).toEqual([]);
    expect(declaredJointPositions({ joint_sequence: ["joint1"] })).toEqual([]);
  });

  it("shares the limits and the mimic chain with the initial pose", () => {
    const tree = freshTree();
    const warnings: string[] = [];
    applyDeclaredJointPositions({ joint_limits: { joint1: [-1, 1] }, now_joint_positions_rad: { joint1: 2 } }, tree.joints, 1, (m) => { warnings.push(m); });
    expect(tree.joints.get("joint1")!.node.quaternion.angleTo(identity())).toBeCloseTo(1, 4);
    expect(warnings).toEqual(['joint "joint1" of robot 1 is at 2, outside [-1, 1]; it was clamped']);

    const arm = buildUrdfTree(parseUrdf(GRIPPER_URDF));
    arm.root.updateMatrixWorld(true);
    applyDeclaredJointPositions({ now_joint_positions_rad: { right_joint: 0.6 } }, arm.joints, 1, () => undefined);
    expect(turnOf(arm, "right_finger_joint")).toBeCloseTo(-0.55, 4);
  });

  it("names the dictionary a wrong joint name came from", () => {
    const warnings: string[] = [];
    applyDeclaredJointPositions({ now_joint_positions_rad: { no_such_joint: 0.4 } }, freshTree().joints, 3, (m) => { warnings.push(m); });
    expect(warnings).toEqual(['robot 3: now joint "no_such_joint" is not in its URDF']);
  });
});

describe("applyFrameJointPositions (§7.3, Q96)", () => {
  it("writes a frame into a second instance of the same URDF, clamped like the live arm", () => {
    const world = robotWorld({ joint1: [-1, 1] });
    const live = harness(world);
    const ghost = harness(world);
    const seen: string[] = [];

    applyFrameJointPositions(1, world.robotDriver(1)!, [2, 0.4, 0, 0, 0, 0, 0.72], ghost.tree.joints,
      (key, message) => { seen.push(`${key}: ${message}`); });

    // The out-of-range command is clamped exactly as §8.3 does it, and reported with the
    // key the live driver uses, so a caller can deduplicate against it.
    expect(turnOf(ghost.tree, "joint1")).toBeCloseTo(1, 4);
    expect(turnOf(ghost.tree, "joint2")).toBeCloseTo(0.4, 4);
    expect(seen).toHaveLength(1);
    expect(seen[0]!.startsWith("limit:1:joint1:")).toBe(true);
    // The two harnesses are separate expansions, so the arm on stage did not move.
    expect(turnOf(live.tree, "joint1")).toBe(0);
  });

  it("is the write the driver performs, so a ghost cannot preview an unreachable pose", () => {
    const world = robotWorld();
    const live = harness(world);
    const ghost = harness(world);
    const pose = [0.2, -0.7, 0.11, 0, 0, 0, 0];
    const traj = trajOf([objectPose(1, [segment(0, [pose])])]);

    live.driver.apply(traj, 0);
    applyFrameJointPositions(1, world.robotDriver(1)!, pose, ghost.tree.joints, () => undefined);

    expect(ghost.tool().distanceTo(live.tool())).toBeLessThan(1e-6);
  });
});
