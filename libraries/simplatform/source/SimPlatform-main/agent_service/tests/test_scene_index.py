from __future__ import annotations

from agent_service.runtime.scene_index import SceneIndex


class _Snapshot:
    revision = 1
    scene_generation = 1
    entities = (
        {"id": 1, "name": "robot", "parent": None, "robot": {}},
        {"id": 10, "name": "red_ball", "parent": None,
         "rigidBody": {"enabled": True, "dynamic": True}},
        {"id": 11, "name": "red_ball_mesh", "parent": 10},
        {"id": 20, "name": "green_box", "parent": None,
         "rigidBody": {"enabled": True, "dynamic": False}},
        {"id": 21, "name": "green_box_wall", "parent": 20},
    )


class _Session:
    def scene_snapshot(self):
        return _Snapshot()


class _Robot:
    handle = 1

    def list_links(self):
        return ()


def test_scene_index_reads_dynamic_state_from_object_root() -> None:
    scene = SceneIndex(_Session(), _Robot())

    assert scene.is_dynamic_object(10)
    assert scene.is_dynamic_object(11)
    assert not scene.is_dynamic_object(20)
    assert not scene.is_dynamic_object(21)
