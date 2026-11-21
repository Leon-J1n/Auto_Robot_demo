"""Entry point scaffold for point-cloud grasp and placement estimation."""
from __future__ import annotations

from typing import Any, Mapping


class PointCloudPoseEstimator:
    """Estimate grasp or placement poses from caller-provided point clouds.

    Algorithm and sensor integrations are intentionally not implemented yet.
    """

    def estimate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        if operation not in {"grasp", "placement"}:
            raise ValueError("operation must be 'grasp' or 'placement'.")
        if "point_cloud" not in request:
            raise ValueError("point_cloud is required.")
        raise NotImplementedError("Point-cloud pose estimation is not implemented yet.")
