"""Tests for trajectory presentation and capture frame hygiene.

The per-segment trajectory display is a hard requirement, and it works by injecting visual-only
marker entities into published snapshot frames. These tests pin the properties the Gateway and
the browser depend on: a marker never gains physics, ids never collide with real entities, and
capture frame times stay strictly increasing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.gateway_client import (MARKER_ID_BASE, PALETTE, TrajectorySegment,  # noqa: E402
                                          GatewayError, marker_entities,
                                          normalize_frame_times, segment_color,
                                          with_trajectories)


def _segment(name: str, count: int = 3) -> TrajectorySegment:
    return TrajectorySegment(id=name, points=[(float(i), 0.5, 0.6) for i in range(count)])


def _frame(entity_count: int = 2) -> dict:
    return {
        "schemaVersion": "1.0", "sceneId": "main", "worldId": 1, "sceneGeneration": 0,
        "revision": 7, "simulationTime": 1.0, "status": "paused",
        "entities": [{"id": index + 1, "name": f"Entity {index}", "parent": None,
                      "local": {}, "world": {}} for index in range(entity_count)],
    }


def test_markers_are_visual_only() -> None:
    entities = marker_entities([_segment("seg-1")])
    assert entities
    for entity in entities:
        # A drawn path must never obstruct the arm nor fall under gravity.
        assert "collider" not in entity
        assert "rigidBody" not in entity
        assert entity["visual"]["shape"] == "sphere"
        assert entity["visual"]["visible"] is True


def test_marker_ids_do_not_collide_with_scene_entities() -> None:
    frame = _frame(entity_count=5)
    merged = with_trajectories(frame, [_segment("seg-1"), _segment("seg-2")])
    ids = [entity["id"] for entity in merged["entities"]]
    assert len(ids) == len(set(ids))
    scene_ids = {entity["id"] for entity in frame["entities"]}
    marker_ids = set(ids) - scene_ids
    assert marker_ids and min(marker_ids) >= MARKER_ID_BASE


def test_each_segment_gets_a_distinct_color() -> None:
    segments = [_segment(f"seg-{index}") for index in range(4)]
    entities = marker_entities(segments)
    colors = {tuple(entity["visual"]["color"][:3]) for entity in entities}
    # Segments are told apart by colour, so four segments must not share one.
    assert len(colors) == 4
    assert segment_color(len(PALETTE)) == segment_color(0)


def test_marker_size_follows_radius() -> None:
    segment = TrajectorySegment(id="seg", points=[(0.0, 0.0, 0.0)], radius=0.02)
    entity = marker_entities([segment])[0]
    assert entity["visual"]["size"] == [0.02, 0.02, 0.02]
    # A non-positive radius would produce a degenerate mesh.
    zero = TrajectorySegment(id="seg", points=[(0.0, 0.0, 0.0)], radius=0.0)
    assert marker_entities([zero])[0]["visual"]["size"][0] > 0.0


def test_marker_positions_are_world_coordinates() -> None:
    segment = TrajectorySegment(id="seg", points=[(0.1, 0.2, 0.3)])
    entity = marker_entities([segment])[0]
    assert entity["local"]["translation"] == [0.1, 0.2, 0.3]
    assert entity["world"]["translation"] == [0.1, 0.2, 0.3]
    assert entity["parent"] is None


def test_with_trajectories_leaves_the_source_frame_untouched() -> None:
    frame = _frame()
    original = len(frame["entities"])
    merged = with_trajectories(frame, [_segment("seg-1")])
    assert len(frame["entities"]) == original
    assert len(merged["entities"]) > original
    # Envelope metadata must survive so the Gateway's binding check still passes.
    assert merged["sceneGeneration"] == frame["sceneGeneration"]
    assert merged["revision"] == frame["revision"]


def test_no_segments_is_a_passthrough() -> None:
    frame = _frame()
    assert with_trajectories(frame, []) is frame


def test_frame_without_entities_is_rejected() -> None:
    with pytest.raises(GatewayError):
        with_trajectories({"schemaVersion": "1.0"}, [_segment("seg")])


def test_frame_times_are_forced_strictly_increasing() -> None:
    # Kinematic replay writes joint targets without advancing the clock, so several frames can
    # share a simulation time; the Gateway rejects those.
    frames = [{"simulationTime": 1.0, "entities": []} for _ in range(4)]
    result = normalize_frame_times(frames, fps=30.0)
    times = [frame["simulationTime"] for frame in result]
    assert times == sorted(times)
    assert all(later > earlier for earlier, later in zip(times, times[1:]))


def test_default_frame_times_are_normalized_to_50_fps() -> None:
    frames = [{"simulationTime": 1.0, "entities": []} for _ in range(1001)]
    times = [frame["simulationTime"] for frame in normalize_frame_times(frames)]
    assert times[-1] - times[0] == pytest.approx(20.0)
    assert times[1] - times[0] == pytest.approx(0.02)


def test_frame_times_preserve_a_real_advancing_clock() -> None:
    frames = [{"simulationTime": value, "entities": []} for value in (0.5, 1.0, 1.5)]
    assert [f["simulationTime"] for f in normalize_frame_times(frames)] == [0.5, 1.0, 1.5]


def test_non_finite_frame_times_are_replaced() -> None:
    frames = [{"simulationTime": 0.0, "entities": []},
              {"simulationTime": float("inf"), "entities": []},
              {"simulationTime": float("nan"), "entities": []}]
    times = [frame["simulationTime"] for frame in normalize_frame_times(frames)]
    assert all(value == value and value not in (float("inf"), float("-inf"))
               for value in times)
    assert times == sorted(times)
