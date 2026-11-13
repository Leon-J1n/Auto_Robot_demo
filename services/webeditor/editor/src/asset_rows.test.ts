import { readFileSync } from "node:fs";
import { expect, it } from "vitest";

it("shows only the asset name and a thumbnail that fills the card (Q154, sizes revised by Q158)", () => {
  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  const row = source.slice(source.indexOf("function assetRow("), source.indexOf("function assetKinds("));
  expect(row).not.toContain("<small>");
  expect(row).not.toContain("item.category");
  expect(row).toContain("data-asset-id=");
  expect(row).toContain("escapeHtml(item.name)");
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  // Q158: the picture is as wide as the card and square, so the fixed 52rem box is gone.
  expect(css).toContain(".thumb-frame { position:relative;width:100%;aspect-ratio:1/1;");
  expect(css).not.toContain("width:52rem;height:52rem");
  expect(css).toContain(".asset-card strong { font-size:13rem; }");
});

/**
 * §4.6 (Q158): two columns of thumbnail-over-name cards. A grid declaration is invisible to `tsc`
 * and to the build, and the width it needs comes from a class only renderContext() writes - so both
 * the layout and the wiring are pinned here, and the live column count is read in browser_smoke.
 */
it("lays the list out in two columns and borrows the width to do it (Q158)", () => {
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  expect(css).toContain(".asset-rows { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));");
  expect(css).toContain(".asset-rows>.empty-line { grid-column:1/-1; }");
  expect(css).toContain(".asset-card { min-width:0;display:flex;flex-direction:column;");
  expect(css).not.toContain("min-height:64rem");
  // The widening is scoped to the wide layouts: the three narrow media queries keep their own widths.
  expect(css).toContain("@media(min-width:1051px){ .workspace.assets-open { --context-track:minmax(300rem,360rem); } }");

  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  const context = source.slice(source.indexOf("function renderContext("), source.indexOf("function updateStatus("));
  expect(context).toContain('workspace.classList.toggle("assets-open", panelVisible && state.panel === "assets");');
});

/**
 * §4.7/§7.2 (Q156): the 大类 moved from a rail inside the panel to the navigation rail. Both halves
 * are pinned here because neither `tsc` nor a build sees a selector string: a leftover `.asset-kind`
 * rule or a leftover click branch would be silently dead, and a rail button without its own feature
 * key would be a capability with no remote exit (坑 106).
 */
it("carries the three 大类 on the navigation rail and leaves one category rail in the panel (Q156)", () => {
  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  const rail = source.slice(source.indexOf('<nav class="rail"'), source.indexOf("</nav>"));
  expect(rail).toContain('data-panel="assets" data-asset-kind-nav="${kind}"');
  expect(rail).toContain('data-feature="navigation.assets.${GROUP_FEATURES[kind]}"');
  expect(rail).toContain('data-requires-feature="panels.assets"');
  expect(rail).toContain("${LIBRARY_KINDS.map((kind) =>");
  expect(rail).not.toContain("<span>Assets</span>");

  const panel = source.slice(source.indexOf("function renderAssets("), source.indexOf("async function refreshLibrary("));
  expect(panel).toContain('<nav class="asset-categories">');
  expect(panel).not.toContain("asset-kinds");
  expect(panel).not.toContain("data-asset-kind-nav");

  // The rail press goes through the one toggle judgement, and the old standalone branch is gone.
  expect(source).toContain("panelTabPress({ panel: state.panel, open: state.panelOpen, kind: state.library.kind }, panel, target.dataset.assetKindNav)");
  expect(source).not.toContain("if (target.dataset.assetKindNav)");
  expect(source).toContain("panelTabActive({ panel: item.dataset.panel!, kind: item.dataset.assetKindNav }");

  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  expect(css).not.toMatch(/\.asset-kinds?\b/);
  expect(css).toContain(".asset-categories {");
  expect(css).toContain(".asset-category.active {");
});

/**
 * §4.7 (Q159): the rail's text is OLS's first level. The template can only carry the declared
 * fallback - the live word arrives with /listCategory - so the refresh has to live in the render
 * path that runs again when the library answers, not in the one-shot template.
 */
it("refreshes the rail's word from the categories OLS published (Q159)", () => {
  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  const context = source.slice(source.indexOf("function renderContext("), source.indexOf("function updateStatus("));
  expect(context).toContain('const kind = item.dataset.assetKindNav as LibraryKind | undefined;');
  expect(context).toContain('if (kind) item.querySelector("span")!.textContent = kindLabel(kind, state.library.sections[kind].categories);');

  // refreshLibrary() re-renders after the lists land, which is what carries the word to the rail.
  const refresh = source.slice(source.indexOf("async function refreshLibrary("), source.indexOf("function showPhaseBlock("));
  expect(refresh).toContain("state.library.sections[kind] = { items, categories };");
  expect(refresh).toMatch(/state\.library\.loading = false;\s*renderContext\(\);/);

  const labels = readFileSync(new URL("./assets.ts", import.meta.url), "utf8");
  expect(labels).toContain('{ scene: "场景", model: "模型", robot: "机器人" }');
});
