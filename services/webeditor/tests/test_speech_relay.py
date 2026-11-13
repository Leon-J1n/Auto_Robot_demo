import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from speech_relay import SpeechRelay


class SpeechRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_results_finish_and_connection_isolation(self):
        sessions = []

        async def stream(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            samples = bytearray()
            sessions.append(samples)
            await ws.send_json({"type": "ready", "sample_rate": 16000, "format": "pcm_s16le"})
            async for message in ws:
                if message.type == web.WSMsgType.BINARY:
                    samples.extend(message.data)
                    await ws.send_json({"type": "result", "segment": 0, "text": message.data.hex(), "final": False})
                else:
                    self.assertEqual(message.json(), {"type": "finish"})
                    await ws.send_json({"type": "done"})
                    break
            await ws.close()
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/stream", stream)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        self.addAsyncCleanup(upstream.close)
        relay = SpeechRelay({"speech_service_url": str(upstream.make_url(""))})
        app = web.Application()
        app.router.add_get("/speech", relay.handle)
        app.on_shutdown.append(relay.cleanup)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        a, b = await asyncio.gather(client.ws_connect("/speech"), client.ws_connect("/speech"))
        self.assertEqual((await a.receive_json())["type"], "ready")
        self.assertEqual((await b.receive_json())["type"], "ready")
        await a.send_bytes(b"\x01\x00")
        await b.send_bytes(b"\x02\x00")
        self.assertEqual((await a.receive_json())["text"], "0100")
        self.assertEqual((await b.receive_json())["text"], "0200")
        await a.send_json({"type": "finish"})
        self.assertEqual((await a.receive_json())["type"], "done")
        await b.close()
        self.assertCountEqual([bytes(samples) for samples in sessions], [b"\x01\x00", b"\x02\x00"])
        await a.close()

    async def test_upstream_failure_returns_error_and_cross_origin_rejected(self):
        relay = SpeechRelay({"speech_service_url": ""})
        app = web.Application()
        app.router.add_get("/speech", relay.handle)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/speech", headers={"Origin": "http://unrelated.invalid"})
        self.assertEqual(response.status, 403)
        ws = await client.ws_connect("/speech")
        self.assertEqual((await ws.receive_json())["type"], "error")
        await ws.close()
