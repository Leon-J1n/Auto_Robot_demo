/**
 * Lighting presets the viewport toolbar can switch between (§4.8.1, Q77, Q114 ③).
 *
 * A preset is a **render-layer** override: the renderer lights the stage with these
 * parameters and nothing here is ever written into the world document, so an export,
 * `/getWorld`, `/captureScene`'s reported 现场 and the scene digest keep describing the
 * lights the file declares. Each entry carries only the three `Light` fields the parser
 * reads plus a pose (场景文件描述 §4.5): `range`, `decay` and the Spot cone are not
 * consumed, so writing them here would be a knob that turns nothing.
 */
import type { LightProps, QuatXyzw, Vec3 } from "../../contracts/types";

export interface PresetLight {
  light: LightProps;
  /** metres in the Z-up world frame the scene file is written in */
  position: Vec3;
  /** the direction the beam travels; local -Z is pointed along it (§4.5) */
  beam?: Vec3;
}

export interface LightingPreset {
  id: string;
  label: string;
  lights: PresetLight[];
}

/** One preset light in the shape the renderer places, ready and copied out of the table. */
export interface PresetLightSource {
  light: LightProps;
  position: Vec3;
  quaternion: QuatXyzw;
}

const IDENTITY: QuatXyzw = [0, 0, 0, 1];

/** Rotate local -Z onto `beam`, so the light the renderer aims along -Z travels where the table says. */
export function beamQuaternion(beam: Vec3): QuatXyzw {
  const length = Math.hypot(...beam);
  if (length === 0) return [...IDENTITY] as QuatXyzw;
  // The arc formula swings +Z, so the beam is fed to it backwards: -Z is what ends up on the beam.
  const [x, y, z] = beam.map((component) => -component / length) as Vec3;
  // A straight-up or straight-down anti-beam is one axis and a half-turn; the general formula stalls there.
  if (x === 0 && y === 0) return z > 0 ? [...IDENTITY] as QuatXyzw : [1, 0, 0, 0];
  const quaternion = [-y, x, 0, 1 + z] as QuatXyzw;
  const norm = Math.hypot(...quaternion);
  return quaternion.map((component) => component / norm) as QuatXyzw;
}

/**
 * The five presets. `neutral` copies the two lights every installed scene ships with
 * (Ambient 0.5 plus a directional 3 m over the table), so switching to it is the
 * documented way back from an experimental preset without reloading the scene.
 */
export const LIGHTING_PRESETS: LightingPreset[] = [
  {
    id: "neutral",
    label: "Neutral",
    lights: [
      { light: { type: "Ambient", intensity: 0.5, color: "#FFFFFF" }, position: [0, 0, 0] },
      { light: { type: "Directional", intensity: 0.7, color: "#FFFFFF" }, position: [0.525, 0, 3] },
    ],
  },
  {
    id: "studio",
    label: "Studio",
    lights: [
      { light: { type: "Ambient", intensity: 0.22, color: "#FFFFFF" }, position: [0, 0, 0] },
      { light: { type: "Directional", intensity: 0.9, color: "#FFF6E8" }, position: [2.2, -1.6, 3], beam: [-0.5, 0.4, -0.75] },
      { light: { type: "Directional", intensity: 0.45, color: "#E8F1FF" }, position: [-2.4, 1.8, 2.2], beam: [0.55, -0.4, -0.6] },
    ],
  },
  {
    id: "dramatic",
    label: "Dramatic",
    lights: [
      { light: { type: "Ambient", intensity: 0.08, color: "#2A3038" }, position: [0, 0, 0] },
      { light: { type: "Directional", intensity: 1.4, color: "#FFD9A0" }, position: [3.2, 0.6, 1.4], beam: [-0.9, -0.15, -0.35] },
    ],
  },
  {
    id: "overcast",
    label: "Overcast",
    lights: [
      { light: { type: "Ambient", intensity: 0.75, color: "#EAF0FF" }, position: [0, 0, 0] },
      { light: { type: "Directional", intensity: 0.25, color: "#FFFFFF" }, position: [0.4, 0, 4] },
    ],
  },
  {
    id: "lamp",
    label: "Lamp",
    lights: [
      { light: { type: "Ambient", intensity: 0.12, color: "#FFF1D6" }, position: [0, 0, 0] },
      // A Point lights in every direction, so its beam is only the marker's orientation.
      { light: { type: "Point", intensity: 1.6, color: "#FFC46B" }, position: [0.5, 0, 1.9], beam: [0, 0, -1] },
    ],
  },
];

export const LIGHTING_PRESET_BY_ID = new Map(LIGHTING_PRESETS.map((preset) => [preset.id, preset]));

/**
 * The render-layer shape of one preset (§4.5 Transform and Light, kept apart from the
 * world file's `ObjectRequest` so a preset can never be mistaken for a scene object).
 * The pose is copied out of the table, so a caller cannot write through to the constant.
 */
export function presetLightSources(preset: LightingPreset): PresetLightSource[] {
  return preset.lights.map((entry) => ({
    light: { ...entry.light },
    position: [...entry.position] as Vec3,
    quaternion: entry.beam ? beamQuaternion(entry.beam) : [...IDENTITY] as QuatXyzw,
  }));
}
