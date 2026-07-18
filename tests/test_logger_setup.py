import logger_setup
from logging.handlers import TimedRotatingFileHandler


def test_bot_keeps_the_canonical_log_filename():
    assert logger_setup._log_filename(["bot.py", "--loop"]) == (
        "alpaca_swing_bot_v2_multi_strategy.log"
    )


def test_dashboard_uses_a_separate_log_file():
    assert logger_setup._log_filename(
        ["pythonw.exe", "-m", "uvicorn", "dashboard.server:app"]
    ) == "dashboard.log"


def test_history_backtest_uses_a_separate_log_file():
    assert logger_setup._log_filename(["backtest_history.py"]) == (
        "backtest_history.log"
    )


def test_module_loggers_share_one_rotating_handler_per_process():
    first = logger_setup.get_logger("tests.shared_logger.first")
    second = logger_setup.get_logger("tests.shared_logger.second")

    first_file = next(
        handler
        for handler in first.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )
    second_file = next(
        handler
        for handler in second.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )

    assert first_file is second_file
