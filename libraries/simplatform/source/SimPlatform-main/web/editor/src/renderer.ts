import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { ColladaLoader } from "three/addons/loaders/ColladaLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import type { EntityView, SceneSnapshot, Vec3 } from "../../contracts/types";
import { cameraFrustumSegments } from "./logic";

export type Tool = "select" | "move" | "rotate" | "scale";

export interface RendererCallbacks {
  onSelect(entityId: number | null): void;
  onTransform(entityId: number, transform: {
    translation: Vec3;
    rotationWxyz: [number, number, number, number];
    scale: Vec3;
  }): void;
  onPerformance(fps: number, renderMs: number): void;
  onError(message: string): void;
}

export class ThreeRendererAdapter {
  private readonly scene = new THREE.Scene();
  private readonly simWorld = new THREE.Group();
  private readonly camera = new THREE.PerspectiveCamera(52, 1, 0.01, 2000);
  private readonly renderer: THREE.WebGLRenderer;
  private readonly orbit: OrbitControls;
  private readonly transform: TransformControls;
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointer = new THREE.Vector2();
  private readonly nodes = new Map<number, THREE.Object3D>();
  private readonly entities = new Map<number, EntityView>();
  private readonly signatures = new Map<number, string>();
  private readonly assetCache = new Map<string, Promise<THREE.Object3D>>();
  private readonly instanceRecords = new Map<number, { mesh: THREE.InstancedMesh; index: number }>();
  private readonly instanceMeshes: THREE.InstancedMesh[] = [];
  private readonly clock = new THREE.Clock();
  private readonly pressed = new Set<string>();
  private selectionBox: THREE.BoxHelper | null = null;
  private selectedId: number | null = null;
  private colliderVisible = false;
  private ifpsVisible = false;
  private orbitInteractionEnabled = true;
  private selectionInteractionEnabled = true;
  private tool: Tool = "select";
  private animation = 0;
  private frames = 0;
  private frameWindow = performance.now();
  private disposed = false;
  private instancing = false;
  private instanceRevision = -1;
  private sceneId: string | undefined;

  constructor(private readonly canvas: HTMLCanvasElement, private readonly callbacks: RendererCallbacks) {
    this.scene.background = new THREE.Color(0x000000);
    // Sim Core uses a Z-up world; Three.js uses Y-up by default.
    // Keep the conversion on the simulated world only so the display grid,
    // camera and interaction helpers remain in Three.js coordinates.
    this.simWorld.rotation.x = -Math.PI / 2;
    this.scene.add(this.simWorld);
    this.scene.fog = new THREE.Fog(0x000000, 30, 120);
    this.camera.position.set(7, 5, 8);
    this.renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: false, powerPreference: "high-performance",
    });
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.orbit = new OrbitControls(this.camera, canvas);
    this.orbit.enableDamping = true;
    this.orbit.target.set(0, 0.8, 0);
    this.transform = new TransformControls(this.camera, canvas);
    this.transform.setSize(0.8);
    this.scene.add(this.transform.getHelper());
    this.transform.addEventListener("dragging-changed", (event) => {
      this.orbit.enabled = this.orbitInteractionEnabled && !event.value;
    });
    this.transform.addEventListener("mouseUp", () => this.commitTransform());
    const grid = new THREE.GridHelper(80, 80, 0x405064, 0x252e38);
    grid.material.opacity = 0.72;
    grid.material.transparent = true;
    this.scene.add(grid);
    this.scene.add(new THREE.AxesHelper(1.5));
    canvas.addEventListener("pointerdown", this.pick);
    canvas.addEventListener("webglcontextlost", this.contextLost);
    window.addEventListener("keydown", this.keyDown);
    window.addEventListener("keyup", this.keyUp);
    this.resize();
    this.render();
  }

  syncScene(snapshot: SceneSnapshot): void {
    this.sceneId = snapshot.sceneId;
    const visibleEntities = snapshot.entities.filter((entity) => entity.visual?.visible);
    const useInstancing = visibleEntities.length >= 200 &&
      visibleEntities.every((entity) => entity.visual?.shape !== "mesh");
    if (useInstancing !== this.instancing) {
      this.instancing = useInstancing;
      const deviceRatio = Math.min(window.devicePixelRatio, 2);
      this.renderer.setPixelRatio(useInstancing ? Math.min(deviceRatio, 0.75) : deviceRatio);
      this.resize();
      this.signatures.clear();
      this.clearInstances();
    }
    if (useInstancing && snapshot.revision !== this.instanceRevision) {
      this.buildInstances(snapshot);
      this.instanceRevision = snapshot.revision;
    }
    const incoming = new Set(snapshot.entities.filter((entity) => entity.id !== 0).map((entity) => entity.id));
    for (const [id, node] of this.nodes) {
      if (!incoming.has(id)) {
        this.transform.detach();
        node.removeFromParent();
        this.disposeObject(node);
        this.nodes.delete(id);
        this.entities.delete(id);
        this.signatures.delete(id);
      }
    }
    this.syncRenderContent(snapshot, useInstancing);
    this.applyTransforms(snapshot, useInstancing);
    this.select(this.selectedId && incoming.has(this.selectedId) ? this.selectedId : null);
  }

  /** Compact timeline frames share their immutable render data with the base snapshot. */
  syncTimelineFrame(snapshot: SceneSnapshot): void {
    const unchanged = this.sceneId === snapshot.sceneId &&
      this.entities.size === snapshot.entities.filter((entity) => entity.id !== 0).length &&
      this.signatures.size === this.entities.size && snapshot.entities.every((entity) => {
        if (entity.id === 0) return true;
        const previous = this.entities.get(entity.id);
        return previous && previous.kind === entity.kind && previous.parent === entity.parent &&
          previous.robotManaged === entity.robotManaged && previous.visual === entity.visual &&
          previous.collider === entity.collider && previous.light === entity.light &&
          previous.camera === entity.camera && previous.rigidBody === entity.rigidBody;
      });
    if (!unchanged) {
      console.debug("Timeline renderer full sync", { entities: snapshot.entities.length });
      this.syncScene(snapshot);
      return;
    }
    for (const entity of snapshot.entities) {
      if (entity.id !== 0) this.entities.set(entity.id, entity);
    }
    this.applyTransforms(snapshot, this.instancing);
  }

  private syncRenderContent(snapshot: SceneSnapshot, useInstancing: boolean): void {
    for (const entity of snapshot.entities) {
      if (entity.id === 0) continue;
      let node = this.nodes.get(entity.id);
      if (!node) {
        node = new THREE.Group();
        node.userData.entityId = entity.id;
        this.nodes.set(entity.id, node);
      }
      this.entities.set(entity.id, entity);
      const signature = JSON.stringify([entity.kind, entity.visual, entity.collider, entity.light,
        this.hasDynamicRigidBody(entity), entity.camera, this.colliderVisible, this.ifpsVisible]);
      if (this.signatures.get(entity.id) !== signature) {
        this.signatures.set(entity.id, signature);
        this.populateNode(node, entity, useInstancing, signature);
      }
    }
  }

  private applyTransforms(snapshot: SceneSnapshot, useInstancing: boolean): void {
    for (const entity of snapshot.entities) {
      if (entity.id === 0) continue;
      const node = this.nodes.get(entity.id)!;
      // Robot-managed links with no ECS parent use world transform; their
      // world transform is computed from JointComponent relations in C++ even
      // when HierarchyComponent::parent is kInvalidEntity (0).
      const useWorldTransform = !useInstancing && entity.robotManaged && !entity.parent;
      const transform = (useInstancing || useWorldTransform) ? entity.world : entity.local;
      if (useInstancing) {
        node.removeFromParent();
      } else {
        const parent = entity.parent ? this.nodes.get(entity.parent) : null;
        if (parent && node.parent !== parent) parent.add(node);
        if (!parent && node.parent !== this.simWorld) this.simWorld.add(node);
      }
      const [x, y, z] = transform.translation;
      const [w, qx, qy, qz] = transform.rotationWxyz;
      node.position.set(x, y, z);
      node.quaternion.set(qx, qy, qz, w);
      node.scale.fromArray(transform.scale);
    }
    if (useInstancing) this.updateInstanceMatrices(snapshot);
  }

  private populateNode(node: THREE.Object3D, entity: EntityView, useInstancing: boolean,
                       signature: string): void {
    for (const child of [...node.children]) {
      if (!child.userData.renderContent) continue;
      node.remove(child);
      this.disposeObject(child);
    }
    if (entity.visual?.visible && !useInstancing) {
      if (entity.visual.shape === "mesh" && entity.visual.resource) {
        const marker = this.meshMarker(entity.id);
        node.add(marker);
        void this.loadMeshVisual(node, entity, signature, marker);
      } else {
        const geometry = this.geometry(entity.visual.shape, entity.visual.size);
        const [r, g, b, a] = entity.visual.color;
        const material = new THREE.MeshStandardMaterial({
          color: new THREE.Color(r, g, b), transparent: a < 1, opacity: a,
          roughness: 0.7, metalness: 0.08,
        });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.userData.entityId = entity.id;
        mesh.userData.renderContent = true;
        node.add(mesh);
      }
    }
    if (entity.light?.enabled) {
      const color = new THREE.Color(...entity.light.color);
      const outerAngle = Math.max(0.001, Math.min(Math.PI * 0.499,
        entity.light.outerConeRadians));
      const innerAngle = Math.max(0, Math.min(outerAngle,
        entity.light.innerConeRadians));
      const penumbra = Math.max(0, Math.min(1,
        1 - innerAngle / outerAngle));
      const light = entity.light.type === "ambient" ? new THREE.AmbientLight(color, entity.light.intensity)
        : entity.light.type === "point" ? new THREE.PointLight(color, entity.light.intensity, entity.light.range, entity.light.decay)
        : entity.light.type === "spot" ? new THREE.SpotLight(color, entity.light.intensity, entity.light.range, outerAngle, penumbra, entity.light.decay)
        : new THREE.DirectionalLight(color, entity.light.intensity);
      light.userData.renderContent = true;
      node.add(light);
      if (light instanceof THREE.SpotLight || light instanceof THREE.DirectionalLight) {
        if (light instanceof THREE.SpotLight) light.target.position.set(0, 0, -1);
        else light.target.position.set(1, 0, 0);
        light.target.userData.renderContent = true;
        node.add(light.target);
      }
    }
    if (entity.light) {
      const color = new THREE.Color(...entity.light.color);
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(0.12, 12, 8),
        new THREE.MeshBasicMaterial({ color, wireframe: true }),
      );
      marker.userData.entityId = entity.id;
      marker.userData.renderContent = true;
      marker.userData.lightMarker = true;
      node.add(marker);
      const label = this.lightLabel(entity.light.type, color);
      label.position.set(0, 0, 0.25);
      label.userData.entityId = entity.id;
      label.userData.renderContent = true;
      label.userData.lightLabel = true;
      label.visible = false;
      node.add(label);
      if (entity.light.type === "point") {
        const range = Math.max(0.05, entity.light.range);
        const guide = this.pointRangeGuide(range, color);
        guide.userData.entityId = entity.id;
        guide.visible = false;
        node.add(guide);
      } else if (entity.light.type === "spot") {
        const guide = this.spotRangeGuide(Math.max(0.05, entity.light.range),
          entity.light.innerConeRadians, entity.light.outerConeRadians, color);
        guide.userData.entityId = entity.id;
        guide.visible = false;
        node.add(guide);
      }
    }
    if (entity.collider && this.colliderVisible) {
      if (entity.collider.shape === "mesh") {
        if (entity.collider.resource) void this.loadMeshCollider(node, entity, signature);
      } else {
        const wire = new THREE.LineSegments(
          new THREE.EdgesGeometry(this.geometry(entity.collider.shape, entity.collider.size)),
          new THREE.LineBasicMaterial({ color: this.colliderColor(entity) }),
        );
        wire.userData.debugOverlay = true;
        wire.userData.renderContent = true;
        node.add(wire);
      }
    }
    if (entity.collider && this.ifpsVisible && entity.collider.ifpsSphereCount > 0) {
      const count = Math.min(entity.collider.ifpsSphereCount, 64);
      for (let index = 0; index < count; index += 1) {
        const angle = (index / count) * Math.PI * 2;
        const sphere = new THREE.Mesh(
          new THREE.SphereGeometry(0.07, 8, 6),
          new THREE.MeshBasicMaterial({ color: 0xfbbf24, wireframe: true }),
        );
        sphere.position.set(Math.cos(angle) * 0.5, 0.12, Math.sin(angle) * 0.5);
        sphere.userData.debugOverlay = true;
        sphere.userData.renderContent = true;
        node.add(sphere);
      }
    }
    if (entity.camera) {
      const marker = new THREE.Mesh(
        new THREE.ConeGeometry(0.12, 0.3, 12),
        new THREE.MeshBasicMaterial({ color: 0x38bdf8, wireframe: true }),
      );
      marker.rotation.z = -Math.PI / 2;
      marker.position.x = 0.15;
      marker.userData.entityId = entity.id;
      marker.userData.renderContent = true;
      node.add(marker);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(
        cameraFrustumSegments(entity.camera), 3));
      const frustum = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0x38bdf8 }));
      frustum.userData.entityId = entity.id;
      frustum.userData.renderContent = true;
      node.add(frustum);
    }
  }

  private meshMarker(entityId: number): THREE.Mesh {
    const marker = new THREE.Mesh(
      new THREE.BoxGeometry(0.12, 0.12, 0.12),
      new THREE.MeshStandardMaterial({ color: 0x64748b, wireframe: true }),
    );
    marker.userData.entityId = entityId;
    marker.userData.renderContent = true;
    return marker;
  }

  private lightLabel(type: string, color: THREE.Color): THREE.Sprite {
    const canvas = document.createElement("canvas");
    canvas.width = 256; canvas.height = 40;
    const context = canvas.getContext("2d")!;
    context.font = "bold 22px sans-serif";
    context.fillStyle = `#${color.getHexString()}`;
    context.fillText(type, 4, 27);
    const texture = new THREE.CanvasTexture(canvas);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }));
    sprite.scale.set(1.8, 0.28, 1);
    return sprite;
  }

  private pointRangeGuide(range: number, color: THREE.Color): THREE.Group {
    const group = new THREE.Group();
    const material = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.32 });
    const circles = [
      (angle: number): THREE.Vector3 => new THREE.Vector3(Math.cos(angle) * range, Math.sin(angle) * range, 0),
      (angle: number): THREE.Vector3 => new THREE.Vector3(Math.cos(angle) * range, 0, Math.sin(angle) * range),
      (angle: number): THREE.Vector3 => new THREE.Vector3(0, Math.cos(angle) * range, Math.sin(angle) * range),
    ];
    for (const point of circles) {
      const points = Array.from({ length: 48 }, (_, index) => point(index / 48 * Math.PI * 2));
      group.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(points), material.clone()));
    }
    material.dispose();
    group.userData.renderContent = true;
    group.userData.lightRange = true;
    return group;
  }

  private spotRangeGuide(range: number, innerAngle: number, outerAngle: number,
                         color: THREE.Color): THREE.Group {
    const group = new THREE.Group();
    const clampedOuter = Math.max(0.001, Math.min(Math.PI * 0.495, outerAngle));
    const clampedInner = Math.max(0, Math.min(clampedOuter, innerAngle));
    const circle = (angle: number): THREE.Vector3[] => {
      const radius = range * Math.tan(angle);
      return Array.from({ length: 48 }, (_, index) => {
        const phase = index / 48 * Math.PI * 2;
        return new THREE.Vector3(Math.cos(phase) * radius, Math.sin(phase) * radius, -range);
      });
    };
    const outerMaterial = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.42 });
    const innerMaterial = new THREE.LineBasicMaterial({ color: 0xffd166, transparent: true, opacity: 0.62 });
    group.add(new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(circle(clampedOuter)), outerMaterial));
    if (clampedInner > 0) group.add(new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(circle(clampedInner)), innerMaterial));
    const coneEdges = (angle: number): THREE.Vector3[] => {
      const points: THREE.Vector3[] = [];
      const radius = range * Math.tan(angle);
      for (let index = 0; index < 4; index += 1) {
        const phase = index / 4 * Math.PI * 2;
        points.push(new THREE.Vector3(),
          new THREE.Vector3(Math.cos(phase) * radius, Math.sin(phase) * radius, -range));
      }
      return points;
    };
    group.add(new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(coneEdges(clampedOuter)), outerMaterial.clone()));
    if (clampedInner > 0) group.add(new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(coneEdges(clampedInner)), innerMaterial.clone()));
    group.userData.renderContent = true;
    group.userData.lightRange = true;
    return group;
  }

  private async loadMeshVisual(node: THREE.Object3D, entity: EntityView, signature: string,
                               marker: THREE.Object3D): Promise<void> {
    const resource = entity.visual?.resource;
    if (!resource) return;
    try {
      const template = await this.loadAsset(resource);
      if (this.disposed || this.nodes.get(entity.id) !== node ||
          this.signatures.get(entity.id) !== signature) return;
      const meshIndex = this.meshIndex(resource);
      const object = this.cloneAsset(template, meshIndex);
      // Only apply the entity's flat color to formats that carry no material
      // data of their own (STL). DAE, OBJ+MTL, and FBX embed their materials,
      // so tinting would overwrite the file's colors with the URDF link color.
      const resourceFormat = (new URL(resource, window.location.href)
        .searchParams.get("format") ?? resource.split(".").pop() ?? "").toLowerCase();
      if (resourceFormat === "stl") this.tintAsset(object, entity.visual!.color);
      object.userData.entityId = entity.id;
      object.userData.renderContent = true;
      object.traverse((child) => { child.userData.entityId = entity.id; });
      if (marker.parent === node) {
        node.remove(marker);
        this.disposeObject(marker);
      }
      node.add(object);
      if (this.selectedId === entity.id) this.select(entity.id);
    } catch (error) {
      this.callbacks.onError(`Failed to load mesh ${resource}: ${String(error)}`);
    }
  }

  private async loadMeshCollider(node: THREE.Object3D, entity: EntityView,
                                 signature: string): Promise<void> {
    const resource = entity.collider?.resource;
    if (!resource) return;
    try {
      const template = await this.loadAsset(resource);
      if (this.disposed || this.nodes.get(entity.id) !== node ||
          this.signatures.get(entity.id) !== signature) return;
      const object = this.cloneAsset(template, this.meshIndex(resource));
      const color = this.colliderColor(entity);
      object.userData.debugOverlay = true;
      object.userData.renderContent = true;
      object.traverse((child) => {
        child.userData.debugOverlay = true;
        const mesh = child as THREE.Mesh;
        if (!mesh.isMesh) return;
        const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
        for (const material of materials) material.dispose();
        mesh.material = new THREE.MeshBasicMaterial({ color, wireframe: true });
      });
      node.add(object);
    } catch (error) {
      this.callbacks.onError(`Failed to load collider mesh ${resource}: ${String(error)}`);
    }
  }

  private colliderColor(entity: EntityView): number {
    return entity.collider?.trigger ? 0xff3bd5 :
      this.hasDynamicRigidBody(entity) ? 0x22d3ee : 0x34d399;
  }

  private hasDynamicRigidBody(entity: EntityView): boolean {
    let current: EntityView | undefined = entity;
    while (current) {
      if (current.rigidBody) return current.rigidBody.dynamic;
      current = current.parent === null ? undefined : this.entities.get(current.parent);
    }
    return false;
  }

  private loadAsset(resource: string): Promise<THREE.Object3D> {
    const url = resource.split("#", 1)[0]!;
    const existing = this.assetCache.get(url);
    if (existing) return existing;
    const parsedForFormat = new URL(url, window.location.href);
    const format = (parsedForFormat.searchParams.get("format") ??
      parsedForFormat.pathname.split(".").pop() ?? "").toLowerCase() || undefined;
    let loading: Promise<THREE.Object3D>;
    if (format === "stl") {
      loading = new STLLoader().loadAsync(url).then((geometry) => {
        if (!geometry.hasAttribute("normal")) geometry.computeVertexNormals();
        return new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ roughness: 0.7, metalness: 0.08 }));
      });
    } else if (format === "obj") {
      loading = fetch(url).then(async (response) => {
        if (!response.ok) throw new Error(`OBJ request failed (${response.status})`);
        const source = await response.text();
        const materialName = source.match(/^\s*mtllib\s+(.+)\s*$/im)?.[1]?.trim();
        const loader = new OBJLoader();
        if (materialName) {
          let materialUrl: string;
          const parsedUrl = new URL(url, window.location.href);
          if (parsedUrl.origin !== window.location.origin) {
            // Remote asset: resolve the MTL relative to the OBJ's directory on the
            // origin server. The browser fetches it directly — no gateway involved.
            const baseUrl = url.endsWith("/") ? url : url.slice(0, url.lastIndexOf("/") + 1);
            materialUrl = new URL(materialName.replace(/\\/g, "/"), baseUrl).toString();
          } else {
            // Gateway-served asset: reconstruct the /file/<name>?format=mtl URL.
            const materialUrlObject = new URL(url, window.location.href);
            materialUrlObject.pathname = `${materialUrlObject.pathname.replace(/\/file$/, "/file/")}${materialName.replace(/\\/g, "/").split("/").map(encodeURIComponent).join("/")}`;
            materialUrlObject.search = "?format=mtl";
            materialUrl = materialUrlObject.toString();
          }
          const materials = await new MTLLoader().loadAsync(materialUrl);
          materials.preload();
          loader.setMaterials(materials);
        }
        return loader.parse(source);
      });
    } else if (format === "fbx") {
      loading = new FBXLoader().loadAsync(url).then((root) => {
        // FBXLoader sets root.scale to UnitScaleFactor/100 when FBX unit metadata is
        // present. When the metadata is absent it leaves scale at [1,1,1], but the C++
        // asset pipeline defaults to UnitScaleFactor=1 (centimetres) and normalises
        // vertex positions to metres. Apply the same 0.01 to stay in sync.
        if (Math.abs(root.scale.x - 1) < 1e-5 &&
            Math.abs(root.scale.y - 1) < 1e-5 &&
            Math.abs(root.scale.z - 1) < 1e-5) {
          root.scale.set(0.01, 0.01, 0.01);
        }
        return root;
      });
    } else if (format === "dae") {
      loading = new ColladaLoader().loadAsync(url).then((result) => {
        const scene = result.scene;
        // ColladaLoader detects Z_UP assets and pre-rotates the root scene by
        // -π/2 around X to normalise to Y-up. simWorld already applies that
        // same rotation for the entire sim world, so undo it here to prevent
        // the conversion from being applied twice.
        scene.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));
        return scene;
      });
    } else {
      loading = Promise.reject(new Error(`unsupported browser mesh format: ${format ?? "unknown"}`));
    }
    const cached = loading.catch((error) => {
      this.assetCache.delete(url);
      throw error;
    });
    this.assetCache.set(url, cached);
    return cached;
  }

  private meshIndex(resource: string): number | null {
    const match = resource.match(/#mesh=(\d+)$/);
    return match ? Number(match[1]) : null;
  }

  private cloneAsset(template: THREE.Object3D, meshIndex: number | null): THREE.Object3D {
    if (meshIndex !== null) {
      const meshes: THREE.Mesh[] = [];
      template.traverse((child) => { if ((child as THREE.Mesh).isMesh) meshes.push(child as THREE.Mesh); });
      const source = meshes[meshIndex];
      if (!source) throw new Error(`mesh index ${meshIndex} is out of range`);
      const selected = source.clone(false) as THREE.Mesh;
      selected.geometry = source.geometry.clone();
      selected.material = Array.isArray(source.material)
        ? source.material.map((material) => material.clone()) : source.material.clone();
      selected.position.set(0, 0, 0);
      selected.quaternion.identity();
      selected.scale.set(1, 1, 1);
      return selected;
    }
    const clone = template.clone(true);
    clone.traverse((child) => {
      const mesh = child as THREE.Mesh;
      if (!mesh.isMesh) return;
      mesh.geometry = mesh.geometry.clone();
      mesh.material = Array.isArray(mesh.material)
        ? mesh.material.map((material) => material.clone()) : mesh.material.clone();
    });
    return clone;
  }

  private tintAsset(object: THREE.Object3D, color: [number, number, number, number]): void {
    const [r, g, b, a] = color;
    // Skip color overwrite for neutral white (1,1,1,1) so OBJ/MTL/DAE material
    // colors loaded from the mesh file are preserved.
    const isNeutralWhite = r >= 1 && g >= 1 && b >= 1 && a >= 1;
    object.traverse((child) => {
      const mesh = child as THREE.Mesh;
      if (!mesh.isMesh) return;
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) {
        const tint = material as THREE.MeshStandardMaterial;
        if (tint.color && !isNeutralWhite) tint.color.setRGB(r, g, b);
        tint.transparent = a < 1;
        tint.opacity = a;
      }
    });
  }

  private geometry(shape: string, size: Vec3): THREE.BufferGeometry {
    if (shape === "sphere") return new THREE.SphereGeometry(Math.max(size[0], 0.02), 24, 16);
    if (shape === "capsule") return new THREE.CapsuleGeometry(Math.max(size[0], 0.02), Math.max(size[2], 0.02), 8, 16);
    if (shape === "cylinder") return new THREE.CylinderGeometry(Math.max(size[0], 0.02), Math.max(size[0], 0.02), Math.max(size[2], 0.02), 24);
    return new THREE.BoxGeometry(Math.max(size[0], 0.02), Math.max(size[1], 0.02), Math.max(size[2], 0.02));
  }

  private buildInstances(snapshot: SceneSnapshot): void {
    this.clearInstances();
    const groups = new Map<string, EntityView[]>();
    for (const entity of snapshot.entities) {
      if (!entity.visual?.visible || entity.camera) continue;
      const key = JSON.stringify([entity.visual.shape, entity.visual.size, entity.visual.color]);
      const group = groups.get(key) ?? [];
      group.push(entity);
      groups.set(key, group);
    }
    for (const entities of groups.values()) {
      const visual = entities[0]!.visual!;
      const [r, g, b, a] = visual.color;
      const mesh = new THREE.InstancedMesh(
        this.geometry(visual.shape, visual.size),
        new THREE.MeshStandardMaterial({ color: new THREE.Color(r, g, b), transparent: a < 1,
          opacity: a, roughness: 0.7, metalness: 0.08 }),
        entities.length,
      );
      mesh.userData.instanceIds = entities.map((entity) => entity.id);
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      this.simWorld.add(mesh);
      this.instanceMeshes.push(mesh);
      entities.forEach((entity, index) => this.instanceRecords.set(entity.id, { mesh, index }));
    }
  }

  private updateInstanceMatrices(snapshot: SceneSnapshot): void {
    const matrix = new THREE.Matrix4();
    const position = new THREE.Vector3();
    const quaternion = new THREE.Quaternion();
    const scale = new THREE.Vector3();
    for (const entity of snapshot.entities) {
      const record = this.instanceRecords.get(entity.id);
      if (!record) continue;
      position.fromArray(entity.world.translation);
      const [w, x, y, z] = entity.world.rotationWxyz;
      quaternion.set(x, y, z, w);
      scale.fromArray(entity.world.scale);
      matrix.compose(position, quaternion, scale);
      record.mesh.setMatrixAt(record.index, matrix);
    }
    for (const mesh of this.instanceMeshes) {
      mesh.instanceMatrix.needsUpdate = true;
      mesh.computeBoundingSphere();
    }
  }

  private clearInstances(): void {
    for (const mesh of this.instanceMeshes) {
      mesh.removeFromParent();
      mesh.geometry.dispose();
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) material.dispose();
    }
    this.instanceMeshes.length = 0;
    this.instanceRecords.clear();
  }

  select(entityId: number | null): void {
    for (const node of this.nodes.values()) {
      for (const child of [...node.children]) {
        if (!child.userData.selectionProxy) continue;
        node.remove(child);
        this.disposeObject(child);
      }
    }
    this.selectedId = entityId;
    for (const node of this.nodes.values()) {
      node.traverse((child) => {
        if (child.userData.lightLabel) child.visible = child.userData.entityId === entityId;
        if (child.userData.lightRange) child.visible = child.userData.entityId === entityId;
      });
    }
    this.transform.detach();
    if (this.selectionBox) {
      this.selectionBox.removeFromParent();
      this.selectionBox.geometry.dispose();
      this.selectionBox.material.dispose();
      this.selectionBox = null;
    }
    const node = entityId == null ? null : this.nodes.get(entityId);
    if (!node) return;
    if (this.instancing) {
      for (const candidate of this.nodes.values()) {
        if (candidate !== node && candidate.parent === this.simWorld) candidate.removeFromParent();
      }
      if (node.parent !== this.simWorld) this.simWorld.add(node);
    }
    const entity = entityId === null ? null : this.entities.get(entityId);
    if (this.instancing && entity?.visual) {
      const proxy = new THREE.Mesh(
        this.geometry(entity.visual.shape, entity.visual.size),
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
      );
      proxy.userData.selectionProxy = true;
      node.add(proxy);
    }
    this.selectionBox = new THREE.BoxHelper(node, 0xfacc15);
    this.scene.add(this.selectionBox);
    if (this.tool !== "select" && entityId !== null && !this.entities.get(entityId)?.robotManaged) {
      this.transform.attach(node);
    }
  }

  setTool(tool: Tool): void {
    this.tool = tool;
    this.transform.enabled = tool !== "select";
    if (tool === "select") this.transform.detach();
    else if (this.selectedId !== null) this.select(this.selectedId);
    this.transform.getHelper().visible = tool !== "select" && this.selectedId !== null;
    if (tool !== "select") this.transform.setMode(tool === "move" ? "translate" : tool);
  }

  setInteractions(orbit: boolean, selection: boolean): void {
    this.orbitInteractionEnabled = orbit;
    this.selectionInteractionEnabled = selection;
    this.orbit.enabled = orbit && !this.transform.dragging;
  }

  setOverlays(collider: boolean, ifps: boolean): void {
    this.colliderVisible = collider;
    this.ifpsVisible = ifps;
    this.signatures.clear();
  }

  frame(entityId?: number): void {
    const box = new THREE.Box3();
    if (entityId && this.nodes.has(entityId)) box.setFromObject(this.nodes.get(entityId)!);
    else if (this.instancing) for (const mesh of this.instanceMeshes) box.expandByObject(mesh);
    else for (const node of this.nodes.values()) box.expandByObject(node);
    if (box.isEmpty()) return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const direction = new THREE.Vector3(1, 0.75, 1).normalize();
    const verticalFov = THREE.MathUtils.degToRad(this.camera.fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * this.camera.aspect);
    const limitingFov = Math.min(verticalFov, horizontalFov);
    const distance = sphere.radius / Math.max(Math.sin(limitingFov / 2), 0.01) * 1.2;
    this.orbit.target.copy(sphere.center);
    this.camera.position.copy(sphere.center).addScaledVector(
      direction, Math.max(distance, this.camera.near * 4));
    this.orbit.update();
  }

  resize = (): void => {
    const width = Math.max(this.canvas.clientWidth, 1);
    const height = Math.max(this.canvas.clientHeight, 1);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  };

  private pick = (event: PointerEvent): void => {
    if (!this.selectionInteractionEnabled || this.transform.dragging) return;
    const bounds = this.canvas.getBoundingClientRect();
    this.pointer.set(((event.clientX - bounds.left) / bounds.width) * 2 - 1, -((event.clientY - bounds.top) / bounds.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects([...this.nodes.values()], true);
    const hit = hits.find((entry) => !entry.object.userData.debugOverlay && !entry.object.userData.selectionProxy);
    const instanceIds = hit?.object.userData.instanceIds as number[] | undefined;
    if (instanceIds && hit?.instanceId !== undefined) {
      this.callbacks.onSelect(instanceIds[hit.instanceId] ?? null);
      return;
    }
    let object: THREE.Object3D | null = hit?.object ?? null;
    while (object && object.userData.entityId == null) object = object.parent;
    this.callbacks.onSelect(object?.userData.entityId ?? null);
  };

  private commitTransform(): void {
    if (this.selectedId == null) return;
    const node = this.nodes.get(this.selectedId);
    if (!node) return;
    const position = new THREE.Vector3();
    const quaternion = new THREE.Quaternion();
    const scale = new THREE.Vector3();
    if (this.instancing) {
      node.updateMatrix();
      const entity = this.entities.get(this.selectedId);
      const parent = entity?.parent ? this.entities.get(entity.parent) : null;
      const localMatrix = node.matrix.clone();
      if (parent) {
        const [w, x, y, z] = parent.world.rotationWxyz;
        const parentMatrix = new THREE.Matrix4().compose(
          new THREE.Vector3().fromArray(parent.world.translation),
          new THREE.Quaternion(x, y, z, w),
          new THREE.Vector3().fromArray(parent.world.scale),
        );
        localMatrix.premultiply(parentMatrix.invert());
      }
      localMatrix.decompose(position, quaternion, scale);
    } else {
      position.copy(node.position); quaternion.copy(node.quaternion); scale.copy(node.scale);
    }
    this.callbacks.onTransform(this.selectedId, {
      translation: position.toArray() as Vec3,
      rotationWxyz: [quaternion.w, quaternion.x, quaternion.y, quaternion.z],
      scale: scale.toArray() as Vec3,
    });
  }

  private keyDown = (event: KeyboardEvent): void => {
    if ((event.target as HTMLElement)?.matches("input, textarea, select")) return;
    this.pressed.add(event.code);
  };
  private keyUp = (event: KeyboardEvent): void => { this.pressed.delete(event.code); };

  private updateFlight(delta: number): void {
    const speed = delta * 5;
    const forward = new THREE.Vector3();
    this.camera.getWorldDirection(forward);
    const right = new THREE.Vector3().crossVectors(forward, this.camera.up).normalize();
    const movement = new THREE.Vector3();
    if (this.pressed.has("KeyW")) movement.add(forward);
    if (this.pressed.has("KeyS")) movement.sub(forward);
    if (this.pressed.has("KeyD")) movement.add(right);
    if (this.pressed.has("KeyA")) movement.sub(right);
    if (this.pressed.has("KeyE")) movement.y += 1;
    if (this.pressed.has("KeyQ")) movement.y -= 1;
    if (movement.lengthSq()) {
      movement.normalize().multiplyScalar(speed);
      this.camera.position.add(movement);
      this.orbit.target.add(movement);
    }
  }

  private render = (): void => {
    if (this.disposed) return;
    const start = performance.now();
    const delta = Math.min(this.clock.getDelta(), 0.1);
    this.updateFlight(delta);
    this.orbit.update();
    this.selectionBox?.update();
    this.renderer.render(this.scene, this.camera);
    this.frames += 1;
    const now = performance.now();
    if (now - this.frameWindow >= 500) {
      this.callbacks.onPerformance(this.frames * 1000 / (now - this.frameWindow), now - start);
      this.frames = 0;
      this.frameWindow = now;
    }
    this.animation = requestAnimationFrame(this.render);
  };

  private contextLost = (event: Event): void => {
    event.preventDefault();
    this.callbacks.onError("WebGL context lost. Waiting for browser recovery.");
  };

  private disposeObject(object: THREE.Object3D): void {
    object.traverse((child) => {
      const mesh = child as THREE.Mesh;
      mesh.geometry?.dispose();
      const materials = Array.isArray(mesh.material) ? mesh.material : mesh.material ? [mesh.material] : [];
      for (const material of materials) material.dispose();
    });
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.animation);
    this.canvas.removeEventListener("pointerdown", this.pick);
    this.canvas.removeEventListener("webglcontextlost", this.contextLost);
    window.removeEventListener("keydown", this.keyDown);
    window.removeEventListener("keyup", this.keyUp);
    this.orbit.dispose();
    this.transform.dispose();
    this.clearInstances();
    for (const loading of this.assetCache.values()) {
      void loading.then((asset) => this.disposeObject(asset)).catch(() => undefined);
    }
    this.assetCache.clear();
    this.renderer.dispose();
  }
}
