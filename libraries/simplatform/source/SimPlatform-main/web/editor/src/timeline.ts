import type { EntityView, SceneSnapshot, TimelineManifest } from "../../contracts/types";

export type CompactTimelineFrame = Partial<Omit<SceneSnapshot, "entities">> & {
  simulationTime: number;
  entities: Array<Partial<EntityView> & Pick<EntityView, "id">>;
  removedEntities?: number[];
};
export type TimelineBundle = { timelineId?: string; snapshot: SceneSnapshot; frames: CompactTimelineFrame[] };
type LoadJson = <T>(url: string, init?: RequestInit) => Promise<T>;

export function reconstructTimelineFrame(base: SceneSnapshot, frame: CompactTimelineFrame): SceneSnapshot {
  const entities = new Map(base.entities.map((entity) => [entity.id, entity]));
  for (const id of frame.removedEntities ?? []) entities.delete(id);
  for (const patch of frame.entities) {
    const existing = entities.get(patch.id);
    entities.set(patch.id, existing ? { ...existing, ...patch } : patch as EntityView);
  }
  return { ...base, ...frame, entities: [...entities.values()] };
}

/** Owns one timeline's requests. Failed requests are evicted so playback can retry. */
export class TimelineLoader {
  private readonly abort = new AbortController();
  private bundle?: Promise<TimelineBundle>;
  private frames = new Map<number, Promise<SceneSnapshot>>();
  constructor(readonly manifest: TimelineManifest & { dataUrl?: string }, private readonly load: LoadJson) {}

  dispose(): void { this.abort.abort(); this.frames.clear(); this.bundle = undefined; }

  private loadBundle(): Promise<TimelineBundle> {
    if (!this.bundle) {
      const request = this.load<TimelineBundle>(this.manifest.dataUrl!, { signal: this.abort.signal });
      this.bundle = request;
      void request.catch(() => { if (this.bundle === request) this.bundle = undefined; });
    }
    return this.bundle;
  }

  async preload(): Promise<void> {
    if (this.manifest.dataUrl) await this.loadBundle();
    else if (this.manifest.frameCount) await this.frame(0);
  }

  async frame(index: number): Promise<SceneSnapshot> {
    if (this.manifest.dataUrl) {
      const bundle = await this.loadBundle();
      const frame = bundle.frames[index];
      if (!frame) throw new Error("Timeline frame is unavailable");
      return reconstructTimelineFrame(bundle.snapshot, frame);
    }
    const descriptor = this.manifest.frames[index];
    if (!descriptor) throw new Error("Timeline frame is unavailable");
    let request = this.frames.get(index);
    if (!request) {
      request = this.load<SceneSnapshot>(descriptor.snapshotUrl, { signal: this.abort.signal });
      this.frames.set(index, request);
      void request.catch(() => { this.frames.delete(index); });
      // Legacy servers return full snapshots: keep only a small playback window.
      for (const previous of this.frames.keys()) {
        if (this.frames.size <= 4) break;
        if (previous !== index) this.frames.delete(previous);
      }
    }
    return request;
  }
}
