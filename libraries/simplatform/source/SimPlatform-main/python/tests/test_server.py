from __future__ import annotations

import tempfile
from multiprocessing import shared_memory

from sim_platform.server import HeadlessService


def test_agent_rpc_scene_state_and_bqlog() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = HeadlessService(cache_directory=f"{directory}/cache")
        try:
            created = service.dispatch({"id": 1, "method": "scene.create_entity",
                                        "params": {"name": "obstacle"}})
            assert created["ok"]
            entity = created["result"]["entity"]
            changed = service.dispatch({"id": 2, "method": "scene.set_transform",
                                        "params": {"entity": entity, "translation": [1, 2, 3]}})
            assert changed["ok"]
            queried = service.dispatch({"id": 3, "method": "scene.query"})
            assert queried["result"]["entities"][0]["translation"] == [1.0, 2.0, 3.0]
            saved = service.dispatch({"id": 4, "method": "state.save"})["result"]["state"]
            service.dispatch({"id": 5, "method": "simulation.step", "params": {"count": 3}})
            restored = service.dispatch({"id": 6, "method": "state.restore",
                                         "params": {"state": saved}})
            assert restored["result"]["time"] == 0.0
            invalid = service.dispatch({"id": 7, "method": "scene.set_transform",
                                        "params": {"entity": 999}})
            assert not invalid["ok"] and invalid["error"]["code"] == "invalid_handle"
            allocated = service.dispatch({"id": 8, "method": "shared_memory.allocate",
                                          "params": {"size": 64}})
            descriptor = allocated["result"]
            client = shared_memory.SharedMemory(name=descriptor["name"])
            try:
                client.buf[:4] = b"SIM1"
                assert bytes(service.shared_buffers[descriptor["name"]].buf[:4]) == b"SIM1"
            finally:
                client.close()
            released = service.dispatch({"id": 9, "method": "shared_memory.release",
                                         "params": {"name": descriptor["name"]}})
            assert released["result"]["released"]
        finally:
            service.close()
