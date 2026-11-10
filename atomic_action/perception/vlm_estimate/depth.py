"""Read metric depth, filling invalid samples from a small local mean."""
import numpy as np


DEPTH_NEIGHBOUR_RADIUS_PX = 2  # A 5x5 window, clipped at image boundaries.


def _depth_state(depth):
    return ('NaN' if np.isnan(depth) else '正无穷' if np.isposinf(depth) else
            '负无穷' if np.isneginf(depth) else str(depth))


class InvalidPointDepth(ValueError):
    """Neither the selected pixel nor its local window supplies depth."""

    def __init__(self, u, v, depth, normalized, width, height, sampling=None):
        state = _depth_state(depth)
        message = f'VLM 选点 ({u}, {v}) 深度无效（{state}）'
        if sampling is not None:
            message += '，且周围 5×5 范围内无有效深度'
        message += '，无法换算三维坐标'
        super().__init__(message)
        self.details = {'error_code': 'VLM_DEPTH_INVALID', 'failed_stage': 'perception',
            'error_message': message, 'details': {
                'pixel': {'x': u, 'y': v}, 'normalized': normalized,
                'image_size': {'width': width, 'height': height}, 'depth_state': state}}
        if sampling is not None:
            self.details['details']['depth_sampling'] = sampling


def sample_depth(depth, u, v, *, normalized=None):
    """Keep valid depth; otherwise average positive, finite local samples.

    The caller keeps the original image ray. No pixels in the input are changed.
    """
    z = float(depth[v, u])
    if np.isfinite(z) and z > 0:
        return z, {'method': 'pixel', 'valid_samples': 1}
    height, width = depth.shape
    radius = DEPTH_NEIGHBOUR_RADIUS_PX
    x0, x1 = max(0, u - radius), min(width, u + radius + 1)
    y0, y1 = max(0, v - radius), min(height, v + radius + 1)
    window = depth[y0:y1, x0:x1]
    valid = window[np.isfinite(window) & (window > 0)]
    sampling = {'method': 'neighbour_mean', 'radius_px': radius,
                'bounds_xyxy': [x0, y0, x1, y1],
                'valid_samples': int(valid.size), 'selected_depth_state': _depth_state(z)}
    if not valid.size:
        raise InvalidPointDepth(u, v, z, normalized, width, height, sampling)
    return float(np.mean(valid, dtype=np.float64)), sampling
