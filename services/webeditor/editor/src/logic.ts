import type { CameraProps, FeatureFlags, LocalAxesProps, PixelBox, RuntimeConfig, Vec3 } from "../../contracts/types";

export function agentDetailsDisabled(runtime: Pick<RuntimeConfig, "disable_agent_details"> | null | undefined): boolean {
  return runtime?.disable_agent_details === true;
}
export type AgentPlanItem = { step: string; status: "completed" | "in_progress" | "pending" | "failed" };

/** §7.3/Q97: what a missing or unusable `ghost_pose_opacity` reads as. */
export const DEFAULT_GHOST_OPACITY = 0.5;

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

/**
 * How opaque the segment-end ghosts are (§7.3, Q97): a finite number in [0,1] as
 * configured, and the default for everything else. Fail-open on purpose -- unlike
 * Q81's boolean, an unknown key here means "nobody tuned it", which should still
 * show the ghost rather than hide a feature the flag already said is on.
 */
export function ghostOpacity(runtime: Pick<RuntimeConfig, "ghost_pose_opacity"> | null | undefined): number {
  const value = runtime?.ghost_pose_opacity;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value : DEFAULT_GHOST_OPACITY;
}

/** §7.3/Q101: what a missing or unusable `traj_waypoint_radius` reads as, in world metres. */
export const DEFAULT_WAYPOINT_RADIUS = 0.01;
/** §7.3/Q101: what a missing or unusable `traj_waypoint_stride_frames` reads as. */
export const DEFAULT_WAYPOINT_STRIDE_FRAMES = 10;

/**
 * §7.3/Q101: the colours the waypoint layers cycle through, one per trajectory index.
 * Hardcoded on purpose - the requirement is "two clips read as two paths at a glance",
 * and a configurable palette is how you end up with two blues.
 */
export const WAYPOINT_COLORS: readonly number[] = [0xff5d5d, 0x4dd2ff, 0xffd166, 0x9b5de5, 0x00d68f, 0xff8fab];

export interface WaypointSettings {
  radius: number;
  strideFrames: number;
}

/**
 * How big and how dense the waypoint spheres are (§7.3, Q101). Fail-open like
 * `ghostOpacity()`: a radius of 0 would collapse every instance matrix and a stride of 0
 * would never advance the sampling loop, so neither is a value to honour - both mean
 * "nobody tuned it" and fall back to the default.
 */
export function waypointSettings(
  runtime: Pick<RuntimeConfig, "traj_waypoint_radius" | "traj_waypoint_stride_frames"> | null | undefined,
): WaypointSettings {
  const radius = runtime?.traj_waypoint_radius;
  const stride = runtime?.traj_waypoint_stride_frames;
  return {
    radius: typeof radius === "number" && Number.isFinite(radius) && radius > 0 ? radius : DEFAULT_WAYPOINT_RADIUS,
    strideFrames: typeof stride === "number" && Number.isInteger(stride) && stride > 0 ? stride : DEFAULT_WAYPOINT_STRIDE_FRAMES,
  };
}

/** The colour of one trajectory's waypoints (§7.3, Q101), cycled so any batch size works. */
export function waypointColor(index: number): number {
  const count = WAYPOINT_COLORS.length;
  const wanted = Number.isInteger(index) ? ((index % count) + count) % count : 0;
  return WAYPOINT_COLORS[wanted]!;
}

/**
 * Which frames of a clip become waypoints (§7.3, Q101): every `strideFrames`-th from frame 0,
 * plus the last frame when the stride did not land on it - the pose a clip ends in is the one
 * worth a marker most, and it is the one an even stride would skip.
 */
export function waypointFrames(totalFrames: number, strideFrames: number): number[] {
  if (!Number.isInteger(totalFrames) || totalFrames < 1 || !Number.isInteger(strideFrames) || strideFrames < 1) return [];
  const last = totalFrames - 1;
  const frames: number[] = [];
  for (let frame = 0; frame <= last; frame += strideFrames) frames.push(frame);
  if (frames[frames.length - 1] !== last) frames.push(last);
  return frames;
}

/**
 * Which of the world's `RobotDriver` objects are leaves of their arm chain (§7.3, Q101):
 * an arm another driver hangs from traces the same path twice, so only the end of each
 * chain is drawn. `parentOf` is the caller's route to `Transform.parent` /
 * `attachmentMount.objectId`, and a cycle stops the walk rather than spinning on it.
 */
export function leafArmIds(objectIds: readonly number[], parentOf: (objectId: number) => number | null): number[] {
  const drivers = new Set(objectIds);
  const hasDriverBelow = new Set<number>();
  for (const objectId of objectIds) {
    const visited = new Set<number>([objectId]);
    let current = parentOf(objectId);
    while (current !== null && !visited.has(current)) {
      if (drivers.has(current)) hasDriverBelow.add(current);
      visited.add(current);
      current = parentOf(current);
    }
  }
  return objectIds.filter((objectId) => !hasDriverBelow.has(objectId));
}

export function mergeSpeech(prefix: string, finalText: string, interimText: string): string {
  return `${prefix}${finalText}${interimText}`;
}

/**
 * §7.6 (**Q129**): which Enter inside the Agent input box counts as the send button. The key is an
 * *alias* of that button, so `agent.input.send` being off means the caller sends nothing and also
 * swallows nothing -- the browser keeps its newline. The other four negatives are the same argument
 * one level down: `shiftKey` because the box is a `<textarea rows="3">` and multi-line is a
 * capability, `ctrl/meta/alt` because modified keys belong to the browser and the OS, `repeat`
 * because one click never repeats and a held Enter must not become a queue of tasks, and
 * `isComposing` because with a Chinese prompt that press commits an IME candidate -- sending it
 * would post a half-written sentence, which is the one way this feature could do harm.
 */
export function enterSendsAgentMessage(features: FeatureFlags, key: Pick<KeyboardEvent,
  "key" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey" | "isComposing" | "repeat">): boolean {
  return featureEnabled(features, "agent.input.send") && key.key === "Enter"
    && !key.shiftKey && !key.ctrlKey && !key.metaKey && !key.altKey
    && !key.isComposing && !key.repeat;
}

export function timelineDueIndex(times: number[], currentIndex: number,
                                 targetSimulationTime: number): number {
  let index = Math.max(0, Math.min(currentIndex, times.length - 1));
  while (index + 1 < times.length && times[index + 1]! <= targetSimulationTime) index += 1;
  return index;
}

/**
 * A `Camera` component with every optional field resolved (§2.6, §3.8):
 * `fov` is degrees and vertical, `near`/`far` are metres, `resolution` is [width, height].
 */
export interface CameraRig {
  verticalFovDegrees: number;
  width: number;
  height: number;
  nearPlane: number;
  farPlane: number;
  /** a depth map exists only for `rgbd` cameras (§4.4) */
  depth: boolean;
}

const CAMERA_DEFAULTS = { fov: 60, near: 0.1, far: 1000, width: 640, height: 480 };

function positiveNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

export function cameraRig(camera: CameraProps | undefined): CameraRig {
  const near = positiveNumber(camera?.near, CAMERA_DEFAULTS.near);
  return {
    verticalFovDegrees: positiveNumber(camera?.fov, CAMERA_DEFAULTS.fov),
    width: positiveNumber(camera?.resolution?.[0], CAMERA_DEFAULTS.width),
    height: positiveNumber(camera?.resolution?.[1], CAMERA_DEFAULTS.height),
    nearPlane: near,
    // A `far` that does not clear `near` would give a degenerate frustum.
    farPlane: Math.max(positiveNumber(camera?.far, CAMERA_DEFAULTS.far), near * 10),
    depth: camera?.type === "rgbd",
  };
}

export function cameraFrustumSegments(camera: CameraRig): number[] {
  const tangent = Math.tan(camera.verticalFovDegrees * Math.PI / 360);
  const aspect = camera.width / camera.height;
  const corners = (depth: number): Vec3[] => {
    const halfHeight = tangent * depth;
    const halfWidth = halfHeight * aspect;
    // Optical frame (§4.4, Q74): +Z forward, +X left, +Y up. The pyramid is symmetric,
    // so the four corners still walk TL -> TR -> BR -> BL of the captured image.
    return [
      [halfWidth, halfHeight, depth], [-halfWidth, halfHeight, depth],
      [-halfWidth, -halfHeight, depth], [halfWidth, -halfHeight, depth],
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

/** §4.9 (Q130): the length a `LocalAxes` axis gets when the file does not give one. */
export const DEFAULT_LOCAL_AXES_LENGTH = 0.1;

const LOCAL_AXIS_DEFAULT_COLORS = { x: "#FF0000", y: "#00FF00", z: "#0000FF" } as const;

/** The same three literal forms `Light.color` accepts (§4.5): `#RGB`, `#RRGGBB`, `#RRGGBBAA`. */
const HEX_LITERAL = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

export interface LocalAxisStyle {
  /** always canonical `#RRGGBB`; three parses neither the short nor the 8-digit form */
  color: string;
  /** the `AA` of an 8-digit literal, one otherwise */
  opacity: number;
}

export interface LocalAxesRig {
  length: number;
  colors: { x: LocalAxisStyle; y: LocalAxisStyle; z: LocalAxisStyle };
  /** only a literal `true` shows the tip letters (§4.9) */
  showLabels: boolean;
  /** metres, offset from the object's local origin to the axes' origin (§4.9) */
  offset: Vec3;
  warnings: string[];
}

/** A hex colour literal into the line colour and the alpha it carries, or null when it is not one. */
function axisStyle(raw: unknown): LocalAxisStyle | null {
  const match = typeof raw === "string" ? HEX_LITERAL.exec(raw.trim()) : null;
  if (!match) return null;
  const digits = match[1]!;
  const expanded = digits.length === 3 ? digits.replace(/./g, (char) => char + char) : digits;
  return {
    color: `#${expanded.slice(0, 6).toUpperCase()}`,
    opacity: expanded.length === 8 ? Number.parseInt(expanded.slice(6, 8), 16) / 255 : 1,
  };
}

/**
 * How one `LocalAxes` block reads (§4.9, **Q130**) - pure, so the parser that routes the
 * warnings into its own `warn` sink and the renderer that draws the axes ask the same
 * question and get the same answer. Nothing here touches Three.js.
 */
export function localAxesRig(props: LocalAxesProps | undefined): LocalAxesRig {
  const warnings: string[] = [];
  const rawLength = props?.length;
  const length = positiveNumber(rawLength, DEFAULT_LOCAL_AXES_LENGTH);
  if (rawLength !== undefined && rawLength !== length) {
    warnings.push(`LocalAxes length ${JSON.stringify(rawLength)} is not a positive number, using ${DEFAULT_LOCAL_AXES_LENGTH}`);
  }
  const rawColors = props?.colors;
  // `null` reads as "not an object" here, which is what the file author means by it.
  if (rawColors !== undefined && (rawColors === null || typeof rawColors !== "object")) {
    warnings.push(`LocalAxes colors ${JSON.stringify(rawColors)} is not an object, using the default red/green/blue`);
  }
  const colors = {} as LocalAxesRig["colors"];
  for (const axis of ["x", "y", "z"] as const) {
    // A key that is simply absent falls back in silence; §3 says a missing field is not an error.
    const raw = rawColors?.[axis];
    const style = axisStyle(raw);
    colors[axis] = style ?? axisStyle(LOCAL_AXIS_DEFAULT_COLORS[axis])!;
    if (!style && raw !== undefined) {
      warnings.push(`LocalAxes colors.${axis} ${JSON.stringify(raw)} is not a hex colour, using ${LOCAL_AXIS_DEFAULT_COLORS[axis]}`);
    }
  }
  const rawOffset = props?.offset;
  // §4.9: offset is a Vec3, [x, y, z] in metres. Absent falls back in silence (§3).
  // A wrong shape or a non-finite component warns and falls back to [0,0,0].
  const offset: Vec3 = [0, 0, 0];
  if (rawOffset !== undefined) {
    if (!Array.isArray(rawOffset) || rawOffset.length !== 3) {
      warnings.push(`LocalAxes offset ${JSON.stringify(rawOffset)} is not a 3-element array, using [0,0,0]`);
    } else {
      for (let i = 0; i < 3; i++) {
        const value = rawOffset[i];
        if (typeof value === "number" && Number.isFinite(value)) {
          offset[i] = value;
        } else {
          warnings.push(`LocalAxes offset[${i}] ${JSON.stringify(value)} is not a finite number, using [0,0,0]`);
          offset[0] = 0; offset[1] = 0; offset[2] = 0;
          break;
        }
      }
    }
  }
  return {
    length,
    colors,
    showLabels: props?.show_labels === true,
    offset,
    warnings,
  };
}

/** What `/get2DAABB` reports about one projected AABB (§5.11). */
export interface ProjectedBox {
  visible: boolean;
  clipped: boolean;
  box: PixelBox | null;
}

/**
 * Eight AABB corners through a column-major view-projection matrix into the pixel
 * rectangle of a `width`×`height` image, origin top left (§5.11).
 *
 * A corner with `w <= 0` sits at or behind the camera: dividing would mirror it onto
 * the opposite side of the frame and inflate the rectangle across the whole image, so
 * such corners are dropped rather than clamped. Dropping them is also why a box that
 * straddles the near plane is under-estimated — §5.11 prescribes exactly this.
 */
export function projectBox(corners: readonly number[], viewProjection: readonly number[],
                           width: number, height: number): ProjectedBox {
  const offscreen: ProjectedBox = { visible: false, clipped: true, box: null };
  if (!(width > 0) || !(height > 0)) return offscreen;
  let xMin = Number.POSITIVE_INFINITY;
  let yMin = Number.POSITIVE_INFINITY;
  let xMax = Number.NEGATIVE_INFINITY;
  let yMax = Number.NEGATIVE_INFINITY;
  for (let index = 0; index + 2 < corners.length; index += 3) {
    const x = corners[index]!;
    const y = corners[index + 1]!;
    const z = corners[index + 2]!;
    const m = viewProjection;
    const w = m[3]! * x + m[7]! * y + m[11]! * z + m[15]!;
    if (!(w > 0)) continue;
    const axis = (row: number) => (m[row]! * x + m[row + 4]! * y + m[row + 8]! * z + m[row + 12]!) / w;
    const px = (axis(0) + 1) * width / 2;
    // NDC y grows upwards, §5.11 counts pixels downwards from the top edge.
    const py = (1 - axis(1)) * height / 2;
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    xMin = Math.min(xMin, px);
    xMax = Math.max(xMax, px);
    yMin = Math.min(yMin, py);
    yMax = Math.max(yMax, py);
  }
  if (!Number.isFinite(xMin)) return offscreen;
  if (xMin >= 0 && yMin >= 0 && xMax <= width && yMax <= height) {
    return { visible: true, clipped: false, box: { x_min: xMin, y_min: yMin, x_max: xMax, y_max: yMax } };
  }
  // Partially out of frame: keep the intersection, and a zero-area one is not visible.
  const clippedXMin = Math.max(0, Math.min(xMin, width));
  const clippedXMax = Math.min(width, Math.max(xMax, 0));
  const clippedYMin = Math.max(0, Math.min(yMin, height));
  const clippedYMax = Math.min(height, Math.max(yMax, 0));
  if (clippedXMax <= clippedXMin || clippedYMax <= clippedYMin) return offscreen;
  return {
    visible: true,
    clipped: true,
    box: { x_min: clippedXMin, y_min: clippedYMin, x_max: clippedXMax, y_max: clippedYMax },
  };
}

/** §4.8.5/Q118: what a missing or unusable `shadows_enabled` reads as -- the look §4.8.5 promises. */
export const DEFAULT_SHADOWS_ENABLED = true;

/**
 * Whether the stage projects shadows (§4.8.5, **Q118**). Three sources answer, and the feature
 * flag is not one of them: `viewport.shadows` only says whether the toolbar checkbox is shown.
 * With the checkbox shown, its own state decides, and a held scene lease pins it to on because
 * 采集像素要可重复; with the checkbox hidden, `config/constant_config.json`'s `shadows_enabled`
 * decides -- and there is nothing to freeze, since every tab reads the same startup value and
 * forcing it on would quietly undo an operator's deliberate "off".
 *
 * The flag is fail-closed like every other key (§9.3, Q81); the constant is fail-open like the
 * numeric ones on this table (Q97/Q101), because a shadow is class ③ and reads "nobody tuned it".
 */
export function shadowsEnabled(
  features: FeatureFlags,
  runtime: Pick<RuntimeConfig, "shadows_enabled"> | null | undefined,
  checked: boolean,
  frozen: boolean,
): boolean {
  if (!featureEnabled(features, "viewport.shadows")) {
    const value = runtime?.shadows_enabled;
    return typeof value === "boolean" ? value : DEFAULT_SHADOWS_ENABLED;
  }
  return frozen || checked;
}

/** §4.4/Q145: what a missing or unusable `viewport_background_color` clears to -- black, the look the renderer was built with. */
export const DEFAULT_VIEWPORT_BACKGROUND = "#000000";

/**
 * The colour the viewport clears to (§4.4, **Q145**): a CSS hex string, `#RGB` or `#RRGGBB`.
 * Fail-open like every other constant on this table (Q97/Q101/Q118) -- a value that is absent,
 * not a string, or not one of those two shapes reads as "nobody tuned it" and falls back to
 * black rather than rejecting the render. `THREE.Color` parses both accepted shapes, so the
 * validated string goes straight to the renderer; the renderer never reads the config itself
 * (the Q96/Q105 division of labour).
 */
export function viewportBackground(
  runtime: Pick<RuntimeConfig, "viewport_background_color"> | null | undefined,
): string {
  const value = runtime?.viewport_background_color;
  if (typeof value !== "string") return DEFAULT_VIEWPORT_BACKGROUND;
  const trimmed = value.trim();
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed) ? trimmed : DEFAULT_VIEWPORT_BACKGROUND;
}

/**
 * Whether the wireframe layer shows every object's box (§4.8.6, **Q119**).
 *
 * One switch, unlike the shadow stage: `viewport.boundingBoxes` decides whether the checkbox is
 * there, and the checkbox decides whether the boxes are drawn. There is no constant fallback here
 * because this layer's default look is *no boxes* -- hiding the host and leaving it unchecked are
 * the same picture, so a key that only owns the host cannot silently undo anybody's decision the
 * way Q118 found `viewport.shadows` doing to the shadows. An absent key reads as off, like every
 * other key on the table (§9.3).
 */
export function boundingBoxesEnabled(features: FeatureFlags, checked: boolean): boolean {
  return featureEnabled(features, "viewport.boundingBoxes") && checked;
}

/** §4.8.6/Q149/Q150: malformed `urdf_bounding_box_per_link` keeps the compact per-link display. */
export const DEFAULT_URDF_LINK_BOXES = true;

/**
 * Whether a URDF object's wireframe is cut open into one box per link (§4.8.6, **Q149**). This sits
 * *under* `viewport.boundingBoxes` rather than beside it: the feature key and the checkbox still
 * decide whether the layer draws at all, and this constant only decides in which shape -- so the
 * remote exit for the whole layer stays the one key Q119 left it, and an operator who never turns
 * the boxes on sees nothing new. Fail-open like every other constant on §9.1's table: absent,
 * `null` or a string reads as "nobody tuned it", and the untuned picture is the single box that
 * an object's links union into.
 */
export function urdfLinkBoxesEnabled(
  runtime: Pick<RuntimeConfig, "urdf_bounding_box_per_link"> | null | undefined,
): boolean {
  const value = runtime?.urdf_bounding_box_per_link;
  return typeof value === "boolean" ? value : DEFAULT_URDF_LINK_BOXES;
}

/**
 * Whether the local-frame axes layer shows (§4.9 with §4.8.8, **Q132**).
 *
 * The same two-line shape as the wireframe above it: `viewport.localAxesToggle` decides whether the
 * checkbox is on the toolbar, the checkbox decides whether the axes are drawn - so with the key off
 * the host is gone *and* the layer is down, which keeps that key the one remote exit for the whole
 * layer (§4.9). It started life as the layer's only switch (**Q130**); the name says what it owns now.
 * No constant fallback, for the reason Q119 gave: the layer's default look is *no axes*, so a host
 * that never arrives is the same picture as an unchecked box, and nobody's declared scene is undone.
 */
export function localAxesEnabled(features: FeatureFlags, checked: boolean): boolean {
  return featureEnabled(features, "viewport.localAxesToggle") && checked;
}

/**
 * §7.2 (**Q136**): the panel is rebuilt from a string whenever anything changes, and writing that
 * string re-creates the list - which puts its scroll offset back to 0 and reloads every thumbnail.
 * Clicking an entry changes the *world*, not the list, so when the markup about to be written is the
 * one already on screen there is nothing to write and the list stays exactly where the user left it.
 * `previous` is `null` on the first paint, where nothing is on screen yet.
 */
export function panelMarkupChanged(previous: string | null, next: string): boolean {
  return previous !== next;
}

/** Which rail tab is pressed, and what the panel is showing. `kind` names one 大类 of the asset view. */
export interface PanelTabState { panel: string; open: boolean; kind: string }

/**
 * §7.2 (**Q156**): a rail press toggles the tab it names. The three asset buttons all name the
 * `assets` panel and differ only by `kind`, so "the tab I am already on" has to include the 大类 -
 * otherwise switching from 预设场景 to 机械臂 would read as a second press on the same tab and
 * close the panel the user just asked to look at.
 */
export function panelTabPress(current: PanelTabState, panel: string, kind?: string): { open: boolean; kind: string } {
  const same = current.panel === panel && (kind === undefined || kind === current.kind);
  const open = !same || !current.open;
  return { open, kind: open && kind !== undefined ? kind : current.kind };
}

/**
 * §7.2 (**Q156**): the same judgement read the other way, for the `active` class. Without the `kind`
 * half all three asset buttons light up together, because they share one `data-panel`.
 */
export function panelTabActive(tab: { panel: string; kind?: string }, current: PanelTabState): boolean {
  return current.open && tab.panel === current.panel && (tab.kind === undefined || tab.kind === current.kind);
}

/**
 * §7.5 (**Q157**): the asset panel borrows the Agent column while it is open. `contextEnabled`
 * is part of the question because a switched-off context panel shows no assets either - collapsing
 * the Agent panel then would leave the workspace with two empty columns.
 */
export function agentPanelVisible(view: { agentEnabled: boolean; contextEnabled: boolean; panel: string; panelOpen: boolean }): boolean {
  const assetsShown = view.contextEnabled && view.panelOpen && view.panel === "assets";
  return view.agentEnabled && !assetsShown;
}

/**
 * Whether the camera wall shows (§7.1, **Q138**).
 *
 * The third copy of the shape Q119 found and Q132 kept: `viewport.cameraWall` decides whether the
 * checkbox is on the toolbar, the checkbox decides whether the wall is on the screen - so with the
 * key off the host is gone *and* the layer is down, which keeps that key the one remote exit for the
 * whole layer. No constant fallback, for the same reason the wireframe gave: the default look is an
 * empty viewport, so a host that never arrives is the same picture as an unchecked box.
 */
export function cameraWallEnabled(features: FeatureFlags, checked: boolean): boolean {
  return featureEnabled(features, "viewport.cameraWall") && checked;
}

/** How often, and how big: what one camera of the wall is asked to produce (**Q138**). */
export interface CameraWallPace {
  readonly maxWidth: number;
  readonly maxHeight: number;
  readonly intervalMs: number;
}

const CAMERA_WALL_CELL = { maxWidth: 256, maxHeight: 192 };
/** The wall is several sensors rendering at once, so it takes a slower beat than the single preview. */
const CAMERA_WALL_MAX_HZ = 12;

/**
 * §4.4 (**Q138**, cell size revised by **Q139**): the wall's ask, derived from the same
 * `camera_preview` caps the Details preview answers to - the caps are a ceiling, never a target, so
 * each number takes the *smaller* of the two. A wall shows every camera at once, so a cell is a
 * thumbnail rather than a frame worth measuring pixels in: `renderSensorFrame()` keeps the sensor's own
 * aspect ratio inside these bounds. **Q139** took the ceiling down from 320×240 because the pictures
 * were bigger than a wall of them is meant to be; the pace is untouched.
 */
export function cameraWallPace(caps: { maxWidth: number; maxHeight: number; maxHz: number }): CameraWallPace {
  return {
    maxWidth: Math.min(CAMERA_WALL_CELL.maxWidth, caps.maxWidth),
    maxHeight: Math.min(CAMERA_WALL_CELL.maxHeight, caps.maxHeight),
    intervalMs: 1000 / Math.min(Math.max(1, caps.maxHz), CAMERA_WALL_MAX_HZ),
  };
}

/** What one sensor frame can be shown as (§4.4, **Q139**). */
export type SensorViewMode = "rgb" | "depth";

/**
 * §7.1 (**Q139**): the mode the wall *shows*, given the one the user left it on.
 *
 * `viewport.cameraPreview.modes.depth` is the key that already owns depth previewing everywhere else
 * (§4.4), so the wall borrows it rather than minting a fourth host key - the layer keeps one remote exit
 * per capability, and this round adds no key at all. RGB needs no answer here: **Q138** put the wall's
 * RGB under `viewport.cameraWall` alone, and a wall that stopped drawing when a *preview* key closed
 * would give that layer two undocumented owners.
 *
 * Nothing is written back to `state`: with the key off the wall draws RGB, and with it on again the
 * 深度 the user asked for comes back by itself.
 */
export function cameraWallModeFor(features: FeatureFlags, requested: SensorViewMode): SensorViewMode {
  return requested === "depth" && !featureEnabled(features, "viewport.cameraPreview.modes.depth") ? "rgb" : requested;
}

/**
 * §4.4 (**Q139**): the pseudo-colour ramp's control stops, near → far - the blue-to-red family a depth
 * image is usually shown in. `t` is the same normalised view depth the grayscale pass already writes,
 * `clamp((viewDepth - near) / (far - near), 0, 1)`, so the two views agree about *what* is being measured
 * and differ only in how it is painted: near is blue where it used to be black, far red where it was white.
 */
const DEPTH_RAMP: ReadonlyArray<readonly [number, readonly [number, number, number]]> = [
  [0, [0, 0, 255]],
  [0.25, [0, 255, 255]],
  [0.5, [0, 255, 0]],
  [0.75, [255, 255, 0]],
  [1, [255, 0, 0]],
];

/** One sample of that ramp: linear between the stops, clamped outside them, whole bytes out. */
export function depthColor(t: number): readonly [number, number, number] {
  const value = Math.min(1, Math.max(0, t));
  for (let stop = 1; stop < DEPTH_RAMP.length; stop += 1) {
    const [at, colour] = DEPTH_RAMP[stop]!;
    if (value <= at) {
      const [from, start] = DEPTH_RAMP[stop - 1]!;
      const step = (value - from) / (at - from);
      return [Math.round(start[0] + (colour[0] - start[0]) * step),
        Math.round(start[1] + (colour[1] - start[1]) * step),
        Math.round(start[2] + (colour[2] - start[2]) * step)];
    }
  }
  return DEPTH_RAMP[DEPTH_RAMP.length - 1]![1];
}

/**
 * §4.4 (**Q139**): the 256-entry table the wall paints a depth cell with, indexed by the byte the pass
 * wrote there.
 *
 * The preview's depth is one 8-bit sample replicated across all three channels, so a byte *is* the
 * index - and a table beats calling `depthColor()` per pixel, because the wall does this for every
 * camera it shows, every beat. Packed for a `Uint32Array` view of an `ImageData`: little-endian
 * `0xAABBGGRR` with alpha full, so one assignment covers a pixel.
 *
 * What the ramp cannot say is "no measurement": an unmeasured ray clears to the far end of the scale
 * exactly as something genuinely at `far` does, which is a property of the 8-bit preview the Details
 * panel already used (`/captureScene` is where `NaN` is expressible, §5.10).
 */
export function depthPalette(): Uint32Array {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    const [red, green, blue] = depthColor(index / 255);
    table[index] = 0xff000000 + (blue << 16) + (green << 8) + red;
  }
  return table;
}

/**
 * §4.8.7 (**Q121**): the containers a viewport recording may be written into, best first. Only MP4 is
 * on the list because that is what the request asks for. The table names High, then Main, then the bare
 * type, and stops there. Measured on this Chromium all five H.264 strings it answers yes to (`640028`,
 * `4d001f`, `42E01E`, `42001f`, bare) really do write a decodable ~2 s stream, and the bare one comes
 * back as `Constrained Baseline` - so naming a Baseline profile would not widen the table, it would
 * only repeat its last entry. What the table is *not* is a promise: `isTypeSupported` says yes to
 * `avc1.42E01E` and the muxer delivers `Constrained Baseline`, so this predicate is only "did it answer
 * yes", and the judge of a take stays the byte count and a decode.
 */
export const RECORD_MIME_CANDIDATES = [
  "video/mp4;codecs=avc1.640028",
  "video/mp4;codecs=avc1.4d001f",
  "video/mp4",
];

/**
 * The first MP4 profile this browser will really write, or `null` for none of them (§4.8.7, **Q121**).
 *
 * `isSupported` is injected rather than read off `MediaRecorder` so the choice stays a pure function --
 * the failure this guards against is invisible from the call site: a browser that says yes to something
 * it cannot encode looks, from the page, exactly like a recording that worked. A `null` answer means
 * the page refuses the click with a toast and produces no file, and it does **not** fall back to WebM:
 * the request was for an `.mp4`, and a `.webm` in the downloads folder is a file nobody asked for.
 */
export function pickRecordMime(isSupported: (mime: string) => boolean): string | null {
  return RECORD_MIME_CANDIDATES.find((mime) => isSupported(mime)) ?? null;
}

/**
 * What a finished take is called on the way to disk (§4.8.7, **Q121**): `viewport-YYYYMMDD-HHMMSS.mp4`,
 * local time, zero padded so the downloads folder sorts them in order.
 *
 * No scene name, deliberately: one take can cross two 现场 (the canvas outlives `Load Scene`), and
 * naming a clip after whichever world happened to be loaded when the button was clicked tells half a
 * lie. `exportScene` can afford `scene_name` because an export is one world read at one instant.
 */
export function recordVideoName(now: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const day = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  const clock = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `viewport-${day}-${clock}.mp4`;
}

/** One shadow volume in the light's own view space: an orthographic box, or a near/far pair (§4.8.5). */
export interface ShadowExtents {
  left: number;
  right: number;
  bottom: number;
  top: number;
  near: number;
  far: number;
}

export interface ShadowRange {
  near: number;
  far: number;
}

/** A camera whose near plane reaches zero cannot project; this is the floor every fit obeys. */
const MIN_SHADOW_NEAR = 0.01;

/** Corners of an AABB, as the eight xyz triples `projectBox` already takes. */
function cornerPoints(corners: readonly number[]): Array<[number, number, number]> {
  const points: Array<[number, number, number]> = [];
  for (let index = 0; index + 2 < corners.length; index += 3) {
    points.push([corners[index]!, corners[index + 1]!, corners[index + 2]!]);
  }
  return points;
}

/** Widening only: a negative margin would cut casters out of the volume they cast into. */
function padOf(margin: number): number {
  return Number.isFinite(margin) && margin > 0 ? margin : 0;
}

/**
 * Fit a directional light's orthographic shadow box to the world AABB as that light sees it
 * (§4.8.5, **Q117**). `view` is the shadow camera's `matrixWorldInverse`, column major, so the
 * answer is already in the frame three renders the depth map from.
 *
 * A hardcoded extent is the mistake this replaces: 1024² texels spread over ±5 m give a table-scale
 * 现场 a shadow so soft it reads as ambient occlusion, while a box fitted tighter than the 现场
 * clips the dark edge off as soon as an arm leaves it.
 */
export function shadowExtentsOf(corners: readonly number[], view: readonly number[], margin: number): ShadowExtents | null {
  const points = cornerPoints(corners);
  if (!points.length || view.length < 16) return null;
  const pad = padOf(margin);
  let uMin = Number.POSITIVE_INFINITY;
  let uMax = Number.NEGATIVE_INFINITY;
  let vMin = Number.POSITIVE_INFINITY;
  let vMax = Number.NEGATIVE_INFINITY;
  let nearDepth = Number.POSITIVE_INFINITY;
  let farDepth = Number.NEGATIVE_INFINITY;
  for (const [x, y, z] of points) {
    const u = view[0]! * x + view[4]! * y + view[8]! * z + view[12]!;
    const v = view[1]! * x + view[5]! * y + view[9]! * z + view[13]!;
    // The camera looks along its own -Z, so a point in front of it carries a negative z.
    const depth = -(view[2]! * x + view[6]! * y + view[10]! * z + view[14]!);
    uMin = Math.min(uMin, u);
    uMax = Math.max(uMax, u);
    vMin = Math.min(vMin, v);
    vMax = Math.max(vMax, v);
    nearDepth = Math.min(nearDepth, depth);
    farDepth = Math.max(farDepth, depth);
  }
  const near = Math.max(MIN_SHADOW_NEAR, nearDepth - pad);
  return {
    left: uMin - pad,
    right: uMax + pad,
    bottom: vMin - pad,
    top: vMax + pad,
    near,
    far: Math.max(farDepth + pad, near + MIN_SHADOW_NEAR),
  };
}

/**
 * The reach of a cone or cube shadow: how close and how far from the light the world gets
 * (§4.8.5, **Q117**). Distances to the corners rather than beam projections, because a `Spot`'s
 * cone and a `Point`'s six faces both have to cover whatever angle they happen to look at.
 */
export function shadowRangeOf(corners: readonly number[], position: readonly number[], margin: number): ShadowRange | null {
  const points = cornerPoints(corners);
  if (!points.length || position.length < 3) return null;
  const pad = padOf(margin);
  let closest = Number.POSITIVE_INFINITY;
  let furthest = Number.NEGATIVE_INFINITY;
  for (const [x, y, z] of points) {
    const distance = Math.hypot(x - position[0]!, y - position[1]!, z - position[2]!);
    closest = Math.min(closest, distance);
    furthest = Math.max(furthest, distance);
  }
  const near = Math.max(MIN_SHADOW_NEAR, closest - pad);
  return { near, far: Math.max(furthest + pad, near + MIN_SHADOW_NEAR) };
}

/**
 * §6.5 (**Q143**): an OBJ index is absolute and counts from the start of the file, but `OBJLoader`
 * reads a file in order, so a face standing above the `v` line it needs resolves that vertex to
 * `undefined` and reaches the stage as NaN -- and three then caches a NaN `boundingBox` on the
 * geometry, from which the §4.5 outline, the §4.8.6 wireframe, `/get2DAABB` and `frame()` all read
 * the same broken number. Six of the 210 OBJs in the shipped library are written that way, all of
 * them robotiq gripper parts (`pad.obj` and `silicone_pad.obj` of the 2F-85, the two visual and the
 * two collision fingers of the 2F-140). Reading the file as a whole and handing the loader a source
 * whose declarations come first parses them *complete*: all 1200 faces of a pad, not the 300 that
 * could find a vertex.
 *
 * One thing has to be normalized on the way. A **negative** index means "count back from the vertices
 * declared so far", so moving the declarations would quietly change what it points at; the same pass
 * therefore rewrites every negative index into the absolute one it already means, counted where the
 * line stands. After that no face depends on the position of a declaration, whatever a future asset
 * looks like. A file that needs neither change is returned as it came in, byte for byte -- 204 of the
 * 210 do.
 */
export function normalizeObjSource(source: string): string {
  // The loader joins `\<newline>` continuations and trims a line before reading its first character,
  // so the scan has to look at the same lines the loader will.
  const lines = source.replace(/\\\n/g, "").split("\n");
  // v, vt, vn: the three slots of an `f`/`l`/`p` token, in the order the token writes them.
  const declared = [0, 0, 0];
  const declarations: string[] = [];
  const rest: string[] = [];
  let reaches = false;
  for (const line of lines) {
    const statement = line.trimStart();
    const slot = declarationSlotOf(statement);
    if (slot >= 0) {
      declared[slot]! += 1;
      declarations.push(line);
      continue;
    }
    if (!/^[flp]\s/.test(statement)) {
      rest.push(line);
      continue;
    }
    rest.push(absoluteIndexLine(line, declared, () => { reaches = true; }));
  }
  return reaches ? [...declarations, ...rest].join("\n") : source;
}

/** Which index slot a declaration fills -- `v` 0, `vt` 1, `vn` 2 -- or -1 when it fills none. */
function declarationSlotOf(statement: string): number {
  const keyword = statement.split(/\s+/, 1)[0];
  return keyword === "v" ? 0 : keyword === "vt" ? 1 : keyword === "vn" ? 2 : -1;
}

/**
 * One `f`, `l` or `p` line with every index written as the absolute index it means. `reaches` fires
 * when a positive index points past the declarations above the line -- the case that needs the hoist.
 * A line whose indices are all absolute already comes back as it went in.
 */
function absoluteIndexLine(line: string, declared: readonly number[], reaches: () => void): string {
  const tokens = line.trimStart().split(/\s+/);
  let rewritten = false;
  for (let index = 1; index < tokens.length; index += 1) {
    const parts = tokens[index]!.split("/");
    let changed = false;
    for (let slot = 0; slot < parts.length && slot < declared.length; slot += 1) {
      const text = parts[slot]!;
      if (text === "") continue;
      const value = Number.parseInt(text, 10);
      if (Number.isNaN(value) || value === 0) continue;
      if (value > 0) {
        if (value > declared[slot]!) reaches();
        continue;
      }
      // `-1` is the last vertex declared above this line, which is what the loader's own
      // `index + declared / 3` arithmetic resolves it to.
      const absolute = declared[slot]! + value + 1;
      // Reaching past the start of the file the reference is broken where it stands; leave it broken
      // the same way instead of inventing a vertex for it.
      if (absolute < 1) continue;
      parts[slot] = String(absolute);
      changed = true;
    }
    if (changed) {
      tokens[index] = parts.join("/");
      rewritten = true;
    }
  }
  return rewritten ? tokens.filter((token) => token !== "").join(" ") : line;
}

/**
 * The axis-aligned bounds of an OBJ's vertices, in the file's own frame, or null when it
 * declares no usable vertex. This is the box the *mesh* actually occupies -- which for 9 of the
 * 25 top-level OBJ models in the shipped library is not the box their metadata AABB claims: OLS
 * authored those 9 in an axis frame that does not match the mesh they ship (X and Y swapped for
 * most, the up axis for two; the mug, model 23, is the repro), so placement -- which measures the
 * metadata box -- reserves a footprint the drawn mesh does not fit, and two props whose boxes
 * merely touch overlap on screen (Q144). Reading the vertices is how placement prefers the truth
 * over the declared number. The OBJ branch of `parseAsset` applies no per-asset rotation (only
 * DAE/FBX do), so this file frame is the same Z-up local frame the metadata AABB is written in.
 *
 * Only `v` lines count; `vt`/`vn` are texture and normal data, not positions. A file whose
 * vertices are declared after the faces that index them still reports the same bounds here --
 * every `v` line is present whatever order the statements come in -- so this needs no
 * `normalizeObjSource` pass first.
 */
export function objVertexBounds(source: string): { min: Vec3; max: Vec3 } | null {
  const min: Vec3 = [Infinity, Infinity, Infinity];
  const max: Vec3 = [-Infinity, -Infinity, -Infinity];
  let seen = 0;
  // The same two things the loader does before it reads a line: join `\`-continuations, trim indent.
  for (const line of source.replace(/\\\n/g, "").split("\n")) {
    const statement = line.trimStart();
    if (!/^v\s/.test(statement)) continue;
    const parts = statement.split(/\s+/);
    const x = Number(parts[1]);
    const y = Number(parts[2]);
    const z = Number(parts[3]);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) continue;
    min[0] = Math.min(min[0]!, x); max[0] = Math.max(max[0]!, x);
    min[1] = Math.min(min[1]!, y); max[1] = Math.max(max[1]!, y);
    min[2] = Math.min(min[2]!, z); max[2] = Math.max(max[2]!, z);
    seen += 1;
  }
  return seen > 0 ? { min, max } : null;
}
