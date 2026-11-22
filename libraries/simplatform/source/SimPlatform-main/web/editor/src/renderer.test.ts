import { describe, expect, it, vi } from "vitest";
import * as THREE from "three";
import type { EntityView, SceneSnapshot, TransformView } from "../../contracts/types";
import { ThreeRendererAdapter } from "./renderer";
import { reconstructTimelineFrame } from "./timeline";

const pose = (x: number): TransformView => ({ translation: [x, 0, 0], rotationWxyz: [1, 0, 0, 0], scale: [1, 1, 1] });
const entity: EntityView = { id: 1, name: "arm", kind: "mesh", parent: null,
  flags: [], robotManaged: true, local: pose(0), world: pose(0),
  visual: { shape: "mesh", resource: "arm.stl", visible: true, minLod: 0, maxLod: 0, size: [1, 1, 1], color: [1, 1, 1, 1] } };
const base = { sceneId: "test", entities: [entity] } as SceneSnapshot;

// Exercise the real Three.js scene graph without a GPU, DOM, or browser automation.
function fixture(snapshot = base) {
  const nodes = new Map(snapshot.entities.map((item) => [item.id, new THREE.Group()]));
  const renderer: ThreeRendererAdapter = Object.assign(Object.create(ThreeRendererAdapter.prototype), {
    sceneId: snapshot.sceneId, entities: new Map(snapshot.entities.map((item) => [item.id, item])),
    nodes, signatures: new Map(snapshot.entities.map((item) => [item.id, "loaded"])),
    simWorld: new THREE.Group(), instancing: false,
  });
  const fullSync = vi.spyOn(renderer, "syncScene").mockImplementation(() => undefined);
  const selection = vi.spyOn(renderer, "select").mockImplementation(() => undefined);
  return { renderer, nodes, fullSync, selection };
}

describe("timeline renderer", () => {
  it("updates world transforms without rebuilding static meshes or selection", () => {
    const { renderer, nodes, fullSync, selection } = fixture();
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshBasicMaterial());
    nodes.get(1)!.add(mesh);
    const stringify = vi.spyOn(JSON, "stringify");
    for (let frame = 0; frame < 100; frame++) {
      renderer.syncTimelineFrame(reconstructTimelineFrame(base, {
        simulationTime: frame / 50, entities: [{ id: 1, world: pose(frame) }],
      }));
    }
    expect(nodes.get(1)!.position.x).toBe(99);
    expect(nodes.get(1)!.children).toEqual([mesh]);
    expect(fullSync).not.toHaveBeenCalled();
    expect(selection).not.toHaveBeenCalled();
    expect(stringify).not.toHaveBeenCalled();
    stringify.mockRestore();
    renderer.syncTimelineFrame(base);
    expect(nodes.get(1)!.position.x).toBe(0);
    mesh.geometry.dispose(); mesh.material.dispose();
  });

  it("preserves local transforms for children and supports nonsequential seeks", () => {
    const child = { ...entity, id: 2, parent: 1, local: pose(2), world: pose(12) };
    const snapshot = { ...base, entities: [entity, child] };
    const { renderer, nodes } = fixture(snapshot);
    renderer.syncTimelineFrame(snapshot);
    expect(nodes.get(2)!.parent).toBe(nodes.get(1));
    expect(nodes.get(2)!.position.x).toBe(2);
  });

  it.each([
    { ...base, entities: [{ ...entity, visual: { ...entity.visual!, resource: "replacement.stl" } }] },
    { ...base, entities: [] },
    { ...base, entities: [{ ...entity, parent: 3 }] },
    { ...base, sceneId: "other" },
    { ...base, entities: [{ ...entity, rigidBody: { enabled: true, dynamic: true, gravity: true, mass: 0.2 } }] },
  ])("uses full sync when static structure changes", (snapshot) => {
    const { renderer, fullSync } = fixture();
    renderer.syncTimelineFrame(snapshot);
    expect(fullSync).toHaveBeenCalledWith(snapshot);
  });

  it("refreshes render content when overlays have been toggled", () => {
    const { renderer, fullSync } = fixture();
    renderer.setOverlays(true, false);
    renderer.syncTimelineFrame(base);
    expect(fullSync).toHaveBeenCalledWith(base);
  });
});
