import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page, request }) => {
  const snapshot = await (await request.get("/api/bootstrap")).json();
  let revision = snapshot.revision as number;
  const roots = (snapshot.entities as Array<{ id: number; parent: number | null }>).filter(
    (entity) => entity.id !== 0 && entity.parent === 0,
  );
  for (const entity of roots) {
    const response = await request.post("/api/commands", { data: {
      requestId: crypto.randomUUID(), sceneId: "main", baseRevision: revision,
      method: "scene.delete_entity", params: { entity: entity.id },
    } });
    if (response.ok()) revision = (await response.json()).revision;
  }
  await page.goto("/");
  await expect(page.locator("#status-primary")).toContainText("Connected", { timeout: 15_000 });
});

test("toggles the context panel from navigation", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile-390", "Desktop context panel workflow");
  const contextPanel = page.locator("#context-panel");
  const sceneNavigation = page.locator("[data-panel=scene]");
  const detailsNavigation = page.locator("[data-panel=details]");

  await expect(contextPanel).toBeHidden();
  await expect(sceneNavigation).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#viewport")).toBeVisible();

  await sceneNavigation.click();
  await expect(contextPanel).toBeVisible();
  await expect(sceneNavigation).toHaveAttribute("aria-expanded", "true");
  await sceneNavigation.click();
  await expect(contextPanel).toBeHidden();
  await expect(page.locator("#viewport")).toBeVisible();

  await detailsNavigation.click();
  await expect(contextPanel).toBeVisible();
  await expect(page.locator("#panel-title")).toHaveText("Details");
  await expect(detailsNavigation).toHaveAttribute("aria-expanded", "true");
  await detailsNavigation.click();
  await expect(contextPanel).toBeHidden();
  await expect(detailsNavigation).toHaveAttribute("aria-expanded", "false");
});

test("edits a native scene and exposes the Qt parity controls", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile-390", "Desktop editing workflow");
  await page.locator("#create-kind").selectOption("cube");
  await page.locator("[data-panel=scene]").click();
  await expect(page.locator(".tree-row", { hasText: "Cube" })).toBeVisible();
  await page.locator(".tree-row", { hasText: "Cube" }).click();
  await expect(page.locator("#selection-label")).toContainText("Cube");

  await page.locator("[data-panel=details]").click();
  await expect(page.locator("#inspector-form")).toBeVisible();
  await page.locator("input[name=name]").fill("E2E Cube");
  await page.locator("input[name=position-0]").fill("2.5");
  await page.getByRole("button", { name: "Apply Changes" }).click();
  await page.locator("[data-panel=scene]").click();
  await expect(page.locator(".tree-row", { hasText: "E2E Cube" })).toBeVisible();
  await page.locator("[data-command='editor.undo']").click();
  await expect(page.locator(".tree-row", { hasText: "Cube" })).toBeVisible();
  await page.locator("[data-command='editor.redo']").click();
  await expect(page.locator(".tree-row", { hasText: "E2E Cube" })).toBeVisible();

  await page.locator("[data-command='simulation.start']").click();
  await expect(page.locator("#status-primary")).toContainText("running");
  await page.locator("[data-command='simulation.pause']").click();
  await expect(page.locator("#status-primary")).toContainText("paused");
  await page.locator("[data-command='simulation.step']").click();
  await page.locator("[data-command='state.save']").click();
  await page.locator("[data-command='state.restore']").click();

  await page.locator("[data-panel=settings]").click();
  await expect(page.getByText("Platform Renderer", { exact: true })).toBeVisible();
  await expect(page.getByText("Browser Renderer", { exact: true })).toBeVisible();
  await page.locator("[data-panel=assets]").click();
  await expect(page.getByText("Built-in", { exact: true })).toBeVisible();
  await expect(page.getByText("Project Assets", { exact: true })).toBeVisible();
  await expect(page.getByText("Uploaded", { exact: true })).toBeVisible();

  const canvas = page.locator("#viewport");
  await expect(canvas).toBeVisible();
  const canvasBox = await canvas.boundingBox();
  expect(canvasBox?.width).toBeGreaterThan(400);
  expect(canvasBox?.height).toBeGreaterThan(400);
});

test("creates a world camera and reports real Agent and sensor states", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile-390", "Desktop panel workflow");
  await page.locator("#create-kind").selectOption("camera-rgbd");
  await page.locator("[data-panel=scene]").click();
  await expect(page.locator(".tree-row", { hasText: "RGBD Camera" }).last()).toBeVisible();
  await page.locator(".tree-row", { hasText: "RGBD Camera" }).last().click();
  await page.locator("[data-panel=details]").click();
  await expect(page.locator("#inspector-form").getByText("Camera", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open Preview" }).click();
  await expect(page.locator("#camera-preview")).toBeVisible();
  await expect(page.locator("#preview-status")).toHaveText("streaming", { timeout: 15_000 });

  await page.locator("#agent-input").fill("create a sphere");
  await page.locator("#agent-form button[type=submit]").click();
  await expect(page.locator(".message.user")).toContainText("create a sphere");
  await expect(page.locator(".message.agent").last()).toContainText(/Queued|not connected|refused|failed/i, { timeout: 20_000 });

  const rail = await page.locator(".rail").boundingBox();
  const context = await page.locator(".context-panel").boundingBox();
  const viewport = await page.locator(".viewport-shell").boundingBox();
  const agent = await page.locator(".agent-panel").boundingBox();
  expect(rail!.x + rail!.width).toBeLessThanOrEqual(context!.x + 1);
  expect(context!.x + context!.width).toBeLessThanOrEqual(viewport!.x + 1);
  expect(viewport!.x + viewport!.width).toBeLessThanOrEqual(agent!.x + 1);
});

test("reviews a published timeline with seek play pause and speed controls", async ({ page, request }) => {
  test.setTimeout(60_000);
  const snapshot = await (await request.get("/api/bootstrap")).json();
  const frames = [0, 0.1, 0.2].map((simulationTime, index) => ({
    ...snapshot, simulationTime, revision: snapshot.revision + index,
  }));
  const timeline = {
    timelineId: "e2e-timeline", sourceTurnId: "e2e-turn", frameCount: 3,
    durationSeconds: 0.2, defaultFps: 30,
    frames: frames.map((frame, index) => ({
      index, simulationTime: frame.simulationTime, sceneRevision: frame.revision,
      snapshotUrl: `/api/timelines/e2e-timeline/frames/${index}`,
    })),
  };
  await page.route("**/api/bootstrap", async (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      ...snapshot,
      presentation: { mode: "sync-and-animate", revision: 1, timeline },
    }),
  }));
  await page.route("**/api/timelines/e2e-timeline/frames/*", async (route) => {
    const index = Number(route.request().url().split("/").pop());
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(frames[index]) });
  });
  await page.reload();

  await expect(page.locator("#timeline")).toBeVisible();
  await expect(page.locator("[data-command='simulation.start']")).toBeDisabled();
  await page.locator("#timeline-rate").selectOption("4");
  await page.locator('[data-timeline="last"]').click();
  await expect(page.locator("#timeline-time")).toContainText("0.20 / 0.20 s");
  await page.locator('[data-timeline="first"]').click();
  await page.locator('[data-timeline="play"]').click();
  await expect(page.locator("#timeline-time")).toContainText("0.20 / 0.20 s", { timeout: 5_000 });
  await page.locator("#timeline-range").fill("1");
  await expect(page.locator("#timeline-time")).toContainText("0.10 / 0.20 s");
});

test("reconnects the event stream and refreshes the scene", async ({ page, context }, testInfo) => {
  test.setTimeout(60_000);
  test.skip(testInfo.project.name !== "desktop-1440", "One browser project covers reconnect");
  await context.setOffline(true);
  await expect(page.locator("#status-primary")).toContainText("Disconnected", { timeout: 10_000 });
  await context.setOffline(false);
  await expect(page.locator("#status-primary")).toContainText("Connected", { timeout: 10_000 });
  const authoritative = await (await context.request.get("/api/bootstrap")).json();
  await expect(page.locator("#status-revision")).toHaveText(`Rev ${authoritative.revision}`);
});

test("keeps the mobile review viewport and timeline inside the screen", async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-390", "Mobile layout gate");
  const snapshot = await (await request.get("/api/bootstrap")).json();
  const frames = [0, 0.1].map((simulationTime, index) => ({
    ...snapshot, simulationTime, revision: snapshot.revision + index,
  }));
  const timeline = {
    timelineId: "mobile-timeline", sourceTurnId: "mobile-turn", frameCount: 2,
    durationSeconds: 0.1, defaultFps: 30,
    frames: frames.map((frame, index) => ({
      index, simulationTime: frame.simulationTime, sceneRevision: frame.revision,
      snapshotUrl: `/api/timelines/mobile-timeline/frames/${index}`,
    })),
  };
  await page.route("**/api/bootstrap", async (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      ...snapshot,
      presentation: { mode: "sync-and-animate", revision: 1, timeline },
    }),
  }));
  await page.route("**/api/timelines/mobile-timeline/frames/*", async (route) => {
    const index = Number(route.request().url().split("/").pop());
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(frames[index]) });
  });
  await page.reload();

  await expect(page.locator("#timeline")).toBeVisible();
  await expect(page.locator(".context-panel")).toBeHidden();
  await expect(page.locator(".agent-panel")).toBeHidden();
  const layout = await page.evaluate(() => ({
    viewportWidth: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    timeline: document.querySelector("#timeline")!.getBoundingClientRect().toJSON(),
    viewport: document.querySelector(".viewport-shell")!.getBoundingClientRect().toJSON(),
  }));
  expect(layout.bodyWidth).toBeLessThanOrEqual(layout.viewportWidth);
  expect(layout.timeline.x).toBeGreaterThanOrEqual(0);
  expect(layout.timeline.x + layout.timeline.width).toBeLessThanOrEqual(layout.viewportWidth);
  expect(layout.viewport.width).toBeGreaterThan(300);
  await page.close({ runBeforeUnload: true });
});

test("keeps the 1000 entity viewport above 30 FPS", async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1440", "The performance gate uses the baseline viewport");
  const snapshot = await (await request.get("/api/bootstrap")).json();
  const response = await request.post("/api/commands", { data: {
    requestId: crypto.randomUUID(), sceneId: "main", baseRevision: snapshot.revision,
    method: "scene.create_primitives", params: { kind: "cube", count: 1000, spacing: 0.3 },
  } });
  expect(response.ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator("#status-primary")).toContainText("Connected", { timeout: 20_000 });
  await page.locator("[data-panel=scene]").click();
  await page.locator("[data-action=frame]").click();
  await expect.poll(async () => {
    const text = await page.locator("#status-performance").innerText();
    return Number(text.match(/([0-9.]+) FPS/)?.[1] ?? 0);
  }, { timeout: 20_000 }).toBeGreaterThanOrEqual(30);
  await expect(page.locator(".tree-row")).toHaveCount(1001);
});
