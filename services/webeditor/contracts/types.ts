/**
 * Wire contract for the world-description flow (docs/Web前端需求规格v3.md §2, §3, §5).
 * The browser owns the scene; gateway and caller speak this vocabulary.
 * ids are uint32 (§2.1), angles are radians except Camera.fov (§2.6),
 * quaternions are xyzw (§2.8), lengths are metres (§2.7).
 */

export type Vec3 = [number, number, number];
export type QuatXyzw = [number, number, number, number];
export type UintId = number;
export type Resolution = [number, number];

export type KnownComponentType =
  | "Transform" | "MetadataRef" | "MeshRenderer" | "Semantic" | "Light" | "Camera" | "RobotDriver" | "RobotAttachment"
  | "LocalAxes";

/** Unknown component_type values are ignored with a console warning (§3.4), hence the open union. */
export type ComponentType = KnownComponentType | (string & {});

/** Every properties object keeps unknown fields verbatim (§3.8 rawComponents). */
export type Props<T extends object> = T & Record<string, unknown>;

export interface TransformProps {
  position?: Vec3;
  quaternion_xyzw?: QuatXyzw;
  scale?: Vec3;
  /** parent object_id_in_scene, or null for the scene root (§2.5) */
  parent?: UintId | null;
}
export interface MetadataRefProps {
  /** "$LIBRARY_SERVER/<id>" - the id alone says nothing about which id space it lives in */
  path?: string;
  /** which OLS id space `path` names, and therefore which route serves it (§6.11) */
  type?: "model" | "robot" | (string & {});
}
export interface MeshRendererProps {
  /** `#RRGGBB` or `#RRGGBBAA` */
  forceOverrideColor?: string;
}
export interface SemanticProps {
  semantic_name?: string;
  category?: string;
  /** not defined yet (§3 组件表); kept in memory, never computed with */
  support?: unknown;
}
export interface LightProps {
  type?: "Ambient" | "Directional" | "Point" | "Spot" | (string & {});
  intensity?: number;
  color?: string;
}
export interface CameraProps {
  type?: "rgb" | "rgbd";
  /** degrees, vertical (§2.6) */
  fov?: number;
  near?: number;
  far?: number;
  resolution?: Resolution;
}
export interface RobotDriverProps {
  joint_sequence?: string[];
  joint_limits?: Record<string, [number, number]>;
  /** the pose the arm is parked at before any trajectory drives it (radians) */
  initial_joint_positions_rad?: Record<string, number>;
  /** the joints it names override `initial_joint_positions_rad`, per joint; the rest stay at their initial angle (§4.7, Q75) */
  now_joint_positions_rad?: Record<string, number>;
  /** joints that may carry further models or arms */
  available_connect_joints?: string[];
  /**
   * §4.7 (Q107): decides nothing but whether this URDF gets the arm decorations - trajectory
   * waypoints (§7.3 item 1) and segment-end ghosts (item 7). `"passive"` is an ordinary
   * non-arm model (a microwave, a drawer cabinet) that a clip still drives joint by joint.
   * Anything missing, not `"passive"` or not a string reads as `"arm"`, without a warning.
   */
  role?: "arm" | "passive";
}
export interface RobotAttachmentProps {
  /** the `object_id_in_scene` this object is mounted on; must equal `Transform.parent` */
  parent_object_id?: UintId;
  parent_joint?: string;
  parent_link?: string;
  child_link?: string;
  connection_type?: string;
  /** the mount pose, which overrides `Transform.position`/`quaternion_xyzw` (§ 场景文件描述 §4.8) */
  transform?: {
    position?: Vec3;
    quaternion_xyzw?: QuatXyzw;
    reference_frame?: string;
  };
}

/**
 * §4.9 (Q130): three axes drawn from the object's local origin along its own +X/+Y/+Z.
 * Editor-only - never captured, never counted in any extent (§4.4).
 */
export interface LocalAxesProps {
  /** metres, from the origin to each tip; all three share it */
  length?: number;
  /** per-axis CSS colour, `#RGB`/`#RRGGBB`/`#RRGGBBAA`; a missing axis keeps its default */
  colors?: { x?: string; y?: string; z?: string };
  /** `X`/`Y`/`Z` sprites at the tips. Only literal `true` shows them (§4.9). */
  show_labels?: boolean;
  /** metres, offset from the object's local origin to the axes' origin; default [0,0,0] */
  offset?: Vec3;
}

export interface WorldComponent<T extends object = Record<string, unknown>> {
  component_type: ComponentType;
  properties: Props<T>;
}

export interface WorldObject {
  object_id_in_scene: UintId;
  object_name_in_scene: string;
  components: WorldComponent[];
}

export interface WorldFile {
  scene_schema_version: number;
  scene_version: number;
  /** §1.2 (Q109): optional uint32, fourth of the four-tuple. Absent means 0, and 0 is written absent. */
  scene_sub_version?: UintId;
  scene_id: UintId;
  scene_name: string;
  objects: WorldObject[];
}

/** Object description as callers send it to /addObject and /updateItem: id may be absent (§5.5). */
export type ObjectRequest = Omit<WorldObject, "object_id_in_scene"> & { object_id_in_scene?: UintId };

// ---------------------------------------------------------------------------
// Gateway <-> browser request channel over /events (§5.1 - §5.4)
// ---------------------------------------------------------------------------

export type BrowserAction =
  | "updateWorld" | "updateItem" | "addObject" | "removeObject"
  | "updateTraj" | "captureScene" | "get2DAABB" | "getWorld";

export interface BrowserHello {
  type: "browser.hello";
  browser_id: string;
  role: "viewer";
}

export interface BrowserRequest<P = Record<string, unknown>> {
  type: "browser.request";
  request_id: string;
  action: BrowserAction;
  payload: P;
  /**
   * true only for updateTraj; the N TRAJ clips follow, concatenated in part order, in the
   * next binary frame (§5.3, **Q99**). That request's payload carries `count` and
   * `byte_lengths` (what to slice by) on top of the `byte_length` dispatch announces.
   */
  expects_binary?: boolean;
}

export interface BrowserError {
  code: string;
  message: string;
}

export interface BrowserResponseSuccess<P = Record<string, unknown>> {
  type: "browser.response";
  request_id: string;
  ok: true;
  payload?: P;
}

export interface BrowserResponseFailure {
  type: "browser.response";
  request_id: string;
  ok: false;
  error: BrowserError;
}

export type BrowserResponse<P = Record<string, unknown>> =
  BrowserResponseSuccess<P> | BrowserResponseFailure;

/**
 * Sent by the browser after a local Load Scene / asset-panel load, for the gateway cache (§5.8).
 *
 * `camera_object_ids` is §9's 候选清单 (§9 item 3): the browser is the only party that knows which
 * objects carry a `Camera` component, and the gateway will not go looking inside the document.
 * Reporting ids is not choosing one -- the selection and its persistence belong to Brain through
 * `POST /sessions{camera_id}`.
 *
 * `report_seq` (Q147) numbers this page's reports, from 1, never persisted anywhere. The gateway
 * echoes it verbatim into `scene.applied.ack_report_seq` and never compares or stores it: it is a
 * return address, not a version. A report without one is a report nobody can confirm to.
 */
export interface BrowserStateSync {
  type: "browser.state_sync";
  payload: { world: WorldFile; camera_object_ids: UintId[]; report_seq: number };
}

/**
 * §5.4 (Q111): "the scene moved", addressed to every connection. It is a **notice**, not a world:
 * a reader that is not the applier re-fetches `GET /getWorld` (§5.8), and the four-tuple below is
 * what lets it skip that fetch when its own view already matches. There is no `source_browser_id`
 * any more -- under one binding it could only ever name the bound browser, which is what an
 * unparameterised `/getWorld` already means (§5.2).
 *
 * `scene_digest` is `null` when the gateway announced a write it did not re-read: a digest belongs
 * to a document (§3), and pairing a stored one with a version that did not come from it is exactly
 * what 流程文档 §5.1 forbids.
 *
 * Q147 turns this notice into an acknowledgement for the tab that caused it. `session_id` is the
 * durable session the gateway held when the scene landed (`null` before the first one), and
 * `ack_report_seq` repeats back the `report_seq` of the browser report that landed -- `null` for
 * every landing that was not a report (a caller's four write actions, `/commitWorld`, a session
 * switch). The reader's four-way verdict is 规格 §5.4: a frame from another session is dropped, an
 * ack below this page's newest report is a late echo of a switch it already left, an ack equal to
 * it confirms the scene this tab already shows, and a `null` or higher ack is somebody else's write
 * this tab has to believe. The gateway never interprets the number, so the Agent side is unaffected.
 */
export interface SceneApplied {
  type: "scene.applied";
  scene_id: number | null;
  scene_version: number | null;
  scene_sub_version: number;
  scene_digest: string | null;
  session_phase: "setup" | "agent_owned";
  session_id: string | null;
  action: BrowserAction;
  source_request_id: string | null;
  ack_report_seq: number | null;
}

export type BrowserToGateway = BrowserHello | BrowserResponse | BrowserStateSync;
export type GatewayToBrowser = BrowserRequest | SceneApplied;

/**
 * §7.3: the five results brain-web/2 fixes. This is the one enumeration the gateway keeps --
 * it classifies what a finished task means for the user, not what the service is running.
 */
export type AgentOutcome = "scene_updated" | "answered" | "clarification_required" | "plan_ready" | "failed";

/**
 * §5.15/Q86, §7.4 (Q116): one Agent task as the gateway publishes it. `agent_state` is
 * upstream's own string and the only thing the panel may show for the running state -- the
 * gateway translates no state names and ends nothing by name. `terminal` is the sole
 * terminal criterion; `outcome` and the result fields appear only once it is true. The scene
 * the browser sent is never echoed back -- a world may be 100 MB (§11.1) and this is a list.
 */
export interface AgentJob {
  id: string;
  task_id: number;
  command: string;
  agent_state: string;
  terminal: boolean;
  state_detail: string | null;
  outcome: AgentOutcome | null;
  reply: string | null;
  plan: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  artifacts: string[];
  commit_receipts: CommitReceiptView[];
  message: string;
  failure_reason: string | null;
  created_at: number;
  updated_at: number;
  /** Transcript row that owns this task; used to reject late cross-session updates. */
  conversation_id: string | null;
}

/**
 * §8.1/§7.3 rule 6: one commit receipt, published from this gateway's own ledger -- the same
 * shape `GET /queryWorldCommit` answers with, and no other source. `committed`/`rejected` carry
 * the four-tuple the receipt recorded; `unknown` is the honest absence (no file on disk, or a
 * file that does not read as a commit record) and keeps only the three naming fields.
 * Brain's `commitReceipts` says **which** transactions a task opened, never what they say.
 */
export interface CommitReceiptView {
  transactionID: string;
  status: "committed" | "rejected" | "unknown";
  reason?: string;
  browserID?: string;
  scene_id?: UintId;
  scene_version?: UintId;
  scene_sub_version?: UintId;
  sceneDigest?: string;
  rejected_objects?: RejectedObject[];
}

/** §5.15: `POST /api/agent/jobs` -- one fresh command plus the whole scene it acts on. */
export interface AgentTaskSubmission {
  command: string;
  scene: WorldFile;
  /** Active transcript row captured when the user submitted this command. */
  conversationId?: string;
  /** Session captured with the scene; stale async submissions are rejected. */
  sessionID?: string;
  /**
   * §7.1: the browser's own idempotency id, 1-128 chars of `[A-Za-z0-9._-]`. One id names one
   * submission, so a retry of the same payload owes back the task it already started and a
   * same-id-different-payload retry is refused as `request_id_reused`. Absent or malformed is
   * `409 session_context_required` -- the gateway will not read a live 现场 for an unattributable
   * command. Retrying with a *fresh* id is how a genuinely new submission is sent.
   */
  clientRequestID: string;
}

/**
 * §5.3 (Q115): one sub-item of `GET /api/health`. The words are the gateway's, not Brain's:
 * `ready` only when Brain grades every one of its own fields well, `degraded` when it answers and
 * something is wrong, `unreachable` when there is no answer at all. `detail` quotes the offending
 * fields (`qwen=unavailable`) and is `null` exactly when nothing is wrong.
 */
export interface ServiceHealthView {
  status: "ready" | "degraded" | "unreachable";
  detail: string | null;
}

/**
 * §5.3: `GET /api/health`. This route answers 200 about *itself*, so a dependency that is down or
 * slow shows up in its own sub-item and never as a 5xx -- and no probe may touch a job (§7.4):
 * a failed health read is not evidence about any task, only about the service.
 */
export interface GatewayHealthView {
  status: string;
  mode: string;
  uptimeSeconds: number;
  agent_service: ServiceHealthView;
  browser: { connected: boolean; browser_id: string | null };
  assets: { reachable: boolean };
}

/**
 * §4: the only two phases a session is ever in. `setup` is where class-② edits run; `agent_owned`
 * is where the Agent does. `POST /sessions` is the one door back, and a page refresh is not (§4.6).
 */
export type SessionPhase = "setup" | "agent_owned";

/**
 * §4/§5: `GET /api/sessionState` -- the persisted half of the session, which is what a refreshed
 * tab reads back to find out where it stands. `browser_id` names the *bound* viewer rather than
 * trusting the caller's claim, because a reader has to be able to tell "this phase is mine" from
 * "someone else holds the scene". Nine of these fields are the disk file and are never cached;
 * `camera_candidates` is the tenth and is the opposite -- a browser's report about a document, so
 * it lives in this process's memory and empties when the scene moves (§9 item 3).
 */
export interface SessionStateView {
  recovery_required?: boolean;
  /** Durable Brain session id returned by /sessionState; absent only for legacy fixtures. */
  sessionID?: string;
  /** Camera selected automatically by Brain/Gateway for this session. */
  camera_id?: UintId | null;
  camera_selection_reason?: string;
  session_phase: SessionPhase;
  lease: SceneLeaseView | null;
  quarantine: SceneQuarantineView | null;
  browser_id: string | null;
  /** §6: the id water mark, and the only seed a browser-side allocator may use. */
  next_object_id: UintId;
  scene_id: UintId | null;
  scene_version: number | null;
  scene_sub_version: UintId;
  sceneDigest: string | null;
  /**
   * §9 item 3: the objects the bound browser reported as carrying a `Camera` component. A list,
   * never a choice -- and empty until a browser has reported one, because the gateway does not
   * look inside the document to find out. A report that describes a scene the stored four-tuple no
   * longer points at is empty for the same reason: the gateway refuses a 旧值 it cannot pair.
   */
  camera_candidates: UintId[];
}

/**
 * §7.2: the standing lease as a read route may show it. `leaseToken` is deliberately absent --
 * it is the credential its holder presents in `X-Brain-Lease`, and an endpoint every tab polls
 * would hand each of them the ability to pass that gate (§5.8).
 */
export interface SceneLeaseView {
  task_id: UintId;
  browser_id: string;
  session_id: string | null;
  scene_identity: Pick<SessionStateView, "scene_id" | "scene_version" | "scene_sub_version" | "sceneDigest">;
  acquired_at: number;
}

/** §8.2: what a release that asked to stay locked leaves behind, until `resolveUncertain` says more. */
export interface SceneQuarantineView {
  browser_id: string | null;
  session_id: string | null;
  task_id: UintId | null;
  retired_lease_token: string | null;
  scene_identity: SceneLeaseView["scene_identity"] | null;
  quarantined_at: number;
}

/**
 * §4 (S4): the phase moved, and this push is the only live notice of it. The gateway sends one
 * edge per real change -- a resumed session and a replayed task announce nothing.
 */
export interface SessionPhaseEvent {
  type: "session.phase_changed";
  payload: { browser_id: string | null; session_phase: SessionPhase };
}

export interface AgentDebugEvent {
  type: "agent.debug";
  payload: { jobId: string; index: number; event: Record<string, unknown> };
}

export interface AgentUpdateEvent {
  /** §5.15: the only Agent frame the gateway publishes now; `agent.turn_updated` went with the browser-side polling loop (Q87). */
  type: "agent.job_updated";
  payload: AgentJob;
}

/** Everything the gateway may push to a browser (§5.1/§5.4 plus §5.15's agent task stream). */
export type GatewayFrame = BrowserRequest | SceneApplied | AgentDebugEvent | AgentUpdateEvent | SessionPhaseEvent;

// ---------------------------------------------------------------------------
// Request payloads (§5.9)
// ---------------------------------------------------------------------------

export interface UpdateWorldPayload {
  world: WorldFile;
  expected_version?: number;
  /**
   * true only when the gateway forwards a `commitWorld` candidate (流程文档 §8.1): any rejected
   * entry then lands nothing. `/updateWorld` omits it and keeps the §3.6 per-object tolerance.
   */
  strict?: boolean;
}
export interface UpdateItemPayload {
  id: UintId;
  object: ObjectRequest;
  expected_version?: number;
}
export interface AddObjectPayload {
  object: ObjectRequest;
  expected_version?: number;
}
export interface RemoveObjectPayload {
  id: UintId;
  detach_children?: boolean;
  expected_version?: number;
}
export interface CaptureScenePayload {
  id: UintId;
  type: "rgb" | "rgbd";
}
export interface Get2DAabbPayload {
  id: UintId;
  camera_id: UintId;
}
export interface UpdateTrajPayload {
  /** must equal the length of the paired binary frame (§5.3) */
  byte_length: number;
}

// ---------------------------------------------------------------------------
// Results (§5.9 - §5.12)
// ---------------------------------------------------------------------------

export interface RejectedObject {
  /** null when the entry's own id failed validation, so index identifies it */
  object_id_in_scene: UintId | null;
  /** position in the submitted objects[] array */
  index: number;
  reason: string;
}

export interface WorldApplyResult {
  ok: true;
  scene_id: UintId;
  scene_version: number;
  object_count: number;
  rejected_objects: RejectedObject[];
}

/**
 * §5.1: where the drivers are relative to the poses the world file declares. `playing` is
 * the animation loop, `paused` is a loaded clip that has moved them (frame 0 of a clip is a
 * sample, not a declaration), `stopped` is no clip at all -- the state a capture requires.
 */
export type PlaybackState = "playing" | "paused" | "stopped";

/**
 * §5.9/§5.1: the browser answers `getWorld` with the document plus the identity the gateway
 * turns into response headers. The digest is the gateway's job (流程文档 §3, R5): this receipt
 * carries no `scene_digest`, and `scene_sub_version` here is the header's 0-for-absent view of
 * a document that §3.1 lets omit the key.
 */
export interface GetWorldResult {
  world: WorldFile;
  scene_id: UintId;
  scene_version: number;
  scene_sub_version: UintId;
  playback_state: PlaybackState;
}

export interface AddObjectResult {
  ok: true;
  object_id_in_scene: UintId;
  scene_version: number;
}

export interface RemoveObjectResult {
  ok: true;
  removed_object_ids: UintId[];
  reparented_object_ids: UintId[];
  scene_version: number;
}

/**
 * §9 (S5): the identity a collection read has to carry beside its pixels or its box.
 *
 * Both `captureScene` and `get2DAABB` are "one observation of one 现场", so the frame and the
 * measurement answer for themselves with the same six fields, and the gateway compares each of
 * them against the reading it took before it dispatched (§9.1's 身份闸). The browser fills them
 * from **the world it rendered this frame from** -- echoing what the request carried would prove
 * nothing.
 *
 * `sceneDigest` is the one field the browser cannot compute (§3 makes the gateway the single
 * digest site), so it reports the digest that was handed back for exactly this four-tuple and
 * `null` when it holds no reading that pairs with what it is rendering -- e.g. right after a
 * class-② local edit, whose new document the tab has not seen digested yet. A `null` is a
 * refusal, not a gap: 缺项或旧值即 `camera_frame_scene_mismatch`.
 */
export interface SceneFrameIdentity {
  browserID: string;
  scene_id: UintId;
  scene_version: number;
  scene_sub_version: UintId;
  sceneDigest: string | null;
  /** §9.1: a frame is only honest with the drivers parked, so the gateway requires `stopped` */
  playback_state: PlaybackState;
}

export interface CaptureSceneResult extends SceneFrameIdentity {
  request_id: string;
  id: UintId;
  type: "rgb" | "rgbd";
  width: number;
  height: number;
  camera_type: "rgb" | "rgbd";
  image_encoding: "png";
  image_base64: string;
  /** only for type=rgbd: width*height*4 bytes, little endian float32 metres, top row first */
  depth_encoding?: "float32le_meters";
  depth_base64?: string;
  depth_unit?: "meter";
  depth_invalid?: "NaN";
  captured_at: string;
  /** current playback frame, null outside playback mode */
  frame_index: number | null;
}

export interface PixelBox {
  x_min: number;
  y_min: number;
  x_max: number;
  y_max: number;
}

export interface Get2DAabbResult extends SceneFrameIdentity {
  request_id: string;
  id: UintId;
  camera_id: UintId;
  width: number;
  height: number;
  coordinate_origin: "top_left";
  visible: boolean;
  clipped: boolean;
  box: PixelBox | null;
  aabb_world: { min: Vec3; max: Vec3 } | null;
  frame_index: number | null;
}

export interface IgnoredTrajObject {
  object_id: UintId;
  reason: string;
}

/** §5.12 (Q99): what one clip in a `/updateTraj` batch reports back. */
export interface TrajSummary {
  /** This clip's index in the request's `trajs` part order. */
  index: number;
  fps: number;
  total_frames: number;
  duration_seconds: number;
  object_count: number;
  attach_count: number;
  /** Judged per clip against the current world; never merged across the batch. */
  ignored_objects: IgnoredTrajObject[];
  warnings: string[];
}

/**
 * §5.12, reshaped by **Q99**: the request carries N clips (`multipart` field `trajs`),
 * so the flat single-clip fields are gone -- `count` plus one summary per part. Which
 * clip the browser is playing is UI state (§7.1), never wire state.
 */
export interface UpdateTrajResult {
  ok: true;
  count: number;
  trajs: TrajSummary[];
}

// ---------------------------------------------------------------------------
// Errors (§5.6) and runtime configuration (§9.2)
// ---------------------------------------------------------------------------

export interface ErrorEnvelope {
  error: BrowserError & { request_id?: string | null };
}

export interface RuntimeConfig {
  /** Hide Agent reply diagnostics only when explicitly true. */
  disable_agent_details?: boolean;
  gateway: { host: string; port: number };
  library_server_url: string;
  /** Gateway-only upstream address; browser audio always uses same-origin /speech. */
  speech_service_url?: string;
  cos_base_url: string;
  max_payload_bytes: number;
  browser_request_timeout_seconds: number;
  capture_timeout_seconds: number;
  max_pending_requests: number;
  camera_preview: { max_width: number; max_height: number; max_hz: number };
  /** §7.3/Q97: the segment-end ghosts' opacity, 0..1. Absent or out-of-range reads as 0.5. */
  ghost_pose_opacity?: number;
  /** §7.3/Q101: waypoint sphere radius in world metres. Absent or non-positive reads as 0.01. */
  traj_waypoint_radius?: number;
  /** §7.3/Q101: one waypoint every N frames. Absent, non-integer or non-positive reads as 10. */
  traj_waypoint_stride_frames?: number;
  /** §4.8.5/Q118: whether the stage projects when `viewport.shadows` hides the checkbox. Absent or non-boolean reads as true. */
  shadows_enabled?: boolean;
  /** §4.4/Q145: the colour the viewport clears to, a CSS hex string (`#RGB` or `#RRGGBB`). Absent or malformed reads as `#000000`. */
  viewport_background_color?: string;
  /** §4.8.6/Q149: draw one wireframe per URDF link instead of one per object. Absent or non-boolean reads as false. */
  urdf_bounding_box_per_link?: boolean;
  /** UI feature flags from features.json; the deleted snapshot route used to carry them. */
  features: FeatureFlags;
}

export type FeatureFlags = Record<string, boolean>;
