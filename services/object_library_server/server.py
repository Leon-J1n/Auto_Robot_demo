#!/usr/bin/env python3
"""OLS - ObjectLibraryServer.

Pure-stdlib HTTP server providing scene / model / robot asset data.
Implements the interface contract in docs/OLS接口.md.
"""
import argparse
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ASSET_TYPES = ("scene", "model", "robot")
TYPE_DIR = {"scene": "scenes", "model": "models", "robot": "robots"}
DEFAULT_CATEGORY = {"scene": "场景.自定义", "model": "模型.自定义", "robot": "机械臂.自定义"}
# ids >= USER_ID_BASE are reserved for regular (non-system) users
USER_ID_BASE = 10000

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8000,
    "data_dir": "./data",
    "allowed_extensions": [".obj", ".mtl", ".png", ".jpg", ".jpeg", ".urdf",
                           ".stl", ".dae", ".gltf", ".glb", ".bin"],
    "downloadable_types": ["model", "robot"],
    "cors_allow_origin": "*",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    upload_user_id INTEGER NOT NULL DEFAULT 0,
    category VARCHAR(32) NOT NULL,
    is_deleted BOOL NOT NULL DEFAULT 0,
    no_display BOOL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name);
"""

PACKAGE_RE = re.compile(r"^package://[^/]+/")
DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Q162: these three answer "what exists / what version", so they must always revalidate.
ORACLE_ROUTES = ("/list", "/listCategory", "/assetVersion")
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

EXTRA_MIME = {
    ".obj": "application/octet-stream",
    ".mtl": "text/plain",
    ".urdf": "application/xml",
    ".stl": "model/stl",
    ".dae": "model/vnd.collada+xml",
    ".gltf": "model/gltf+json",
    ".glb": "model/gltf-binary",
}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validate_type(value):
    if value not in ASSET_TYPES:
        raise ApiError(400, "bad_request", f"type must be one of scene|model|robot, got: {value!r}")
    return value


def parse_id(value, name="id"):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", f"{name} must be an integer, got: {value!r}")
    if v < 0:
        raise ApiError(400, "bad_request", f"{name} must be >= 0")
    return v


def parse_uid(value):
    if value is None or value == "":
        return 0
    return parse_id(value, "uid")


def normalize_rel_path(raw):
    """Validate a client-supplied relative path, return normalized '/'-separated parts."""
    if raw is None or raw == "":
        raise ApiError(400, "bad_request", "path must not be empty")
    if "\x00" in raw:
        raise ApiError(403, "forbidden", "path contains null byte")
    rel = raw.replace("\\", "/")
    rel = PACKAGE_RE.sub("", rel, count=1)
    if rel == "" or rel == "/":
        raise ApiError(400, "bad_request", "path must reference a file")
    if rel.startswith("/"):
        raise ApiError(403, "forbidden", "absolute path is not allowed")
    if DRIVE_RE.match(rel):
        raise ApiError(403, "forbidden", "drive-qualified path is not allowed")
    for part in rel.split("/"):
        if part == "..":
            raise ApiError(403, "forbidden", "'..' is not allowed in path")
    return [p for p in rel.split("/") if p not in ("", ".")], rel


def safe_join(base_dir, parts):
    """Join validated relative parts under base_dir, rejecting symlink traversal."""
    cur = Path(base_dir)
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise ApiError(403, "forbidden", "symbolic links are not allowed")
    base_r = os.path.realpath(base_dir)
    target_r = os.path.realpath(cur)
    if target_r != base_r and not target_r.startswith(base_r + os.sep):
        raise ApiError(403, "forbidden", "path escapes asset directory")
    return cur


def guess_content_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def read_version_file(path):
    """Parse a VERSION.txt ('AppVersion|ResVersion <ver>' + 'built at <time>'); return (version, built)."""
    try:
        text = Path(path).read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return "unknown", "unknown"
    version = built = None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(?:app|res)?version\s+(.+)$", line, re.IGNORECASE)
        if m:
            version = m.group(1).strip()
            continue
        m = re.match(r"^built at\s+(.+)$", line, re.IGNORECASE)
        if m:
            built = m.group(1).strip()
    return version or "unknown", built or "unknown"


def ensure_schema(data_dir):
    for typ in ASSET_TYPES:
        con = get_conn(data_dir, typ)
        try:
            con.executescript(SCHEMA)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(assets)")}
            if "no_display" not in columns:
                con.execute("ALTER TABLE assets ADD COLUMN no_display BOOL NOT NULL DEFAULT 0")
            con.commit()
        finally:
            con.close()


def get_conn(data_dir, typ):
    db = Path(data_dir) / f"{typ}_index.db"
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def fetch_one(data_dir, typ, sql, args):
    con = get_conn(data_dir, typ)
    try:
        row = con.execute(sql, args).fetchone()
    finally:
        con.close()
    return row


def fetch_all(data_dir, typ, sql, args):
    con = get_conn(data_dir, typ)
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return rows


def find_visible_by_id(data_dir, typ, rid, uid):
    row = fetch_one(data_dir, typ, "SELECT * FROM assets WHERE id = ?", (rid,))
    if row is None or row["is_deleted"] or row["upload_user_id"] not in (0, uid):
        raise ApiError(404, "not_found", f"{typ} {rid} not found")
    return row


def find_visible_by_name(data_dir, typ, name, uid):
    row = fetch_one(
        data_dir, typ,
        "SELECT * FROM assets WHERE name = ? AND is_deleted = 0 AND upload_user_id IN (0, ?) ORDER BY id LIMIT 1",
        (name, uid))
    if row is None:
        raise ApiError(404, "not_found", f"{typ} named {name!r} not found")
    return row


def next_upload_id(data_dir, typ, uid):
    if uid > 0:
        row = fetch_one(data_dir, typ, "SELECT MAX(id) AS m FROM assets WHERE id >= ?", (USER_ID_BASE,))
        return (row["m"] or USER_ID_BASE - 1) + 1
    row = fetch_one(data_dir, typ, "SELECT MAX(id) AS m FROM assets WHERE id < ?", (USER_ID_BASE,))
    cand = (row["m"] or 0) + 1
    if cand < USER_ID_BASE:
        return cand
    # system pool exhausted: fall back to global max + 1
    row = fetch_one(data_dir, typ, "SELECT MAX(id) AS m FROM assets", ())
    return (row["m"] or 0) + 1


def asset_version(data_dir, typ, row):
    """Q162: 16-hex token over the asset's file stats plus its index row. Stat only, never reads bytes."""
    asset_dir = Path(data_dir) / TYPE_DIR[typ] / str(row["id"])
    entries = []
    for path in asset_dir.rglob("*"):
        if not path.is_file():
            continue
        st = path.stat()
        entries.append((path.relative_to(asset_dir).as_posix(), st.st_size, st.st_mtime_ns))
    entries.sort()
    payload = json.dumps({"name": row["name"], "category": row["category"], "files": entries},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class OLSHandler(BaseHTTPRequestHandler):
    server_version = "OLS/1.0"
    config = None

    protocol_version = "HTTP/1.1"

    cache_version = ""

    # ---- plumbing -------------------------------------------------------

    def data_dir(self):
        return Path(self.config["data_dir"])

    def allowed_exts(self):
        return {e.lower() for e in self.config["allowed_extensions"]}

    def downloadable(self):
        return set(self.config["downloadable_types"])

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", self.config.get("cors_allow_origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _cache_control(self):
        if self.cache_version:
            return IMMUTABLE_CACHE_CONTROL
        return "no-cache"

    def _etag_matches(self, etag):
        header = self.headers.get("If-None-Match")
        if not header:
            return False
        if header.strip() == "*":
            return True
        candidates = [c.strip() for c in header.split(",")]
        return etag in candidates or "W/" + etag in candidates

    def _send_not_modified(self, etag):
        self.send_response(304)
        self._cors_headers()
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", self._cache_control())
        self.end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        etag = '"j-%s"' % hashlib.sha256(body).hexdigest()[:32]
        if status == 200 and self._etag_matches(etag):
            return self._send_not_modified(etag)
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", self._cache_control())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, code, message):
        self._send_json({"error": code, "message": message}, status)

    def _send_file(self, target):
        if not target.is_file():
            raise ApiError(404, "not_found", "file not found")
        st = target.stat()
        etag = '"f-%x-%x"' % (st.st_size, st.st_mtime_ns)
        if self._etag_matches(etag):
            return self._send_not_modified(etag)
        data = target.read_bytes()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", guess_content_type(target.name))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", self._cache_control())
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _merge_form_body(self, qs, body):
        if body and self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            try:
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                return qs
            for k, v in form.items():
                qs.setdefault(k, v)
        return qs

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        table = ROUTES_GET if method == "GET" else ROUTES_POST
        handler_name = table.get(path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        # Q162: v is only a cache key; validating it would cost a directory walk per file request.
        self.cache_version = (qs.get("v", [""])[0]
                              if method == "GET" and path not in ORACLE_ROUTES else "")
        if handler_name is None:
            return self._send_error(404, "not_found", f"unknown endpoint: {method} {parsed.path}")
        try:
            getattr(self, "api_" + handler_name)(qs)
        except ApiError as e:
            try:
                self._send_error(e.status, e.code, e.message)
            except ConnectionError:
                self.close_connection = True
        except ConnectionError:
            # Q163: the client hung up mid-response. Windows raises 10053, which is a
            # sibling of BrokenPipeError, not a subclass - answering 500 on the same dead
            # socket only raises a second time and buries the log in two tracebacks.
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            self._send_error(500, "internal_error", "unexpected server error")

    def handle_one_request(self):
        # Q163: a cancelled request can also die before dispatch (reading the request line
        # or headers) or on a route that writes directly, e.g. OPTIONS. The dispatch guard
        # is still needed on its own: without it the generic handler would try to answer
        # 500 on the dead socket first.
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        path = urlparse(self.path).path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        if path not in ROUTES_GET and path not in ROUTES_POST:
            return self._send_error(404, "not_found", f"unknown endpoint: OPTIONS {urlparse(self.path).path}")
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- param helpers ---------------------------------------------------

    def p(self, qs, key, required=True, default=None):
        vals = qs.get(key)
        if not vals or vals[0] == "":
            if required:
                raise ApiError(400, "bad_request", f"missing parameter: {key}")
            return default
        return vals[0]

    def uid(self, qs):
        return parse_uid(qs.get("uid", [""])[0] if qs.get("uid") else None)

    # ---- query endpoints --------------------------------------------------

    def api_list(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        category = self.p(qs, "category", required=False)
        if category:
            rows = fetch_all(self.data_dir(), typ,
                             "SELECT id, name, category FROM assets "
                             "WHERE is_deleted = 0 AND no_display = 0 AND upload_user_id IN (0, ?) AND category = ? ORDER BY id",
                             (uid, category))
        else:
            rows = fetch_all(self.data_dir(), typ,
                             "SELECT id, name, category FROM assets "
                             "WHERE is_deleted = 0 AND no_display = 0 AND upload_user_id IN (0, ?) ORDER BY id",
                             (uid,))
        self._send_json({"type": typ,
                         "items": [{"id": r["id"], "name": r["name"], "category": r["category"],
                                    "version": asset_version(self.data_dir(), typ, r)} for r in rows]})

    def api_assetVersion(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        rid = parse_id(self.p(qs, "id"))
        row = find_visible_by_id(self.data_dir(), typ, rid, uid)
        self._send_json({"type": typ, "id": rid, "version": asset_version(self.data_dir(), typ, row)})

    def api_listCategory(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        rows = fetch_all(self.data_dir(), typ,
                         "SELECT DISTINCT category FROM assets "
                         "WHERE is_deleted = 0 AND upload_user_id IN (0, ?) ORDER BY category",
                         (uid,))
        self._send_json({"type": typ, "category": [r["category"] for r in rows]})

    def _serve_asset_json(self, typ, row, with_files):
        asset_dir = self.data_dir() / TYPE_DIR[typ] / str(row["id"])
        main_file = asset_dir / f"{row['name']}.json"
        if not main_file.is_file():
            raise ApiError(404, "not_found", f"{typ} data file not found")
        try:
            result = json.loads(main_file.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(500, "internal_error", f"{typ} data file is not valid JSON")
        if with_files:
            manifest = asset_dir / f"{row['name']}_files.json"
            if not manifest.is_file():
                raise ApiError(404, "not_found", f"{typ} files manifest not found")
            try:
                result.update(json.loads(manifest.read_text("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(500, "internal_error", "files manifest is not valid JSON")
        self._send_json(result)

    def api_scene(self, qs):
        uid = self.uid(qs)
        row = find_visible_by_id(self.data_dir(), "scene", parse_id(self.p(qs, "id")), uid)
        self._serve_asset_json("scene", row, with_files=False)

    def api_sceneByName(self, qs):
        uid = self.uid(qs)
        row = find_visible_by_name(self.data_dir(), "scene", self.p(qs, "name"), uid)
        self._serve_asset_json("scene", row, with_files=False)

    def _model_or_robot_by_id(self, typ, qs):
        uid = self.uid(qs)
        row = find_visible_by_id(self.data_dir(), typ, parse_id(self.p(qs, "id")), uid)
        self._serve_asset_json(typ, row, with_files=True)

    def _model_or_robot_by_name(self, typ, qs):
        uid = self.uid(qs)
        row = find_visible_by_name(self.data_dir(), typ, self.p(qs, "name"), uid)
        self._serve_asset_json(typ, row, with_files=True)

    def api_model(self, qs):
        self._model_or_robot_by_id("model", qs)

    def api_modelByName(self, qs):
        self._model_or_robot_by_name("model", qs)

    def api_robot(self, qs):
        self._model_or_robot_by_id("robot", qs)

    def api_robotByName(self, qs):
        self._model_or_robot_by_name("robot", qs)

    # ---- file endpoints ---------------------------------------------------

    def _resolve_asset_file(self, typ, row, raw_path):
        if typ not in self.downloadable():
            raise ApiError(403, "forbidden", f"type {typ!r} is not downloadable")
        parts, rel = normalize_rel_path(raw_path)
        ext = os.path.splitext(rel)[1].lower()
        if ext not in self.allowed_exts():
            raise ApiError(403, "forbidden", f"extension {ext!r} is not in allowed_extensions")
        base_dir = self.data_dir() / TYPE_DIR[typ] / str(row["id"])
        return safe_join(base_dir, parts)

    def api_file(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        row = find_visible_by_id(self.data_dir(), typ, parse_id(self.p(qs, "id")), uid)
        target = self._resolve_asset_file(typ, row, self.p(qs, "path", required=False))
        self._send_file(target)

    def api_fileByName(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        row = find_visible_by_name(self.data_dir(), typ, self.p(qs, "name"), uid)
        target = self._resolve_asset_file(typ, row, self.p(qs, "path", required=False))
        self._send_file(target)

    def api_thumbnail(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        row = find_visible_by_id(self.data_dir(), typ, parse_id(self.p(qs, "id")), uid)
        manifest_file = self.data_dir() / TYPE_DIR[typ] / str(row["id"]) / f"{row['name']}_files.json"
        if not manifest_file.is_file():
            raise ApiError(404, "not_found", "files manifest not found")
        try:
            manifest = json.loads(manifest_file.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(500, "internal_error", "files manifest is not valid JSON")
        thumb = manifest.get("thumbnail")
        if not thumb:
            raise ApiError(404, "not_found", "thumbnail is not registered")
        parts, _ = normalize_rel_path(thumb)
        target = safe_join(self.data_dir() / TYPE_DIR[typ] / str(row["id"]), parts)
        self._send_file(target)

    # ---- write endpoints ---------------------------------------------------

    def api_deleteAsset(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        rid = parse_id(self.p(qs, "id"))
        row = fetch_one(self.data_dir(), typ, "SELECT * FROM assets WHERE id = ?", (rid,))
        if row is None or row["is_deleted"]:
            raise ApiError(404, "not_found", f"{typ} {rid} not found")
        if uid == 0:
            pass  # system admin may delete any record
        elif row["upload_user_id"] == uid:
            pass
        elif row["upload_user_id"] == 0:
            raise ApiError(403, "forbidden", "system assets cannot be deleted by regular users")
        else:
            raise ApiError(404, "not_found", f"{typ} {rid} not found")
        con = get_conn(self.data_dir(), typ)
        try:
            con.execute("UPDATE assets SET is_deleted = 1 WHERE id = ?", (rid,))
            con.commit()
        finally:
            con.close()
        self._send_json({"deleted": True, "type": typ, "id": rid})

    def api_presignUploadID(self, qs):
        body = self._read_body()
        qs = self._merge_form_body(qs, body)
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        name = self.p(qs, "name")
        if ("\x00" in name or "/" in name or "\\" in name
                or name in (".", "..") or DRIVE_RE.match(name)):
            raise ApiError(400, "bad_request", "name must be a plain file-stem without path separators")
        entrance = self.p(qs, "entranceFilePath")
        parts, rel = normalize_rel_path(entrance)
        category = self.p(qs, "category", required=False) or DEFAULT_CATEGORY[typ]

        new_id = next_upload_id(self.data_dir(), typ, uid)

        con = get_conn(self.data_dir(), typ)
        try:
            con.execute("INSERT INTO assets (id, name, upload_user_id, category, is_deleted) VALUES (?, ?, ?, ?, 0)",
                        (new_id, name, uid, category))
            con.commit()
        finally:
            con.close()

        asset_dir = self.data_dir() / TYPE_DIR[typ] / str(new_id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"is_on_cos": False, "path": rel}
        (asset_dir / f"{name}_files.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
        self._send_json({"type": typ, "id": new_id, "name": name})

    def api_uploadFile(self, qs):
        typ = validate_type(self.p(qs, "type"))
        uid = self.uid(qs)
        rid = parse_id(self.p(qs, "id"))
        raw_path = self.p(qs, "path")
        role = (self.p(qs, "role", required=False) or "reference").lower()
        if role not in ("metadata", "thumbnail", "reference"):
            raise ApiError(400, "bad_request", "role must be one of metadata|thumbnail|reference")

        row = fetch_one(self.data_dir(), typ, "SELECT * FROM assets WHERE id = ?", (rid,))
        if row is None or row["is_deleted"]:
            raise ApiError(404, "not_found", f"{typ} {rid} not found")
        if row["upload_user_id"] != uid:
            raise ApiError(403, "forbidden", "uid does not match the asset upload_user_id")

        parts, rel = normalize_rel_path(raw_path)
        if role == "metadata" and typ == "scene":
            raise ApiError(400, "bad_request", "scene manifest has no metadata field")

        content = self._read_body()
        asset_dir = self.data_dir() / TYPE_DIR[typ] / str(rid)
        asset_dir.mkdir(parents=True, exist_ok=True)
        target = safe_join(asset_dir, parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        manifest_file = asset_dir / f"{row['name']}_files.json"
        try:
            manifest = json.loads(manifest_file.read_text("utf-8")) if manifest_file.is_file() else {"is_on_cos": False}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(500, "internal_error", "files manifest is not valid JSON")

        entrance = manifest.get("path")
        if role == "thumbnail":
            manifest["thumbnail"] = rel
        elif role == "metadata":
            manifest["metadata"] = rel
        else:  # reference
            if rel != entrance and typ in ("model", "robot"):
                refs = manifest.setdefault("reference", [])
                if rel not in refs:
                    refs.append(rel)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")

        self._send_json({"uploaded": True, "type": typ, "id": rid, "path": rel, "role": role})


ROUTES_GET = {
    "/list": "list",
    "/listCategory": "listCategory",
    "/assetVersion": "assetVersion",
    "/scene": "scene",
    "/sceneByName": "sceneByName",
    "/model": "model",
    "/modelByName": "modelByName",
    "/robot": "robot",
    "/robotByName": "robotByName",
    "/file": "file",
    "/fileByName": "fileByName",
    "/thumbnail": "thumbnail",
}
ROUTES_POST = {
    "/deleteAsset": "deleteAsset",
    "/presignUploadID": "presignUploadID",
    "/uploadFile": "uploadFile",
}


def load_config(args):
    cfg = dict(DEFAULT_CONFIG)
    candidates = []
    if args.config:
        candidates.append(args.config)
        candidates.append("./config.json")
    else:
        candidates.append("./config.json")
    path = next((c for c in candidates if os.path.isfile(c)), None)
    if path is None:
        sys.stderr.write(f"[OLS] config file not found (tried: {', '.join(candidates)})\n")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[OLS] config file {path} is not valid JSON: {e}\n")
        sys.exit(1)
    if not isinstance(loaded, dict):
        sys.stderr.write(f"[OLS] config file {path} must contain a JSON object\n")
        sys.exit(1)
    cfg.update(loaded)
    if args.host:
        cfg["host"] = args.host
    if args.port is not None:
        cfg["port"] = int(args.port)
    if args.data_dir:
        cfg["data_dir"] = args.data_dir
    return cfg


def make_server(config):
    class BoundHandler(OLSHandler):
        pass
    BoundHandler.config = config
    ensure_schema(config["data_dir"])
    return ThreadingHTTPServer((config["host"], int(config["port"])), BoundHandler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="OLS - ObjectLibraryServer")
    parser.add_argument("--host", help="bind host (default: 127.0.0.1 or config value)")
    parser.add_argument("--port", type=int, help="bind port (default: 8000 or config value)")
    parser.add_argument("--data-dir", dest="data_dir", help="data directory (default: ./data)")
    parser.add_argument("--config", default=None, help="config file path (default: ./config.json)")
    args = parser.parse_args(argv)

    config = load_config(args)
    Path(config["data_dir"]).mkdir(parents=True, exist_ok=True)
    code_ver, code_built = read_version_file(Path(__file__).resolve().parent / "VERSION.txt")
    res_ver, res_built = read_version_file(Path(config["data_dir"]) / "VERSION.txt")
    print(f"[OLS] code version: {code_ver} (built at {code_built})")
    print(f"[OLS] resource version: {res_ver} (built at {res_built})")
    httpd = make_server(config)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(f"[OLS] ObjectLibraryServer listening on http://{host}:{port} "
          f"(data_dir={Path(config['data_dir']).resolve()})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
