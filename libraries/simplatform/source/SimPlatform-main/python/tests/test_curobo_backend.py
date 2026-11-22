from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from sim_platform.curobo_backend import CuroboCollisionBackend, _CuroboRuntime


class _FakeTensor:
    def __init__(self, values: object):
        self.values = np.asarray(values)

    @property
    def shape(self) -> tuple[int, ...]:
        return self.values.shape

    def unsqueeze(self, axis: int) -> _FakeTensor:
        return _FakeTensor(np.expand_dims(self.values, axis))

    def detach(self) -> _FakeTensor:
        return self

    def reshape(self, *shape: int) -> _FakeTensor:
        return _FakeTensor(self.values.reshape(*shape))

    def cpu(self) -> _FakeTensor:
        return self

    def numpy(self) -> np.ndarray:
        return self.values

    def __getitem__(self, item: object) -> np.ndarray:
        return self.values[item]


class _FakeTorch:
    float32 = np.float32

    @staticmethod
    def device(value: str) -> str:
        return value

    @staticmethod
    def no_grad() -> nullcontext[None]:
        return nullcontext()


class _FakeDeviceCfg:
    def __init__(self, *, device: str, dtype: object):
        self.device = device
        self.dtype = dtype

    def to_device(self, values: np.ndarray) -> _FakeTensor:
        return _FakeTensor(np.asarray(values, dtype=self.dtype))


class _FakeCheckerCfg:
    calls: list[dict[str, object]] = []

    @classmethod
    def load_from_config(cls, **values: object) -> object:
        cls.calls.append(values)
        return SimpleNamespace()


class _FakeChecker:
    def __init__(self, _config: object):
        self.kinematics = SimpleNamespace(joint_names=("joint_b", "joint_a"))
        self.last_query: _FakeTensor | None = None

    def validate(self, query: _FakeTensor) -> _FakeTensor:
        self.last_query = query
        return _FakeTensor(query.values.sum(axis=-1) == 0.0)

    def validate_trajectory(self, query: _FakeTensor) -> _FakeTensor:
        self.last_query = query
        return _FakeTensor(query.values.sum(axis=-1) == 0.0)


def _backend() -> CuroboCollisionBackend:
    runtime = _CuroboRuntime(_FakeTorch, _FakeDeviceCfg, _FakeCheckerCfg, _FakeChecker)
    return CuroboCollisionBackend("robot.yml", {"cuboid": {}}, device="cpu", _runtime=runtime)


def test_curobo_backend_maps_platform_joint_order_and_fixed_joints() -> None:
    backend = _backend()
    names = ("fixed", "joint_a", "joint_b")
    collisions = backend.configurations_collide([[99.0, 1.0, -1.0], [99.0, 1.0, 2.0]], names)

    assert collisions.tolist() == [False, True]
    assert backend.configuration_collides([99.0, 1.0, 2.0], names)
    assert backend._checker.last_query.shape == (1, 1, 2)
    assert backend._checker.last_query[0, 0].tolist() == [2.0, 1.0]


def test_curobo_backend_checks_trajectories_and_rebuilds_scene() -> None:
    backend = _backend()
    names = ("fixed", "joint_a", "joint_b")
    trajectories = np.array([[[0.0, 1.0, -1.0], [0.0, 2.0, 1.0]]])

    assert backend.trajectories_collide(trajectories, names).tolist() == [[False, True]]
    backend.update_scene({"mesh": {"obstacle": {}}})
    assert _FakeCheckerCfg.calls[-1]["scene_model"] == {"mesh": {"obstacle": {}}}


def test_curobo_backend_rejects_incompatible_joint_names() -> None:
    backend = _backend()
    with pytest.raises(ValueError, match="missing"):
        backend.validate_platform_joints(("joint_a",))
    with pytest.raises(ValueError, match="not unique"):
        backend.validate_platform_joints(("joint_a", "joint_a", "joint_b"))
