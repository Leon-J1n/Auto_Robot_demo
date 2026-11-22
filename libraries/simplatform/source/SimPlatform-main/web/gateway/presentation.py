from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PresentationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Timeline:
    timeline_id: str
    source_turn_id: str
    frames: list[dict[str, Any]] = field(default_factory=list)


class PresentationStore:
    def __init__(self, initial_snapshot: dict[str, Any], mode: str = "real-sync",
                 storage_path: Path | None = None,
                 binding: tuple[str, int] | None = None,
                 scene_id: str = "main"):
        self._lock = threading.RLock()
        self._storage_path = storage_path
        self.mode = mode
        self.revision = 0
        self.snapshot = copy.deepcopy(initial_snapshot)
        self.timeline: Timeline | None = None
        self.binding = binding
        self.presentation_binding = binding
        self.detached = False
        self.scene_id = scene_id
        self._restore()

    def _restore(self) -> None:
        if self._storage_path is None or not self._storage_path.is_file():
            return
        try:
            value = json.loads(self._storage_path.read_text(encoding="utf-8"))
            mode = str(value["mode"])
            snapshot = value["snapshot"]
            if mode not in {"real-sync", "sync-and-animate"} or not isinstance(snapshot, dict):
                raise ValueError("invalid presentation")
            timeline = value.get("timeline")
            restored_timeline = None
            if timeline is not None:
                frames = timeline["frames"]
                if not isinstance(frames, list) or len(frames) < 2:
                    raise ValueError("invalid timeline")
                restored_timeline = Timeline(
                    str(timeline["timelineId"]), str(timeline["sourceTurnId"]),
                    copy.deepcopy(frames))
            self.mode = mode
            self.revision = max(0, int(value["presentationRevision"]))
            self.snapshot = copy.deepcopy(snapshot)
            self.timeline = restored_timeline
            stored_binding = value.get("binding")
            if stored_binding is not None:
                self.presentation_binding = (str(stored_binding[0]), int(stored_binding[1]))
                if self.binding is not None:
                    self.detached = self.presentation_binding != self.binding
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return

    def _persist(self) -> None:
        if self._storage_path is None:
            return
        timeline = None
        if self.timeline is not None:
            timeline = {
                "timelineId": self.timeline.timeline_id,
                "sourceTurnId": self.timeline.source_turn_id,
                "frames": self.timeline.frames,
            }
        value = {
            "mode": self.mode,
            "presentationRevision": self.revision,
            "snapshot": self.snapshot,
            "timeline": timeline,
            "binding": (list(self.presentation_binding)
                        if self.presentation_binding is not None else None),
        }
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._storage_path.with_name(
            f".{self._storage_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
            os.replace(temporary, self._storage_path)
        finally:
            temporary.unlink(missing_ok=True)

    def set_mode(self, mode: str, authoritative: dict[str, Any]) -> dict[str, Any]:
        if mode not in {"real-sync", "sync-and-animate"}:
            raise PresentationError("INVALID_MODE", "unsupported presentation mode")
        with self._lock:
            if mode != self.mode:
                self.mode = mode
                self.revision += 1
                self.timeline = None
                self.snapshot = copy.deepcopy(authoritative)
                self.presentation_binding = self.binding
                self.detached = False
                self._persist()
            return self.current()

    def sync(self, authoritative: dict[str, Any]) -> None:
        with self._lock:
            if self.mode == "real-sync":
                self.snapshot = copy.deepcopy(authoritative)
                self.timeline = None
                self.presentation_binding = self.binding
                self.detached = False
                self.revision += 1
                self._persist()

    def publish_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.snapshot = copy.deepcopy(snapshot)
            self.timeline = None
            self.presentation_binding = self.binding
            self.detached = False
            self.revision += 1
            self._persist()
            return self.current()

    def publish_timeline(
        self, frames: list[dict[str, Any]], source_turn_id: str,
        base_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(frames) < 2:
            raise PresentationError(
                "INVALID_TIMELINE",
                "timeline must contain at least 2 frames",
            )
        with self._lock:
            static_snapshot = copy.deepcopy(base_snapshot or frames[0])
            base_entities = {entity["id"]: entity for entity in static_snapshot.get("entities", [])}
            compact_frames = []
            for frame in frames:
                compact_frames.append({
                    **{key: frame[key] for key in (
                        "schemaVersion", "sceneId", "worldId", "sceneGeneration",
                        "revision", "simulationTime", "status") if key in frame},
                    "entities": [
                        {**{key: entity[key] for key in ("id", "parent") if key in entity},
                         **{key: {component: list(values) for component, values in entity[key].items()}
                            for key in ("local", "world") if key in entity}}
                        for entity in frame.get("entities", [])
                        if base_snapshot is not None or any(entity.get(key) != base_entities.get(entity["id"], {}).get(key)
                               for key in ("parent", "local", "world"))
                    ],
                })
            self.timeline = Timeline(uuid.uuid4().hex, source_turn_id, compact_frames)
            self.snapshot = static_snapshot
            self.presentation_binding = self.binding
            self.detached = False
            self.revision += 1
            self._persist()
            return self.current()

    def frame(self, timeline_id: str, index: int) -> dict[str, Any]:
        with self._lock:
            if self.timeline is None or self.timeline.timeline_id != timeline_id:
                raise KeyError(timeline_id)
            if not 0 <= index < len(self.timeline.frames):
                raise IndexError(index)
            frame = {**copy.deepcopy(self.snapshot), **copy.deepcopy(self.timeline.frames[index])}
            static_entities = {
                int(entity["id"]): copy.deepcopy(entity)
                for entity in self.snapshot.get("entities", [])
                if isinstance(entity, dict) and "id" in entity
            }
            for transform in frame.get("entities", []):
                entity_id = int(transform["id"])
                base = static_entities.get(entity_id, {})
                base.update(transform)
                static_entities[entity_id] = base
            frame["entities"] = list(static_entities.values())
            return frame

    def compact_frame(self, timeline_id: str, index: int) -> dict[str, Any]:
        with self._lock:
            if self.timeline is None or self.timeline.timeline_id != timeline_id:
                raise KeyError(timeline_id)
            if not 0 <= index < len(self.timeline.frames):
                raise IndexError(index)
            return copy.deepcopy(self.timeline.frames[index])

    def timeline_data(self, timeline_id: str) -> dict[str, Any]:
        with self._lock:
            if self.timeline is None or self.timeline.timeline_id != timeline_id:
                raise KeyError(timeline_id)
            return {"snapshot": self.snapshot, "frames": self.timeline.frames}

    def current(self) -> dict[str, Any]:
        with self._lock:
            timeline = None
            if self.timeline is not None:
                times = [float(item.get("simulationTime", 0.0)) for item in self.timeline.frames]
                timeline = {
                    "timelineId": self.timeline.timeline_id,
                    "sceneId": self.scene_id,
                    "simSessionId": (self.presentation_binding[0]
                                     if self.presentation_binding else ""),
                    "sceneGeneration": (self.presentation_binding[1]
                                        if self.presentation_binding else 0),
                    "presentationRevision": self.revision,
                    "sourceTurnId": self.timeline.source_turn_id,
                    "dataUrl": f"/api/timelines/{self.timeline.timeline_id}/data",
                    "frameCount": len(self.timeline.frames),
                    "durationSeconds": max(0.0, times[-1] - times[0]),
                    "defaultFps": 50,
                    "frames": [
                        {
                            "index": index,
                            "simulationTime": times[index],
                            "sceneRevision": int(frame.get("revision", 0)),
                            "snapshotUrl": (
                                f"/api/timelines/{self.timeline.timeline_id}/frames/{index}"
                            ),
                        }
                        for index, frame in enumerate(self.timeline.frames)
                    ],
                }
            return {
                "mode": self.mode,
                "presentationRevision": self.revision,
                "snapshot": copy.deepcopy(self.snapshot),
                "timeline": timeline,
                "detached": self.detached,
            }

    def restore_current(self, value: dict[str, Any]) -> None:
        """Restore a previously published presentation for a conversation."""
        with self._lock:
            mode = str(value.get("mode", "real-sync"))
            if mode not in {"real-sync", "sync-and-animate"}:
                raise ValueError("invalid presentation mode")
            self.mode = mode
            self.revision = max(0, int(value.get("presentationRevision", 0)))
            self.snapshot = copy.deepcopy(value.get("snapshot", self.snapshot))
            timeline = value.get("timeline")
            if isinstance(timeline, dict) and isinstance(timeline.get("frames"), list):
                self.timeline = Timeline(str(timeline.get("timelineId", uuid.uuid4().hex)),
                                         str(timeline.get("sourceTurnId", "unknown")),
                                         copy.deepcopy(timeline["frames"]))
            else:
                self.timeline = None
            self.presentation_binding = self.binding
            self.detached = False
            self._persist()
