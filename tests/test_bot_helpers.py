import bot


def test_stepped_stop_target():
    # 0 TP filled -> initial SL; 1 -> entry; 2 -> tp1; 3 -> None (closed)
    assert bot.stepped_stop_target(0, entry=100.0, initial_sl=90.0, tp1=102.0) == 90.0
    assert bot.stepped_stop_target(1, 100.0, 90.0, 102.0) == 100.0
    assert bot.stepped_stop_target(2, 100.0, 90.0, 102.0) == 102.0
    assert bot.stepped_stop_target(3, 100.0, 90.0, 102.0) is None
