import { describe, expect, it } from "vitest";
import { agentHealthLabel, jobBadgeClass, jobRow, jobWorking, PLAN_READY_COPY, pollStallNotice, SCENE_UPDATED_COPY } from "./result_view";
import type { AgentJob, CommitReceiptView } from "../../contracts/types";

const DIGEST = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90";

function job(over: Partial<AgentJob> = {}): AgentJob {
  return { id: "12", task_id: 12, conversation_id: "c1", command: "pick the cup", agent_state: "已完成", terminal: true,
    state_detail: null, outcome: null, reply: null, plan: null, metrics: null, artifacts: [],
    commit_receipts: [], message: "已完成", failure_reason: null, created_at: 0, updated_at: 0, ...over };
}

function receipt(over: Partial<CommitReceiptView> = {}): CommitReceiptView {
  return { transactionID: "tx-1", status: "committed",
    scene_id: 7, scene_version: 13, scene_sub_version: 0, sceneDigest: DIGEST, ...over };
}

/**
 * §5.15 rule 8's table is the whole of this file's subject: five outcomes, one sentence each, and
 * a "明确禁止" column that says what must never be said. `message` is what the gateway publishes
 * (upstream text: `agent_state or failure_reason or ''`), so every case names it deliberately to
 * prove the row is not just passing it through.
 */
describe("jobRow (§5.15 rule 8)", () => {
  it("keeps old validation traces out of the panel while retaining a diagnostic", () => {
    const text = jobRow(job({outcome: "failed", message: "provider_error: ValidationError: 1 validation error for SpatialRelation input_value=secret"})).text;
    expect(text).toContain("provider_error");
    expect(text).toContain("task-12");
    expect(text).not.toContain("input_value");
    expect(text).not.toContain("ValidationError");
  });
  it("says the scene changed for a scene_updated landing, not that it went well", () => {
    const row = jobRow(job({ outcome: "scene_updated", message: "已完成", reply: "机械臂执行成功",
      commit_receipts: [receipt()] }));
    expect(row.text).toBe(SCENE_UPDATED_COPY);
    expect(row.identity).toBe(`现场 #7 · v13.0 · 摘要 ${DIGEST.slice(0, 8)}`);
  });

  it("says only the fixed sentence for a plan that was written down", () => {
    expect(jobRow(job({ outcome: "plan_ready", message: "已生成计划" })).text).toBe(PLAN_READY_COPY);
    expect(PLAN_READY_COPY).toBe("计划已生成，尚未执行");
  });

  it("answers an answered outcome with the reply, not the status line", () => {
    expect(jobRow(job({ outcome: "answered", reply: "桌面上有两只杯子", message: "已完成" })).text)
      .toBe("桌面上有两只杯子");
  });

  /** A question is not a failure and not an execution: it is the thing the user has to answer. */
  it("shows a clarification as the question it is", () => {
    const row = jobRow(job({ outcome: "clarification_required", reply: "哪只杯子？", message: "已完成" }));
    expect(row.text).toBe("哪只杯子？");
    expect(jobBadgeClass({ agentState: row.state, terminal: true, outcome: "clarification_required" }))
      .toBe("is-done");
  });

  it("quotes the failure upstream gave, in the order the gateway publishes it", () => {
    expect(jobRow(job({ outcome: "failed", message: "UPSTREAM_ERROR: 抓取超时" })).text)
      .toBe("UPSTREAM_ERROR: 抓取超时");
    expect(jobRow(job({ outcome: "failed", message: "", failure_reason: "任务不存在" })).text)
      .toBe("任务不存在");
    expect(jobRow(job({ outcome: "failed", message: "", failure_reason: "", agent_state: "aborted" })).text)
      .toBe("aborted");
  });

  it("explains a transform collision as an uncommitted edit refusal", () => {
    const text = jobRow(job({ outcome: "failed", message: "scene_edit_transform_collision" })).text;
    expect(text).toContain("编辑失败");
    expect(text).toContain("目标位置存在碰撞");
    expect(text).toContain("场景未改变");
    expect(text).toContain("scene_edit_transform_collision");
    expect(text).not.toContain("编辑成功");
  });

  it("explains a layout collision as an uncommitted add refusal", () => {
    const text = jobRow(job({ outcome: "failed", message: "scene_edit_layout_collision" })).text;
    expect(text).toContain("编辑失败");
    expect(text).toContain("新增物体的位置与现有场景冲突");
    expect(text).toContain("场景未改变");
    expect(text).toContain("scene_edit_layout_collision");
    expect(text).not.toContain("编辑成功");
  });

  it("says nothing of its own about a task that has not ended", () => {
    expect(jobRow(job({ terminal: false, outcome: null, message: "正在规划" })).text).toBe("正在规划");
  });

  /** The prohibition in the table's third column, asserted over every branch we hold the copy for. */
  it("never calls a finished task a success", () => {
    for (const outcome of ["scene_updated", "answered", "clarification_required", "plan_ready", "failed"] as const) {
      const row = jobRow(job({ outcome, reply: null, message: "" }));
      expect(row.text).not.toContain("成功");
      expect(row.identity ?? "").not.toContain("成功");
    }
  });

  it("keeps `agent_state` exactly as the service called itself", () => {
    // §7.4: there is no table to mistranslate a new word with, because there is no table.
    expect(jobRow(job({ agent_state: "正在渲染" })).state).toBe("正在渲染");
  });

  it("lists the skill order, strings and objects alike", () => {
    // 流程文档 §7.3 lets a `plan.commands` entry be an object; quoting it beats guessing a key.
    const row = jobRow(job({ outcome: "plan_ready", plan: { commands: ["pick", { skill: "place", arm: 2 }, 3, null] } }));
    expect(row.commands).toEqual(["pick", '{"skill":"place","arm":2}', "3"]);
    expect(jobRow(job({ plan: null })).commands).toEqual([]);
    expect(jobRow(job({ plan: { commands: "pick" } })).commands).toEqual([]);
  });

  it("reports a real model-call count, zero included, and stays silent without one", () => {
    expect(jobRow(job({ metrics: { model_calls: 3 } })).modelCalls).toBe("Qwen 调用 3 次");
    // §5.15: "没有调用就写 0" -- so a 0 is an answer, not an absence.
    expect(jobRow(job({ metrics: { model_calls: 0 } })).modelCalls).toBe("Qwen 调用 0 次");
    expect(jobRow(job({ metrics: null }))).toMatchObject({ modelCalls: null });
    expect(jobRow(job({ metrics: { model_calls: "三" } })).modelCalls).toBeNull();
  });

  it("links the artifacts through the gateway's proxy route, by task", () => {
    const row = jobRow(job({ outcome: "plan_ready", artifacts: ["commands", "scene_patch"] }));
    expect(row.artifacts).toEqual([
      { name: "commands", url: "/tasks/12/artifacts/commands" },
      { name: "scene_patch", url: "/tasks/12/artifacts/scene_patch" },
    ]);
  });

  it("names a new 现场 only from a receipt that says it landed", () => {
    expect(jobRow(job({ outcome: "scene_updated", commit_receipts: [] })).identity).toBeNull();
    expect(jobRow(job({ outcome: "scene_updated", commit_receipts: [receipt({ status: "unknown" })] })).identity).toBeNull();
    expect(jobRow(job({ outcome: "scene_updated", commit_receipts: [receipt({ status: "rejected" })] })).identity).toBeNull();
    // The last committed one wins: a task may open more than one transaction.
    const second = receipt({ transactionID: "tx-2", scene_version: 14, scene_sub_version: 2 });
    expect(jobRow(job({ outcome: "scene_updated", commit_receipts: [receipt(), second] })).identity)
      .toBe(`现场 #7 · v14.2 · 摘要 ${DIGEST.slice(0, 8)}`);
    // An answer for another outcome never shows a 现场 it did not write.
    expect(jobRow(job({ outcome: "answered" })).identity).toBeNull();
  });
});

/** §7.4 rules 2 and 4: only `terminal === false` spins, and the class never comes from a name. */
describe("jobWorking / jobBadgeClass (§7.4, §8)", () => {
  it("spins only a row the gateway said is still running", () => {
    expect(jobWorking({ agentState: "执行中", terminal: false })).toBe(true);
    expect(jobWorking({ agentState: "执行中", terminal: true })).toBe(false);
    // A localStorage row from before the flag reads as ended, or last week's task turns forever.
    expect(jobWorking({ agentState: "执行中" })).toBe(false);
    expect(jobWorking({ terminal: false })).toBe(false);
  });

  it("takes the class from outcome, and reads an absent outcome as not-failed", () => {
    expect(jobBadgeClass({ agentState: "执行中", terminal: false, outcome: "failed" })).toBe("is-pending");
    expect(jobBadgeClass({ agentState: "x", terminal: true, outcome: "failed" })).toBe("is-failed");
    for (const outcome of ["scene_updated", "answered", "clarification_required", "plan_ready", null] as const) {
      expect(jobBadgeClass({ agentState: "x", terminal: true, outcome })).toBe("is-done");
    }
  });
});

/**
 * §8 (R9): the service bar replaced a badge that never measured anything -- `state.agentOnline` only
 * ever said "this tab sent a task and the send did not throw". These two functions are the whole of
 * the new wording, and the second one is the reason the first can stay honest: a probe result and a
 * task result are two different readings, and neither borrows the other's number.
 */
describe("agentHealthLabel (§8)", () => {
  it("says the three words the gateway grades with, quoting the fields that failed", () => {
    expect(agentHealthLabel({ status: "ready", detail: null })).toBe("Agent 服务：就绪");
    expect(agentHealthLabel({ status: "degraded", detail: "qwen=unavailable" })).toBe("Agent 服务：降级（qwen=unavailable）");
    expect(agentHealthLabel({ status: "unreachable", detail: "Agent 服务无响应" })).toBe("Agent 服务：不可达（Agent 服务无响应）");
    expect(agentHealthLabel({ status: "ready", detail: "" })).toBe("Agent 服务：就绪");
  });

  /** A gateway that will not answer is not a gateway that says the service is down. */
  it("keeps a missing reading apart from an unreachable one", () => {
    expect(agentHealthLabel(null)).toBe("Agent 服务：状态未知");
    expect(agentHealthLabel(null)).not.toBe(agentHealthLabel({ status: "unreachable", detail: null }));
  });

  it("never lets the bar talk about a task", () => {
    for (const status of ["ready", "degraded", "unreachable"] as const) {
      expect(agentHealthLabel({ status, detail: "assets=down" })).not.toContain("任务");
    }
  });
});

describe("pollStallNotice (§8)", () => {
  it("counts this tab's unread tasks and says nothing when none were", () => {
    expect(pollStallNotice(0)).toBeNull();
    expect(pollStallNotice(1)).toBe("任务状态暂无法更新（连续轮询失败 1 次）");
    expect(pollStallNotice(4)).toBe("任务状态暂无法更新（连续轮询失败 4 次）");
  });

  /** §8's hard口径: the number comes from the task reads, never from the health probe. */
  it("stays out of the service's wording and out of the task's verdict", () => {
    const notice = pollStallNotice(3) ?? "";
    expect(notice).not.toContain("服务");
    expect(notice).not.toContain("失败原因");
    expect(notice).not.toContain("成功");
  });
});
