"""Compile the authored scene and check actual mesh bounds and initial contacts.

Run from the repository root with the project Python interpreter.
"""
import json
from itertools import combinations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import mujoco
import numpy as np

from execution import SceneResourceCoordinator
from execution.backends.mujoco.backend import MuJoCoExecutionBackend
from execution.resources import component, pose_matrix
from demo.interaction.object_library import control_from_grasp_frame
from libraries.kinematic_chain.service import load_urdf


def check():
    folder = Path(__file__).resolve().parent
    root = folder.parents[1]
    task = json.loads((folder / "commands.005.json").read_text(encoding="utf-8"))
    resources = SceneResourceCoordinator(
        scene_file=folder / task["scene_file"],
        asset_root=root / "services/object_library_server/data").resolve(task)
    backend = MuJoCoExecutionBackend()
    backend.load(resources, root / "tmp/commands005_piper_layout/runtime")
    model, data = backend.model, backend.data
    names = ["Apple", "Banana", "Marker", "Screwdriver", "RubiksCube", "Basket", "Tray"]
    bounds = {}
    for name in names:
        body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
        points = []
        for geom in np.flatnonzero(model.geom_bodyid == body):
            if model.geom_type[geom] != mujoco.mjtGeom.mjGEOM_MESH:
                continue
            mesh = model.geom_dataid[geom]
            start, count = model.mesh_vertadr[mesh], model.mesh_vertnum[mesh]
            vertices = model.mesh_vert[start:start + count]
            points.extend(vertices @ data.geom_xmat[geom].reshape(3, 3).T + data.geom_xpos[geom])
        if not points:
            raise AssertionError(f"No mesh bounds for {name}")
        bounds[name] = np.min(points, axis=0), np.max(points, axis=0)
    separations = []
    for a, b in combinations(names, 2):
        lo_a, hi_a = bounds[a]
        lo_b, hi_b = bounds[b]
        gap = float(np.max(np.maximum(lo_a[:2] - hi_b[:2], lo_b[:2] - hi_a[:2])))
        if gap < .02:
            raise AssertionError(f"{a}/{b}: horizontal mesh clearance {gap:.6f} m < 20 mm")
        separations.append({"pair": [a, b], "separation_m": gap})
    penetrations = []
    for contact in data.contact:
        if contact.dist < -1e-4:
            pair = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY,
                                    model.geom_bodyid[g]) for g in (contact.geom1, contact.geom2)]
            penetrations.append({"pair": pair, "distance_m": float(contact.dist)})
    report = {"scene_version": task["scene_version"],
              "bounds": {name: [lo.tolist(), hi.tolist()] for name, (lo, hi) in bounds.items()},
              "minimum_horizontal_separation_m": min(x["separation_m"] for x in separations),
              "penetrations": penetrations}
    output = root / "tmp/commands005_piper_layout/layout_check.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if penetrations:
        raise AssertionError(f"Initial penetrations: {penetrations}")
    ref = component(resources.robot_object, "RobotModelRef")
    kin = load_urdf(resources.robot_urdf, base_link=ref["base_link"], tip_link=ref["tip_link"],
                    tcp_transform=ref.get("tcp_transform"),
                    world_from_base=pose_matrix(resources.robot_object),
                    initial_joints=resources.initial_joints)
    control_from_grasp = control_from_grasp_frame(resources)
    checks = []
    # Use the downward grasp orientation from the failed Apple request, with
    # the authored tool mount and actual asset limits. Check the whole depth
    # range plus raised approaches over both objects and containers, so
    # non-overlap alone cannot pass preflight.
    for name in ("Apple", "RubiksCube", "Basket", "Tray"):
        obj = next(o for o in resources.scene["objects"] if o["object_name_in_scene"] == name)
        position = np.asarray(component(obj, "Transform")["position"], dtype=float)
        if name in {"Basket", "Tray"}:
            position[2] = bounds[name][1][2]
        for offset in (0., .005, .01, .015, .02, .025, .03, .08):
            grasp = np.eye(4)
            grasp[:3, :3] = [[0, 1, 0], [1, 0, 0], [0, 0, -1]]
            grasp[:3, 3] = position + [0, 0, .02 + offset]
            control = grasp @ np.linalg.inv(control_from_grasp)
            try:
                result = kin.solve_one({"position": control[:3, 3], "rotation_matrix": control[:3, :3]},
                                       seed=resources.initial_joints, backend="ssik", timeout=5)
            except Exception as exc:
                raise AssertionError(f"{name} IK preflight at offset {offset}: {exc}") from exc
            checks.append({"object": name, "height_offset_m": offset, "solver": result.solver})
    report["ik_checks"] = checks
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"success": True, "minimum_clearance_m": report["minimum_horizontal_separation_m"],
                      "penetrations": len(penetrations), "ik_checks": len(checks), "report": str(output)}))


if __name__ == "__main__":
    check()
