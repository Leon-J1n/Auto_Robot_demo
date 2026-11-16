"""Compatibility import for the WebEditor TRAJ v2 exporter.

The runtime imports this module from ``execution.output``.  Keep the
implementation in the historical module so older callers remain compatible.
"""

from execution.trajectory_export import (  # noqa: F401
    validate_traj,
    write_traj,
    write_traj_from_jsonl,
)

__all__ = ["validate_traj", "write_traj", "write_traj_from_jsonl"]
