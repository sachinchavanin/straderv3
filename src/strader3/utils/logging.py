"""Logging configuration with structlog."""

import logging
import sys
from pathlib import Path

import structlog


def setup_logging(
    level: str = "INFO",
    log_dir: str = "data/logs",
    json_format: bool = True,
    console: bool = True,
) -> None:
    """Configure structured logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files
        json_format: Use JSON format (True) or console format (False)
        console: Also log to stdout
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = []

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        handlers.append(console_handler)

    file_handler = logging.FileHandler(log_path / "system.log")
    file_handler.setLevel(log_level)
    handlers.append(file_handler)

    error_handler = logging.FileHandler(log_path / "errors.log")
    error_handler.setLevel(logging.ERROR)
    handlers.append(error_handler)

    logging.basicConfig(
        level=log_level,
        handlers=handlers,
        format="%(message)s",
    )

    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_trade_logger():
    """Get a logger specifically for trade events."""
    return structlog.get_logger("trades")
