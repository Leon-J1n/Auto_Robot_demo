/**
 * TRAJ v2 decoder (spec v2.1 §8.1, layout per docs/轨迹文件布局描述.md §7).
 *
 * Decoding is the only place that reads bytes: every access is little-endian,
 * the magic is compared as a number, and segment poses stay zero-copy
 * Float32Array windows over the original buffer (§8.8), because playback must
 * never re-slice a 100 MB trajectory.
 */
import type { UintId } from "../../contracts/types";

/** Written as 'TRAJ' but packed big-endian-then-emitted-little, so bytes on disk read "JART" (§7.2). */
export const TRAJ_MAGIC = 0x5452414a;
export const TRAJ_VERSION = 2;
/** The only frame stride the current format produces (§5.13). */
export const TRAJ_POSE_DIM = 7;
export const TRAJ_HEADER_BYTES = 32;
const OBJECT_HEADER_BYTES = 12;
const SEGMENT_BYTES = 16;
const ATTACH_BYTES = 16;
/** scene-root sentinel (§2.5, layout doc §7.7). */
export const TRAJ_WORLD_ROOT = 0xffffffff;

export class TrajError extends Error {
  constructor(readonly code: string, message: string, readonly status: number = 400) {
    super(message);
    this.name = "TrajError";
  }
}

export interface TrajSegment {
  startFrame: number;
  frameCount: number;
  dataOffset: number;
  /** §3.3 step number, not a segment index: 0 idle / 1 grasp / 2 move / 3 place. */
  step: number;
  /** `frameCount * poseDim` floats, a view on the source buffer. */
  poses: Float32Array;
}

export interface TrajObject {
  objectId: UintId;
  /** null once the 0xFFFFFFFF sentinel is resolved (§2.5). */
  parentId: UintId | null;
  poseDim: number;
  defaultPose: number[];
  segments: TrajSegment[];
}

export interface TrajAttach {
  frame: number;
  childId: UintId;
  parentId: UintId | null;
}

/** One continuous run of a `step_count`, gaps between its segments included (§7.3). */
export interface TrajStep {
  step: number;
  /** Inclusive frame range. */
  startFrame: number;
  endFrame: number;
}

export interface Traj {
  version: number;
  fps: number;
  totalFrames: number;
  objectCount: number;
  attachCount: number;
  durationSeconds: number;
  byteLength: number;
  dataStart: number;
  objects: TrajObject[];
  attaches: TrajAttach[];
  steps: TrajStep[];
}

function truncated(what: string): TrajError {
  return new TrajError("TRAJ_TRUNCATED", `trajectory ends before ${what}`);
}

/** A cursor that refuses to read past the end of the buffer (§7.1). */
class Reader {
  private readonly view: DataView;

  constructor(private readonly buffer: ArrayBuffer) {
    if (buffer.byteLength < TRAJ_HEADER_BYTES) throw truncated("the 32 byte header");
    this.view = new DataView(buffer);
  }

  get byteLength(): number {
    return this.buffer.byteLength;
  }

  require(offset: number, length: number, what: string): void {
    if (!Number.isInteger(offset) || offset < 0 || length < 0 || offset + length > this.byteLength) {
      throw truncated(`${what} at byte ${offset} (${length} byte${length === 1 ? "" : "s"})`);
    }
  }

  u32(offset: number, what: string): number {
    this.require(offset, 4, what);
    return this.view.getUint32(offset, true);
  }

  u16(offset: number, what: string): number {
    this.require(offset, 2, what);
    return this.view.getUint16(offset, true);
  }

  floats(offset: number, count: number, what: string): number[] {
    this.require(offset, count * 4, what);
    const out: number[] = [];
    for (let index = 0; index < count; index += 1) out.push(this.view.getFloat32(offset + index * 4, true));
    return out;
  }

  /** Zero-copy window; the stride check keeps `new Float32Array` from throwing. */
  window(offset: number, count: number, what: string): Float32Array {
    this.require(offset, count * 4, what);
    if (offset % 4 !== 0) throw new TrajError("TRAJ_TRUNCATED", `${what} at byte ${offset} is not 4-byte aligned`);
    return new Float32Array(this.buffer, offset, count);
  }
}

function readObject(reader: Reader, offset: number, totalFrames: number): { object: TrajObject; next: number } {
  const record = `object header at byte ${offset}`;
  const objectId = reader.u32(offset, `${record} id`);
  const rawParent = reader.u32(offset + 4, `${record} parent`);
  const poseDim = reader.u16(offset + 8, `${record} pose_dim`);
  const segmentCount = reader.u16(offset + 10, `${record} segment_count`);
  if (poseDim !== TRAJ_POSE_DIM) {
    throw new TrajError("TRAJ_POSE_DIM_UNSUPPORTED", `object ${objectId} declares pose_dim ${poseDim}, only 7 is defined`);
  }
  const defaultPoseAt = offset + OBJECT_HEADER_BYTES;
  const segmentsAt = defaultPoseAt + poseDim * 4;
  reader.require(segmentsAt, SEGMENT_BYTES * segmentCount, `segment table of object ${objectId}`);

  const segments: TrajSegment[] = [];
  for (let index = 0; index < segmentCount; index += 1) {
    const at = segmentsAt + index * SEGMENT_BYTES;
    const startFrame = reader.u32(at, `segment ${index} of object ${objectId}`);
    const frameCount = reader.u32(at + 4, `frame_count of segment ${index}`);
    const dataOffset = reader.u32(at + 8, `data_offset of segment ${index}`);
    const step = reader.u32(at + 12, `step_count of segment ${index}`);
    const bytes = frameCount * poseDim * 4;
    if (dataOffset + bytes > reader.byteLength) {
      throw truncated(`segment ${index} of object ${objectId} (${bytes} bytes from ${dataOffset})`);
    }
    if (startFrame + frameCount > totalFrames) {
      throw new TrajError("INVALID_PARAM", `segment ${index} of object ${objectId} covers frames ${startFrame}..${startFrame + frameCount - 1} past total_frames ${totalFrames}`);
    }
    segments.push({
      startFrame,
      frameCount,
      dataOffset,
      step,
      poses: reader.window(dataOffset, frameCount * poseDim, `pose data of object ${objectId} segment ${index}`),
    });
  }
  const object: TrajObject = {
    objectId,
    parentId: rawParent === TRAJ_WORLD_ROOT ? null : rawParent,
    poseDim,
    defaultPose: reader.floats(defaultPoseAt, poseDim, `default pose of object ${objectId}`),
    // Frame lookup walks the segments in order (§8.5), and a hand-edited file
    // need not have written them that way - same rule as the attach table.
    segments: segments.sort((left, right) => left.startFrame - right.startFrame),
  };
  return { object, next: segmentsAt + SEGMENT_BYTES * segmentCount };
}

/**
 * §7.3: `step_count` names a step, not a segment, and one step may span several
 * segments with idle gaps between them; those gaps belong to the same step. A
 * different step in between starts a new run, so the same step number can
 * legitimately appear twice (a generator that revisits a phase).
 */
export function stepsFrom(objects: TrajObject[]): TrajStep[] {
  const covered = objects
    .flatMap((object) => object.segments.map((segment) => ({ step: segment.step, startFrame: segment.startFrame, endFrame: segment.startFrame + segment.frameCount - 1 })))
    .sort((left, right) => left.startFrame - right.startFrame || left.endFrame - right.endFrame);

  const steps: TrajStep[] = [];
  for (const entry of covered) {
    const last = steps[steps.length - 1];
    if (last && last.step === entry.step) {
      last.endFrame = Math.max(last.endFrame, entry.endFrame);
      continue;
    }
    steps.push({ step: entry.step, startFrame: entry.startFrame, endFrame: entry.endFrame });
  }
  return steps;
}

export function decodeTraj(buffer: ArrayBuffer): Traj {
  const reader = new Reader(buffer);
  const magic = reader.u32(0, "magic");
  if (magic !== TRAJ_MAGIC) throw new TrajError("TRAJ_INVALID_MAGIC", `magic is 0x${magic.toString(16).padStart(8, "0")}, expected 0x${TRAJ_MAGIC.toString(16)}`);
  const version = reader.u32(4, "version");
  if (version !== TRAJ_VERSION) throw new TrajError("TRAJ_UNSUPPORTED_VERSION", `format version ${version} is not ${TRAJ_VERSION}`);
  const fps = reader.u32(8, "fps");
  if (fps < 1) throw new TrajError("INVALID_PARAM", `fps ${fps} is not positive`);
  const totalFrames = reader.u32(12, "total_frames");
  const objectCount = reader.u32(16, "object_count");
  const attachCount = reader.u32(20, "attach_count");
  const indexOffset = reader.u32(24, "index_offset");
  const attachOffset = reader.u32(28, "attach_offset");
  if (indexOffset < TRAJ_HEADER_BYTES) throw new TrajError("INVALID_PARAM", `index_offset ${indexOffset} overlaps the header`);
  if (attachOffset < indexOffset) throw new TrajError("INVALID_PARAM", `attach_offset ${attachOffset} precedes index_offset ${indexOffset}`);

  let cursor = indexOffset;
  const objects: TrajObject[] = [];
  for (let index = 0; index < objectCount; index += 1) {
    const read = readObject(reader, cursor, totalFrames);
    objects.push(read.object);
    cursor = read.next;
  }
  if (cursor > attachOffset) throw new TrajError("INVALID_PARAM", `index region ends at ${cursor} but attach_offset is ${attachOffset}`);

  reader.require(attachOffset, ATTACH_BYTES * attachCount, "attach table");
  const attaches: TrajAttach[] = [];
  for (let index = 0; index < attachCount; index += 1) {
    const at = attachOffset + index * ATTACH_BYTES;
    const frame = reader.u32(at, `attach ${index} frame`);
    const childId = reader.u32(at + 4, `attach ${index} child_id`);
    const rawParent = reader.u32(at + 8, `attach ${index} parent_id`);
    attaches.push({ frame, childId, parentId: rawParent === TRAJ_WORLD_ROOT ? null : rawParent });
  }
  const start = attachOffset + ATTACH_BYTES * attachCount;

  return {
    version,
    fps,
    totalFrames,
    objectCount,
    attachCount,
    durationSeconds: totalFrames / fps,
    byteLength: reader.byteLength,
    /** §7.4: derived, never stored in the header (`attach_offset + 16 * attach_count`). */
    dataStart: start,
    objects,
    // Replay order matters (§8.6): the layout guarantees ascending frames, but a
    // hand-edited file must not depend on that.
    attaches: attaches.sort((left, right) => left.frame - right.frame),
    steps: stepsFrom(objects),
  };
}

/**
 * The pose one object holds at a frame (§8.5): its own sample inside a segment,
 * the previous segment's last sample across a gap or past the end, the Index
 * default before the first segment. The result is a window on the source buffer,
 * so a playback loop never copies a frame (§8.8).
 */
export function poseAtFrame(object: TrajObject, frame: number): ArrayLike<number> {
  const wanted = Number.isFinite(frame) && frame > 0 ? Math.floor(frame) : 0;
  let held: TrajSegment | null = null;
  for (const segment of object.segments) {
    if (segment.startFrame > wanted) break;
    if (wanted < segment.startFrame + segment.frameCount) {
      const index = (wanted - segment.startFrame) * object.poseDim;
      return segment.poses.subarray(index, index + object.poseDim);
    }
    if (segment.frameCount > 0) held = segment;
  }
  if (!held) return object.defaultPose;
  return held.poses.subarray((held.frameCount - 1) * object.poseDim, held.frameCount * object.poseDim);
}

/**
 * The step the playhead is inside (§7.3, Q96), walked the same way `poseAtFrame`
 * walks segments so the two can never disagree about which run a frame belongs to:
 * inside a run gives that run, a gap or the frames past the last run give the last
 * run that had already started, and a frame before the first run gives nothing -
 * there is no step whose end is still ahead.
 */
export function stepAtFrame(steps: readonly TrajStep[], frame: number): TrajStep | null {
  const wanted = Number.isFinite(frame) && frame > 0 ? Math.floor(frame) : 0;
  let held: TrajStep | null = null;
  for (const step of steps) {
    if (step.startFrame > wanted) break;
    if (wanted <= step.endFrame) return step;
    held = step;
  }
  return held;
}
