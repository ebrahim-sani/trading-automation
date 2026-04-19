"""
TTFM Backtest Diagnostic

Runs a single symbol through every filter gate and reports how many bars
are blocked at each step.  Run this BEFORE the full backtest to pinpoint bugs.

Usage:
    python diagnose.py
    python diagnose.py --symbol XAUUSDm --days 30
"""

import sys, os, logging, argparse

# Force UTF-8 output on Windows so box-drawing chars don't crash
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from backtest_engine import (
    _get_atr, _last_pivot_high, _last_pivot_low,
    _ema, _compute_adx,
)

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("Diagnose")


def fetch_bars(symbol, tf, days):
    utc_to   = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=days)
    bars = mt5.copy_rates_range(symbol, tf, utc_from, utc_to)
    if bars is None or len(bars) == 0:
        log.error(f"No data: {mt5.last_error()}")
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    
    s_info = mt5.symbol_info(symbol)
    if s_info is not None and "spread" in df.columns:
        df["spread"] = df["spread"] * s_info.point
        
    return df


def diagnose(symbol: str, days: int, min_score: int,
             session_start: float, session_end: float):

    log.info(f"Fetching data for {symbol}...")
    df_m5 = fetch_bars(symbol, mt5.TIMEFRAME_M5, days)
    df_h1 = fetch_bars(symbol, mt5.TIMEFRAME_H1, days + 30)
    df_h4 = fetch_bars(symbol, mt5.TIMEFRAME_H4, days + 60)

    if df_m5.empty:
        log.error("No M5 data — aborting")
        return

    log.info(f"M5 bars: {len(df_m5)}  |  H1: {len(df_h1)}  |  H4: {len(df_h4)}")
    log.info(f"M5 range: {df_m5.index[0]}  →  {df_m5.index[-1]}")
    # Auto-scale min_score the same way the engine does (100/140 ratio)
    scaled_min_score = max(1, round(min_score * 100 / 140))
    log.info(f"Session filter: {session_start}-{session_end} UTC decimal hour")
    log.info(f"Score threshold: live {min_score}/140 -> backtest {scaled_min_score}/100")

    opens  = df_m5["open"].values
    highs  = df_m5["high"].values
    lows   = df_m5["low"].values
    closes = df_m5["close"].values
    vols   = df_m5["volume"].values if "volume" in df_m5.columns else np.ones(len(df_m5))
    times  = df_m5.index

    # ── Counters ────────────────────────────────────────────────────────────
    n_total          = 0
    n_out_session    = 0
    n_spike_kill     = 0
    n_no_pivot       = 0
    n_sweep_zero     = 0
    n_trend_zero     = 0
    n_score_too_low  = 0
    n_signals        = 0

    # Track sample scores when sweep fires
    sweep_scores_bull = []
    sweep_scores_bear = []
    score_samples     = []   # (bar_time, bull, bear, sweep_b, sweep_r, trend_b, trend_r)

    min_bars = 8 + 8 + 50  # left + right + buffer
    p_top_liq = p_bot_liq = None
    p_top_idx = p_bot_idx = 0

    LEFT, RIGHT, CAP = 8, 8, 120

    def htf_bias(m5_bar_idx):
        bar_time = times[m5_bar_idx]
        for df_htf, label in [(df_h1, "H1"), (df_h4, "H4")]:
            subset = df_htf[df_htf.index <= bar_time]
            if len(subset) < 201:
                return "UNKNOWN", "UNKNOWN"
        c1 = df_h1[df_h1.index <= bar_time]["close"].values
        c4 = df_h4[df_h4.index <= bar_time]["close"].values
        e1 = _ema(c1, 200)
        e4 = _ema(c4, 200)
        bias1 = "BULLISH" if c1[-1] > e1[-1] else "BEARISH"
        bias4 = "BULLISH" if c4[-1] > e4[-1] else "BEARISH"
        return bias1, bias4

    log.info("Scanning bars...")

    for i in range(min_bars, len(df_m5) - 1):
        n_total += 1

        # ── Session filter ───────────────────────────────────────────────
        bar_time    = times[i]
        decimal_hr  = bar_time.hour + bar_time.minute / 60.0
        in_session  = (session_start <= decimal_hr < session_end)
        if not in_session:
            n_out_session += 1
            continue

        sl_h = highs[:i]
        sl_l = lows[:i]
        sl_c = closes[:i]
        sl_o = opens[:i]
        sl_v = vols[:i]

        # ── Spike filter ────────────────────────────────────────────────
        atr = _get_atr(sl_h[-50:], sl_l[-50:], sl_c[-50:], 14)
        if atr <= 0:
            continue
        recent_range = float(np.max(sl_h[-10:] - sl_l[-10:]))
        if recent_range > atr * 2.5:
            n_spike_kill += 1
            continue

        # ── Pivot detection ─────────────────────────────────────────────
        new_high, nh_idx = _last_pivot_high(sl_h, LEFT, RIGHT, CAP)
        new_low,  nl_idx = _last_pivot_low(sl_l, LEFT, RIGHT, CAP)

        if new_high is not None:
            p_top_liq = new_high
            p_top_idx = nh_idx
        if new_low is not None:
            p_bot_liq = new_low
            p_bot_idx = nl_idx

        if p_top_liq is None or p_bot_liq is None:
            n_no_pivot += 1
            continue

        top_age = (i - 1) - p_top_idx
        bot_age = (i - 1) - p_bot_idx

        open_c  = float(sl_o[-1])
        high_c  = float(sl_h[-1])
        low_c   = float(sl_l[-1])
        close_c = float(sl_c[-1])
        vol_c   = float(sl_v[-1])

        # ── Sweep check ─────────────────────────────────────────────────
        bull_age_mult = 1.0 - (min(bot_age, 80) / 160.0)
        bear_age_mult = 1.0 - (min(top_age, 80) / 160.0)

        bull_is_sweep = (low_c < p_bot_liq and close_c > p_bot_liq)
        bear_is_sweep = (high_c > p_top_liq and close_c < p_top_liq)

        sweep_bull = int(20 * bull_age_mult) if bull_is_sweep else 0
        sweep_bear = int(20 * bear_age_mult) if bear_is_sweep else 0

        if sweep_bull == 0 and sweep_bear == 0:
            n_sweep_zero += 1
            continue

        # ── Trend check ──────────────────────────────────────────────
        bias_1h, bias_4h = htf_bias(i)
        adx = _compute_adx(sl_h, sl_l, sl_c, 14)
        trend_strength = max(0.0, min(1.0, (adx - 20) / 20.0))
        trend_pts = int(10 + (10 * trend_strength))

        trend_bull = trend_pts if (bias_1h == "BULLISH" and bias_4h == "BULLISH") else 0
        trend_bear = trend_pts if (bias_1h == "BEARISH" and bias_4h == "BEARISH") else 0

        # ── Displacement ────────────────────────────────────────────────
        body_frac = abs(close_c - open_c) / (high_c - low_c) if (high_c - low_c) > 0 else 0
        disp_pts  = int(10 + min(1.0, (body_frac - 0.5) / 0.4) * 10) if body_frac > 0.5 else 0
        disp_bull = disp_pts if close_c > open_c else 0
        disp_bear = disp_pts if close_c < open_c else 0

        # ── Volume spike ────────────────────────────────────────────────
        avg_vol    = float(np.mean(sl_v[-22:-1])) if len(sl_v) > 22 else 1.0
        spike_ratio = vol_c / avg_vol if avg_vol > 0 else 1.0
        volm_score = int(10 + min(1.0, (spike_ratio - 1.5) / 1.5) * 10) if spike_ratio > 1.5 else 0

        total_bull = trend_bull + sweep_bull + disp_bull + volm_score
        total_bear = trend_bear + sweep_bear + disp_bear + volm_score

        score_samples.append({
            "time": str(bar_time)[:16],
            "bias_1h": bias_1h, "bias_4h": bias_4h,
            "adx": round(adx, 1),
            "trend_bull": trend_bull, "trend_bear": trend_bear,
            "sweep_bull": sweep_bull, "sweep_bear": sweep_bear,
            "disp_bull": disp_bull, "disp_bear": disp_bear,
            "volm": volm_score,
            "total_bull": total_bull, "total_bear": total_bear,
            "bot_liq": round(p_bot_liq, 5), "top_liq": round(p_top_liq, 5),
            "low_c": round(low_c, 5), "high_c": round(high_c, 5),
        })

        if sweep_bull > 0: sweep_scores_bull.append(total_bull)
        if sweep_bear > 0: sweep_scores_bear.append(total_bear)

        if trend_bull == 0 and trend_bear == 0:
            n_trend_zero += 1

        if total_bull < scaled_min_score and total_bear < scaled_min_score:
            n_score_too_low += 1
            continue

        n_signals += 1

    # ─────────────────────────────────────────────────────────────────────
    print("\n" + "═"*65)
    print(f"  DIAGNOSIS REPORT — {symbol}")
    print("═"*65)
    print(f"  Total bars scanned      : {n_total:>7,}")
    print(f"  ❌ Out of session        : {n_out_session:>7,}  ({n_out_session/n_total*100:.1f}%)")
    print(f"  ❌ Spike filter kill     : {n_spike_kill:>7,}")
    print(f"  ❌ No pivot found yet    : {n_no_pivot:>7,}")
    print(f"  ❌ No sweep on this bar  : {n_sweep_zero:>7,}")
    print(f"  ──────────────────────────────────────────────────────────")
    print(f"  ✅ Bars with sweep fired : {len(score_samples):>7,}")
    print(f"       └ Trend score = 0  : {n_trend_zero:>7,}  (HTF bias mis-aligned or UNKNOWN)")
    print(f"       └ Score too low    : {n_score_too_low:>7,}  (below scaled threshold={scaled_min_score}/100)")
    print(f"  ✅ Signals generated     : {n_signals:>7,}")
    print("="*65)

    if sweep_scores_bull:
        print(f"\n  Bull sweep score stats (n={len(sweep_scores_bull)}):")
        print(f"    mean={np.mean(sweep_scores_bull):.1f}  max={np.max(sweep_scores_bull)}  min={np.min(sweep_scores_bull)}")
    if sweep_scores_bear:
        print(f"\n  Bear sweep score stats (n={len(sweep_scores_bear)}):")
        print(f"    mean={np.mean(sweep_scores_bear):.1f}  max={np.max(sweep_scores_bear)}  min={np.min(sweep_scores_bear)}")

    if score_samples:
        print(f"\n  Last 10 sweep bars:")
        print(f"  {'Time':<17} {'1H':<8} {'4H':<8} {'ADX':>5}  {'TrB':>4} {'TrR':>4}  {'SwB':>4} {'SwR':>4}  {'TotB':>5} {'TotR':>5}")
        for s in score_samples[-10:]:
            print(f"  {s['time']:<17} {s['bias_1h']:<8} {s['bias_4h']:<8} {s['adx']:>5}  "
                  f"{s['trend_bull']:>4} {s['trend_bear']:>4}  "
                  f"{s['sweep_bull']:>4} {s['sweep_bear']:>4}  "
                  f"{s['total_bull']:>5} {s['total_bear']:>5}")

    print()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="EURUSDm")
    p.add_argument("--days",   type=int, default=30)
    p.add_argument("--min-score", type=int, default=75)
    p.add_argument("--session-start", type=float, default=7.5)
    p.add_argument("--session-end",   type=float, default=19.0)
    args = p.parse_args()

    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    mt5.symbol_select(args.symbol, True)
    diagnose(args.symbol, args.days, args.min_score,
             args.session_start, args.session_end)
    mt5.shutdown()


if __name__ == "__main__":
    main()
