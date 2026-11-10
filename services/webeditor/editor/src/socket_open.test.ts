/**
 * §5.8 (2026-11-05): which socket open is allowed to pull the authoritative world.
 *
 * `main.ts` wires the DOM on import, so it has no importable surface and this pins the
 * `onopen` body as written. The rule it protects is about *when an answer may land*: a pull
 * resolves later than it started, and `loadAuthoritativeWorld` writes whatever comes back
 * straight into `state.world` and the scene graph. `start()` already awaits one pull before
 * the socket exists, and that is the only pull that cannot race a user action -- the page is
 * not interactive yet. The `onopen` pull is a second read of the same document whose roundtrip
 * overlaps the asset loading a preset-scene click just started, so its older answer lands on
 * top of the newer scene and the scene the user loaded flashes back to what the gateway held
 * before (colleague report, 2026-11-05). A reconnect is not the same case: the tab was deaf,
 * the gateway may be a new process, and reading before reporting is the catch-up.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const mainSource = readFileSync(new URL("./main.ts", import.meta.url), "utf-8");

function onOpenBody(): string {
  const start = mainSource.indexOf("connection.onopen = () => {");
  expect(start, "main.ts no longer assigns connection.onopen").toBeGreaterThanOrEqual(0);
  const end = mainSource.indexOf("connection.binaryType", start);
  expect(end, "the onopen body no longer ends at connection.binaryType").toBeGreaterThan(start);
  return mainSource.slice(start, end);
}

function branch(before: string, after: string): string {
  const [, body] = onOpenBody().match(new RegExp(`${before}([\\s\\S]*?)${after}`)) ?? [];
  expect(body, `main.ts onopen no longer has a ${before} branch`).toBeDefined();
  return body as string;
}

describe("§5.8 the authoritative read a socket open may make", () => {
  it("pulls the gateway's world on a reconnect only", () => {
    const reconnect = branch("if \\(firstSocketOpenDone\\) \\{", "\\} else \\{");
    expect(reconnect).toContain("loadAuthoritativeWorld()");
    expect(reconnect).toContain("reportWorld()");
    // One read lives in the whole onopen body, and it is this one.
    expect(onOpenBody().match(/loadAuthoritativeWorld\(/g) ?? []).toHaveLength(1);
  });

  it("pushes the world it already holds on the page's first open", () => {
    const firstOpen = branch("\\} else \\{", "\\n    \\}");
    expect(firstOpen).toContain("firstSocketOpenDone = true;");
    expect(firstOpen).toContain("reportWorld()");
    expect(firstOpen).not.toContain("loadAuthoritativeWorld(");
  });

  it("still re-reports on every open (§5.5/Q112) and reads once in start() before the socket exists", () => {
    expect(onOpenBody().match(/reportWorld\(\)/g) ?? []).toHaveLength(2);
    const startup = mainSource.slice(mainSource.indexOf("async function start("));
    const read = startup.indexOf("await loadAuthoritativeWorld()");
    expect(read, "start() no longer reads the authoritative world").toBeGreaterThanOrEqual(0);
    expect(startup.indexOf("connectWebSocket()"), "start() opens the socket before reading").toBeGreaterThan(read);
  });
});
