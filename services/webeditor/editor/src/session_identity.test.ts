import { describe, expect, it } from "vitest";
import { appliedNoticeVerdict, sceneDocumentChanged, sessionChanged } from "./session_identity";

const base = { scene_id: 3, scene_version: 4, scene_sub_version: 0 };

describe("session identity refresh", () => {
  it("invalidates a notice when the browser switches scene documents", () => {
    expect(sceneDocumentChanged(base, { ...base, scene_id: 1 })).toBe(true);
  });

  it("keeps a notice for ordinary version and sub-version edits", () => {
    expect(sceneDocumentChanged(base, { ...base, scene_version: 5 })).toBe(false);
    expect(sceneDocumentChanged(base, { ...base, scene_sub_version: 1 })).toBe(false);
  });

  it("detects an authoritative external session replacement", () => {
    expect(sessionChanged("old", "new")).toBe(true);
    expect(sessionChanged("same", "same")).toBe(false);
    expect(sessionChanged(null, "new")).toBe(false);
  });
});

/** §5.4 (Q147): the four-way verdict a tab makes about a notice before it touches its world. */
describe("scene.applied acknowledgement", () => {
  const tab = (report_seq: number, session_id: string | null = "session-a") => ({ report_seq, session_id });
  const frame = (over: Partial<{ session_id: string | null; ack_report_seq: number | null }> = {}) =>
    ({ session_id: null, ack_report_seq: null, ...over });

  it("ignores a notice that landed in somebody else's session", () => {
    expect(appliedNoticeVerdict(tab(3), frame({ session_id: "session-b", ack_report_seq: 3 })))
      .toBe("other_session");
    // Asked first, so a late echo from another session is dropped for the session reason.
    expect(appliedNoticeVerdict(tab(3), frame({ session_id: "session-b", ack_report_seq: 2 })))
      .toBe("other_session");
  });

  it("filters on the session only when both sides actually name one", () => {
    // Before the first `POST /sessions` neither side has a session to compare; the gateway reports
    // `null` and a fresh tab has not read the route yet. Dropping notices there would freeze setup.
    expect(appliedNoticeVerdict(tab(0, null), frame({ session_id: "session-b" }))).toBe("apply");
    expect(appliedNoticeVerdict(tab(2, "session-a"), frame({}))).toBe("apply");
  });

  it("ignores the echo of a switch this tab already left", () => {
    // Clicked 1, 2, 3 in a row: the tab is on 3 and the gateway's echo of 2 arrives late because
    // its report handler awaits a Brain session call. Re-reading it is what rolled the screen back.
    expect(appliedNoticeVerdict(tab(3), frame({ ack_report_seq: 2 }))).toBe("late_echo");
    expect(appliedNoticeVerdict(tab(3), frame({ ack_report_seq: 1 }))).toBe("late_echo");
  });

  it("confirms the report it is still waiting for", () => {
    expect(appliedNoticeVerdict(tab(3), frame({ ack_report_seq: 3 }))).toBe("confirm");
    expect(appliedNoticeVerdict(tab(1), frame({ ack_report_seq: 1 }))).toBe("confirm");
  });

  it("believes a landing it did not ask for", () => {
    // A caller's write, a `/commitWorld`, a session switch: none of them is a report, so none of
    // them carries a number, and the Agent is not allowed to move the scene under a running task.
    expect(appliedNoticeVerdict(tab(3), frame({}))).toBe("apply");
    expect(appliedNoticeVerdict(tab(0), frame({}))).toBe("apply");
    // A sibling tab under the same browser_id reported: higher than anything this page sent.
    expect(appliedNoticeVerdict(tab(3), frame({ ack_report_seq: 4 }))).toBe("apply");
  });
});
