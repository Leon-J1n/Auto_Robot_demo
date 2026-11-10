/** The scene identity fields used when deciding whether a session acknowledgement is stale. */
export type SceneIdentity = {
  scene_id: number;
  scene_version: number;
  scene_sub_version: number;
};

/** A normal edit keeps the session; switching to another scene document does not. */
export function sceneDocumentChanged(previous: SceneIdentity, current: SceneIdentity): boolean {
  return previous.scene_id !== current.scene_id;
}

/** A notice belongs to one durable session id. */
export function sessionChanged(previous: string | null | undefined, current: string | null | undefined): boolean {
  return previous != null && current != null && previous !== current;
}

/** What this tab knows about its own reports when a `scene.applied` arrives (规格 §5.4, Q147). */
export type LocalReports = {
  session_id: string | null | undefined;
  /** The `report_seq` of the newest report this page actually sent; 0 when it has sent none. */
  report_seq: number;
};

/** The two keys Q147 added to the notice. */
export type AppliedNotice = {
  session_id: string | null;
  ack_report_seq: number | null;
};

export type AppliedVerdict = "other_session" | "late_echo" | "confirm" | "apply";

/**
 * The four-way verdict of 规格 §5.4, in the order the reader has to ask them.
 *
 * A tab that switched scenes 1→2→3 must not let the echo of 2 -- which can land after 3 because the
 * gateway waits on a Brain session call before it broadcasts -- talk it into re-reading 2. And a
 * landing this tab did not ask for is an Agent or caller write, which the Agent is not allowed to
 * move under a running task, so that one is believed outright.
 */
export function appliedNoticeVerdict(local: LocalReports, notice: AppliedNotice): AppliedVerdict {
  if (sessionChanged(local.session_id, notice.session_id)) return "other_session";
  if (notice.ack_report_seq === null) return "apply";
  if (notice.ack_report_seq < local.report_seq) return "late_echo";
  if (notice.ack_report_seq === local.report_seq) return "confirm";
  // Above this tab's own count: a sibling tab under the same browser_id reported, so this is a
  // write from elsewhere, not an acknowledgement of anything we sent.
  return "apply";
}
