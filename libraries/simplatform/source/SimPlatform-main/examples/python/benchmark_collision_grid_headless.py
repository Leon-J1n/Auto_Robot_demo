"""Benchmark a square grid of falling balls with no renderer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from collision_grid_benchmark_common import (  # noqa: E402
    BALL_RADIUS_M,
    DEFAULT_GRID_SIZE,
    PLANE_POSITION_M,
    ball_specs,
    plane_size,
    print_summary,
    write_summary,
)
from sim_platform import Simulator, Transform  # noqa: E402


DEFAULT_OUTPUT = PROJECT_ROOT / "build" / "benchmarks" / "collision_grid_headless_summary.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.frames <= 0 or args.dt <= 0.0 or args.progress_every < 0 or
            args.grid_size < 2):
        raise ValueError(
            "frames and dt must be positive; grid-size must be at least 2; "
            "progress-every cannot be negative"
        )

    ball_count = args.grid_size * args.grid_size
    floor_size = plane_size(args.grid_size)

    with Simulator(
        physics=args.backend, collision=args.collision_backend,
        cache_directory=PROJECT_ROOT / "build" / "benchmark-cache"
    ) as simulator:
        world = simulator.create_world(fixed_time_step=args.dt, seed=0)
        plane = world.create_primitive("Benchmark Floor Cube", "box", (1.0, 1.0, 1.0))
        plane.transform = Transform(translation=PLANE_POSITION_M, scale=floor_size)
        plane.set_rigid_body(
            enabled=True,
            dynamic=False,
            gravity=False,
            mass=1.0,
            restitution=1.0,
        )

        balls = []
        for spec in ball_specs(args.grid_size):
            ball = world.create_primitive(
                f"Benchmark Ball {spec['i']}_{spec['j']}",
                "sphere",
                (BALL_RADIUS_M, BALL_RADIUS_M, BALL_RADIUS_M),
            )
            ball.transform = Transform(
                translation=tuple(spec["initial_position_m"]), scale=(0.5, 0.5, 0.5)
            )
            ball.set_rigid_body(
                enabled=True,
                dynamic=True,
                gravity=True,
                mass=1.0,
                restitution=float(spec["restitution"]),
            )
            balls.append(ball)

        frames = []
        for frame_index in range(args.frames):
            physics_ms = simulator.step_profiled(world)
            collisions, collision_ms = world.scene_collisions(balls)
            frames.append(
                {
                    "frame": frame_index,
                    "simulation_time_s": simulator.time(world),
                    "physics_tick_ms": physics_ms,
                    "collision_check_ms": collision_ms,
                    "ball_collisions": collisions.tolist(),
                }
            )
            if args.progress_every and (frame_index + 1) % args.progress_every == 0:
                print(
                    f"frame {frame_index + 1}/{args.frames}: "
                    f"physics={physics_ms:.4f} ms, collision={collision_ms:.4f} ms, "
                    f"colliding={int(collisions.sum())}/{ball_count}"
                )

        final_positions = [ball.transform.translation for ball in balls]

    summary = write_summary(
        mode="headless",
        physics_backend=args.backend,
        collision_backend=args.collision_backend,
        grid_size=args.grid_size,
        fixed_time_step_s=args.dt,
        frames=frames,
        ball_entities=(ball.handle for ball in balls),
        final_positions=final_positions,
        render_enabled=False,
        output=args.output,
    )
    print_summary(summary, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
