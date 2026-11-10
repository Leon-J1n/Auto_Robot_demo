import { describe, expect, it } from "vitest";
import { lightingFreeze, mergeSessionPhase, newSessionView, parseSessionPhase } from "./session_state";
import type { SceneLeaseView, SceneQuarantineView, SessionStateView } from "../../contracts/types";

/**
 * §4 (S4-b): the gateway's phase vocabulary, and nothing else is a phase. A `null` here means
 * "this reading says nothing", which is deliberately not the same as `setup` -- the caller keeps
 * believing what it already did rather than opening the class-② gates on a typo.
 */
describe("parseSessionPhase (§4)", () => {
  it("takes the two words the protocol states", () => {
    expect(parseSessionPhase("setup")).toBe("setup");
    expect(parseSessionPhase("agent_owned")).toBe("agent_owned");
  });

  it("reads everything else as silence", () => {
    for (const value of ["Setup", "agent-owned", "", null, undefined, 0, { phase: "setup" }]) {
      expect(parseSessionPhase(value)).toBeNull();
    }
  });
});

describe("mergeSessionPhase (§4, 三者不一致以 /api/sessionState 为准)", () => {
  it("lets the authoritative route overwrite whatever is held", () => {
    expect(mergeSessionPhase("agent_owned", "session-state", "setup"))
      .toEqual({ phase: "setup", reread: false });
    expect(mergeSessionPhase(null, "session-state", "agent_owned"))
      .toEqual({ phase: "agent_owned", reread: false });
  });

  it("lets a live push move the belief in either direction", () => {
    expect(mergeSessionPhase("setup", "broadcast", "agent_owned")).toEqual({ phase: "agent_owned", reread: false });
    expect(mergeSessionPhase("agent_owned", "broadcast", "setup")).toEqual({ phase: "setup", reread: false });
  });

  /** The header answers a question about the world; nobody asked it about the session. */
  it("never lets a response header overwrite a held phase", () => {
    expect(mergeSessionPhase("agent_owned", "world-header", "setup")).toEqual({ phase: "agent_owned", reread: true });
    expect(mergeSessionPhase("setup", "world-header", "agent_owned"))
      .toEqual({ phase: "setup", reread: true });
  });

  it("takes a header as an answer only while the browser believes nothing", () => {
    expect(mergeSessionPhase(null, "world-header", "agent_owned"))
      .toEqual({ phase: "agent_owned", reread: false });
  });

  it("sends a contradiction to the authoritative route and an agreement nowhere", () => {
    expect(mergeSessionPhase("agent_owned", "world-header", "agent_owned"))
      .toEqual({ phase: "agent_owned", reread: false });
  });

  /** The re-read cannot ask for another re-read, or the disagreement path would loop. */
  it("closes the disagreement loop: a session-state reading never asks again", () => {
    for (const held of ["setup", "agent_owned", null] as const) {
      for (const incoming of ["setup", "agent_owned"]) {
        expect(mergeSessionPhase(held, "session-state", incoming).reread).toBe(false);
      }
    }
  });

  it("keeps both the belief and the silence of a reading that says nothing", () => {
    for (const source of ["session-state", "broadcast", "world-header"] as const) {
      expect(mergeSessionPhase("agent_owned", source, undefined)).toEqual({ phase: "agent_owned", reread: false });
      expect(mergeSessionPhase(null, source, "running")).toEqual({ phase: null, reread: false });
    }
  });
});

const IDENTITY = { scene_id: 7, scene_version: 13, scene_sub_version: 0, sceneDigest: "a1b2" };

function lease(over: Partial<SceneLeaseView> = {}): SceneLeaseView {
  return { task_id: 12, browser_id: "browser-mine", session_id: "s-1", scene_identity: IDENTITY,
    acquired_at: 0, ...over };
}

function view(over: Partial<SessionStateView> = {}): SessionStateView {
  return { session_phase: "setup", lease: null, quarantine: null, browser_id: "browser-mine",
    next_object_id: 42, scene_id: 7, scene_version: 13, scene_sub_version: 0, sceneDigest: "a1b2",
    camera_candidates: [4, 5], ...over };
}

function quarantine(taskId: number | null): SceneQuarantineView {
  return { browser_id: "browser-mine", session_id: "s-1", task_id: taskId, retired_lease_token: null,
    scene_identity: taskId === null ? null : IDENTITY, quarantined_at: 0 };
}

/**
 * §9 item 3: camera candidates are evidence for Brain's automatic choice, not a manual picker. The
 * two negative controls below matter more than the positive ones: a
 * door that greyed itself out on `agent_owned` would close the way back out of the phase, and one
 * that greyed itself out on another tab being bound would lock out the hand-over a closed tab is
 * supposed to allow.
 */
describe("newSessionView (§9 item 3)", () => {
  const NAMES: Record<number, string> = { 4: "ID4 腕部", 5: "ID5 顶视" };
  const nameOf = (id: number): string | null => NAMES[id] ?? null;

  it("says so when the authoritative route has not answered yet", () => {
    const door = newSessionView(null, nameOf);
    expect(door.cameras).toEqual([]);
    expect(door.blocked).toBe("还没读到会话状态，新建会话失败");
  });

  it("lists the reported cameras in the reported order, never as a choice already made", () => {
    expect(newSessionView(view({ camera_candidates: [5, 4] }), nameOf).cameras)
      .toEqual([{ id: 5, label: "#5 ID5 顶视" }, { id: 4, label: "#4 ID4 腕部" }]);
    expect(newSessionView(view({ camera_candidates: [5, 4] }), nameOf).blocked).toBeNull();
  });

  it("keeps an id whose name this tab has not caught up with", () => {
    expect(newSessionView(view(), () => null).cameras)
      .toEqual([{ id: 4, label: "相机 #4" }, { id: 5, label: "相机 #5" }]);
  });

  it("offers the door while the phase is `agent_owned` -- that is the phase this exits", () => {
    expect(newSessionView(view({ session_phase: "agent_owned" }), nameOf).blocked).toBeNull();
  });

  it("offers the door to a tab that is not the bound one, because a closed tab hands over", () => {
    expect(newSessionView(view({ browser_id: "browser-other" }), nameOf).blocked).toBeNull();
  });

  it("refuses while a lease stands, and names the task that holds it", () => {
    const door = newSessionView(view({ lease: lease({ task_id: 9 }) }), nameOf);
    expect(door.cameras).toEqual([]);
    expect(door.blocked).toBe("任务 #9 进行中，新建会话失败");
  });

  /**
   * **Q128** (2026-10-27): `allowActiveTask` is the flag `createSession` is about to put in the
   * `POST /sessions` body, and the gateway's own rule is `held == "lease" and not allow_active_task`
   * -- with it, the running task keeps its lease in the old session's context. Both explicit entries
   * set it, so a door that refused anyway was a failure the browser invented for itself, and §4's
   * modal (which only opens in `agent_owned`) was offering a 新建会话 that could never happen.
   */
  it("opens for the caller that declared the task may keep running", () => {
    const door = newSessionView(view({ lease: lease({ task_id: 9 }) }), nameOf, true);
    expect(door.blocked).toBeNull();
    expect(door.cameras).toEqual([{ id: 4, label: "#4 ID4 腕部" }, { id: 5, label: "#5 ID5 顶视" }]);
  });

  /** §8.3: 新建会话 is not evidence of an unlock, so it is not offered as one either. */
  it("refuses a quarantine, because opening a session proves nothing about it", () => {
    const door = newSessionView(view({ quarantine: quarantine(12) }), nameOf);
    expect(door.blocked).toBe("上一个任务的结果还没确认，新建会话失败");
    expect(door.cameras).toEqual([]);
  });

  /** Q128 opens the lease branch only: a quarantine refuses whoever asks, flag set or not. */
  it("refuses a quarantine even to the caller that declared allowActiveTask", () => {
    expect(newSessionView(view({ quarantine: quarantine(12) }), nameOf, true).blocked)
      .toBe("上一个任务的结果还没确认，新建会话失败");
  });

  it("still offers a session when the scene has no camera candidates", () => {
    const door = newSessionView(view({ camera_candidates: [] }), nameOf);
    expect(door.cameras).toEqual([]);
    expect(door.blocked).toBeNull();
  });
});

/**
 * §2.3's last item, delivered with §9: the lighting preset is a render-layer look, so the *phase*
 * must not gate it (R1 says `agent_owned` leaves it free) -- only a held lease does, because that
 * is what makes collected pixels have to stay repeatable.
 */
describe("lightingFreeze (§2.3, §9)", () => {
  it("leaves the preset alone before the route answers and while no lease stands", () => {
    expect(lightingFreeze(null)).toBeNull();
    expect(lightingFreeze(view())).toBeNull();
  });

  it("is silent in `agent_owned` with no lease -- R1's negative control", () => {
    expect(lightingFreeze(view({ session_phase: "agent_owned" }))).toBeNull();
  });

  it("freezes while a task holds the scene, and names the task", () => {
    expect(lightingFreeze(view({ lease: lease({ task_id: 9 }) })))
      .toBe("任务 #9 进行中，光照和阴影暂时改不了，任务结束后自动恢复");
    expect(lightingFreeze(view({ lease: lease({ browser_id: "browser-other" }) })))
      .toContain("任务 #12");
  });

  /** A quarantine means the last write has no conclusion; the lease that caused it is gone. */
  it("does not freeze on a quarantine, only on the lease itself", () => {
    expect(lightingFreeze(view({ lease: null, quarantine: quarantine(12) }))).toBeNull();
  });
});

/**
 * **Q127** (2026-10-27), §12.24 ①: the sentences this module writes are read by a person who has no
 * use for this system's own names. 租约／接管／现场／冻结 say the same thing as the plain state in
 * protocol vocabulary, and `GET /api/sessionState`／`POST /resolveUncertain` are routes a user cannot
 * type into the page. The mechanism those words carried stays in the spec (§4.6, §4.8.1, §9) -- this
 * is a negative control on the interface, not on the rule.
 */
describe("the sentences name nothing internal (§12.24 ①, Q127)", () => {
  const sentences = [
    newSessionView(null, () => null).blocked,
    newSessionView(view({ quarantine: quarantine(12) }), () => null).blocked,
    newSessionView(view({ lease: lease({ task_id: 9 }) }), () => null).blocked,
    lightingFreeze(view({ lease: lease({ task_id: 9 }) })),
  ];

  it("has four sentences to check, so a silent refactor cannot empty the list", () => {
    expect(sentences.filter((sentence) => typeof sentence === "string")).toHaveLength(4);
  });

  it("keeps this system's vocabulary out of what the user reads", () => {
    for (const sentence of sentences) {
      for (const internal of ["租约", "接管", "现场", "冻结", "§", "sessionState", "resolveUncertain"]) {
        expect(sentence!).not.toContain(internal);
      }
    }
  });
});
