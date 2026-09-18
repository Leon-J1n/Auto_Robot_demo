"""Build and view scene_004 with randomly placed grasp objects.

The grasp-object catalogue is discovered at runtime from
robotsim/assets/grasp_objects.  Each valid child directory must contain an OBJ
mesh.  A material/PNG texture and convex-decomposition STL files are used when
present; otherwise a neutral material and a conservative box collider are used.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORLD_MODEL_DIR = PROJECT_ROOT / "world_model"
ROBOTSIM_DIR = WORLD_MODEL_DIR / "robotsim"
SOURCE_SCENE = ROBOTSIM_DIR / "scene_000.xml"
OUTPUT_SCENE = ROBOTSIM_DIR / "scene_999.xml"
OBJECTS_DIR = ROBOTSIM_DIR / "assets" / "grasp_objects"

# These values come from work_table_top in scene_000.xml.  They are checked
# against that geom when the output is generated, so scene changes fail loudly.
TABLE_TOP_Z = 0.600
TABLE_X_MIN, TABLE_X_MAX = 0.150, 0.900
TABLE_Y_MIN, TABLE_Y_MAX = -0.750, 0.750
EDGE_MARGIN = 0.025


@dataclass(frozen=True)
class ObjectAsset:
    name: str
    directory: Path
    obj: Path
    texture: Path | None
    material_color: tuple[float, float, float] | None
    collision_meshes: tuple[Path, ...]
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]

    @property
    def radius_xy(self) -> float:
        # A yaw-independent footprint.  It is conservative, which is useful for
        # guaranteeing that arbitrary meshes stay on the table.
        center_x = 0.5 * (self.bounds_min[0] + self.bounds_max[0])
        center_y = 0.5 * (self.bounds_min[1] + self.bounds_max[1])
        return max(math.hypot(x - center_x, y - center_y) for x, y in _obj_xy_vertices(self.obj))

    @property
    def half_height(self) -> float:
        return 0.5 * (self.bounds_max[2] - self.bounds_min[2])

    @property
    def center_offset(self) -> tuple[float, float, float]:
        return tuple(-0.5 * (lo + hi) for lo, hi in zip(self.bounds_min, self.bounds_max))


@dataclass(frozen=True)
class Placement:
    asset: ObjectAsset
    x: float
    y: float
    z: float
    yaw: float


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return cleaned or "object"


def _relative_posix(path: Path, start: Path) -> str:
    return Path(os.path.relpath(path.resolve(), start.resolve())).as_posix()


def _obj_vertices(path: Path):
    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        for line in stream:
            if not line.startswith("v "):
                continue
            fields = line.split()
            if len(fields) >= 4:
                yield float(fields[1]), float(fields[2]), float(fields[3])


def _obj_xy_vertices(path: Path):
    for x, y, _ in _obj_vertices(path):
        yield x, y


def _obj_bounds(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    vertices = list(_obj_vertices(path))
    if not vertices:
        raise ValueError(f"OBJ has no vertices: {path}")
    low = tuple(min(v[axis] for v in vertices) for axis in range(3))
    high = tuple(max(v[axis] for v in vertices) for axis in range(3))
    return low, high


def _texture_from_mtl(directory: Path) -> Path | None:
    for mtl in sorted(directory.glob("*.mtl")):
        for line in mtl.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("map_kd "):
                candidate = directory / line.split(maxsplit=1)[1].strip()
                if candidate.is_file():
                    return candidate
    pngs = sorted(directory.glob("*.png"))
    return pngs[0] if pngs else None


def _material_color_from_mtl(directory: Path) -> tuple[float, float, float] | None:
    """Read diffuse Kd from an MTL when the model has no texture image."""
    for mtl in sorted(directory.glob("*.mtl")):
        for line in mtl.read_text(encoding="utf-8", errors="ignore").splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0].lower() == "kd":
                try:
                    return tuple(max(0.0, min(1.0, float(value))) for value in fields[1:4])
                except ValueError:
                    pass
    return None


def discover_objects() -> list[ObjectAsset]:
    """Return every usable grasp-object directory, without a hard-coded list."""
    if not OBJECTS_DIR.is_dir():
        raise FileNotFoundError(f"Grasp-object directory not found: {OBJECTS_DIR}")
    objects: list[ObjectAsset] = []
    catalogue_dirs = {path.name: path for path in OBJECTS_DIR.iterdir() if path.is_dir()}
    # Keep compatibility with the repository's legacy models/ catalogue.  A
    # grasp_objects copy always wins when both locations contain a mesh.
    legacy_dir = WORLD_MODEL_DIR.parent / "models"
    if legacy_dir.is_dir():
        for path in legacy_dir.iterdir():
            if path.is_dir() and (path.name not in catalogue_dirs or not list(catalogue_dirs[path.name].glob("*.obj"))):
                catalogue_dirs[path.name] = path
    for directory in sorted(catalogue_dirs.values()):
        objs = sorted(directory.glob("*.obj"))
        if not objs:
            print(f"Skipping {directory.name}: no OBJ file")
            continue
        preferred = directory / "textured.obj"
        obj = preferred if preferred.is_file() else objs[0]
        low, high = _obj_bounds(obj)
        objects.append(
            ObjectAsset(
                name=_safe_name(directory.name),
                directory=directory,
                obj=obj,
                texture=_texture_from_mtl(directory),
                material_color=_material_color_from_mtl(directory),
                collision_meshes=tuple(sorted(directory.glob("*_coacd_*.stl"))),
                bounds_min=low,
                bounds_max=high,
            )
        )
    if not objects:
        raise RuntimeError(f"No usable OBJ models found below {OBJECTS_DIR}")
    return objects


def place_objects(objects: list[ObjectAsset], mode: str, rng: random.Random) -> list[Placement]:
    """Sample collision-free initial poses entirely inside the tabletop."""
    gap = 0.050 if mode == "separated" else 0.002
    placements: list[Placement] = []
    # Large objects go first; this makes rejection sampling much more reliable.
    for asset in sorted(objects, key=lambda item: item.radius_xy, reverse=True):
        radius = asset.radius_xy
        xmin = TABLE_X_MIN + EDGE_MARGIN + radius
        xmax = TABLE_X_MAX - EDGE_MARGIN - radius
        ymin = TABLE_Y_MIN + EDGE_MARGIN + radius
        ymax = TABLE_Y_MAX - EDGE_MARGIN - radius
        if xmin > xmax or ymin > ymax:
            raise RuntimeError(f"{asset.name} is too large for the configured tabletop")
        for _ in range(5000):
            x, y = rng.uniform(xmin, xmax), rng.uniform(ymin, ymax)
            if all(math.hypot(x - p.x, y - p.y) >= radius + p.asset.radius_xy + gap for p in placements):
                # Start directly above the tabletop. Dropping spherical objects
                # from a random height made their body frames roll to arbitrary
                # orientations before grasp-pose conversion.
                z = TABLE_TOP_Z + asset.half_height + 0.002
                # Keep each asset's authored/object-frame orientation fixed.
                # Only its tabletop position is randomized, so calibrated grasp
                # poses remain valid from one scene reset to the next.
                placements.append(Placement(asset, x, y, z, 0.0))
                break
        else:
            raise RuntimeError(
                f"Could not fit all {len(objects)} objects in {mode!r} mode. "
                "Remove objects, reduce their scale, or use contact mode."
            )
    return placements


def _fmt(values) -> str:
    return " ".join(f"{value:.9g}" for value in values)


def _quat_from_yaw(yaw: float) -> tuple[float, float, float, float]:
    return math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)


def _asset_xml(placements: list[Placement]) -> str:
    lines = ["    <!-- Random grasp-object assets (generated; do not edit manually). -->"]
    for index, placement in enumerate(placements):
        asset = placement.asset
        prefix = f"grasp_{index}_{asset.name}"
        mesh_root = ROBOTSIM_DIR / "assets" / "ur5e"
        obj_file = _relative_posix(asset.obj, mesh_root)
        lines.append(f'    <mesh name="{prefix}_visual_mesh" file="{obj_file}" />')
        if asset.texture:
            texture_file = _relative_posix(asset.texture, ROBOTSIM_DIR)
            lines.append(f'    <texture name="{prefix}_texture" type="2d" file="{texture_file}" />')
            lines.append(f'    <material name="{prefix}_material" texture="{prefix}_texture" specular="0.2" shininess="0.15" />')
        else:
            color = asset.material_color or (0.7, 0.7, 0.7)
            lines.append(f'    <material name="{prefix}_material" rgba="{_fmt((*color, 1.0))}" />')
        for collider_index, collider in enumerate(asset.collision_meshes):
            collider_file = _relative_posix(collider, mesh_root)
            lines.append(f'    <mesh name="{prefix}_collision_{collider_index}" file="{collider_file}" />')
    return "\n".join(lines)


def _body_xml(placements: list[Placement]) -> str:
    lines = ["    <!-- Random grasp-object bodies (generated; do not edit manually). -->"]
    for index, placement in enumerate(placements):
        asset = placement.asset
        prefix = f"grasp_{index}_{asset.name}"
        quat = _quat_from_yaw(placement.yaw)
        lines.append(
            f'    <body name="{prefix}" pos="{_fmt((placement.x, placement.y, placement.z))}" '
            f'quat="{_fmt(quat)}">'
        )
        # Keep the authored object frame stable; the grasp JSON is expressed in
        # this frame rather than in a compiler-generated inertia frame.
        # A normal free joint lets released payloads fall and settle naturally.
        # Initial orientation is fixed by the body's quaternion above.
        lines.append(f'      <freejoint name="{prefix}_freejoint" align="false" />')
        lines.append(
            f'      <site name="{prefix}_object_frame" pos="0 0 0" '
            'quat="1 0 0 0" size="0.008" group="4" rgba="0.2 0.8 0.2 0.7" />'
        )
        # Visible object-frame triad: X=red, Y=green, Z=blue.  The triad is
        # attached to the same authored frame used by grasp_pose.json.
        # Group 0 is enabled by the default MuJoCo viewer configuration.
        offset = _fmt(asset.center_offset)
        lines.append(
            f'      <geom name="{prefix}_visual" type="mesh" mesh="{prefix}_visual_mesh" '
            f'material="{prefix}_material" pos="{offset}" contype="0" conaffinity="0" group="2" />'
        )
        if asset.collision_meshes:
            # A small mass per convex part keeps total masses in a practical range.
            part_mass = max(0.01, 0.12 / len(asset.collision_meshes))
            for collider_index, _ in enumerate(asset.collision_meshes):
                lines.append(
                    f'      <geom name="{prefix}_collision_{collider_index}" type="mesh" '
                    f'mesh="{prefix}_collision_{collider_index}" pos="{offset}" mass="{part_mass:.6g}" '
                    'rgba="0 0 0 0" contype="1" conaffinity="1" condim="4" '
                    'friction="0.9 0.02 0.002" />'
                )
        else:
            sizes = tuple(0.5 * (hi - lo) for lo, hi in zip(asset.bounds_min, asset.bounds_max))
            lines.append(
                f'      <geom name="{prefix}_collision_box" type="box" size="{_fmt(sizes)}" '
                f'pos="{offset}" mass="0.12" rgba="0 0 0 0" contype="1" conaffinity="1" '
                'condim="4" friction="0.9 0.02 0.002" />'
            )
        lines.append("    </body>")
    return "\n".join(lines)


def _validate_table(source: str) -> None:
    root = ET.fromstring(source)
    geom = root.find(".//geom[@name='work_table_top']")
    if geom is None:
        raise RuntimeError("scene_000.xml has no work_table_top geom")
    position = [float(value) for value in geom.get("pos", "").split()]
    size = [float(value) for value in geom.get("size", "").split()]
    if len(position) != 3 or len(size) != 3 or not math.isclose(position[2] + size[2], TABLE_TOP_Z):
        raise RuntimeError("The table geometry changed; update the scene_004 tabletop constants")


def generate_scene(placements: list[Placement]) -> Path:
    source = SOURCE_SCENE.read_text(encoding="utf-8")
    _validate_table(source)
    source = source.replace('<mujoco model="scene_000">', '<mujoco model="scene_999">', 1)
    source = source.replace("  </asset>", _asset_xml(placements) + "\n  </asset>", 1)
    source = source.replace("  </worldbody>", _body_xml(placements) + "\n  </worldbody>", 1)

    # Extend the authored robot home key with every free-joint pose.  This keeps
    # scene_view.py compatible with the generated scene instead of resetting the
    # objects to the world origin.
    match = re.search(r'(<key\s+name="home"\s+qpos=")([^"]*)("\s*/>)', source)
    if not match:
        raise RuntimeError("scene_000.xml has no home keyframe")
    extra_qpos = []
    for p in placements:
        extra_qpos.extend((p.x, p.y, p.z, *_quat_from_yaw(p.yaw)))
    replacement = match.group(1) + match.group(2).strip() + " " + _fmt(extra_qpos) + match.group(3)
    source = source[: match.start()] + replacement + source[match.end() :]

    # Parse once before writing so a programming error cannot leave broken XML.
    ET.fromstring(source)
    OUTPUT_SCENE.write_text(source, encoding="utf-8")
    return OUTPUT_SCENE


def _object_body_ids(model, placements: list[Placement], mujoco_module) -> set[int]:
    return {
        mujoco_module.mj_name2id(model, mujoco_module.mjtObj.mjOBJ_BODY, f"grasp_{i}_{p.asset.name}")
        for i, p in enumerate(placements)
    }


def _root_object_body(model, body_id: int, object_ids: set[int]) -> int | None:
    while body_id > 0:
        if body_id in object_ids:
            return body_id
        body_id = int(model.body_parentid[body_id])
    return None


def settle_and_check(model, data, placements: list[Placement], mode: str, mujoco_module) -> bool:
    """Settle objects, then verify tabletop containment and separated-mode contacts."""
    # Keep all position-controlled robot joints at the authored home values.
    for actuator_id in range(model.nu):
        joint_id = int(model.actuator_trnid[actuator_id, 0])
        if joint_id >= 0:
            data.ctrl[actuator_id] = data.qpos[model.jnt_qposadr[joint_id]]
    for _ in range(max(1, int(2.0 / model.opt.timestep))):
        mujoco_module.mj_step(model, data)

    object_ids = _object_body_ids(model, placements, mujoco_module)
    for body_id in object_ids:
        x, y, z = data.xpos[body_id]
        if not (TABLE_X_MIN <= x <= TABLE_X_MAX and TABLE_Y_MIN <= y <= TABLE_Y_MAX and z >= TABLE_TOP_Z - 0.01):
            return False
    if mode == "separated":
        for index in range(data.ncon):
            contact = data.contact[index]
            body_a = _root_object_body(model, int(model.geom_bodyid[contact.geom1]), object_ids)
            body_b = _root_object_body(model, int(model.geom_bodyid[contact.geom2]), object_ids)
            if body_a is not None and body_b is not None and body_a != body_b:
                return False
    return True


def load_and_settle(placements: list[Placement], mode: str, seed: int):
    try:
        import mujoco
    except ImportError as exc:
        raise SystemExit(
            "MuJoCo is not installed in this Python environment. Install the project dependencies "
            "with: python -m pip install -r requirements.txt"
        ) from exc
    for attempt in range(20):
        if attempt:
            placements = place_objects([p.asset for p in placements], mode, random.Random(seed + attempt))
            generate_scene(placements)
        model = mujoco.MjModel.from_xml_path(str(OUTPUT_SCENE))
        data = mujoco.MjData(model)
        home_key = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, "home")
        if home_key >= 0:
            mujoco.mj_resetDataKeyframe(model, data, home_key)
        mujoco.mj_forward(model, data)
        if settle_and_check(model, data, placements, mode, mujoco):
            return model, data, placements, mujoco
    raise RuntimeError("Could not obtain a valid settled arrangement after 20 attempts")


def view_static(model, data, mujoco_module) -> None:
    import mujoco.viewer

    with mujoco.viewer.launch_passive(model, data) as viewer:
        viewer.cam.lookat[:] = (0.53, 0.0, TABLE_TOP_Z)
        viewer.cam.distance = 2.0
        viewer.cam.azimuth = 135.0
        viewer.cam.elevation = -35.0
        while viewer.is_running():
            # Deliberately do not step: this is a static inspection scene.
            viewer.sync()
            time.sleep(1.0 / 60.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and view randomized MuJoCo scene_004.")
    parser.add_argument("--mode", choices=("contact", "separated"), default="separated")
    parser.add_argument("--seed", type=int, help="fixed seed for a reproducible arrangement")
    parser.add_argument("--generate-only", action="store_true", help="write scene_004.xml without opening MuJoCo")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    objects = discover_objects()
    placements = place_objects(objects, args.mode, random.Random(seed))
    path = generate_scene(placements)
    print(f"Generated {path}")
    print(f"Objects: {', '.join(p.asset.name for p in placements)}")
    print(f"Mode: {args.mode}; seed: {seed}")
    if args.generate_only:
        return
    model, data, placements, mujoco_module = load_and_settle(placements, args.mode, seed)
    print("Objects settled successfully; opening the static viewer.")
    view_static(model, data, mujoco_module)


if __name__ == "__main__":
    main()
