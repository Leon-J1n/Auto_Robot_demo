/**
 * §5.4/§5.8 (Q147): the acknowledgement a scene switch waits for.
 *
 * `main.ts` wires the DOM on import, so this pins the two functions as written; the four-way
 * verdict itself is a pure export and is tested in `session_identity.test.ts`. What is pinned here
 * is the shape of the defect: the tab that clicked is the one holding scene 3, the gateway's echo
 * of scene 2 can land after it (the report handler awaits a Brain session call before it
 * broadcasts), and the old rule "four-tuple differs, so re-read `/getWorld`" then painted 2 over 3
 * and re-reported it -- both sides ending on the scene the user had already left. So the report
 * carries a number, the echo carries it back, and the reader that recognises its own number is not
 * allowed to touch the world.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const mainSource = readFileSync(new URL("./main.ts", import.meta.url), "utf-8");

function body(marker: string, end: string, label: string): string {
  const start = mainSource.indexOf(marker);
  expect(start, `main.ts no longer contains ${marker}`).toBeGreaterThanOrEqual(0);
  const stop = mainSource.indexOf(end, start);
  expect(stop, `${label} no longer ends at ${end}`).toBeGreaterThan(start);
  return mainSource.slice(start, stop);
}

function reportBody(): string {
  return body("function reportWorld(): void {", "\nfunction worldStatus()", "reportWorld()");
}

function adoptBody(): string {
  return body("async function adoptWorld(broadcast: SceneApplied): Promise<void> {",
    "\nasync function applyAppliedNotice", "adoptWorld()");
}

describe("§5.8 the number a report carries", () => {
  it("counts only reports that were actually sent", () => {
    const report = reportBody();
    expect(report).toContain("worldReportSeq += 1;");
    // The not-open branch returns before the counter moves: a retry is a new report, and a report
    // that never left must not raise the number the echo is compared against.
    const retry = report.slice(0, report.indexOf("worldReportSeq += 1;"));
    expect(retry).toContain("worldReportRetryCount < 30");
    expect(retry).toContain("return;");
    expect(retry.slice(retry.indexOf("return;")).indexOf("worldReportSeq")).toBeLessThan(0);
  });

  it("sends the number beside the document", () => {
    const sent = reportBody().slice(reportBody().indexOf("socket.send("));
    expect(sent).toContain("report_seq: worldReportSeq");
    expect(sent).toContain("camera_object_ids: state.world.cameraObjectIds()");
  });

  it("never persists it", () => {
    // A refreshed tab that resumed an old number would read its own first echo as another tab's
    // write and re-adopt the gateway's scene for nothing.
    expect(mainSource).toContain("let worldReportSeq = 0;");
    expect(mainSource).not.toMatch(/localStorage[^\n]*worldReportSeq/);
    expect(mainSource).not.toMatch(/worldReportSeq[^\n]*localStorage/);
  });
});

describe("§5.4 what a reader does with a notice", () => {
  it("asks the acknowledgement before it touches the world", () => {
    const adopt = adoptBody();
    expect(adopt).toContain("appliedNoticeVerdict(localReportReading(), broadcast)");
    expect(adopt.indexOf("appliedNoticeVerdict")).toBeLessThan(adopt.indexOf("applyAppliedNotice"));
    // The session mismatch is the one verdict worth re-asking: this tab's reading can be a
    // round-trip behind, and a session switch announces itself over the socket first.
    expect(adopt).toMatch(/if \(verdict === "other_session"\) \{[\s\S]*?await refreshSessionState\(\);/);
    expect(adopt).toMatch(/if \(verdict === "late_echo"\) return;/);
  });

  it("confirms its own report without re-reading or re-reporting", () => {
    const adopt = adoptBody();
    const confirm = body("if (verdict === \"confirm\") {", "\n  }", "the confirm branch");
    expect(adopt).toContain("if (verdict === \"confirm\") {");
    expect(confirm).not.toContain("reportWorld(");
    expect(confirm).not.toContain("fetchJSON(");
    expect(confirm).not.toContain("state.world =");
    expect(confirm).toContain("refreshSessionState()");
  });

  it("keeps the newest notice that arrives mid re-read instead of dropping it", () => {
    const adopt = adoptBody();
    expect(adopt).toContain("if (adoptingWorld) { queuedAdoption = broadcast; return; }");
    expect(adopt).toContain("notice = queuedAdoption;");
    // The guard is the entry's, so a queued frame was already judged when it arrived.
    expect(adopt.indexOf("appliedNoticeVerdict")).toBeLessThan(adopt.indexOf("queuedAdoption = broadcast"));
  });

  it("still re-reads a landing it did not report", () => {
    const apply = body("async function applyAppliedNotice(broadcast: SceneApplied): Promise<void> {",
      "\n/** §8/Q87", "applyAppliedNotice()");
    expect(apply).toContain('const read = await fetchJSON<unknown>("/getWorld")');
    expect(apply).toContain("state.world = world;");
    // The four-tuple is still the test for a caller's write, and it re-reports the cameras (§5.8).
    expect(apply).toContain("if (broadcast.source_request_id !== null) reportWorld();");
  });
});
