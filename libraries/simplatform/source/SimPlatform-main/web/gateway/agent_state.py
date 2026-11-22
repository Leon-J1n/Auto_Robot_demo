from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path
from typing import Any


class GatewayAgentState:
    def __init__(self, path: Path | None = None, max_jobs: int = 1000):
        self.path = path
        self.max_jobs = max_jobs
        self.conversations: dict[str, tuple[str, str, int, str]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self._restore()

    def _restore(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            for scene_id, binding in value.get("conversations", {}).items():
                if isinstance(binding, list) and len(binding) == 4:
                    self.conversations[str(scene_id)] = (
                        str(binding[0]), str(binding[1]), int(binding[2]), str(binding[3]))
            jobs = value.get("jobs", {})
            if isinstance(jobs, dict):
                self.jobs = {
                    str(job_id): dict(job) for job_id, job in jobs.items()
                    if isinstance(job, dict)
                }
                for job in self.jobs.values():
                    if job.get("status") in {"queued", "running"}:
                        job.update(
                            status="interrupted",
                            message="Gateway restarted before the Agent job completed",
                            recovered=True,
                        )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    def persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps({
                "version": 1,
                "conversations": {
                    key: list(value) for key, value in self.conversations.items()
                },
                "jobs": self.jobs,
            }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def bind(self, scene_id: str, value: tuple[str, str, int, str]) -> None:
        self.conversations[scene_id] = value
        self.persist()

    def unbind(self, scene_id: str) -> None:
        if self.conversations.pop(scene_id, None) is not None:
            self.persist()

    def put_job(self, job: dict[str, Any]) -> None:
        self.jobs[str(job["id"])] = copy.deepcopy(job)
        if len(self.jobs) > self.max_jobs:
            ordered = sorted(
                self.jobs.values(), key=lambda item: float(item.get("createdAt", 0.0)))
            for old in ordered[:len(self.jobs) - self.max_jobs]:
                self.jobs.pop(str(old["id"]), None)
        self.persist()
