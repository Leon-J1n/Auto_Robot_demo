/**
 * §5.13 from the browser's side: every error code this build can raise must be a code
 * the gateway knows, because `BROWSER_ERROR_STATUS.get(code, 502)` silently turns an
 * unknown code into "upstream broke". M7-1 found the mirror image of that defect
 * (`INTERNAL_ERROR` missing from the table); this pins the other half, so renaming a
 * code here without touching §5.13 fails a test instead of changing a status code.
 *
 * `main.ts` forwards the raised code verbatim, so pinning the raise sites pins the wire.
 */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * Raised only inside a render pass: `renderer.ts:loadModel` catches it and reports one
 * diagnostic (§6.8), so it never becomes a `browser.response` error and needs no row in
 * §5.13. `renderer.test.ts` proves that containment; adding a code here without that
 * proof is how a 400 silently becomes a 502.
 */
const RENDER_ONLY = new Set(["INVALID_URDF"]);

/**
 * Mapped by the gateway but not raised by any handler yet: §5.13 reserves them for a
 * browser that knows which scene it was asked about. Listing them keeps a typo in a
 * real code from hiding behind an unused table row.
 */
const RESERVED_BY_SPEC = new Set(["SCENE_NOT_FOUND", "SCENE_NOT_LOADED"]);

const RAISE = /new (?:WorldError|LibraryError|TrajError)\(\s*"([A-Z][A-Z0-9_]*)"/g;

function raisedCodes(): Map<string, string[]> {
  const sourceDir = fileURLToPath(new URL(".", import.meta.url));
  const found = new Map<string, string[]>();
  for (const name of readdirSync(sourceDir).sort()) {
    if (!name.endsWith(".ts") || name.endsWith(".test.ts")) continue;
    const text = readFileSync(`${sourceDir}/${name}`, "utf-8");
    for (const [, code] of text.matchAll(RAISE)) {
      if (!code) continue;
      found.set(code, [...(found.get(code) ?? []), name]);
    }
  }
  return found;
}

function gatewayTable(): Map<string, number> {
  const python = readFileSync(new URL("../../local_gateway.py", import.meta.url), "utf-8");
  const [, block] = python.match(/BROWSER_ERROR_STATUS: dict\[str, int\] = \{([\s\S]*?)\n\}/) ?? [];
  if (!block) throw new Error("local_gateway.py no longer declares BROWSER_ERROR_STATUS as a literal dict");
  const table = new Map<string, number>();
  for (const [, code, status] of block.matchAll(/"([A-Z0-9_]+)":\s*(\d+)/g)) {
    if (!code || !status) continue;
    table.set(code, Number(status));
  }
  return table;
}

describe("§5.13 code coverage across the channel", () => {
  const raised = raisedCodes();
  const mapped = gatewayTable();

  it("raises only codes the gateway can translate", () => {
    const unmapped = [...raised.keys()].filter((code) => !mapped.has(code) && !RENDER_ONLY.has(code));
    expect(unmapped).toEqual([]);
  });

  it("keeps the render-only codes render-only", () => {
    // A code that stops being raised anywhere must not stay in the exception list.
    const unraised = [...RENDER_ONLY].filter((code) => !raised.has(code));
    expect(unraised).toEqual([]);
    const overreaching = [...RENDER_ONLY].filter((code) => mapped.has(code));
    expect(overreaching).toEqual([]);
  });

  it("does not let a mapped code go unused without saying so", () => {
    const silent = [...mapped.keys()].filter((code) => !raised.has(code) && !RESERVED_BY_SPEC.has(code));
    expect(silent).toEqual([]);
    const stale = [...RESERVED_BY_SPEC].filter((code) => raised.has(code));
    expect(stale).toEqual([]);
  });

  it("carries the §5.13 families the specification names", () => {
    const families = {
      "400 INVALID_*": ["MISSING_PARAM", "INVALID_PARAM", "INVALID_WORLD_SCHEMA", "BODY_ID_MISMATCH",
        "MISSING_TRANSFORM_COMPONENT", "INVALID_PARENT_CYCLE", "CAMERA_TYPE_MISMATCH", "EMPTY_AABB"],
      "400 TRAJ_*": ["TRAJ_INVALID_MAGIC", "TRAJ_UNSUPPORTED_VERSION", "TRAJ_TRUNCATED", "TRAJ_POSE_DIM_UNSUPPORTED"],
      "404 *_NOT_FOUND": ["OBJECT_NOT_FOUND", "CAMERA_NOT_FOUND", "MODEL_NOT_FOUND"],
      "409": ["ID_ALREADY_EXISTS", "VERSION_CONFLICT"],
      "422/424": ["MODEL_METADATA_INVALID", "DEPENDENCY_FETCH_FAILED"],
      "5xx": ["INTERNAL_ERROR", "UPSTREAM_ERROR", "UPSTREAM_RENDER_FAILED", "GATEWAY_FRAMING_ERROR"],
    };
    for (const [family, codes] of Object.entries(families)) {
      for (const code of codes) {
        expect(mapped.has(code), `${family}: ${code} must stay mapped`).toBe(true);
      }
    }
  });
});
