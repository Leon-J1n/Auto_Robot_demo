import "./styles.css";
import { SpeechInput } from "./speech";
import {
  Activity, Bot, Box, Boxes, Camera, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, ChevronUp,
  Circle, CircleDot, Crosshair, Download, FileUp, Focus, Info, Lightbulb, ListTree, LoaderCircle, Mic,
  MousePointer2, Pause, Play, Plus, RefreshCw, Repeat, Search, Send, SlidersHorizontal,
  Square, Trash2, X, History,
  createIcons,
} from "lucide";
import type { AgentJob, AgentOutcome, AgentTaskSubmission, BrowserHello, BrowserRequest, BrowserResponse, CaptureSceneResult, FeatureFlags, GatewayFrame, GatewayHealthView, Get2DAabbResult, PlaybackState, RuntimeConfig, SceneApplied, ServiceHealthView, SessionPhase, SessionStateView, UpdateTrajResult, Vec3 } from "../../contracts/types";
import {
  agentDetailsDisabled, agentPanelVisible, agentPlanItemsFromEvent, boundingBoxesEnabled, cameraRig, cameraWallEnabled, cameraWallModeFor, cameraWallPace,
  depthPalette, enterSendsAgentMessage, featureEnabled,
  ghostOpacity, leafArmIds,
  localAxesEnabled, panelMarkupChanged, panelTabActive, panelTabPress, pickRecordMime, recordVideoName, shadowsEnabled, urdfLinkBoxesEnabled, viewportBackground, waypointColor, waypointFrames, waypointSettings,
  type AgentPlanItem, type SensorViewMode,
} from "./logic";
import { applySceneRequest, applyTrajRequest, isWriteAction, type SceneRequestResult } from "./scene_requests";
import { GROUP_LABELS, LIBRARY_KINDS, categoryLabel, groupAssets, kindLabel, type AssetGroup } from "./assets";
import { applyFrameJointPositions, declaredJointPositions, TrajDriver } from "./drivers";
import { applyAabbRequest, applyCaptureRequest, pngBase64, type CaptureView } from "./capture";
import { NATIVE_SENSOR, ThreeRendererAdapter, type WaypointPath } from "./renderer";
import { LIGHTING_PRESET_BY_ID, LIGHTING_PRESETS } from "./lighting";
import { CLASS_TWO_ENTRIES, CONVERSATION_SCENE_SWITCH_MESSAGE, generateBrowserID, phaseBlockMessage, phaseOfferMessage, persistentBrowserID, sceneExportName, sceneRevisionLabel, withBrowserIDHeader,
  submissionAfterSend, submissionForSend, submissionContextMatches, conversationListActiveId, reusableConversation,
  type AgentSubmissionContext,
  type PendingPhaseEdit, type PendingSubmission } from "./local_edits";
import { lightingFreeze, mergeSessionPhase, newSessionView, type NewSessionView, type PhaseSource } from "./session_state";
import { agentHealthLabel, jobBadgeClass, jobRow, jobWorking, pollStallNotice, type JobRow } from "./result_view";
import { appliedNoticeVerdict, sessionChanged } from "./session_identity";
import { createLibraryClient, type LibraryClient, type LibraryItem, type LibraryKind } from "./library";
import { World, WorldError, parseWorld, type WorldObjectNode } from "./world";
import { BASELINE_SCENE_ID, findCameraObjects, placeAsset, type BoundsMeasure } from "./placement";
import { createSerialQueue } from "./serial_queue";
import { poseAtFrame, stepAtFrame, type Traj, type TrajError, type TrajObject } from "./traj";
import { TrajFrameProvider } from "./timeline";

type Panel = "scene" | "details" | "assets";
type AgentMessage = { role: "user" | "agent"; text: string; agentState?: string; terminal?: boolean;
  outcome?: AgentOutcome; jobId?: string; planKey?: string; time: number; row?: JobRow };
type AgentDebugEntry = { jobId: string; index: number; event: Record<string, unknown> };
type ConversationSummary = {
  conversationId: string; title: string; updatedAt: string;
};
type ConversationDetail = {
  conversationId: string;
  sessionID?: string | null;
  sceneBinding?: { sceneId?: string | number; simSessionId?: string | null };
  sceneSnapshot?: Record<string, unknown> | null;
  messages: Array<{ role: "user" | "assistant"; content: string; turnId: string; createdAt: string }>;
  debugEvents?: Array<{ eventId?: number; turnId?: string; payload?: Record<string, unknown> }>;
};
/** One OLS id space as the panel shows it (§4.6). */
type LibrarySection = { items: LibraryItem[]; categories: string[] };
/**
 * §7.3 (Q100): a clip the browser holds, plus what the toolbar selector calls it. The
 * name is a filename from Load Traj, or null for a clip a caller pushed -- filenames are
 * the user's business and never go on the wire (§5.12).
 */
type TrajEntry = { traj: Traj; name: string | null };
/** §9.3: which `navigation.assets.*` flag gates which group. */
const GROUP_FEATURES: Record<LibraryKind, string> = { scene: "presets", model: "prefabs", robot: "robots" };
/** §4.7 (Q156): the rail carries the three 大类, so each needs its own icon. */
const GROUP_ICONS: Record<LibraryKind, string> = { scene: "boxes", model: "box", robot: "bot" };

const state = {
  // The viewport draws this world; Load Scene (M1-5) and /getWorld (M2-3) feed it.
  world: new World({ scene_schema_version: 1, scene_version: 0, scene_sub_version: 0, scene_id: 0, scene_name: "" }),
  // §4 (Q114): which half of the three-layer defence this browser is in. S4-b wired the three
  // readings: `/api/sessionState` on load and reconnect, the two `/events` pushes live, and the
  // `X-Session-Phase` header of a `/getWorld` as a cross-check that never overwrites. It starts
  // as `null` -- nothing read yet -- and `phaseBlockMessage` lets a browser that has heard nothing
  // keep editing, because the Gateway's CAS is the layer that refuses a late edit (§4 layer 2).
  sessionPhase: null as SessionPhase | null,
  // §4/§8: the whole readback. **Q123** took the panel that displayed it away; what is left of it
  // feeds `lightingFreeze` (§2.3), `newSessionView` (§9 item 3) and the paired-scene digest.
  session: null as SessionStateView | null,
  // §4 (S6-b): the class-② tap the phase gate held back, waiting on the modal's answer.
  pendingPhaseEdit: null as PendingPhaseEdit | null,
  // §5.12 (Q99): every clip this browser holds, in arrival order; playback reads one of them.
  trajs: [] as TrajEntry[],
  trajIndex: 0,
  // §7.3: the active clip, so the frame readout, Details and the ghosts keep asking for
  // "the trajectory" and get whichever one the selector points at.
  get traj(): Traj | null { return this.trajs[this.trajIndex]?.traj ?? null; },
  // §7.3 (Q101): the toolbar checkbox. Unchecked draws only the active clip's waypoints;
  // it is view state -- never on the wire, never a world edit.
  waypoints: { showAll: false },
  // §4.8.5 (Q118): the other toolbar checkbox, same class of view state. Default on, and it is
  // what the renderer gets *while the checkbox is shown*; hide it with `viewport.shadows` and
  // `shadows_enabled` takes over. A held lease shows it as on without overwriting the choice.
  shadows: true,
  // §4.8.6 (Q119): the wireframe layer's own view state. Default off -- a box around everything is a
  // diagnostic, not the look of the stage -- and there is no lease freeze behind it, because §4.4
  // already lifts the helpers out of every capture, so the collected bytes never see this flag.
  boxes: false,
  // §4.8.8 (Q132): the local-axes layer's own view state, and the same default for the same reason -
  // axes on every object is a reading aid, not the look of the stage. The file still says *which*
  // objects have a frame to show (§4.9); this box says whether this page wants to see them.
  localAxes: false,
  // §7.1 (Q138): the camera wall's own view state, and the same default for the same reason - a wall
  // of live sensor frames is a thing you ask for, not the look of the stage. View state only: it goes
  // nowhere on the wire, and the wall never enters a capture (§4.4's exclusion list is untouched).
  cameraWall: false,
  // §4.4 (Q139): which view the wall was *asked* for - one switch for the whole wall, not per cell, since
  // the two buttons sit in its header. The key decides whether that ask is answerable (`cameraWallModeFor`),
  // so this is a preference the depth host key can veto without erasing. View state, like the box above it.
  cameraWallMode: "rgb" as SensorViewMode,
  // §4.8.7 (Q121): what a take would be made of. Off by default, because a recording is asked to show
  // the 现场 as the free camera sees it rather than the diagnostic layer someone happened to leave on.
  // View state only - nothing here goes on the wire, and a take that wants the boxes in it is one click away.
  recordOverlays: false,
  // §4.6: the asset panel is a view of OLS, so it holds what the last read returned.
  client: null as LibraryClient | null,
  library: {
    sections: {
      scene: { items: [], categories: [] },
      model: { items: [], categories: [] },
      robot: { items: [], categories: [] },
    } as Record<LibraryKind, LibrarySection>,
    // §4.6 (Q93): the two rails of the hierarchy; `category` null means "whatever the first group is".
    kind: "scene" as LibraryKind,
    category: null as string | null,
    error: null as string | null,
    loading: false,
  },
  selectedId: null as number | null,
  panel: "scene" as Panel,
  panelOpen: false,
  websocket: "connecting" as "connecting" | "connected" | "reconnecting" | "offline",
  // §8 (R9): the service bar reads only `GET /api/health`'s `agent_service` sub-item. A null is a
  // gateway that has not answered yet -- a different reading from a gateway that says 不可达.
  agentService: null as ServiceHealthView | null,
  // §8: how many consecutive task reads this tab could not complete. The gateway's
  // AGENT_POLL_ERROR_LIMIT is what ends a job and its counter is never published (§5.15), so this
  // counts this browser's own reads and reaches no verdict about any task.
  jobPollErrors: 0,
  messages: [] as AgentMessage[],
  agentDebug: [] as AgentDebugEntry[],
  agentPlan: [] as AgentPlanItem[],
  agentPlanKey: null as string | null,
  // §7.1 (S4): the command this browser has sent and not yet seen the end of, with the id it went
  // out under. Null between an acceptance (or a spent id) and the next send.
  agentPending: null as PendingSubmission | null,
  conversationHistory: [] as ConversationSummary[],
  activeConversationId: null as string | null,
  historyOpen: false,
  previewMode: "rgb" as "rgb" | "depth",
  renderFps: 0,
  renderMs: 0,
  features: {} as FeatureFlags,
  // §9.1: the runtime config carries the camera preview's size and rate caps.
  runtime: null as RuntimeConfig | null,
  // §7.3: the playhead is a frame number; everything else is derived from the clip.
  playIndex: 0,
  playing: false,
  rate: 1,
  loop: false,
};
let frames: TrajFrameProvider | null = null;
/**
 * §7.3 (Q101): the swept end-effector paths of this batch, keyed by trajectory index. A sweep
 * reads every sampled frame of every clip, so it runs when a batch arrives or the world moves,
 * not when the playhead moves.
 */
let waypointCache = new Map<number, Array<{ objectId: number; points: Vec3[] }>>();
/** the rAF handle of the playback loop (§8.7) */
let playbackHandle = 0;
/** wall-clock and simulation time captured when playback (re)starts or re-anchors */
let anchorWallMs = 0;
let anchorSimulationTime = 0;
let agentToastTimer = 0;
// The conversation transcript belongs to a Brain session.  Keep the binding locally so a
// scene/session rebind cannot leave the previous transcript on screen while the new empty
// conversation is being created by the Gateway.
let sessionConversationSessionID: string | null = null;
let sessionConversationInFlight: Promise<void> | null = null;
let conversationSelectionGeneration = 0;
let conversationTransitionInFlight: Promise<void> | null = null;
const persistedConversationMessages = new Set<string>();
let sessionStateHydrated = false;
let sessionCreationInFlight = false;
let sessionRefreshVersion = 0;
let sessionRefreshInFlight: Promise<void> | null = null;
// On a fresh load the Gateway can answer before Brain has observed browser.hello.  That
// transient `browser=not_bound` reading is not a service degradation; retry until the
// browser binding settles so the UI never flashes a misleading degraded badge.
let agentHealthWarmupUntil = performance.now() + 10000;
let agentHealthRetryTimer: number | null = null;
let socket: WebSocket | null = null;
let reconnectTimer: number | null = null;
// The very first `onopen` is not a catch-up: `start()` already read the authoritative world before
// connecting, so pulling again there only races a scene the user is mid-loading and can clobber it
// back to the empty gateway scene (2026-11-05 闪现). Every later `onopen` is a real reconnect and
// does need the pull.
let firstSocketOpenDone = false;
// §5.1/§4 (S4): /events identifies each viewer by a uuid sent in browser.hello, and that uuid has
// to survive a refresh -- the binding, the resumed session and `browser_new_session_required` are
// all keyed on it, so a new one per load makes the refreshed tab look like a different browser.
const BROWSER_ID = persistentBrowserID(localStorage, () => generateBrowserID(crypto));

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
<div class="workbench">
  <main class="workspace">
    <nav class="rail" data-feature="navigation"><button data-panel="scene" data-feature="navigation.scene" data-requires-feature="panels.scene" aria-controls="context-panel" aria-expanded="false"><i data-lucide="list-tree"></i><span>Scene</span></button><button data-panel="details" data-feature="navigation.details" data-requires-feature="panels.details" aria-controls="context-panel" aria-expanded="false"><i data-lucide="sliders-horizontal"></i><span>Details</span></button>${LIBRARY_KINDS.map((kind) => `<button data-panel="assets" data-asset-kind-nav="${kind}" data-feature="navigation.assets.${GROUP_FEATURES[kind]}" data-requires-feature="panels.assets" aria-controls="context-panel" aria-expanded="false"><i data-lucide="${GROUP_ICONS[kind]}"></i><span>${GROUP_LABELS[kind]}</span></button>`).join("")}</nav>
    <aside id="context-panel" class="context-panel collapsed" data-feature="panels.context" aria-hidden="true"><header class="panel-heading" data-feature="panels.heading"><div><small id="panel-eyebrow">SCENE</small><h2 id="panel-title">Hierarchy</h2></div><span id="panel-count" data-feature="panels.heading.count">0</span></header><div id="context-content" class="context-content"></div></aside>
    <section class="viewport-shell">
      <div class="viewport-toolbar" data-feature="viewport.toolbar"><button class="button" data-action="load-scene" data-feature="viewport.loadScene" title="Load Scene (local json)"><i data-lucide="file-up"></i><b>加载场景</b></button><button class="button" data-action="load-traj" data-feature="viewport.loadTraj" title="Load Traj (local .traj)"><i data-lucide="file-up"></i><b>加载轨迹</b></button><select id="traj-select" data-feature="playback.trajSelector" aria-label="Trajectory selector" title="Trajectory to play" hidden></select><label class="toolbar-check" data-feature="playback.trajWaypoints" title="Show the waypoints of every loaded trajectory"><input type="checkbox" id="waypoint-all-trajs"><b>显示轨迹</b></label><button data-action="focus" data-feature="viewport.focusSelection" title="Focus Selection"><i data-lucide="focus"></i></button><button data-action="frame" data-feature="viewport.frameScene" title="Frame Scene"><i data-lucide="crosshair"></i></button><button data-action="snapshot" data-feature="viewport.snapshot" title="Copy viewport screenshot to clipboard"><i data-lucide="camera"></i></button><button data-action="record" data-feature="viewport.record" title="Record the viewport to an MP4 file"><i data-lucide="circle-dot"></i></button><label class="toolbar-check" data-feature="viewport.record" title="Include the auxiliary layers in the recording"><input type="checkbox" id="record-overlays"><b>录制辅助图层</b></label><span id="selection-label" data-feature="viewport.selectionLabel">World</span><select id="lighting-preset" data-feature="viewport.lightingPreset" aria-label="Lighting preset" title="Lighting preset"><option value="">Scene lights</option>${LIGHTING_PRESETS.map((preset) => `<option value="${preset.id}">${preset.label}</option>`).join("")}</select><label class="toolbar-check" data-feature="viewport.shadows" title="Projected shadows"><input type="checkbox" id="shadows-toggle" checked><b>投射阴影</b></label><label class="toolbar-check" data-feature="viewport.boundingBoxes" title="Draw one bounding box per object"><input type="checkbox" id="bbox-toggle"><b>显示包围盒</b></label><label class="toolbar-check" data-feature="viewport.localAxesToggle" title="Draw the local frame each object declares"><input type="checkbox" id="local-axes-toggle"><b>显示坐标轴</b></label><label class="toolbar-check" data-feature="viewport.cameraWall" title="Show what every camera in the scene sees"><input type="checkbox" id="camera-wall-toggle"><b>相机画面</b></label><button data-action="export-scene" data-feature="viewport.exportScene" title="Export scene JSON"><i data-lucide="download"></i></button><div class="segmented" data-feature="viewport.tools"><button data-tool="select" class="active" title="Select only"><i data-lucide="mouse-pointer-2"></i></button></div></div>
      <div class="canvas-wrap"><canvas id="viewport" data-feature="viewport.canvas"></canvas>
        <div id="toast" class="toast hidden" data-feature="viewport.notifications"></div>
        <aside id="camera-wall" class="camera-wall hidden" aria-label="Camera views"><header class="camera-wall-head"><strong>相机画面</strong><span id="camera-wall-count">0</span><span class="camera-wall-modes"><button type="button" data-camera-wall-mode="rgb" title="RGB image">RGB</button><button type="button" data-camera-wall-mode="depth" data-feature="viewport.cameraPreview.modes.depth" title="Pseudo-coloured depth">深度</button></span><button type="button" data-action="close-camera-wall" title="Close" aria-label="关闭相机画面"><i data-lucide="x"></i></button></header><div id="camera-wall-cells" class="camera-wall-cells"></div></aside>
        <div id="phase-block" class="phase-block hidden" role="alert"><div class="phase-block-copy"><p></p><p class="phase-offer"></p></div><div class="phase-block-actions"><button type="button" class="phase-accept" data-action="phase-new-session">新建会话</button><button type="button" class="phase-dismiss" data-action="dismiss-phase-block" aria-label="取消">取消</button></div></div>
      </div>
      <div id="timeline" class="timeline hidden" data-feature="playback.bar"><button data-timeline="play" data-feature="playback.play" title="Play"><i data-lucide="play"></i></button><button data-timeline="prev" data-feature="playback.play" title="Previous frame"><i data-lucide="chevron-left"></i></button><button data-timeline="next" data-feature="playback.play" title="Next frame"><i data-lucide="chevron-right"></i></button><div class="timeline-track" data-feature="playback.seek"><input id="timeline-range" type="range" min="0" max="0" step="1" value="0" aria-label="Timeline position"><div class="timeline-ticks" id="timeline-ticks" aria-hidden="true"></div></div><span id="timeline-time" data-feature="playback.seek">0.00 / 0.00 s</span><span id="timeline-frame" data-feature="playback.seek">0 / 0</span><select id="timeline-rate" data-feature="playback.rate" aria-label="Playback speed"><option value="0.25">0.25x</option><option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option><option value="4">4x</option></select><button data-timeline="loop" data-feature="playback.loop" title="Loop playback"><i data-lucide="repeat"></i></button></div>
      <div class="diagnostics" id="diagnostics" data-feature="diagnostics"><button data-action="toggle-diagnostics" data-feature="diagnostics.toggle"><span>Log & Diagnostics</span><i data-lucide="chevron-up"></i></button><div id="diagnostics-content" data-feature="diagnostics.entries"></div></div>
    </section>
    <aside class="agent-panel" data-feature="agent"><header class="agent-header" data-feature="agent.header"><h2>Agent</h2><div class="agent-header-actions"><button type="button" data-action="new-conversation" data-feature="agent.history" title="New conversation" aria-label="New conversation"><i data-lucide="plus"></i></button><button type="button" data-action="toggle-history" data-feature="agent.history" title="Conversation history" aria-label="Conversation history"><i data-lucide="history"></i></button><span id="agent-state" class="unknown" data-feature="agent.status">Agent 服务：状态未知</span></div></header><div id="agent-toast" class="agent-toast hidden" role="status"></div><section id="agent-history" class="agent-history hidden" data-feature="agent.history.list"><header><strong>Conversation History</strong><button type="button" data-action="toggle-history" title="Close history" aria-label="Close history"><i data-lucide="x"></i></button></header><div id="agent-history-list"></div></section><div id="agent-stall" class="agent-stall hidden" role="status"></div><div id="agent-messages" class="agent-messages" data-feature="agent.messages"></div><section class="agent-debug" data-feature="agent.debug"><header><strong>Agent Debug</strong><span id="agent-debug-count">0</span></header><div id="agent-debug-events"></div></section><form id="agent-form" data-feature="agent.input"><textarea id="agent-input" data-feature="agent.input.text" rows="3" placeholder="Describe a scene change..."></textarea><div><p><button type="button" data-action="speech" data-feature="agent.input.voiceInput" title="Voice input"><i data-lucide="mic"></i></button><button type="submit" class="send" data-feature="agent.input.send" title="Send"><i data-lucide="send"></i></button></p></div></form></aside>
  </main>
  <footer class="status-bar" data-feature="statusBar"><span id="status-primary" data-feature="statusBar.primary">Connecting to Web Gateway...</span><span id="status-performance" data-feature="statusBar.performance">-- FPS</span><span id="status-revision" data-feature="statusBar.revision">Rev --</span></footer>
  <input id="scene-file" type="file" accept="application/json,.json" hidden>
  <input id="traj-file" type="file" accept=".traj,application/octet-stream" multiple hidden>
</div>`;

const canvas = document.querySelector<HTMLCanvasElement>("#viewport")!;
const renderer = new ThreeRendererAdapter(canvas, {
  onSelect: (id) => selectEntity(id ?? null),
  onPerformance: (fps, renderMs) => { state.renderFps = fps; state.renderMs = renderMs; updateStatus(); },
  onError: showError,
  onRobotTree: () => scheduleRobotLanding(),
});
new ResizeObserver(() => renderer.resize()).observe(canvas.parentElement!);

// §8.3/§8.4: the trajectory writes through the renderer's view, never into the world.
const driver = new TrajDriver(renderer.trajView, () => state.world, showError);

// §4.4/§5.10: a capture draws into its own canvas, never into the viewport.
const captureCanvas = document.createElement("canvas");
// §4.4 (Q108): the screenshot's own canvas, so a viewport capture never resizes the one
// `/captureScene` writes into (their sizes differ: sensor resolution vs the drawing buffer).
const snapshotCanvas = document.createElement("canvas");

/**
 * §9.1: the digest this tab may pair with the world it is rendering.
 *
 * Only the gateway computes a digest (§3), so the browser's whole knowledge of one is what
 * `/api/sessionState` handed back for a four-tuple. Pair it or say nothing: after a class-② local
 * edit the document under this renderer is no longer the one that digest describes, and reporting
 * the old one would be the "旧值" §9 refuses. `null` is the honest answer, and the gateway's
 * 身份闸 turns it into a refusal until the next readback re-pairs them.
 */
function pairedSceneDigest(): string | null {
  const read = state.session;
  if (!read || read.sceneDigest === null) return null;
  if (read.scene_id !== state.world.sceneId || read.scene_version !== state.world.version
    || read.scene_sub_version !== state.world.subVersion) return null;
  return read.sceneDigest;
}

const captureView: CaptureView = {
  image: (id) => renderer.renderSensorFrame(id, "rgb", captureCanvas, NATIVE_SENSOR)
    ? { width: captureCanvas.width, height: captureCanvas.height, base64: pngBase64(captureCanvas.toDataURL("image/png")) }
    : null,
  depth: (id) => renderer.renderSensorDepth(id),
  box: (objectId, cameraId) => renderer.projectObjectBox(objectId, cameraId),
  frameIndex: () => (state.traj ? state.playIndex : null),
  identity: () => ({ browserID: BROWSER_ID, sceneDigest: pairedSceneDigest(), playback_state: playbackState() }),
};

const usedIcons = {
  Activity, Bot, Box, Boxes, Camera, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, ChevronUp,
  Circle, CircleDot, Crosshair, Download, FileUp, Focus, Info, Lightbulb, ListTree, LoaderCircle, Mic,
  MousePointer2, Pause, Play, Repeat,
  Search, Send, SlidersHorizontal, Square, Trash2, X,
  History, Plus, RefreshCw,
};

function drawIcons(): void { createIcons({ icons: usedIcons }); }
function escapeHtml(value: unknown): string { return String(value ?? "").replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[c]!); }
function selectedObject(): WorldObjectNode | null {
  return state.selectedId === null ? null : state.world.get(state.selectedId) ?? null;
}
function enabled(path: string): boolean { return featureEnabled(state.features, path); }
function when(path: string, content: string): string { return enabled(path) ? content : ""; }

const panelFeatures: Record<Panel, string> = {
  scene: "panels.scene", details: "panels.details", assets: "panels.assets",
};

function ensureEnabledPanel(): void {
  if (enabled(panelFeatures[state.panel])) return;
  state.panel = (["scene", "details", "assets"] as Panel[])
    .find((panel) => enabled(panelFeatures[panel])) ?? "scene";
}

/**
 * §7.5 (Q157): the Agent column is lent to the asset panel while that panel is open. Both the
 * feature pass at start() and every renderContext() ask this one question, so opening and closing
 * the panel restore the column by the same rule that took it away.
 */
function applyAgentVisibility(): void {
  const visible = agentPanelVisible({
    agentEnabled: enabled("agent.conversation"), contextEnabled: enabled("panels.context"),
    panel: state.panel, panelOpen: state.panelOpen,
  });
  document.querySelector<HTMLElement>(".agent-panel")!.hidden = !visible;
  const workspace = document.querySelector<HTMLElement>(".workspace")!;
  if (visible) workspace.style.removeProperty("--agent-track"); else workspace.style.setProperty("--agent-track", "0px");
}

function applyFeatureVisibility(): void {
  ensureEnabledPanel();
  document.querySelectorAll<HTMLElement>("[data-feature]").forEach((element) => {
    element.hidden = !enabled(element.dataset.feature!)
      || Boolean(element.dataset.requiresFeature && !enabled(element.dataset.requiresFeature));
  });
  applyAgentVisibility();
  const track = (element: HTMLElement, name: string, visible: boolean): void => {
    if (visible) element.style.removeProperty(name); else element.style.setProperty(name, "0px");
  };
  const workbench = document.querySelector<HTMLElement>(".workbench")!;
  track(workbench, "--status-track", enabled("statusBar"));
  const workspace = document.querySelector<HTMLElement>(".workspace")!;
  track(workspace, "--rail-track", enabled("navigation"));
  track(workspace, "--context-track", enabled("panels.context") && state.panelOpen);
  const viewport = document.querySelector<HTMLElement>(".viewport-shell")!;
  track(viewport, "--toolbar-track", enabled("viewport.toolbar"));
  track(viewport, "--diagnostics-track", enabled("diagnostics"));
  renderer.setInteractions(enabled("viewport.orbit"), enabled("viewport.selectEntity"));
  // §4.4 (Q105/Q106): these two keys have no DOM host - they decide when the frustum lines
  // and the light placeholder spheres show in the scene graph, which is the renderer's.
  renderer.setCameraFrustumAlways(enabled("viewport.cameraFrustumAlways"));
  renderer.setLightMarkerAlways(enabled("viewport.lightMarkerAlways"));
  // §4.8.8 (Q132): `viewport.localAxesToggle` puts the checkbox on the toolbar, the checkbox says
  // whether the declared frames are drawn - the same split as the wireframe under it, so this key is
  // still the one remote exit for the whole layer even though it no longer talks to the renderer.
  renderLocalAxes();
  // §4.4 (Q131): the two pieces of viewport furniture on the same shape - no DOM host, one boolean
  // each. They only ever answer for the screen: a capture has never seen them (§4.4's exclusion list
  // and `type.endsWith("Helper")`), so a key closing a layer takes something off the editor, not off
  // an image anyone exports.
  renderer.setGridEnabled(enabled("viewport.grid"));
  renderer.setWorldAxesEnabled(enabled("viewport.worldAxes"));
  // §4.8.5 (Q118): `viewport.shadows` has a DOM host and *only* a DOM host - with the key off the
  // checkbox is gone and `shadows_enabled` speaks; see `logic.ts:shadowsEnabled()`.
  renderShadows();
  // §4.8.6 (Q119): the same split for the wireframe layer, minus the constant - with
  // `viewport.boundingBoxes` off the checkbox is gone and so are the boxes.
  renderBoxes();
  // §7.1 (Q138): and the same split again for the camera wall - `viewport.cameraWall` owns the
  // checkbox, the checkbox owns the wall, so with the key off there is no host *and* no wall.
  renderCameraWall();
  // §4.8.7 (Q121): the take's two controls, plus the renderer's page-load answer. With
  // `viewport.record` off both hosts go, and `toggleRecording()` still refuses a click that reaches
  // the hidden button (§12.22 ③ D) - the same reading Q119 pinned for the bounding-box checkbox.
  renderRecordChrome();
  if (!enabled(`viewport.cameraPreview.modes.${state.previewMode}`)) {
    state.previewMode = enabled("viewport.cameraPreview.modes.rgb") ? "rgb" : "depth";
  }
  // §7.1 (Q100): the loop above only sees the flag; the selector is also gated by how
  // many clips the browser holds, so its own rule runs last and wins.
  renderTrajSelector();
}

/** One fetch that reads the body as JSON and keeps the headers for the caller that needs them. */
async function fetchJSON<T>(url: string, init?: RequestInit): Promise<{ body: T; headers: Headers }> {
  const response = await fetch(url, withBrowserIDHeader(init, BROWSER_ID));
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(body?.error?.message ?? body?.message ?? `${response.status} ${response.statusText}`) as Error & { code?: string }; error.code = body?.error?.code; throw error; }
  return { body: body as T, headers: response.headers };
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  return (await fetchJSON<T>(url, init)).body;
}

function updateAll(): void { renderContext(); updateStatus(); updateSelection(); updateAgent(); drawIcons(); }
function selectEntity(id: number | null): void { state.selectedId = id; renderer.select(id); renderContext(); updateSelection(); }
function updateSelection(): void {
  const node = selectedObject();
  document.querySelector("#selection-label")!.textContent = node ? `${node.name}  #${node.id}` : "No selection";
}

/**
 * §4.8.2: the download is the document `/getWorld` would answer with, taken from the
 * world the browser holds. No request is made, so the export also carries whatever a
 * lighting preset or a drag changed, while `scene_version` stays where it is.
 */
function exportScene(): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.world.toJSON(), null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = sceneExportName(state.world.meta);
  link.click();
  URL.revokeObjectURL(url);
}

/** §4.4 (Q108): copy the viewport's current frame, editor layers left out, to the clipboard. */
async function copyViewportSnapshot(): Promise<void> {
  if (!renderer.captureViewport(snapshotCanvas)) {
    showError("Viewport capture unavailable: the canvas has no pixels to read back.");
    return;
  }
  const png = await new Promise<Blob | null>((resolve) => snapshotCanvas.toBlob(resolve, "image/png"));
  if (!png) { showError("The screenshot could not be encoded as PNG."); return; }
  try {
    // One await before the write keeps the click's user activation; `ClipboardItem` throwing
    // on an old browser is the reason the whole write sits inside the try.
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
  } catch (cause) {
    showError(`Screenshot not copied: ${cause instanceof Error ? cause.message : String(cause)}`);
    return;
  }
  showError(`截图已复制：${snapshotCanvas.width}×${snapshotCanvas.height} 像素`);
}

/** §4.8.7 (**Q121**): what a take costs, and how long it may run without anybody clicking. */
const RECORD_BIT_RATE = 8_000_000;
const RECORD_TIMESLICE_MS = 1_000;
const RECORD_MAX_MS = 300_000;

/**
 * §4.8.7 (**Q121**): the one take this page runs. Everything here is browser-local - each frame is
 * its own viewport pass rendered into a canvas nobody else looks at, the bytes never leave the page,
 * and the file is handed over by a download. `url` outlives the take on purpose: revoking it inside
 * the click that starts the download races the browser reading the blob back, so the previous clip's
 * URL is released when the next one is saved.
 */
const recording = {
  recorder: null as MediaRecorder | null,
  tap: null as (() => void) | null,
  chunks: [] as Blob[],
  mime: "",
  timer: 0,
  url: "",
};

/** The button is its own two states; the checkbox is what the next take would be made of (§4.8.7). */
function renderRecordChrome(): void {
  const active = recording.recorder !== null;
  const button = document.querySelector<HTMLButtonElement>('[data-action="record"]');
  if (button) {
    button.innerHTML = `<i data-lucide="${active ? "square" : "circle-dot"}"></i>`;
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.title = active ? "Stop recording and download the MP4" : "Record the viewport to an MP4 file";
  }
  const checkbox = document.querySelector<HTMLInputElement>("#record-overlays");
  if (checkbox) checkbox.checked = state.recordOverlays;
  drawIcons();
  // With nothing running the layer is still asked, and still answers: the page-load line is the
  // evidence that this one is wired (§4.8.6's same lesson - silence reads like "not connected").
  if (!active) renderer.setRecordFrameTap(null, !state.recordOverlays);
}

/** One button, two states: idle starts a take, running ends it and the file goes to the downloads folder. */
function toggleRecording(): void {
  // The key owns both hosts, and a click that reaches the hidden one must still not start a recorder
  // (§12.22 ③ D - the reading Q119 pinned for the bounding-box checkbox).
  if (!enabled("viewport.record")) {
    showError("录制功能已关闭（viewport.record）。");
    return;
  }
  if (recording.recorder) { stopRecording(); return; }
  if (typeof MediaRecorder === "undefined" || typeof document.createElement("canvas").captureStream !== "function") {
    showError("This browser cannot record the viewport: no MediaRecorder, or no canvas stream to read.");
    return;
  }
  const mime = pickRecordMime((type) => MediaRecorder.isTypeSupported(type));
  if (!mime) {
    // No MP4 encoder is a refusal, not a licence to hand over some other container (§4.8.7).
    showError("No MP4 encoder in this browser, so nothing was recorded - the stage does not fall back to WebM.");
    return;
  }
  startRecording(mime);
}

function startRecording(mime: string): void {
  const chunks: Blob[] = [];
  // The stream reads a canvas the take renders into through the §4.4 viewport pass, never the viewport
  // canvas itself: the screen shows the editor layers, and a stream of the screen has no way to leave
  // them out (§4.8.7). It has to be sized before the stream is opened, or the stream keeps the empty
  // buffer it saw first.
  const proxy = document.createElement("canvas");
  proxy.width = canvas.width;
  proxy.height = canvas.height;
  const stream = proxy.captureStream(0);
  // `requestFrame()` is the manual half of `captureStream(0)` - the push a timed stream would do on its
  // own clock, which is the clock we do not want - and the DOM typings here do not carry it.
  const track = stream.getVideoTracks()[0]! as MediaStreamTrack & { requestFrame(): void };
  const recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: RECORD_BIT_RATE });
  recorder.ondataavailable = (event) => { if (event.data.size > 0) chunks.push(event.data); };
  recorder.onstop = () => {
    stream.getTracks().forEach((candidate) => candidate.stop());
    finishClip(chunks, mime);
  };
  recorder.onerror = () => {
    // By the time this fires the recorder is already torn down, so `onstop` will not come and there
    // is no file to hand over - the take is lost, and saying otherwise would be a lie.
    showError("The encoder failed during this take, so nothing was downloaded.");
    stopRecording();
  };
  recording.recorder = recorder;
  recording.tap = () => { if (renderer.recordFrameTo(proxy)) track.requestFrame(); };
  recording.chunks = chunks;
  recording.mime = mime;
  recorder.start(RECORD_TIMESLICE_MS);
  renderer.setRecordFrameTap(recording.tap, !state.recordOverlays);
  recording.timer = window.setTimeout(() => {
    showError(`Recording reached its ${RECORD_MAX_MS / 60_000}-minute limit and was saved.`);
    stopRecording();
  }, RECORD_MAX_MS);
  renderRecordChrome();
  showError(`Recording the viewport (${state.recordOverlays ? "editor layers included" : "editor layers held out"}) - click again to stop and download.`);
}

function stopRecording(): void {
  const recorder = recording.recorder;
  if (!recorder) return;
  recording.recorder = null;
  if (recording.timer) {
    window.clearTimeout(recording.timer);
    recording.timer = 0;
  }
  // Stop feeding before stopping: a frame pushed into a closing recorder is a frame nobody asked for.
  renderer.setRecordFrameTap(null, !state.recordOverlays);
  recording.tap = null;
  // An errored recorder is already `inactive` and will never answer `stop()`, so only a live one is asked.
  if (recorder.state === "recording" || recorder.state === "paused") recorder.stop();
  renderRecordChrome();
}

/** §4.8.7 (**Q121**): the checkbox changes what the next - or the running - take is made of. */
function toggleRecordOverlays(checked: boolean): void {
  state.recordOverlays = checked;
  if (!enabled("viewport.record")) {
    showError("录制功能已关闭（viewport.record）。");
    return;
  }
  // Re-installing the same tap is how the policy changes mid-take; the frame counter belongs to the
  // take and is not reset by it, so the line the click prints still tells the truth about the clip.
  // Each delivered frame asks the pass again, so the switch takes effect on the next one rather than
  // at the next stop-and-restart. With no take running the tap is stale (that track is stopped), and
  // `renderRecordChrome()` below asks the layer its idle question instead.
  if (recording.recorder && recording.tap) renderer.setRecordFrameTap(recording.tap, !checked);
  renderRecordChrome();
  showError(checked
    ? "录制辅助图层已开启：包围盒、描边、路点与幽灵一起录进视频。"
    : "录制辅助图层已关闭：这些辅助图层不进视频，屏幕上照常显示。");
}

/** The take's whole answer: bytes to disk, or a refusal with the frame count that proves what happened. */
function finishClip(chunks: Blob[], mime: string): void {
  const frames = renderer.recordReport().frames;
  const blob = new Blob(chunks, { type: mime });
  if (blob.size === 0) {
    // Measured on this machine: with the software GL stack a take delivers frames and still ends at 0
    // bytes, so the byte count is the judge and a support answer is not proof - and a 0-byte "video"
    // is worse than no file at all (§4.8.7, 坑 94).
    showError(`Recording wrote 0 bytes after ${frames} delivered frames - no file was downloaded.`);
    return;
  }
  if (recording.url) URL.revokeObjectURL(recording.url);
  const url = URL.createObjectURL(blob);
  recording.url = url;
  const link = document.createElement("a");
  link.href = url;
  link.download = recordVideoName(new Date());
  link.click();
  showError(`Recording saved: ${frames} frames, ${Math.round(blob.size / 1024)} kB -> ${link.download}`);
}

/** §4.8.1: swap the scene's whole lighting rig for a preset, locally. */
/**
 * §4.8.1 (Q114 ③): the preset is a render-layer look, not an edit of the scene. The
 * world document is not touched, so no `syncScene`, no re-render of the hierarchy and
 * no revision of any kind - the digest of the 现场 is the same with a preset on as
 * without one. An empty choice is "Scene lights" and gives the file's own rig back.
 */
function chooseLightingPreset(id: string): void {
  // §2.3's last item: `agent_owned` alone does not touch this entry (R1), but a held lease does --
  // and the click still has to say why, so the menu is `aria-disabled`, never `disabled`.
  const frozen = lightingFreeze(state.session);
  if (frozen !== null) {
    const select = document.querySelector<HTMLSelectElement>("#lighting-preset");
    if (select) select.value = "";
    showError(frozen);
    return;
  }
  const preset = LIGHTING_PRESET_BY_ID.get(id) ?? null;
  renderer.setLightingOverride(preset);
  showError(preset ? `光照已切换：${preset.label}` : "光照已跟随场景");
}

/**
 * §2.3 / §9: put the freeze on the control that has to obey it.
 *
 * Called wherever the authoritative session reading lands, because the lease is only ever seen
 * there. A preset that was already on when the task took the lease is put back to "Scene lights" --
 * keeping it lit would mean the collected pixels depend on a look this tab chose after the fact.
 */
function renderLightingFreeze(): void {
  // §4.8.5 (Q117/Q118): shadows freeze at the same moment for the same reason - the pixels a
  // collection produces must not depend on whether this tab happened to switch them off. Only
  // the checkbox path can be frozen; a hidden checkbox means the constant config already spoke.
  renderShadows();
  const select = document.querySelector<HTMLSelectElement>("#lighting-preset");
  if (select === null) return;
  const frozen = lightingFreeze(state.session);
  if (frozen === null) {
    select.removeAttribute("aria-disabled");
    return;
  }
  select.setAttribute("aria-disabled", "true");
  if (renderer.lightingOverrideCount === 0 && select.value === "") return;
  renderer.setLightingOverride(null);
  select.value = "";
  showError(frozen);
}

/**
 * §4.8.5 (Q118): the one place that decides what the shadow stage gets.
 *
 * The decision itself is the pure `logic.ts:shadowsEnabled()` -- `viewport.shadows` only picks
 * which of the two controls speaks (the checkbox, or `shadows_enabled` in the constant config),
 * and a held lease still pins the *checkbox* path to on, because §9.1 wants collected pixels
 * repeatable. `state.shadows` keeps what the user asked for, so releasing the lease puts their
 * own setting back without anything having to remember it somewhere else.
 */
function renderShadows(): void {
  const box = document.querySelector<HTMLInputElement>("#shadows-toggle");
  const frozen = lightingFreeze(state.session) !== null;
  const on = shadowsEnabled(state.features, state.runtime, state.shadows, frozen);
  if (box) {
    box.checked = on;
    if (frozen) box.setAttribute("aria-disabled", "true"); else box.removeAttribute("aria-disabled");
  }
  renderer.setShadowsEnabled(on);
}

/** A click answers with a reason even while frozen; 点击必须有响应 (§2.3), and the box snaps back to on. */
function toggleShadows(checked: boolean): void {
  const frozen = lightingFreeze(state.session);
  if (frozen !== null) { renderShadows(); showError(frozen); return; }
  state.shadows = checked;
  renderShadows();
  showError(checked ? "投射阴影已开启" : "投射阴影已关闭");
}

/**
 * §4.8.6 (Q119): the wireframe layer answers to one switch -- `viewport.boundingBoxes` puts the
 * checkbox on the toolbar, the checkbox says whether the boxes are drawn. No constant fallback and
 * no lease freeze: with the key off the stage looks exactly like it does unchecked, and §4.4 already
 * lifts the helpers out of `/captureScene` and out of a screenshot, so collected pixels never
 * depend on this box. See `logic.ts:boundingBoxesEnabled()`.
 *
 * Q149 adds a second question *under* that switch, not beside it: `urdf_bounding_box_per_link` says
 * in which shape a URDF object's boxes appear -- one per link, or the one its links union into. It
 * cannot turn the layer on or off, so the remote exit Q119 left in `viewport.boundingBoxes` is
 * unchanged, and `/get2DAABB` keeps answering with the union whichever way this reads.
 */
function renderBoxes(): void {
  renderer.setBoundingBoxesEnabled(boundingBoxesEnabled(state.features, state.boxes));
  renderer.setLinkBoxesEnabled(urdfLinkBoxesEnabled(state.runtime));
}

/** 点击必须有响应 (§2.3): the click is answered whether or not it changed the picture. */
function toggleBoxes(checked: boolean): void {
  state.boxes = checked;
  renderBoxes();
  showError(checked ? "包围盒已显示" : "包围盒已隐藏");
}

/**
 * §4.8.8 (Q132): one switch for the layer the file declares per object -
 * `viewport.localAxesToggle` owns the checkbox, the checkbox owns the axes. The objects that own a
 * frame are still the scene file's answer (§4.9), and nothing pins this layer to a selection.
 * See `logic.ts:localAxesEnabled()`.
 */
function renderLocalAxes(): void {
  renderer.setLocalAxesEnabled(localAxesEnabled(state.features, state.localAxes));
}

/** 点击必须有响应 (§2.3), same as the box above it: the click is answered either way. */
function toggleLocalAxes(checked: boolean): void {
  state.localAxes = checked;
  renderLocalAxes();
  showError(checked ? "坐标轴已显示" : "坐标轴已隐藏");
}

/** §7.1 (**Q138**): the cells' markup last written, and the timer that repaints them. */
let wallMarkup: string | null = null;
let wallTimer = 0;
let wallPaceMs = 0;
/** §4.4 (**Q139**): 256 entries, built once - remaking the depth table every beat would be the cheap part of the job done the expensive way. */
const wallDepthPalette = depthPalette();

/**
 * §7.1 (**Q138**): the camera wall - one live frame per `Camera` object, floated over the viewport.
 *
 * The cells are built from a string, so this is the second place Q136's guard earns its keep: writing
 * it every beat would throw away the very `<canvas>` elements the pixels went into and flash the wall.
 * The rebuild happens only when the *camera set* changes; each beat paints into the canvases already
 * on screen. Every cell asks the same offscreen pass the Details preview and `/captureScene` use (§4.4),
 * so the wall shows what the sensor sees - not what this page's orbit camera happens to be looking at.
 *
 * **Q139** redrew the cell: the name went above the picture and centred, the pixel size came out of the
 * UI (the number never answered a question here - what is left is the one thing the size was hiding,
 * §5.11's "this cell has no frame"), and the header gained the RGB／深度 pair that used to exist only on
 * the Details panel.
 */
function renderCameraWall(): void {
  const wall = document.querySelector<HTMLElement>("#camera-wall")!;
  const on = cameraWallEnabled(state.features, state.cameraWall);
  const box = document.querySelector<HTMLInputElement>("#camera-wall-toggle");
  if (box) box.checked = state.cameraWall;
  wall.classList.toggle("hidden", !on);
  if (!on) { clearInterval(wallTimer); wallPaceMs = 0; return; }
  const mode = cameraWallModeFor(state.features, state.cameraWallMode);
  document.querySelectorAll<HTMLButtonElement>("[data-camera-wall-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.cameraWallMode === mode);
  });
  const cameras = findCameraObjects(state.world);
  document.querySelector("#camera-wall-count")!.textContent = String(cameras.length);
  const markup = cameras.map((node) => `<div class="camera-wall-cell"><b class="camera-wall-name">${escapeHtml(node.name)}</b><canvas data-camera-id="${node.id}" aria-label="${escapeHtml(node.name)}"></canvas><code class="camera-wall-stale" data-camera-status></code></div>`).join("")
    || `<p class="empty-line">场景里没有相机。</p>`;
  if (panelMarkupChanged(wallMarkup, markup)) {
    const cells = document.querySelector("#camera-wall-cells")!;
    cells.innerHTML = markup;
    wallMarkup = markup;
    // §4.4 (Q139): claim the 2D context before the sensor ever paints into it, so a depth cell's
    // read-modify-write stays on the CPU-backed path instead of copying a GPU buffer twice a beat.
    cells.querySelectorAll<HTMLCanvasElement>("canvas[data-camera-id]").forEach((canvas) => { canvas.getContext("2d", { willReadFrequently: true }); });
  }
  const pace = cameraWallPace(previewCaps());
  if (wallPaceMs !== pace.intervalMs) {
    clearInterval(wallTimer);
    wallTimer = window.setInterval(loadCameraWall, pace.intervalMs);
    wallPaceMs = pace.intervalMs;
  }
  loadCameraWall();
}

/**
 * One beat of the wall: every cell takes its camera's own frame in the mode the header says, or says
 * back that it has none (§5.11). A non-`rgbd` camera has no depth map at all (§4.4), so an empty cell in
 * 深度 mode names the reason instead of repeating the generic one.
 */
function loadCameraWall(): void {
  const pace = cameraWallPace(previewCaps());
  const mode = cameraWallModeFor(state.features, state.cameraWallMode);
  for (const canvas of document.querySelectorAll<HTMLCanvasElement>("#camera-wall-cells canvas[data-camera-id]")) {
    const liveliness = renderer.renderSensorFrame(Number(canvas.dataset.cameraId), mode, canvas, { maxWidth: pace.maxWidth, maxHeight: pace.maxHeight });
    if (liveliness && mode === "depth") tintDepthFrame(canvas);
    const status = canvas.parentElement?.querySelector("[data-camera-status]");
    if (status) status.textContent = liveliness ? "" : mode === "depth" ? "无深度" : "无画面";
  }
}

/**
 * §4.4 (**Q139**): paint one depth cell with the pseudo-colour table. The pass wrote the normalised view
 * depth into all three channels, so the red byte *is* the index and one `Uint32Array` assignment covers
 * a pixel - which is why this reads a table rather than calling `depthColor()` per pixel.
 */
function tintDepthFrame(canvas: HTMLCanvasElement): void {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return;
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  const pixels = new Uint32Array(image.data.buffer);
  for (let index = 0; index < pixels.length; index += 1) pixels[index] = wallDepthPalette[pixels[index]! & 0xff]!;
  context.putImageData(image, 0, 0);
}

/** 点击必须有响应 (§2.3), the same reading as the two checkboxes above it. */
function toggleCameraWall(checked: boolean): void {
  state.cameraWall = checked;
  renderCameraWall();
  showError(checked ? "相机画面已显示" : "相机画面已隐藏");
}

/**
 * §4.4 (**Q139**): the header's two buttons are one switch, so the answer to a click is the button
 * lighting up and every cell redrawing on the next line - no toast, because the change is the feedback.
 */
function chooseCameraWallMode(mode: SensorViewMode): void {
  state.cameraWallMode = mode;
  renderCameraWall();
}

/**
 * §7.1 (**Q139**): the wall is dragged by its header and never leaves the viewport it floats over.
 *
 * The stylesheet owns the resting corner; the first move converts that into `left`/`top` on the element
 * itself, which is the only way to override an anchor the panel is positioned by. Nothing is persisted:
 * a wall the user parked stays parked while the page lives, and closing the layer does not erase where
 * they put it - the same respect for where a panel was left that Q136 won for the list.
 */
function dragCameraWall(event: PointerEvent): void {
  const target = event.target as HTMLElement;
  const head = target.closest<HTMLElement>(".camera-wall-head");
  if (!head || target.closest("button")) return;
  const wall = head.parentElement!;
  const wrap = wall.parentElement!;
  const origin = { x: event.clientX, y: event.clientY };
  const panel = wall.getBoundingClientRect();
  const frame = wrap.getBoundingClientRect();
  const startLeft = panel.left - frame.left;
  const startTop = panel.top - frame.top;
  const move = (step: PointerEvent) => {
    const maxLeft = Math.max(0, wrap.clientWidth - wall.offsetWidth);
    const maxTop = Math.max(0, wrap.clientHeight - wall.offsetHeight);
    wall.style.right = "auto";
    wall.style.left = `${Math.min(maxLeft, Math.max(0, startLeft + step.clientX - origin.x))}px`;
    wall.style.top = `${Math.min(maxTop, Math.max(0, startTop + step.clientY - origin.y))}px`;
  };
  const stop = () => {
    head.removeEventListener("pointermove", move);
    head.removeEventListener("pointerup", stop);
    head.removeEventListener("pointercancel", stop);
  };
  // The header is the handle, so the pointer stays with it even when the cursor outruns the panel.
  head.setPointerCapture(event.pointerId);
  head.addEventListener("pointermove", move);
  head.addEventListener("pointerup", stop);
  head.addEventListener("pointercancel", stop);
}

/** §7.2: the tree is the world's `Transform.parent` relation, name plus id, and nothing else. */
function treeRows(parent: number | null, depth = 0): string {
  const ids = parent === null ? state.world.roots().map((node) => node.id) : state.world.childrenOf(parent);
  return ids.map((id) => {
    const node = state.world.get(id);
    if (!node) return "";
    const icon = node.camera ? "camera" : node.light ? "lightbulb" : node.robotDriver ? "bot" : "box";
    const branch = state.world.childrenOf(id).length > 0 ? "chevron-down" : "chevron-right";
    return `<div class="tree-row${id === state.selectedId ? " selected" : ""}" style="--depth:${depth}" data-entity="${id}"><i class="indent"></i><i data-lucide="${branch}"></i><i data-lucide="${icon}"></i><span>${escapeHtml(node.name)}</span><code>#${id}</code></div>${treeRows(id, depth + 1)}`;
  }).join("");
}
function renderHierarchy(): string {
  const tree = treeRows(null) || `<p class="empty-line">No objects in the world.</p>`;
  return `${when("panels.scene.search", '<label class="search"><i data-lucide="search"></i><input id="tree-search" type="search" placeholder="Search objects"></label>')}${when("panels.scene.hierarchy", `<div class="tree">${tree}</div>`)}`;
}
/** §4.7: every value the inspector shows is text, and the panel never posts anywhere. */
function readout(label: string, value: string): string { return `<div class="readout"><span>${escapeHtml(label)}</span><code>${escapeHtml(value)}</code></div>`; }
/** The parser keeps unknown component fields verbatim (§3.8), so they are printed as written. */
function describe(value: unknown): string {
  if (value === null || value === undefined) return "none";
  if (Array.isArray(value)) return value.map((item) => describe(item)).join(", ");
  if (typeof value === "number") return String(Number(value.toFixed(4)));
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** §4.7: Details reads the selected object back out of the world this browser owns. */
function renderDetails(): string {
  const node = selectedObject();
  if (!node) return when("panels.details.emptyState", `<div class="empty"><i data-lucide="info"></i><p>Select an object to inspect it.</p></div>`);
  const tags = node.rawComponents.map((component) => `<span>${escapeHtml(component.component_type)}</span>`).join("");
  const identity = when("panels.details.identity", `<section><header><strong>Identity</strong><span>${escapeHtml(node.name)}</span></header>${when("panels.details.identity.entityId", readout("Object ID", String(node.id)))}${when("panels.details.identity.parent", readout("Parent", node.transform.parent === null ? "scene root" : String(node.transform.parent)))}${when("panels.details.identity.flags", `<div class="tags">${tags}</div>`)}</section>`);
  // §8.5: playback writes poses through the driver, so these are the numbers the world file declares.
  const notice = state.traj ? when("panels.details.transform.notice", `<p class="notice">播放中的位姿来自 TRAJ 采样，此处显示世界文件声明的值。</p>`) : "";
  const transform = when("panels.details.transform", `<section><header><strong>Transform</strong><span>Local</span></header>${notice}${readout("Position", describe(node.transform.position))}${readout("Quaternion XYZW", describe(node.transform.quaternion))}${readout("Scale", describe(node.transform.scale))}</section>`);
  const model = node.metadataRef ? `<section><header><strong>Model</strong><span>MetadataRef</span></header>${readout("path", node.metadataRef.path)}</section>` : "";
  const semantic = node.semantic ? `<section><header><strong>Semantic</strong><span>world file</span></header>${readout("semantic_name", describe(node.semantic.semantic_name))}${readout("category", describe(node.semantic.category))}</section>` : "";
  const light = node.light ? `<section><header><strong>Lighting</strong><span>${escapeHtml(node.light.type ?? "Ambient")}</span></header>${readout("Intensity", describe(node.light.intensity))}${readout("Color", describe(node.light.color))}</section>` : "";
  const rig = node.camera ? cameraRig(node.camera) : null;
  const preview = enabled("panels.details.camera.preview") ? `<div class="camera-detail-preview"><div class="preview-tabs"><button type="button" data-preview="rgb" class="${state.previewMode === "rgb" ? "active" : ""}">RGB</button><button type="button" data-preview="depth" class="${state.previewMode === "depth" ? "active" : ""}">Depth</button></div><div class="preview-frame"><canvas id="detail-camera-image" aria-label="Live camera frame"></canvas><span id="detail-preview-status">no_data</span></div><footer id="detail-preview-meta"></footer></div>` : "";
  const camera = node.camera && rig ? when("panels.details.camera", `<section><header><strong>Camera</strong><span>${escapeHtml(node.camera.type ?? "rgb").toUpperCase()}</span></header>${readout("Resolution", `${rig.width}×${rig.height}`)}${readout("Vertical FOV", `${rig.verticalFovDegrees}°`)}${readout("Near plane", describe(rig.nearPlane))}${readout("Far plane", describe(rig.farPlane))}${readout("Depth", rig.depth ? "yes (rgbd)" : "no (rgb)")}${preview}</section>`) : "";
  // §4.7: the parked pose is what the world file declares; a joint the caller wrote via
  // `now_joint_positions_rad` is marked so `/updateItem` is visible without a reload.
  const pose = declaredJointPositions(node.robotDriver);
  const poseText = pose.length
    ? pose.map((entry) => `${entry.name} ${describe(entry.value)}${entry.source === "now" ? " (now)" : ""}`).join(", ")
    : "none declared";
  // §4.8.4 was the joint-angle input (Q81). Q114 puts it in the ① class: a manual write
  // to the parked pose is gone for good, so the panel reports the pose and nothing else.
  const joints = readout("Parked pose (rad)", poseText);
  // §4.7 (Q107): the panel reports the *effective* role, so a typo'd value reads as the
  // default it behaves like instead of as something it is not.
  const role = node.robotDriver ? readout("Role", node.robotDriver.role === "passive" ? "passive" : "arm") : "";
  const robot = node.robotDriver ? when("panels.details.robot", `<section><header><strong>Robot Driver</strong><span>${node.robotDriver.joint_sequence?.length ?? 0} joints</span></header>${notice}${role}${readout("Joint sequence", describe(node.robotDriver.joint_sequence ?? []))}${readout("Joint limits", describe(node.robotDriver.joint_limits ?? {}))}${joints}</section>`) : "";
  return `<div class="inspector">${identity}${transform}${model}${semantic}${light}${camera}${robot}</div>`;
}

/** §4.6 (Q154): a row shows the picture and name; categories belong to navigation. */
function assetRow(kind: LibraryKind, item: LibraryItem): string {
  const client = state.client;
  const thumb = client
    ? `<span class="thumb-frame"><img class="asset-thumb" src="${escapeHtml(client.thumbnailUrl(kind, item.id))}" alt="" loading="lazy"></span>`
    : "";
  return `<button type="button" class="asset-card" title="${escapeHtml(item.name)}" data-asset-kind="${kind}" data-asset-id="${item.id}">${thumb}<span>${when("panels.assets.name", `<strong>${escapeHtml(item.name)}</strong>`)}</span></button>`;
}

/** §4.6: the first level is the three id spaces, minus any group the flags switch off. */
function assetKinds(): LibraryKind[] {
  return LIBRARY_KINDS.filter((kind) => enabled(`navigation.assets.${GROUP_FEATURES[kind]}`));
}

/** §4.6 (Q156): the 大类 the panel actually shows - a flag can switch the stored choice off. */
function currentKind(): LibraryKind | null {
  const kinds = assetKinds();
  return kinds.includes(state.library.kind) ? state.library.kind : kinds[0] ?? null;
}

/** §4.6 (Q93/Q156): the one rail left in the panel lists the categories that have items, in `/listCategory` order. */
function assetGroups(kind: LibraryKind): AssetGroup[] {
  const section = state.library.sections[kind];
  return groupAssets(section.items, section.categories);
}

/** The chosen category, or the first one whenever the stored choice no longer exists (a kind switch). */
function currentCategory(kind: LibraryKind, groups: AssetGroup[]): string | null {
  return groups.some((group) => group.category === state.library.category) ? state.library.category : groups[0]?.category ?? null;
}

/** §4.6 (Q156): one vertical category rail on the left, the items of that category on the right. */
function renderAssets(): string {
  const kind = currentKind();
  if (!kind) return "";
  const groups = assetGroups(kind);
  const selected = currentCategory(kind, groups);
  const rows = groups.find((group) => group.category === selected)?.items ?? [];
  const empty = state.library.error ? "OLS 不可用" : state.library.loading ? "读取中…" : "这个类别没有条目";
  const notice = state.library.error ? `<p class="asset-notice">资产列表读取失败：${escapeHtml(state.library.error)}</p>` : "";
  const categories = groups.map((group) => `<button type="button" class="asset-category${group.category === selected ? " active" : ""}" data-asset-category="${escapeHtml(group.category)}" aria-current="${String(group.category === selected)}"><span title="${escapeHtml(group.category)}">${escapeHtml(categoryLabel(group.category))}</span><em>${group.items.length}</em></button>`).join("");
  // §4.6: the panel only reads OLS - upload, delete and the dependency dialog are gone (§10.4).
  const list = enabled("panels.assets.list")
    ? `<div class="asset-rows">${rows.map((item) => assetRow(kind, item)).join("") || `<p class="empty-line">${empty}</p>`}</div>`
    : "";
  return `<div class="asset-browser"><nav class="asset-categories">${categories}</nav><div class="asset-list">${notice}${list}</div></div>`;
}

/** §4.6: a failed read only says so in the panel; the live world is never touched. */
async function refreshLibrary(): Promise<void> {
  const client = state.client;
  if (!client || state.library.loading) return;
  state.library.loading = true;
  client.invalidateMetadata();
  renderContext();
  const failures: string[] = [];
  for (const kind of LIBRARY_KINDS) {
    try {
      // Sequential: three kinds x two routes is already the whole of OLS's read surface.
      const items = await client.list(kind);
      const categories = await client.listCategories(kind);
      state.library.sections[kind] = { items, categories };
    } catch (error) {
      failures.push(`${kind}: ${(error as Error).message}`);
    }
  }
  state.library.error = failures.length ? failures.join("；") : null;
  state.library.loading = false;
  renderContext();
}

/**
 * §1.3/§4 (R2, Q114 ②, S6-b): a class-② entry the phase gate refuses still answers the click --
 * and now asks. The old modal only named the way out ("Agent 面板 → 新建会话") and left the tap to be
 * started over; this one holds the tap (`pending`) and offers to run it after that door.
 * The sentence has to stay visible whatever the user chooses, so 关闭 only drops the held intent.
 */
function showPhaseBlock(reason: string, pending: PendingPhaseEdit): void {
  const box = document.querySelector<HTMLElement>("#phase-block")!;
  state.pendingPhaseEdit = pending;
  box.querySelector("p")!.textContent = reason;
  box.querySelector<HTMLElement>(".phase-offer")!.textContent = phaseOfferMessage(pending);
  box.classList.remove("hidden");
}

function dismissPhaseBlock(): void {
  state.pendingPhaseEdit = null;
  document.querySelector<HTMLElement>("#phase-block")!.classList.add("hidden");
}

/**
 * §4 (S6-b): the modal's way out, taken on the user's word.
 *
 * `createSession` is the same door the panel used to carry -- `POST /sessions`, never a
 * force-cancel -- so nothing here disturbs what AgentBrain is still computing for the old session.
 * A refusal (no conclusion yet, nothing read yet, or Brain's own `task_busy`) keeps the modal open
 * with the reason in it: the offer line is where the
 * answer goes, because a five-second toast would look exactly like a button that does nothing.
 * Only an accepted session closes it and redoes the tap that opened it.
 */
async function acceptPhaseBlock(): Promise<void> {
  const pending = state.pendingPhaseEdit;
  if (pending === null) { dismissPhaseBlock(); return; }
  const result = await createSession(true);
  if (!result.ok) {
    document.querySelector<HTMLElement>("#phase-block .phase-offer")!.textContent = result.reason ?? "新建会话没有开起来";
    return;
  }
  dismissPhaseBlock();
  if (pending.kind === "load-scene") document.querySelector<HTMLInputElement>("#scene-file")!.click();
  else await chooseLibraryItem(pending.library, pending.id);
}

/** §4.6/§12.7: a 预设场景 replaces the world; every other entry is placed into it. */
const enqueueLibraryAction = createSerialQueue();
function chooseLibraryItem(kind: LibraryKind, id: number): Promise<void> {
  return enqueueLibraryAction(() => performLibraryItemChoice(kind, id));
}

async function performLibraryItemChoice(kind: LibraryKind, id: number): Promise<void> {
  const item = state.library.sections[kind].items.find((candidate) => candidate.id === id);
  if (!item || !state.client) return;
  // §1.3: both branches are class-② entries - they change the 现场, so the phase decides.
  const blocked = conversationSceneSwitchMessage();
  if (blocked) { showPhaseBlock(blocked, { kind: "library-entry", library: kind, id, name: item.name }); return; }
  if (kind === "scene") {
    try {
      // §4.8 (Q114 ②): a preset-scene switch is a class-② edit - it advances the sub-version
      // one step above the world being left, then `applyWorld` reports the whole document, so
      // the Gateway adopts it and a refresh reads the new scene back instead of the old one.
      const world = parseWorld(await state.client.scene(id));
      world.rebaseSubVersionAfterSwitch(state.world.subVersion);
      applyWorld(world);
    } catch (error) {
      showError(`加载预设场景「${item.name}」失败：${(error as Error).message}`);
    }
    return;
  }
  try {
    // §4.6: an empty 现场 has no table to place on, so it first takes the baseline scene's furniture.
    if (state.world.objectCount === 0) applyWorld(parseWorld(await state.client.scene(BASELINE_SCENE_ID)));
    // §1.3 (Q114): a panel placement is a browser-local write, so it advances the sub-version
    // rather than the scene version - the positive control says 摘要变、`scene_version` 不变.
    // §4.6.1 (2026-11-05 穿模修复): collision reads the browser's own boxes, not OLS's AABB.
    // `sceneAABB` is the same wireframe/selection measure and reflects an articulated URDF's
    // current joint state; `settle` pushes the world so a just-added object exists to measure.
    const syncedObjectIds = new Set<number>();
    const measure: BoundsMeasure = {
      sceneAABB: (objectId) => renderer.sceneAABB(objectId),
      settle: async (objectId) => {
        // Sync once for the existing scene and again for the new provisional object,
        // not once per obstacle. Loading promises are still checked on every request.
        if (!syncedObjectIds.has(objectId)) {
          renderer.syncScene(state.world);
          for (const object of state.world.objects) syncedObjectIds.add(object.id);
        }
        // A mounted object's world box depends on its ancestors' loaded links, but
        // unrelated geometry (including the excluded arm family) must not block it.
        const required = new Set<number>();
        const pending = [objectId];
        while (pending.length > 0) {
          const id = pending.pop()!;
          if (required.has(id)) continue;
          required.add(id);
          const object = state.world.get(id);
          if (object?.transform.parent != null) pending.push(object.transform.parent);
          const attachmentParent = object?.attachment?.parent_object_id;
          if (typeof attachmentParent === "number") pending.push(attachmentParent);
        }
        await Promise.all([...required].map((id) => renderer.readyObject(id)));
      },
    };
    const placed = await placeAsset(state.world, state.client, kind, id, item.name, {}, measure);
    renderer.syncScene(state.world);
    // §7.3: a placed object does not end playback; the current frame is re-applied.
    applyWriteCoexistence();
    updateStatus();
    // §5.8: the panel write is now the world the other viewers adopt.
    reportWorld();
    selectEntity(placed.selected);
    if (placed.notice) showError(placed.notice);
  } catch (error) {
    // §4.6.1: `settle` built the provisional prop into the visible renderer at the world origin so
    // it could be measured; a refused placement rolls it back out of `world` but the renderer node
    // is not reconciled by `placeLocal`. Re-sync here or the rejected object stays at the origin as
    // an orphan until the next placement or a refresh.
    renderer.syncScene(state.world);
    updateStatus();
    showError(`摆放资产「${item.name}」失败：${(error as Error).message}`);
  }
}
/** §7.2 (Q136): the markup last actually written into the panel - see `panelMarkupChanged()`. */
let contextMarkup: string | null = null;
function renderContext(): void {
  ensureEnabledPanel();
  const definitions: Record<Panel, [string, string, () => string]> = { scene: ["SCENE", "Hierarchy", renderHierarchy], details: ["ENTITY", "Details", renderDetails], assets: ["LIBRARY", "Assets", renderAssets] };
  const [eyebrow, title, render] = definitions[state.panel]; document.querySelector("#panel-eyebrow")!.textContent = eyebrow; document.querySelector("#panel-title")!.textContent = title; document.querySelector("#panel-count")!.textContent = String(state.panel === "assets" ? LIBRARY_KINDS.reduce((total, kind) => total + state.library.sections[kind].items.length, 0) : state.world.objectCount);
  // §7.2 (Q136): writing the same string again would re-create the list and put its scroll offset back
  // to 0, so a click that changed the world but not this panel leaves the list exactly where it was.
  const markup = enabled("panels.context") ? render() : "";
  if (panelMarkupChanged(contextMarkup, markup)) {
    document.querySelector("#context-content")!.innerHTML = markup;
    contextMarkup = markup;
  }
  const panelVisible = enabled("panels.context") && state.panelOpen;
  const contextPanel = document.querySelector<HTMLElement>("#context-panel")!;
  contextPanel.classList.toggle("collapsed", !panelVisible);
  contextPanel.setAttribute("aria-hidden", String(!panelVisible));
  const workspace = document.querySelector<HTMLElement>(".workspace")!;
  if (panelVisible) workspace.style.removeProperty("--context-track"); else workspace.style.setProperty("--context-track", "0px");
  // §4.6 (Q158): the two-column list needs the width Q157 borrows from the Agent column.
  workspace.classList.toggle("assets-open", panelVisible && state.panel === "assets");
  applyAgentVisibility();
  document.querySelectorAll<HTMLElement>("[data-panel]").forEach((item) => {
    // §7.2 (Q156): the three asset buttons share one `data-panel`, so the 大类 decides which is lit.
    const active = panelTabActive({ panel: item.dataset.panel!, kind: item.dataset.assetKindNav },
      { panel: state.panel, open: panelVisible, kind: currentKind() ?? state.library.kind });
    item.classList.toggle("active", active);
    item.setAttribute("aria-expanded", String(active));
    // §4.7 (Q159): the rail's word is OLS's own first level, which only arrives with /listCategory.
    const kind = item.dataset.assetKindNav as LibraryKind | undefined;
    if (kind) item.querySelector("span")!.textContent = kindLabel(kind, state.library.sections[kind].categories);
  });
  // §4 (S4-c): the panel has just been rebuilt, so the cards it did not have a second ago are
  // tagged with whatever the phase says now.
  renderPhaseLock();
  drawIcons();
  updatePreview();
  // §7.1 (Q138): the wall hangs on the same fact this function just re-read - which cameras the world
  // has. Its cells are only rebuilt when that set changes, so this costs a string compare per render.
  renderCameraWall();
}

function updateStatus(): void {
  const connected = state.websocket === "connected";
  document.querySelector("#status-primary")!.textContent = `${connected ? "Connected" : "Disconnected"} · ${worldStatus()}`;
  document.querySelector("#status-performance")!.textContent = `${state.renderFps.toFixed(0)} FPS / ${state.renderMs.toFixed(1)} ms`;
  document.querySelector("#status-revision")!.textContent = sceneRevisionLabel(state.world.version, state.world.subVersion);
}
/**
 * §8 (R9): the service bar and the read stall note. They sit next to the task rows and say nothing
 * about any of them -- the bar quotes `GET /api/health`, the note counts this tab's own failed reads,
 * and a task's colour still comes from `terminal` + `outcome` alone (§7.4).
 */
function renderAgentStatus(): void {
  const badge = document.querySelector<HTMLElement>("#agent-state")!;
  const label = agentHealthLabel(state.agentService);
  // The panel is 285 px wide and a degraded reading quotes the fields that failed, so the badge
  // ellipsizes and the full text lives in its tooltip -- the reading is not shortened, only clamped.
  badge.textContent = label;
  badge.title = label;
  badge.className = state.agentService === null ? "unknown" : state.agentService.status;
  const stall = pollStallNotice(state.jobPollErrors);
  const note = document.querySelector("#agent-stall")!;
  note.textContent = stall ?? "";
  note.classList.toggle("hidden", stall === null);
}

/**
 * §9 item 3: the 新建会话 door, read from the one route that knows which cameras exist.
 *
 * `aria-disabled` rather than `disabled`, on purpose: the click delegation opens with a `disabled`
 * return, so a real attribute would swallow the tap and the panel would never get to say the reason
 * -- the same failure R2 was written against, and `renderPhaseLock` follows the same rule.
 */
function sessionDoor(allowActiveTask: boolean): NewSessionView {
  return newSessionView(state.session, (id) => state.world.get(id)?.name ?? null, allowActiveTask);
}

/**
 * §4/§9 item 3: open a session on the camera selected by Brain/Gateway from the current scene.
 * A refusal is returned in the caller's own words and the readout is left alone, because
 * the next successful read is what will say whether the phase moved.
 *
 * It answers to its caller instead of raising a toast because its callers have different room:
 * §4's modal (S6-b) has the screen and must keep the reason visible -- a five-second toast would
 * look like a broken button, which is the same reason the gate itself is a modal (spec §4.6) --
 * and `createConversation` throws the same sentence into the error channel it already has.
 *
 * **Q128** (2026-10-27): the door is read *with* `allowActiveTask`, because that is the flag this
 * function is about to send. Reading it without the flag refused a held lease here, so the two
 * explicit entries -- the modal's 新建会话 and the panel header's -- never reached the `/sessions`
 * branch that parks the running task in its own old session, and the offer in §4's modal was
 * refused by the very click that accepted it.
 */
async function createSession(allowActiveTask = false, resetWorld = false): Promise<{ ok: boolean; reason: string | null }> {
  const door = sessionDoor(allowActiveTask);
  if (door.blocked !== null) return { ok: false, reason: door.blocked };
  conversationSelectionGeneration += 1;
  const transitionGeneration = conversationSelectionGeneration;
  sessionCreationInFlight = true;
  const resetDocument = resetWorld ? new World({ scene_schema_version: 1, scene_version: 0, scene_sub_version: 0,
    scene_id: 0, scene_name: "" }) : null;
  try {
    const view = await json<{ sessionID: string; session_phase: SessionPhase; camera_id: number | null }>(
      "/sessions",
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ browserID: BROWSER_ID, ...(allowActiveTask ? { allowActiveTask: true } : {}),
          ...(resetDocument ? { expectedScene: resetDocument.toJSON() } : {}) }) });
    if (transitionGeneration !== conversationSelectionGeneration) return { ok: false, reason: "会话切换已被取消" };
    // Report the empty workspace only after the new binding exists, so the previous transcript
    // and its scene survive both the switch and a refused POST /sessions.
    if (resetDocument) applyWorld(resetDocument);
    // The phase may or may not have moved (resuming the same session says nothing), so what the
    // browser believes about the binding comes from the authoritative route, not from this answer.
    await refreshSessionState();
    if (transitionGeneration !== conversationSelectionGeneration) return { ok: false, reason: "会话切换已被取消" };
    // A Gateway session is also the boundary of the Agent transcript.  The explicit button must
    // create a fresh history row even when Brain resumes the same durable session id.
    await openConversationForSession(view.sessionID, resetWorld ? 0 : state.session?.scene_id ?? state.world.sceneId, true);
    // Q126 (2026-10-27): `state.session?.sessionID !== view.sessionID` used to be a third reason
    // to call this switch cancelled, and it fired on every click. The `scene.applied` echo of the
    // reset document schedules its own read ~4 ms after `POST /sessions` answers; that read is the
    // newer one, so the awaited read discarded its own answer (measured: scheduled 7.203/discarded
    // 7.409 against 7.207/applied 7.511) and the belief was still the previous session when the
    // clause ran -- while the same read proved the Gateway had in fact bound the new id 100 ms
    // later. The POST is the authority on the binding it just made, so the only thing that can
    // cancel this transition is a newer selection -- here, after the conversation POST.
    if (transitionGeneration !== conversationSelectionGeneration) return { ok: false, reason: "会话切换已被取消" };
    return { ok: true, reason: null };
  } catch (error) {
    // Gateway 的 `task_busy`／Brain 自己的拒答都在这里保持原话（§4 第 2 层不替对方改口径）。
    return { ok: false, reason: `新建会话失败：${(error as Error).message}` };
  } finally {
    sessionCreationInFlight = false;
  }
}

function updateAgent(): void {
  const latestJobs = new Map<string, AgentMessage>();
  for (const message of state.messages) if (message.jobId) latestJobs.set(message.jobId, message);
  state.messages = state.messages.filter((message, index, messages) =>
    !message.jobId || messages.findIndex((candidate) => candidate.jobId === message.jobId) === index);
  for (let index = 0; index < state.messages.length; index += 1) {
    const message = state.messages[index]!;
    if (message.jobId) state.messages[index] = latestJobs.get(message.jobId) ?? message;
  }
  renderAgentStatus();
  const target = document.querySelector("#agent-messages")!; target.innerHTML = state.messages.length ? state.messages.map(agentMessageMarkup).join("") : when("agent.messages.emptyState", `<div class="agent-empty"><i data-lucide="bot"></i><strong>Agent</strong><p>向Agent发送命令</p></div>`); target.scrollTop = target.scrollHeight; localStorage.setItem(`robot-sim-agent:${state.world.meta.scene_id}`, JSON.stringify(state.messages.slice(-200))); drawIcons();
}
function renderConversationHistory(): void {
  const panel = document.querySelector<HTMLElement>("#agent-history")!;
  panel.classList.toggle("hidden", !state.historyOpen);
  const target = document.querySelector<HTMLElement>("#agent-history-list")!;
  target.innerHTML = state.conversationHistory.map((item) => `
    <div class="conversation-row${item.conversationId === state.activeConversationId ? " active" : ""}">
      <button type="button" class="conversation-select" data-conversation-id="${escapeHtml(item.conversationId)}">
        <strong>${escapeHtml(item.title || "New conversation")}</strong><small>${new Date(item.updatedAt).toLocaleString()}</small>
      </button><span class="conversation-time">${relativeTime(item.updatedAt)}</span>
      <button type="button" class="conversation-delete" data-delete-conversation="${escapeHtml(item.conversationId)}" title="Delete conversation" aria-label="Delete conversation" ${item.conversationId === state.activeConversationId ? "disabled" : ""}><i data-lucide="trash-2"></i></button>
    </div>`).join("") || `<p class="empty-line">No conversation history.</p>`;
  drawIcons();
}
function relativeTime(value: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  if (!Number.isFinite(seconds) || seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}天前`;
  if (seconds < 86400 * 365) return `${Math.floor(seconds / (86400 * 30))}个月前`;
  return `${Math.floor(seconds / (86400 * 365))}年前`;
}
async function refreshConversationHistory(expectedConversationId?: string): Promise<void> {
  const result = await json<{ activeConversationId: string | null; conversations: ConversationSummary[] }>("/api/agent/conversations");
  if (expectedConversationId !== undefined && state.activeConversationId !== expectedConversationId) return;
  state.activeConversationId = conversationListActiveId(result.activeConversationId, expectedConversationId);
  state.conversationHistory = result.conversations;
  renderConversationHistory();
}
async function appendConversationMessage(role: "user" | "assistant", content: string, turnId: string,
                                         explicitConversationId?: string): Promise<void> {
  const conversationId = explicitConversationId ?? state.activeConversationId;
  if (!conversationId || !content.trim()) return;
  const key = `${conversationId}:${role}:${turnId}`;
  if (persistedConversationMessages.has(key)) return;
  persistedConversationMessages.add(key);
  try {
    await json(`/api/agent/conversations/${encodeURIComponent(conversationId)}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, content, turnId }),
    });
    await refreshConversationHistory(conversationId);
  } catch (error) {
    // The live row remains visible; a later refresh can still show the Gateway-side failure
    // without making a task fail just because transcript persistence was unavailable.
    persistedConversationMessages.delete(key);
    console.warn(`[conversation] could not persist ${role} message: ${(error as Error).message}`);
  }
}

/** Clear all browser-local Agent context before opening the transcript for a new Brain session. */
function clearAgentContextForSession(): void {
  state.messages = [];
  state.agentDebug = [];
  state.agentPlan = [];
  state.agentPlanKey = null;
  state.agentPending = null;
  state.jobPollErrors = 0;
  localStorage.removeItem(`robot-sim-agent:${state.world.meta.scene_id}`);
  updateAgentDebug();
}

/** Create (and immediately list) the history row belonging to one durable Brain session. */
async function openConversationForSession(sessionID: string, sceneID: number | null, force = false): Promise<void> {
  if (!sessionID) return;
  if (!force && sessionConversationSessionID === sessionID && state.activeConversationId) return;
  const generation = conversationSelectionGeneration;
  while (sessionConversationInFlight) {
    await sessionConversationInFlight;
    if (generation !== conversationSelectionGeneration) return;
    if (!force && sessionConversationSessionID === sessionID && state.activeConversationId) return;
  }
  const selectedId = state.activeConversationId;
  const isCurrent = () => generation === conversationSelectionGeneration
    && state.activeConversationId === selectedId && state.session?.sessionID === sessionID;
  if (!isCurrent()) return;
  const work = (async () => {
    const existing = await reusableConversation(selectedId, sessionID, force,
      (id) => json<ConversationDetail>(`/api/agent/conversations/${encodeURIComponent(id)}`), isCurrent);
    if (!isCurrent()) return;
    if (existing) {
      applyConversation(existing);
      await refreshConversationHistory(existing.conversationId);
      return;
    }
    clearAgentContextForSession();
    const detail = await json<ConversationDetail>("/api/agent/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionID, sceneID, title: `场景会话 ${sessionID.slice(0, 8)}`, scene: state.world.toJSON() }),
    });
    if (!isCurrent()) return;
    applyConversation(detail);
    await refreshConversationHistory(detail.conversationId);
  })();
  sessionConversationInFlight = work;
  try {
    await work;
  } catch (error) {
    // The session itself remains valid; keep the old transcript cleared and report the history
    // failure instead of silently showing context from another session.
    showError(`会话历史创建失败：${(error as Error).message}`);
  } finally {
    if (sessionConversationInFlight === work) sessionConversationInFlight = null;
  }
}
async function toggleConversationHistory(): Promise<void> {
  state.historyOpen = !state.historyOpen;
  renderConversationHistory();
  if (state.historyOpen) {
    try { await refreshConversationHistory(); }
    catch (error) { state.historyOpen = false; renderConversationHistory(); showError((error as Error).message); }
  }
}
async function activateConversation(conversationId: string): Promise<void> {
  const generation = ++conversationSelectionGeneration;
  try {
    const detail = await json<ConversationDetail>(`/api/agent/conversations/${encodeURIComponent(conversationId)}/activate`, { method: "POST" });
    if (generation !== conversationSelectionGeneration) return;
    applyConversation(detail);
    // A history row is a session *and* its scene. The Gateway switches its Brain binding before
    // returning this detail; only then is it safe to report the saved document back to the browser.
    if (detail.sceneSnapshot && typeof detail.sceneSnapshot === "object") {
      try {
        applyWorld(parseWorld(detail.sceneSnapshot));
        await refreshSessionState();
      } catch (error) {
        showError(`恢复会话场景失败：${(error as Error).message}`);
      }
    }
  } catch (error) { showError((error as Error).message); }
}
async function createConversation(): Promise<void> {
  if (conversationTransitionInFlight) return conversationTransitionInFlight;
  const work = (async () => {
    // Isolate a running task in its old session; this is not an unlock. Gateway
    // still refuses quarantine and unconfirmed writes before switching. Q128: the door now reads
    // this same flag, so it no longer refuses on the browser's side what the POST was told to allow.
    const result = await createSession(true, true);
    if (!result.ok) throw new Error(result.reason ?? "新建会话失败");
    showAgentToast("已新建对话并切换到空场景");
  })();
  conversationTransitionInFlight = work;
  try { await work; }
  catch (error) { showError((error as Error).message); }
  finally { if (conversationTransitionInFlight === work) conversationTransitionInFlight = null; }
}
async function deleteConversation(conversationId: string): Promise<void> {
  try {
    await json(`/api/agent/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
    state.conversationHistory = state.conversationHistory.filter((item) => item.conversationId !== conversationId);
    renderConversationHistory();
    showAgentToast("对话已删除");
  } catch (error) { showError((error as Error).message); }
}
function showAgentToast(message: string): void {
  const toast = document.querySelector<HTMLElement>("#agent-toast")!;
  window.clearTimeout(agentToastTimer);
  toast.textContent = message;
  toast.classList.remove("hidden");
  agentToastTimer = window.setTimeout(() => toast.classList.add("hidden"), 2400);
}
function applyConversation(detail: ConversationDetail): void {
  state.activeConversationId = detail.conversationId;
  if (detail.sessionID) sessionConversationSessionID = detail.sessionID;
  persistedConversationMessages.clear();
  for (const message of detail.messages) persistedConversationMessages.add(`${detail.conversationId}:${message.role}:${message.turnId}`);
  state.messages = detail.messages.map((message) => ({
    role: message.role === "assistant" ? "agent" : "user",
    text: message.content,
    planKey: message.role === "assistant" ? message.turnId : undefined,
    time: Date.parse(message.createdAt) || Date.now(),
  }));
  state.agentDebug = (detail.debugEvents ?? []).map((entry, index) => ({
    jobId: String(entry.turnId ?? "history"), index,
    event: entry.payload ?? {},
  }));
  state.agentPlan = [];
  state.agentPlanKey = null;
  state.agentPending = null;
  state.jobPollErrors = 0;
  state.historyOpen = false;
  // The scene is restored by activateConversation after the Gateway has switched its binding.
  updateAll();
  updateAgentDebug();
  renderConversationHistory();
  void hydrateRestoredJobs(detail.conversationId);
}
/**
 * §5.15: the journal stores each Agent sentence under the task id that produced it, but not the
 * verdict, so switching back to a finished session would show the reply with no state. The gateway
 * still answers for those tasks; only the ids this transcript names are asked for, because the
 * blanket catch-up in `refreshAgentJobs` would push another session's tasks into this row list.
 */
async function hydrateRestoredJobs(conversationId: string): Promise<void> {
  const keys = new Set(state.messages.filter((message) => !message.jobId && message.planKey)
                                      .map((message) => message.planKey));
  if (!keys.size) return;
  try {
    const result = await json<{ jobs: AgentJob[] }>("/api/agent/jobs");
    if (state.activeConversationId !== conversationId) return;
    for (const job of result.jobs) {
      if (state.activeConversationId !== conversationId) return;
      if (keys.has(job.id)) updateAgentJob(job);
    }
  } catch (error) {
    console.warn(`[agent] could not restore the states of past tasks: ${(error as Error).message}`);
  }
}
function updateAgentDebug(): void { const target = document.querySelector<HTMLElement>("#agent-debug-events")!; document.querySelector("#agent-debug-count")!.textContent = String(state.agentDebug.length); for (const entry of state.agentDebug) { const plan = agentPlanItemsFromEvent(entry.event); if (plan.length) { state.agentPlan = plan; state.agentPlanKey = entry.jobId; } } target.innerHTML = state.agentDebug.map((entry) => { const item = entry.event.item; const itemType = item && typeof item === "object" ? (item as Record<string, unknown>).type : undefined; const type = String(entry.event.type ?? itemType ?? "event"); return `<article><header><code>${escapeHtml(entry.jobId.slice(0, 8))}</code><span>${escapeHtml(type)}</span></header><pre>${escapeHtml(JSON.stringify(entry.event, null, 2))}</pre></article>`; }).join("") || `<p class="empty-line">No debug events.</p>`; updateAgent(); target.scrollTop = target.scrollHeight; }

let previewTimer = 0;
let previewState = "";
/** §9.1: `camera_preview` from the runtime config caps the preview's size and rate. */
function previewCaps(): { maxWidth: number; maxHeight: number; maxHz: number } {
  const caps = state.runtime?.camera_preview;
  return { maxWidth: caps?.max_width ?? 640, maxHeight: caps?.max_height ?? 480, maxHz: caps?.max_hz ?? 30 };
}
function updatePreview(): void {
  clearInterval(previewTimer);
  const node = selectedObject();
  if (!node?.camera || state.panel !== "details" || !state.panelOpen || !enabled("panels.details.camera")) return;
  const depthCapable = node.camera.type === "rgbd";
  if (!depthCapable) state.previewMode = "rgb";
  document.querySelectorAll<HTMLButtonElement>("[data-preview]").forEach((button) => {
    button.disabled = button.dataset.preview === "depth" && !depthCapable;
    button.classList.toggle("active", button.dataset.preview === state.previewMode);
  });
  loadPreview();
  previewTimer = window.setInterval(loadPreview, 1000 / Math.max(1, previewCaps().maxHz));
}
function loadPreview(): void {
  const node = selectedObject();
  const canvas = document.querySelector<HTMLCanvasElement>("#detail-camera-image");
  const status = document.querySelector("#detail-preview-status");
  if (!node?.camera || !canvas || !status || !state.panelOpen) return;
  const caps = previewCaps();
  const rig = cameraRig(node.camera);
  // The adapter returns false when the object is not a camera or the world has no such id (§5.11).
  const liveliness = renderer.renderSensorFrame(node.id, state.previewMode, canvas, { maxWidth: caps.maxWidth, maxHeight: caps.maxHeight });
  const result = liveliness ? "live" : "unavailable";
  status.textContent = result;
  const key = `${node.id}:${state.previewMode}:${result}:${canvas.width}x${canvas.height}`;
  if (key !== previewState) { console.debug("Camera preview state", { objectId: node.id, mode: state.previewMode, status: result }); previewState = key; }
  const meta = document.querySelector("#detail-preview-meta");
  if (meta) meta.textContent = `${canvas.width}×${canvas.height} preview · ${rig.width}×${rig.height} sensor · ${caps.maxHz} Hz${state.previewMode === "depth" ? " · near black / far white" : ""}`;
}
function showError(message: string): void { if (!enabled("viewport.notifications")) return; const toast = document.querySelector("#toast")!; toast.textContent = message; toast.classList.remove("hidden"); setTimeout(() => toast.classList.add("hidden"), 5000); }

/**
 * §7.3 (Q100): install a batch of clips and pick which one is active. Null or an empty
 * list ends playback and lets the renderer put every node back where the world file
 * says - the trajectory never wrote to the world, so that is enough to undo it.
 * Switching clips runs through here too (§7.1), which is why the playhead always rests
 * at frame 0: two clips need not share an fps or an object set, so "the same frame"
 * would mean something different in each.
 */
function setTrajectory(trajs: TrajEntry[] | null, index = 0): void {
  pausePlayback();
  state.trajs = trajs ?? [];
  state.trajIndex = state.trajs.length ? index : 0;
  state.playIndex = 0;
  const active = state.traj;
  frames = active ? new TrajFrameProvider(active, driver) : null;
  driver.reset();
  renderTrajSelector();
  const bar = document.querySelector<HTMLElement>("#timeline")!;
  // §4.7: Details reports that live poses come from TRAJ sampling, so it redraws with the clip.
  if (!frames || frames.lastFrame < 1) {
    bar.classList.add("hidden");
    // No clip means nothing to preview (§7.3, Q96), and this is the same path a write
    // that ends playback goes through (§5.4, Q76).
    renderer.clearGhost();
    logGhosts();
    // Waypoints preview the clip too, so they end with it (§7.3, Q101).
    waypointCache = new Map();
    renderer.clearWaypoints();
    logWaypointLayers();
    renderer.syncScene(state.world);
    // No clip is driving joints any more, so the arms return to the declared pose (§4.7).
    renderer.reparkRobots();
    updatePlaybackUi();
    renderContext();
    return;
  }
  bar.classList.remove("hidden");
  const range = document.querySelector<HTMLInputElement>("#timeline-range")!;
  range.max = String(frames.lastFrame);
  renderPlaybackTicks(frames);
  showFrame(0);
  // §7.3 (Q101): entering playback is what draws the waypoints, so the sweep belongs here,
  // after the stage has the clip's own frame back.
  recomputeWaypoints();
  renderContext();
}

/**
 * §7.1 (Q100): list the batch in the toolbar and point the menu at the active clip. One
 * clip is not a choice, and `playback.trajSelector` off means "always play the first", so
 * both states hide the select instead of showing a menu with one option in it.
 */
function renderTrajSelector(): void {
  const select = document.querySelector<HTMLSelectElement>("#traj-select")!;
  select.hidden = state.trajs.length < 2 || !enabled("playback.trajSelector");
  select.innerHTML = state.trajs.map((entry, index) => {
    // Local files are labelled by name; a caller's batch is numbered, because the wire
    // carries no names (§5.12).
    const label = `${entry.name ?? `轨迹 ${index + 1}`} · ${entry.traj.totalFrames} 帧 · ${entry.traj.fps} fps`;
    return `<option value="${index}">${escapeHtml(label)}</option>`;
  }).join("");
  select.value = String(state.trajIndex);
}

/** Apply one frame locally: the whole scene is a function of the frame number (§8.5, §8.6). */
function showFrame(index: number): void {
  const frame = frames?.frame(index);
  if (!frame) return;
  state.playIndex = frame.index;
  updatePlaybackUi();
  updateGhostPose();
}

/**
 * Which arms a clip's waypoints belong to (§7.3, Q101): the objects it carries that have a
 * `RobotDriver` in the world and have no other driver hanging from them. The chain is the
 * world file's own parentage - a mounted gripper names the arm in `RobotAttachment` (**Q89**)
 * and a plainly-hung one in `Transform.parent`, and the two agree for an accepted mount
 * (`world.ts:applyAttachment`), so the mount is read first and the parent is the fallback.
 * The candidate filter reads `armDriver()`, not `robotDriver()`: a passive URDF model is out
 * of this list (**Q107**), and because that happens before the leaves are found, a passive
 * microwave on the wrist cannot demote the gripper hanging next to it.
 */
function leafArmTargets(traj: Traj): number[] {
  const driven = traj.objects
    .filter((object) => state.world.armDriver(object.objectId))
    .map((object) => object.objectId);
  return leafArmIds(driven, (objectId) => {
    const node = state.world.get(objectId);
    return node?.attachmentMount?.objectId ?? node?.transform.parent ?? null;
  });
}

/** One layer per (clip, leaf arm) pair, because a batch can hold two disconnected arms (§7.3, Q101). */
function waypointKey(trajectoryIndex: number, objectId: number): string {
  return `${trajectoryIndex}:${objectId}`;
}

/**
 * Sweep the batch for where each leaf arm's end effector has been (§7.3, Q101/Q102): every
 * clip is played through its own provider, one sampled frame at a time, and the live playhead
 * is put back at the end so the sweep leaves the picture exactly as it found it. It is
 * synchronous and sends nothing, and it needs no hook on a caller's write - **Q76** ends
 * playback for every write, and playback ending takes the waypoints with it.
 */
function recomputeWaypoints(): void {
  waypointCache = new Map();
  if (!enabled("playback.trajWaypoints")) {
    renderer.clearWaypoints();
    logWaypointLayers();
    return;
  }
  const { strideFrames } = waypointSettings(state.runtime);
  const rest = state.playIndex;
  for (let index = 0; index < state.trajs.length; index += 1) {
    const entry = state.trajs[index];
    if (!entry) continue;
    const paths = leafArmTargets(entry.traj).map((objectId) => ({ objectId, points: [] as Vec3[] }));
    if (!paths.length) continue;
    const provider = new TrajFrameProvider(entry.traj, driver);
    for (const frame of waypointFrames(entry.traj.totalFrames, strideFrames)) {
      provider.frame(frame);
      for (const path of paths) {
        const point = renderer.toolTipPosition(path.objectId);
        if (point) path.points.push(point);
      }
    }
    // §8.6: this clip's Attach table hung objects on its arms for the sweep; put them back
    // where the world file hangs them before the next clip reads a hierarchy it did not write.
    for (const event of entry.traj.attaches) driver.restoreParent(event.childId);
    const swept = paths.filter((path) => path.points.length > 0);
    if (swept.length) waypointCache.set(index, swept);
  }
  showFrame(rest);
  renderWaypoints();
}

/**
 * §8.5, §7.3 (Q101): a robot's URDF can land after the clip that drives it, and a fresh
 * subtree carries the angles the world file declares - so without this the arm snaps back
 * to its pose under a timeline that still reads the frame it is on, and the sweep that
 * could not measure a tool centre point before would never run at all. One pass covers
 * both, because every robot of a scene can land in the same tick.
 */
let robotLandingTimer = 0;
function scheduleRobotLanding(): void {
  if (!frames) return;
  if (robotLandingTimer) window.clearTimeout(robotLandingTimer);
  robotLandingTimer = window.setTimeout(() => {
    robotLandingTimer = 0;
    showFrame(state.playIndex);
    recomputeWaypoints();
  }, 0);
}

/**
 * Paint what the cache holds (§7.3, Q101): every layer is on the stage and the checkbox
 * decides which are visible. The colour follows the trajectory index, so a clip keeps its own
 * colour across the two states instead of swapping the moment the box is ticked.
 */
function renderWaypoints(): void {
  const paths: WaypointPath[] = [];
  const visible = new Set<string>();
  for (const [index, entries] of waypointCache) {
    for (const entry of entries) {
      const key = waypointKey(index, entry.objectId);
      paths.push({ key, trajectoryIndex: index, objectId: entry.objectId, color: waypointColor(index), points: entry.points });
      if (index === state.trajIndex || state.waypoints.showAll) visible.add(key);
    }
  }
  renderer.setWaypointPaths(paths, waypointSettings(state.runtime).radius);
  renderer.setWaypointVisibility(visible);
  logWaypointLayers();
}

/** §12.13: one diagnostics line per paint - the evidence counts layers, not pixels. */
function logWaypointLayers(): void {
  console.debug(`Trajectory waypoints ${JSON.stringify({ active: state.trajIndex, showAll: state.waypoints.showAll, layers: renderer.waypointLayers() })}`);
}

/**
 * The objects the segment-end ghost mirrors (§7.3, Q98): every object the clip carries
 * that has a `RobotDriver` in the world, each previewing the same end-of-step frame. An
 * object with no segments of its own still gets one; its sampled pose is the default, so
 * that ghost simply coincides with the live robot. `armDriver()` is the gate (**Q107**): a
 * passive URDF model is driven like any other robot but never mirrored, because a fleet of
 * microwaves ghosting around is not a preview of where an arm is about to go.
 */
function ghostTargets(): Array<{ object: TrajObject; frame: number }> {
  const traj = state.traj;
  if (!traj || !frames || !enabled("playback.ghostPose")) return [];
  const step = stepAtFrame(frames.steps, state.playIndex);
  if (!step) return [];
  return traj.objects
    .filter((object) => state.world.armDriver(object.objectId))
    .map((object) => ({ object, frame: step.endFrame }));
}

let lastGhostLogKey = "";

/**
 * §12.15 (Q107): one line per change of the mirrored set. A ghost is not an object
 * (`/getWorld` never lists it, §5.5) and pixels only say "something was drawn", so this is
 * the only page-side readout of *which* robots are ghosted - the reason `markerStates()` and
 * `waypointLayers()` got theirs (**Q105**, **Q101**).
 */
function logGhosts(): void {
  const states = renderer.ghostStates();
  const key = JSON.stringify(states);
  if (key === lastGhostLogKey) return;
  lastGhostLogKey = key;
  console.debug(`Ghosts ${JSON.stringify({ active: state.trajIndex, playhead: state.playIndex, ghosts: states })}`);
}

/**
 * Paint the ghosts at the end of the step the playhead is in (§7.3, Q96/Q97/Q98). The
 * joints come from the same write path the live arms use, so the preview can never show a
 * pose an arm could not hold; the warnings are swallowed because the live driver has
 * already reported the out-of-range joint once.
 */
function updateGhostPose(): void {
  const targets = ghostTargets();
  const opacity = ghostOpacity(state.runtime);
  renderer.retainGhosts(new Set(targets.map((target) => target.object.objectId)));
  for (const target of targets) {
    const driver = state.world.robotDriver(target.object.objectId);
    if (!driver) continue;
    const joints = renderer.syncGhost(target.object.objectId);
    if (!joints) {
      // The second instance is still loading: once it exists, the same call paints it.
      void renderer.buildGhost(target.object.objectId, opacity).then((built) => { if (built) updateGhostPose(); });
      continue;
    }
    applyFrameJointPositions(target.object.objectId, driver,
      poseAtFrame(target.object, target.frame), joints, () => {});
  }
  logGhosts();
}

/**
 * Draw the `step_count` and Attach marks the clip declares (§7.3, Q90). The two kinds
 * look identical and carry no text: a mark's position on the strip is the whole
 * message, and clicking one seeks there.
 */
function renderPlaybackTicks(provider: TrajFrameProvider): void {
  const host = document.querySelector<HTMLElement>("#timeline-ticks")!;
  const share = (frame: number): number => (frame / provider.lastFrame) * 100;
  const mark = (kind: string, frame: number): string =>
    `<span class="timeline-tick ${kind}" data-seek-frame="${frame}" style="left:${share(frame).toFixed(3)}%"></span>`;
  const marks: string[] = [];
  if (enabled("playback.stepMarkers")) for (const step of provider.steps) marks.push(mark("step", step.startFrame));
  if (enabled("playback.attachMarkers")) for (const event of provider.attaches) marks.push(mark("attach", event.frame));
  host.innerHTML = marks.join("");
}

function updatePlaybackUi(): void {
  const provider = frames;
  const total = provider?.durationSeconds ?? 0;
  const time = provider ? provider.simulationTime(state.playIndex) : 0;
  document.querySelector("#timeline-time")!.textContent = `${time.toFixed(2)} / ${total.toFixed(2)} s`;
  document.querySelector("#timeline-frame")!.textContent = `${state.playIndex} / ${provider?.lastFrame ?? 0}`;
  document.querySelector<HTMLInputElement>("#timeline-range")!.value = String(state.playIndex);
  document.querySelector<HTMLButtonElement>('[data-timeline="loop"]')!.classList.toggle("active", state.loop);
  const play = document.querySelector<HTMLButtonElement>('[data-timeline="play"]')!;
  const title = state.playing ? "Pause" : "Play";
  if (play.title !== title) {
    play.innerHTML = `<i data-lucide="${state.playing ? "pause" : "play"}"></i>`;
    play.title = title;
    drawIcons();
  }
}

function startPlayback(): void {
  const provider = frames;
  if (!provider) return;
  if (state.playIndex >= provider.lastFrame) showFrame(0);
  state.playing = true;
  anchorWallMs = performance.now();
  anchorSimulationTime = provider.simulationTime(state.playIndex);
  playbackHandle = requestAnimationFrame(tickPlayback);
  updatePlaybackUi();
}

function pausePlayback(): void {
  state.playing = false;
  if (playbackHandle) cancelAnimationFrame(playbackHandle);
  playbackHandle = 0;
  updatePlaybackUi();
}

/** One bar button: play/pause, single-frame steps, loop. */
function playbackControl(control: string | undefined): void {
  if (control === "play") {
    if (state.playing) pausePlayback();
    else startPlayback();
    return;
  }
  if (control === "prev" || control === "next") {
    // Stepping is a manual read of the clip, so the clock stops first.
    pausePlayback();
    showFrame(state.playIndex + (control === "next" ? 1 : -1));
  } else if (control === "loop") {
    state.loop = !state.loop;
    updatePlaybackUi();
  }
}

/** §8.7: one animation frame of playback - late ticks skip to the due frame. */
function tickPlayback(now: number): void {
  const provider = frames;
  if (!provider || !state.playing) return;
  const target = anchorSimulationTime + Math.max(0, now - anchorWallMs) * state.rate / 1000;
  const playhead = provider.playhead(state.playIndex, target, state.loop);
  if (playhead.index !== state.playIndex) {
    showFrame(playhead.index);
    if (playhead.index === 0) {
      // A wrapped loop re-anchors the clock; the frames it skipped are not owed.
      anchorWallMs = now;
      anchorSimulationTime = 0;
    }
  }
  if (playhead.finished) {
    pausePlayback();
    return;
  }
  playbackHandle = requestAnimationFrame(tickPlayback);
}

/** §7.3 (Q88): the five preset options are the only rate a caller can pick. */
function setPlaybackRate(rate: number): void {
  state.rate = rate;
  const provider = frames;
  if (!provider || !state.playing) return;
  // Changing speed mid-play re-anchors the clock so the new rate starts now (Q42).
  anchorWallMs = performance.now();
  anchorSimulationTime = provider.simulationTime(state.playIndex);
}

/**
 * §7.3, revised by **Q76** (2026-10-14): a caller write means the scene is being edited,
 * not played, so **every** write ends playback and the arms go back to the pose their own
 * world file declares (§ 场景文件描述 §4.7). The old rule re-applied the current frame
 * after an item write, which left the stage half clip / half file. A trajectory call
 * after the write hands the objects and the joints back to the clip (§8.5).
 */
/** §5.1: what the `getWorld` receipt reports beside the document. A loaded clip has moved the
 * drivers, even at frame 0, so only dropping the clip back to `stopped` puts them home (§8.5). */
function playbackState(): PlaybackState {
  if (state.playing) return "playing";
  return frames ? "paused" : "stopped";
}

function applyWriteCoexistence(): void {
  setTrajectory(null);
}

function connectWebSocket(): void {
  reconnectTimer = null;
  state.websocket = state.websocket === "connecting" ? "connecting" : "reconnecting"; updateStatus(); const connection = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/events`);
  socket = connection;
  connection.onopen = () => { if (reconnectTimer !== null) { window.clearTimeout(reconnectTimer); reconnectTimer = null; } const hello: BrowserHello = { type: "browser.hello", browser_id: BROWSER_ID, role: "viewer" }; connection.send(JSON.stringify(hello)); // §5.15/Q87: only a reconnect missed broadcasts; the first open has nothing to catch up on.
    // §5.5 (Q112): re-report the loaded scene so a gateway that restarted loses no water mark --
    // otherwise its `X-Next-Object-ID` is behind and an id-free /addObject pre-claims a spent id.
    // Report even an empty world: it is the authoritative scene switch and must clear camera
    // candidates from the previous scene instead of leaving a stale camera visible.
    if (firstSocketOpenDone) {
      // Reconnect: this tab was deaf and missed broadcasts, and the Gateway may be a new process.
      // Read the newly bound authority first; otherwise a restart into the protocol empty scene
      // would be overwritten by stale browser memory on the first state_sync.
      void loadAuthoritativeWorld().then(() => reportWorld());
    } else {
      // First open: `start()` already read the authoritative world before connecting, so pulling
      // again here only races a scene the user is loading and can flash it back to the empty
      // gateway scene (2026-11-05). Push the world we already hold instead of overwriting it.
      firstSocketOpenDone = true;
      void reportWorld();
    }
    // §4: the phase may have moved while this tab was deaf, and no broadcast reaches a closed
    // socket -- so every open, first or re-, asks the route that answers from disk.
    void refreshSessionState();
    const caughtUp = state.websocket === "reconnecting"; state.websocket = "connected"; updateStatus(); if (caughtUp) void refreshAgentJobs(); };
  connection.binaryType = "arraybuffer";
  connection.onmessage = (message) => {
    if (typeof message.data === "string") handleGatewayFrame(connection, message.data);
    else acceptBinaryPayload(message.data as ArrayBuffer);
  };
  connection.onclose = () => { pendingBinary = null; if (socket !== connection) return; state.websocket = "reconnecting"; updateStatus(); if (reconnectTimer === null) reconnectTimer = window.setTimeout(connectWebSocket, 1500); }; connection.onerror = () => { if (connection.readyState !== WebSocket.CLOSED) connection.close(); };
}

/** §5.3: the request that asked for bytes waits for exactly one following binary frame. */
let pendingBinary: { connection: WebSocket; request: BrowserRequest } | null = null;

function acceptBinaryPayload(data: ArrayBuffer): void {
  const pending = pendingBinary;
  pendingBinary = null;
  if (!pending) {
    console.warn("[ws] binary frame with no pending updateTraj request; dropped");
    return;
  }
  reply(pending.connection, pending.request, () => {
    const applied = applyTrajRequest(state.world, pending.request, data);
    // §5.3 (Q99): one push is a whole batch, and it replaces whatever the browser held.
    // §7.1 (Q100): the default selection is the first clip, so a push rests there, paused.
    setTrajectory(applied.trajs.map((traj) => ({ traj, name: null })));
    return applied.result;
  });
}

/** §5.1/§5.4: /events carries agent events plus the browser request/response channel. */
function handleGatewayFrame(connection: WebSocket, raw: string): void {
  if (raw === "pong") return;
  let frame: GatewayFrame;
  try {
    frame = JSON.parse(raw) as GatewayFrame;
  } catch {
    console.warn("[ws] non-JSON text frame ignored");
    return;
  }
  if (!frame || typeof frame !== "object" || typeof frame.type !== "string") return;
  if (frame.type === "browser.request") { answerBrowserRequest(connection, frame); return; }
  if (frame.type === "scene.applied") { void adoptWorld(frame); return; }
  // §4 (S4): the one edge the gateway announces when the session changes hands. The frame carries
  // the phase only, so the rest -- bound browser, lease holder, quarantine -- comes
  // from the authoritative route the announcement points at.
  if (frame.type === "session.phase_changed") {
    applyPhase("broadcast", frame.payload.session_phase);
    void refreshSessionState();
    return;
  }
  if (frame.type === "agent.debug") {
    const entry = frame.payload;
    if (enabled("agent.debug") && !state.agentDebug.some((seen) => seen.jobId === entry.jobId && seen.index === entry.index)) {
      state.agentDebug.push(entry); state.agentDebug = state.agentDebug.slice(-500); updateAgentDebug();
    }
  } else if (frame.type === "agent.job_updated") {
    if (frame.payload.conversation_id === state.activeConversationId) updateAgentJob(frame.payload);
  }
}

/** §4.2/§5.9: run one caller request against the live world and answer the gateway. */
function answerBrowserRequest(connection: WebSocket, request: BrowserRequest): void {
  if (request.expects_binary === true) {
    // §5.3: the TRAJ bytes are the next frame, so the answer waits for them.
    pendingBinary = { connection, request };
    return;
  }
  // A capture and a box read the scene graph, not just the world, so they have their
  // own handlers; everything else is a world question that `applySceneRequest` answers.
  reply(connection, request, () => {
    if (request.action === "captureScene") return applyCaptureRequest(state.world, request, captureView);
    if (request.action === "get2DAABB") return applyAabbRequest(state.world, request, captureView);
    return applySceneRequest(state.world, request.action, request.payload ?? {}, playbackState());
  });
}

/** One §5.1 response per request: run the work, answer, and resync the viewport for writes. */
function reply(connection: WebSocket, request: BrowserRequest, work: () => SceneRequestResult | UpdateTrajResult | CaptureSceneResult | Get2DAabbResult): void {
  let frame: BrowserResponse<SceneRequestResult | UpdateTrajResult | CaptureSceneResult | Get2DAabbResult>;
  try {
    const payload = work();
    if (isWriteAction(request.action)) {
      renderer.syncScene(state.world);
      // §7.3/Q76: the write ends playback, the clip is not the scene's owner any more.
      applyWriteCoexistence();
      updateStatus();
      if (request.action === "updateWorld") void refreshSessionState();
    }
    frame = { type: "browser.response", request_id: request.request_id, ok: true, payload };
  } catch (error) {
    const failure = error as WorldError | TrajError;
    frame = {
      type: "browser.response", request_id: request.request_id, ok: false,
      error: { code: typeof failure.code === "string" ? failure.code : "INTERNAL_ERROR", message: failure.message },
    };
    console.warn(`[ws] ${request.action} failed: ${failure.message}`);
  }
  if (connection.readyState === WebSocket.OPEN) connection.send(JSON.stringify(frame));
}

/** §4 (S4): one reading of the session phase, merged by the source it came from. */
function applyPhase(source: PhaseSource, value: unknown): void {
  const merged = mergeSessionPhase(state.sessionPhase, source, value);
  const moved = merged.phase !== state.sessionPhase;
  state.sessionPhase = merged.phase;
  // Only a move repaints: `scene.applied` carries the phase with every write, and a card list that
  // did not change does not need re-tagging.
  if (moved) renderPhaseLock();
  if (merged.reread) void refreshSessionState();
}

/**
 * §4 (S4-c, layer 1): the class-② entries the phase refuses, made visible.
 *
 * Every class-② entry still gates on click (`phaseBlockMessage` -> `showPhaseBlock`), and this is
 * only the look that says so beforehand. Deliberately **not** the `disabled` attribute: the click
 * delegation opens with `if ((target as HTMLButtonElement).disabled) return;`, so a real `disabled`
 * swallows the tap and the modal never gets to answer -- the failure mode R2 was written against.
 * `aria-disabled` carries the state to a reader; the handler keeps running.
 */
function renderPhaseLock(): void {
  const blocked = phaseBlockMessage(state.sessionPhase) !== null;
  app.classList.toggle("agent-owned", blocked);
  document.querySelectorAll<HTMLElement>(CLASS_TWO_ENTRIES).forEach((entry) => {
    if (blocked) entry.setAttribute("aria-disabled", "true");
    else entry.removeAttribute("aria-disabled");
  });
}

/**
 * §4/§5: the authoritative readback -- what a refreshed tab uses to find out where it stands.
 *
 * Q126 (2026-10-27): the reads are serialized on purpose. `createSession` awaits one right after
 * `POST /sessions`, and the `scene.applied` echo of that same click schedules another one a few
 * milliseconds later; with last-scheduled-wins that second bump made the awaited read throw away
 * a correct answer. Chaining instead keeps the discard rule (an answer never overwrites a newer
 * one) while making the ordering the schedule's, so a read that was asked for last is also the
 * last one that lands. No extra request: each read already fetched before it checked the stamp.
 */
async function refreshSessionState(): Promise<void> {
  const previous = sessionRefreshInFlight;
  const work = (async () => {
    if (previous) await previous.catch(() => {});
    return readCurrentSessionState(++sessionRefreshVersion, conversationSelectionGeneration);
  })();
  sessionRefreshInFlight = work;
  try { await work; }
  finally { if (sessionRefreshInFlight === work) sessionRefreshInFlight = null; }
}

async function readCurrentSessionState(version: number, selectionGeneration: number): Promise<void> {
  try {
    // The id travels as the caller saying who is asking; the answer names the bound browser
    // whatever it says, and that is exactly what tells "this phase is mine" from "someone else
    // holds the scene" (§5 -- the route does not trust the claim, it reports against it).
    const view = await json<SessionStateView>(`/api/sessionState?browser_id=${encodeURIComponent(BROWSER_ID)}`);
    if (version !== sessionRefreshVersion || selectionGeneration !== conversationSelectionGeneration) return;
    const previousSessionID = state.session?.sessionID ?? null;
    state.session = view;
    const changed = sessionChanged(previousSessionID, view.sessionID);
    renderLightingFreeze();
    applyPhase("session-state", view.session_phase);
    // A scene switch or Gateway rebind creates a new Brain session without going through the
    // session button.  It must still split the transcript.  On the first read, only create one
    // when there is no hydrated history; otherwise a page refresh would duplicate the current row.
    const hasSession = typeof view.sessionID === "string" && view.sessionID.length > 0;
    const needsConversation = hasSession && !sessionCreationInFlight && (
      (changed && previousSessionID !== null)
      || state.activeConversationId === null
      || (sessionConversationSessionID !== null && sessionConversationSessionID !== view.sessionID)
    );
    sessionStateHydrated = true;
    if (needsConversation) await openConversationForSession(view.sessionID!, view.scene_id, false);
    // Session creation is owned by the Gateway when it adopts a scene, by submitAgent for the
    // empty bootstrap world, or by the explicit New conversation button.  Do not race that
    // authority with a second browser-side POST /sessions during every refresh.
  } catch (error) {
    // A gateway that will not say leaves the belief as it was; layer 2 still refuses the write.
    console.warn(`[session] could not read the session state: ${(error as Error).message}`);
  }
}

function conversationSceneSwitchMessage(): string | null {
  const phaseMessage = phaseBlockMessage(state.sessionPhase);
  if (phaseMessage) return phaseMessage;
  // Durable transcript evidence that this session already received an Agent command. A fresh
  // setup conversation remains free to choose a scene without creating another session first.
  return state.messages.some((message) => message.role === "user")
    ? CONVERSATION_SCENE_SWITCH_MESSAGE : null;
}

let automaticSessionInFlight = false;
async function ensureAutomaticSession(view: SessionStateView): Promise<void> {
  if (automaticSessionInFlight || view.sessionID
      || view.lease !== null || view.quarantine !== null) return;
  // A clean browser starts with the protocol's empty bootstrap world.  Do not create a durable
  // conversation merely by opening or refreshing the page; the first command (or the explicit
  // New conversation button) is the session boundary.
  if (view.scene_id === 0 && view.scene_version === 0 && state.world.objectCount === 0) return;
  automaticSessionInFlight = true;
  sessionCreationInFlight = true;
  try {
    const created = await json<{ sessionID: string }>("/sessions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ browserID: BROWSER_ID }) });
    await refreshSessionState();
    await openConversationForSession(created.sessionID, state.session?.scene_id ?? state.world.sceneId, false);
  } catch (error) {
    console.warn(`[session] automatic session creation deferred: ${(error as Error).message}`);
  } finally {
    sessionCreationInFlight = false;
    automaticSessionInFlight = false;
  }
}

let adoptingWorld = false;
// A notice that arrives while a re-read is in flight waits and then runs. Dropping it used to be the
// lesser evil, but under unconditional cross-scene adoption (流程文档 §1.2 第 4 条) the frame behind
// yours is exactly the one that names the scene the user is looking at now, so the newest one wins
// instead of disappearing (Q147).
let queuedAdoption: SceneApplied | null = null;

function localReportReading() {
  return { session_id: state.session?.sessionID ?? null, report_seq: worldReportSeq };
}

/** §5.4/Q111: a write landed somewhere, so decide from its four-tuple whether to re-read. */
async function adoptWorld(broadcast: SceneApplied): Promise<void> {
  // §4: a write landing is also a live notice of the phase, whether or not we re-read anything.
  applyPhase("broadcast", broadcast.session_phase);
  // Q147: the notice says which session it landed in and which of our reports it answers.
  let verdict = appliedNoticeVerdict(localReportReading(), broadcast);
  if (verdict === "other_session") {
    // This tab's session reading can simply be a round-trip behind -- a session switch announces
    // itself over this socket before `/api/sessionState` has been asked -- so ask the authoritative
    // route once and judge again before throwing a real landing away.
    await refreshSessionState();
    verdict = appliedNoticeVerdict(localReportReading(), broadcast);
    if (verdict === "other_session") return;
  }
  // A late echo of a switch this tab already left. Re-reading it is what rolled the screen back.
  if (verdict === "late_echo") return;
  if (verdict === "confirm") {
    // The gateway adopted exactly what this page reported, so the scene on screen is the scene on
    // disk: no re-read (that is the race), no re-report (that is what pinned both sides to the old
    // scene), only the phase readout this notice also carries.
    void refreshSessionState();
    updateStatus();
    return;
  }
  if (adoptingWorld) { queuedAdoption = broadcast; return; }
  adoptingWorld = true;
  try {
    let notice: SceneApplied | null = broadcast;
    while (notice !== null) {
      await applyAppliedNotice(notice);
      notice = queuedAdoption;
      queuedAdoption = null;
    }
  } catch (error) {
    // The authoritative viewer may have closed mid-flight; the next broadcast retries.
    console.warn(`[ws] could not adopt the applied world: ${(error as Error).message}`);
  } finally {
    adoptingWorld = false;
  }
}

async function applyAppliedNotice(broadcast: SceneApplied): Promise<void> {
  // The frame names the scene, it does not carry one. The four-tuple is the whole test, and it
  // doubles as the de-duplication the old `source_browser_id` did by name: the browser that
  // applied the write already holds the announced version, so it re-reads nothing (§5.2).
  if (broadcast.scene_id === state.world.sceneId && broadcast.scene_version === state.world.version
    && broadcast.scene_sub_version === state.world.subVersion) {
    // §5.8 (S6-b): the four-tuple is already ours, but the report that paired `camera_candidates`
    // to it never happened -- a caller's write is committed by the gateway itself (`record_scene`),
    // and only this side knows which objects carry a `Camera`. Without the list the 新建会话 door is
    // dead, which is the exit §4's modal offers, so re-report it here. `source_request_id` is what
    // keeps this loop-free: it names a caller's write, while the echo of our own `state_sync`
    // carries none (`apply_state_sync` passes None), so a report never triggers another report.
    // Re-reporting a document the gateway already stored reads as `idempotent` (§1.2: the four-tuple
    // matches), so no version moves and `agent_owned` does not turn it into a refused edit.
    if (broadcast.source_request_id !== null) reportWorld();
    // reportWorld starts an early read which can race the state_sync acknowledgement. This read
    // is deliberately after scene.applied, so the session and camera candidates are authoritative.
    void refreshSessionState();
    updateStatus();
    return;
  }
  // One address: whatever the gateway holds as the bound browser. `?browser_id=` would only
  // name a viewer, and a viewer's world is not the session's.
  const read = await fetchJSON<unknown>("/getWorld");
  // §4: the stamp on that answer is a cross-check, not an order -- it contradicts, we go ask
  // `/api/sessionState`, which is the reading that decides (§4 三者不一致).
  applyPhase("world-header", read.headers.get("X-Session-Phase"));
  const world = parseWorld(read.body);
  state.world = world;
  renderer.syncScene(world);
  applyWriteCoexistence();
  updateStatus();
  // §5.8 (S6-b): the same re-report on the other branch -- this tab came to the new 现场 by
  // re-reading it instead of by applying the write, so it is the one holding the document whose
  // cameras nobody has reported yet.
  reportWorld();
  // Close the same state_sync/read race on the re-read branch.
  void refreshSessionState();
}
/** §8/Q87: a job row carries the `jobRow` reading of itself, so the markup stays formatting. */
function updateAgentJob(job: AgentJob): void {
  if (job.conversation_id !== state.activeConversationId) return;
  const row = jobRow(job);
  // A restored journal row knows the task only by `planKey`; matching it here is how the row gets the
  // state and verdict the task ended with instead of staying a bare sentence.
  const item = state.messages.find((message) => message.jobId === job.id || message.planKey === job.id);
  // A task that ends releases its lease, and the lease comes from the authoritative route.
  const ended = item !== undefined && item.terminal === false && job.terminal;
  if (item) {
    item.jobId = job.id;
    item.agentState = job.agent_state;
    item.terminal = job.terminal;
    item.outcome = job.outcome ?? undefined;
    item.row = row;
    // An empty sentence means upstream said nothing; keeping the previous text beats a blank row.
    if (row.text) item.text = row.text;
    item.planKey = job.id;
  } else state.messages.push({ role: "agent", text: row.text, agentState: job.agent_state, terminal: job.terminal,
    outcome: job.outcome ?? undefined, row, jobId: job.id, planKey: job.id, time: Date.now() });
  updateAgent();
  if (ended) {
    // Gateway journals terminal replies before broadcasting, including when this tab is closed.
    void refreshConversationHistory();
    void refreshSessionState();
  }
}
/** §5.15/Q87: one job's row -- the parts come from `jobRow`, and this only formats them. */
function agentMessageMarkup(message: AgentMessage): string {
  const hideDetails = message.role === "agent" && agentDetailsDisabled(state.runtime);
  const row = message.row;
  const hasStream = message.role === "agent" && (message.planKey ?? message.jobId) === state.agentPlanKey && state.agentPlan.length > 0;
  // Two sources for a skill order: the debug stream's steps carry per-step status and win while
  // the task runs; once it has ended, `plan.commands` is what the result itself names (§8).
  const steps = hasStream
    ? `<div class="message-plan">${state.agentPlan.map((item) => `<div class="agent-plan-item ${item.status}"><i data-lucide="${item.status === "completed" ? "check-circle-2" : item.status === "in_progress" ? "loader-circle" : "circle"}"></i><span>${escapeHtml(item.step)}</span></div>`).join("")}</div>`
    : row && row.commands.length
      ? `<div class="message-plan">${row.commands.map((line) => `<div class="agent-plan-item"><span>${escapeHtml(line)}</span></div>`).join("")}</div>`
      : "";
  const facts = row ? [row.identity, row.modelCalls].filter((line): line is string => line !== null) : [];
  const detail = facts.length ? `<p class="message-detail">${facts.map((line) => escapeHtml(line)).join(" · ")}</p>` : "";
  const files = row && row.artifacts.length
    ? `<p class="message-artifacts">${row.artifacts.map((item) => `<a href="${item.url}" download>${escapeHtml(item.name)}</a>`).join("")}</p>`
    : "";
  const text = hideDetails ? message.text.replace(/（[^（）]*；诊断 task-\d+）/g, "") : message.text;
  const body = !hideDetails && hasStream && jobWorking(message) ? "" : `<p>${escapeHtml(text)}</p>`;
  // §7.4: the badge quotes `agent_state` exactly as the service called itself; the colour comes
  // from `terminal` + `outcome`, never from the name (§8).
  const footer = message.agentState !== undefined
    ? `<footer><span class="${jobBadgeClass(message)}">${escapeHtml(message.agentState)}</span>${message.jobId ? `<code>${escapeHtml(message.jobId.slice(0, 8))}</code>` : ""}</footer>`
    : "";
  return `<article class="message ${message.role}"><header><strong>${message.role === "user" ? "You" : "Agent"}</strong><time>${new Date(message.time).toLocaleTimeString()}</time></header>${body}${hideDetails ? "" : steps + detail + files + footer}</article>`;
}
/**
 * §8 (R9): the one source behind the service bar. §5.3 keeps `GET /api/health` answering 200 about
 * itself, so a route that fails outright means *this tab* could not ask -- a missing reading, which
 * the bar shows as 状态未知 rather than borrowing the gateway's word 不可达 for its own silence.
 */
async function refreshAgentHealth(): Promise<void> {
  try {
    const health = await json<GatewayHealthView>("/api/health");
    const transientBrowserBinding = health.agent_service.status === "degraded"
      && (health.agent_service.detail ?? "").includes("browser=not_bound")
      && performance.now() < agentHealthWarmupUntil;
    if (transientBrowserBinding) {
      state.agentService = null;
      if (agentHealthRetryTimer === null) {
        agentHealthRetryTimer = window.setTimeout(() => {
          agentHealthRetryTimer = null;
          void refreshAgentHealth();
        }, 500);
      }
    } else {
      state.agentService = health.agent_service;
    }
  } catch (error) {
    state.agentService = null;
    console.warn(`[agent] could not read the service health: ${(error as Error).message}`);
  }
  renderAgentStatus();
}

/**
 * §5.15/Q87: /events is fire-and-forget, so a browser that missed the broadcasts during a
 * reconnect catches up from the gateway instead of starting its own polling loop.
 */
async function refreshAgentJobs(): Promise<void> {
  const conversationId = state.activeConversationId;
  if (!conversationId) return;
  try {
    const result = await json<{ jobs: AgentJob[] }>(`/api/agent/jobs?conversationId=${encodeURIComponent(conversationId)}`);
    if (state.activeConversationId !== conversationId) return;
    const hadNote = state.jobPollErrors > 0;
    state.jobPollErrors = 0;
    if (!result.jobs.length) { if (hadNote) updateAgent(); return; }
    for (const job of result.jobs) {
      if (state.activeConversationId !== conversationId) return;
      updateAgentJob(job);
    }
  } catch (error) {
    // The count is this tab's own (§5.15 keeps the gateway's counter inside the gateway), and all it
    // buys is one honest note above the rows: the state they show is as of the last read that worked.
    state.jobPollErrors += 1;
    updateAgent();
    console.warn(`[agent] could not catch up on tasks: ${(error as Error).message}`);
  }
}
/** §5.15/§7.1: one round is `{command, scene, clientRequestID}` -- the whole world, because only this browser holds it. */
async function submitAgent(text: string): Promise<void> {
  const value = text.trim();
  if (!value) { showError("Agent command cannot be empty"); return; }
  const selectionAtSend = conversationSelectionGeneration;
  if (conversationTransitionInFlight) {
    try { await conversationTransitionInFlight; }
    catch { return; }
  }
  if (selectionAtSend !== conversationSelectionGeneration) return;
  if (sessionCreationInFlight) { showError("会话正在创建，请稍后发送"); return; }
  // Loading a scene reports it asynchronously. Wait for the Gateway binding and its history row
  // before capturing a command; otherwise the first click can still name the previous session.
  const sceneBeforeRefresh = JSON.stringify(state.world.toJSON());
  await refreshSessionState();
  if (sessionRefreshInFlight) await sessionRefreshInFlight;
  if (sessionConversationInFlight) await sessionConversationInFlight;
  if (selectionAtSend !== conversationSelectionGeneration
      || sceneBeforeRefresh !== JSON.stringify(state.world.toJSON())) return;
  if (!state.session?.sessionID) {
    const opened = await createSession();
    if (!opened.ok) { showError(opened.reason ?? "创建会话失败"); return; }
  }
  const sessionID = state.session?.sessionID;
  const conversationId = state.activeConversationId;
  if (!sessionID || !conversationId || sessionConversationSessionID !== sessionID) {
    showError("当前会话尚未准备好，请稍后发送"); return;
  }
  const scene = state.world.toJSON();
  const captured: AgentSubmissionContext = { sessionID, conversationId, sceneJSON: JSON.stringify(scene) };
  state.agentPlan = [];
  state.agentPlanKey = null;
  const clientRequestID = generateBrowserID(crypto);
  state.messages.push({ role: "user", text: value, time: Date.now() });
  updateAgent();
  await appendConversationMessage("user", value, clientRequestID, conversationId);
  const current: AgentSubmissionContext = {
    sessionID: state.session?.sessionID ?? "",
    conversationId: state.activeConversationId ?? "",
    sceneJSON: JSON.stringify(state.world.toJSON()),
  };
  if (!submissionContextMatches(captured, current)) return;
  const attempt = submissionForSend(state.agentPending, value, () => clientRequestID);
  state.agentPending = attempt;
  const body: AgentTaskSubmission = { command: value, scene, clientRequestID: attempt.clientRequestID,
    conversationId, sessionID };
  try {
    const job = await json<AgentJob>("/api/agent/jobs", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (state.activeConversationId !== conversationId || state.session?.sessionID !== sessionID) return;
    state.agentPending = submissionAfterSend(state.agentPending, null);
    updateAgentJob(job);
  } catch (error) {
    if (state.activeConversationId !== conversationId || state.session?.sessionID !== sessionID) return;
    const failure = error as Error & { code?: string };
    state.agentPending = submissionAfterSend(state.agentPending, failure);
    state.messages.push({ role: "agent", text: failure.message, terminal: true, outcome: "failed", time: Date.now() });
    void appendConversationMessage("assistant", failure.message, clientRequestID + ":failure", conversationId);
    updateAgent();
  }
}

const speechInput = new SpeechInput(
  document.querySelector<HTMLTextAreaElement>("#agent-input")!,
  document.querySelector<HTMLButtonElement>('[data-action="speech"]')!,
  showError, () => showAgentToast("正在聆听…"),
);
function toggleSpeech(): void {
  if (!enabled("agent.input.voiceInput") || speechSubmissionPending) return;
  void speechInput.toggle();
  document.querySelector<HTMLTextAreaElement>("#agent-input")!.focus();
}

app.addEventListener("click", async (event) => {
  const target = (event.target as HTMLElement).closest<HTMLElement>("button, [data-entity], [data-seek-frame]"); if (!target) return;
  if ((target as HTMLButtonElement).disabled) return;
  const featureElement = target.closest<HTMLElement>("[data-feature]");
  if (featureElement && !enabled(featureElement.dataset.feature!)) return;
  if (target.dataset.panel) {
    const panel = target.dataset.panel as Panel;
    if (!enabled(panelFeatures[panel])) return;
    // §7.2 (Q156): the three asset buttons share `data-panel="assets"`, so the 大类 is part of the tab.
    const pressed = panelTabPress({ panel: state.panel, open: state.panelOpen, kind: state.library.kind }, panel, target.dataset.assetKindNav);
    const switched = pressed.open && pressed.kind !== state.library.kind;
    state.panelOpen = pressed.open;
    state.panel = panel;
    state.library.kind = pressed.kind as LibraryKind;
    if (switched) state.library.category = null;
    renderContext();
    return;
  }
  // §7.3 (Q90): a mark is a manual read of the clip, so it stops the clock like the step buttons do.
  if (target.dataset.seekFrame !== undefined) { pausePlayback(); showFrame(Number(target.dataset.seekFrame)); return; }
  if (target.dataset.timeline) { playbackControl(target.dataset.timeline); return; }
  if (target.dataset.preview) { const mode = target.dataset.preview as "rgb" | "depth"; if (!enabled(`viewport.cameraPreview.modes.${mode}`)) return; state.previewMode = mode; document.querySelectorAll("[data-preview]").forEach((item) => item.classList.toggle("active", item === target)); loadPreview(); return; }
  // §4.4 (Q139): the wall's mode pair is the preview pair's sibling, on its own attribute because the
  // `[data-preview]` selector above is global and would light up the wrong buttons. RGB needs no gate -
  // `viewport.cameraWall` is the only owner of the wall's RGB (Q138) - and 深度 is already refused by the
  // `data-feature` guard a few lines up, so one key still decides one capability.
  if (target.dataset.cameraWallMode) { chooseCameraWallMode(target.dataset.cameraWallMode as SensorViewMode); return; }
  if (target.dataset.entity && enabled("panels.scene.selectEntity")) { selectEntity(Number(target.dataset.entity)); return; }
  // §4.6 (Q156): the 大类 lives on the rail now, so only the category rail is left in here.
  if (target.dataset.assetCategory) { state.library.category = target.dataset.assetCategory; renderContext(); return; }
  const assetKind = target.dataset.assetKind as LibraryKind | undefined;
  if (assetKind) { await chooseLibraryItem(assetKind, Number(target.dataset.assetId)); return; }
  if (target.dataset.deleteConversation) { event.stopPropagation(); await deleteConversation(target.dataset.deleteConversation); return; }
  if (target.dataset.conversationId) { await activateConversation(target.dataset.conversationId); return; }
  const action = target.dataset.action;
  // §1.3: Load Scene is the other class-② entry, and it gates the same way.
  if (action === "load-scene") {
    const blocked = conversationSceneSwitchMessage();
    if (blocked) showPhaseBlock(blocked, { kind: "load-scene" });
    else document.querySelector<HTMLInputElement>("#scene-file")!.click();
  }
  else if (action === "phase-new-session") await acceptPhaseBlock();
  else if (action === "dismiss-phase-block") dismissPhaseBlock();
  // Q123: the strip's 新建会话／核验并恢复两枚按钮随 `#session-strip` 一起删除。同一道 `POST /sessions`
  // 门还剩两个入口——上面那次弹窗的 `phase-new-session` 与面板头的 `new-conversation`；
  // `POST /api/agent/recover` 从此只有调用方／运维直接调，浏览器侧不再替用户按下去。
  else if (action === "load-traj") document.querySelector<HTMLInputElement>("#traj-file")!.click();
  else if (action === "focus") renderer.frame(state.selectedId ?? undefined);
  else if (action === "frame") renderer.frame();
  else if (action === "export-scene") exportScene();
  else if (action === "snapshot") void copyViewportSnapshot();
  else if (action === "record") toggleRecording();
  // §7.1 (Q138): the wall's close is the same switch as the checkbox, reached from the other end - it
  // clears `state.cameraWall`, and `renderCameraWall()` puts that back on the toolbar box.
  else if (action === "close-camera-wall") toggleCameraWall(false);
  else if (action === "toggle-diagnostics") document.querySelector("#diagnostics")!.classList.toggle("open");
  else if (action === "new-conversation") await createConversation();
  else if (action === "toggle-history") await toggleConversationHistory();
  else if (action === "speech") toggleSpeech();
});
let speechSubmissionPending = false;
app.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  if (form.id !== "agent-form" || !enabled("agent.input.send") || speechSubmissionPending) return;
  speechSubmissionPending = true;
  const input = document.querySelector<HTMLTextAreaElement>("#agent-input")!;
  input.readOnly = true;
  try {
    if (!await speechInput.stop()) return;
    void submitAgent(input.value);
    input.value = "";
  } finally {
    input.readOnly = false;
    speechSubmissionPending = false;
  }
});
// §7.1 (Q139): the wall's header is its handle. A pointer listener, not a mouse one, so a pen or a touch
// drags it the same way - and these events never reach the canvas, because the panel floats above it.
app.addEventListener("pointerdown", dragCameraWall);
// §7.6 (Q129): Enter in the Agent box *is* the send button, so it dispatches the same submit event
// instead of carrying a second copy of that path. Not answering means leaving the event alone --
// Shift+Enter, an IME candidate, a held key and a hidden button all keep the browser's newline.
app.addEventListener("keydown", (event) => {
  const box = event.target as HTMLElement;
  const voiceButton = box.closest<HTMLElement>('[data-action="speech"]');
  if ((box.id !== "agent-input" && !voiceButton) || !enterSendsAgentMessage(state.features, event)) return;
  event.preventDefault();
  box.closest("form")!.requestSubmit();
});
app.addEventListener("change", (event) => { const target = event.target as HTMLInputElement | HTMLSelectElement; if (target.id === "timeline-rate") { setPlaybackRate(Number(target.value)); } else if (target.id === "traj-select") { // §7.1 (Q100): picking another clip is the same path a new batch takes -- no request, frame 0.
  setTrajectory(state.trajs, Number(target.value)); } else if (target.id === "waypoint-all-trajs") { // §7.1 (Q101): a view state -- the layers already exist, only which show changes.
  state.waypoints.showAll = (target as HTMLInputElement).checked; renderWaypoints(); } else if (target.id === "shadows-toggle") { // §4.8.5 (Q117): view state too, but this one answers to a lease.
  toggleShadows((target as HTMLInputElement).checked); } else if (target.id === "bbox-toggle") { // §4.8.6 (Q119): view state, one switch, and no lease behind it.
  toggleBoxes((target as HTMLInputElement).checked); } else if (target.id === "local-axes-toggle") { // §4.8.8 (Q132): view state again; it only ever answers for the screen.
  toggleLocalAxes((target as HTMLInputElement).checked); } else if (target.id === "camera-wall-toggle") { // §7.1 (Q138): view state again, and it too only ever answers for the screen.
  toggleCameraWall((target as HTMLInputElement).checked); } else if (target.id === "record-overlays") { // §4.8.7 (Q121): view state; it changes what the next - or the running - take is made of.
  toggleRecordOverlays((target as HTMLInputElement).checked); } else if (target.id === "lighting-preset") { chooseLightingPreset(target.value); } else if (target.id === "scene-file") { const file = (target as HTMLInputElement).files?.[0]; if (file) void loadSceneFile(file); target.value = ""; } else if (target.id === "traj-file") { const files = Array.from((target as HTMLInputElement).files ?? []); if (files.length) void loadTrajFiles(files); target.value = ""; } });
// The drag value is read before pausing, because pausing repaints the bar from the old playhead.
app.addEventListener("input", (event) => { const target = event.target as HTMLInputElement; if (target.id === "timeline-range") { const index = Number(target.value); pausePlayback(); showFrame(index); } if (target.id === "tree-search") document.querySelectorAll<HTMLElement>(".tree-row").forEach((row) => { row.hidden = !row.textContent!.toLowerCase().includes(target.value.toLowerCase()); }); });
// A thumbnail that 403/404s or is blocked leaves the card clickable (§4.6); the frame behind it shows through.
app.addEventListener("error", (event) => { const image = event.target; if (image instanceof HTMLImageElement && image.classList.contains("asset-thumb")) image.classList.add("missing"); }, true);

/** §11.1: a world file larger than this is refused before it reaches the parser. */
const MAX_WORLD_BYTES = 100 * 1024 * 1024;

/** §4.1: both ways of getting a world - the local file (§4.1a) and a 预设场景 (§4.6) - end here. */
function applyWorld(world: World): void {
  state.world = world;
  // §7.3: a new world replaces the objects a clip names, so playback ends with it.
  setTrajectory(null);
  // §4.8.1: a fresh world brings its own lighting rig, so the preset menu says so too -
  // and the renderer drops the previous scene's override with it (Q114 ③).
  document.querySelector<HTMLSelectElement>("#lighting-preset")!.value = "";
  renderer.setLightingOverride(null);
  // §5.8: report it so the gateway caches this world and the other viewers adopt it.
  reportWorld();
  updateStatus();
  // §7.1 (Q138): a new world is a new set of cameras, and this is the one funnel every way of
  // getting a world ends in - the wall's cells are the only thing here that depends on it.
  renderCameraWall();
}

/** §4.1a: parse locally, render, then report the active world so the gateway can cache it (§5.8). */
async function loadSceneFile(file: File): Promise<void> {
  if (file.size > MAX_WORLD_BYTES) { showError(`加载场景失败：${file.name} 超过 100 MB 上限`); return; }
  let world: World;
  try {
    world = parseWorld(JSON.parse(await file.text()));
  } catch (error) {
    showError(`加载场景失败：${(error as Error).message}`);
    return;
  }
  applyWorld(world);
}

/** §4.1a: a TRAJ file is the same clip a caller pushes through /updateTraj (§5.12), so the
 *  manual path reuses the browser-side decoder; the local world is only the lookup table.
 *  §4.3 (Q100): it joins the batch that is already held rather than replacing it, and
 *  becomes the active clip.
 *  §4.3 (Q104): the picker is `multiple`, so one selection may be N files. They are handed
 *  to the decoder as one batch -- concatenated bytes plus `byte_lengths` -- which is what
 *  makes §5.3's all-or-nothing free instead of a second code path; one file is the N=1
 *  case and behaves exactly as it did before. The active clip after an append is the
 *  first file of that batch, the same rule a caller's push follows.
 */
async function loadTrajFiles(files: File[]): Promise<void> {
  const oversized = files.find((file) => file.size > MAX_WORLD_BYTES);
  if (oversized) { showError(`加载轨迹失败：${oversized.name} 超过 100 MB 上限`); return; }
  const buffers = await Promise.all(files.map((file) => file.arrayBuffer()));
  const byteLengths = buffers.map((buffer) => buffer.byteLength);
  const payload = new Uint8Array(byteLengths.reduce((total, size) => total + size, 0));
  let offset = 0;
  for (const buffer of buffers) {
    payload.set(new Uint8Array(buffer), offset);
    offset += buffer.byteLength;
  }
  const request: BrowserRequest = {
    type: "browser.request",
    request_id: generateBrowserID(crypto),
    action: "updateTraj",
    // §5.3: N hand-picked files are a batch of N, described the same way the gateway
    // describes a multipart push, so the decoder sees one shape.
    payload: { byte_length: payload.byteLength, count: files.length, byte_lengths: byteLengths },
    expects_binary: true,
  };
  try {
    const applied = applyTrajRequest(state.world, request, payload.buffer);
    // §7.3: the batch rests at frame 0, paused, exactly like the caller's push.
    const held = state.trajs.length;
    const appended = files.map((file, index) => ({ traj: applied.trajs[index]!, name: file.name }));
    setTrajectory(state.trajs.concat(appended), held);
    const ignored = applied.result.trajs.reduce((total, summary) => total + summary.ignored_objects.length, 0);
    if (ignored) showError(`加载轨迹：${ignored} 个对象不在当前世界中，已忽略`);
  } catch (error) {
    showError(`加载轨迹失败：${trajFileMessage(error as Error, files)}`);
  }
}

/** The decoder names a failed batch member by index (§5.12); the user picked files, so
 *  say which one. A framing error names no index and keeps its own wording.
 */
function trajFileMessage(error: Error, files: File[]): string {
  const match = /^trajs\[(\d+)\]: (.*)$/s.exec(error.message);
  if (!match) return error.message;
  const index = Number(match[1]);
  return `${files[index]?.name ?? `第 ${index + 1} 个文件`}：${match[2]}`;
}

let worldReportRetryTimer: number | null = null;
let worldReportRetryCount = 0;
// Q147: the number this page puts on every report so the gateway can echo it back as
// `scene.applied.ack_report_seq`. Page lifetime on purpose -- persisting it would hand a refreshed
// tab a number the gateway never saw and read its own first echo as somebody else's write.
let worldReportSeq = 0;
function reportWorld(): void {
  if (socket?.readyState !== WebSocket.OPEN) {
    // A new conversation can replace the local world while the WebSocket is reconnecting.  Do
    // not lose that reset: retry a bounded number of times and let the authoritative Gateway
    // scene.applied frame drive the final readback.
    if (worldReportRetryTimer === null && worldReportRetryCount < 30) {
      worldReportRetryCount += 1;
      worldReportRetryTimer = window.setTimeout(() => {
        worldReportRetryTimer = null;
        reportWorld();
      }, 100);
    }
    return;
  }
  worldReportRetryCount = 0;
  worldReportSeq += 1;
  // §9 item 3: the camera candidates travel beside the document because only this side knows which
  // objects carry a `Camera` component; the gateway never looks inside a world, so an empty list
  // here is a browser that has not reported one, not a scene with no cameras.
  socket.send(JSON.stringify({
    type: "browser.state_sync",
    payload: { world: state.world.toJSON(), camera_object_ids: state.world.cameraObjectIds(), report_seq: worldReportSeq },
  }));
  // §9.1: a digest only exists once the gateway has computed it for the document it just adopted,
  // so re-read the session view here -- a capture has to pair one with what it renders, and a local
  // edit leaves the previous pair describing the wrong four-tuple.
  void refreshSessionState();
}

function worldStatus(): string {
  const { scene_id, scene_name, scene_version } = state.world.meta;
  return `#${scene_id}${scene_name ? ` ${scene_name}` : ""} · v${scene_version} · ${state.world.objectCount} objects`;
}
window.addEventListener("beforeunload", () => renderer.dispose());
async function loadAuthoritativeWorld(): Promise<void> {
  // A refresh must render the Gateway's current document before the browser reports anything.
  // Starting from the in-memory empty World and immediately state_sync-ing it would otherwise
  // overwrite the last committed scene and make a durable conversation appear to have lost its
  // context.  The Gateway is the authority for this browser after a restart as well as during a
  // live session, so a failed read is surfaced but never replaced with a guessed scene.
  try {
    const read = await fetchJSON<unknown>("/getWorld");
    const world = parseWorld(read.body);
    state.world = world;
    renderer.syncScene(world);
    updateStatus();
  } catch (error) {
    console.warn(`[startup] could not read authoritative world: ${(error as Error).message}`);
  }
}
async function start(): Promise<void> { drawIcons(); renderer.syncScene(state.world); try { const config = await json<RuntimeConfig>("/api/runtimeConfig"); const client = createLibraryClient(config); state.client = client; renderer.setLibrary(client); state.runtime = config; renderer.setBackgroundColor(viewportBackground(config)); state.features = config.features; applyFeatureVisibility(); await loadAuthoritativeWorld(); void refreshLibrary(); const active = (await json<{ activeConversationId: string | null }>("/api/agent/conversations")).activeConversationId; if (active) { const detail = await json<ConversationDetail>(`/api/agent/conversations/${encodeURIComponent(active)}`); applyConversation(detail); } else { state.messages = []; state.agentDebug = []; state.agentPlan = []; state.agentPlanKey = null; updateAgent(); updateAgentDebug(); } connectWebSocket(); void refreshSessionState(); void refreshAgentHealth(); // §8 (R9) + §12.18⑤：启动即查、此后每 15 秒一次（内置值，不做成常量）；第二个 tab 跑的是同一段代码，所以它同样轮询，没有新增 WS 帧。
window.setInterval(() => { void refreshAgentHealth(); // §2.3: `agent_owned` is also when a lease can appear without any frame saying so (the Agent takes it mid-task), so the authoritative route is polled while that phase holds -- the freeze and the session door read from it.
if (state.sessionPhase === "agent_owned") void refreshSessionState(); }, 15000); } catch (error) { console.error("WebEditor startup failed", error); state.websocket = "offline"; updateStatus(); showError(`Web Gateway unavailable: ${(error as Error).message}`); setTimeout(start, 2000); } }
void start();
