"""Canonical Loguru logging configuration with stdlib interception and JSONL sink."""

import logging
import sys
from pathlib import Path
from typing import Final

from loguru import logger

_CONSOLE_FORMAT: Final[str] = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level> <dim>{extra}</dim>"
)
_LOG_ROTATION_SIZE: Final[str] = "50 MB"
_LOG_RETENTION_COUNT: Final[int] = 10

_STDLIB_TO_LOGURU_LEVEL: Final[tuple[tuple[int, str], ...]] = (
    (logging.DEBUG, "DEBUG"),
    (logging.INFO, "INFO"),
    (logging.WARNING, "WARNING"),
    (logging.ERROR, "ERROR"),
    (logging.CRITICAL, "CRITICAL"),
)


def _resolve_loguru_level(levelno: int) -> str | int:
    for std_level, name in _STDLIB_TO_LOGURU_LEVEL:
        if std_level == levelno:
            return name
    return levelno


class _StdlibInterceptHandler(logging.Handler):
    """Routes standard library logging records (Telethon, SQLAlchemy, APScheduler) into Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        level_name = _resolve_loguru_level(record.levelno)

        frame = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level_name,
            record.getMessage(),
        )


def configure_loguru(log_file_path: Path) -> None:
    """Initializes Loguru console and rotating JSONL sinks and intercepts stdlib loggers."""
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    logger.remove()

    logger.add(
        sys.stdout,
        level="INFO",
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    logger.add(
        str(log_file_path),
        level="DEBUG",
        serialize=True,
        rotation=_LOG_ROTATION_SIZE,
        retention=_LOG_RETENTION_COUNT,
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
    )

    root_logger = logging.getLogger()
    root_logger.handlers = [_StdlibInterceptHandler()]
    root_logger.setLevel(logging.INFO)

    for noisy_logger in (
        "telethon.client.updates",
        "telethon.client.uploads",
        "telethon.client.downloads",
        "telethon.network",
        "apscheduler.scheduler",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
