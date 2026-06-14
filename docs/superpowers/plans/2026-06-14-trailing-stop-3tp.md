# Stepped Trailing Stop + 3 Take-Profit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single all-or-nothing exit with a 3-level scale-out (TP1/TP2/TP3 at ⅓/⅔/full of each strategy's ATR target, 33/33/34 split) and a stepped stop that ratchets to breakeven after TP1 and to TP1 after TP2.

**Architecture:** Pure exit logic lives in `strategy.py` (`split_take_profit`, `simulate_exit_scaleout`) and is unit-tested with pytest. Backtests emit one `Trade` row per leg. The live bot replaces its single OCO bracket with a market entry + 3 limit-sell orders + a managed stop order, deriving stop state from live Alpaca order/position data each loop (no new DB columns). Spec: `docs/superpowers/specs/2026-06-14-trailing-stop-3tp-design.md`.

**Tech Stack:** Python 3.11, pandas, alpaca-py, pytest (new dev dep).

---

### Task 1: Test scaffolding (pytest)

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `pytest.ini`

- [ ] **Step 1: Install pytest into the venv**

Run: `./.venv/Scripts/python.exe -m pip install pytest`
Expected: installs pytest, ends with `Successfully installed ... pytest-<ver>`.

- [ ] **Step 2: Add pytest to requirements.txt**

Append this line to `requirements.txt`:
```
pytest>=8.0
```

- [ ] **Step 3: Create the tests package + path bootstrap**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:
```python
"""Make the project root importable so tests can `import strategy`, `import config`, etc."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

Create `pytest.ini`:
```ini
[pytest]
testpaths = tests
addopts = -q
```

- [ ] **Step 4: Create a trivial test and verify the harness runs**

Create `tests/test_smoke.py`:
```python
import config

def test_imports():
    assert config.BAR_TIMEFRAME == "4h"
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/__init__.py tests/conftest.py pytest.ini tests/test_smoke.py
git commit -m "test: bootstrap pytest harness"
```

---

### Task 2: TP/qty split helpers + config

**Files:**
- Modify: `config.py` (after the `BAR_TIMEFRAME` block, ~line 40)
- Modify: `strategy.py` (add helpers near the top, after `is_tp_reachable_in_days`, ~line 40)
- Test: `tests/test_splits.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_splits.py`:
```python
import strategy as S

def test_split_take_profit_thirds():
    tp1, tp2, tp3 = S.split_take_profit(100.0, 108.0)
    assert round(tp1, 4) == 102.6667
    assert round(tp2, 4) == 105.3333
    assert tp3 == 108.0

def test_split_take_profit_zero_distance():
    # degenerate target at/under entry -> all equal entry/target
    assert S.split_take_profit(100.0, 100.0) == (100.0, 100.0, 100.0)

def test_split_qty_divisible():
    assert S.split_qty(9) == [3, 3, 3]

def test_split_qty_remainder_on_last():
    assert S.split_qty(10) == [3, 3, 4]
    assert S.split_qty(4) == [1, 1, 2]

def test_split_qty_below_three_is_empty():
    # caller handles <3 via fallback; helper returns [] to signal "can't scale"
    assert S.split_qty(2) == []
    assert S.split_qty(0) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_splits.py -v`
Expected: FAIL with `AttributeError: module 'strategy' has no attribute 'split_take_profit'`.

- [ ] **Step 3: Add `TP_SPLITS` to config.py**

After the `HISTORY_WARMUP_DAYS = 90` line in `config.py`, add:
```python
# Position fraction sold at TP1 / TP2 / TP3 (must sum to 1.0).
TP_SPLITS: tuple[float, float, float] = (0.33, 0.33, 0.34)
```

- [ ] **Step 4: Add the helpers to strategy.py**

In `strategy.py`, immediately after the `is_tp_reachable_in_days` function (before the earnings cache section), add:
```python
def split_take_profit(entry_price: float, take_profit: float) -> tuple[float, float, float]:
    """Place 3 TP levels at 1/3, 2/3, and full of the entry->target distance."""
    d = take_profit - entry_price
    return (entry_price + d / 3.0, entry_price + 2.0 * d / 3.0, take_profit)


def split_qty(qty: int) -> list[int]:
    """Whole-share split for 3 TP legs: floor thirds, remainder on the last leg.

    Returns [] when there are fewer than 3 shares (caller falls back to a single
    target in that case)."""
    q = int(qty)
    if q < 3:
        return []
    base = q // 3
    return [base, base, q - 2 * base]
```

- [ ] **Step 5: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_splits.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add config.py strategy.py tests/test_splits.py
git commit -m "feat: TP-level and whole-share split helpers"
```

---

### Task 3: EntrySignal gains tp1/tp2/tp3

**Files:**
- Modify: `strategy.py` (the `EntrySignal` dataclass, ~lines 170-178)
- Test: `tests/test_entry_signal.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_entry_signal.py`:
```python
import pandas as pd
import strategy as S

def test_entry_signal_autopopulates_tps():
    sig = S.EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                        stop_loss=90.0, take_profit=108.0, atr=2.0, rsi=55.0)
    assert round(sig.tp1, 4) == 102.6667
    assert round(sig.tp2, 4) == 105.3333
    assert sig.tp3 == 108.0
    # take_profit retained as the full target (== tp3)
    assert sig.take_profit == sig.tp3

def test_real_checker_sets_tps():
    # any checker that builds an EntrySignal should now expose tp1/tp2/tp3
    sig = S.EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=200.0,
                        stop_loss=180.0, take_profit=212.0, atr=3.0, rsi=60.0)
    assert sig.tp1 < sig.tp2 < sig.tp3
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_entry_signal.py -v`
Expected: FAIL with `TypeError`/`AttributeError` about `tp1`.

- [ ] **Step 3: Update the EntrySignal dataclass**

Replace the `EntrySignal` dataclass in `strategy.py` with:
```python
@dataclass
class EntrySignal:
    date: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float
    rsi: float
    strategy: str = "trend_pullback"
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0

    def __post_init__(self):
        # Derive the 3-level ladder from the entry and full target unless the
        # caller already supplied tp3. All 6 strategy checkers get this for free.
        if not self.tp3:
            self.tp1, self.tp2, self.tp3 = split_take_profit(self.entry_price, self.take_profit)
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_entry_signal.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_entry_signal.py
git commit -m "feat: EntrySignal exposes tp1/tp2/tp3 ladder"
```

---

### Task 4: Scale-out exit simulation

**Files:**
- Modify: `strategy.py` (add `ExitLeg` dataclass + `simulate_exit_scaleout` after `simulate_exit`, ~line 557)
- Test: `tests/test_simulate_exit_scaleout.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_simulate_exit_scaleout.py`:
```python
import pandas as pd
import strategy as S
from config import PARAMS

def _df(bars):
    # bars: list of (low, high, close); open == close for simplicity
    idx = pd.date_range("2026-01-02", periods=len(bars), freq="D")
    return pd.DataFrame({"open":[c for _,_,c in bars], "high":[h for _,h,_ in bars],
                         "low":[l for l,_,_ in bars], "close":[c for _,_,c in bars]}, index=idx)

def _sig(entry=100.0, sl=90.0, tp=108.0):
    return S.EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=entry,
                         stop_loss=sl, take_profit=tp, atr=2.0, rsi=55.0,
                         strategy="trend_pullback")

def test_all_three_tps_hit():
    # entry bar at 0; bar1 ranges through all TPs (tp1 102.67, tp2 105.33, tp3 108)
    df = _df([(100,100,100), (101,109,107)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert [l.reason for l in legs] == ["tp1","tp2","tp3"]
    assert round(sum(l.fraction for l in legs), 6) == 1.0

def test_tp1_then_stop_to_breakeven():
    # bar1 hits tp1 (high 103) then bar2 falls to entry (low 100) -> stop at breakeven
    df = _df([(100,100,100), (101,103,102), (99.5,102,100)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert legs[0].reason == "tp1"
    assert legs[1].reason == "stop_loss"
    assert legs[1].exit_price == 100.0          # stop moved to entry after TP1
    assert round(legs[1].fraction, 6) == 0.67

def test_tp1_tp2_then_stop_to_tp1():
    df = _df([(100,100,100), (101,106,105), (102,106,103)])  # bar2 dips to 102 < tp1(102.67)
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert [l.reason for l in legs] == ["tp1","tp2","stop_loss"]
    assert round(legs[2].exit_price, 4) == 102.6667         # stop at TP1
    assert round(legs[2].fraction, 6) == 0.34

def test_immediate_stop_full_size():
    df = _df([(100,100,100), (89,95,90)])   # bar1 low 89 <= sl 90
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert len(legs) == 1
    assert legs[0].reason == "stop_loss" and legs[0].fraction == 1.0
    assert legs[0].exit_price == 90.0

def test_time_stop_on_remainder():
    # never hits a TP; flat at breakeven+; max hold for trend_pullback = 5 bars
    bars = [(100,100,100)] + [(99,101,100) for _ in range(7)]
    legs = S.simulate_exit_scaleout(_df(bars), 0, _sig(tp=130.0), PARAMS)
    assert len(legs) == 1 and legs[0].reason == "time_stop" and legs[0].fraction == 1.0

def test_stop_checked_before_tp_same_bar():
    # bar1 ranges from below SL to above TP1; conservative = stop wins
    df = _df([(100,100,100), (89,103,95)])
    legs = S.simulate_exit_scaleout(df, 0, _sig(), PARAMS)
    assert legs[0].reason == "stop_loss" and legs[0].fraction == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_simulate_exit_scaleout.py -v`
Expected: FAIL with `AttributeError: ... 'simulate_exit_scaleout'`.

- [ ] **Step 3: Implement ExitLeg + simulate_exit_scaleout**

In `strategy.py`, after the existing `simulate_exit` function, add:
```python
@dataclass
class ExitLeg:
    exit_date: pd.Timestamp
    exit_price: float
    reason: str          # tp1 | tp2 | tp3 | stop_loss | time_stop | end_of_data
    bars_held: int
    fraction: float      # portion of the original position this leg closes


def simulate_exit_scaleout(
    df: pd.DataFrame, entry_idx: int, signal: EntrySignal, p: StrategyParams = PARAMS
) -> list[ExitLeg]:
    """Walk forward producing per-leg exits for the 3-TP / stepped-stop model.

    Per bar: check the stop FIRST (conservative) against the *current* floor, then
    TP1->TP2->TP3 in order. A TP fill raises the stepped floor effective the NEXT
    bar (breakeven after TP1, TP1 price after TP2). Time-stop / end-of-data close
    whatever remains.
    """
    from config import TP_SPLITS
    entry = signal.entry_price
    tps = [signal.tp1, signal.tp2, signal.tp3]
    fracs = list(TP_SPLITS)
    stop = signal.stop_loss
    max_days = _max_holding_days(signal, p)

    legs: list[ExitLeg] = []
    tp_hit = [False, False, False]
    remaining = 1.0

    for i in range(entry_idx + 1, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx

        # 1) stop first, against the floor as of the previous bar
        if float(bar["low"]) <= stop:
            legs.append(ExitLeg(bar.name, stop, "stop_loss", bars_held, remaining))
            return legs

        # 2) take-profits in ascending order (a wide bar can fill several)
        for k in range(3):
            if not tp_hit[k] and float(bar["high"]) >= tps[k]:
                tp_hit[k] = True
                legs.append(ExitLeg(bar.name, tps[k], f"tp{k+1}", bars_held, fracs[k]))
                remaining -= fracs[k]

        if tp_hit[2]:
            return legs  # fully scaled out

        # 3) raise the stepped floor (effective next bar)
        if tp_hit[1]:
            stop = max(stop, signal.tp1)
        elif tp_hit[0]:
            stop = max(stop, entry)

        # 4) time-stop on the remainder (only at breakeven+)
        if bars_held >= max_days and float(bar["close"]) >= entry and remaining > 1e-9:
            legs.append(ExitLeg(bar.name, float(bar["close"]), "time_stop", bars_held, remaining))
            return legs

    if remaining > 1e-9:
        last = df.iloc[len(df) - 1]
        legs.append(ExitLeg(last.name, float(last["close"]), "end_of_data",
                            len(df) - 1 - entry_idx, remaining))
    return legs
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_simulate_exit_scaleout.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_simulate_exit_scaleout.py
git commit -m "feat: scale-out exit simulation with stepped stop"
```

---

### Task 5: Backtest emits per-leg trades

**Files:**
- Modify: `strategy.py` (`backtest_ticker`, ~lines 563-620)
- Test: `tests/test_backtest_ticker_scaleout.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_backtest_ticker_scaleout.py`:
```python
import pandas as pd
import strategy as S
from config import PARAMS, StrategyType

def _make_df():
    # 70 flat warmup bars then a clean uptrend so trend_pullback fires and TPs hit
    import numpy as np
    n = 90
    base = [100.0]*60
    rise = list(np.linspace(100, 130, n-60))
    close = base + rise
    idx = pd.date_range("2025-11-01", periods=n, freq="D")
    df = pd.DataFrame({"open": close, "high":[c*1.02 for c in close],
                       "low":[c*0.99 for c in close], "close": close,
                       "volume":[1_000_000]*n}, index=idx)
    return df

def test_backtest_ticker_emits_multiple_legs():
    df = _make_df()
    trades = S.backtest_ticker(df, "TEST", pd.Timestamp("2025-11-01"),
                               PARAMS, StrategyType.TREND_PULLBACK)
    # at least one entry produced scale-out legs with tp* reasons
    reasons = {t.exit_reason for t in trades}
    assert reasons & {"tp1", "tp2", "tp3"}
    # fractions of one entry's legs sum to ~1 position (shares add up)
    assert all(t.shares > 0 for t in trades)
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_backtest_ticker_scaleout.py -v`
Expected: FAIL (current `backtest_ticker` only emits single-exit trades; no `tp1/tp2/tp3` reasons).

- [ ] **Step 3: Update backtest_ticker to emit per-leg trades**

In `strategy.py`, replace the body of the `for idx in range(len(df))` loop in `backtest_ticker` (the part from `sig = entry_checker(...)` through `in_trade_until = idx + bars`) with:
```python
        sig = entry_checker(df, idx, p)
        if sig is None:
            continue

        # Nearest target (TP1) must be reachable, else skip
        if not is_tp_reachable_in_days(sig.entry_price, sig.tp1, sig.atr, days=4):
            continue

        legs = simulate_exit_scaleout(df, idx, sig, p)
        if not legs:
            continue
        shares_total = p.dollars_per_trade / sig.entry_price
        for leg in legs:
            shares = shares_total * leg.fraction
            trades.append(Trade(
                ticker=ticker,
                entry_date=sig.date,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.tp3,
                exit_date=leg.exit_date if isinstance(leg.exit_date, pd.Timestamp) else pd.Timestamp(leg.exit_date),
                exit_price=leg.exit_price,
                exit_reason=leg.reason,
                bars_held=leg.bars_held,
                shares=shares,
                pnl_dollars=(leg.exit_price - sig.entry_price) * shares,
                pnl_pct=(leg.exit_price - sig.entry_price) / sig.entry_price,
                strategy=strategy.value if isinstance(strategy, StrategyType) else strategy,
            ))
        in_trade_until = idx + max(leg.bars_held for leg in legs)
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_backtest_ticker_scaleout.py -v`
Expected: PASS (1 passed). Also run the full suite: `./.venv/Scripts/python.exe -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_backtest_ticker_scaleout.py
git commit -m "feat: backtest emits one trade row per scale-out leg"
```

---

### Task 6: Stats count tp1/tp2/tp3 as take-profits

**Files:**
- Modify: `backtest_2025.py` (`compute_stats`, ~lines 75-100)
- Test: `tests/test_compute_stats.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_compute_stats.py`:
```python
import pandas as pd
import backtest_2025 as B
from strategy import Trade

def _t(reason, pnl):
    return Trade(ticker="X", entry_date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                 stop_loss=90.0, take_profit=108.0, exit_date=pd.Timestamp("2026-01-05"),
                 exit_price=104.0, exit_reason=reason, bars_held=3, shares=1.0,
                 pnl_dollars=pnl, pnl_pct=pnl/100.0, strategy="trend_pullback")

def test_tp_legs_counted_as_take_profit():
    trades = [_t("tp1", 2), _t("tp2", 3), _t("tp3", 4), _t("stop_loss", -5), _t("time_stop", 1)]
    s = B.compute_stats(trades)
    assert s["tp_count"] == 3
    assert s["sl_count"] == 1
    assert s["time_count"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_compute_stats.py -v`
Expected: FAIL (`tp_count` == 0 because only `exit_reason == "take_profit"` is matched).

- [ ] **Step 3: Update compute_stats**

In `backtest_2025.py`, near the top of `compute_stats`, replace the three filter lines:
```python
    tp_t = [t for t in trades if t.exit_reason == "take_profit"]
    sl_t = [t for t in trades if t.exit_reason == "stop_loss"]
    time_t = [t for t in trades if t.exit_reason == "time_stop"]
```
with:
```python
    TP_REASONS = {"take_profit", "tp1", "tp2", "tp3"}
    tp_t = [t for t in trades if t.exit_reason in TP_REASONS]
    sl_t = [t for t in trades if t.exit_reason == "stop_loss"]
    time_t = [t for t in trades if t.exit_reason == "time_stop"]
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_compute_stats.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add backtest_2025.py tests/test_compute_stats.py
git commit -m "feat: count tp1/tp2/tp3 legs as take-profit exits in stats"
```

---

### Task 7: Live stop-target + qty helpers

**Files:**
- Modify: `bot.py` (add pure helpers near the top, after `_make_client_order_id`, ~line 45)
- Test: `tests/test_bot_helpers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bot_helpers.py`:
```python
import bot

def test_stepped_stop_target():
    # 0 TP filled -> initial SL; 1 -> entry; 2 -> tp1; 3 -> None (closed)
    assert bot.stepped_stop_target(0, entry=100.0, initial_sl=90.0, tp1=102.0) == 90.0
    assert bot.stepped_stop_target(1, 100.0, 90.0, 102.0) == 100.0
    assert bot.stepped_stop_target(2, 100.0, 90.0, 102.0) == 102.0
    assert bot.stepped_stop_target(3, 100.0, 90.0, 102.0) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_bot_helpers.py -v`
Expected: FAIL `AttributeError: module 'bot' has no attribute 'stepped_stop_target'`.

- [ ] **Step 3: Implement the helper**

In `bot.py`, after `_make_client_order_id`, add:
```python
def stepped_stop_target(n_tp_filled: int, entry: float, initial_sl: float, tp1: float):
    """Where the stop should sit given how many TP legs have filled.

    0 -> initial SL, 1 -> entry (breakeven), 2 -> TP1, 3 -> None (position closed).
    """
    return [initial_sl, entry, tp1, None][min(int(n_tp_filled), 3)]
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_bot_helpers.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_bot_helpers.py
git commit -m "feat: stepped stop-target helper for live bot"
```

---

### Task 8: Live scaled-entry placement (fake Alpaca client)

**Files:**
- Modify: `bot.py` (add `_place_scaled_entry`, near the order section, ~after line 170)
- Create: `tests/fakes.py`
- Test: `tests/test_place_scaled_entry.py`

- [ ] **Step 1: Create the fake trading client**

Create `tests/fakes.py`:
```python
"""Minimal stand-in for alpaca TradingClient — records submitted orders, no network."""
import itertools

class FakeOrder:
    _ids = itertools.count(1)
    def __init__(self, req):
        self.id = f"ord-{next(self._ids)}"
        self.client_order_id = getattr(req, "client_order_id", None)
        self.symbol = getattr(req, "symbol", None)
        self.qty = getattr(req, "qty", None)
        self.side = getattr(req, "side", None)
        self.limit_price = getattr(req, "limit_price", None)
        self.stop_price = getattr(req, "stop_price", None)
        self.type = type(req).__name__
        self.status = "new"
        self.legs = []

class FakeTradingClient:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
    def submit_order(self, req):
        o = FakeOrder(req)
        self.submitted.append(o)
        return o
    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_place_scaled_entry.py`:
```python
import pandas as pd
import bot
from tests.fakes import FakeTradingClient
from strategy import EntrySignal

def _sig():
    return EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                       stop_loss=90.0, take_profit=108.0, atr=2.0, rsi=55.0,
                       strategy="ensemble")

def test_place_scaled_entry_orders():
    tc = FakeTradingClient()
    bot._place_scaled_entry(tc, "AMD", qty=9, sig=_sig(), strat_name="ensemble")
    types = [o.type for o in tc.submitted]
    # 1 market buy + 3 limit sells + 1 stop sell
    assert types.count("MarketOrderRequest") == 1
    assert types.count("LimitOrderRequest") == 3
    assert types.count("StopOrderRequest") == 1
    sells = [o for o in tc.submitted if o.type == "LimitOrderRequest"]
    assert sorted(int(o.qty) for o in sells) == [3, 3, 3]
    # limit prices are tp1/tp2/tp3
    assert sorted(round(o.limit_price, 2) for o in sells) == [102.67, 105.33, 108.0]
    stop = [o for o in tc.submitted if o.type == "StopOrderRequest"][0]
    assert int(stop.qty) == 9 and stop.stop_price == 90.0
    # every order is bot-owned
    assert all(o.client_order_id and o.client_order_id.startswith("swingv2") for o in tc.submitted)
```

- [ ] **Step 3: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_place_scaled_entry.py -v`
Expected: FAIL `AttributeError: ... '_place_scaled_entry'`.

- [ ] **Step 4: Implement _place_scaled_entry**

In `bot.py`, add (uses `strategy.split_qty`, imported as `from strategy import split_qty` at top — add to the existing strategy import line):
```python
def _place_scaled_entry(tc, ticker: str, qty: int, sig, strat_name: str) -> dict:
    """Market buy + 3 limit-sell TP legs + a full-qty protective stop. Bot-owned."""
    from alpaca.trading.requests import (MarketOrderRequest, LimitOrderRequest, StopOrderRequest)
    from alpaca.trading.enums import OrderSide, TimeInForce

    entry_coid = _make_client_order_id(strat_name, ticker, "entry")
    buy = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                             time_in_force=TimeInForce.DAY, client_order_id=entry_coid)
    entry_order = tc.submit_order(buy)

    legs = split_qty(qty)            # [a, a, qty-2a]
    tps = [sig.tp1, sig.tp2, sig.tp3]
    leg_orders = []
    for i, (lq, tp) in enumerate(zip(legs, tps), start=1):
        coid = _make_client_order_id(strat_name, ticker, f"tp{i}")
        req = LimitOrderRequest(symbol=ticker, qty=lq, side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC,
                                limit_price=round(tp, 2), client_order_id=coid)
        leg_orders.append(tc.submit_order(req))

    stop_coid = _make_client_order_id(strat_name, ticker, "stop")
    stop_req = StopOrderRequest(symbol=ticker, qty=qty, side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC,
                                stop_price=round(sig.stop_loss, 2), client_order_id=stop_coid)
    stop_order = tc.submit_order(stop_req)

    return {"entry": entry_order, "tp_legs": leg_orders, "stop": stop_order,
            "entry_coid": entry_coid, "alpaca_id": str(getattr(entry_order, "id", "") or "")}
```

Also update the strategy import at the top of `bot.py` to include `split_qty`:
```python
from strategy import add_indicators, get_entry_checker, simulate_exit, is_tp_reachable_in_days, split_qty
```

- [ ] **Step 5: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_place_scaled_entry.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add bot.py tests/fakes.py tests/test_place_scaled_entry.py
git commit -m "feat: live scaled entry (3 limit TP legs + protective stop)"
```

---

### Task 9: Live stepped-stop management (fake client)

**Files:**
- Modify: `bot.py` (add `_count_filled_tp_legs` + `_sync_stepped_stop`, near `_close_owned`, ~line 300)
- Test: `tests/test_sync_stepped_stop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_stepped_stop.py`:
```python
import bot
from tests.fakes import FakeTradingClient, FakeOrder

class _Req:
    def __init__(self, **kw): self.__dict__.update(kw)

def test_sync_moves_stop_to_breakeven_after_one_tp(monkeypatch):
    tc = FakeTradingClient()
    # current open stop at initial SL, qty 6 remaining (one TP of 3 already filled)
    stop = FakeOrder(_Req(symbol="AMD", qty=6, side="sell", stop_price=90.0,
                          client_order_id="swingv2-stop-ensemble-AMD-1"))
    stop.type = "StopOrderRequest"
    # 1 TP leg filled, 2 still open
    monkeypatch.setattr(bot, "_open_stop_order", lambda tc_, t: stop)
    monkeypatch.setattr(bot, "_count_filled_tp_legs", lambda tc_, t: 1)
    monkeypatch.setattr(bot, "_position_qty", lambda tc_, tk: 6.0)

    trade = {"ticker":"AMD","strategy":"ensemble","entry_price":100.0,
             "stop_loss":90.0,"take_profit":108.0}
    bot._sync_stepped_stop(tc, trade)

    # old stop cancelled, new stop placed at breakeven (100.0) for qty 6
    assert stop.client_order_id.split("-")[0] == "swingv2"
    assert tc.cancelled == [stop.id]
    new = [o for o in tc.submitted if o.type == "StopOrderRequest"][-1]
    assert new.stop_price == 100.0 and int(new.qty) == 6
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_sync_stepped_stop.py -v`
Expected: FAIL `AttributeError: ... '_sync_stepped_stop'`.

- [ ] **Step 3: Implement the helpers**

In `bot.py`, add:
```python
def _position_qty(tc, ticker: str) -> float:
    try:
        pos = tc.get_open_position(ticker)
        return abs(float(pos.qty)) if pos else 0.0
    except Exception:
        return 0.0


def _our_sell_orders(tc, ticker: str):
    """Open + recently-closed SELL orders for the symbol that we own."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    out = []
    for st in (QueryOrderStatus.OPEN, QueryOrderStatus.CLOSED):
        try:
            req = GetOrdersRequest(status=st, symbols=[ticker], side=OrderSide.SELL, limit=50)
            out.extend(tc.get_orders(filter=req) or [])
        except Exception:
            pass
    return [o for o in out if str(getattr(o, "client_order_id", "") or "").startswith(CLIENT_ORDER_PREFIX)]


def _count_filled_tp_legs(tc, trade: dict) -> int:
    n = 0
    for o in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(o, "client_order_id", "") or "")
        if "-tp" in coid and str(getattr(o, "status", "")).lower() in ("filled", "done_for_day", "closed"):
            n += 1
    return n


def _open_stop_order(tc, trade: dict):
    for o in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(o, "client_order_id", "") or "")
        if "-stop-" in coid and str(getattr(o, "status", "")).lower() in ("new", "accepted", "held", "pending_new"):
            return o
    return None


def _sync_stepped_stop(tc, trade: dict):
    """Move the resting stop to match how many TP legs have filled (breakeven/TP1)."""
    entry = float(trade["entry_price"])
    from strategy import split_take_profit
    tp1, _, _ = split_take_profit(entry, float(trade["take_profit"]))

    n = _count_filled_tp_legs(tc, trade)
    target = stepped_stop_target(n, entry, float(trade["stop_loss"]), tp1)
    if target is None:
        return  # all TPs filled; reconciliation closes the trade elsewhere

    stop = _open_stop_order(tc, trade)
    if stop is None:
        return
    if abs(float(getattr(stop, "stop_price", 0.0)) - target) < 1e-6:
        return  # already at the right level

    qty = _position_qty(tc, trade["ticker"]) or float(getattr(stop, "qty", 0) or 0)
    if qty < 1:
        return
    try:
        tc.cancel_order_by_id(stop.id)
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        coid = _make_client_order_id(trade["strategy"], trade["ticker"], "stop")
        tc.submit_order(StopOrderRequest(symbol=trade["ticker"], qty=int(qty),
                                         side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                         stop_price=round(target, 2), client_order_id=coid))
        log.info("  %s stepped stop -> $%.2f (%d TP legs filled)", trade["ticker"], target, n)
    except Exception as e:
        log.error("  Failed to move stepped stop for %s: %s", trade["ticker"], e)
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_sync_stepped_stop.py -v`
Expected: PASS (1 passed). Run full suite `-q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_sync_stepped_stop.py
git commit -m "feat: live stepped-stop management from Alpaca order state"
```

---

### Task 10: Wire scale-out into the live run loop

**Files:**
- Modify: `bot.py` — entry block in `run_once` (~lines 116-170) and `_reconcile_and_exit` (~lines 258-300)

- [ ] **Step 1: Use TP1 for the reachability check + branch on qty**

In `run_once`, change the reachability call to use TP1:
```python
                if not is_tp_reachable_in_days(sig.entry_price, sig.tp1, sig.atr, days=4):
```
Then replace the order-placement `try:` block so that `qty >= 3` calls `_place_scaled_entry` and persists the trade, while `qty < 3` keeps the existing single-bracket path (rename the current bracket code into an `else`/`_place_single_bracket` for `qty == 1..2`, still skipping `qty < 1`). Concretely, inside the existing `try:` after computing `qty`:
```python
                    if qty < 1:
                        log.info("  $%.0f/trade buys <1 whole share of %s @ $%.2f — skipping",
                                 PARAMS.dollars_per_trade, ticker, sig.entry_price)
                        continue
                    coid = _make_client_order_id(strat_name, ticker, "entry")
                    if qty >= 3:
                        info = _place_scaled_entry(tc, ticker, qty, sig, strat_name)
                        coid, alpaca_id = info["entry_coid"], info["alpaca_id"]
                        log.info("  Scaled entry %s x%d (3 TP legs + stop) [coid=%s]", ticker, qty, coid)
                    else:
                        from alpaca.trading.requests import (MarketOrderRequest, TakeProfitRequest, StopLossRequest)
                        from alpaca.trading.enums import OrderSide, TimeInForce
                        tp = TakeProfitRequest(limit_price=round(sig.tp3, 2))
                        sl = StopLossRequest(stop_price=round(sig.stop_loss, 2))
                        mkt = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                                                 time_in_force=TimeInForce.DAY, take_profit=tp,
                                                 stop_loss=sl, client_order_id=coid)
                        order = tc.submit_order(mkt)
                        alpaca_id = str(getattr(order, "id", "") or "")
                        log.info("  Single bracket %s x%d (qty<3) [coid=%s]", ticker, qty, coid)

                    orders_placed += 1
                    db_mod.save_trade(ticker, strat_name, str(sig.date), sig.entry_price,
                                      sig.stop_loss, sig.tp3, shares=qty,
                                      client_order_id=coid, alpaca_order_id=alpaca_id)
                    send_notification(
                        f"Bot V2: {ticker} entry ({strat_name})",
                        f"Entry ${sig.entry_price:.2f}\nSL ${sig.stop_loss:.2f}\n"
                        f"TP1 ${sig.tp1:.2f} / TP2 ${sig.tp2:.2f} / TP3 ${sig.tp3:.2f}\n"
                        f"Qty {qty}\nRef {coid}")
```
(Delete the old single-bracket block that this replaces so there is exactly one entry path.)

- [ ] **Step 2: Call the stepped-stop sync each loop for owned open positions**

In `_reconcile_and_exit`, inside the `for trade in ...` loop, after confirming the position still exists (`pos is not None`) and before/with the time-stop check, add:
```python
            if _verify_owned(tc, trade):
                _sync_stepped_stop(tc, trade)
```

- [ ] **Step 3: Verify nothing regressed (unit + import)**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass.
Run: `./.venv/Scripts/python.exe -c "import bot; print('bot import OK')"`
Expected: `bot import OK`.

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: wire 3-TP scale-out + stepped stop into live run loop"
```

---

### Task 11: Dashboard exit-reason labels

**Files:**
- Modify: `dashboard/index.html` (recent-trades reason rendering)

- [ ] **Step 1: Add friendly labels for the new exit reasons**

In `dashboard/index.html`, just before the Recent-trades table is built (in `loadHome`), add a small map and use it for the reason cell:
```javascript
  const REASON_LABEL = { tp1:'TP1', tp2:'TP2', tp3:'TP3', take_profit:'TP',
                         stop_loss:'Stop', time_stop:'Time', end_of_data:'Open' };
```
Then change the reason cell in the closed-trades `.map(...)` from `t.exit_reason||'—'` to:
```javascript
          REASON_LABEL[t.exit_reason] || t.exit_reason || '—',
```

- [ ] **Step 2: Restart dashboard and verify it renders**

Run: `pwsh -NoProfile -File scripts/manage.ps1 restart-dashboard`
Then: `pwsh -NoProfile -Command "(Invoke-WebRequest http://localhost:8004/ -UseBasicParsing).StatusCode"`
Expected: `200`.

- [ ] **Step 3: Commit**

```bash
git add dashboard/index.html
git commit -m "feat: label tp1/tp2/tp3 exit reasons in dashboard"
```

---

### Task 12: Rerun 4h backtests, release

**Files:**
- Modify: `VERSION`, `CHANGELOG.md`

- [ ] **Step 1: Rerun all three 4h backtests**

Run:
```bash
./.venv/Scripts/python.exe backtest_2025.py
./.venv/Scripts/python.exe backtest_2024.py
./.venv/Scripts/python.exe backtest_2026.py
```
Expected: each ends `... backtest complete. Best strategy: ...`. No tracebacks.

- [ ] **Step 2: Verify per-leg rows recorded and timeframe stays 4h**

Run:
```bash
PYTHONPATH=. ./.venv/Scripts/python.exe -c "from dashboard import db; from collections import Counter; \
h=db.get_backtest_history(1000); print('latest tf:', Counter(r['timeframe'] for r in db.get_backtest_results()));"
```
Expected: `latest tf: Counter({'4h': 18})`.

- [ ] **Step 3: Bump version + changelog**

Run: `pwsh -NoProfile -File scripts/version.ps1 -Bump minor`  (0.4.0 → 0.5.0)
Then fill the new `## [0.5.0]` section in `CHANGELOG.md` under **Added**:
```
- 3-level take-profit scale-out (TP1/TP2/TP3 at 1/3, 2/3, full of each strategy's
  ATR target; 33/33/34 split) with a stepped stop that ratchets to breakeven after
  TP1 and to TP1 after TP2. Backtests emit one trade row per leg
  (`tp1`/`tp2`/`tp3`/`stop_loss`/`time_stop`). Live bot places a market entry + 3
  limit TP legs + a managed protective stop (≥3 shares; falls back to a single
  bracket below that). All 4h backtests rerun. New pytest suite under `tests/`.
```

- [ ] **Step 4: Restart bot on new code and commit the release**

Run: `pwsh -NoProfile -File scripts/manage.ps1 restart-bot -Strategy ensemble -Interval 30`
Then:
```bash
git add VERSION CHANGELOG.md
git commit -m "release: 3-TP scale-out + stepped stop (v0.5.0); rerun 4h backtests"
```
Expected: post-commit hook tags `v0.5.0+build<N>` (push depends on GCM being seeded).

- [ ] **Step 5: Final verification**

Run: `pwsh -NoProfile -File scripts/manage.ps1 status`
Expected: BOT HEALTHY, DASHBOARD HEALTHY.

---

## Notes for the implementer

- **No new DB columns.** Live stepped-stop state is derived each loop from Alpaca order/position data; the ladder is recomputed from the stored `entry_price` + `take_profit` (=TP3) via `split_take_profit`.
- **`simulate_exit` (single-exit) is kept** for `dashboard/strategy_examples.py`; backtests use `simulate_exit_scaleout`. Do not delete the old one.
- **Whole-share reality:** at `dollars_per_trade=$200` live orders are `qty<1` and skipped, so live scale-out only engages once sizing is raised. Backtests (fractional shares) always exercise it.
- Run the full suite `./.venv/Scripts/python.exe -m pytest -q` after each task.
