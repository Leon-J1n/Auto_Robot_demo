"""Turning planned joint paths into marker data the browser timeline can show.

A joint path says nothing directly about where the hand goes, so each waypoint is put
through forward kinematics for the pinch frame -- the point between the fingertips --
and the resulting curve is what gets drawn.

This module produces *data only*. The in-process version drew paths by calling an
editor-only RPC, which a separate service cannot use, so nothing here draws, connects,
or imports from the web gateway: `segment_markers` returns plain dataclasses carrying
an id, a colour, a radius, and world XYZ points, and whoever publishes snapshot frames
decides how to render them.

FK for a whole segment runs in a single platform call. Over Live Python that is one
round trip instead of one per waypoint, which matters because a shortcut RRT path can
carry a few hundred waypoints and this sits between a command and its answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

# Okabe-Ito, an eight-colour qualitative palette designed to stay distinguishable under
# the common forms of colour blindness. Black is replaced by a light grey that reads
# against a dark viewport. Segments take these in order and wrap around, which only
# happens past eight paths on screen at once.
PALETTE: tuple[tuple[float, float, float], ...] = (
    (0.337, 0.706, 0.914),  # sky blue
    (0.902, 0.624, 0.000),  # orange
    (0.000, 0.620, 0.451),  # bluish green
    (0.941, 0.894, 0.259),  # yellow
    (0.000, 0.447, 0.698),  # blue
    (0.835, 0.369, 0.000),  # vermillion
    (0.800, 0.475, 0.655),  # reddish purple
    (0.780, 0.780, 0.780),  # light grey
)

DEFAULT_MARKER_SPACING = 0.02
DEFAULT_MARKER_RADIUS = 0.008


@dataclass(frozen=True)
class TrajectoryMarkers:
    """One planned segment's gripper curve, ready to be rendered by a caller."""

    id: str
    color: tuple[float, float, float]
    radius: float
    points: tuple[tuple[float, float, float], ...]
    label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def marker_count(self) -> int:
        return len(self.points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "color": [float(value) for value in self.color],
            "radius": float(self.radius),
            "points": [[float(value) for value in point] for point in self.points],
            "label": self.label,
            "metadata": dict(self.metadata),
        }


def trajectory_color(index: int) -> tuple[float, float, float]:
    """The palette entry for a segment, wrapping past the end of the palette."""
    return PALETTE[int(index) % len(PALETTE)]


def thin_by_arc_length(points: Sequence[Sequence[float]],
                       spacing: float = DEFAULT_MARKER_SPACING) -> list[list[float]]:
    """Drop waypoints until consecutive markers are at least `spacing` metres apart.

    Spacing is measured along the path rather than by index. Waypoint density varies
    enormously between path types -- a joint interpolation is evenly spaced in joint
    angle, which bunches up near the shoulder and stretches out at full reach, while an
    RRT path after shortcutting has no regular spacing at all -- so a fixed stride would
    look dense on one path and sparse on another. Distance gives every path the same
    on-screen density whatever produced it.

    The first and last points always survive: they are where the motion starts and ends,
    which is the part of a path a reader most needs to see.
    """
    if spacing <= 0.0:
        raise ValueError("marker spacing must be positive")
    positions = np.atleast_2d(np.asarray(points, dtype=float))
    if positions.size == 0:
        return []
    if positions.shape[1] != 3:
        raise ValueError("each waypoint must contain three coordinates")
    kept = [positions[0]]
    for point in positions[1:-1]:
        if float(np.linalg.norm(point - kept[-1])) >= spacing:
            kept.append(point)
    if len(positions) > 1:
        # The last point is unconditional, so it can land closer than `spacing` to its
        # predecessor. Better a slightly tight final pair than a path that stops short of
        # where the arm actually ends up.
        kept.append(positions[-1])
    return [[float(value) for value in point] for point in kept]


def gripper_path(kinematics: Any, joint_path: Sequence[Sequence[float]], *,
                 spacing: float = DEFAULT_MARKER_SPACING) -> list[list[float]]:
    """The gripper-tip curve of a joint-space trajectory, thinned for display.

    Takes the FK service rather than the runtime because the runtime does not own one --
    each caller builds its own, already configured for the pinch frame this draws.

    FK runs for the whole path in a single platform call rather than per waypoint.
    """
    waypoints = np.atleast_2d(np.asarray(joint_path, dtype=float))
    if waypoints.size == 0:
        return []
    poses = kinematics.forward_batch(waypoints)
    return thin_by_arc_length([pose["position"] for pose in poses], spacing)


def segment_markers(kinematics: Any, segments: Iterable[Any], *,
                    spacing: float = DEFAULT_MARKER_SPACING,
                    radius: float = DEFAULT_MARKER_RADIUS,
                    prefix: str = "segment",
                    start_index: int = 0) -> list[TrajectoryMarkers]:
    """Marker data for every planned segment, each in its own palette colour.

    A segment is anything carrying joint waypoints: a planner result dict
    (`{"waypoints": [...], ...}`), an object with a `.trajectory` dict, or a bare
    (N, 6) array of waypoints. Diagnostics the planner reported travel through to
    `metadata` so a caller can label the timeline entry without re-deriving them.

    Every segment handed in gets an entry, so the timeline can show one per planned
    segment. A segment whose gripper does not move produces two coincident points --
    the first and last waypoint, which arc-length thinning always keeps -- rather than
    being dropped; an empty segment, which has no points at all, is skipped.

    `start_index` offsets both the generated ids and the palette, for callers that
    build up the timeline across several calls: without it every call would restart at
    `segment_1` in the first colour, and segments from different commands would be
    indistinguishable on screen and collide by id.
    """
    result: list[TrajectoryMarkers] = []
    for offset, segment in enumerate(segments):
        index = int(start_index) + offset
        waypoints, metadata = _segment_waypoints(segment)
        points = gripper_path(kinematics, waypoints, spacing=spacing)
        if len(points) < 2:
            continue
        result.append(TrajectoryMarkers(
            id=f"{prefix}_{index + 1}",
            color=trajectory_color(index),
            radius=float(radius),
            points=tuple(tuple(float(value) for value in point) for point in points),
            label=str(metadata.get("label", f"{prefix} {index + 1}")),
            metadata=metadata,
        ))
    return result


def _segment_waypoints(segment: Any) -> tuple[np.ndarray, dict[str, Any]]:
    """Joint waypoints and reportable metadata from one planned segment."""
    trajectory = getattr(segment, "trajectory", None)
    if isinstance(trajectory, Mapping):
        segment = trajectory
    if isinstance(segment, Mapping):
        waypoints = np.atleast_2d(np.asarray(segment["waypoints"], dtype=float))
        metadata = {key: segment[key] for key in
                    ("planner", "path_type", "duration", "planning_time", "diagnostics")
                    if key in segment}
        if "label" in segment:
            metadata["label"] = segment["label"]
        return waypoints, metadata
    return np.atleast_2d(np.asarray(segment, dtype=float)), {}


def path_length(points: Sequence[Sequence[float]]) -> float:
    """Total arc length of a Cartesian curve, in metres."""
    positions = np.atleast_2d(np.asarray(points, dtype=float))
    if len(positions) < 2:
        return 0.0
    return float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1)))
