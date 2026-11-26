"""Production Bearer boundary and XAuth cache/transport checks; no live services."""
import copy
import hashlib
import ipaddress
import json
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import test_api
from xauth_client import AuthError, TokenVerifier, XAuthClient, bearer_token


def identity(uid=7, *, now=None, ttl=3600, role="user"):
    now = time.time() if now is None else now
    return {"available": True,
            "user": {"user_id": uid, "is_active": True, "role": role},
            "session": {"expires_at": datetime.fromtimestamp(now + ttl, timezone.utc).isoformat(),
                        "expires_in": ttl}}


class Client:
    def __init__(self, result=None):
        self.result = identity() if result is None else result
        self.calls = []

    def check(self, token):
        self.calls.append(token)
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.wall = 1000.0
        self.mono = 10.0
        self.client = Client(identity(now=self.wall))
        self.verifier = TokenVerifier(self.client, clock=lambda: self.wall,
                                      monotonic=lambda: self.mono)

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def test_cache_has_fixed_600_second_deadline(self):
        self.verifier.verify("secret")
        self.advance(599)
        self.verifier.verify("secret")
        self.assertEqual(len(self.client.calls), 1)
        self.advance(1)
        self.verifier.verify("secret")
        self.assertEqual(len(self.client.calls), 2)
        self.assertNotIn("secret", self.verifier._cache)
        self.assertIn(hashlib.sha256(b"secret").hexdigest(), self.verifier._cache)

    def test_absolute_expiry_and_remaining_bound_cache(self):
        self.client.result = identity(now=self.wall, ttl=10)
        self.verifier.verify("short")
        self.advance(10)
        with self.assertRaises(AuthError) as caught:
            self.verifier.verify("short")
        self.assertEqual(caught.exception.status, 401)

    def test_wall_clock_rollback_does_not_extend_cache(self):
        self.verifier.verify("secret")
        self.wall -= 1000
        self.mono += 600
        self.verifier.verify("secret")
        self.assertEqual(len(self.client.calls), 2)

    def test_upstream_failure_only_uses_still_valid_cache(self):
        self.verifier.verify("known")
        self.client.result = AuthError(503, "AUTH_UNAVAILABLE", "unavailable")
        self.advance(599)
        self.assertEqual(self.verifier.verify("known")["user"]["user_id"], 7)
        with self.assertRaises(AuthError):
            self.verifier.verify("unknown")
        self.advance(1)
        with self.assertRaises(AuthError) as caught:
            self.verifier.verify("known")
        self.assertEqual(caught.exception.status, 503)

    def test_invalid_and_malformed_identities_never_cached(self):
        for result in ({"available": False}, identity(uid=0, now=self.wall),
                       identity(uid=True, now=self.wall), {"available": True},
                       {"available": "true"}, [], None):
            self.client.result = result
            with self.subTest(result=result), self.assertRaises(AuthError):
                self.verifier.verify("invalid")
        self.assertEqual(self.verifier._cache, {})

    def test_callers_cannot_mutate_cached_identity(self):
        result = self.verifier.verify("secret")
        result["user"]["user_id"] = 999
        self.assertEqual(self.verifier.verify("secret")["user"]["user_id"], 7)

    def test_concurrent_miss_is_coalesced(self):
        threads = [threading.Thread(target=self.verifier.verify, args=("one",)) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.client.calls), 1)

    def test_different_token_misses_do_not_share_remote_lock(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingClient(Client):
            def check(inner, token):
                if token == "slow":
                    started.set()
                    release.wait(2)
                return super().check(token)

        client = BlockingClient(identity(now=self.wall))
        verifier = TokenVerifier(client, clock=lambda: self.wall, monotonic=lambda: self.mono)
        slow = threading.Thread(target=verifier.verify, args=("slow",))
        slow.start()
        self.assertTrue(started.wait(1))
        fast = threading.Thread(target=verifier.verify, args=("fast",))
        fast.start()
        fast.join(0.2)
        self.assertFalse(fast.is_alive(), "a slow token must not block another token's miss")
        release.set()
        slow.join(1)

    def test_bearer_parser(self):
        self.assertEqual(bearer_token("bEaReR value"), "value")
        for header in (None, "", "Basic value", "Bearer", "Bearer one two"):
            with self.subTest(header=header), self.assertRaises(AuthError):
                bearer_token(header)


class AuthBoundaryTests(test_api.APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = Client()
        cls.httpd.RequestHandlerClass.auth_verifier = TokenVerifier(cls.client)

    def request(self, endpoint, *, method="GET", token="user7", headers=None, body=None):
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        req = Request(f"http://127.0.0.1:{self.port}" + endpoint,
                      method=method, headers=request_headers, data=body)
        try:
            response = urlopen(req)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.read(), response.headers

    def test_health_is_public_liveness(self):
        status, raw, headers = self.request("/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "ok")
        self.assertEqual(headers["Cache-Control"], "private, no-cache")

    def test_every_endpoint_requires_bearer_even_with_uid_or_query_token(self):
        for method, routes in (("GET", server.ROUTES_GET), ("POST", server.ROUTES_POST)):
            for endpoint in routes:
                if endpoint == "/health":
                    continue
                with self.subTest(endpoint=endpoint):
                    status, raw, headers = self.request(endpoint + "?uid=0&token=forged",
                                                        method=method, token=None)
                    self.assertEqual(status, 401, raw)
                    self.assertEqual(headers["Cache-Control"], "no-store")

    def test_system_read_and_own_upload_uid_spoof_ignored(self):
        self.assertEqual(self.request("/model?id=1&uid=999")[0], 200)
        status, raw, _ = self.request("/presignUploadID?type=model&name=owned&entranceFilePath=owned.obj&uid=0",
                                      method="POST")
        self.assertEqual(status, 200, raw)
        rid = json.loads(raw)["id"]
        row = server.fetch_one(self.data_dir, "model", "SELECT * FROM assets WHERE id=?", (rid,))
        self.assertEqual(row["upload_user_id"], 7)
        self.assertGreaterEqual(rid, 10000)
        for endpoint in (f"/assetVersion?type=model&id={rid}", f"/file?type=model&id={rid}&path=owned.obj"):
            self.client.result = identity(uid=8, role="admin")
            self.assertEqual(self.request(endpoint + "&uid=7", token="user8")[0], 404)
        self.client.result = identity()

    def test_admin_role_cannot_delete_or_upload_system_asset(self):
        self.client.result = identity(uid=9, role="admin")
        self.assertEqual(self.request("/model?id=1", token="admin9")[0], 200)
        for endpoint in ("/deleteAsset?type=model&id=1&uid=0",
                         "/uploadFile?type=model&id=1&path=apple.obj&uid=0"):
            self.assertEqual(self.request(endpoint, method="POST", token="admin9", body=b"new")[0], 403)
        self.client.result = identity()

    def test_auth_checked_before_conditional_cache_response(self):
        status, _, headers = self.request("/model?id=1&v=version")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Cache-Control"].startswith("private"))
        self.assertIn("Authorization", headers["Vary"])
        status, _, _ = self.request("/model?id=1&v=version", token=None,
                                     headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 401)

    def test_preflight_allows_auth_and_does_not_require_token(self):
        status, _, headers = self.request("/file", method="OPTIONS", token=None)
        self.assertEqual(status, 204)
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])

    def test_invalid_token_401_and_upstream_failure_503(self):
        self.client.result = {"available": False}
        self.assertEqual(self.request("/list?type=model", token="invalid")[0], 401)
        self.client.result = AuthError(503, "AUTH_UNAVAILABLE", "unavailable")
        self.assertEqual(self.request("/list?type=model", token="unavailable")[0], 503)
        self.client.result = identity()


class TrustedMachineIpTests(test_api.APITestCase):
    """Q173: whitelisted peer IPs read without credentials; writes stay authenticated."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = Client()
        cls.httpd.RequestHandlerClass.auth_verifier = TokenVerifier(cls.client)
        cls.httpd.RequestHandlerClass.trusted_machine_ips = frozenset({ipaddress.ip_address("127.0.0.1")})

    def request(self, endpoint, *, method="GET", token="user7", headers=None, body=None):
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        req = Request(f"http://127.0.0.1:{self.port}" + endpoint,
                      method=method, headers=request_headers, data=body)
        try:
            response = urlopen(req)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.read(), response.headers

    def whitelist(self, *entries):
        ips = frozenset(ipaddress.ip_address(entry) for entry in entries)
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "trusted_machine_ips",
                        self.httpd.RequestHandlerClass.trusted_machine_ips)
        self.httpd.RequestHandlerClass.trusted_machine_ips = ips

    def test_trusted_reads_need_no_credentials(self):
        status, raw, _ = self.request("/list?type=model&uid=0", token=None)
        self.assertEqual(status, 200, raw)
        self.assertEqual({item["id"] for item in json.loads(raw)["items"]}, {1, 2})
        self.assertEqual(self.request("/model?id=1&uid=0", token=None)[0], 200)
        self.assertEqual(self.request("/file?type=model&id=1&path=apple.obj", token=None)[0], 200)

    def test_trusted_uid_scopes_the_view(self):
        con = sqlite3.connect(str(Path(self.data_dir) / "model_index.db"))
        con.execute("INSERT INTO assets (id, name, upload_user_id, category) VALUES (?,?,?,?)",
                    (10000, "private", 7, "模型.自定义"))
        con.commit()
        con.close()
        self.addCleanup(self._drop_private_asset)
        (Path(self.data_dir) / "models" / "10000").mkdir(parents=True, exist_ok=True)
        (Path(self.data_dir) / "models" / "10000" / "private.json").write_text("{}", "utf-8")
        (Path(self.data_dir) / "models" / "10000" / "private_files.json").write_text(
            json.dumps({"is_on_cos": False, "path": "private.obj"}), "utf-8")
        (Path(self.data_dir) / "models" / "10000" / "private.obj").write_bytes(b"v 0 0 0\n")
        system, _, _ = self.request("/list?type=model&uid=0", token=None)
        self.assertEqual(system, 200)
        owner, raw, _ = self.request("/list?type=model&uid=7", token=None)
        self.assertEqual(owner, 200)
        self.assertEqual({item["id"] for item in json.loads(raw)["items"]}, {1, 2, 10000})
        self.assertEqual(self.request("/model?id=10000&uid=0", token=None)[0], 404)
        self.assertEqual(self.request("/model?id=10000&uid=7", token=None)[0], 200)

    def _drop_private_asset(self):
        con = sqlite3.connect(str(Path(self.data_dir) / "model_index.db"))
        con.execute("DELETE FROM assets WHERE id=10000")
        con.commit()
        con.close()
        shutil.rmtree(Path(self.data_dir) / "models" / "10000", ignore_errors=True)

    def test_trusted_missing_uid_defaults_to_system_view(self):
        status, raw, _ = self.request("/list?type=model", token=None)
        self.assertEqual(status, 200, raw)
        self.assertEqual({item["id"] for item in json.loads(raw)["items"]}, {1, 2})

    def test_invalid_uid_is_rejected(self):
        self.assertEqual(self.request("/list?type=model&uid=abc", token=None)[0], 400)

    def test_writes_are_never_exempt(self):
        for endpoint in ("/presignUploadID?type=model&name=owned&entranceFilePath=owned.obj&uid=0",
                         "/deleteAsset?type=model&id=1&uid=0",
                         "/uploadFile?type=model&id=1&path=apple.obj&uid=0"):
            with self.subTest(endpoint=endpoint):
                status, raw, _ = self.request(endpoint, method="POST", token=None, body=b"x")
                self.assertEqual(status, 401, raw)

    def test_non_whitelisted_peer_still_requires_bearer(self):
        self.whitelist("10.9.9.9")
        self.assertEqual(self.request("/list?type=model&uid=0", token=None)[0], 401)

    def test_forwarded_headers_do_not_buy_exemption(self):
        self.whitelist("10.9.9.9")
        for header in ("X-Forwarded-For", "X-Real-IP"):
            with self.subTest(header=header):
                status, raw, _ = self.request("/list?type=model&uid=0", token=None,
                                              headers={header: "10.9.9.9"})
                self.assertEqual(status, 401, raw)

    def test_present_credentials_are_still_verified_on_trusted_peer(self):
        self.client.result = {"available": False}
        self.addCleanup(setattr, self.client, "result", identity())
        self.assertEqual(self.request("/list?type=model", token="invalid")[0], 401)

    def test_empty_whitelist_keeps_the_strict_boundary(self):
        self.whitelist()
        self.assertEqual(self.request("/list?type=model&uid=0", token=None)[0], 401)

    def test_whitelist_comes_from_config_and_bad_entry_fails_closed(self):
        config = {**self.config, "port": 0, "auth": {"trusted_machine_ips": ["127.0.0.1"]}}
        httpd = server.make_server(config, auth_verifier=TokenVerifier(self.client))
        try:
            self.assertEqual(httpd.RequestHandlerClass.trusted_machine_ips,
                             frozenset({ipaddress.ip_address("127.0.0.1")}))
        finally:
            httpd.server_close()
        with self.assertRaises(ValueError):
            server.make_server({**self.config, "port": 0,
                                "auth": {"trusted_machine_ips": ["not-an-ip"]}},
                               auth_verifier=TokenVerifier(self.client))


class TransportTests(unittest.TestCase):
    def test_real_local_http_xauth_protocol(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                data = json.dumps(identity()).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
            thread = threading.Thread(target=httpd.serve_forever)
            thread.start()
            try:
                client = XAuthClient(f"http://127.0.0.1:{httpd.server_port}")
                self.assertEqual(TokenVerifier(client).verify("test-token")["user"]["user_id"], 7)
                self.assertEqual(requests, [("/is_available", {"token": "test-token"})])
            finally:
                httpd.shutdown()
                thread.join()

    def test_remote_http_and_missing_config_fail_closed(self):
        for url in ("", "http://example.invalid", "https://user:pass@example.invalid"):
            with self.subTest(url=url), self.assertRaises(AuthError) as caught:
                XAuthClient(url).check("secret")
            self.assertEqual(caught.exception.status, 503)

    def test_https_keeps_hostname_verification_and_configured_sni(self):
        with patch("xauth_client.ssl.create_default_context") as context, \
                patch("xauth_client.NamedHTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"available":false}'
            XAuthClient("https://192.0.2.1", ca_certs="test-ca.pem",
                        server_name="auth.example.invalid").check("token")
            context.return_value.load_verify_locations.assert_called_once_with(cafile="test-ca.pem")
            connection.assert_called_once_with("192.0.2.1", 443, "auth.example.invalid",
                                                context.return_value, 5.0)


if __name__ == "__main__":
    unittest.main()
