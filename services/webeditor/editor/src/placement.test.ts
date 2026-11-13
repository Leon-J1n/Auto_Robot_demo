// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { Vec3, WorldComponent, WorldFile } from "../../contracts/types";
import { parseModelMetadata, type Aabb6, type LibraryFileKind, type ModelMetadata } from "./library";
import {
  BASELINE_SCENE_ID,
  armFamilyIds,
  armOrigin,
  findArmObjects,
  findGripperObjects,
  findTableObject,
  localBox,
  orientedBox,
  overlaps,
  pedestalPose,
  placeAsset,
  readAsset,
  placementQuaternion,
  propComponents,
  armComponents,
  rewriteComponents,
  spotOnTable,
  TABLE_YAW,
  type AssetSource,
  type BoundsMeasure,
  type Box,
} from "./placement";
import { parseWorld, type World } from "./world";

/**
 * Every number below is the real OLS document for that asset, read out of the library
 * server's data directory on 2026-10-31: the table is model 29, the arm robot 1, the
 * pedestal robot 4. The scene is the re-exported template the editor already ships.
 */
const TABLE_AABB: Aabb6 = [0.75, -0.75, 0.375, -0.375, 0.6, 0];
const APPLE_AABB: Aabb6 = [0.037544876, -0.037110556, 0.037398798, -0.037333214, 0.033874024, -0.037373223];
const ARM_AABB: Aabb6 = [0.856347, -0.075499, 0.232935, -0.075525, 0.234524, 0];
const PIPER_AABB: Aabb6 = [0.057652737, -0.313841019, 0.048500003, -0.048500018, 0.241500665, 0];
/** robot 15: the one shipped arm whose box reaches 0.92 m below its own origin. */
const BAXTER_AABB: Aabb6 = [0.83884519, -0.5019719, 1.033837856, -1.033848003, 0.875249982, -0.924180031];
const PEDESTAL_AABB: Aabb6 = [0.1, -0.1, 0.1, -0.1, 0.25, -0.25];
/**
 * model 23, the mug, exactly as OLS ships it: the AABB below declares X wide and Y narrow,
 * but the mesh it points at is the other way round (see MUG_OBJ). 9 of the 25 top-level OBJ
 * models carry such a frame mismatch (X/Y swapped for most, the up axis for two); the mug is
 * the one the user reproduced the 穿模 with (Q144).
 */
const MUG_AABB: Aabb6 = [0.065113, -0.065627, 0.046532, -0.04999, 0.114619, -5e-06];
/** `scene_one_arm[0]`: the triple every shipped arm publishes. */
const PERCENT: Vec3 = [0.2, 0.3, 0.5];

const UR5E_DRIVER = {
  role: "arm",
  joint_sequence: ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint", "wrist_1_joint", "wrist_2_joint", "wrist_3_joint"],
  available_connect_joints: ["wrist_3_joint"],
  available_connect_link: ["wrist_3_link"],
  // The one pose the arm hands out, written in `wrist_3_link`'s own frame.
  position: [[2.679489213841535e-09, 0.0999999837652335, 9.719897775539721e-09]],
  quaternion_xyzw: [[-0.7071067556514947, 9.473426349705847e-09, -9.47342713345485e-09, 0.7071068067215996]],
  robot_attachment: { properties: { child_link: [] } },
};

const PIPER_DRIVER = {
  role: "arm",
  joint_sequence: ["joint1", "joint6"],
  available_connect_joints: ["joint6"],
  available_connect_link: ["link6"],
  position: [[0, 0, 0]],
  quaternion_xyzw: [[0, 0, 0, 1]],
  robot_attachment: { properties: { child_link: [] } },
};

/** The asset-side `physical_properties` exactly as OLS publishes it (Q165), `scale` included. */
function physical(isStatic: boolean, mass: number, values: [number, number, number], restitution: number, scale: [number, number] = [1, 1]): Record<string, unknown> {
  return {
    is_static: isStatic,
    mass_kg: mass,
    friction: { format: "[sliding, torsional, rolling]", values: [...values] },
    restitution,
    scale: [...scale],
  };
}

const DOCUMENTS: Record<string, Record<string, unknown>> = {
  "model:29": {
    path: "table.urdf",
    semantic_properties: { object_name: "table", object_id: 29 },
    geometric_properties: { AABB: TABLE_AABB },
    physical_properties: physical(true, 22.0, [0.8, 0.01, 0.001], 0.05, [0.8, 2.0]),
    robot_driver: { properties: { role: "passive", joint_sequence: [], joint_limits: {}, available_connect_joints: [], available_connect_link: [], position: [], quaternion_xyzw: [] } },
  },
  "model:1": {
    path: "apple.obj",
    semantic_properties: { object_name: "apple", object_id: 1 },
    geometric_properties: { AABB: APPLE_AABB },
    physical_properties: physical(false, 0.068, [0.8, 0.005, 0.0005], 0.05),
  },
  "model:23": {
    // The mug: an OBJ whose metadata AABB is transposed against the mesh it names (Q144).
    path: "mug.obj",
    semantic_properties: { object_name: "mug", object_id: 23 },
    geometric_properties: { AABB: MUG_AABB },
  },
  "model:25": {
    // A URDF-bearing 模型: articulated, and still a prop (§6.11 id spaces).
    path: "drawer.urdf",
    semantic_properties: { object_name: "drawer", object_id: 25 },
    geometric_properties: { AABB: [0.2, -0.2, 0.2, -0.2, 0.3, 0] },
    physical_properties: physical(true, 2.5, [0.7, 0.01, 0.001], 0.05, [1.0, 2.5]),
  },
  "model:33": { path: "button.obj", semantic_properties: { object_name: "button", object_id: 33 } },
  "robot:1": {
    path: "ur5e_arm.urdf",
    semantic_properties: { object_name: "ur5e_arm", object_id: 1 },
    geometric_properties: { AABB: ARM_AABB },
    physical_properties: physical(false, 20.9949, [1.0, 0.005, 0.0001], 0.05),
    scene_one_arm: [{ robot_pose_percent_compared_to_table_length_center: PERCENT, has_pedestal: true, pedestal_id: 4, pedestal_scale: [1, 1, 1] }],
    robot_driver: { properties: UR5E_DRIVER },
  },
  "robot:2": {
    path: "robotiq_2f85.urdf",
    semantic_properties: { object_name: "robotiq_2f85", object_id: 2 },
    geometric_properties: { AABB: [0.07421503, -0.07421503, 0.0375, -0.0375, 0.16022, -3e-09] },
    physical_properties: physical(false, 0.901, [0.7, 0.01, 0.001], 0.05),
    robot_driver: {
      properties: {
        role: "arm",
        joint_sequence: ["robotiq_2f85_right_driver_joint"],
        available_connect_joints: [],
        available_connect_link: [],
        position: [],
        quaternion_xyzw: [],
        robot_attachment: { properties: { child_link: ["robotiq_2f85_mount"] } },
      },
    },
  },
  "robot:3": {
    path: "d435i.obj",
    semantic_properties: { object_name: "d435i", object_id: 3, tag: "D435i相机,深度相机,双目,RealSense" },
    geometric_properties: { AABB: [0.045, -0.044914, 0.0125, -0.0125, 0, -0.025055] },
    physical_properties: physical(false, 0.055, [0.55, 0.005, 0.0005], 0.12),
  },
  "robot:4": {
    path: "robot_pedestal.obj",
    semantic_properties: { object_name: "robot_pedestal", object_id: 4 },
    geometric_properties: { AABB: PEDESTAL_AABB },
    physical_properties: physical(true, 8.0, [0.8, 0.01, 0.001], 0.05),
  },
  "robot:5": {
    // ur3, with the one field every shipped arm does fill left out, to pin the default.
    path: "ur3.urdf",
    semantic_properties: { object_name: "ur3", object_id: 5 },
    geometric_properties: { AABB: ARM_AABB },
    physical_properties: physical(false, 18.4, [1.0, 0.005, 0.0001], 0.05),
    scene_one_arm: [{ robot_pose_percent_compared_to_table_length_center: PERCENT, has_pedestal: true, pedestal_id: 4 }],
    robot_driver: { properties: UR5E_DRIVER },
  },
  "robot:8": {
    path: "piper.urdf",
    semantic_properties: { object_name: "piper", object_id: 8 },
    geometric_properties: { AABB: PIPER_AABB },
    scene_one_arm: [{ robot_pose_percent_compared_to_table_length_center: PERCENT, has_pedestal: true, pedestal_id: 4, pedestal_scale: [1, 1, 1] }],
    robot_driver: { properties: PIPER_DRIVER },
  },
  "robot:9": {
    path: "nero.urdf",
    semantic_properties: { object_name: "nero", object_id: 9 },
    geometric_properties: { AABB: ARM_AABB },
    // The arm that needs no furniture under it.
    scene_one_arm: [{ robot_pose_percent_compared_to_table_length_center: PERCENT, has_pedestal: false }],
    robot_driver: { properties: { ...PIPER_DRIVER, available_connect_joints: ["joint7"], available_connect_link: ["link7"] } },
  },
  "robot:11": {
    path: "d455.obj",
    semantic_properties: { object_name: "d455", object_id: 11, tag: "D455相机,深度相机,双目,RealSense" },
    geometric_properties: { AABB: [0.06199725, -0.06199725, 0.01449953, -0.0145, 0, -0.026] },
    physical_properties: physical(false, 0.072, [0.8, 0.01, 0.001], 0.08),
  },
  "robot:13": {
    // An arm that publishes no `scene_one_arm` at all.
    path: "pr2.urdf",
    semantic_properties: { object_name: "pr2", object_id: 13 },
    geometric_properties: { AABB: ARM_AABB },
    robot_driver: { properties: { ...UR5E_DRIVER, available_connect_joints: ["r_wrist_roll_joint"], available_connect_link: ["r_wrist_roll_link"] } },
  },
  "robot:24": {
    path: "arm.urdf",
    semantic_properties: { object_name: "support_test_arm", object_id: 24 },
    geometric_properties: { AABB: [1, -1, 1, -1, 1, -0.1] },
    scene_one_arm: [{ robot_pose_percent_compared_to_table_length_center: PERCENT, has_pedestal: true, pedestal_id: 4 }],
    robot_driver: { properties: PIPER_DRIVER },
  },
  "robot:16": {
    // An arm whose metadata names no driver, so the tree has to answer for it.
    path: "scara_4dof.urdf",
    semantic_properties: { object_name: "scara_4dof", object_id: 16 },
    geometric_properties: { AABB: ARM_AABB },
    scene_one_arm: [{ robot_pose_percent_compared_to_table_length_center: PERCENT, has_pedestal: true, pedestal_id: 4, pedestal_scale: [1, 1, 1] }],
  },
};

/** A tree of its own: root, one fixed joint, one movable joint, one leaf. */
const BARE_URDF = `<?xml version="1.0"?>
<robot name="bare">
  <link name="base_mount"/>
  <link name="body_link"/>
  <link name="tip_link"/>
  <joint name="base_joint" type="fixed"><parent link="base_mount"/><child link="body_link"/></joint>
  <joint name="arm_joint" type="revolute"><parent link="body_link"/><child link="tip_link"/><limit lower="-1" upper="1"/></joint>
</robot>`;

const SUPPORT_URDF = `<robot name="support-test">
  <link name="base_link"/>
  <link name="base_mount"><visual><geometry><mesh filename="base.obj"/></geometry></visual></link>
  <link name="arm_link"><visual><geometry><mesh filename="arm.obj"/></geometry></visual></link>
  <joint name="base_fixed" type="fixed"><parent link="base_link"/><child link="base_mount"/></joint>
  <joint name="arm_joint" type="revolute"><parent link="base_mount"/><child link="arm_link"/><origin xyz="0 0 0.1"/><limit lower="-1" upper="1"/></joint>
</robot>`;
const BASE_OBJ = "v -0.1 -0.1 0\nv 0.1 0.1 0.05\n";
const LOWER_ARM_OBJ = "v -0.1 -0.1 -0.2\nv 0.1 0.1 -0.15\n";

/**
 * The mug's mesh, reduced to the two corners that carry its extremes. Its X and Y are the
 * transpose of what MUG_AABB declares, which is the whole of the model-23 defect. The second
 * vertex is written *after* the face that indexes it, so this fixture also pins that vertex
 * bounds do not depend on statement order (the Q143 shape); `vt`/`vn` lines are here to prove
 * they are not mistaken for positions.
 */
const MUG_OBJ = `# mug
vt 0 0
vn 0 0 1
v -0.04999 -0.065113 -5e-06
f 1/1/1 2/2/2 1/1/1
v 0.046532 0.065627 0.114619
`;

/** What MUG_OBJ's vertices actually bound -- the box placement must use instead of MUG_AABB. */
const MUG_MESH_BOX: Box = { min: [-0.04999, -0.065113, -5e-06] as Vec3, max: [0.046532, 0.065627, 0.114619] as Vec3 };

function stubSource(): AssetSource & { reads: string[], metaReads: string[] } {
  const reads: string[] = [];
  const metaReads: string[] = [];
  const documentFor = (kind: LibraryFileKind, id: number): Record<string, unknown> => {
    const document = DOCUMENTS[`${kind}:${id}`];
    if (!document) throw new Error(`no fixture for ${kind} ${id}`);
    return document;
  };
  return {
    reads,
    metaReads,
    model: (id: number): Promise<ModelMetadata> => {
      metaReads.push(`model:${id}`);
      return Promise.resolve(parseModelMetadata(id, documentFor("model", id), "model"));
    },
    robot: (id: number): Promise<ModelMetadata> => {
      metaReads.push(`robot:${id}`);
      return Promise.resolve(parseModelMetadata(id, documentFor("robot", id), "robot"));
    },
    fileText: (id: number, _path: string, _isOnCos: boolean, kind: LibraryFileKind = "model"): Promise<string> => {
      reads.push(`${kind}:${id}`);
      // Only the mug has a mesh to bound; every other read still gets the vertex-less tree,
      // so an asset placement that was free of vertex reads before stays free of them.
      return Promise.resolve(kind === "model" && id === 23 ? MUG_OBJ : BARE_URDF);
    },
  };
}

/** One robot's tag rewritten, so the role judgement can be pinned against a wording OLS does not ship today. */
function taggedSource(id: number, tag: string): AssetSource {
  const source = stubSource();
  const document = DOCUMENTS[`robot:${id}`]!;
  const semantic = document.semantic_properties as Record<string, unknown>;
  const rewritten = { ...document, semantic_properties: { ...semantic, tag } };
  source.robot = async (requested: number) => parseModelMetadata(requested,
    requested === id ? rewritten : DOCUMENTS[`robot:${requested}`]!, "robot");
  return source;
}

/** happy-dom replaces the global `URL`, so `new URL(..., import.meta.url)` stops being a `file:` URL. */
const sceneFixture = resolve(dirname(fileURLToPath(import.meta.url)), "../../docs/ur5e_table_empty.json");
const realScene = (): WorldFile => JSON.parse(readFileSync(sceneFixture, "utf-8")) as WorldFile;
const sceneWorld = (): World => parseWorld(realScene());

/** Only the two lights and the table: nothing of the arm's, the gripper's or a camera's. */
function strippedWorld(): World {
  const keep = new Set([0, 1, 7]);
  return parseWorld({ ...realScene(), objects: realScene().objects.filter((node) => keep.has(node.object_id_in_scene)) });
}

/**
 * A `type:model` prop the hand is carrying - the shipped wrist-mounted microwave (model 30) is
 * exactly this: hung on the arm (`RobotAttachment` + `Transform.parent`) yet a library **model**,
 * not a member of the robot id space. It moves with the arm, but it is a thing standing over the
 * table, so placement has to keep clearing it (the 2026-11-05 穿模 report).
 */
function wristCarriedModel(id: number): WorldFile["objects"][number] {
  const components: WorldComponent[] = [
    { component_type: "Transform", properties: { position: [0.5, 0, 0.7], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: 2 } },
    { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/30", type: "model" } },
    { component_type: "RobotAttachment", properties: { parent_object_id: 2, parent_joint: "wrist_3_joint", parent_link: "wrist_3_link", child_link: "microwave_mount", connection_type: "fixed", transform: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], reference_frame: "parent_link" } } },
    { component_type: "Semantic", properties: { semantic_name: "microwave", category: "object", support: null } },
  ];
  return { object_id_in_scene: id, object_name_in_scene: "microwave", components };
}

function props(world: World, id: number, type: string): Record<string, unknown> {
  const component = world.get(id)?.rawComponents.find((entry) => entry.component_type === type);
  return (component?.properties ?? {}) as Record<string, unknown>;
}

function componentTypes(world: World, id: number): string[] {
  return (world.get(id)?.rawComponents ?? []).map((component) => component.component_type);
}

function position(world: World, id: number): Vec3 {
  return props(world, id, "Transform").position as Vec3;
}

function expectVecClose(actual: Vec3 | undefined, expected: readonly number[]): void {
  expect(actual).toBeDefined();
  expected.forEach((value, axis) => expect(actual![axis]).toBeCloseTo(value, 6));
}

function expectBoxClose(actual: Box, expected: Box): void {
  expectVecClose(actual.min, expected.min);
  expectVecClose(actual.max, expected.max);
}

function sequence(values: readonly number[]): () => number {
  let index = 0;
  return () => values[index++ % values.length]!;
}

describe("the box an asset ships in (§4.6)", () => {
  it("rests an offset mesh on the surface without including its empty origin", () => {
    const local: Box = { min: [1, 2, 0.3], max: [1.2, 2.4, 0.5] };
    const footprint = orientedBox(local, [0, 0, 0], TABLE_YAW, [1, 1, 1]);
    expectBoxClose(footprint, { min: [-2.4, 1, 0.3], max: [-2, 1.2, 0.5] });
    const table = localBox(TABLE_AABB);
    const spot = spotOnTable(table, footprint, [], { random: () => 0.5 })!;
    const placed = orientedBox(local, spot.position, TABLE_YAW, [1, 1, 1]);
    expect(placed.min[2]).toBeCloseTo(table.max[2], 8);
    expectBoxClose(placed, spot.box);
  });

  it("reads the six AABB floats as three unordered pairs, because both may be positive", () => {
    expect(localBox([0.69149415, 0.09149412, 0.4, -0.4, 0.8, 0])).toEqual({
      min: [0.09149412, -0.4, 0],
      max: [0.69149415, 0.4, 0.8],
    });
  });

  it("turns a box by the table's yaw, which is what lays a 机械臂 along world +Y", () => {
    expectBoxClose(orientedBox(localBox(ARM_AABB), [0, 0, 0], TABLE_YAW, [1, 1, 1]), {
      min: [-0.232935, -0.075499, 0],
      max: [0.075525, 0.856347, 0.234524],
    });
  });
});

describe("the pose the percentages give an arm (§4.6)", () => {
  const table = localBox(TABLE_AABB);

  it("adds all three offsets to the middle of the table's long edge", () => {
    // The edge runs along X on the Y-/Z+ side: centre (0, -0.375, 0.6), spans 1.5/0.75/0.6.
    // Every percentage is added to that point, so Y moves towards the table's interior.
    expectVecClose(armOrigin(table, PERCENT), [0.3, -0.15, 0.9]);
    expectVecClose(armOrigin(table, PERCENT, -1), [-0.3, -0.15, 0.9]);
  });

  it("writes that point as the arm's origin, whatever its own box reaches below it", () => {
    // The key names 原点位置, so an arm whose geometry hangs under its origin - baxter
    // ships a box that drops 0.92 m below - still stands with its origin on the point.
    expectVecClose(armOrigin(table, PERCENT), [0.3, -0.15, 0.9]);
    const pose = pedestalPose(armOrigin(table, PERCENT), localBox(BAXTER_AABB), localBox(PEDESTAL_AABB), [1, 1, 1]);
    expectVecClose(pose.position, [0.3, -0.15, -0.274180031]);
  });

  it("publishes no placement at all for an arm with no `scene_one_arm`", async () => {
    const asset = await readAsset(stubSource(), "robot", 13);
    expect(asset.role).toBe("arm");
    expect(asset.placement).toBeNull();
  });

  it("seats a robot on its fixed base geometry instead of a movable link below it", async () => {
    const source = stubSource();
    source.robot = async (id) => parseModelMetadata(id, DOCUMENTS[`robot:${id}`]!, "robot");
    source.fileText = async (id, path, _isOnCos, kind = "model") => kind === "robot" && id === 24 && path === "arm.urdf" ? SUPPORT_URDF
      : path === "base.obj" ? BASE_OBJ : LOWER_ARM_OBJ;

    const armAsset = await readAsset(source, "robot", 24);
    const world = strippedWorld();
    const report = await placeAsset(world, source, "robot", 24, "support_test_arm");
    const arm = world.get(report.selected!)!;
    const pedestal = world.objects.find((node) => node.semantic?.category === "support")!;
    expect(pedestal.transform.position[2]! + 0.25).toBeCloseTo(arm.transform.position[2]!, 8);
    expect(pedestal.transform.position[2]! + 0.25)
      .not.toBeCloseTo(arm.transform.position[2]! + armAsset.box!.min[2]!, 3);
  });

  it("falls back to the whole-asset box when the base mesh format cannot be measured", async () => {
    const source = stubSource();
    source.robot = async (id) => parseModelMetadata(id, DOCUMENTS[`robot:${id}`]!, "robot");
    source.fileText = async (id, path, _isOnCos, kind = "model") => kind === "robot" && id === 24 && path === "arm.urdf"
      ? SUPPORT_URDF.replace("base.obj", "base.stl")
      : path === "base.stl" ? "solid base" : LOWER_ARM_OBJ;

    const arm = await readAsset(source, "robot", 24);
    expect(arm.baseBottomZ).toBeNull();
    const pose = pedestalPose([0, 0, 0.9], arm.box, localBox(PEDESTAL_AABB), [1, 1, 1], arm.baseBottomZ ?? null);
    expect(pose.position[2]! + 0.25).toBeCloseTo(0.8, 8);
  });

  it("keeps a metadata-driven arm switch available when the optional support measurement cannot read its URDF", async () => {
    const source = stubSource();
    source.robot = async (id) => parseModelMetadata(id, DOCUMENTS[`robot:${id}`]!, "robot");
    source.fileText = async () => { throw new Error("URDF file unavailable"); };

    const arm = await readAsset(source, "robot", 24);
    expect(arm.role).toBe("arm");
    expect(arm.baseBottomZ).toBeUndefined();
    const pose = pedestalPose([0, 0, 0.9], arm.box, localBox(PEDESTAL_AABB), [1, 1, 1], arm.baseBottomZ ?? null);
    expect(pose.position[2]! + 0.25).toBeCloseTo(0.8, 8);
  });
});

describe("the pedestal under an arm (§4.6)", () => {
  it("puts the box top against the arm's foot and writes the declared scale through", () => {
    const pose = pedestalPose([0.3, -0.15, 0.9] as Vec3, localBox(ARM_AABB), localBox(PEDESTAL_AABB), [1, 1, 1]);
    // The arm's own AABB bottoms out at z=0, so its foot is the origin the percentages
    // gave it. The declared 1 stays 1 even though the 0.5 m box then floats 0.4 m up:
    // closing that gap is the metadata author's call, not this function's.
    expect(pose.scale).toEqual([1, 1, 1]);
    expect(pose.position[2]! + 0.25).toBeCloseTo(0.9, 6);
    expect(pose.position[2]! - 0.25).toBeCloseTo(0.4, 6);
    expectVecClose([pose.position[0], pose.position[1], 0] as Vec3, [0.3, -0.15, 0]);
  });

  it("takes the arm's origin as its foot when the arm has no box, and scales the top by the declared z", () => {
    const pose = pedestalPose([0, 0, 0.4] as Vec3, null, localBox(PEDESTAL_AABB), [1, 1, 2]);
    expect(pose.scale).toEqual([1, 1, 2]);
    expect(pose.position[2]).toBeCloseTo(-0.1, 6);
  });
});

describe("a free spot on the table top (§4.6)", () => {
  const table = localBox(TABLE_AABB);
  const footprint = localBox(APPLE_AABB);
  const blocker: Box = { min: [-0.1, -0.1, 0.55] as Vec3, max: [0.1, 0.1, 0.7] as Vec3 };

  it("stands the box on the top and centres it on the sampled point", () => {
    const spot = spotOnTable(table, footprint, [], { random: () => 0.5 })!;
    expect(spot.box.min[2]).toBeCloseTo(0.6, 6);
    expect(spot.box.max[2]).toBeCloseTo(0.671247247, 6);
    // The origin sits under the box's own centre, which the apple's AABB does not put at 0.
    expect(spot.position[2]).toBeCloseTo(0.637373223, 6);
    expect(spot.position[0]).toBeCloseTo(-0.00021716, 6);
    expect(spot.position[1]).toBeCloseTo(-0.000032792, 6);
  });

  it("refuses a candidate that runs into another model and takes the next sample", () => {
    // random 0 lands the box in the near corner, already clear of the blocker in the middle.
    expect(spotOnTable(table, footprint, [blocker], { random: () => 0 })!.box.max[0]).toBeLessThan(-0.1);
    // One sample, and it is the blocked centre: with the sweep off there is no second chance.
    expect(spotOnTable(table, footprint, [blocker], { random: () => 0.5, attempts: 1, scanSteps: 0 })).toBeNull();
  });

  it("sweeps the whole top for a gap the samples all missed, instead of giving up (Q144)", () => {
    // Every sample is pinned onto the blocker, so phase one can never succeed on its own; the
    // free band is the strip just past it. The sweep finds that strip rather than reporting 放不下.
    const swept = spotOnTable(table, footprint, [blocker], { random: () => 0.5, attempts: 1 })!;
    expect(swept).not.toBeNull();
    expect(overlaps(swept.box, blocker)).toBe(false);
    // Centre-first, so the spot it settles on hugs the blocker instead of jumping to a far corner.
    expect(Math.abs(swept.position[0])).toBeLessThan(0.3);
    // The same call with 64 unlucky samples lands in the same place: the sweep, not the dice, decides.
    expect(spotOnTable(table, footprint, [blocker], { random: () => 0.5, attempts: 64 })!.box).toEqual(swept.box);
  });

  it("gives up rather than overlap when nothing is free", () => {
    const full: Box = { min: [-0.75, -0.375, 0.55] as Vec3, max: [0.75, 0.375, 0.7] as Vec3 };
    expect(spotOnTable(table, footprint, [full], { random: () => 0.5, attempts: 8 })).toBeNull();
  });

  it("finds a gap the old fixed grid stepped straight over (2026-11-05 follow-up to Q144)", () => {
    // Two blockers leave only a strip on the X axis. With the apple's footprint the legal
    // centre falls in [-0.2627, -0.2373]; the 33-point grid samples -0.2672 and -0.2227 either
    // side of it and never enters the strip, so the old sweep reported 放不下 with room in plain
    // view. The flush-edge pass puts the box right against the nearer obstacle instead.
    const left: Box = { min: [-0.75, -0.375, 0.55] as Vec3, max: [-0.3, 0.375, 0.7] as Vec3 };
    const right: Box = { min: [-0.2, -0.375, 0.55] as Vec3, max: [0.75, 0.375, 0.7] as Vec3 };
    // Phase one is pinned onto the blocked centre, so it is the deterministic pass that has to win.
    const spot = spotOnTable(table, footprint, [left, right], { random: () => 0.5, attempts: 1 })!;
    expect(spot).not.toBeNull();
    expect(overlaps(spot.box, left)).toBe(false);
    expect(overlaps(spot.box, right)).toBe(false);
    // Centre-first puts it against the nearer blocker, flush - touching, not overlapping.
    expect(spot.box.max[0]).toBeCloseTo(right.min[0]!, 9);
    expect(spot.box.min[0]).toBeGreaterThan(left.max[0]!);
  });

  it("reaches that gap on every click, so 放不下 no longer depends on the dice", () => {
    // The reported symptom: a few refusals, then a sudden success on the same table. Real
    // Math.random in phase one, a guaranteed sweep - so success is now a certainty, not a roll.
    const left: Box = { min: [-0.75, -0.375, 0.55] as Vec3, max: [-0.3, 0.375, 0.7] as Vec3 };
    const right: Box = { min: [-0.2, -0.375, 0.55] as Vec3, max: [0.75, 0.375, 0.7] as Vec3 };
    for (let click = 0; click < 30; click += 1) {
      expect(spotOnTable(table, footprint, [left, right], { attempts: 5 })).not.toBeNull();
    }
  });

  it("refuses a strip narrower than the prop rather than forcing an overlap", () => {
    // The strip here (0.06 wide) is below the apple's 0.0747 footprint, so no flush centre is
    // legal either - completeness must not become "shove it in anyway".
    const left: Box = { min: [-0.75, -0.375, 0.55] as Vec3, max: [-0.3, 0.375, 0.7] as Vec3 };
    const right: Box = { min: [-0.24, -0.375, 0.55] as Vec3, max: [0.75, 0.375, 0.7] as Vec3 };
    expect(spotOnTable(table, footprint, [left, right], { random: () => 0.5, attempts: 1 })).toBeNull();
  });
});

describe("readAsset: what one click is (§4.6)", () => {
  it("calls a 机械臂 with links to hand out an arm and one naming a child_link a gripper", async () => {
    const source = stubSource();
    expect((await readAsset(source, "robot", 1)).role).toBe("arm");
    expect((await readAsset(source, "robot", 8)).role).toBe("arm");
    expect((await readAsset(source, "robot", 2)).role).toBe("gripper");
    // Metadata answered for every one of them, so the click cost no file read at all.
    expect(source.reads).toEqual([]);
  });

  // Q161: those two tags are the live index's own words - Chinese. No row of the 14-row robot
  // index says `camera` in English, so an English-only judgement called every shipped camera a 道具.
  it("calls a camera by the tag OLS publishes, the only place one says so", async () => {
    expect((await readAsset(stubSource(), "robot", 3)).role).toBe("camera");
    expect((await readAsset(stubSource(), "robot", 11)).role).toBe("camera");
  });

  it("still calls a camera by the English spelling of that tag", async () => {
    expect((await readAsset(taggedSource(3, "d435i,camera"), "robot", 3)).role).toBe("camera");
  });

  it("leaves a Chinese-tagged 底座 a prop: the word is 相机, not the script", async () => {
    expect((await readAsset(taggedSource(4, "机器人底座,支架,圆柱"), "robot", 4)).role).toBe("prop");
  });

  it("leaves a 模型 a prop however many joints its URDF has (§6.11)", async () => {
    const source = stubSource();
    const asset = await readAsset(source, "model", 25);
    expect(asset.role).toBe("prop");
    expect(source.reads).toEqual(["model:25"]);
    expect(asset.connectLinks).toEqual([]);
    expect(asset.childLink).toBeNull();
  });

  it("lifts robot_attachment out of the driver it is about to write", async () => {
    const asset = await readAsset(stubSource(), "robot", 2);
    expect(asset.childLink).toBe("robotiq_2f85_mount");
    expect(asset.driver).not.toHaveProperty("robot_attachment");
    expect(asset.driver).toMatchObject({ joint_sequence: ["robotiq_2f85_right_driver_joint"] });
  });

  it("reads the URDF tree for a 机械臂 the metadata leaves blank (§4.7)", async () => {
    const source = stubSource();
    const asset = await readAsset(source, "robot", 16);
    expect(source.reads).toEqual(["robot:16"]);
    // A tree: the root is what may be hung from, the leaves what it hands out.
    expect(asset.childLink).toBe("base_mount");
    expect(asset.connectLinks).toEqual(["tip_link"]);
    expect(asset.connectJoints).toEqual(["arm_joint"]);
    expect(asset.driver).toMatchObject({ role: "passive", joint_sequence: ["arm_joint"] });
    expect(asset.role).toBe("arm");
  });

  it("names an asset by OLS' own object_name when the panel gives no name", async () => {
    expect((await readAsset(stubSource(), "robot", 4)).name).toBe("robot_pedestal");
  });

  it("prefers the Chinese metadata name over the supplied panel name", async () => {
    const source = stubSource();
    const model = source.model;
    source.model = async (id) => {
      const metadata = await model(id);
      return { ...metadata, raw: { ...metadata.raw, semantic_properties: { object_name_Chinese: "苹果" } } };
    };
    expect((await readAsset(source, "model", 1, "apple")).name).toBe("苹果");
  });

  it("uses the authored quaternion for both prop and arm transforms and their footprint", async () => {
    const asset = await readAsset(stubSource(), "model", 1);
    asset.metadata = { ...asset.metadata, raw: { ...asset.metadata.raw,
      geometric_properties: { placement_quaternion_xyzw: [0, 0, 0, 1] } } };
    expect(placementQuaternion(asset)).toEqual([0, 0, 0, 1]);
    for (const components of [propComponents(asset, [0, 0, 0], 0), armComponents(asset, [0, 0, 0])]) {
      expect(components[0]!.properties).toMatchObject({ quaternion_xyzw: [0, 0, 0, 1] });
    }
    const box: Box = { min: [0, 0, 0], max: [2, 1, 1] };
    expect(orientedBox(box, [0, 0, 0], placementQuaternion(asset), [1, 1, 1])).toEqual(box);
    for (const invalid of [undefined, [0, 0, 0], [0, 0, 0, 0], [0, 0, NaN, 1], "bad"]) {
      asset.metadata.raw.geometric_properties = { placement_quaternion_xyzw: invalid };
      expect(placementQuaternion(asset)).toEqual(TABLE_YAW);
    }
  });
});

describe("an OBJ 模型 whose metadata AABB is transposed against its mesh (§4.6, Q144)", () => {
  it("bounds the prop by the vertices OLS ships, not by the AABB it declares", async () => {
    const source = stubSource();
    const asset = await readAsset(source, "model", 23, "mug");
    // The declared box is X-wide / Y-narrow; the mesh is the other way round. Placement takes the mesh.
    expect(asset.box).toEqual(MUG_MESH_BOX);
    expect(localBox(MUG_AABB)).not.toEqual(MUG_MESH_BOX);
    // It cost exactly one read, of the mug's own file, and only because that geometry is an OBJ.
    expect(source.reads).toEqual(["model:23"]);
  });

  it("leaves a URDF 模型 on its metadata box, so a prop with no OBJ is never fetched for bounds", async () => {
    expect((await readAsset(stubSource(), "model", 25)).box).toEqual(localBox([0.2, -0.2, 0.2, -0.2, 0.3, 0]));
  });

  it("measures a second mug against the first one's mesh, through the whole placeAsset path", async () => {
    const world = sceneWorld();
    const source = stubSource();
    // Both clicks pin their one sample on the table centre, so the second is refused there and
    // the sweep settles it as near the first as the *meshes* allow.
    const first = await placeAsset(world, source, "model", 23, "mug", { random: () => 0.5, attempts: 1 });
    const second = await placeAsset(world, source, "model", 23, "mug", { random: () => 0.5, attempts: 1 });
    expect(first.selected!).not.toBe(second.selected!);
    const drawn = (id: number): Box => orientedBox(MUG_MESH_BOX, position(world, id), TABLE_YAW, [1, 1, 1] as Vec3);
    // The occupied set the second click measured against carried the corrected box, so what the
    // renderer draws is clear too. (The unit case above is the pin; this one guards the wiring
    // from readAsset through occupiedBoxes into a real placement.)
    expect(overlaps(drawn(first.selected!), drawn(second.selected!))).toBe(false);
  });
});

describe("clicking a 模型 puts it on the shipped table", () => {
  it("adds it resting on the top, supported by the table, and says so", async () => {
    const world = sceneWorld();
    const version = world.version;
    const report = await placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0 });
    const id = report.selected!;
    expect(report.added).toEqual([id]);
    expect(report.notice).toBe("已把「apple」摆到桌面上");
    expect(componentTypes(world, id)).toEqual(["Transform", "MetadataRef", "MeshRenderer", "Semantic", "PhysicalProperties"]);
    expect(props(world, id, "Semantic")).toEqual({ semantic_name: "apple", category: "object", support: 1 });
    expect(props(world, id, "MetadataRef")).toEqual({ path: "$LIBRARY_SERVER/1", type: "model" });
    expectVecClose(position(world, id), [-0.187601202, -0.337889444, 0.637373223]);
    // A prop on the table names it in `Semantic.support` and keeps a null parent, the
    // way every shipped scene writes it.
    expect(props(world, id, "Transform")).toMatchObject({ parent: null, scale: [1, 1, 1], quaternion_xyzw: [0, 0, Math.SQRT1_2, Math.SQRT1_2] });
    // A browser-local write moves the sub-version and leaves the scene version alone (Q114 ②).
    expect(world.version).toBe(version);
    expect(world.subVersion).toBe(1);
  });

  it("takes the middle of the top although the 机械臂 reaches over it (§4.6.1, Q137)", async () => {
    const world = sceneWorld();
    // One attempt, and it is the centre sample - the point that sits inside the authored
    // arm's box, which is the click Q134 refused with 桌面上放不下. An arm that moves every
    // frame is not an obstacle a prop has to be measured against.
    const report = await placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0.5, attempts: 1 });
    // The middle of the allowed band is the table's own centre on both horizontal axes; the
    // two small offsets are the apple's footprint centre, which `spotOnTable` subtracts, and
    // the z is the same table top every other prop in this file stands on.
    expectVecClose(position(world, report.selected!), [0.525032792, -0.00021716, 0.637373223]);
    expect(report.notice).toBe("已把「apple」摆到桌面上");
  });

  it("still steps around a 模型 that already stands on that middle", async () => {
    const world = sceneWorld();
    const first = await placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0.5, attempts: 1 });
    // The blocker a prop has to dodge is another prop: the second click samples the same
    // middle, finds the first apple there and takes the next sample - the near corner.
    const second = await placeAsset(world, stubSource(), "model", 1, "apple", { random: sequence([0.5, 0.5, 0, 0]) });
    expectVecClose(position(world, second.selected!), [-0.187601202, -0.337889444, 0.637373223]);
    expect(position(world, second.selected!)).not.toEqual(position(world, first.selected!));
  });

  it("refuses a model whose metadata publishes no box rather than dropping it at the origin", async () => {
    const world = sceneWorld();
    const before = world.objectCount;
    await expect(placeAsset(world, stubSource(), "model", 33, "button")).rejects.toThrowError(/的元数据里没有包围盒/);
    expect(world.objectCount).toBe(before);
  });

  it("refuses a scene with no table, which is why an empty 现场 loads the baseline first", async () => {
    expect(BASELINE_SCENE_ID).toBe(1);
    const empty = parseWorld({ scene_schema_version: 1, scene_version: 1, scene_id: 0, scene_name: "", objects: [] });
    await expect(placeAsset(empty, stubSource(), "model", 1, "apple")).rejects.toThrowError(/场景里没有工作台/);
  });
});

/**
 * §4.6.1 (2026-11-05 穿模修复): collision must read the browser's own box - the one it draws
 * the wireframe from, already joint-driven for an articulated URDF - and fall back to OLS's
 * declared AABB only for an object the renderer has not measured yet. `BoundsMeasure` is the
 * seam placement uses to read it; a stub replaces the GPU here.
 */
describe("placement collision trusts the browser's own box, not OLS's AABB (§4.6.1, 穿模)", () => {
  it.each([[2, "gripper"], [11, "camera"]] as const)(
    "replaces robot %i (%s) without reading unrelated table metadata or geometry",
    async (id, name) => {
      const source = stubSource();
      source.model = async () => { throw new Error("replacement must not read the table asset"); };
      const measure: BoundsMeasure = {
        sceneAABB: () => { throw new Error("replacement must not measure geometry"); },
        settle: async () => { throw new Error("replacement must not wait for geometry"); },
      };
      const report = await placeAsset(sceneWorld(), source, "robot", id, name, {}, measure);
      expect(report.added).toEqual([]);
      expect(report.updated.length).toBeGreaterThan(0);
    },
  );

  it.each([[2, "gripper"], [11, "camera"]] as const)(
    "replaces robot %i (%s) in a scene without a table",
    async (id, name) => {
      const document = realScene();
      document.objects = document.objects.filter((object) => object.object_id_in_scene !== 1);
      const report = await placeAsset(parseWorld(document), stubSource(), "robot", id, name);
      expect(report.added).toEqual([]);
      expect(report.updated.length).toBeGreaterThan(0);
    },
  );

  it("waits for participating obstacles and uses their loaded bounds", async () => {
    const world = sceneWorld();
    let releaseObstacle!: () => void;
    let obstacleRequested!: () => void;
    const obstacleReady = new Promise<void>((resolve) => { releaseObstacle = resolve; });
    const requested = new Promise<void>((resolve) => { obstacleRequested = resolve; });
    const settled = new Set<number>();
    const measure: BoundsMeasure = {
      settle: async (id) => {
        if (id === 5) {
          obstacleRequested();
          await obstacleReady;
        }
        settled.add(id);
      },
      sceneAABB: (id) => id === 5 && settled.has(id)
        ? { min: [-5, -5, 0], max: [5, 5, 1.5] }
        : null,
    };
    const placement = placeAsset(world, stubSource(), "model", 1, "apple", {}, measure);
    await requested;
    releaseObstacle();
    await expect(placement).rejects.toThrowError(/桌面上放不下/);
    // The arm family is deliberately outside collision checks and must not be awaited.
    expect([2, 3, 4].some((id) => settled.has(id))).toBe(false);
  });

  it("waits for the table before reading its height", async () => {
    const world = sceneWorld();
    let ready = false;
    const measure: BoundsMeasure = {
      settle: async () => { ready = true; },
      sceneAABB: (id) => id === 1 && ready
        ? { min: [-0.75, -0.375, 0], max: [0.75, 0.375, 0.8] }
        : null,
    };
    const report = await placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0 }, measure);
    expect(position(world, report.selected!)[2]).toBeCloseTo(0.8 - APPLE_AABB[5], 6);
  });

  /** A seam whose boxes come from a pure function, recording the ids it was consulted for. */
  function stubMeasure(boxesFor: (objectId: number) => Box | null): BoundsMeasure & { settled: number[]; reads: number[] } {
    const settled: number[] = [];
    const reads: number[] = [];
    return {
      settled,
      reads,
      sceneAABB: (objectId) => {
        reads.push(objectId);
        return boxesFor(objectId);
      },
      settle: async (objectId) => {
        settled.push(objectId);
      },
    };
  }

  /** Every id already in the shipped scene - anything past these is the just-added prop. */
  const SHIPPED_IDS = new Set([0, 1, 2, 3, 4, 5, 6]);

  it("settles and measures the just-placed prop through the seam before choosing its spot", async () => {
    const world = sceneWorld();
    // The seam answers only for the provisional prop; the table and every obstacle fall
    // through to metadata, so this pins that the candidate's own footprint is settled first.
    const measure = stubMeasure(() => null);
    const report = await placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0.5, attempts: 1 }, measure);
    const provisional = report.selected!;
    expect(SHIPPED_IDS.has(provisional)).toBe(false);
    // The table is ready before its height is read, then the new prop is built.
    expect(measure.settled).toEqual([1, provisional, 5, 6]);
    expect(measure.reads).toContain(provisional);
    // Seam returned null for it, so the metadata centre placement is unchanged (matches Q137 above).
    expectVecClose(position(world, provisional), [0.525032792, -0.00021716, 0.637373223]);
  });

  it("uses the seam's box for the candidate even when its metadata box is small enough to fit", async () => {
    const world = sceneWorld();
    // The apple's own metadata box clears the table easily, but the browser measured a
    // table-spanning footprint - so the seam, not OLS, is what makes the click refuse.
    const measure = stubMeasure((id) => (SHIPPED_IDS.has(id) ? null : { min: [-10, -10, -10] as Vec3, max: [10, 10, 10] as Vec3 }));
    const before = world.objectCount;
    await expect(placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0.5, attempts: 1 }, measure))
      .rejects.toThrowError(/桌面上放不下/);
    // A refused click tears the provisional back out, so the scene is exactly as it was.
    expect(world.objectCount).toBe(before);
  });

  it("measures an existing obstacle with the seam box, not its (tiny) metadata box", async () => {
    const world = sceneWorld();
    // The overhead camera (id 5) publishes a small box that leaves the whole top free (the Q137
    // test above proves a prop lands at the centre past it). The seam instead reports the
    // browser-measured box reaching across the entire tabletop band, so no spot fits and the
    // click refuses - proof the obstacle box came from `sceneAABB`, not the metadata AABB.
    const measuredOverReach: Box = { min: [-5, -5, 0] as Vec3, max: [5, 5, 1.5] as Vec3 };
    const measure = stubMeasure((id) => (id === 5 ? measuredOverReach : null));
    await expect(placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0.5, attempts: 1 }, measure))
      .rejects.toThrowError(/桌面上放不下/);
    expect(measure.reads).toContain(5);
  });

  it("falls back to OLS's box when the seam cannot measure a scene object yet", async () => {
    const world = sceneWorld();
    const source = stubSource();
    // A seam that answers for nothing must reproduce the metadata placement exactly, and
    // must still consult OLS for the obstacles - the old path, kept for unmeasured objects.
    const measure = stubMeasure(() => null);
    const report = await placeAsset(world, source, "model", 1, "apple", { random: () => 0.5, attempts: 1 }, measure);
    expectVecClose(position(world, report.selected!), [0.525032792, -0.00021716, 0.637373223]);
    expect(source.metaReads).toEqual(["model:1", "model:29", "robot:3", "robot:4"]);
  });

  it("still refuses a prop that has neither a seam box nor a metadata box", async () => {
    const world = sceneWorld();
    const measure = stubMeasure(() => null);
    const before = world.objectCount;
    await expect(placeAsset(world, stubSource(), "model", 33, "button", {}, measure))
      .rejects.toThrowError(/的元数据里没有包围盒/);
    expect(world.objectCount).toBe(before);
  });

  it("places a prop that OLS has no box for but the browser measured one", async () => {
    const world = sceneWorld();
    // The reverse of the bug this whole change exists for: metadata model 33 publishes no
    // AABB at all, yet the renderer drew it and can bound it - so the browser box wins.
    const measure = stubMeasure((id) => (SHIPPED_IDS.has(id) ? null : { min: [-0.02, -0.02, -0.02] as Vec3, max: [0.02, 0.02, 0.02] as Vec3 }));
    const report = await placeAsset(world, stubSource(), "model", 33, "button", { random: () => 0 }, measure);
    expect(report.selected).not.toBeNull();
    expect(world.objectCount).toBeGreaterThan(6);
    expect(measure.settled).toEqual([1, report.selected, 5, 6]);
  });

  it("measures the table from the seam box when the stage can supply it", async () => {
    const world = sceneWorld();
    // The seam hands back the table's real top box for id 1; placement should use it rather
    // than re-reading model:29's metadata AABB. The metadata table stays unread because the
    // seam answered first.
    const tableTop: Box = { min: [-0.75, -0.375, 0.6] as Vec3, max: [0.75, 0.375, 0.6] as Vec3 };
    const source = stubSource();
    const measure = stubMeasure((id) => (id === 1 ? tableTop : null));
    await placeAsset(world, source, "model", 1, "apple", { random: () => 0.5, attempts: 1 }, measure);
    expect(measure.reads[0]).toBe(1);
    expect(source.metaReads).not.toContain("model:29");
  });
});

describe("the 机械臂 is not part of what a new prop has to clear (§4.6.1, Q137)", () => {
  it("closes the family over both edges a shipped scene uses to hang things on an arm", () => {
    // 2 the arm, 3 the gripper it carries (`RobotAttachment`), 4 the wrist camera hung on
    // that gripper (`Transform.parent`) - the whole assembly that moves with the arm.
    expect([...armFamilyIds(sceneWorld())].sort((a, b) => a - b)).toEqual([2, 3, 4]);
  });

  it("keeps a wrist-carried model prop out of the family (2026-11-05 穿模)", () => {
    const world = parseWorld({ ...realScene(), objects: [...realScene().objects, wristCarriedModel(40)] });
    const family = armFamilyIds(world);
    // The arm, its gripper and the wrist camera still close the family...
    expect([family.has(2), family.has(3), family.has(4)]).toEqual([true, true, true]);
    // ...but a `type:model` prop hung on the wrist (the microwave, model 30) is a thing on the
    // stage, not part of the assembly: it stays an obstacle, or a new prop lands inside it.
    expect(family.has(40)).toBe(false);
  });

  it("refuses to place a prop inside a model the hand is carrying", async () => {
    const world = parseWorld({ ...realScene(), objects: [...realScene().objects, wristCarriedModel(40)] });
    // The seam blocks the whole tabletop band ONLY through the wrist-mounted model (id 40). If the
    // closure still swallowed it as arm family that box would be ignored and the click would land -
    // the reported 穿模. With the fix id 40 stays an obstacle, nothing fits, and the click refuses.
    const block: Box = { min: [-5, -5, 0] as Vec3, max: [5, 5, 1.5] as Vec3 };
    const measure: BoundsMeasure = {
      sceneAABB: (objectId) => (objectId === 40 ? block : null),
      settle: async () => undefined,
    };
    await expect(placeAsset(world, stubSource(), "model", 1, "apple", { random: () => 0.5, attempts: 1 }, measure))
      .rejects.toThrowError(/桌面上放不下/);
  });

  it("leaves the furniture of the stage in the collision set", () => {
    // The table is where the spot is sampled, the pedestal is what the arm stands on and
    // never moves, and the camera looking down is a thing a prop really can run into.
    const family = armFamilyIds(sceneWorld());
    expect([family.has(1), family.has(5), family.has(6)]).toEqual([false, false, false]);
  });

  it("names no family for a scene that has no arm", () => {
    expect(armFamilyIds(strippedWorld()).size).toBe(0);
  });

  it("does not even ask OLS what the arm and its hand are", async () => {
    const source = stubSource();
    await placeAsset(sceneWorld(), source, "model", 1, "apple", { random: () => 0.5 });
    // The clicked 模型, the table it lands on, and the two things still measured against:
    // the camera looking down (robot 3) and the pedestal (robot 4).
    expect(source.metaReads).toEqual(["model:1", "model:29", "robot:3", "robot:4"]);
  });

  it("is the arm's own box that used to refuse that middle sample - the rule changed, the geometry did not", async () => {
    const world = sceneWorld();
    const source = stubSource();
    const tableNode = findTableObject(world)!;
    const armNode = world.get(2)!;
    const table = orientedBox((await readAsset(source, "model", 29)).box!, tableNode.transform.position, tableNode.transform.quaternion, tableNode.transform.scale);
    const arm = orientedBox((await readAsset(source, "robot", 1)).box!, armNode.transform.position, armNode.transform.quaternion, armNode.transform.scale);
    const footprint = orientedBox((await readAsset(source, "model", 1)).box!, [0, 0, 0] as Vec3, TABLE_YAW, [1, 1, 1] as Vec3);
    // Put the arm back in the set and the middle sample is inside it - the refusal Q134 gave.
    // scanSteps:0 pins phase one alone; with the sweep on, Q144 would simply relocate off the arm.
    expect(spotOnTable(table, footprint, [arm], { random: () => 0.5, attempts: 1, scanSteps: 0 })).toBeNull();
    expect(spotOnTable(table, footprint, [], { random: () => 0.5, attempts: 1 })).not.toBeNull();
  });
});

describe("switching the 机械臂 of the shipped scene", () => {
  it("keeps the arm's identity and takes the pose its own percentages give", async () => {
    const world = sceneWorld();
    expect(findArmObjects(world).map((node) => node.id)).toEqual([2]);
    expect(findGripperObjects(world).map((node) => node.id)).toEqual([3]);
    const report = await placeAsset(world, stubSource(), "robot", 8, "piper");
    expect(report.updated).toEqual([2, 3, 6]);
    expect(report.added).toEqual([]);
    expect(report.selected).toBe(2);
    expect(report.notice).toBe("已把机械臂换成「piper」");
    expect(world.get(2)?.name).toBe("piper");
    // The percentages are added to the middle of the long edge of the box the table
    // actually occupies; this fixture stands it at x=0.525, so 0.2 of its 1.5 m length
    // lands 0.3 beyond that and 0.3 of its 0.75 m depth comes back in from the -Y face.
    expectVecClose(position(world, 2), [0.825, -0.15, 0.9]);
    expect(props(world, 2, "Transform")).toMatchObject({ quaternion_xyzw: [0, 0, Math.SQRT1_2, Math.SQRT1_2], parent: null });
    expect(props(world, 2, "RobotDriver")).toMatchObject({ available_connect_joints: ["joint6"], available_connect_link: ["link6"] });
    expect(props(world, 2, "MetadataRef")).toEqual({ path: "$LIBRARY_SERVER/8", type: "robot" });
    // What the scene authored around the arm survives the swap (§3.8), and the shipped
    // arm carries no MeshRenderer, so the swap adds none.
    expect(componentTypes(world, 2)).toEqual(["Transform", "MetadataRef", "RobotDriver", "Semantic", "PhysicalProperties"]);
  });

  it("stands an arm that publishes no `scene_one_arm` at the triple the shipped ones all agree on", async () => {
    const world = sceneWorld();
    await placeAsset(world, stubSource(), "robot", 13, "pr2");
    expectVecClose(position(world, 2), [0.825, -0.15, 0.9]);
    // Nothing says it needs furniture, so the pedestal the template authored goes.
    expect(world.get(6)).toBeUndefined();
  });

  it("stands the pedestal under it with its top at the arm's foot and the scale the arm declares", async () => {
    const world = sceneWorld();
    await placeAsset(world, stubSource(), "robot", 8, "piper");
    expectVecClose(position(world, 6), [0.825, -0.15, 0.65]);
    expectVecClose(props(world, 6, "Transform").scale as Vec3, [1, 1, 1]);
    expect(props(world, 6, "MetadataRef")).toEqual({ path: "$LIBRARY_SERVER/4", type: "robot" });
    expect(props(world, 6, "Semantic")).toEqual({ semantic_name: "robot_pedestal", category: "support", support: null });
  });

  it("defaults a pedestal scale that the arm left out to [1, 1, 1]", async () => {
    const world = sceneWorld();
    await placeAsset(world, stubSource(), "robot", 5, "ur3");
    expectVecClose(position(world, 6), [0.825, -0.15, 0.65]);
    expectVecClose(props(world, 6, "Transform").scale as Vec3, [1, 1, 1]);
  });

  it("re-hangs the gripper on the new arm's own connect tables", async () => {
    const world = sceneWorld();
    await placeAsset(world, stubSource(), "robot", 8, "piper");
    expect(props(world, 3, "RobotAttachment")).toEqual({
      parent_object_id: 2,
      parent_joint: "joint6",
      parent_link: "link6",
      child_link: "robotiq_2f85_mount",
      connection_type: "fixed",
      transform: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], reference_frame: "parent_link" },
    });
    expect(props(world, 3, "Transform")).toMatchObject({ scale: [1, 1, 1], parent: 2 });
  });

  it("writes back the ur5e mount exactly as the template authored it, because that is where it came from", async () => {
    const world = sceneWorld();
    const authored = world.get(3)?.rawComponents.find((entry) => entry.component_type === "RobotAttachment")?.properties;
    await placeAsset(world, stubSource(), "robot", 1, "ur5e_arm");
    expect(world.get(3)?.rawComponents.find((entry) => entry.component_type === "RobotAttachment")?.properties).toEqual(authored);
  });

  it("drops the pedestal when the arm that replaces it stands on nothing", async () => {
    const world = sceneWorld();
    const report = await placeAsset(world, stubSource(), "robot", 9, "nero");
    expect(report.removed).toEqual([6]);
    expect(world.get(6)).toBeUndefined();
    expect(report.notice).toBe("已把机械臂换成「nero」");
  });

  it("stands the first arm up, with its pedestal, in a scene that has neither", async () => {
    const world = strippedWorld();
    const report = await placeAsset(world, stubSource(), "robot", 8, "piper");
    expect(report.added).toHaveLength(2);
    expect(report.removed).toEqual([]);
    expect(report.notice).toBe("已把机械臂「piper」摆到桌边");
    const [armId, pedestalId] = report.added;
    expect(report.selected).toBe(armId);
    expect(findArmObjects(world).map((node) => node.id)).toEqual([armId]);
    expect(props(world, armId!, "Transform")).toMatchObject({ parent: null });
    expect(props(world, armId!, "Semantic")).toMatchObject({ category: "robot" });
    expect(props(world, pedestalId!, "Semantic")).toMatchObject({ category: "support" });
  });
});

describe("switching the 夹爪 and the 相机", () => {
  it("replaces the gripper the arm carries instead of adding a second one", async () => {
    const world = sceneWorld();
    const report = await placeAsset(world, stubSource(), "robot", 2, "robotiq_2f85");
    expect(report.updated).toEqual([3]);
    expect(report.added).toEqual([]);
    expect(report.notice).toBe("已把夹爪换成「robotiq_2f85」");
    expect(report.selected).toBe(3);
    expect(props(world, 3, "RobotDriver")).toMatchObject({ joint_sequence: ["robotiq_2f85_right_driver_joint"] });
    expect(props(world, 3, "RobotAttachment")).toMatchObject({ parent_link: "wrist_3_link", parent_joint: "wrist_3_joint", child_link: "robotiq_2f85_mount" });
    expect(componentTypes(world, 3)).toEqual(["Transform", "MetadataRef", "RobotDriver", "RobotAttachment", "Semantic", "PhysicalProperties"]);
  });

  it("hangs the first gripper from the arm the scene already has", async () => {
    const world = strippedWorld();
    const armId = (await placeAsset(world, stubSource(), "robot", 8, "piper")).added[0]!;
    const report = await placeAsset(world, stubSource(), "robot", 2, "robotiq_2f85");
    expect(report.notice).toBe("已把夹爪「robotiq_2f85」挂到机械臂上");
    expect(props(world, report.selected!, "RobotAttachment")).toMatchObject({ parent_object_id: armId, parent_joint: "joint6", parent_link: "link6" });
    expect(componentTypes(world, report.selected!)).toEqual(["Transform", "MetadataRef", "RobotDriver", "RobotAttachment", "Semantic", "PhysicalProperties"]);
    expect(findGripperObjects(world).map((node) => node.id)).toEqual([report.selected]);
  });

  it("refuses a gripper when the scene has no arm to hang it from", async () => {
    const world = strippedWorld();
    expect(findTableObject(world)?.id).toBe(1);
    await expect(placeAsset(world, stubSource(), "robot", 2, "robotiq_2f85")).rejects.toThrowError(/场景里没有机械臂/);
  });

  it("replaces both cameras by reference and keeps the viewpoints the scene authored", async () => {
    const world = sceneWorld();
    const overhead = world.get(5)?.rawComponents.find((entry) => entry.component_type === "Transform")?.properties;
    const report = await placeAsset(world, stubSource(), "robot", 11, "d455");
    expect(report.updated.sort((left, right) => left - right)).toEqual([4, 5]);
    expect(report.added).toEqual([]);
    expect(report.notice).toBe("已把 2 个相机换成「d455」");
    expect(world.get(5)?.rawComponents.find((entry) => entry.component_type === "Transform")?.properties).toEqual(overhead);
    expect(props(world, 4, "Camera")).toMatchObject({ type: "rgbd", fov: 60 });
    expect(props(world, 4, "MetadataRef")).toEqual({ path: "$LIBRARY_SERVER/11", type: "robot" });
    expect(props(world, 4, "Semantic")).toMatchObject({ category: "sensor" });
  });

  it("stands a camera on the table when the scene has none to replace", async () => {
    const world = strippedWorld();
    const report = await placeAsset(world, stubSource(), "robot", 11, "d455", { random: () => 0 });
    expect(report.notice).toBe("场景里本来没有相机，已把「d455」摆到桌面上");
    expect(componentTypes(world, report.selected!)).toEqual(["Transform", "MetadataRef", "MeshRenderer", "Camera", "Semantic", "PhysicalProperties"]);
    expect(props(world, report.selected!, "Camera")).toEqual({ type: "rgbd", fov: 60, near: 0.01, far: 10, resolution: [640, 480] });
    expect(props(world, report.selected!, "Semantic")).toMatchObject({ category: "sensor", support: 1 });
  });
});

/**
 * §4.10 (Q165): OLS's own scene files write this component as the asset's `physical_properties`
 * verbatim minus `scale`, so a placement and every swap write the same copy; keys the asset does
 * not declare are the instance's own and survive a swap. The read side keeps a file's copy untouched.
 */
describe("PhysicalProperties on placed and swapped objects (§4.10, Q165)", () => {
  it("stands a prop with the asset's own numbers and never copies the allowed scale range", async () => {
    const world = sceneWorld();
    const report = await placeAsset(world, stubSource(), "model", 25, "drawer", { random: () => 0 });
    expect(props(world, report.selected!, "PhysicalProperties")).toEqual({
      is_static: true,
      mass_kg: 2.5,
      friction: { format: "[sliding, torsional, rolling]", values: [0.7, 0.01, 0.001] },
      restitution: 0.05,
    });
  });

  it("writes the component even when the asset declares no physical properties at all", async () => {
    const world = strippedWorld();
    const report = await placeAsset(world, stubSource(), "robot", 8, "piper");
    expect(componentTypes(world, report.selected!)).toContain("PhysicalProperties");
    expect(props(world, report.selected!, "PhysicalProperties")).toEqual({});
  });

  it("makes the pedestal static and the arm it carries movable, each with its own mass", async () => {
    const world = strippedWorld();
    const report = await placeAsset(world, stubSource(), "robot", 1, "ur5");
    const pedestal = world.objects.find((node) => node.semantic?.category === "support")!;
    expect(props(world, report.selected!, "PhysicalProperties")).toMatchObject({ is_static: false, mass_kg: 20.9949 });
    expect(props(world, pedestal.id, "PhysicalProperties")).toMatchObject({ is_static: true, mass_kg: 8.0 });
  });

  it("refreshes the asset's numbers on an arm swap and keeps keys the new asset does not declare", async () => {
    const world = sceneWorld();
    const node = world.get(2)!;
    world.updateLocalItem(2, {
      object_name_in_scene: node.name,
      components: [...node.rawComponents, { component_type: "PhysicalProperties", properties: { is_static: true, mass_kg: 999, curator: "lab" } }],
    });
    await placeAsset(world, stubSource(), "robot", 5, "ur3");
    expect(props(world, 2, "PhysicalProperties")).toEqual({
      is_static: false,
      mass_kg: 18.4,
      friction: { format: "[sliding, torsional, rolling]", values: [1.0, 0.005, 0.0001] },
      restitution: 0.05,
      curator: "lab",
    });
  });

  it("adds the component on a swap when the scene file never carried one", async () => {
    const world = sceneWorld();
    expect(componentTypes(world, 2)).not.toContain("PhysicalProperties");
    await placeAsset(world, stubSource(), "robot", 5, "ur3");
    expect(props(world, 2, "PhysicalProperties")).toMatchObject({ is_static: false, mass_kg: 18.4 });
  });

  it("gives a swapped gripper and both swapped cameras the new asset's properties", async () => {
    const world = sceneWorld();
    await placeAsset(world, stubSource(), "robot", 2, "robotiq_2f85");
    expect(props(world, 3, "PhysicalProperties")).toMatchObject({ is_static: false, mass_kg: 0.901 });
    await placeAsset(world, stubSource(), "robot", 11, "d455");
    for (const id of [4, 5]) {
      expect(props(world, id, "PhysicalProperties")).toEqual({
        is_static: false,
        mass_kg: 0.072,
        friction: { format: "[sliding, torsional, rolling]", values: [0.8, 0.01, 0.001] },
        restitution: 0.08,
      });
    }
  });

  it("leaves the viewpoint a camera swap must not touch alone", async () => {
    const world = sceneWorld();
    const camera = props(world, 5, "Camera");
    const transform = props(world, 5, "Transform");
    await placeAsset(world, stubSource(), "robot", 11, "d455");
    expect(props(world, 5, "Camera")).toEqual(camera);
    expect(props(world, 5, "Transform")).toEqual(transform);
  });
});

describe("rewriteComponents (§3.8)", () => {
  it("replaces by type, keeps the rest where they are and appends what was missing", () => {
    const components: WorldComponent[] = [
      { component_type: "Transform", properties: { position: [1, 2, 3], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: null } },
      { component_type: "LocalAxes", properties: { length: 0.3 } },
    ];
    const node = parseWorld({
      scene_schema_version: 1, scene_version: 1, scene_id: 1, scene_name: "",
      objects: [{ object_id_in_scene: 1, object_name_in_scene: "a", components }],
    }).get(1)!;
    const rewritten = rewriteComponents(node, [
      { component_type: "Transform", properties: { position: [9, 9, 9], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: null } },
      { component_type: "Semantic", properties: { semantic_name: "a", category: "robot", support: null } },
    ]);
    expect(rewritten.map((component) => component.component_type)).toEqual(["Transform", "LocalAxes", "Semantic"]);
    expect(rewritten[0]!.properties).toMatchObject({ position: [9, 9, 9] });
    // An untouched component is the very object the scene handed over, not a copy.
    expect(rewritten[1]).toBe(components[1]);
  });

  it("builds a list from nothing for an object the scene has never carried", () => {
    expect(rewriteComponents(null, [{ component_type: "Semantic", properties: {} }])).toEqual([{ component_type: "Semantic", properties: {} }]);
  });
});
