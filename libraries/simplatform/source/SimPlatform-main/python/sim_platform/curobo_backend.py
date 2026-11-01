from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, Iterable, Sequence

import numpy as np


class CuroboUnavailableError(RuntimeError):
    """Raised when the optional cuRobo runtime cannot be used."""


@dataclass(frozen=True)
class _CuroboRuntime:
    torch: Any
    device_cfg_type: Any
    checker_cfg_type: Any
    checker_type: Any


def _load_runtime() -> _CuroboRuntime:
    try:
        import torch
        from curobo.collision_checking import RobotCollisionChecker, RobotCollisionCheckerCfg
        from curobo.types import DeviceCfg
    except (ImportError, OSError, RuntimeError) as error:
        raise CuroboUnavailableError(
            "cuRobo is unavailable; install the CUDA-specific sim-platform extra in a "
            "supported Linux environment"
        ) from error
    return _CuroboRuntime(torch, DeviceCfg, RobotCollisionCheckerCfg, RobotCollisionChecker)


class CuroboCollisionBackend:
    """Optional GPU backend for batched robot configuration collision queries.

    The backend owns cuRobo's robot and scene model. Platform joint arrays are reordered by
    name, which removes fixed joints that are not degrees of freedom in cuRobo.
    """

    def __init__(
        self,
        robot_config: object,
        scene_model: object = None,
        *,
        device: str = "cuda:0",
        _runtime: _CuroboRuntime | None = None,
    ) -> None:
        runtime_supplied = _runtime is not None
        self._runtime = _runtime or _load_runtime()
        self._device = device
        self._robot_config = robot_config
        self._scene_model = scene_model
        if not runtime_supplied:
            self._validate_device()
        self._build_checker()

    @staticmethod
    def is_installed() -> bool:
        return find_spec("curobo") is not None and find_spec("torch") is not None

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(str(name) for name in self._checker.kinematics.joint_names)

    def _validate_device(self) -> None:
        torch = self._runtime.torch
        device = torch.device(self._device)
        if device.type != "cuda":
            raise CuroboUnavailableError("cuRobo collision queries require a CUDA device")
        if not torch.cuda.is_available():
            raise CuroboUnavailableError("PyTorch cannot access an NVIDIA CUDA device")
        major, _minor = torch.cuda.get_device_capability(device)
        if major < 8:
            raise CuroboUnavailableError(
                "cuRobo requires a post-Turing NVIDIA GPU (compute capability 8.0 or newer)"
            )

    def _build_checker(self) -> None:
        torch = self._runtime.torch
        device_cfg = self._runtime.device_cfg_type(
            device=torch.device(self._device), dtype=torch.float32
        )
        config = self._runtime.checker_cfg_type.load_from_config(
            robot_config=self._robot_config,
            scene_model=self._scene_model,
            device_cfg=device_cfg,
        )
        self._device_cfg = device_cfg
        self._checker = self._runtime.checker_type(config)

    def update_scene(self, scene_model: object) -> None:
        """Replace the cuRobo scene and rebuild its GPU collision cache."""
        self._scene_model = scene_model
        self._build_checker()

    def validate_platform_joints(self, platform_joint_names: Sequence[str]) -> None:
        names = tuple(platform_joint_names)
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"platform joint names are not unique: {sorted(duplicates)}")
        missing = [name for name in self.joint_names if name not in names]
        if missing:
            raise ValueError(f"cuRobo joints are missing from the platform robot: {missing}")

    def _active_positions(
        self, positions: np.ndarray, platform_joint_names: Sequence[str] | None
    ) -> np.ndarray:
        if platform_joint_names is None:
            if positions.shape[-1] != len(self.joint_names):
                raise ValueError(
                    f"positions must end with {len(self.joint_names)} cuRobo joints"
                )
            return positions
        self.validate_platform_joints(platform_joint_names)
        if positions.shape[-1] != len(platform_joint_names):
            raise ValueError("positions width does not match platform_joint_names")
        indices = {name: index for index, name in enumerate(platform_joint_names)}
        return np.ascontiguousarray(positions[..., [indices[name] for name in self.joint_names]])

    @staticmethod
    def _as_array(positions: Iterable[Any] | np.ndarray) -> np.ndarray:
        if isinstance(positions, np.ndarray):
            return np.asarray(positions, dtype=np.float64)
        return np.asarray(list(positions), dtype=np.float64)

    def configurations_collide(
        self,
        positions: Iterable[Iterable[float]] | np.ndarray,
        platform_joint_names: Sequence[str] | None = None,
    ) -> np.ndarray:
        values = self._as_array(positions)
        if values.ndim != 2:
            raise ValueError("positions must have shape (configuration_count, joint_count)")
        active = self._active_positions(values, platform_joint_names)
        query = self._device_cfg.to_device(active).unsqueeze(1)
        with self._runtime.torch.no_grad():
            valid = self._checker.validate(query)
        return np.logical_not(valid.detach().reshape(-1).cpu().numpy().astype(np.bool_))

    def configuration_collides(
        self,
        positions: Iterable[float] | np.ndarray,
        platform_joint_names: Sequence[str] | None = None,
    ) -> bool:
        values = self._as_array(positions)
        if values.ndim != 1:
            raise ValueError("positions must have shape (joint_count,)")
        return bool(self.configurations_collide(values.reshape(1, -1), platform_joint_names)[0])

    def trajectories_collide(
        self,
        positions: Iterable[Iterable[Iterable[float]]] | np.ndarray,
        platform_joint_names: Sequence[str] | None = None,
    ) -> np.ndarray:
        values = self._as_array(positions)
        if values.ndim != 3:
            raise ValueError("positions must have shape (batch, horizon, joint_count)")
        active = self._active_positions(values, platform_joint_names)
        query = self._device_cfg.to_device(active)
        with self._runtime.torch.no_grad():
            valid = self._checker.validate_trajectory(query)
        return np.logical_not(valid.detach().cpu().numpy().astype(np.bool_))
