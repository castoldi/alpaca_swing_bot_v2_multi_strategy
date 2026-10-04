# Ensemble 20% stop on the production engine, 2022–2026

**Date:** 2026-10-03 · **Script:** `research/stop_width_production.py` ·
**Raw output:** `research/results/stop_width_production.txt` ·
**Follows:** [disaster-stop-2004-2026.md](disaster-stop-2004-2026.md)

## Question

On the legacy 4h engine, a wide stop beat the current 9% ensemble stop on return in
every test (2016–26: +1,634% vs +1,304% for a 20% stop). That engine differs from the
real one by up to ~40 points a year. Does the gain survive on the **production
engine**, the one the yearly runners use?

## Method

- One candidate, chosen in advance: `ensemble_stop_loss_pct = 0.20` (live: 0.09).
- Production engine via the capital-size harness (`research/capital_size_experiment.py`):
  minute-level execution, earnings filter, daily-loss kill switch, leveraged/group
  caps, 20% whole-share sizing, 5 slots, $5,000, **annual reset** (each year starts at
  $5,000; "5y comp" chains the five yearly returns).
- Candidates re-collected with the 20% stop, because the stop changes each entry's
  exit path. The 9% baseline reuses the harness's cached candidates and **reproduces
  the published +249.6% exactly**.
- Drawdown is realized-equity drawdown (the harness convention), not mark-to-market.

## Results

| Ensemble @ $5k | 2022 | 2023 | 2024 | 2025 | 2026 YTD | 5y comp | End $ | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **9% stop (live)** | −35.0% | +90.5% | +59.9% | +31.6% | +34.2% | **+249.6%** | $17,480 | 40.6% |
| 20% stop | −33.4% | +106.6% | +50.4% | +21.5% | +40.6% | **+253.6%** | $17,681 | 39.3% |

| Ensemble | Trades | Win % | Avg win | Avg loss | Worst trade | Stop-outs |
|---|---:|---:|---:|---:|---:|---:|
| 9% stop | 831 | 74% | +4.2% | −8.6% | −27.4% | 192 |
| 20% stop | 617 | 87% | +3.9% | −15.4% | −31.2% | 57 |

## Findings

1. **On the production engine the 20% stop is a wash.** +253.6% vs +249.6% over five
   years: $201 more on $5,000, well inside noise. It won 3 years (2022, 2023, 2026) and
   lost 2 (2024 by 9.5 points, 2025 by 10.1 points).
2. **The big legacy-engine gain did not survive.** Most of it came from things the
   production setup does not have: one continuous book with no January reset (stuck
   positions get a whole extra year to recover), and 4h-bar fills.
3. **2022 did not get worse here** (−33.4% vs −35.0%), unlike in the legacy test.
   Drawdown is about the same (39.3% vs 40.6%).
4. **The trade shape changes for the worse.** Stop-outs fall from 192 to 57 and the
   win rate rises to 87%, but the average loss nearly doubles (−8.6% → −15.4%) and the
   worst trade is −31.2%. Each losing trade costs about 3% of the account instead of
   about 1.7%.

## Verdict

**Keep the 9% stop. Do not change `ensemble_stop_loss_pct`.** The wider stop adds no
measurable return on the real engine and makes each loss bigger. This closes the
stop-width line opened by the no-stop question:

- no stop: rejected ([no-stop-2016-2026.md](no-stop-2016-2026.md),
  [no-stop-bear-stress-test-2004-2015.md](no-stop-bear-stress-test-2004-2015.md))
- −20/−25/−30% disaster stops: looked better only on the legacy engine
  ([disaster-stop-2004-2026.md](disaster-stop-2004-2026.md))
- 20% stop on the production engine: no improvement (this document)

The same pattern was found earlier with stops ×0.75–2.0
([bear-market-defence.md](bear-market-defence.md) §4): wider stops trade bear-year
losses for bull-year gains, and the net is small. 55 trials spent on exit rules.
