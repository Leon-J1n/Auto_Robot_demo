from sim_platform.server import HeadlessService


def test_compact_capture_and_mutations_do_not_read_full_snapshot(monkeypatch):
    service = HeadlessService(run_simulation=False)
    try:
        service.world.create_primitive("cube", "box", (0.1, 0.1, 0.1))
        before = service.simulator.scene_snapshot(service.world).as_dict()
        compact = service.simulator.scene_transforms(service.world)
        assert compact["entities"]
        for item, original in zip(compact["entities"], before["entities"]):
            assert item == {key: original[key] for key in item}
            assert not ({"visual", "collider", "rigidBody", "robot"} & item.keys())
        def forbidden(*args):
            raise AssertionError("full snapshot must not be serialized")
        monkeypatch.setattr(service.simulator, "scene_snapshot", forbidden)
        for _ in range(10):
            result = service.dispatch({"method": "simulation.step", "params": {"count": 10, "paced": False}})
            assert result["ok"], result
        result = service.dispatch({"method": "scene.transforms"})
        assert result["ok"], result
        assert abs(result["result"]["simulationTime"] - 0.2) < 1e-9
        generation, revision = service.simulator.scene_metadata(service.world)
        assert service.scene_generation == generation
        assert service.scene_revision >= revision
    finally:
        service.close()
