"""Discover manifests without importing optional solver dependencies."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path

from .contracts import IKBackend, IKError, RobotModel


@dataclass(frozen=True)
class SolverDescriptor:
    name: str
    factory: str
    aliases: tuple[str, ...] = ()
    auto_priority: int | None = None


class SolverRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, SolverDescriptor] = {}
        self._names: dict[str, str] = {}
        self.discovery_errors: dict[str, str] = {}

    @classmethod
    def discover(cls, root: str | Path | None = None) -> SolverRegistry:
        registry = cls()
        directory = Path(root) if root is not None else Path(__file__).resolve().parents[1] / "libraries"
        for manifest in sorted(directory.glob("*/solver.json")):
            try:
                spec = json.loads(manifest.read_text(encoding="utf-8"))
                if spec.get("api_version") != 1:
                    raise ValueError("Expected api_version: 1")
                if not isinstance(spec.get("aliases", []), list):
                    raise ValueError("aliases must be a list of strings")
                registry.register(SolverDescriptor(
                    name=spec["name"], factory=spec["factory"],
                    aliases=tuple(spec.get("aliases", [])),
                    auto_priority=spec.get("auto_priority")))
            except Exception as exc:
                registry.discovery_errors[str(manifest)] = str(exc)
        return registry

    def register(self, descriptor: SolverDescriptor) -> None:
        names = (descriptor.name, *descriptor.aliases)
        if (not all(isinstance(name, str) and name and name != "auto" for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("Solver names/aliases must be unique non-empty strings other than auto")
        if (not isinstance(descriptor.factory, str) or descriptor.factory.count(":") != 1
                or not all(descriptor.factory.split(":"))):
            raise ValueError("Factory must be module.path:callable")
        if descriptor.auto_priority is not None and (isinstance(descriptor.auto_priority, bool)
                                                    or not isinstance(descriptor.auto_priority, int)):
            raise ValueError("auto_priority must be an integer or null")
        if any(name in self._names for name in names):
            raise ValueError(f"Duplicate solver name or alias: {names}")
        self._descriptors[descriptor.name] = descriptor
        self._names.update({name: descriptor.name for name in names})

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._names))

    @property
    def auto_backends(self) -> list[str]:
        eligible = [d for d in self._descriptors.values() if d.auto_priority is not None]
        return [d.name for d in sorted(eligible, key=lambda d: (d.auto_priority, d.name))]

    def resolve(self, name: str) -> str:
        if not isinstance(name, str):
            raise IKError("IK backend name must be a string")
        try:
            return self._names[name]
        except KeyError:
            raise IKError(f"Unknown IK backend {name!r}; registered: {', '.join(self.names)}") from None

    def create(self, name: str, robot: RobotModel) -> IKBackend:
        descriptor = self._descriptors[self.resolve(name)]
        module_name, factory_name = descriptor.factory.split(":")
        backend = getattr(importlib.import_module(module_name), factory_name)(robot)
        if not backend.supports(robot):
            raise IKError(f"{descriptor.name} does not support this robot model")
        return backend


def get_registry() -> SolverRegistry:
    """A fresh manifest snapshot; newly added libraries are seen by new services."""
    return SolverRegistry.discover()
