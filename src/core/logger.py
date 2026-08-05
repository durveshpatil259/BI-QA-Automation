"""Centralised logging configuration.

Provides two things:

* :func:`get_logger` — a module-level logger writing to the console and to a
  rolling application log file under ``config/app.log``.
* :func:`get_project_logger` — a logger bound to a specific project that also
  writes into that project's ``Logs/`` folder, so every analysis run leaves an
  audit trail inside the project on disk (a hard product requirement).

Handlers are attached exactly once per logger name to avoid duplicate log
lines when Streamlit re-runs the script on every interaction.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Application-level log lives next to global config.
_APP_LOG_DIR = Path(__file__).resolve().parents[2] / "config"
_APP_LOG_FILE = _APP_LOG_DIR / "app.log"

_DEFAULT_LEVEL = logging.INFO


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)


def _has_handler(logger: logging.Logger, kind: type, target: str | None = None) -> bool:
    """Return True if *logger* already owns a handler of *kind* (optionally
    pointing at file *target*), so we never attach duplicates."""
    for handler in logger.handlers:
        if isinstance(handler, kind):
            if target is None:
                return True
            base = getattr(handler, "baseFilename", None)
            if base and Path(base) == Path(target):
                return True
    return False


def get_logger(name: str = "bi_testpilot", level: int = _DEFAULT_LEVEL) -> logging.Logger:
    """Return the shared application logger (console + rolling app.log)."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not _has_handler(logger, logging.StreamHandler):
        console = logging.StreamHandler()
        console.setFormatter(_build_formatter())
        logger.addHandler(console)

    try:
        _APP_LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not _has_handler(logger, RotatingFileHandler, str(_APP_LOG_FILE)):
            file_handler = RotatingFileHandler(
                _APP_LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(_build_formatter())
            logger.addHandler(file_handler)
    except OSError:
        # Never let logging setup crash the app; console logging still works.
        logger.warning("Could not attach application file log handler.")

    return logger


def get_project_logger(project_logs_dir: Path, level: int = _DEFAULT_LEVEL) -> logging.Logger:
    """Return a logger that writes into a specific project's ``Logs/`` folder.

    Parameters
    ----------
    project_logs_dir:
        Absolute path to the project's ``Logs`` sub-folder.
    """
    project_logs_dir = Path(project_logs_dir)
    # Unique logger name per project folder keeps handlers isolated.
    logger_name = f"bi_testpilot.project.{project_logs_dir.parent.name}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = True  # also surface in the app-level console/file log

    log_file = project_logs_dir / "analysis.log"
    try:
        project_logs_dir.mkdir(parents=True, exist_ok=True)
        if not _has_handler(logger, RotatingFileHandler, str(log_file)):
            file_handler = RotatingFileHandler(
                log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(_build_formatter())
            logger.addHandler(file_handler)
    except OSError:
        get_logger().warning("Could not attach project file log handler at %s", log_file)

    return logger
