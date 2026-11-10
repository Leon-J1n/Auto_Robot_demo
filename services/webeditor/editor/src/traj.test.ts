/**
 * TRAJ v2 decoder tests (M4-1): the shipped 12 KB fixture is the reference, and
 * every §5.13 header error is provoked by patching one field of that file, so
 * the negative cases exercise the real layout rather than a made-up one.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  decodeTraj,
  poseAtFrame,
  stepAtFrame,
  stepsFrom,
  TrajError,
  TRAJ_MAGIC,
  TRAJ_POSE_DIM,
  TRAJ_VERSION,
  type TrajObject,
  type TrajSegment,
  type TrajStep,
} from "./traj";

const fixtureUrl = new URL("../../tests/fixtures/robot_grasp.traj", import.meta.url);

/** A detached buffer: a Node Buffer shares a pooled ArrayBuffer with an offset. */
function fixtureBytes(): ArrayBuffer {
  const raw = readFileSync(fixtureUrl);
  const copy = new ArrayBuffer(raw.byteLength);
  new Uint8Array(copy).set(raw);
  return copy;
}

function patch(buffer: ArrayBuffer, write: (view: DataView) => void): ArrayBuffer {
  const copy = fixtureBytes();
  write(new DataView(copy));
  return copy;
}

function expectTrajError(action: () => unknown, code: string, status = 400): void {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(TrajError);
    expect((error as TrajError).code).toBe(code);
    expect((error as TrajError).status).toBe(status);
    return;
  }
  throw new Error(`expected a TrajError with code ${code}`);
}

/** tsconfig has noUncheckedIndexedAccess, so positional reads need an explicit guard. */
function need<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`${what} is missing from the trajectory`);
  return value;
}

/** Offsets of the shipped file, straight from docs/轨迹文件布局描述.md §6. */
const ARM_RECORD = 32;
const APPLE_RECORD = 216;
const BOX_RECORD = 256;
const FIRST_SEGMENT = ARM_RECORD + 12 + TRAJ_POSE_DIM * 4;

describe("decodeTraj", () => {
  it("reads the header of the shipped grasp trajectory", () => {
    const traj = decodeTraj(fixtureBytes());
    expect(traj.version).toBe(TRAJ_VERSION);
    expect(traj.fps).toBe(60);
    expect(traj.totalFrames).toBe(720);
    expect(traj.durationSeconds).toBe(12);
    expect(traj.objectCount).toBe(3);
    expect(traj.attachCount).toBe(2);
    expect(traj.byteLength).toBe(12_088);
    expect(traj.dataStart).toBe(328);
  });

  it("maps the 0xFFFFFFFF parent to the scene root (§2.5)", () => {
    const traj = decodeTraj(fixtureBytes());
    expect(traj.objects.map((object) => object.objectId)).toEqual([1, 2, 3]);
    expect(traj.objects.map((object) => object.parentId)).toEqual([null, null, null]);
    expect(traj.objects.every((object) => object.poseDim === 7)).toBe(true);
  });

  it("carries the arm default pose and its nine segments", () => {
    const arm = need(decodeTraj(fixtureBytes()).objects[0], "arm");
    expect(arm.defaultPose).toHaveLength(TRAJ_POSE_DIM);
    expect(arm.defaultPose.map((value) => Number(value.toFixed(4)))).toEqual([2.75, -1.91, 2.28, -1.95, -1.5708, -1.96, 0]);
    expect(arm.segments.map((segment) => segment.startFrame)).toEqual([120, 220, 310, 340, 400, 490, 530, 580, 630]);
    expect(arm.segments.map((segment) => segment.step)).toEqual([1, 1, 1, 2, 2, 2, 3, 3, 3]);
    expect(arm.segments.map((segment) => [segment.frameCount, segment.dataOffset])).toEqual([
      [70, 328], [60, 2288], [30, 3968], [40, 4808], [70, 5928], [40, 7888], [30, 9008], [20, 9848], [60, 10408],
    ]);
  });

  it("keeps the two objects without segments on their default pose", () => {
    const objects = decodeTraj(fixtureBytes()).objects;
    const apple = need(objects[1], "apple");
    const box = need(objects[2], "box");
    expect(apple.segments).toEqual([]);
    expect(box.segments).toEqual([]);
    // float32 storage: 0.35 lands as 0.34999999…, so compare at wire precision.
    expect(apple.defaultPose.map((value) => Number(value.toFixed(4)))).toEqual([0.35, 0, 0.04, 0, 0, 0, 1]);
    expect(box.defaultPose.map((value) => Number(value.toFixed(4)))).toEqual([0.25, 0.3, 0.05, 0, 0, 0, 1]);
  });

  it("exposes pose data as zero-copy windows over the source buffer (§8.8)", () => {
    const buffer = fixtureBytes();
    const traj = decodeTraj(buffer);
    const first = need(need(traj.objects[0], "arm").segments[0], "first segment");
    expect(first.poses).toBeInstanceOf(Float32Array);
    expect(first.poses.length).toBe(70 * TRAJ_POSE_DIM);
    expect(first.poses.buffer).toBe(buffer);
    expect(first.poses.byteOffset).toBe(328);
  });

  it("starts and ends the arm data on the default pose so looping has no jump", () => {
    const arm = need(decodeTraj(fixtureBytes()).objects[0], "arm");
    const closing = need(arm.segments[arm.segments.length - 1], "closing segment");
    const opening = need(arm.segments[0], "opening segment");
    const lastFrame = closing.poses.slice((closing.frameCount - 1) * TRAJ_POSE_DIM, closing.frameCount * TRAJ_POSE_DIM);
    expect(Array.from(lastFrame)).toEqual(arm.defaultPose);
    expect(Array.from(opening.poses.slice(0, TRAJ_POSE_DIM))).toEqual(arm.defaultPose);
  });

  it("resolves the two attach events to arm then box", () => {
    expect(decodeTraj(fixtureBytes()).attaches).toEqual([
      { frame: 340, childId: 2, parentId: 1 },
      { frame: 530, childId: 2, parentId: 3 },
    ]);
  });

  it("merges the idle gaps of a step into that step (§7.3)", () => {
    const { steps } = decodeTraj(fixtureBytes());
    expect(steps).toEqual([
      { step: 1, startFrame: 120, endFrame: 339 },
      { step: 2, startFrame: 340, endFrame: 529 },
      { step: 3, startFrame: 530, endFrame: 689 },
    ]);
  });

  it("rejects a magic that is not the number 0x5452414A", () => {
    // The bytes on disk spell "JART"; ASCII comparison must not be used (§7.2).
    expect(new Uint8Array(fixtureBytes()).slice(0, 4).reduce((text, byte) => text + String.fromCharCode(byte), "")).toBe("JART");
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(0, TRAJ_MAGIC + 1, true))), "TRAJ_INVALID_MAGIC");
  });

  it("rejects a version other than 2 and a stride other than 7", () => {
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(4, 3, true))), "TRAJ_UNSUPPORTED_VERSION");
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint16(ARM_RECORD + 8, 6, true))), "TRAJ_POSE_DIM_UNSUPPORTED");
  });

  it("rejects files that end early in any region", () => {
    const whole = fixtureBytes();
    expectTrajError(() => decodeTraj(whole.slice(0, 31)), "TRAJ_TRUNCATED");
    expectTrajError(() => decodeTraj(whole.slice(0, 300)), "TRAJ_TRUNCATED");
    expectTrajError(() => decodeTraj(whole.slice(0, 12_080)), "TRAJ_TRUNCATED");
    expectTrajError(
      () => decodeTraj(patch(whole, (view) => view.setUint32(FIRST_SEGMENT + 8, 99_999, true))),
      "TRAJ_TRUNCATED",
    );
  });

  it("rejects a segment data offset that is not 4-byte aligned", () => {
    expectTrajError(
      () => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(FIRST_SEGMENT + 8, 329, true))),
      "TRAJ_TRUNCATED",
    );
  });

  it("rejects header fields that point at overlapping or impossible regions", () => {
    // The three index records end at 296, so an attach region earlier than that is illegal.
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(28, 100, true))), "INVALID_PARAM");
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(24, 8, true))), "INVALID_PARAM");
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(28, 16, true))), "INVALID_PARAM");
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(8, 0, true))), "INVALID_PARAM");
  });

  it("rejects frame ranges that run past total_frames", () => {
    expectTrajError(() => decodeTraj(patch(fixtureBytes(), (view) => view.setUint32(12, 100, true))), "INVALID_PARAM");
  });

  it("orders attach events by frame even when the file does not", () => {
    const swapped = patch(fixtureBytes(), (view) => {
      view.setUint32(296, 530, true);
      view.setUint32(312, 340, true);
    });
    expect(decodeTraj(swapped).attaches.map((attach) => attach.frame)).toEqual([340, 530]);
  });
});

function segment(startFrame: number, frameCount: number, step: number): TrajSegment {
  return { startFrame, frameCount, dataOffset: 0, step, poses: new Float32Array(frameCount * TRAJ_POSE_DIM) };
}

function arm(segments: TrajSegment[]): TrajObject {
  return { objectId: 1, parentId: null, poseDim: TRAJ_POSE_DIM, defaultPose: [], segments };
}

describe("stepsFrom", () => {
  it("groups nothing when no object carries segments", () => {
    expect(stepsFrom([arm([])])).toEqual([]);
  });

  it("sorts segments before grouping so write order does not matter", () => {
    const steps = stepsFrom([arm([segment(400, 10, 2), segment(100, 10, 1)])]);
    expect(steps).toEqual([
      { step: 1, startFrame: 100, endFrame: 109 },
      { step: 2, startFrame: 400, endFrame: 409 },
    ]);
  });

  it("keeps two runs when another step separates them", () => {
    const steps = stepsFrom([arm([segment(0, 10, 1), segment(20, 10, 2), segment(40, 10, 1)])]);
    expect(steps).toEqual([
      { step: 1, startFrame: 0, endFrame: 9 },
      { step: 2, startFrame: 20, endFrame: 29 },
      { step: 1, startFrame: 40, endFrame: 49 },
    ]);
  });

  it("merges overlapping runs across objects of the same step", () => {
    const steps = stepsFrom([arm([segment(0, 100, 1)]), arm([]), { ...arm([segment(50, 10, 1)]), objectId: 4 }]);
    expect(steps).toEqual([{ step: 1, startFrame: 0, endFrame: 99 }]);
  });
});

describe("poseAtFrame (§8.5)", () => {
  /** A run whose frame `n` carries `n + 1` in slot 0, so every hit is identifiable. */
  function ramped(startFrame: number, frameCount: number): TrajSegment {
    const poses = new Float32Array(frameCount * TRAJ_POSE_DIM);
    for (let index = 0; index < frameCount; index += 1) poses[index * TRAJ_POSE_DIM] = startFrame + index + 1;
    return { startFrame, frameCount, dataOffset: 0, step: 1, poses };
  }

  function timed(segments: TrajSegment[], defaultPose = [0, 0, 0, 0, 0, 0, 1]): TrajObject {
    return { objectId: 1, parentId: null, poseDim: TRAJ_POSE_DIM, defaultPose, segments };
  }

  /** Values as the shipped generator wrote them, read back through python struct. */
  function expectPose(pose: ArrayLike<number>, expected: number[]): void {
    expect(pose.length).toBe(expected.length);
    expected.forEach((value, index) => {
      expect(pose[index]).toBeCloseTo(value, 4);
    });
  }

  it("reads the Index default before the first segment", () => {
    const value = timed([ramped(100, 10)], [7, 0, 0, 0, 0, 0, 1]);
    expect(poseAtFrame(value, 0)[0]).toBe(7);
    expect(poseAtFrame(value, 99)[0]).toBe(7);
    expect(poseAtFrame(value, 100)[0]).toBe(101);
  });

  it("holds the previous segment's last frame across a gap and past the end", () => {
    const value = timed([ramped(0, 10), ramped(100, 10)]);
    expect(poseAtFrame(value, 9)[0]).toBe(10);
    expect(poseAtFrame(value, 50)[0]).toBe(10);
    expect(poseAtFrame(value, 99)[0]).toBe(10);
    expect(poseAtFrame(value, 100)[0]).toBe(101);
    expect(poseAtFrame(value, 109)[0]).toBe(110);
    expect(poseAtFrame(value, 500)[0]).toBe(110);
  });

  it("has nothing to hold for an object that declares no segments", () => {
    const value = timed([], [0.25, 0.3, 0.05, 0, 0, 0, 1]);
    expectPose(poseAtFrame(value, 0), [0.25, 0.3, 0.05, 0, 0, 0, 1]);
    expectPose(poseAtFrame(value, 719), [0.25, 0.3, 0.05, 0, 0, 0, 1]);
  });

  it("floors a fractional frame and reads a negative or missing one as frame 0", () => {
    const value = timed([ramped(100, 10)], [7, 0, 0, 0, 0, 0, 1]);
    expect(poseAtFrame(value, 103.9)[0]).toBe(104);
    expect(poseAtFrame(value, -1)[0]).toBe(7);
    expect(poseAtFrame(value, Number.NaN)[0]).toBe(7);
  });

  it("walks the shipped grasp file frame by frame", () => {
    const value = decodeTraj(fixtureBytes());
    const arm = need(value.objects[0], "arm");
    const apple = need(value.objects[1], "apple");

    expectPose(poseAtFrame(arm, 0), [2.75, -1.91, 2.28, -1.95, -1.5708, -1.96, 0]);
    expectPose(poseAtFrame(arm, 119), [2.75, -1.91, 2.28, -1.95, -1.5708, -1.96, 0]);
    // The last approach sample, before the 30 idle frames that precede the grasp step.
    expectPose(poseAtFrame(arm, 189), [0.39072, -1.52469, -2.51783, -0.66986, 1.5708, -1.18007, 0]);
    expectPose(poseAtFrame(arm, 200), [0.39072, -1.52469, -2.51783, -0.66986, 1.5708, -1.18007, 0]);
    // Frame 340 is where the Attach table closes the gripper, hence 0.72.
    expectPose(poseAtFrame(arm, 340), [0.39072, -1.82464, -2.5843, -0.30345, 1.5708, -1.18007, 0.72]);
    // The file ends where it began: the arm returns home and holds it to the last frame.
    expectPose(poseAtFrame(arm, 689), [2.75, -1.91, 2.28, -1.95, -1.5708, -1.96, 0]);
    expectPose(poseAtFrame(arm, 719), [2.75, -1.91, 2.28, -1.95, -1.5708, -1.96, 0]);
    // Objects 2 and 3 carry no segments at all, so they sit on their Index default.
    expectPose(poseAtFrame(apple, 400), [0.35, 0, 0.04, 0, 0, 0, 1]);
  });

  it("returns a window on the source buffer rather than a copy of the frame (§8.8)", () => {
    const buffer = fixtureBytes();
    const arm = need(decodeTraj(buffer).objects[0], "arm");
    const first = poseAtFrame(arm, 340);
    expect(first.length).toBe(TRAJ_POSE_DIM);

    // Writing over the Data region changes what the same frame reads back.
    new DataView(buffer).setFloat32(4808, 1.25, true);
    expect(poseAtFrame(arm, 340)[0]).toBe(1.25);
  });
});

describe("stepAtFrame (§7.3, Q96)", () => {
  function stepsOf(segments: TrajSegment[]): TrajStep[] {
    return stepsFrom([arm(segments)]);
  }

  it("has no step to preview before the first one starts", () => {
    const steps = stepsOf([segment(100, 40, 1), segment(200, 40, 2)]);
    expect(stepAtFrame(steps, 0)).toBeNull();
    expect(stepAtFrame(steps, 99)).toBeNull();
    expect(stepAtFrame(steps, 100)).toEqual({ step: 1, startFrame: 100, endFrame: 139 });
  });

  it("keeps a gap inside the step it sits in and holds the last step past the end", () => {
    const steps = stepsOf([segment(100, 40, 1), segment(200, 40, 2)]);
    expect(stepAtFrame(steps, 139)).toEqual({ step: 1, startFrame: 100, endFrame: 139 });
    expect(stepAtFrame(steps, 150)).toEqual({ step: 1, startFrame: 100, endFrame: 139 });
    expect(stepAtFrame(steps, 199)).toEqual({ step: 1, startFrame: 100, endFrame: 139 });
    expect(stepAtFrame(steps, 200)).toEqual({ step: 2, startFrame: 200, endFrame: 239 });
    expect(stepAtFrame(steps, 719)).toEqual({ step: 2, startFrame: 200, endFrame: 239 });
  });

  it("resolves the second run when a step number repeats", () => {
    const steps = stepsOf([segment(0, 10, 1), segment(20, 10, 2), segment(40, 10, 1)]);
    expect(stepAtFrame(steps, 5)).toEqual({ step: 1, startFrame: 0, endFrame: 9 });
    expect(stepAtFrame(steps, 15)).toEqual({ step: 1, startFrame: 0, endFrame: 9 });
    expect(stepAtFrame(steps, 35)).toEqual({ step: 2, startFrame: 20, endFrame: 29 });
    expect(stepAtFrame(steps, 45)).toEqual({ step: 1, startFrame: 40, endFrame: 49 });
  });

  it("has nothing for a clip with no segments and reads a fractional frame as its floor", () => {
    expect(stepAtFrame([], 0)).toBeNull();
    const steps = stepsOf([segment(100, 40, 1)]);
    expect(stepAtFrame(steps, 103.9)).toEqual({ step: 1, startFrame: 100, endFrame: 139 });
    expect(stepAtFrame(steps, -1)).toBeNull();
    expect(stepAtFrame(steps, Number.NaN)).toBeNull();
  });

  it("previews the frame each step of the shipped grasp clip ends on", () => {
    const steps = decodeTraj(fixtureBytes()).steps;
    expect(steps).toEqual([
      { step: 1, startFrame: 120, endFrame: 339 },
      { step: 2, startFrame: 340, endFrame: 529 },
      { step: 3, startFrame: 530, endFrame: 689 },
    ]);
    // The playhead rests on frames the arm only inherits: none before step 1 started,
    // and the last step still ahead of nothing after it.
    expect(stepAtFrame(steps, 0)).toBeNull();
    expect(stepAtFrame(steps, 119)).toBeNull();
    // 200 is inside step 1's own gap (190-219), so the preview does not change.
    expect(stepAtFrame(steps, 200)?.endFrame).toBe(339);
    expect(stepAtFrame(steps, 400)?.endFrame).toBe(529);
    expect(stepAtFrame(steps, 700)?.endFrame).toBe(689);
    expect(stepAtFrame(steps, 719)?.endFrame).toBe(689);
  });
});
