"""SQLite persistence for conversations, messages, turns and public domain events.

This is the auditable business layer of the two-layer context described in design doc §8. The
model-side context (message list or upstream session id) lives beside it and is never exposed
through the HTTP contract.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    target TEXT NOT NULL DEFAULT 'platform',
    status TEXT NOT NULL DEFAULT 'active',
    scene_id TEXT,
    sim_session_id TEXT,
    scene_generation INTEGER,
    connect TEXT,
    presentation_mode TEXT NOT NULL DEFAULT 'real-sync',
    model_context TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    turn_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    presentation_revision INTEGER,
    affected_objects TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS conversation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    turn_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS turns_request_idx
    ON turns (conversation_id, request_id);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages (conversation_id, id);
CREATE INDEX IF NOT EXISTS events_conversation_idx
    ON conversation_events (conversation_id, event_id);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class ConversationStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus one lock: turns run on the event loop but recovery and
        # the CLI touch the same connection.
        self._db = sqlite3.connect(
            str(self.path) if self.path is not None else ":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- service metadata ------------------------------------------------------------

    def set_service_context(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO service_metadata (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), _now()))
            self._db.commit()

    def service_context(self, key: str) -> Any:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM service_metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(str(row["value"]))
        except json.JSONDecodeError:
            return None

    # -- conversations ---------------------------------------------------------------

    def create_conversation(self, *, target: str = "platform", scene_id: str | None = None,
                            sim_session_id: str | None = None,
                            scene_generation: int | None = None, connect: str | None = None,
                            presentation_mode: str = "real-sync") -> str:
        conversation_id = uuid.uuid4().hex
        stamp = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO conversations (conversation_id, target, status, scene_id,"
                " sim_session_id, scene_generation, connect, presentation_mode, created_at,"
                " updated_at) VALUES (?,?,'active',?,?,?,?,?,?,?)",
                (conversation_id, target, scene_id, sim_session_id, scene_generation, connect,
                 presentation_mode, stamp, stamp))
            self._db.commit()
        return conversation_id

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_conversations(self, *, target: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM conversations"
        params: tuple[Any, ...] = ()
        if target is not None:
            query += " WHERE target = ?"
            params = (target,)
        query += " ORDER BY created_at DESC"
        with self._lock:
            return [dict(row) for row in self._db.execute(query, params).fetchall()]

    def update_binding(self, conversation_id: str, *, scene_id: str | None,
                       sim_session_id: str | None, scene_generation: int | None,
                       connect: str | None, presentation_mode: str | None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE conversations SET scene_id = COALESCE(?, scene_id),"
                " sim_session_id = COALESCE(?, sim_session_id),"
                " scene_generation = COALESCE(?, scene_generation),"
                " connect = COALESCE(?, connect),"
                " presentation_mode = COALESCE(?, presentation_mode), updated_at = ?"
                " WHERE conversation_id = ?",
                (scene_id, sim_session_id, scene_generation, connect, presentation_mode,
                 _now(), conversation_id))
            self._db.commit()

    def set_model_context(self, conversation_id: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE conversations SET model_context = ?, updated_at = ?"
                " WHERE conversation_id = ?",
                (json.dumps(value, ensure_ascii=False), _now(), conversation_id))
            self._db.commit()

    def model_context(self, conversation_id: str) -> Any:
        row = self.conversation(conversation_id)
        if row is None or not row.get("model_context"):
            return None
        try:
            return json.loads(str(row["model_context"]))
        except json.JSONDecodeError:
            return None

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
            self._db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            self._db.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
            self._db.execute("DELETE FROM conversation_events WHERE conversation_id = ?",
                             (conversation_id,))
            self._db.commit()
            return cursor.rowcount > 0

    # -- messages -------------------------------------------------------------------

    def add_message(self, conversation_id: str, role: str, content: str,
                    turn_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages (conversation_id, role, content, turn_id, created_at)"
                " VALUES (?,?,?,?,?)", (conversation_id, role, content, turn_id, _now()))
            self._db.commit()

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,)).fetchall()]

    # -- turns ----------------------------------------------------------------------

    def find_turn_by_request(self, conversation_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM turns WHERE conversation_id = ? AND request_id = ?",
                (conversation_id, request_id)).fetchone()
        return dict(row) if row is not None else None

    def create_turn(self, conversation_id: str, request_id: str) -> str:
        turn_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO turns (turn_id, conversation_id, request_id, status, created_at)"
                " VALUES (?,?,?,'queued',?)", (turn_id, conversation_id, request_id, _now()))
            self._db.commit()
        return turn_id

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
        return dict(row) if row is not None else None

    def turns(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM turns WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,)).fetchall()]

    def update_turn(self, turn_id: str, *, status: str | None = None,
                    message: str | None = None, error_code: str | None = None,
                    presentation_revision: int | None = None,
                    affected_objects: list[str] | None = None) -> None:
        terminal = status in {"succeeded", "failed", "cancelled", "interrupted"}
        with self._lock:
            self._db.execute(
                "UPDATE turns SET status = COALESCE(?, status),"
                " message = COALESCE(?, message), error_code = COALESCE(?, error_code),"
                " presentation_revision = COALESCE(?, presentation_revision),"
                " affected_objects = COALESCE(?, affected_objects),"
                " finished_at = CASE WHEN ? THEN ? ELSE finished_at END"
                " WHERE turn_id = ?",
                (status, message, error_code, presentation_revision,
                 None if affected_objects is None
                 else json.dumps(affected_objects, ensure_ascii=False),
                 1 if terminal else 0, _now(), turn_id))
            self._db.commit()

    def interrupt_running_turns(self) -> int:
        """Restart recovery: a turn that was running may already have changed the simulation,
        so it is marked interrupted and never blindly replayed (design doc §8)."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE turns SET status = 'interrupted', finished_at = ?,"
                " message = CASE WHEN message = '' THEN '服务重启，该任务已中断。' ELSE message END"
                " WHERE status IN ('queued','running')", (_now(),))
            self._db.commit()
            return int(cursor.rowcount)

    # -- events ---------------------------------------------------------------------

    def add_event(self, conversation_id: str, type: str, payload: dict[str, Any] | None = None,
                  turn_id: str | None = None) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO conversation_events (conversation_id, turn_id, type, payload,"
                " created_at) VALUES (?,?,?,?,?)",
                (conversation_id, turn_id, type,
                 json.dumps(payload or {}, ensure_ascii=False), _now()))
            self._db.commit()
            return int(cursor.lastrowid or 0)

    def events(self, conversation_id: str, *, after: int = 0,
               limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM conversation_events WHERE conversation_id = ? AND event_id > ?"
                " ORDER BY event_id LIMIT ?",
                (conversation_id, int(after), max(1, int(limit)))).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try:
                value["payload"] = json.loads(str(value.get("payload") or "{}"))
            except json.JSONDecodeError:
                value["payload"] = {}
            result.append(value)
        return result
