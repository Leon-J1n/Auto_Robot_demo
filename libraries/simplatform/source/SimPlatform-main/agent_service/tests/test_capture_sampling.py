from types import SimpleNamespace

from agent_service.runtime.executor import _FrameCapture


def test_capture_samples_before_requesting_and_reuses_static_scene():
    calls = {"full": 0, "transforms": 0}
    runtime = SimpleNamespace(_simulation_time=0.0, execution_mode="actuator")

    def compact():
        return {"sceneGeneration": 1, "simulationTime": runtime._simulation_time,
                "entities": [{"id": 1, "local": {"translation": [runtime._simulation_time, 0, 0]},
                              "world": {"translation": [runtime._simulation_time, 0, 0]}}]}

    def full():
        calls["full"] += 1
        frame = compact()
        frame["entities"][0]["visual"] = {"mesh": "static-mesh"}
        return SimpleNamespace(as_dict=lambda: frame)

    def transforms():
        calls["transforms"] += 1
        return compact()

    runtime.session = SimpleNamespace(scene_snapshot=full, scene_transforms=transforms)
    capture = _FrameCapture(runtime, max_frames=None, enabled=True)
    capture.capture()
    for tick in range(1, 501):
        runtime._simulation_time = tick * 0.002
        capture.capture_at_fps()
    assert calls == {"full": 1, "transforms": 50}
    assert len(capture.frames) == 51
    assert capture.frames[0]["entities"][0]["world"]["translation"][0] == 0
    assert capture.frames[-1]["entities"][0]["world"]["translation"][0] == 1
    assert capture.frames[0]["entities"][0]["visual"] is capture.frames[-1]["entities"][0]["visual"]
