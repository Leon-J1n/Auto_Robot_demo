"""Shared defaults, loaded from the project config independently of cwd."""
from copy import deepcopy
import json
import math
from pathlib import Path


GLOBAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'global.json'
_KEYS = {'motion': {'velocity_scale', 'acceleration_scale'},
         'trajectory': {'fps', 'pause_seconds'}}


def validate_config(config, *, partial=False):
    if not isinstance(config, dict) or set(config) - set(_KEYS):
        raise ValueError('Config requires motion and trajectory objects')
    for section, keys in _KEYS.items():
        if partial and section not in config:
            continue
        values = config.get(section)
        if not isinstance(values, dict) or set(values) - keys or (not partial and set(values) != keys):
            raise ValueError(f'Invalid {section} config keys; expected {sorted(keys)}')
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f'{section}.{key} must be finite numeric data')
            if section == 'motion' and not 0.01 <= value <= 1.0:
                raise ValueError(f'motion.{key} must be between 0.01 and 1.0')
            if key == 'fps' and (value != int(value) or value <= 0):
                raise ValueError('trajectory.fps must be a positive integer')
            if key == 'pause_seconds' and value < 0:
                raise ValueError('trajectory.pause_seconds must be nonnegative')
    return deepcopy(config)


def load_global_config():
    # Do not cache across runs: editing the file takes effect on the next run.
    return validate_config(json.loads(GLOBAL_CONFIG_PATH.read_text(encoding='utf-8')))


def trajectory_settings(scene=None, *, fps=None, pause_seconds=None):
    values = dict((scene or {}).get('trajectory', {}))
    if fps is not None:
        values['fps'] = fps
    if pause_seconds is not None:
        values['pause_seconds'] = pause_seconds
    if set(values) < _KEYS['trajectory']:
        values = {**load_global_config()['trajectory'], **values}
    validate_config({'trajectory': values}, partial=True)
    values['fps'] = int(values['fps'])
    return values


def motion_settings(constraints=None):
    values = {key: value for key, value in (constraints or {}).items() if key in _KEYS['motion']}
    if set(values) < _KEYS['motion']:
        values = {**load_global_config()['motion'], **values}
    validate_config({'motion': values}, partial=True)
    return values
