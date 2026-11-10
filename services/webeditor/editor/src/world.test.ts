import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ObjectRequest, WorldComponent, WorldFile } from "../../contracts/types";
import { parseWorld, World, WorldError } from "./world";

const fixtureUrl = new URL("../../tests/fixtures/scene_example.json", import.meta.url);
const sampleWorld = () => JSON.parse(readFileSync(fixtureUrl, "utf-8")) as WorldFile;

/** Deliberately loose: several cases feed invalid entries on purpose. */
interface RawObject {
  object_id_in_scene?: number;
  object_name_in_scene?: string;
  components?: WorldComponent[];
}

function transform(parent?: number | null, extra: Record<string, unknown> = {}): WorldComponent {
  return {
    component_type: "Transform",
    properties: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: parent ?? null, ...extra },
  };
}

function doc(objects: readonly unknown[], meta: Partial<WorldFile> = {}): Record<string, unknown> {
  return {
    scene_schema_version: 1,
    scene_version: 1,
    scene_id: 7,
    scene_name: "unit",
    objects,
    ...meta,
  };
}

function object(id: number, parent: number | null, name = `o${id}`): RawObject {
  return { object_id_in_scene: id, object_name_in_scene: name, components: [transform(parent)] };
}

function emptyWorld(version = 1): World {
  return parseWorld(doc([], { scene_version: version }));
}

function expectCode(action: () => unknown, code: string, status = 400): void {
  expect(action).toThrow(WorldError);
  try {
    action();
  } catch (error) {
    expect((error as WorldError).code).toBe(code);
    expect((error as WorldError).status).toBe(status);
  }
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

describe("parseWorld", () => {
  it("loads the shipped scene fixture with parents, camera, light and robot driver", () => {
    const world = parseWorld(sampleWorld());
    expect(world.objects.map((node) => node.id)).toEqual([0, 1, 2, 3, 5]);
    expect(world.get(1)?.transform.parent).toBe(0);
    expect(world.get(2)?.transform.parent).toBe(0);
    expect(world.childrenOf(0)).toEqual([1, 2]);
    expect(world.roots().map((node) => node.id)).toEqual([0, 3, 5]);
    expect(world.camera(2)).toMatchObject({ type: "rgbd", fov: 60, near: 0.1, far: 1000, resolution: [640, 480] });
    expect(world.get(1)?.light).toMatchObject({ type: "Ambient", intensity: 0.5 });
    expect(world.robotDriver(3)?.joint_sequence).toHaveLength(6);
    expect(world.get(3)?.metadataRef).toEqual({ path: "$LIBRARY_SERVER/1" });
    expect(world.meta).toMatchObject({ scene_id: 1, scene_name: "Sample Scene", scene_version: 1 });
  });

  /**
   * §9 item 3: the candidate list is a component read, not a name read. UR5e's empty template
   * really does carry two cameras, so the list has to say both -- picking "the first one" here is
   * the failure the spec names, and it would be invisible in a single-camera fixture.
   */
  it("lists camera objects by component, in document order, and picks none of them", () => {
    expect(parseWorld(sampleWorld()).cameraObjectIds()).toEqual([2]);
    const cameraComponent = (): WorldComponent => ({
      component_type: "Camera",
      properties: { type: "rgb", fov: 60, near: 0.1, far: 100, resolution: [640, 480] },
    });
    const cameraObject = (id: number): RawObject => ({
      object_id_in_scene: id, object_name_in_scene: `cam${id}`, components: [transform(null), cameraComponent()],
    });
    const two = parseWorld(doc([cameraObject(5), cameraObject(4), object(6, null, "door")]));
    expect(two.cameraObjectIds()).toEqual([5, 4]);
    // A Camera with no properties dictionary still counts: §2.6 reads an absent `type` as `rgb`,
    // and `world.camera(id)` returns that same empty dictionary, so list and capture agree.
    const bare = parseWorld(doc([{ object_id_in_scene: 7, object_name_in_scene: "bare",
      components: [transform(null), { component_type: "Camera", properties: null as unknown as Record<string, unknown> }] }]));
    expect(bare.cameraObjectIds()).toEqual([7]);
  });

  it("keeps every raw component dictionary untouched, including undefined fields (§3.8)", () => {
    const world = parseWorld(sampleWorld());
    const cube = world.get(0);
    expect(cube?.semantic).toMatchObject({ semantic_name: "cube", category: "object", support: 5 });
    expect(cube?.rawComponents.map((component) => component.component_type))
      .toEqual(["Transform", "MetadataRef", "MeshRenderer", "Semantic"]);
    const table = world.get(5)?.rawComponents.find((component) => component.component_type === "Semantic");
    expect(table?.properties.support).toBeNull();
    expect(world.toJSON()).toEqual(sampleWorld());
  });

  it("rejects duplicated ids and keeps the first occurrence", () => {
    const world = parseWorld(doc([object(1, null, "first"), object(1, null, "second"), object(2, 1)]));
    expect(world.objects.map((node) => node.id)).toEqual([1, 2]);
    expect(world.get(1)?.name).toBe("first");
    expect(world.childrenOf(1)).toEqual([2]);
  });

  it("resolves forward parent references regardless of array order (§3.6)", () => {
    const world = parseWorld(doc([object(2, 1, "child"), object(1, null, "parent")]));
    expect(world.get(2)?.transform.parent).toBe(1);
    expect(world.childrenOf(1)).toEqual([2]);
  });

  it("rejects a parent cycle and the objects hanging below it", () => {
    const world = parseWorld(doc([object(1, 2, "a"), object(2, 1, "b"), object(3, 1, "descendant")]));
    expect(world.get(1)).toBeUndefined();
    expect(world.get(2)).toBeUndefined();
    expect(world.get(3)).toBeUndefined();
    expect(world.objects).toEqual([]);
  });

  it("rejects self parenting", () => {
    const world = parseWorld(doc([object(1, 1)]));
    expect(world.objects).toEqual([]);
  });

  it("drops objects whose parent does not exist", () => {
    const world = parseWorld(doc([object(1, 99)]));
    expect(world.objects).toEqual([]);
  });

  it("ignores unknown component types with a warning instead of failing the scene (§3.4)", () => {
    const unknown = { component_type: "PhysXBody", properties: { mass: 2 } };
    const world = parseWorld(doc([{ ...object(1, null), components: [transform(null), unknown] }]));
    expect(world.objects).toHaveLength(1);
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('unknown component_type "PhysXBody"'));
    expect(world.get(1)?.rawComponents).toContainEqual(unknown);
  });

  it("falls back to the identity pose when Transform is missing or malformed", () => {
    const world = parseWorld(doc([
      { object_id_in_scene: 1, object_name_in_scene: "no transform", components: [] },
      { object_id_in_scene: 2, object_name_in_scene: "bad scale", components: [transform(null, { scale: [1, 0, 2], position: [1, 2] })] },
    ]));
    expect(world.get(1)?.transform).toEqual({ position: [0, 0, 0], quaternion: [0, 0, 0, 1], scale: [1, 1, 1], parent: null });
    expect(world.get(2)?.transform.scale).toEqual([1, 1, 1]);
    expect(world.get(2)?.transform.position).toEqual([0, 0, 0]);
  });

  it("accepts non contiguous ids and an empty object list", () => {
    expect(parseWorld(doc([])).objects).toEqual([]);
    const world = parseWorld(doc([object(9, null), object(120, null)]));
    expect(world.objectCount).toBe(2);
  });

  it("refuses ids that are not uint32 (§2.1)", () => {
    const world = parseWorld(doc([
      { object_id_in_scene: undefined, components: [transform()] },
      { object_id_in_scene: -1, components: [transform()] },
      { object_id_in_scene: 1.5, components: [transform()] },
      { object_id_in_scene: "3", components: [transform()] },
      object(4, null),
    ]));
    expect(world.objects.map((node) => node.id)).toEqual([4]);
  });

  it("maps the 0xFFFFFFFF root sentinel to parent null (§2.5)", () => {
    const world = parseWorld(doc([{ ...object(1, null), components: [transform(0xffffffff)] }]));
    expect(world.get(1)?.transform.parent).toBeNull();
    expect(world.roots().map((node) => node.id)).toEqual([1]);
  });

  it("rejects malformed world documents with INVALID_WORLD_SCHEMA", () => {
    for (const bad of [null, [], "world", {}, { ...doc([]), objects: "none" }, { ...doc([]), scene_schema_version: 2 }]) {
      expect(() => parseWorld(bad)).toThrow(WorldError);
    }
    expect(() => parseWorld({ ...doc([]), scene_schema_version: 2 })).toThrow(/scene_schema_version/);
  });
});

describe("the real scene file (docs/ur5e_table_empty.json)", () => {
  const realWorld = () =>
    JSON.parse(readFileSync(new URL("../../docs/ur5e_table_empty.json", import.meta.url), "utf-8")) as WorldFile;

  it("keeps every object and reads the id space each MetadataRef names (§4.2)", () => {
    const world = parseWorld(realWorld());
    expect(world.objects.map((node) => node.id)).toEqual([0, 7, 1, 2, 3, 4, 5, 6]);
    expect(world.get(1)?.metadataRef).toEqual({ path: "$LIBRARY_SERVER/29", type: "model" });
    expect(world.get(2)?.metadataRef).toEqual({ path: "$LIBRARY_SERVER/1", type: "robot" });
    // The cameras and the pedestal are robots 3 and 4, not models 3 and 4 (§6.11).
    expect([4, 5, 6].map((id) => world.get(id)?.metadataRef)).toEqual([
      { path: "$LIBRARY_SERVER/3", type: "robot" },
      { path: "$LIBRARY_SERVER/3", type: "robot" },
      { path: "$LIBRARY_SERVER/4", type: "robot" },
    ]);
  });

  it("mounts the gripper where its attachment says and parks the arm (§4.7, §4.8)", () => {
    const world = parseWorld(realWorld());
    expect(world.get(3)?.transform).toEqual({
      position: [2.679489213841535e-09, 0.0999999837652335, 9.719897775539721e-09],
      quaternion: [-0.7071067556514947, 9.473426349705847e-09, -9.47342713345485e-09, 0.7071068067215996],
      scale: [1, 1, 1],
      parent: 2,
    });
    // The pose is an offset in `wrist_3_link`'s own frame, so the view hangs it there.
    expect(world.get(3)?.attachmentMount).toEqual({ objectId: 2, link: "wrist_3_link" });
    // The wrist camera rides the gripper, so it keeps the plain Transform parent.
    expect(world.get(4)?.attachmentMount).toBeUndefined();
    expect(world.robotDriver(2)?.initial_joint_positions_rad).toMatchObject({ shoulder_lift_joint: -2.094395, elbow_joint: 1.570796 });
    expect(world.robotDriver(2)?.available_connect_joints).toEqual(["wrist_3_joint"]);
    // Saving what was loaded must not lose the mount: the component list is byte-identical.
    expect(world.toJSON()).toEqual(realWorld());
  });
});

describe("MetadataRef and the OLS id space (§4.2)", () => {
  function ref(id: number, type?: string): WorldComponent {
    return { component_type: "MetadataRef", properties: { path: `$LIBRARY_SERVER/${id}`, ...(type ? { type } : {}) } };
  }

  function oneOf(components: WorldComponent[]): World {
    return parseWorld(doc([{ ...object(1, null), components }]));
  }

  it("carries type through to the renderer, because model 3 and robot 3 are different assets", () => {
    expect(oneOf([transform(), ref(3, "robot")]).get(1)?.metadataRef).toEqual({ path: "$LIBRARY_SERVER/3", type: "robot" });
    expect(oneOf([transform(), ref(29, "model")]).get(1)?.metadataRef).toEqual({ path: "$LIBRARY_SERVER/29", type: "model" });
  });

  it("warns about a type that names neither id space and lets the path decide", () => {
    const world = oneOf([transform(), { component_type: "MetadataRef", properties: { path: "$LIBRARY_SERVER/robot?id=3", type: "urdf" } }]);
    expect(world.get(1)?.metadataRef).toEqual({ path: "$LIBRARY_SERVER/robot?id=3" });
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('MetadataRef type "urdf" is neither "model" nor "robot"'));
  });
});

describe("RobotAttachment (场景文件描述 §4.8)", () => {
  /** The gripper's mount as the re-exported scene writes it: 0.1 m along the wrist link's +Y. */
  const MOUNT = {
    position: [2.679489213841535e-09, 0.0999999837652335, 9.719897775539721e-09],
    quaternion_xyzw: [-0.7071067556514947, 9.473426349705847e-09, -9.47342713345485e-09, 0.7071068067215996],
    reference_frame: "parent_link",
  };

  function gripper(components: WorldComponent[]): World {
    return parseWorld(doc([object(2, null, "arm"), { ...object(3, 2, "gripper"), components }]));
  }

  function attachment(parentId?: unknown, transform?: unknown): WorldComponent {
    return {
      component_type: "RobotAttachment",
      properties: { parent_joint: "wrist_3_joint", parent_link: "wrist_3_link", child_link: "robotiq_base_link", ...(parentId === undefined ? {} : { parent_object_id: parentId }), ...(transform === undefined ? {} : { transform }) },
    };
  }

  it("replaces the Transform position and quaternion and keeps its scale and parent", () => {
    const world = gripper([transform(2, { position: [9, 9, 9], quaternion_xyzw: [1, 0, 0, 0], scale: [2, 2, 2] }), attachment(2, MOUNT)]);
    expect(world.get(3)?.transform).toEqual({
      position: MOUNT.position,
      quaternion: MOUNT.quaternion_xyzw,
      scale: [2, 2, 2],
      parent: 2,
    });
    expect(console.warn).not.toHaveBeenCalled();
  });

  it("records the mount as the link its reference_frame names (§4.8)", () => {
    const world = gripper([transform(2), attachment(2, MOUNT)]);
    expect(world.get(3)?.attachmentMount).toEqual({ objectId: 2, link: "wrist_3_link" });
    // The exporter always writes the frame; leaving it out means the same thing.
    const noFrame = gripper([transform(2), attachment(2, { position: MOUNT.position, quaternion_xyzw: MOUNT.quaternion_xyzw })]);
    expect(noFrame.get(3)?.attachmentMount).toEqual({ objectId: 2, link: "wrist_3_link" });
    expect(console.warn).not.toHaveBeenCalled();
  });

  it("mounts on the parent object for a frame it does not know, and says so", () => {
    const world = gripper([transform(2), attachment(2, { ...MOUNT, reference_frame: "world" })]);
    expect(world.get(3)?.attachmentMount).toEqual({ objectId: 2, link: null });
    expect(world.get(3)?.transform.position).toEqual(MOUNT.position);
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('reference_frame "world" is not a known frame'));

    const bare = gripper([transform(2), { component_type: "RobotAttachment", properties: { parent_object_id: 2, transform: MOUNT } }]);
    expect(bare.get(3)?.attachmentMount).toEqual({ objectId: 2, link: null });
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("names no parent_link"));
  });

  it("supplies the whole pose when the Transform carries only scale and parent, as the real file does", () => {
    const world = gripper([{ component_type: "Transform", properties: { scale: [1, 1, 1], parent: 2 } }, attachment(2, MOUNT)]);
    expect(world.get(3)?.transform.position).toEqual(MOUNT.position);
    expect(world.get(3)?.transform.parent).toBe(2);
  });

  it("only replaces what it carries: a position without a quaternion leaves the file's orientation", () => {
    const world = gripper([transform(2, { position: [9, 9, 9], quaternion_xyzw: [0, 0, 0.7071, 0.7071] }), attachment(2, { position: [0, 0, 0.1] })]);
    expect(world.get(3)?.transform).toMatchObject({ position: [0, 0, 0.1], quaternion: [0, 0, 0.7071, 0.7071] });
  });

  it("steps aside for the plain Transform when the two disagree about the parent", () => {
    const world = gripper([transform(2, { position: [0, 0, 0.5] }), attachment(4, MOUNT)]);
    expect(world.get(3)?.transform.position).toEqual([0, 0, 0.5]);
    // The component stays on the record: dropping it would rewrite the user's scene on save.
    expect(world.get(3)?.attachment).toBeDefined();
    expect(world.get(3)?.attachmentMount).toBeUndefined();
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("RobotAttachment parent_object_id 4 disagrees with Transform.parent 2"));
  });

  it("steps aside for a parent it cannot read or a pose it does not carry", () => {
    const notAnId = gripper([transform(2, { position: [0, 0, 0.5] }), attachment("2", MOUNT)]);
    expect(notAnId.get(3)?.transform.position).toEqual([0, 0, 0.5]);
    expect(notAnId.get(3)?.attachmentMount).toBeUndefined();
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('parent_object_id "2" is not a uint32'));

    const noPose = gripper([transform(2, { position: [0, 0, 0.5] }), attachment(2)]);
    expect(noPose.get(3)?.transform.position).toEqual([0, 0, 0.5]);
    expect(noPose.get(3)?.attachmentMount).toBeUndefined();
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("RobotAttachment without a transform position or quaternion"));
  });

  it("keeps the attachment in rawComponents so the scene round-trips untouched (§3.8)", () => {
    const component = attachment(2, MOUNT);
    const world = gripper([transform(2), component]);
    expect(world.get(3)?.rawComponents).toContainEqual(component);
    expect(world.toJSON().objects[1]!.components).toContainEqual(component);
  });
});

/**
 * §4.9 (**Q130**): the ninth component type. The parse layer owns the two questions a scene file
 * asks - does this type stop being "unknown", and where do the warnings go - while the numbers
 * themselves are `localAxesRig`'s, tested in `logic.test.ts`.
 */
describe("LocalAxes (场景文件描述 §4.9, Q130)", () => {
  it("is a known type with its own typed view, and still keeps the dictionary verbatim", () => {
    const component = { component_type: "LocalAxes", properties: { length: 0.2, show_labels: true } };
    const world = parseWorld(doc([{ ...object(1, null), components: [transform(null), component] }]));
    expect(world.get(1)?.localAxes).toEqual({ length: 0.2, show_labels: true });
    expect(console.warn).not.toHaveBeenCalled();
    expect(world.get(1)?.rawComponents).toContainEqual(component);
  });

  it("keeps the first of two blocks, as every repeated type does (§3.4)", () => {
    const world = parseWorld(doc([{ ...object(1, null), components: [
      transform(null),
      { component_type: "LocalAxes", properties: { length: 0.2 } },
      { component_type: "LocalAxes", properties: { length: 0.5, link: "tool0" } },
    ] }]));
    expect(world.get(1)?.localAxes).toEqual({ length: 0.2 });
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("duplicate LocalAxes component, keeping the first one"));
  });

  it("routes a field the file got wrong through the per-object warning sink", () => {
    parseWorld(doc([{ ...object(1, null), components: [
      transform(null), { component_type: "LocalAxes", properties: { length: 0, colors: { y: "chartreuse" } } },
    ] }]));
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("[world] object 1: LocalAxes length 0 is not a positive number, using 0.1"));
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('LocalAxes colors.y "chartreuse" is not a hex colour, using #00FF00'));
  });

  it("routes a bad offset through the same warning sink", () => {
    parseWorld(doc([{ ...object(1, null), components: [
      transform(null), { component_type: "LocalAxes", properties: { offset: [0.1, "bad", 0.3] } },
    ] }]));
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('[world] object 1: LocalAxes offset[1] "bad" is not a finite number, using [0,0,0]'));
  });

  it("says nothing about an object that does not declare it", () => {
    expect(parseWorld(doc([object(1, null)])).get(1)?.localAxes).toBeUndefined();
    expect(console.warn).not.toHaveBeenCalled();
  });

  it("round-trips through toJSON, so an export carries the component back out", () => {
    const component = { component_type: "LocalAxes", properties: { length: 0.3 } };
    const world = parseWorld(doc([{ ...object(1, null), components: [transform(null), component] }]));
    expect(world.toJSON().objects[0]!.components).toContainEqual(component);
  });
});

describe("World mutation", () => {
  it("applies a whole world and bumps the version by one over the one it held", () => {
    const world = emptyWorld(4);
    const result = world.applyWorld(sampleWorld());
    expect(result).toMatchObject({ ok: true, scene_id: 1, scene_version: 5, object_count: 5, rejected_objects: [] });
    expect(world.toJSON().scene_version).toBe(5);
  });

  it("reports rejected objects without aborting the apply (§3.6)", () => {
    const world = emptyWorld(1);
    const result = world.applyWorld(doc([object(1, null), { object_id_in_scene: 2, components: [transform(77)] }]));
    expect(result.object_count).toBe(1);
    expect(result.rejected_objects).toEqual([{ object_id_in_scene: 2, index: 1, reason: "PARENT_NOT_FOUND:77" }]);
  });

  it("lands nothing when a strict candidate has any rejection (流程文档 §8.1)", () => {
    const world = emptyWorld(1);
    const before = world.toJSON();
    const result = world.applyWorld(doc([object(1, null), object(2, 99)], { scene_version: 4 }), undefined, true);
    expect(result).toEqual({
      ok: true,
      scene_id: 7,
      scene_version: 1,
      object_count: 0,
      rejected_objects: [{ object_id_in_scene: 2, index: 1, reason: "PARENT_NOT_FOUND:99" }],
    });
    expect(world.toJSON()).toEqual(before);
  });

  it("does not let a refused strict candidate spend an id (§5.5)", () => {
    const world = emptyWorld(1);
    world.applyWorld(doc([object(1, null), object(50, 99)]), undefined, true);
    expect(world.addObject({ object_name_in_scene: "Cup", components: [transform(null)] }).object_id_in_scene).toBe(0);
  });

  it("applies a strict candidate in one step when nothing is rejected", () => {
    const world = emptyWorld(1);
    // §8.1: a commit candidate is exactly one version past the 现场, and the answer has to name
    // that same version -- a Gateway that sees a different one quarantines the lease.
    const result = world.applyWorld(doc([object(1, null), object(2, 1)], { scene_version: 2 }), undefined, true);
    expect(result).toMatchObject({ scene_version: 2, object_count: 2, rejected_objects: [] });
    expect(world.objects.map((node) => node.id)).toEqual([1, 2]);
  });

  it("pulls a viewer forward by one when the world it adopts is already behind it", () => {
    const world = emptyWorld(5);
    expect(world.applyWorld(doc([object(1, null)], { scene_version: 2 })).scene_version).toBe(6);
  });

  it("keeps ordinary updateWorld versioning relative to the live world", () => {
    const world = emptyWorld(5);
    expect(world.applyWorld(doc([object(1, null)], { scene_version: 99 })).scene_version).toBe(6);
  });

  it("names the drop path of every rejected object, not only the first one (§3.6)", () => {
    const result = emptyWorld(1).applyWorld(doc([
      object(1, null, "kept"),
      object(1, null, "duplicate"),
      null,
      object(3, 99),
      object(4, 3),
    ]));

    expect(result.object_count).toBe(1);
    // The malformed-entry rejections come from the first pass, the hierarchy ones after
    // it, so a caller can tell "this entry was never an object" from "its parent fell".
    expect(result.rejected_objects).toEqual([
      { object_id_in_scene: null, index: 2, reason: "OBJECT_NOT_AN_OBJECT" },
      { object_id_in_scene: 1, index: 1, reason: "DUPLICATE_OBJECT_ID" },
      { object_id_in_scene: 3, index: 3, reason: "PARENT_NOT_FOUND:99" },
      { object_id_in_scene: 4, index: 4, reason: "PARENT_REJECTED:3" },
    ]);
  });

  it("allocates ids above the watermark and never reuses a released one (§5.5)", () => {
    const world = parseWorld(doc([object(0, null), object(4, null)]));
    expect(world.addObject({ object_name_in_scene: "Cup", components: [transform(null)] }).object_id_in_scene).toBe(5);
    world.removeObject(5);
    expect(world.addObject({ object_name_in_scene: "Cup2", components: [transform(null)] }).object_id_in_scene).toBe(6);
  });

  it("refuses an id that already exists", () => {
    const world = parseWorld(doc([object(0, null)]));
    expectCode(() => world.addObject({ object_id_in_scene: 0, object_name_in_scene: "Dup", components: [transform(null)] }),
      "ID_ALREADY_EXISTS", 409);
  });

  it("requires a Transform component when adding an object", () => {
    const world = emptyWorld();
    expectCode(() => world.addObject({ object_name_in_scene: "Bare", components: [] }), "MISSING_TRANSFORM_COMPONENT");
  });

  it("replaces everything but the id on updateItem (§4.2)", () => {
    const world = parseWorld(sampleWorld());
    const components = [transform(2), { component_type: "MeshRenderer", properties: { forceOverrideColor: "#FF0000" } }];
    const result = world.updateItem(5, { object_name_in_scene: "Table2", components });
    expect(result.scene_version).toBe(2);
    const table = world.get(5);
    expect(table?.name).toBe("Table2");
    expect(table?.transform.parent).toBe(2);
    expect(table?.semantic).toBeUndefined();
    expect(table?.rawComponents).toEqual(components);
  });

  it("rejects updateItem on id mismatch, unknown id and cyclic parent", () => {
    const world = parseWorld(sampleWorld());
    expectCode(() => world.updateItem(5, { object_id_in_scene: 3, object_name_in_scene: "x", components: [transform(null)] }), "BODY_ID_MISMATCH");
    expectCode(() => world.updateItem(42, { object_name_in_scene: "x", components: [transform(null)] }), "OBJECT_NOT_FOUND", 404);
    // Light1 (1) hangs below Cube (0), so making 0 a child of 1 closes a cycle.
    expectCode(() => world.updateItem(0, { object_name_in_scene: "Cube", components: [transform(1)] }), "INVALID_PARENT_CYCLE");
    expectCode(() => world.updateItem(0, { object_name_in_scene: "Cube", components: [] }), "MISSING_TRANSFORM_COMPONENT");
    expect(world.version).toBe(1);
    expect(world.get(0)?.transform.parent).toBeNull();
  });

  it("cascades by default and detaches children on request (§5.14)", () => {
    const cascade = parseWorld(doc([object(1, null, "Table"), object(2, 1, "Cup"), object(3, 2, "Pen"), object(4, null, "Other")]));
    const removed = cascade.removeObject(1);
    expect(removed.removed_object_ids.sort()).toEqual([1, 2, 3]);
    expect(removed.reparented_object_ids).toEqual([]);
    expect(cascade.objects.map((node) => node.id)).toEqual([4]);

    const detach = parseWorld(doc([object(1, null, "Table"), object(2, 1, "Cup"), object(3, 2, "Pen"), object(4, null, "Other")]));
    const detached = detach.removeObject(1, true);
    expect(detached.removed_object_ids).toEqual([1]);
    expect(detached.reparented_object_ids).toEqual([2]);
    expect(detach.get(2)?.transform.parent).toBeNull();
    expect(detach.get(3)?.transform.parent).toBe(2);
    expect(detach.toJSON().objects.find((entry) => entry.object_id_in_scene === 2)).toMatchObject({
      components: [{ component_type: "Transform", properties: { parent: null } }],
    });
  });

  it("re-parents only the direct children when detaching (§5.14)", () => {
    const world = parseWorld(doc([object(1, null, "Root"), object(2, 1, "Mid"), object(3, 2, "Leaf"), object(4, 3, "Tip")]));
    const result = world.removeObject(2, true);
    expect(result.removed_object_ids).toEqual([2]);
    expect(result.reparented_object_ids).toEqual([3]);
    expect(world.get(3)?.transform.parent).toBe(1);
    // grandchildren keep their own parent link (§5.14 层级不变)
    expect(world.get(4)?.transform.parent).toBe(3);
  });

  it("does not bump the version when a repeated removal fails", () => {
    const world = parseWorld(doc([object(1, null)]));
    expectCode(() => world.removeObject(9), "OBJECT_NOT_FOUND", 404);
    expect(world.version).toBe(1);
    world.removeObject(1);
    expect(world.version).toBe(2);
    expect(world.objects).toEqual([]);
    expectCode(() => world.removeObject(1), "OBJECT_NOT_FOUND", 404);
    expect(world.version).toBe(2);
  });

  it("enforces expected_version as optimistic concurrency (§5.7)", () => {
    const world = parseWorld(doc([object(1, null)]));
    expectCode(() => world.addObject({ object_name_in_scene: "x", components: [transform(null)] }, 99), "VERSION_CONFLICT", 409);
    expectCode(() => world.applyWorld(doc([]), 99), "VERSION_CONFLICT", 409);
    expectCode(() => world.updateItem(1, { object_name_in_scene: "x", components: [transform(null)] }, 3), "VERSION_CONFLICT", 409);
    expect(world.version).toBe(1);
    expect(world.addObject({ object_name_in_scene: "x", components: [transform(null)] }, 1).scene_version).toBe(2);
  });
});

describe("browser-local writes (§4.8, Q77, Q114)", () => {
  /**
   * Q114's ① class, as a type-level negative control: the gizmo and the Details joint
   * input are gone, so a local pose write must not exist on the public interface at all
   * -- a half-deleted method is exactly what this locks out.
   */
  it("offers no local pose write on the public interface (§4.8.3, Q114)", () => {
    const world = parseWorld(doc([object(1, null, "Cube")]));
    // @ts-expect-error the gizmo's setLocalTransform was deleted with it
    expect(world.setLocalTransform).toBeUndefined();
    // @ts-expect-error the Details joint input's setLocalJointPosition was deleted with it
    expect(world.setLocalJointPosition).toBeUndefined();
  });

  /** The revision not moving is the whole observable contract of "never reported". */
  it("leaves scene_version alone so the next gateway write still passes its check", () => {
    const world = parseWorld(doc([object(1, null, "Cube")], { scene_version: 4 }));
    world.addLocalObject({ object_name_in_scene: "New", components: [transform(null)] });
    world.removeLocalObject(1);
    expect(world.version).toBe(4);
    // The optimistic check the gateway path enforces still sees the version the user last read.
    expect(world.addObject({ object_name_in_scene: "x", components: [transform(null)] }, 4).scene_version).toBe(5);
  });

  /**
   * §1.3 (Q114): the positive control the protocol names for a class-② entry -- 面板加一个
   * 物体 ⇒ 摘要变、`scene_version` 不变、子版本 +1. The digest itself is S2; the counter
   * half of that sentence is what this pins down.
   */
  it("advances scene_sub_version for every local add and remove (§1.2, Q109)", () => {
    const world = parseWorld(doc([object(1, null, "Cube")], { scene_version: 4 }));
    expect(world.subVersion).toBe(0);
    world.addLocalObject({ object_name_in_scene: "New", components: [transform(null)] });
    expect(world.subVersion).toBe(1);
    world.removeLocalObject(1);
    expect([world.version, world.subVersion]).toEqual([4, 2]);
  });

  /**
   * §4.8 (Q114 ②): a preset-scene switch is a class-② edit, so its four-tuple must strictly
   * advance against the world being left. Two presets that share a `scene_id` used to report a
   * non-increasing `(scene_version, scene_sub_version)` and the Gateway silently refused it -
   * a refresh then read back the pre-switch scene. The rebase bases on the previous sub-version,
   * not on whatever the incoming file carries, and forces `toJSON()` to write the key.
   */
  it("rebases the sub-version one step above the previous scene when switching presets (§4.8, Q114 ②)", () => {
    const preset = parseWorld(doc([object(1, null, "Cube")], { scene_version: 4 }));
    expect(preset.subVersion).toBe(0);
    preset.rebaseSubVersionAfterSwitch(0);
    expect([preset.version, preset.subVersion]).toEqual([4, 1]);
    expect(preset.toJSON().scene_sub_version).toBe(1);
    // Continuing the clock: a switch out of an already-edited scene moves one rung higher.
    preset.rebaseSubVersionAfterSwitch(5);
    expect(preset.subVersion).toBe(6);
    // The file's own sub-version is not trusted: the rebase is relative to the world being left.
    const carriesThree = parseWorld(doc([], { scene_version: 4, scene_sub_version: 3 }));
    carriesThree.rebaseSubVersionAfterSwitch(1);
    expect(carriesThree.subVersion).toBe(2);
  });

  /**
   * §4.6.1 (2026-11-05 穿模修复): a placement is measured against the live stage, so the object
   * is stood up first and rewritten into place once a spot is found. That whole measure-then-place
   * is ONE browser content edit - the sub-version advances once, not twice as `addLocalObject` +
   * `updateLocalItem` would - and a click that refuses leaves both the scene and the counter
   * exactly as they were.
   */
  it("records a measured placement as one sub-version step and rolls a refused one back", async () => {
    const world = parseWorld(doc([], { scene_version: 4 }));
    const at = (name: string, position: number[]): ObjectRequest => ({
      object_name_in_scene: name,
      components: [transform(null, { position })],
    });
    let settledWith: number | null = null;
    const placed = await world.placeLocal(
      at("Cube", [0, 0, 0]),
      async (id) => { settledWith = id; },
      async () => at("Cube", [0.2, -0.1, 0.6]),
    );
    // The staging object was handed to `settle` (so the renderer could build it), and the id that
    // survives is the final one - stood at the measured spot, one sub-version up from the empty world.
    expect(settledWith).toBe(placed);
    expect(world.get(placed)?.transform.position).toEqual([0.2, -0.1, 0.6]);
    expect([world.version, world.subVersion, world.objectCount]).toEqual([4, 1, 1]);

    // A refused placement throws mid-way: the staging object is torn back out and the counter
    // stays exactly where it was, so a click that lands nothing never churns the four-tuple.
    await expect(world.placeLocal(
      at("Mug", [0, 0, 0]),
      async () => undefined,
      async () => { throw new Error("桌面上放不下"); },
    )).rejects.toThrow("桌面上放不下");
    expect([world.version, world.subVersion, world.objectCount]).toEqual([4, 1, 1]);
  });

  /** §4.6: the panel swap of an arm rewrites that object, and the furniture it drops goes with it. */
  it("rewrites an object in place and cascades a local removal, both against the sub-version", () => {
    const world = parseWorld(doc([object(1, null, "Arm"), object(2, 1, "Gripper")], { scene_version: 4 }));
    world.updateLocalItem(1, {
      object_name_in_scene: "Piper",
      components: [transform(null, { position: [0.3, -0.225, 0.6] }), { component_type: "Semantic", properties: { semantic_name: "piper" } }],
    });
    expect(world.get(1)?.name).toBe("Piper");
    expect(world.get(1)?.rawComponents.map((component) => component.component_type)).toEqual(["Transform", "Semantic"]);
    // The gripper still names the object it hangs from, because the swap kept its id.
    expect(world.get(2)?.transform.parent).toBe(1);
    expect([world.version, world.subVersion]).toEqual([4, 1]);
    world.removeLocalSubtree(1);
    expect(world.objects.map((node) => node.id)).toEqual([]);
    expect([world.version, world.subVersion]).toEqual([4, 2]);
  });

  /** A refused local rewrite leaves no counter behind, exactly like the gateway path. */
  it("validates a local rewrite the way /updateItem does", () => {
    const world = parseWorld(doc([object(1, null, "Cube")], { scene_version: 4 }));
    expectCode(() => world.updateLocalItem(9, { object_name_in_scene: "x", components: [transform(null)] }), "OBJECT_NOT_FOUND", 404);
    expectCode(() => world.updateLocalItem(1, { object_name_in_scene: "x", components: [] }), "MISSING_TRANSFORM_COMPONENT", 400);
    expectCode(() => world.updateLocalItem(1, { object_id_in_scene: 2, object_name_in_scene: "x", components: [transform(null)] }), "BODY_ID_MISMATCH");
    expectCode(() => world.removeLocalSubtree(9), "OBJECT_NOT_FOUND", 404);
    expect([world.version, world.subVersion]).toEqual([4, 0]);
  });

  /** A failed local write must not leave a sub-version behind: the counter means "content moved". */
  it("does not advance the sub-version when the local write is refused", () => {
    const world = parseWorld(doc([object(1, null, "Cube")], { scene_version: 4 }));
    expectCode(() => world.removeLocalObject(9), "OBJECT_NOT_FOUND", 404);
    expectCode(() => world.addLocalObject({ object_name_in_scene: "x", components: [] }), "MISSING_TRANSFORM_COMPONENT", 400);
    expect(world.subVersion).toBe(0);
  });

  /** Semantic zero does not make absent and explicit zero identical JCS documents. */
  it("preserves the input sub-version presence and exports local edits", () => {
    const untouched = parseWorld(doc([object(1, null, "Cube")]));
    expect(untouched.toJSON().scene_sub_version).toBeUndefined();
    untouched.addLocalObject({ object_name_in_scene: "New", components: [transform(null)] });
    expect(untouched.toJSON().scene_sub_version).toBe(1);
    expect(parseWorld(doc([], { scene_sub_version: 3 })).subVersion).toBe(3);
    expect(parseWorld(doc([])).subVersion).toBe(0);
    expect(parseWorld(doc([], { scene_sub_version: 0 })).toJSON().scene_sub_version).toBe(0);
  });

  it("round-trips a strict Agent candidate with explicit sub-version zero", () => {
    const world = parseWorld(doc([]));
    const candidate = doc([object(1, null, "Cube")], { scene_version: 2, scene_sub_version: 0 });
    world.applyWorld(candidate, world.version, true);
    expect(world.toJSON()).toEqual(candidate);
    world.applyWorld(doc([], { scene_version: 3 }), world.version, true);
    expect(world.toJSON().scene_sub_version).toBeUndefined();
  });

  /** The two counters are one ladder: a `scene_version` step rebases the rungs below it. */
  it("rebases the sub-version to zero when an Agent write advances the scene (§1.2)", () => {
    const world = parseWorld(doc([object(1, null, "Cube")], { scene_version: 2 }));
    world.addLocalObject({ object_name_in_scene: "New", components: [transform(null)] });
    expect(world.subVersion).toBe(1);
    world.addObject({ object_name_in_scene: "x", components: [transform(null)] });
    expect([world.version, world.subVersion]).toEqual([3, 0]);
    world.removeLocalObject(1);
    expect(world.subVersion).toBe(1);
    expect(world.applyWorld(doc([object(8, null, "Replaced")]), 3).scene_version).toBe(4);
    expect([world.version, world.subVersion]).toEqual([4, 0]);
  });

  /** §4.7 (Q107): one microwave-shaped driver, with whatever spelling of `role` the case asks for. */
  function passiveWorld(role: unknown): World {
    return parseWorld(doc([{
      object_id_in_scene: 4, object_name_in_scene: "microwave",
      components: [transform(null), {
        component_type: "RobotDriver",
        properties: { joint_sequence: ["door_joint"], ...(role === undefined ? {} : { role }) },
      }],
    }]));
  }

  it("withdraws a passive driver from the arm decorations and from nothing else (§4.7, Q107)", () => {
    const world = passiveWorld("passive");
    expect(world.robotDriver(4)?.joint_sequence).toEqual(["door_joint"]);
    expect(world.armDriver(4)).toBeUndefined();
    // Driving and editing stay: §8.3 counts it as a robot.
    // And the field rides out through the export untouched, exactly as it came in (§3.6).
    expect(world.toJSON().objects[0]!.components[1]).toEqual({
      component_type: "RobotDriver",
      properties: { joint_sequence: ["door_joint"], role: "passive" },
    });
    expect(world.version).toBe(1);
  });

  it("reads every other spelling of role as the arm default (§4.7, Q107)", () => {
    for (const role of [undefined, "arm", "Passive", "door", 7, null, {}, []]) {
      const world = passiveWorld(role);
      expect(world.armDriver(4)?.joint_sequence).toEqual(["door_joint"]);
      expect(world.robotDriver(4)?.joint_sequence).toEqual(["door_joint"]);
    }
    expect(passiveWorld("arm").armDriver(9)).toBeUndefined();
  });

  it("allocates local ids from the watermark without reusing one it released", () => {
    const world = parseWorld(doc([object(10, null, "Old")]));
    const first = world.addLocalObject({ object_name_in_scene: "A", components: [transform(null)] });
    const second = world.addLocalObject({ object_name_in_scene: "B", components: [transform(null)] });
    expect([first, second]).toEqual([11, 12]);
    world.removeLocalObject(second);
    expect(world.addLocalObject({ object_name_in_scene: "C", components: [transform(null)] })).toBe(13);
  });

  it("requires a Transform like /addObject does", () => {
    const world = parseWorld(doc([]));
    expectCode(() => world.addLocalObject({ object_name_in_scene: "x", components: [] }), "MISSING_TRANSFORM_COMPONENT", 400);
    world.addLocalObject({ object_id_in_scene: 1, object_name_in_scene: "x", components: [transform(null)] });
    expectCode(() => world.addLocalObject({ object_id_in_scene: 1, object_name_in_scene: "x", components: [transform(null)] }), "ID_ALREADY_EXISTS", 409);
  });

  /** A light can carry children in a hand-written file; deleting it must not take them down. */
  it("detaches a local removal's children rather than cascading", () => {
    const world = parseWorld(doc([object(1, null, "Key"), object(2, 1, "Glow"), object(3, 2, "Spark")]));
    world.removeLocalObject(1);
    expect(world.objects.map((node) => node.id)).toEqual([2, 3]);
    expect(world.get(2)?.transform.parent).toBeNull();
    expect(world.get(3)?.transform.parent).toBe(2);
    expect(world.toJSON().objects.find((entry) => entry.object_id_in_scene === 2)?.components[0]).toMatchObject({
      properties: { parent: null },
    });
  });

  it("reports an unknown id instead of failing quietly", () => {
    const world = parseWorld(doc([object(1, null)]));
    expectCode(() => world.removeLocalObject(9), "OBJECT_NOT_FOUND", 404);
  });
});
