"""Shared scene definition and JSON Summary helpers for collision-grid benchmarks."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable


DEFAULT_GRID_SIZE = 10
BALL_DIAMETER_M = 0.5
BALL_RADIUS_M = BALL_DIAMETER_M / 2.0
PLANE_HEIGHT_M = 10.0
# Preserve the previous 2 cm floor's top surface at z=+0.01 m.
PLANE_POSITION_M = (0.0, 0.0, -4.99)


def validate_grid_size(grid_size: int) -> int:
    if grid_size < 2:
        raise ValueError("grid-size must be at least 2 to preserve both endpoint values")
    return grid_size


def plane_size(grid_size: int = DEFAULT_GRID_SIZE) -> tuple[float, float, float]:
    size = float(validate_grid_size(grid_size))
    return (size, size, PLANE_HEIGHT_M)


def ball_specs(grid_size: int = DEFAULT_GRID_SIZE) -> list[dict[str, Any]]:
    grid_size = validate_grid_size(grid_size)
    offset = 0.5 * (grid_size - 1)
    return [
        {
            "index": i * grid_size + j,
            "i": i,
            "j": j,
            "initial_position_m": [
                -offset + i,
                -offset + j,
                2.0 + 8.0 * i / (grid_size - 1),
            ],
            "diameter_m": BALL_DIAMETER_M,
            "restitution": j / (grid_size - 1),
        }
        for i in range(grid_size)
        for j in range(grid_size)
    ]


def timing_stats(values: Iterable[float]) -> dict[str, float]:
    samples = list(values)
    if not samples:
        return {"average_ms": 0.0, "minimum_ms": 0.0, "maximum_ms": 0.0}
    return {
        "average_ms": statistics.fmean(samples),
        "minimum_ms": min(samples),
        "maximum_ms": max(samples),
    }


def write_summary(
    *,
    mode: str,
    physics_backend: str,
    collision_backend: str = "fcl",
    grid_size: int = DEFAULT_GRID_SIZE,
    fixed_time_step_s: float,
    frames: list[dict[str, Any]],
    ball_entities: Iterable[int],
    final_positions: Iterable[Iterable[float]],
    render_enabled: bool,
    output: Path,
) -> dict[str, Any]:
    grid_size = validate_grid_size(grid_size)
    ball_count = grid_size * grid_size
    specs = ball_specs(grid_size)
    floor_size = plane_size(grid_size)
    entities = list(ball_entities)
    positions = [list(map(float, position)) for position in final_positions]
    if len(entities) != len(specs):
        raise ValueError(f"the benchmark must contain exactly {ball_count} ball entities")
    if len(positions) != len(specs) or any(len(position) != 3 for position in positions):
        raise ValueError(
            f"final_positions must contain exactly {ball_count} XYZ positions"
        )

    collision_history = [
        [bool(frame["ball_collisions"][index]) for frame in frames]
        for index in range(len(specs))
    ]
    ball_results = []
    for spec, entity, position, history in zip(
        specs, entities, positions, collision_history, strict=True
    ):
        collision_frames = [index for index, colliding in enumerate(history) if colliding]
        ball_results.append(
            {
                **spec,
                "entity": entity,
                "final_position_m": position,
                "below_floor_at_end": position[2] < -BALL_RADIUS_M,
                "collided_ever": bool(collision_frames),
                "collision_frame_count": len(collision_frames),
                "first_collision_frame": collision_frames[0] if collision_frames else None,
            }
        )

    timing = {
        "collision_check": timing_stats(
            float(frame["collision_check_ms"]) for frame in frames
        ),
        "physics_tick": timing_stats(float(frame["physics_tick_ms"]) for frame in frames),
    }
    if all("collision_sync_ms" in frame for frame in frames):
        timing["collision_sync"] = timing_stats(
            float(frame["collision_sync_ms"]) for frame in frames
        )
    if render_enabled:
        timing["render"] = timing_stats(float(frame["render_ms"]) for frame in frames)

    summary: dict[str, Any] = {
        "benchmark": f"{grid_size}x{grid_size} collision grid",
        "grid_size": grid_size,
        "ball_count": ball_count,
        "mode": mode,
        "backends": {
            "physics": {"mujoco": "MuJoCo", "bullet3": "Bullet3"}[physics_backend],
            "collision": {"fcl": "FCL", "bullet3": "Bullet3",
                          "coal": "Coal",
                          "ifps-cpu": "IFPS-CPU",
                          "ifps-gpu": "IFPS-GPU"}[collision_backend],
            "renderer": "Vulkan" if mode == "gui" else "None",
        },
        "fixed_time_step_s": fixed_time_step_s,
        "frame_count": len(frames),
        "per_frame_rendering": render_enabled,
        "timing_scope": {
            "physics_tick_ms": (
                "one fixed simulation tick excluding collision backend scene sync"
                if "collision_sync" in timing
                else "one fixed simulation tick including collision backend scene sync"
            ),
            "collision_sync_ms": (
                "post-physics collision backend transform synchronization"
                if "collision_sync" in timing else None
            ),
            "collision_check_ms": (
                f"one native batch scene-collision query for all {ball_count} balls"
            ),
            "render_ms": "one Vulkan editor viewport render" if render_enabled else None,
        },
        "scene": {
            "plane": {
                "position_m": list(PLANE_POSITION_M),
                "size_m": list(floor_size),
                "footprint_m": {
                    "x": [-grid_size / 2.0, grid_size / 2.0],
                    "y": [-grid_size / 2.0, grid_size / 2.0],
                },
                "top_surface_z_m": 0.01,
            },
            "coordinate_note": (
                f"The temporary floor is {grid_size} m wide and centered at z=-4.99 m, "
                "preserving its top surface at z=0.01 m. Ball rows span initial heights "
                "from 2 to 10 m; columns span restitution values from 0 to 1."
            ),
            "balls": [{**spec, "entity": entity} for spec, entity in zip(specs, entities, strict=True)],
        },
        "frames": frames,
        "ball_results": ball_results,
        "timing_summary": timing,
        "collision_summary": {
            "balls_collided_ever": sum(result["collided_ever"] for result in ball_results),
            "balls_never_collided": sum(not result["collided_ever"] for result in ball_results),
            "balls_below_floor_at_end": sum(
                result["below_floor_at_end"] for result in ball_results
            ),
        },
    }
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def print_summary(summary: dict[str, Any], output: Path) -> None:
    timing = summary["timing_summary"]
    collisions = summary["collision_summary"]
    ball_count = int(summary["ball_count"])
    print("\nSummary")
    print(f"  mode/backends: {summary['mode']} / {summary['backends']}")
    print(f"  frames: {summary['frame_count']}")
    for name in ("physics_tick", "collision_sync", "collision_check", "render"):
        if name not in timing:
            continue
        values = timing[name]
        print(
            f"  {name}: avg={values['average_ms']:.6f} ms, "
            f"min={values['minimum_ms']:.6f} ms, max={values['maximum_ms']:.6f} ms"
        )
    print(
        f"  balls collided ever: {collisions['balls_collided_ever']}/{ball_count}; "
        f"never collided: {collisions['balls_never_collided']}/{ball_count}"
    )
    print(
        f"  balls below floor at end: "
        f"{collisions['balls_below_floor_at_end']}/{ball_count}"
    )
    print(f"  full per-frame/per-ball Summary: {output.expanduser().resolve()}")
