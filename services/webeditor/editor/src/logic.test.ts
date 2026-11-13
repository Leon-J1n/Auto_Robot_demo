import { describe, expect, it } from "vitest";
import {
  agentPanelVisible, agentPlanItemsFromEvent, boundingBoxesEnabled, cameraFrustumSegments, cameraRig, cameraWallEnabled, cameraWallModeFor, cameraWallPace,
  DEFAULT_GHOST_OPACITY, DEFAULT_LOCAL_AXES_LENGTH, DEFAULT_SHADOWS_ENABLED, DEFAULT_URDF_LINK_BOXES, DEFAULT_VIEWPORT_BACKGROUND, DEFAULT_WAYPOINT_RADIUS, DEFAULT_WAYPOINT_STRIDE_FRAMES,
  depthColor, depthPalette, enterSendsAgentMessage, featureEnabled, ghostOpacity, leafArmIds, localAxesEnabled, localAxesRig, mergeSpeech, normalizeObjSource, objVertexBounds, panelMarkupChanged, panelTabActive, panelTabPress, projectBox, pickRecordMime, recordVideoName,
  RECORD_MIME_CANDIDATES, shadowExtentsOf, shadowRangeOf, shadowsEnabled, timelineDueIndex, urdfLinkBoxesEnabled, viewportBackground,
  WAYPOINT_COLORS, waypointColor, waypointFrames, waypointSettings,
} from "./logic";
describe("feature switches", () => {
  it("requires explicit switches and honors disabled parents", () => {
    const features = {
      "panels.details": true,
      "panels.details.camera": true,
      "panels.details.camera.preview": true,
      "panels.details.transform.notice": false,
      "viewport.cameraPreview.modes": false,
      "viewport.cameraPreview.modes.depth": true,
    };
    expect(featureEnabled(features, "panels.details.camera.preview")).toBe(true);
    expect(featureEnabled(features, "panels.details.transform.notice")).toBe(false);
    expect(featureEnabled(features, "viewport.cameraPreview.modes.depth")).toBe(false);
    // §9.3 retires the inspector's edit switches, so an unknown path is off by default.
    expect(featureEnabled(features, "panels.details.camera.updateRate")).toBe(false);
  });
});

describe("segment-end ghost opacity (§7.3, Q97)", () => {
  it("reads the configured number and falls back to the default for everything else", () => {
    expect(ghostOpacity({ ghost_pose_opacity: 0.2 })).toBe(0.2);
    expect(ghostOpacity({ ghost_pose_opacity: 0 })).toBe(0);
    expect(ghostOpacity({ ghost_pose_opacity: 1 })).toBe(1);
    // Fail-open: an unknown key means nobody tuned it, not that the ghost should vanish.
    expect(ghostOpacity({})).toBe(DEFAULT_GHOST_OPACITY);
    expect(ghostOpacity(null)).toBe(DEFAULT_GHOST_OPACITY);
    expect(ghostOpacity(undefined)).toBe(DEFAULT_GHOST_OPACITY);
    // A config file can carry anything, so a value that is not a usable opacity is ignored.
    expect(ghostOpacity({ ghost_pose_opacity: 1.5 })).toBe(DEFAULT_GHOST_OPACITY);
    expect(ghostOpacity({ ghost_pose_opacity: -0.1 })).toBe(DEFAULT_GHOST_OPACITY);
    expect(ghostOpacity({ ghost_pose_opacity: Number.NaN })).toBe(DEFAULT_GHOST_OPACITY);
    expect(ghostOpacity({ ghost_pose_opacity: "0.2" as unknown as number })).toBe(DEFAULT_GHOST_OPACITY);
    expect(ghostOpacity({ ghost_pose_opacity: null as unknown as number })).toBe(DEFAULT_GHOST_OPACITY);
  });

  it("ships 50% as the default", () => {
    expect(DEFAULT_GHOST_OPACITY).toBe(0.5);
  });
});

describe("the viewport background colour (§4.4, Q145)", () => {
  it("reads a configured #RRGGBB or #RGB string verbatim", () => {
    expect(viewportBackground({ viewport_background_color: "#123456" })).toBe("#123456");
    expect(viewportBackground({ viewport_background_color: "#000000" })).toBe("#000000");
    expect(viewportBackground({ viewport_background_color: "#fff" })).toBe("#fff");
    expect(viewportBackground({ viewport_background_color: "#0f0" })).toBe("#0f0");
    // Hex digits are case-insensitive, and surrounding whitespace is trimmed before the shape check.
    expect(viewportBackground({ viewport_background_color: "#ABCdef" })).toBe("#ABCdef");
    expect(viewportBackground({ viewport_background_color: "  #123456  " })).toBe("#123456");
  });

  it("falls back to black for everything that is not one of those two shapes", () => {
    expect(DEFAULT_VIEWPORT_BACKGROUND).toBe("#000000");
    expect(viewportBackground({})).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground(null)).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground(undefined)).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    // Fail-open like the numeric constants: a value nobody can use reads as "nobody tuned it".
    expect(viewportBackground({ viewport_background_color: "black" })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: "123456" })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: "#12345" })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: "#1234567" })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: "#gggggg" })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: "#12 34" })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: 0x123456 as unknown as string })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
    expect(viewportBackground({ viewport_background_color: null as unknown as string })).toBe(DEFAULT_VIEWPORT_BACKGROUND);
  });
});

describe("waypoint size and density (§7.3, Q101)", () => {
  it("reads both configured numbers", () => {
    expect(waypointSettings({ traj_waypoint_radius: 0.05, traj_waypoint_stride_frames: 20 }))
      .toEqual({ radius: 0.05, strideFrames: 20 });
    // A stride of 1 is legal: it is the "no sampling" end of the parameter.
    expect(waypointSettings({ traj_waypoint_stride_frames: 1 }).strideFrames).toBe(1);
  });

  it("falls back to the shipped defaults", () => {
    expect(DEFAULT_WAYPOINT_RADIUS).toBe(0.01);
    expect(DEFAULT_WAYPOINT_STRIDE_FRAMES).toBe(10);
    expect(waypointSettings({})).toEqual({ radius: DEFAULT_WAYPOINT_RADIUS, strideFrames: DEFAULT_WAYPOINT_STRIDE_FRAMES });
    expect(waypointSettings(null)).toEqual({ radius: DEFAULT_WAYPOINT_RADIUS, strideFrames: DEFAULT_WAYPOINT_STRIDE_FRAMES });
    expect(waypointSettings(undefined)).toEqual({ radius: DEFAULT_WAYPOINT_RADIUS, strideFrames: DEFAULT_WAYPOINT_STRIDE_FRAMES });
  });

  it("refuses the values that would break the sweep, field by field", () => {
    // 0 and negative are the two the doc names: a 0 radius collapses every instance matrix
    // and a 0 stride never advances the sampling loop.
    expect(waypointSettings({ traj_waypoint_radius: 0 }).radius).toBe(DEFAULT_WAYPOINT_RADIUS);
    expect(waypointSettings({ traj_waypoint_radius: -1 }).radius).toBe(DEFAULT_WAYPOINT_RADIUS);
    expect(waypointSettings({ traj_waypoint_stride_frames: 0 }).strideFrames).toBe(DEFAULT_WAYPOINT_STRIDE_FRAMES);
    expect(waypointSettings({ traj_waypoint_stride_frames: -3 }).strideFrames).toBe(DEFAULT_WAYPOINT_STRIDE_FRAMES);
    expect(waypointSettings({ traj_waypoint_radius: Number.NaN }).radius).toBe(DEFAULT_WAYPOINT_RADIUS);
    expect(waypointSettings({ traj_waypoint_radius: Number.POSITIVE_INFINITY }).radius).toBe(DEFAULT_WAYPOINT_RADIUS);
    expect(waypointSettings({ traj_waypoint_stride_frames: Number.NaN }).strideFrames).toBe(DEFAULT_WAYPOINT_STRIDE_FRAMES);
    expect(waypointSettings({ traj_waypoint_stride_frames: 2.5 }).strideFrames).toBe(DEFAULT_WAYPOINT_STRIDE_FRAMES);
    expect(waypointSettings({ traj_waypoint_radius: "0.05" as unknown as number }).radius).toBe(DEFAULT_WAYPOINT_RADIUS);
    expect(waypointSettings({ traj_waypoint_stride_frames: "10" as unknown as number }).strideFrames).toBe(DEFAULT_WAYPOINT_STRIDE_FRAMES);
    expect(waypointSettings({ traj_waypoint_radius: null as unknown as number }).radius).toBe(DEFAULT_WAYPOINT_RADIUS);
    expect(waypointSettings({ traj_waypoint_stride_frames: null as unknown as number }).strideFrames).toBe(DEFAULT_WAYPOINT_STRIDE_FRAMES);
    // One bad field does not discard the other.
    expect(waypointSettings({ traj_waypoint_radius: 0.08, traj_waypoint_stride_frames: 0 }))
      .toEqual({ radius: 0.08, strideFrames: DEFAULT_WAYPOINT_STRIDE_FRAMES });
  });

  it("samples every strideFrames-th frame and always ends on the last one", () => {
    const sampled = waypointFrames(720, 10);
    expect(sampled).toHaveLength(73);
    expect(sampled[0]).toBe(0);
    expect(sampled[1]).toBe(10);
    expect(sampled[71]).toBe(710);
    expect(sampled[72]).toBe(719);
    // The counts §12.13 ③ reads off the diagnostics line.
    expect(waypointFrames(840, 10)).toHaveLength(85);
    expect(waypointFrames(840, 20)).toHaveLength(43);
    expect(waypointFrames(720, 20)).toHaveLength(37);
    // Frame 0 is a waypoint whatever the stride, and a clip shorter than the stride is two.
    expect(waypointFrames(5, 10)).toEqual([0, 4]);
    expect(waypointFrames(1, 10)).toEqual([0]);
    // A stride of 1 is every frame, with no duplicate at the end.
    expect(waypointFrames(720, 1)).toHaveLength(720);
    expect(waypointFrames(720, 1)[719]).toBe(719);
    // When the stride lands on the last frame it is not added twice.
    expect(waypointFrames(721, 10)).toHaveLength(73);
    expect(waypointFrames(721, 10)[72]).toBe(720);
    expect(waypointFrames(21, 10)).toEqual([0, 10, 20]);
    // Ordered, ascending, inside the clip.
    const dense = waypointFrames(95, 7);
    expect([...dense].sort((a, b) => a - b)).toEqual(dense);
    expect(dense.every((frame) => frame >= 0 && frame < 95)).toBe(true);
    // A clip with no frames has no waypoints; neither does a nonsensical stride.
    expect(waypointFrames(0, 10)).toEqual([]);
    expect(waypointFrames(-5, 10)).toEqual([]);
    expect(waypointFrames(Number.NaN, 10)).toEqual([]);
    expect(waypointFrames(720, 0)).toEqual([]);
    expect(waypointFrames(720, 2.5)).toEqual([]);
  });

  it("cycles a hardcoded palette by trajectory index and keeps the active clip's colour stable", () => {
    expect(WAYPOINT_COLORS).toHaveLength(6);
    expect(waypointColor(0)).toBe(WAYPOINT_COLORS[0]);
    expect(waypointColor(3)).toBe(WAYPOINT_COLORS[3]);
    expect(waypointColor(6)).toBe(WAYPOINT_COLORS[0]);
    expect(waypointColor(7)).toBe(waypointColor(1));
    expect(new Set(WAYPOINT_COLORS).size).toBe(WAYPOINT_COLORS.length);
    // A batch can only be as long as the config allows, and nothing beyond it is distinct.
    expect(waypointColor(-1)).toBe(WAYPOINT_COLORS[WAYPOINT_COLORS.length - 1]);
    expect(waypointColor(Number.NaN)).toBe(WAYPOINT_COLORS[0]);
    expect(waypointColor(1.5)).toBe(WAYPOINT_COLORS[0]);
  });
});

describe("leaf arms of a driver chain (§7.3, Q101)", () => {
  // The world's parentage is a function here, so each case states its own chain.
  const parentOf = (parents: Record<number, number | null>) => (objectId: number) => parents[objectId] ?? null;

  it("keeps only the end of a chain", () => {
    // The user's own example: 3=gripper hangs off 2=arm, so only the gripper is drawn.
    expect(leafArmIds([2, 3], parentOf({ 3: 2 }))).toEqual([3]);
    // Three deep: A <- B <- C leaves C.
    expect(leafArmIds([1, 2, 3], parentOf({ 2: 1, 3: 2 }))).toEqual([3]);
  });

  it("draws an isolated driver and every branch of a disconnected batch", () => {
    expect(leafArmIds([2], parentOf({}))).toEqual([2]);
    // Two chains that never meet are two strings of waypoints, both drawn.
    expect(leafArmIds([1, 2, 3, 4], parentOf({ 2: 1, 4: 3 }))).toEqual([2, 4]);
    // A driver hanging off a plain object (a table) is a chain of one, drawn as itself.
    expect(leafArmIds([2, 3], parentOf({ 2: 7 }))).toEqual([2, 3]);
    // The same arm now carrying a gripper: what it hangs on is not what drops it.
    expect(leafArmIds([2, 3], parentOf({ 2: 7, 3: 2 }))).toEqual([3]);
  });

  it("walks back out of a cycle instead of spinning", () => {
    // Not a legal world file, but the walk must not become the browser's last frame.
    expect(leafArmIds([1, 2], parentOf({ 1: 2, 2: 1 }))).toEqual([]);
    // A node that is its own parent stops the walk at once, so it stays a leaf.
    expect(leafArmIds([1, 2], parentOf({ 1: 1 }))).toEqual([1, 2]);
    // A parent id that is not a driver (removed object, plain table) ends the walk.
    expect(leafArmIds([1], parentOf({ 1: 99 }))).toEqual([1]);
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

describe("speech transcript merging", () => {
  it("preserves the draft prefix and committed transcript", () => {
    expect(mergeSpeech("move the ", "cube ", "left")).toBe("move the cube left");
    expect(mergeSpeech("existing ", "final", "")).toBe("existing final");
  });
});

describe("Enter as the send button (§7.6, Q129)", () => {
  const SEND_ON = { "agent.input.send": true };

  /** A real keydown carries six flags besides `key`; a case names only the one it flips. */
  type KeyFlags = Pick<KeyboardEvent, "key" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey"
    | "isComposing" | "repeat">;
  function press(over: Partial<KeyFlags> = {}): KeyFlags {
    return { key: "Enter", shiftKey: false, ctrlKey: false, metaKey: false, altKey: false,
      isComposing: false, repeat: false, ...over };
  }

  it("sends a plain Enter while the send button is there", () => {
    expect(enterSendsAgentMessage(SEND_ON, press())).toBe(true);
  });

  /** The box is a `<textarea rows="3">`: dropping this line would take multi-line prompts away. */
  it("leaves Shift+Enter to the textarea's own newline", () => {
    expect(enterSendsAgentMessage(SEND_ON, press({ shiftKey: true }))).toBe(false);
  });

  /** The one case that would make the feature harmful: that Enter picks a Chinese candidate. */
  it("does not send the Enter that commits an input-method candidate", () => {
    expect(enterSendsAgentMessage(SEND_ON, press({ isComposing: true }))).toBe(false);
  });

  it("does not turn a held key into a queue of tasks", () => {
    expect(enterSendsAgentMessage(SEND_ON, press({ repeat: true }))).toBe(false);
  });

  it("leaves modified Enters to the browser and the operating system", () => {
    expect(enterSendsAgentMessage(SEND_ON, press({ ctrlKey: true }))).toBe(false);
    expect(enterSendsAgentMessage(SEND_ON, press({ metaKey: true }))).toBe(false);
    expect(enterSendsAgentMessage(SEND_ON, press({ altKey: true }))).toBe(false);
  });

  /** Off means the button -- and therefore its alias -- does not exist, so the key stays default. */
  it("sends nothing when the button it aliases is switched off", () => {
    expect(enterSendsAgentMessage({ "agent.input.send": false }, press())).toBe(false);
    expect(enterSendsAgentMessage({}, press())).toBe(false);
  });

  it("answers only for Enter", () => {
    for (const key of ["a", " ", "NumpadEnter", "Escape"]) {
      expect(enterSendsAgentMessage(SEND_ON, press({ key }))).toBe(false);
    }
  });
});

describe("timeline scheduling", () => {
  it("skips expired display frames to keep wall-clock duration", () => {
    const times = [0, 0.002, 0.004, 0.006, 0.008];
    expect(timelineDueIndex(times, 0, 0.001)).toBe(0);
    expect(timelineDueIndex(times, 0, 0.006)).toBe(3);
    expect(timelineDueIndex(times, 3, 1)).toBe(4);
  });
});

describe("camera frustum", () => {
  it("uses Sim Core optical axes, vertical FOV, aspect ratio, and clip distances", () => {
    const points = cameraFrustumSegments({
      verticalFovDegrees: 60, width: 640, height: 480, nearPlane: 0.1, farPlane: 10, depth: true,
    });
    expect(points).toHaveLength(72);
    expect(points.slice(0, 3)).toEqual([0, 0, 0]);
    // The first far corner is right-up at `far`: X width, Y height, Z depth.
    expect(points[3]).toBeCloseTo(10 * Math.tan(Math.PI / 6) * 4 / 3);
    expect(points[4]).toBeCloseTo(10 * Math.tan(Math.PI / 6));
    expect(points[5]).toBe(10);
    expect(points).toContain(0.1);
  });
});

describe("box projection (§5.11)", () => {
  // Column-major, the layout Three.js stores matrices in.
  const FLAT = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  // `w = -z`, so a point in front of the camera has a negative camera-space z.
  const AHEAD = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, -1, 0, 0, 0, 0];

  it("maps clip space onto pixels counted from the top left", () => {
    // The two opposite corners of the whole image: ndc (-1,-1) is bottom left, (1,1) top right.
    expect(projectBox([-1, -1, 0, 1, 1, 0], FLAT, 4, 2)).toEqual({
      visible: true, clipped: false, box: { x_min: 0, y_min: 0, x_max: 4, y_max: 2 },
    });
    // The optical axis meets the image at its centre.
    expect(projectBox([0, 0, 0], FLAT, 4, 2).box).toEqual({ x_min: 2, y_min: 1, x_max: 2, y_max: 1 });
  });

  it("drops corners at or behind the camera instead of mirroring them across the frame", () => {
    // (0.5, 0, -1) is in front and lands at x = 3; the same point at z = +1 is behind.
    expect(projectBox([0.5, 0, -1, 0.5, 0, 1], AHEAD, 4, 2)).toEqual({
      visible: true, clipped: false, box: { x_min: 3, y_min: 1, x_max: 3, y_max: 1 },
    });
    // A corner exactly on the camera plane divides by zero, so it goes the same way.
    expect(projectBox([0.5, 0, -1, 0.5, 0, 0], AHEAD, 4, 2).box?.x_min).toBe(3);
  });

  it("calls a box with nothing in front of the camera invisible", () => {
    expect(projectBox([0.5, 0, 1, -0.5, 0, 1, 0.5, 1, 1], AHEAD, 4, 2)).toEqual({
      visible: false, clipped: true, box: null,
    });
  });

  it("clips a box that leaves the frame and keeps the part the caller can draw", () => {
    expect(projectBox([-2, -0.5, 0, 0.5, 0.5, 0], FLAT, 4, 2)).toEqual({
      visible: true, clipped: true, box: { x_min: 0, y_min: 0.5, x_max: 3, y_max: 1.5 },
    });
  });

  it("calls a box wholly past an edge invisible", () => {
    // Past the right edge: the intersection has no area left.
    const past = projectBox([1.2, -0.5, 0, 2, 0.5, 0], FLAT, 4, 2);
    expect(past).toEqual({ visible: false, clipped: true, box: null });
    // Exactly grazing it leaves nothing to draw either.
    expect(projectBox([1, -0.5, 0, 2, 0.5, 0], FLAT, 4, 2).visible).toBe(false);
  });

  it("orders the rectangle no matter how the corners arrive", () => {
    const forwards = projectBox([-0.5, -0.5, 0, 0.5, 0.5, 0], FLAT, 4, 2).box!;
    const backwards = projectBox([0.5, 0.5, 0, -0.5, -0.5, 0], FLAT, 4, 2).box!;
    expect(backwards).toEqual(forwards);
    expect(forwards.x_min).toBeLessThan(forwards.x_max);
    expect(forwards.y_min).toBeLessThan(forwards.y_max);
  });

  it("refuses an image with no area", () => {
    expect(projectBox([0, 0, 0], FLAT, 0, 2)).toEqual({ visible: false, clipped: true, box: null });
    expect(projectBox([0, 0, 0], FLAT, 4, Number.NaN).visible).toBe(false);
  });
});

describe("camera rig", () => {
  it("normalises the contract fields of a Camera component", () => {
    expect(cameraRig({ type: "rgbd", fov: 60, near: 0.1, far: 1000, resolution: [640, 480] })).toEqual({
      verticalFovDegrees: 60, width: 640, height: 480, nearPlane: 0.1, farPlane: 1000, depth: true,
    });
  });

  it("fills defaults for missing fields and keeps a non-degenerate frustum", () => {
    expect(cameraRig({})).toEqual({
      verticalFovDegrees: 60, width: 640, height: 480, nearPlane: 0.1, farPlane: 1000, depth: false,
    });
    expect(cameraRig(undefined).farPlane).toBeGreaterThan(cameraRig(undefined).nearPlane);
    // far <= near would collapse the frustum, so it is pushed past the near plane
    expect(cameraRig({ near: 5, far: 2 }).farPlane).toBe(50);
    expect(cameraRig({ fov: -1, near: 0, far: Number.NaN, resolution: [0, -8] }).verticalFovDegrees).toBe(60);
  });
});

/**
 * §4.9 (**Q130**): the one reading of a `LocalAxes` block. It lives here rather than in the
 * renderer because both `world.ts`, which warns, and `renderer.ts`, which draws, call it - so
 * a field the file got wrong is decided once, in a test that needs neither a scene nor a GPU.
 */
describe("local axes rig (§4.9, Q130)", () => {
  const DEFAULTS = {
    x: { color: "#FF0000", opacity: 1 },
    y: { color: "#00FF00", opacity: 1 },
    z: { color: "#0000FF", opacity: 1 },
  };

  it("reads all three fields as the file wrote them", () => {
    expect(localAxesRig({ length: 0.25, colors: { x: "#112233", y: "#445566", z: "#778899" }, show_labels: true })).toEqual({
      length: 0.25,
      colors: { x: { color: "#112233", opacity: 1 }, y: { color: "#445566", opacity: 1 }, z: { color: "#778899", opacity: 1 } },
      showLabels: true,
      offset: [0, 0, 0],
      warnings: [],
    });
  });

  it("fills the documented defaults for an empty block", () => {
    expect(localAxesRig({})).toEqual({ length: DEFAULT_LOCAL_AXES_LENGTH, colors: DEFAULTS, showLabels: false, offset: [0, 0, 0], warnings: [] });
    expect(localAxesRig(undefined).length).toBe(DEFAULT_LOCAL_AXES_LENGTH);
  });

  it("warns about a length it cannot use and still gives the axes 0.1 m", () => {
    const rig = localAxesRig({ length: 0 });
    expect(rig.length).toBe(DEFAULT_LOCAL_AXES_LENGTH);
    expect(rig.warnings).toEqual(["LocalAxes length 0 is not a positive number, using 0.1"]);
    // Everything that is not a usable number reads the same way, and none of it aborts the load.
    for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY, "long", null, true]) {
      expect(localAxesRig({ length: bad as unknown as number }).length).toBe(DEFAULT_LOCAL_AXES_LENGTH);
    }
    // Absent is not wrong: §3 falls back in silence.
    expect(localAxesRig({}).warnings).toEqual([]);
  });

  it("falls back per axis, so one bad colour cannot recolour the frame", () => {
    const rig = localAxesRig({ colors: { x: "red", y: "#4488ff", z: "#00ff0080" } });
    expect(rig.colors.x).toEqual(DEFAULTS.x);
    expect(rig.colors.y).toEqual({ color: "#4488FF", opacity: 1 });
    // Three.js parses neither `#RGB` nor an 8-digit literal, so both are resolved here.
    expect(rig.colors.z).toEqual({ color: "#00FF00", opacity: 128 / 255 });
    expect(rig.warnings).toEqual(['LocalAxes colors.x "red" is not a hex colour, using #FF0000']);
    expect(localAxesRig({ colors: { x: "#445" } }).colors.x).toEqual({ color: "#444455", opacity: 1 });
    // A key the file never wrote is a default, not a defect (§4.9, example one).
    expect(localAxesRig({ colors: { x: "#123456" } }).warnings).toEqual([]);
  });

  it("says so once when `colors` itself is not a mapping", () => {
    for (const bad of ["#FF0000", null, 7]) {
      const rig = localAxesRig({ colors: bad as unknown as { x?: string } });
      expect(rig.colors).toEqual(DEFAULTS);
      expect(rig.warnings).toEqual(["LocalAxes colors " + JSON.stringify(bad) + " is not an object, using the default red/green/blue"]);
    }
  });

  it("shows tip letters only for a literal true", () => {
    expect(localAxesRig({ show_labels: true }).showLabels).toBe(true);
    // The tolerant reading `RobotDriver.role` set in §4.7: wanting fewer decorations is
    // not a data error, so nothing here warns.
    for (const falsy of [undefined, false, "true", 1, null]) {
      const rig = localAxesRig({ show_labels: falsy as unknown as boolean });
      expect(rig.showLabels).toBe(false);
      expect(rig.warnings).toEqual([]);
    }
  });

  it("reads offset as the file wrote it, and falls back to [0,0,0] when absent", () => {
    expect(localAxesRig({ offset: [0.1, -0.2, 0.3] }).offset).toEqual([0.1, -0.2, 0.3]);
    expect(localAxesRig({}).offset).toEqual([0, 0, 0]);
    expect(localAxesRig(undefined).offset).toEqual([0, 0, 0]);
  });

  it("warns about an offset it cannot use and still gives [0,0,0]", () => {
    // Not an array
    const rig1 = localAxesRig({ offset: "0.1,0.2,0.3" as unknown as [number, number, number] });
    expect(rig1.offset).toEqual([0, 0, 0]);
    expect(rig1.warnings).toEqual(['LocalAxes offset "0.1,0.2,0.3" is not a 3-element array, using [0,0,0]']);

    // Wrong length
    const rig2 = localAxesRig({ offset: [0.1, 0.2] as unknown as [number, number, number] });
    expect(rig2.offset).toEqual([0, 0, 0]);
    expect(rig2.warnings).toEqual(["LocalAxes offset [0.1,0.2] is not a 3-element array, using [0,0,0]"]);

    // Non-finite component
    const rig3 = localAxesRig({ offset: [0.1, Number.NaN, 0.3] });
    expect(rig3.offset).toEqual([0, 0, 0]);
    expect(rig3.warnings).toEqual(["LocalAxes offset[1] null is not a finite number, using [0,0,0]"]);

    const rig4 = localAxesRig({ offset: [0.1, "0.2", 0.3] as unknown as [number, number, number] });
    expect(rig4.offset).toEqual([0, 0, 0]);
    expect(rig4.warnings).toEqual(['LocalAxes offset[1] "0.2" is not a finite number, using [0,0,0]']);

    // Absent is not wrong: §3 falls back in silence.
    expect(localAxesRig({}).warnings).toEqual([]);
  });
});

/**
 * §4.8.5 (**Q117**): the arithmetic behind the shadow volumes, kept in the same family as
 * `projectBox` so a fit can be tested without a GPU - and without the renderer's own
 * `fitShadowRigs`, which is only the loop over these two.
 */
describe("shadow volume fitting (§4.8.5, Q117)", () => {
  // Column-major, the layout Three.js stores matrices in.
  const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  /** The eight corners of a 2 m cube centred on the origin, flat xyz as `projectBox` takes them. */
  const CUBE = [-1, -1, -1, 1, -1, -1, -1, 1, -1, 1, 1, -1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 1];
  /** The same cube moved 4 m up a Y-up stage: 3 m to 5 m below a lamp at 10 m. */
  const CRATE = [-1, 3, -1, 1, 3, -1, -1, 5, -1, 1, 5, -1, -1, 3, 1, 1, 3, 1, -1, 5, 1, 1, 5, 1];

  it("hugs the square a directional light sees, widened by the margin", () => {
    // A camera 6 m above the cube looking down its own -Z: depth runs 5 m to 7 m.
    const view = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, -6, 1];
    expect(shadowExtentsOf(CUBE, view, 0.25)).toEqual({
      left: -1.25, right: 1.25, bottom: -1.25, top: 1.25, near: 4.75, far: 7.25,
    });
  });

  it("keeps a volume that only reaches behind the camera legal instead of inverting it", () => {
    // +6 on the translation puts every corner behind the eye, so all eight depths are negative.
    const behind = shadowExtentsOf(CUBE, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 6, 1], 0.25)!;
    expect(behind.near).toBeCloseTo(0.01, 12);
    expect(behind.far).toBeGreaterThan(behind.near);
  });

  it("takes a margin that only ever widens, and none at all when it is not a number", () => {
    expect(shadowExtentsOf(CUBE, IDENTITY, -3)!.left).toBe(-1);
    expect(shadowExtentsOf(CUBE, IDENTITY, Number.NaN)!.right).toBe(1);
    expect(shadowExtentsOf(CUBE, IDENTITY, 0.5)!.left).toBe(-1.5);
  });

  it("refuses to fit what it cannot read", () => {
    expect(shadowExtentsOf([], IDENTITY, 0.25)).toBeNull();
    expect(shadowExtentsOf(CUBE, IDENTITY.slice(0, 15), 0.25)).toBeNull();
    expect(shadowRangeOf([], [0, 0, 0], 0.25)).toBeNull();
    expect(shadowRangeOf(CUBE, [0, 0], 0.25)).toBeNull();
  });

  it("brackets a cone and a cube by the distance to the corners", () => {
    // The nearest corner is sqrt(27) m off the lamp and the farthest sqrt(51) m, and the margin
    // is what keeps a caster's own silhouette inside the map rather than clipped at its rim.
    expect(shadowRangeOf(CRATE, [0, 10, 0], 0)!.near).toBeCloseTo(Math.sqrt(27), 9);
    expect(shadowRangeOf(CRATE, [0, 10, 0], 0)!.far).toBeCloseTo(Math.sqrt(51), 9);
    expect(shadowRangeOf(CRATE, [0, 10, 0], 0.25)!.near).toBeCloseTo(Math.sqrt(27) - 0.25, 9);
    expect(shadowRangeOf(CRATE, [0, 10, 0], 0.25)!.far).toBeCloseTo(Math.sqrt(51) + 0.25, 9);
  });

  it("keeps a light that stands inside the world off a zero near plane", () => {
    const inside = shadowRangeOf([0, 0, 0, 1, 0, 0], [0, 0, 0], 0.25)!;
    expect(inside.near).toBeCloseTo(0.01, 12);
    expect(inside.far).toBeCloseTo(1.25, 12);
  });
});

describe("shadow switching reads three sources (§4.8.5, Q118)", () => {
  const SHOWN = { "viewport.shadows": true };
  const HIDDEN = { "viewport.shadows": false };

  it("lets the checkbox decide while the flag shows it", () => {
    // The constant is not read on this path: a hidden control is the only thing it speaks for.
    expect(shadowsEnabled(SHOWN, { shadows_enabled: false }, true, false)).toBe(true);
    expect(shadowsEnabled(SHOWN, { shadows_enabled: true }, false, false)).toBe(false);
  });

  it("hands the decision to the constant when the flag hides the checkbox", () => {
    // And `state.shadows` is out of the picture - whatever this tab last clicked says nothing here.
    expect(shadowsEnabled(HIDDEN, { shadows_enabled: true }, false, false)).toBe(true);
    expect(shadowsEnabled(HIDDEN, { shadows_enabled: false }, true, false)).toBe(false);
  });

  it("pins only the checkbox path to on while a lease is held", () => {
    expect(shadowsEnabled(SHOWN, { shadows_enabled: false }, false, true)).toBe(true);
    // An operator's deliberate "off" in the constant config is not a per-tab whim, so freezing it
    // to on during a collection would be the renderer disobeying the very config it reads.
    expect(shadowsEnabled(HIDDEN, { shadows_enabled: false }, true, true)).toBe(false);
  });

  it("reads an unusable constant as the documented default, not as off", () => {
    const unusable = [undefined, null, "false", 0, 1, Number.NaN];
    for (const value of unusable) {
      expect(shadowsEnabled(HIDDEN, { shadows_enabled: value as unknown as boolean }, false, false))
        .toBe(DEFAULT_SHADOWS_ENABLED);
    }
    expect(shadowsEnabled(HIDDEN, null, false, false)).toBe(DEFAULT_SHADOWS_ENABLED);
  });

  it("stays fail-closed on a missing feature key and then asks the constant", () => {
    // This is the case that started the change: a gateway started before the key existed reports a
    // snapshot without it, so the checkbox hides -- and the shadows must not vanish along with it.
    expect(shadowsEnabled({}, { shadows_enabled: true }, true, false)).toBe(true);
    expect(shadowsEnabled({}, { shadows_enabled: false }, true, false)).toBe(false);
  });
});

describe("bounding boxes answer to one switch (§4.8.6, Q119)", () => {
  it("draws only when the flag shows the checkbox and the checkbox is on", () => {
    const SHOWN = { "viewport.boundingBoxes": true };
    expect(boundingBoxesEnabled(SHOWN, true)).toBe(true);
    expect(boundingBoxesEnabled(SHOWN, false)).toBe(false);
  });

  it("keeps the layer off once the flag has taken the checkbox away", () => {
    // Whatever this tab last clicked says nothing here -- and this is the one case where a hidden
    // host and an unchecked box are the same picture, which is why there is no constant to fall
    // back to the way §4.8.5 needed one.
    expect(boundingBoxesEnabled({ "viewport.boundingBoxes": false }, true)).toBe(false);
    expect(boundingBoxesEnabled({}, true)).toBe(false);
  });
});

/**
 * §4.8.6 (**Q149**): `urdf_bounding_box_per_link` says in which *shape* a URDF object's wireframe
 * is drawn, and nothing else. It is not a second exit for the layer - Q119's key and checkbox still
 * decide whether anything is drawn at all - and it never changes the measure: `/get2DAABB`, the
 * selection outline and placement read `worldAabb()`, which is the union of the same link boxes.
 */
describe("a URDF box shape answers to the constant config (§4.8.6, Q149/Q150)", () => {
  it("draws one box per link by default and accepts the merged shape when configured", () => {
    expect(urdfLinkBoxesEnabled(null)).toBe(true);
    expect(urdfLinkBoxesEnabled({ urdf_bounding_box_per_link: false })).toBe(false);
    expect(urdfLinkBoxesEnabled({ urdf_bounding_box_per_link: true })).toBe(true);
  });

  it("reads an unusable constant as the documented per-link default", () => {
    for (const value of [undefined, null, "true", "false", 0, 1, Number.NaN]) {
      expect(urdfLinkBoxesEnabled({ urdf_bounding_box_per_link: value as unknown as boolean }))
        .toBe(DEFAULT_URDF_LINK_BOXES);
    }
    // A gateway started before the key existed answers without it, and that is the same picture.
    expect(urdfLinkBoxesEnabled(null)).toBe(DEFAULT_URDF_LINK_BOXES);
    expect(urdfLinkBoxesEnabled(undefined)).toBe(DEFAULT_URDF_LINK_BOXES);
    expect(urdfLinkBoxesEnabled({})).toBe(DEFAULT_URDF_LINK_BOXES);
  });
});

describe("local axes answer to the toolbar checkbox (§4.8.8, Q132)", () => {
  it("draws only when the flag shows the checkbox and the checkbox is on", () => {
    const SHOWN = { "viewport.localAxesToggle": true };
    expect(localAxesEnabled(SHOWN, true)).toBe(true);
    expect(localAxesEnabled(SHOWN, false)).toBe(false);
  });

  it("keeps the layer off once the flag has taken the checkbox away", () => {
    // Same reading as the wireframe above: with no host there is nothing to click, and a closed layer
    // is what the page shows -- Q130's key owned the layer alone, Q132 moved that owner behind a box.
    expect(localAxesEnabled({ "viewport.localAxesToggle": false }, true)).toBe(false);
    expect(localAxesEnabled({}, true)).toBe(false);
  });

  it("does not answer to the name this key had before the checkbox", () => {
    // **Q132** renamed `viewport.localAxes` -> `viewport.localAxesToggle`. A gateway that has not
    // restarted (坑 22) hands out the old snapshot, and the honest answer there is "no checkbox, no
    // axes" -- this pins which way that miss goes instead of leaving it to a stale default.
    expect(localAxesEnabled({ "viewport.localAxes": true }, true)).toBe(false);
  });
});

describe("a panel that has not changed is not rewritten (§7.2, Q136)", () => {
  it("reports no change when the markup already matches what is on screen", () => {
    // The list is rebuilt from this one string, so a write re-creates every row: the scroll offset
    // goes back to 0 and every thumbnail reloads. Clicking an asset changes the world, not this list.
    const list = '<ul><li>robot</li><li>table</li></ul>';
    expect(panelMarkupChanged(list, list)).toBe(false);
    expect(panelMarkupChanged("", "")).toBe(false);
  });

  it("reports a change the moment the rows differ", () => {
    // `treeRows()` writes `selected` into its markup, so hierarchy highlighting still gets its write;
    // the asset list, which carries no selection, is the one that keeps its scroll offset.
    expect(panelMarkupChanged("<li>robot</li>", "<li>robot</li><li>table</li>")).toBe(true);
    expect(panelMarkupChanged("<li>a</li>", "<li>b</li>")).toBe(true);
  });

  it("always paints the first time, whatever the panel turns out to hold", () => {
    // `null` is "nothing has been confirmed on screen yet" -- a different fact from "the panel is empty".
    expect(panelMarkupChanged(null, "<li>robot</li>")).toBe(true);
    expect(panelMarkupChanged(null, "")).toBe(true);
  });
});

describe("the rail's three asset tabs (§7.2, Q156)", () => {
  const onScenes = { panel: "assets", open: true, kind: "scene" };

  it("opens the panel on the 大类 that was pressed", () => {
    expect(panelTabPress({ panel: "scene", open: false, kind: "scene" }, "assets", "robot"))
      .toEqual({ open: true, kind: "robot" });
  });

  it("closes the panel when the 大类 already on show is pressed again", () => {
    expect(panelTabPress(onScenes, "assets", "scene")).toEqual({ open: false, kind: "scene" });
  });

  it("switches the 大类 without closing the panel - the tab is (panel, kind), not panel alone", () => {
    // Reading the tab as `panel` alone would call this a second press on the same tab and shut
    // the panel the user just asked to look at.
    expect(panelTabPress(onScenes, "assets", "model")).toEqual({ open: true, kind: "model" });
  });

  it("leaves the stored 大类 alone for the tabs that do not name one", () => {
    expect(panelTabPress(onScenes, "details")).toEqual({ open: true, kind: "scene" });
    expect(panelTabPress({ panel: "details", open: true, kind: "robot" }, "details"))
      .toEqual({ open: false, kind: "robot" });
  });

  it("lights exactly one of the three asset buttons", () => {
    const rail = ["scene", "model", "robot"].map((kind) => ({ panel: "assets", kind }));
    expect(rail.map((tab) => panelTabActive(tab, onScenes))).toEqual([true, false, false]);
    expect(rail.map((tab) => panelTabActive(tab, { ...onScenes, kind: "robot" }))).toEqual([false, false, true]);
  });

  it("lights nothing while the panel is closed, and still lights the plain tabs when it is open", () => {
    expect(panelTabActive({ panel: "assets", kind: "scene" }, { ...onScenes, open: false })).toBe(false);
    expect(panelTabActive({ panel: "details" }, { panel: "details", open: true, kind: "scene" })).toBe(true);
    expect(panelTabActive({ panel: "details" }, onScenes)).toBe(false);
  });
});

describe("the Agent column is lent to the asset panel (§7.5, Q157)", () => {
  const idle = { agentEnabled: true, contextEnabled: true, panel: "scene", panelOpen: true };

  it("gives the column back whenever the asset panel is not the one on show", () => {
    expect(agentPanelVisible(idle)).toBe(true);
    expect(agentPanelVisible({ ...idle, panel: "details" })).toBe(true);
    expect(agentPanelVisible({ ...idle, panel: "assets", panelOpen: false })).toBe(true);
  });

  it("takes the column only while the asset panel is actually on show", () => {
    expect(agentPanelVisible({ ...idle, panel: "assets" })).toBe(false);
  });

  it("keeps its own feature key the last word", () => {
    // Collapsing is not switching off: with agent.conversation off the panel stays gone even
    // after the asset panel closes, and the assets view never brings it back.
    expect(agentPanelVisible({ ...idle, agentEnabled: false })).toBe(false);
    expect(agentPanelVisible({ ...idle, agentEnabled: false, panel: "assets" })).toBe(false);
  });

  it("does not take the column when the context panel is switched off - nothing is showing then", () => {
    expect(agentPanelVisible({ ...idle, contextEnabled: false, panel: "assets" })).toBe(true);
  });
});

describe("the camera wall answers to the toolbar checkbox (§7.1, Q138)", () => {
  it("shows only when the flag hosts the checkbox and the checkbox is on", () => {
    const SHOWN = { "viewport.cameraWall": true };
    expect(cameraWallEnabled(SHOWN, true)).toBe(true);
    expect(cameraWallEnabled(SHOWN, false)).toBe(false);
  });

  it("keeps the wall down once the flag has taken the checkbox away", () => {
    // The third copy of the shape Q119 found and Q132 kept: no host means nothing to click, and a
    // panel that is not on screen is what the page shows anyway - so this key stays the one remote
    // exit for the whole layer, and there is no constant to fall back to the way §4.8.5 needed one.
    expect(cameraWallEnabled({ "viewport.cameraWall": false }, true)).toBe(false);
    expect(cameraWallEnabled({}, true)).toBe(false);
  });

  it("does not answer to the camera key the page already had", () => {
    // `viewport.cameraPreview` has gated the Details panel's preview since §4.4 and is on in every
    // snapshot; a gateway that has not restarted (坑 22) hands out one with no `viewport.cameraWall`
    // in it, and the honest reading of that is "no checkbox, no wall" rather than a borrowed yes.
    expect(cameraWallEnabled({ "viewport.cameraPreview": true }, true)).toBe(false);
  });
});

describe("the camera wall asks each sensor for a thumbnail (§4.4, Q138)", () => {
  it("takes its own cell when the runtime caps are roomier (§4.4, Q139)", () => {
    // This is the shipped `camera_preview` (640x480 @ 30) -- the wall shows every camera at once, so
    // it asks for a reading of each, not a frame anyone would measure pixels in. Q139 took the cell
    // down from 320x240: a wall of pictures is a glance, and the pace never moved with it.
    expect(cameraWallPace({ maxWidth: 640, maxHeight: 480, maxHz: 30 })).toEqual({
      maxWidth: 256, maxHeight: 192, intervalMs: 1000 / 12,
    });
  });

  it("takes the runtime cap when it is the tighter of the two", () => {
    // §9.1's numbers are a ceiling and never a target: a gateway that allows 160x120 at 5 Hz does not
    // offer the wall a bigger or faster picture than it offers the single preview next to it.
    expect(cameraWallPace({ maxWidth: 160, maxHeight: 120, maxHz: 5 })).toEqual({
      maxWidth: 160, maxHeight: 120, intervalMs: 200,
    });
  });

  it("beats no faster than its own ceiling and no slower than once a second", () => {
    // The floor is the load-bearing half: `1000 / maxHz` with a `maxHz` of 0 is `Infinity`, and
    // `setInterval` reads that as "as fast as this browser will run", not as "never".
    expect(cameraWallPace({ maxWidth: 640, maxHeight: 480, maxHz: 1000 }).intervalMs).toBe(1000 / 12);
    expect(cameraWallPace({ maxWidth: 640, maxHeight: 480, maxHz: 0 }).intervalMs).toBe(1000);
  });
});

describe("the wall's depth view is one the depth key still answers for (§4.4/§7.1, Q139)", () => {
  it("keeps the 深度 the user asked for while the key is on", () => {
    expect(cameraWallModeFor({ "viewport.cameraPreview.modes.depth": true }, "depth")).toBe("depth");
  });

  it("draws RGB instead once the key has taken depth away", () => {
    // One capability, one remote exit: `viewport.cameraPreview.modes.depth` already owns the Details
    // panel's depth preview (§4.4), so the wall borrows it instead of minting a fourth host key - and a
    // gateway that has not restarted hands out a snapshot where the key is simply absent (坑 22),
    // which §9.3 reads as off.
    expect(cameraWallModeFor({ "viewport.cameraPreview.modes.depth": false }, "depth")).toBe("rgb");
    expect(cameraWallModeFor({}, "depth")).toBe("rgb");
  });

  it("never refuses RGB and never erases the preference", () => {
    // `state.cameraWallMode` stays 深度 while the key is off, so switching the key back restores the view
    // the user left there. This judge answers "what shows now", not "what did they want".
    expect(cameraWallModeFor({}, "rgb")).toBe("rgb");
    expect(cameraWallModeFor({ "viewport.cameraPreview.modes.depth": false }, "rgb")).toBe("rgb");
    expect(cameraWallModeFor({ "viewport.cameraWall": false }, "rgb")).toBe("rgb");
  });
});

describe("one depth byte maps to one pseudo-colour (§4.4, Q139)", () => {
  it("runs blue at near to red at far, through cyan, green and yellow", () => {
    // Same ordering the grayscale pass already had - low depth dark, high depth light - so 深度 mode
    // changes how the picture reads, not what it says. §13.51's near/far sentence is still true.
    expect(depthColor(0)).toEqual([0, 0, 255]);
    expect(depthColor(0.25)).toEqual([0, 255, 255]);
    expect(depthColor(0.5)).toEqual([0, 255, 0]);
    expect(depthColor(0.75)).toEqual([255, 255, 0]);
    expect(depthColor(1)).toEqual([255, 0, 0]);
  });

  it("interpolates between the stops and clamps outside them", () => {
    expect(depthColor(0.125)).toEqual([0, 128, 255]);
    expect(depthColor(-3)).toEqual(depthColor(0));
    expect(depthColor(4)).toEqual(depthColor(1));
  });

  it("indexes the byte the sensor pass wrote, packed the way an ImageData wants it", () => {
    const table = depthPalette();
    expect(table.length).toBe(256);
    // Little-endian `0xAABBGGRR`, alpha full, so one assignment covers a pixel.
    expect(table[0]).toBe(0xffff0000);
    expect(table[255]).toBe(0xff0000ff);
    expect(Array.from(table).every((entry) => (entry >>> 24) === 0xff)).toBe(true);
    for (const index of [0, 64, 128, 200, 255]) {
      const [red, green, blue] = depthColor(index / 255);
      expect(table[index]).toBe(0xff000000 + (blue << 16) + (green << 8) + red);
    }
  });
});

describe("a take is offered the encoder it can really use (§4.8.7, Q121)", () => {
  it("prefers the profile with the most headroom in it", () => {
    // Both High profiles answer first because the table is ordered that way on purpose: this machine
    // writes `avc1.640028`/`avc1.4d001f` and answers yes to `video/mp4` too, so the plain one is the
    // last resort, not the first choice.
    expect(pickRecordMime((mime) => mime === "video/mp4")).toBe("video/mp4");
    expect(pickRecordMime((mime) => mime !== "video/mp4;codecs=avc1.4d001f")).toBe(
      "video/mp4;codecs=avc1.640028",
    );
    expect(pickRecordMime(() => true)).toBe(RECORD_MIME_CANDIDATES[0]);
  });

  it("refuses a browser that only knows profiles outside the table", () => {
    // The Baseline strings do encode on this machine - the earlier claim that they write 0 bytes was
    // read off a whole-frame average of a dark scene and is gone (see the table's doc). They stay out
    // because bare `video/mp4` is already answered as `Constrained Baseline`, so a browser that knows
    // only them has no encoder this page would name, and `null` means refuse the click.
    const baseline = (mime: string) => mime === "avc1.42001f" || mime === "avc1.42E01E";
    expect(pickRecordMime(baseline)).toBeNull();
    expect(pickRecordMime(() => false)).toBeNull();
    expect(RECORD_MIME_CANDIDATES.some((mime) => mime.includes("42001f") || mime.includes("42E01E"))).toBe(false);
    // And no WebM in the table either: the request was an `.mp4`, and a fallback container is a file
    // nobody asked for - `null` means "refuse with a toast", which is the other half of this ruling.
    expect(RECORD_MIME_CANDIDATES.some((mime) => mime.includes("webm"))).toBe(false);
  });

  it("names the file by the clock and not by the world on stage", () => {
    expect(recordVideoName(new Date(2026, 9, 24, 9, 7, 5))).toBe("viewport-20261024-090705.mp4");
    // Local time, zero padded; no scene name, because one take can cross two 现场.
    expect(recordVideoName(new Date(2026, 11, 31, 23, 59, 58))).toBe("viewport-20261231-235958.mp4");
    expect(recordVideoName(new Date(2026, 0, 1, 0, 0, 0))).toBe("viewport-20260101-000000.mp4");
  });

  it("keeps the record button behind a fail-closed key", () => {
    expect(featureEnabled({ "viewport.record": true }, "viewport.record")).toBe(true);
    expect(featureEnabled({ "viewport.record": false }, "viewport.record")).toBe(false);
    expect(featureEnabled({}, "viewport.record")).toBe(false);
  });
});

/**
 * §6.5 (**Q143**): `OBJLoader` reads a file in order, so a face above the `v` line it needs resolves
 * that vertex to `undefined` and the geometry lands on the stage as NaN -- which three then caches as
 * a NaN bounding box, and from there the §4.5 outline, the §4.8.6 wireframe, `/get2DAABB` and
 * `frame()` all read the same broken number. Six of the 210 OBJs in the shipped library are written
 * that way, all of them robotiq gripper parts. The fix is to read the file as a whole before parsing
 * it, which is exact for one reason and conditional for another: an OBJ index is absolute and counts
 * from the start of the file, so moving a declaration changes nothing -- *unless* the index is
 * negative, which means "count back from the vertices declared so far". Those are rewritten into the
 * absolute index they already mean, in the same pass and at their own line, so no future asset can
 * fall between the two rules.
 */
describe("OBJ source whose faces index vertices the file declares later (§6.5, Q143)", () => {
  /** One vertex after the face that needs it: the mistake `pad.obj` makes 900 times, in miniature. */
  const pad = "o Pad\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvn 0 0 1\nf 1//1 2//1 3//1\nf 1//1 3//1 4//1\nv 0 0 1\n";

  it("moves every declaration above the faces that index them", () => {
    expect(normalizeObjSource(pad)).toBe(
      "v 0 0 0\nv 1 0 0\nv 0 1 0\nvn 0 0 1\nv 0 0 1\no Pad\nf 1//1 2//1 3//1\nf 1//1 3//1 4//1\n",
    );
  });

  it("returns a file that needs nothing byte for byte", () => {
    const inOrder = "o A\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n";
    expect(normalizeObjSource(inOrder)).toBe(inOrder);
    // Declaring vertices after *a* face is legal and common -- each object declares its own before its
    // own faces. 22 of the 210 shipped files look like this and must not be rewritten: the trigger is
    // a face reaching past the declarations above it, not the mere order of the lines.
    const grouped = "o A\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\no B\nv 2 0 0\nv 2 1 0\nv 2 0 1\nf 4 5 6\n";
    expect(normalizeObjSource(grouped)).toBe(grouped);
  });

  it("turns a negative index into the absolute one it means, so the move cannot change it", () => {
    expect(normalizeObjSource("v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\nl -3 -2\nf 1 2 4\nv 5 5 5\n"))
      .toBe("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 5 5 5\nf 1 2 3\nl 1 2\nf 1 2 4\n");
    // A file that only uses negative indices needs no move at all, so it is left exactly as it came:
    // the loader resolves those against the declarations above the line, which stay above it.
    const relative = "v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\n";
    expect(normalizeObjSource(relative)).toBe(relative);
  });

  it("leaves a reference that is broken where it stands broken the same way", () => {
    // `-5` with nothing declared above it points past the start of the file. Inventing a vertex for it
    // would turn a defective asset into a wrong one.
    expect(normalizeObjSource("f -5 -1 -1\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 5\n"))
      .toBe("v 0 0 0\nv 1 0 0\nv 0 1 0\nf -5 -1 -1\nf 1 2 5\n");
  });

  it("counts vt and vn in their own slots, and never rewrites a positive index", () => {
    // The face reaches past the normals declared above it -- slot 2, not slot 0 -- so the `vn` moves
    // and the face text stays as it was written.
    expect(normalizeObjSource("v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nf 1/1/2 2/2/2 3/3/2\nvn 0 0 1\n"))
      .toBe("v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nvn 0 0 1\nf 1/1/2 2/2/2 3/3/2\n");
  });

  it("sees the lines the loader will see: continuations joined, indentation trimmed", () => {
    expect(normalizeObjSource("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 \\\n4\nv 9 9 9\n"))
      .toBe("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 9 9 9\nf 1 2 4\n");
    // An indented statement is a statement -- the loader trims before it reads the first character --
    // and a line whose indices are already absolute keeps the bytes it came with.
    expect(normalizeObjSource("v 0 0 0\nv 1 0 0\nv 0 1 0\n\tf 1 2 4\n v 9 9 9\n"))
      .toBe("v 0 0 0\nv 1 0 0\nv 0 1 0\n v 9 9 9\n\tf 1 2 4\n");
  });
});

describe("the box an OBJ's vertices really occupy (§4.6, Q144)", () => {
  it("reads the extremes of the v lines, in the file's own frame", () => {
    expect(objVertexBounds("v 0.046532 0.065627 0.114619\nv -0.04999 -0.065113 -5e-06\n")).toEqual({
      min: [-0.04999, -0.065113, -5e-06],
      max: [0.046532, 0.065627, 0.114619],
    });
  });

  it("counts only positions -- vt and vn are texture and normal data, not corners of the mesh", () => {
    expect(objVertexBounds("vt 5 5\nvn 0 0 9\nv 1 2 3\n")).toEqual({ min: [1, 2, 3], max: [1, 2, 3] });
  });

  it("does not care whether a vertex is declared before or after the face that indexes it", () => {
    // The model-23 shape: the second corner is written below the face, and the bounds still see it.
    expect(objVertexBounds("v -1 -1 0\nf 1 2 1\nv 1 1 2\n")).toEqual({ min: [-1, -1, 0], max: [1, 1, 2] });
  });

  it("skips a vertex that is not three finite numbers, and reports null when none is left", () => {
    expect(objVertexBounds("v 0 0 0\nv nan 1 1\nv 2 2 2 extra\n")).toEqual({ min: [0, 0, 0], max: [2, 2, 2] });
    expect(objVertexBounds("vt 1 1\nvn 0 0 1\nf 1 1 1\n")).toBeNull();
    expect(objVertexBounds("")).toBeNull();
  });
});
