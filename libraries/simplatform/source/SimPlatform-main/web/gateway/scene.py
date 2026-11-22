from __future__ import annotations

import math
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sim_platform import Robot, Simulator, Transform
from sim_platform.sdk import Camera, Entity, State

from .assets import AssetService


SHAPES = {0: "box", 1: "sphere", 2: "capsule", 3: "cylinder", 4: "mesh"}


class SceneError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class BackendConfig:
    physics: str = "none"
    collision: str = "fcl"
    renderer: str = "vulkan"
    browser_renderer: str = "three"
    fixed_time_step: float = 0.002
    real_time_factor: float = 1.0


class SceneService:
    scene_id = "main"

    @staticmethod
    def _validate_spot_cone(light_type: str, inner: float, outer: float) -> None:
        if light_type != "spot":
            return
        if (not math.isfinite(inner) or not math.isfinite(outer) or inner < 0.0 or
                outer <= 0.0 or inner > outer or outer >= math.pi / 2.0):
            raise SceneError(
                "INVALID_LIGHT_CONE",
                "Spotlight cone angles must satisfy 0 <= inner <= outer < pi/2 radians",
            )

    def __init__(self, assets: AssetService, *, cache_directory: Path, plugin_directory: Path):
        self.assets = assets
        self.cache_directory = cache_directory
        self.plugin_directory = plugin_directory
        self.config = BackendConfig()
        self.lock = threading.RLock()
        self.revision = 1
        self.status = "paused"
        self.last_physics_ms = 0.0
        self.logs: list[dict[str, Any]] = []
        self.states: dict[str, State] = {}
        self.undo_stack: list[State] = []
        self.redo_stack: list[State] = []
        self._kinds: dict[int, str] = {}
        self._source_assets: dict[int, str] = {}
        self._create_native()
        self.initial_state = self.simulator.save_state(self.world)

    def _create_native(self) -> None:
        self.simulator = Simulator(
            physics=self.config.physics,
            collision=self.config.collision,
            cache_directory=self.cache_directory,
            plugin_directory=self.plugin_directory,
        )
        self.world = self.simulator.create_world(fixed_time_step=self.config.fixed_time_step)
        default_light = self.world.create_light("Sky Light", "directional", intensity=1.0,
                                                angular_diameter_radians=0.05)
        # Directional lights emit along local +X; rotate the default sky light to point down.
        default_light.transform = Transform(
            default_light.transform.translation, (0.7071067812, 0.0, 0.7071067812, 0.0),
            default_light.transform.scale)
        self._kinds[default_light.handle] = "light"
        ambient_light = self.world.create_light("环境光", "ambient", intensity=0.25)
        self._kinds[ambient_light.handle] = "light"

    def close(self) -> None:
        with self.lock:
            self.simulator.close()

    def save_state_file(self, path: Path) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.simulator.save_state_file(self.world, path)

    def reset_to_default(self) -> None:
        """Replace the current runtime with a clean default conversation scene."""
        with self.lock:
            old_simulator = self.simulator
            old_world = self.world
            old_kinds = self._kinds
            old_sources = self._source_assets
            try:
                self._kinds = {}
                self._source_assets = {}
                self._create_native()
                self.simulator.pause(self.world)
                self.status = "paused"
                self.initial_state = self.simulator.save_state(self.world)
                self.states.clear()
                self.undo_stack.clear()
                self.redo_stack.clear()
                self.revision += 1
                old_simulator.close()
                self._log("info", "conversation.create", revision=self.revision)
            except Exception:
                self.simulator.close()
                self.simulator = old_simulator
                self.world = old_world
                self._kinds = old_kinds
                self._source_assets = old_sources
                raise

    def restore_state_file(self, path: Path) -> None:
        with self.lock:
            if not path.is_file():
                raise SceneError("STATE_NOT_FOUND", "Conversation scene state was not found")
            old_simulator = self.simulator
            old_world = self.world
            old_kinds = self._kinds
            old_sources = self._source_assets
            try:
                self._create_native()
                self.simulator.restore_state_file(self.world, path)
                self.simulator.pause(self.world)
                self.status = "paused"
                self.initial_state = self.simulator.save_state(self.world)
                self.states.clear()
                self.undo_stack.clear()
                self.redo_stack.clear()
                self._kinds = {}
                self._source_assets = {}
                self.revision += 1
                old_simulator.close()
                self._log("info", "conversation.restore", revision=self.revision)
            except Exception:
                self.simulator.close()
                self.simulator = old_simulator
                self.world = old_world
                self._kinds = old_kinds
                self._source_assets = old_sources
                raise

    def asset_in_use(self, asset_id: str) -> bool:
        with self.lock:
            live = set(self._native_info())
            return any(entity in live and source == asset_id
                       for entity, source in self._source_assets.items())

    def _log(self, level: str, message: str, **details: Any) -> None:
        self.logs.append({"time": time.time(), "level": level, "message": message, **details})
        del self.logs[:-300]

    def _native_info(self) -> dict[int, Any]:
        return {item.entity: item for item in self.world.editor_entities()}

    @staticmethod
    def _transform(value: Transform) -> dict[str, list[float]]:
        return {
            "translation": list(value.translation),
            "rotationWxyz": list(value.rotation_wxyz),
            "scale": list(value.scale),
        }

    def _kind(self, item: Any) -> str:
        if item.entity in self._kinds:
            return self._kinds[item.entity]
        if "robot" in item.flags:
            return "robot"
        if "joint" in item.flags:
            return "link"
        if "camera" in item.flags:
            return "camera"
        if "light" in item.flags:
            return "light"
        if item.visual:
            return SHAPES.get(int(item.visual["shape"]), "mesh")
        return "entity"

    def _robot_view(self, item: Any) -> dict[str, Any] | None:
        if "robot" not in item.flags:
            return None
        robot = Robot(self.world, item.entity)
        positions, velocities, efforts = robot.joint_state
        return {
            "jointNames": list(robot.joint_names),
            "positions": positions.tolist(),
            "velocities": velocities.tolist(),
            "efforts": efforts.tolist(),
        }

    def snapshot(self, *, agent_connected: bool = False,
                 agent_endpoint: str | None = None) -> dict[str, Any]:
        with self.lock:
            entities: list[dict[str, Any]] = [{
                "id": 0, "name": "World", "parent": None, "kind": "world",
                "flags": [], "robotManaged": False,
                "local": self._transform(Transform()), "world": self._transform(Transform()),
            }]
            for item in self.world.editor_entities():
                visual = None
                if item.visual:
                    visual = {
                        "visible": item.visual["visible"],
                        "minLod": item.visual["min_lod"],
                        "maxLod": item.visual["max_lod"],
                        "shape": SHAPES.get(int(item.visual["shape"]), "mesh"),
                        "size": list(item.visual["size"]),
                        "color": list(item.visual["color"]),
                        "resource": self.assets.register_resource(str(item.visual["resource"])),
                    }
                collider = None
                if item.collider:
                    collider = {
                        "shape": SHAPES.get(int(item.collider["shape"]), "mesh"),
                        "size": list(item.collider["size"]),
                        "resource": self.assets.register_resource(
                            str(item.collider["resource"])),
                        "trigger": item.collider["trigger"],
                        "friction": list(item.collider["friction"]),
                        "restitution": item.collider["restitution"],
                        "ifpsSphereCount": item.collider["ifps_sphere_count"],
                    }
                rigid_body = None
                if item.rigid_body:
                    rigid_body = {"enabled": True, "dynamic": item.rigid_body["dynamic"],
                                  "gravity": item.rigid_body["gravity"],
                                  "mass": item.rigid_body["mass"]}
                camera = None
                if item.camera and item.sensor:
                    camera = {
                        "output": item.camera["output"],
                        "frameId": item.sensor["frame_id"],
                        "updateRateHz": item.sensor["update_rate_hz"],
                        "width": item.camera["width"], "height": item.camera["height"],
                        "verticalFovDegrees": math.degrees(item.camera["vertical_fov_radians"]),
                        "nearPlane": item.camera["near_plane"],
                        "farPlane": item.camera["far_plane"],
                    }
                light = None
                if item.light:
                    light = {
                        "type": item.light["type"], "color": list(item.light["color"]),
                        "intensity": item.light["intensity"], "range": item.light["range"],
                        "decay": item.light["decay"], "innerConeRadians": item.light["inner_cone_radians"],
                        "outerConeRadians": item.light["outer_cone_radians"],
                        "angularDiameterRadians": item.light["angular_diameter_radians"],
                        "enabled": item.light["enabled"],
                    }
                entities.append({
                    "id": item.entity, "name": item.name, "parent": item.parent or 0,
                    "kind": self._kind(item), "flags": sorted(item.flags),
                    "robotManaged": "joint" in item.flags,
                    "local": self._transform(item.local), "world": self._transform(item.world),
                    **({"visual": visual} if visual else {}),
                    **({"collider": collider} if collider else {}),
                    **({"rigidBody": rigid_body} if rigid_body else {}),
                    **({"camera": camera} if camera else {}),
                    **({"light": light} if light else {}),
                    **({"robot": self._robot_view(item)} if "robot" in item.flags else {}),
                })
            return {
                "sceneId": self.scene_id, "revision": self.revision,
                "status": self.status, "simulationTime": self.simulator.time(self.world),
                "entities": entities,
                "settings": {
                    "physics": self.config.physics, "collision": self.config.collision,
                    "renderer": self.config.renderer,
                    "browserRenderer": self.config.browser_renderer,
                    "fixedTimeStep": self.config.fixed_time_step,
                    "realTimeFactor": self.config.real_time_factor,
                },
                "capabilities": {
                    "physics": ["none", "mujoco", "bullet3"],
                    "collision": ["fcl", "bullet3", "coal", "ifps-cpu", "ifps-gpu"],
                    "renderer": ["none", "vulkan"],
                    "browserRenderer": ["three"],
                },
                "performance": {"renderFps": 60.0, "renderMs": 0.0,
                                "physicsMs": self.last_physics_ms},
                "agent": {"connected": agent_connected, "endpoint": agent_endpoint},
            }

    def _entity(self, entity_id: int) -> Entity:
        if entity_id == 0:
            raise SceneError("INVALID_ENTITY", "World root is not a native entity")
        if entity_id not in self._native_info():
            raise SceneError("ENTITY_NOT_FOUND", "Entity was not found")
        return self.world.entity(entity_id)

    def _capture_edit_state(self) -> None:
        self.initial_state = self.simulator.save_state(self.world)

    def _create_primitive(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind", "cube")).lower()
        shape = "box" if kind in {"cube", "plane"} else kind
        if shape not in {"box", "sphere", "capsule", "cylinder"}:
            raise SceneError("INVALID_ARGUMENT", "Primitive kind is not supported")
        size = params.get("size", [1.0, 1.0, 0.02 if kind == "plane" else 1.0])
        entity = self.world.create_primitive(str(params.get("name", kind.title())), shape, size)
        self._kinds[entity.handle] = kind
        parent = int(params.get("parent", 0))
        if parent:
            entity.set_parent(self._entity(parent))
        if "translation" in params:
            current = entity.transform
            entity.transform = Transform(tuple(params["translation"]), current.rotation_wxyz,
                                         current.scale)
        self._capture_edit_state()
        return {"entity": entity.handle}

    def _create_primitives(self, params: dict[str, Any]) -> dict[str, Any]:
        count = int(params.get("count", 1))
        if count < 1 or count > 10_000:
            raise SceneError("INVALID_ARGUMENT", "Primitive count must be between 1 and 10000")
        kind = str(params.get("kind", "cube")).lower()
        shape = "box" if kind in {"cube", "plane"} else kind
        if shape not in {"box", "sphere", "capsule", "cylinder"}:
            raise SceneError("INVALID_ARGUMENT", "Primitive kind is not supported")
        columns = max(1, math.ceil(math.sqrt(count)))
        spacing = float(params.get("spacing", 0.35))
        size = params.get("size", [0.2, 0.2, 0.2])
        handles: list[int] = []
        root: Entity | None = None
        for index in range(count):
            entity = self.world.create_primitive(f"{kind.title()} {index + 1}", shape, size)
            x = (index % columns - columns / 2) * spacing
            z = (index // columns - columns / 2) * spacing
            entity.transform = Transform((x, 0.1, z), entity.transform.rotation_wxyz,
                                         entity.transform.scale)
            if root is None:
                root = entity
            elif bool(params.get("group", True)):
                entity.set_parent(root, preserve_world=True)
            self._kinds[entity.handle] = kind
            handles.append(entity.handle)
        self._capture_edit_state()
        return {"entities": handles, "count": count, "root": root.handle if root else None}

    def _create_camera(self, params: dict[str, Any]) -> dict[str, Any]:
        parent_id = int(params.get("parent", 0))
        parent = self._entity(parent_id) if parent_id else None
        output = str(params.get("output", "rgbd")).lower()
        camera = self.world.create_camera(
            parent, frame_id=str(params.get("frameId", "camera_optical_frame")),
            update_rate_hz=float(params.get("updateRateHz", 30.0)),
            width=int(params.get("width", 640)), height=int(params.get("height", 480)),
            vertical_fov_radians=math.radians(float(params.get("verticalFovDegrees", 60.0))),
            near_plane=float(params.get("nearPlane", 0.1)),
            far_plane=float(params.get("farPlane", 10.0)))
        camera.configure(
            frame_id=str(params.get("frameId", "camera_optical_frame")),
            update_rate_hz=float(params.get("updateRateHz", 30.0)),
            width=int(params.get("width", 640)), height=int(params.get("height", 480)),
            vertical_fov_radians=math.radians(float(params.get("verticalFovDegrees", 60.0))),
            near_plane=float(params.get("nearPlane", 0.1)),
            far_plane=float(params.get("farPlane", 10.0)), output=output)
        self._kinds[camera.handle] = "camera"
        camera.set_name(str(params.get("name", f"{output.upper()} Camera")))
        self._capture_edit_state()
        return {"entity": camera.handle}

    def _create_light(self, params: dict[str, Any]) -> dict[str, Any]:
        light_type = str(params.get("type", "point")).lower()
        inner_cone = float(params.get("innerConeRadians", 0.35))
        outer_cone = float(params.get("outerConeRadians", 0.7))
        self._validate_spot_cone(light_type, inner_cone, outer_cone)
        default_names = {"point": "Point Light", "spot": "Spotlight",
                         "directional": "Directional Light", "parallel": "Directional Light",
                         "sky": "Sky Light", "ambient": "环境光"}
        light = self.world.create_light(str(params.get("name", default_names.get(
            light_type, "Point Light"))), light_type,
                                        self._entity(int(params["parent"])) if int(params.get("parent", 0)) else None,
                                        color=params.get("color", [1.0, 1.0, 1.0]), intensity=float(params.get("intensity", 1.0)),
                                        range=float(params.get("range", 10.0)), decay=float(params.get("decay", 2.0)),
                                        inner_cone_radians=inner_cone,
                                        outer_cone_radians=outer_cone,
                                        angular_diameter_radians=float(params.get("angularDiameterRadians", 0.0)),
                                        enabled=bool(params.get("enabled", True)))
        current = light.transform
        rotation = params.get("rotationWxyz",
                              [0.7071067812, 0.0, 0.7071067812, 0.0]
                              if light_type in {"directional", "parallel", "sky"}
                              else current.rotation_wxyz)
        light.transform = Transform(tuple(params.get("translation", current.translation)),
                                    tuple(rotation), current.scale)
        self._kinds[light.handle] = "light"
        self._capture_edit_state()
        return {"entity": light.handle}

    def _load_asset(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self.assets.require(str(params.get("assetId", "")))
        if record.path is None:
            raise SceneError("INVALID_ASSET", "Built-in assets use create commands")
        try:
            native: Entity
            if record.path.suffix.lower() == ".urdf":
                native = self.world.load_robot(record.path)
                self._kinds[native.handle] = "robot"
            else:
                native = self.world.load_asset(record.path)
                self._kinds[native.handle] = "asset"
            self._source_assets[native.handle] = record.id
            if int(params.get("parent", 0)):
                native.set_parent(self._entity(int(params["parent"])))
            if "translation" in params:
                current = native.transform
                native.transform = Transform(tuple(params["translation"]), current.rotation_wxyz,
                                             current.scale)
            self._capture_edit_state()
            self._log("info", f"Loaded asset {record.name}", assetId=record.id)
            return {"entity": native.handle}
        except Exception as error:
            raise SceneError("ASSET_IMPORT_FAILED", str(error)) from error

    def _update_entity(self, params: dict[str, Any]) -> dict[str, Any]:
        entity_id = int(params.get("entity", 0))
        entity = self._entity(entity_id)
        info = self._native_info()[entity_id]
        if "name" in params:
            entity.set_name(str(params["name"]))
        transform = params.get("transform")
        if isinstance(transform, dict):
            local = info.local
            entity.transform = Transform(
                tuple(transform.get("translation", local.translation)),
                tuple(transform.get("rotationWxyz", local.rotation_wxyz)),
                tuple(transform.get("scale", local.scale)))
        visual = params.get("visual")
        if isinstance(visual, dict):
            current = info.visual
            if current is None:
                raise SceneError("COMPONENT_MISSING", "Entity has no Visual component")
            entity.set_visual(visible=bool(visual.get("visible", current["visible"])),
                              min_lod=int(visual.get("minLod", current["min_lod"])),
                              max_lod=int(visual.get("maxLod", current["max_lod"])))
        body = params.get("rigidBody")
        if isinstance(body, dict):
            collider = info.collider
            if collider is None:
                raise SceneError("COMPONENT_MISSING", "RigidBody requires a Collider")
            entity.set_rigid_body(
                enabled=bool(body.get("enabled", True)),
                dynamic=bool(body.get("dynamic", False)),
                gravity=bool(body.get("gravity", True)),
                mass=float(body.get("mass", 1.0)),
                friction=body.get("friction", collider["friction"]),
                restitution=float(body.get("restitution", collider["restitution"])))
        if "ifpsSphereCount" in params:
            entity.ifps_sphere_count = int(params["ifpsSphereCount"])
        camera = params.get("camera")
        if isinstance(camera, dict):
            if "camera" not in info.flags:
                raise SceneError("COMPONENT_MISSING", "Entity is not a Camera")
            current = info.camera
            sensor = info.sensor
            assert current is not None and sensor is not None
            Camera(self.world, entity_id).configure(
                frame_id=str(camera.get("frameId", sensor["frame_id"])),
                update_rate_hz=float(camera.get("updateRateHz", sensor["update_rate_hz"])),
                width=int(camera.get("width", current["width"])),
                height=int(camera.get("height", current["height"])),
                vertical_fov_radians=math.radians(float(camera.get(
                    "verticalFovDegrees", math.degrees(current["vertical_fov_radians"])))),
                near_plane=float(camera.get("nearPlane", current["near_plane"])),
                far_plane=float(camera.get("farPlane", current["far_plane"])),
                output=str(camera.get("output", current["output"])))
        light = params.get("light")
        if isinstance(light, dict):
            current = info.light
            if current is None: raise SceneError("COMPONENT_MISSING", "Entity is not a Light")
            light_type = str(light.get("type", current["type"]))
            inner_cone = float(light.get("innerConeRadians", current["inner_cone_radians"]))
            outer_cone = float(light.get("outerConeRadians", current["outer_cone_radians"]))
            self._validate_spot_cone(light_type, inner_cone, outer_cone)
            entity.set_light(type=light_type, color=light.get("color", current["color"]),
                             intensity=float(light.get("intensity", current["intensity"])), range=float(light.get("range", current["range"])),
                             decay=float(light.get("decay", current["decay"])),
                             inner_cone_radians=inner_cone,
                             outer_cone_radians=outer_cone,
                             angular_diameter_radians=float(light.get("angularDiameterRadians", current["angular_diameter_radians"])),
                             enabled=bool(light.get("enabled", current["enabled"])))
        self._capture_edit_state()
        return {"entity": entity_id}

    def _set_parent(self, params: dict[str, Any]) -> dict[str, Any]:
        entity_id = int(params.get("entity", 0))
        parent_id = int(params.get("parent", 0))
        info = self._native_info().get(entity_id)
        if info is None:
            raise SceneError("ENTITY_NOT_FOUND", "Entity was not found")
        if "joint" in info.flags:
            raise SceneError("ROBOT_LINK_LOCKED", "Robot Link cannot be detached from its articulation")
        self._entity(entity_id).set_parent(self.world.entity(parent_id) if parent_id else None,
                                           preserve_world=True)
        self._capture_edit_state()
        return {"entity": entity_id, "parent": parent_id or None}

    def _delete(self, params: dict[str, Any]) -> dict[str, Any]:
        entity_id = int(params.get("entity", 0))
        info = self._native_info().get(entity_id)
        if info is None:
            raise SceneError("ENTITY_NOT_FOUND", "Entity was not found")
        if "joint" in info.flags:
            raise SceneError("ROBOT_LINK_LOCKED", "Robot Link cannot be deleted independently")
        before = set(self._native_info())
        self._entity(entity_id).destroy()
        deleted = sorted(before - set(self._native_info()))
        self._capture_edit_state()
        return {"deleted": deleted}

    def _robot_target(self, params: dict[str, Any]) -> dict[str, Any]:
        robot_id = int(params.get("robot", 0))
        targets = [float(value) for value in params.get("targets", [])]
        robot = Robot(self.world, robot_id)
        if len(targets) != robot.joint_count:
            raise SceneError("INVALID_ARGUMENT", "Joint target count does not match the robot")
        robot.command_joint_positions(targets)
        return {"robot": robot_id, "targets": targets}

    def _apply_backends(self, params: dict[str, Any]) -> dict[str, Any]:
        next_config = BackendConfig(
            physics=str(params.get("physics", self.config.physics)).lower(),
            collision=str(params.get("collision", self.config.collision)).lower(),
            renderer=str(params.get("renderer", self.config.renderer)).lower(),
            browser_renderer=str(params.get("browserRenderer", self.config.browser_renderer)).lower(),
            fixed_time_step=float(params.get("fixedTimeStep", self.config.fixed_time_step)),
            real_time_factor=float(params.get("realTimeFactor", self.config.real_time_factor)))
        if next_config.browser_renderer != "three":
            raise SceneError("BACKEND_UNAVAILABLE", "Only the Three.js browser renderer is available")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "web-backend-switch.simstate"
            self.simulator.save_state_file(self.world, state_path)
            old_simulator = self.simulator
            old_world = self.world
            old_config = self.config
            try:
                self.config = next_config
                self._create_native()
                self.simulator.restore_state_file(self.world, state_path)
                self.initial_state = self.simulator.save_state(self.world)
                old_simulator.close()
            except Exception as error:
                self.simulator.close()
                self.simulator = old_simulator
                self.world = old_world
                self.config = old_config
                raise SceneError("BACKEND_CONFIGURATION_FAILED", str(error)) from error
        self._log("info", "Backend configuration applied", settings=params)
        return {"settings": self.snapshot()["settings"]}

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
                "scene.create_primitive": self._create_primitive,
                "scene.create_primitives": self._create_primitives,
                "scene.create_camera": self._create_camera,
                "scene.create_light": self._create_light,
                "scene.load_asset": self._load_asset,
                "scene.update_entity": self._update_entity,
                "scene.set_parent": self._set_parent,
                "scene.delete_entity": self._delete,
                "robot.command_joint_positions": self._robot_target,
                "backend.apply": self._apply_backends,
            }
            record_history = method.startswith("scene.")
            before = self.simulator.save_state(self.world) if record_history else None
            if method in handlers:
                result = handlers[method](params)
            elif method == "editor.undo":
                if not self.undo_stack:
                    raise SceneError("UNDO_EMPTY", "There is no edit to undo")
                self.redo_stack.append(self.simulator.save_state(self.world))
                self.simulator.restore_state(self.world, self.undo_stack.pop())
                self._capture_edit_state(); result = {}
            elif method == "editor.redo":
                if not self.redo_stack:
                    raise SceneError("REDO_EMPTY", "There is no edit to redo")
                self.undo_stack.append(self.simulator.save_state(self.world))
                self.simulator.restore_state(self.world, self.redo_stack.pop())
                self._capture_edit_state(); result = {}
            elif method == "simulation.start":
                self.simulator.start(self.world); self.status = "running"; result = {}
            elif method == "simulation.pause":
                self.simulator.pause(self.world); self.status = "paused"; result = {}
            elif method == "simulation.step":
                count = max(1, int(params.get("count", 1)))
                self.last_physics_ms = self.simulator.step_profiled(self.world, count)
                self.status = "paused"; result = {"count": count}
            elif method == "simulation.reset":
                self.simulator.restore_state(self.world, self.initial_state)
                self.status = "paused"; result = {}
            elif method == "state.save":
                name = str(params.get("name", "default"))
                self.states[name] = self.simulator.save_state(self.world); result = {"name": name}
            elif method == "state.restore":
                name = str(params.get("name", "default"))
                if name not in self.states:
                    raise SceneError("STATE_NOT_FOUND", "Saved state was not found")
                self.simulator.restore_state(self.world, self.states[name]); self.status = "paused"
                result = {"name": name}
            else:
                raise SceneError("METHOD_NOT_FOUND", f"Unknown command: {method}")
            if before is not None:
                self.undo_stack.append(before)
                del self.undo_stack[:-100]
                self.redo_stack.clear()
            self.revision += 1
            self._log("info", method, revision=self.revision)
            return {**result, "revision": self.revision}

    def tick(self) -> bool:
        with self.lock:
            if self.status != "running":
                return False
            self.last_physics_ms = self.simulator.step_profiled(self.world, 1)
            return True
