"""Benchmark a square grid of falling balls in the Vulkan editor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from collision_grid_benchmark_common import (  # noqa: E402
    DEFAULT_GRID_SIZE,
    print_summary,
    write_summary,
)
from sim_platform import EditorSession  # noqa: E402


DEFAULT_EDITOR = PROJECT_ROOT / "build" / "windows-gui" / "Release" / "sim_editor.exe"
DEFAULT_OUTPUT = PROJECT_ROOT / "build" / "benchmarks" / "collision_grid_gui_summary.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editor", type=Path, default=DEFAULT_EDITOR)
    parser.add_argument("--backend", choices=["mujoco", "bullet3"], default="mujoco")
    parser.add_argument("--collision-backend",
                        choices=["fcl", "bullet3", "coal", "ifps-cpu", "ifps-gpu"],
                        default="fcl")
    parser.add_argument("--frames", type=int, default=1000)
    parser.add_argument(
        "--grid-size", type=int, default=DEFAULT_GRID_SIZE,
        help="square side length (at least 2; default: 10)",
    )
    parser.add_argument("--dt", type=float, default=0.002)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--progress-every", type=int, default=60)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument(
        "--skip-render",
        action="store_true",
        help="keep the Vulkan GUI backend active but skip per-frame rendering for diagnostics",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.frames <= 0 or args.dt <= 0.0 or args.progress_every < 0 or
            args.grid_size < 2):
        raise ValueError(
            "frames and dt must be positive; grid-size must be at least 2; "
            "progress-every cannot be negative"
        )
    if abs(args.dt - 0.002) > 1.0e-12:
        raise ValueError("the editor benchmark currently uses the editor fixed dt of 0.002 s")

    ball_count = args.grid_size * args.grid_size
    with EditorSession.launch(args.editor) as simulator:
        _plane, balls = simulator.create_collision_grid_benchmark(
            physics=args.backend, collision=args.collision_backend,
            grid_size=args.grid_size,
        )
        frames = []
        for frame_index in range(args.frames):
            profile = simulator.profile_step(balls, render=not args.skip_render)
            frames.append(
                {
                    "frame": frame_index,
                    "simulation_time_s": profile["simulation_time"],
                    "physics_tick_ms": profile["physics_tick_ms"],
                    "collision_sync_ms": profile["collision_sync_ms"],
                    "collision_check_ms": profile["collision_check_ms"],
                    "render_ms": profile["render_ms"],
                    "ball_collisions": profile["collisions"],
                }
            )
            if args.progress_every and (frame_index + 1) % args.progress_every == 0:
                print(
                    f"frame {frame_index + 1}/{args.frames}: "
                    f"physics={profile['physics_tick_ms']:.4f} ms, "
                    f"sync={profile['collision_sync_ms']:.4f} ms, "
                    f"collision={profile['collision_check_ms']:.4f} ms, "
                    f"render={profile['render_ms']:.4f} ms, "
                    f"colliding={sum(profile['collisions'])}/{ball_count}"
                )

        summary = write_summary(
            mode="gui",
            physics_backend=args.backend,
            collision_backend=args.collision_backend,
            grid_size=args.grid_size,
            fixed_time_step_s=args.dt,
            frames=frames,
            ball_entities=balls,
            final_positions=(simulator.get_transform(ball)[0] for ball in balls),
            render_enabled=not args.skip_render,
            output=args.output,
        )
        print_summary(summary, args.output)
        if args.keep_open:
            input("Benchmark complete. Press Enter to close the editor...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
