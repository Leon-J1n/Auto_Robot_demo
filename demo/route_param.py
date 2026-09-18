"""Minimal route recording for the demo runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


class RouteRecorder:
    """Persist trajectory points grouped only by execution step number."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.data: list[dict[str, Any]] = []
        self._flush()

    def record_step(
        self,
        *,
        step_index: int,
        step_type: str,
        result: Mapping[str, Any],
        gripper_width: float,
    ) -> None:
        """Record one step using only timestamp, six joints and gripper width."""
        trajectory = result.get("trajectory", {})
        points: list[dict[str, Any]] = []
        if isinstance(trajectory, Mapping):
            waypoints = trajectory.get("waypoints", [])
            timestamps = trajectory.get("time_from_start", [])
            if isinstance(waypoints, Sequence) and not isinstance(waypoints, (str, bytes)):
                if not isinstance(timestamps, Sequence) or isinstance(timestamps, (str, bytes)):
                    timestamps = list(range(len(waypoints)))
                for timestamp, waypoint in zip(timestamps, waypoints):
                    if not isinstance(waypoint, Sequence) or isinstance(waypoint, (str, bytes)):
                        continue
                    points.append(
                        {
                            "time_from_start_s": float(timestamp),
                            "joint_positions_rad": [float(value) for value in waypoint],
                            "gripper_width_m": float(gripper_width),
                        }
                    )

        if not points:
            points = [{
                "time_from_start_s": 0.0,
                "joint_positions_rad": [],
                "gripper_width_m": float(gripper_width),
            }]
        self.data.append({"step_index": int(step_index), "type": step_type, "points": points})
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def record_samples(self, *, step_index: int, step_type: str, samples: list[dict[str, Any]]) -> None:
        points = [{
            "time_from_start_s": float(s["time_from_start_s"]),
            "joint_positions_rad": [float(v) for v in s["joint_positions_rad"]],
            "gripper_width_m": float(s["gripper_width_m"]),
        } for s in samples]
        self.data.append({"step_index": int(step_index), "type": step_type, "points": points})
        self._flush()
