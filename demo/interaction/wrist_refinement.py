"""Local wrist RGB-D refinement of an already selected grasp."""
import numpy as np
from .object_library import GraspResolutionError

MAX_CORRECTION_M = .10
MAX_VERTICAL_CORRECTION_M = .05


def refinement_error(message, **details):
    error = GraspResolutionError(message)
    error.details = {'error_code': 'WRIST_GRASP_REFINEMENT_FAILED',
                     'failed_stage': 'wrist_refinement', 'error_message': message,
                     'details': details}
    return error


def refine_surface(estimator, frame, surface, target, *, vertical=True):
    """Locate the target in the complete current wrist frame."""
    instruction = ('这是接近完成后的完整腕部图像。重新定位指定的同一目标物体的可见抓取点。'
        '本次只修正位置，朝向已确定，无需长轴参考点，返回 is_elongated=false、axis_points=null。'
        '目标被遮挡或无法确认时返回 confidence=0。')
    estimate = estimator.estimate_frame(frame, point_type='vertical' if vertical else 'grasp',
                                        target=target, instruction=instruction)
    confidence = estimate.get('confidence')
    if confidence is None or not np.isfinite(confidence) or confidence < .5:
        raise refinement_error('腕部图像无法可靠确认原抓取目标', estimate=estimate)
    point = np.array([estimate['world_point'][key] for key in 'xyz'], dtype=float)
    delta = point - surface
    if not np.isfinite(point).all() or np.linalg.norm(delta) > MAX_CORRECTION_M or abs(delta[2]) > MAX_VERTICAL_CORRECTION_M:
        raise refinement_error('腕部精定位偏移过大，可能选中了其他部件', estimate=estimate,
            correction_world_m=delta.tolist(), maximum_correction_m=MAX_CORRECTION_M,
            maximum_vertical_correction_m=MAX_VERTICAL_CORRECTION_M)
    return estimate, {'camera_id': frame.camera_id, 'timestamp': frame.timestamp, 'sequence': frame.sequence,
        'coarse_surface_world': surface.tolist(), 'refined_surface_world': point.tolist(),
        'correction_world_m': delta.tolist(), 'correction_distance_m': float(np.linalg.norm(delta)),
        'image_region': 'full_frame'}
