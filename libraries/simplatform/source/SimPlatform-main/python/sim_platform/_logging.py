from __future__ import annotations

import sys
import logging
from threading import Lock

from bq.log import log as _bq_log
from bq.defs.log_level import log_level as _bq_level


_LOG = None
_SERVICE = "python_sdk"
_LOCK = Lock()
_LEVELS = (_bq_level.verbose, _bq_level.debug, _bq_level.info,
           _bq_level.warning, _bq_level.error, _bq_level.fatal)
_PYTHON_LEVELS = (5, logging.DEBUG, logging.INFO, logging.WARNING,
                  logging.ERROR, logging.CRITICAL)


def _create_log(service: str):
    service = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in service)
    config = "\n".join((
        "appenders_config.binary.type=compressed_file",
        "appenders_config.binary.levels=[all]",
        f"appenders_config.binary.file_name=logs/{service}",
        "appenders_config.binary.always_create_new_file=true",
        "log.buffer_size=65535",
        "log.recovery=true",
        "log.thread_mode=async",
    ))
    logger = _bq_log.create_log(f"robot_sim_platform_{service}", config)
    if not logger.is_valid():
        raise RuntimeError(f"BqLog failed to initialize logger for {service}")
    return logger


def configure_service_logging(service: str) -> None:
    """Route this Python process to its own BqLog stream."""
    global _LOG, _SERVICE
    with _LOCK:
        if _LOG is not None and _SERVICE == service:
            return
        _LOG = _create_log(service)
        _SERVICE = service


def log(level: int, category: str, message: str) -> None:
    if not 0 <= level < len(_LEVELS):
        raise ValueError("BqLog severity must be in the range 0..5")
    with _LOCK:
        logger = _LOG if _LOG is not None else _create_log(_SERVICE)
        globals()["_LOG"] = logger
        (_LOG.verbose, _LOG.debug, _LOG.info, _LOG.warning, _LOG.error, _LOG.fatal)[level](
            "{}: {}", category, message)


def verbose(category: str, message: str) -> None:
    log(0, category, message)


def debug(category: str, message: str) -> None:
    log(1, category, message)


def info(category: str, message: str) -> None:
    log(2, category, message)


def warning(category: str, message: str) -> None:
    log(3, category, message)


def error(category: str, message: str) -> None:
    log(4, category, message)


def fatal(category: str, message: str) -> None:
    log(5, category, message)


def flush() -> None:
    _bq_log.force_flush_all_logs()


class BqLogHandler(logging.Handler):
    """Route standard Python logger records into the native BqLog binary stream."""

    def emit(self, record: logging.LogRecord) -> None:
        level = 0 if record.levelno <= 5 else (
            1 if record.levelno <= logging.DEBUG else
            2 if record.levelno <= logging.INFO else
            3 if record.levelno <= logging.WARNING else
            4 if record.levelno <= logging.ERROR else 5)
        message = self.format(record)
        log(level, record.name, message)


def install_standard_logging(service: str = "python_sdk") -> BqLogHandler:
    configure_service_logging(service)
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, BqLogHandler):
            return handler
    handler = BqLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return handler


def log_verbose(behavior: str) -> None:
    frame = sys._getframe(1)
    event = f"{frame.f_code.co_filename}:{frame.f_code.co_name}:{frame.f_lineno} {behavior}"
    log(0, "VERBOSE", event)
