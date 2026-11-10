"""Browser profile partitions must keep live worlds and browser responses apart."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from local_gateway import DEFAULT_CONSTANT_CONFIG, GATEWAY_APP_KEY, create_app


class BrowserProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="gateway-profiles-")
        self.addCleanup(self.workspace.cleanup)
        config = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
        config["agent_service_url"] = ""
        config["browser_request_timeout_seconds"] = 1
        self.app = create_app(config, state_path=Path(self.workspace.name) / "gateway_state.json")
        self.gateway = self.app[GATEWAY_APP_KEY]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, browser_id: str):
        socket = await self.client.ws_connect("/events")
        await socket.send_json({"type": "browser.hello", "browser_id": browser_id, "role": "viewer"})
        for _ in range(100):
            if browser_id in self.gateway.browsers:
                return socket
            await asyncio.sleep(0.005)
        self.fail(f"browser profile {browser_id} did not register")

    async def request_frame(self, socket):
        message = await asyncio.wait_for(socket.receive_json(), timeout=2)
        self.assertEqual(message["type"], "browser.request")
        self.assertEqual(message["action"], "getWorld")
        return message

    async def test_concurrent_world_reads_and_pending_responses_stay_in_their_profiles(self):
        first = await self.connect("profile-a")
        second = await self.connect("profile-b")
        self.gateway.profiles["profile-a"].state.start_session("session-a")
        self.gateway.profiles["profile-b"].state.start_session("session-b")

        first_read = asyncio.create_task(self.client.get(
            "/getWorld", headers={"X-WebEditor-Browser-ID": "profile-a"}))
        second_read = asyncio.create_task(self.client.get(
            "/getWorld", headers={"X-WebEditor-Browser-ID": "profile-b"}))
        first_frame = await self.request_frame(first)
        second_frame = await self.request_frame(second)

        await first.send_json({"type": "browser.response", "request_id": second_frame["request_id"],
                               "ok": True, "payload": {"world": {"owner": "wrong-profile"},
                                                          "playback_state": "stopped"}})
        await asyncio.sleep(0.02)
        self.assertFalse(second_read.done(), "one profile must not settle another profile's pending request")

        await first.send_json({"type": "browser.response", "request_id": first_frame["request_id"],
                               "ok": True, "payload": {"world": {"owner": "profile-a"},
                                                          "playback_state": "stopped"}})
        await second.send_json({"type": "browser.response", "request_id": second_frame["request_id"],
                                "ok": True, "payload": {"world": {"owner": "profile-b"},
                                                           "playback_state": "stopped"}})

        first_response, second_response = await asyncio.gather(first_read, second_read)
        self.assertEqual((await first_response.json())["owner"], "profile-a")
        self.assertEqual((await second_response.json())["owner"], "profile-b")
        self.assertEqual(first_response.headers["X-Browser-ID"], "profile-a")
        self.assertEqual(second_response.headers["X-Browser-ID"], "profile-b")
        self.assertEqual(self.gateway.profiles["profile-a"].state.session_id, "session-a")
        self.assertEqual(self.gateway.profiles["profile-b"].state.session_id, "session-b")

        state_file = json.loads((Path(self.workspace.name) / "gateway_state.json").read_text("utf-8"))
        profile_files = list((Path(self.workspace.name) / "gateway_profiles").glob("*.json"))
        self.assertEqual(state_file["browser_id"], "profile-a")
        self.assertEqual(len(profile_files), 1)
        persisted = json.loads(profile_files[0].read_text("utf-8"))
        self.assertEqual(persisted["browser_id"], "profile-b")
        await first.close()
        await second.close()

    async def test_profile_header_selects_state_without_reinterpreting_existing_query_fields(self):
        first = await self.connect("profile-a")
        second = await self.connect("profile-b")
        self.gateway.profiles["profile-a"].state.start_session("session-a")
        self.gateway.profiles["profile-b"].state.start_session("session-b")

        response = await self.client.get("/sessions?browser_id=profile-b",
                                         headers={"X-WebEditor-Browser-ID": "profile-a"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["browserID"], "profile-a")
        self.assertEqual(body["sessionID"], "session-a")
        await first.close()
        await second.close()


if __name__ == "__main__":
    unittest.main()
