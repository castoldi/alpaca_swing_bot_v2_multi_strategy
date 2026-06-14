import pandas as pd
import strategy as S
from config import PARAMS

def _df(bars):
    idx = pd.date_range("2026-01-02", periods=len(bars), freq="D")
    return pd.DataFrame({"open":[c for _,_,c in bars], "high":[h for _,h,_ in bars],
                         "low":[l for l,_,_ in bars], "close":[c for _,_,c in bars]}, index=idx)

def _sig(entry=100.0, sl=90.0, tp=108.0):
    return S.EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=entry,
                         stop_loss=sl, take_profit=tp, atr=2.0, rsi=55.0,
                         strategy="trend_pullback")

def test_all_three_tps_hit():
    df = _df([(100,100,100), (101,109,107)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert [l.reason for l in legs] == ["tp1","tp2","tp3"]
    assert round(sum(l.fraction for l in legs), 6) == 1.0

def test_tp1_then_stop_to_breakeven():
    df = _df([(100,100,100), (101,103,102), (99.5,102,100)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert legs[0].reason == "tp1"
    assert legs[1].reason == "stop_loss"
    assert legs[1].exit_price == 100.0
    assert round(legs[1].fraction, 6) == 0.67

def test_tp1_tp2_then_stop_to_tp1():
    df = _df([(100,100,100), (101,106,105), (102,106,103)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert [l.reason for l in legs] == ["tp1","tp2","stop_loss"]
    assert round(legs[2].exit_price, 4) == 102.6667
    assert round(legs[2].fraction, 6) == 0.34

def test_immediate_stop_full_size():
    df = _df([(100,100,100), (89,95,90)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert len(legs) == 1
    assert legs[0].reason == "stop_loss" and legs[0].fraction == 1.0
    assert legs[0].exit_price == 90.0

def test_time_stop_on_remainder():
    bars = [(100,100,100)] + [(99,101,100) for _ in range(7)]
    legs = S.simulate_exit_scaleout(_df(bars), 0, _sig(tp=130.0), PARAMS)
    assert len(legs) == 1 and legs[0].reason == "time_stop" and legs[0].fraction == 1.0

def test_stop_checked_before_tp_same_bar():
    df = _df([(100,100,100), (89,103,95)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert legs[0].reason == "stop_loss" and legs[0].fraction == 1.0
