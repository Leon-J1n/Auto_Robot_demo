import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeConfig } from "../../contracts/types";
import type { FetchLike } from "./library";
import {
  discoverReferences,
  LibraryClient,
  LibraryError,
  normalizeModelPath,
  parseModelMetadata,
  resolveMetadataTarget,
} from "./library";

const OLS = "http://127.0.0.1:8000";
const COS = "https://cos.example.com/models";

function config(overrides: Partial<RuntimeConfig> = {}): RuntimeConfig {
  return {
    gateway: { host: "127.0.0.1", port: 8080 },
    library_server_url: OLS,
    cos_base_url: COS,
    max_payload_bytes: 104857600,
    browser_request_timeout_seconds: 10,
    capture_timeout_seconds: 15,
    max_pending_requests: 8,
    camera_preview: { max_width: 640, max_height: 480, max_hz: 30 },
    features: { "viewport.loadScene": true },
    ...overrides,
  };
}

function respond(status: number, body = ""): Response {
  return new Response(body, { status });
}

function json(status: number, value: unknown): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

function client(value: RuntimeConfig, routes: Record<string, Response | (() => Response)>): { library: LibraryClient; called: string[] } {
  const called: string[] = [];
  const fetchImpl: FetchLike = async (url) => {
    called.push(url);
    const entry = routes[url];
    if (entry === undefined) throw new Error(`unexpected request ${url}`);
    return (typeof entry === "function" ? entry() : entry).clone();
  };
  return { library: new LibraryClient(value, fetchImpl), called };
}

function expectLibraryError(action: () => Promise<unknown>, code: string, status: number): Promise<void> {
  return expect(action()).rejects.toMatchObject({ code, status });
}

function expectLibraryFailure(action: () => unknown, code: string, status: number): void {
  try {
    action();
    expect.fail(`should have raised ${code}`);
  } catch (error) {
    expect(error).toBeInstanceOf(LibraryError);
    expect(error).toMatchObject({ code, status });
  }
}

const MODEL_DOCUMENT = {
  path: "apple.obj",
  is_on_cos: false,
  reference: ["apple.mtl", "texture.png"],
  semantic_properties: { object_name: "apple", object_id: 1 },
  geometric_properties: { AABB: [0.037544876, -0.037110556, 0.037398798, -0.037333214, 0.033874024, -0.037373223] },
  grasp_properties: { poses: [[0, 0, 0, 3.14, 0, 0, "ry", 1.04]] },
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

describe("resolveMetadataTarget", () => {
  it("reduces every accepted form to the OLS model id", () => {
    expect(resolveMetadataTarget("$LIBRARY_SERVER/0")).toEqual({ id: 0, kind: "model" });
    expect(resolveMetadataTarget(" $LIBRARY_SERVER/12 ")).toEqual({ id: 12, kind: "model" });
    expect(resolveMetadataTarget("http://127.0.0.1:8000/7")).toEqual({ id: 7, kind: "model" });
    expect(resolveMetadataTarget("http://127.0.0.1:8000/model?id=9")).toEqual({ id: 9, kind: "model" });
    expect(resolveMetadataTarget(`${OLS}/file?id=3&path=apple.obj`)).toEqual({ id: 3, kind: "model" });
  });

  it("keeps a 机械臂 in its own id space (§6.11)", () => {
    expect(resolveMetadataTarget("$LIBRARY_SERVER/robot?id=0")).toEqual({ id: 0, kind: "robot" });
    expect(resolveMetadataTarget("$LIBRARY_SERVER/model?id=0")).toEqual({ id: 0, kind: "model" });
    // The substituted form of the same route is accepted like `/model?id=` is (§6.1).
    expect(resolveMetadataTarget(`${OLS}/robot?id=2`)).toEqual({ id: 2, kind: "robot" });
  });

  it("refuses an empty or unreplaced placeholder instead of GETting it", () => {
    for (const value of ["", "  ", "$LIBRARY_SERVER/", "$LIBRARY_SERVER/<id>", "$LIBRARY_SERVER/-1",
                         "$LIBRARY_SERVER/robot?id=", "$LIBRARY_SERVER/scene?id=1", "apple.obj", `${OLS}/apple.obj`]) {
      expect(() => resolveMetadataTarget(value)).toThrow(LibraryError);
    }
    expect(() => resolveMetadataTarget(null)).toThrow(LibraryError);
  });

  it("takes the id space from MetadataRef.type, which is what a bare number needs (§4.2)", () => {
    // The real scene's camera is robot 3; without the type this would load model 3.
    expect(resolveMetadataTarget("$LIBRARY_SERVER/3", "robot")).toEqual({ id: 3, kind: "robot" });
    expect(resolveMetadataTarget("$LIBRARY_SERVER/29", "model")).toEqual({ id: 29, kind: "model" });
    // A type the file does not supply leaves the documented default in place (§6.1).
    for (const missing of [undefined, null, ""]) {
      expect(resolveMetadataTarget("$LIBRARY_SERVER/7", missing)).toEqual({ id: 7, kind: "model" });
    }
  });

  it("lets type decide even when the path has already been substituted", () => {
    expect(resolveMetadataTarget(`${OLS}/model?id=4`, "robot")).toEqual({ id: 4, kind: "robot" });
    expect(resolveMetadataTarget("$LIBRARY_SERVER/robot?id=4", "model")).toEqual({ id: 4, kind: "model" });
  });

  it("refuses a type that names neither id space", () => {
    for (const bad of ["urdf", "Model", "scene", 3, {}, []]) {
      expect(() => resolveMetadataTarget("$LIBRARY_SERVER/3", bad)).toThrow(/neither "model" nor "robot"/);
    }
  });
});

describe("normalizeModelPath", () => {
  it("rewrites package:// into a model-relative path", () => {
    expect(normalizeModelPath("package://ur5e/meshes/wrist.dae")).toBe("meshes/wrist.dae");
    expect(() => normalizeModelPath("package://ur5e/")).toThrow(LibraryError);
  });

  it("resolves sibling and nested references inside the model directory", () => {
    expect(normalizeModelPath("crate.mtl", "crate.obj")).toBe("crate.mtl");
    expect(normalizeModelPath("wood.tga", "meshes/case.obj")).toBe("meshes/wood.tga");
    expect(normalizeModelPath("../textures/wood.tga", "meshes/sub/case.obj")).toBe("meshes/textures/wood.tga");
    expect(normalizeModelPath("meshes\\sub\\..\\a.obj")).toBe("meshes/a.obj");
  });

  it("refuses paths that escape the model directory or are not relative", () => {
    for (const value of ["../evil.obj", "a/../../evil.obj", "/etc/passwd", "C:/models/a.obj", `${OLS}/file?id=1`, "a\0b.obj", " "]) {
      expect(() => normalizeModelPath(value)).toThrow(LibraryError);
    }
  });
});

describe("discoverReferences", () => {
  it("follows OBJ to its material library and the MTL to textures", () => {
    const obj = "# c\nmtllib case.mtl\nusemtl m\n";
    expect(discoverReferences("case.obj", obj)).toEqual(["case.mtl"]);
    const mtl = "newmtl m\nmap_Kd textures/wood.tga\nbump map.jpg\nmap_bump ./normal.png\n";
    expect(discoverReferences("case.mtl", mtl)).toEqual(["textures/wood.tga", "map.jpg", "normal.png"]);
  });

  it("reads Collada image links and glTF uris, ignoring embedded data", () => {
    const dae = '<image id="t"><init_from>file:img/apple.png</init_from></image> <texture location="fx.png"/> ';
    expect(discoverReferences("model.dae", dae).sort()).toEqual(["fx.png", "img/apple.png"]);
    expect(discoverReferences("model.gltf", '{"buffers":[{"uri":"data:base64,AA"}],"images":[{"uri":"img/a.png"}]}'))
      .toEqual(["img/a.png"]);
  });

  it("resolves package:// links found inside a Collada document", () => {
    expect(discoverReferences("robot.dae", '<image><init_from>package://robot/meshes/arm.obj</init_from></image>'))
      .toEqual(["meshes/arm.obj"]);
  });

  it("drops references that would leave the model directory or point at the entry itself", () => {
    expect(discoverReferences("sub/case.obj", "mtllib ../../outside.mtl\nmtllib case.obj\n")).toEqual([]);
    // Staying inside the model directory is allowed.
    expect(discoverReferences("sub/case.obj", "mtllib ../outside.mtl\n")).toEqual(["outside.mtl"]);
  });
});

describe("parseModelMetadata", () => {
  it("keeps the metadata verbatim alongside the normalised fields", () => {
    const model = parseModelMetadata(1, MODEL_DOCUMENT);
    expect(model).toMatchObject({
      modelId: 1,
      path: "apple.obj",
      isOnCos: false,
      reference: ["apple.mtl", "texture.png"],
      aabb: [0.037544876, -0.037110556, 0.037398798, -0.037333214, 0.033874024, -0.037373223],
    });
    expect(model.raw).toEqual(MODEL_DOCUMENT);
    expect(model.graspPoses).toHaveLength(1);
    // Metadata read through /model is in the model id space.
    expect(model.kind).toBe("model");
    expect(parseModelMetadata(1, MODEL_DOCUMENT, "robot").kind).toBe("robot");
  });

  it("reads a malformed AABB as absent, because placement may not assume one (§4.6)", () => {
    expect(parseModelMetadata(1, { ...MODEL_DOCUMENT, geometric_properties: { AABB: [0.1, -0.1] } }).aabb).toBeUndefined();
    expect(parseModelMetadata(1, { ...MODEL_DOCUMENT, geometric_properties: { AABB: ["0.1", 0, 0, 0, 0, 0] } }).aabb).toBeUndefined();
    expect(parseModelMetadata(1, { ...MODEL_DOCUMENT, geometric_properties: {} }).aabb).toBeUndefined();
  });

  it("reports 422 for a document without a usable path", () => {
    // The code is what the gateway reads (§5.13): asserting only "it threw" would keep
    // passing if the code drifted, and the caller would silently get 502 instead of 422.
    expectLibraryFailure(() => parseModelMetadata(2, { reference: [] }), "MODEL_METADATA_INVALID", 422);
    expectLibraryFailure(() => parseModelMetadata(2, null), "MODEL_METADATA_INVALID", 422);
    expectLibraryFailure(() => parseModelMetadata(2, [1, 2]), "MODEL_METADATA_INVALID", 422);
    expectLibraryFailure(() => parseModelMetadata(2, { path: " " }), "MODEL_METADATA_INVALID", 422);
  });
});

describe("LibraryClient URLs", () => {
  it("splits is_on_cos between direct cloud storage and the OLS file route", () => {
    const { library } = client(config(), {});
    expect(library.fileUrl(5, "meshes/a.obj", true)).toBe(`${COS}/meshes/a.obj`);
    expect(library.fileUrl(5, "meshes/a.obj", false)).toBe(`${OLS}/file?type=model&id=5&path=meshes%2Fa.obj`);
    expect(library.fileUrl(5, "a b.obj", false)).toBe(`${OLS}/file?type=model&id=5&path=a%20b.obj`);
    // /file carries the id space as type, so a robot mesh never asks the model directory.
    expect(library.fileUrl(2, "meshes/link1.stl", false, "robot")).toBe(`${OLS}/file?type=robot&id=2&path=meshes%2Flink1.stl`);
  });

  it("points /thumbnail at the requested type and id", () => {
    const { library } = client(config(), {});
    expect(library.thumbnailUrl("model", 1)).toBe(`${OLS}/thumbnail?type=model&id=1`);
    expect(library.thumbnailUrl("robot", 2)).toBe(`${OLS}/thumbnail?type=robot&id=2`);
    expect(library.thumbnailUrl("scene", 0)).toBe(`${OLS}/thumbnail?type=scene&id=0`);
  });

  it("refuses an is_on_cos model while the storage base url is unset", () => {
    const { library } = client(config({ cos_base_url: "" }), {});
    expect(() => library.fileUrl(5, "a.obj", true)).toThrow(LibraryError);
  });

  it("reads OLS straight from the configured base url (the gateway proxy is retired, §6.9)", () => {
    const { library } = client(config(), {});
    expect(library.olsUrl("list?type=model")).toBe(`${OLS}/list?type=model`);
    expect(library.olsUrl("/file?type=model&id=1&path=a.obj")).toBe(`${OLS}/file?type=model&id=1&path=a.obj`);
    expect(library.olsUrl("listCategory?type=robot")).toBe(`${OLS}/listCategory?type=robot`);
    expect(library.olsUrl("robot?id=1")).toBe(`${OLS}/robot?id=1`);
    expect(library.olsUrl("thumbnail?type=scene&id=0")).toBe(`${OLS}/thumbnail?type=scene&id=0`);
  });

  it("normalises the configured library base url without a trailing slash", () => {
    const { library } = client(config({ library_server_url: `${OLS}//` }), {});
    expect(library.olsUrl("model?id=4")).toBe(`${OLS}/model?id=4`);
  });
});

describe("LibraryClient requests", () => {
  it("fetches metadata and lists from the OLS routes", async () => {
    const routes = {
      [`${OLS}/model?id=1`]: json(200, MODEL_DOCUMENT),
      [`${OLS}/list?type=scene`]: json(200, { items: [{ id: 4, name: "kitchen" }, { id: 5 }] }),
      [`${OLS}/list?type=model`]: json(200, { items: [] }),
      [`${OLS}/scene?id=4`]: json(200, { scene_id: 4, scene_name: "kitchen" }),
    };
    const { library, called } = client(config(), routes);
    expect((await library.model(1)).path).toBe("apple.obj");
    expect(await library.list("scene")).toEqual([{ id: 4, name: "kitchen" }, { id: 5, name: "" }]);
    expect(await library.list("model")).toEqual([]);
    expect(await library.scene(4)).toMatchObject({ scene_id: 4, scene_name: "kitchen" });
    expect(called.filter((url) => url.endsWith("/model?id=1"))).toHaveLength(1);
  });

  it("keeps the category of every listed item and drops a null one", async () => {
    const routes = {
      [`${OLS}/list?type=robot`]: json(200, {
        items: [{ id: 0, name: "ur5e", category: "机械臂.六轴" }, { id: 1, name: "panda", category: null }],
      }),
    };
    const { library } = client(config(), routes);
    expect(await library.list("robot")).toEqual([{ id: 0, name: "ur5e", category: "机械臂.六轴" }, { id: 1, name: "panda" }]);
  });

  it("reads the category list of a type from /listCategory", async () => {
    const routes = { [`${OLS}/listCategory?type=model`]: json(200, { type: "model", category: ["模型.抓取物", "模型.容器", 42] }) };
    const { library, called } = client(config(), routes);
    expect(await library.listCategories("model")).toEqual(["模型.抓取物", "模型.容器"]);
    expect(called).toEqual([`${OLS}/listCategory?type=model`]);
  });

  it("fails a category list that carries no categories", async () => {
    const { library } = client(config(), { [`${OLS}/listCategory?type=scene`]: json(200, {}) });
    await expectLibraryError(() => library.listCategories("scene"), "UPSTREAM_ERROR", 502);
  });

  it("loads a robot from its own route and remembers the id space", async () => {
    const routes = {
      [`${OLS}/robot?id=0`]: json(200, { path: "ur5e.urdf", reference: ["meshes/base_link.stl"] }),
      [`${OLS}/file?type=robot&id=0&path=ur5e.urdf`]: respond(200, "<robot name=\"ur5e\"/>"),
      [`${OLS}/file?type=robot&id=0&path=meshes%2Fbase_link.stl`]: respond(200, "solid cube"),
    };
    const { library, called } = client(config(), routes);
    const robot = await library.robot(0);
    expect(robot).toMatchObject({ modelId: 0, kind: "robot", path: "ur5e.urdf" });
    const bundle = await library.bundle(robot);
    expect(bundle.entry).toBe("ur5e.urdf");
    expect(called).toEqual([
      `${OLS}/robot?id=0`,
      `${OLS}/file?type=robot&id=0&path=ur5e.urdf`,
      `${OLS}/file?type=robot&id=0&path=meshes%2Fbase_link.stl`,
    ]);
  });

  it("fails a listing that carries no items", async () => {
    const { library } = client(config(), { [`${OLS}/list?type=model`]: json(200, {}) });
    await expectLibraryError(() => library.list("model"), "UPSTREAM_ERROR", 502);
  });

  it("maps 404, 403, 5xx and a dead server onto distinct error codes", async () => {
    const reachable = client(config(), { [`${OLS}/model?id=404`]: respond(404), [`${OLS}/model?id=403`]: respond(403), [`${OLS}/model?id=500`]: respond(500) });
    await expectLibraryError(() => reachable.library.model(404), "MODEL_NOT_FOUND", 404);
    await expectLibraryError(() => reachable.library.model(403), "INVALID_PARAM", 403);
    await expectLibraryError(() => reachable.library.model(500), "UPSTREAM_ERROR", 502);
    const { library } = client(config(), {});
    await expectLibraryError(() => library.model(1), "UPSTREAM_ERROR", 502);
  });

  it("serves a repeated file from cache instead of downloading it twice", async () => {
    const url = `${OLS}/file?type=model&id=1&path=apple.obj`;
    const { library, called } = client(config(), { [url]: respond(200, "v 0 0 0\n") });
    expect(await library.fileText(1, "apple.obj", false)).toBe("v 0 0 0\n");
    expect(await library.fileText(1, "apple.obj", false)).toBe("v 0 0 0\n");
    expect(called.filter((value) => value === url)).toHaveLength(1);
  });

  it("forgets a failed read so a restarted library server can be reached again (§4.6)", async () => {
    const url = `${OLS}/list?type=model`;
    const { library, called } = client(config(), {
      [url]: () => (called.filter((value) => value === url).length === 1
        ? respond(503)
        : json(200, { items: [{ id: 0, name: "apple", category: "模型.抓取物" }] })),
    });
    await expectLibraryError(() => library.list("model"), "UPSTREAM_ERROR", 502);
    expect(await library.list("model")).toEqual([{ id: 0, name: "apple", category: "模型.抓取物" }]);
    expect(called.filter((value) => value === url)).toHaveLength(2);
  });

  it("re-reads a successful route only after the panel asks for it", async () => {
    const url = `${OLS}/list?type=scene`;
    const { library, called } = client(config(), { [url]: () => json(200, { items: [{ id: 1, name: "Sample Scene" }] }) });
    expect(await library.list("scene")).toHaveLength(1);
    expect(await library.list("scene")).toHaveLength(1);
    library.invalidateMetadata();
    expect(await library.list("scene")).toHaveLength(1);
    expect(called.filter((value) => value === url)).toHaveLength(2);
  });
});

describe("LibraryClient.bundle", () => {
  it("reports 424 when a referenced file is missing", async () => {
    const model = parseModelMetadata(3, { path: "crate.obj", reference: ["gone.mtl"] });
    const { library } = client(config(), {
      [`${OLS}/file?type=model&id=3&path=crate.obj`]: respond(200, "mtllib gone.mtl\n"),
      [`${OLS}/file?type=model&id=3&path=gone.mtl`]: respond(404),
    });
    await expectLibraryError(() => library.bundle(model), "DEPENDENCY_FETCH_FAILED", 424);
  });

  it("reports 424 when the entry file itself cannot be fetched", async () => {
    const model = parseModelMetadata(6, { path: "missing.obj", reference: [] });
    const { library } = client(config(), { [`${OLS}/file?type=model&id=6&path=missing.obj`]: respond(500) });
    await expectLibraryError(() => library.bundle(model), "DEPENDENCY_FETCH_FAILED", 424);
  });

  it("degrades to untextured geometry when the library refuses a texture", async () => {
    const model = parseModelMetadata(7, { path: "case.obj", reference: ["case.mtl", "wood.tga", "shade.png"] });
    const { library } = client(config(), {
      [`${OLS}/file?type=model&id=7&path=case.obj`]: respond(200, "mtllib case.mtl\n"),
      [`${OLS}/file?type=model&id=7&path=case.mtl`]: respond(200, "map_Kd wood.tga\n"),
      [`${OLS}/file?type=model&id=7&path=wood.tga`]: respond(403),
      [`${OLS}/file?type=model&id=7&path=shade.png`]: respond(404),
    });
    const bundle = await library.bundle(model);
    expect(bundle.entry).toBe("case.obj");
    expect([...bundle.files.keys()]).toEqual(["case.obj", "case.mtl"]);
    expect(bundle.failures).toEqual(["wood.tga", "shade.png"]);
    expect(console.warn).toHaveBeenCalledTimes(2);
  });

  it("decodes bytes for the loaders and honours the storage branch", async () => {
    const model = parseModelMetadata(8, { path: "model.glb", is_on_cos: true, reference: [] });
    const { library, called } = client(config(), { [`${COS}/model.glb`]: respond(200, "glTF") });
    const bundle = await library.bundle(model);
    expect(new TextDecoder().decode(bundle.files.get("model.glb"))).toBe("glTF");
    expect(called).toEqual([`${COS}/model.glb`]);
  });
});
