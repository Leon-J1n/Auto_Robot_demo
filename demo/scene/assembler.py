"""Assemble an executable scene from an immutable scene asset and OLS refs.

The scene JSON is an authoring document.  This module creates an in-memory
execution document by resolving ``MetadataRef`` entries through OLS and by
locating the corresponding files in the local asset mirror.  It deliberately
never writes the source scene back to disk.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation


class SceneAssemblyError(ValueError):
    """The authoring scene cannot be assembled into an executable scene."""


def _component(obj, name):
    return next((item["properties"] for item in obj.get("components", [])
                 if item.get("component_type") == name), {})


def _component_entry(obj, name):
    return next((item for item in obj.get("components", [])
                 if item.get("component_type") == name), None)


def _flatten_world_transforms(objects):
    """Convert ordinary WebEditor parent chains to world-rooted execution poses."""
    by_id = {str(obj["object_id_in_scene"]): obj for obj in objects}
    cache = {}

    def world_matrix(obj, visiting=()):
        ident = str(obj["object_id_in_scene"])
        if ident in cache:
            return cache[ident]
        if ident in visiting:
            raise SceneAssemblyError("Transform parent cycle in scene")
        transform = _component(obj, "Transform")
        position = np.asarray(transform.get("position", [0, 0, 0]), dtype=float)
        quaternion = np.asarray(transform.get("quaternion_xyzw", [0, 0, 0, 1]), dtype=float)
        scale = np.asarray(transform.get("scale", [1, 1, 1]), dtype=float)
        if position.shape != (3,) or quaternion.shape != (4,) or scale.shape != (3,):
            raise SceneAssemblyError(f"Invalid Transform for object {ident}")
        local = np.eye(4)
        local[:3, :3] = Rotation.from_quat(quaternion).as_matrix() @ np.diag(scale)
        local[:3, 3] = position
        parent = transform.get("parent")
        if parent in (None, ""):
            result = local
        else:
            parent_obj = by_id.get(str(parent))
            if parent_obj is None:
                raise SceneAssemblyError(f"Transform parent {parent} is missing")
            result = world_matrix(parent_obj, visiting + (ident,)) @ local
        cache[ident] = result
        return result

    for obj in objects:
        if _component_entry(obj, "RobotAttachment") is not None:
            continue
        transform = _component(obj, "Transform")
        if transform.get("parent") in (None, ""):
            # Authoring quaternions may carry a common scale. Child transforms
            # already normalize through Rotation.from_quat; apply the same
            # convention to roots in this runtime copy, never to source assets.
            quaternion = np.asarray(transform.get("quaternion_xyzw", [0, 0, 0, 1]), dtype=float)
            if (quaternion.shape != (4,) or not np.isfinite(quaternion).all()
                    or np.linalg.norm(quaternion) < 1e-12):
                raise SceneAssemblyError(f"Invalid quaternion for object {obj['object_id_in_scene']}")
            transform["quaternion_xyzw"] = Rotation.from_quat(quaternion).as_quat().tolist()
            continue
        matrix = world_matrix(obj)
        rotation_scale = matrix[:3, :3]
        lengths = np.linalg.norm(rotation_scale, axis=0)
        if np.any(lengths <= 1e-12):
            raise SceneAssemblyError(f"Transform scale is zero for object {obj['object_id_in_scene']}")
        transform["position"] = matrix[:3, 3].tolist()
        transform["quaternion_xyzw"] = Rotation.from_matrix(rotation_scale / lengths).as_quat().tolist()
        transform["scale"] = lengths.tolist()
        transform["parent"] = None


def _ensure_component(obj, name, properties):
    entry = _component_entry(obj, name)
    if entry is None:
        obj.setdefault("components", []).append({
            "component_type": name,
            "properties": deepcopy(properties),
        })
        return properties
    entry.setdefault("properties", {}).update(deepcopy(properties))
    return entry["properties"]


def _ols_ref(obj):
    ref = _component(obj, "MetadataRef")
    path = ref.get("path")
    match = re.fullmatch(r"\$LIBRARY_SERVER/(\d+)", str(path or ""))
    object_id = ref.get("object_id")
    if object_id is None and match:
        object_id = int(match.group(1))
    if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id < 0:
        return None
    asset_type = ref.get("type")
    if asset_type not in {"model", "robot"}:
        return None
    if match and int(match.group(1)) != object_id:
        raise SceneAssemblyError(f"Conflicting OLS IDs on {obj.get('object_name_in_scene')}")
    return asset_type, object_id


def _metadata_name(metadata):
    try:
        value = metadata["semantic_properties"]["object_name"]
    except (KeyError, TypeError) as exc:
        raise SceneAssemblyError("OLS metadata is missing semantic_properties.object_name") from exc
    if not isinstance(value, str) or not value:
        raise SceneAssemblyError("OLS object_name must be a non-empty string")
    return value


class SceneAssembler:
    """Resolve authoring scenes into executable scene dictionaries."""

    def __init__(self, asset_root: str | Path, object_library):
        self.asset_root = Path(asset_root).resolve()
        self.object_library = object_library
        self.metadata_cache: dict[tuple[str, int], dict] = {}
        self.file_cache: dict[tuple[str, int], str] = {}

    def _metadata(self, asset_type, object_id):
        key = (asset_type, object_id)
        if key not in self.metadata_cache:
            getter = (self.object_library.get_model if asset_type == "model"
                      else self.object_library.get_robot)
            metadata = getter(object_id)
            if not isinstance(metadata, dict):
                raise SceneAssemblyError(f"OLS returned invalid {asset_type} metadata for {object_id}")
            self.metadata_cache[key] = metadata
        return self.metadata_cache[key]

    def _asset_path(self, asset_type, object_id, metadata):
        key = (asset_type, object_id)
        if key in self.file_cache:
            return self.file_cache[key]
        folder = "models" if asset_type == "model" else "robots"
        directory = self.asset_root / folder / str(object_id)
        if not directory.is_dir():
            raise SceneAssemblyError(f"Local OLS asset directory is missing: {directory}")
        name = _metadata_name(metadata)
        manifest_path = directory / f"{name}_files.json"
        manifest = None
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SceneAssemblyError(f"Invalid OLS file manifest: {manifest_path}") from exc
        entrance = manifest.get("path") if isinstance(manifest, dict) else None
        candidates = []
        if isinstance(entrance, str):
            candidates.append(directory / entrance)
        candidates.extend(sorted(directory.glob("*.urdf")))
        candidates.extend(sorted(directory.glob("*.obj")))
        candidates.extend(sorted(directory.glob("*.stl")))
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise SceneAssemblyError(f"OLS asset has no executable file: {directory}")
        relative_file = source.relative_to(directory).as_posix()
        relative = f"{folder}/{object_id}/{relative_file}"
        self.file_cache[key] = relative
        return relative

    @staticmethod
    def _urdf_links(path: Path):
        root = ET.parse(path).getroot()
        links = {node.get("name") for node in root.findall("link") if node.get("name")}
        children = {node.find("child").get("link") for node in root.findall("joint")
                    if node.find("child") is not None and node.find("child").get("link")}
        roots = links - children
        return root, links, roots

    def _robot_model_ref(self, robot, robot_path, attachments, gripper):
        current = _component(robot, "RobotModelRef")
        root, links, roots = self._urdf_links(self.asset_root / robot_path)
        base_link = current.get("base_link") or next(iter(roots), None)
        if not base_link or base_link not in links:
            raise SceneAssemblyError("OLS robot URDF has no valid root link")
        attachment = next(iter(attachments), None)
        attachment_props = _component(attachment, "RobotAttachment") if attachment else {}
        tip_link = current.get("tip_link") or attachment_props.get("parent_link")
        if not tip_link:
            tip_link = "attachment_site" if "attachment_site" in links else base_link
        if tip_link not in links:
            raise SceneAssemblyError(f"Robot tip link is absent from OLS URDF: {tip_link}")
        default_pairs = []
        if gripper:
            gripper_props = _component(gripper, "GripperModel")
            gripper_path = gripper_props.get("urdf")
            if gripper_path and (self.asset_root / gripper_path).is_file():
                _, tool_links, _ = self._urdf_links(self.asset_root / gripper_path)
                if {"robotiq_2f85_base_mount", "wrist_2_link"}.issubset(links | tool_links):
                    default_pairs = [["robotiq_2f85_base_mount", "wrist_2_link"]]
        result = {
            "urdf": robot_path,
            "base_link": base_link,
            "tip_link": tip_link,
            "allowed_collision_link_pairs": current.get(
                "allowed_collision_link_pairs", default_pairs,
            ),
        }
        # The current OLS UR5e + Robotiq assembly has a calibrated grasp
        # center.  Keep this as an adapter default; a future OLS robot can
        # provide its own calibration metadata and override it in the scene.
        if current.get("grasp_frame"):
            result["grasp_frame"] = deepcopy(current["grasp_frame"])
        elif gripper and _component(gripper, "MetadataRef").get("path") == "$LIBRARY_SERVER/2":
            result["grasp_frame"] = {
                "link": "robotiq_2f85_base",
                "position": [0.0, 0.0, 0.145],
                "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        elif gripper and attachment_props.get("child_link"):
            result["grasp_frame"] = {
                "link": attachment_props["child_link"],
                "position": [0.0, 0.0, 0.0],
                "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        _ensure_component(robot, "RobotModelRef", result)
        return result

    @staticmethod
    def _dynamic_ids(task):
        dynamic, static = set(), set()
        for command in task.get("commands", []) if isinstance(task, dict) else []:
            if not isinstance(command, dict):
                continue
            skill = command.get("skill_name")
            params = command.get("parameters", {})
            target = params.get("target") if isinstance(params, dict) else None
            if skill in {"put", "release"}:
                static.add(str(target))
            elif skill in {"pick", "grasp", "push", "pull"}:
                dynamic.add(str(target))
            if skill == "pour" and isinstance(params, dict):
                dynamic.add(str(params.get("source")))
        return dynamic - static

    @staticmethod
    def _is_static_name(name):
        value = name.lower()
        return any(token in value for token in ("table", "basket", "tray", "floor", "pedestal", "support", "cabinet"))

    def _assemble_model_object(self, obj, metadata, asset_path, dynamic_ids):
        name = obj.get("object_name_in_scene", "")
        authored_asset = _component(obj, "AssetRef").get("asset_path")
        if isinstance(authored_asset, str) and authored_asset:
            # An authoring scene may intentionally select a visual-only or
            # dry variant while OLS remains the source of identity/metadata.
            asset_path = authored_asset
        elif not authored_asset:
            _ensure_component(obj, "AssetRef", {"asset_path": asset_path})
        if not _component(obj, "Collider"):
            _ensure_component(obj, "Collider", {"enabled": True})
        if (str(obj.get("object_id_in_scene")) in dynamic_ids
                and asset_path.lower().endswith(".urdf")):
            urdf_path = self.asset_root / asset_path
            dry_path = urdf_path.with_name(urdf_path.stem + "_dry.obj")
            if dry_path.is_file():
                asset_path = dry_path.relative_to(self.asset_root).as_posix()
                _ensure_component(obj, "AssetRef", {"asset_path": asset_path})
        if asset_path.lower().endswith(".urdf"):
            root, links, _ = self._urdf_links(self.asset_root / asset_path)
            if any(node.get("type") not in {None, "fixed"} for node in root.findall("joint")):
                articulated = [node.get("name") for node in root.findall("link")
                               if node.get("name") and node.get("name") not in {"base", "base_link"}]
                if articulated:
                    _ensure_component(obj, "ArticulatedModel", {"links": articulated})
        if _component(obj, "RigidBody"):
            return
        semantic = _component(obj, "Semantic")
        category = str(semantic.get("category", "")).lower()
        is_dynamic = str(obj.get("object_id_in_scene")) in dynamic_ids
        # Articulated URDFs are driven through their joint adapter; they are
        # not floating rigid bodies and must never receive a free joint.
        if asset_path.lower().endswith(".urdf"):
            is_dynamic = False
        is_dynamic = is_dynamic and not self._is_static_name(name)
        if not is_dynamic and category in {"object", "graspable"}:
            is_dynamic = not self._is_static_name(name)
        mass = metadata.get("physical_properties", {}).get("mass_kg", 0.1)
        try:
            mass = float(mass)
        except (TypeError, ValueError):
            mass = 0.1
        _ensure_component(obj, "RigidBody", {"dynamic": bool(is_dynamic), "mass": max(mass, 1e-6)})

    def _assemble_robot_object(self, obj, metadata, asset_path):
        # RobotDriver and/or RobotAttachment determine whether this robot
        # asset is the arm, a mounted tool, or a visual-only accessory.
        if _component_entry(obj, "RobotAttachment") is not None:
            gripper = _component(obj, "GripperModel")
            _ensure_component(obj, "GripperModel", {
                "urdf": asset_path,
                "type": gripper.get("type") or _metadata_name(metadata),
                "_ols_model_id": int(metadata["semantic_properties"]["object_id"]),
                "_ols_asset_type": "robot",
            })
            return
        if _component_entry(obj, "RobotDriver") is not None or _component_entry(obj, "RobotModelRef") is not None:
            return
        # Cameras and other visual accessories are excluded from the control
        # scene below, but still retain their OLS identity.
        if _component_entry(obj, "Camera") is not None or _component_entry(obj, "Light") is not None:
            return
        if not _component(obj, "Collider"):
            _ensure_component(obj, "AssetRef", {"asset_path": asset_path})
            _ensure_component(obj, "Collider", {"enabled": True})

    def assemble(self, scene, task=None):
        result = deepcopy(scene)
        objects = result.get("objects")
        if not isinstance(objects, list) or not objects:
            raise SceneAssemblyError("Scene must contain a non-empty objects array")
        dynamic_ids = self._dynamic_ids(task or {})
        metadata_by_object = {}
        for obj in objects:
            ref = _ols_ref(obj)
            if ref is None:
                continue
            asset_type, object_id = ref
            metadata = self._metadata(asset_type, object_id)
            metadata_by_object[id(obj)] = metadata
            asset_path = self._asset_path(asset_type, object_id, metadata)
            if asset_type == "model":
                self._assemble_model_object(obj, metadata, asset_path, dynamic_ids)
            else:
                self._assemble_robot_object(obj, metadata, asset_path)

        # Legacy authoring scenes describe the floor and pedestal visually;
        # their control geometry is deterministic and does not need an OLS
        # mesh lookup.
        for obj in objects:
            if _component_entry(obj, "Collider") is not None:
                continue
            name = str(obj.get("object_name_in_scene", "")).lower()
            if name == "floor":
                _ensure_component(obj, "Collider", {
                    "enabled": True, "shape": "box", "size": [6.0, 6.0, 0.12],
                })
            elif "pedestal" in name:
                _ensure_component(obj, "Collider", {
                    "enabled": True, "shape": "cylinder", "size": [0.14, 0.14, 0.498],
                })

        robots = [obj for obj in objects if _component_entry(obj, "RobotModelRef") is not None
                  or (_component_entry(obj, "RobotAttachment") is None
                      and _component(obj, "RobotDriver").get("role") != "passive"
                      and bool(_component(obj, "RobotDriver").get("joint_sequence")))]
        if len(robots) != 1:
            raise SceneAssemblyError(f"Expected one arm object, found {len(robots)}")
        robot = robots[0]
        attachments = [obj for obj in objects if _component_entry(obj, "RobotAttachment") is not None]
        robot_meta = metadata_by_object.get(id(robot))
        if robot_meta is None:
            ref = _ols_ref(robot)
            if ref is None or ref[0] != "robot":
                raise SceneAssemblyError("Robot arm must reference an OLS robot asset")
            robot_meta = self._metadata(*ref)
        robot_path = self._asset_path("robot", _ols_ref(robot)[1], robot_meta)
        gripper = attachments[0] if attachments else None
        self._robot_model_ref(robot, robot_path, attachments, gripper)
        for attachment in attachments:
            mount = _component(attachment, "RobotAttachment")
            if not mount.get("parent_joint"):
                root, _, _ = self._urdf_links(self.asset_root / robot_path)
                for joint in root.findall("joint"):
                    child = joint.find("child")
                    if child is not None and child.get("link") == mount.get("parent_link"):
                        mount["parent_joint"] = joint.get("name")
                        break

        # Authoring scenes may contain cameras/lights for a viewer.  They are
        # retained in the source document but omitted from the physical scene.
        excluded = [str(obj["object_id_in_scene"]) for obj in objects
                    if _component_entry(obj, "Camera") is not None
                    or _component_entry(obj, "Light") is not None]
        result["objects"] = [obj for obj in objects if str(obj["object_id_in_scene"]) not in set(excluded)]
        _flatten_world_transforms(result["objects"])
        settings = result.setdefault("named_targets", {})
        robot_driver = _component(robot, "RobotDriver")
        initial = robot_driver.get("initial_joint_positions_rad")
        if "home" not in settings and isinstance(initial, dict):
            sequence = robot_driver.get("joint_sequence", [])
            settings["home"] = {"request": {"target": {"type": "joint",
                "joint_positions": [initial[name] for name in sequence]}}}
        result.setdefault("_assembly", {})["source"] = "ols"
        result["_assembly"]["excluded_nonphysical_objects"] = excluded
        result["_assembly"]["resolved_assets"] = len(self.metadata_cache)
        return result

