import { describe, expect, it, vi } from "vitest";
import { BROWSER_ID_KEY, CLASS_TWO_ENTRIES, CONVERSATION_SCENE_SWITCH_MESSAGE, PHASE_BLOCK_MESSAGE,
  generateBrowserID, persistentBrowserID, phaseBlockMessage, withBrowserIDHeader,
  phaseOfferMessage, sceneExportName, sceneRevisionLabel, submissionAfterSend, submissionContextMatches,
  conversationListActiveId, reusableConversation,
  submissionForSend } from "./local_edits";
import type { WorldMeta } from "./world";

/** A store is two functions, so the tests hand over an object instead of jsdom's localStorage. */
function store(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return { getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    peek: (key: string) => data.get(key) };
}

/** §4.8.2 only ever names the scene and its revision, so the tests carry a sub-version to prove it stays out. */
function meta(scene_name: string, scene_version: number, scene_sub_version = 0): WorldMeta {
  return { scene_schema_version: 1, scene_version, scene_sub_version, scene_id: 1, scene_name };
}

/**
 * Q114's three-way sort took the rest of this module with it: the gizmo and the joint
 * input (class ①) are gone, and the lighting preset (class ③) is a render-layer
 * override now, tested in `renderer.test.ts` against `toJSON()` staying untouched.
 * Naming a download is one local edit left; the phase gate is the other.
 */
describe("sceneExportName (§4.8.2, Q78)", () => {
  it("names the download after the scene and the revision it was read at", () => {
    expect(sceneExportName(meta("Sample Scene", 12))).toBe("Sample Scene_v12.json");
  });

  it("replaces what a file name cannot carry", () => {
    expect(sceneExportName(meta("a/b\\c:d*e?f\"g<h>i|j", 3))).toBe("a_b_c_d_e_f_g_h_i_j_v3.json");
    // A control character in the middle of a name still has to go.
    expect(sceneExportName(meta("desk\u0000lamp", 3))).toBe("desk_lamp_v3.json");
  });

  it("trims a trailing dot or space so the extension is not the whole name", () => {
    expect(sceneExportName(meta("scene.", 1))).toBe("scene_v1.json");
    expect(sceneExportName(meta("scene   ", 1))).toBe("scene_v1.json");
    expect(sceneExportName(meta("...", 1))).toBe("scene_v1.json");
  });

  it("falls back to a generic stem for an unnamed scene", () => {
    expect(sceneExportName(meta("", 7))).toBe("scene_v7.json");
  });

  /** §4.8.2: the name is the Agent-facing revision. A browser-local edit is not a new version. */
  it("leaves the sub-version out of the file name", () => {
    expect(sceneExportName(meta("Sample Scene", 12, 4))).toBe("Sample Scene_v12.json");
  });
});

/**
 * §8 (S4-d): the status bar is the one place the sub-version shows, and it shows only when there
 * is something to show. `v12` and `v12.0` would be two readings of the same scene, and the whole
 * point of the readout is that a diagnosis can tell "the Agent wrote this" from "the user added
 * four things on top" without opening anything.
 */
describe("sceneRevisionLabel (§8)", () => {
  it("adds the sub-version only when a local edit is riding on the scene", () => {
    expect(sceneRevisionLabel(13, 0)).toBe("v13");
    expect(sceneRevisionLabel(13, 2)).toBe("v13.2");
  });

  it("keeps an Agent write and a browser edit from reading the same", () => {
    // §12.17 ②: a fresh Agent write always carries sub_version 0, so its readout is a bare `v{n}`;
    // a browser edit always carries a non-zero one. Two producers, two shapes, no collision.
    expect(sceneRevisionLabel(14, 0)).toBe("v14");
    expect(sceneRevisionLabel(13, 4)).toBe("v13.4");
    expect(sceneRevisionLabel(14, 0)).not.toContain(".");
  });
});

/**
 * §1.3 (R2): the gate answers, it does not silence. `setup` and an unknown phase let the
 * entry run - the browser the Gateway never told otherwise keeps editing, and layer 2 is
 * the one that refuses the write - while `agent_owned` returns the one fixed sentence.
 */
describe("phaseBlockMessage (§1.3, Q114 ②)", () => {
  it("lets a setup phase edit and names nothing else", () => {
    expect(phaseBlockMessage("setup")).toBeNull();
    expect(phaseBlockMessage(null)).toBeNull();
    expect(phaseBlockMessage(undefined)).toBeNull();
  });

  it("blocks an agent-owned phase with the reason in the message", () => {
    expect(phaseBlockMessage("agent_owned")).toBe(PHASE_BLOCK_MESSAGE);
    expect(PHASE_BLOCK_MESSAGE).toBe("任务进行中，场景暂时不能改。");
  });

  /**
   * **Q127** (2026-10-27): 接管／现场／租约 are names this system uses for its own state, and a user
   * reading them on screen has nothing to do with them. The mechanism stays in §4.6 and §4.8.1.
   */
  it("says the state without naming this system's own vocabulary", () => {
    for (const sentence of [PHASE_BLOCK_MESSAGE, CONVERSATION_SCENE_SWITCH_MESSAGE]) {
      for (const internal of ["租约", "接管", "现场", "§", "resolveUncertain", "sessionState"]) {
        expect(sentence).not.toContain(internal);
      }
    }
    expect(CONVERSATION_SCENE_SWITCH_MESSAGE).toBe("这个会话里有任务正在进行。");
  });
});

/**
 * §4 (S6-b): the modal asks instead of only refusing. **Q122** cut it to one question -- what the
 * `yes` will redo -- because the two explanatory sentences it used to carry are said by the door
 * itself when they are true (`newSessionView`'s `blocked`, written back into this same line).
 */
describe("phaseOfferMessage (§4, S6-b, Q122)", () => {
  it("offers to reopen the file picker for a refused Load Scene", () => {
    expect(phaseOfferMessage({ kind: "load-scene" })).toBe("新建会话并重新打开场景文件选择框？");
  });

  it("names the 预设场景 a refused card was pointing at", () => {
    expect(phaseOfferMessage({ kind: "library-entry", library: "scene", id: 12, name: "装配工位" }))
      .toBe("新建会话并重新加载预设场景「装配工位」？");
  });

  it("names an added asset by name, both kinds sharing the one sentence", () => {
    for (const kind of ["model", "robot"] as const) {
      expect(phaseOfferMessage({ kind: "library-entry", library: kind, id: 3, name: "UR5e" }))
        .toBe("新建会话并重新添加「UR5e」？");
    }
  });

  /** Q122: the offer states the ask and nothing else -- no lease promise, no cancellation claim. */
  it("says nothing about what the door will refuse", () => {
    const offer = phaseOfferMessage({ kind: "load-scene" });
    expect(offer).not.toContain("租约");
    expect(offer).not.toContain("不受影响");
    expect(offer).not.toContain("照原话被拒绝");
  });
});

/**
 * §4 (S4-c, layer 1): the greyed look is the promise, `phaseBlockMessage` is the answer, and the
 * two have to name the same entries. The risk this pins is the one a reader cannot see: an R1
 * control that gets greyed by a selector that reached one attribute too far.
 */
describe("CLASS_TWO_ENTRIES (§4, S4-c)", () => {
  it("names the entries the gate answers", () => {
    expect(CLASS_TWO_ENTRIES).toContain('[data-action="load-scene"]');
    expect(CLASS_TWO_ENTRIES).toContain("[data-asset-kind]");
  });

  it("leaves every R1 control out of the refused set", () => {
    for (const free of ["load-traj", "export-scene", "snapshot", "focus", "frame", "lighting-preset",
      "speech", "toggle-diagnostics", "new-conversation", "data-asset-kind-nav", "data-timeline"]) {
      expect(CLASS_TWO_ENTRIES).not.toContain(free);
    }
  });
});

/**
 * §4/§5.1 (S4): the gateway keys the binding and the resumable session on this string, so the
 * id is the one thing a refresh must not regenerate -- the refreshed tab is the same viewer.
 */
describe("persistentBrowserID (§4, S4-a)", () => {
  it("generates once and writes it back", () => {
    const generated = vi.fn(() => "uuid-first-load");
    const storage = store();
    expect(persistentBrowserID(storage, generated)).toBe("uuid-first-load");
    expect(storage.peek(BROWSER_ID_KEY)).toBe("uuid-first-load");
  });

  it("reads the stored id back and never regenerates", () => {
    const generated = vi.fn(() => "should-not-be-called");
    const storage = store({ [BROWSER_ID_KEY]: "uuid-from-last-time" });
    expect(persistentBrowserID(storage, generated)).toBe("uuid-from-last-time");
    expect(generated).not.toHaveBeenCalled();
    // A read-back is not a rewrite: the value is left exactly where it was.
    expect(storage.peek(BROWSER_ID_KEY)).toBe("uuid-from-last-time");
  });

  it("treats a stored empty string as no id at all", () => {
    // The gateway asks for a non-empty browserID, so an empty one is not an identity.
    const storage = store({ [BROWSER_ID_KEY]: "" });
    expect(persistentBrowserID(storage, () => "fresh")).toBe("fresh");
    expect(storage.peek(BROWSER_ID_KEY)).toBe("fresh");
  });

  it("leaves another key alone", () => {
    const storage = store({ "webeditor.other": "keep me" });
    persistentBrowserID(storage, () => "fresh");
    expect(storage.peek("webeditor.other")).toBe("keep me");
  });

  it("uses randomUUID when the secure-context API is available", () => {
    const randomUUID = vi.fn(() => "uuid-secure");
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes);
    expect(generateBrowserID({ randomUUID, getRandomValues })).toBe("uuid-secure");
    expect(randomUUID).toHaveBeenCalledOnce();
    expect(getRandomValues).not.toHaveBeenCalled();
  });

  it("falls back to a version-four UUID on an ordinary LAN HTTP origin", () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.fill(0xab);
      return bytes;
    });
    const id = generateBrowserID({ getRandomValues });
    expect(id).toBe("abababab-abab-4bab-abab-abababababab");
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it("adds browser identity without replacing existing request headers", () => {
    const init = withBrowserIDHeader({ method: "POST", headers: { "Content-Type": "application/json" } }, "browser-a");
    const headers = new Headers(init.headers);
    expect(init.method).toBe("POST");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-WebEditor-Browser-ID")).toBe("browser-a");
  });
});

/**
 * §7.1: one `clientRequestID` is one submission. The id survives a retry of the same sentence and
 * dies when the attempt does -- which is also the only way out of `request_id_reused`.
 */
describe("submissionForSend / submissionAfterSend (§7.1, S4-a)", () => {
  const generate = () => "id-2";

  it("generates for the first send", () => {
    expect(submissionForSend(null, "pick the cup", generate))
      .toEqual({ command: "pick the cup", clientRequestID: "id-2" });
  });

  it("reuses the id while the same sentence is unanswered", () => {
    const pending = { command: "pick the cup", clientRequestID: "id-1" };
    expect(submissionForSend(pending, "pick the cup", generate)).toBe(pending);
  });

  it("generates for a different sentence beside an open attempt", () => {
    expect(submissionForSend({ command: "pick the cup", clientRequestID: "id-1" }, "wipe the table", generate))
      .toEqual({ command: "wipe the table", clientRequestID: "id-2" });
  });

  it("trims to the same text the caller sends", () => {
    // `submitAgent` trims before it asks, so " pick " and "pick" are one submission.
    const pending = { command: "pick", clientRequestID: "id-1" };
    expect(submissionForSend(pending, "pick", generate).clientRequestID).toBe("id-1");
  });

  it("ends the attempt when the task was accepted", () => {
    expect(submissionAfterSend({ command: "pick", clientRequestID: "id-1" }, null)).toBeNull();
  });

  it("ends the attempt when the id came back spent on other content", () => {
    // A resend of the same id would be refused again; only a new one moves.
    expect(submissionAfterSend({ command: "pick", clientRequestID: "id-1" },
      { code: "request_id_reused" })).toBeNull();
  });

  it("keeps the id open for every failure that started no task", () => {
    const pending = { command: "pick", clientRequestID: "id-1" };
    for (const code of ["task_busy", "session_context_required", "INVALID_PARAM",
      "UPSTREAM_ERROR", "AGENT_UNAVAILABLE", undefined]) {
      expect(submissionAfterSend(pending, { code })).toBe(pending);
    }
    // A fetch that threw before an answer has no code at all, and is the common case.
    expect(submissionAfterSend(pending, {})).toBe(pending);
  });

  it("has nothing to keep when nothing was pending", () => {
    expect(submissionAfterSend(null, { code: "task_busy" })).toBeNull();
  });
});

describe("submissionContextMatches", () => {
  const captured = { sessionID: "s1", conversationId: "c1", sceneJSON: '{"scene_id":1}' };
  it("accepts the unchanged async submission context", () => {
    expect(submissionContextMatches(captured, { ...captured })).toBe(true);
  });
  it("rejects a session, transcript, or scene changed while history persisted", () => {
    expect(submissionContextMatches(captured, { ...captured, sessionID: "s2" })).toBe(false);
    expect(submissionContextMatches(captured, { ...captured, conversationId: "c2" })).toBe(false);
    expect(submissionContextMatches(captured, { ...captured, sceneJSON: '{"scene_id":2}' })).toBe(false);
    expect(submissionContextMatches(captured, null)).toBe(false);
  });
});

describe("conversationListActiveId", () => {
  it("keeps the local selection during a scoped refresh", () => {
    expect(conversationListActiveId("gateway-active", "local-selected")).toBe("local-selected");
  });
  it("adopts the gateway active row only for an unscoped refresh", () => {
    expect(conversationListActiveId("gateway-active")).toBe("gateway-active");
  });
});

describe("reusableConversation", () => {
  const rebound = { conversationId: "selected", sessionID: "new-session" };
  it("reuses the selected row after Gateway has rebound it to the new session", async () => {
    const read = vi.fn().mockResolvedValue(rebound);
    expect(await reusableConversation("selected", "new-session", false, read, () => true)).toBe(rebound);
    expect(read).toHaveBeenCalledWith("selected");
  });
  it("keeps explicit new conversations fresh and rejects a row for another session", async () => {
    const read = vi.fn().mockResolvedValue(rebound);
    expect(await reusableConversation("selected", "new-session", true, read, () => true)).toBeNull();
    expect(read).not.toHaveBeenCalled();
    expect(await reusableConversation("selected", "other-session", false, read, () => true)).toBeNull();
  });
  it("discards a detail response when the user changes selection during the read", async () => {
    let current = true;
    const read = async () => { current = false; return rebound; };
    expect(await reusableConversation("selected", "new-session", false, read, () => current)).toBeNull();
  });
});
