#!/usr/bin/env python3
"""One-off migration: build SQLite {scene,model,robot}_index.db from legacy *_index.json.

Usage: python init_index.py [--data-dir ./data]
Records imported from legacy data are system assets (upload_user_id=0, is_deleted=0).
The optional no_display flag defaults to false.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

LEGACY = {
    "scene": ("scenes/scene_index.json", "场景.自定义"),
    "model": ("models/model_index.json", "模型.自定义"),
    "robot": ("robots/robot_index.json", "机械臂.自定义"),
}


def rows_from_legacy(typ, data_dir):
    json_path = Path(data_dir) / LEGACY[typ][0]
    if not json_path.is_file():
        return None
    data = json.loads(json_path.read_text("utf-8"))
    default_cat = LEGACY[typ][1]
    if isinstance(data, list):  # scenes: [{id, name, category}, ...]
        return [(int(e["id"]), e["name"], 0, e.get("category") or default_cat,
                 int(e.get("no_display", False))) for e in data]
    by_id = data.get("by_id", {})
    category = data.get("category", {})
    no_display = data.get("no_display", {})
    return [(int(k), by_id[k], 0, category.get(k) or default_cat,
             int(no_display.get(k, False))) for k in sorted(by_id, key=int)]


def write_db(typ, data_dir, rows):
    db = Path(data_dir) / f"{typ}_index.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    try:
        con.executescript("""
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                upload_user_id INTEGER NOT NULL DEFAULT 0,
                category VARCHAR(32) NOT NULL,
                is_deleted BOOL NOT NULL DEFAULT 0,
                no_display BOOL NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_assets_name ON assets(name);
        """)
        con.executemany(
            "INSERT INTO assets (id, name, upload_user_id, category, is_deleted, no_display) "
            "VALUES (?, ?, ?, ?, 0, ?)", rows)
        con.commit()
    finally:
        con.close()
    return db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="./data")
    args = parser.parse_args()
    for typ in ("scene", "model", "robot"):
        rows = rows_from_legacy(typ, args.data_dir)
        if rows is None:
            print(f"[init_index] {typ}: legacy JSON index not found, skipped")
            continue
        db = write_db(typ, args.data_dir, rows)
        print(f"[init_index] {typ}: wrote {len(rows)} records -> {db}")
    print("[init_index] done")


if __name__ == "__main__":
    sys.exit(main())
