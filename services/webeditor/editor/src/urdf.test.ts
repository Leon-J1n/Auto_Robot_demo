// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";
import { BoxGeometry, CylinderGeometry, Mesh, Object3D, Quaternion, SphereGeometry, Vector3 } from "three";
import type { Vec3 } from "../../contracts/types";
import { LibraryError } from "./library";
import { buildUrdfTree, endEffectorNode, parseUrdf, rpyToQuaternion, setJointValue, toolTipLinks, type MeshFactory, type UrdfRobot, type UrdfTree } from "./urdf";

const ROBOT = `<?xml version="1.0"?>
<robot name="arm">
  <material name="aluminium"><color rgba="0.6 0.65 0.7 1"/></material>
  <link name="base">
    <visual name="pad">
      <origin xyz="0 0 0.01" rpy="0 0 0"/>
      <geometry><box size="0.4 0.4 0.02"/></geometry>
      <material name="aluminium"/>
    </visual>
    <visual name="base_mesh">
      <geometry><mesh filename="package://arm/meshes/base.stl" scale="0.001 0.001 0.001"/></geometry>
    </visual>
  </link>
  <link name="shoulder">
    <visual>
      <geometry><cylinder radius="0.05" length="0.2"/></geometry>
      <material><color rgba="0.2 0.4 0.9 0.5"/></material>
    </visual>
  </link>
  <link name="wrist">
    <visual>
      <geometry><sphere radius="0.03"/></geometry>
    </visual>
  </link>
  <joint name="joint1" type="continuous">
    <parent link="base"/><child link="shoulder"/>
    <origin xyz="0 0 0.1" rpy="0 0 1.5707963267948966"/>
    <axis xyz="0 0 1"/>
  </joint>
  <joint name="joint2" type="revolute">
    <parent link="shoulder"/><child link="wrist"/>
    <origin xyz="0.2 0 0" rpy="0 0 0"/>
    <axis xyz="0 1 0"/>
    <limit lower="-1.5" upper="1.5" effort="30" velocity="2"/>
  </joint>
</robot>`;

/**
 * The mimic graph the real grippers ship with: a commanded driver joint, one
 * joint that copies it, and one that scales and shifts the copy.
 */
const MIRROR_URDF = `<?xml version="1.0"?>
<robot name="mirror">
  <link name="base"/><link name="right"/><link name="left"/><link name="tip"/>
  <joint name="right_joint" type="revolute">
    <parent link="base"/><child link="right"/>
    <origin xyz="0 0 0.1"/><axis xyz="0 0 1"/>
  </joint>
  <joint name="left_joint" type="revolute">
    <parent link="base"/><child link="left"/>
    <origin xyz="0 0 0.1" rpy="0 0 3.14159265"/><axis xyz="0 0 1"/>
    <mimic joint="right_joint"/>
  </joint>
  <joint name="tip_joint" type="revolute">
    <parent link="left"/><child link="tip"/>
    <origin xyz="0.2 0 0"/><axis xyz="0 0 1"/>
    <mimic joint="left_joint" multiplier="-1" offset="0.05"/>
  </joint>
</robot>`;

function robot(source = ROBOT) {
  return parseUrdf(source);
}

function expand(source = ROBOT, meshes: MeshFactory = () => null) {
  const tree = buildUrdfTree(robot(source), meshes);
  tree.root.updateMatrixWorld(true);
  return tree;
}

describe("parseUrdf", () => {
  it("reads links, joints, roots and the materials a visual refers to", () => {
    const value = robot();
    expect(value.name).toBe("arm");
    expect(value.links.map((link) => link.name)).toEqual(["base", "shoulder", "wrist"]);
    expect(value.roots).toEqual(["base"]);
    expect(value.joints.map((joint) => [joint.name, joint.type, joint.parent, joint.child])).toEqual([
      ["joint1", "continuous", "base", "shoulder"],
      ["joint2", "revolute", "shoulder", "wrist"],
    ]);
    expect(value.joints[0]!.axis).toEqual([0, 0, 1]);
    expect(value.joints[0]!.limit).toBeNull();
    expect(value.joints[1]!.limit).toEqual([-1.5, 1.5]);
    // the named robot-level material is resolved for the pad, the inline one for the cylinder
    expect(value.links[0]!.visuals[0]!.rgba).toEqual([0.6, 0.65, 0.7, 1]);
    expect(value.links[1]!.visuals[0]!.rgba).toEqual([0.2, 0.4, 0.9, 0.5]);
    expect(value.links[2]!.visuals[0]!.rgba).toEqual([0.8, 0.8, 0.8, 1]);
  });

  it("rewrites package:// mesh filenames into the model directory", () => {
    expect(robot().meshFiles).toEqual(["meshes/base.stl"]);
    expect(robot().links[0]!.visuals[1]!.geometry).toEqual({
      kind: "mesh", filename: "meshes/base.stl", scale: [0.001, 0.001, 0.001],
    });
  });

  it("collects every distinct mesh file in document order", () => {
    const value = robot(`<robot name="r">
  <link name="a"><visual><geometry><mesh filename="m1.stl"/></geometry></visual>
    <visual><geometry><mesh filename="m2.stl"/></geometry></visual></link>
  <link name="b"><visual><geometry><mesh filename="m1.stl"/></geometry></visual></link>
</robot>`);
    expect(value.meshFiles).toEqual(["m1.stl", "m2.stl"]);
    expect(value.roots).toEqual(["a", "b"]);
  });

  it("refuses mesh filenames that would leave the model directory", () => {
    for (const filename of ["../escape.stl", "/etc/passwd", "http://evil/x.stl", "C:/arm/x.stl"]) {
      const source = `<robot name="r"><link name="a"><visual><geometry><mesh filename="${filename}"/></geometry></visual></link></robot>`;
      try {
        parseUrdf(source);
        expect.fail(`${filename} should not load`);
      } catch (error) {
        expect(error).toBeInstanceOf(LibraryError);
        expect((error as LibraryError).code).toBe("DEPENDENCY_FETCH_FAILED");
        expect((error as LibraryError).status).toBe(424);
        expect((error as LibraryError).failures).toEqual([filename]);
      }
    }
  });

  it("reads <mimic> with the defaults the URDF spec gives it", () => {
    const value = robot(MIRROR_URDF);
    expect(value.joints.map((joint) => joint.mimic)).toEqual([
      null,
      { joint: "right_joint", multiplier: 1, offset: 0 },
      { joint: "left_joint", multiplier: -1, offset: 0.05 },
    ]);
  });

  it("rejects malformed documents and broken hierarchies", () => {
    const cases: Array<[string, RegExp]> = [
      ["<notarobot/>", /root must be <robot>/],
      ["<robot name='r'></robot>", /at least one link/],
      ["<robot><link name='a'/><link name='a'/></robot>", /duplicate link name/],
      ["<robot><link name='a'/><joint name='j' type='wobbly'><parent link='a'/><child link='a'/></joint></robot>", /unknown type/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='fixed'><child link='b'/></joint></robot>", /parent link/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='fixed'><parent link='a'/><child link='missing'/></joint></robot>", /child link/],
      ["<robot><link name='a'/><link name='b'/><joint name='j1' type='fixed'><parent link='a'/><child link='b'/></joint><joint name='j2' type='fixed'><parent link='a'/><child link='b'/></joint></robot>", /more than one parent/],
      ["<robot><link name='a'/><link name='b'/><joint name='j1' type='fixed'><parent link='a'/><child link='b'/></joint><joint name='j2' type='fixed'><parent link='b'/><child link='a'/></joint></robot>", /own ancestor/],
      ["<robot><link name='a'><visual><geometry/></visual></link></robot>", /exactly one geometry/],
      ["<robot><link name='a'><visual><geometry><capsule radius='1'/></geometry></visual></link></robot>", /unsupported geometry/],
      ["<robot><link name='a'><visual><geometry><box size='1 0 1'/></geometry></visual></link></robot>", /greater than zero/],
      ["<robot><link name='a'><visual><geometry><mesh filename=''/></geometry></visual></link></robot>", /no filename/],
      ["<robot><link name='a'><visual><geometry><mesh filename='m.stl' scale='0 1 1'/></geometry></visual></link></robot>", /scale must not be zero/],
      ["<robot><link name='a'/><joint name='a'/></robot>", /joint a has unknown type/],
      ["<robot><link name='a'><visual><origin xyz='0 0'/></visual></link></robot>", /needs 3 numbers/],
      ["<robot><link name='a'><visual><origin rpy='x y z'/></visual></link></robot>", /non-numeric/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='revolute'><parent link='a'/><child link='b'/><limit lower='2' upper='1'/></joint></robot>", /above upper/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='revolute'><parent link='a'/><child link='b'/><mimic/></joint></robot>", /<mimic> without a joint attribute/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='revolute'><parent link='a'/><child link='b'/><mimic joint='nope'/></joint></robot>", /mimics "nope", which is not defined/],
      ["<robot><link name='a'/><link name='b'/><link name='c'/><joint name='bolt' type='fixed'><parent link='a'/><child link='b'/></joint><joint name='j' type='revolute'><parent link='a'/><child link='c'/><mimic joint='bolt'/></joint></robot>", /cannot hold a value/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='revolute'><parent link='a'/><child link='b'/><mimic joint='j'/></joint></robot>", /cyclic <mimic> chain/],
      ["<robot><link name='a'/><link name='b'/><link name='c'/><joint name='j1' type='revolute'><parent link='a'/><child link='b'/><mimic joint='j2'/></joint><joint name='j2' type='revolute'><parent link='a'/><child link='c'/><mimic joint='j1'/></joint></robot>", /cyclic <mimic> chain/],
      ["<robot><link name='a'/><link name='b'/><joint name='j' type='revolute'><parent link='a'/><child link='b'/><mimic joint='j' multiplier='x'/></joint></robot>", /mimic multiplier is not numeric/],
      ["<robot><link name='a'/></robot", /well formed/],
    ];
    for (const [source, message] of cases) {
      try {
        parseUrdf(source);
        expect.fail(`should have rejected ${source.slice(0, 40)}`);
      } catch (error) {
        expect((error as Error).message).toMatch(message);
        // §6.8: a bad URDF is reported as one diagnostic by `renderer.ts:loadModel`, so
        // the code and status are all a reader has to go on -- they must not drift.
        expect(error).toBeInstanceOf(LibraryError);
        expect(error).toMatchObject({ code: "INVALID_URDF", status: 400 });
      }
    }
  });
});

describe("rpyToQuaternion", () => {
  it("emits xyzw in the order the world files use (§2.8)", () => {
    expect(rpyToQuaternion([0, 0, 0])).toEqual([0, 0, 0, 1]);
    const quarter = Math.SQRT1_2;
    for (const [rpy, expected] of [
      [[Math.PI / 2, 0, 0], [quarter, 0, 0, quarter]],
      [[0, Math.PI / 2, 0], [0, quarter, 0, quarter]],
      [[0, 0, Math.PI / 2], [0, 0, quarter, quarter]],
    ] as Array<[readonly number[], readonly number[]]>) {
      rpyToQuaternion(rpy as Vec3).forEach((value, index) => expect(value).toBeCloseTo(expected[index]!, 12));
    }
  });

  it("applies roll-pitch-yaw as fixed-axis rotations, yaw last", () => {
    const yawed = new Vector3(1, 0, 0).applyQuaternion(new Quaternion().fromArray(rpyToQuaternion([0, 0, Math.PI / 2])));
    expect(yawed.x).toBeCloseTo(0, 5);
    expect(yawed.y).toBeCloseTo(1, 5);
    const pitched = new Vector3(0, 0, 1).applyQuaternion(new Quaternion().fromArray(rpyToQuaternion([0, Math.PI / 2, 0])));
    expect(pitched.x).toBeCloseTo(1, 5);
    // Rz(yaw) * Ry(pitch) * Rx(roll) on (0,0,1): only the ZYX reading gives -Y.
    const compound = new Vector3(0, 0, 1).applyQuaternion(new Quaternion().fromArray(rpyToQuaternion([Math.PI / 2, Math.PI / 2, 0])));
    expect(compound.x).toBeCloseTo(0, 5);
    expect(compound.y).toBeCloseTo(-1, 5);
    expect(compound.z).toBeCloseTo(0, 5);
  });
});

describe("buildUrdfTree", () => {
  it("nests link nodes under their joint nodes and visuals under links", () => {
    const tree = expand();
    expect(tree.root.name).toBe("arm");
    expect([...tree.links.keys()]).toEqual(["base", "shoulder", "wrist"]);
    expect([...tree.joints.keys()]).toEqual(["joint1", "joint2"]);
    expect(tree.joints.get("joint1")!.node.parent).toBe(tree.links.get("base"));
    expect(tree.links.get("shoulder")!.parent).toBe(tree.joints.get("joint1")!.node);
    expect(tree.links.get("base")!.parent).toBe(tree.root);
    expect(tree.links.get("base")!.children.map((node) => node.name)).toEqual(["pad", "base_mesh", "joint1"]);
  });

  it("composes joint origins along the chain at the zero pose", () => {
    const tree = expand();
    const wrist = tree.links.get("wrist")!.getWorldPosition(new Vector3());
    // base -> joint1 (0 0 0.1, yaw 90deg) -> shoulder -> joint2 (0.2 0 0)
    expect(wrist.x).toBeCloseTo(0, 5);
    expect(wrist.y).toBeCloseTo(0.2, 5);
    expect(wrist.z).toBeCloseTo(0.1, 5);
    const shoulderRotation = tree.joints.get("joint1")!.node.getWorldQuaternion(new Quaternion());
    expect(shoulderRotation.w).toBeCloseTo(Math.SQRT1_2, 5);
  });

  it("builds primitive geometry with the URDF axis convention", () => {
    const tree = expand();
    const pad = tree.links.get("base")!.children.find((node) => node.name === "pad")!;
    const boxMesh = pad.children[0] as Mesh;
    expect(boxMesh.geometry).toBeInstanceOf(BoxGeometry);
    const boxSize = (boxMesh.geometry as BoxGeometry).parameters;
    expect([boxSize.width, boxSize.height, boxSize.depth]).toEqual([0.4, 0.4, 0.02]);
    expect(pad.position.toArray()).toEqual([0, 0, 0.01]);

    const cylinderMesh = (tree.links.get("shoulder")!.children[0] as Mesh).children[0] as Mesh;
    expect(cylinderMesh.geometry).toBeInstanceOf(CylinderGeometry);
    // A URDF cylinder is Z-up, so the mesh must be rotated to Three's Y-up axis.
    cylinderMesh.geometry.computeBoundingBox();
    const bounds = cylinderMesh.geometry.boundingBox!;
    expect(bounds.min.z).toBeCloseTo(-0.1, 5);
    expect(bounds.max.z).toBeCloseTo(0.1, 5);

    const sphereMesh = (tree.links.get("wrist")!.children[0] as Mesh).children[0] as Mesh;
    expect(sphereMesh.geometry).toBeInstanceOf(SphereGeometry);
    expect((sphereMesh.geometry as SphereGeometry).parameters.radius).toBe(0.03);
  });

  it("applies the mesh scale and keeps the visual empty when the asset is unavailable", () => {
    const loaded = new Object3D();
    const tree = expand(ROBOT, (filename) => (filename === "meshes/base.stl" ? loaded : null));
    const holder = tree.links.get("base")!.children.find((node) => node.name === "base_mesh")!;
    expect(holder.children).toEqual([loaded]);
    expect(loaded.scale.toArray()).toEqual([0.001, 0.001, 0.001]);
    expect(tree.links.get("base")!.children.some((node) => node instanceof Mesh)).toBe(false);
  });

  it("asks the mesh factory only for mesh visuals", () => {
    const asked: string[] = [];
    expand(ROBOT, (filename) => {
      asked.push(filename);
      return null;
    });
    expect(asked).toEqual(["meshes/base.stl"]);
  });

  it("keeps a fixed joint as a transform node without a handle", () => {
    const tree = expand(`<robot name="f">
  <link name="a"/><link name="b"/>
  <joint name="bolt" type="fixed"><parent link="a"/><child link="b"/><origin xyz="1 0 0"/></joint>
</robot>`);
    expect(tree.joints.get("bolt")).toBeUndefined();
    expect(tree.links.get("b")!.parent!.name).toBe("bolt");
    expect(tree.links.get("b")!.getWorldPosition(new Vector3()).toArray()).toEqual([1, 0, 0]);
  });

  it("puts the mimic relation on the handle the driver writes through", () => {
    const tree = expand(MIRROR_URDF);
    expect(tree.joints.get("right_joint")!.mimic).toBeNull();
    expect(tree.joints.get("left_joint")!.mimic).toEqual({ joint: "right_joint", multiplier: 1, offset: 0 });
    expect(tree.joints.get("tip_joint")!.mimic).toEqual({ joint: "left_joint", multiplier: -1, offset: 0.05 });
  });

  it("hangs every root link directly under the robot node", () => {
    const tree = expand(`<robot name="two">
  <link name="a"><visual><geometry><box size="1 1 1"/></geometry></visual></link>
  <link name="b"/>
</robot>`);
    expect(tree.root.children.map((node) => node.name)).toEqual(["a", "b"]);
    expect(tree.root.children[0]!.children).toHaveLength(1);
  });
});

/**
 * Two revolute joints, one prismatic whose axis its own origin turns, and one
 * joint with an unusable axis - enough to show where a joint value lands (§8.4).
 */
const SETTING = `<?xml version="1.0"?>
<robot name="set">
  <link name="base"/><link name="upper"/><link name="wrist"/><link name="carriage"/><link name="tip"/>
  <joint name="yaw" type="revolute">
    <parent link="base"/><child link="upper"/>
    <origin xyz="0 0 0.2" rpy="0 0 0"/><axis xyz="0 0 1"/>
    <limit lower="-1" upper="1" effort="10" velocity="1"/>
  </joint>
  <joint name="pitch" type="revolute">
    <parent link="upper"/><child link="wrist"/>
    <origin xyz="0.3 0 0" rpy="0 0 0"/><axis xyz="0 1 0"/>
  </joint>
  <joint name="slide" type="prismatic">
    <parent link="wrist"/><child link="carriage"/>
    <origin xyz="0.1 0 0" rpy="0 0 1.5707963267948966"/><axis xyz="2 0 0"/>
    <limit lower="0" upper="0.1" effort="10" velocity="1"/>
  </joint>
  <joint name="dead" type="revolute">
    <parent link="wrist"/><child link="tip"/>
    <origin xyz="0 0.1 0" rpy="0 0 0"/><axis xyz="0 0 0"/>
  </joint>
</robot>`;

function positionOf(tree: UrdfTree, link: string): Vector3 {
  tree.root.updateMatrixWorld(true);
  return tree.links.get(link)!.getWorldPosition(new Vector3());
}

function expectPosition(actual: Vector3, [x, y, z]: [number, number, number]): void {
  expect(actual.x).toBeCloseTo(x, 5);
  expect(actual.y).toBeCloseTo(y, 5);
  expect(actual.z).toBeCloseTo(z, 5);
}

describe("setJointValue (§8.4)", () => {
  it("turns a revolute joint about its axis on top of the origin and moves everything downstream", () => {
    const tree = expand(SETTING);
    expectPosition(positionOf(tree, "carriage"), [0.4, 0, 0.2]);

    setJointValue(tree.joints.get("yaw")!, Math.PI / 2);
    // The 0.4 m reach swings about Z through (0, 0, 0.2); a revolute joint never translates.
    expectPosition(positionOf(tree, "carriage"), [0, 0.4, 0.2]);
    expect(tree.joints.get("yaw")!.node.position.toArray()).toEqual([0, 0, 0.2]);
  });

  it("restores the expanded pose for a zero value instead of wiping the joint origin", () => {
    const tree = expand(SETTING);
    const pitch = tree.joints.get("pitch")!;

    setJointValue(pitch, 0.6);
    setJointValue(pitch, 0);
    expect(pitch.node.quaternion.toArray()).toEqual([0, 0, 0, 1]);
    expect(pitch.node.position.toArray()).toEqual([0.3, 0, 0]);
    expectPosition(positionOf(tree, "carriage"), [0.4, 0, 0.2]);
  });

  it("slides a prismatic joint along the axis its own origin has turned", () => {
    const tree = expand(SETTING);
    const slide = tree.joints.get("slide")!;

    setJointValue(slide, 0.05);
    // The axis is written unnormalised (2 0 0) and the origin yaws 90 deg, so the
    // travel direction is +Y and the distance is 0.05 m: neither 0.1 nor along +X.
    expectPosition(positionOf(tree, "carriage"), [0.4, 0.05, 0.2]);
    expect(slide.node.position.x).toBeCloseTo(0.1, 5);
    expect(slide.node.position.y).toBeCloseTo(0.05, 5);
    expect(slide.node.position.z).toBeCloseTo(0, 5);
    expect(slide.node.quaternion.z).toBeCloseTo(Math.SQRT1_2, 5);
    expect(slide.node.quaternion.w).toBeCloseTo(Math.SQRT1_2, 5);
  });

  it("drives a continuous joint like a revolute one", () => {
    const tree = expand();
    setJointValue(tree.joints.get("joint1")!, Math.PI);
    // base -> joint1 (0 0 0.1, yaw 90 deg): half a turn lands the wrist on -Y.
    expectPosition(positionOf(tree, "wrist"), [0, -0.2, 0.1]);
  });

  it("leaves a joint with a zero-length axis at its origin rather than guessing a direction", () => {
    const tree = expand(SETTING);
    const dead = tree.joints.get("dead")!;

    setJointValue(dead, 1);
    expect(dead.node.quaternion.toArray()).toEqual([0, 0, 0, 1]);
    expectPosition(positionOf(tree, "tip"), [0.3, 0.1, 0.2]);
  });

  it("reads a value that is not a finite number as zero", () => {
    const tree = expand(SETTING);
    const yaw = tree.joints.get("yaw")!;

    setJointValue(yaw, Math.PI / 4);
    expect(positionOf(tree, "carriage").y).toBeGreaterThan(0.25);
    setJointValue(yaw, Number.NaN);
    expectPosition(positionOf(tree, "carriage"), [0.4, 0, 0.2]);
    setJointValue(yaw, Number.POSITIVE_INFINITY);
    expectPosition(positionOf(tree, "carriage"), [0.4, 0, 0.2]);
  });
});

describe("endEffectorNode (§8.6)", () => {
  it("returns the child link of the last joint the sequence names", () => {
    const tree = expand(SETTING);
    expect(endEffectorNode(robot(SETTING), tree, ["yaw", "pitch", "slide"])).toBe(tree.links.get("carriage"));
  });

  it("skips a name the tree has no handle for and still finds the last real joint", () => {
    const tree = expand(SETTING);
    expect(endEffectorNode(robot(SETTING), tree, ["yaw", "ghost", "pitch"])).toBe(tree.links.get("wrist"));
  });

  it("falls back to the deepest leaf when the sequence resolves to nothing", () => {
    const tree = expand(SETTING);
    // tip is a leaf too, but shallower: carriage is three joints from the root.
    expect(endEffectorNode(robot(SETTING), tree, [])).toBe(tree.links.get("carriage"));
    expect(endEffectorNode(robot(SETTING), tree, ["ghost", "dead"])).toBe(tree.links.get("tip"));
  });

  it("names the only link of a robot that has no joints", () => {
    const source = '<robot name="box"><link name="only"/></robot>';
    const tree = expand(source);
    expect(endEffectorNode(robot(source), tree, [])).toBe(tree.links.get("only"));
  });
});

/**
 * The shape every gripper in OLS ships (§7.3, Q102 measured it): two symmetric finger
 * pads, one per driver side, at the same depth.
 */
const GRIPPER_URDF = `<?xml version="1.0"?>
<robot name="gripper">
  <link name="base"/><link name="right_driver"/><link name="left_driver"/>
  <link name="right_silicone_pad"/><link name="left_silicone_pad"/>
  <joint name="base_right" type="fixed"><parent link="base"/><child link="right_driver"/></joint>
  <joint name="base_left" type="fixed"><parent link="base"/><child link="left_driver"/></joint>
  <joint name="right_pad_joint" type="revolute"><parent link="right_driver"/><child link="right_silicone_pad"/><axis xyz="0 1 0"/></joint>
  <joint name="left_pad_joint" type="revolute"><parent link="left_driver"/><child link="left_silicone_pad"/><axis xyz="0 1 0"/></joint>
</robot>`;

describe("toolTipLinks (§7.3, Q102)", () => {
  it("returns the one deepest leaf of an arm", () => {
    // base -> shoulder -> wrist: the法兰/tool frame the waypoints follow.
    expect(toolTipLinks(robot())).toEqual(["wrist"]);
    expect(toolTipLinks(robot(MIRROR_URDF))).toEqual(["tip"]);
  });

  it("returns both finger pads of a symmetric gripper, in document order", () => {
    expect(toolTipLinks(robot(GRIPPER_URDF))).toEqual(["right_silicone_pad", "left_silicone_pad"]);
    // Their mean is the grasp centre, so the set has to hold both: "the last link" would
    // be whichever one the file happened to declare first.
    const swapped = robot(GRIPPER_URDF.replace(
      '<link name="right_silicone_pad"/><link name="left_silicone_pad"/>',
      '<link name="left_silicone_pad"/><link name="right_silicone_pad"/>',
    ));
    expect(toolTipLinks(swapped)).toEqual(["left_silicone_pad", "right_silicone_pad"]);
  });

  it("drops a leaf that is shallower than the deepest one", () => {
    const source = `<robot name="d"><link name="base"/><link name="stub"/><link name="a"/><link name="b"/>
  <joint name="j0" type="fixed"><parent link="base"/><child link="stub"/></joint>
  <joint name="j1" type="fixed"><parent link="base"/><child link="a"/></joint>
  <joint name="j2" type="fixed"><parent link="a"/><child link="b"/></joint>
</robot>`;
    expect(toolTipLinks(robot(source))).toEqual(["b"]);
  });

  it("walks a chain of nothing but fixed joints", () => {
    // A model with no movable joint still has an end; the driver's absence is not the
    // absence of a tooltip (O1: a gripper can be mounted as a fixed chain).
    const source = `<robot name="f"><link name="a"/><link name="b"/><link name="c"/>
  <joint name="f1" type="fixed"><parent link="a"/><child link="b"/></joint>
  <joint name="f2" type="fixed"><parent link="b"/><child link="c"/></joint>
</robot>`;
    expect(toolTipLinks(robot(source))).toEqual(["c"]);
  });

  it("falls back to every root when nothing hangs below it", () => {
    expect(toolTipLinks(robot('<robot name="box"><link name="only"/></robot>'))).toEqual(["only"]);
    // Two roots and no joints are two leaves at the same depth, so both are kept and
    // the caller averages them - a degenerate model, but not a crash.
    expect(toolTipLinks(robot('<robot name="two"><link name="a"/><link name="b"/></robot>'))).toEqual(["a", "b"]);
  });

  it("reads only the joint graph, so simulator and mimic tags cannot move it", () => {
    // §7.3 Q102: `<gazebo>` names a reference frame and `<mimic>` says how a joint is
    // coupled; neither is a link, and neither is consulted here.
    const withExtensions = ROBOT.replace("</robot>", `
  <gazebo reference="wrist"><material>Gazebo/Blue</material></gazebo>
  <gazebo><mechanicalTransmission type="robotiq_transmission"/></gazebo>
</robot>`);
    expect(toolTipLinks(robot(withExtensions))).toEqual(["wrist"]);
    // MIRROR's `tip` hangs off a mimicked joint, and the answer is still the structural leaf.
    expect(toolTipLinks(robot(MIRROR_URDF))).toEqual(["tip"]);
    expect(robot(MIRROR_URDF).joints[1]!.mimic).not.toBeNull();
  });

  it("keeps a repeated link name to one entry instead of throwing", () => {
    // `parseUrdf` rejects a duplicate link name outright (see the cases above), so the
    // only way to hand this function a repeated name is data built somewhere else.
    const doubled = { links: [{ name: "tip" }, { name: "tip" }], joints: [] } as unknown as UrdfRobot;
    expect(toolTipLinks(doubled)).toEqual(["tip"]);
    const repeatedParent = {
      links: [{ name: "a" }, { name: "b" }, { name: "a" }],
      joints: [{ parent: "a", child: "b" }],
    } as unknown as UrdfRobot;
    expect(toolTipLinks(repeatedParent)).toEqual(["b"]);
    expect(toolTipLinks({ links: [], joints: [] } as unknown as UrdfRobot)).toEqual([]);
  });
});
