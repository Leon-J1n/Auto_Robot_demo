"""Locate strategy for RGB-D VLM point estimation."""

from __future__ import annotations

from typing import Any, Mapping

from atomic_action.perception.vlm_estimate import VLMPointEstimator


class VLMEstimate:
    strategy_id = "vlm_estimate"

    def __init__(self, config: Mapping[str, Any] | None = None, *, estimator: VLMPointEstimator | None = None) -> None:
        self.config = config or {}
        self.estimator = estimator or VLMPointEstimator()

    def locate(self, request: Mapping[str, Any], context: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = context.get("runtime")
        if runtime is None or not hasattr(runtime, "get_camera_frame"):
            raise RuntimeError("VLM Estimate requires a camera runtime.")
        target = request["target"]
        point_type = target["type"]
        if point_type not in {"grasp", "place"}:
            raise ValueError("VLM Estimate supports grasp and place targets only.")
        params = request.get("strategy_params", {})
        description = params.get("target_description") or target.get("object") or target.get("category")
        if not description:
            raise ValueError("VLM Estimate requires target.object, target.category or strategy_params.target_description.")
        frame = runtime.get_camera_frame(str(params.get("camera_id", "global")))
        point = self.estimator.estimate_frame(
            frame, point_type=point_type, target=str(description),
            instruction=target.get("instruction"),
        )
        return {
            "point_only": True,
            "target_pose": None,
            "world_point": point["world_point"],
            "confidence": point["confidence"],
            "source": "vlm_estimate",
            "estimate": point,
        }
