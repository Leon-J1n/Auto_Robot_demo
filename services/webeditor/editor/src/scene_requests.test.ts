import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowserAction, BrowserRequest, GetWorldResult, PlaybackState, WorldComponent, WorldFile } from "../../contracts/types";
import { applySceneRequest, applyTrajRequest, isWriteAction, type SceneRequestResult } from "./scene_requests";
import { parseWorld, World, WorldError } from "./world";

const trajFixture = new URL("../../tests/fixtures/robot_grasp.traj", import.meta.url);

function transform(parent?: number | null): WorldComponent {
  return {
    component_type: "Transform",
    properties: { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: parent ?? null },
  };
}

function doc(objects: Record<string, unknown>[], meta: Partial<WorldFile> = {}): Record<string, unknown> {
  return { scene_schema_version: 1, scene_version: 1, scene_id: 7, scene_name: "unit", objects, ...meta };
}

function object(id: number, parent: number | null, name = `o${id}`): Record<string, unknown> {
  return { object_id_in_scene: id, object_name_in_scene: name, components: [transform(parent)] };
}

function world(objects = [object(0, null), object(1, 0), object(2, 1)]): World {
  return parseWorld(doc(objects));
}

function run(state: World, action: BrowserAction, payload: Record<string, unknown> = {}, playbackState: PlaybackState = "stopped"): SceneRequestResult {
  return applySceneRequest(state, action, payload, playbackState);
}

function expectCode(state: World, action: BrowserAction, payload: Record<string, unknown>, code: string, status = 400): void {
  expect(() => run(state, action, payload)).toThrow(WorldError);
  try {
    run(state, action, payload);
  } catch (error) {
    expect((error as WorldError).code).toBe(code);
    expect((error as WorldError).status).toBe(status);
  }
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

describe("applySceneRequest.updateWorld", () => {
  it("replaces the scene and answers with the §5.9 body", () => {
    const state = world();
    const result = run(state, "updateWorld", { world: doc([object(4, null)], { scene_id: 9, scene_name: "next" }) });
    expect(result).toEqual({ ok: true, scene_id: 9, scene_version: 2, object_count: 1, rejected_objects: [] });
    expect(state.objects.map((node) => node.id)).toEqual([4]);
  });

  it("refuses a payload without a world object", () => {
    expectCode(world(), "updateWorld", {}, "INVALID_WORLD_SCHEMA");
    expectCode(world(), "updateWorld", { world: [] }, "INVALID_WORLD_SCHEMA");
  });

  it("reports 409 without applying when expected_version is stale", () => {
    const state = world();
    expectCode(state, "updateWorld", { world: doc([object(4, null)]), expected_version: 0 }, "VERSION_CONFLICT", 409);
    expect(state.objectCount).toBe(3);
  });

  it("holds a strict candidate back whole, and still applies the good subset without the flag (流程文档 §8.1)", () => {
    const candidate = doc([object(4, null), object(5, 99)]);
    const strict = world();
    expect(run(strict, "updateWorld", { world: candidate, strict: true })).toMatchObject({
      scene_version: 1,
      object_count: 3,
      rejected_objects: [{ object_id_in_scene: 5, index: 1, reason: "PARENT_NOT_FOUND:99" }],
    });
    expect(strict.objects.map((node) => node.id)).toEqual([0, 1, 2]);

    const tolerant = world();
    expect(run(tolerant, "updateWorld", { world: candidate })).toMatchObject({ scene_version: 2, object_count: 1 });
    expect(tolerant.objects.map((node) => node.id)).toEqual([4]);
  });
});

describe("applySceneRequest.updateItem", () => {
  it("replaces every component of one object (§4.2)", () => {
    const state = parseWorld(doc([{ ...object(1, null), components: [transform(null), { component_type: "Light", properties: { type: "Ambient" } }] }]));
    const result = run(state, "updateItem", {
      id: 1,
      object: { object_name_in_scene: "renamed", components: [transform(null), { component_type: "Semantic", properties: { semantic_name: "s" } }] },
    });
    expect(result).toMatchObject({ ok: true, scene_version: 2, object_count: 1 });
    expect(state.get(1)?.name).toBe("renamed");
    expect(state.get(1)?.light).toBeUndefined();
    expect(state.get(1)?.rawComponents.map((component) => component.component_type)).toEqual(["Transform", "Semantic"]);
    expect(state.get(1)?.transform.parent).toBeNull();
  });

  it("refuses a replacement without a Transform", () => {
    expectCode(world(), "updateItem", { id: 0, object: { components: [] } }, "MISSING_TRANSFORM_COMPONENT");
  });

  it("keeps an id present in the body only when it matches the query id", () => {
    const state = world();
    expect(() => run(state, "updateItem", { id: 1, object: { object_id_in_scene: 1, object_name_in_scene: "x", components: [transform(null)] } })).not.toThrow();
    expectCode(state, "updateItem", { id: 1, object: { object_id_in_scene: 2, components: [transform(null)] } }, "BODY_ID_MISMATCH");
  });

  it("answers 404 for an unknown id and 400 for a missing or malformed id", () => {
    const state = world();
    expectCode(state, "updateItem", { id: 42, object: { components: [transform(null)] } }, "OBJECT_NOT_FOUND", 404);
    expectCode(state, "updateItem", { object: { components: [transform(null)] } }, "MISSING_PARAM");
    expectCode(state, "updateItem", { id: -1, object: { components: [transform(null)] } }, "INVALID_PARAM");
    expectCode(state, "updateItem", { id: 0 }, "INVALID_PARAM");
  });
});

describe("applySceneRequest.addObject", () => {
  it("allocates max(id)+1 and returns it to the caller (§5.5)", () => {
    const state = world();
    expect(run(state, "addObject", { object: { object_name_in_scene: "new", components: [transform(null)] } }))
      .toEqual({ ok: true, object_id_in_scene: 3, scene_version: 2 });
  });

  it("never reuses a removed id", () => {
    const state = world();
    run(state, "removeObject", { id: 2 });
    expect(run(state, "addObject", { object: { object_name_in_scene: "new", components: [transform(null)] } }))
      .toMatchObject({ object_id_in_scene: 3 });
  });

  it("refuses a duplicate id, a missing Transform and an invalid parent", () => {
    const state = world();
    expectCode(state, "addObject", { object: object(1, null) }, "ID_ALREADY_EXISTS", 409);
    expectCode(state, "addObject", { object: { object_name_in_scene: "x" } }, "MISSING_TRANSFORM_COMPONENT");
    expectCode(state, "addObject", { object: object(9, 404) }, "INVALID_PARENT_CYCLE");
    expectCode(state, "addObject", {}, "INVALID_PARAM");
  });
});

describe("applySceneRequest.removeObject", () => {
  it("cascades the whole subtree by default (§5.14)", () => {
    const state = world();
    expect(run(state, "removeObject", { id: 0 })).toEqual({
      ok: true, removed_object_ids: [0, 1, 2], reparented_object_ids: [], scene_version: 2,
    });
    expect(state.objectCount).toBe(0);
  });

  it("re-parents the direct children when detach_children is set", () => {
    const state = world();
    expect(run(state, "removeObject", { id: 1, detach_children: true })).toEqual({
      ok: true, removed_object_ids: [1], reparented_object_ids: [2], scene_version: 2,
    });
    expect(state.get(2)?.transform.parent).toBe(0);
  });

  it("reports 404 for an absent id and leaves the version alone", () => {
    const state = world();
    expectCode(state, "removeObject", { id: 42 }, "OBJECT_NOT_FOUND", 404);
    expect(state.version).toBe(1);
  });
});

describe("applySceneWorld requests beyond the writes", () => {
  it("serves getWorld as the document plus the §5.1 identity beside it", () => {
    const state = world();
    const payload = run(state, "getWorld", {}, "paused") as GetWorldResult;
    expect(payload).toEqual({
      world: expect.objectContaining({ scene_id: 7, scene_name: "unit", scene_version: 1 }),
      scene_id: 7,
      scene_version: 1,
      scene_sub_version: 0,
      playback_state: "paused",
    });
    expect(payload.world.objects).toHaveLength(3);
    // §3.1: a document no class-② edit has touched omits the key, while the receipt still
    // reports the header's 0 -- the two views must not be confused for a missing sub-version.
    expect("scene_sub_version" in payload.world).toBe(false);
  });

  it("reports the sub-version a class-② edit advanced, from the same read as the document", () => {
    const state = world();
    state.addLocalObject({ object_name_in_scene: "lamp", components: [transform(null)] });
    const payload = run(state, "getWorld", {}, "stopped") as GetWorldResult;
    expect(payload.scene_sub_version).toBe(1);
    expect(payload.world.scene_sub_version).toBe(1);
    expect(payload.scene_version).toBe(state.version);
    expect(payload.scene_id).toBe(state.sceneId);
  });

  it("has no browser handler for the render actions, so they fail as 500", () => {
    for (const action of ["captureScene", "get2DAABB"] as BrowserAction[]) {
      expectCode(world(), action, {}, "INTERNAL_ERROR", 500);
    }
  });

  it("treats expected_version of the wrong type as a bad parameter", () => {
    expectCode(world(), "addObject", { object: { components: [transform(null)] }, expected_version: "3" }, "INVALID_PARAM");
  });

  it("marks only the four writes as scene-changing", () => {
    const writes: BrowserAction[] = ["updateWorld", "updateItem", "addObject", "removeObject"];
    const others: BrowserAction[] = ["getWorld", "captureScene", "get2DAABB", "updateTraj"];
    expect(writes.map(isWriteAction)).toEqual([true, true, true, true]);
    expect(others.map(isWriteAction)).toEqual([false, false, false, false]);
  });
});

describe("applyTrajRequest", () => {
  const trajBytes = () => {
    const raw = readFileSync(trajFixture);
    const copy = new ArrayBuffer(raw.byteLength);
    new Uint8Array(copy).set(raw);
    return copy;
  };

  function request(payload: Record<string, unknown>): BrowserRequest {
    return { type: "browser.request", request_id: "req-1", action: "updateTraj", payload, expects_binary: true };
  }

  /** §5.3 (Q99): the payload the gateway announces for one clip. */
  const oneClip = (buffer: ArrayBuffer) => ({ byte_length: buffer.byteLength, count: 1, byte_lengths: [buffer.byteLength] });

  function expectFailure(state: World, payload: Record<string, unknown>, buffer: ArrayBuffer, code: string, status = 400): void {
    try {
      applyTrajRequest(state, request(payload), buffer);
    } catch (error) {
      expect((error as WorldError).code).toBe(code);
      expect((error as WorldError).status).toBe(status);
      return;
    }
    throw new Error(`expected a failure with code ${code}`);
  }

  it("answers the §5.12 body when every trajectory object is on stage", () => {
    const state = world([object(1, null), object(2, null), object(3, null)]);
    const buffer = trajBytes();
    const applied = applyTrajRequest(state, request(oneClip(buffer)), buffer);
    expect(applied.result).toEqual({
      ok: true,
      count: 1,
      trajs: [{
        index: 0, fps: 60, total_frames: 720, duration_seconds: 12, object_count: 3, attach_count: 2,
        ignored_objects: [], warnings: [],
      }],
    });
    // The decoded batch is what the selector (Q100) and FK (M4-3) consume.
    expect(applied.trajs[0]?.objects[0]?.segments).toHaveLength(9);
    expect(applied.trajs[0]?.attaches).toHaveLength(2);
  });

  it("slices a concatenated batch by the announced byte_lengths, not by reading headers (§5.3)", () => {
    const clip = trajBytes();
    const buffer = new Uint8Array(clip.byteLength * 2);
    buffer.set(new Uint8Array(clip), 0);
    buffer.set(new Uint8Array(clip), clip.byteLength);
    const sliced = buffer.buffer;
    const state = world([object(1, null), object(2, null), object(3, null)]);
    const applied = applyTrajRequest(
      state,
      request({ byte_length: sliced.byteLength, count: 2, byte_lengths: [clip.byteLength, clip.byteLength] }),
      sliced,
    );
    expect(applied.trajs).toHaveLength(2);
    expect(applied.result.count).toBe(2);
    expect(applied.result.trajs.map((summary) => summary.index)).toEqual([0, 1]);
    expect(applied.result.trajs.map((summary) => summary.total_frames)).toEqual([720, 720]);
  });

  it("never touches the world: a trajectory is not a scene change (§5.2)", () => {
    const state = world([object(1, null), object(2, null), object(3, null)]);
    const buffer = trajBytes();
    applyTrajRequest(state, request(oneClip(buffer)), buffer);
    expect(state.version).toBe(1);
    expect(state.objectCount).toBe(3);
  });

  it("reports objects that are not in the scene but still answers ok (§8.2)", () => {
    const state = world([object(1, null), object(2, null)]);
    const buffer = trajBytes();
    const applied = applyTrajRequest(state, request(oneClip(buffer)), buffer);
    expect(applied.result.trajs[0]?.ignored_objects).toEqual([{ object_id: 3, reason: "OBJECT_NOT_IN_SCENE" }]);
    expect(applied.result.trajs[0]?.warnings).toEqual(["ATTACH_PARENT_MISSING@frame=530"]);
  });

  it("reports every object that is not in the scene and warns on both attaches (§8.2, §8.6)", () => {
    const buffer = trajBytes();
    // A scene that shares no id with the trajectory: playback simply has nothing to move.
    const applied = applyTrajRequest(world([object(7, null), object(8, null)]), request(oneClip(buffer)), buffer);
    expect(applied.result.trajs[0]?.ignored_objects).toEqual([
      { object_id: 1, reason: "OBJECT_NOT_IN_SCENE" },
      { object_id: 2, reason: "OBJECT_NOT_IN_SCENE" },
      { object_id: 3, reason: "OBJECT_NOT_IN_SCENE" },
    ]);
    expect(applied.result.trajs[0]?.warnings).toEqual(["ATTACH_PARENT_MISSING@frame=340", "ATTACH_PARENT_MISSING@frame=530"]);
  });

  it("fails the request when the binary frame does not match its announced length (§5.3)", () => {
    const buffer = trajBytes();
    expectFailure(world(), { ...oneClip(buffer), byte_length: buffer.byteLength - 1 }, buffer, "GATEWAY_FRAMING_ERROR", 502);
    expectFailure(world(), { ...oneClip(buffer), byte_length: buffer.byteLength + 4096 }, buffer, "GATEWAY_FRAMING_ERROR", 502);
    expectFailure(world(), {}, buffer, "INVALID_PARAM");
    expectFailure(world(), { byte_length: "12088" }, buffer, "INVALID_PARAM");
  });

  it("refuses a batch description that does not add up (§5.3, Q99)", () => {
    const buffer = trajBytes();
    const announced = oneClip(buffer);
    // count is the gateway's own tally of the parts it read.
    expectFailure(world(), { ...announced, count: 2 }, buffer, "INVALID_PARAM");
    expectFailure(world(), { byte_length: buffer.byteLength, count: 1 }, buffer, "INVALID_PARAM");
    expectFailure(world(), { ...announced, byte_lengths: [] }, buffer, "INVALID_PARAM");
    expectFailure(world(), { ...announced, count: 0 }, buffer, "INVALID_PARAM");
    // The parts must exactly fill the frame: overshoot and a trailing gap are both a
    // broken announcement, and 502 says whose fault it is (§5.13).
    expectFailure(world(), { byte_length: buffer.byteLength, count: 2, byte_lengths: [buffer.byteLength, 8] }, buffer, "GATEWAY_FRAMING_ERROR", 502);
    expectFailure(world(), { byte_length: buffer.byteLength, count: 2, byte_lengths: [100, 8] }, buffer, "GATEWAY_FRAMING_ERROR", 502);
  });

  it("fails the whole batch when one clip cannot be decoded, and says which (§5.3, Q99)", () => {
    const clip = trajBytes();
    const head = new Uint8Array(clip);
    const tail = new Uint8Array(clip.slice(0, 300));
    const buffer = new Uint8Array(head.byteLength + tail.byteLength);
    buffer.set(head, 0);
    buffer.set(tail, head.byteLength);
    const joined = buffer.buffer;
    try {
      applyTrajRequest(
        world([object(1, null), object(2, null), object(3, null)]),
        request({ byte_length: joined.byteLength, count: 2, byte_lengths: [head.byteLength, tail.byteLength] }),
        joined,
      );
      throw new Error("expected the second clip to fail");
    } catch (error) {
      expect((error as WorldError).code).toBe("TRAJ_TRUNCATED");
      // The part index is in the message, so a caller with two clips knows which to re-send.
      expect((error as WorldError).message).toContain("trajs[1]");
    }
  });
});
