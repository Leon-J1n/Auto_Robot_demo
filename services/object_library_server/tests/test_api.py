#!/usr/bin/env python3
"""End-to-end tests for OLS (ObjectLibraryServer).

Builds a temporary data directory with a small fixture library, starts the
real HTTP server on an ephemeral port, and exercises every endpoint through
urllib. Run with:  python tests/test_api.py
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
import init_index  # noqa: E402


def http(method, url, body=None):
    req = Request(url, data=body, method=method)
    try:
        with urlopen(req) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except HTTPError as e:
        return e.code, e.read(), (e.headers.get("Content-Type", "") if e.headers else "")


def http_with_headers(method, url, headers=None):
    req = Request(url, method=method, headers=headers or {})
    try:
        with urlopen(req) as resp:
            return resp.status, resp.read(), resp.headers
    except HTTPError as e:
        return e.code, e.read(), e.headers


class APITestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ols_test_")
        cls.data = Path(cls.tmp) / "data"
        (cls.data / "scenes" / "1").mkdir(parents=True)
        (cls.data / "models" / "1").mkdir(parents=True)
        (cls.data / "models" / "2").mkdir(parents=True)
        (cls.data / "robots" / "1").mkdir(parents=True)
        cls.data_dir = str(cls.data)
        server.ensure_schema(cls.data)
        cls.insert("scene", (1, "ur5e_table_empty", "场景.空白"))
        cls.insert("model", (1, "apple", "模型.抓取物"))
        cls.insert("model", (2, "basket", "模型.容器"))
        cls.insert("robot", (1, "ur5e_arm", "机械臂.UR5"))

        sn = "ur5e_table_empty"
        (cls.data / "scenes" / "1" / f"{sn}.json").write_text(
            json.dumps({"scene_id": 1, "scene_name": sn}), "utf-8")
        (cls.data / "scenes" / "1" / f"{sn}_files.json").write_text(
            json.dumps({"is_on_cos": False, "path": f"{sn}.json", "thumbnail": f"{sn}_thumbnail.png"}), "utf-8")
        (cls.data / "scenes" / "1" / f"{sn}_thumbnail.png").write_bytes(b"\x89PNG-scene")

        (cls.data / "models" / "1" / "apple.json").write_text(
            json.dumps({"semantic_properties": {"object_id": 1, "object_name": "apple"},
                        "grasp_properties": {"pose_count": 1}}), "utf-8")
        (cls.data / "models" / "1" / "apple_files.json").write_text(
            json.dumps({"is_on_cos": False, "metadata": "apple.json", "path": "apple.obj",
                        "thumbnail": "apple_thumbnail.png", "reference": ["apple.mtl"]}), "utf-8")
        (cls.data / "models" / "1" / "apple.obj").write_bytes(b"v 0 0 0\n")
        (cls.data / "models" / "1" / "apple.mtl").write_bytes(b"newmtl m0\n")
        (cls.data / "models" / "1" / "apple_thumbnail.png").write_bytes(b"\x89PNG-apple")
        (cls.data / "models" / "1" / "secret.txt").write_text("no", "utf-8")
        (cls.data / "models" / "2" / "basket.json").write_text(
            json.dumps({"semantic_properties": {"object_id": 2, "object_name": "basket"}}), "utf-8")
        (cls.data / "models" / "2" / "basket_files.json").write_text(
            json.dumps({"is_on_cos": False, "path": "basket.obj"}), "utf-8")

        (cls.data / "robots" / "1" / "ur5e_arm.urdf").write_text('<robot name="ur5e"/>\n', "utf-8")
        (cls.data / "robots" / "1" / "ur5e_arm.json").write_text(
            json.dumps({"semantic_properties": {"object_id": 1, "object_name": "ur5e_arm"}}), "utf-8")
        (cls.data / "robots" / "1" / "ur5e_arm_files.json").write_text(
            json.dumps({"is_on_cos": False, "metadata": "ur5e_arm.json", "path": "ur5e_arm.urdf",
                        "thumbnail": "ur5e_arm_thumbnail.png"}), "utf-8")

        cls.config = {
            "host": "127.0.0.1", "port": 0, "data_dir": cls.data_dir,
            "allowed_extensions": [".obj", ".mtl", ".png", ".urdf", ".json"],
            "downloadable_types": ["model", "robot"],
        }
        cls.httpd = server.make_server(cls.config)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def insert(cls, typ, row):
        """row = (id, name, category); imported fixtures are system assets."""
        con = sqlite3.connect(str(Path(cls.data_dir) / f"{typ}_index.db"))
        try:
            con.execute("INSERT INTO assets (id, name, upload_user_id, category) VALUES (?,?,?,?)",
                        (row[0], row[1], 0, row[2]))
            con.commit()
        finally:
            con.close()

    # ---- request helpers ----

    def url(self, endpoint, **params):
        return f"http://127.0.0.1:{self.port}{endpoint}?" + urlencode(params)

    def get(self, endpoint, **params):
        return http("GET", self.url(endpoint, **params))

    def get_with_headers(self, endpoint, headers=None, **params):
        return http_with_headers("GET", self.url(endpoint, **params), headers=headers)

    def get_json(self, endpoint, **params):
        status, body, ctype = self.get(endpoint, **params)
        return status, json.loads(body) if body else None, ctype

    def post(self, endpoint, body=b"", **params):
        status, resp, ctype = http("POST", self.url(endpoint, **params), body=body)
        return status, json.loads(resp) if resp else None, ctype

    def presign(self, typ, name, entrance, uid=None, category=None):
        params = {"type": typ, "name": name, "entranceFilePath": entrance}
        if uid is not None:
            params["uid"] = uid
        if category is not None:
            params["category"] = category
        status, parsed, _ = self.post("/presignUploadID", **params)
        self.assertEqual(status, 200, parsed)
        return parsed["id"]

    def upload(self, typ, rid, path, content, uid, role=None):
        params = {"type": typ, "id": rid, "path": path, "uid": uid}
        if role is not None:
            params["role"] = role
        return self.post("/uploadFile", body=content, **params)[:2]

    def max_id(self, typ):
        status, parsed, _ = self.get_json("/list", type=typ)
        self.assertEqual(status, 200)
        return max((i["id"] for i in parsed["items"]), default=0)


class TestQueryEndpoints(APITestCase):
    def test_list_model(self):
        status, data, ctype = self.get_json("/list", type="model")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("application/json"))
        self.assertEqual(data["type"], "model")
        self.assertEqual([i["id"] for i in data["items"]], [1, 2])
        self.assertEqual({k: v for k, v in data["items"][0].items() if k != "version"},
                         {"id": 1, "name": "apple", "category": "模型.抓取物"})

    def test_list_category_filter(self):
        _, data, __ = self.get_json("/list", type="model", category="模型.容器")
        self.assertEqual([i["id"] for i in data["items"]], [2])

    def test_no_display_hides_only_from_list(self):
        cases = (("scene", "/scene", "ur5e_table_empty", "/sceneByName"),
                 ("model", "/model", "apple", "/modelByName"),
                 ("robot", "/robot", "ur5e_arm", "/robotByName"))
        for typ, by_id, name, by_name in cases:
            with self.subTest(type=typ):
                with closing(server.get_conn(self.data, typ)) as con, con:
                    con.execute("UPDATE assets SET no_display = 1 WHERE id = 1")
                try:
                    self.assertNotIn(1, [item["id"] for item in
                                     self.get_json("/list", type=typ)[1]["items"]])
                    self.assertNotIn(1, [item["id"] for item in
                                     self.get_json("/list", type=typ,
                                                   category=self._category(typ))[1]["items"]])
                    self.assertEqual(self.get_json(by_id, id=1)[0], 200)
                    self.assertEqual(self.get_json(by_name, name=name)[0], 200)
                finally:
                    with closing(server.get_conn(self.data, typ)) as con, con:
                        con.execute("UPDATE assets SET no_display = 0 WHERE id = 1")

    @staticmethod
    def _category(typ):
        return {"scene": "场景.空白", "model": "模型.抓取物", "robot": "机械臂.UR5"}[typ]

    def test_list_bad_type(self):
        status, data, _ = self.get_json("/list", type="car")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "bad_request")

    def test_listCategory(self):
        _, data, _ = self.get_json("/listCategory", type="robot")
        self.assertEqual(data["category"], ["机械臂.UR5"])

    def test_scene_by_id(self):
        status, data, _ = self.get_json("/scene", id=1)
        self.assertEqual(status, 200)
        self.assertEqual(data["scene_name"], "ur5e_table_empty")
        self.assertNotIn("is_on_cos", data)  # scenes return the entry file only

    def test_scene_by_name(self):
        _, data, _ = self.get_json("/sceneByName", name="ur5e_table_empty")
        self.assertEqual(data["scene_id"], 1)

    def test_scene_unknown_name_is_404(self):
        status, _, _ = self.get("/sceneByName", name="does_not_exist")
        self.assertEqual(status, 404)

    def test_model_merges_manifest(self):
        status, data, _ = self.get_json("/model", id=1)
        self.assertEqual(status, 200)
        self.assertEqual(data["semantic_properties"]["object_name"], "apple")
        self.assertEqual(data["path"], "apple.obj")
        self.assertEqual(data["reference"], ["apple.mtl"])

    def test_model_by_name(self):
        _, data, _ = self.get_json("/modelByName", name="apple")
        self.assertEqual(data["semantic_properties"]["object_id"], 1)

    def test_robot_by_name(self):
        _, data, _ = self.get_json("/robotByName", name="ur5e_arm")
        self.assertEqual(data["path"], "ur5e_arm.urdf")
        self.assertEqual(data["metadata"], "ur5e_arm.json")

    def test_bad_id_and_uid_params(self):
        for bad in ({"id": "abc"}, {"id": "-3"}, {"uid": "x"}, {"uid": "-1"}):
            params = {"type": "model", "id": "1", "uid": "0"}
            params.update({k: str(v) for k, v in bad.items()})
            if "id" in bad:
                params["id"] = str(bad["id"])
            else:
                params["uid"] = str(bad["uid"])
            status, data, _ = self.get_json("/model", **params)
            self.assertEqual(status, 400, bad)
            self.assertEqual(data["error"], "bad_request")

    def test_missing_params(self):
        status, _, _ = self.get("/model")
        self.assertEqual(status, 400)
        status, _, _ = self.get("/sceneByName")
        self.assertEqual(status, 400)

    def test_unknown_endpoint(self):
        status, data, _ = self.get_json("/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "not_found")

    def test_cors_headers(self):
        with urlopen(Request(self.url("/list", type="model"))) as resp:
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
            self.assertIn("POST", resp.headers["Access-Control-Allow-Methods"])
        # error responses carry CORS headers too
        try:
            urlopen(Request(self.url("/model", id="abc")))
            self.fail("expected HTTPError")
        except HTTPError as e:
            self.assertEqual(e.headers["Access-Control-Allow-Origin"], "*")

    def test_cors_preflight(self):
        req = Request(self.url("/file", type="model", id=1, path="apple.obj"), method="OPTIONS")
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(resp.headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")
        req = Request(self.url("/nope"), method="OPTIONS")
        try:
            urlopen(req)
            self.fail("expected HTTPError")
        except HTTPError as e:
            self.assertEqual(e.code, 404)


class TestFileEndpoints(APITestCase):
    def test_file_ok(self):
        status, body, ctype = self.get("/file", type="model", id=1, path="apple.obj")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"v 0 0 0\n")
        self.assertEqual(ctype, "application/octet-stream")

    def test_file_mtl_content_type(self):
        _, _, ctype = self.get("/file", type="model", id=1, path="apple.mtl")
        self.assertEqual(ctype, "text/plain")

    def test_file_package_prefix(self):
        status, body, _ = self.get("/file", type="robot", id=1, path="package://ur5e_arm/ur5e_arm.urdf")
        self.assertEqual(status, 200)
        self.assertIn("<robot", body.decode())

    def test_file_by_name(self):
        status, body, _ = self.get("/fileByName", type="model", name="apple", path="apple.mtl")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"newmtl m0\n")

    def test_traversal_rejected(self):
        status, data, _ = self.get_json("/file", type="model", id=1, path="../2/basket.json")
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "forbidden")

    def test_absolute_rejected(self):
        for bad in ("/etc/passwd", "C:/Windows/win.ini", "\\\\server\\share\\x.obj"):
            status, _, _ = self.get("/file", type="model", id=1, path=bad)
            self.assertEqual(status, 403, bad)

    def test_null_byte_rejected(self):
        status, _, _ = self.get("/file", type="model", id=1, path="apple.obj\x00.png")
        self.assertEqual(status, 403)

    def test_extension_not_allowed(self):
        status, data, _ = self.get_json("/file", type="model", id=1, path="secret.txt")
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "forbidden")

    def test_missing_path_param(self):
        status, data, _ = self.get_json("/file", type="model", id=1)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "bad_request")

    def test_missing_file(self):
        status, _, _ = self.get("/file", type="model", id=1, path="nope.png")
        self.assertEqual(status, 404)

    def test_scene_not_downloadable(self):
        status, data, _ = self.get_json("/file", type="scene", id=1, path="ur5e_table_empty.json")
        self.assertEqual(status, 403)

    def test_invisible_id_rejected(self):
        status, _, _ = self.get("/file", type="model", id=999, path="apple.obj")
        self.assertEqual(status, 404)

    def test_thumbnail(self):
        for typ, ident, expect in (("model", 1, b"\x89PNG-apple"), ("scene", 1, b"\x89PNG-scene")):
            status, body, ctype = self.get("/thumbnail", type=typ, id=ident)
            self.assertEqual(status, 200, typ)
            self.assertEqual(body, expect)
            self.assertEqual(ctype, "image/png")

    def test_thumbnail_missing_field(self):
        status, _, _ = self.get("/thumbnail", type="model", id=2)
        self.assertEqual(status, 404)


class TestUidIsolation(APITestCase):
    def test_cross_user_invisibility(self):
        rid = self.presign("model", "cube", "cube.obj", uid=7)
        self.assertEqual(rid, 10000)  # user segment starts at 10000

        ids = lambda uid: [i["id"] for i in self.get_json("/list", type="model", uid=uid)[1]["items"]]
        self.assertIn(rid, ids(7))
        self.assertNotIn(rid, ids(8))
        self.assertNotIn(rid, ids(0))

        # upload the metadata file so /model can resolve, as uid=7
        meta = json.dumps({"semantic_properties": {"object_id": rid, "object_name": "cube"}}).encode()
        st, _ = self.upload("model", rid, "cube.json", meta, 7, role="metadata")
        self.assertEqual(st, 200)

        self.assertEqual(self.get_json("/model", id=rid, uid=7)[0], 200)
        self.assertEqual(self.get_json("/model", id=rid, uid=8)[0], 404)
        self.assertEqual(self.get_json("/modelByName", name="cube", uid=8)[0], 404)
        self.assertEqual(self.get_json("/thumbnail", type="model", id=rid, uid=8)[0], 404)

    def test_upload_owner_mismatch_403(self):
        rid = self.presign("model", "lock", "lock.obj", uid=7)
        st, _ = self.upload("model", rid, "lock.obj", b"v 0 0 0\n", uid=8)
        self.assertEqual(st, 403)
        st, _ = self.upload("model", rid, "lock.obj", b"v 0 0 0\n", uid=7)
        self.assertEqual(st, 200)

    def test_uid_defaults_to_zero(self):
        rid = self.presign("model", "sysmod", "sysmod.obj")
        ids0 = [i["id"] for i in self.get_json("/list", type="model")[1]["items"]]
        self.assertIn(rid, ids0)
        st, _ = self.upload("model", rid, "sysmod.obj", b"v 0 0 0\n", uid=0)
        self.assertEqual(st, 200)

    def test_system_assets_visible_to_all(self):
        for uid in (0, 7, 8):
            self.assertEqual(self.get_json("/model", id=1, uid=uid)[0], 200)


class TestUploadFlow(APITestCase):
    def test_full_upload_and_manifest(self):
        rid = self.presign("model", "toy", "toy.obj", uid=5, category="模型.玩具")
        d = self.data / "models" / str(rid)
        self.assertEqual(json.loads((d / "toy_files.json").read_text("utf-8")),
                         {"is_on_cos": False, "path": "toy.obj"})

        st, _ = self.upload("model", rid, "toy.obj", b"v 0 0 0\n", 5)
        self.assertEqual(st, 200)
        st, parsed = self.upload("model", rid, "toy.mtl", b"newmtl a\n", 5, role="reference")
        self.assertEqual(st, 200)
        st, _ = self.upload("model", rid, "toy.json",
                            json.dumps({"semantic_properties": {"object_id": rid, "object_name": "toy"}}).encode(),
                            5, role="metadata")
        self.assertEqual(st, 200)
        st, _ = self.upload("model", rid, "toy_thumbnail.png", b"\x89PNG-toy", 5, role="thumbnail")
        self.assertEqual(st, 200)
        st, _ = self.upload("model", rid, "meshes/part.obj", b"v 1 1 1\n", 5, role="reference")
        self.assertEqual(st, 200)

        manifest = json.loads((d / "toy_files.json").read_text("utf-8"))
        self.assertEqual(manifest["metadata"], "toy.json")
        self.assertEqual(manifest["thumbnail"], "toy_thumbnail.png")
        self.assertIn("toy.mtl", manifest["reference"])
        self.assertIn("meshes/part.obj", manifest["reference"])

        _, data, _ = self.get_json("/modelByName", name="toy", uid=5)
        self.assertEqual(data["semantic_properties"]["object_name"], "toy")
        status, body, _ = self.get("/file", type="model", id=rid, path="meshes/part.obj", uid=5)
        self.assertEqual((status, body), (200, b"v 1 1 1\n"))
        status, body, _ = self.get("/thumbnail", type="model", id=rid, uid=5)
        self.assertEqual((status, body), (200, b"\x89PNG-toy"))
        cats = self.get_json("/listCategory", type="model", uid=5)[1]["category"]
        self.assertIn("模型.玩具", cats)

    def test_default_category(self):
        rid = self.presign("model", "plain", "plain.obj", uid=5)
        items = self.get_json("/list", type="model", uid=5)[1]["items"]
        self.assertEqual([i["category"] for i in items if i["id"] == rid], ["模型.自定义"])
        rid2 = self.presign("scene", "splain", "splain.json", uid=5)
        items2 = self.get_json("/list", type="scene", uid=5)[1]["items"]
        self.assertEqual([i["category"] for i in items2 if i["id"] == rid2], ["场景.自定义"])

    def test_scene_upload_rules(self):
        rid = self.presign("scene", "myscene", "myscene.json", uid=6)
        st, data = self.upload("scene", rid, "myscene.json", b"{}", 6, role="metadata")
        self.assertEqual((st, data["error"]), (400, "bad_request"))  # scene manifests have no metadata field
        st, _ = self.upload("scene", rid, "myscene.png", b"\x89PNG-scene", 6, role="thumbnail")
        self.assertEqual(st, 200)
        st, _ = self.upload("scene", rid, "extra.json", b"{}", 6, role="reference")
        self.assertEqual(st, 200)
        manifest = json.loads((self.data / "scenes" / str(rid) / "myscene_files.json").read_text("utf-8"))
        self.assertEqual(manifest["thumbnail"], "myscene.png")
        self.assertNotIn("reference", manifest)  # scene manifests stay simplified

    def test_presign_rejects_bad_name(self):
        for bad in ("a/b", "a\\b", "..", ""):
            params = {"type": "model", "name": bad, "entranceFilePath": "x.obj", "uid": 5}
            st, parsed, _ = self.post("/presignUploadID", **params)
            self.assertEqual(st, 400, bad)

    def test_upload_unknown_id_and_bad_role(self):
        st, parsed = self.upload("model", 99999, "apple.obj", b"x", 0)
        self.assertEqual((st, parsed["error"]), (404, "not_found"))
        st, parsed = self.upload("model", 1, "apple_extra.mtl", b"x", 0, role="bogus")
        self.assertEqual((st, parsed["error"]), (400, "bad_request"))
        st, parsed = self.upload("model", 1, "../escape.obj", b"x", 0)
        self.assertEqual((st, parsed["error"]), (403, "forbidden"))

    def test_entrance_upload_does_not_duplicate_reference(self):
        rid = self.presign("robot", "solo", "urdf/solo.urdf", uid=5)
        st, _ = self.upload("robot", rid, "urdf/solo.urdf", b"<robot name='solo'/>", 5)
        self.assertEqual(st, 200)
        manifest = json.loads((self.data / "robots" / str(rid) / "solo_files.json").read_text("utf-8"))
        self.assertEqual(manifest, {"is_on_cos": False, "path": "urdf/solo.urdf"})


class TestIdAllocation(APITestCase):
    """id 分区规则：uid>0 从 10000 起累加；uid=0 在 10000 以下累加，用尽后回退全表最大+1。"""

    BASE_ROWS = {
        "scene": [(1, "ur5e_table_empty", "场景.空白")],
        "model": [(1, "apple", "模型.抓取物"), (2, "basket", "模型.容器")],
        "robot": [(1, "ur5e_arm", "机械臂.UR5")],
    }

    def setUp(self):
        for typ, rows in self.BASE_ROWS.items():
            con = sqlite3.connect(str(Path(self.data_dir) / f"{typ}_index.db"))
            try:
                con.execute("DELETE FROM assets")
                con.executemany(
                    "INSERT INTO assets (id, name, upload_user_id, category) VALUES (?,?,0,?)", rows)
                con.commit()
            finally:
                con.close()

    def test_user_ids_start_at_10000_and_increment(self):
        r1 = self.presign("model", "u1", "u1.obj", uid=7)
        r2 = self.presign("model", "u2", "u2.obj", uid=7)
        r3 = self.presign("model", "u3", "u3.obj", uid=8)  # 不同用户共用同一保留区
        self.assertEqual((r1, r2, r3), (10000, 10001, 10002))

    def test_system_ids_below_10000(self):
        r1 = self.presign("model", "s1", "s1.obj")
        r2 = self.presign("model", "s2", "s2.obj")
        self.assertEqual((r1, r2), (3, 4))  # 系统区从 1,2 之后累加
        ru = self.presign("model", "u1", "u1.obj", uid=9)
        self.assertEqual(ru, 10000)  # 与系统区互不干扰

    def test_system_pool_exhausted_falls_back(self):
        self.insert("model", (9999, "edge", "模型.抓取物"))
        r = self.presign("model", "s1", "s1.obj")
        self.assertEqual(r, 10000)  # max(id<10000)+1=10000 触发回退：全表最大(9999)+1
        self.insert("model", (10001, "occupied", "模型.抓取物"))
        r2 = self.presign("model", "s2", "s2.obj")
        self.assertEqual(r2, 10002)  # 回退取全表最大(10001)+1

    def test_deleted_user_ids_not_reused(self):
        r1 = self.presign("model", "gone", "gone.obj", uid=7)
        st = self.post("/deleteAsset", type="model", id=r1, uid=7)[0]
        self.assertEqual(st, 200)
        r2 = self.presign("model", "again", "again.obj", uid=7)
        self.assertEqual(r2, 10001)

    def test_segments_are_per_type(self):
        rs = self.presign("scene", "myscene", "myscene.json", uid=7)
        rm = self.presign("model", "mymod", "mymod.obj", uid=7)
        rr = self.presign("robot", "myarm", "myarm.urdf", uid=7)
        self.assertEqual((rs, rm, rr), (10000, 10000, 10000))  # 各类型独立计数


class TestDeleteAsset(APITestCase):
    def test_soft_delete_and_visibility(self):
        rid = self.presign("model", "gone", "gone.obj", uid=3)
        st, parsed = self.post("/deleteAsset", type="model", id=1, uid=3)[:2]
        self.assertEqual(st, 403)  # system asset cannot be deleted by a regular user
        st, parsed, _ = self.post("/deleteAsset", type="model", id=rid, uid=3)
        self.assertEqual((st, parsed), (200, {"deleted": True, "type": "model", "id": rid}))
        self.assertEqual(self.get_json("/model", id=rid, uid=3)[0], 404)
        ids3 = [i["id"] for i in self.get_json("/list", type="model", uid=3)[1]["items"]]
        self.assertNotIn(rid, ids3)
        # id is never reused
        rid2 = self.presign("model", "after", "after.obj", uid=3)
        self.assertGreater(rid2, rid)
        # unknown -> 404; other user's -> 404 (invisible)
        st, _ = self.post("/deleteAsset", type="model", id=99999, uid=3)[:2]
        self.assertEqual(st, 404)
        st, _ = self.post("/deleteAsset", type="model", id=rid2, uid=4)[:2]
        self.assertEqual(st, 404)
        # files stay on disk (soft delete)
        self.assertTrue((self.data / "models" / str(rid) / "gone_files.json").is_file())

    def test_admin_uid0_can_delete_any(self):
        rid = self.presign("robot", "owned", "owned.urdf", uid=8)
        st, parsed, _ = self.post("/deleteAsset", type="robot", id=rid, uid=0)
        self.assertEqual(parsed["deleted"], True)

    def test_delete_twice_is_404(self):
        rid = self.presign("model", "twice", "twice.obj", uid=3)
        self.assertEqual(self.post("/deleteAsset", type="model", id=rid, uid=3)[0], 200)
        st, _ = self.post("/deleteAsset", type="model", id=rid, uid=3)[:2]
        self.assertEqual(st, 404)


class TestCaching(APITestCase):
    """Q162: ETag/304 on every GET, v decides immutable, three oracle routes always revalidate."""

    def probe_asset(self, uid=0):
        rid = self.presign("model", f"probe{self.max_id('model') + 1}", "probe.obj", uid=uid)
        self.assertEqual(self.upload("model", rid, "probe.obj", b"v 0 0 0\n", uid)[0], 200)
        return rid

    def test_json_etag_round_trip(self):
        for endpoint, params in (("/model", {"id": 1}), ("/robot", {"id": 1}), ("/scene", {"id": 1}),
                                 ("/modelByName", {"name": "apple"}),
                                 ("/list", {"type": "model"}), ("/listCategory", {"type": "model"}),
                                 ("/assetVersion", {"type": "model", "id": 1})):
            with self.subTest(endpoint=endpoint):
                status, body, headers = self.get_with_headers(endpoint, **params)
                self.assertEqual(status, 200)
                etag = headers.get("ETag")
                self.assertTrue(etag)
                status, empty, headers = self.get_with_headers(
                    endpoint, headers={"If-None-Match": etag}, **params)
                self.assertEqual(status, 304)
                self.assertEqual(empty, b"")
                self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
                self.assertIsNone(headers.get("Content-Length"))
                self.assertEqual(headers.get("ETag"), etag)
                status, again, _ = self.get_with_headers(
                    endpoint, headers={"If-None-Match": '"stale"'}, **params)
                self.assertEqual((status, again), (200, body))

    def test_file_and_thumbnail_etag_round_trip(self):
        for endpoint, params in (("/file", {"type": "model", "id": 1, "path": "apple.obj"}),
                                 ("/fileByName", {"type": "model", "name": "apple", "path": "apple.obj"}),
                                 ("/thumbnail", {"type": "model", "id": 1})):
            with self.subTest(endpoint=endpoint):
                status, body, headers = self.get_with_headers(endpoint, **params)
                self.assertEqual(status, 200)
                etag = headers.get("ETag")
                status, empty, headers = self.get_with_headers(
                    endpoint, headers={"If-None-Match": etag}, **params)
                self.assertEqual((status, empty), (304, b""))
                self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
                self.assertIsNone(headers.get("Content-Length"))
                status, again, _ = self.get_with_headers(
                    endpoint, headers={"If-None-Match": '"stale"'}, **params)
                self.assertEqual((status, again), (200, body))

    def test_file_etag_follows_the_bytes_on_disk(self):
        rid = self.probe_asset()
        params = {"type": "model", "id": rid, "path": "probe.obj"}
        _, _, headers = self.get_with_headers("/file", **params)
        first = headers.get("ETag")
        target = self.data / "models" / str(rid) / "probe.obj"
        target.write_bytes(b"v 1 1 1\nv 2 2 2\n")
        status, body, headers = self.get_with_headers("/file", **params)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("ETag"), first)
        self.assertEqual(body, b"v 1 1 1\nv 2 2 2\n")

    def test_v_decides_cache_strength(self):
        cases = (("/model", {"id": 1}), ("/robot", {"id": 1}), ("/scene", {"id": 1}),
                 ("/modelByName", {"name": "apple"}),
                 ("/file", {"type": "model", "id": 1, "path": "apple.obj"}),
                 ("/fileByName", {"type": "model", "name": "apple", "path": "apple.obj"}),
                 ("/thumbnail", {"type": "model", "id": 1}))
        for endpoint, params in cases:
            with self.subTest(endpoint=endpoint):
                _, _, plain = self.get_with_headers(endpoint, **params)
                self.assertEqual(plain.get("Cache-Control"), "no-cache")
                status, _, tagged = self.get_with_headers(endpoint, v="deadbeefdeadbeef", **params)
                self.assertEqual(status, 200)
                self.assertEqual(tagged.get("Cache-Control"), "public, max-age=31536000, immutable")

    def test_304_keeps_the_cache_strength_of_its_request(self):
        params = {"type": "model", "id": 1, "path": "apple.obj"}
        _, _, headers = self.get_with_headers("/file", v="deadbeefdeadbeef", **params)
        status, _, again = self.get_with_headers(
            "/file", v="deadbeefdeadbeef", headers={"If-None-Match": headers.get("ETag")}, **params)
        self.assertEqual(status, 304)
        self.assertEqual(again.get("Cache-Control"), "public, max-age=31536000, immutable")

    def test_oracle_routes_never_go_immutable(self):
        for endpoint, params in (("/list", {"type": "model"}), ("/listCategory", {"type": "model"}),
                                 ("/assetVersion", {"type": "model", "id": 1})):
            with self.subTest(endpoint=endpoint):
                _, _, headers = self.get_with_headers(endpoint, v="deadbeefdeadbeef", **params)
                self.assertEqual(headers.get("Cache-Control"), "no-cache")

    def test_v_is_a_cache_key_not_a_credential(self):
        params = {"type": "model", "id": 1, "path": "apple.obj"}
        status, body, _ = self.get_with_headers("/file", v="not-the-real-version", **params)
        self.assertEqual((status, body), (200, b"v 0 0 0\n"))

    def test_assetVersion_agrees_with_list(self):
        for typ in ("scene", "model", "robot"):
            with self.subTest(type=typ):
                items = self.get_json("/list", type=typ)[1]["items"]
                self.assertTrue(items)
                for item in items:
                    status, data, _ = self.get_json("/assetVersion", type=typ, id=item["id"])
                    self.assertEqual(status, 200)
                    self.assertEqual(data["type"], typ)
                    self.assertEqual(data["id"], item["id"])
                    self.assertRegex(data["version"], r"^[0-9a-f]{16}$")
                    self.assertEqual(data["version"], item["version"])

    def test_assetVersion_errors_and_uid_visibility(self):
        self.assertEqual(self.get_json("/assetVersion", type="car", id=1)[0], 400)
        self.assertEqual(self.get_json("/assetVersion", type="model", id="abc")[0], 400)
        self.assertEqual(self.get_json("/assetVersion", type="model")[0], 400)
        self.assertEqual(self.get_json("/assetVersion", type="model", id=99999)[0], 404)
        rid = self.probe_asset(uid=7)
        self.assertEqual(self.get_json("/assetVersion", type="model", id=rid, uid=7)[0], 200)
        self.assertEqual(self.get_json("/assetVersion", type="model", id=rid, uid=8)[0], 404)

    def test_version_tracks_files_and_index_row(self):
        rid = self.probe_asset()
        asset_dir = self.data / "models" / str(rid)

        def version():
            return self.get_json("/assetVersion", type="model", id=rid)[1]["version"]

        base = version()
        self.assertEqual(version(), base)  # reading does not change it

        target = asset_dir / "probe.obj"
        target.write_bytes(b"v 3 3 3\n")
        changed_bytes = version()
        self.assertNotEqual(changed_bytes, base)

        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        changed_mtime = version()
        self.assertNotEqual(changed_mtime, changed_bytes)

        nested = asset_dir / "meshes"
        nested.mkdir()
        (nested / "part.obj").write_bytes(b"v 4 4 4\n")
        changed_added = version()
        self.assertNotEqual(changed_added, changed_mtime)

        (nested / "part.obj").unlink()
        nested.rmdir()
        self.assertEqual(version(), changed_mtime)  # removing the added file restores that token

        with closing(server.get_conn(self.data, "model")) as con, con:
            con.execute("UPDATE assets SET category = ? WHERE id = ?", ("模型.容器", rid))
        self.assertNotEqual(version(), changed_mtime)

    def test_version_is_stat_only(self):
        with tempfile.TemporaryDirectory(prefix="ols_ver_") as tmp:
            data = Path(tmp)
            asset_dir = data / "models" / "1"
            asset_dir.mkdir(parents=True)
            target = asset_dir / "a.obj"
            target.write_bytes(b"aaaa")
            st = target.stat()
            row = {"id": 1, "name": "a", "category": "模型.抓取物"}
            before = server.asset_version(data, "model", row)
            target.write_bytes(b"bbbb")
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertEqual(server.asset_version(data, "model", row), before)

    def test_version_does_not_leak_into_the_asset_documents(self):
        _, data, _ = self.get_json("/model", id=1)
        self.assertNotIn("version", data)
        _, scene, _ = self.get_json("/scene", id=1)
        self.assertNotIn("version", scene)


class TestIndexSchema(unittest.TestCase):
    def test_existing_database_is_migrated_without_losing_records(self):
        with tempfile.TemporaryDirectory(prefix="ols_schema_") as tmp:
            db = Path(tmp) / "model_index.db"
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL, "
                            "upload_user_id INTEGER NOT NULL DEFAULT 0, category VARCHAR(32) NOT NULL, "
                            "is_deleted BOOL NOT NULL DEFAULT 0)")
                con.execute("INSERT INTO assets VALUES (1, 'apple', 0, '模型.抓取物', 0)")
            server.ensure_schema(tmp)
            server.ensure_schema(tmp)
            with closing(sqlite3.connect(db)) as con, con:
                self.assertEqual(con.execute("SELECT id, name, no_display FROM assets").fetchall(),
                                 [(1, "apple", 0)])

    def test_legacy_json_no_display_is_imported(self):
        with tempfile.TemporaryDirectory(prefix="ols_index_") as tmp:
            data = Path(tmp)
            (data / "scenes").mkdir()
            (data / "models").mkdir()
            (data / "scenes" / "scene_index.json").write_text(
                json.dumps([{"id": 1, "name": "shown"},
                            {"id": 2, "name": "hidden", "no_display": True}]), "utf-8")
            (data / "models" / "model_index.json").write_text(
                json.dumps({"by_id": {"1": "shown", "2": "hidden"},
                            "no_display": {"1": False, "2": True}}), "utf-8")
            for typ in ("scene", "model"):
                rows = init_index.rows_from_legacy(typ, data)
                init_index.write_db(typ, data, rows)
                with closing(sqlite3.connect(data / f"{typ}_index.db")) as con, con:
                    self.assertEqual(con.execute("SELECT id, no_display FROM assets ORDER BY id").fetchall(),
                                     [(1, 0), (2, 1)])


class HangupWriter:
    """Lets `ok_writes` writes through, then raises the way a hung-up client does."""

    def __init__(self, error, ok_writes):
        self.error = error
        self.ok_writes = ok_writes
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        if len(self.writes) > self.ok_writes:
            raise self.error
        return len(data)

    def flush(self):
        pass


class TestClientHangup(APITestCase):
    """Q163: a cancelled request is not a server error (§12.46)."""

    HANGUPS = (BrokenPipeError("pipe"), ConnectionAbortedError("10053"), ConnectionResetError("10054"))

    def dispatch(self, path, error=None, ok_writes=99):
        handler = object.__new__(server.OLSHandler)
        handler.config = self.config
        handler.path = path
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.client_address = ("127.0.0.1", 40000)
        handler.headers = Message()
        handler.cache_version = ""
        handler.close_connection = False
        handler.wfile = HangupWriter(error, ok_writes)
        handler.rfile = io.BytesIO(b"")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            handler._dispatch("GET")
        return handler, stderr.getvalue()

    def test_hangup_while_writing_the_body_is_silent(self):
        for error in self.HANGUPS:
            with self.subTest(error=type(error).__name__):
                handler, stderr = self.dispatch("/list?type=model", error, ok_writes=1)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(len(handler.wfile.writes), 2, "a 500 must not be attempted on the dead socket")
                self.assertIn(b"200", handler.wfile.writes[0])
                self.assertTrue(handler.close_connection)

    def test_hangup_while_writing_the_headers_is_silent(self):
        for error in self.HANGUPS:
            with self.subTest(error=type(error).__name__):
                handler, stderr = self.dispatch("/list?type=model", error, ok_writes=0)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(len(handler.wfile.writes), 1)
                self.assertTrue(handler.close_connection)

    def test_hangup_while_answering_an_api_error_is_silent(self):
        for error in self.HANGUPS:
            with self.subTest(error=type(error).__name__):
                handler, stderr = self.dispatch("/model?id=999999", error, ok_writes=0)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(len(handler.wfile.writes), 1)
                self.assertIn(b"404", handler.wfile.writes[0])
                self.assertTrue(handler.close_connection)

    def test_hangup_while_sending_a_file_is_silent(self):
        handler, stderr = self.dispatch("/file?type=model&id=1&path=apple.obj",
                                        ConnectionAbortedError("10053"), ok_writes=1)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(len(handler.wfile.writes), 2)
        self.assertTrue(handler.close_connection)

    def test_a_real_failure_still_reports_500(self):
        with patch.object(server.OLSHandler, "api_list", MagicMock(side_effect=ValueError("boom"))):
            handler, stderr = self.dispatch("/list?type=model")
        self.assertIn("Traceback", stderr)
        self.assertIn("ValueError", stderr)
        self.assertIn(b"500", handler.wfile.writes[0])
        self.assertFalse(handler.close_connection)

    def test_a_hangup_does_not_change_a_successful_response(self):
        handler, stderr = self.dispatch("/list?type=model")
        self.assertEqual(stderr.count('"GET /list?type=model HTTP/1.1" 200'), 1)
        self.assertEqual(len(handler.wfile.writes), 2)
        self.assertIn(b"ETag", handler.wfile.writes[0])
        self.assertFalse(handler.close_connection)


class TestHangupOutsideDispatch(APITestCase):
    """Q163 §12.46 ②: the request can also die before dispatch, or on a direct-writing route."""

    def handler(self, rfile, error=None, ok_writes=99):
        handler = object.__new__(server.OLSHandler)
        handler.config = self.config
        handler.client_address = ("127.0.0.1", 40001)
        handler.headers = Message()
        handler.cache_version = ""
        handler.close_connection = False
        handler.wfile = HangupWriter(error, ok_writes)
        handler.rfile = rfile
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            handler.handle_one_request()
        return handler, stderr.getvalue()

    def test_hangup_while_reading_the_request_line_is_silent(self):
        for error in TestClientHangup.HANGUPS:
            with self.subTest(error=type(error).__name__):
                rfile = MagicMock()
                rfile.readline.side_effect = error
                handler, stderr = self.handler(rfile)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(handler.wfile.writes, [])
                self.assertTrue(handler.close_connection)

    def test_hangup_while_answering_a_preflight_is_silent(self):
        request = b"OPTIONS /list HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        handler, stderr = self.handler(io.BytesIO(request), ConnectionAbortedError("10053"), ok_writes=0)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(len(handler.wfile.writes), 1)
        self.assertIn(b"204", handler.wfile.writes[0])
        self.assertTrue(handler.close_connection)

    def test_a_complete_preflight_is_untouched(self):
        request = b"OPTIONS /list HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        handler, stderr = self.handler(io.BytesIO(request))
        self.assertIn(b"204", handler.wfile.writes[0])
        self.assertIn(b"Access-Control-Max-Age: 86400", handler.wfile.writes[0])
        self.assertFalse(handler.close_connection)


if __name__ == "__main__":
    unittest.main(verbosity=2)
