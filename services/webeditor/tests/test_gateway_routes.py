"""The gateway's surviving HTTP surface: health, runtime config, agent routes, static fallback.

The scene itself lives in the browser (spec v2 §1.1), so `/api/commands`,
`/api/bootstrap`, `/api/assets*`, `/api/sensors*`, `/api/timelines*` and the
presentation/agent-control routes are gone (§10.1); the WebSocket protocol has
its own suite in tests/test_ws_protocol.py. The SPA shell comes from `dist/`,
which `pack.py` puts into a release (§11.7), so these tests stage one.
"""
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import local_gateway
from local_gateway import (
    DEFAULT_CONSTANT_CONFIG,
    StandaloneGateway,
    create_app,
    load_constant_config,
)


def stage_dist(root: Path) -> Path:
    """A minimal built SPA: the shell plus one hashed asset next to it."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>spa shell</body></html>", "utf-8")
    (dist / "assets" / "index-abc.js").write_text("console.log('bundle')", "utf-8")
    return dist


class GatewayRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
        # §5.15: this suite only covers the surviving HTTP surface, so the Agent relay is
        # switched off here; tests/test_gateway_agent.py drives it against a fake service.
        config["agent_service_url"] = ""
        self.gateway = StandaloneGateway(config=config)
        # The gateway is built against the repository first (it reads features.json from
        # ROOT at startup, 坑 22); only the request handlers below see the staged root.
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-surface-")
        self.addCleanup(self.workspace.cleanup)
        root = Path(self.workspace.name)
        stage_dist(root)
        (root / "config").mkdir()
        (root / "config" / "constant_config.json").write_text(json.dumps({"library_server_url": "secret"}), "utf-8")
        self.addCleanup(setattr, local_gateway, "ROOT", local_gateway.ROOT)
        local_gateway.ROOT = root
        app = web.Application()
        app.router.add_get("/api/health", self.gateway.health)
        app.router.add_get("/api/runtimeConfig", self.gateway.runtime_config)
        app.router.add_get("/api/agent/conversations", self.gateway.conversations_list)
        app.router.add_post("/api/agent/conversations", self.gateway.conversations_create)
        app.router.add_get("/api/agent/conversations/{conversationId}", self.gateway.conversation)
        app.router.add_delete("/api/agent/conversations/{conversationId}", self.gateway.conversation)
        app.router.add_post("/api/agent/conversations/{conversationId}/activate", self.gateway.conversation)
        app.router.add_get("/api/agent/jobs", self.gateway.jobs)
        app.router.add_post("/api/agent/jobs", self.gateway.jobs)
        app.router.add_get("/api/agent/jobs/{jobId}", self.gateway.job)
        app.router.add_get("/events", self.gateway.events)
        app.router.add_get("/", self.gateway.static)
        app.router.add_get("/{route:.*}", self.gateway.static)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_health_and_runtime_config(self):
        response = await self.client.get("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "ok")
        response = await self.client.get("/api/runtimeConfig")
        self.assertEqual(response.status, 200)
        config = await response.json()
        self.assertEqual(config["library_server_url"], "http://127.0.0.1:8000")
        self.assertEqual(config["max_pending_requests"], 8)
        # §7.3/Q97: the ghosts' opacity is a browser concern, so it travels in this payload.
        self.assertEqual(config["ghost_pose_opacity"], 0.5)
        # §7.3/Q101: the same for the waypoint spheres' radius and sampling stride -- both
        # are drawn in the browser and have no server-side consumer.
        self.assertEqual(config["traj_waypoint_radius"], 0.01)
        self.assertEqual(config["traj_waypoint_stride_frames"], 10)
        # §4.8.5/Q118: the shadow default is a browser concern too -- it is what the stage
        # projects once `viewport.shadows` has hidden the toolbar checkbox.
        self.assertIs(config["shadows_enabled"], True)
        # §4.8.6/Q149/Q150: the browser defaults to one tight wireframe per URDF link.
        self.assertIs(config["urdf_bounding_box_per_link"], True)
        # §4.4/Q145: the viewport clear colour is a browser concern too -- the gateway never
        # renders, it only hands the configured hex string through.
        self.assertEqual(config["viewport_background_color"], "#000000")
        self.assertIs(config["disable_agent_details"], False)
        # Registered before the catch-all, so this must be JSON, not index.html (§9.1, 坑2).
        self.assertNotIn("<html", json.dumps(config))

    async def test_runtime_config_carries_disable_agent_details(self):
        self.gateway.config["disable_agent_details"] = True
        config = await (await self.client.get("/api/runtimeConfig")).json()
        self.assertIs(config["disable_agent_details"], True)

    async def test_runtime_config_carries_the_feature_flags(self):
        config = await (await self.client.get("/api/runtimeConfig")).json()
        self.assertEqual(config["features"], self.gateway.features)
        self.assertTrue(config["features"]["viewport.loadScene"])

    async def test_agent_tasks_are_disabled_when_no_service_is_configured(self):
        # §5.15/Q86: an empty agent_service_url is the deployment's way of switching the
        # task route off; the relay itself is covered in tests/test_gateway_agent.py.
        response = await self.client.post("/api/agent/jobs", json={"command": "move the arm", "scene": {"objects": []}})
        self.assertEqual(response.status, 501)
        self.assertEqual((await response.json())["error"]["code"], "AGENT_UNAVAILABLE")
        self.assertEqual((await (await self.client.get("/api/agent/jobs")).json())["jobs"], [])
        self.assertEqual((await self.client.get("/api/agent/jobs/job-1")).status, 404)
        # §10.1: the cancel route is gone, and only GET falls through to the SPA shell.
        self.assertIn((await self.client.post("/api/agent/jobs/job-1/cancel", json={})).status, (404, 405))

    async def test_conversation_lifecycle(self):
        created = await (await self.client.post("/api/agent/conversations", json={
            "title": "pick", "sessionID": "session-a", "sceneID": 7})).json()
        conversation_id = created["conversationId"]
        self.assertEqual(created["sceneBinding"]["sceneId"], 7)
        self.assertEqual(created["sceneBinding"]["simSessionId"], "session-a")
        self.assertEqual(created["sessionID"], "session-a")
        self.assertEqual(created["presentationMode"], "real-sync")
        second = await (await self.client.post("/api/agent/conversations", json={
            "title": "next", "sessionID": "session-b", "sceneID": 8})).json()
        listed = await (await self.client.get("/api/agent/conversations")).json()
        self.assertEqual(listed["activeConversationId"], second["conversationId"])
        self.assertEqual(len(listed["conversations"]), 2)
        activated = await (await self.client.post(f"/api/agent/conversations/{conversation_id}/activate")).json()
        self.assertEqual(activated["conversationId"], conversation_id)
        self.assertEqual((await (await self.client.get("/api/agent/conversations")).json())["activeConversationId"], conversation_id)
        self.assertEqual((await self.client.delete(f"/api/agent/conversations/{conversation_id}")).status, 200)
        self.assertEqual((await self.client.get(f"/api/agent/conversations/{conversation_id}")).status, 404)

    async def test_removed_scene_routes_are_gone(self):
        # The SPA catch-all only matches GET, so writes to deleted routes are 405 (404 if unmatched).
        for method, path in [("POST", "/api/commands"), ("POST", "/api/assets/upload"),
                             ("POST", "/api/state/load-simstate"), ("POST", "/api/presentation/mode"),
                             ("POST", "/api/agent-control/captures")]:
            with self.subTest(path=path):
                self.assertIn((await self.client.request(method, path, json={})).status, (404, 405))
        # Unknown GETs fall through to the SPA shell, which is what the router in dist/ expects.
        for path in ("/api/bootstrap", "/api/assets", "/api/sensors/1/frame", "/api/metrics"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])

    async def test_static_shell_is_the_fallback(self):
        for path in ("/", "/scenes/42"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertIn("spa shell", await response.text())

    async def test_static_serves_built_assets_as_themselves(self):
        # §11.7: the bundle's /assets/index-<hash>.js has to arrive as JS, not as the shell.
        response = await self.client.get("/assets/index-abc.js")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.headers["Content-Type"])
        self.assertEqual(await response.text(), "console.log('bundle')")

    async def test_static_cannot_escape_dist(self):
        # config/ sits next to dist/ in a release, so a traversal must land on the shell
        # rather than read the gateway's own config file out through a static route.
        response = await self.client.get("/..%2Fconfig%2Fconstant_config.json")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("spa shell", await response.text())

    async def test_static_without_a_build_says_so(self):
        # §11.7 ③: no dist/index.html means nobody ran `npm run build`; a 404 that names the
        # command beats the old editor/index.html fallback, which rendered a blank page.
        empty = tempfile.TemporaryDirectory(prefix="gateway-no-dist-")
        self.addCleanup(empty.cleanup)
        local_gateway.ROOT = Path(empty.name)
        response = await self.client.get("/")
        self.assertEqual(response.status, 404)
        self.assertIn("npm run build", await response.text())

    async def test_gateway_holds_no_server_side_scene(self):
        self.assertIsNone(self.gateway.world_cache)
        self.assertEqual(self.gateway.scene_version, 0)
        self.assertEqual(self.gateway.browsers, {})
        for attribute in ("entities", "asset_service", "saved_states", "undo_stack", "settings"):
            with self.subTest(attribute=attribute):
                self.assertFalse(hasattr(self.gateway, attribute))

    async def test_get_world_is_registered_before_the_spa_catch_all(self):
        # Uses the real create_app(): with /getWorld after /{route:.*} this would be HTML (§5.9, 坑2).
        client = TestClient(TestServer(create_app(config=json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG)))))
        await client.start_server()
        try:
            response = await client.get("/getWorld")
            self.assertEqual(response.status, 503)
            self.assertIn("application/json", response.headers["Content-Type"])
            self.assertEqual((await response.json())["error"]["code"], "NO_BROWSER_CONNECTED")
        finally:
            await client.close()


class ConstantConfigTests(unittest.TestCase):
    def test_features_are_loaded_from_the_repository_root(self):
        gateway = StandaloneGateway()
        self.assertIn("viewport.loadScene", gateway.features)
        self.assertTrue(all(isinstance(value, bool) for value in gateway.features.values()))

    def test_default_config_file_matches_the_built_in_defaults(self):
        self.assertEqual(load_constant_config(), json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG)))


if __name__ == "__main__":
    unittest.main()
