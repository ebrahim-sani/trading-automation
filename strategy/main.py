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
            # Forex & Metals
            "EURUSDm", "GBPUSDm", "XAUUSDm",
            "USDJPYm", "USDCADm",
            "EURJPYm", "GBPJPYm",
            "XAGUSDm",
            # Crypto
            "BTCUSDm", "ETHUSDm",
            # Indices (Nasdaq, S&P, Dow, DAX)
            "USTECm", "US500m", "US30m", "DE30m",
            # Stocks
            "AAPLm", "TSLAm", "NVDAm", "MSFTm", "AMZNm"
        ],
        timeframe_entry       = "M5",
        left_bars             = 4,       # Faster Pivot detection
        right_bars            = 4,       
        min_rr                = 2.5,     # Risk Reward
        min_score             = 70,      # More frequent execution (70/100)
        risk_usd              = 5.0,
        trade_timeout_minutes = 60,
        max_open_trades       = 6,
        max_pivot_bars        = 288,     # 24-hour memory (on M5)
        # ── Timezone / Session Target ──
        # Important for Nigerian (WAT) & UK traders: 
        # MT5 timestamps are in your BROKER's timezone. Your specific broker is using exactly UTC (Lagos Time - 1).
        # 07:30 MT5 Broker Time = 08:30 Lagos Time
        # 19:00 MT5 Broker Time = 20:00 Lagos Time
        session_start_hour    = 7.5,      # 07:30 Broker Time (Starts exactly at 08:30 Nigerian Time)
        session_end_hour      = 22.0,     # 22:00 Broker Time (NY Close)

        max_weekly_loss_usd   = 40.0,    # 8× risk — halt for the week
        max_consecutive_loss  = 2,       # Cool down after 2 straight losses
        cooldown_hours        = 2,       # Cool down for 2 hours
    ).run()