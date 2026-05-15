"""日志配置。对齐 Java 端 logback 输出到 ${LOG_DIR}/mysterious.log 的行为。"""

from __future__ import annotations

import sys
from logging.config import dictConfig
from pathlib import Path

from app.core.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "level": "INFO",
                    "formatter": "default",
                    "stream": sys.stdout,
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": "INFO",
                    "formatter": "default",
                    "filename": str(log_dir / "mysterious.log"),
                    "maxBytes": 50 * 1024 * 1024,
                    "backupCount": 10,
                    "encoding": "utf-8",
                },
            },
            "loggers": {
                "uvicorn": {"level": "INFO", "handlers": ["console", "file"], "propagate": False},
                "uvicorn.error": {"level": "INFO", "handlers": ["console", "file"], "propagate": False},
                "uvicorn.access": {"level": "INFO", "handlers": ["console", "file"], "propagate": False},
                "sqlalchemy.engine": {"level": "WARNING", "handlers": ["console"], "propagate": False},
                "app": {"level": "INFO", "handlers": ["console", "file"], "propagate": False},
            },
            "root": {"level": "INFO", "handlers": ["console", "file"]},
        }
    )
