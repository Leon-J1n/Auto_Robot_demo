/**
 * What one Agent task shows in the panel (§8, Q116, spec §5.15 rule 8).
 *
 * Three rules shape everything here, and they pull in different directions:
 *
 * 1. The state **word** is upstream's. `agent_state` is quoted exactly as it arrived and
 *    translated nothing -- a table by name would become a mistranslation the day the service adds
 *    a state (§7.4). The gateway writes no Chinese of its own either; `message` is upstream text.
 * 2. The **meaning** is `outcome`. The sentence and the badge class come from `terminal` +
 *    `outcome`, never from a state name, and the five outcomes are the whole enumeration.
 * 3. No finished task is called a success. The table's "明确禁止" column is the reason this module
 *    owns the copy at all: `scene_updated` says 场景已更新, `plan_ready` says 计划已生成，尚未执行,
 *    `clarification_required` says the question and reads as neither a failure nor an execution.
 *
 * The service bar's wording lives here for the same reason (§8): it is the panel's other copy, and
 * it obeys the same hard rule -- an `unreachable` probe is a statement about the *service*, and may
 * never colour a task row or word its end.
 */
import type { AgentJob, AgentOutcome, CommitReceiptView, ServiceHealthView } from "../../contracts/types";

/** §5.15 rule 8: the one fixed sentence a plan that was only written down gets. */
export const PLAN_READY_COPY = "计划已生成，尚未执行";
/** §5.15 rule 8: what landed is a new 现场, which is a fact about the scene, not a job well done. */
export const SCENE_UPDATED_COPY = "场景已更新";
/** The digest is 64 hex; the row is a diagnosis aid, and the panel is 285 px wide. */
const DIGEST_IN_ROW = 8;
const SCENE_EDIT_TRANSFORM_COLLISION = "scene_edit_transform_collision";
const SCENE_EDIT_LAYOUT_COLLISION = "scene_edit_layout_collision";

/** Scene-edit collision codes are safe refusals, not generic task failures. */
function failureSentence(job: AgentJob): string {
  const source = job.message || job.failure_reason || job.agent_state;
  if (/ValidationError|validation error for|Traceback|errors\.pydantic\.dev/.test(source)) {
    const code = source.match(/^([a-z][a-z0-9_]+):/)?.[1] ?? "semantic_validation_failed";
    return `任务未通过语义校验，请重新说明对象与参照物。（${code}；诊断 task-${job.task_id}）`;
  }
  if (source.includes(SCENE_EDIT_TRANSFORM_COLLISION)) {
    return `编辑失败：目标位置存在碰撞或超出可用范围，场景未改变。（${source}）`;
  }
  if (source.includes(SCENE_EDIT_LAYOUT_COLLISION)) {
    return `编辑失败：新增物体的位置与现有场景冲突，场景未改变。（${source}）`;
  }
  return source;
}

/** §5.15 rule 8: the row's pieces, decided once per job so the markup stays a formatting step. */
export type JobRow = {
  /** The sentence the row says: FE copy for the two outcomes that need it, upstream text elsewhere. */
  text: string;
  /** `agent_state` exactly as it arrived (§7.4), or empty when the service named nothing. */
  state: string;
  /** The new four-tuple of a `scene_updated` landing, from this gateway's own receipt. */
  identity: string | null;
  /** The skill order `plan.commands` names, strings and objects alike (§7.3's shape allows both). */
  commands: string[];
  /** `metrics.model_calls` as a count, including a real `0` -- an absent key says nothing. */
  modelCalls: string | null;
  /** Downloads the gateway's whitelist offers for this task, as proxy paths (§5.9). */
  artifacts: { name: string; url: string }[];
};

/** §5.15 rule 8: the sentence one outcome is allowed to say. */
function outcomeSentence(job: AgentJob): string {
  // A gateway/browser retry can briefly publish the terminal text before the outcome field is
  // attached.  These two Brain codes are still unambiguous safe refusals, so never expose them as
  // a generic raw task line even during that short transition.
  const source = job.message || job.failure_reason || job.agent_state;
  if (source.includes(SCENE_EDIT_TRANSFORM_COLLISION) || source.includes(SCENE_EDIT_LAYOUT_COLLISION)) {
    return failureSentence(job);
  }
  switch (job.outcome) {
    case "plan_ready":
      return PLAN_READY_COPY;
    case "scene_updated":
      return SCENE_UPDATED_COPY;
    case "answered":
    case "clarification_required":
      // A clarification is the question the Agent is waiting on. It is not a failure, and the
      // reply is the whole of what the user needs to answer it.
      return job.reply ?? job.message;
    case "failed":
      return failureSentence(job);
    default:
      // No outcome yet: a running row says what the service said, and nothing about its end.
      return job.message;
  }
}

/** The last receipt that says the write landed -- `unknown` and `rejected` name no new 现场. */
function landedIdentity(receipts: CommitReceiptView[]): string | null {
  for (const receipt of [...receipts].reverse()) {
    if (receipt.status !== "committed") continue;
    const { scene_id: sceneId, scene_version: version, scene_sub_version: subVersion, sceneDigest } = receipt;
    if (sceneId === undefined || version === undefined || sceneDigest === undefined) continue;
    // The sub-version is written out even when it is the `0` an Agent write always carries: that
    // zero is the fact §12.17 ② pins, and "v13.0" is what says no local edit is riding on it.
    const sub = subVersion === undefined ? "" : `.${subVersion}`;
    return `现场 #${sceneId} · v${version}${sub} · 摘要 ${sceneDigest.slice(0, DIGEST_IN_ROW)}`;
  }
  return null;
}

/** One `plan.commands` entry as text; a null means it names no step at all and drops out. */
function commandText(item: unknown): string | null {
  if (typeof item === "string") return item;
  if (typeof item === "number" || typeof item === "boolean") return String(item);
  // The flow doc §7.3 lets a command be an object; quoting it is honest where guessing a key
  // (`step`? `skill`? `name`?) would silently drop the step the Agent actually named.
  if (item && typeof item === "object") return JSON.stringify(item);
  return null;
}

/** §8: everything one job contributes to the panel, with nothing invented for a missing field. */
export function jobRow(job: AgentJob): JobRow {
  const plan = job.plan?.commands;
  const calls = job.metrics?.model_calls;
  return {
    text: outcomeSentence(job),
    state: job.agent_state,
    identity: job.outcome === "scene_updated" ? landedIdentity(job.commit_receipts) : null,
    commands: Array.isArray(plan) ? plan.map(commandText).filter((line): line is string => line !== null) : [],
    modelCalls: typeof calls === "number" && Number.isFinite(calls) ? `Qwen 调用 ${calls} 次` : null,
    artifacts: job.artifacts.map((name) => ({ name, url: `/tasks/${job.task_id}/artifacts/${encodeURIComponent(name)}` })),
  };
}

/**
 * §7.4 rule 2: `terminal === false` is the only thing that spins. Rows restored from
 * `localStorage` (§4.9) predate the flag, so a missing one reads as ended -- otherwise a
 * task from last week would turn forever. It is a spinner rule only; no protocol decision
 * is made from it.
 */
export function jobWorking(message: { agentState?: string; terminal?: boolean }): boolean {
  return message.agentState !== undefined && (message.terminal ?? true) === false;
}

/** §7.4 rule 4 + §8: the badge class comes from `terminal` and `outcome`, never from a state name. */
export function jobBadgeClass(message: { agentState?: string; terminal?: boolean;
                                          outcome?: AgentOutcome | null }): string {
  if (jobWorking(message)) return "is-pending";
  return message.outcome === "failed" ? "is-failed" : "is-done";
}

/** §8 (R9): the three words the gateway uses for the Agent service, in the bar's own wording. */
const HEALTH_COPY: Record<ServiceHealthView["status"], string> = {
  ready: "就绪", degraded: "降级", unreachable: "不可达",
};

/**
 * §8: the service bar. `null` is the case the bar has to keep separate -- the *gateway* did not
 * answer, which is not the same reading as the gateway saying the Agent service is unreachable.
 */
export function agentHealthLabel(view: ServiceHealthView | null): string {
  if (view === null) return "Agent 服务：状态未知";
  const detail = view.detail ? `（${view.detail}）` : "";
  return `Agent 服务：${HEALTH_COPY[view.status]}${detail}`;
}

/**
 * §8: the one sentence a stalled task list may add, and it is about *this tab's* reads.
 *
 * The gateway's `AGENT_POLL_ERROR_LIMIT` is the only thing that ends a task, and that counter stays
 * inside the gateway (§5.15: `poll_errors` is internal, never published). So the number here counts
 * the consecutive job reads this browser could not complete -- a fact it owns -- and it says nothing
 * about how the task ended.
 */
export function pollStallNotice(failures: number): string | null {
  return failures > 0 ? `任务状态暂无法更新（连续轮询失败 ${failures} 次）` : null;
}
