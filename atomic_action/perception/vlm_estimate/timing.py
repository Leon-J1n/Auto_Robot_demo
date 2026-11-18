"""Command-scoped client request timings, independent of perception results."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter


_current = ContextVar('vlm_command_timing', default=None)


@contextmanager
def command_timing_scope():
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def start_command(command_id):
    _current.set({'command_id': str(command_id), 'elapsed_s': 0., 'request_count': 0, 'failed_count': 0})


def request_elapsed():
    current = _current.get()
    return current['elapsed_s'] if current is not None else 0.


@contextmanager
def request_timer():
    current = _current.get()
    measurement = {}
    started = perf_counter()
    succeeded = False
    try:
        yield measurement
        succeeded = True
    finally:
        measurement['elapsed_s'] = perf_counter() - started
        if current is not None:
            current['elapsed_s'] += measurement['elapsed_s']
            current['request_count'] += 1
            current['failed_count'] += int(not succeeded)


def stamp_command(row):
    current = _current.get()
    if current is None or current['command_id'] != str(row.get('command_id')):
        return
    row['vlm_request_count'] = current['request_count']
    row['vlm_failed_request_count'] = current['failed_count']
    if current['request_count']:
        row['vlm_request_s'] = current['elapsed_s']
    else:
        row.pop('vlm_request_s', None)
