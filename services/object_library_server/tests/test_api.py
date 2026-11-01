#!/usr/bin/env python3
"""End-to-end tests for OLS (ObjectLibraryServer).

Builds a temporary data directory with a small fixture library, starts the
real HTTP server on an ephemeral port, and exercises every endpoint through
urllib. Run with:  python tests/test_api.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def http(method, url, body=None):
    req = Request(url, data=body, method=method)
    try:
        with urlopen(req) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except HTTPError as e:
        return e.code, e.read(), (e.headers.get("Content-Type", "") if e.headers else "")


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
        self.assertEqual(data["items"][0], {"id": 1, "name": "apple", "category": "模型.抓取物"})

    def test_list_category_filter(self):
        _, data, __ = self.get_json("/list", type="model", category="模型.容器")
        self.assertEqual([i["id"] for i in data["items"]], [2])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
