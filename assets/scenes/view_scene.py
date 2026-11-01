"""Static JSON scene viewer. All model files are resolved inside final_models.

Dependencies: numpy, mujoco. No scene_000.xml or external asset directory needed.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import mujoco
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def fmt(values):
    return " ".join(f"{float(v):.12g}" for v in values)


def quat_matrix(q):
    q = np.asarray(q, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all() or np.linalg.norm(q) < 1e-12:
        raise ValueError("quaternion_xyzw must contain four finite, nonzero values")
    x, y, z, w = q / np.linalg.norm(q)
    return np.array([
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ])


def matrix_quat(matrix):
    q = np.empty(4)
    mujoco.mju_mat2Quat(q, np.ascontiguousarray(matrix).ravel())
    return q


def pose(position=(0, 0, 0), rotation=None):
    result = np.eye(4)
    result[:3, 3] = position
    if rotation is not None:
        result[:3, :3] = rotation
    return result


def urdf_origin(element):
    if element is None:
        return np.eye(4)
    xyz = [float(v) for v in element.get("xyz", "0 0 0").split()]
    r, p, y = [float(v) for v in element.get("rpy", "0 0 0").split()]
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return pose(xyz, np.array([
        [cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
        [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr],
        [-sp, cp*sr, cp*cr],
    ]))


def joint_motion(joint, value):
    element = joint.find("axis")
    axis = np.array([float(v) for v in (element.get("xyz", "1 0 0") if element is not None else "1 0 0").split()])
    axis /= np.linalg.norm(axis)
    if joint.get("type") == "prismatic":
        return pose(axis * value)
    x, y, z = axis
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return pose(rotation=np.eye(3) + math.sin(value)*cross + (1-math.cos(value))*(cross @ cross))


def urdf_link_poses(path, joint_positions):
    robot = ET.parse(path).getroot()
    links = {link.get("name"): link for link in robot.findall("link")}
    joints = robot.findall("joint")
    by_name = {j.get("name"): j for j in joints}
    child_names = {j.find("child").get("link") for j in joints}
    roots = set(links) - child_names
    if len(roots) != 1:
        raise ValueError(f"Expected one URDF root: {path}")
    values, active = {}, set()

    def value(name):
        if name in values:
            return values[name]
        if name in active:
            raise ValueError(f"URDF mimic cycle: {path}:{name}")
        active.add(name)
        mimic = by_name[name].find("mimic")
        result = float(joint_positions.get(name, 0)) if mimic is None else value(mimic.get("joint"))*float(mimic.get("multiplier", "1"))+float(mimic.get("offset", "0"))
        active.remove(name)
        values[name] = result
        return result

    transforms = {}
    def visit(name, transform):
        transforms[name] = transform
        for joint in joints:
            if joint.find("parent").get("link") != name:
                continue
            local = urdf_origin(joint.find("origin"))
            if joint.get("type") != "fixed":
                local = local @ joint_motion(joint, value(joint.get("name")))
            visit(joint.find("child").get("link"), transform @ local)
    visit(next(iter(roots)), np.eye(4))
    return transforms


def urdf_visuals(path, joint_positions):
    robot = ET.parse(path).getroot()
    transforms = urdf_link_poses(path, joint_positions)

    for link in robot.findall("link"):
        transform = transforms[link.get("name")]
        for visual in link.findall("visual"):
            mesh = visual.find("geometry/mesh")
            rgba = visual.find("material/color")
            color = [float(v) for v in rgba.get("rgba").split()] if rgba is not None else None
            if mesh is None:
                shape = next(iter(visual.find("geometry")))
                if shape.tag not in ("box", "sphere", "cylinder"):
                    raise ValueError(f"Unsupported URDF visual: {shape.tag}")
                yield dict(type=shape.tag, **shape.attrib), transform @ urdf_origin(visual.find("origin")), [1, 1, 1], color
                continue
            yield (path.parent / mesh.get("filename")).resolve(), transform @ urdf_origin(visual.find("origin")), [float(v) for v in mesh.get("scale", "1 1 1").split()], color


def material_info(mesh, material_name=None):
    texture, color = None, [0.7, 0.7, 0.7, 1.0]
    for line in mesh.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("mtllib "):
            mtl = mesh.parent / line.split(maxsplit=1)[1]
            if mtl.exists():
                selected = False
                for entry in mtl.read_text(encoding="utf-8-sig").splitlines():
                    if entry.startswith("newmtl "):
                        name = entry.split(maxsplit=1)[1].strip()
                        selected = material_name is None or name == material_name
                    if not selected:
                        continue
                    if entry.lower().startswith("map_kd "):
                        texture = (mtl.parent / entry.split(maxsplit=1)[1]).resolve()
                    elif entry.startswith("Kd "):
                        color = [float(v) for v in entry.split()[1:4]] + [1.0]
    return texture, color


def material_parts(mesh):
    """Preserve each OBJ submaterial rather than using the last MTL color."""
    lines = mesh.read_text(encoding="utf-8-sig").splitlines()
    names = list(dict.fromkeys(l.split(maxsplit=1)[1] for l in lines if l.startswith("usemtl ")))
    if len(names) <= 1:
        yield mesh, *material_info(mesh, names[0] if names else None)
        return
    import hashlib
    cache = ROOT / "test" / "preview_cache"
    cache.mkdir(exist_ok=True, parents=True)
    declarations = [l for l in lines if l.startswith(("v ", "vt ", "vn "))]
    for index, name in enumerate(names):
        active = None
        faces = []
        for l in lines:
            if l.startswith("usemtl "):
                active = l.split(maxsplit=1)[1]
            elif l.startswith("f ") and active == name:
                faces.append(l)
        if faces:
            target = cache / (hashlib.sha256(str(mesh).encode()).hexdigest()[:16]+f"_{index}.obj")
            target.write_text("\n".join(declarations+faces)+"\n", encoding="utf-8")
            yield target, *material_info(mesh, name)


def resolve_metadata(scene_path, reference, asset_type=None):
    # The platform uses OLS references.  The local preview resolves them through
    # the checked-in robot/model indices, so scene JSON remains portable.
    if reference.startswith("$LIBRARY_SERVER/"):
        asset_id = reference.rsplit("/", 1)[-1]
        if asset_type == "robot":
            index = read_json(ROOT / "robots" / "robot_index.json")
            name = index["by_id"].get(str(asset_id))
            base = ROOT / "robots" / str(asset_id)
        elif asset_type == "model":
            index = read_json(ROOT / "models" / "model_index.json")
            name = index["by_id"].get(str(asset_id))
            base = ROOT / "models" / str(asset_id)
        else:
            raise ValueError(f"OLS reference needs MetadataRef.type: {reference}")
        if not name:
            raise ValueError(f"Unknown local OLS asset: {reference} ({asset_type})")
        metadata = base / f"{name}.json"
    else:
        metadata = (scene_path.parent / reference).resolve()
    if not metadata.is_relative_to(ROOT) or not metadata.is_file():
        raise ValueError(f"Missing asset metadata in final_models: {metadata}")
    manifest = metadata.with_name(metadata.stem + "_files.json")
    return (metadata.parent / read_json(manifest)["path"]).resolve()


def build(scene_path, settings_path):
    scene = read_json(scene_path)
    settings = read_json(settings_path) if settings_path.exists() else {}
    objects = {o["object_id_in_scene"]: o for o in scene["objects"]}
    if len(objects) != len(scene["objects"]):
        raise ValueError("Duplicate object IDs")
    components = {i: {c["component_type"]: c["properties"] for c in o["components"]} for i, o in objects.items()}
    poses, visiting = {}, set()
    paths, link_poses = {}, {}
    for i, cs in components.items():
        if "MetadataRef" in cs:
            ref = cs["MetadataRef"]
            paths[i] = resolve_metadata(scene_path, ref["path"], ref.get("type"))
            joints = cs.get("RobotDriver", {}).get("initial_joint_positions_rad", {})
            if paths[i].suffix == ".urdf":
                link_poses[i] = urdf_link_poses(paths[i], joints)

    def world_pose(i):
        if i in poses:
            return poses[i]
        if i in visiting:
            raise ValueError("Transform.parent cycle")
        visiting.add(i)
        transform = components[i]["Transform"]
        attachment = components[i].get("RobotAttachment")
        if attachment:
            at = attachment["transform"]
            local = pose(at["position"], quat_matrix(at["quaternion_xyzw"]))
            parent = attachment["parent_object_id"]
            if transform.get("parent") != parent:
                raise ValueError(f"Attachment parent mismatch: object {i}")
            if "position" in transform or "quaternion_xyzw" in transform:
                raise ValueError(f"Conflicting attachment Transform: object {i}")
            if at.get("reference_frame") != "parent_link":
                raise ValueError(f"Unsupported attachment frame: object {i}")
            parent_link = attachment["parent_link"]
            local = link_poses[parent][parent_link] @ local
            child_link = attachment.get("child_link")
            if child_link:
                local = local @ np.linalg.inv(link_poses[i][child_link])
            poses[i] = world_pose(parent) @ local
        else:
            local = pose(transform.get("position", [0, 0, 0]), quat_matrix(transform.get("quaternion_xyzw", [0, 0, 0, 1])))
            parent = transform.get("parent")
            poses[i] = local if parent is None else world_pose(parent) @ local
        visiting.remove(i)
        return poses[i]

    xml = ET.Element("mujoco", model=scene["scene_name"])
    ET.SubElement(xml, "compiler", angle="radian")
    visual = ET.SubElement(xml, "visual")
    ET.SubElement(visual, "global", offwidth="1280", offheight="1280")
    ambient = np.zeros(3)
    for cs in components.values():
        light = cs.get("Light", {})
        if light.get("type") == "Ambient":
            color = light.get("color", "#FFFFFF")
            ambient += np.array([int(color[k:k+2], 16)/255 for k in (1, 3, 5)]) * float(light["intensity"])
    ET.SubElement(visual, "headlight", ambient=fmt(np.clip(ambient, 0, 1)), diffuse="0 0 0", specular="0 0 0")
    asset = ET.SubElement(xml, "asset")
    # Keep exported scene photos consistent with the reference view: a white
    # studio-style background instead of MuJoCo's default black sky.
    ET.SubElement(
        asset,
        "texture",
        name="scene_sky",
        type="skybox",
        builtin="gradient",
        rgb1="1 1 1",
        rgb2="1 1 1",
        width="512",
        height="512",
    )
    world = ET.SubElement(xml, "worldbody")
    ET.SubElement(world, "geom", type="plane", size="3 3 0.1", rgba="0.8 0.82 0.86 1")
    bounds = []
    for i, obj in objects.items():
        cs, transform = components[i], world_pose(i)
        light = cs.get("Light")
        if light and light["type"] in ("Directional", "Point"):
            color = light.get("color", "#FFFFFF")
            rgb = np.array([int(color[k:k+2], 16)/255 for k in (1,3,5)])*float(light["intensity"])
            ET.SubElement(world, "light", name=f"light_{i}", pos=fmt(transform[:3,3]),
                          dir=fmt(transform[:3,:3] @ np.array([0,0,-1])),
                          directional="true" if light["type"] == "Directional" else "false",
                          diffuse=fmt(rgb), ambient="0 0 0", specular="0 0 0")
        if "MetadataRef" in cs:
            ref = cs["MetadataRef"]
            path = resolve_metadata(scene_path, ref["path"], ref.get("type"))
            driver = components.get(i, {}).get("RobotDriver", {})
            joints = driver.get("initial_joint_positions_rad", {})
            if not joints:
                joints = settings.get("joint_positions_rad", {}).get(str(i), {})
            specs = urdf_visuals(path, joints) if path.suffix == ".urdf" else [(path, np.eye(4), [1, 1, 1], None)]
            scale = np.asarray(cs["Transform"]["scale"], dtype=float)
            for part, (mesh, local, mesh_scale, urdf_color) in enumerate(specs):
                if isinstance(mesh, dict):
                    shape = mesh["type"]
                    if shape == "box":
                        shape = "box"
                        size = np.array([float(v) for v in mesh["size"].split()])*scale/2
                    elif shape == "sphere":
                        size = [float(mesh["radius"])*scale[0]]
                    else:
                        size = [float(mesh["radius"])*scale[0], float(mesh["length"])*scale[2]/2]
                    placed = transform @ local
                    ET.SubElement(world, "geom", name=f"o{i}_p{part}", type=shape,
                                  size=fmt(size), rgba=fmt(urdf_color or [0.6,0.6,0.6,1]),
                                  pos=fmt(placed[:3,3]), quat=fmt(matrix_quat(placed[:3,:3])), contype="0", conaffinity="0")
                    continue
                if not mesh.is_relative_to(ROOT) or not mesh.is_file():
                    raise ValueError(f"Missing mesh inside final_models: {mesh}")
                local = local.copy()
                local[:3, 3] *= scale
                placed = transform @ local
                for subpart, (part_mesh, texture, rgba) in enumerate(material_parts(mesh)):
                    name = f"o{i}_p{part}_m{subpart}"
                    ET.SubElement(asset, "mesh", name=name, file=str(part_mesh), scale=fmt(scale*np.array(mesh_scale)))
                    attributes = dict(name=name, rgba=fmt(rgba), specular="0.1")
                    if texture is not None:
                        ET.SubElement(asset, "texture", name=name, type="2d", file=str(texture))
                        attributes["texture"] = name
                    ET.SubElement(asset, "material", **attributes)
                    ET.SubElement(world, "geom", name=name, type="mesh", mesh=name, material=name, pos=fmt(placed[:3, 3]), quat=fmt(matrix_quat(placed[:3, :3])), contype="0", conaffinity="0")
                bounds.append(placed[:3, 3])
        if "Camera" in cs:
            camera = cs["Camera"]
            # D435i mesh optical axis is +Z; MuJoCo camera looks along -Z.
            optical = transform[:3, :3] @ np.diag([1, -1, -1])
            ET.SubElement(world, "camera", name=f"camera_{i}", pos=fmt(transform[:3, 3]), quat=fmt(matrix_quat(optical)), fovy=str(camera["fov"]))

    model = mujoco.MjModel.from_xml_string(ET.tostring(xml, encoding="unicode"))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return model, data, settings


def write_png(path, pixels):
    h, w, _ = pixels.shape
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data)&0xffffffff)
    raw = b"".join(b"\0" + row.tobytes() for row in pixels)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w,h,8,2,0,0,0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    parser = argparse.ArgumentParser(description="View a final_models JSON scene")
    parser.add_argument("scene_id", type=int, help="Scene ID from scene_index.json")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=1280)
    args = parser.parse_args()
    scene_dir = HERE / str(args.scene_id)
    json_files = [p for p in scene_dir.glob("*.json")
                  if p.name != "scene_index.json" and not p.stem.endswith(("_file", "_files"))]
    if len(json_files) != 1:
        raise SystemExit(f"Scene {args.scene_id} must contain exactly one scene JSON: {scene_dir}")
    scene_path = json_files[0].resolve()
    settings_path = args.settings
    if settings_path is None:
        settings = {}
    else:
        settings = read_json(settings_path.resolve())
    model, data, _ = build(scene_path, settings_path or Path("__missing_view_settings__.json"))
    camera = mujoco.MjvCamera()
    camera.lookat[:] = settings.get("view", {}).get("lookat", [0.35, 0, 1.0])
    camera.distance = settings.get("view", {}).get("distance", 3.8)
    # Look from the robot side for the push/pull scene so appliance fronts
    # remain visible; this is a preview setting, not a platform JSON field.
    camera.azimuth = settings.get("view", {}).get("azimuth", 45 if args.scene_id == 7 else 225)
    camera.elevation = settings.get("view", {}).get("elevation", -20)
    if args.screenshot:
        with mujoco.Renderer(model, width=args.width, height=args.height) as renderer:
            renderer.update_scene(data, camera=camera)
            write_png(args.screenshot.resolve(), renderer.render())
        print(f"Saved {args.screenshot}")
        return
    from mujoco import viewer as mjviewer
    with mjviewer.launch_passive(model, data) as viewer:
        viewer.cam.lookat[:] = camera.lookat
        viewer.cam.distance = camera.distance
        viewer.cam.azimuth = camera.azimuth
        viewer.cam.elevation = camera.elevation
        print("Static initialization preview. Edit JSON, then restart to reload.")
        while viewer.is_running():
            viewer.sync()
            time.sleep(1/60)


if __name__ == "__main__":
    main()
