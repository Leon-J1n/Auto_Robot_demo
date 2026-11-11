"""Estimate a grasp or placement point and project it into MuJoCo world space."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from sensors.camera import CameraFrame, CameraIntrinsics, CameraPose


_PROMPT_FILES = {
    "grasp": "vlm_pick.json",
    "handle": "vlm_pick.json",
    "vertical": "vlm_pick.json",
    "place": "vlm_put.json",
}

from .config import load_vlm_config
from .depth import InvalidPointDepth, sample_depth


class VLMPointEstimator:
    """Use a VLM for image coordinates and metric depth for the 3-D point."""

    def __init__(self, client: Any = None, *, model: str | None = None) -> None:
        self._client = client
        self._settings = load_vlm_config()
        self.model = model or os.getenv("QWEN_MODEL") or self._settings["model"]

    def estimate_frame(
        self,
        frame: CameraFrame,
        *,
        point_type: str,
        target: str,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = self.estimate(
                frame.rgb, frame.depth, frame.intrinsics, frame.camera_pose,
                point_type=point_type, target=target, instruction=instruction)
        except InvalidPointDepth as exc:
            exc.details['details'].update(camera_id=frame.camera_id, sequence=frame.sequence)
            raise
        if (point_type == 'grasp' and result.get('axis_status') == 'invalid'
                and not result.get('axis_depth_failure')):
            reason = result['axis_reason']
            retry_instruction = ((instruction.strip() + '\n') if instruction else '') + (
                f'上一次长轴参考点不可用：{reason}。请重新判断是否为细长物体；'
                '若是，在抓取点两侧的可见表面内部选取两个局部长轴参考点，'
                '避开背景、轮廓和遮挡；无明确方向时返回 is_elongated=false、axis_points=null。')
            try:
                result = self.estimate(
                    frame.rgb, frame.depth, frame.intrinsics, frame.camera_pose,
                    point_type=point_type, target=target, instruction=retry_instruction)
            except ValueError as exc:
                # A bad retry must not discard the first valid grasp point.
                result.update(axis_status='invalid', axis_reason=f'{reason}；重试失败：{exc}')
            result.update(axis_retry_count=1, axis_initial_failure=reason)
        result.update(camera_id=frame.camera_id, timestamp=frame.timestamp, sequence=frame.sequence)
        return result

    def estimate(
        self,
        rgb: np.ndarray,
        depth: np.ndarray,
        intrinsics: CameraIntrinsics,
        camera_pose: CameraPose,
        *,
        point_type: str,
        target: str,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        if point_type not in _PROMPT_FILES:
            raise ValueError("point_type must be 'grasp', 'handle', 'vertical' or 'place'.")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("A nonempty target description is required.")
        rgb = np.asarray(rgb)
        depth = np.asarray(depth)
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise ValueError("rgb must be an HxWx3 uint8 RGB image.")
        height, width = rgb.shape[:2]
        if depth.shape != (height, width):
            raise ValueError("depth must match the RGB image height and width.")
        if (intrinsics.width, intrinsics.height) != (width, height):
            raise ValueError("Camera intrinsics do not match the image dimensions.")
        if not np.isfinite([intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy]).all() or intrinsics.fx <= 0 or intrinsics.fy <= 0:
            raise ValueError("Camera intrinsics must be finite with positive focal lengths.")
        rotation = np.asarray(camera_pose.rotation_matrix, dtype=float)
        position = np.asarray(camera_pose.position, dtype=float)
        if rotation.shape != (3, 3) or position.shape != (3,) or not np.isfinite(rotation).all() or not np.isfinite(position).all():
            raise ValueError("Camera pose must contain a finite 3x3 rotation and 3-D position.")

        from PIL import Image

        image = io.BytesIO()
        Image.fromarray(rgb, mode="RGB").save(image, format="PNG")
        image_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        task = self._load_prompt(point_type)
        target_name = target.strip()
        prompt = (f"目标物体：{task['target'].replace('{target}', target_name)}\n"
                  f"感知需求：{task['description']}\n"
                  f"输出结构：{task['respond']}\n")
        if instruction:
            prompt += f"补充要求：{instruction.strip()}\n"
        prompt += "请严格按照输出结构返回结果，只返回 JSON，不要 Markdown 或其他说明。"
        try:
            response = self._get_client().responses.create(
                model=self.model,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": image_url},
                ]}],
            )
        except Exception as exc:
            exception_name = type(exc).__name__.lower()
            timed_out = isinstance(exc, TimeoutError) or 'timeout' in exception_name
            response_failed = any(marker in exception_name for marker in ('apistatus', 'ratelimit'))
            payload = getattr(exc, 'details', None)
            if not isinstance(payload, dict):
                payload = {}
            payload.update(
                error_code=('VLM_REQUEST_TIMEOUT' if timed_out else
                            'VLM_RESPONSE_FAILED' if response_failed else 'VLM_REQUEST_FAILED'),
                error_message=str(exc) or type(exc).__name__,
                failed_stage='vlm_request',
            )
            exc.details = payload
            raise
        try:
            output_text = getattr(response, "output_text", None) or "".join(
                part.text for item in response.output if item.type == "message"
                for part in item.content if getattr(part, "text", None)
            )
            point = self._parse_point(output_text)
        except Exception as exc:
            exc.details = {
                'error_code': 'VLM_RESPONSE_FAILED',
                'error_message': str(exc),
                'failed_stage': 'vlm_response',
            }
            raise
        u = round(point["x"] * (width - 1))
        v = round(point["y"] * (height - 1))
        z, depth_sampling = sample_depth(depth, u, v,
                                        normalized={'x': point['x'], 'y': point['y']})

        # MuJoCo cameras look along local -Z, with local +Y pointing upward.
        camera_point = np.array([
            (u - intrinsics.cx) * z / intrinsics.fx,
            -(v - intrinsics.cy) * z / intrinsics.fy,
            -z,
        ])
        world_point = position + rotation @ camera_point
        result = {
            "point_type": point_type,
            "pixel": {"x": u, "y": v},
            "normalized": {"x": point["x"], "y": point["y"]},
            "depth_m": z,
            "depth_sampling": depth_sampling,
            "world_point": {"x": float(world_point[0]), "y": float(world_point[1]), "z": float(world_point[2])},
            "frame": "world",
            "confidence": point.get("confidence"),
        }
        if point_type in {'grasp', 'handle'}:
            from .axis import estimate_axis
            if point_type == 'grasp':
                result.update(estimate_axis(point, depth, intrinsics, position, rotation))
            if point_type == 'handle':
                from .handle import estimate_handle
                try:
                    result.update(estimate_handle(point, depth, intrinsics, position, rotation, result))
                except ValueError as exc:
                    exc.details = {'error_code': 'HANDLE_POSE_ESTIMATION_FAILED',
                        'error_message': str(exc), 'failed_stage': 'perception',
                        'details': {'estimate': result, 'references': point}}
                    raise
        return result

    @staticmethod
    def _load_prompt(point_type: str) -> dict[str, Any]:
        path = Path(__file__).with_name("prompts") / _PROMPT_FILES[point_type]
        try:
            prompt = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"找不到提示词文件: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"提示词文件不是有效 JSON: {path}") from exc
        if not isinstance(prompt, dict) or set(prompt) != {"target", "description", "respond"} or any(
            not isinstance(prompt.get(key), str) or not prompt[key].strip()
            for key in ("target", "description", "respond")
        ):
            raise ValueError(f"提示词文件必须包含非空的 target、description、respond: {path}")
        return prompt

    def _get_client(self) -> Any:
        if self._client is None:
            api_key = os.getenv("DASHSCOPE_API_KEY") or self._settings["api_key"]
            if not api_key.strip():
                raise ValueError("VLM api_key is missing; set config/vlm.json or DASHSCOPE_API_KEY")
            from openai import OpenAI

            self._client = OpenAI(
                api_key=api_key,
                base_url=self._settings["base_url"],
                timeout=self._settings["timeout_seconds"],
                max_retries=self._settings["max_retries"],
            )
        return self._client

    @staticmethod
    def _parse_point(text: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        try:
            result, _ = decoder.raw_decode(text.lstrip())
        except json.JSONDecodeError as exc:
            raise ValueError("VLM response must start with a JSON object.") from exc
        if not isinstance(result, dict):
            raise ValueError("VLM response must be a JSON object.")
        coordinates = result.get('grasp_point', result)
        if not isinstance(coordinates, dict):
            raise ValueError('VLM grasp_point must contain normalized x and y.')
        point = {}
        for key in ("x", "y"):
            value = coordinates.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"VLM {key} must be a finite normalized number in [0, 1].")
            point[key] = float(value)
        confidence = result.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not np.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("VLM confidence must be in [0, 1].")
            point["confidence"] = float(confidence)
        for key in ('is_elongated', 'axis_points', 'panel_points'):
            if key in result:
                point[key] = result[key]
        return point
