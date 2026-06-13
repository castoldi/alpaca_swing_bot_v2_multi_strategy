"""Logger setup: rotating file logger + stdout mirror."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).parent
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger(name: str = __name__, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)

    # File handler (daily rotation, 14-day retention)
    fh = TimedRotatingFileHandler(
        LOGS_DIR / "alpaca_swing_bot_v2_multi_strategy.log",
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
    )
    fh.setFormatter(_FORMAT)
    logger.addHandler(fh)

    # Stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_FORMAT)
    logger.addHandler(sh)

    return logger