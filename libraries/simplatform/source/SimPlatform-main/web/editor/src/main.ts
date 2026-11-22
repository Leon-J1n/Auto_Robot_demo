import "./styles.css";
import { Euler, MathUtils, Quaternion } from "three";
import {
  Activity, ArchiveRestore, Bot, Box, Boxes, Camera, ChevronDown, ChevronRight, ChevronUp,
  Crosshair, Eye, FileUp, Focus, Image as ImageIcon, Info, Layers3, Lightbulb, ListTree, Mic,
  MousePointer2, Move3d, Pause, Play, Plus, Redo2, RefreshCw, Rotate3d, Save, Scaling, SkipBack, SkipForward,
  Search, Send, Settings2, SlidersHorizontal, StepForward, Trash2, Undo2, Upload, X, History,
  createIcons,
} from "lucide";
import type { AssetSummary, EntityView, FeatureFlags, SceneEvent, SceneSnapshot, TimelineManifest, Vec3 } from "../../contracts/types";
import { agentPlanItemsFromEvent, decideSnapshot, featureEnabled, mergeSpeech, resolveAssetDependency, timelineDelayMs, timelineDueIndex, validateCamera, validateScale, validateSpotCone, type AgentPlanItem } from "./logic";
import { ThreeRendererAdapter, type Tool } from "./renderer";
import { TimelineLoader } from "./timeline";

type Panel = "scene" | "details" | "settings" | "assets";
type AgentMessage = { role: "user" | "agent"; text: string; status?: string; jobId?: string; planKey?: string; time: number };
type AgentDebugEntry = { jobId: string; index: number; event: Record<string, unknown> };
type AgentJob = { id: string; message: string; status: string; debugEvents?: Record<string, unknown>[] };
type ConversationSummary = {
  conversationId: string; title: string; updatedAt: string;
};
type ConversationDetail = {
  conversationId: string;
  messages: Array<{ role: "user" | "assistant"; content: string; turnId: string; createdAt: string }>;
  snapshot?: SceneSnapshot;
  debugEvents?: Array<{ eventId?: number; turnId?: string; payload?: Record<string, unknown> }>;
  presentation?: { mode: "real-sync" | "sync-and-animate"; snapshot: SceneSnapshot; timeline: TimelineManifest | null } | null;
};

const state = {
  snapshot: null as SceneSnapshot | null,
  assets: [] as AssetSummary[],
  selectedId: 0,
  panel: "scene" as Panel,
  panelOpen: false,
  tool: "select" as Tool,
  collider: false,
  ifps: false,
  websocket: "connecting" as "connecting" | "connected" | "reconnecting" | "offline",
  busy: false,
  logs: [] as Array<Record<string, unknown>>,
  messages: [] as AgentMessage[],
  agentDebug: [] as AgentDebugEntry[],
  agentPlan: [] as AgentPlanItem[],
  agentPlanKey: null as string | null,
  conversationHistory: [] as ConversationSummary[],
  activeConversationId: null as string | null,
  historyOpen: false,
  previewMode: "rgb" as "rgb" | "depth",
  renderFps: 0,
  renderMs: 0,
  features: {} as FeatureFlags,
  presentationMode: "real-sync" as "real-sync" | "sync-and-animate",
  timeline: null as TimelineManifest | null,
  timelineIndex: 0,
  timelinePlaying: false,
  timelineRate: 1,
};
let timelineTimer = 0;
let timelineAnchorWallMs = 0;
let timelineAnchorSimulationTime = 0;
let timelineLoader: TimelineLoader | null = null;
let timelineGeneration = 0;
let timelineSeek = 0;
let timelinePlayRequest = 0;
let agentToastTimer = 0;

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
<div class="workbench">
  <header class="command-bar" data-feature="commandBar">
    <div class="brand" data-feature="chrome.brand"><span>RS</span><div><strong>WebEditor</strong><small>Scene / main</small></div></div>
    <button class="button" data-action="upload" data-feature="commandBar.assetUpload"><i data-lucide="file-up"></i><b>Load Asset</b></button>
    <button class="button" data-action="load-simstate" title="Load a .simstate file saved from sim_editor"><i data-lucide="archive-restore"></i><b>Load Simstate</b></button>
    <select id="create-kind" class="select" aria-label="Create entity" data-feature="commandBar.create"><option value="">Add...</option><option value="cube">Cube</option><option value="sphere">Sphere</option><option value="plane">Plane</option><option value="capsule">Capsule</option><option value="cylinder">Cylinder</option><option value="camera-rgb">RGB Camera</option><option value="camera-rgbd">RGBD Camera</option><option value="light-point">Point Light</option><option value="light-spot">Spotlight</option><option value="light-directional">Directional Light</option><option value="light-ambient">环境光</option></select>
    <div class="sim-buttons" data-feature="commandBar.history"><button data-command="editor.undo" data-feature="commandBar.history.undo" title="Undo"><i data-lucide="undo-2"></i></button><button data-command="editor.redo" data-feature="commandBar.history.redo" title="Redo"><i data-lucide="redo-2"></i></button></div>
    <div class="sim-buttons" data-feature="commandBar.simulation"><button data-command="simulation.start" data-feature="commandBar.simulation.start" title="Start"><i data-lucide="play"></i></button><button data-command="simulation.pause" data-feature="commandBar.simulation.pause" title="Pause"><i data-lucide="pause"></i></button><button data-command="simulation.step" data-feature="commandBar.simulation.step" title="Step"><i data-lucide="step-forward"></i></button><button data-command="simulation.reset" data-feature="commandBar.simulation.reset" title="Reset"><i data-lucide="refresh-cw"></i></button></div>
    <div class="sim-buttons" data-feature="commandBar.state"><button data-command="state.save" data-feature="commandBar.state.save" title="Save State"><i data-lucide="save"></i></button><button data-command="state.restore" data-feature="commandBar.state.restore" title="Restore State"><i data-lucide="archive-restore"></i></button></div>
    <label class="rtf" data-feature="commandBar.simulation.realTimeFactor" title="Real-Time Factor: simulated time divided by elapsed wall-clock time. 1.0 is real time; 0.5 is half speed; 2.0 is double speed.">RTF <i data-lucide="info"></i><input id="rtf" type="number" min="0.01" max="10" step="any" value="1" aria-label="Real-Time Factor"></label>
    <div class="connection" data-feature="commandBar.connection"><i id="connection-dot"></i><span id="connection-label">connecting</span></div>
  </header>
  <main class="workspace">
    <nav class="rail" data-feature="navigation"><button data-panel="scene" data-feature="navigation.scene" data-requires-feature="panels.scene" aria-controls="context-panel" aria-expanded="false"><i data-lucide="list-tree"></i><span>Scene</span></button><button data-panel="details" data-feature="navigation.details" data-requires-feature="panels.details" aria-controls="context-panel" aria-expanded="false"><i data-lucide="sliders-horizontal"></i><span>Details</span></button><button data-panel="settings" data-feature="navigation.settings" data-requires-feature="panels.settings" aria-controls="context-panel" aria-expanded="false"><i data-lucide="settings-2"></i><span>Settings</span></button><button data-panel="assets" data-feature="navigation.assets" data-requires-feature="panels.assets" aria-controls="context-panel" aria-expanded="false"><i data-lucide="boxes"></i><span>Assets</span></button></nav>
    <aside id="context-panel" class="context-panel collapsed" data-feature="panels.context" aria-hidden="true"><header class="panel-heading" data-feature="panels.heading"><div><small id="panel-eyebrow">SCENE</small><h2 id="panel-title">Hierarchy</h2></div><span id="panel-count" data-feature="panels.heading.count">0</span></header><div id="context-content" class="context-content"></div></aside>
    <section class="viewport-shell">
      <div class="viewport-toolbar" data-feature="viewport.toolbar"><div class="segmented" data-feature="viewport.tools"><button class="active" data-tool="select" data-feature="viewport.tools.select" title="Select"><i data-lucide="mouse-pointer-2"></i></button><button data-tool="move" data-feature="viewport.tools.move" title="Move"><i data-lucide="move-3d"></i></button><button data-tool="rotate" data-feature="viewport.tools.rotate" title="Rotate"><i data-lucide="rotate-3d"></i></button><button data-tool="scale" data-feature="viewport.tools.scale" title="Scale"><i data-lucide="scaling"></i></button></div><i class="separator"></i><button data-action="focus" data-feature="viewport.focusSelection" title="Focus Selection"><i data-lucide="focus"></i></button><button data-action="frame" data-feature="viewport.frameScene" title="Frame Scene"><i data-lucide="crosshair"></i></button><label data-feature="viewport.overlays.collider"><input id="collider-toggle" type="checkbox">Collider</label><label data-feature="viewport.overlays.ifps"><input id="ifps-toggle" type="checkbox">IFPS</label><div class="sync-mode segmented" aria-label="Scene synchronization mode"><button class="active" data-mode="real-sync">Live</button><button data-mode="sync-and-animate">Review</button></div><span id="selection-label" data-feature="viewport.selectionLabel">World</span></div>
      <div class="canvas-wrap" id="drop-zone"><canvas id="viewport" data-feature="viewport.canvas"></canvas><div class="viewport-badge" data-feature="viewport.rendererBadge"><i data-lucide="box"></i>Three.js WebGL</div>
        <section id="camera-preview" class="camera-preview hidden" data-feature="viewport.cameraPreview"><header data-feature="viewport.cameraPreview.header"><div><small>SENSOR VIEW</small><strong id="camera-name">Camera</strong></div><button data-action="close-preview" data-feature="viewport.cameraPreview.close"><i data-lucide="x"></i></button></header><div class="preview-tabs" data-feature="viewport.cameraPreview.modes"><button class="active" data-preview="rgb" data-feature="viewport.cameraPreview.modes.rgb">RGB</button><button data-preview="depth" data-feature="viewport.cameraPreview.modes.depth">Depth</button></div><div class="preview-frame"><img id="camera-image" data-feature="viewport.cameraPreview.image" alt="Live camera frame"><span id="preview-status" data-feature="viewport.cameraPreview.status">no_data</span></div><footer id="preview-meta" data-feature="viewport.cameraPreview.metadata">No sensor selected</footer></section>
        <div id="toast" class="toast hidden" data-feature="viewport.notifications"></div>
      </div>
      <div id="timeline" class="timeline hidden"><button data-timeline="first" title="First frame"><i data-lucide="skip-back"></i></button><button data-timeline="play" title="Play"><i data-lucide="play"></i></button><input id="timeline-range" type="range" min="0" max="0" value="0" aria-label="Timeline position"><span id="timeline-time">0.00 / 0.00 s</span><select id="timeline-rate" aria-label="Playback speed"><option value="0.25">0.25x</option><option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option><option value="4">4x</option></select><button data-timeline="last" title="Last frame"><i data-lucide="skip-forward"></i></button></div>
      <div class="diagnostics" id="diagnostics" data-feature="diagnostics"><button data-action="toggle-diagnostics" data-feature="diagnostics.toggle"><span>Log & Diagnostics</span><i data-lucide="chevron-up"></i></button><div id="diagnostics-content" data-feature="diagnostics.entries"></div></div>
    </section>
    <aside class="agent-panel" data-feature="agent"><header class="agent-header" data-feature="agent.header"><div><small>ASSISTANT</small><h2>Scene Agent</h2></div><div class="agent-header-actions"><button type="button" data-action="new-conversation" data-feature="agent.history" title="New conversation" aria-label="New conversation"><i data-lucide="plus"></i></button><button type="button" data-action="toggle-history" data-feature="agent.history" title="Conversation history" aria-label="Conversation history"><i data-lucide="history"></i></button><span id="agent-state" data-feature="agent.status">Offline</span></div></header><div id="agent-toast" class="agent-toast hidden" role="status"></div><section id="agent-history" class="agent-history hidden" data-feature="agent.history.list"><header><strong>Conversation History</strong><button type="button" data-action="toggle-history" title="Close history" aria-label="Close history"><i data-lucide="x"></i></button></header><div id="agent-history-list"></div></section><div id="agent-messages" class="agent-messages" data-feature="agent.messages"></div><section class="agent-debug" data-feature="agent.debug"><header><strong>Agent Debug</strong><span id="agent-debug-count">0</span></header><div id="agent-debug-events"></div></section><button class="operations" data-action="toggle-operations" data-feature="agent.operations"><i data-lucide="activity"></i>Agent Operations <span id="operation-count">0</span></button><div id="operations-content" class="operations-content" data-feature="agent.operations"></div><form id="agent-form" data-feature="agent.input"><textarea id="agent-input" data-feature="agent.input.text" rows="3" placeholder="Describe a scene change..."></textarea><div><span id="speech-status" data-feature="agent.input.speechStatus">Text</span><p><button type="button" data-action="speech" data-feature="agent.input.voiceInput" title="Voice input"><i data-lucide="mic"></i></button><button type="submit" class="send" data-feature="agent.input.send" title="Send"><i data-lucide="send"></i></button></p></div></form></aside>
  </main>
  <footer class="status-bar" data-feature="statusBar"><span id="status-primary" data-feature="statusBar.primary">Connecting to Web Gateway...</span><span id="status-performance" data-feature="statusBar.performance">-- FPS</span><span id="status-backends" data-feature="statusBar.backends">Physics -- / Collision --</span><span id="status-revision" data-feature="statusBar.revision">Rev --</span></footer>
  <input id="asset-upload" type="file" accept=".urdf,.obj,.dae,.fbx,.stl" multiple hidden>
  <input id="simstate-upload" type="file" accept=".simstate" hidden>
  <input id="asset-dependencies" type="file" webkitdirectory multiple hidden>
  <div id="dependency-modal" class="modal-backdrop hidden" data-feature="assetDependencies" role="dialog" aria-modal="true" aria-labelledby="dependency-title">
    <section class="dependency-dialog">
      <header data-feature="assetDependencies.heading"><div><small>ASSET DEPENDENCIES</small><h2 id="dependency-title">发现了模型依赖项</h2></div><button id="dependency-close" data-feature="assetDependencies.close" type="button" title="取消"><i data-lucide="x"></i></button></header>
      <p id="dependency-message" data-feature="assetDependencies.message"></p>
      <ul id="dependency-list" data-feature="assetDependencies.list"></ul>
      <p class="dependency-question" data-feature="assetDependencies.question">将会上传全部依赖项，是否继续？</p>
      <footer><button id="dependency-cancel" class="button" data-feature="assetDependencies.cancel" type="button">否</button><button id="dependency-select" class="button primary" data-feature="assetDependencies.selectFolder" type="button" hidden>选择依赖目录</button><button id="dependency-confirm" class="button primary" data-feature="assetDependencies.confirm" type="button">是，继续上传</button></footer>
    </section>
  </div>
</div>`;

const canvas = document.querySelector<HTMLCanvasElement>("#viewport")!;
const renderer = new ThreeRendererAdapter(canvas, {
  onSelect: (id) => selectEntity(id ?? 0),
  onTransform: (entity, transform) => void sendCommand("scene.update_entity", { entity, transform }),
  onPerformance: (fps, renderMs) => { state.renderFps = fps; state.renderMs = renderMs; updateStatus(); },
  onError: showError,
});
new ResizeObserver(() => renderer.resize()).observe(canvas.parentElement!);

const usedIcons = {
  Activity, ArchiveRestore, Bot, Box, Boxes, Camera, ChevronDown, ChevronRight, ChevronUp,
  Crosshair, Eye, FileUp, Focus, Image: ImageIcon, Info, Layers3, ListTree, Mic,
  MousePointer2, Move3d, Pause, Play, Redo2, RefreshCw, Rotate3d, Save, Scaling,
  Search, Send, Settings2, SlidersHorizontal, SkipBack, SkipForward, StepForward, Trash2, Undo2, Upload, X,
  Lightbulb,
  History, Plus,
};

function drawIcons(): void { createIcons({ icons: usedIcons }); }
function escapeHtml(value: unknown): string { return String(value ?? "").replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[c]!); }
function selectedEntity(): EntityView | undefined { return state.snapshot?.entities.find((entity) => entity.id === state.selectedId); }
function rigidBodyOwner(entity: EntityView): EntityView | undefined {
  let current: EntityView | undefined = entity;
  while (current) {
    if (current.rigidBody) return current;
    current = current.parent === null ? undefined : state.snapshot?.entities.find((item) => item.id === current!.parent);
  }
  return undefined;
}
function enabled(path: string): boolean { return featureEnabled(state.features, path); }
function when(path: string, content: string): string { return enabled(path) ? content : ""; }

const panelFeatures: Record<Panel, string> = {
  scene: "panels.scene", details: "panels.details",
  settings: "panels.settings", assets: "panels.assets",
};

function ensureEnabledPanel(): void {
  if (enabled(panelFeatures[state.panel])) return;
  state.panel = (["scene", "details", "settings", "assets"] as Panel[])
    .find((panel) => enabled(panelFeatures[panel])) ?? "scene";
}

function applyFeatureVisibility(): void {
  ensureEnabledPanel();
  document.querySelectorAll<HTMLElement>("[data-feature]").forEach((element) => {
    element.hidden = !enabled(element.dataset.feature!)
      || Boolean(element.dataset.requiresFeature && !enabled(element.dataset.requiresFeature));
  });
  document.querySelector<HTMLElement>(".sync-mode")!.hidden =
    !enabled("presentation.syncAndAnimate");
  document.querySelector<HTMLElement>(".agent-panel")!.hidden =
    !enabled("agent.conversation");
  const track = (element: HTMLElement, name: string, visible: boolean): void => {
    if (visible) element.style.removeProperty(name); else element.style.setProperty(name, "0px");
  };
  const workbench = document.querySelector<HTMLElement>(".workbench")!;
  track(workbench, "--command-track", enabled("commandBar"));
  track(workbench, "--status-track", enabled("statusBar"));
  const workspace = document.querySelector<HTMLElement>(".workspace")!;
  track(workspace, "--rail-track", enabled("navigation"));
  track(workspace, "--context-track", enabled("panels.context") && state.panelOpen);
  track(workspace, "--agent-track", enabled("agent.conversation"));
  const viewport = document.querySelector<HTMLElement>(".viewport-shell")!;
  track(viewport, "--toolbar-track", enabled("viewport.toolbar"));
  track(viewport, "--diagnostics-track", enabled("diagnostics"));
  const tools: Tool[] = ["select", "move", "rotate", "scale"];
  if (!enabled(`viewport.tools.${state.tool}`)) {
    state.tool = tools.find((tool) => enabled(`viewport.tools.${tool}`)) ?? "select";
    renderer.setTool(state.tool);
  }
  renderer.setInteractions(enabled("viewport.orbit"), enabled("viewport.selectEntity"));
  if (!enabled("viewport.overlays.collider")) state.collider = false;
  if (!enabled("viewport.overlays.ifps")) state.ifps = false;
  if (!enabled(`viewport.cameraPreview.modes.${state.previewMode}`)) {
    state.previewMode = enabled("viewport.cameraPreview.modes.rgb") ? "rgb" : "depth";
  }
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(body?.error?.message ?? body?.message ?? `${response.status} ${response.statusText}`) as Error & { code?: string }; error.code = body?.error?.code; throw error; }
  return body as T;
}

async function refreshSnapshot(): Promise<void> {
  state.snapshot = await json<SceneSnapshot>("/api/bootstrap");
  state.presentationMode = state.snapshot.presentation?.mode ?? "real-sync";
  state.features = state.snapshot.features;
  setTimeline(state.snapshot.presentation?.timeline ?? null);
  applyFeatureVisibility();
  if (!state.snapshot.entities.some((entity) => entity.id === state.selectedId)) state.selectedId = 0;
  renderer.syncScene(state.snapshot); renderer.setTool(state.tool); updateAll();
  updateCommandState();
}
async function refreshAssets(): Promise<void> { state.assets = (await json<{ assets: AssetSummary[] }>("/api/assets")).assets; if (state.panel === "assets") renderContext(); }
async function refreshDiagnostics(): Promise<void> { state.logs = (await json<{ logs: Array<Record<string, unknown>> }>("/api/diagnostics")).logs; renderDiagnostics(); }

async function sendCommand(method: string, params: Record<string, unknown> = {}): Promise<void> {
  if (!state.snapshot || state.busy || state.presentationMode === "sync-and-animate") return;
  state.busy = true; updateCommandState();
  try {
    await json("/api/commands", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ requestId: crypto.randomUUID(), sceneId: state.snapshot.sceneId, baseRevision: state.snapshot.revision, method, params }) });
    await refreshSnapshot(); await refreshDiagnostics();
  } catch (error) {
    const typed = error as Error & { code?: string }; if (typed.code === "REVISION_CONFLICT") await refreshSnapshot(); showError(typed.code ? `${typed.code}: ${typed.message}` : typed.message);
  } finally { state.busy = false; updateCommandState(); }
}

function updateAll(): void { renderContext(); updateStatus(); updateSelection(); updatePreview(); updateAgent(); updateCommandState(); drawIcons(); }
function updateCommandState(): void {
  const readonly = state.presentationMode === "sync-and-animate";
  document.querySelectorAll<HTMLButtonElement>("[data-command]").forEach((button) => { button.disabled = state.busy || readonly; });
  document.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((button) => { if (button.dataset.tool !== "select") button.disabled = readonly; });
  document.querySelectorAll<HTMLButtonElement>("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === state.presentationMode));
  document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>(
    '#create-kind, [data-action="upload"], [data-action="import-remote"], [data-action="delete-entity"], [data-action="send-joints"], [data-delete-asset], #inspector-form input, #inspector-form select, #inspector-form button, #backend-form input, #backend-form select, #backend-form button',
  ).forEach((control) => { control.disabled = readonly; });
  document.querySelectorAll<HTMLButtonElement>("[data-asset]").forEach((button) => { button.disabled = readonly || button.disabled; });
}
function selectEntity(id: number): void { state.selectedId = id; renderer.select(id === 0 ? null : id); renderContext(); updateSelection(); updatePreview(); }
function updateSelection(): void { const entity = selectedEntity(); document.querySelector("#selection-label")!.textContent = entity ? `${entity.name}  #${entity.id}` : "No selection"; }

function treeRows(parent: number | null, depth = 0): string {
  if (!state.snapshot) return "";
  return state.snapshot.entities.filter((entity) => entity.parent === parent).map((entity) => `<div class="tree-row${entity.id === state.selectedId ? " selected" : ""}" style="--depth:${depth}" draggable="${enabled("panels.scene.dragReparent") && entity.id !== 0 && !entity.robotManaged}" data-entity="${entity.id}"><i class="indent"></i><i data-lucide="${state.snapshot!.entities.some((item) => item.parent === entity.id) ? "chevron-down" : "chevron-right"}"></i><i data-lucide="${entity.kind === "camera" ? "camera" : entity.kind === "light" ? "lightbulb" : entity.kind === "robot" ? "bot" : entity.kind === "world" ? "layers-3" : "box"}"></i><span>${escapeHtml(entity.name)}</span>${entity.visual ? `<i data-lucide="eye"></i>` : ""}</div>${treeRows(entity.id, depth + 1)}`).join("");
}
function renderHierarchy(): string {
  const footer = enabled("panels.scene.dragReparent") || enabled("panels.scene.deleteEntity")
    ? `<div class="panel-footer">${when("panels.scene.dragReparent", "<span>Drag to reparent</span>")}${when("panels.scene.deleteEntity", '<button class="danger" data-action="delete-entity" data-feature="panels.scene.deleteEntity"><i data-lucide="trash-2"></i></button>')}</div>` : "";
  return `${when("panels.scene.search", '<label class="search"><i data-lucide="search"></i><input id="tree-search" type="search" placeholder="Search entities"></label>')}${when("panels.scene.hierarchy", `<div class="tree">${treeRows(null)}</div>`)}${footer}`;
}
function numberInput(label: string, name: string, value: number, options = 'step="any"'): string { return `<label class="field"><span>${label}</span><input type="number" name="${name}" value="${Number(value).toFixed(4)}" ${options}></label>`; }
function vectorFields(prefix: string, value: Vec3, disabled = false): string { return `<div class="vector"><span>${prefix}</span>${["X", "Y", "Z"].map((axis, index) => `<label><small>${axis}</small><input type="number" step="any" name="${prefix.toLowerCase()}-${index}" value="${value[index]!.toFixed(4)}" ${disabled ? "disabled" : ""}></label>`).join("")}</div>`; }

function renderDetails(): string {
  const entity = selectedEntity();
  if (!entity) return when("panels.details.emptyState", `<div class="empty"><i data-lucide="info"></i><p>Select an entity to inspect it.</p></div>`);
  const q = new Quaternion(entity.local.rotationWxyz[1], entity.local.rotationWxyz[2], entity.local.rotationWxyz[3], entity.local.rotationWxyz[0]);
  const e = new Euler().setFromQuaternion(q, "XYZ"); const rotation = [MathUtils.radToDeg(e.x), MathUtils.radToDeg(e.y), MathUtils.radToDeg(e.z)] as Vec3;
  const bodyOwner = rigidBodyOwner(entity);
  const body = bodyOwner?.rigidBody ?? { enabled: false, dynamic: false, gravity: true, mass: 1 };
  const inheritedBody = bodyOwner !== undefined && bodyOwner.id !== entity.id;
  const bodyReadOnly = bodyOwner !== undefined && (!entity.collider || inheritedBody);
  const bodyDisabled = bodyReadOnly ? "disabled" : "";
  const identity = when("panels.details.identity", `<section><header><strong>Identity</strong><span>${escapeHtml(entity.kind)}</span></header>${when("panels.details.identity.name", `<label class="field"><span>Name</span><input name="name" value="${escapeHtml(entity.name)}" ${entity.id === 0 ? "disabled" : ""}></label>`)}${when("panels.details.identity.entityId", `<div class="readout"><span>Entity ID</span><code>${entity.id}</code></div>`)}${when("panels.details.identity.parent", `<div class="readout"><span>Parent</span><code>${entity.parent ?? "None"}</code></div>`)}${when("panels.details.identity.flags", `<div class="tags">${entity.flags.map((flag) => `<span>${escapeHtml(flag)}</span>`).join("") || "<span>root</span>"}</div>`)}</section>`);
  const transform = entity.id !== 0 ? when("panels.details.transform", `<section><header><strong>Transform</strong><span>Local</span></header>${entity.robotManaged ? when("panels.details.transform.notice", `<p class="notice">Managed by the robot articulation. Transform is read-only.</p>`) : ""}${when("panels.details.transform.position", vectorFields("Position", entity.local.translation, entity.robotManaged))}${when("panels.details.transform.rotation", vectorFields("Rotation", rotation, entity.robotManaged))}${when("panels.details.transform.scale", vectorFields("Scale", entity.local.scale, entity.robotManaged))}</section>`) : "";
  const visual = entity.visual ? when("panels.details.visual", `<section><header><strong>Visual / LOD</strong><span>${escapeHtml(entity.visual.shape)}</span></header>${when("panels.details.visual.visible", `<label class="check"><input type="checkbox" name="visible" ${entity.visual.visible ? "checked" : ""}>Visible</label>`)}<div class="two">${when("panels.details.visual.minLod", numberInput("Finest LOD", "minLod", entity.visual.minLod, 'min="0" max="7" step="1"'))}${when("panels.details.visual.maxLod", numberInput("Coarsest LOD", "maxLod", entity.visual.maxLod, 'min="0" max="7" step="1"'))}</div></section>`) : "";
  const physicsNotice = inheritedBody
    ? `<p class="notice">RigidBody inherited from ${escapeHtml(bodyOwner!.name)}.</p>`
    : entity.rigidBody && !entity.collider
      ? `<p class="notice">Collider geometry is provided by descendant mesh entities.</p>`
      : !entity.rigidBody
        ? `<p class="notice">Enable RigidBody to include this collider in physics simulation.</p>`
        : "";
  const physics = entity.collider || entity.rigidBody ? when("panels.details.physics", `<section><header><strong>Physics / IFPS</strong><span>${bodyOwner ? body.dynamic ? "Dynamic" : "Static" : "No body"}</span></header>${when("panels.details.physics.notice", physicsNotice)}${when("panels.details.physics.rigidBodyEnabled", `<label class="check"><input type="checkbox" name="bodyEnabled" ${body.enabled ? "checked" : ""} ${bodyDisabled}>RigidBody Enabled</label>`)}${when("panels.details.physics.dynamic", `<label class="check"><input type="checkbox" name="dynamic" ${body.dynamic ? "checked" : ""} ${bodyDisabled}>Dynamic</label>`)}${when("panels.details.physics.gravity", `<label class="check"><input type="checkbox" name="gravity" ${body.gravity ? "checked" : ""} ${bodyDisabled}>Gravity</label>`)}${when("panels.details.physics.mass", numberInput("Mass kg", "mass", body.mass, `min="0.0001" step="any" ${bodyDisabled}`))}${entity.collider ? when("panels.details.physics.friction", vectorFields("Friction", entity.collider.friction, bodyReadOnly)) : ""}${entity.collider ? when("panels.details.physics.restitution", numberInput("Restitution", "restitution", entity.collider.restitution, `min="0" max="1" step="any" ${bodyDisabled}`)) : ""}${entity.collider ? when("panels.details.physics.ifpsSphereCount", numberInput("IFPS Spheres", "ifpsSphereCount", entity.collider.ifpsSphereCount, `min="0" max="4096" step="1" ${bodyDisabled}`)) : ""}</section>`) : "";
  const camera = entity.camera ? when("panels.details.camera", `<section><header><strong>Camera</strong></header><label class="field"><span>Output</span><select name="cameraOutput"><option value="rgb" ${entity.camera.output === "rgb" ? "selected" : ""}>RGB</option><option value="rgbd" ${entity.camera.output === "rgbd" ? "selected" : ""}>RGBD</option></select></label><label class="field"><span>Frame ID</span><input name="frameId" value="${escapeHtml(entity.camera.frameId)}"></label><div class="two">${numberInput("Update Hz", "updateRateHz", entity.camera.updateRateHz, 'min="0.1" max="240" step="any"')}${numberInput("Vertical FOV", "verticalFovDegrees", entity.camera.verticalFovDegrees, 'min="1" max="179" step="any"')}${numberInput("Width", "cameraWidth", entity.camera.width, 'min="1" max="8192" step="1"')}${numberInput("Height", "cameraHeight", entity.camera.height, 'min="1" max="8192" step="1"')}${numberInput("Near Plane", "nearPlane", entity.camera.nearPlane, 'min="0.001" step="any"')}${numberInput("Far Plane", "farPlane", entity.camera.farPlane, 'min="0.01" step="any"')}</div></section>`) : "";
  const light = entity.light ? `<section><header><strong>Lighting</strong><span>${escapeHtml(entity.light.type === "ambient" ? "环境光" : entity.light.type)}</span></header><label class="field"><span>Type</span><select name="lightType"><option ${entity.light.type === "point" ? "selected" : ""}>point</option><option ${entity.light.type === "spot" ? "selected" : ""}>spot</option><option ${entity.light.type === "directional" ? "selected" : ""}>directional</option><option ${entity.light.type === "ambient" ? "selected" : ""}>ambient</option></select></label><label class="field"><span>Color</span><input type="color" name="lightColor" value="#${entity.light.color.map((value) => Math.round(Math.max(0, Math.min(1, value)) * 255).toString(16).padStart(2, "0")).join("")}"></label><label class="check"><input type="checkbox" name="lightEnabled" ${entity.light.enabled ? "checked" : ""}>Enabled</label>${numberInput("Intensity", "lightIntensity", entity.light.intensity, 'min="0" step="any"')}${entity.light.type === "point" || entity.light.type === "spot" ? `${numberInput("Range", "lightRange", entity.light.range, 'min="0" step="any"')}${numberInput("Decay", "lightDecay", entity.light.decay, 'min="0" step="any"')}` : ""}${entity.light.type === "spot" ? `${numberInput("Inner Cone Angle (deg)", "lightInner", MathUtils.radToDeg(entity.light.innerConeRadians), 'min="0" max="89.9" step="any"')}${numberInput("Outer Cone Angle (deg)", "lightOuter", MathUtils.radToDeg(entity.light.outerConeRadians), 'min="0.1" max="89.9" step="any"')}` : ""}${entity.light.type === "directional" ? numberInput("Angular Diameter", "lightAngular", entity.light.angularDiameterRadians, 'min="0" max="1.571" step="any"') : ""}</section>` : "";
  const apply = entity.id !== 0 ? when("panels.details.apply", `<div class="apply"><span id="validation"></span><button class="button primary" type="submit">Apply Changes</button></div>`) : "";
  return `<form id="inspector-form" class="inspector">${identity}${transform}${visual}${physics}${camera}${light}${entity.robot ? renderRobot(entity) : ""}${apply}</form>`;
}
function renderRobot(entity: EntityView): string {
  if (!enabled("panels.details.robot")) return "";
  const robot = entity.robot!;
  const showNames = enabled("panels.details.robot.jointNames");
  const showPositions = enabled("panels.details.robot.positions");
  const showTargets = enabled("panels.details.robot.targets");
  const rows = robot.jointNames.map((name, index) => `${showNames ? `<span>${escapeHtml(name)}</span>` : "<span></span>"}${showPositions ? `<code>${robot.positions[index]?.toFixed(4) ?? "--"}</code>` : "<span></span>"}${showTargets ? `<input type="number" step="any" name="joint-${index}" value="${robot.positions[index] ?? 0}">` : "<span></span>"}`).join("");
  const table = showNames || showPositions || showTargets ? `<div class="joints"><b>${showNames ? "Joint" : ""}</b><b>${showPositions ? "Position" : ""}</b><b>${showTargets ? "Target" : ""}</b>${rows}</div>` : "";
  return `<section><header><strong>Robot Control</strong><span>${robot.jointNames.length} joints</span></header>${table}${when("panels.details.robot.send", `<button class="button full" type="button" data-action="send-joints" data-feature="panels.details.robot.send">Send Joint Targets</button>`)}</section>`;
}

function renderSettings(): string {
  const settings = state.snapshot?.settings, capabilities = state.snapshot?.capabilities; if (!settings || !capabilities) return "";
  const select = (label: string, name: string, values: string[], current: string) => `<label class="field"><span>${label}</span><select name="${name}">${values.map((value) => `<option ${value === current ? "selected" : ""}>${escapeHtml(value)}</option>`).join("")}</select></label>`;
  const manager = when("panels.settings.backendManager", `<section><header><strong>Backend Manager</strong><span>Paused only</span></header>${when("panels.settings.backendManager.physics", select("Physics", "physics", capabilities.physics ?? [], settings.physics))}${when("panels.settings.backendManager.collision", select("Collision", "collision", capabilities.collision ?? [], settings.collision))}${when("panels.settings.backendManager.platformRenderer", select("Platform Renderer", "renderer", capabilities.renderer ?? [], settings.renderer))}${when("panels.settings.backendManager.browserRenderer", select("Browser Renderer", "browserRenderer", capabilities.browserRenderer ?? [], settings.browserRenderer))}${when("panels.settings.backendManager.notice", `<p class="notice">Platform rendering and the browser adapter are independent capabilities.</p>`)}</section>`);
  const timing = when("panels.settings.timing", `<section><header><strong>Simulation Timing</strong><span>Authoritative</span></header>${when("panels.settings.timing.fixedTimeStep", numberInput("Fixed Time Step", "fixedTimeStep", settings.fixedTimeStep, 'min="0.0001" max="1" step="any"'))}${when("panels.settings.timing.realTimeFactor", numberInput("Real-Time Factor", "realTimeFactor", settings.realTimeFactor, 'min="0.01" max="10" step="any" title="Simulated time divided by elapsed wall-clock time. 1.0 is real time."'))}</section>`);
  return `<form id="backend-form" class="inspector">${manager}${timing}${when("panels.settings.apply", `<div class="apply"><span></span><button class="button primary" type="submit" ${state.snapshot?.status === "running" ? "disabled" : ""}>Apply Backends</button></div>`)}</form>`;
}
function renderAssets(): string {
  const create = enabled("panels.assets.create");
  const groups = enabled("panels.assets.list") ? (["builtin", "project", "uploaded", "remote"] as const)
    .filter((source) => enabled(`panels.assets.groups.${source}`)).map((source) => {
      const assets = state.assets.filter((asset) => asset.source === source);
      const rows = assets.map((asset) => `<button class="asset-row" ${create ? `draggable="true" data-asset="${escapeHtml(asset.id)}"` : "disabled"}><i data-lucide="${asset.type === "Sensor" ? "camera" : asset.type === "Primitive" ? "box" : "image"}"></i><span>${when("panels.assets.name", `<strong>${escapeHtml(asset.name)}</strong>`)}${when("panels.assets.metadata", `<small>${escapeHtml(asset.type)} · ${escapeHtml(asset.importStatus)}</small>`)}</span>${source === "uploaded" || source === "remote" ? when("panels.assets.delete", `<i data-delete-asset="${escapeHtml(asset.id)}" data-lucide="trash-2"></i>`) : ""}</button>`).join("") || `<p class="empty-line">No assets</p>`;
      const label = source === "builtin" ? "Built-in" : source === "project" ? "Project Assets" : source === "remote" ? "Remote" : "Uploaded";
      return `<section class="asset-group"><header><strong>${label}</strong><span>${assets.length}</span></header>${rows}</section>`;
    }).join("") : "";
  return `${when("panels.assets.search", '<label class="search"><i data-lucide="search"></i><input id="asset-search" type="search" placeholder="Filter assets"></label>')}${when("panels.assets.upload", '<button class="button full upload" data-action="upload" data-feature="panels.assets.upload"><i data-lucide="upload"></i>Choose URDF / Mesh File</button>')}${when("panels.assets.importRemote", '<button class="button full" data-action="import-remote" data-feature="panels.assets.importRemote"><i data-lucide="link"></i>Load Remote Asset</button>')}<div>${groups}</div>`;
}
function renderContext(): void {
  ensureEnabledPanel();
  const definitions: Record<Panel, [string, string, () => string]> = { scene: ["SCENE", "Hierarchy", renderHierarchy], details: ["ENTITY", "Details", renderDetails], settings: ["GLOBAL", "Settings", renderSettings], assets: ["LIBRARY", "Assets", renderAssets] };
  const [eyebrow, title, render] = definitions[state.panel]; document.querySelector("#panel-eyebrow")!.textContent = eyebrow; document.querySelector("#panel-title")!.textContent = title; document.querySelector("#panel-count")!.textContent = String(state.panel === "assets" ? state.assets.length : state.snapshot?.entities.length ?? 0); document.querySelector("#context-content")!.innerHTML = enabled("panels.context") ? render() : "";
  const panelVisible = enabled("panels.context") && state.panelOpen;
  const contextPanel = document.querySelector<HTMLElement>("#context-panel")!;
  contextPanel.classList.toggle("collapsed", !panelVisible);
  contextPanel.setAttribute("aria-hidden", String(!panelVisible));
  const workspace = document.querySelector<HTMLElement>(".workspace")!;
  if (panelVisible) workspace.style.removeProperty("--context-track"); else workspace.style.setProperty("--context-track", "0px");
  document.querySelectorAll<HTMLElement>("[data-panel]").forEach((item) => {
    const active = item.dataset.panel === state.panel && panelVisible;
    item.classList.toggle("active", active);
    item.setAttribute("aria-expanded", String(active));
  });
  drawIcons();
}

function updateStatus(): void {
  const snapshot = state.snapshot, connected = state.websocket === "connected"; document.querySelector("#connection-dot")!.className = connected ? "online" : ""; document.querySelector("#connection-label")!.textContent = state.websocket; if (!snapshot) return;
  document.querySelector("#status-primary")!.textContent = `${connected ? "Connected" : "Disconnected"} · ${snapshot.status} · t=${snapshot.simulationTime.toFixed(3)}s`;
  document.querySelector("#status-performance")!.textContent = `${state.renderFps.toFixed(0)} FPS / ${state.renderMs.toFixed(1)} ms · Physics ${snapshot.performance.physicsMs.toFixed(2)} ms`;
  document.querySelector("#status-backends")!.textContent = `${snapshot.settings.physics} · ${snapshot.settings.collision} · ${snapshot.settings.renderer}`; document.querySelector("#status-revision")!.textContent = `Rev ${snapshot.revision}`; document.querySelector<HTMLInputElement>("#rtf")!.value = String(snapshot.settings.realTimeFactor);
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
  const connected = state.snapshot?.agent.connected; const badge = document.querySelector("#agent-state")!; badge.textContent = connected ? "Connected" : "Offline"; badge.className = connected ? "connected" : "";
  const target = document.querySelector("#agent-messages")!; target.innerHTML = state.messages.length ? state.messages.map((message) => { const hasPlan = message.role === "agent" && (message.planKey ?? message.jobId) === state.agentPlanKey && state.agentPlan.length > 0; const working = Boolean(message.status && !terminalAgentStatuses.has(message.status)); const plan = hasPlan ? `<div class="message-plan">${state.agentPlan.map((item) => `<div class="agent-plan-item ${item.status}"><i data-lucide="${item.status === "completed" ? "check-circle-2" : item.status === "in_progress" ? "loader-circle" : "circle"}"></i><span>${escapeHtml(item.step)}</span></div>`).join("")}</div>` : ""; return `<article class="message ${message.role}"><header><strong>${message.role === "user" ? "You" : "Agent"}</strong><time>${new Date(message.time).toLocaleTimeString()}</time></header>${hasPlan && working ? "" : `<p>${escapeHtml(message.text)}</p>`}${plan}${message.status ? `<footer><span class="${escapeHtml(message.status)}">${escapeHtml(message.status)}</span>${message.jobId ? `<code>${escapeHtml(message.jobId.slice(0, 8))}</code>` : ""}</footer>` : ""}</article>`; }).join("") : when("agent.messages.emptyState", `<div class="agent-empty"><i data-lucide="bot"></i><strong>Scene Agent</strong><p>Commands execute through the Gateway and Sim Core.</p></div>`); target.scrollTop = target.scrollHeight; const operations = state.messages.filter((message) => message.jobId); document.querySelector("#operation-count")!.textContent = String(operations.length); document.querySelector("#operations-content")!.innerHTML = operations.map((message) => `<div><code>${escapeHtml(message.jobId?.slice(0, 8))}</code><span>${escapeHtml(message.status ?? "queued")}</span><small>${escapeHtml(message.text)}</small></div>`).join("") || `<p class="empty-line">No Agent operations.</p>`; localStorage.setItem(`robot-sim-agent:${state.snapshot?.sceneId ?? "main"}`, JSON.stringify(state.messages.slice(-200))); drawIcons();
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
async function refreshConversationHistory(): Promise<void> {
  const result = await json<{ activeConversationId: string | null; conversations: ConversationSummary[] }>("/api/agent/conversations");
  state.activeConversationId = result.activeConversationId;
  state.conversationHistory = result.conversations;
  renderConversationHistory();
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
  try {
    const detail = await json<ConversationDetail>(`/api/agent/conversations/${encodeURIComponent(conversationId)}/activate`, { method: "POST" });
    applyConversation(detail);
  } catch (error) { showError((error as Error).message); }
}
async function createConversation(): Promise<void> {
  try {
    const detail = await json<ConversationDetail>("/api/agent/conversations", { method: "POST" });
    applyConversation(detail);
    showAgentToast("已新建对话");
  } catch (error) { showError((error as Error).message); }
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
  state.historyOpen = false;
  // Activation restores the authoritative scene on the server. Stop any old
  // playback before applying the new conversation's state.
  clearTimeout(timelineTimer);
  state.timelinePlaying = false;
  const presentation = detail.presentation;
  const snapshot = detail.snapshot ?? presentation?.snapshot ?? state.snapshot;
  if (!snapshot) throw new Error("Conversation scene snapshot is unavailable");
  state.snapshot = snapshot;
  state.presentationMode = presentation?.mode ?? snapshot.presentation?.mode ?? "real-sync";
  setTimeline(presentation?.timeline ?? snapshot.presentation?.timeline ?? null);
  renderer.syncScene(snapshot);
  updateAll();
  updateAgentDebug();
  renderConversationHistory();
}
function updateAgentDebug(): void { const target = document.querySelector<HTMLElement>("#agent-debug-events")!; document.querySelector("#agent-debug-count")!.textContent = String(state.agentDebug.length); for (const entry of state.agentDebug) { const plan = agentPlanItemsFromEvent(entry.event); if (plan.length) { state.agentPlan = plan; state.agentPlanKey = entry.jobId; } } target.innerHTML = state.agentDebug.map((entry) => { const item = entry.event.item; const itemType = item && typeof item === "object" ? (item as Record<string, unknown>).type : undefined; const type = String(entry.event.type ?? itemType ?? "event"); return `<article><header><code>${escapeHtml(entry.jobId.slice(0, 8))}</code><span>${escapeHtml(type)}</span></header><pre>${escapeHtml(JSON.stringify(entry.event, null, 2))}</pre></article>`; }).join("") || `<p class="empty-line">No debug events.</p>`; updateAgent(); target.scrollTop = target.scrollHeight; }
function mergeAgentDebug(jobId: string, events: Record<string, unknown>[]): void { if (!enabled("agent.debug")) return; for (let index = 0; index < events.length; index += 1) { if (!state.agentDebug.some((entry) => entry.jobId === jobId && entry.index === index)) state.agentDebug.push({ jobId, index, event: events[index]! }); } state.agentDebug = state.agentDebug.slice(-500); updateAgentDebug(); }
async function refreshAgentDebug(): Promise<void> { if (!enabled("agent.debug")) return; const jobs = (await json<{ jobs: AgentJob[] }>("/api/agent/jobs")).jobs; for (const job of jobs) mergeAgentDebug(job.id, job.debugEvents ?? []); }
function renderDiagnostics(): void { document.querySelector("#diagnostics-content")!.innerHTML = state.logs.slice(-80).reverse().map((entry) => `<div class="log-row"><time>${new Date(Number(entry.time) * 1000).toLocaleTimeString()}</time><span>${escapeHtml(entry.level)}</span><code>${escapeHtml(entry.message)}</code></div>`).join("") || `<p class="empty-line">No operations recorded.</p>`; }

let previewTimer = 0;
function updatePreview(forceOpen = false): void { const entity = selectedEntity(), preview = document.querySelector<HTMLElement>("#camera-preview")!; if (!enabled("viewport.cameraPreview") || !entity?.camera) { preview.classList.add("hidden"); clearInterval(previewTimer); return; } if (forceOpen && enabled("panels.details.camera.preview")) preview.classList.remove("hidden"); if (preview.classList.contains("hidden")) return; document.querySelector("#camera-name")!.textContent = entity.name; document.querySelector("#preview-meta")!.textContent = `${entity.camera.width}×${entity.camera.height} · ${entity.camera.updateRateHz} Hz · t=${state.snapshot?.simulationTime.toFixed(3)}s`; loadPreview(); clearInterval(previewTimer); previewTimer = window.setInterval(loadPreview, Math.max(250, 1000 / entity.camera.updateRateHz)); }
function loadPreview(): void { const entity = selectedEntity(); if (!entity?.camera) return; const status = document.querySelector("#preview-status")!; status.textContent = "loading"; const displayed = document.querySelector<HTMLImageElement>("#camera-image")!, image = new Image(); image.onload = () => { displayed.src = image.src; status.textContent = "streaming"; }; image.onerror = () => { status.textContent = state.websocket === "connected" ? "no_data" : "disconnected"; }; image.src = `/api/sensors/${entity.id}/frame?mode=${state.previewMode}&t=${Date.now()}`; }
function showError(message: string): void { if (!enabled("viewport.notifications") && state.snapshot) return; const toast = document.querySelector("#toast")!; toast.textContent = message; toast.classList.remove("hidden"); setTimeout(() => toast.classList.add("hidden"), 5000); }
function formNumber(form: FormData, name: string): number { const value = Number(form.get(name)); if (!Number.isFinite(value)) throw new Error(`${name} must be a finite number`); return value; }
function formNumberOr(form: FormData, name: string, fallback: number): number { return form.has(name) ? formNumber(form, name) : fallback; }
function formCheckedOr(form: FormData, name: string, fallback: boolean): boolean { return form.has(name) ? form.get(name) === "on" : fallback; }

async function applyInspector(element: HTMLFormElement): Promise<void> {
  const entity = selectedEntity(); if (!entity || entity.id === 0) return; const form = new FormData(element);
  try {
    const params: Record<string, unknown> = { entity: entity.id };
    const bodyOwner = rigidBodyOwner(entity);
    const inheritedBody = bodyOwner !== undefined && bodyOwner.id !== entity.id;
    if (enabled("panels.details.identity.name")) params.name = String(form.get("name"));
    if (!entity.robotManaged && enabled("panels.details.transform")) {
      const currentQ = new Quaternion(entity.local.rotationWxyz[1], entity.local.rotationWxyz[2], entity.local.rotationWxyz[3], entity.local.rotationWxyz[0]);
      const currentE = new Euler().setFromQuaternion(currentQ, "XYZ");
      const currentRotation = [MathUtils.radToDeg(currentE.x), MathUtils.radToDeg(currentE.y), MathUtils.radToDeg(currentE.z)] as Vec3;
      const translation = [0, 1, 2].map((i) => formNumberOr(form, `position-${i}`, entity.local.translation[i]!)) as Vec3;
      const rotation = [0, 1, 2].map((i) => MathUtils.degToRad(formNumberOr(form, `rotation-${i}`, currentRotation[i]!))) as Vec3;
      const scale = [0, 1, 2].map((i) => formNumberOr(form, `scale-${i}`, entity.local.scale[i]!)) as Vec3;
      const scaleError = validateScale(scale); if (scaleError) throw new Error(scaleError); const q = new Quaternion().setFromEuler(new Euler(...rotation)); params.transform = { translation, rotationWxyz: [q.w, q.x, q.y, q.z], scale };
    }
    if (entity.visual && enabled("panels.details.visual")) { const minLod = formNumberOr(form, "minLod", entity.visual.minLod), maxLod = formNumberOr(form, "maxLod", entity.visual.maxLod); if (minLod > maxLod) throw new Error("Finest LOD cannot exceed Coarsest LOD"); params.visual = { visible: formCheckedOr(form, "visible", entity.visual.visible), minLod, maxLod }; }
    if (entity.collider && !inheritedBody && enabled("panels.details.physics")) { const body = entity.rigidBody ?? { enabled: false, dynamic: false, gravity: true, mass: 1 }; const mass = formNumberOr(form, "mass", body.mass), friction = [0, 1, 2].map((i) => formNumberOr(form, `friction-${i}`, entity.collider!.friction[i]!)) as Vec3, restitution = formNumberOr(form, "restitution", entity.collider.restitution), count = formNumberOr(form, "ifpsSphereCount", entity.collider.ifpsSphereCount); if (mass <= 0 || friction.some((value) => value < 0) || restitution < 0 || restitution > 1 || count < 0) throw new Error("Physics values are outside the supported range"); params.rigidBody = { enabled: formCheckedOr(form, "bodyEnabled", body.enabled), dynamic: formCheckedOr(form, "dynamic", body.dynamic), gravity: formCheckedOr(form, "gravity", body.gravity), mass, friction, restitution }; params.ifpsSphereCount = Math.floor(count); }
    if (entity.camera && enabled("panels.details.camera")) { const nearPlane = formNumberOr(form, "nearPlane", entity.camera.nearPlane), farPlane = formNumberOr(form, "farPlane", entity.camera.farPlane), cameraError = validateCamera(nearPlane, farPlane); if (cameraError) throw new Error(cameraError); params.camera = { output: form.get("cameraOutput") ?? entity.camera.output, frameId: form.get("frameId") ?? entity.camera.frameId, updateRateHz: formNumberOr(form, "updateRateHz", entity.camera.updateRateHz), width: formNumberOr(form, "cameraWidth", entity.camera.width), height: formNumberOr(form, "cameraHeight", entity.camera.height), verticalFovDegrees: formNumberOr(form, "verticalFovDegrees", entity.camera.verticalFovDegrees), nearPlane, farPlane }; }
    if (entity.light) {
      const color = String(form.get("lightColor") ?? "").match(/^#([0-9a-f]{6})$/i);
      const rgb = color ? [0, 1, 2].map((index) => Number.parseInt(color[1]!.slice(index * 2, index * 2 + 2), 16) / 255) : entity.light.color;
      const innerConeRadians = MathUtils.degToRad(formNumberOr(form, "lightInner", MathUtils.radToDeg(entity.light.innerConeRadians)));
      const outerConeRadians = MathUtils.degToRad(formNumberOr(form, "lightOuter", MathUtils.radToDeg(entity.light.outerConeRadians)));
      if ((form.get("lightType") ?? entity.light.type) === "spot") {
        const coneError = validateSpotCone(innerConeRadians, outerConeRadians);
        if (coneError) throw new Error(coneError);
      }
      params.light = { type: form.get("lightType") ?? entity.light.type, enabled: formCheckedOr(form, "lightEnabled", entity.light.enabled), intensity: formNumberOr(form, "lightIntensity", entity.light.intensity), range: formNumberOr(form, "lightRange", entity.light.range), decay: formNumberOr(form, "lightDecay", entity.light.decay), innerConeRadians, outerConeRadians, angularDiameterRadians: formNumberOr(form, "lightAngular", entity.light.angularDiameterRadians), color: rgb };
    }
    await sendCommand("scene.update_entity", params);
  } catch (error) { const validation = document.querySelector("#validation"); if (validation) validation.textContent = (error as Error).message; }
}

async function createAsset(assetId: string, translation?: Vec3): Promise<void> { if (!enabled("panels.assets.create") && !assetId.startsWith("builtin:")) return; const parent = state.selectedId || 0; if (assetId.startsWith("builtin:camera-")) await sendCommand("scene.create_camera", { parent, output: assetId.endsWith("rgbd") ? "rgbd" : "rgb", translation }); else if (assetId.startsWith("builtin:light-")) await sendCommand("scene.create_light", { parent, type: assetId.split("-")[1], translation }); else if (assetId.startsWith("builtin:")) await sendCommand("scene.create_primitive", { parent, kind: assetId.split(":")[1], translation }); else { const asset = state.assets.find((item) => item.id === assetId); if (asset) await sendCommand("scene.load_asset", { parent, assetId, translation }); } }

function setTimeline(timeline: TimelineManifest | null): void {
  clearTimeout(timelineTimer);
  state.timelinePlaying = false;
  timelineGeneration += 1;
  timelinePlayRequest += 1;
  timelineLoader?.dispose();
  state.timeline = timeline;
  state.timelineIndex = 0;
  timelineLoader = timeline ? new TimelineLoader(timeline, json) : null;
  const generation = timelineGeneration;
  if (timelineLoader) {
    console.debug("Timeline load", { frameCount: timeline?.frameCount, compact: Boolean(timeline?.dataUrl) });
    void timelineLoader.preload().catch((error) => {
      if ((error as Error).name !== "AbortError" && generation === timelineGeneration) {
        showError(`Timeline preload failed: ${(error as Error).message}`);
      }
    });
  }
  const element = document.querySelector<HTMLElement>("#timeline")!;
  element.classList.toggle(
    "hidden", !enabled("presentation.timeline") || !timeline || timeline.frameCount < 2);
  const range = document.querySelector<HTMLInputElement>("#timeline-range")!;
  range.max = String(Math.max(0, (timeline?.frameCount ?? 1) - 1));
  range.value = "0";
  updateTimelineControls();
}

function updateTimelineControls(): void {
  const timeline = state.timeline;
  const current = timeline?.frames[state.timelineIndex];
  const start = timeline?.frames[0]?.simulationTime ?? 0;
  const time = Math.max(0, (current?.simulationTime ?? start) - start);
  document.querySelector("#timeline-time")!.textContent = `${time.toFixed(2)} / ${(timeline?.durationSeconds ?? 0).toFixed(2)} s`;
  document.querySelector<HTMLInputElement>("#timeline-range")!.value = String(state.timelineIndex);
  const play = document.querySelector<HTMLButtonElement>('[data-timeline="play"]')!;
  const title = state.timelinePlaying ? "Pause" : "Play";
  if (play.title !== title) {
    play.innerHTML = `<i data-lucide="${state.timelinePlaying ? "pause" : "play"}"></i>`;
    play.title = title;
    drawIcons();
  }
}

async function showTimelineFrame(index: number): Promise<void> {
  const timeline = state.timeline;
  if (!timeline) return;
  const generation = timelineGeneration;
  const seek = ++timelineSeek;
  const loader = timelineLoader;
  if (!loader) return;
  state.timelineIndex = Math.max(0, Math.min(index, timeline.frameCount - 1));
  const frame = timeline.frames[state.timelineIndex];
  if (!frame) return;
  try {
    const snapshot = await loader.frame(state.timelineIndex);
    if (generation !== timelineGeneration || seek !== timelineSeek) return;
    state.snapshot = snapshot;
    renderer.syncTimelineFrame(snapshot);
    updateStatus();
    updateTimelineControls();
  } catch (error) {
    if (generation !== timelineGeneration || seek !== timelineSeek) return;
    state.timelinePlaying = false;
    updateTimelineControls();
    showError(`Timeline frame failed: ${(error as Error).message}`);
  }
}

function scheduleTimeline(): void {
  clearTimeout(timelineTimer);
  const timeline = state.timeline;
  if (!timeline || !state.timelinePlaying) return;
  if (state.timelineIndex >= timeline.frameCount - 1) {
    state.timelinePlaying = false;
    updateTimelineControls();
    return;
  }
  const elapsedSimulationTime = Math.max(0, performance.now() - timelineAnchorWallMs) *
    state.timelineRate / 1000;
  const dueIndex = timelineDueIndex(timeline.frames.map((frame) => frame.simulationTime),
    state.timelineIndex, timelineAnchorSimulationTime + elapsedSimulationTime);
  if (dueIndex > state.timelineIndex) {
    // Always yield a browser task when catching up. A chain of resolved bundle
    // promises otherwise starves requestAnimationFrame while playback is late.
    timelineTimer = window.setTimeout(() => {
      void showTimelineFrame(dueIndex).then(scheduleTimeline);
    }, 0);
    return;
  }
  const next = timeline.frames[state.timelineIndex + 1]!;
  const delay = timelineDelayMs(timelineAnchorWallMs, timelineAnchorSimulationTime,
    next.simulationTime, state.timelineRate, performance.now());
  timelineTimer = window.setTimeout(() => {
    void showTimelineFrame(state.timelineIndex + 1).then(scheduleTimeline);
  }, delay);
}

async function toggleTimelinePlayback(): Promise<void> {
  if (!state.timeline) return;
  const timeline = state.timeline;
  const generation = timelineGeneration;
  const request = ++timelinePlayRequest;
  if (state.timelinePlaying) {
    state.timelinePlaying = false;
    clearTimeout(timelineTimer);
  } else {
    try {
      await timelineLoader?.preload();
      if (generation !== timelineGeneration || request !== timelinePlayRequest) return;
      if (state.timelineIndex >= timeline.frameCount - 1) await showTimelineFrame(0);
      if (generation !== timelineGeneration || request !== timelinePlayRequest) return;
      state.timelinePlaying = true;
      timelineAnchorWallMs = performance.now();
      timelineAnchorSimulationTime = state.timeline.frames[state.timelineIndex]!.simulationTime;
      scheduleTimeline();
    } catch (error) {
      if (generation !== timelineGeneration || request !== timelinePlayRequest) return;
      showError(`Timeline preload failed: ${(error as Error).message}`);
    }
  }
  updateTimelineControls();
}

async function setPresentationMode(mode: "real-sync" | "sync-and-animate"): Promise<void> {
  const result = await json<{ mode: typeof mode; snapshot: SceneSnapshot; timeline: TimelineManifest | null }>("/api/presentation/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode, basePresentationRevision: state.snapshot?.presentation?.revision ?? 0 }) });
  state.presentationMode = result.mode;
  state.snapshot = result.snapshot;
  setTimeline(result.timeline);
  renderer.syncScene(result.snapshot);
  updateAll();
  updateCommandState();
}

function connectWebSocket(): void {
  state.websocket = state.websocket === "connecting" ? "connecting" : "reconnecting"; updateStatus(); const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/events`);
  socket.onopen = () => { const recovering = state.websocket === "reconnecting"; state.websocket = "connected"; updateStatus(); if (recovering) void refreshSnapshot(); };
  socket.onmessage = (message) => { if (message.data === "pong") return; const event = JSON.parse(message.data) as SceneEvent<Record<string, unknown>>; if (event.type === "scene.changed" || event.type === "simulation.tick") { const snapshot = event.payload.snapshot as SceneSnapshot | undefined, decision = decideSnapshot(state.snapshot?.revision ?? 0, event); if (event.type === "simulation.tick" && snapshot && snapshot.revision >= (state.snapshot?.revision ?? 0)) { state.snapshot = snapshot; renderer.syncScene(snapshot); updateStatus(); } else if (decision === "reload") void refreshSnapshot(); else if (snapshot && decision === "apply") { state.snapshot = snapshot; renderer.syncScene(snapshot); updateAll(); } } else if (event.type === "agent.debug") { const payload = event.payload as unknown as { jobId: string; index: number; event: Record<string, unknown> }; if (enabled("agent.debug") && !state.agentDebug.some((entry) => entry.jobId === payload.jobId && entry.index === payload.index)) { state.agentDebug.push(payload); state.agentDebug = state.agentDebug.slice(-500); updateAgentDebug(); } } else if (event.type === "agent.job_updated" || event.type === "agent.turn_updated") updateAgentJob(event.payload as unknown as AgentJob); else if (event.type.startsWith("presentation.")) void refreshSnapshot(); };
  socket.onclose = () => { state.websocket = "reconnecting"; updateStatus(); setTimeout(connectWebSocket, 1500); }; socket.onerror = () => socket.close();
}
function updateAgentJob(job: AgentJob): void { mergeAgentDebug(job.id, job.debugEvents ?? []); const item = state.messages.find((message) => message.jobId === job.id); if (item) { item.status = job.status; item.text = job.message || item.text; item.planKey = job.id; } else state.messages.push({ role: "agent", text: job.message, status: job.status, jobId: job.id, planKey: job.id, time: Date.now() }); updateAgent(); }
const terminalAgentStatuses = new Set(["success", "failed", "cancelled", "timeout", "interrupted"]);
async function trackAgentJob(jobId: string): Promise<void> { for (;;) { await new Promise((resolve) => window.setTimeout(resolve, 1000)); try { const job = await json<AgentJob>(`/api/agent/jobs/${encodeURIComponent(jobId)}`); updateAgentJob(job); if (terminalAgentStatuses.has(job.status)) return; } catch { return; } } }
async function submitAgent(text: string): Promise<void> { const value = text.trim(); if (!value) { showError("Agent command cannot be empty"); return; } state.agentPlan = []; state.agentPlanKey = null; state.messages.push({ role: "user", text: value, time: Date.now() }); updateAgent(); try { const job = await json<AgentJob>("/api/agent/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: value, sceneId: state.snapshot?.sceneId }) }); updateAgentJob(job); if (!terminalAgentStatuses.has(job.status)) void trackAgentJob(job.id); } catch (error) { state.messages.push({ role: "agent", text: (error as Error).message, status: "failed", time: Date.now() }); updateAgent(); } }

type Speech = { start(): void; stop(): void; continuous: boolean; interimResults: boolean; lang: string; onresult: ((event: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null; onerror: ((event: { error: string }) => void) | null; onend: (() => void) | null };
let recognition: Speech | null = null, speechActive = false, speechPrefix = "", speechFinal = "";
function toggleSpeech(): void { if (!enabled("agent.input.voiceInput")) return; if (speechActive) { recognition?.stop(); return; } const api = window as unknown as { SpeechRecognition?: new () => Speech; webkitSpeechRecognition?: new () => Speech }; const Constructor = api.SpeechRecognition ?? api.webkitSpeechRecognition; if (!Constructor) { showError("Speech recognition is not available in this browser"); return; } recognition = new Constructor(); const input = document.querySelector<HTMLTextAreaElement>("#agent-input")!; speechPrefix = input.value; speechFinal = ""; speechActive = true; recognition.continuous = true; recognition.interimResults = true; recognition.lang = navigator.language || "en-US"; recognition.onresult = (event) => { let interim = ""; for (let i = event.resultIndex; i < event.results.length; i += 1) { const result = event.results[i]!; if (result.isFinal) speechFinal += result[0].transcript; else interim += result[0].transcript; } input.value = mergeSpeech(speechPrefix, speechFinal, interim); document.querySelector("#speech-status")!.textContent = interim ? "Listening · interim" : "Listening"; }; recognition.onerror = (event) => { showError(`Speech recognition failed: ${event.error}`); speechActive = false; document.querySelector("#speech-status")!.textContent = "Text"; }; recognition.onend = () => { speechActive = false; document.querySelector("#speech-status")!.textContent = "Text"; }; recognition.start(); document.querySelector("#speech-status")!.textContent = "Listening"; }

app.addEventListener("click", async (event) => {
  const target = (event.target as HTMLElement).closest<HTMLElement>("button, [data-entity], [data-delete-asset]"); if (!target) return;
  if ((target as HTMLButtonElement).disabled) return;
  const featureElement = target.closest<HTMLElement>("[data-feature]");
  if (featureElement && !enabled(featureElement.dataset.feature!)) return;
  if (target.dataset.panel) { const panel = target.dataset.panel as Panel; if (!enabled(panelFeatures[panel])) return; state.panelOpen = state.panel !== panel || !state.panelOpen; state.panel = panel; renderContext(); return; }
  if (target.dataset.mode) { await setPresentationMode(target.dataset.mode as "real-sync" | "sync-and-animate"); return; }
  if (target.dataset.timeline) { if (!state.timeline) return; if (target.dataset.timeline === "first") await showTimelineFrame(0); else if (target.dataset.timeline === "last") await showTimelineFrame(state.timeline.frameCount - 1); else await toggleTimelinePlayback(); return; }
  if (target.dataset.tool) { const tool = target.dataset.tool as Tool; if (state.presentationMode === "sync-and-animate" && tool !== "select") return; if (!enabled(`viewport.tools.${tool}`)) return; state.tool = tool; renderer.setTool(state.tool); document.querySelectorAll("[data-tool]").forEach((item) => item.classList.toggle("active", item === target)); return; }
  if (target.dataset.command) { await sendCommand(target.dataset.command, target.dataset.command.startsWith("state.") ? { name: "default" } : {}); return; }
  if (target.dataset.asset && enabled("panels.assets.create")) { await createAsset(target.dataset.asset); return; }
  if (target.dataset.entity && enabled("panels.scene.selectEntity")) { selectEntity(Number(target.dataset.entity)); return; }
  if (target.dataset.deleteAsset) { event.stopPropagation(); try { await json(`/api/assets/${encodeURIComponent(target.dataset.deleteAsset)}`, { method: "DELETE" }); await refreshAssets(); } catch (error) { showError((error as Error).message); } return; }
  if (target.dataset.deleteConversation) { event.stopPropagation(); await deleteConversation(target.dataset.deleteConversation); return; }
  if (target.dataset.conversationId) { await activateConversation(target.dataset.conversationId); return; }
  const action = target.dataset.action;
  if (action === "upload") document.querySelector<HTMLInputElement>("#asset-upload")!.click(); else if (action === "load-simstate") document.querySelector<HTMLInputElement>("#simstate-upload")!.click(); else if (action === "import-remote") { const url = prompt("Remote asset URL:"); if (url?.trim()) void importRemoteAsset(url.trim()); } else if (action === "focus") renderer.frame(state.selectedId || undefined); else if (action === "frame") renderer.frame(); else if (action === "delete-entity" && state.selectedId !== 0 && confirm(`Delete ${selectedEntity()?.name}?`)) await sendCommand("scene.delete_entity", { entity: state.selectedId }); else if (action === "open-preview") updatePreview(true); else if (action === "close-preview") { document.querySelector("#camera-preview")!.classList.add("hidden"); clearInterval(previewTimer); } else if (action === "toggle-diagnostics") { document.querySelector("#diagnostics")!.classList.toggle("open"); if (document.querySelector("#diagnostics")!.classList.contains("open")) await refreshDiagnostics(); } else if (action === "new-conversation") await createConversation(); else if (action === "toggle-history") await toggleConversationHistory(); else if (action === "toggle-operations") document.querySelector("#operations-content")!.classList.toggle("open"); else if (action === "speech") toggleSpeech(); else if (action === "send-joints") { const entity = selectedEntity(); if (!entity?.robot) return; const form = new FormData(document.querySelector<HTMLFormElement>("#inspector-form")!); await sendCommand("robot.command_joint_positions", { robot: entity.id, targets: entity.robot.jointNames.map((_, i) => formNumberOr(form, `joint-${i}`, entity.robot!.positions[i] ?? 0)) }); }
});
app.addEventListener("submit", (event) => { event.preventDefault(); const form = event.target as HTMLFormElement; if (form.id === "inspector-form" && enabled("panels.details.apply")) void applyInspector(form); else if (form.id === "backend-form" && enabled("panels.settings.apply")) void sendCommand("backend.apply", Object.fromEntries(new FormData(form).entries())); else if (form.id === "agent-form" && enabled("agent.input.send")) { const input = document.querySelector<HTMLTextAreaElement>("#agent-input")!; void submitAgent(input.value); input.value = ""; } });
app.addEventListener("change", (event) => { const target = event.target as HTMLInputElement | HTMLSelectElement; if (target.id === "timeline-rate") { state.timelineRate = Number(target.value); if (state.timelinePlaying && state.timeline) { timelineAnchorWallMs = performance.now(); timelineAnchorSimulationTime = state.timeline.frames[state.timelineIndex]!.simulationTime; scheduleTimeline(); } } else if (target.id === "create-kind" && target.value) { const value = target.value; void (value.startsWith("light-") ? sendCommand("scene.create_light", { parent: state.selectedId || 0, type: value.slice(6) }) : createAsset(`builtin:${value}`)); target.value = ""; } else if (target.id === "collider-toggle" || target.id === "ifps-toggle") { state.collider = enabled("viewport.overlays.collider") && document.querySelector<HTMLInputElement>("#collider-toggle")!.checked; state.ifps = enabled("viewport.overlays.ifps") && document.querySelector<HTMLInputElement>("#ifps-toggle")!.checked; renderer.setOverlays(state.collider, state.ifps); if (state.snapshot) renderer.syncScene(state.snapshot); } else if (target.id === "rtf" && enabled("commandBar.simulation.realTimeFactor")) void sendCommand("backend.apply", { realTimeFactor: Number(target.value) }); else if (target.id === "asset-upload" && (target as HTMLInputElement).files?.[0]) void prepareAssetEntry(Array.from((target as HTMLInputElement).files!)); else if (target.id === "simstate-upload" && (target as HTMLInputElement).files?.[0]) { void loadSimstate((target as HTMLInputElement).files![0]!); (target as HTMLInputElement).value = ""; } else if (target.id === "asset-dependencies" && (target as HTMLInputElement).files?.[0]) void prepareDependencyBundle(Array.from((target as HTMLInputElement).files!)); });
app.addEventListener("input", (event) => { const target = event.target as HTMLInputElement; if (target.id === "timeline-range") { state.timelinePlaying = false; void showTimelineFrame(Number(target.value)); } if (target.id === "tree-search") document.querySelectorAll<HTMLElement>(".tree-row").forEach((row) => { row.hidden = !row.textContent!.toLowerCase().includes(target.value.toLowerCase()); }); if (target.id === "asset-search") document.querySelectorAll<HTMLElement>(".asset-row").forEach((row) => { row.hidden = !row.textContent!.toLowerCase().includes(target.value.toLowerCase()); }); });
app.addEventListener("dragstart", (event) => { const target = event.target as HTMLElement; if (target.dataset.entity && enabled("panels.scene.dragReparent")) event.dataTransfer?.setData("application/x-entity", target.dataset.entity); if (target.dataset.asset && enabled("panels.assets.create")) event.dataTransfer?.setData("application/x-asset", target.dataset.asset); });
app.addEventListener("dragover", (event) => { if ((event.target as HTMLElement).closest(".tree-row") || enabled("viewport.assetDrop") && (event.target as HTMLElement).closest("#drop-zone")) event.preventDefault(); });
app.addEventListener("drop", (event) => { event.preventDefault(); const tree = (event.target as HTMLElement).closest<HTMLElement>(".tree-row"), entity = event.dataTransfer?.getData("application/x-entity"), asset = event.dataTransfer?.getData("application/x-asset"); if (entity && tree && enabled("panels.scene.dragReparent")) void sendCommand("scene.set_parent", { entity: Number(entity), parent: Number(tree.dataset.entity) }); else if (asset && enabled("panels.assets.create") && enabled("viewport.assetDrop")) void createAsset(asset, [0, 0.5, 0]); });
document.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", () => { const mode = (button as HTMLElement).dataset.preview as "rgb" | "depth"; if (!enabled(`viewport.cameraPreview.modes.${mode}`)) return; state.previewMode = mode; document.querySelectorAll("[data-preview]").forEach((item) => item.classList.toggle("active", item === button)); loadPreview(); }));

type UploadFile = { path: string; file: File };
let pendingAssetEntry: File | null = null;

function selectedPath(file: File): string {
  const relative = file.webkitRelativePath.replace(/\\/g, "/");
  return relative.includes("/") ? relative.slice(relative.indexOf("/") + 1) : file.name;
}

async function urdfDependencyReferences(file: File): Promise<string[]> {
  const documentNode = new DOMParser().parseFromString(await file.text(), "application/xml");
  const parseError = documentNode.querySelector("parsererror");
  if (parseError) throw new Error(`URDF XML 无法解析：${parseError.textContent?.trim() ?? "未知错误"}`);
  const dependencies: string[] = [];
  documentNode.querySelectorAll("mesh[filename], texture[filename]").forEach((element) => {
    const value = element.getAttribute("filename");
    if (!value) return;
    if (!dependencies.includes(value)) dependencies.push(value);
  });
  return dependencies;
}

async function objDependencyReferences(file: File): Promise<string[]> {
  const dependencies: string[] = [];
  for (const line of (await file.text()).split(/\r?\n/)) {
    const match = /^\s*mtllib\s+(.+?)\s*$/i.exec(line);
    if (match?.[1] && !dependencies.includes(match[1])) dependencies.push(match[1]);
  }
  return dependencies;
}

function assetDependencyReferences(file: File): Promise<string[]> {
  return file.name.toLowerCase().endsWith(".obj") ? objDependencyReferences(file) : urdfDependencyReferences(file);
}

async function urdfDependencies(entry: UploadFile): Promise<string[]> {
  return (await urdfDependencyReferences(entry.file)).map((value) => resolveAssetDependency(entry.path, value));
}

function dependencyModalMode(mode: "folder" | "confirm"): void {
  document.querySelector<HTMLButtonElement>("#dependency-cancel")!.textContent = mode === "folder" ? "取消" : "否";
  document.querySelector<HTMLButtonElement>("#dependency-select")!.hidden = mode !== "folder" || !enabled("assetDependencies.selectFolder");
  document.querySelector<HTMLButtonElement>("#dependency-confirm")!.hidden = mode !== "confirm" || !enabled("assetDependencies.confirm");
}

function requestDependencyFolder(name: string, dependencies: string[]): void {
  if (!enabled("assetDependencies") || !enabled("assetDependencies.selectFolder")) { pendingAssetEntry = null; showError("Asset dependency folder selection is disabled by feature configuration"); return; }
  const modal = document.querySelector<HTMLElement>("#dependency-modal")!;
  document.querySelector("#dependency-message")!.textContent = `发现了 ${name} 的以下依赖项：`;
  document.querySelector("#dependency-list")!.innerHTML = dependencies.map((path) => `<li>${escapeHtml(path)}</li>`).join("");
  modal.querySelector<HTMLElement>(".dependency-question")!.textContent = "请选择一个同时包含入口 URDF 与上述依赖项的模型目录。";
  dependencyModalMode("folder");
  modal.classList.remove("hidden");
  const cancel = () => { modal.classList.add("hidden"); pendingAssetEntry = null; };
  document.querySelector<HTMLButtonElement>("#dependency-select")!.onclick = () => {
    modal.classList.add("hidden");
    document.querySelector<HTMLInputElement>("#asset-dependencies")!.click();
  };
  document.querySelector<HTMLButtonElement>("#dependency-cancel")!.onclick = cancel;
  document.querySelector<HTMLButtonElement>("#dependency-close")!.onclick = cancel;
}

function confirmDependencies(name: string, dependencies: string[]): Promise<boolean> {
  if (!enabled("assetDependencies") || !enabled("assetDependencies.confirm")) return Promise.resolve(false);
  const modal = document.querySelector<HTMLElement>("#dependency-modal")!;
  document.querySelector("#dependency-message")!.textContent = `发现了 ${name} 的以下依赖项：`;
  document.querySelector("#dependency-list")!.innerHTML = dependencies.map((path) => `<li>${escapeHtml(path)}</li>`).join("");
  modal.querySelector<HTMLElement>(".dependency-question")!.textContent = "将会上传全部依赖项，是否继续？";
  dependencyModalMode("confirm");
  modal.classList.remove("hidden");
  return new Promise((resolve) => {
    const finish = (accepted: boolean) => { modal.classList.add("hidden"); resolve(accepted); };
    document.querySelector<HTMLButtonElement>("#dependency-confirm")!.onclick = () => finish(true);
    document.querySelector<HTMLButtonElement>("#dependency-cancel")!.onclick = () => finish(false);
    document.querySelector<HTMLButtonElement>("#dependency-close")!.onclick = () => finish(false);
  });
}

async function prepareAssetEntry(files: File[]): Promise<void> {
  const input = document.querySelector<HTMLInputElement>("#asset-upload")!;
  try {
    const urdfs = files.filter((file) => file.name.toLowerCase().endsWith(".urdf"));
    if (urdfs.length > 1) throw new Error("一次只能选择一个入口 URDF");
    const meshes = files.filter((item) => /\.(obj|fbx|stl)$/i.test(item.name));
    if (!urdfs.length && meshes.length > 1) throw new Error("一次只能选择一个入口网格文件");
    const file = urdfs[0] ?? meshes[0];
    if (!file) throw new Error("请选择一个 URDF、OBJ、FBX 或 STL 文件");
    const entry = { path: file.name, file };
    if (file.name.toLowerCase().endsWith(".obj")) {
      // OBJ materials and textures are relative to the OBJ; preserve every selected
      // path so the gateway can validate and rewrite exporter absolute references.
      const selected = files.map((item) => ({ path: selectedPath(item), file: item }));
      const references = await objDependencyReferences(file);
      if (!references.length) { await uploadAsset(entry, [entry]); return; }
      const selectedNames = new Set(selected.map((item) => item.file.name.toLowerCase()));
      const missing = references.filter((reference) => !selectedNames.has(pathBasename(reference).toLowerCase()));
      if (missing.length) {
        pendingAssetEntry = file;
        requestDependencyFolder(file.name, references);
      } else if (await confirmDependencies(file.name, references)) {
        await uploadAsset(entry, selected);
      }
      return;
    }
    if (!file.name.toLowerCase().endsWith(".urdf")) {
      await uploadAsset(entry, [entry]);
      return;
    }
    const references = await urdfDependencyReferences(file);
    if (!references.length) { await uploadAsset(entry, [entry]); return; }

    const selected = files.map((item) => ({ path: selectedPath(item), file: item }));
    const autoBundle: UploadFile[] = [entry];
    const missing: string[] = [];
    for (const reference of references) {
      let expected: string;
      try { expected = resolveAssetDependency(entry.path, reference); } catch { missing.push(reference); continue; }
      const exact = selected.filter((item) => item.path.toLowerCase() === expected.toLowerCase());
      const basename = selected.filter((item) => item.file.name.toLowerCase() === pathBasename(reference).toLowerCase());
      const candidates = exact.length === 1 ? exact : exact.length > 1 ? exact : basename;
      if (candidates.length !== 1) missing.push(reference);
      else autoBundle.push({ path: expected, file: candidates[0]!.file });
    }
    if (!missing.length) {
      if (await confirmDependencies(file.name, references)) await uploadAsset(entry, autoBundle);
      return;
    }
    pendingAssetEntry = file;
    requestDependencyFolder(file.name, references);
  } catch (error) {
    showError((error as Error).message);
  } finally {
    input.value = "";
  }
}

function pathBasename(value: string): string { return value.replace(/\\/g, "/").split("/").pop() ?? value; }

async function prepareDependencyBundle(files: File[]): Promise<void> {
  const input = document.querySelector<HTMLInputElement>("#asset-dependencies")!;
  const original = pendingAssetEntry;
  if (!original) { input.value = ""; return; }
  try {
    const selected = files.map((file) => ({ path: selectedPath(file), file }));
    let candidates = selected.filter((item) => item.file.name.toLowerCase() === original.name.toLowerCase());
    if (candidates.length > 1) candidates = candidates.filter((item) => item.file.size === original.size);
    if (candidates.length !== 1) throw new Error(`所选目录中未能唯一找到入口文件 ${original.name}，请改选模型的共同根目录`);
    const entry = candidates[0]!;
    if (entry.file.name.toLowerCase().endsWith(".obj")) {
      const references = await objDependencyReferences(entry.file);
      if (references.length && !await confirmDependencies(entry.file.name, references)) { pendingAssetEntry = null; return; }
      await uploadAsset(entry, selected);
      pendingAssetEntry = null;
      return;
    }
    const dependencies = await urdfDependencies(entry);
    const byPath = new Map(selected.map((item) => [item.path.toLowerCase(), item]));
    const missing = dependencies.filter((path) => !byPath.has(path.toLowerCase()));
    if (missing.length) throw new Error(`所选目录仍缺少依赖资源：${missing.join(", ")}。请选择包含这些路径的共同根目录`);
    if (dependencies.length && !await confirmDependencies(entry.file.name, dependencies)) { pendingAssetEntry = null; return; }
    const bundle = [entry, ...dependencies.map((path) => byPath.get(path.toLowerCase())!)];
    await uploadAsset(entry, bundle);
    pendingAssetEntry = null;
  } catch (error) {
    showError((error as Error).message);
    requestDependencyFolder(original.name, await assetDependencyReferences(original));
  } finally {
    input.value = "";
  }
}

async function uploadAsset(entry: UploadFile, files: UploadFile[]): Promise<void> {
  const form = new FormData();
  form.append("entry", entry.path);
  for (const item of files) { form.append("path", item.path); form.append("files", item.file, item.file.name); }
  await json("/api/assets/upload", { method: "POST", body: form });
  await refreshAssets();
  showError(`${entry.file.name} uploaded`);
}
async function importRemoteAsset(url: string): Promise<void> {
  try {
    state.busy = true; updateCommandState();
    const result = await json<{ asset: AssetSummary }>("/api/assets/import-remote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    await refreshAssets();
    await createAsset(result.asset.id);
  } catch (error) { showError((error as Error).message); }
  finally { state.busy = false; updateCommandState(); }
}
async function loadSimstate(file: File): Promise<void> {
  try {
    state.busy = true; updateCommandState();
    const form = new FormData();
    form.append("file", file, file.name);
    await json("/api/state/load-simstate", { method: "POST", body: form });
    await refreshSnapshot();
    await refreshAssets();
    showError(`Simstate loaded: ${file.name}`);
  } catch (error) { showError((error as Error).message); }
  finally { state.busy = false; updateCommandState(); }
}
window.addEventListener("beforeunload", () => renderer.dispose());
async function start(): Promise<void> { drawIcons(); try { await Promise.all([refreshSnapshot(), refreshAssets()]); const active = (await json<{ activeConversationId: string | null }>("/api/agent/conversations")).activeConversationId; if (active) { const detail = await json<ConversationDetail>(`/api/agent/conversations/${encodeURIComponent(active)}`); applyConversation(detail); } else { state.messages = []; state.agentDebug = []; state.agentPlan = []; state.agentPlanKey = null; updateAgent(); updateAgentDebug(); } connectWebSocket(); } catch (error) { console.error("WebEditor startup failed", error); state.websocket = "offline"; updateStatus(); showError(`Web Gateway unavailable: ${(error as Error).message}`); setTimeout(start, 2000); } }
void start();
