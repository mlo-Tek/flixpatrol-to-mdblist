#!/usr/bin/env python3
"""Entry point for the mlo-Tek fork."""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import flixpatrol_to_mdblist as sync
from mlo_list_layout import install as install_list_layout
from mlo_patches import install as install_patches
from mlo_static_fix import install as install_static_fix
from mlo_verify_fix import install as install_verify_fix


def enable_persistent_file_logging() -> Path:
    """Mirror console logs into /app/config/logs with daily rotation."""
    # /app/config is the persistent Unraid appdata mapping, so logs survive updates.
    config_dir = Path(os.environ.get("CONFIG_DIR", "/app/config"))
    log_dir = Path(os.environ.get("LOG_DIR", str(config_dir / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / os.environ.get(
        "LOG_FILE_NAME", "flixpatrol-to-mdblist.log"
    )
    retention_days = int(os.environ.get("LOG_RETENTION_DAYS", "14"))

    handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=max(retention_days, 0),
        encoding="utf-8",
        delay=False,
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    return log_file


if __name__ == "__main__":
    log_file = enable_persistent_file_logging()
    install_patches(sync)
    install_list_layout(sync)
    install_static_fix(sync)
    install_verify_fix(sync)
    sync.logger.info("Persistent log file: %s", log_file)
    sync.main()
