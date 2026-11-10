/**
 * The browser half of the caller write APIs (spec v2.1 §4.2, §5.3, §5.5, §5.9, §5.12, §5.14).
 * The gateway relays `browser.request` frames; this is what answers them, so the
 * §5.13 codes are raised here rather than duplicated in the channel layer.
 */
import type {
  AddObjectResult,
  BrowserAction,
  BrowserRequest,
  GetWorldResult,
  ObjectRequest,
  PlaybackState,
  RemoveObjectResult,
  TrajSummary,
  UpdateTrajResult,
  WorldApplyResult,
} from "../../contracts/types";
import { isUintId, World, WorldError } from "./world";
import { decodeTraj, type Traj } from "./traj";

/** Whatever one of the five handled actions answers (§5.9, §5.10 shape for getWorld). */
export type SceneRequestResult = WorldApplyResult | AddObjectResult | RemoveObjectResult | GetWorldResult;

const WRITE_ACTIONS: BrowserAction[] = ["updateWorld", "updateItem", "addObject", "removeObject"];

/** A write changed the world, so the caller has to be told which version it produced. */
export function isWriteAction(action: BrowserAction): boolean {
  return WRITE_ACTIONS.includes(action);
}

function expectedVersion(payload: Record<string, unknown>): number | undefined {
  const raw = payload.expected_version;
  if (raw === undefined || raw === null) return undefined;
  if (!isUintId(raw)) throw new WorldError("INVALID_PARAM", `expected_version ${JSON.stringify(raw)} is not a uint32`);
  return raw;
}

function objectOf(payload: Record<string, unknown>, name: string): ObjectRequest {
  const value = payload[name];
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new WorldError("INVALID_PARAM", `${name} must be an object description`);
  }
  return value as ObjectRequest;
}

/** §5.13: `id` is a required uint32 on every camera and object route. */
export function idOf(payload: Record<string, unknown>): number {
  const raw = payload.id;
  if (raw === undefined || raw === null) throw new WorldError("MISSING_PARAM", "id is required");
  if (!isUintId(raw)) throw new WorldError("INVALID_PARAM", `id ${JSON.stringify(raw)} is not a uint32`);
  return raw;
}

/** Run one caller request against the live world and return the browser.response payload. */
export function applySceneRequest(
  world: World,
  action: BrowserAction,
  payload: Record<string, unknown>,
  playbackState: PlaybackState,
): SceneRequestResult {
  const expected = expectedVersion(payload);
  switch (action) {
    case "updateWorld": {
      const value = payload.world;
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new WorldError("INVALID_WORLD_SCHEMA", "updateWorld needs a world object");
      }
      // §8.1: a commitWorld candidate rides this same action and asks for whole-or-nothing.
      return world.applyWorld(value, expected, payload.strict === true);
    }
    case "updateItem":
      return world.updateItem(idOf(payload), objectOf(payload, "object"), expected);
    case "addObject":
      return world.addObject(objectOf(payload, "object"), expected);
    case "removeObject":
      return world.removeObject(idOf(payload), payload.detach_children === true, expected);
    case "getWorld":
      // §5.1: the gateway turns these into response headers beside the document, so one read
      // cannot report a version and a world that came from different moments.
      return {
        world: world.toJSON(),
        scene_id: world.sceneId,
        scene_version: world.version,
        scene_sub_version: world.subVersion,
        playback_state: playbackState,
      };
    default:
      throw new WorldError("INTERNAL_ERROR", `this build has no browser handler for ${action}`, 500);
  }
}

export interface TrajApplication {
  /** Decoded batch, in request part order: the play bar (M4-4) and FK (M4-3) read the active one. */
  trajs: Traj[];
  result: UpdateTrajResult;
}

/** §5.3 (Q99): the frame must say how many clips it holds and where each one starts. */
function trajectorySlice(payload: Record<string, unknown>, buffer: ArrayBuffer): ArrayBuffer[] {
  const declared = payload.byte_length;
  if (typeof declared !== "number" || !Number.isInteger(declared) || declared < 0) {
    throw new WorldError("INVALID_PARAM", "updateTraj needs a numeric payload.byte_length");
  }
  if (declared !== buffer.byteLength) {
    throw new WorldError("GATEWAY_FRAMING_ERROR", `the binary frame held ${buffer.byteLength} bytes but ${declared} were announced`, 502);
  }
  const count = payload.count;
  const lengths = payload.byte_lengths;
  if (typeof count !== "number" || !Number.isInteger(count) || count < 1) {
    throw new WorldError("INVALID_PARAM", `updateTraj needs a payload.count of 1 or more, got ${JSON.stringify(count)}`);
  }
  if (!Array.isArray(lengths) || lengths.length !== count) {
    throw new WorldError("INVALID_PARAM", `updateTraj needs payload.byte_lengths with ${count} entries`);
  }
  // The two numbers come from different places in the gateway (dispatch writes byte_length,
  // the handler writes byte_lengths), so their agreement is not self-evident here.
  let offset = 0;
  const slices: ArrayBuffer[] = [];
  for (const [index, raw] of lengths.entries()) {
    if (typeof raw !== "number" || !Number.isInteger(raw) || raw < 1) {
      throw new WorldError("INVALID_PARAM", `byte_lengths[${index}] must be a positive integer, got ${JSON.stringify(raw)}`);
    }
    if (offset + raw > buffer.byteLength) {
      throw new WorldError("GATEWAY_FRAMING_ERROR", `byte_lengths[${index}] ends at ${offset + raw}, past the ${buffer.byteLength} byte frame`, 502);
    }
    slices.push(buffer.slice(offset, offset + raw));
    offset += raw;
  }
  if (offset !== declared) {
    throw new WorldError("GATEWAY_FRAMING_ERROR", `byte_lengths add up to ${offset} but ${declared} were announced`, 502);
  }
  return slices;
}

/** §5.13: name the clip that failed so a batch of five is not a hunt through five files. */
function labelTrajError(index: number, error: unknown): never {
  const known = error as { code?: string; message?: string; status?: number };
  throw new WorldError(known.code ?? "INTERNAL_ERROR", `trajs[${index}]: ${known.message ?? String(error)}`, known.status ?? 400);
}

/**
 * §5.3/§5.12: answer `updateTraj` once its binary frame has been paired to the
 * request. Which trajectory objects apply is a scene question (§8.2), so this
 * does read the world, but it never mutates it: a trajectory is not a world
 * change and must not bump `scene_version`.
 *
 * **Q99**: one request may carry N independent v2 clips. They are sliced by the
 * announced `byte_lengths` (never by reading a header, which is exactly what a broken
 * clip cannot be trusted about), decoded one at a time, and judged against the world
 * one at a time -- all or nothing, so the browser's list always matches what the
 * caller holds (§5.3).
 */
export function applyTrajRequest(world: World, request: BrowserRequest, buffer: ArrayBuffer): TrajApplication {
  const slices = trajectorySlice(request.payload ?? {}, buffer);
  const present = new Set(world.objects.map((node) => node.id));
  const trajs = slices.map((slice, index) => {
    try {
      return decodeTraj(slice);
    } catch (error) {
      return labelTrajError(index, error);
    }
  });
  const summaries: TrajSummary[] = trajs.map((traj, index) => ({
    index,
    fps: traj.fps,
    total_frames: traj.totalFrames,
    duration_seconds: traj.durationSeconds,
    // §5.12 counts what the file declares; ignored_objects says what applied.
    object_count: traj.objectCount,
    attach_count: traj.attachCount,
    ignored_objects: traj.objects
      .filter((object) => !present.has(object.objectId))
      .map((object) => ({ object_id: object.objectId, reason: "OBJECT_NOT_IN_SCENE" })),
    // §8.6: an attach whose new parent is not on stage is dropped, the child keeps
    // its current parent, and playback carries on.
    warnings: traj.attaches
      .filter((attach) => attach.parentId !== null && !present.has(attach.parentId))
      .map((attach) => `ATTACH_PARENT_MISSING@frame=${attach.frame}`),
  }));
  return { trajs, result: { ok: true, count: trajs.length, trajs: summaries } };
}
