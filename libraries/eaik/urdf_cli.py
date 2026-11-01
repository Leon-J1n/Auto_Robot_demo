"""Command-line EAIK URDF entry: inspect/export a chain or solve a target JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .urdf import load_urdf


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urdf", required=True, type=Path)
    parser.add_argument("--base-link", help="Defaults to the URDF root link")
    parser.add_argument("--tip-link", required=True, help="Link defining the endpoint before an optional TCP offset")
    parser.add_argument("--target", type=Path, help="World-frame pose JSON accepted by solve_one")
    parser.add_argument("--seed", nargs="+", type=float, help="Radians, in the printed joint_names order")
    parser.add_argument("--tcp-transform", type=Path, help="JSON rigid 4x4 matrix: tip_from_tcp")
    parser.add_argument("--world-from-base", type=Path, help="JSON rigid 4x4 matrix: world_from_base")
    parser.add_argument("--continuous-limits", type=Path, help="JSON mapping continuous joint names to finite [lower, upper] radian windows")
    parser.add_argument("--all", action="store_true", help="Return up to --max-solutions solutions")
    parser.add_argument("--max-solutions", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=2.)
    parser.add_argument("--max-joint-step", type=float, help="Maximum per-joint change relative to --seed, radians")
    parser.add_argument("--export", type=Path, help="Save selected-chain EAIK parameters as JSON")
    args = parser.parse_args()
    try:
        kin = load_urdf(args.urdf, base_link=args.base_link, tip_link=args.tip_link,
                        initial_joints=args.seed,
                        tcp_transform=_read_json(args.tcp_transform) if args.tcp_transform else None,
                        world_from_base=_read_json(args.world_from_base) if args.world_from_base else None,
                        continuous_limits=_read_json(args.continuous_limits) if args.continuous_limits else None)
        if args.export:
            args.export.parent.mkdir(parents=True, exist_ok=True)
            args.export.write_text(json.dumps(kin.robot.eaik_parameters, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
        output = {"joint_names": list(kin.robot.joint_names), "base_link": kin.robot.base_link,
                  "tip_link": kin.robot.tip_link,
                  "current_pose": {key: value.tolist() for key, value in kin.forward(kin.robot.current_joints()).items()}}
        if args.target:
            kwargs = {"seed": args.seed, "backend": "eaik", "timeout": args.timeout,
                      "max_joint_step": args.max_joint_step}
            if args.all:
                result = kin.solve_all(_read_json(args.target), max_solutions=args.max_solutions, **kwargs)
            else:
                result = kin.solve_one(_read_json(args.target), **kwargs)
            output.update({"solution": result.solution.tolist(),
                           "solutions": [q.tolist() for q in result.solutions], **result.metadata()})
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        parser.exit(1, f"URDF/EAIK error: {exc}\n")


if __name__ == "__main__":
    main()
