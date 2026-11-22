import { describe, expect, it, vi } from "vitest";
import type { EntityView, SceneSnapshot, TimelineManifest } from "../../contracts/types";
import { reconstructTimelineFrame, TimelineLoader, type TimelineBundle } from "./timeline";

const entity = { id: 1, name: "object", visual: { mesh: "shared" }, world: { translation: [0, 0, 0] } } as unknown as EntityView;
const base = { simulationTime: 0, entities: [entity, { ...entity, id: 2 }] } as SceneSnapshot;
const manifest = { timelineId: "test", dataUrl: "/data", frameCount: 2, frames: [
  { index: 0, snapshotUrl: "/0" }, { index: 1, snapshotUrl: "/1" },
] } as TimelineManifest;
const bundle: TimelineBundle = { snapshot: base, frames: [
  { simulationTime: 0, entities: [] },
  { simulationTime: 0.02, entities: [{ id: 1, name: "changed" }], removedEntities: [2] },
] };

describe("compact timeline", () => {
  it("reconstructs absolute frames without mutating static data or retaining later removals", () => {
    const changed = reconstructTimelineFrame(base, bundle.frames[1]!);
    expect(changed.entities).toHaveLength(1);
    expect(changed.entities[0]!.name).toBe("changed");
    expect(changed.entities[0]!.visual).toBe(entity.visual);
    const rewind = reconstructTimelineFrame(base, bundle.frames[0]!);
    expect(rewind.entities).toHaveLength(2);
    expect(rewind.entities[0]).toBe(entity);
    expect(entity.name).toBe("object");
  });

  it("downloads one bundle for preload and arbitrary seeks", async () => {
    const load = vi.fn().mockResolvedValue(bundle);
    const loader = new TimelineLoader(manifest, load);
    await Promise.all([loader.preload(), loader.frame(1), loader.frame(0)]);
    expect(load).toHaveBeenCalledTimes(1);
    expect((await loader.frame(1)).simulationTime).toBe(0.02);
    loader.dispose();
    expect(load.mock.calls[0]![1].signal.aborted).toBe(true);
  });

  it("retries a failed bundle instead of caching its rejection", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("Failed to fetch")).mockResolvedValue(bundle);
    const loader = new TimelineLoader(manifest, load);
    await expect(loader.preload()).rejects.toThrow("Failed to fetch");
    expect((await loader.frame(1)).simulationTime).toBe(0.02);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("loads legacy frames lazily and preserves full snapshots", async () => {
    const load = vi.fn().mockResolvedValue(base);
    const loader = new TimelineLoader({ ...manifest, dataUrl: undefined }, load);
    await loader.preload();
    expect(load).toHaveBeenCalledTimes(1);
    expect(await loader.frame(1)).toBe(base);
    expect(load).toHaveBeenCalledTimes(2);
  });
});
