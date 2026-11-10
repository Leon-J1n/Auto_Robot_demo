"""RGB-D acquisition stage for open-vocabulary object localization."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


class UnknownObjectLocate:
    """Generate localization candidates using open-vocabulary grounding and 3D projection."""

    strategy_id = "unknown_object_locate"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = config or {}

    def locate(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Capture the requested camera frame and leave VLM grounding pending.

        This deliberately stops before semantic detection, segmentation and
        3-D pose estimation.  The frame arrays remain available in the result
        for an in-process caller; when ``save_dir`` is supplied, the runtime
        also writes RGB and depth files for inspection.
        """
        runtime = context.get("runtime")
        if runtime is None or not hasattr(runtime, "get_camera_frame"):
            raise RuntimeError("Unknown Object Locate requires a camera runtime.")

        params = dict(request.get("strategy_params", {}))
        camera_id = str(params.get("camera_id", "global"))
        frame = runtime.get_camera_frame(camera_id)
        saved_files = {}
        save_dir = params.get("save_dir")
        if save_dir:
            save_dir = Path(save_dir)
            if not save_dir.is_absolute():
                scene_path = getattr(runtime, "scene_path", None)
                project_root = (
                    Path(scene_path).resolve().parents[2]
                    if scene_path is not None
                    else Path.cwd()
                )
                save_dir = project_root / save_dir
            saved_files = {
                key: str(value)
                for key, value in runtime.save_camera_frame(
                    camera_id, save_dir, prefix=params.get("file_prefix")
                ).items()
            }

        target = request.get("target", {})
        description = (
            params.get("target_description")
            or target.get("object")
            or target.get("instruction")
        )
        return {
            "observation_only": True,
            "target_pose": None,
            "object_pose": None,
            "confidence": 0.0,
            "source": "camera_rgbd",
            "candidates": [],
            "observation": {
                "camera_id": frame.camera_id,
                "timestamp": frame.timestamp,
                "sequence": frame.sequence,
                "rgb": frame.rgb,
                "depth": frame.depth,
                "rgb_shape": list(frame.rgb.shape),
                "depth_shape": list(frame.depth.shape),
                "depth_unit": "meter",
                "intrinsics": frame.intrinsics.matrix,
                "camera_pose": frame.camera_pose,
                "saved_files": saved_files,
            },
            "vlm": {
                "status": "pending_implementation",
                "target_description": description,
            },
        }
