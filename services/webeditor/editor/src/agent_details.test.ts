import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { agentDetailsDisabled } from "./logic";
import { jobBadgeClass, jobWorking } from "./result_view";

// main.ts owns the DOM; exercise its actual markup without starting the editor.
const main = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
const source = main.slice(main.indexOf("function agentMessageMarkup("), main.indexOf("/**", main.indexOf("function agentMessageMarkup(")));
const render = new Function("state", "agentDetailsDisabled", "jobWorking", "jobBadgeClass", "escapeHtml", "message",
  source.replace("message: AgentMessage): string", "message)").replace("(line): line is string", "(line)")
  + "\nreturn agentMessageMarkup(message);");
const message = {
  role: "agent", text: "场景已更新", time: 0, jobId: "task-123456", agentState: "completed", terminal: true,
  row: { identity: "现场 #1 · v2 · 摘要 abc", modelCalls: "Qwen 调用 2 次", commands: ["move"],
    artifacts: [{ name: "scene_patch", url: "/patch" }, { name: "scene_config", url: "/config" }] },
};
function markup(flag: unknown, value = message, streaming = false): string {
  return render({ runtime: { disable_agent_details: flag }, agentPlanKey: value.jobId,
    agentPlan: streaming ? [{ step: "internal step", status: "in_progress" }] : [] },
    agentDetailsDisabled, jobWorking, jobBadgeClass, (text: string) => text, value) as string;
}

describe("Agent reply detail visibility", () => {
  it("hides diagnostic sections while keeping the reply", () => {
    const html = markup(true);
    expect(html).toContain("场景已更新");
    for (const detail of ["task-", "Qwen", "摘要", "v2", "scene_patch", "scene_config", "message-plan", "<footer>"]) {
      expect(html).not.toContain(detail);
    }
  });
  it.each([false, undefined, null, "true", 1])("preserves details for %s", (flag) => {
    expect(agentDetailsDisabled({ disable_agent_details: flag as boolean })).toBe(false);
    expect(markup(flag)).toContain("scene_patch");
    expect(markup(flag)).toContain("Qwen 调用 2 次");
    expect(markup(flag)).toContain("task-123");
  });
  it("keeps the running reply when its streamed steps are hidden", () => {
    const html = markup(true, { ...message, terminal: false, text: "正在处理" }, true);
    expect(html).toContain("正在处理");
    expect(html).not.toContain("internal step");
  });
  it("removes the generated diagnostic task suffix from failures and restored messages", () => {
    const value = { ...message, text: "任务未通过语义校验，请重新说明对象与参照物。（invalid；诊断 task-12）" };
    expect(markup(true, value)).not.toContain("task-12");
    expect(markup(true, value)).toContain("请重新说明对象与参照物");
    expect(markup(false, value)).toContain("task-12");
  });
  it("does not filter user messages", () => {
    expect(markup(true, { ...message, role: "user", text: "查看 scene_patch" })).toContain("查看 scene_patch");
  });
});

/**
 * §7.5 (Q157): the Agent column has two writers - the feature pass that runs once at start() and
 * renderContext(), which runs on every panel press. Wiring only the first is the failure this pins:
 * the layout is right on load and wrong after the first click, with nothing thrown.
 */
describe("the Agent column has one writer, called from both render paths (§7.5, Q157)", () => {
  const writer = main.slice(main.indexOf("function applyAgentVisibility("), main.indexOf("function applyFeatureVisibility("));
  const featurePass = main.slice(main.indexOf("function applyFeatureVisibility("), main.indexOf("function renderContext("));
  const contextPass = main.slice(main.indexOf("function renderContext("), main.indexOf("function updateStatus("));

  it("asks the shared judgement and writes both the host and the grid track", () => {
    expect(writer).toContain("agentPanelVisible({");
    expect(writer).toContain('agentEnabled: enabled("agent.conversation")');
    expect(writer).toContain('contextEnabled: enabled("panels.context")');
    expect(writer).toContain('document.querySelector<HTMLElement>(".agent-panel")!.hidden = !visible;');
    expect(writer).toContain('workspace.style.removeProperty("--agent-track")');
    expect(writer).toContain('workspace.style.setProperty("--agent-track", "0px")');
  });

  it("is called by the startup feature pass and by every context render", () => {
    expect(featurePass).toContain("applyAgentVisibility();");
    expect(contextPass).toContain("applyAgentVisibility();");
  });

  it("leaves no second writer of the Agent host or its track behind", () => {
    const outside = main.replace(writer, "");
    expect(outside).not.toContain('.agent-panel")!.hidden');
    expect(outside).not.toContain('"--agent-track"');
  });
});
