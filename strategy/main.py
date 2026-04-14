#!/usr/bin/env python3
"""
TTFM Alpha Combiner [v7]
─────────────────────────────────────────────────────────────────────
5-Factor Scoring Engine — execution fires when score >= minScore AND
sweep is present (sweep is always mandatory).

Factor               Points  Logic
──────────────────── ──────  ──────────────────────────────────────────
1. Macro Trend          20   EMA(200) on H1 + H4 both agree
2. Sweep / Reversion    20   low < botLiq and close > botLiq (longs)
3. Displacement         20   body > 50% of candle range
4. ATR Expansion        20   currentATR(14) > SMA(ATR, 10)
5. Volume Spike         20   volume > SMA(vol, 20) × 1.5

Run on the same Windows machine as MT5.
Install: pip install MetaTrader5 numpy requests
"""
import logging
from engine import StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    StrategyEngine(
        symbols = [
            "EURUSDm", "GBPUSDm", "XAUUSDm",
            "USDJPYm", "USDCADm",
            "EURJPYm", "GBPJPYm",
            "XAGUSDm",
            "BTCUSDm", "ETHUSDm",
        ],
        timeframe_entry       = "M5",
        left_bars             = 8,       # Pine: leftBars
        right_bars            = 8,       # Pine: rightBars
        min_rr                = 2.5,     # Pine: minRR
        min_score             = 80,      # Pine: minScore
        risk_usd              = 5.0,
        trade_timeout_minutes = 60,
        max_open_trades       = 6,
        max_pivot_bars        = 120,
        # ── Timezone / Session Target ──
        # Important for Nigerian (WAT) & UK traders: 
        # MT5 timestamps are in your BROKER's timezone (usually UTC+2 / EET), not local Lagos time!
        # 10:00 MT5 Broker Time = 08:00 London Time = 09:00 Lagos Time
        # 19:00 MT5 Broker Time = 17:00 London Time = 18:00 Lagos Time
        session_start_hour    = 10,       # 10:00 Broker Time (London Open)
        session_end_hour      = 19,       # 19:00 Broker Time (London Close)

        max_daily_loss_usd    = 15.0,    # 3× risk — halt for the day
        max_weekly_loss_usd   = 40.0,    # 8× risk — halt for the week
        max_consecutive_loss  = 2,       # Cool down after 2 straight losses
        cooldown_hours        = 2,       # Cool down for 2 hours
    ).run()