/**
 * What the browser changes on its own (§4.8, §1.3, Q78, Q114).
 *
 * Q114's three-way sort left very little here. The gizmo and the Details joint-angle
 * input (class ①) are deleted outright, and the lighting preset (class ③) moved to
 * `renderer.setLightingOverride`, where it can only ever be a render-layer look -
 * it must not advance the scene's sub-version, reach an export, or change the digest.
 * What is left is the class-② bookkeeping: the name an export downloads, and the
 * phase gate that decides whether a class-② entry runs at all. Two more things the
 * browser has to remember by itself live here since S4: the id a refreshed tab reads
 * back as its own (§4), and the `clientRequestID` one submission goes out under (§7.1).
 */
import type { SessionPhase } from "../../contracts/types";
import type { LibraryKind } from "./library";
import type { WorldMeta } from "./world";

const ILLEGAL_IN_FILENAME = /[\\/:*?"<>|\u0000-\u001f]/g;

/** `{scene_name}_v{scene_version}.json`, with the characters a download rejects replaced (§4.8.2). */
export function sceneExportName(meta: WorldMeta): string {
  const cleaned = meta.scene_name.replace(ILLEGAL_IN_FILENAME, "_").replace(/[. ]+$/, "").trim();
  return `${cleaned || "scene"}_v${meta.scene_version}.json`;
}

/**
 * §8 (S4-d): the status-bar revision, and the one place the sub-version is allowed to show.
 *
 * The export name keeps it out (§4.8.2 -- a browser-local edit is not a new version), but a
 * diagnosis readout has to say which of the two it is looking at: `v12` is what the Agent wrote,
 * `v12.4` is the same scene with four edits this browser added on top of it.
 */
export function sceneRevisionLabel(scene_version: number, scene_sub_version: number): string {
  return scene_sub_version ? `v${scene_version}.${scene_sub_version}` : `v${scene_version}`;
}

/** The one sentence a blocked class-② entry says, so every gate reads the same way (R2).
 * **Q122**: the reason only -- what the modal then offers to do belongs to the second line.
 * **Q127** (2026-10-27): the reason in the user's words -- 接管/现场 is what this system calls the
 * phase, not what the user sees; the modal still only says why the tap stopped. */
export const PHASE_BLOCK_MESSAGE = "任务进行中，场景暂时不能改。";

/** A completed Agent turn still owns the conversation context. Switching its scene must be
 * explicit too, otherwise the Gateway would silently create a new session underneath history. */
export const CONVERSATION_SCENE_SWITCH_MESSAGE = "这个会话里有任务正在进行。";

/**
 * §4 (S6-b): the tap the modal is holding, so the way out it offers is also the retry.
 *
 * `agent_owned` used to end the conversation here: say the sentence, let the user go find the
 * 新建会话 button in the Agent panel and start the click over. The modal asks instead, and a
 * `yes` has to land on the same 现场 the user was aiming at -- hence the intent travels with it.
 */
export type PendingPhaseEdit =
  | { kind: "load-scene" }
  | { kind: "library-entry"; library: LibraryKind; id: number; name: string };

/**
 * §4 (S6-b): the second line of the modal -- the question, and nothing else (**Q122**).
 *
 * The two sentences that used to sit here (opening a session does not cancel the Agent's task; a held
 * lease or an unresolved quarantine refuses the door) are now said only when they are true: the
 * refusal is written back into this same line by `acceptPhaseBlock`, in `newSessionView`'s own words.
 */
export function phaseOfferMessage(pending: PendingPhaseEdit): string {
  const redo = pending.kind === "load-scene" ? "重新打开场景文件选择框"
    : pending.library === "scene" ? `重新加载预设场景「${pending.name}」`
      : `重新添加「${pending.name}」`;
  return `新建会话并${redo}？`;
}

/**
 * §1.3/§4 (R2): whether a class-② entry (asset panel add/remove, scene switch) may run.
 * The judgment returns the message instead of a bare boolean because the caller has to
 * show it -- a greyed control that still swallows the click was the failure this clause
 * replaced. An unknown phase is `setup`: since S4-b a browser really does start with
 * nothing read yet (`sessionPhase: null`, filled by the first `/api/sessionState`), and one
 * the Gateway has never told otherwise keeps editing -- the authoritative layer is the one
 * that refuses the write (§4 layer 2).
 */
export function phaseBlockMessage(phase: SessionPhase | null | undefined): string | null {
  return phase === "agent_owned" ? PHASE_BLOCK_MESSAGE : null;
}

/**
 * §4 (S4-c, layer 1): the entries the refused-look is put on, as one selector.
 *
 * These are exactly the two `phaseBlockMessage` gates answer, so the look and the answer cannot
 * drift apart. Everything R1 leaves free -- `Load Traj`, playback, export, snapshot, the lighting
 * preset -- stays out of it: the freeze is on the 现场, not on looking at it.
 */
export const CLASS_TWO_ENTRIES = '[data-action="load-scene"], [data-asset-kind]';

/** §4 (S4): the one key a refreshed tab reads its own id back from. */
export const BROWSER_ID_KEY = "webeditor.browserID";

/** The two calls `persistentBrowserID` makes, so a test can hand it a Map-shaped stand-in. */
export type IdStore = { getItem(key: string): string | null; setItem(key: string, value: string): void };

export type BrowserCryptoSource = {
  randomUUID?: () => string;
  getRandomValues: (array: Uint8Array) => Uint8Array;
};

/** Generate a browser identity on both secure and ordinary LAN HTTP origins. */
export function generateBrowserID(source: BrowserCryptoSource): string {
  if (typeof source.randomUUID === "function") return source.randomUUID();
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Add the browser identity to every gateway HTTP request without touching OLS requests. */
export function withBrowserIDHeader(init: RequestInit | undefined, browserID: string): RequestInit {
  const headers = new Headers(init?.headers);
  headers.set("X-WebEditor-Browser-ID", browserID);
  return { ...init, headers };
}

/**
 * §4/§5.1 (S4): the id this viewer keeps, refresh after refresh.
 *
 * The gateway's binding, the session it resumes and `browser_new_session_required` are all keyed
 * on this string, so a fresh uuid per page load makes every refresh a different browser -- and a
 * refreshed tab is by definition the same one. An empty stored value is not an id (the gateway
 * asks for a non-empty string), so it generates like a missing one.
 */
export function persistentBrowserID(store: IdStore, generate: () => string): string {
  const stored = store.getItem(BROWSER_ID_KEY);
  if (stored) return stored;
  const fresh = generate();
  store.setItem(BROWSER_ID_KEY, fresh);
  return fresh;
}

/** §7.1: one submission this browser has sent and not yet seen the end of. */
export type PendingSubmission = { command: string; clientRequestID: string };

export type AgentSubmissionContext = {
  sessionID: string;
  conversationId: string;
  sceneJSON: string;
};

/** An async transcript write may finish after the user has switched session or scene. */
export function submissionContextMatches(captured: AgentSubmissionContext,
                                         current: AgentSubmissionContext | null): boolean {
  return current !== null && captured.sessionID === current.sessionID
    && captured.conversationId === current.conversationId && captured.sceneJSON === current.sceneJSON;
}

/** A scoped history refresh may update rows but never replace the user's selected row. */
export function conversationListActiveId(current: string | null, expected?: string): string | null {
  return expected === undefined ? current : expected;
}

/** A scene rebind may have updated the selected journal row before the browser saw the session. */
export async function reusableConversation<T extends { conversationId: string; sessionID?: string | null }>(
  selectedId: string | null, sessionID: string, force: boolean,
  read: (id: string) => Promise<T>, isCurrent: () => boolean,
): Promise<T | null> {
  if (force || selectedId === null || !isCurrent()) return null;
  const detail = await read(selectedId);
  return isCurrent() && detail.conversationId === selectedId && detail.sessionID === sessionID ? detail : null;
}

/**
 * §7.1: the id one send goes out under. The same text that has not been answered yet reuses the id
 * it already used -- one submission is one task, and a fresh id on a retry would start a second one
 * beside the first. A new text, or an old one whose attempt is over, generates.
 */
export function submissionForSend(pending: PendingSubmission | null, command: string,
                                  generate: () => string): PendingSubmission {
  return pending && pending.command === command ? pending : { command, clientRequestID: generate() };
}

/**
 * §7.1: what one answer leaves behind.
 *
 * An acceptance ends the attempt -- the next send of the same sentence is a new task and needs a
 * new id. So does `request_id_reused`: that refusal says this id is spent on a payload that no
 * longer matches, and the only way out of it is a fresh id, not another resend of the same one.
 * Every other failure (busy, no context, no Agent service, unreachable, a thrown fetch) left no
 * submission standing, so the attempt stays open and its id is the one a retry has to carry.
 */
export function submissionAfterSend(pending: PendingSubmission | null,
                                    error: { code?: string } | null): PendingSubmission | null {
  if (error === null || error.code === "request_id_reused") return null;
  return pending;
}
