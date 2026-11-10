import { describe, expect, it } from "vitest";
import type { BrowserRequest, ComponentType, WorldComponent, WorldFile } from "../../contracts/types";
import { applyAabbRequest, applyCaptureRequest, bytesToBase64, float32ToBase64, pngBase64, type CaptureIdentity, type CaptureView, type SensorBox, type SensorDepth, type SensorImage } from "./capture";
import { WorldError, parseWorld, type World } from "./world";

const WIDTH = 4;
const HEIGHT = 2;
/** §9.1: the identity the app side supplies; the four-tuple comes from the world under test. */
const IDENTITY: CaptureIdentity = { browserID: "browser-1", sceneDigest: "d".repeat(64), playback_state: "stopped" };
const FRAME_IDENTITY = {
  browserID: "browser-1", scene_id: 3, scene_version: 7, scene_sub_version: 0,
  sceneDigest: "d".repeat(64), playback_state: "stopped",
};

function component(component_type: ComponentType, properties: Record<string, unknown>): WorldComponent {
  return { component_type, properties };
}

function cameraObject(id: number, properties: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    object_id_in_scene: id,
    object_name_in_scene: `cam${id}`,
    components: [
      component("Transform", { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: null }),
      component("Camera", { type: "rgbd", fov: 60, near: 0.1, far: 100, resolution: [WIDTH, HEIGHT], ...properties }),
    ],
  };
}

function doc(objects: Record<string, unknown>[], meta: Partial<WorldFile> = {}): Record<string, unknown> {
  return { scene_schema_version: 1, scene_version: 7, scene_id: 3, scene_name: "capture", objects, ...meta };
}

/** One `View` per test: what the renderer and the play bar owe a capture. */
function view(overrides: Partial<CaptureView> = {}): CaptureView {
  const image: SensorImage = { width: WIDTH, height: HEIGHT, base64: "aW1hZ2U=" };
  const meters = new Float32Array(WIDTH * HEIGHT);
  meters.fill(2.5);
  const depth: SensorDepth = { width: WIDTH, height: HEIGHT, meters };
  return { image: () => image, depth: () => depth, box: () => box(), frameIndex: () => null, identity: () => IDENTITY, ...overrides };
}

/** The §5.11 measurement of a 0.08 m cube centred 2 m along the optical axis. */
function box(overrides: Partial<SensorBox> = {}): SensorBox {
  return {
    width: WIDTH, height: HEIGHT,
    aabb: { min: [1.96, -0.04, -0.04], max: [2.04, 0.04, 0.04] },
    visible: true, clipped: false,
    box: { x_min: 1.2, y_min: 0.4, x_max: 2.8, y_max: 1.6 },
    ...overrides,
  };
}

function request(payload: Record<string, unknown>): BrowserRequest {
  return { type: "browser.request", request_id: "req-1", action: "captureScene", payload };
}

function capture(payload: Record<string, unknown>, world: World = parseWorld(doc([cameraObject(2)])), overrides: Partial<CaptureView> = {}) {
  return applyCaptureRequest(world, request(payload), view(overrides));
}

function cameraWorld(type: "rgb" | "rgbd" = "rgbd"): World {
  return parseWorld(doc([cameraObject(2, { type })]));
}

function expectCode(payload: Record<string, unknown>, code: string, status: number, world?: World, overrides?: Partial<CaptureView>): void {
  try {
    capture(payload, world ?? cameraWorld(), overrides ?? {});
    expect.unreachable(`${code} did not fire`);
  } catch (error) {
    expect(error).toBeInstanceOf(WorldError);
    expect((error as WorldError).code).toBe(code);
    expect((error as WorldError).status).toBe(status);
  }
}

describe("applyCaptureRequest (§5.10)", () => {
  it("answers an rgb capture with the §5.10 body and no depth fields", () => {
    const result = capture({ id: 2, type: "rgb" }, cameraWorld("rgb"));
    expect(result).toEqual({
      request_id: "req-1", id: 2, type: "rgb", width: WIDTH, height: HEIGHT, camera_type: "rgb",
      image_encoding: "png", image_base64: "aW1hZ2U=",
      captured_at: expect.any(String), frame_index: null, ...FRAME_IDENTITY,
    });
    expect(new Date(result.captured_at).getTime()).not.toBeNaN();
  });

  it("adds the float32 depth map for rgbd and keeps the frame the geometry was driven from", () => {
    const result = capture({ id: 2, type: "rgbd" }, parseWorld(doc([cameraObject(2)])), { frameIndex: () => 340 });
    expect(result.frame_index).toBe(340);
    expect(result.depth_encoding).toBe("float32le_meters");
    expect(result.depth_unit).toBe("meter");
    expect(result.depth_invalid).toBe("NaN");
    const raw = Buffer.from(result.depth_base64 ?? "", "base64");
    expect(raw.length).toBe(WIDTH * HEIGHT * 4);
    expect(new Float32Array(raw.buffer, raw.byteOffset, WIDTH * HEIGHT)[0]).toBe(2.5);
  });

  it("refuses a type that is not the entity's Camera.type in both directions", () => {
    expectCode({ id: 2, type: "rgb" }, "CAMERA_TYPE_MISMATCH", 400, parseWorld(doc([cameraObject(2, { type: "rgbd" })])));
    expectCode({ id: 2, type: "rgbd" }, "CAMERA_TYPE_MISMATCH", 400, parseWorld(doc([cameraObject(2, { type: "rgb" })])));
  });

  it("reads a Camera with no type as rgb (§2.6)", () => {
    const state = parseWorld(doc([cameraObject(2, { type: undefined })]));
    expect(capture({ id: 2, type: "rgb" }, state).camera_type).toBe("rgb");
    expectCode({ id: 2, type: "rgbd" }, "CAMERA_TYPE_MISMATCH", 400, state);
  });

  it("reports an object without a Camera as 404", () => {
    const state = parseWorld(doc([
      cameraObject(2),
      { object_id_in_scene: 5, object_name_in_scene: "apple", components: [component("Transform", { position: [0, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: null })] },
    ]));
    expectCode({ id: 5, type: "rgb" }, "CAMERA_NOT_FOUND", 404, state);
    expectCode({ id: 99, type: "rgb" }, "CAMERA_NOT_FOUND", 404, state);
  });

  it("validates the params the way §5.13 does", () => {
    expectCode({ type: "rgb" }, "MISSING_PARAM", 400);
    expectCode({ id: -1, type: "rgb" }, "INVALID_PARAM", 400);
    expectCode({ id: 1.5, type: "rgb" }, "INVALID_PARAM", 400);
    expectCode({ id: "2", type: "rgb" }, "INVALID_PARAM", 400);
    expectCode({ id: 2 }, "MISSING_PARAM", 400);
    expectCode({ id: 2, type: "depth" }, "INVALID_PARAM", 400);
  });

  it("turns a failed render into 502 UPSTREAM_RENDER_FAILED", () => {
    expectCode({ id: 2, type: "rgb" }, "UPSTREAM_RENDER_FAILED", 502, cameraWorld("rgb"), { image: () => null });
    expectCode({ id: 2, type: "rgbd" }, "UPSTREAM_RENDER_FAILED", 502, undefined, { depth: () => null });
  });

  it("refuses a depth map that does not match the image size", () => {
    expectCode({ id: 2, type: "rgbd" }, "UPSTREAM_RENDER_FAILED", 502, undefined, {
      depth: () => ({ width: WIDTH / 2, height: HEIGHT, meters: new Float32Array(WIDTH * HEIGHT / 2) }),
    });
  });
});

describe("applyAabbRequest (§5.11)", () => {
  function boxWorld(): World {
    return parseWorld(doc([
      cameraObject(2),
      { object_id_in_scene: 5, object_name_in_scene: "apple", components: [component("Transform", { position: [2, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: null })] },
    ]));
  }

  function aabb(payload: Record<string, unknown>, world: World = boxWorld(), overrides: Partial<CaptureView> = {}) {
    return applyAabbRequest(world, { type: "browser.request", request_id: "req-2", action: "get2DAABB", payload }, view(overrides));
  }

  function expectBoxCode(payload: Record<string, unknown>, code: string, status: number, world?: World, overrides?: Partial<CaptureView>): void {
    try {
      aabb(payload, world ?? boxWorld(), overrides ?? {});
      expect.unreachable(`${code} did not fire`);
    } catch (error) {
      expect(error).toBeInstanceOf(WorldError);
      expect((error as WorldError).code).toBe(code);
      expect((error as WorldError).status).toBe(status);
    }
  }

  it("answers with the §5.11 body", () => {
    const state = boxWorld();
    expect(aabb({ id: 5, camera_id: 2 }, state)).toEqual({
      request_id: "req-2", id: 5, camera_id: 2, width: WIDTH, height: HEIGHT,
      coordinate_origin: "top_left", visible: true, clipped: false,
      box: { x_min: 1.2, y_min: 0.4, x_max: 2.8, y_max: 1.6 },
      aabb_world: { min: [1.96, -0.04, -0.04], max: [2.04, 0.04, 0.04] },
      frame_index: null, ...FRAME_IDENTITY,
    });
    // A read route must not touch the world it measured (§5.9, same reason as Q33).
    expect(state.version).toBe(7);
  });

  it("keeps reporting the world box when the projection misses the frame", () => {
    const result = aabb({ id: 5, camera_id: 2 }, undefined, {
      box: () => box({ visible: false, clipped: true, box: null }),
      frameIndex: () => 340,
    });
    expect(result).toMatchObject({ visible: false, clipped: true, box: null, frame_index: 340 });
    expect(result.aabb_world?.max[0]).toBe(2.04);
  });

  it("passes a clipped rectangle through untouched", () => {
    const clipped = box({ clipped: true, box: { x_min: 0, y_min: 0, x_max: 4, y_max: 1.6 } });
    const result = aabb({ id: 5, camera_id: 2 }, undefined, { box: () => clipped });
    expect(result.box).toEqual({ x_min: 0, y_min: 0, x_max: 4, y_max: 1.6 });
  });

  it("separates an absent object from a target that is not a camera", () => {
    expectBoxCode({ id: 99, camera_id: 2 }, "OBJECT_NOT_FOUND", 404);
    // Checked first, so a request naming two unknown ids blames the object.
    expectBoxCode({ id: 99, camera_id: 98 }, "OBJECT_NOT_FOUND", 404);
    expectBoxCode({ id: 5, camera_id: 5 }, "CAMERA_NOT_FOUND", 404);
    expectBoxCode({ id: 5, camera_id: 98 }, "CAMERA_NOT_FOUND", 404);
  });

  it("reports an object with nothing to measure as 400 EMPTY_AABB", () => {
    // The renderer finds no geometry to bound, which §5.11 gives one code for.
    expectBoxCode({ id: 5, camera_id: 2 }, "EMPTY_AABB", 400, undefined, { box: () => null });
  });

  it("validates both ids the way §5.13 does", () => {
    expectBoxCode({ camera_id: 2 }, "MISSING_PARAM", 400);
    expectBoxCode({ id: 5 }, "MISSING_PARAM", 400);
    expectBoxCode({ id: -1, camera_id: 2 }, "INVALID_PARAM", 400);
    expectBoxCode({ id: 5, camera_id: 0x1_0000_0000 }, "INVALID_PARAM", 400);
    expectBoxCode({ id: "5", camera_id: 2 }, "INVALID_PARAM", 400);
    expectBoxCode({ id: 5, camera_id: 2.5 }, "INVALID_PARAM", 400);
  });
});

/**
 * §9.1 (S5): the identity block. Both routes answer for the 现场 they observed, and the point of
 * the pairing is that a frame the gateway cannot match to a reading is a frame it must refuse --
 * so the six fields are taken from the world under test, never from what the request carried.
 */
describe("capture identity block (§9.1)", () => {
  const edited = () => parseWorld(doc([cameraObject(2, { type: "rgb" })], { scene_sub_version: 2 }));

  it("names the rendering tab and the four-tuple of the world it rendered", () => {
    const result = capture({ id: 2, type: "rgb" }, edited());
    expect(result).toMatchObject({
      browserID: "browser-1", scene_id: 3, scene_version: 7, scene_sub_version: 2,
      sceneDigest: "d".repeat(64), playback_state: "stopped",
    });
  });

  it("gives the box the same six fields as the frame -- one observation, one口径", () => {
    const state = parseWorld(doc([
      cameraObject(2),
      { object_id_in_scene: 5, object_name_in_scene: "apple", components: [component("Transform", { position: [2, 0, 0], quaternion_xyzw: [0, 0, 0, 1], scale: [1, 1, 1], parent: null })] },
    ], { scene_sub_version: 1 }));
    const frame = capture({ id: 2, type: "rgbd" }, state);
    const measured = applyAabbRequest(state, { type: "browser.request", request_id: "req-3", action: "get2DAABB", payload: { id: 5, camera_id: 2 } }, view());
    for (const key of ["browserID", "scene_id", "scene_version", "scene_sub_version", "sceneDigest", "playback_state"] as const) {
      expect(measured[key]).toEqual(frame[key]);
    }
  });

  it("says null rather than a stale digest when this tab has no reading that pairs", () => {
    expect(capture({ id: 2, type: "rgb" }, edited(), { identity: () => ({ ...IDENTITY, sceneDigest: null }) })).toMatchObject({ sceneDigest: null });
  });

  it("carries the play bar's own word, so a frame taken mid-playback is distinguishable", () => {
    for (const playback_state of ["playing", "paused"] as const) {
      expect(capture({ id: 2, type: "rgb" }, edited(), { identity: () => ({ ...IDENTITY, playback_state }) }))
        .toMatchObject({ playback_state });
    }
  });
});

describe("§5.10 encodings", () => {
  it("base64-encodes with the standard padding rules", () => {
    const encode = (text: string) => bytesToBase64(new TextEncoder().encode(text));
    expect(encode("")).toBe("");
    expect(encode("f")).toBe("Zg==");
    expect(encode("fo")).toBe("Zm8=");
    expect(encode("foo")).toBe("Zm9v");
    expect(encode("foob")).toBe("Zm9vYg==");
    expect(encode("fooba")).toBe("Zm9vYmE=");
    expect(encode("foobar")).toBe("Zm9vYmFy");
    // 256 different bytes is the alphabet's whole value range, padding included.
    const all = new Uint8Array(256).map((_, index) => index);
    expect(Buffer.from(bytesToBase64(all), "base64")).toEqual(Buffer.from(all));
  });

  it("writes the depth map as little-endian float32 and keeps NaN", () => {
    const meters = new Float32Array([1.5, Number.NaN, -0.25, 4096.125]);
    const encoded = float32ToBase64(meters);
    const raw = Buffer.from(encoded, "base64");
    expect(raw).toEqual(Buffer.from(meters.buffer));
    expect(raw.length).toBe(16);
    const back = new Float32Array(raw.buffer, raw.byteOffset, 4);
    expect(back[0]).toBe(1.5);
    expect(Number.isNaN(back[1])).toBe(true);
    expect(back[2]).toBeCloseTo(-0.25, 5);
    expect(back[3]).toBeCloseTo(4096.125, 3);
  });

  it("strips the png data url prefix and rejects anything else", () => {
    expect(pngBase64("data:image/png;base64,Zm9v")).toBe("Zm9v");
    // A zero-sized canvas comes back as the empty-string data url in some engines.
    expect(() => pngBase64("data:")).toThrow(WorldError);
    try {
      pngBase64("data:image/jpeg;base64,Zm9v");
      expect.unreachable();
    } catch (error) {
      expect((error as WorldError).code).toBe("UPSTREAM_RENDER_FAILED");
    }
  });
});
