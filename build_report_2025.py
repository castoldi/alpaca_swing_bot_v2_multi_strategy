"""HTML report builder for 2025 backtest — shared with 2026."""
from __future__ import annotations

import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Import from backtest module
_here = Path(__file__).parent
sys.path.insert(0, str(_here))
from backtest_2025 import (
    TICKERS, PARAMS, BACKTEST_START,
    PROFIT_COLOR, LOSS_COLOR, PRICE_COLOR, NEUTRAL_COLOR,
    BG_DARK, BG_CARD, BORDER_COLOR, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
    STRATEGY_COLORS, compute_stats, per_ticker_stats, compute_max_drawdown,
    apply_portfolio_cap, download_history,
)
from config import StrategyType
from strategy import add_indicators, Trade


def dark_template():
    return go.layout.Template(
        layout=dict(
            paper_bgcolor=BG_CARD, plot_bgcolor=BG_CARD,
            font=dict(family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif", color=TEXT_PRIMARY),
            xaxis=dict(gridcolor="#252830", zerolinecolor="#252830"),
            yaxis=dict(gridcolor="#252830", zerolinecolor="#252830"),
        )
    )


def strategy_comparison_equity(all_results: dict[str, list[Trade]]) -> str:
    fig = go.Figure()
    has_data = False
    for sname, trades in all_results.items():
        if not trades:
            continue
        has_data = True
        sorted_t = sorted(trades, key=lambda t: t.exit_date)
        cum = np.cumsum([t.pnl_dollars for t in sorted_t])
        dates = [t.exit_date for t in sorted_t]
        color = STRATEGY_COLORS.get(sname, PRICE_COLOR)
        fig.add_trace(go.Scatter(x=dates, y=cum, mode="lines",
                                 line=dict(color=color, width=2),
                                 name=sname.replace("_", " ").title()))
    if not has_data:
        fig.add_annotation(text="No trades for any strategy", showarrow=False)
    fig.add_hline(y=0, line=dict(color=TEXT_MUTED, dash="dash"))
    fig.update_layout(title="Strategy Comparison — Cumulative P&L ($)", height=400,
                      template=dark_template(), margin=dict(l=40, r=20, t=50, b=40),
                      hovermode="x unified")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def equity_curve_figure(all_trades: list[Trade]) -> str:
    return strategy_comparison_equity({"strategy": all_trades})


def monthly_pnl_figure(all_trades: list[Trade]) -> str:
    if not all_trades:
        return "<p class='muted'>No trades.</p>"
    df_t = pd.DataFrame([{"month": pd.Timestamp(t.exit_date).to_period("M").to_timestamp(),
                           "pnl": t.pnl_dollars, "count": 1} for t in all_trades])
    monthly = df_t.groupby("month").agg(pnl=("pnl", "sum"), count=("count", "sum")).sort_index()
    colors = [PROFIT_COLOR if v >= 0 else LOSS_COLOR for v in monthly["pnl"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=monthly.index, y=monthly["pnl"],
                         marker_color=colors,
                         text=monthly["pnl"].apply(lambda x: f"${x:+,.0f}"),
                         textposition="outside",
                         hovertemplate="%{x|%b %Y}<br>P&L: $%{y:+,.2f}<br>Trades: %{customdata}<extra></extra>",
                         customdata=monthly["count"], name="Monthly P&L"))
    fig.add_hline(y=0, line=dict(color=TEXT_MUTED, dash="dash"))
    fig.update_layout(title="Monthly P&L", height=320, template=dark_template(),
                      margin=dict(l=40, r=20, t=50, b=40), yaxis_title="P&L ($)")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def exit_reason_pie(all_trades: list[Trade]) -> str:
    if not all_trades:
        return "<p class='muted'>No trades.</p>"
    tp = sum(1 for t in all_trades if t.exit_reason in {"take_profit", "tp1", "tp2", "tp3"})
    sl = sum(1 for t in all_trades if t.exit_reason in {"stop_loss", "gap_stop"})
    ts = sum(1 for t in all_trades if t.exit_reason == "time_stop")
    cross = sum(1 for t in all_trades if t.exit_reason == "sma_cross_down")
    fig = go.Figure()
    fig.add_trace(go.Pie(labels=["Take Profit", "Stop Loss", "SMA Cross", "Time Stop"],
                         values=[tp, sl, cross, ts],
                         marker_colors=[PROFIT_COLOR, LOSS_COLOR, "#38bdf8", "#f59e0b"],
                         textinfo="label+percent", hole=0.4))
    fig.update_layout(title="Exit Reason Distribution", height=320,
                      template=dark_template(), margin=dict(l=20, r=20, t=50, b=20))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def ticker_chart(ticker: str, df: pd.DataFrame, trades: list[Trade]) -> str:
    df = add_indicators(df)
    view = df.loc[df.index >= pd.Timestamp(BACKTEST_START)]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3], vertical_spacing=0.05,
                        subplot_titles=("Price", "RSI(14)"))
    fig.add_trace(go.Candlestick(x=view.index, open=view["open"], high=view["high"],
                                 low=view["low"], close=view["close"], name="OHLC",
                                 increasing_line_color=PROFIT_COLOR, decreasing_line_color=LOSS_COLOR,
                                 showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=view.index, y=view["sma_fast"], name=f"SMA{PARAMS.sma_fast}",
                             line=dict(color="#f59e0b", width=1.2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=view.index, y=view["sma_slow"], name=f"SMA{PARAMS.sma_slow}",
                             line=dict(color="#8b5cf6", width=1.2)), row=1, col=1)

    strategy_colors_map = {"trend_pullback": "#3b82f6", "breakout": "#f59e0b",
                           "mean_reversion": "#8b5cf6", "momentum_macd": "#34d399",
                           "ensemble": "#f472b6", "regime": "#fb923c",
                           "sma_50_cross": "#38bdf8"}
    for t in trades:
        color = strategy_colors_map.get(t.strategy, PROFIT_COLOR if t.pnl_dollars > 0 else LOSS_COLOR)
        fig.add_trace(go.Scatter(x=[t.entry_date], y=[t.entry_price], mode="markers",
                                 marker=dict(symbol="triangle-up", size=10, color=color,
                                             line=dict(color="white", width=0.8)),
                                 name=t.strategy, showlegend=False,
                                 hovertext=f"ENTRY {t.ticker}<br>${t.entry_price:.2f}"), row=1, col=1)
        fig.add_trace(go.Scatter(x=[t.exit_date], y=[t.exit_price], mode="markers",
                                 marker=dict(symbol="x", size=9, color=color,
                                             line=dict(color="white", width=0.8)),
                                 name="Exit", showlegend=False,
                                 hovertext=f"EXIT ({t.exit_reason})<br>P&L ${t.pnl_dollars:+.2f}"), row=1, col=1)

    fig.add_trace(go.Scatter(x=view.index, y=view["rsi"], name="RSI",
                             line=dict(color=PRICE_COLOR, width=1.2), showlegend=False), row=2, col=1)
    fig.add_hline(y=PARAMS.rsi_pullback_max, line=dict(color="#9ca3af", dash="dash"), row=2, col=1)
    fig.add_hline(y=70, line=dict(color="#9ca3af", dash="dash"), row=2, col=1)
    fig.update_layout(title=f"{ticker} — {len(trades)} trade(s)", height=500,
                      margin=dict(l=40, r=20, t=60, b=40), xaxis_rangeslider_visible=False,
                      template=dark_template(), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=2, col=1)
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ── HTML helpers ──────────────────────────────────────────────────────────────

def fmt_money(x: float) -> str: return f"${x:+,.2f}" if x else "$0.00"
def fmt_pct(x: float) -> str: return f"{x*100:+.2f}%"

CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background: #0f1117; color: #e1e7ef; min-height: 100vh; }
.container { max-width: 1440px; margin: 0 auto; padding: 20px 24px; }
.header { background: linear-gradient(135deg, #1a1d27, #0f1117); border-bottom: 1px solid #2a2d35; padding: 18px 28px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.3px; display: flex; align-items: center; gap: 10px; }
.header h1 span.badge { font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; background: #22c55e20; color: #22c55e; border: 1px solid #22c55e40; }
.header .subtitle { font-size: 13px; color: #8892a4; }
.strategy-tabs { display: flex; gap: 6px; margin: 16px 0; flex-wrap: wrap; }
.strategy-tab { padding: 8px 18px; border-radius: 8px; border: 1px solid #2a2d35; background: #1a1d27; color: #8892a4; font-size: 13px; font-weight: 600; cursor: default; }
.strategy-tab.active { border-color: #60a5fa; color: #e1e7ef; background: #1a1d2740; }
h2 { font-size: 18px; font-weight: 600; color: #c8cedb; margin: 28px 0 12px; }
h3 { font-size: 16px; font-weight: 600; color: #c8cedb; margin: 0 0 4px; }
.muted { color: #6b7280; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }
.kpi { background: #1a1d27; border: 1px solid #2a2d35; border-radius: 12px; padding: 16px 18px; transition: .15s; }
.kpi:hover { border-color: #3b3f4b; }
.kpi .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: #6b7280; margin-bottom: 4px; }
.kpi .value { font-size: 24px; font-weight: 700; }
.kpi .sub { font-size: 12px; color: #6b7280; margin-top: 2px; }
.pos { color: #22c55e; }
.neg { color: #ef4444; }
.neu { color: #f59e0b; }
.params { font-size: 13px; color: #8892a4; margin-bottom: 16px; padding: 12px 16px; background: #1a1d27; border: 1px solid #2a2d35; border-radius: 10px; }
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.chart-grid .full { grid-column: 1 / -1; }
.chart-wrap { background: #1a1d27; border: 1px solid #2a2d35; border-radius: 12px; padding: 12px; }
.strategy-section { background: #1a1d27; border: 1px solid #2a2d35; border-radius: 12px; padding: 16px; margin: 16px 0; border-left: 3px solid #60a5fa; }
.strategy-section h3 { display: flex; align-items: center; gap: 8px; }
.strategy-section h3 .color-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.ticker-section { background: #1a1d27; border: 1px solid #2a2d35; border-radius: 12px; padding: 16px; margin-top: 16px; }
.ticker-section summary { cursor: pointer; color: #60a5fa; font-size: 13px; padding: 6px 0; user-select: none; }
.ticker-section summary:hover { text-decoration: underline; }
.table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #2a2d35; background: #161822; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #1a1d27; color: #8892a4; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.04em; padding: 10px 14px; text-align: left; border-bottom: 1px solid #2a2d35; white-space: nowrap; }
td { padding: 10px 14px; border-bottom: 1px solid #252830; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #1e212b; }
.ticker-badge { font-weight: 700; color: #93c5fd; }
.strategy-row { display: flex; gap: 12px; flex-wrap: wrap; }
.strategy-kpi { flex: 1; min-width: 160px; background: #161822; border: 1px solid #2a2d35; border-radius: 8px; padding: 12px 14px; }
.strategy-kpi .label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; color: #6b7280; }
.strategy-kpi .value { font-size: 20px; font-weight: 700; margin-top: 2px; }
.strategy-kpi .sub { font-size: 11px; color: #6b7280; }
@media(max-width: 768px) { .container { padding: 12px; } .kpis { grid-template-columns: repeat(2, 1fr); } .chart-grid { grid-template-columns: 1fr; } }
"""


def render_summary_table(stats_rows: list[dict]) -> str:
    headers = ["Ticker", "Trades", "Wins", "Losses", "Win %", "Total P&L", "Avg %", "Best %", "Worst %", "Avg Held"]
    rows = []
    for s in stats_rows:
        pnl_cls = "pos" if s["total_pnl"] > 0 else ("neg" if s["total_pnl"] < 0 else "")
        rows.append(f"<tr><td class='ticker-badge'>{s['ticker']}</td><td>{s['trades']}</td><td>{s['wins']}</td>"
                    f"<td>{s['losses']}</td><td>{s['win_rate']*100:.1f}%</td>"
                    f"<td class='{pnl_cls}'>{fmt_money(s['total_pnl'])}</td><td>{fmt_pct(s['avg_pnl_pct'])}</td>"
                    f"<td>{fmt_pct(s['best_pct'])}</td><td>{fmt_pct(s['worst_pct'])}</td>"
                    f"<td>{s['avg_bars_held']:.1f}</td></tr>")
    return f"<div class='table-wrap'><table><thead><tr>{''.join(f'<th>{h}</th>' for h in headers)}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def render_trades_table(trades: list[Trade]) -> str:
    if not trades:
        return "<p class='muted'>No trades.</p>"
    rows = []
    for t in sorted(trades, key=lambda x: x.entry_date):
        cls = "pos" if t.pnl_dollars > 0 else "neg"
        target = f"${t.take_profit:.2f}" if t.take_profit > 0 else "—"
        rows.append(f"<tr><td>{t.entry_date.strftime('%Y-%m-%d')}</td><td>{t.exit_date.strftime('%Y-%m-%d')}</td>"
                    f"<td>${t.entry_price:.2f}</td><td>${t.exit_price:.2f}</td><td>${t.stop_loss:.2f}</td>"
                    f"<td>{target}</td><td>{t.bars_held}d</td>"
                    f"<td>{t.exit_reason.replace('_', ' ')}</td><td style='font-size:11px'>{t.strategy}</td>"
                    f"<td class='{cls}'>{fmt_money(t.pnl_dollars)}</td><td class='{cls}'>{fmt_pct(t.pnl_pct)}</td></tr>")
    return ("<div class='table-wrap'><table><thead><tr><th>Entry</th><th>Exit</th><th>Entry $</th><th>Exit $</th>"
            "<th>Stop</th><th>Target</th><th>Held</th><th>Reason</th><th>Strategy</th><th>P&L $</th><th>P&L %</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def render_strategy_comparison_table(all_strategy_results: dict) -> str:
    headers = ["Strategy", "Trades", "Wins", "Losses", "Win %", "Total P&L", "Avg %",
               "Avg Win %", "Avg Loss %", "Max DD %", "PF", "ROI on Cap"]
    rows = []
    for sname, sdata in sorted(all_strategy_results.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        s = sdata
        pnl_cls = "pos" if s["total_pnl"] > 0 else ("neg" if s["total_pnl"] < 0 else "")
        color = STRATEGY_COLORS.get(sname, "#8892a4")
        pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
        rows.append(f"<tr><td style='color:{color};font-weight:700'>{sname.replace('_', ' ').title()}</td>"
                    f"<td>{s['trades']}</td><td>{s['wins']}</td><td>{s['losses']}</td>"
                    f"<td>{s['win_rate']*100:.1f}%</td>"
                    f"<td class='{pnl_cls}'>{fmt_money(s['total_pnl'])}</td><td>{fmt_pct(s['avg_pnl_pct'])}</td>"
                    f"<td>{s['avg_win_pct']:.1f}%</td><td>{s['avg_loss_pct']:.1f}%</td>"
                    f"<td class='neg'>{fmt_pct(s.get('max_drawdown_pct', 0))}</td><td>{pf_str}</td>"
                    f"<td class='{pnl_cls}'>{fmt_pct(s.get('roi_on_cap', 0))}</td></tr>")
    return f"<div class='table-wrap'><table><thead><tr>{''.join(f'<th>{h}</th>' for h in headers)}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def strategy_kpi_cards(strat_name: str, s: dict) -> str:
    color = STRATEGY_COLORS.get(strat_name, "#8892a4")
    pnl_cls = "pos" if s["total_pnl"] > 0 else ("neg" if s["total_pnl"] < 0 else "")
    pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
    return f"""<div class="strategy-section" style="border-left: 3px solid {color}">
      <h3><span class="color-dot" style="background:{color}"></span>{strat_name.replace('_', ' ').title()}</h3>
      <div class="strategy-row">
        <div class="strategy-kpi"><div class="label">Trades</div><div class="value">{s['trades']}</div><div class="sub">{s['wins']}W / {s['losses']}L</div></div>
        <div class="strategy-kpi"><div class="label">Win Rate</div><div class="value">{s['win_rate']*100:.1f}%</div><div class="sub">Avg win {s['avg_win_pct']:.1f}% · Avg loss {s['avg_loss_pct']:.1f}%</div></div>
        <div class="strategy-kpi"><div class="label">Total P&amp;L</div><div class="value {pnl_cls}">{fmt_money(s['total_pnl'])}</div><div class="sub">Avg {fmt_pct(s['avg_pnl_pct'])} per trade</div></div>
        <div class="strategy-kpi"><div class="label">Profit Factor</div><div class="value">{pf_str}</div><div class="sub">Avg held {s['avg_bars_held']:.1f}d</div></div>
        <div class="strategy-kpi"><div class="label">Exit Split</div><div class="value" style="font-size:14px">TP {s['tp_count']} · SL {s['sl_count']} · Cross {s.get('signal_count', 0)} · Time {s['time_count']}</div></div>
        <div class="strategy-kpi"><div class="label">Best / Worst</div><div class="value" style="font-size:16px">{fmt_pct(s['best_pct'])} / {fmt_pct(s['worst_pct'])}</div><div class="sub">Max DD {fmt_pct(s.get('max_drawdown_pct', 0))}</div></div>
      </div></div>"""


def params_html_for_strategy(strat: StrategyType) -> str:
    p = PARAMS
    if strat == StrategyType.TREND_PULLBACK:
        return (f"<strong>Trend Pullback:</strong> ${p.dollars_per_trade:.0f}/trade · SL {p.stop_loss_pct*100:.0f}% · "
                f"TP {p.atr_tp_multiple}×ATR [{p.take_profit_floor_pct*100:.0f}%, {p.take_profit_cap_pct*100:.0f}%] · "
                f"time stop if breakeven+")
    elif strat == StrategyType.BREAKOUT:
        return (f"<strong>Breakout:</strong> ${p.dollars_per_trade:.0f}/trade · SL {p.breakout_stop_loss_pct*100:.0f}% · "
                f"TP {p.breakout_atr_multiple}×ATR [{p.breakout_tp_floor_pct*100:.0f}%, {p.breakout_tp_cap_pct*100:.0f}%] · "
                f"time stop if breakeven+")
    elif strat == StrategyType.MOMENTUM_MACD:
        return (f"<strong>MACD Momentum:</strong> ${p.dollars_per_trade:.0f}/trade · SL {p.macd_stop_loss_pct*100:.0f}% · "
                f"TP {p.macd_tp_multiple}×ATR [{p.macd_tp_floor_pct*100:.0f}%, {p.macd_tp_cap_pct*100:.0f}%] · "
                f"MACD cross + RSI momentum · time stop if breakeven+")
    elif strat == StrategyType.ENSEMBLE:
        return (f"<strong>Ensemble:</strong> ${p.dollars_per_trade:.0f}/trade · SL {p.ensemble_stop_loss_pct*100:.0f}% · "
                f"TP {p.ensemble_tp_multiple}×ATR [{p.ensemble_tp_floor_pct*100:.0f}%, {p.ensemble_tp_cap_pct*100:.0f}%] · "
                f"Weighted vote of all 5 strategies · time stop if breakeven+")
    elif strat == StrategyType.REGIME_ADAPTIVE:
        return (f"<strong>Regime Adaptive:</strong> ${p.dollars_per_trade:.0f}/trade · "
                f"SL {p.stop_loss_pct*100:.0f}% · TP {p.atr_tp_multiple}×ATR · "
                f"Risk-on/off via EMA cross")
    elif strat == StrategyType.SMA_50_CROSS:
        return (f"<strong>SMA 50 Cross:</strong> ${p.dollars_per_trade:.0f}/trade · "
                f"Daily close vs SMA(50) · SL {p.sma_cross_stop_loss_pct*100:.0f}% · "
                "No take profit · exit on cross below")
    else:
        return (f"<strong>Mean Reversion:</strong> ${p.dollars_per_trade:.0f}/trade · "
                f"SL {p.mr_stop_loss_pct*100:.0f}% · TP {p.mr_atr_multiple}×ATR "
                f"[{p.mr_tp_floor_pct*100:.0f}%, {p.mr_tp_cap_pct*100:.0f}%] · time stop if breakeven+")


def build_report_2025(strategy_results: dict, per_strategy_details: dict, overall_best: str) -> str:
    """Build the full HTML report for 2025 backtest."""
    strat_sections = []
    for sname, sdata in sorted(strategy_results.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        sdata["max_drawdown_pct"] = compute_max_drawdown(sdata.get("_trades", []))
        sdata["roi_on_cap"] = sdata["total_pnl"] / PARAMS.max_concurrent_capital if PARAMS.max_concurrent_capital else 0.0
        strat_sections.append(strategy_kpi_cards(sname, sdata))

    comparison_table = render_strategy_comparison_table(strategy_results)

    # Per-ticker detail sections
    ticker_sections = []
    for tk in TICKERS:
        tk_trades = []
        for sname in sorted(per_strategy_details.keys()):
            per_tk = per_strategy_details.get(sname, {}).get(tk, (pd.DataFrame(), []))
            tk_trades.extend(per_tk[1])

        if not tk_trades:
            continue

        # Combine all trades for equity
        combined_equity = strategy_comparison_equity(
            {sname: per_strategy_details.get(sname, {}).get(tk, (pd.DataFrame(), []))[1]
             for sname in sorted(per_strategy_details.keys())}
        )
        tk_chart_html = ticker_chart(tk, per_strategy_details.get(list(per_strategy_details.keys())[0], {}).get(tk, (pd.DataFrame(), []))[0], tk_trades)
        tk_stats = per_ticker_stats(tk, tk_trades)

        # Per-strategy breakdown for this ticker
        strat_rows = []
        for sname in sorted(per_strategy_details.keys()):
            per_tk = per_strategy_details.get(sname, {}).get(tk, (pd.DataFrame(), []))
            s_trades = per_tk[1]
            if not s_trades:
                continue
            s_pnl = sum(t.pnl_dollars for t in s_trades)
            s_wr = sum(1 for t in s_trades if t.pnl_dollars > 0) / len(s_trades) * 100
            s_count = len(s_trades)
            color = STRATEGY_COLORS.get(sname, "#8892a4")
            pnl_cls = "pos" if s_pnl > 0 else "neg"
            strat_rows.append(f"<tr><td style='color:{color}'>{sname.replace('_', ' ').title()}</td>"
                              f"<td>{s_count}</td><td>{s_wr:.0f}%</td><td class='{pnl_cls}'>{fmt_money(s_pnl)}</td></tr>")

        strat_table_html = ""
        if strat_rows:
            strat_table_html = ("<div class='table-wrap' style='margin-top:12px'><table><thead>"
                                "<tr><th>Strategy</th><th>Trades</th><th>Win %</th><th>P&L</th></tr></thead>"
                                f"<tbody>{''.join(strat_rows)}</tbody></table></div>")

        tk_stats_kpis = (f"<div class='kpis' style='margin:8px 0'>"
                         f"<div class='kpi'><div class='label'>Trades</div><div class='value'>{tk_stats['trades']}</div></div>"
                         f"<div class='kpi'><div class='label'>Win %</div><div class='value'>{tk_stats['win_rate']*100:.0f}%</div></div>"
                         f"<div class='kpi'><div class='label'>P&L</div><div class='value {'pos' if tk_stats['total_pnl'] > 0 else 'neg'}'>{fmt_money(tk_stats['total_pnl'])}</div></div>"
                         f"</div>")

        ticker_sections.append(f"<details class='ticker-section'><summary>{tk} — {len(tk_trades)} trades, "
                               f"{fmt_money(sum(t.pnl_dollars for t in tk_trades))}</summary>"
                               f"{tk_stats_kpis}"
                               f"<div class='chart-grid'>"
                               f"<div class='chart-wrap full'>{tk_chart_html}</div>"
                               f"<div class='chart-wrap full'>{combined_equity}</div>"
                               f"</div>"
                               f"{strat_table_html}"
                               f"</details>")

    # Comparison equity curve (all strategies combined)
    all_equity_chart = strategy_comparison_equity(
        {sname: sdata.get("_trades", []) for sname, sdata in strategy_results.items()}
    )

    # Combined trades table
    all_trades = []
    for sname in sorted(strategy_results.keys()):
        all_trades.extend(strategy_results[sname].get("_trades", []))
    trades_table_html = render_trades_table(all_trades)

    # Best strategy badge
    best_color = STRATEGY_COLORS.get(overall_best, "#60a5fa") if overall_best else "#60a5fa"

    from datetime import datetime
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>{CSS}</style><title>Alpaca Swing Bot V2 — 2025 Backtest</title></head><body>
<div class="header"><h1>📊 Alpaca Swing Bot V2<span class="badge">2025 Backtest</span></h1>
<div class="subtitle">{len(strategy_results)} strategies · {len(TICKERS)} tickers · real market data</div></div>
<div class="container">
  <h2>Strategy Comparison</h2>
  {comparison_table}
  <h2>Best: <span style="color:{best_color}">{overall_best.replace('_', ' ').title() if overall_best else 'N/A'}</span></h2>
  <div class="chart-grid">
    <div class="chart-wrap full">{all_equity_chart}</div>
  </div>
  <h2>Per-Strategy Details</h2>
  {"".join(strat_sections)}
  <h2>All Trades</h2>
  {trades_table_html}
  <h2>Ticker Detail</h2>
  {"".join(ticker_sections) if ticker_sections else "<p class='muted'>No trades for any ticker.</p>"}
  <div class="params" style="margin-top:20px">
    <strong>System:</strong> ALIENWARE 16 · RTX 5050 4GB · yfinance live data · Generated {now_str}
  </div>
</div></body></html>"""
    return html
