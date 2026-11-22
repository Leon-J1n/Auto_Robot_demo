"""Replay a saved presentation through real HTTP without calling a model.

Run with Python from the repository root. The source is read-only; all gateway
persistence and scene assets live in a temporary directory.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from agent_service.gateway_client import GatewayClient, normalize_frame_times
from sim_platform.live import connect
from web.gateway.agent import AgentResult
from web.gateway.assets import AssetService
from web.gateway.server import LIVE_ENDPOINT_KEY, create_app


class BenchmarkAgent:
    connected = True
    endpoint = "benchmark://no-model"

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def probe(self):
        return True

    async def list_conversations(self):
        return []

    async def ensure_conversation(self, **kwargs):
        return "benchmark"

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        await self.release.wait()
        return AgentResult("benchmark", "benchmark-turn", "complete", "succeeded")


async def benchmark(source: Path) -> dict:
    saved = json.loads(source.read_text(encoding="utf-8"))
    stored_frames = saved["timeline"]["frames"]
    base = saved["snapshot"]
    with tempfile.TemporaryDirectory(prefix="timeline-benchmark-") as directory:
        root = Path(directory)
        agent = BenchmarkAgent()
        app = create_app(agent_client=agent,
                         asset_service=AssetService(root / "uploads"),
                         presentation_storage=root / "presentation.json",
                         agent_state_storage=root / "agent-state.json")
        async with TestClient(TestServer(app)) as client:
            session = await asyncio.to_thread(connect, app[LIVE_ENDPOINT_KEY])
            try:
                native = (await asyncio.to_thread(session.scene_snapshot)).as_dict()
            finally:
                await asyncio.to_thread(session.close)
            binding = {key: native[key] for key in
                       ("schemaVersion", "sceneId", "worldId", "sceneGeneration")}
            # Saved browser snapshots have a synthetic World root; native frames do not.
            entities = {entity["id"]: entity for entity in base["entities"]
                        if entity["id"] != 0}
            frames = []
            for stored in stored_frames:
                patches = {entity["id"]: entity for entity in stored["entities"]}
                frames.append({**binding, "revision": stored.get("revision", 0),
                               "simulationTime": stored["simulationTime"],
                               "status": "paused", "entities": [
                                   {**entity, **patches.get(key, {})}
                                   for key, entity in entities.items()]})
            await client.post("/api/agent/jobs", json={"text": "benchmark"})
            for _ in range(200):
                if agent.calls:
                    break
                await asyncio.sleep(0.01)
            context = agent.calls[0]["control_context"]
            started = time.perf_counter()
            try:
                frames = normalize_frame_times(frames)
                async with GatewayClient(context["gatewayBaseUrl"],
                                         context["controlToken"], context["jobId"]) as publisher:
                    result = await publisher.publish_capture(frames)
                published = time.perf_counter()
                async with ClientSession() as browser:
                    async with browser.get(str(client.make_url(
                            result["timeline"]["dataUrl"]))) as response:
                        response.raise_for_status()
                        data = await response.read()
                downloaded = time.perf_counter()
                bundle = json.loads(data)
                parsed = time.perf_counter()
                assert len(bundle["frames"]) == len(frames)
                assert bundle["frames"][-1]["simulationTime"] == frames[-1]["simulationTime"]
                published_base = {entity["id"]: entity for entity in bundle["snapshot"]["entities"]}
                for expected, actual in zip(frames, bundle["frames"], strict=True):
                    patches = {entity["id"]: entity for entity in actual["entities"]}
                    for entity in expected["entities"]:
                        restored = {**published_base[entity["id"]], **patches.get(entity["id"], {})}
                        for key in ("local", "world"):
                            assert restored[key] == entity[key], (entity["id"], key)
                return {"frames": len(frames), "entities": len(entities),
                        "duration_seconds": frames[-1]["simulationTime"] - frames[0]["simulationTime"],
                        "publish_seconds": published - started,
                        "download_seconds": downloaded - published,
                        "total_seconds": parsed - started,
                        "all_frame_transforms_verified": True,
                        "bundle_bytes": len(data)}
            finally:
                agent.release.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(args.source)), indent=2))
