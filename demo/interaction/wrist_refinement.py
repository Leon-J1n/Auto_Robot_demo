"""Local wrist RGB-D refinement of an already selected grasp."""
import numpy as np
from .object_library import GraspResolutionError

def refinement_error(message, **details):
    error = GraspResolutionError(message)
    error.details = {'error_code': 'WRIST_GRASP_REFINEMENT_FAILED',
                     'failed_stage': 'wrist_refinement', 'error_message': message,
                     'details': details}
    return error


def refine_surface(estimator, frame, surface, target, *, vertical=True):
    """Locate the target in the current wrist frame or its selected region."""
    instruction = ('这是接近完成后的腕部图像。重新定位指定的同一目标物体的可见抓取点。'
        '本次只修正位置，朝向已确定，无需长轴参考点，返回 is_elongated=false、axis_points=null。'
        '目标被遮挡或无法确认时返回 confidence=0。')
    estimate = estimator.estimate_frame(frame, point_type='vertical' if vertical else 'grasp',
                                        target=target, instruction=instruction)
    confidence = estimate.get('confidence')
    if confidence is None or not np.isfinite(confidence) or confidence < .5:
        raise refinement_error('腕部图像无法可靠确认原抓取目标', estimate=estimate)
    point = np.array([estimate['world_point'][key] for key in 'xyz'], dtype=float)
    if not np.isfinite(point).all():
        raise refinement_error('腕部抓取点的三维坐标无效', estimate=estimate)
    delta = point - surface
    return estimate, {'camera_id': frame.camera_id, 'timestamp': frame.timestamp, 'sequence': frame.sequence,
        'coarse_surface_world': surface.tolist(), 'refined_surface_world': point.tolist(),
        'correction_world_m': delta.tolist(), 'correction_distance_m': float(np.linalg.norm(delta)),
        'image_region': 'full_frame'}
