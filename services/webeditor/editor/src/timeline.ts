/**
 * Local frame source for trajectory playback (spec v2.1 §8.5~§8.7).
 *
 * The loader this replaces fetched one scene snapshot per frame over HTTP. A
 * decoded TRAJ is already wholly in memory, so a seek is a synchronous function
 * of the frame number and playback never touches the network (§8.7). `frame()`
 * is idempotent: it rebuilds the attach hierarchy from the world file up to the
 * wanted frame (§8.6) rather than mutating whatever happens to be on screen,
 * which is what lets scrubbing, looping and catch-up all take one path.
 */
import { timelineDueIndex } from "./logic";
import type { TrajDriver } from "./drivers";
import type { Traj, TrajAttach, TrajStep } from "./traj";

/** One applied frame: where the playhead ended up and what moved (§8.2). */
export interface TrajFrame {
  index: number;
  simulationTime: number;
  driven: number[];
  missing: number[];
}

export interface Playhead {
  index: number;
  /** the clip is over: playback rests on this frame instead of wrapping (§7.3) */
  finished: boolean;
}

export class TrajFrameProvider {
  private timeTable: number[] | null = null;
  /** every child the Attach table moves, in first-mention order */
  private readonly attachChildren: number[];

  constructor(private readonly traj: Traj, private readonly driver: TrajDriver) {
    this.attachChildren = [...new Set(traj.attaches.map((event) => event.childId))];
  }

  get fps(): number { return this.traj.fps; }
  get totalFrames(): number { return this.traj.totalFrames; }
  get durationSeconds(): number { return this.traj.durationSeconds; }
  get steps(): readonly TrajStep[] { return this.traj.steps; }
  get attaches(): readonly TrajAttach[] { return this.traj.attaches; }

  /** A file that names no frames still has a frame 0 to rest on (§8.5). */
  get lastFrame(): number { return Math.max(0, this.traj.totalFrames - 1); }

  simulationTime(index: number): number {
    return index / this.traj.fps;
  }

  /** `simulationTime = index / fps` (§7.3); built once and reused every tick. */
  times(): number[] {
    if (!this.timeTable) {
      this.timeTable = [];
      for (let index = 0; index <= this.lastFrame; index += 1) this.timeTable.push(this.simulationTime(index));
    }
    return this.timeTable;
  }

  /**
   * Put the scene exactly as the trajectory describes it at `frame`, clamped to
   * the clip. Frames with no sample of their own fall back as §8.5 says, inside
   * `poseAtFrame`.
   */
  frame(index: number): TrajFrame {
    const wanted = Math.min(Math.max(Math.floor(Number.isFinite(index) && index > 0 ? index : 0), 0), this.lastFrame);
    // §8.6: replay from the hierarchy the world file itself declares, so two
    // seeks that end on the same frame end on the same scene.
    for (const childId of this.attachChildren) this.driver.restoreParent(childId);
    for (const event of this.traj.attaches) {
      if (event.frame > wanted) break;
      this.driver.attach(event.childId, event.parentId);
    }
    const { driven, missing } = this.driver.apply(this.traj, wanted);
    return { index: wanted, simulationTime: this.simulationTime(wanted), driven, missing };
  }

  /**
   * Where the clock says the playhead belongs (§8.7): a browser that fell behind
   * skips to the due frame instead of queueing the frames it missed. Past the end
   * of a looping clip it wraps to frame 0; a non-looping one reports `finished`
   * and stays on the last frame rather than jumping back to the default pose (§7.3).
   */
  playhead(current: number, targetSimulationTime: number, loop: boolean): Playhead {
    if (loop && targetSimulationTime >= this.durationSeconds) return { index: 0, finished: false };
    const due = timelineDueIndex(this.times(), current, targetSimulationTime);
    const index = Math.min(Math.max(due, 0), this.lastFrame);
    return { index, finished: !loop && index >= this.lastFrame };
  }
}
