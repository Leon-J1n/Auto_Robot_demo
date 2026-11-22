import type { CameraView, FeatureFlags, SceneEvent, SceneSnapshot, Vec3 } from "../../contracts/types";

export type SnapshotDecision = "ignore" | "apply" | "reload";
export type AgentPlanItem = { step: string; status: "completed" | "in_progress" | "pending" | "failed" };

export function agentPlanItemsFromEvent(event: Record<string, unknown>): AgentPlanItem[] {
  const eventItem = event.item as Record<string, unknown> | undefined;
  const payload = event.payload as Record<string, unknown> | undefined;
  const candidates: unknown[] = [event.plan, event.steps, eventItem?.plan, eventItem?.steps, eventItem?.items,
    payload?.plan, payload?.steps, payload?.items];
  for (const candidate of candidates) {
    const values: unknown[] | null = Array.isArray(candidate) ? candidate :
      candidate && typeof candidate === "object" && Array.isArray((candidate as Record<string, unknown>).steps) ?
        (candidate as Record<string, unknown>).steps as unknown[] : null;
    if (!values?.length) continue;
    const items = values.map((value): AgentPlanItem | null => {
      if (typeof value === "string") return { step: value, status: "pending" };
      if (!value || typeof value !== "object") return null;
      const item = value as Record<string, unknown>;
      const step = item.step ?? item.text ?? item.description ?? item.title ?? item.name;
      if (typeof step !== "string" || !step.trim()) return null;
      const raw = String(item.status ?? "pending").toLowerCase().replace(/[ -]/g, "_");
      const status = item.completed === true || ["completed", "complete", "done", "succeeded", "success"].includes(raw) ?
        "completed" : ["failed", "failure", "error"].includes(raw) ? "failed" :
          ["in_progress", "running", "active"].includes(raw) ? "in_progress" : "pending";
      return { step, status };
    }).filter((item): item is AgentPlanItem => item !== null);
    if (!items.length) continue;
    if (!items.some((item) => item.status === "in_progress" || item.status === "failed")) {
      const next = items.find((item) => item.status === "pending");
      if (next) next.status = "in_progress";
    }
    return items;
  }
  return [];
}

export function featureEnabled(features: FeatureFlags, path: string): boolean {
  const parts = path.split(".");
  for (let count = 1; count <= parts.length; count += 1) {
    const value = features[parts.slice(0, count).join(".")];
    if (value === false) return false;
  }
  return features[path] === true;
}

export function decideSnapshot(currentRevision: number, event: SceneEvent<Record<string, unknown>>): SnapshotDecision {
  if (event.revision <= currentRevision) return "ignore";
  if (event.revision > currentRevision + 1) return "reload";
  return "apply";
}

export function validateScale(scale: Vec3): string | null {
  if (scale.some((value) => !Number.isFinite(value))) return "Scale values must be finite";
  if (scale.some((value) => value <= 0)) return "Scale values must be greater than zero";
  return null;
}

export function validateCamera(nearPlane: number, farPlane: number): string | null {
  if (!Number.isFinite(nearPlane) || !Number.isFinite(farPlane)) return "Camera planes must be finite";
  if (nearPlane <= 0 || farPlane <= nearPlane) return "Far Plane must be greater than Near Plane";
  return null;
}

export function validateSpotCone(innerRadians: number, outerRadians: number): string | null {
  if (!Number.isFinite(innerRadians) || !Number.isFinite(outerRadians)) {
    return "Spotlight cone angles must be finite";
  }
  if (innerRadians < 0 || outerRadians <= 0 || outerRadians >= Math.PI / 2) {
    return "Spotlight cone angles must satisfy 0 <= inner <= outer < 90 degrees";
  }
  if (innerRadians > outerRadians) return "Inner Cone Angle cannot exceed Outer Cone Angle";
  return null;
}

export function mergeSpeech(prefix: string, finalText: string, interimText: string): string {
  return `${prefix}${finalText}${interimText}`;
}

export function timelineDelayMs(anchorWallMs: number, anchorSimulationTime: number,
                                targetSimulationTime: number, rate: number,
                                nowMs: number): number {
  const safeRate = Number.isFinite(rate) && rate > 0 ? rate : 1;
  const targetWallMs = anchorWallMs +
    Math.max(0, targetSimulationTime - anchorSimulationTime) * 1000 / safeRate;
  return Math.max(0, targetWallMs - nowMs);
}

export function timelineDueIndex(times: number[], currentIndex: number,
                                 targetSimulationTime: number): number {
  let index = Math.max(0, Math.min(currentIndex, times.length - 1));
  while (index + 1 < times.length && times[index + 1]! <= targetSimulationTime) index += 1;
  return index;
}

export function eventSnapshot(event: SceneEvent<Record<string, unknown>>): SceneSnapshot | null {
  const candidate = event.payload.snapshot;
  return candidate && typeof candidate === "object" ? candidate as SceneSnapshot : null;
}

export function resolveAssetDependency(entry: string, value: string): string {
  const raw = value.replace(/\\/g, "/").trim();
  const packageMatch = /^package:\/\/([^/]+)\/(.+)$/i.exec(raw);
  if (packageMatch) {
    const packageName = packageMatch[1]!;
    const entryParts = entry.split("/").filter(Boolean);
    const packageIndex = entryParts.map((part) => part.toLowerCase())
      .lastIndexOf(packageName.toLowerCase());
    const parts = packageIndex >= 0 ? entryParts.slice(0, packageIndex + 1) : [];
    const packageRootLength = parts.length;
    for (const part of packageMatch[2]!.split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") {
        if (parts.length <= packageRootLength) throw new Error(`依赖路径超出 ROS 包目录：${value}`);
        parts.pop();
      } else parts.push(part);
    }
    if (parts.length === packageRootLength) throw new Error(`无效的 ROS 包依赖路径：${value}`);
    return parts.join("/");
  }
  if (!raw || raw.startsWith("/") || raw.includes("://") || /^[A-Za-z]:/.test(raw)) {
    throw new Error(`不支持的依赖路径：${value}`);
  }
  const parts = entry.split("/").slice(0, -1);
  for (const part of raw.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) throw new Error(`依赖路径超出所选目录：${value}`);
      parts.pop();
    } else parts.push(part);
  }
  return parts.join("/");
}

export function cameraFrustumSegments(camera: CameraView): number[] {
  const tangent = Math.tan(camera.verticalFovDegrees * Math.PI / 360);
  const aspect = camera.width / camera.height;
  const corners = (depth: number): Vec3[] => {
    const halfHeight = tangent * depth;
    const halfWidth = halfHeight * aspect;
    // Sim Core optical coordinates: +X forward, +Y left, +Z up.
    return [
      [depth, halfWidth, halfHeight], [depth, -halfWidth, halfHeight],
      [depth, -halfWidth, -halfHeight], [depth, halfWidth, -halfHeight],
    ];
  };
  const near = corners(camera.nearPlane);
  const far = corners(camera.farPlane);
  const points: number[] = [];
  const segment = (first: Vec3, second: Vec3) => points.push(...first, ...second);
  for (let index = 0; index < 4; index += 1) {
    segment([0, 0, 0], far[index]!);
    segment(near[index]!, near[(index + 1) % 4]!);
    segment(far[index]!, far[(index + 1) % 4]!);
  }
  return points;
}
