import { describe, expect, it } from "vitest";
import type { SceneEvent } from "../../contracts/types";
import { agentPlanItemsFromEvent, cameraFrustumSegments, decideSnapshot, featureEnabled, mergeSpeech, resolveAssetDependency, timelineDelayMs, timelineDueIndex, validateCamera, validateScale, validateSpotCone } from "./logic";

function event(revision: number): SceneEvent<Record<string, unknown>> {
  return { eventId: String(revision), sceneId: "main", revision, type: "scene.changed",
    timestamp: "2026-08-02T00:00:00Z", actor: { type: "system" }, payload: {} };
}

describe("feature switches", () => {
  it("requires explicit switches and honors disabled parents", () => {
    const features = {
      "panels.details": true,
      "panels.details.physics": true,
      "panels.details.physics.mass": true,
      "panels.details.physics.ifpsSphereCount": false,
      "viewport.tools": false,
      "viewport.tools.move": true,
    };
    expect(featureEnabled(features, "panels.details.physics.mass")).toBe(true);
    expect(featureEnabled(features, "panels.details.physics.ifpsSphereCount")).toBe(false);
    expect(featureEnabled(features, "viewport.tools.move")).toBe(false);
    expect(featureEnabled(features, "panels.details.physics.restitution")).toBe(false);
  });
});

describe("Agent plan events", () => {
  it("maps Codex todo lists to completed, active, and pending presentation states", () => {
    expect(agentPlanItemsFromEvent({ type: "item.updated", item: { type: "todo_list", items: [
      { text: "Inspect the scene", completed: true },
      { text: "Move the robot", completed: false },
      { text: "Publish the scene", completed: false },
    ] } })).toEqual([
      { step: "Inspect the scene", status: "completed" },
      { step: "Move the robot", status: "in_progress" },
      { step: "Publish the scene", status: "pending" },
    ]);
  });

  it("preserves an explicit active item", () => {
    expect(agentPlanItemsFromEvent({ steps: [
      { step: "First", status: "pending" },
      { step: "Second", status: "in_progress" },
    ] })).toEqual([
      { step: "First", status: "pending" },
      { step: "Second", status: "in_progress" },
    ]);
  });

  it("preserves a failed phase from structured progress", () => {
    expect(agentPlanItemsFromEvent({ type: "phase", phase: "execute", status: "failed", plan: [
      { title: "编译命令", status: "completed" },
      { title: "执行动作", status: "failed" },
      { title: "同步场景", status: "pending" },
    ] })).toEqual([
      { step: "编译命令", status: "completed" },
      { step: "执行动作", status: "failed" },
      { step: "同步场景", status: "pending" },
    ]);
  });
});

describe("scene event revision handling", () => {
  it("deduplicates stale events and reloads gaps", () => {
    expect(decideSnapshot(5, event(5))).toBe("ignore");
    expect(decideSnapshot(5, event(6))).toBe("apply");
    expect(decideSnapshot(5, event(8))).toBe("reload");
  });
});

describe("inspector validation", () => {
  it("rejects invalid transforms and camera planes", () => {
    expect(validateScale([1, 0, 1])).toContain("greater than zero");
    expect(validateScale([1, Number.NaN, 1])).toContain("finite");
    expect(validateScale([1, 2, 3])).toBeNull();
    expect(validateCamera(1, 0.5)).toContain("Far Plane");
    expect(validateCamera(0.1, 100)).toBeNull();
  });

  it("validates spotlight inner and outer half-angles", () => {
    expect(validateSpotCone(Math.PI / 9, Math.PI / 4)).toBeNull();
    expect(validateSpotCone(Math.PI / 3, Math.PI / 4)).toContain("cannot exceed");
    expect(validateSpotCone(0, Math.PI / 2)).toContain("90 degrees");
  });
});

describe("speech transcript merging", () => {
  it("preserves the draft prefix and committed transcript", () => {
    expect(mergeSpeech("move the ", "cube ", "left")).toBe("move the cube left");
    expect(mergeSpeech("existing ", "final", "")).toBe("existing final");
  });
});

describe("timeline scheduling", () => {
  it("uses absolute simulation time and honors playback rate", () => {
    expect(timelineDelayMs(1000, 2, 2.5, 1, 1200)).toBe(300);
    expect(timelineDelayMs(1000, 2, 2.5, 2, 1200)).toBe(50);
    expect(timelineDelayMs(1000, 2, 2.5, 4, 1200)).toBe(0);
  });

  it("skips expired display frames to keep wall-clock duration", () => {
    const times = [0, 0.002, 0.004, 0.006, 0.008];
    expect(timelineDueIndex(times, 0, 0.001)).toBe(0);
    expect(timelineDueIndex(times, 0, 0.006)).toBe(3);
    expect(timelineDueIndex(times, 3, 1)).toBe(4);
  });
});

describe("URDF dependency resolution", () => {
  it("resolves ROS package URIs from a package root or workspace root", () => {
    const uri = "package://jaka_description/meshes/jaka_zu7_meshes/Link_0.STL";
    expect(resolveAssetDependency("urdf/jaka_zu7.urdf", uri))
      .toBe("meshes/jaka_zu7_meshes/Link_0.STL");
    expect(resolveAssetDependency("src/jaka_description/urdf/jaka_zu7.urdf", uri))
      .toBe("src/jaka_description/meshes/jaka_zu7_meshes/Link_0.STL");
    expect(() => resolveAssetDependency("robot.urdf", "https://example.com/link.stl"))
      .toThrow("不支持的依赖路径");
  });
});

describe("camera frustum", () => {
  it("uses Sim Core optical axes, vertical FOV, aspect ratio, and clip distances", () => {
    const points = cameraFrustumSegments({ output: "rgbd", frameId: "camera", updateRateHz: 30,
      width: 640, height: 480, verticalFovDegrees: 60, nearPlane: 0.1, farPlane: 10 });
    expect(points).toHaveLength(72);
    expect(points.slice(0, 3)).toEqual([0, 0, 0]);
    expect(points[3]).toBe(10);
    expect(points[4]).toBeCloseTo(10 * Math.tan(Math.PI / 6) * 4 / 3);
    expect(points[5]).toBeCloseTo(10 * Math.tan(Math.PI / 6));
    expect(points).toContain(0.1);
  });
});
