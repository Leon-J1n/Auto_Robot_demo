/**
 * The browser half of the two synchronous read routes `GET /captureScene` and
 * `GET /get2DAABB` (spec v2.1 §4.4, §4.5, §5.9, §5.10, §5.11).
 *
 * The gateway relays and never judges the picture, so the camera checks and the §5.10 encodings —
 * PNG plus a float32 little-endian metre depth map — all resolve here, against the world this
 * browser is authoritative over. Since §9 (S5) each answer also carries the identity block (§9.1),
 * which only this side can fill in truthfully: the gateway compares it against its own reading and
 * refuses a frame that does not describe the 现场 it locked.
 */
import type { BrowserRequest, CameraProps, CaptureSceneResult, Get2DAabbResult, PixelBox, PlaybackState, SceneFrameIdentity, Vec3 } from "../../contracts/types";
import { idOf } from "./scene_requests";
import { isUintId, World, WorldError } from "./world";

export interface SensorImage {
  width: number;
  height: number;
  /** the PNG bytes, already base64 */
  base64: string;
}

export interface SensorDepth {
  width: number;
  height: number;
  /** metres along the optical axis, row major, top row first, `NaN` where nothing was hit */
  meters: Float32Array;
}

/** §5.11: one object's world box and where that box lands on one camera's image. */
export interface SensorBox {
  width: number;
  height: number;
  aabb: { min: Vec3; max: Vec3 };
  visible: boolean;
  clipped: boolean;
  box: PixelBox | null;
}

/**
 * §9 (S5): the half of a frame's identity this module cannot read out of the world document --
 * who is rendering, what digest the gateway computed for this exact four-tuple, and whether the
 * drivers are parked. The four-tuple itself comes from the same `World` the frame was drawn from.
 */
export interface CaptureIdentity {
  browserID: string;
  sceneDigest: string | null;
  playback_state: PlaybackState;
}

/** What the renderer and the play bar have to offer a capture; kept narrow so it is testable. */
export interface CaptureView {
  image(objectId: number): SensorImage | null;
  depth(objectId: number): SensorDepth | null;
  box(objectId: number, cameraId: number): SensorBox | null;
  /** the frame the current geometry was driven from, `null` while no trajectory is loaded (§5.10) */
  frameIndex(): number | null;
  /** §9.1: read once per request, so the identity and the pixels describe the same moment */
  identity(): CaptureIdentity;
}

const PNG_PREFIX = /^data:image\/png;base64,/;
// `String.fromCharCode` takes a list of arguments, so a megabyte goes in slices.
const BASE64_CHUNK = 8192;

/**
 * Base64 of a byte run. The 640×480 depth map is 1.2 MB and `btoa` is native: the same
 * bytes cost ~0.04 s here against ~0.3 s for a per-character loop, which is the
 * difference between meeting §11.5's 250 ms and missing it.
 */
export function bytesToBase64(bytes: Uint8Array): string {
  let text = "";
  for (let start = 0; start < bytes.length; start += BASE64_CHUNK) {
    text += String.fromCharCode(...bytes.subarray(start, start + BASE64_CHUNK));
  }
  return btoa(text);
}

/** §5.10: `width*height*4` bytes of little-endian float32. Every engine we ship for is LE. */
export function float32ToBase64(meters: Float32Array): string {
  return bytesToBase64(new Uint8Array(meters.buffer, meters.byteOffset, meters.byteLength));
}

export function pngBase64(dataUrl: string): string {
  if (!PNG_PREFIX.test(dataUrl)) {
    throw new WorldError("UPSTREAM_RENDER_FAILED", `the canvas did not encode a png data url (got ${dataUrl.slice(0, 32)}…)`, 502);
  }
  return dataUrl.slice(dataUrl.indexOf(",") + 1);
}

/** §2.6 leaves `Camera.type` optional and the renderer reads an absent type as `rgb`. */
function cameraType(camera: CameraProps): "rgb" | "rgbd" {
  return camera.type ?? "rgb";
}

/**
 * §9.1's identity block: the four-tuple from the world this request is answering from, plus the
 * two fields only the app knows. Shared by both routes because a box is as much an observation of
 * one 现场 as a frame is -- the spec says both responses carry the same six fields.
 */
function identityFields(world: World, identity: CaptureIdentity): SceneFrameIdentity {
  return {
    browserID: identity.browserID,
    scene_id: world.sceneId,
    scene_version: world.version,
    scene_sub_version: world.subVersion,
    sceneDigest: identity.sceneDigest,
    playback_state: identity.playback_state,
  };
}

function requestedType(payload: Record<string, unknown>): "rgb" | "rgbd" {
  const raw = payload.type;
  if (raw === undefined || raw === null) throw new WorldError("MISSING_PARAM", "type is required");
  if (raw !== "rgb" && raw !== "rgbd") throw new WorldError("INVALID_PARAM", `type must be rgb or rgbd, got ${JSON.stringify(raw)}`);
  return raw;
}

/** Answer one `captureScene` request against the live world and the live scene graph. */
export function applyCaptureRequest(world: World, request: BrowserRequest, view: CaptureView): CaptureSceneResult {
  const payload = request.payload ?? {};
  const objectId = idOf(payload);
  const type = requestedType(payload);
  const camera = world.camera(objectId);
  if (!camera) throw new WorldError("CAMERA_NOT_FOUND", `object ${objectId} has no Camera component`, 404);
  // §5.10: no implicit mode switch — the caller has to ask for what the entity is.
  const declared = cameraType(camera);
  if (type !== declared) {
    throw new WorldError("CAMERA_TYPE_MISMATCH", `capture asked for ${type} but camera ${objectId} is ${declared}`, 400);
  }

  const image = view.image(objectId);
  if (!image) throw new WorldError("UPSTREAM_RENDER_FAILED", `camera ${objectId} produced no rgb frame`, 502);
  const result: CaptureSceneResult = {
    request_id: request.request_id,
    id: objectId,
    type,
    ...identityFields(world, view.identity()),
    width: image.width,
    height: image.height,
    camera_type: declared,
    image_encoding: "png",
    image_base64: image.base64,
    captured_at: new Date().toISOString(),
    frame_index: view.frameIndex(),
  };
  if (type === "rgb") return result;

  const depth = view.depth(objectId);
  if (!depth) throw new WorldError("UPSTREAM_RENDER_FAILED", `camera ${objectId} produced no depth map`, 502);
  if (depth.width !== image.width || depth.height !== image.height) {
    throw new WorldError("UPSTREAM_RENDER_FAILED", `depth ${depth.width}×${depth.height} does not match the image ${image.width}×${image.height}`, 502);
  }
  return {
    ...result,
    depth_encoding: "float32le_meters",
    depth_base64: float32ToBase64(depth.meters),
    depth_unit: "meter",
    depth_invalid: "NaN",
  };
}

/** §5.9: `/get2DAABB` needs both a target object and the camera that measures it. */
function cameraIdOf(payload: Record<string, unknown>): number {
  const raw = payload.camera_id;
  if (raw === undefined || raw === null) throw new WorldError("MISSING_PARAM", "camera_id is required");
  if (!isUintId(raw)) throw new WorldError("INVALID_PARAM", `camera_id ${JSON.stringify(raw)} is not a uint32`);
  return raw;
}

/**
 * Answer one `get2DAABB` request (§5.11). The projection is measured against the
 * geometry as it stands, so a loaded trajectory reports the frame it was driven from.
 */
export function applyAabbRequest(world: World, request: BrowserRequest, view: CaptureView): Get2DAabbResult {
  const payload = request.payload ?? {};
  const objectId = idOf(payload);
  const cameraId = cameraIdOf(payload);
  if (!world.get(objectId)) throw new WorldError("OBJECT_NOT_FOUND", `object ${objectId} is not in the scene`, 404);
  if (!world.camera(cameraId)) throw new WorldError("CAMERA_NOT_FOUND", `object ${cameraId} has no Camera component`, 404);
  // One code covers "nothing measurable": no node, an empty node (§3.7 gives an object
  // without a `MetadataRef` no placeholder geometry), or a lost GL context. §5.11 does
  // not split them, and the caller cannot act on the difference.
  const measured = view.box(objectId, cameraId);
  if (!measured) throw new WorldError("EMPTY_AABB", `object ${objectId} has no geometry measurable against camera ${cameraId}`, 400);
  return {
    request_id: request.request_id,
    id: objectId,
    camera_id: cameraId,
    ...identityFields(world, view.identity()),
    width: measured.width,
    height: measured.height,
    coordinate_origin: "top_left",
    visible: measured.visible,
    clipped: measured.clipped,
    box: measured.box,
    aabb_world: measured.aabb,
    frame_index: view.frameIndex(),
  };
}
