/**
 * Where the browser's belief about the session phase comes from (§4, S4).
 *
 * Three readings exist and they are not equally authoritative. `GET /api/sessionState` is the
 * one that decides: it reads the persisted state, and it is the only reading that also names the
 * bound browser, the lease and the quarantine. The two `/events` pushes -- `session.phase_changed`
 * and the phase riding on `scene.applied` -- are live notices of the same edge, so they move the
 * belief. The `X-Session-Phase` header of a `/getWorld` is a stamp on an answer we asked for
 * another reason: it never overwrites what we hold, and when it disagrees it sends us back to the
 * authoritative route (§6.3's "不一致时以 `/api/sessionState` 为准").
 *
 * The same route is also the only place a 新建会话 can find out which cameras exist at all, so
 * §9 item 3's door (`newSessionView`) lives here beside the phase readings it is judged from.
 */
import type { SessionPhase, SessionStateView } from "../../contracts/types";

/** The three readings §4 lists, in the order §4 ranks them. */
export type PhaseSource = "session-state" | "broadcast" | "world-header";

/**
 * §4: one reading as the protocol states it. Anything else -- a typo, a missing header, a phase
 * from a gateway older than this clause -- says nothing, and silence must not look like `setup`:
 * the caller keeps whatever it already believed.
 */
export function parseSessionPhase(value: unknown): SessionPhase | null {
  return value === "setup" || value === "agent_owned" ? value : null;
}

/**
 * §4: what one reading leaves the browser holding, and whether it has to go check.
 *
 * `reread` is the disagreement path: a `/getWorld` header that contradicts the held phase is the
 * only reading that can raise it, and the answer to it is one `GET /api/sessionState` -- which
 * closes the loop by definition, because the authoritative source never sets the flag.
 */
export function mergeSessionPhase(held: SessionPhase | null, source: PhaseSource,
                                  incoming: unknown): { phase: SessionPhase | null; reread: boolean } {
  const read = parseSessionPhase(incoming);
  if (read === null) return { phase: held, reread: false };
  if (source === "world-header") {
    // With nothing believed yet, a stamp is still better than no answer at all.
    if (held === null) return { phase: read, reread: false };
    return { phase: held, reread: read !== held };
  }
  return { phase: read, reread: false };
}

/**
 * §2.3 (class ③'s last item) / §9: a held lease freezes the lighting preset.
 *
 * The preset never edits the document, so unlike a class-② entry it is not gated by the *phase* --
 * `agent_owned` alone leaves it free (R1). What stops it is a lease: while a task holds the scene,
 * §9.1 requires the two live `getWorld` reads around a capture to agree, and §7.2 additionally wants
 * the pixels themselves repeatable, which a preset swapped mid-collection would break. Releasing the
 * lease unfreezes it; nothing here needs a new broadcast, since the lease is read from the same
 * authoritative route as the phase.
 *
 * **Q127** (2026-10-27) moved the mechanism out of this sentence and left it above: the user sees
 * which task is running and which two controls are unavailable, not the words 冻结/租约/可重复. The
 * one string covers the lighting select *and* the shadows checkbox because both freeze together for
 * the same reason -- naming both costs nothing and keeps the shadow click from being told about
 * lights. The Gateway's own words stay untranslated (§4 layer 2).
 */
export function lightingFreeze(view: SessionStateView | null): string | null {
  if (view === null || view.lease === null) return null;
  return `任务 #${view.lease.task_id} 进行中，光照和阴影暂时改不了，任务结束后自动恢复`;
}

/** §9 item 3: one camera this panel may start a session on. */
export type CameraChoice = { id: number; label: string };

/** §9 item 3: what 新建会话 can offer right now, and -- when it cannot -- the reason it says so. */
export type NewSessionView = {
  cameras: CameraChoice[];
  /** null exactly when the request would be worth sending. */
  blocked: string | null;
};

/**
 * §9 item 3: the 候选清单 is evidence for Brain's automatic camera choice, not a manual picker.
 *
 * The cameras come from the browser's own report (`World.cameraObjectIds()` through
 * `browser.state_sync`), because the component is the only honest test for "is this a camera" and
 * UR5e's template really does hold two. Since **Q123** nothing here is user-pickable: the list is
 * evidence the door reads, and Brain/Gateway choose the `camera_id` from it.
 *
 * Three readings do *not* block it, each for a reason worth keeping in mind before adding a fourth:
 * `agent_owned` is the phase 新建会话 exists to leave; another tab being bound is a fact about the
 * binding rather than about the scene, and a closed bound tab is supposed to hand the session over
 * (refusing here would lock that recovery out -- the gateway answers `browser_new_session_required`
 * when the other tab is actually alive); and `nameOf` missing a name changes nothing, since an id
 * the browser reported is a camera whether or not this tab's world has caught up with it yet. An
 * empty list is also not a block: Brain may bind a session without a camera and report an explicit
 * platform error if a task needs one.
 *
 * **Q128** (2026-10-27) adds the fourth case to the lease only: `allowActiveTask` is the same flag
 * the caller already puts in the `POST /sessions` body, and the gateway's rule is
 * `held == "lease" and not allow_active_task` -- with the flag it moves that lease into the old
 * session's own context and lets the task finish there. Both explicit entries pass it, so refusing
 * a held lease locally used to make that parameter dead code and the button *certainly* fail exactly
 * when the modal that offers it is on screen (`#phase-block` only appears in `agent_owned`). A
 * quarantine still blocks, with or without the flag (§8.3: opening a session is not evidence of an
 * unlock), and so does `commit_unconfirmed` -- over there, because `SceneLeaseView` does not carry
 * that field and this door must not guess about a write whose outcome nobody has read.
 *
 * **Q127**: every sentence here is the current state in the user's own reach. 租约/接管/现场 and the
 * route names (`GET /api/sessionState`, `POST /resolveUncertain`) live in these comments, not in the
 * toast.
 */
export function newSessionView(view: SessionStateView | null,
                               nameOf: (id: number) => string | null = () => null,
                               allowActiveTask = false): NewSessionView {
  if (view === null) return { cameras: [], blocked: "还没读到会话状态，新建会话失败" };
  if (view.quarantine !== null) {
    return { cameras: [], blocked: "上一个任务的结果还没确认，新建会话失败" };
  }
  if (view.lease !== null && !allowActiveTask) {
    return { cameras: [], blocked: `任务 #${view.lease.task_id} 进行中，新建会话失败` };
  }
  const cameras = view.camera_candidates.map((id) => {
    const name = nameOf(id);
    return { id, label: name === null ? `相机 #${id}` : `#${id} ${name}` };
  });
  return { cameras, blocked: null };
}
