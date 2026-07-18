"""Logger setup: rotating file logger + stdout mirror."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).parent
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_FILE_HANDLERS: dict[str, TimedRotatingFileHandler] = {}
_STDOUT_HANDLER: logging.StreamHandler | None = None


def _log_filename(argv: Sequence[str] | None = None) -> str:
    """Return one stable log file per independently running project process."""
    args = [str(value) for value in (argv if argv is not None else sys.argv)]
    joined = " ".join(args).lower()
    script = Path(args[0]).stem.lower() if args else "application"

    if "dashboard.server:app" in joined:
        return "dashboard.log"
    if "pytest" in joined:
        return "pytest.log"
    if script == "bot":
        return "alpaca_swing_bot_v2_multi_strategy.log"
    if script.startswith("backtest"):
        safe = re.sub(r"[^a-z0-9_.-]+", "_", script)
        return f"{safe}.log"
    if script not in {"", "-", "__main__", "python", "pythonw"}:
        safe = re.sub(r"[^a-z0-9_.-]+", "_", script)
        return f"{safe}.log"
    return "application.log"


def get_logger(name: str = __name__, level: int = logging.INFO) -> logging.Logger:
    global _STDOUT_HANDLER

    logger = logging.getLogger(name)
    if getattr(logger, "_swing_bot_configured", False):
        return logger
    logger.setLevel(level)
    logger.propagate = False

    # One shared handler per process/file avoids multiple Windows file handles
    # fighting each other during rotation. Separate processes use separate files.
    filename = _log_filename()
    fh = _FILE_HANDLERS.get(filename)
    if fh is None:
        fh = TimedRotatingFileHandler(
            LOGS_DIR / filename,
            when="midnight",
            interval=1,
            backupCount=14,
            encoding="utf-8",
        )
        fh.setFormatter(_FORMAT)
        _FILE_HANDLERS[filename] = fh
    logger.addHandler(fh)

    if _STDOUT_HANDLER is None:
        _STDOUT_HANDLER = logging.StreamHandler(sys.stdout)
        _STDOUT_HANDLER.setFormatter(_FORMAT)
    logger.addHandler(_STDOUT_HANDLER)
    logger._swing_bot_configured = True

    return logger
