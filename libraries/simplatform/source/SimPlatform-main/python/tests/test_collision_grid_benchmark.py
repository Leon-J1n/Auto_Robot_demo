from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "examples" / "python"))

from collision_grid_benchmark_common import (  # noqa: E402
    DEFAULT_GRID_SIZE,
    ball_specs,
    plane_size,
    validate_grid_size,
    write_summary,
)


@pytest.mark.parametrize("grid_size", [2, 3, 10, 16])
def test_grid_parameters_cover_unique_row_column_combinations(grid_size: int) -> None:
    specs = ball_specs(grid_size)
    assert len(specs) == grid_size * grid_size
    assert plane_size(grid_size) == (float(grid_size), float(grid_size), 10.0)

    heights = [specs[row * grid_size]["initial_position_m"][2]
               for row in range(grid_size)]
    restitutions = [specs[column]["restitution"] for column in range(grid_size)]
    assert heights[0] == 2.0
    assert heights[-1] == 10.0
    assert restitutions[0] == 0.0
    assert restitutions[-1] == 1.0
    assert heights == sorted(set(heights))
    assert restitutions == sorted(set(restitutions))

    combinations = set()
    for spec in specs:
        row = int(spec["i"])
        column = int(spec["j"])
        assert spec["initial_position_m"][2] == heights[row]
        assert spec["restitution"] == restitutions[column]
        combinations.add((spec["initial_position_m"][2], spec["restitution"]))
    assert len(combinations) == grid_size * grid_size


def test_default_grid_size_and_validation() -> None:
    assert len(ball_specs()) == DEFAULT_GRID_SIZE**2
    with pytest.raises(ValueError, match="at least 2"):
        validate_grid_size(1)


def test_summary_uses_dynamic_grid_metadata(tmp_path: Path) -> None:
    grid_size = 3
    specs = ball_specs(grid_size)
    output = tmp_path / "summary.json"
    summary = write_summary(
        mode="headless",
        physics_backend="mujoco",
        collision_backend="ifps-cpu",
        grid_size=grid_size,
        fixed_time_step_s=0.002,
        frames=[{
            "physics_tick_ms": 0.1,
            "collision_check_ms": 0.2,
            "ball_collisions": [False] * len(specs),
        }],
        ball_entities=range(len(specs)),
        final_positions=(spec["initial_position_m"] for spec in specs),
        render_enabled=False,
        output=output,
    )
    assert summary["benchmark"] == "3x3 collision grid"
    assert summary["grid_size"] == 3
    assert summary["ball_count"] == 9
    assert summary["scene"]["plane"]["size_m"] == [3.0, 3.0, 10.0]
    assert "all 9 balls" in summary["timing_scope"]["collision_check_ms"]
    assert output.is_file()
