/**
 * ObjectLibraryServer / COS client (spec v2.1 §6).
 * Only simple GETs are used, never custom headers, so no CORS preflight is
 * triggered (§6.9); paths are always resolved inside a model directory.
 */
import type { RuntimeConfig } from "../../contracts/types";

export const LIBRARY_SERVER_REF = /^\$LIBRARY_SERVER\/(\d+)$/;
/** A 机械臂 has its own id space, so the placeholder has to name the route (Q51). */
const LIBRARY_SERVER_QUERY = /^\$LIBRARY_SERVER\/(model|robot)\?id=(\d+)$/;
const ABSOLUTE_ID_REF = /^https?:\/\/[^/]+\/(?:model\?id=)?(\d+)$/;
const QUERY_ID_REF = /(?:^|[?&])id=(\d+)(?:$|[&])/;
const ROUTE_KIND_REF = /\/(model|robot)\/?$/;

export class LibraryError extends Error {
  constructor(readonly code: string, message: string, readonly status: number = 400, readonly failures: string[] = []) {
    super(message);
    this.name = "LibraryError";
  }
}

/** Where one `MetadataRef.path` points: an id plus the id space it belongs to. */
export interface MetadataTarget {
  id: number;
  kind: LibraryFileKind;
}

/**
 * Which OLS id space a reference names. `MetadataRef.type` decides it when present,
 * because `model` and `robot` are separate id spaces (§6.11) and a bare number in
 * `$LIBRARY_SERVER/<id>` says nothing on its own; the documented default for a bare
 * number stays `model` (§6.1). The already-substituted URL forms carry their own
 * route, so they need no `type`.
 *
 * The path is never treated as a GET-able URL, because OLS has no `/{id}` route.
 */
export function resolveMetadataTarget(path: unknown, type?: unknown): MetadataTarget {
  const kindHint = metadataKindHint(type);
  if (typeof path !== "string" || !path.trim()) throw new LibraryError("INVALID_PARAM", "MetadataRef.path is empty");
  const value = path.trim();
  const direct = LIBRARY_SERVER_REF.exec(value);
  if (direct) return { id: Number(direct[1]), kind: kindHint ?? "model" };
  const placeholder = LIBRARY_SERVER_QUERY.exec(value);
  if (placeholder) return { id: Number(placeholder[2]), kind: kindHint ?? (placeholder[1] as LibraryFileKind) };
  const absolute = ABSOLUTE_ID_REF.exec(value);
  if (absolute) return { id: Number(absolute[1]), kind: kindHint ?? "model" };
  if (value.startsWith("http://") || value.startsWith("https://")) {
    const url = new URL(value);
    const query = QUERY_ID_REF.exec(url.search);
    if (query) {
      const routeKind = ROUTE_KIND_REF.exec(url.pathname)?.[1];
      return { id: Number(query[1]), kind: kindHint ?? (routeKind === "robot" ? "robot" : "model") };
    }
  }
  throw new LibraryError("INVALID_PARAM", `MetadataRef.path "${value}" is not a resolved $LIBRARY_SERVER/<id> reference`);
}

function metadataKindHint(type: unknown): LibraryFileKind | null {
  if (type === undefined || type === null || type === "") return null;
  if (type !== "model" && type !== "robot") {
    throw new LibraryError("INVALID_PARAM", `MetadataRef.type ${JSON.stringify(type)} is neither "model" nor "robot"`);
  }
  return type;
}

/**
 * Normalise a path relative to the model directory: `package://<pkg>/` is
 * rewritten away (same rule OLS applies), and absolute, NUL or escaping paths
 * are refused instead of being fetched (§6.3, §6.4).
 */
export function normalizeModelPath(value: string, from?: string): string {
  const raw = value.replace(/\\/g, "/").trim();
  if (!raw) throw new LibraryError("INVALID_PARAM", "empty file path");
  if (raw.includes("\0")) throw new LibraryError("INVALID_PARAM", "path contains a NUL byte");
  if (/^[a-zA-Z]:\//.test(raw) || raw.startsWith("/")) throw new LibraryError("INVALID_PARAM", `absolute path is not allowed: ${value}`);
  // `package://<pkg>/` is rewritten to the model directory, every other scheme stays a URL (§6.4).
  const withoutPackage = raw.startsWith("package://") ? raw.replace(/^package:\/\/[^/]*\/?/, "") : raw;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(withoutPackage)) throw new LibraryError("INVALID_PARAM", `URL is not a model-relative path: ${value}`);
  const parts = (from ? [...from.split("/").slice(0, -1), ...withoutPackage.split("/")] : withoutPackage.split("/"));
  const resolved: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!resolved.length) throw new LibraryError("INVALID_PARAM", `path escapes the model directory: ${value}`);
      resolved.pop();
      continue;
    }
    resolved.push(part);
  }
  if (!resolved.length) throw new LibraryError("INVALID_PARAM", `path resolves to nothing: ${value}`);
  return resolved.join("/");
}

export interface ModelMetadata {
  modelId: number;
  /** which OLS id space the metadata came from; /file needs it as `type` (§6.3) */
  kind: LibraryFileKind;
  /** geometry file to load, model-relative */
  path: string;
  isOnCos: boolean;  /** additional files of the model, model-relative */
  reference: string[];
  aabb?: Aabb6;
  graspPoses?: unknown;
  semantic?: Record<string, unknown>;
  /** full document as returned by OLS (§3.8 spirit: keep it) */
  raw: Record<string, unknown>;
}

/**
 * `geometric_properties.AABB` as OLS publishes it: six floats in the order
 * `x+, x-, y+, y-, z+, z-`, each one the signed distance of that face from the
 * model's own origin. Both members of a pair may be positive (`coffee_table`
 * measures 0.09149412 to 0.69149415 along X), so the two are read as an
 * unordered pair, never as a min and a max.
 */
export type Aabb6 = [number, number, number, number, number, number];

/** The three id spaces OLS publishes; scene/model/robot ids are not interchangeable. */
export type LibraryKind = "scene" | "model" | "robot";
/** Only these two have files, so `/file?type=` refuses `scene` (`docs/OLS接口.md`). */
export type LibraryFileKind = Extract<LibraryKind, "model" | "robot">;

export interface LibraryItem {
  id: number;
  name: string;
  /** index `category` entry, e.g. `模型.抓取物` or `机械臂.六轴`; absent when OLS has none */
  category?: string;
}

const COLLADA_IMAGE = /<(?:image|texture|effect|entry)\b[^>]*\b(?:href|url|location)\s*=\s*["']([^"']+)["']/gi;
const COLLADA_INIT_FROM = /<init_from>\s*([^<]+?)\s*<\/init_from>/gi;
const GLTF_URI = /"(?:uri|basename)"\s*:\s*"([^"]+)"/g;
const TEXTURE_SUFFIX = /\.(png|jpe?g|tga|dds|gif|bmp|exr|ktx2?)$/i;

/**
 * Best-effort discovery of files a loaded geometry references by itself
 * (OBJ -> MTL -> textures, DAE -> init_from images, glTF -> buffers/images),
 * each resolved inside the model directory (§6.4, §6.5).
 */
export function discoverReferences(entry: string, text: string): string[] {
  const found = new Set<string>();
  const add = (value: string) => {
    if (value.startsWith("data:")) return;
    try {
      const normalized = normalizeModelPath(value, entry);
      if (normalized !== entry) found.add(normalized);
    } catch {
      // An unresolvable reference is reported by the loader that needs it.
    }
  };
  const lower = entry.toLowerCase();
  if (lower.endsWith(".obj") || lower.endsWith(".mtl")) {
    for (const line of text.split(/\r?\n/)) {
      if (!/^\s*(mtllib|map_k[a-ds]|map_bump|bump|disp|decal)\s/i.test(line)) continue;
      const value = line.replace(/^\s*\S+\s+/, "").trim().replace(/^"|"$/g, "");
      if (value) add(value);
    }
  } else if (lower.endsWith(".dae")) {
    // Collada stores images both as `<init_from>file:/…</init_from>` and as
    // document-level href/url references; the `file:` scheme is a local marker.
    for (const match of text.matchAll(COLLADA_INIT_FROM)) add(match[1]!.replace(/^file:/, ""));
    for (const match of text.matchAll(COLLADA_IMAGE)) add(match[1]!.replace(/^file:/, ""));
  } else if (lower.endsWith(".gltf")) {
    for (const match of text.matchAll(GLTF_URI)) add(match[1]!);
  }
  return [...found];
}

/** A malformed `geometric_properties.AABB` reads as absent; nothing downstream may assume it. */
function readAabb(value: unknown): Aabb6 | undefined {
  if (!Array.isArray(value) || value.length !== 6) return undefined;
  const numbers = value.filter((item): item is number => typeof item === "number" && Number.isFinite(item));
  return numbers.length === 6 ? numbers as Aabb6 : undefined;
}

export function parseModelMetadata(modelId: number, document: unknown, kind: LibraryFileKind = "model"): ModelMetadata {
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new LibraryError("MODEL_METADATA_INVALID", `metadata for ${kind} ${modelId} is not an object`, 422);
  }
  const value = document as Record<string, unknown>;
  if (typeof value.path !== "string" || !value.path.trim()) {
    throw new LibraryError("MODEL_METADATA_INVALID", `metadata for ${kind} ${modelId} has no usable path`, 422);
  }
  const reference = Array.isArray(value.reference) ? value.reference.filter((item): item is string => typeof item === "string") : [];
  const geometric = value.geometric_properties as Record<string, unknown> | undefined;
  return {
    modelId,
    kind,
    path: normalizeModelPath(value.path),
    isOnCos: value.is_on_cos === true,
    reference: reference.map((item) => normalizeModelPath(item)),
    aabb: readAabb(geometric?.AABB),
    graspPoses: (value.grasp_properties as Record<string, unknown> | undefined)?.poses,
    semantic: value.semantic_properties as Record<string, unknown> | undefined,
    raw: value,
  };
}

export type FetchLike = (input: string, init?: { signal?: AbortSignal }) => Promise<Response>;

/**
 * Maps a model-relative path to the URL to fetch, honouring `is_on_cos`
 * and the same-session URL cache (§6.3, §6.7).
 * Only a successful read is cached: a rejected one is forgotten, so a restarted
 * OLS can be read again without reloading the page (§4.6).
 */
export class LibraryClient {
  private readonly jsonCache = new Map<string, Promise<unknown>>();
  private readonly binaryCache = new Map<string, Promise<ArrayBuffer>>();
  private readonly textCache = new Map<string, Promise<string>>();

  constructor(private readonly config: RuntimeConfig, private readonly fetchImpl: FetchLike) {}

  /** The browser talks to OLS directly; `library_server_url` is the only base (§6.9). */
  olsUrl(relative: string): string {
    const clean = relative.replace(/^\//, "");
    return `${this.config.library_server_url.replace(/\/+$/, "")}/${clean}`;
  }

  fileUrl(modelId: number, path: string, isOnCos: boolean, kind: LibraryFileKind = "model"): string {
    const normalized = normalizeModelPath(path);
    if (isOnCos) {
      // `path` is documented as already model-relative, so it is only appended (§6.3, §6.10).
      const base = this.config.cos_base_url.replace(/\/+$/, "");
      if (!base) throw new LibraryError("INVALID_PARAM", `cos_base_url is empty but model ${modelId} is flagged is_on_cos`);
      return `${base}/${normalized}`;
    }
    return this.olsUrl(`file?type=${kind}&id=${modelId}&path=${encodeURIComponent(normalized)}`);
  }

  /** `/thumbnail?type=<kind>&id=<n>`: the index holds the path, the caller only needs the URL. */
  thumbnailUrl(kind: LibraryKind, id: number): string {
    return this.olsUrl(`thumbnail?type=${kind}&id=${id}`);
  }

  async list(kind: LibraryKind): Promise<LibraryItem[]> {
    const document = await this.getJson(this.olsUrl(`list?type=${kind}`));
    const items = (document as { items?: unknown }).items;
    if (!Array.isArray(items)) throw new LibraryError("UPSTREAM_ERROR", `OLS /list?type=${kind} returned no items`, 502);
    return items.map((item) => {
      const entry = item as Record<string, unknown>;
      const category = typeof entry.category === "string" && entry.category ? entry.category : undefined;
      return { id: Number(entry.id), name: String(entry.name ?? ""), ...(category ? { category } : {}) };
    });
  }

  /**
   * `GET /listCategory?type=<kind>`: the spelling is the one both docs/OLS接口.md and
   * the ObjectLibraryServer implementation use (`server.py` ROUTES_GET).
   */
  async listCategories(kind: LibraryKind): Promise<string[]> {
    const document = await this.getJson(this.olsUrl(`listCategory?type=${kind}`));
    const categories = (document as { category?: unknown }).category;
    if (!Array.isArray(categories)) throw new LibraryError("UPSTREAM_ERROR", `OLS /listCategory?type=${kind} returned no categories`, 502);
    return categories.filter((item): item is string => typeof item === "string" && !!item);
  }

  async scene(sceneId: number): Promise<unknown> {
    return this.getJson(this.olsUrl(`scene?id=${sceneId}`));
  }

  async model(modelId: number): Promise<ModelMetadata> {
    const document = await this.getJson(this.olsUrl(`model?id=${modelId}`));
    return parseModelMetadata(modelId, document, "model");
  }

  /** A 机械臂 sits in its own id space; only the metadata route differs. */
  async robot(robotId: number): Promise<ModelMetadata> {
    const document = await this.getJson(this.olsUrl(`robot?id=${robotId}`));
    return parseModelMetadata(robotId, document, "robot");
  }

  /**
   * Drop the cached metadata documents so the next read reaches OLS again.
   * The panel's refresh button uses it: an asset list is only as fresh as its last read.
   */
  invalidateMetadata(): void {
    this.jsonCache.clear();
  }

  async fileText(modelId: number, path: string, isOnCos: boolean, kind: LibraryFileKind = "model"): Promise<string> {
    return this.read(this.fileUrl(modelId, path, isOnCos, kind), this.textCache, (response) => response.text());
  }

  async fileBytes(modelId: number, path: string, isOnCos: boolean, kind: LibraryFileKind = "model"): Promise<ArrayBuffer> {
    return this.read(this.fileUrl(modelId, path, isOnCos, kind), this.binaryCache, (response) => response.arrayBuffer());
  }

  /**
   * Fetch the entry file plus every `reference` item. Only a texture that the
   * library refuses (403/404) is dropped and reported (§6.6); anything else -
   * including a missing mesh or MTL - fails the whole model (§6.3).
   */
  async bundle(model: ModelMetadata): Promise<{ entry: string; files: Map<string, ArrayBuffer>; failures: string[] }> {
    const files = new Map<string, ArrayBuffer>();
    const failures: string[] = [];
    for (const path of [model.path, ...model.reference]) {
      try {
        files.set(path, await this.fileBytes(model.modelId, path, model.isOnCos, model.kind));
      } catch (error) {
        const status = error instanceof LibraryError ? error.status : 0;
        if ((status === 403 || status === 404) && TEXTURE_SUFFIX.test(path)) {
          console.warn(`[library] texture ${path} unavailable for model ${model.modelId}, rendering without it`);
          failures.push(path);
          continue;
        }
        throw new LibraryError("DEPENDENCY_FETCH_FAILED",
          `model ${model.modelId} could not fetch ${path}`, 424, [...failures, path]);
      }
    }
    return { entry: model.path, files, failures };
  }

  /** Cache the promise, but forget a failure so a later retry can reach OLS again (§4.6). */
  private cached<T>(cache: Map<string, Promise<T>>, url: string, load: () => Promise<T>): Promise<T> {
    const cached = cache.get(url);
    if (cached) return cached;
    const request = load().catch((error) => {
      cache.delete(url);
      throw error;
    });
    cache.set(url, request);
    return request;
  }

  private getJson(url: string): Promise<unknown> {
    return this.cached(this.jsonCache, url, async () => {
      const response = await this.request(url);
      const body = await response.text();
      try {
        return JSON.parse(body) as unknown;
      } catch {
        throw new LibraryError("UPSTREAM_ERROR", `${url} did not return JSON`, response.status);
      }
    });
  }

  private read<T>(url: string, cache: Map<string, Promise<T>>, load: (response: Response) => Promise<T>): Promise<T> {
    return this.cached(cache, url, async () => load(await this.request(url)));
  }

  private async request(url: string): Promise<Response> {
    let response: Response;
    try {
      response = await this.fetchImpl(url);
    } catch (error) {
      throw new LibraryError("UPSTREAM_ERROR", `${url} is unreachable: ${(error as Error).message}`, 502);
    }
    if (response.ok) return response;
    if (response.status === 404) throw new LibraryError("MODEL_NOT_FOUND", `${url} not found`, 404);
    if (response.status === 403) throw new LibraryError("INVALID_PARAM", `${url} is refused by the library server`, 403);
    if (response.status >= 500) throw new LibraryError("UPSTREAM_ERROR", `${url} returned ${response.status}`, 502);
    throw new LibraryError("INVALID_PARAM", `${url} returned ${response.status}`, response.status);
  }
}

/** Build a client from the runtime config the gateway served (§9.1).
 *  The default is a wrapper rather than `fetch` itself: calling a detached
 *  `window.fetch` throws "Illegal invocation" in Chromium, which made every OLS
 *  read look unreachable. */
export function createLibraryClient(config: RuntimeConfig, fetchImpl: FetchLike = (input, init) => fetch(input, init)): LibraryClient {
  return new LibraryClient(config, fetchImpl);
}
