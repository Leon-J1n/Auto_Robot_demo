from types import SimpleNamespace

from agent_service.qwen_runner import PlatformAgentRunner


def test_debug_scene_seed_requires_a_numeric_debug_command():
    assert PlatformAgentRunner._debug_scene_seed("DEBUG42") == 42
    assert PlatformAgentRunner._debug_scene_seed("DEBUG -7") == -7
    assert PlatformAgentRunner._debug_scene_seed("DEBUG") is None
    assert PlatformAgentRunner._debug_scene_seed("DEBUG42 extra") is None
    assert PlatformAgentRunner._debug_scene_seed("move banana") is None


def test_compose_uses_explicit_debug_seed(monkeypatch):
    runner = object.__new__(PlatformAgentRunner)
    intent = SimpleNamespace(
        entities=[SimpleNamespace(entity_id="red_ball", semantic_name="红色球",
                                   category="object", aliases=[])],
        operations=[], spatial_relations=[],
    )
    first = runner._compose(intent, placement_seed=123)[0]
    second = runner._compose(intent, placement_seed=123)[0]
    assert first == second


def test_compose_does_not_use_catalog_seed_by_default(monkeypatch):
    runner = object.__new__(PlatformAgentRunner)
    intent = SimpleNamespace(
        entities=[SimpleNamespace(entity_id="red_ball", semantic_name="红色球",
                                   category="object", aliases=[])],
        operations=[], spatial_relations=[],
    )
    seeds = iter((101, 202))

    class _SystemRandom:
        def randrange(self, *_args):
            return next(seeds)

    monkeypatch.setattr("agent_service.qwen_runner.random.SystemRandom", _SystemRandom)
    first = runner._compose(intent)[0]
    second = runner._compose(intent)[0]
    assert first != second
