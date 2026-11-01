"""Check/export scene geometry, optionally run genuine EAIK FK/IK round trips.

Run with python -B -m libraries.eaik.check_model --help.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from kinematics.models.mujoco_model import MujocoRobotModel
from utils import SceneRobotRuntime
from .model_data import extract_model_data, parameter_forward


ROOT = Path(__file__).resolve().parents[2]


def check_scene(scene, *, samples=200, try_eaik=False):
    runtime = SceneRobotRuntime(scene)
    try:
        robot = MujocoRobotModel(runtime)
        saved_qpos = runtime.data.qpos.copy()
        parameters = extract_model_data(runtime)
        rng = np.random.default_rng(20261007)
        limits = np.asarray(parameters["joint_limits"])
        joint_samples = np.vstack((np.zeros(len(limits)), robot.current_joints(),
                                   rng.uniform(limits[:, 0], limits[:, 1], (samples, len(limits)))))
        max_position_error = max_rotation_error = 0.
        for q in joint_samples:
            actual = robot.forward(q)
            predicted = robot.base_transform(q) @ parameter_forward(parameters, q)
            max_position_error = max(max_position_error, float(np.linalg.norm(predicted[:3, 3] - actual["position"])))
            max_rotation_error = max(max_rotation_error, float(np.max(np.abs(predicted[:3, :3] - actual["rotation_matrix"]))))
        if max_position_error > 1e-9 or max_rotation_error > 1e-9:
            raise ValueError("Exported geometry does not reproduce the scene FK")
        np.testing.assert_array_equal(saved_qpos, runtime.data.qpos)
        h = np.asarray(parameters["H_rows"])
        check = {
            "sample_count": len(joint_samples),
            "parameter_fk_max_position_error_m": max_position_error,
            "parameter_fk_max_rotation_element_error": max_rotation_error,
            "joint_2_3_4_parallel": bool(np.allclose(np.abs(h[1:4] @ h[1]), 1, atol=1e-10)),
            "live_state_unchanged": True,
            "eaik": {"status": "not_requested"},
        }
        if try_eaik:
            try:
                from eaik.IK_Homogeneous import HomogeneousRobot
            except ImportError as exc:
                check["eaik"] = {"status": "unavailable", "reason": str(exc)}
            else:
                bot = HomogeneousRobot(np.asarray(parameters["joint_transforms_zero"]))
                if not bot.hasKnownDecomposition():
                    raise ValueError("EAIK does not recognize the exported chain")
                max_fk_error = max_ik_error = 0.
                min_valid = None
                for q in joint_samples:
                    target = parameter_forward(parameters, q)
                    fk = np.asarray(bot.fwdKin(q))
                    max_fk_error = max(max_fk_error, float(np.max(np.abs(fk-target))))
                    valid_errors = []
                    for candidate in bot.IK(target).Q:
                        candidate = np.asarray(candidate, dtype=float)
                        if candidate.shape != q.shape or not np.all(np.isfinite(candidate)):
                            continue
                        # Reject least-squares/approximate answers by independent FK.
                        error = float(np.max(np.abs(parameter_forward(parameters, candidate)-target)))
                        # EAIK returns periodic angles; check a limit-valid equivalent.
                        center = (limits[:, 0]+limits[:, 1])/2
                        nearest = candidate + 2*np.pi*np.round((center-candidate)/(2*np.pi))
                        if error <= 1e-7 and np.all(nearest >= limits[:, 0]-1e-10) and np.all(nearest <= limits[:, 1]+1e-10):
                            valid_errors.append(error)
                    if not valid_errors:
                        raise ValueError(f"EAIK returned no accurate limit-valid solution for q={q.tolist()}")
                    max_ik_error = max(max_ik_error, max(valid_errors))
                    min_valid = len(valid_errors) if min_valid is None else min(min_valid, len(valid_errors))
                if max_fk_error > 1e-9:
                    raise ValueError("EAIK FK disagrees with exported geometry")
                check["eaik"] = {"status": "passed", "family": bot.getKinematicFamily(),
                                 "fk_max_transform_element_error": max_fk_error,
                                 "ik_max_transform_element_error": max_ik_error,
                                 "minimum_valid_solutions": min_valid}
        return parameters, check
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", nargs="+", type=Path, default=[ROOT / "world_model/robotsim/scene_002.xml"])
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--try-eaik", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "model_checks")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for scene in args.scenes:
        parameters, check = check_scene(scene, samples=args.samples, try_eaik=args.try_eaik)
        result = {"parameters": parameters, "checks": check}
        destination = args.output_dir / f"{scene.stem}.eaik.json"
        destination.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        summary.append({"scene": scene.name, "output": str(destination), **check})
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.try_eaik and any(item["eaik"]["status"] != "passed" for item in summary):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
