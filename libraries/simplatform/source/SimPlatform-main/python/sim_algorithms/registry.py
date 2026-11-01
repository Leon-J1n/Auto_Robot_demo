from __future__ import annotations

from collections.abc import Callable
from typing import Any


registry: dict[str, Callable[..., Any]] = {}


def register(name: str, function: Callable[..., Any]) -> None:
    if not name:
        raise ValueError("algorithm name must not be empty")
    if name in registry:
        raise ValueError(f"algorithm already registered: {name}")
    registry[name] = function


def get(name: str) -> Callable[..., Any]:
    try:
        return registry[name]
    except KeyError:
        raise KeyError(f"algorithm not registered: {name}") from None


def call(name: str, *args: Any, **kwargs: Any) -> Any:
    return get(name)(*args, **kwargs)


def names() -> tuple[str, ...]:
    return tuple(sorted(registry))
