import { describe, expect, it } from "vitest";
import type { QuatXyzw, Vec3 } from "../../contracts/types";
import { beamQuaternion, LIGHTING_PRESET_BY_ID, LIGHTING_PRESETS, presetLightSources } from "./lighting";
import type { PresetLight } from "./lighting";

/** The presets are a constant, so a missing one is a broken table, not a test input. */
function presetLight(id: string, index: number): PresetLight {
  const light = LIGHTING_PRESET_BY_ID.get(id)?.lights[index];
  if (!light) throw new Error(`preset ${id} has no light ${index}`);
  return light;
}

/** `q v q^-1` written out, so the assertion does not lean on the same math as the source. */
function rotate([vx, vy, vz]: Vec3, [qx, qy, qz, qw]: QuatXyzw): Vec3 {
  const tx = 2 * (qy * vz - qz * vy);
  const ty = 2 * (qz * vx - qx * vz);
  const tz = 2 * (qx * vy - qy * vx);
  return [vx + qw * tx + (qy * tz - qz * ty), vy + qw * ty + (qz * tx - qx * tz), vz + qw * tz + (qx * ty - qy * tx)];
}

function normalized(vector: Vec3): Vec3 {
  const length = Math.hypot(...vector);
  return vector.map((component) => component / length) as Vec3;
}

describe("beamQuaternion (场景文件描述 §4.5)", () => {
  it("points the beam of travel onto the local -Z the renderer lights along", () => {
    const beams: Vec3[] = [[-0.5, 0.4, -0.75], [0, 0, -1], [1, 0, 0], [0.2, -0.9, 0.35], [0, 1, 0]];
    for (const beam of beams) {
      const emitted = rotate([0, 0, -1], beamQuaternion(beam));
      expect(emitted[0]).toBeCloseTo(normalized(beam)[0], 6);
      expect(emitted[1]).toBeCloseTo(normalized(beam)[1], 6);
      expect(emitted[2]).toBeCloseTo(normalized(beam)[2], 6);
    }
  });

  it("stays unit length for a beam that is not normalised", () => {
    const quaternion = beamQuaternion([2, -3, 5]);
    expect(Math.hypot(...quaternion)).toBeCloseTo(1, 12);
  });

  /** The half-turn branch: the general formula divides by `1 + z`, which is zero pointing up. */
  it("takes the one-axis shortcut straight up and down instead of a zero-length quaternion", () => {
    expect(beamQuaternion([0, 0, -1])).toEqual([0, 0, 0, 1]);
    expect(beamQuaternion([0, 0, 1])).toEqual([1, 0, 0, 0]);
    expect(rotate([0, 0, -1], beamQuaternion([0, 0, -1]))).toEqual([0, 0, -1]);
  });

  it("leaves the pose alone for a beam it cannot read", () => {
    expect(beamQuaternion([0, 0, 0])).toEqual([0, 0, 0, 1]);
  });
});

describe("LIGHTING_PRESETS (§4.8.1, Q77)", () => {
  it("keys the dropdown by a unique id for every preset", () => {
    const ids = LIGHTING_PRESETS.map((preset) => preset.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(LIGHTING_PRESET_BY_ID.get("studio")?.label).toBe("Studio");
    expect(LIGHTING_PRESET_BY_ID.size).toBe(ids.length);
    for (const preset of LIGHTING_PRESETS) expect(preset.lights.length).toBeGreaterThan(0);
  });

  /**
   * §4.8.1 (**Q133**): the dropdown option and the `光照已切换：<预设名>` toast read the same
   * `label`, so a parenthetical there is a mechanism lecture on the interface twice over.
   */
  it("names every preset with a bare word, no parenthetical", () => {
    for (const preset of LIGHTING_PRESETS) {
      expect(preset.label).not.toMatch(/[()（）]/);
      expect(preset.label.trim()).toBe(preset.label);
      expect(preset.label.length).toBeGreaterThan(0);
    }
  });

  /** §4.5: the renderer reads three fields, so a fourth would be a knob that turns nothing. */
  it("writes only the three Light fields the parser reads", () => {
    for (const preset of LIGHTING_PRESETS) {
      for (const light of preset.lights) {
        expect(Object.keys(light.light).sort()).toEqual(["color", "intensity", "type"]);
        expect(["Ambient", "Directional", "Point", "Spot"]).toContain(light.light.type);
      }
    }
  });

  /** `neutral` is the documented way back without reloading, so it has to match the shipped rig. */
  it("copies the scene's own two lights in the neutral preset", () => {
    const neutral = LIGHTING_PRESET_BY_ID.get("neutral");
    expect(neutral?.lights.map((light) => light.light)).toEqual([
      { type: "Ambient", intensity: 0.5, color: "#FFFFFF" },
      { type: "Directional", intensity: 0.7, color: "#FFFFFF" },
    ]);
    expect(presetLight("neutral", 1).position).toEqual([0.525, 0, 3]);
  });
});

describe("presetLightSources (§4.8.1, Q114 ③)", () => {
  it("hands the renderer a pose, never an object request", () => {
    const [ambient, key] = presetLightSources(LIGHTING_PRESET_BY_ID.get("neutral")!);
    expect(ambient).toEqual({
      light: { type: "Ambient", intensity: 0.5, color: "#FFFFFF" },
      position: [0, 0, 0],
      quaternion: [0, 0, 0, 1],
    });
    expect(key?.light).toEqual({ type: "Directional", intensity: 0.7, color: "#FFFFFF" });
    expect(key?.position).toEqual([0.525, 0, 3]);
  });

  it("orientates only the lights that carry a beam", () => {
    const sources = presetLightSources(LIGHTING_PRESET_BY_ID.get("dramatic")!);
    expect(sources[0]?.quaternion).toEqual([0, 0, 0, 1]);
    expect(sources[1]?.quaternion).toEqual(beamQuaternion([-0.9, -0.15, -0.35]));
  });

  /** The table is a constant: a render pass must not be able to write through into it. */
  it("copies the pose and the fields rather than sharing arrays with the preset", () => {
    const preset = LIGHTING_PRESET_BY_ID.get("studio")!;
    const sources = presetLightSources(preset);
    sources[1]!.position[0] = 99;
    sources[1]!.light.intensity = 0;
    expect(preset.lights[1]?.position[0]).toBe(2.2);
    expect(preset.lights[1]?.light.intensity).toBe(0.9);
  });
});
