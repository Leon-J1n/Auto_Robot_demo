import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { createSerialQueue } from "./serial_queue";

describe("library edit queue", () => {
  it("does not start the next scene edit before the current one finishes", async () => {
    const enqueue = createSerialQueue();
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    const order: string[] = [];
    let active = 0;
    let peak = 0;
    const run = (name: string, gate?: Promise<void>) => enqueue(async () => {
      active += 1;
      peak = Math.max(peak, active);
      order.push(`${name}:start`);
      await gate;
      order.push(`${name}:end`);
      active -= 1;
    });

    const first = run("first", firstGate);
    const second = run("second");
    await Promise.resolve();
    expect(order).toEqual(["first:start"]);
    releaseFirst();
    await Promise.all([first, second]);
    expect(order).toEqual(["first:start", "first:end", "second:start", "second:end"]);
    expect(peak).toBe(1);
  });

  it("continues after a failed placement and wires the library action through the queue", async () => {
    const enqueue = createSerialQueue();
    await expect(enqueue(async () => { throw new Error("placement failed"); })).rejects.toThrow("placement failed");
    await expect(enqueue(async () => "next placement")).resolves.toBe("next placement");

    const main = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
    expect(main).toMatch(/const enqueueLibraryAction\s*=\s*createSerialQueue\(\)/);
    expect(main).toMatch(/return enqueueLibraryAction\(\(\)\s*=>\s*performLibraryItemChoice\(kind, id\)\)/);
  });
});
