// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { Group, Object3D, Scene, Vector3 } from "three";
import type { WorldComponent } from "../../contracts/types";
import { TrajDriver, type TrajView } from "./drivers";
import { decodeTraj, type Traj, type TrajAttach, type TrajObject, type TrajSegment } from "./traj";
import { TrajFrameProvider } from "./timeline";
import { parseWorld, type World } from "./world";

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

function objectOf(id: number, name: string, components: WorldComponent[]): Record<string, unknown> {
  return { object_id_in_scene: id, object_name_in_scene: name, components };
}

function segment(startFrame: number, poses: number[][]): TrajSegment {
  return { startFrame, frameCount: poses.length, dataOffset: 0, step: 1, poses: new Float32Array(poses.flat()) };
}

function objectPose(id: number, segments: TrajSegment[] = [], defaultPose = [0, 0, 0, 0, 0, 0, 1]): TrajObject {
  return { objectId: id, parentId: null, poseDim: 7, defaultPose, segments };
}

function trajOf(objects: TrajObject[], attaches: TrajAttach[] = [], totalFrames = 600): Traj {
  return {
    version: 2, fps: 50, totalFrames, objectCount: objects.length, attachCount: attaches.length,
    durationSeconds: totalFrames / 50, byteLength: 0, dataStart: 0,
    objects, attaches: [...attaches].sort((left, right) => left.frame - right.frame),
    steps: [{ step: 0, startFrame: 0, endFrame: totalFrames - 1 }],
  };
}

interface Harness {
  provider: TrajFrameProvider;
  driver: TrajDriver;
  nodes: Map<number, Object3D>;
  root: Group;
}

/**
 * Ordinary objects only: playback semantics are about the frame, the hierarchy
 * and the clock, and the joint chain `drivers.test.ts` already drives. The stage
 * still hangs nodes the way the world file says, because `restoreParent` reads
 * that parent back out of it.
 */
function harness(traj: Traj, parentOf: Record<number, number | null> = {}): Harness {
  const world: World = parseWorld({
    scene_schema_version: 1, scene_version: 1, scene_id: 7, scene_name: "playback",
    objects: [1, 2, 3].map((id) => objectOf(id, `object ${id}`, [transform(parentOf[id] ?? null)])),
  });
  const root = new Group();
  const scene = new Scene();
  scene.add(root);
  const nodes = new Map<number, Object3D>();
  for (const object of world.objects) {
    const group = new Group();
    group.name = `object ${object.id}`;
    nodes.set(object.id, group);
    root.add(group);
  }
  const view: TrajView = {
    object: (objectId) => nodes.get(objectId) ?? null,
    joints: () => null,
    robot: () => null,
    root: () => root,
    worldParent: (objectId) => {
      const parentId = world.get(objectId)?.transform.parent ?? null;
      return parentId === null ? root : nodes.get(parentId) ?? root;
    },
    refresh: () => scene.updateMatrixWorld(true),
  };
  const driver = new TrajDriver(view, () => world, () => undefined);
  return { provider: new TrajFrameProvider(traj, driver), driver, nodes, root };
}

function positionX(node: Object3D): number {
  return node.getWorldPosition(new Vector3()).x;
}

describe("TrajFrameProvider seeks locally (§8.5, §8.7)", () => {
  it("has no network, no per-frame snapshots: one call is a pure function of the frame", () => {
    const { provider } = harness(trajOf([objectPose(1)]));
    expect(provider.fps).toBe(50);
    expect(provider.totalFrames).toBe(600);
    expect(provider.lastFrame).toBe(599);
    expect(provider.simulationTime(50)).toBeCloseTo(1, 6);
    expect(provider.times()[50]).toBeCloseTo(1, 6);
    expect(provider.times()).toBe(provider.times());
  });

  it("clamps a wanted frame into the clip instead of inventing one", () => {
    const { provider, nodes } = harness(trajOf([objectPose(1, [segment(0, [[7, 0, 0, 0, 0, 0, 1]])])]));
    expect(provider.frame(-4).index).toBe(0);
    expect(provider.frame(Number.NaN).index).toBe(0);
    expect(provider.frame(1.7).index).toBe(1);
    expect(provider.frame(10_000).index).toBe(599);
    expect(positionX(nodes.get(1)!)).toBeCloseTo(7, 4);
  });

  it("holds the last sample of a segment across the gap and the default before it (§8.5)", () => {
    const traj = trajOf([objectPose(2, [
      segment(100, [[1, 0, 0, 0, 0, 0, 1], [2, 0, 0, 0, 0, 0, 1]]),
      segment(400, [[3, 0, 0, 0, 0, 0, 1]]),
    ], [0.5, 0, 0, 0, 0, 0, 1])]);
    const { provider, nodes } = harness(traj);
    const apple = nodes.get(2)!;
    for (const [frame, x] of [[0, 0.5], [99, 0.5], [100, 1], [101, 2], [399, 2], [400, 3], [599, 3]] as const) {
      expect(provider.frame(frame).simulationTime).toBeCloseTo(frame / 50, 6);
      expect(positionX(apple)).toBeCloseTo(x, 4);
    }
  });

  it("replays every attach up to the frame, so a jump forward grasps and a jump back lets go (§8.6)", () => {
    const traj = trajOf(
      [objectPose(1), objectPose(2), objectPose(3)],
      [{ frame: 50, childId: 2, parentId: 3 }, { frame: 300, childId: 3, parentId: 1 }],
    );
    const { provider, driver, nodes, root } = harness(traj);
    const apple = nodes.get(2)!;
    const box = nodes.get(3)!;

    provider.frame(20);
    expect(apple.parent).toBe(root);
    expect(box.parent).toBe(root);

    provider.frame(120);
    expect(apple.parent).toBe(driver.attachTarget(3));
    expect(box.parent).toBe(root);

    // Forward jump over both events: the later one wins, and the apple follows its box.
    provider.frame(400);
    expect(box.parent).toBe(nodes.get(1)!);
    expect(apple.parent).toBe(box);

    // Rewinding is not undoing: the hierarchy is rebuilt from the world file, not patched.
    provider.frame(0);
    expect(apple.parent).toBe(root);
    expect(box.parent).toBe(root);
  });

  it("rebuilds the same scene for the same frame however it was reached", () => {
    // The world file hangs the apple on the box; frame 60 moves it onto the arm.
    const traj = trajOf([objectPose(1), objectPose(2), objectPose(3)], [{ frame: 60, childId: 2, parentId: 1 }]);
    const { provider, nodes } = harness(traj, { 2: 3 });
    const apple = nodes.get(2)!;

    provider.frame(599);
    expect(apple.parent).toBe(nodes.get(1)!);
    provider.frame(80);
    expect(apple.parent).toBe(nodes.get(1)!);
    provider.frame(599);
    provider.frame(80);
    expect(apple.parent).toBe(nodes.get(1)!);

    // Out of the attach's range the authored parent comes back, not the last view.
    provider.frame(0);
    expect(apple.parent).toBe(nodes.get(3)!);
  });
});

describe("TrajFrameProvider clocks playback (§8.7)", () => {
  it("skips the frames a slow browser already missed", () => {
    const { provider } = harness(trajOf([objectPose(1)]));
    expect(provider.playhead(0, 0, false)).toEqual({ index: 0, finished: false });
    expect(provider.playhead(0, 0.019, false)).toEqual({ index: 0, finished: false });
    expect(provider.playhead(0, 0.02, false)).toEqual({ index: 1, finished: false });
    // 10 frames of simulation time has elapsed since the playhead sat at 1.
    expect(provider.playhead(1, 0.22, false).index).toBe(11);
  });

  it("wraps a looping clip at its duration and never reports it finished", () => {
    const { provider } = harness(trajOf([objectPose(1)]));
    expect(provider.durationSeconds).toBe(12);
    expect(provider.playhead(598, 12, true)).toEqual({ index: 0, finished: false });
    expect(provider.playhead(598, 31.5, true)).toEqual({ index: 0, finished: false });
    expect(provider.playhead(598, 11.979, true)).toEqual({ index: 598, finished: false });
    expect(provider.playhead(598, 11.98, true)).toEqual({ index: 599, finished: false });
  });

  it("rests on the last frame when a clip runs out without a loop", () => {
    const { provider } = harness(trajOf([objectPose(1)]));
    expect(provider.playhead(598, 12, false)).toEqual({ index: 599, finished: true });
    expect(provider.playhead(599, 40, false)).toEqual({ index: 599, finished: true });
  });

  it("moves a one-frame clip no further than it can go", () => {
    const { provider } = harness(trajOf([objectPose(1)], [], 1));
    expect(provider.lastFrame).toBe(0);
    expect(provider.playhead(0, 5, false)).toEqual({ index: 0, finished: true });
  });
});

describe("TrajFrameProvider on the real grasp file", () => {
  it("jumps to the end and back, then lands on exactly the same hierarchy", () => {
    const traj = decodeTraj(trajBytes());
    const { provider, nodes } = harness(traj, { 2: 1 });
    expect(provider.totalFrames).toBe(traj.totalFrames);
    expect(traj.attaches.length).toBe(traj.attachCount);

    // This stage has every object the file names, so nothing is reported missing.
    const last = provider.frame(provider.lastFrame);
    expect(last.driven).toEqual(traj.objects.map((object) => object.objectId));
    expect(last.missing).toEqual([]);
    const children = [...new Set(traj.attaches.map((event) => event.childId))].map((id) => nodes.get(id)!);
    const endParents = children.map((child) => child.parent);

    provider.frame(0);
    provider.frame(provider.lastFrame);
    expect(children.map((child) => child.parent)).toEqual(endParents);
  });
});
