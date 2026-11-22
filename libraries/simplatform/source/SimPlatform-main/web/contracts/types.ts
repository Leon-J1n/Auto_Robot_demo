export type Vec3 = [number, number, number];
export type QuatWxyz = [number, number, number, number];

export interface TransformView {
  translation: Vec3;
  rotationWxyz: QuatWxyz;
  scale: Vec3;
}

export interface VisualView {
  visible: boolean;
  minLod: number;
  maxLod: number;
  shape: "box" | "sphere" | "capsule" | "cylinder" | "mesh";
  size: Vec3;
  color: [number, number, number, number];
  resource?: string;
}

export interface ColliderView {
  shape: VisualView["shape"];
  size: Vec3;
  resource?: string;
  trigger: boolean;
  friction: Vec3;
  restitution: number;
  ifpsSphereCount: number;
}

export interface RigidBodyView {
  enabled: boolean;
  dynamic: boolean;
  gravity: boolean;
  mass: number;
}

export interface CameraView {
  output: "rgb" | "rgbd";
  frameId: string;
  updateRateHz: number;
  width: number;
  height: number;
  verticalFovDegrees: number;
  nearPlane: number;
  farPlane: number;
}
export interface LightView {
  type: "point" | "spot" | "directional" | "ambient";
  color: Vec3;
  intensity: number; range: number; decay: number;
  innerConeRadians: number; outerConeRadians: number;
  angularDiameterRadians: number; enabled: boolean;
}

export interface RobotView {
  jointNames: string[];
  positions: number[];
  velocities: number[];
  efforts: number[];
}

export interface EntityView {
  id: number;
  name: string;
  parent: number | null;
  kind: string;
  flags: string[];
  local: TransformView;
  world: TransformView;
  visual?: VisualView;
  collider?: ColliderView;
  rigidBody?: RigidBodyView;
  camera?: CameraView;
  light?: LightView;
  robot?: RobotView;
  robotManaged: boolean;
}

export interface BackendSettings {
  physics: string;
  collision: string;
  renderer: string;
  browserRenderer: string;
  fixedTimeStep: number;
  realTimeFactor: number;
}

export type FeatureFlags = Record<string, boolean>;

export interface SceneSnapshot {
  sceneId: string;
  revision: number;
  status: "stopped" | "paused" | "running" | "error";
  simulationTime: number;
  entities: EntityView[];
  settings: BackendSettings;
  capabilities: Record<string, string[]>;
  performance: { renderFps: number; renderMs: number; physicsMs: number };
  agent: { connected: boolean; endpoint: string | null };
  features: FeatureFlags;
  presentation?: {
    mode: "real-sync" | "sync-and-animate";
    revision: number;
    timeline: TimelineManifest | null;
    detached?: boolean;
  };
}

export interface TimelineManifest {
  dataUrl?: string;
  timelineId: string;
  sceneId: string;
  simSessionId: string;
  sceneGeneration: number;
  presentationRevision: number;
  sourceTurnId: string;
  frameCount: number;
  durationSeconds: number;
  defaultFps: number;
  frames: Array<{
    index: number;
    simulationTime: number;
    sceneRevision: number;
    snapshotUrl: string;
  }>;
}

export interface SceneEvent<T = unknown> {
  eventId: string;
  sceneId: string;
  revision: number;
  type: string;
  timestamp: string;
  actor: { type: "user" | "agent" | "system"; id?: string };
  payload: T;
}

export interface AssetSummary {
  id: string;
  name: string;
  type: string;
  source: "builtin" | "project" | "uploaded" | "remote";
  importStatus: "ready" | "processing" | "failed";
  size?: number;
}

export interface ApiErrorBody {
  requestId: string;
  error: { code: string; message: string; details?: unknown; retryable: boolean };
}

export interface CommandRequest {
  requestId: string;
  sceneId: string;
  baseRevision: number;
  method: string;
  params?: Record<string, unknown>;
}
