"""CPU-only execution controls shared by the cuRobo client and worker."""
from __future__ import annotations

import math


def positive_timeout(value: object, name: str) -> float:
    """Validate a finite, positive wall-clock budget in seconds."""
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite positive number')
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f'{name} must be a finite positive number')
    return result


def request_budgets(config: dict, op: str, payload: dict) -> tuple[float, float, float]:
    """Return total, initialization and solve budgets; none extends the total."""
    request = payload.get('request') or {}
    total = positive_timeout(config.get('request_timeout', 120.), 'request_timeout')
    requested = request.get('constraints', {}).get('timeout')
    if requested is not None:
        total = min(total, positive_timeout(requested, 'constraints.timeout'))
    initialization = min(total, positive_timeout(
        config.get('initialization_timeout', 60.), 'initialization_timeout'))
    planning = request.get('planning', {}).get('planning_time') if op == 'plan' else None
    solve = total if planning is None else min(total, positive_timeout(planning, 'planning_time'))
    return total, initialization, solve


def fatal_cuda_error(error: BaseException) -> bool:
    """Identify CUDA contexts that must never serve another request."""
    message = str(error).lower()
    return any(marker in message for marker in (
        'illegal memory access', 'illegal instruction', 'device-side assert',
        'misaligned address', 'unspecified launch failure',
        'cuda error 700', 'cuda error 710', 'cuda error 719',
    ))
