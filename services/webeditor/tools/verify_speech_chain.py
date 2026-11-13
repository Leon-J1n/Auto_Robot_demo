"""Real model -> ASR websocket -> real gateway relay, using disposable ports only."""
import asyncio
import json
import sys
import tempfile
import wave
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_gateway


async def main():
    service_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(service_root))
    import server
    config = json.loads((service_root / "config.json").read_text(encoding="utf-8"))
    config["provider"] = "cpu"
    recognizer, provider = server.create_recognizer(config)
    offline = server.create_offline_recognizer(config, provider)
    asr = TestServer(server.create_app(recognizer, provider, config, offline))
    await asr.start_server()
    try:
        with tempfile.TemporaryDirectory() as folder:
            gateway_config = {**local_gateway.DEFAULT_CONSTANT_CONFIG, "agent_service_url": "",
                              "speech_service_url": str(asr.make_url(""))}
            client = TestClient(TestServer(local_gateway.create_app(gateway_config, state_path=Path(folder) / "state.json")))
            await client.start_server()
            try:
                ws = await client.ws_connect("/speech")
                assert (await ws.receive_json())["type"] == "ready"
                filename = service_root / config["model_dir"] / "test_wavs" / "0.wav"
                with wave.open(str(filename)) as wav:
                    audio = wav.readframes(wav.getnframes())
                results = {}
                async def receive():
                    async for message in ws:
                        data = message.json()
                        if data["type"] == "error":
                            raise RuntimeError(data)
                        if data["type"] == "result":
                            results[data["segment"]] = data["text"]
                        if data["type"] == "done":
                            return
                    raise RuntimeError("Disconnected before done")
                reader = asyncio.create_task(receive())
                for offset in range(0, len(audio), 3200):
                    await ws.send_bytes(audio[offset:offset + 3200])
                    await asyncio.sleep(0.1)
                await ws.send_json({"type": "finish"})
                await asyncio.wait_for(reader, 60)
                text = "".join(results[key] for key in sorted(results))
                assert "研究" in text, text
                print("Gateway + real ASR transcript:", text)
                await ws.close()
            finally:
                await client.close()
    finally:
        await asr.close()


if __name__ == "__main__":
    asyncio.run(main())
