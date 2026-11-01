"""Validation shared by local and JSON position-control transports."""

import math
from typing import Iterable


def effort_limit_values(limits: Iterable[float | None], count: int) -> list[float | None]:
    values = list(limits)
    if len(values) != count:
        raise ValueError("joint effort limit count does not match joint targets")
    result: list[float | None] = []
    for value in values:
        if value is None:
            result.append(None)
        elif isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError("effort limits must be finite nonnegative numbers or None")
        else:
            result.append(float(value))
    return result
