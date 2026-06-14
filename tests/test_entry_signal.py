import pandas as pd
import strategy as S

def test_entry_signal_autopopulates_tps():
    sig = S.EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                        stop_loss=90.0, take_profit=108.0, atr=2.0, rsi=55.0)
    assert round(sig.tp1, 4) == 102.6667
    assert round(sig.tp2, 4) == 105.3333
    assert sig.tp3 == 108.0
    assert sig.take_profit == sig.tp3

def test_real_checker_sets_tps():
    sig = S.EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=200.0,
                        stop_loss=180.0, take_profit=212.0, atr=3.0, rsi=60.0)
    assert sig.tp1 < sig.tp2 < sig.tp3
