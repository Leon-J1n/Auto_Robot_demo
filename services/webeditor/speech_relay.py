"""Dedicated browser-to-ASR WebSocket relay; never shares scene/event queues."""
import asyncio
import json
import logging
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

LOGGER = logging.getLogger(__name__)


class SpeechRelay:
    def __init__(self, config):
        self.config = config
        self.sockets = set()

    async def handle(self, request):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Cross-origin speech connections are not allowed")
        if len(self.sockets) >= 8:
            raise web.HTTPServiceUnavailable(text="Speech connections are busy")
        downstream = web.WebSocketResponse(heartbeat=15, max_msg_size=32768)
        self.sockets.add(downstream)
        try:
            await downstream.prepare(request)
            base = str(self.config.get("speech_service_url") or "").rstrip("/")
            if not base:
                raise ValueError("Speech service is not configured")
            async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=5)) as client:
                async with client.ws_connect(base + "/stream", heartbeat=15, max_msg_size=65536) as upstream:
                    async def audio():
                        async for message in downstream:
                            if message.type == WSMsgType.BINARY:
                                await upstream.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                payload = json.loads(message.data)
                                if payload != {"type": "finish"}:
                                    raise ValueError("Unsupported speech control message")
                                await upstream.send_json(payload)
                            elif message.type == WSMsgType.ERROR:
                                break

                    async def results():
                        async for message in upstream:
                            if message.type == WSMsgType.TEXT:
                                await downstream.send_str(message.data)
                            elif message.type == WSMsgType.ERROR:
                                break

                    tasks = [asyncio.create_task(audio()), asyncio.create_task(results())]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning("Speech relay disconnected: %s", error)
            if downstream.prepared and not downstream.closed:
                await downstream.send_json({"type": "error", "message": "语音服务连接失败，请检查服务是否启动。"})
        finally:
            self.sockets.discard(downstream)
            if downstream.prepared:
                await downstream.close()
        return downstream

    async def cleanup(self, app):
        await asyncio.gather(*(socket.close() for socket in list(self.sockets)))
